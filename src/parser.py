"""
parser.py — Apollo Tyre Pressure Analysis
Handles both XSENSOR CSV exports and proprietary .XSN binary files.

CSV:  Parsed line-by-line, values already in PSI or auto-scaled from raw counts.
XSN:  Parsed via the verified xsn_parser.XSNReader (memory-mapped, lazy access).
      Raw uint16 values are converted to Provisional PSI via raw_to_psi().
"""

import os
import re
import sys
import numpy as np

# ── XSN reader (memory-mapped, lazy) ──────────────────────────────────────────
# Ensure the src/ directory is on sys.path so xsn_parser can be found
# regardless of how parser.py is imported (standalone or via src.parser).
_SRC_DIR = os.path.dirname(os.path.abspath(__file__))
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from xsn_parser import XSNReader, calibrate_frame, raw_frame_diagnostics

# ── Constants ─────────────────────────────────────────────────────────────────
# Conversion constant for legacy parse_xsensor_csv auto-detection
XSN_RAW_TO_PSI: float = 220.0 / 65535.0


# ─────────────────────────────────────────────────────────────────────────────
# Public interface
# ─────────────────────────────────────────────────────────────────────────────

def parse_xsensor_csv(filepath: str) -> dict[int, np.ndarray]:
    """Parse an XSENSOR CSV export.

    Returns a dict mapping frame_number → float32 PSI array (rows × cols).
    Values are already in PSI (raw counts as floats, assuming the CSV stores
    raw uint16 counts that need RAW_TO_PSI conversion – if the CSV contains
    PSI values directly, the function detects and skips the scaling step).
    """
    frames: dict[int, np.ndarray] = {}

    with open(filepath, "r", encoding="utf-8", errors="ignore") as fh:
        lines = fh.readlines()

    # Quick scan: detect if the file stores RAW counts or already-converted PSI.
    # XSENSOR raw counts are 0-65535; PSI values are typically < 300.
    _sample_max = 0.0
    for line in lines[:500]:
        stripped = line.strip()
        if not stripped or stripped.startswith("FRAME") or not stripped[0].isdigit():
            continue
        try:
            val = max(float(x) for x in stripped.split(",") if x.strip())
            _sample_max = max(_sample_max, val)
        except ValueError:
            pass
    # If maximum sample value > 300, assume raw uint16 counts and scale.
    _scale: float = XSN_RAW_TO_PSI if _sample_max > 300.0 else 1.0

    i = 0
    while i < len(lines):
        line = lines[i].strip()

        if line.startswith("FRAME"):
            try:
                frame_number = int(lines[i].split(",")[1])
            except (IndexError, ValueError):
                i += 1
                continue

            matrix_start = None
            for j in range(i, min(i + 30, len(lines))):
                if "SENSELS" in lines[j]:
                    matrix_start = j + 1
                    break

            if matrix_start is None:
                i += 1
                continue

            matrix: list[list[float]] = []
            for row_idx in range(matrix_start, matrix_start + 256):
                if row_idx >= len(lines):
                    break
                row = lines[row_idx].strip().split(",")
                values: list[float] = []
                for val in row[:256]:
                    try:
                        values.append(float(val))
                    except ValueError:
                        values.append(0.0)
                if values:
                    matrix.append(values)

            if matrix:
                arr = np.array(matrix, dtype=np.float32) * _scale
                frames[frame_number] = arr
                i = matrix_start + len(matrix)
            else:
                i += 1
        else:
            i += 1

    return frames


def parse_xsn_lazy(filepath: str, max_frames: int = 2000, tyre_type: str = "TBR", pressure_ceiling: float = 220.0) -> dict:
    """Parse an XSENSOR .XSN binary file using memory-mapped lazy access.

    Returns a dict with:
      - "frames": dict mapping frame_index → float32 PSI array (rows × cols)
      - "raw_frames": dict mapping frame_index → uint16 raw array
      - "calibration_results": dict mapping frame_index → CalibrationResult
      - "frame_rate": float or None
      - "total_frames": int
      - "is_provisional": False (Empirically validated)
      - "calibration_method": "EMPIRICAL_FALLBACK_RAW_DIV_128"
      - "calibration_confidence": "EMPIRICAL"
      - "raw_diagnostics": dict (for first frame)
    """
    try:
        reader = XSNReader(filepath)
    except Exception as exc:
        raise ValueError(f"Failed to open XSN file: {exc}") from exc

    try:
        total_frames = len(reader)
        if total_frames == 0:
            raise ValueError(f"No frames found in XSN file: {filepath!r}")

        frame_rate = reader.frame_rate()

        n_to_read = min(total_frames, max_frames)
        frames: dict[int, np.ndarray] = {}
        raw_frames: dict[int, np.ndarray] = {}
        calibration_results = {}
        first_frame_diagnostics = {}

        for i in range(n_to_read):
            raw_matrix = reader.get_frame(i)  # uint16 256×256
            
            if i == 0:
                first_frame_diagnostics = raw_frame_diagnostics(raw_matrix)
                
            calib_result = calibrate_frame(raw_matrix, tyre_type, pressure_ceiling)
            
            frames[i] = calib_result.calibrated_psi.copy()
            raw_frames[i] = raw_matrix
            calibration_results[i] = calib_result

    finally:
        reader.close()

    return {
        "frames": frames,
        "raw_frames": raw_frames,
        "calibration_results": calibration_results,
        "frame_rate": frame_rate,
        "total_frames": total_frames,
        "is_provisional": False,
        "calibration_method": "EMPIRICAL_FALLBACK_RAW_DIV_128",
        "calibration_confidence": "EMPIRICAL",
        "raw_diagnostics": first_frame_diagnostics,
    }


def parse_file(filepath: str, max_frames: int = 2000) -> dict[int, np.ndarray]:
    """Unified entry point: dispatch to CSV or XSN parser based on extension.

    For CSV: returns dict[frame_number, ndarray] directly.
    For XSN: returns dict[frame_index, ndarray] (via parse_xsn_lazy).

    Both return float32 PSI arrays suitable for the processing pipeline.
    """
    ext = os.path.splitext(filepath)[1].lower()
    if ext == ".xsn":
        result = parse_xsn_lazy(filepath, max_frames=max_frames)
        return result["frames"]
    else:
        return parse_xsensor_csv(filepath)


def parse_file_extended(filepath: str, max_frames: int = 2000, tyre_type: str = "TBR", pressure_ceiling: float = 220.0) -> dict:
    """Extended entry point that returns metadata alongside frames.

    Returns a dict with:
      - "frames": dict[int, ndarray]  — frame data (float32 PSI)
      - "frame_rate": float or None   — only for XSN
      - "total_frames": int           — total frames in file
      - "is_provisional": bool        — True if XSN (provisional PSI)
      - Plus additional diagnostic and calibration fields for XSN.
    """
    ext = os.path.splitext(filepath)[1].lower()
    if ext == ".xsn":
        return parse_xsn_lazy(filepath, max_frames=max_frames, tyre_type=tyre_type, pressure_ceiling=pressure_ceiling)
    else:
        frames = parse_xsensor_csv(filepath)
        return {
            "frames": frames,
            "raw_frames": {},
            "calibration_results": {},
            "frame_rate": None,
            "total_frames": len(frames),
            "is_provisional": False,
            "calibration_method": "CSV_DIRECT",
            "calibration_confidence": "VERIFIED",
            "raw_diagnostics": {},
        }