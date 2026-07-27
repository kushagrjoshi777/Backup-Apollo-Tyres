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
 * Calculates tyre metrics and contact patch bounds for a flat uint8 array on the fly.
 * Matches calculations in processor.py.
 * @param {Uint8Array} data - flat uint8 array
 * @param {number} vmax - VMAX PSI
 * @param {number} frameNumber - current frame number
 */
function computeFrameMetrics(data, vmin, vmax, frameNumber) {
  const W = 256, H = 256;
  const SENSEL_AREA_MM2 = 6.45;
  const PSI_TO_N_PER_MM2 = 0.00689476;
  
  // vmin PSI threshold converted to uint8 level
  const threshByte = Math.ceil((vmin / vmax) * 255);

  let active = 0;
  let sumPsi = 0;
  let maxByte = 0;

  // Bounding box bounds (original_coords format: row index is from lower to upper in mat, 
  // but note that our serialized array is already flipped vertically (origin=lower to origin=upper).
  // Thus row index in flat array corresponds to (255 - row_origin_lower).
  // Let's find min/max row/col values.
  let rowMin = H, rowMax = -1;
  let colMin = W, colMax = -1;

  for (let r = 0; r < H; r++) {
    const rOffset = r * W;
    for (let c = 0; c < W; c++) {
      const b = data[rOffset + c];
      if (b > maxByte) maxByte = b;

      if (b > threshByte) {
        active++;
        const psi = (b / 255) * vmax;
        sumPsi += psi;

        // Since the array is flipped vertically in serialise_frame,
        // row r = 0 is actually the TOP row (index 255 in original array).
        // Let's reconstruct the original y-coordinate (origin=lower):
        // original_y = 255 - r
        const origY = 255 - r;

        if (origY < rowMin) rowMin = origY;
        if (origY > rowMax) rowMax = origY;
        if (c < colMin) colMin = c;
        if (c > colMax) colMax = c;
      }
    }
  }

  const peakPsi = (maxByte / 255) * vmax;
  const avgPsi = active > 0 ? sumPsi / active : 0.0;
  const contactAreaMm2 = active * SENSEL_AREA_MM2;
  const totalLoadN = sumPsi * PSI_TO_N_PER_MM2 * SENSEL_AREA_MM2;
  const totalLoadKg = totalLoadN / 9.80665;
  const contactPct = (active / (W * H)) * 100;

  let widthMm = 0;
  let lengthMm = 0;
  let bbox = [0, 0, 0, 0];

  if (rowMax >= 0) {
    const heightSensels = rowMax - rowMin + 1;
    const widthSensels = colMax - colMin + 1;
    lengthMm = Math.round(heightSensels * 2.54 * 10) / 10;
    widthMm = Math.round(widthSensels * 2.54 * 10) / 10;
    bbox = [colMin, rowMin, colMax, rowMax];
  }

  return {
    metrics: {
      frame_number: frameNumber,
      peak_pressure_psi: Math.round(peakPsi * 100) / 100,
      avg_pressure_psi: Math.round(avgPsi * 100) / 100,
      contact_area_mm2: Math.round(contactAreaMm2 * 10) / 10,
      estimated_load_kg: Math.round(totalLoadKg * 10) / 10,
      active_sensels: active,
      contact_pct: Math.round(contactPct * 100) / 100
    },
    contact_patch: {
      width_mm: widthMm,
      length_mm: lengthMm,
      bbox: bbox
    }
  };
}
