"""
processor.py  —  Apollo Tyre Pressure Analysis
Handles: parsing (CSV + XSN), FFT noise reduction, 13 metrics, heatmap serialisation.
"""

import sys
import os
import re
import base64
import numpy as np
from scipy.fft import fft2, ifft2, fftshift, ifftshift
from scipy.ndimage import median_filter
import cv2
import math
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

# All selectable noise-removal engines (key → label); mirrored in the UI.
FILTER_TYPES = (
    "none",
    "gaussian",
    "median",
    "bilateral",
    "fft_butterworth_lp",
    "fft_gaussian_lp",
    "fft_ideal_lp",
    "fft_notch",
)


@dataclass
class PipelineConfig:
    tyre_type: str = "TBR"
    pressure_ceiling: float = 220.0
    min_pressure: float = 18.0           # contact threshold (18.0 PSI for TBR, 5.0 PSI for PCR)
    # ── main noise-removal engine ──
    filter_type: str = "fft_butterworth_lp"
    fft_enabled: bool = True             # legacy flag (derived from filter_type)
    fft_cutoff: float = 0.80             # fraction of Nyquist retained (Fourier filters)
    fft_filter_strength: int = 2         # Butterworth order
    gaussian_sigma: float = 1.0          # spatial Gaussian sigma (sensels)
    median_size: int = 3                 # spatial median kernel (odd)
    bilateral_sigma_color: float = 25.0  # PSI
    bilateral_sigma_space: float = 2.0   # sensels
    notch_sensitivity: float = 6.0       # spectral peak / background ratio
    # ── despeckle stages ──
    temporal_filter_enabled: bool = True
    temporal_spike_threshold: float = 3.0
    spatial_filter_enabled: bool = True
    spatial_mad_threshold: float = 4.0
    # ── clean-up stages ──
    patch_filter_enabled: bool = True
    sensel_area_mm2: float = 2.4336      # 1.56 mm × 1.56 mm
    min_patch_area_mm2: float = 12.0
    patch_preserve_threshold_psi: float = 25.0
    morph_open: int = 0                  # binary opening kernel (0 = off)
    morph_close: int = 0                 # grey closing kernel (0 = off)
    keep_largest_only: bool = False
    min_component_px: int = 12
    # ── misc ──
    lineout_detection_enabled: bool = False
    lineout_threshold_pct: float = 50.0
    baseline_frame_index: int | None = None
    rotation_k: int = 1                  # 90° CCW turns applied for display (1 → rolls bottom→top)


# (type, min, max) — user supplied filter overrides are coerced + clamped here.
_CONFIG_SPEC = {
    "filter_type":                   (str,   None, None),
    "fft_cutoff":                    (float, 0.05, 1.0),
    "fft_filter_strength":           (int,   1,    8),
    "gaussian_sigma":                (float, 0.2,  6.0),
    "median_size":                   (int,   3,    9),
    "bilateral_sigma_color":         (float, 2.0,  120.0),
    "bilateral_sigma_space":         (float, 0.5,  8.0),
    "notch_sensitivity":             (float, 2.0,  20.0),
    "temporal_filter_enabled":       (bool,  None, None),
    "temporal_spike_threshold":      (float, 1.0,  8.0),
    "spatial_filter_enabled":        (bool,  None, None),
    "spatial_mad_threshold":         (float, 1.5,  10.0),
    "patch_filter_enabled":          (bool,  None, None),
    "min_patch_area_mm2":            (float, 0.0,  200.0),
    "patch_preserve_threshold_psi":  (float, 5.0,  200.0),
    "morph_open":                    (int,   0,    7),
    "morph_close":                   (int,   0,    7),
    "keep_largest_only":             (bool,  None, None),
    "min_component_px":              (int,   1,    200),
    "min_pressure":                  (float, 0.0,  100.0),
    "rotation_k":                    (int,   0,    3),
}


def config_with_overrides(base: "PipelineConfig", overrides: dict | None) -> "PipelineConfig":
    """Return a copy of ``base`` with validated user filter overrides applied."""
    cfg = PipelineConfig(**{k: v for k, v in base.__dict__.items()})
    for key, val in (overrides or {}).items():
        spec = _CONFIG_SPEC.get(key)
        if spec is None or val is None:
            continue
        typ, lo, hi = spec
        try:
            if typ is bool:
                val = val if isinstance(val, bool) else str(val).lower() in ("1", "true", "yes", "on")
            elif typ is int:
                val = int(round(float(val)))
            elif typ is float:
                val = float(val)
            else:
                val = str(val)
        except (TypeError, ValueError):
            continue
        if lo is not None:
            val = max(lo, val)
        if hi is not None:
            val = min(hi, val)
        if key == "filter_type" and val not in FILTER_TYPES:
            continue
        if key == "median_size" and val % 2 == 0:
            val += 1
        setattr(cfg, key, val)
    cfg.fft_enabled = cfg.filter_type.startswith("fft_")
    return cfg
# Allow importing from parent src/ directory
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from src.parser import parse_file_extended
from src.xsn_parser import XSNReader

