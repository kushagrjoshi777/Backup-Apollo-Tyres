"""
processor.py  —  Apollo Tyre Pressure Analysis
Handles: parsing (CSV + XSN), FFT noise reduction, 13 metrics, heatmap serialisation.
"""

import sys
import os
import re
import numpy as np
from scipy.fft import fft2, ifft2, fftshift, ifftshift
from scipy.ndimage import median_filter

# Allow importing from parent src/ directory
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from src.parser import parse_file_extended

# ── Constants ─────────────────────────────────────────────────────────────────
PRESSURE_THRESHOLD   = 5.0    # PSI — below this is considered noise / no-contact
CONTACT_AREA_MIN     = 100    # minimum active sensels to count as a "meaningful" frame
DEFAULT_SENSEL_MM    = 1.6    # mm pitch per sensel (0.16 cm XSENSOR standard)
XSN_SENSEL_MM        = 1.6    # mm pitch for XSENSOR HX510 sensor mat (0.16 cm = 1.6 mm)

# Tyre type → pressure ceiling (PSI)
VMAX_BY_TYPE = {"PCR": 100, "TBR": 220}
DEFAULT_VMAX = 150

# Max frames to process from large files (perf guard)
MAX_XSN_FRAMES = 2000


# ── Public API ────────────────────────────────────────────────────────────────

def extract_metadata_from_file(filepath: str) -> dict:
    """Extract auto-detected speed, tyre position, and sensel pitch from filename / CSV header."""
    filename = os.path.basename(filepath)
    ext = os.path.splitext(filename)[1].lower()
    speed = 0.0
    position = "FR"

    # Search filename for speed / position hints
    speed_match = re.search(r"(\d+(?:\.\d+)?)\s*(?:kmph|km/h|kmh)", filename, re.IGNORECASE)
    if speed_match:
        speed = float(speed_match.group(1))

    pos_match = re.search(r"\b(FL|FR|RL|RR)\b", filename, re.IGNORECASE)
    if pos_match:
        position = pos_match.group(1).upper()
    else:
        pos_match2 = re.search(r"[_\-](FL|FR|RL|RR)[_\-\.]", filename, re.IGNORECASE)
        if pos_match2:
            position = pos_match2.group(1).upper()

    # Default sensel pitch
    is_xsn = ext == ".xsn"
    sensel_width_mm  = XSN_SENSEL_MM if is_xsn else DEFAULT_SENSEL_MM
    sensel_height_mm = XSN_SENSEL_MM if is_xsn else DEFAULT_SENSEL_MM

    # For CSV files: search header for explicit sensel pitch
    if not is_xsn:
        try:
            with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
                for _ in range(50):
                    line = f.readline()
                    if not line:
                        break
                    if speed == 0.0:
                        sm = re.search(r"(\d+(?:\.\d+)?)\s*(?:kmph|km/h|kmh)", line, re.IGNORECASE)
                        if sm:
                            speed = float(sm.group(1))
                    if pos_match is None:
                        pm = re.search(r"\b(FL|FR|RL|RR)\b", line, re.IGNORECASE)
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
        "is_xsn": is_xsn,
    }



def apply_fft_butterworth(
    frame_psi: np.ndarray,
    keep_ratio: float = 0.97,
    butterworth_order: int = 2,
    is_xsn: bool = False,
) -> tuple[np.ndarray, np.ndarray]:
    """
    Despikes single-pixel hardware errors (>200 PSI) and applies a 2D Butterworth FFT Lowpass filter
    matching Jatin Files despike_and_fft_filter.
    """
    # 1. Despike single isolated hardware noise sensels
    med = median_filter(frame_psi, size=3)
    spike_mask = (frame_psi > 200.0) | (np.abs(frame_psi - med) > 50.0)
    clean_frame = frame_psi.copy()
    clean_frame[spike_mask] = med[spike_mask]

    # 2. Butterworth 2D FFT Filter (d0 = 140.0 * keep_ratio)
    rows, cols = clean_frame.shape
    f_shift = fftshift(fft2(clean_frame.astype(np.float64)))
    cy, cx = rows // 2, cols // 2
    y, x = np.ogrid[-cy : rows - cy, -cx : cols - cx]
    d = np.sqrt(x**2 + y**2)

    d0 = max(10.0, min(140.0, keep_ratio * 140.0))
    h_butterworth = 1.0 / (1.0 + (d / max(1.0, d0)) ** (2 * butterworth_order))

    filtered = np.real(ifft2(ifftshift(f_shift * h_butterworth)))
    filtered = np.clip(filtered, 0.0, None)

    noise = clean_frame - filtered
    return filtered.astype(np.float32), noise.astype(np.float32)


