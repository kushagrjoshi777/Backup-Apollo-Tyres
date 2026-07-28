/**
 * heatmap.js  —  Apollo Tyre Dashboard
 * Canvas heatmap renderer using a jet colourmap (matches matplotlib's jet).
 * Includes on-the-fly morphological noise filtering and metrics re-calculation.
 */

const JET = (() => {
  // Pre-compute 256-entry jet LUT: R,G,B bytes
  const lut = new Uint8Array(256 * 3);
  for (let i = 0; i < 256; i++) {
    const t = i / 255;
    let r, g, b;
    // Standard jet formula
    if (t < 0.125)      { r = 0; g = 0; b = 0.5 + t * 4; }
    else if (t < 0.375) { r = 0; g = (t - 0.125) * 4; b = 1; }
    else if (t < 0.625) { r = (t - 0.375) * 4; g = 1; b = 1 - (t - 0.375) * 4; }
    else if (t < 0.875) { r = 1; g = 1 - (t - 0.625) * 4; b = 0; }
    else                { r = 1 - (t - 0.875) * 4; g = 0; b = 0; }
    lut[i * 3]     = Math.round(Math.max(0, Math.min(1, r)) * 255);
    lut[i * 3 + 1] = Math.round(Math.max(0, Math.min(1, g)) * 255);
    lut[i * 3 + 2] = Math.round(Math.max(0, Math.min(1, b)) * 255);
  }
  return lut;
})();

/**
 * Draw a flat uint8 (0-255) sensel array onto a canvas.
 * @param {HTMLCanvasElement} canvas
 * @param {Uint8Array|number[]} data  - flat array of 256×256 = 65536 uint8 values
 */
function drawHeatmap(canvas, data) {
  const ctx = canvas.getContext('2d');
  const W = 256, H = 256;
  const img = ctx.createImageData(W, H);
  const px  = img.data;

  for (let i = 0; i < W * H; i++) {
    const v  = data[i];          // 0-255 normalised
    const li = v * 3;
    const pi = i * 4;
    if (v === 0) {
      // True zero  → black background (no pressure)
      px[pi]     = 10;
      px[pi + 1] = 12;
      px[pi + 2] = 16;
    } else {
      px[pi]     = JET[li];
      px[pi + 1] = JET[li + 1];
      px[pi + 2] = JET[li + 2];
    }
    px[pi + 3] = 255;
  }

  ctx.putImageData(img, 0, 0);
}

/**
 * Draw bounding-box overlay on an overlay canvas.
 * bbox = [x0, y0, x1, y1] in sensel coordinates (0-255), origin=lower.
 */
function drawBbox(overlayCanvas, bbox) {
  const ctx = overlayCanvas.getContext('2d');
  const W = overlayCanvas.width, H = overlayCanvas.height;
  ctx.clearRect(0, 0, W, H);

  if (!bbox || bbox.every(v => v === 0)) return;

  // bbox origin=lower, canvas origin=upper
  const [x0, y0, x1, y1] = bbox;
  // Flip y for canvas coordinates
  const cy0 = H - 1 - y1;
  const cy1 = H - 1 - y0;

  const scaleX = W / 256;
  const scaleY = H / 256;

  const rx = x0 * scaleX;
  const ry = cy0 * scaleY;
  const rw = (x1 - x0 + 1) * scaleX;
  const rh = (cy1 - cy0 + 1) * scaleY;

  ctx.strokeStyle = '#9e7ce6'; // Apollo Violet/Purple accent
  ctx.lineWidth   = 2;
  ctx.setLineDash([6, 3]);
  ctx.strokeRect(rx, ry, rw, rh);

  // Corner ticks
  ctx.setLineDash([]);
  ctx.lineWidth = 2;
  [[rx, ry], [rx+rw, ry], [rx, ry+rh], [rx+rw, ry+rh]].forEach(([cx, cy]) => {
    ctx.beginPath();
    ctx.arc(cx, cy, 3.5, 0, Math.PI * 2);
    ctx.fillStyle = '#9e7ce6';
    ctx.fill();
  });
}

/**
 * Draw the jet colour-bar legend on a canvas.
 */
function drawColorbar(canvas, vmax) {
  const ctx = canvas.getContext('2d');
  const W = canvas.width, H = canvas.height;
  const grad = ctx.createLinearGradient(0, 0, W, 0);
  // Sample jet at 11 stops
  for (let s = 0; s <= 10; s++) {
    const t   = s / 10;
    const idx = Math.round(t * 255);
    const r   = JET[idx * 3], g = JET[idx * 3 + 1], b = JET[idx * 3 + 2];
    grad.addColorStop(t, `rgb(${r},${g},${b})`);
  }
  ctx.fillStyle = grad;
  ctx.fillRect(0, 0, W, H);
}