# ── Constants ─────────────────────────────────────────────────────────────────
PRESSURE_THRESHOLD_TBR = 18.0  # PSI — TBR contact threshold per XSENSOR / Apollo spec
PRESSURE_THRESHOLD_PCR = 5.0   # PSI — PCR contact threshold
PRESSURE_THRESHOLD     = 18.0  # Default TBR threshold
CONTACT_AREA_MIN       = 100   # minimum active sensels to count as a "meaningful" frame
DEFAULT_SENSEL_MM      = 1.56  # mm pitch per sensel (40 cm / 256 sensels = 1.5625 mm ≈ 1.56 mm)
XSN_SENSEL_MM          = 1.56  # mm pitch for XSENSOR HX510 sensor mat (verified from XSN header)

# Tyre type → pressure ceiling (PSI)
VMAX_BY_TYPE = {"PCR": 100, "TBR": 220}
DEFAULT_VMAX = 150

# Max frames to process from large files (perf guard)
MAX_XSN_FRAMES = 3000


def default_min_pressure(tyre_type: str = "TBR") -> float:
    return PRESSURE_THRESHOLD_TBR if str(tyre_type).upper() == "TBR" else PRESSURE_THRESHOLD_PCR


# ── Public API ────────────────────────────────────────────────────────────────

def extract_metadata_from_file(filepath: str) -> dict:
    """Extract auto-detected speed, tyre position, and sensel pitch from XSN binary header or CSV header."""
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
    pitch_source = "default"

    if is_xsn:
        try:
            with XSNReader(filepath) as reader:
                pitch = reader.sensel_pitch_mm()
                if pitch is not None:
                    sensel_width_mm, sensel_height_mm = pitch
                    pitch_source = "xsn_header"
        except Exception:
            pass
    else:
        # For CSV files: search header for explicit sensel pitch
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
                            pitch_source = "csv_header"
                        except (IndexError, ValueError):
                            pass
                    if "Sensel Height (cm)" in line:
                        try:
                            sensel_height_mm = float(line.split(",")[1].strip()) * 10.0
                            pitch_source = "csv_header"
                        except (IndexError, ValueError):
                            pass
        except Exception:
            pass

    return {
        "speed": speed,
        "position": position,
        "sensel_width_mm": sensel_width_mm,
        "sensel_height_mm": sensel_height_mm,
        "pitch_source": pitch_source,
        "is_xsn": is_xsn,
    }


def _radial_freq(shape: tuple) -> np.ndarray:
    """Normalised radial spatial frequency (0 = DC, 1.0 = Nyquist along an axis)."""
    fy = np.fft.fftfreq(shape[0])[:, None]
    fx = np.fft.fftfreq(shape[1])[None, :]
    return np.sqrt(fx ** 2 + fy ** 2) / 0.5


def _fourier_transfer(shape: tuple, spectrum_mag: np.ndarray | None, config: PipelineConfig) -> np.ndarray:
    """Build the (un-shifted) Fourier-domain transfer function H for the active filter."""
    r = _radial_freq(shape)
    c = max(config.fft_cutoff, 0.02)
    kind = config.filter_type

    if kind == "fft_butterworth_lp":
        return 1.0 / (1.0 + (r / c) ** (2 * max(1, config.fft_filter_strength)))
    if kind == "fft_gaussian_lp":
        return np.exp(-(r ** 2) / (2.0 * c ** 2))
    if kind == "fft_ideal_lp":
        return (r <= c).astype(np.float64)
    if kind == "fft_notch" and spectrum_mag is not None:
        mags = fftshift(spectrum_mag)
        bg = np.maximum(median_filter(mags, size=9), 1e-6)
        peaks = (mags > config.notch_sensitivity * bg) & (fftshift(r) > 0.10)
        blob = cv2.dilate(peaks.astype(np.float32), np.ones((3, 3), np.uint8))
        blob = cv2.GaussianBlur(blob, (0, 0), 1.0)
        return ifftshift(1.0 - np.clip(blob * 2.0, 0.0, 1.0))
    return np.ones(shape, dtype=np.float64)


