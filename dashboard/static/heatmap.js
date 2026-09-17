/**
 * heatmap.js  —  Apollo Tyre Dashboard
 * Canvas heatmap renderer: jet colourmap, exact tyre outline, noise filtering.
 * All pressure data is normalised against vmax_psi on the backend before arriving
 * here as uint8 (0-255).  The jet LUT maps those byte values to colours so the
 * visual scale is always correlated to the real PSI range shown in the legend.
 */

// ── Jet Look-Up Table ─────────────────────────────────────────────────────────
const JET = (() => {
  const lut = new Uint8Array(256 * 3);
  for (let i = 0; i < 256; i++) {
    const t = i / 255;
    let r, g, b;
    if      (t < 0.125) { r = 0; g = 0; b = 0.5 + t * 4; }
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
 * @param {Uint8Array|number[]} data  – flat array of W×H uint8 values
 * @param {number} [W=256]  – grid width in sensels
 * @param {number} [H=256]  – grid height in sensels
 */
function drawHeatmap(canvas, data, W = 256, H = 256) {
  const ctx = canvas.getContext('2d');

  // Resize canvas to match sensel grid (preserves aspect ratio via CSS)
  canvas.width  = W;
  canvas.height = H;

  const img = ctx.createImageData(W, H);
  const px  = img.data;

  for (let i = 0; i < W * H; i++) {
    const v  = data[i];   // 0-255 normalised byte
    const li = v * 3;
    const pi = i * 4;
    if (v === 0) {
      // True zero → white background (no contact)
      px[pi]     = 255;
      px[pi + 1] = 255;
      px[pi + 2] = 255;
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
 * Draw the EXACT tyre outline on the overlay canvas using per-row edge points.
 * Renders a thin dark stroke polygon that matches the real tyre contact boundary,
 * NOT a rectangular bounding box.
 *
 * @param {HTMLCanvasElement} overlayCanvas
 * @param {Array<{x:number, y:number}>} outlinePoints  – polygon vertices in sensel coords
 * @param {number} gridW  – sensel grid width  (e.g. 256 or 129)
 * @param {number} gridH  – sensel grid height (e.g. 256 or 126)
 */
function drawTyreOutline(overlayCanvas, outlinePoints, gridW = 256, gridH = 256) {
  const ctx = overlayCanvas.getContext('2d');
  const CW  = overlayCanvas.width;
  const CH  = overlayCanvas.height;
  ctx.clearRect(0, 0, CW, CH);

  if (!outlinePoints || outlinePoints.length === 0) return;

  const scaleX = CW / gridW;
  const scaleY = CH / gridH;

  const contours = Array.isArray(outlinePoints[0]) ? outlinePoints : [outlinePoints];

  for (const contour of contours) {
    if (!contour || contour.length < 3) continue;

    ctx.beginPath();
    const p0 = contour[0];
    ctx.moveTo(p0.x * scaleX, p0.y * scaleY);
    for (let i = 1; i < contour.length; i++) {
      const p = contour[i];
      ctx.lineTo(p.x * scaleX, p.y * scaleY);
    }
    ctx.closePath();

    ctx.fillStyle = 'rgba(0, 0, 0, 0.02)';
    ctx.fill();

    ctx.strokeStyle = 'rgba(30, 30, 30, 0.85)';
    ctx.lineWidth   = 1.2;
    ctx.setLineDash([]);
    ctx.stroke();
  }
}

/**
 * Draw a simple bounding-box outline (fallback if no outline_points available).
 * Uses the same dark style as drawTyreOutline for consistency.
 * @param {HTMLCanvasElement} overlayCanvas
 * @param {number[]} bbox  – [x0, y0, x1, y1] in sensel coords (origin=lower)
 * @param {number} gridW
 * @param {number} gridH
 */
function drawBbox(overlayCanvas, bbox, gridW = 256, gridH = 256) {
  const ctx = overlayCanvas.getContext('2d');
  const CW  = overlayCanvas.width;
  const CH  = overlayCanvas.height;
  ctx.clearRect(0, 0, CW, CH);

  if (!bbox || bbox.every(v => v === 0)) return;

  const [x0, y0, x1, y1] = bbox;
  const scaleX = CW / gridW;
  const scaleY = CH / gridH;

  const rx = x0 * scaleX;
  const ry = y0 * scaleY;
  const rw = (x1 - x0 + 1) * scaleX;
  const rh = (y1 - y0 + 1) * scaleY;

  ctx.strokeStyle = 'rgba(30, 30, 30, 0.75)';
  ctx.lineWidth   = 1.5;
  ctx.setLineDash([5, 3]);
  ctx.strokeRect(rx, ry, rw, rh);
  ctx.setLineDash([]);
}

/**
 * Draw the jet colour-bar legend on a canvas.
 * @param {HTMLCanvasElement} canvas
 * @param {number} vmax  – maximum pressure in PSI
 */
function drawColorbar(canvas, vmax) {
  const ctx = canvas.getContext('2d');
  const W = canvas.width, H = canvas.height;
  const grad = ctx.createLinearGradient(0, 0, W, 0);
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
 * Morphological noise filter: removes isolated active sensels (flicker).
 * A pixel is kept only if it has ≥2 active neighbours in its 3×3 window.
 * @param {Uint8Array|number[]} srcData – W×H flat array
 * @param {number} W
 * @param {number} H
 * @returns {Uint8Array} – filtered flat array
 */
function applyNoiseFilter(srcData, W = 256, H = 256) {
  const dstData = new Uint8Array(W * H);

  for (let r = 0; r < H; r++) {
    const rOffset = r * W;
    for (let c = 0; c < W; c++) {
      const idx = rOffset + c;
      const val = srcData[idx];
      if (val === 0) continue;

      let activeNeighbors = 0;
      for (let dr = -1; dr <= 1; dr++) {
        const nr = r + dr;
        if (nr < 0 || nr >= H) continue;
        const nrOffset = nr * W;
        for (let dc = -1; dc <= 1; dc++) {
          if (dr === 0 && dc === 0) continue;
          const nc = c + dc;
          if (nc >= 0 && nc < W && srcData[nrOffset + nc] > 0) {
            activeNeighbors++;
          }
        }
      }

      dstData[idx] = activeNeighbors >= 2 ? val : 0;
    }
  }

  return dstData;
}

// computeFrameMetrics() REMOVED — metrics are now computed on the backend
// from float32 PSI data (not uint8 visualization values) for numerical accuracy.