/**
 * Noise filter: removes isolated active sensels (flicker).
 * A pixel is kept if it has at least 2 active neighbors in its 3x3 window.
 * @param {Uint8Array|number[]} srcData - 256x256 flat array
 * @returns {Uint8Array} - filtered flat array
 */
function applyNoiseFilter(srcData) {
  const W = 256, H = 256;
  const dstData = new Uint8Array(W * H);

  for (let r = 0; r < H; r++) {
    const rOffset = r * W;
    for (let c = 0; c < W; c++) {
      const idx = rOffset + c;
      const val = srcData[idx];
      if (val === 0) continue;

      let activeNeighbors = 0;
      // Scan 3x3 window around (r, c)
      for (let dr = -1; dr <= 1; dr++) {
        const nr = r + dr;
        if (nr < 0 || nr >= H) continue;
        const nrOffset = nr * W;

        for (let dc = -1; dc <= 1; dc++) {
          if (dr === 0 && dc === 0) continue;
          const nc = c + dc;
          if (nc >= 0 && nc < W) {
            if (srcData[nrOffset + nc] > 0) {
              activeNeighbors++;
            }
          }
        }
      }

      if (activeNeighbors >= 2) {
        dstData[idx] = val;
      } else {
        dstData[idx] = 0; // Remove isolated sensel
      }
    }
  }

  return dstData;
}

/**
 * Calculates all 13 tyre parameters and contact patch for a flat uint8 array.
 * Matches processor.py _frame_metrics_13() exactly.
 * @param {Uint8Array|Array} data       – flat 256×256 uint8 normalised values (0-255)
 * @param {number}           vmin       – min pressure PSI (threshold)
 * @param {number}           vmax       – max pressure PSI (scale ceiling)
 * @param {number}           frameNumber
 * @param {string}           [position] – 'FL'|'FR'|'RL'|'RR' for S1/S2 side assignment
 */