def apply_noise_filter(
    frame_psi: np.ndarray,
    config: PipelineConfig,
    support_mask: np.ndarray | None = None,
) -> tuple[np.ndarray, np.ndarray, int, float]:
    """
    Main noise-removal engine. Supports spatial (Gaussian / median / bilateral) and
    Fourier-domain (Butterworth / Gaussian / ideal low-pass, spectral notch) filters.

    CRITICAL: Filtering smooths pressure noise WITHOUT stretching or reshaping the
    footprint. When ``support_mask`` (the un-blurred contact mask) is supplied or derived
    from the pre-smoothed frame, normalized convolution / boundary clamping keeps real
    footprint edges and tread grooves intact so smoothing never bleeds pressure outward
    into non-contact sensels or erodes contact sensels below ``min_pressure``.

    Returns (filtered, removed_noise, n_spikes_repaired, energy_removed).
    """
    kind = config.filter_type
    src = frame_psi.astype(np.float32)
    n_spikes = 0

    if support_mask is None:
        support_mask = src >= config.min_pressure
    else:
        support_mask = np.asarray(support_mask, dtype=bool)

    if kind == "none":
        filtered = np.where(support_mask, src, 0.0).astype(np.float32)
    elif kind in ("gaussian", "median", "bilateral"):
        if kind == "gaussian":
            sigma = float(config.gaussian_sigma)
            # Normalized convolution inside the contact support mask prevents edge bleed/shrinkage
            masked_src = np.where(support_mask, src, 0.0).astype(np.float32)
            weight = support_mask.astype(np.float32)
            num = cv2.GaussianBlur(masked_src, (0, 0), sigma)
            den = cv2.GaussianBlur(weight, (0, 0), sigma)
            filtered = np.where(support_mask & (den > 1e-5), num / np.maximum(den, 1e-5), 0.0).astype(np.float32)
        elif kind == "median":
            med = median_filter(src, size=int(config.median_size)).astype(np.float32)
            # Keep original value if median across groove/edge would drop an active sensel below threshold
            filtered = np.where(
                support_mask,
                np.where(med >= config.min_pressure, med, src),
                0.0,
            ).astype(np.float32)
        else:
            bil = cv2.bilateralFilter(
                src, d=0,
                sigmaColor=float(config.bilateral_sigma_color),
                sigmaSpace=float(config.bilateral_sigma_space),
            ).astype(np.float32)
            filtered = np.where(
                support_mask,
                np.where(bil >= config.min_pressure, bil, src),
                0.0,
            ).astype(np.float32)
    else:
        # Fourier path — first repair hard single-sensel spikes so they do not ring.
        med = median_filter(src, size=3)
        diff = np.abs(src - med)
        spike_mask = (diff > (20.0 + 0.3 * med)) & support_mask
        n_spikes = int(np.sum(spike_mask))
        work_src = src.copy()
        work_src[spike_mask] = np.maximum(med[spike_mask], config.min_pressure)

        pad = int(max(0, min(16, min(work_src.shape) - 1)))
        padded = np.pad(work_src.astype(np.float64), pad, mode="reflect") if pad else work_src.astype(np.float64)
        spectrum = fft2(padded)
        h = _fourier_transfer(padded.shape, np.abs(spectrum), config)
        out = np.real(ifft2(spectrum * h))
        if pad:
            out = out[pad:-pad, pad:-pad]
        out_f32 = out.astype(np.float32)
        # Confine smoothed pressures strictly to the true footprint support mask
        filtered = np.where(
            support_mask,
            np.maximum(out_f32, config.min_pressure),
            0.0,
        ).astype(np.float32)

    noise = np.where(support_mask, src - filtered, 0.0).astype(np.float32)
    energy_removed = float(np.sum(np.abs(noise)))
    return filtered, noise, n_spikes, energy_removed


def apply_fft_butterworth(frame_psi, config):
    """Backward-compatible alias — forwards to the multi-engine noise filter."""
    return apply_noise_filter(frame_psi, config)


def morphological_cleanup(frame: np.ndarray, config: PipelineConfig) -> np.ndarray:
    """Optional binary opening (speck removal) and grey closing (gap bridging)."""
    out = frame
    if config.morph_open and config.morph_open >= 2:
        k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (config.morph_open, config.morph_open))
        mask = (out > 0).astype(np.uint8)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, k)
        out = np.where(mask > 0, out, 0.0).astype(np.float32)
    if config.morph_close and config.morph_close >= 2:
        k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (config.morph_close, config.morph_close))
        out = cv2.morphologyEx(out.astype(np.float32), cv2.MORPH_CLOSE, k)
    return out


def keep_largest_component(frame: np.ndarray, threshold: float) -> np.ndarray:
    """Zero everything except the single largest connected contact region."""
    binary = (frame >= threshold).astype(np.uint8)
    if not np.any(binary):
        return frame
    n, labels, stats, _ = cv2.connectedComponentsWithStats(binary, connectivity=8)
    if n <= 2:
        return frame
    biggest = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    return np.where(labels == biggest, frame, 0.0).astype(np.float32)


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


