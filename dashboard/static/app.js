/**
 * app.js – Apollo Tyre Dashboard
 * Supports .csv and .xsn files, FFT noise reduction, exact tyre outline.
 */

'use strict';

// ──────────────────────────────────────────────────────
// STATE
// ──────────────────────────────────────────────────────

const state = {
  jobId:              null,
  data:               null,
  frameIndex:         0,
  selectedFrameNumber:null,
  playing:            false,
  lastFrameTs:        0,
  speedMultiplier:    5,
  noiseFilterActive:  false,
  minPressure:        5,
  maxPressure:        220,
  speed:              0,
  position:           'FR',
  fftKeepRatio:       0.97,
  // grid dims come from the data (may be 256×256 or 126×129 for XSN)
  gridW:              256,
  gridH:              256,
  senselMm:           1.6,
};

// ──────────────────────────────────────────────────────
// DOM REFS
// ──────────────────────────────────────────────────────

const $ = id => document.getElementById(id);

const uploadPage    = $('upload-page');
const dashPage      = $('dashboard-page');
const dropZone      = $('drop-zone');
const fileInput     = $('file-input');
const progressWrap  = $('upload-progress');
const progressBar   = $('progress-bar');
const progressLbl   = $('progress-label');
const progressSub   = $('progress-sub');

const statusPill    = $('status-pill');
const fileBadge     = $('file-badge');
const tyreTypeBadge = $('tyre-type-badge');
const positionBadge = $('position-badge');
const fftBadge      = $('fft-badge');
const noiseToggle   = $('noise-toggle');
const themeToggle   = $('theme-toggle');
const newFileBtn    = $('new-file-btn');
const exportBtn     = $('export-btn');

const tirePreset        = $('tyre-preset');
const tyrePositionInput = $('tyre-position');
const minPressureInput  = $('min-pressure');
const maxPressureInput  = $('max-pressure');
const speedInput        = $('speed');
const customRangeGroup  = $('custom-range-group');
const minVal            = $('min-val');
const maxVal            = $('max-val');
const fftKeepInput      = $('fft-keep');
const fftValLabel       = $('fft-val');
const fftHintLabel      = $('fft-hint');

const animCanvas    = $('anim-canvas');
const animOverlay   = $('anim-overlay');
const bestCanvas    = $('best-canvas');
const bestOverlay   = $('best-overlay');
const colorbarCvs   = $('colorbar-canvas');

const btnPlay       = $('btn-play');
const playIcon      = $('play-icon');
const pauseIcon     = $('pause-icon');
const frameSlider   = $('frame-slider');
const sliderLabel   = $('slider-label');
const speedSlider   = $('speed-slider');
const speedLabel    = $('speed-label');

const pressureMeta  = $('pressure-meta');
const speedMeta     = $('speed-meta');
const posMeta       = $('pos-meta');
const fftMeta       = $('fft-meta');
const bestFrameInput= $('best-frame-input');
const bestFrameBtn  = $('best-frame-btn');

// ──────────────────────────────────────────────────────
// EVENT LISTENERS – UPLOAD PAGE
// ──────────────────────────────────────────────────────

dropZone.addEventListener('click', () => fileInput.click());

fileInput.addEventListener('change', () => {
  if (fileInput.files.length) {
    autoDetectFileMetadata(fileInput.files[0]);
    startUpload(fileInput.files[0]);
  }
});

['dragenter', 'dragover'].forEach(evt =>
  dropZone.addEventListener(evt, e => {
    e.preventDefault();
    dropZone.classList.add('drag-over');
  })
);

['dragleave', 'drop'].forEach(evt =>
  dropZone.addEventListener(evt, e => {
    e.preventDefault();
    dropZone.classList.remove('drag-over');
  })
);

dropZone.addEventListener('drop', e => {
  const file = e.dataTransfer.files[0];
  if (file) {
    autoDetectFileMetadata(file);
    startUpload(file);
  }
});

