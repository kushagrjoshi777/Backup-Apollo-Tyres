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
import cv2
import math
from dataclasses import dataclass, field

@dataclass
class PipelineConfig:
    tyre_type: str = "TBR"
    pressure_ceiling: float = 220.0
    min_pressure: float = 5.0
    fft_enabled: bool = True
    fft_cutoff: float = 0.97
    fft_filter_strength: int = 2
    temporal_filter_enabled: bool = True
    temporal_spike_threshold: float = 3.0
    spatial_filter_enabled: bool = True  
    spatial_mad_threshold: float = 4.0
    patch_filter_enabled: bool = True
    sensel_area_mm2: float = 2.434
    min_patch_area_mm2: float = 12.0
    patch_preserve_threshold_psi: float = 20.0
    lineout_detection_enabled: bool = False
    lineout_threshold_pct: float = 50.0
    baseline_frame_index: int | None = None

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
    config: PipelineConfig,
) -> tuple[np.ndarray, np.ndarray, int, float]:
    """
    XSENSOR-inspired Fourier noise reduction.
    """
    if not config.fft_enabled:
        return frame_psi.copy(), np.zeros_like(frame_psi), 0, 0.0
        
    med = median_filter(frame_psi, size=3)
    diff = np.abs(frame_psi - med)
    spike_mask = diff > (20.0 + 0.3 * med)
    spike_mask &= (frame_psi > config.min_pressure)
    n_spikes = int(np.sum(spike_mask))
    
    clean_frame = frame_psi.copy()
    clean_frame[spike_mask] = med[spike_mask]

    rows, cols = clean_frame.shape
    f_shift = fftshift(fft2(clean_frame.astype(np.float64)))
    cy, cx = rows // 2, cols // 2
    y, x = np.ogrid[-cy : rows - cy, -cx : cols - cx]
    d = np.sqrt(x**2 + y**2)

    f_nyquist = min(rows, cols) / 2.0
    d0 = max(10.0, min(f_nyquist * 0.9, config.fft_cutoff * f_nyquist * 0.85))
    h_butterworth = 1.0 / (1.0 + (d / max(1.0, d0)) ** (2 * config.fft_filter_strength))

    filtered = np.real(ifft2(ifftshift(f_shift * h_butterworth)))
    filtered = np.clip(filtered, 0.0, None)

    filtered[filtered < config.min_pressure] = 0.0

    noise = clean_frame - filtered
    energy_removed = float(np.sum(np.abs(noise)))
    
    return filtered.astype(np.float32), noise.astype(np.float32), n_spikes, energy_removed


def zero_pressure_baseline(frames: dict[int, np.ndarray], config: PipelineConfig) -> tuple[np.ndarray | None, dict]:
    """Find and return a zero-pressure baseline frame.
    
    Auto-detection: finds frame with fewest active sensels (< 50 above min_pressure).
    If config.baseline_frame_index is set, uses that specific frame.
    Returns (baseline_frame_or_None, diagnostics_dict).
    """
    diag = {"baseline_frame_index": None, "baseline_min": 0, "baseline_max": 0, 
            "baseline_mean": 0, "baseline_p95": 0, "baseline_p99": 0, "method": "none"}
    
    if config.baseline_frame_index is not None:
        idx = config.baseline_frame_index
        if idx in frames:
            bl = frames[idx].astype(np.float32)
            diag.update({"baseline_frame_index": idx, "method": "manual",
                         "baseline_min": float(np.min(bl)), "baseline_max": float(np.max(bl)),
                         "baseline_mean": float(np.mean(bl)), 
                         "baseline_p95": float(np.percentile(bl, 95)),
                         "baseline_p99": float(np.percentile(bl, 99))})
            return bl, diag
    
    sorted_keys = sorted(frames.keys())
    min_active = float('inf')
    best_bl_idx = None
    for k in sorted_keys[:20]:
        active = int(np.sum(frames[k] > config.min_pressure))
        if active < min_active:
            min_active = active
            best_bl_idx = k
    
    if best_bl_idx is not None and min_active < 50:
        bl = frames[best_bl_idx].astype(np.float32)
        diag.update({"baseline_frame_index": best_bl_idx, "method": "auto_min_contact",
                     "baseline_min": float(np.min(bl)), "baseline_max": float(np.max(bl)),
                     "baseline_mean": float(np.mean(bl)),
                     "baseline_p95": float(np.percentile(bl, 95)),
                     "baseline_p99": float(np.percentile(bl, 99))})
        return bl, diag
    
    return None, diag