def prepare_job(
    filepath: str,
    tyre_type: str = "TBR",
    min_pressure: float | None = None,
    max_pressure: float = 220.0,
    speed: float = 0.0,
    position: str = "FR",
) -> dict:
    """
    Parse + calibrate + baseline-subtract + trim once. The returned context is cached so
    the (cheap) filter pipeline can be re-run live with different noise settings.
    """
    tyre_type = (tyre_type or "TBR").upper()
    if min_pressure is None or (tyre_type == "TBR" and abs(min_pressure - 5.0) < 1e-3):
        min_pressure = default_min_pressure(tyre_type)

    meta = extract_metadata_from_file(filepath)
    if speed == 0.0 and meta["speed"] > 0:
        speed = meta["speed"]
    if position not in ("FL", "FR", "RL", "RR") or position == "FR":
        if meta["position"] in ("FL", "FR", "RL", "RR"):
            position = meta["position"]

    sensel_w = meta.get("sensel_width_mm", DEFAULT_SENSEL_MM)
    sensel_h = meta.get("sensel_height_mm", DEFAULT_SENSEL_MM)
    is_xsn = meta["is_xsn"]

    base_config = PipelineConfig(
        tyre_type=tyre_type,
        pressure_ceiling=max_pressure,
        min_pressure=min_pressure,
        sensel_area_mm2=sensel_w * sensel_h,
    )

    parsed = parse_file_extended(
        filepath, max_frames=MAX_XSN_FRAMES,
        tyre_type=base_config.tyre_type, pressure_ceiling=base_config.pressure_ceiling,
    )
    if parsed.get("sensel_width_mm"):
        sensel_w = float(parsed["sensel_width_mm"])
    if parsed.get("sensel_height_mm"):
        sensel_h = float(parsed["sensel_height_mm"])
    base_config.sensel_area_mm2 = sensel_w * sensel_h

    calibrated_frames = parsed["frames"]
    raw_frames = parsed.get("raw_frames", {})
    calibration_results = parsed.get("calibration_results", {})

    if not calibrated_frames:
        raise ValueError("No frames found in the file.")

    # 1. Zero-pressure baseline (only subtract if recording is NOT already thresholded/processed)
    baseline_frame, baseline_diag = zero_pressure_baseline(calibrated_frames, base_config)
    if baseline_frame is not None and float(np.max(baseline_frame)) < min_pressure:
        # Note: Do not subtract sub-threshold noise floor from contact sensels in XSN files,
        # because XSENSOR raw / 128 is already absolute PSI and subtracting background drift
        # shifts the 18 PSI contact threshold. Keep baseline_diag for audit only.
        pass

    # 2. Playback window
    trimmed = _trim_frames(calibrated_frames, base_config.pressure_ceiling, min_pressure)
    if not trimmed:
        trimmed = calibrated_frames
    frame_numbers = sorted(trimmed.keys())

    return {
        "base_config": base_config,
        "trimmed": {fn: np.asarray(trimmed[fn], dtype=np.float32) for fn in frame_numbers},
        "frame_numbers": frame_numbers,
        "raw": {fn: raw_frames[fn] for fn in frame_numbers if fn in raw_frames},
        "calib": {fn: calibration_results[fn] for fn in frame_numbers if fn in calibration_results},
        "baseline_diag": baseline_diag,
        "sensel_w": sensel_w,
        "sensel_h": sensel_h,
        "pitch_source": meta.get("pitch_source", "default"),
        "is_xsn": is_xsn,
        "speed": speed,
        "position": position,
        "vmax_psi": max_pressure,
        "calibration_method": parsed.get("calibration_method", "UNKNOWN"),
        "calibration_confidence": parsed.get("calibration_confidence", "UNKNOWN"),
        "frame_rate": parsed.get("frame_rate"),
        "is_provisional": parsed.get("is_provisional", False),
        "total_frames": parsed.get("total_frames", len(calibrated_frames)),
    }


def _outline_contours(mask: np.ndarray, up: int = 4, eps: float = 0.25) -> list:
    """
    Sub-sensel accurate, densely sampled outer boundary of a contact mask.
    The mask is upsampled ``up``× before contour tracing so the polygon follows the real
    edge with many vertices (not a coarse 1-sensel staircase). Coordinates are returned
    as flat [x0, y0, x1, y1, …] lists in sensel units (pixel i spans [i, i+1]).
    """
    if mask is None or not np.any(mask):
        return []
    m = np.pad(mask.astype(np.float32), 1)
    big = cv2.resize(m, None, fx=up, fy=up, interpolation=cv2.INTER_LINEAR)
    binary = (big >= 0.5).astype(np.uint8)
    contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    out = []
    for cnt in contours:
        if cv2.contourArea(cnt) < 3 * up * up:
            continue
        approx = cv2.approxPolyDP(cnt, eps, True) if eps > 0 else cnt
        pts = approx.reshape(-1, 2).astype(np.float32)
        if len(pts) < 3:
            continue
        pts = (pts + 0.5) / up - 1.0
        out.append(np.round(pts, 2).flatten().tolist())
    return out


def _rot(arr: np.ndarray, k: int) -> np.ndarray:
    return np.ascontiguousarray(np.rot90(arr, k % 4))


def _b64_u8(arr_u8: np.ndarray) -> str:
    return base64.b64encode(np.ascontiguousarray(arr_u8, dtype=np.uint8).tobytes()).decode("ascii")