def _xsn_temporal_average(
    raw_frames: dict[int, np.ndarray],
    min_pressure: float,
    high_contact_min: int = 1000,
) -> np.ndarray | None:
    """Average high-contact frames to reduce per-sensel random noise for static tests."""
    candidates = [
        arr.astype(np.float32)
        for arr in raw_frames.values()
        if int(np.sum(arr > min_pressure)) >= high_contact_min
    ]
    if len(candidates) < 2:
        return None
    return np.mean(candidates, axis=0).astype(np.float32)


def load_and_process(
    filepath: str,
    tyre_type: str = "TBR",
    min_pressure: float = 5.0,
    max_pressure: float = 220.0,
    speed: float = 0.0,
    position: str = "FR",
    fft_keep_ratio: float = 0.97,
) -> dict:
    """
    Full pipeline: parse (CSV or XSN) → trim → FFT filter → 13 metrics → serialise.
    """
    vmax_psi = max_pressure

    # Extract metadata
    meta = extract_metadata_from_file(filepath)
    if speed == 0.0 and meta["speed"] > 0:
        speed = meta["speed"]
    if position not in ("FL", "FR", "RL", "RR") or position == "FR":
        if meta["position"] in ("FL", "FR", "RL", "RR"):
            position = meta["position"]

    sensel_w = meta.get("sensel_width_mm", DEFAULT_SENSEL_MM)
    sensel_h = meta.get("sensel_height_mm", DEFAULT_SENSEL_MM)

    # Parse file (CSV or XSN)
    is_xsn = meta["is_xsn"]
    parsed = parse_file_extended(filepath, max_frames=MAX_XSN_FRAMES)
    raw_frames     = parsed["frames"]
    xsn_frame_rate = parsed.get("frame_rate")
    is_provisional = parsed.get("is_provisional", False)
    total_frames   = parsed.get("total_frames", len(raw_frames))

    # ── [XSN-DIAG] Processor traceability ─────────────────────────────────
    print(f"[XSN-DIAG] processor.load_and_process()")
    print(f"[XSN-DIAG]   is_xsn       : {is_xsn}")
    print(f"[XSN-DIAG]   parser used   : src.parser.parse_file_extended -> {'parse_xsn_lazy' if is_xsn else 'parse_xsensor_csv'}")
    print(f"[XSN-DIAG]   raw frames    : {len(raw_frames)}")
    print(f"[XSN-DIAG]   is_provisional: {is_provisional}")
    print(f"[XSN-DIAG]   frame_rate    : {xsn_frame_rate}")
    if raw_frames:
        fk = sorted(raw_frames.keys())
        f0 = raw_frames[fk[0]]
        print(f"[XSN-DIAG]   frame[{fk[0]}] verify: shape={f0.shape} min={f0.min():.4f} max={f0.max():.4f} mean={f0.mean():.4f}")
    # ─────────────────────────────────────────────────────────────────────

    if not raw_frames:
        raise ValueError("No frames found in the file.")

    # 1. Select valid frames & 50-frame playback window centered around peak contact
    trimmed = _trim_frames(raw_frames, min_pressure)
    if not trimmed:
        trimmed = raw_frames

    frame_numbers = sorted(trimmed.keys())

    # 2. Apply FFT noise reduction to all frames
    filtered_frames: dict = {}
    for fn in frame_numbers:
        arr = trimmed[fn]
        if arr.dtype != np.float32:
            arr = arr.astype(np.float32)
        filtered, _ = apply_fft_butterworth(arr, keep_ratio=fft_keep_ratio, is_xsn=is_xsn)
        filtered_frames[fn] = filtered

    # 3. Per-frame 13-parameter metrics (on filtered data)
    metrics_list = [
        _frame_metrics_13(
            filtered_frames[fn], fn, min_pressure, position, sensel_w, sensel_h, vmax_psi
        )
        for fn in frame_numbers
    ]

    # 4. Best-contact frame (max contact area)
    best_idx  = int(np.argmax([m["active_sensels"] for m in metrics_list]))
    best_fn   = frame_numbers[best_idx]
    best_arr  = filtered_frames[best_fn]

    # 5. Exact tyre outline (edge-detection based contact patch)
    patch = _contact_patch_exact(best_arr, min_pressure, sensel_w, sensel_h)

    # 6. Serialise filtered frames as flat uint8 arrays (PSI-normalised)
    frames_data = {
        str(fn): _serialise_frame(filtered_frames[fn], vmax_psi)
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
        "fft_keep_ratio":    fft_keep_ratio,
        "sensel_w_mm":       sensel_w,
        "sensel_h_mm":       sensel_h,
        "is_xsn":            is_xsn,
        "is_provisional":    is_provisional,
        "frame_rate":        xsn_frame_rate,
        "total_frames_in_file": total_frames,
    }