// ── Auto-detect metadata from filename ───────────────
function autoDetectFileMetadata(file) {
  const fname = file.name;

  const speedMatch = fname.match(/(\d+(?:\.\d+)?)\s*(?:kmph|km\/h|kmh)/i);
  if (speedMatch && speedMatch[1]) {
    speedInput.value = parseInt(speedMatch[1], 10);
    state.speed = parseInt(speedMatch[1], 10);
  }

  const posMatch = fname.match(/\b(FL|FR|RL|RR)\b/i) || fname.match(/[_\-](FL|FR|RL|RR)[_\-\.]/i);
  if (posMatch && posMatch[1]) {
    const pos = posMatch[1].toUpperCase();
    if (tyrePositionInput) tyrePositionInput.value = pos;
    state.position = pos;
  }

  // For CSV: read first 2000 bytes to check header
  if (!fname.toLowerCase().endsWith('.xsn')) {
    const reader = new FileReader();
    reader.onload = function(e) {
      const text = e.target.result;
      if (!speedMatch) {
        const headerSpeed = text.match(/(\d+(?:\.\d+)?)\s*(?:kmph|km\/h|kmh)/i);
        if (headerSpeed && headerSpeed[1]) {
          speedInput.value = parseInt(headerSpeed[1], 10);
          state.speed = parseInt(headerSpeed[1], 10);
        }
      }
      if (!posMatch) {
        const headerPos = text.match(/\b(FL|FR|RL|RR)\b/i);
        if (headerPos && headerPos[1]) {
          const pos = headerPos[1].toUpperCase();
          if (tyrePositionInput) tyrePositionInput.value = pos;
          state.position = pos;
        }
      }
    };
    reader.readAsText(file.slice(0, 2000));
  }
}

// ── Preset handler ────────────────────────────────────
tirePreset.addEventListener('change', () => {
  if (tirePreset.value === 'tbr-preset') {
    minPressureInput.value = 5;
    maxPressureInput.value = 220;
    customRangeGroup.style.display = 'none';
    state.minPressure = 5;
    state.maxPressure = 220;
  } else if (tirePreset.value === 'pcr-preset') {
    minPressureInput.value = 5;
    maxPressureInput.value = 100;
    customRangeGroup.style.display = 'none';
    state.minPressure = 5;
    state.maxPressure = 100;
  } else {
    customRangeGroup.style.display = 'flex';
    customRangeGroup.style.flexDirection = 'column';
    customRangeGroup.style.gap = '16px';
  }
  updatePressureDisplay();
});

if (tyrePositionInput) {
  tyrePositionInput.addEventListener('change', () => { state.position = tyrePositionInput.value; });
}

minPressureInput.addEventListener('input', () => { state.minPressure = parseInt(minPressureInput.value); updatePressureDisplay(); });
maxPressureInput.addEventListener('input', () => { state.maxPressure = parseInt(maxPressureInput.value); updatePressureDisplay(); });
speedInput.addEventListener('input', () => { state.speed = parseInt(speedInput.value) || 0; });

// FFT slider
fftKeepInput.addEventListener('input', () => {
  const pct = parseInt(fftKeepInput.value, 10);
  state.fftKeepRatio = pct / 100;
  updateFftDisplay(pct);
});

function updateFftDisplay(pct) {
  const fftPct = pct !== undefined ? pct : Math.round(state.fftKeepRatio * 100);
  if (fftValLabel)  fftValLabel.textContent  = `${fftPct}% preserved`;
  if (fftHintLabel) fftHintLabel.textContent = fftPct >= 95 ? 'Keep ' + fftPct + '% of signal' :
                                               fftPct >= 85 ? 'Moderate filtering' : 'Aggressive filtering';
}

function updatePressureDisplay() {
  minVal.textContent = `${state.minPressure} PSI`;
  maxVal.textContent = `${state.maxPressure} PSI`;
}

// ── Theme toggle ──────────────────────────────────────
themeToggle.addEventListener('change', () => {
  document.documentElement.classList.toggle('theme-dark', themeToggle.checked);
  const label = themeToggle.parentElement.querySelector('.theme-label');
  if (label) label.textContent = themeToggle.checked ? 'Light' : 'Dark';
  localStorage.setItem('theme-dark', themeToggle.checked);
});

if (localStorage.getItem('theme-dark') === 'true') {
  themeToggle.checked = true;
  document.documentElement.classList.add('theme-dark');
  themeToggle.parentElement.querySelector('.theme-label').textContent = 'Light';
}

