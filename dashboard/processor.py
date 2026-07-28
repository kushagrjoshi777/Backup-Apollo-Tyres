"""
processor.py  —  Apollo Tyre Pressure Analysis
Handles: parsing, frame trimming, 13 metrics calculation, heatmap serialization.
Wraps the logic from src/parser.py and src/viewer.py.
"""

import sys
import os
import re
import numpy as np

# Allow importing from parent src/ directory
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from src.parser import parse_xsensor_csv

# ── Constants ────────────────────────────────────────────────────────────────
PRESSURE_THRESHOLD   = 5.0   # PSI — below this is considered noise / no-contact
CONTACT_AREA_MIN     = 100   # minimum active sensels to count as a "meaningful" frame
DEFAULT_SENSEL_MM    = 1.6   # mm pitch per sensel (0.16 cm XSENSOR standard)

# Tyre type → pressure ceiling (PSI)
VMAX_BY_TYPE = {
    "PCR": 100,
    "TBR": 220,
}
DEFAULT_VMAX = 150


# ── Public API ────────────────────────────────────────────────────────────────

def extract_metadata_from_file(filepath: str) -> dict:
    """Extract auto-detected speed and tyre position from filename and CSV header."""
    filename = os.path.basename(filepath)
    speed = 0.0
    position = "FR"

    # Search in filename
    speed_match = re.search(r'(\d+(?:\.\d+)?)\s*(?:kmph|km/h|kmh)', filename, re.IGNORECASE)
    if speed_match:
        speed = float(speed_match.group(1))

    pos_match = re.search(r'\b(FL|FR|RL|RR)\b', filename, re.IGNORECASE)
    if pos_match:
        position = pos_match.group(1).upper()
    else:
        # Check underscore prefix/suffix like _FR_ or _FL
        pos_match2 = re.search(r'[_\-](FL|FR|RL|RR)[_\-\.]', filename, re.IGNORECASE)
        if pos_match2:
            position = pos_match2.group(1).upper()

    # Search inside first 50 lines of CSV
    sensel_width_mm = DEFAULT_SENSEL_MM
    sensel_height_mm = DEFAULT_SENSEL_MM
    try:
        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            for _ in range(50):
                line = f.readline()
                if not line:
                    break
                if speed == 0.0:
                    sm = re.search(r'(\d+(?:\.\d+)?)\s*(?:kmph|km/h|kmh)', line, re.IGNORECASE)
                    if sm:
                        speed = float(sm.group(1))
                if pos_match is None:
                    pm = re.search(r'\b(FL|FR|RL|RR)\b', line, re.IGNORECASE)
                    if pm:
                        position = pm.group(1).upper()
                if "Sensel Width (cm)" in line:
                    try:
                        sensel_width_mm = float(line.split(",")[1].strip()) * 10.0
                    except (IndexError, ValueError):
                        pass
                if "Sensel Height (cm)" in line:
                    try:
                        sensel_height_mm = float(line.split(",")[1].strip()) * 10.0
                    except (IndexError, ValueError):
                        pass
    except Exception:
        pass

    return {
        "speed": speed,
        "position": position,
        "sensel_width_mm": sensel_width_mm,
        "sensel_height_mm": sensel_height_mm,
    }


def load_and_process(
    filepath: str,
    tyre_type: str = "TBR",
    min_pressure: float = 5.0,
    max_pressure: float = 220.0,
    speed: float = 0.0,
    position: str = "FR"
) -> dict:
    """
    Full pipeline: parse → trim → compute 13 metrics → serialise.
    """
    vmax_psi = max_pressure

    # Extract metadata if speed or position default/unset
    meta = extract_metadata_from_file(filepath)
    if speed == 0.0 and meta["speed"] > 0:
        speed = meta["speed"]
    if position not in ("FL", "FR", "RL", "RR") or position == "FR":
        if meta["position"] in ("FL", "FR", "RL", "RR"):
            position = meta["position"]

    sensel_w = meta.get("sensel_width_mm", DEFAULT_SENSEL_MM)
    sensel_h = meta.get("sensel_height_mm", DEFAULT_SENSEL_MM)

    raw_frames = parse_xsensor_csv(filepath)

    if not raw_frames:
        raise ValueError("No frames found in the CSV file.")

    # 1. Trim empty / no-contact frames
    trimmed = _trim_frames(raw_frames, min_pressure)

    if not trimmed:
        trimmed = raw_frames

    frame_numbers = sorted(trimmed.keys())

    # 2. Per-frame metrics (including all 13 metrics)
    metrics_list = [
        _frame_metrics_13(trimmed[fn], fn, min_pressure, position, sensel_w, sensel_h, vmax_psi)
        for fn in frame_numbers
    ]

    # 3. Best-contact frame (max contact area)
    best_idx   = int(np.argmax([m["active_sensels"] for m in metrics_list]))
    best_fn    = frame_numbers[best_idx]
    best_frame = trimmed[best_fn]

    # 4. Contact-patch bounding box for best frame
    patch = _contact_patch(best_frame, min_pressure, sensel_w, sensel_h)

    # 5. Serialise frames as flat uint8 arrays
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
        "position":          position,
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


