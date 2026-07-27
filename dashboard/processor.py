"""
processor.py  —  Apollo Tyre Pressure Analysis
Handles: parsing, frame trimming, metrics, heatmap serialization.
Wraps the logic from src/parser.py and src/viewer.py.
"""

import sys
import os
import numpy as np

# Allow importing from parent src/ directory
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from src.parser import parse_xsensor_csv

# ── Constants ────────────────────────────────────────────────────────────────
PRESSURE_THRESHOLD   = 5.0   # PSI — below this is considered noise / no-contact
CONTACT_AREA_MIN     = 100   # minimum active sensels to count as a "meaningful" frame
SENSEL_AREA_MM2      = 6.45  # mm² per sensel (XSENSOR standard 256×256 mat)
PSI_TO_N_PER_MM2     = 0.00689476  # 1 PSI → N/mm²

# Tyre type → pressure ceiling (PSI)
VMAX_BY_TYPE = {
    "PCR": 100,
    "TBR": 220,
}
DEFAULT_VMAX = 150

# ── Public API ────────────────────────────────────────────────────────────────

def load_and_process(
    filepath: str,
    tyre_type: str = "TBR",
    min_pressure: float = 5.0,
    max_pressure: float = 220.0,
    speed: float = 0.0
) -> dict:
    """
    Full pipeline: parse → trim → compute metrics → serialise.
    tyre_type: "PCR" or "TBR" or "CUSTOM"
    min_pressure: min pressure threshold (below this is noise)
    max_pressure: max pressure ceiling for colour scale.
    Returns a dict ready to JSON-serialise and send to the frontend.
    """
    vmax_psi = max_pressure

    raw_frames = parse_xsensor_csv(filepath)

    if not raw_frames:
        raise ValueError("No frames found in the CSV file.")

    # 1. Trim empty / no-contact frames
    trimmed = _trim_frames(raw_frames, min_pressure)

    if not trimmed:
        # Fall back to all frames if trimming removed everything
        trimmed = raw_frames

    frame_numbers = sorted(trimmed.keys())

    # 2. Per-frame metrics
    metrics_list = [_frame_metrics(trimmed[fn], fn, min_pressure) for fn in frame_numbers]

    # 3. Best-contact frame (max contact area)
    best_idx   = int(np.argmax([m["active_sensels"] for m in metrics_list]))
    best_fn    = frame_numbers[best_idx]
    best_frame = trimmed[best_fn]

    # 4. Contact-patch analysis for best frame
    patch = _contact_patch(best_frame, min_pressure)

    # 5. Serialise frames as flat normalised arrays for the canvas renderer
    frames_data = {
        str(fn): _serialise_frame(trimmed[fn], vmax_psi)
        for fn in frame_numbers
    }

    return {
        "frame_numbers":     frame_numbers,
        "frames":            frames_data,
        "metrics":           metrics_list,
        "best_frame_index":  best_idx,
        "best_frame_number": best_fn,
        "best_metrics":      metrics_list[best_idx],
        "contact_patch":     patch,
        "vmax":              vmax_psi,
        "vmin":              min_pressure,
        "speed":             speed,
        "tyre_type":         tyre_type.upper(),
    }


# ── Internal helpers ──────────────────────────────────────────────────────────

def _trim_frames(raw: dict, threshold: float) -> dict:
    """Remove frames with fewer than CONTACT_AREA_MIN active sensels."""
    return {
        fn: arr
        for fn, arr in raw.items()
        if int(np.sum(arr > threshold)) >= CONTACT_AREA_MIN
    }


def _frame_metrics(arr: np.ndarray, frame_number: int, threshold: float) -> dict:
    """Compute all scalar metrics for a single frame."""
    mask          = arr > threshold
    active        = int(np.sum(mask))
    total_sensels = arr.size  # 256 × 256 = 65 536

    peak_psi  = float(np.max(arr))
    avg_psi   = float(np.mean(arr[mask])) if active > 0 else 0.0

    contact_area_mm2 = active * SENSEL_AREA_MM2
    total_load_n     = float(np.sum(arr[mask]) * PSI_TO_N_PER_MM2 * SENSEL_AREA_MM2)
    total_load_kg    = total_load_n / 9.80665

    return {
        "frame_number":      frame_number,
        "peak_pressure_psi": round(peak_psi, 2),
        "avg_pressure_psi":  round(avg_psi, 2),
        "contact_area_mm2":  round(contact_area_mm2, 1),
        "estimated_load_kg": round(total_load_kg, 1),
        "active_sensels":    active,
        "contact_pct":       round(100.0 * active / total_sensels, 2),
    }


def _contact_patch(arr: np.ndarray, threshold: float) -> dict:
    """Bounding box + dimensions of the contact patch."""
    mask = arr > threshold
    rows = np.any(mask, axis=1)
    cols = np.any(mask, axis=0)

    if not rows.any():
        return {"width_mm": 0, "length_mm": 0, "bbox": [0, 0, 0, 0]}

    row_min = int(np.argmax(rows))
    row_max = int(len(rows) - 1 - np.argmax(rows[::-1]))
    col_min = int(np.argmax(cols))
    col_max = int(len(cols) - 1 - np.argmax(cols[::-1]))

    # XSENSOR 256x256 mat: each sensel ~2.54 mm pitch
    PITCH_MM  = 2.54
    length_mm = round((row_max - row_min + 1) * PITCH_MM, 1)
    width_mm  = round((col_max - col_min + 1) * PITCH_MM, 1)

    return {
        "width_mm":  width_mm,
        "length_mm": length_mm,
        "bbox": [col_min, row_min, col_max, row_max],  # x0,y0,x1,y1 sensel coords
    }


def _serialise_frame(arr: np.ndarray, vmax_psi: float) -> list:
    """
    Return a flat list of uint8 values (0-255) for canvas rendering.
    Normalised against vmax_psi.
    Row-major, origin=lower -> flip vertically so row 0 is at top in canvas.
    """
    clipped    = np.clip(arr, 0, vmax_psi)
    normalised = (clipped / vmax_psi * 255).astype(np.uint8)
    flipped    = np.flipud(normalised)
    return flipped.flatten().tolist()