// ── Noise (morphological) filter toggle ──────────────
noiseToggle.addEventListener('change', () => {
  state.noiseFilterActive = noiseToggle.checked;
  if (state.data) {
    renderBestFrame();
    updateAnimFrame(state.frameIndex, false);
  }
});

// ── File operations ───────────────────────────────────
newFileBtn.addEventListener('click', resetToUpload);
exportBtn.addEventListener('click', exportResults);

// ──────────────────────────────────────────────────────
// UPLOAD FLOW
// ──────────────────────────────────────────────────────

function startUpload(file) {
  const ext = file.name.toLowerCase().split('.').pop();
  if (ext !== 'csv' && ext !== 'xsn') {
    alert('Please select a .csv or .xsn file.');
    return;
  }

  fileBadge.textContent = file.name;
  showProgressUI();
  setStatus('working', 'Processing');

  const selectedPos = tyrePositionInput ? tyrePositionInput.value : state.position;
  state.position = selectedPos;
  state.speed = parseInt(speedInput.value, 10) || 0;
  state.fftKeepRatio = parseInt(fftKeepInput.value, 10) / 100;

  const fd = new FormData();
  fd.append('file', file);
  fd.append('tyre_type', state.minPressure === 5 && state.maxPressure === 100 ? 'PCR' : 'TBR');
  fd.append('min_pressure', state.minPressure);
  fd.append('max_pressure', state.maxPressure);
  fd.append('speed', state.speed);
  fd.append('position', state.position);
  fd.append('fft_keep_ratio', state.fftKeepRatio);

  const xhr = new XMLHttpRequest();
  xhr.open('POST', '/upload');

  xhr.upload.addEventListener('progress', e => {
    if (e.lengthComputable) {
      const pct = Math.round(e.loaded / e.total * 100);
      setProgress(pct * 0.4, `Uploading ${pct}%`, formatBytes(e.loaded) + ' / ' + formatBytes(e.total));
    }
  });

  xhr.addEventListener('load', () => {
    if (xhr.status === 202) {
      const { job_id } = JSON.parse(xhr.responseText);
      state.jobId = job_id;
      setProgress(40, 'Processing', 'Analysing pressure data…');
      pollJob(job_id);
    } else {
      const err = JSON.parse(xhr.responseText || '{}');
      onError(err.error || 'Upload failed');
    }
  });

  xhr.addEventListener('error', () => onError('Network error'));
  xhr.send(fd);
}

function pollJob(jobId) {
  const startTime = Date.now();
  const interval = setInterval(async () => {
    try {
      const res  = await fetch(`/status/${jobId}`);
      const json = await res.json();

      if (json.status === 'done') {
        clearInterval(interval);
        setProgress(95, 'Loading', 'Rendering dashboard…');
        loadResult(jobId);
      } else if (json.status === 'error') {
        clearInterval(interval);
        onError(json.error || 'Processing failed');
      } else {
        const elapsed = (Date.now() - startTime) / 1000;
        const pct = 40 + (1 - Math.exp(-elapsed / 15)) * 50;
        setProgress(pct, 'Processing', `${elapsed.toFixed(1)}s elapsed`);
      }
    } catch { /* continue polling */ }
  }, 1000);
}