def _frame_metrics_13(
    arr: np.ndarray,
    frame_number: int,
    threshold: float,
    position: str,
    sensel_w_mm: float,
    sensel_h_mm: float,
    vmax_psi: float
) -> dict:
    """Compute all 13 standard tyre parameters for a frame."""
    mask   = arr > threshold
    active = int(np.sum(mask))

    sensel_area_mm2 = sensel_w_mm * sensel_h_mm

    peak_psi = float(np.max(arr)) if active > 0 else 0.0
    avg_psi  = float(np.mean(arr[mask])) if active > 0 else 0.0

    contact_area_mm2 = active * sensel_area_mm2
    PSI_TO_N_PER_MM2 = 0.00689476
    total_load_n     = float(np.sum(arr[mask]) * PSI_TO_N_PER_MM2 * sensel_area_mm2)
    total_load_kg    = total_load_n / 9.80665

    rows_any = np.any(mask, axis=1)
    cols_any = np.any(mask, axis=0)

    if not rows_any.any():
        return {
            "frame_number":         frame_number,
            "contact_area_mm2":     0.0,
            "contact_ratio_pct":    0.0,
            "fp_width_mm":          0.0,
            "fp_length_mm":         0.0,
            "outer_shoulder_s1_mm": 0.0,
            "inner_shoulder_s2_mm": 0.0,
            "shoulder_skewness_pct":0.0,
            "squareness_s1_s2_pct": 100.0,
            "squareness_avg":       0.0,
            "land_sea_ratio":       0.0,
            "contact_pressure_psi": 0.0,
            "max_length_mm":        0.0,
            "max_width_mm":         0.0,
            "peak_pressure_psi":    0.0,
            "avg_pressure_psi":     0.0,
            "estimated_load_kg":    0.0,
            "active_sensels":       0,
            "contact_pct":          0.0,
        }

    r_min = int(np.argmax(rows_any))
    r_max = int(len(rows_any) - 1 - np.argmax(rows_any[::-1]))
    c_min = int(np.argmax(cols_any))
    c_max = int(len(cols_any) - 1 - np.argmax(cols_any[::-1]))

    bbox_h_sensels = r_max - r_min + 1
    bbox_w_sensels = c_max - c_min + 1
    bbox_area_sensels = bbox_h_sensels * bbox_w_sensels

    fp_width_mm  = round(bbox_w_sensels * sensel_w_mm, 1)
    fp_length_mm = round(bbox_h_sensels * sensel_h_mm, 1)

    contact_ratio_pct = round((active / bbox_area_sensels) * 100.0, 1) if bbox_area_sensels > 0 else 0.0

    # Max length across columns
    max_col_sensels = 0
    col_lengths = []
    for c in range(c_min, c_max + 1):
        col_mask = mask[:, c]
        if np.any(col_mask):
            c_rmin = int(np.argmax(col_mask))
            c_rmax = int(len(col_mask) - 1 - np.argmax(col_mask[::-1]))
            c_len = c_rmax - c_rmin + 1
            col_lengths.append(c_len)
            if c_len > max_col_sensels:
                max_col_sensels = c_len
        else:
            col_lengths.append(0)

    max_length_mm = round(max_col_sensels * sensel_h_mm, 1)

    # Max width across rows
    max_row_sensels = 0
    for r in range(r_min, r_max + 1):
        row_mask = mask[r, :]
        if np.any(row_mask):
            r_cmin = int(np.argmax(row_mask))
            r_cmax = int(len(row_mask) - 1 - np.argmax(row_mask[::-1]))
            r_w = r_cmax - r_cmin + 1
            if r_w > max_row_sensels:
                max_row_sensels = r_w
    max_width_mm = round(max_row_sensels * sensel_w_mm, 1)

    # Outer Shoulder S1 vs Inner Shoulder S2
    shoulder_w_sensels = max(2, int(round(0.20 * bbox_w_sensels)))
    left_strip_lens  = col_lengths[:shoulder_w_sensels]
    right_strip_lens = col_lengths[-shoulder_w_sensels:]

    left_s_mm  = float(np.mean(left_strip_lens)) * sensel_h_mm if left_strip_lens else 0.0
    right_s_mm = float(np.mean(right_strip_lens)) * sensel_h_mm if right_strip_lens else 0.0

    # For FL & RL (Left of car): Outer = Left, Inner = Right
    # For FR & RR (Right of car): Outer = Right, Inner = Left
    if position in ("FL", "RL"):
        s1 = round(left_s_mm, 1)
        s2 = round(right_s_mm, 1)
    else:
        s1 = round(right_s_mm, 1)
        s2 = round(left_s_mm, 1)

    # Skewness % = ((S1 - S2) / ((S1 + S2) / 2)) * 100
    s_avg_val = (s1 + s2) / 2.0
    shoulder_skewness_pct = round(((s1 - s2) / s_avg_val) * 100.0, 1) if s_avg_val > 0 else 0.0

    # Squareness (S1/S2) %
    squareness_s1_s2_pct = round((s1 / s2) * 100.0, 1) if s2 > 0 else 100.0

    # Squareness (Avg) = s_avg_val / fp_length_mm
    squareness_avg = round(s_avg_val / fp_length_mm, 2) if fp_length_mm > 0 else 0.0

    # Land/sea ratio
    sea_sensels = bbox_area_sensels - active
    land_sea_ratio = round(active / sea_sensels, 2) if sea_sensels > 0 else 0.0

    return {
        "frame_number":          frame_number,
        "contact_area_mm2":      round(contact_area_mm2, 1),
        "contact_ratio_pct":     contact_ratio_pct,
        "fp_width_mm":           fp_width_mm,
        "fp_length_mm":          fp_length_mm,
        "outer_shoulder_s1_mm":  s1,
        "inner_shoulder_s2_mm":  s2,
        "shoulder_skewness_pct": shoulder_skewness_pct,
        "squareness_s1_s2_pct":  squareness_s1_s2_pct,
        "squareness_avg":        squareness_avg,
        "land_sea_ratio":        land_sea_ratio,
        "contact_pressure_psi":  round(avg_psi, 2),
        "maximum_length_mm":     max_length_mm,
        "maximum_width_mm":      max_width_mm,
        # Legacy/Extra fields for compatibility
        "peak_pressure_psi":     round(peak_psi, 2),
        "avg_pressure_psi":      round(avg_psi, 2),
        "estimated_load_kg":     round(total_load_kg, 1),
        "active_sensels":        active,
        "contact_pct":           round(100.0 * active / (256 * 256), 2),
    }


def _contact_patch(arr: np.ndarray, threshold: float, sensel_w_mm: float, sensel_h_mm: float) -> dict:
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

    length_mm = round((row_max - row_min + 1) * sensel_h_mm, 1)
    width_mm  = round((col_max - col_min + 1) * sensel_w_mm, 1)

    return {
        "width_mm":  width_mm,
        "length_mm": length_mm,
        "bbox": [col_min, row_min, col_max, row_max],
    }


def _serialise_frame(arr: np.ndarray, vmax_psi: float) -> list:
    """
    Return a flat list of uint8 values (0-255) for canvas rendering.
    Normalised against vmax_psi.
    Origin=lower -> flip vertically so row 0 is at top in canvas.
    """
    clipped    = np.clip(arr, 0, vmax_psi)
    normalised = (clipped / vmax_psi * 255).astype(np.uint8)
    flipped    = np.flipud(normalised)
    return flipped.flatten().tolist()

