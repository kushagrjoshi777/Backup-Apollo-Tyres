from __future__ import annotations

import mmap
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

import numpy as np

@dataclass
class CalibrationResult:
    """Result of calibrating a single raw XSN frame."""
    calibrated_psi: np.ndarray      # float32 PSI matrix
    raw_frame: np.ndarray           # original uint16 for tracing
    invalid_mask: np.ndarray        # bool: NaN, inf, negative raw
    saturation_mask: np.ndarray     # bool: raw >= 65530
    calibration_method: str         # e.g. "EMPIRICAL_FALLBACK_RAW_DIV_128"
    pressure_range: str             # e.g. "TBR"
    pressure_ceiling: float         # e.g. 220.0
    confidence: str                 # VERIFIED|METADATA-DERIVED|EMPIRICAL|PROVISIONAL|UNKNOWN

PRESSURE_RANGES = {
    "PCR": 100.0,
    "TBR": 220.0,
}


MAGIC = b"\xFE\xCA\xAD\xDE"

ROWS = 256
COLS = 256

HEADER_SIZE = 48
TRAILER_SIZE = 44

FRAME_SIZE = HEADER_SIZE + (ROWS * COLS * 2) + TRAILER_SIZE


@dataclass(frozen=True)
class XSNFrameInfo:
    number: int
    offset: int
    timestamp: int


class XSNReader:

    def __init__(self, filepath):

        self.filepath = Path(filepath)

        self.file = self.filepath.open("rb")

        # Memory-map the file.
        # This prevents loading a 300+ MB XSN completely into RAM.
        self.mm = mmap.mmap(
            self.file.fileno(),
            0,
            access=mmap.ACCESS_READ
        )

        self.size = self.mm.size()

        self.frames = self._find_frames()


    def close(self):

        self.mm.close()
        self.file.close()


    def __enter__(self):

        return self


    def __exit__(self, exc_type, exc_value, traceback):

        self.close()


    def _find_frames(self):

        frames = []

        position = 0

        while True:

            offset = self.mm.find(
                MAGIC,
                position
            )

            if offset < 0:
                break


            # Make sure enough data exists.
            if offset + FRAME_SIZE <= self.size:

                frame_number = struct.unpack_from(
                    "<I",
                    self.mm,
                    offset + 4
                )[0]


                payload_size = struct.unpack_from(
                    "<I",
                    self.mm,
                    offset + 12
                )[0]


                # Validate the frame structure.
                expected_payload = (
                    ROWS * COLS * 2
                ) + 44


                if payload_size == expected_payload:

                    timestamp = struct.unpack_from(
                        "<I",
                        self.mm,
                        offset + 16
                    )[0]


                    frames.append(
                        XSNFrameInfo(
                            number=frame_number,
                            offset=offset,
                            timestamp=timestamp
                        )
                    )


            position = offset + 1


        return frames


    def __len__(self):

        return len(self.frames)


    def get_frame(self, index):

        info = self.frames[index]

        data = np.frombuffer(
            self.mm,
            dtype="<u2",
            count=ROWS * COLS,
            offset=info.offset + HEADER_SIZE
        )


        return data.reshape(
            ROWS,
            COLS
        ).copy()


    def iter_frames(self):

        for i, info in enumerate(self.frames):

            yield (
                info,
                self.get_frame(i)
            )


    def frame_rate(self):

        if len(self.frames) < 2:
            return None


        timestamps = np.array(
            [f.timestamp for f in self.frames],
            dtype=np.float64
        )


        differences = np.diff(timestamps)

        median_delta = np.median(
            differences
        )


        if median_delta <= 0:
            return None


        return 1_000_000 / median_delta


# ---------------------------------------------------------
# Empirically calibrated raw → PSI conversion
# ---------------------------------------------------------
# Verified against 245_50R20_FL_Static CSV ground truth.
# Across frames 1–2813, for non-zero values:
#   correlation(XSN_RAW / 128, CSV_PSI) ≈ 0.999999995
#   max error ≈ 0.005 PSI (CSV rounding to 2 decimals)
#
# Keep this function isolated so calibration can be updated
# if Apollo provides a different sensor calibration.
# ---------------------------------------------------------