async function loadResult(jobId) {
  const res  = await fetch(`/result/${jobId}`);
  const data = await res.json();
  if (data.error) { onError(data.error); return; }

  state.data = data;
  state.selectedFrameNumber = data.best_frame_number;
  if (data.speed !== undefined) state.speed = data.speed;
  if (data.position) state.position = data.position;
  if (data.fft_keep_ratio !== undefined) state.fftKeepRatio = data.fft_keep_ratio;

  // Determine grid dimensions from first frame
  const firstFn  = String(data.frame_numbers[0]);
  const firstArr = data.frames[firstFn];
  // frames are serialised as flat arrays; sqrt gives the grid size for square grids,
  // but for XSN we get rows×cols from the result metadata
  if (data.sensel_w_mm && data.sensel_h_mm) {
    state.senselMm = (data.sensel_w_mm + data.sensel_h_mm) / 2;
  }

  // Determine grid dimensions from the contact_patch outline or best_metrics
  // Backend sends frames as flat arrays of size rows*cols
  const bm = data.best_metrics;
  if (bm && bm.active_sensels > 0) {
    // Infer from outline_points or frame size
    const totalSensels = firstArr.length;
    // Try to figure out grid W/H from the outline points bounding box
    const op = (data.contact_patch || {}).outline_points || [];
    if (op.length > 0) {
      const maxX = Math.max(...op.map(p => p.x)) + 1;
      const maxY = Math.max(...op.map(p => p.y)) + 1;
      // gridW = max column index + 1, but we don't know exact dims from outline alone
      // Use sqrt as fallback for square grids, otherwise detect from frame array length
      const sqrtTotal = Math.sqrt(totalSensels);
      if (Number.isInteger(sqrtTotal)) {
        state.gridW = sqrtTotal;
        state.gridH = sqrtTotal;
      } else {
        // For 126×129 = 16254 sensels
        // Try common XSN dimensions
        for (const [w, h] of [[129, 126], [126, 129], [128, 128], [256, 256], [64, 64]]) {
          if (w * h === totalSensels) {
            state.gridW = w;
            state.gridH = h;
            break;
          }
        }
      }
    } else {
      const sqrtTotal = Math.sqrt(totalSensels);
      if (Number.isInteger(sqrtTotal)) {
        state.gridW = sqrtTotal;
        state.gridH = sqrtTotal;
      }
    }
  }

  setProgress(100, 'Complete', `${data.frame_numbers.length} frames`);
  setTimeout(() => { showDashboard(data); setStatus('done', 'Ready'); }, 300);
}

// ──────────────────────────────────────────────────────
// DASHBOARD
// ──────────────────────────────────────────────────────