def temporal_antispeckle(frames_dict: dict[int, np.ndarray], config: PipelineConfig) -> tuple[dict[int, np.ndarray], dict]:
    """Temporal consistency check across consecutive frames.
    
    For each sensel, if current value deviates extremely from temporal neighbours,
    replace with spatial 8-neighbour median estimate.
    """
    keys = sorted(frames_dict.keys())
    result = {}
    total_spikes = 0
    total_repaired = 0
    
    for idx_i, k in enumerate(keys):
        current = frames_dict[k].copy()
        
        if not config.temporal_filter_enabled or len(keys) < 3:
            result[k] = current
            continue
        
        prev_frame = frames_dict[keys[max(0, idx_i - 1)]]
        next_frame = frames_dict[keys[min(len(keys) - 1, idx_i + 1)]]
        
        stack = np.stack([prev_frame, current, next_frame], axis=0)
        temporal_median = np.median(stack, axis=0)
        temporal_std = np.std(stack, axis=0)
        
        safe_std = np.maximum(temporal_std, 5.0)
        deviation = np.abs(current - temporal_median)
        spike_mask = deviation > (config.temporal_spike_threshold * safe_std)
        spike_mask &= (current > config.min_pressure)
        
        n_spikes = int(np.sum(spike_mask))
        total_spikes += n_spikes
        
        if n_spikes > 0:
            spatial_med = median_filter(current, size=3)
            current[spike_mask] = spatial_med[spike_mask]
            total_repaired += n_spikes
        
        result[k] = current
    
    diag = {"temporal_spikes_detected": total_spikes, "temporal_spikes_repaired": total_repaired}
    return result, diag

def spatial_spike_filter(frame: np.ndarray, config: PipelineConfig) -> tuple[np.ndarray, int]:
    """Conservative 3x3 spatial outlier detection using Median Absolute Deviation.
    
    Preserves tread ribs, grooves, and legitimate pressure gradients.
    Only replaces genuinely inconsistent isolated values.
    """
    if not config.spatial_filter_enabled:
        return frame.copy(), 0
    
    result = frame.copy()
    med = median_filter(frame, size=3)
    
    diff = np.abs(frame - med)
    mad = median_filter(diff, size=3)
    mad_safe = np.maximum(mad, 2.0)
    
    spike_mask = diff > (config.spatial_mad_threshold * mad_safe)
    spike_mask &= (frame > config.min_pressure)
    
    n_spikes = int(np.sum(spike_mask))
    if n_spikes > 0:
        result[spike_mask] = med[spike_mask]
    
    return result, n_spikes

def sensel_patch_filter(frame: np.ndarray, config: PipelineConfig) -> tuple[np.ndarray, dict]:
    """XSENSOR-inspired sensel patch filtering.
    
    Remove tiny isolated patches while preserving patches with high pressure.
    Uses 8-connected component analysis.
    """
    if not config.patch_filter_enabled:
        return frame.copy(), {"patches_detected": 0, "patches_removed": 0, 
                               "sensels_removed": 0, "sensels_preserved_by_pressure": 0}
    
    threshold_count = math.ceil(config.min_patch_area_mm2 / config.sensel_area_mm2)
    
    binary = (frame >= config.min_pressure).astype(np.uint8)
    if not np.any(binary):
        return frame.copy(), {"patches_detected": 0, "patches_removed": 0,
                               "sensels_removed": 0, "sensels_preserved_by_pressure": 0}
    
    num_labels, labels, stats, _ = cv2.connectedComponentsWithStats(binary, connectivity=8)
    
    result = frame.copy()
    patches_detected = num_labels - 1
    patches_removed = 0
    sensels_removed = 0
    sensels_preserved = 0
    
    for i in range(1, num_labels):
        area = stats[i, cv2.CC_STAT_AREA]
        if area < threshold_count:
            component_mask = (labels == i)
            max_pressure_in_patch = float(np.max(frame[component_mask]))
            if max_pressure_in_patch < config.patch_preserve_threshold_psi:
                result[component_mask] = 0.0
                patches_removed += 1
                sensels_removed += area
            else:
                sensels_preserved += area
                
    diag = {
        "patches_detected": patches_detected,
        "patches_removed": patches_removed,
        "sensels_removed": sensels_removed,
        "sensels_preserved_by_pressure": sensels_preserved,
    }
    return result, diag

