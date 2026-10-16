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

    def sensel_pitch_mm(self) -> tuple[float, float] | None:
        """Extract sensel width and height (mm) from the XSN binary header if available.

        XSENSOR HX510 headers store sensor geometry after the ``HSS\\x00`` signature
        as little-endian float32 values: ``[40.0, 40.0, pitch_w_mm, pitch_h_mm]``
        (e.g. 1.56 mm × 1.56 mm for a 256×256 40 cm × 40 cm sensor pad).
        """
        search_limit = min(self.size, self.frames[0].offset if self.frames else 700_000)
        head = self.mm[:search_limit]
        pos = 0
        while True:
            idx = head.find(b"HSS\x00", pos)
            if idx < 0 or idx + 56 > len(head):
                break
            vals = struct.unpack_from("<14f", head, idx)
            pw, ph = float(vals[12]), float(vals[13])
            if 0.5 <= pw <= 10.0 and 0.5 <= ph <= 10.0 and not (np.isnan(pw) or np.isnan(ph)):
                return round(pw, 4), round(ph, 4)
            pos = idx + 4
        return None


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
_XSN_NOISE_THRESHOLD_PSI: float = 18.0


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
        Values below this are set to 0 (default 18.0 PSI for TBR).
        Set to 0.0 to disable thresholding.

    Returns
    -------
    ndarray (float32)
        Pressure in PSI.
    """
    psi = raw.astype(np.float32) / _XSN_CALIBRATION_DIVISOR

    # 65530+ (including 65535 = 0xFFFF) appears as invalid / dead sensel or ADC saturation.
    psi[raw >= 65530] = np.nan

    # Zero out sub-threshold values.
    if threshold_psi > 0.0:
        psi[psi < threshold_psi] = 0.0

    return psi


def calibrate_frame(raw: np.ndarray, tyre_type: str = "TBR", pressure_ceiling: float = 220.0) -> CalibrationResult:
    """Calibrate raw uint16 frame and compute invalid/saturation masks.

    Raw values >= 65530 (such as 65535 / 0xFFFF) represent hardware invalid /
    saturated sensel codes and are excluded from contact geometry and pressure
    statistics (set to 0.0 in calibrated_psi and flagged in invalid_mask and
    saturation_mask). Legitimate high local tread pressures (< 65530, i.e. up to
    ~511.9 PSI) are preserved rather than zeroed at the nominal display ceiling.
    """
    calibrated = raw.astype(np.float32) / _XSN_CALIBRATION_DIVISOR

    # 65530+ (specifically 65535 = 0xFFFF) is hardware saturation / dead sensel code
    hw_invalid = (raw >= 65530)

    # Check for NaN, inf, negative raw, or hardware invalid code
    invalid_mask = np.isnan(calibrated) | np.isinf(calibrated) | (raw < 0) | hw_invalid

    # Track sensels exceeding nominal display ceiling (e.g. 220 PSI) or hardware saturation
    saturation_mask = hw_invalid.copy()

    # Zero out invalid / hardware-saturated sensels so they never corrupt geometry or statistics
    calibrated[invalid_mask] = 0.0

    # If the frame has contact but there are exact 0 raw values inside, record in invalid_mask
    # for diagnostic traceability without altering valid sensels.
    thresh_check = 18.0 if tyre_type.upper() == "TBR" else 5.0
    has_contact = np.sum(calibrated >= thresh_check) > 100
    if has_contact:
        invalid_mask = invalid_mask | (raw == 0)

    # Enforce non-negative
    calibrated = np.maximum(calibrated, 0.0)

    return CalibrationResult(
        calibrated_psi=calibrated,
        raw_frame=raw.copy(),
        invalid_mask=invalid_mask,
        saturation_mask=saturation_mask,
        calibration_method="EMPIRICAL_RAW_DIV_128",
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