function showDashboard(data) {
  uploadPage.classList.remove('active');
  dashPage.classList.add('active');

  const { frame_numbers, vmax, vmin, tyre_type, speed, position } = data;
  const fftPct = Math.round((data.fft_keep_ratio || state.fftKeepRatio) * 100);
  const isXsn  = !!data.is_xsn;
  const isProvisional = !!data.is_provisional;

  // ── [XSN-DIAG] Frontend traceability ─────────────────────────────────
  console.log('[XSN-DIAG] ═══════════════════════════════════════════════');
  console.log('[XSN-DIAG] showDashboard() — data received from backend');
  console.log('[XSN-DIAG]   source_file     :', data.source_file);
  console.log('[XSN-DIAG]   is_xsn          :', isXsn);
  console.log('[XSN-DIAG]   is_provisional  :', isProvisional);
  console.log('[XSN-DIAG]   frame_rate      :', data.frame_rate);
  console.log('[XSN-DIAG]   total_frames    :', data.total_frames_in_file);
  console.log('[XSN-DIAG]   playback frames :', frame_numbers.length);
  console.log('[XSN-DIAG]   best_frame      :', data.best_frame_number);
  console.log('[XSN-DIAG]   peak PSI        :', data.best_metrics?.peak_pressure_psi);
  console.log('[XSN-DIAG]   avg PSI         :', data.best_metrics?.avg_pressure_psi);
  console.log('[XSN-DIAG]   contact area    :', data.best_metrics?.contact_area_mm2, 'mm²');
  console.log('[XSN-DIAG]   heatmap renders from: data.frames[frameNum] → drawHeatmap(canvas, flatUint8, W, H)');
  console.log('[XSN-DIAG]   heatmap source file : static/heatmap.js → drawHeatmap()');
  console.log('[XSN-DIAG] ═══════════════════════════════════════════════');
  // ─────────────────────────────────────────────────────────────────────

  tyreTypeBadge.textContent = tyre_type;
  if (positionBadge) positionBadge.textContent = position || state.position;
  if (fftBadge)      fftBadge.textContent = `FFT ${fftPct}%`;
  if (pressureMeta)  pressureMeta.textContent = `${vmin}–${vmax} ${isProvisional ? 'Prov. PSI' : 'PSI'}`;
  if (speedMeta)     speedMeta.textContent = `${speed} km/h`;
  if (posMeta)       posMeta.textContent = position || state.position;
  if (fftMeta)       fftMeta.textContent = `${fftPct}% kept`;

  // XSN-specific UI elements
  const provisionalBanner = $('provisional-banner');
  if (provisionalBanner) provisionalBanner.style.display = isProvisional ? 'flex' : 'none';

  const frameRateBadge = $('frame-rate-badge');
  if (frameRateBadge) {
    if (data.frame_rate != null) {
      frameRateBadge.textContent = `${data.frame_rate.toFixed(2)} FPS`;
      frameRateBadge.style.display = 'inline-block';
    } else {
      frameRateBadge.style.display = 'none';
    }
  }

  const framerateCard = $('framerate-card');
  const framerateMeta = $('framerate-meta');
  if (framerateCard && framerateMeta) {
    if (data.frame_rate != null) {
      framerateMeta.textContent = `${data.frame_rate.toFixed(2)} FPS`;
      framerateCard.style.display = '';
    } else {
      framerateCard.style.display = 'none';
    }
  }

  const totalframesCard = $('totalframes-card');
  const totalframesMeta = $('totalframes-meta');
  if (totalframesCard && totalframesMeta) {
    if (data.total_frames_in_file != null) {
      totalframesMeta.textContent = `${data.total_frames_in_file.toLocaleString()}`;
      totalframesCard.style.display = '';
    } else {
      totalframesCard.style.display = 'none';
    }
  }

  // Update PSI unit labels for provisional
  const psiUnitLabel = $('psi-unit-label');
  if (psiUnitLabel) psiUnitLabel.textContent = isProvisional ? 'Prov. PSI' : 'PSI';

  drawColorbar(colorbarCvs, vmax);
  $('vmin-label').textContent = vmin;
  $('vmid-label').textContent = Math.round((vmin + vmax) / 2);
  $('vmax-label').textContent = vmax;

  // Resize overlay canvases to match grid
  [animOverlay, bestOverlay].forEach(c => {
    c.width  = state.gridW;
    c.height = state.gridH;
  });

  renderBestFrame();

  frameSlider.min   = 0;
  frameSlider.max   = frame_numbers.length - 1;
  frameSlider.value = data.best_frame_index;
  state.frameIndex  = data.best_frame_index;

  if (bestFrameInput) {
    bestFrameInput.min   = Math.min(...frame_numbers);
    bestFrameInput.max   = Math.max(...frame_numbers);
    bestFrameInput.value = data.best_frame_number;
  }

  updateAnimFrame(data.best_frame_index, false);

  // Clone buttons to remove old listeners
  const newBtnPlay      = btnPlay.cloneNode(true);
  btnPlay.replaceWith(newBtnPlay);
  const newFrameSlider  = frameSlider.cloneNode(true);
  frameSlider.replaceWith(newFrameSlider);
  const newSpeedSlider  = speedSlider.cloneNode(true);
  speedSlider.replaceWith(newSpeedSlider);

  if (bestFrameBtn) {
    const newBestBtn = bestFrameBtn.cloneNode(true);
    bestFrameBtn.replaceWith(newBestBtn);
    newBestBtn.addEventListener('click', selectCustomFrame);
  }

  $('btn-play').addEventListener('click', togglePlay);
  $('frame-slider').addEventListener('input', () => {
    state.frameIndex = parseInt($('frame-slider').value, 10);
    updateAnimFrame(state.frameIndex, false);
  });
  $('speed-slider').addEventListener('input', () => {
    state.speedMultiplier = parseInt($('speed-slider').value, 10);
    $('speed-label').textContent = `${state.speedMultiplier}×`;
  });
}