# ── Internal helpers ──────────────────────────────────────────────────────────

def _trim_frames(raw_dict: dict, threshold: float = 1.0) -> dict:
    """Jatin Files viewing logic:
    1. Filter out corrupted frames (>2700 or hardware glitch where >2% sensels are >200 PSI).
    2. Require > 100 sensels with pressure > threshold.
    3. Compute load per valid frame and locate peak contact frame.
    4. Return a 50-frame playback window centered around the peak contact event.
    """
    frame_numbers = sorted(raw_dict.keys())
    num_frames = len(frame_numbers)
    max_valid_frame = min(2700, num_frames)

    valid_fns = [
        fn for fn in frame_numbers[:max_valid_frame]
        if np.mean(raw_dict[fn] > 200.0) < 0.02 and np.sum(raw_dict[fn] > threshold) >= CONTACT_AREA_MIN
    ]
    if not valid_fns:
        valid_fns = frame_numbers[:min(50, num_frames)]
        return {fn: raw_dict[fn] for fn in valid_fns}

    # Calculate load per frame to find peak contact frame
    valid_loads = [
        float(np.sum(raw_dict[fn][(raw_dict[fn] > threshold) & (raw_dict[fn] < 200.0)]))
        for fn in valid_fns
    ]
    peak_sub_idx = int(np.argmax(valid_loads))

    # 50-frame playback window centered around peak contact
    half_window = 25
    start_idx = max(0, peak_sub_idx - half_window)
    end_idx = min(len(valid_fns), peak_sub_idx + half_window + 1)
    playback_fns = valid_fns[start_idx:end_idx]

    return {fn: raw_dict[fn] for fn in playback_fns}




def _frame_metrics_13(
    arr: np.ndarray,
    frame_number: int,
    threshold: float,
    position: str,
    sensel_w_mm: float,
    sensel_h_mm: float,
    vmax_psi: float,
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
            "maximum_length_mm":    0.0,
            "maximum_width_mm":     0.0,
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

    # Outer Shoulder S1 vs Inner Shoulder S2 (outer 20% strips)
    shoulder_w_sensels = max(2, int(round(0.20 * bbox_w_sensels)))
    left_strip_lens  = col_lengths[:shoulder_w_sensels]
    right_strip_lens = col_lengths[-shoulder_w_sensels:]

    left_s_mm  = float(np.mean(left_strip_lens)) * sensel_h_mm if left_strip_lens else 0.0
    right_s_mm = float(np.mean(right_strip_lens)) * sensel_h_mm if right_strip_lens else 0.0

    # FL & RL (Left of car): Outer = Left, Inner = Right
    # FR & RR (Right of car): Outer = Right, Inner = Left
    if position in ("FL", "RL"):
        s1 = round(left_s_mm, 1)
        s2 = round(right_s_mm, 1)
    else:
        s1 = round(right_s_mm, 1)
        s2 = round(left_s_mm, 1)

    s_avg_val = (s1 + s2) / 2.0
    shoulder_skewness_pct = round(((s1 - s2) / s_avg_val) * 100.0, 1) if s_avg_val > 0 else 0.0
    squareness_s1_s2_pct  = round((s1 / s2) * 100.0, 1) if s2 > 0 else 100.0
    squareness_avg        = round(s_avg_val / fp_length_mm, 2) if fp_length_mm > 0 else 0.0

    sea_sensels    = bbox_area_sensels - active
    land_sea_ratio = round(active / sea_sensels, 2) if sea_sensels > 0 else 0.0

    total_sensels = arr.shape[0] * arr.shape[1]

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
        "peak_pressure_psi":     round(peak_psi, 2),
        "avg_pressure_psi":      round(avg_psi, 2),
        "estimated_load_kg":     round(total_load_kg, 1),
        "active_sensels":        active,
        "contact_pct":           round(100.0 * active / total_sensels, 2),
    }