def run_pipeline(ctx: dict, config: PipelineConfig) -> dict:
    """
    Run the whole filter chain for every playback frame with the given config.
    Geometry / footprint mask is separated from pressure smoothing so that Fourier /
    Gaussian / median / bilateral smoothing never stretches or reshapes the footprint.
    """
    vmax_psi = ctx["vmax_psi"]
    sensel_w, sensel_h = ctx["sensel_w"], ctx["sensel_h"]
    position = ctx["position"]
    is_xsn = ctx["is_xsn"]
    raw_frames = ctx["raw"]
    calibration_results = ctx["calib"]
    frame_numbers = ctx["frame_numbers"]
    min_pressure = config.min_pressure
    sensel_area_mm2 = sensel_w * sensel_h
    config.sensel_area_mm2 = sensel_area_mm2

    temporal_filtered, temporal_diag = temporal_antispeckle(ctx["trimmed"], config)

    def work(fn: int) -> dict:
        arr = temporal_filtered[fn]

        raw_min = raw_max = raw_mean = 0.0
        invalid_count = saturation_count = 0
        if is_xsn and fn in raw_frames and fn in calibration_results:
            flat_raw = raw_frames[fn].flatten()
            valid_raw = flat_raw[flat_raw < 65530]
            nz = valid_raw[valid_raw > 0]
            raw_min = float(np.min(nz)) if len(nz) else 0.0
            raw_max = float(np.max(valid_raw)) if len(valid_raw) else 0.0
            raw_mean = float(np.mean(nz)) if len(nz) else 0.0
            invalid_count = int(np.sum(calibration_results[fn].invalid_mask))
            saturation_count = int(np.sum(calibration_results[fn].saturation_mask))

        flat_cal = arr.flatten()
        active_cal = flat_cal[flat_cal >= min_pressure]
        cal_min = float(np.min(active_cal)) if len(active_cal) else 0.0
        cal_max = float(np.max(flat_cal))
        cal_mean = float(np.mean(active_cal)) if len(active_cal) else 0.0
        cal_p95 = float(np.percentile(flat_cal, 95))
        cal_p98 = float(np.percentile(flat_cal, 98))

        # 1. Despeckle isolated single-sensel hardware spikes
        spatial_filtered, n_spatial = spatial_spike_filter(arr, config)

        # 2. Compute true contact geometry mask from un-blurred pressure data
        geom_patched, _patch_diag = sensel_patch_filter(spatial_filtered, config)
        geom_patched = morphological_cleanup(geom_patched, config)
        geom_mask = _segment_contact(geom_patched, min_pressure, config.min_component_px)
        if config.keep_largest_only and np.any(geom_mask):
            geom_only = keep_largest_component(np.where(geom_mask, spatial_filtered, 0.0).astype(np.float32), min_pressure)
            geom_mask = geom_only >= min_pressure

        # 3. Smooth pressure noise strictly within the contact mask (no boundary stretching)
        filtered, noise, n_fft, energy_removed = apply_noise_filter(spatial_filtered, config, support_mask=geom_mask)
        segmented = np.where(geom_mask, filtered, 0.0).astype(np.float32)

        disp = _rot(segmented, config.rotation_k)
        disp_mask = _rot(geom_mask, config.rotation_k)
        # Rotate sensel dimensions if rotation_k is odd
        disp_w = sensel_h if (config.rotation_k % 2 == 1) else sensel_w
        disp_h = sensel_w if (config.rotation_k % 2 == 1) else sensel_h

        metrics = _frame_metrics_13(disp, fn, min_pressure, position, disp_w, disp_h, vmax_psi, contact_mask=disp_mask)
        seg_mask = disp_mask
        if np.any(seg_mask):
            rows_any = np.any(seg_mask, axis=1)
            cols_any = np.any(seg_mask, axis=0)
            r0, r1 = int(np.argmax(rows_any)), int(len(rows_any) - 1 - np.argmax(rows_any[::-1]))
            c0, c1 = int(np.argmax(cols_any)), int(len(cols_any) - 1 - np.argmax(cols_any[::-1]))
            metrics["bbox_width_mm"] = round((c1 - c0 + 1) * disp_w, 1)
            metrics["bbox_length_mm"] = round((r1 - r0 + 1) * disp_h, 1)
        else:
            metrics["bbox_width_mm"] = metrics["bbox_length_mm"] = 0.0

        clipped = np.clip(disp, 0, vmax_psi)
        u8 = (clipped / vmax_psi * 255).astype(np.uint8)
        outline = _outline_contours(disp_mask)

        active_sensels = int(np.sum(seg_mask))
        diag = {
            "frame_index": fn,
            "raw_min": raw_min, "raw_max": raw_max, "raw_mean": raw_mean,
            "calibrated_min": cal_min, "calibrated_max": cal_max, "calibrated_mean": cal_mean,
            "calibrated_p95": cal_p95, "calibrated_p98": cal_p98,
            "active_sensels": active_sensels,
            "contact_area_mm2": round(active_sensels * sensel_area_mm2, 1),
            "contact_area_cm2": round(active_sensels * sensel_area_mm2 / 100.0, 2),
            "total_load_n": float(np.sum(segmented) * 0.00689476 * sensel_area_mm2),
            "invalid_count": invalid_count,
            "saturation_count": saturation_count,
            "spatial_spike_count": n_spatial,
            "temporal_spike_count": temporal_diag.get("temporal_spikes_detected", 0) if fn == frame_numbers[0] else 0,
            "noise_removed_psi_sum": round(energy_removed, 1),
        }
        return {
            "fn": fn, "segmented": segmented, "spatial_filtered": spatial_filtered,
            "u8": u8, "outline": outline, "metrics": metrics, "diag": diag,
            "n_spatial": n_spatial,
        }

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(work, frame_numbers))
    by_fn = {r["fn"]: r for r in results}

    metrics_list = [by_fn[fn]["metrics"] for fn in frame_numbers]
    frame_diagnostics = [by_fn[fn]["diag"] for fn in frame_numbers]
    total_repaired_spikes = temporal_diag.get("temporal_spikes_repaired", 0) + sum(r["n_spatial"] for r in results)

    # Prefer full on-mat contact frames when selecting best_idx
    best_idx = int(np.argmax([m["active_sensels"] for m in metrics_list]))
    best_fn = frame_numbers[best_idx]
    best_seg = by_fn[best_fn]["segmented"]
    patch = _contact_patch_exact(_rot(best_seg, config.rotation_k), min_pressure, sensel_w, sensel_h)
    patch.pop("outline_points", None)

    total_invalid = sum(d["invalid_count"] for d in frame_diagnostics)
    total_saturated = sum(d["saturation_count"] for d in frame_diagnostics)

    disp_shape = by_fn[best_fn]["u8"].shape
    grid_rows, grid_cols = int(disp_shape[0]), int(disp_shape[1])

    calib_audit = {
        "sensor": f"XSENSOR HX510 ({sensel_w:.2f}×{sensel_h:.2f} mm)" if is_xsn else f"CSV ({sensel_w:.2f}×{sensel_h:.2f} mm)",
        "vehicle_type": config.tyre_type,
        "pressure_range": f"Threshold {min_pressure:.0f} PSI | Display 0-{config.pressure_ceiling:.0f} PSI",
        "calibration_source": f"XSN Header Pitch ({ctx.get('pitch_source', 'xsn_header')}) + Raw/128 PSI" if is_xsn else "CSV Direct",
        "calibration_method": ctx["calibration_method"],
        "raw_domain": "uint16 [0, 65529] (65530+ invalid)" if is_xsn else "N/A",
        "psi_domain": "float32 [0, 511.95]",
        "offset": 0.0,
        "scale": 0.0078125 if is_xsn else 1.0,
        "saturated_values": total_saturated,
        "invalid_values": total_invalid,
        "repaired_spikes": total_repaired_spikes,
        "baseline_frame": ctx["baseline_diag"].get("baseline_frame_index"),
        "calibration_confidence": ctx["calibration_confidence"],
    }

    # ── Pixel inspector payload for the best frame (display orientation) ──
    k = config.rotation_k
    pd = {
        "grid_rows": grid_rows, "grid_cols": grid_cols,
        "calibration_method": calib_audit["calibration_method"],
        "pressure_range": config.tyre_type,
        "pressure_ceiling": config.pressure_ceiling,
        "calibration_confidence": calib_audit["calibration_confidence"],
        "encoding": "b64",
        "_dtypes": {},
    }

    def _put(name, a, dtype):
        dt = np.dtype(dtype)
        buf = np.ascontiguousarray(_rot(np.asarray(a), k)).astype(dt).tobytes()
        pd[name] = base64.b64encode(buf).decode("ascii")
        pd["_dtypes"][name] = dt.str

    def _empty(name):
        pd[name] = ""
        pd["_dtypes"][name] = "<f4"

    if is_xsn and best_fn in raw_frames and best_fn in calibration_results:
        cres = calibration_results[best_fn]
        _put("best_frame_raw", raw_frames[best_fn], "<u2")
        _put("best_frame_calibrated", cres.calibrated_psi, "<f4")
        _put("invalid_mask", cres.invalid_mask, "u1")
        _put("saturation_mask", cres.saturation_mask, "u1")
    else:
        _empty("best_frame_raw")
        if best_fn in ctx["trimmed"]:
            _put("best_frame_calibrated", ctx["trimmed"][best_fn], "<f4")
        else:
            _empty("best_frame_calibrated")
        _empty("invalid_mask")
        _empty("saturation_mask")
    _put("best_frame_filtered", by_fn[best_fn]["spatial_filtered"], "<f4")
    _put("best_frame_final", best_seg, "<f4")
    _put("contact_mask", best_seg >= min_pressure, "u1")
    if best_fn in ctx["trimmed"]:
        diff = np.abs(ctx["trimmed"][best_fn] - by_fn[best_fn]["spatial_filtered"])
        _put("spike_mask", diff > 0.1, "u1")
    else:
        _empty("spike_mask")

    cfg_dict = dict(config.__dict__)
    return {
        "frame_numbers":     frame_numbers,
        "frames":            {str(fn): _b64_u8(by_fn[fn]["u8"]) for fn in frame_numbers},
        "frames_encoding":   "b64-u8",
        "outlines":          {str(fn): by_fn[fn]["outline"] for fn in frame_numbers},
        "grid_rows":         grid_rows,
        "grid_cols":         grid_cols,
        "metrics":           metrics_list,
        "best_frame_index":  best_idx,
        "best_frame_number": best_fn,
        "best_metrics":      metrics_list[best_idx],
        "contact_patch":     patch,
        "vmax":              vmax_psi,
        "vmin":              min_pressure,
        "speed":             ctx["speed"],
        "position":          position,
        "tyre_type":         config.tyre_type,
        "filter_type":       config.filter_type,
        "sensel_w_mm":       sensel_w,
        "sensel_h_mm":       sensel_h,
        "is_xsn":            is_xsn,
        "is_provisional":    ctx["is_provisional"],
        "frame_rate":        ctx["frame_rate"],
        "total_frames_in_file": ctx["total_frames"],
        "calibration_audit": calib_audit,
        "frame_diagnostics": frame_diagnostics,
        "baseline_diagnostics": ctx["baseline_diag"],
        "pipeline_config":   cfg_dict,
        "pixel_data":        pd,
    }