function renderBestFrame() {
  const { frames, vmax, vmin, contact_patch, position } = state.data;
  const frameNum = state.selectedFrameNumber ?? state.data.best_frame_number;
  const rawFrame = frames[String(frameNum)];

  const displayData = state.noiseFilterActive
    ? applyNoiseFilter(rawFrame, state.gridW, state.gridH)
    : rawFrame;

  // Re-compute metrics client-side (for noise-filtered view)
  let outlinePoints = (contact_patch || {}).outline_points || [];
  let patchW = (contact_patch || {}).width_mm;
  let patchL = (contact_patch || {}).length_mm;

  if (state.noiseFilterActive) {
    const analyzed = computeFrameMetrics(
      displayData, vmin, vmax, frameNum, position,
      state.gridW, state.gridH, state.senselMm
    );
    outlinePoints = analyzed.contact_patch.outline_points;
    patchW = analyzed.contact_patch.width_mm;
    patchL = analyzed.contact_patch.length_mm;
    updateMetrics(analyzed.metrics);
  } else {
    const bestIdx = state.data.best_frame_index;
    const frameIdx = state.data.frame_numbers.indexOf(frameNum);
    const m = state.data.metrics[frameIdx >= 0 ? frameIdx : bestIdx];
    if (m) updateMetrics(m);
  }

  drawHeatmap(bestCanvas, displayData, state.gridW, state.gridH);
  drawTyreOutline(bestOverlay, outlinePoints, state.gridW, state.gridH);

  $('p-width').textContent  = patchW  ?? '—';
  $('p-length').textContent = patchL  ?? '—';
  $('best-frame-tag').textContent = `Frame ${frameNum}`;
}

function selectCustomFrame() {
  const requested = Number($('best-frame-input').value);
  if (!state.data || Number.isNaN(requested)) return;
  const idx = state.data.frame_numbers.indexOf(requested);
  if (idx === -1) { alert(`Frame ${requested} not found.`); return; }
  state.selectedFrameNumber = requested;
  renderBestFrame();
}

function togglePlay() {
  state.playing = !state.playing;
  const btn = $('btn-play');
  if (state.playing) {
    $('play-icon').classList.add('hidden');
    $('pause-icon').classList.remove('hidden');
    btn.classList.add('playing');
    state.lastFrameTs = performance.now();
    requestAnimationFrame(animLoop);
  } else {
    $('play-icon').classList.remove('hidden');
    $('pause-icon').classList.add('hidden');
    btn.classList.remove('playing');
  }
}

function animLoop(ts) {
  if (!state.playing) return;
  const msBetweenFrames = 1000 / (12 * state.speedMultiplier / 5);
  if (ts - state.lastFrameTs >= msBetweenFrames) {
    state.lastFrameTs = ts;
    const total = state.data.frame_numbers.length;
    state.frameIndex = (state.frameIndex + 1) % total;
    $('frame-slider').value = state.frameIndex;
    updateAnimFrame(state.frameIndex, false);
  }
  requestAnimationFrame(animLoop);
}

function updateAnimFrame(idx, updateSlider = true) {
  const { frame_numbers, frames, metrics, vmax, vmin, position } = state.data;
  const fn       = frame_numbers[idx];
  const rawFrame = frames[String(fn)];

  const displayData = state.noiseFilterActive
    ? applyNoiseFilter(rawFrame, state.gridW, state.gridH)
    : rawFrame;

  drawHeatmap(animCanvas, displayData, state.gridW, state.gridH);

  if (state.noiseFilterActive) {
    const analyzed = computeFrameMetrics(
      displayData, vmin, vmax, fn, position,
      state.gridW, state.gridH, state.senselMm
    );
    updateMetrics(analyzed.metrics);
  } else {
    if (metrics && metrics[idx]) updateMetrics(metrics[idx]);
  }

  $('anim-frame-tag').textContent = `Frame ${fn}`;
  $('slider-label').textContent   = `Frame ${fn} (${idx + 1}/${frame_numbers.length})`;
  if (updateSlider) $('frame-slider').value = idx;
}