_XSN_CALIBRATION_DIVISOR: float = 128.0
_XSN_NOISE_THRESHOLD_PSI: float = 5.0


def raw_to_psi(
    raw: np.ndarray,
    *,
    threshold_psi: float = _XSN_NOISE_THRESHOLD_PSI,
) -> np.ndarray:
    """DEPRECATED: Use calibrate_frame() instead.
    Convert raw uint16 XSN sensor values to PSI.

    Parameters
    ----------
    raw : ndarray
        Raw uint16 matrix from XSNReader.get_frame().
    threshold_psi : float
        Values below this are set to 0 (matching CSV export behaviour).
        Set to 0.0 to disable thresholding.

    Returns
    -------
    ndarray (float32)
        Pressure in PSI.
    """
    psi = raw.astype(np.float32) / _XSN_CALIBRATION_DIVISOR

    # 65535 appears as saturation / invalid in raw recordings.
    psi[raw >= 65535] = np.nan

    # Zero out sub-threshold values (matches CSV Threshold = 5.0 PSI).
    if threshold_psi > 0.0:
        psi[psi < threshold_psi] = 0.0

    return psi


def calibrate_frame(raw: np.ndarray, tyre_type: str = "TBR", pressure_ceiling: float = 220.0) -> CalibrationResult:
    """Calibrate raw uint16 frame and compute invalid/saturation masks."""
    calibrated = raw.astype(np.float32) / 128.0
    
    # 65530+ is typically saturation/invalid
    saturation_mask = (raw >= 65530)
    
    # Check for NaN, inf, or negative raw
    invalid_mask = np.isnan(calibrated) | np.isinf(calibrated) | (raw < 0)
    
    # If the frame has contact but there are exact 0 raw values, mark them as invalid?
    # Based on instructions: "where raw == 0 and the frame has clear contact elsewhere"
    has_contact = np.sum(calibrated > 5.0) > 100
    if has_contact:
        invalid_mask |= (raw == 0)

    # Suspicious values > pressure_ceiling * 1.15
    suspicious = calibrated > pressure_ceiling * 1.15
    calibrated[suspicious] = 0.0
    saturation_mask |= suspicious
    
    # Enforce non-negative
    calibrated = np.maximum(calibrated, 0)
    
    return CalibrationResult(
        calibrated_psi=calibrated,
        raw_frame=raw.copy(),
        invalid_mask=invalid_mask,
        saturation_mask=saturation_mask,
        calibration_method="EMPIRICAL_FALLBACK_RAW_DIV_128",
        pressure_range=tyre_type,
        pressure_ceiling=pressure_ceiling,
        confidence="EMPIRICAL"
    )

def raw_frame_diagnostics(raw: np.ndarray) -> dict:
    """Compute comprehensive diagnostics for a raw uint16 frame."""
    flat = raw.flatten().astype(np.float64)
    nonzero = flat[flat > 0]
    return {
        "dtype": str(raw.dtype),
        "shape": list(raw.shape),
        "raw_min": float(np.min(flat)),
        "raw_max": float(np.max(flat)),
        "raw_mean": float(np.mean(flat)),
        "raw_median": float(np.median(flat)),
        "p1": float(np.percentile(flat, 1)),
        "p5": float(np.percentile(flat, 5)),
        "p50": float(np.percentile(flat, 50)),
        "p95": float(np.percentile(flat, 95)),
        "p98": float(np.percentile(flat, 98)),
        "p99": float(np.percentile(flat, 99)),
        "p99_9": float(np.percentile(flat, 99.9)),
        "count_negative": int(np.sum(flat < 0)),
        "count_ge_65530": int(np.sum(flat >= 65530)),
        "count_zero": int(np.sum(flat == 0)),
        "count_active": int(np.sum(nonzero > 0)) if len(nonzero) > 0 else 0,
    }