function computeFrameMetrics(data, vmin, vmax, frameNumber, position) {
  const W = 256, H = 256;
  const SENSEL_MM   = 1.6;     // mm per sensel (XSENSOR 256×256 standard)
  const SENSEL_AREA = SENSEL_MM * SENSEL_MM;   // 2.56 mm²
  const PSI_TO_N    = 0.00689476;

  const threshByte = Math.ceil((vmin / vmax) * 255);
  const pos = (position || 'FR').toUpperCase();

  let active = 0, sumPsi = 0, maxByte = 0;
  let rMin = H, rMax = -1, cMin = W, cMax = -1;

  // ── Pass 1: bounding box + basic aggregates ───────────────────────────────
  for (let r = 0; r < H; r++) {
    const off = r * W;
    for (let c = 0; c < W; c++) {
      const b = data[off + c];
      if (b > maxByte) maxByte = b;
      if (b > threshByte) {
        active++;
        sumPsi += (b / 255) * vmax;
        if (r < rMin) rMin = r;
        if (r > rMax) rMax = r;
        if (c < cMin) cMin = c;
        if (c > cMax) cMax = c;
      }
    }
  }

  const peakPsi   = (maxByte / 255) * vmax;
  const avgPsi    = active > 0 ? sumPsi / active : 0;
  const loadKg    = (sumPsi * PSI_TO_N * SENSEL_AREA) / 9.80665;
  const areaM2    = active * SENSEL_AREA;
  const contactPct = (active / (W * H)) * 100;

  if (rMax < 0) {
    // No active sensels
    const z = { frame_number: frameNumber, contact_area_mm2: 0, contact_ratio_pct: 0,
      fp_width_mm: 0, fp_length_mm: 0, outer_shoulder_s1_mm: 0, inner_shoulder_s2_mm: 0,
      shoulder_skewness_pct: 0, squareness_s1_s2_pct: 100, squareness_avg: 0,
      land_sea_ratio: 0, contact_pressure_psi: 0, maximum_length_mm: 0, maximum_width_mm: 0,
      peak_pressure_psi: 0, avg_pressure_psi: 0, estimated_load_kg: 0,
      active_sensels: 0, contact_pct: 0 };
    return { metrics: z, contact_patch: { width_mm: 0, length_mm: 0, bbox: [0,0,0,0] } };
  }

  const bboxH   = rMax - rMin + 1;
  const bboxW   = cMax - cMin + 1;
  const fpLen   = Math.round(bboxH * SENSEL_MM * 10) / 10;
  const fpWid   = Math.round(bboxW * SENSEL_MM * 10) / 10;
  const bboxArea = bboxH * bboxW;
  const contRatio = bboxArea > 0 ? Math.round((active / bboxArea) * 1000) / 10 : 0;
  const landSea   = bboxArea > active ? Math.round((active / (bboxArea - active)) * 100) / 100 : 0;

  // ── Pass 2: per-column lengths (for max-length, shoulder strips) ─────────
  const colLengths = new Array(bboxW).fill(0);
  let maxColLen = 0;
  for (let ci = 0; ci < bboxW; ci++) {
    const c = cMin + ci;
    let cRmin = -1, cRmax = -1;
    for (let r = rMin; r <= rMax; r++) {
      if (data[r * W + c] > threshByte) {
        if (cRmin < 0) cRmin = r;
        cRmax = r;
      }
    }
    const len = cRmax >= 0 ? cRmax - cRmin + 1 : 0;
    colLengths[ci] = len;
    if (len > maxColLen) maxColLen = len;
  }
  const maxLenMm = Math.round(maxColLen * SENSEL_MM * 10) / 10;

  // ── Pass 3: per-row widths (for max-width) ────────────────────────────────
  let maxRowWid = 0;
  for (let r = rMin; r <= rMax; r++) {
    let rCmin = -1, rCmax = -1;
    const off = r * W;
    for (let c = cMin; c <= cMax; c++) {
      if (data[off + c] > threshByte) {
        if (rCmin < 0) rCmin = c;
        rCmax = c;
      }
    }
    const w = rCmax >= 0 ? rCmax - rCmin + 1 : 0;
    if (w > maxRowWid) maxRowWid = w;
  }
  const maxWidMm = Math.round(maxRowWid * SENSEL_MM * 10) / 10;

  // ── Shoulder S1 / S2 (outer 20% strips) ──────────────────────────────────
  const shoulderW = Math.max(2, Math.round(0.20 * bboxW));
  const leftLens  = colLengths.slice(0, shoulderW);
  const rightLens = colLengths.slice(-shoulderW);
  const leftMm    = leftLens.reduce((a, b) => a + b, 0) / leftLens.length * SENSEL_MM;
  const rightMm   = rightLens.reduce((a, b) => a + b, 0) / rightLens.length * SENSEL_MM;

  // FL & RL → outer = left side;  FR & RR → outer = right side
  const s1 = Math.round((['FL','RL'].includes(pos) ? leftMm : rightMm) * 10) / 10;
  const s2 = Math.round((['FL','RL'].includes(pos) ? rightMm : leftMm) * 10) / 10;

  const sAvg     = (s1 + s2) / 2;
  const skewness = sAvg > 0 ? Math.round(((s1 - s2) / sAvg) * 1000) / 10 : 0;
  const sqRatio  = s2 > 0   ? Math.round((s1 / s2) * 1000) / 10 : 100;
  const sqAvg    = fpLen > 0 ? Math.round((sAvg / fpLen) * 100) / 100 : 0;

  // ── Reconstruct original bbox coords (origin=lower for overlay drawing) ──
  const origR0 = 255 - rMax;
  const origR1 = 255 - rMin;
  const bboxOrig = [cMin, origR0, cMax, origR1];

  return {
    metrics: {
      frame_number:          frameNumber,
      contact_area_mm2:      Math.round(areaM2 * 10) / 10,
      contact_ratio_pct:     contRatio,
      fp_width_mm:           fpWid,
      fp_length_mm:          fpLen,
      outer_shoulder_s1_mm:  s1,
      inner_shoulder_s2_mm:  s2,
      shoulder_skewness_pct: skewness,
      squareness_s1_s2_pct:  sqRatio,
      squareness_avg:        sqAvg,
      land_sea_ratio:        landSea,
      contact_pressure_psi:  Math.round(avgPsi * 100) / 100,
      maximum_length_mm:     maxLenMm,
      maximum_width_mm:      maxWidMm,
      // Backward-compat aliases
      peak_pressure_psi:     Math.round(peakPsi * 100) / 100,
      avg_pressure_psi:      Math.round(avgPsi * 100) / 100,
      estimated_load_kg:     Math.round(loadKg * 10) / 10,
      active_sensels:        active,
      contact_pct:           Math.round(contactPct * 100) / 100,
    },
    contact_patch: {
      width_mm:  fpWid,
      length_mm: fpLen,
      bbox: bboxOrig,
    }
  };
}