function updateMetrics(m) {
  const setEl = (id, val) => {
    const el = $(id);
    if (el) el.textContent = (val !== undefined && val !== null) ? val : '—';
  };

  setEl('m-area',     m.contact_area_mm2 ? m.contact_area_mm2.toLocaleString() : '—');
  setEl('m-ratio',    m.contact_ratio_pct);
  setEl('m-fp-w',     m.fp_width_mm);
  setEl('m-fp-l',     m.fp_length_mm);
  setEl('m-s1',       m.outer_shoulder_s1_mm);
  setEl('m-s2',       m.inner_shoulder_s2_mm);
  setEl('m-skew',     m.shoulder_skewness_pct);
  setEl('m-sq-ratio', m.squareness_s1_s2_pct);
  setEl('m-sq-avg',   m.squareness_avg);
  setEl('m-ls-ratio', m.land_sea_ratio);
  setEl('m-avg',      m.contact_pressure_psi || m.avg_pressure_psi);
  setEl('m-max-l',    m.maximum_length_mm);
  setEl('m-max-w',    m.maximum_width_mm);
  setEl('m-frame',    m.frame_number);
  setEl('m-peak',     m.peak_pressure_psi);
  setEl('m-load',     m.estimated_load_kg ? m.estimated_load_kg.toLocaleString() : '—');

  const covBar = $('coverage-bar');
  if (covBar) covBar.style.width = `${Math.min(m.contact_pct || 0, 100)}%`;
}

// ──────────────────────────────────────────────────────
// EXPORT FUNCTIONALITY
// ──────────────────────────────────────────────────────

function exportResults() {
  if (!state.data || !state.jobId) {
    alert('No analysis data available to export.');
    return;
  }

  try {
    const canvasData = bestCanvas.toDataURL('image/png');

    fetch(`/export/${state.jobId}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ speed: state.speed, canvas_data: canvasData })
    })
    .then(res => {
      if (!res.ok) throw new Error(`Export failed: ${res.statusText}`);
      return res.blob();
    })
    .then(blob => {
      const url  = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href  = url;
      link.download = `Apollo_TyrePressure_${state.speed}kmph_Report.pdf`;
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
      URL.revokeObjectURL(url);
    })
    .catch(err => { console.error('Export error:', err); alert(`Export failed: ${err.message}`); });
  } catch (err) {
    console.error('Export preparation error:', err);
    alert(`Export failed: ${err.message}`);
  }
}

// ──────────────────────────────────────────────────────
// UI HELPERS
// ──────────────────────────────────────────────────────

function showProgressUI() {
  progressWrap.classList.remove('hidden');
  progressBar.style.width = '0%';
}

function setProgress(pct, label, sub) {
  progressBar.style.width = pct + '%';
  progressLbl.textContent = label;
  progressSub.textContent = sub;
}

function setStatus(status, text) {
  statusPill.className = 'status-pill';
  if (status === 'working') statusPill.classList.add('working');
  if (status === 'done')    statusPill.classList.add('done');
  statusPill.textContent = text;
}

function onError(msg) {
  setStatus('error', 'Error');
  setProgress(0, 'Error', msg);
  console.error('Error:', msg);
}

function resetToUpload() {
  state.playing = false;
  $('play-icon').classList.remove('hidden');
  $('pause-icon').classList.add('hidden');
  $('btn-play').classList.remove('playing');

  state.data              = null;
  state.jobId             = null;
  state.frameIndex        = 0;
  state.selectedFrameNumber = null;
  state.gridW             = 256;
  state.gridH             = 256;
  state.senselMm          = 1.6;

  fileInput.value = '';
  progressWrap.classList.add('hidden');
  setStatus('idle', 'Idle');
  dashPage.classList.remove('active');
  uploadPage.classList.add('active');

  // Hide XSN-specific UI elements
  const provisionalBanner = $('provisional-banner');
  if (provisionalBanner) provisionalBanner.style.display = 'none';
  const frameRateBadge = $('frame-rate-badge');
  if (frameRateBadge) frameRateBadge.style.display = 'none';
  const framerateCard = $('framerate-card');
  if (framerateCard) framerateCard.style.display = 'none';
  const totalframesCard = $('totalframes-card');
  if (totalframesCard) totalframesCard.style.display = 'none';
  const psiUnitLabel = $('psi-unit-label');
  if (psiUnitLabel) psiUnitLabel.textContent = 'PSI';
}

function formatBytes(bytes) {
  if (bytes < 1024)    return bytes + ' B';
  if (bytes < 1048576) return (bytes / 1024).toFixed(1) + ' KB';
  return (bytes / 1048576).toFixed(1) + ' MB';
}

// ── Initialise ────────────────────────────────────────
updatePressureDisplay();
updateFftDisplay(97);