def _contact_patch_exact(
    arr: np.ndarray,
    threshold: float,
    sensel_w_mm: float,
    sensel_h_mm: float,
) -> dict:
    """Compute the exact tyre contact outline (per-row edge points) + bounding box.

    Returns a dictionary with:
      - width_mm, length_mm  : bounding-box dimensions
      - bbox                 : [col_min, row_min_orig, col_max, row_max_orig] in
                               origin-lower (XSENSOR convention)
      - outline_points       : list of {x, y} in canvas sensel coords (origin-upper,
                               as returned after _serialise_frame flip).
                               These trace the exact outer edge of the contact patch.
    """
    mask = arr > threshold
    rows_any = np.any(mask, axis=1)
    cols_any = np.any(mask, axis=0)

    if not rows_any.any():
        return {
            "width_mm": 0, "length_mm": 0,
            "bbox": [0, 0, 0, 0],
            "outline_points": [],
        }

    row_min = int(np.argmax(rows_any))
    row_max = int(len(rows_any) - 1 - np.argmax(rows_any[::-1]))
    col_min = int(np.argmax(cols_any))
    col_max = int(len(cols_any) - 1 - np.argmax(cols_any[::-1]))

    length_mm = round((row_max - row_min + 1) * sensel_h_mm, 1)
    width_mm  = round((col_max - col_min + 1) * sensel_w_mm, 1)

    H = arr.shape[0]

    # Build exact outline by tracing left and right edges of each row,
    # then top and bottom edges of each column.
    outline: list[dict] = []

    # Right-side outline (left edge going down, then right edge going up)
    left_edge: list[tuple[int, int]] = []
    right_edge: list[tuple[int, int]] = []

    for r in range(row_min, row_max + 1):
        row_mask = mask[r, :]
        if not np.any(row_mask):
            continue
        c_l = int(np.argmax(row_mask))
        c_r = int(len(row_mask) - 1 - np.argmax(row_mask[::-1]))
        # canvas y = H - 1 - r  (flip because _serialise_frame uses flipud)
        canvas_y = H - 1 - r
        left_edge.append((c_l, canvas_y))
        right_edge.append((c_r, canvas_y))

    # Combine: left edge top→bottom, right edge bottom→top = closed polygon
    outline_points = [{"x": x, "y": y} for x, y in left_edge]
    outline_points += [{"x": x, "y": y} for x, y in reversed(right_edge)]

    # Also add top and bottom edges (col scan)
    top_edge: list[tuple[int, int]] = []
    bot_edge: list[tuple[int, int]] = []
    for c in range(col_min, col_max + 1):
        col_mask = mask[:, c]
        if not np.any(col_mask):
            continue
        r_top = int(np.argmax(col_mask))
        r_bot = int(len(col_mask) - 1 - np.argmax(col_mask[::-1]))
        top_edge.append((c, H - 1 - r_top))
        bot_edge.append((c, H - 1 - r_bot))

    # Final outline: combine left + top + right + bottom for a polygon
    # Simplified: use left/right edge polygon which gives the clean shape
    # (top/bottom edges fill in, but for stroke outline the left+right is enough)
    outline_points = [{"x": x, "y": y} for x, y in left_edge]
    outline_points += [{"x": x, "y": y} for x, y in reversed(right_edge)]

    # bbox in origin-lower convention (for backend compatibility)
    orig_r0 = H - 1 - row_max
    orig_r1 = H - 1 - row_min
    bbox = [col_min, orig_r0, col_max, orig_r1]

    return {
        "width_mm":  width_mm,
        "length_mm": length_mm,
        "bbox":      bbox,
        "outline_points": outline_points,
    }


def _serialise_frame(arr: np.ndarray, vmax_psi: float) -> list:
    """Return a flat list of uint8 values (0-255) for canvas rendering.

    Normalised against vmax_psi.  Origin=lower → flip vertically so row 0
    is at top in canvas.  Correctly maps PSI → jet LUT index.
    """
    clipped    = np.clip(arr, 0, vmax_psi)
    normalised = (clipped / vmax_psi * 255).astype(np.uint8)
    flipped    = np.flipud(normalised)
    return flipped.flatten().tolist()
