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

from xsn_parser import XSNReader, raw_to_psi

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


def parse_xsn_lazy(filepath: str, max_frames: int = 2000) -> dict:
    """Parse an XSENSOR .XSN binary file using memory-mapped lazy access.

    Returns a dict with:
      - "frames": dict mapping frame_index → float32 PSI array (rows × cols)
      - "frame_rate": float or None
      - "total_frames": int (total frames in file, before capping)
      - "is_provisional": True (XSN PSI values are provisional)

    Frames are read lazily from the mmap and converted to Provisional PSI.
    Only the first `max_frames` are materialised.

    Parameters
    ----------
    filepath   : Path to the .xsn file.
    max_frames : Safety cap – only read the first N frames (default 2000).
    """
    # ── [XSN-DIAG] Traceability logging ───────────────────────────────────
    file_size = os.path.getsize(filepath)
    print(f"[XSN-DIAG] ===========================================================")
    print(f"[XSN-DIAG] parse_xsn_lazy() called")
    print(f"[XSN-DIAG]   filepath     : {filepath}")
    print(f"[XSN-DIAG]   file size    : {file_size:,} bytes ({file_size / 1048576:.1f} MB)")
    print(f"[XSN-DIAG]   parser module: {__file__}")
    print(f"[XSN-DIAG]   reader class : src/xsn_parser.py -> XSNReader")
    print(f"[XSN-DIAG]   conversion   : raw_to_psi (raw / 128.0, threshold 5 PSI)")
    # ─────────────────────────────────────────────────────────────────────

    try:
        reader = XSNReader(filepath)
    except Exception as exc:
        raise ValueError(f"Failed to open XSN file: {exc}") from exc

    try:
        total_frames = len(reader)
        if total_frames == 0:
            raise ValueError(f"No frames found in XSN file: {filepath!r}")

        frame_rate = reader.frame_rate()

        # ── [XSN-DIAG] ────────────────────────────────────────────────────
        print(f"[XSN-DIAG]   XSNReader OK : {total_frames} frames detected")
        print(f"[XSN-DIAG]   frame rate   : {frame_rate}")
        # ──────────────────────────────────────────────────────────────────

        n_to_read = min(total_frames, max_frames)
        frames: dict[int, np.ndarray] = {}

        for i in range(n_to_read):
            raw_matrix = reader.get_frame(i)  # uint16 256×256
            psi_matrix = raw_to_psi(raw_matrix)
            # Replace NaN (saturated 65535 values) with 0 for downstream compatibility
            psi_matrix = np.nan_to_num(psi_matrix, nan=0.0)
            frames[i] = psi_matrix

            # ── [XSN-DIAG] frame 0 and mid-frame stats ────────────────────
            if i == 0:
                print(f"[XSN-DIAG]   frame[0]     : shape={psi_matrix.shape} dtype={psi_matrix.dtype}")
                print(f"[XSN-DIAG]                  min={psi_matrix.min():.4f}  max={psi_matrix.max():.4f}  mean={psi_matrix.mean():.4f}")
            elif i == n_to_read // 2:
                print(f"[XSN-DIAG]   frame[{i}]  : shape={psi_matrix.shape} dtype={psi_matrix.dtype}")
                print(f"[XSN-DIAG]                  min={psi_matrix.min():.4f}  max={psi_matrix.max():.4f}  mean={psi_matrix.mean():.4f}")
            # ──────────────────────────────────────────────────────────────

    finally:
        reader.close()

    # ── [XSN-DIAG] ────────────────────────────────────────────────────────
    print(f"[XSN-DIAG]   frames read  : {len(frames)} (cap={max_frames})")
    print(f"[XSN-DIAG]   provisional  : True")
    print(f"[XSN-DIAG]   PATH: XSN file -> XSNReader(mmap) -> get_frame() -> raw_to_psi() -> float32 PSI")
    print(f"[XSN-DIAG] ===========================================================")
    # ─────────────────────────────────────────────────────────────────────

    return {
        "frames": frames,
        "frame_rate": frame_rate,
        "total_frames": total_frames,
        "is_provisional": True,
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


def parse_file_extended(filepath: str, max_frames: int = 2000) -> dict:
    """Extended entry point that returns metadata alongside frames.

    Returns a dict with:
      - "frames": dict[int, ndarray]  — frame data (float32 PSI)
      - "frame_rate": float or None   — only for XSN
      - "total_frames": int           — total frames in file
      - "is_provisional": bool        — True if XSN (provisional PSI)
    """
    ext = os.path.splitext(filepath)[1].lower()
    if ext == ".xsn":
        return parse_xsn_lazy(filepath, max_frames=max_frames)
    else:
        frames = parse_xsensor_csv(filepath)
        return {
            "frames": frames,
            "frame_rate": None,
            "total_frames": len(frames),
            "is_provisional": False,
        }