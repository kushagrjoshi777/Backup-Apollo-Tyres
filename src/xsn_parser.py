from __future__ import annotations

import mmap
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import numpy as np


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
    """Convert raw uint16 XSN sensor values to PSI.

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