def _segment_contact(
    arr: np.ndarray,
    threshold: float = 5.0,
    min_area: int = 12,
) -> np.ndarray:
    """
    Connected-component analysis to isolate genuine tyre contact patches
    and reject background noise dots/artifacts.
    """
    binary = (arr >= threshold).astype(np.uint8)
    if not np.any(binary):
        return np.zeros(arr.shape, dtype=bool)

    num_labels, labels, stats, _ = cv2.connectedComponentsWithStats(binary, connectivity=8)
    clean_mask = np.zeros(arr.shape, dtype=bool)

    for i in range(1, num_labels):
        area = stats[i, cv2.CC_STAT_AREA]
        if area >= min_area:
            clean_mask[labels == i] = True

    return clean_mask


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
    Full pipeline: parse (CSV or XSN) → calibrate → baseline → saturation handling → 
    temporal filter → spatial filter → FFT → patch filter → metrics → serialise.
    """
    vmax_psi = max_pressure

    meta = extract_metadata_from_file(filepath)
    if speed == 0.0 and meta["speed"] > 0:
        speed = meta["speed"]
    if position not in ("FL", "FR", "RL", "RR") or position == "FR":
        if meta["position"] in ("FL", "FR", "RL", "RR"):
            position = meta["position"]

    sensel_w = meta.get("sensel_width_mm", DEFAULT_SENSEL_MM)
    sensel_h = meta.get("sensel_height_mm", DEFAULT_SENSEL_MM)
    sensel_area_mm2 = sensel_w * sensel_h

    is_xsn = meta["is_xsn"]
    
    config = PipelineConfig(
        tyre_type=tyre_type.upper(),
        pressure_ceiling=max_pressure,
        min_pressure=min_pressure,
        fft_enabled=True,
        fft_cutoff=fft_keep_ratio,
        sensel_area_mm2=sensel_area_mm2,
    )

    parsed = parse_file_extended(filepath, max_frames=MAX_XSN_FRAMES, tyre_type=config.tyre_type, pressure_ceiling=config.pressure_ceiling)
    calibrated_frames = parsed["frames"]
    raw_frames = parsed.get("raw_frames", {})
    calibration_results = parsed.get("calibration_results", {})
    xsn_frame_rate = parsed.get("frame_rate")
    is_provisional = parsed.get("is_provisional", False)
    total_frames = parsed.get("total_frames", len(calibrated_frames))
    raw_diagnostics = parsed.get("raw_diagnostics", {})

    if not calibrated_frames:
        raise ValueError("No frames found in the file.")

    # 1. Zero-pressure baseline
    baseline_frame, baseline_diag = zero_pressure_baseline(calibrated_frames, config)
    if baseline_frame is not None:
        for k in calibrated_frames:
            calibrated_frames[k] = np.maximum(calibrated_frames[k] - baseline_frame, 0.0)

    # 2. Select valid frames & 50-frame playback window
    trimmed = _trim_frames(calibrated_frames, config.pressure_ceiling, min_pressure)
    if not trimmed:
        trimmed = calibrated_frames

    frame_numbers = sorted(trimmed.keys())
    
    # 3. Temporal Anti-speckle (only on playback window)
    playback_frames = {fn: trimmed[fn] for fn in frame_numbers}
    temporal_filtered, temporal_diag = temporal_antispeckle(playback_frames, config)

    # Pipeline processing and diagnostics
    filtered_frames = {}
    pixel_data_store = {}
    frame_diagnostics = []
    
    total_repaired_spikes = temporal_diag.get("temporal_spikes_repaired", 0)
    total_invalid = 0
    total_saturated = 0

    for fn in frame_numbers:
        arr = temporal_filtered[fn]
        
        # Diagnostics base
        raw_min = raw_max = raw_mean = 0.0
        invalid_count = saturation_count = 0
        
        if is_xsn and fn in raw_frames:
            raw_matrix = raw_frames[fn]
            flat_raw = raw_matrix.flatten()
            nonzero_raw = flat_raw[flat_raw > 0]
            raw_min = float(np.min(nonzero_raw)) if len(nonzero_raw) > 0 else 0.0
            raw_max = float(np.max(flat_raw))
            raw_mean = float(np.mean(nonzero_raw)) if len(nonzero_raw) > 0 else 0.0
            
            calib_res = calibration_results[fn]
            invalid_count = int(np.sum(calib_res.invalid_mask))
            saturation_count = int(np.sum(calib_res.saturation_mask))
            total_invalid += invalid_count
            total_saturated += saturation_count
            
        flat_cal = arr.flatten()
        active_cal = flat_cal[flat_cal > config.min_pressure]
        cal_min = float(np.min(active_cal)) if len(active_cal) > 0 else 0.0
        cal_max = float(np.max(flat_cal))
        cal_mean = float(np.mean(active_cal)) if len(active_cal) > 0 else 0.0
        cal_p95 = float(np.percentile(flat_cal, 95))
        cal_p98 = float(np.percentile(flat_cal, 98))
        
        # 4. Spatial Filter
        spatial_filtered, n_spatial_spikes = spatial_spike_filter(arr, config)
        total_repaired_spikes += n_spatial_spikes
        
        # 5. FFT
        fft_filtered, noise, n_fft_spikes, energy_removed = apply_fft_butterworth(spatial_filtered, config)
        
        # 6. Patch filter
        patch_filtered, patch_diag = sensel_patch_filter(fft_filtered, config)
        
        # 7. Contact segmentation
        mask = _segment_contact(patch_filtered, min_pressure)
        segmented = np.where(mask, patch_filtered, 0.0).astype(np.float32)
        
        filtered_frames[fn] = segmented
        
        # Store for pixel inspector (just the required fields, we will extract for best frame later)
        pixel_data_store[fn] = {
            "spatial_filtered": spatial_filtered,
            "segmented": segmented,
            "spatial_spikes": n_spatial_spikes,
        }
        
        # Diagnostics
        active_sensels = int(np.sum(segmented > config.min_pressure))
        frame_diagnostics.append({
            "frame_index": fn,
            "raw_min": raw_min, "raw_max": raw_max, "raw_mean": raw_mean,
            "calibrated_min": cal_min, "calibrated_max": cal_max, "calibrated_mean": cal_mean,
            "calibrated_p95": cal_p95, "calibrated_p98": cal_p98,
            "active_sensels": active_sensels, 
            "contact_area_mm2": active_sensels * sensel_area_mm2,
            "total_load_n": float(np.sum(segmented) * 0.00689476 * sensel_area_mm2),
            "invalid_count": invalid_count, 
            "saturation_count": saturation_count,
            "spatial_spike_count": n_spatial_spikes, 
            "temporal_spike_count": temporal_diag.get("temporal_spikes_detected", 0) if fn == frame_numbers[0] else 0,
        })

    # Metrics
    metrics_list = [
        _frame_metrics_13(
            filtered_frames[fn], fn, min_pressure, position, sensel_w, sensel_h, vmax_psi
        )
        for fn in frame_numbers
    ]

    best_idx  = int(np.argmax([m["active_sensels"] for m in metrics_list]))
    best_fn   = frame_numbers[best_idx]
    best_arr  = filtered_frames[best_fn]

    patch = _contact_patch_exact(best_arr, min_pressure, sensel_w, sensel_h)

    frames_data = {
        str(fn): _serialise_frame(filtered_frames[fn], vmax_psi)
        for fn in frame_numbers
    }
    
    # Build calibration audit
    calib_audit = {
        "sensor": "XSENSOR HX510" if is_xsn else "Unknown CSV",
        "vehicle_type": config.tyre_type,
        "pressure_range": f"0-{config.pressure_ceiling} PSI",
        "calibration_source": "Empirical cross-validation with CSV" if is_xsn else "CSV",
        "calibration_method": parsed.get("calibration_method", "UNKNOWN"),
        "raw_domain": "uint16 [0, 65535]" if is_xsn else "N/A",
        "psi_domain": "float32 [0, 511.99]",
        "offset": 0.0,
        "scale": 0.0078125 if is_xsn else 1.0,
        "saturated_values": total_saturated,
        "invalid_values": total_invalid,
        "repaired_spikes": total_repaired_spikes,
        "baseline_frame": baseline_diag.get("baseline_frame_index"),
        "calibration_confidence": parsed.get("calibration_confidence", "UNKNOWN"),
    }
    
    # Build Pixel data for best frame
    best_pixel_data = {
        "grid_rows": 256,
        "grid_cols": 256,
        "calibration_method": calib_audit["calibration_method"],
        "pressure_range": config.tyre_type,
        "pressure_ceiling": config.pressure_ceiling,
        "calibration_confidence": calib_audit["calibration_confidence"],
    }
    
    if is_xsn and best_fn in raw_frames and best_fn in calibration_results:
        best_pixel_data["best_frame_raw"] = raw_frames[best_fn].flatten().tolist()
        calib_res = calibration_results[best_fn]
        best_pixel_data["best_frame_calibrated"] = calib_res.calibrated_psi.flatten().tolist()
        best_pixel_data["invalid_mask"] = calib_res.invalid_mask.flatten().tolist()
        best_pixel_data["saturation_mask"] = calib_res.saturation_mask.flatten().tolist()
    else:
        best_pixel_data["best_frame_raw"] = []
        best_pixel_data["best_frame_calibrated"] = calibrated_frames[best_fn].flatten().tolist() if best_fn in calibrated_frames else []
        best_pixel_data["invalid_mask"] = []
        best_pixel_data["saturation_mask"] = []
        
    if best_fn in pixel_data_store:
        pds = pixel_data_store[best_fn]
        best_pixel_data["best_frame_filtered"] = pds["spatial_filtered"].flatten().tolist()
        best_pixel_data["best_frame_final"] = pds["segmented"].flatten().tolist()
        best_pixel_data["contact_mask"] = (pds["segmented"] > min_pressure).flatten().tolist()
        
        # Approximate spike mask
        if best_fn in calibrated_frames:
            orig = calibrated_frames[best_fn]
            diff = np.abs(orig - pds["spatial_filtered"])
            best_pixel_data["spike_mask"] = (diff > 0.1).flatten().tolist()
        else:
            best_pixel_data["spike_mask"] = []
    else:
        best_pixel_data["best_frame_filtered"] = []
        best_pixel_data["best_frame_final"] = []
        best_pixel_data["contact_mask"] = []
        best_pixel_data["spike_mask"] = []

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
        "tyre_type":         config.tyre_type,
        "fft_keep_ratio":    fft_keep_ratio,
        "sensel_w_mm":       sensel_w,
        "sensel_h_mm":       sensel_h,
        "is_xsn":            is_xsn,
        "is_provisional":    is_provisional,
        "frame_rate":        xsn_frame_rate,
        "total_frames_in_file": total_frames,
        "calibration_audit": calib_audit,
        "frame_diagnostics": frame_diagnostics,
        "baseline_diagnostics": baseline_diag,
        "pipeline_config": config.__dict__,
        "pixel_data": best_pixel_data,
    }


# ── Internal helpers ──────────────────────────────────────────────────────────

def _trim_frames(raw_dict: dict, pressure_ceiling: float, threshold: float = 1.0) -> dict:
    """Jatin Files viewing logic:
    1. Filter out corrupted frames (>2700 or hardware glitch where >2% sensels are > pressure_ceiling * 1.15).
    2. Require > 100 sensels with pressure > threshold.
    3. Compute load per valid frame and locate peak contact frame.
    4. Return a 50-frame playback window centered around the peak contact event.
    """
    frame_numbers = sorted(raw_dict.keys())
    num_frames = len(frame_numbers)
    max_valid_frame = min(2700, num_frames)

    valid_fns = [
        fn for fn in frame_numbers[:max_valid_frame]
        if np.mean(raw_dict[fn] > pressure_ceiling * 1.15) < 0.02 and np.sum(raw_dict[fn] > threshold) >= CONTACT_AREA_MIN
    ]
    if not valid_fns:
        valid_fns = frame_numbers[:min(50, num_frames)]
        return {fn: raw_dict[fn] for fn in valid_fns}

    # Calculate load per frame to find peak contact frame
    valid_loads = [
        float(np.sum(raw_dict[fn][(raw_dict[fn] > threshold) & (raw_dict[fn] < pressure_ceiling * 1.15)]))
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
    """Compute all standard tyre footprint parameters per Apollo Tyres reference spec.

    Reference definitions:
      FP Width W  — transverse width at the centre of FP (shoulder-to-shoulder)
      FP Length L — longitudinal length at the centre of FP (rolling direction)
      S1/S2       — shoulder contact lengths at 10% inset from outer tread edges
      Squareness  — (S1/L)*100, (S2/L)*100, and Avg Squareness
      PA          — perimeter area (convex hull enclosing footprint)
      CA          — contact area (active sensels only, excludes grooves)
      Contact Ratio — (CA / PA) * 100
    """
    proc_thresh = max(threshold, 8.0) if threshold <= 5.0 else threshold
    mask = _segment_contact(arr, proc_thresh)
    active = int(np.sum(mask))

    sensel_area_mm2 = sensel_w_mm * sensel_h_mm
    peak_psi = float(np.max(arr[mask])) if active > 0 else 0.0
    avg_psi  = float(np.mean(arr[mask])) if active > 0 else 0.0

    contact_area_mm2 = active * sensel_area_mm2  # CA
    PSI_TO_N_PER_MM2 = 0.00689476
    total_load_n     = float(np.sum(arr[mask]) * PSI_TO_N_PER_MM2 * sensel_area_mm2)
    total_load_kg    = total_load_n / 9.80665

    rows_any = np.any(mask, axis=1)
    cols_any = np.any(mask, axis=0)

    if not rows_any.any() or not cols_any.any():
        return {
            "frame_number":         frame_number,
            "contact_area_mm2":     0.0,
            "perimeter_area_mm2":   0.0,
            "contact_ratio_pct":    0.0,
            "fp_width_mm":          0.0,
            "fp_length_mm":         0.0,
            "outer_shoulder_s1_mm": 0.0,
            "inner_shoulder_s2_mm": 0.0,
            "shoulder_skewness_pct":0.0,
            "squareness_s1_pct":    0.0,
            "squareness_s2_pct":    0.0,
            "squareness_avg_pct":   0.0,
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

    height_span_mm = (r_max - r_min + 1) * sensel_h_mm
    width_span_mm = (c_max - c_min + 1) * sensel_w_mm

    is_horizontal_roll = width_span_mm > height_span_mm

    if is_horizontal_roll:
        c_mid = (c_min + c_max) // 2
        r_mid = (r_min + r_max) // 2

        col_mask = mask[:, c_mid]
        if np.any(col_mask):
            w_span = (len(col_mask) - 1 - np.argmax(col_mask[::-1])) - np.argmax(col_mask) + 1
        else:
            w_span = r_max - r_min + 1
        fp_width_mm = round(w_span * sensel_h_mm, 1)

        row_mask = mask[r_mid, :]
        if np.any(row_mask):
            l_span = (len(row_mask) - 1 - np.argmax(row_mask[::-1])) - np.argmax(row_mask) + 1
        else:
            l_span = c_max - c_min + 1
        fp_length_mm = round(l_span * sensel_w_mm, 1)

        max_w_sens = max([(len(mask[:, c]) - 1 - np.argmax(mask[:, c][::-1])) - np.argmax(mask[:, c]) + 1 
                          for c in range(c_min, c_max + 1) if np.any(mask[:, c])], default=r_max - r_min + 1)
        max_l_sens = max([(len(mask[r, :]) - 1 - np.argmax(mask[r, :][::-1])) - np.argmax(mask[r, :]) + 1 
                          for r in range(r_min, r_max + 1) if np.any(mask[r, :])], default=c_max - c_min + 1)

        max_width_mm = round(max_w_sens * sensel_h_mm, 1)
        max_length_mm = round(max_l_sens * sensel_w_mm, 1)

        n_transverse = r_max - r_min + 1
        inset = max(1, int(round(0.10 * n_transverse)))
        r_s1 = min(r_max, r_min + inset)
        r_s2 = max(r_min, r_max - inset)

        def _get_row_rolling_length(row_idx):
            rm = mask[row_idx, :]
            if not np.any(rm): return 0.0
            span = (len(rm) - 1 - np.argmax(rm[::-1])) - np.argmax(rm) + 1
            return float(span * sensel_w_mm)

        s_top = _get_row_rolling_length(r_s1)
        s_bot = _get_row_rolling_length(r_s2)

        if position in ("FL", "RL"):
            s1, s2 = round(s_top, 1), round(s_bot, 1)
        else:
            s1, s2 = round(s_bot, 1), round(s_top, 1)

    else:
        c_mid = (c_min + c_max) // 2
        r_mid = (r_min + r_max) // 2

        row_mask = mask[r_mid, :]
        if np.any(row_mask):
            w_span = (len(row_mask) - 1 - np.argmax(row_mask[::-1])) - np.argmax(row_mask) + 1
        else:
            w_span = c_max - c_min + 1
        fp_width_mm = round(w_span * sensel_w_mm, 1)

        col_mask = mask[:, c_mid]
        if np.any(col_mask):
            l_span = (len(col_mask) - 1 - np.argmax(col_mask[::-1])) - np.argmax(col_mask) + 1
        else:
            l_span = r_max - r_min + 1
        fp_length_mm = round(l_span * sensel_h_mm, 1)

        max_w_sens = max([(len(mask[r, :]) - 1 - np.argmax(mask[r, :][::-1])) - np.argmax(mask[r, :]) + 1 
                          for r in range(r_min, r_max + 1) if np.any(mask[r, :])], default=c_max - c_min + 1)
        max_l_sens = max([(len(mask[:, c]) - 1 - np.argmax(mask[:, c][::-1])) - np.argmax(mask[:, c]) + 1 
                          for c in range(c_min, c_max + 1) if np.any(mask[:, c])], default=r_max - r_min + 1)

        max_width_mm = round(max_w_sens * sensel_w_mm, 1)
        max_length_mm = round(max_l_sens * sensel_h_mm, 1)

        n_transverse = c_max - c_min + 1
        inset = max(1, int(round(0.10 * n_transverse)))
        c_s1 = min(c_max, c_min + inset)
        c_s2 = max(c_min, c_max - inset)

        def _get_col_rolling_length(col_idx):
            cm = mask[:, col_idx]
            if not np.any(cm): return 0.0
            span = (len(cm) - 1 - np.argmax(cm[::-1])) - np.argmax(cm) + 1
            return float(span * sensel_h_mm)

        s_left = _get_col_rolling_length(c_s1)
        s_right = _get_col_rolling_length(c_s2)

        if position in ("FL", "RL"):
            s1, s2 = round(s_left, 1), round(s_right, 1)
        else:
            s1, s2 = round(s_right, 1), round(s_left, 1)

    max_s = max(s1, s2)
    shoulder_skewness_pct = round((abs(s1 - s2) / max_s) * 100.0, 1) if max_s > 0 else 0.0

    if fp_length_mm > 0:
        squareness_s1_pct = round((s1 / fp_length_mm) * 100.0, 1)
        squareness_s2_pct = round((s2 / fp_length_mm) * 100.0, 1)
        squareness_avg_pct = round(((s1 + s2) / (2.0 * fp_length_mm)) * 100.0, 1)
    else:
        squareness_s1_pct = squareness_s2_pct = squareness_avg_pct = 0.0

    points = np.argwhere(mask)
    if len(points) >= 3:
        pts_mm = np.column_stack((points[:, 1] * sensel_w_mm, points[:, 0] * sensel_h_mm)).astype(np.float32)
        hull = cv2.convexHull(pts_mm)
        perimeter_area_mm2 = float(cv2.contourArea(hull))
    else:
        perimeter_area_mm2 = contact_area_mm2

    perimeter_area_mm2 = max(perimeter_area_mm2, contact_area_mm2)

    contact_ratio_pct = round((contact_area_mm2 / perimeter_area_mm2) * 100.0, 1) if perimeter_area_mm2 > 0 else 0.0

    sea_area = perimeter_area_mm2 - contact_area_mm2
    land_sea_ratio = round(contact_area_mm2 / sea_area, 2) if sea_area > 0 else 0.0

    total_sensels = arr.size
    contact_pct = round((active / total_sensels) * 100.0, 2) if total_sensels > 0 else 0.0

    return {
        "frame_number":          frame_number,
        "contact_area_mm2":      round(contact_area_mm2, 1),
        "perimeter_area_mm2":    round(perimeter_area_mm2, 1),
        "contact_ratio_pct":     contact_ratio_pct,
        "fp_width_mm":           fp_width_mm,
        "fp_length_mm":          fp_length_mm,
        "outer_shoulder_s1_mm":  s1,
        "inner_shoulder_s2_mm":  s2,
        "shoulder_skewness_pct": shoulder_skewness_pct,
        "squareness_s1_pct":     squareness_s1_pct,
        "squareness_s2_pct":     squareness_s2_pct,
        "squareness_avg_pct":    squareness_avg_pct,
        "land_sea_ratio":        land_sea_ratio,
        "contact_pressure_psi":  round(avg_psi, 2),
        "maximum_length_mm":     max_length_mm,
        "maximum_width_mm":      max_width_mm,
        "peak_pressure_psi":     round(peak_psi, 2),
        "avg_pressure_psi":      round(avg_psi, 2),
        "estimated_load_kg":     round(total_load_kg, 1),
        "active_sensels":        active,
        "contact_pct":           contact_pct,
    }



def _contact_patch_exact(
    arr: np.ndarray,
    threshold: float,
    sensel_w_mm: float,
    sensel_h_mm: float,
) -> dict:
    """
    Compute exact tyre contact contour(s) using OpenCV findContours on segmented mask.
    Returns clean outer contours without bounding box distortions or stray lines.
    """
    clean_mask = _segment_contact(arr, threshold)
    H, W = arr.shape

    if not np.any(clean_mask):
        return {
            "width_mm": 0, "length_mm": 0,
            "bbox": [0, 0, 0, 0],
            "outline_points": [],
        }

    rows_any = np.any(clean_mask, axis=1)
    cols_any = np.any(clean_mask, axis=0)
    row_min = int(np.argmax(rows_any))
    row_max = int(len(rows_any) - 1 - np.argmax(rows_any[::-1]))
    col_min = int(np.argmax(cols_any))
    col_max = int(len(cols_any) - 1 - np.argmax(cols_any[::-1]))

    length_mm = round((row_max - row_min + 1) * sensel_h_mm, 1)
    width_mm  = round((col_max - col_min + 1) * sensel_w_mm, 1)

    contours, _ = cv2.findContours(
        clean_mask.astype(np.uint8),
        cv2.RETR_EXTERNAL,
        cv2.CHAIN_APPROX_TC89_KCOS
    )

    outline_contours = []
    for cnt in contours:
        if cv2.contourArea(cnt) < 10:
            continue
        approx = cv2.approxPolyDP(cnt, epsilon=0.5, closed=True)
        points = []
        for pt in approx:
            c, r = int(pt[0][0]), int(pt[0][1])
            # canvas y = H - 1 - r (due to np.flipud in _serialise_frame)
            points.append({"x": c, "y": H - 1 - r})
        if len(points) >= 3:
            outline_contours.append(points)

    outline_points = outline_contours[0] if len(outline_contours) == 1 else outline_contours

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