def load_and_process(
    filepath: str,
    tyre_type: str = "TBR",
    min_pressure: float | None = None,
    max_pressure: float = 220.0,
    speed: float = 0.0,
    position: str = "FR",
    filters: dict | None = None,
) -> dict:
    """One-shot convenience wrapper: prepare the job then run the filter pipeline."""
    ctx = prepare_job(filepath, tyre_type, min_pressure, max_pressure, speed, position)
    cfg = config_with_overrides(ctx["base_config"], filters)
    return run_pipeline(ctx, cfg)


# ── Internal helpers ──────────────────────────────────────────────────────────

def _trim_frames(raw_dict: dict, pressure_ceiling: float, threshold: float = 18.0) -> dict:
    """Select active footprint frames centered around the peak contact event."""
    frame_numbers = sorted(raw_dict.keys())
    num_frames = len(frame_numbers)

    valid_fns = [
        fn for fn in frame_numbers
        if np.sum(raw_dict[fn] >= threshold) >= CONTACT_AREA_MIN
    ]
    if not valid_fns:
        valid_fns = frame_numbers[:min(50, num_frames)]
        return {fn: raw_dict[fn] for fn in valid_fns}

    # Calculate active sensels / load per frame to find peak contact frame
    valid_loads = [
        float(np.sum(raw_dict[fn][raw_dict[fn] >= threshold]))
        for fn in valid_fns
    ]
    peak_sub_idx = int(np.argmax(valid_loads))

    # 56-frame playback window centered around peak contact
    half_window = 28
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
    contact_mask: np.ndarray | None = None,
) -> dict:
    """Compute tyre footprint parameters per Apollo Tyres / XSENSOR reference spec.

    Assumes ``arr`` is oriented in display orientation (rolling axis = vertical rows,
    transverse axis = horizontal columns: 1 = Leading Edge at top, 2 = Trailing Edge at
    bottom, A = Outer Tyre Side, B = Inner Tyre Side).

    Definitions:
      Contact Area (CA)     — Net thresholded contact area (active sensels >= threshold × pitch²)
      Gross Tyre Area (PA)  — Total footprint envelope area (convex hull enclosing outer tyre boundary,
                              including tread grooves/voids within the footprint)
      Perimeter             — Outer boundary perimeter length of the footprint envelope (convex hull arc length)
      Contact Ratio         — (CA / Gross Tyre Area) * 100
      FP Width W            — Transverse shoulder-to-shoulder width across the footprint centre
      FP Length L           — Longitudinal rolling length along the footprint centre
      S1 / S2               — Outer (A) and Inner (B) shoulder contact lengths at 10% transverse inset
      Squareness            — (S1/L)*100, (S2/L)*100, and Avg Squareness
    """
    if contact_mask is not None:
        mask = np.asarray(contact_mask, dtype=bool)
    else:
        mask = _segment_contact(arr, threshold)
    active = int(np.sum(mask))

    sensel_area_mm2 = sensel_w_mm * sensel_h_mm
    peak_psi = float(np.max(arr[mask])) if active > 0 else 0.0
    avg_psi  = float(np.mean(arr[mask])) if active > 0 else 0.0

    contact_area_mm2 = active * sensel_area_mm2  # Net Contact Area (CA)
    contact_area_cm2 = contact_area_mm2 / 100.0
    PSI_TO_N_PER_MM2 = 0.00689476
    total_load_n     = float(np.sum(arr[mask]) * PSI_TO_N_PER_MM2 * sensel_area_mm2)
    total_load_kg    = total_load_n / 9.80665

    rows_any = np.any(mask, axis=1)
    cols_any = np.any(mask, axis=0)

    if not rows_any.any() or not cols_any.any():
        return {
            "frame_number":         frame_number,
            "contact_area_mm2":     0.0,
            "contact_area_cm2":     0.0,
            "gross_tyre_area_mm2":  0.0,
            "gross_tyre_area_cm2":  0.0,
            "perimeter_area_mm2":   0.0,
            "perimeter_mm":         0.0,
            "perimeter_cm":         0.0,
            "contact_ratio_pct":    0.0,
            "fp_width_mm":          0.0,
            "fp_length_mm":         0.0,
            "outer_shoulder_s1_mm": 0.0,
            "inner_shoulder_s2_mm": 0.0,
            "shoulder_skewness_pct":0.0,
            "squareness_s1_pct":    0.0,
            "squareness_s2_pct":    0.0,
            "squareness_avg_pct":   0.0,
            "contact_pressure_psi": 0.0,
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

    # In display orientation (rotated 90° CCW so tyre points bottom-to-top):
    # - Columns (c) span the transverse width (Shoulder A ↔ Shoulder B)
    # - Rows (r) span the longitudinal rolling length (1 Leading Edge ↔ 2 Trailing Edge)
    c_mid = (c_min + c_max) // 2
    r_mid = (r_min + r_max) // 2

    # Use a central band (+/- 5 sensels) so a circumferential groove at exact c_mid
    # or lateral sipes at exact r_mid do not artificially under-report centre L or W.
    def _row_span(r_idx: int) -> int:
        rm = mask[r_idx, :]
        if not np.any(rm):
            return 0
        return int((len(rm) - 1 - np.argmax(rm[::-1])) - np.argmax(rm) + 1)

    def _col_span(c_idx: int) -> int:
        cm = mask[:, c_idx]
        if not np.any(cm):
            return 0
        return int((len(cm) - 1 - np.argmax(cm[::-1])) - np.argmax(cm) + 1)

    w_band = [_row_span(r) for r in range(max(r_min, r_mid - 5), min(r_max, r_mid + 5) + 1)]
    w_span = max(w_band) if any(w_band) else (c_max - c_min + 1)
    fp_width_mm = round(w_span * sensel_w_mm, 1)

    l_band = [_col_span(c) for c in range(max(c_min, c_mid - 8), min(c_max, c_mid + 8) + 1)]
    l_span = max(l_band) if any(l_band) else (r_max - r_min + 1)
    fp_length_mm = round(l_span * sensel_h_mm, 1)

    n_transverse = c_max - c_min + 1
    inset = max(1, int(round(0.10 * n_transverse)))
    c_left = min(c_max, c_min + inset)
    c_right = max(c_min, c_max - inset)

    def _shoulder_len_mm(c_center: int) -> float:
        spans = [_col_span(c) for c in range(max(c_min, c_center - 2), min(c_max, c_center + 2) + 1)]
        best = max(spans) if spans else 0
        return float(best * sensel_h_mm)

    s_left = _shoulder_len_mm(c_left)
    s_right = _shoulder_len_mm(c_right)

    # A = Outer Tyre Side (S1), B = Inner Tyre Side (S2)
    # For Left tyres (FL, RL) viewed from top/footprint, Left is Outer (A), Right is Inner (B).
    # For Right tyres (FR, RR), Right is Outer (A), Left is Inner (B).
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

    # Gross Tyre Area (total footprint envelope including grooves/voids) & Perimeter
    # Computed from the convex hull of the outer corners of all active contact sensels.
    points = np.argwhere(mask)
    if len(points) >= 3:
        x0 = points[:, 1].astype(np.float32) * sensel_w_mm
        y0 = points[:, 0].astype(np.float32) * sensel_h_mm
        corners = np.vstack([
            np.column_stack((x0, y0)),
            np.column_stack((x0 + sensel_w_mm, y0)),
            np.column_stack((x0, y0 + sensel_h_mm)),
            np.column_stack((x0 + sensel_w_mm, y0 + sensel_h_mm)),
        ]).astype(np.float32)
        hull = cv2.convexHull(corners)
        gross_tyre_area_mm2 = float(cv2.contourArea(hull))
        perimeter_mm = float(cv2.arcLength(hull, True))
    else:
        gross_tyre_area_mm2 = contact_area_mm2
        perimeter_mm = 0.0

    gross_tyre_area_mm2 = max(gross_tyre_area_mm2, contact_area_mm2)
    gross_tyre_area_cm2 = gross_tyre_area_mm2 / 100.0
    perimeter_cm = perimeter_mm / 10.0

    contact_ratio_pct = round((contact_area_mm2 / gross_tyre_area_mm2) * 100.0, 1) if gross_tyre_area_mm2 > 0 else 0.0

    total_sensels = arr.size
    contact_pct = round((active / total_sensels) * 100.0, 2) if total_sensels > 0 else 0.0

    return {
        "frame_number":          frame_number,
        "contact_area_mm2":      round(contact_area_mm2, 1),
        "contact_area_cm2":      round(contact_area_cm2, 2),
        "gross_tyre_area_mm2":   round(gross_tyre_area_mm2, 1),
        "gross_tyre_area_cm2":   round(gross_tyre_area_cm2, 2),
        "perimeter_area_mm2":    round(gross_tyre_area_mm2, 1),
        "perimeter_mm":          round(perimeter_mm, 1),
        "perimeter_cm":          round(perimeter_cm, 2),
        "contact_ratio_pct":     contact_ratio_pct,
        "fp_width_mm":           fp_width_mm,
        "fp_length_mm":          fp_length_mm,
        "outer_shoulder_s1_mm":  s1,
        "inner_shoulder_s2_mm":  s2,
        "shoulder_skewness_pct": shoulder_skewness_pct,
        "squareness_s1_pct":     squareness_s1_pct,
        "squareness_s2_pct":     squareness_s2_pct,
        "squareness_avg_pct":    squareness_avg_pct,
        "contact_pressure_psi":  round(avg_psi, 2),
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
            # No y-inversion needed — frame data is no longer flipud'd
            points.append({"x": c, "y": r})
        if len(points) >= 3:
            outline_contours.append(points)

    outline_points = outline_contours[0] if len(outline_contours) == 1 else outline_contours

    bbox = [col_min, row_min, col_max, row_max]

    return {
        "width_mm":  width_mm,
        "length_mm": length_mm,
        "bbox":      bbox,
        "outline_points": outline_points,
    }


def _serialise_frame(arr: np.ndarray, vmax_psi: float) -> list:
    """Return a flat list of uint8 values (0-255) for canvas rendering.

    Normalised against vmax_psi.  No vertical flip — preserves original
    sensor orientation so outer tread is at bottom, inner at top,
    matching XSENSOR Desktop display convention.
    """
    clipped    = np.clip(arr, 0, vmax_psi)
    normalised = (clipped / vmax_psi * 255).astype(np.uint8)
    return normalised.flatten().tolist()
