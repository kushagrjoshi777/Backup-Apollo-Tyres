/**
 * app.js – Apollo Tyre Dashboard
 * Professional UI controller with smooth theme transitions and export
 */

'use strict';

// ──────────────────────────────────────────────────────
// STATE
// ──────────────────────────────────────────────────────

const state = {
  jobId: null,
  data: null,
  frameIndex: 0,
  selectedFrameNumber: null,
  playing: false,
  lastFrameTs: 0,
  speedMultiplier: 5,
  noiseFilterActive: false,
  minPressure: 5,
  maxPressure: 220,
  speed: 0,
  position: 'FR'
};

// ──────────────────────────────────────────────────────
// DOM REFS
// ──────────────────────────────────────────────────────

const $ = id => document.getElementById(id);

const uploadPage = $('upload-page');
const dashPage = $('dashboard-page');
const dropZone = $('drop-zone');
const fileInput = $('file-input');
const progressWrap = $('upload-progress');
const progressBar = $('progress-bar');
const progressLbl = $('progress-label');
const progressSub = $('progress-sub');

const statusPill = $('status-pill');
const fileBadge = $('file-badge');
const tyreTypeBadge = $('tyre-type-badge');
const positionBadge = $('position-badge');
const noiseToggle = $('noise-toggle');
const themeToggle = $('theme-toggle');
const newFileBtn = $('new-file-btn');
const exportBtn = $('export-btn');

const tirePreset = $('tyre-preset');
const tyrePositionInput = $('tyre-position');
const minPressureInput = $('min-pressure');
const maxPressureInput = $('max-pressure');
const speedInput = $('speed');
const customRangeGroup = $('custom-range-group');
const minVal = $('min-val');
const maxVal = $('max-val');

const animCanvas = $('anim-canvas');
const animOverlay = $('anim-overlay');
const bestCanvas = $('best-canvas');
const bestOverlay = $('best-overlay');
const colorbarCvs = $('colorbar-canvas');

const btnPlay = $('btn-play');
const playIcon = $('play-icon');
const pauseIcon = $('pause-icon');
const frameSlider = $('frame-slider');
const sliderLabel = $('slider-label');
const speedSlider = $('speed-slider');
const speedLabel = $('speed-label');

const pressureMeta = $('pressure-meta');
const speedMeta = $('speed-meta');
const posMeta = $('pos-meta');
const bestFrameInput = $('best-frame-input');
const bestFrameBtn = $('best-frame-btn');

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

function autoDetectFileMetadata(file) {
  const fname = file.name;

  // Detect speed from filename (e.g., 40kmph or 15kmph)
  const speedMatch = fname.match(/(\d+(?:\.\d+)?)\s*(?:kmph|km\/h|kmh)/i);
  if (speedMatch && speedMatch[1]) {
    const spd = parseInt(speedMatch[1], 10);
    speedInput.value = spd;
    state.speed = spd;
  }

  // Detect position from filename (e.g., _FR or FL)
  const posMatch = fname.match(/\b(FL|FR|RL|RR)\b/i) || fname.match(/[_\-](FL|FR|RL|RR)[_\-\.]/i);
  if (posMatch && posMatch[1]) {
    const pos = posMatch[1].toUpperCase();
    if (tyrePositionInput) tyrePositionInput.value = pos;
    state.position = pos;
  }

  // Read first 2000 bytes to check header if needed
  const reader = new FileReader();
  reader.onload = function(e) {
    const text = e.target.result;
    if (!speedMatch) {
      const headerSpeed = text.match(/(\d+(?:\.\d+)?)\s*(?:kmph|km\/h|kmh)/i);
      if (headerSpeed && headerSpeed[1]) {
        const spd = parseInt(headerSpeed[1], 10);
        speedInput.value = spd;
        state.speed = spd;
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

// Preset handler
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
  tyrePositionInput.addEventListener('change', () => {
    state.position = tyrePositionInput.value;
  });
}

minPressureInput.addEventListener('input', () => {
  state.minPressure = parseInt(minPressureInput.value);
  updatePressureDisplay();
});

maxPressureInput.addEventListener('input', () => {
  state.maxPressure = parseInt(maxPressureInput.value);
  updatePressureDisplay();
});

speedInput.addEventListener('input', () => {
  state.speed = parseInt(speedInput.value) || 0;
});

function updatePressureDisplay() {
  minVal.textContent = `${state.minPressure} PSI`;
  maxVal.textContent = `${state.maxPressure} PSI`;
}

// Theme toggle
themeToggle.addEventListener('change', () => {
  document.documentElement.classList.toggle('theme-dark', themeToggle.checked);
  const label = themeToggle.parentElement.querySelector('.theme-label');
  if (label) {
    label.textContent = themeToggle.checked ? 'Light' : 'Dark';
  }
  localStorage.setItem('theme-dark', themeToggle.checked);
});

// Load saved theme
if (localStorage.getItem('theme-dark') === 'true') {
  themeToggle.checked = true;
  document.documentElement.classList.add('theme-dark');
  themeToggle.parentElement.querySelector('.theme-label').textContent = 'Light';
}

// Noise filter
noiseToggle.addEventListener('change', () => {
  state.noiseFilterActive = noiseToggle.checked;
  if (state.data) {
    renderBestFrame();
    updateAnimFrame(state.frameIndex, false);
  }
});

// File operations
newFileBtn.addEventListener('click', resetToUpload);
exportBtn.addEventListener('click', exportResults);

// ──────────────────────────────────────────────────────
// UPLOAD FLOW
// ──────────────────────────────────────────────────────

function startUpload(file) {
  if (!file.name.toLowerCase().endsWith('.csv')) {
    alert('Please select a .csv file.');
    return;
  }

  fileBadge.textContent = file.name;
  showProgressUI();
  setStatus('working', 'Processing');

  const selectedPos = tyrePositionInput ? tyrePositionInput.value : state.position;
  state.position = selectedPos;
  state.speed = parseInt(speedInput.value, 10) || 0;

  const fd = new FormData();
  fd.append('file', file);
  fd.append('tyre_type', state.minPressure === 5 && state.maxPressure === 100 ? 'PCR' : 'TBR');
  fd.append('min_pressure', state.minPressure);
  fd.append('max_pressure', state.maxPressure);
  fd.append('speed', state.speed);
  fd.append('position', state.position);

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
      setProgress(40, 'Processing', 'Analyzing pressure data...');
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
      const res = await fetch(`/status/${jobId}`);
      const json = await res.json();

      if (json.status === 'done') {
        clearInterval(interval);
        setProgress(95, 'Loading', 'Rendering dashboard...');
        loadResult(jobId);
      } else if (json.status === 'error') {
        clearInterval(interval);
        onError(json.error || 'Processing failed');
      } else {
        const elapsed = (Date.now() - startTime) / 1000;
        const pct = 40 + (1 - Math.exp(-elapsed / 15)) * 50;
        setProgress(pct, 'Processing', `${elapsed.toFixed(1)}s`);
      }
    } catch {
      // Continue polling
    }
  }, 1000);
}

async function loadResult(jobId) {
  const res = await fetch(`/result/${jobId}`);
  const data = await res.json();
  if (data.error) {
    onError(data.error);
    return;
  }

  state.data = data;
  state.selectedFrameNumber = data.best_frame_number;
  if (data.speed !== undefined) state.speed = data.speed;
  if (data.position) state.position = data.position;
  setProgress(100, 'Complete', `${data.frame_numbers.length} frames`);

  setTimeout(() => {
    showDashboard(data);
    setStatus('done', 'Ready');
  }, 300);
}

// ──────────────────────────────────────────────────────
// DASHBOARD
// ──────────────────────────────────────────────────────

function showDashboard(data) {
  uploadPage.classList.remove('active');
  dashPage.classList.add('active');

  const { frame_numbers, vmax, vmin, tyre_type, speed, position } = data;

  tyreTypeBadge.textContent = tyre_type;
  if (positionBadge) positionBadge.textContent = position || state.position;
  if (pressureMeta) pressureMeta.textContent = `${vmin}–${vmax} PSI`;
  if (speedMeta) speedMeta.textContent = `${speed} km/h`;
  if (posMeta) posMeta.textContent = position || state.position;

  drawColorbar(colorbarCvs, vmax);
  $('vmin-label').textContent = vmin;
  $('vmid-label').textContent = Math.round((vmin + vmax) / 2);
  $('vmax-label').textContent = vmax;

  renderBestFrame();

  frameSlider.min = 0;
  frameSlider.max = frame_numbers.length - 1;
  frameSlider.value = data.best_frame_index;
  state.frameIndex = data.best_frame_index;

  if (bestFrameInput) {
    bestFrameInput.min = Math.min(...frame_numbers);
    bestFrameInput.max = Math.max(...frame_numbers);
    bestFrameInput.value = data.best_frame_number;
  }

  updateAnimFrame(data.best_frame_index, false);

  // Clone buttons to remove old listeners
  const newBtnPlay = btnPlay.cloneNode(true);
  btnPlay.replaceWith(newBtnPlay);
  const newFrameSlider = frameSlider.cloneNode(true);
  frameSlider.replaceWith(newFrameSlider);
  const newSpeedSlider = speedSlider.cloneNode(true);
  speedSlider.replaceWith(newSpeedSlider);
  if (bestFrameBtn) {
    const newBestFrameBtn = bestFrameBtn.cloneNode(true);
    bestFrameBtn.replaceWith(newBestFrameBtn);
    newBestFrameBtn.addEventListener('click', selectCustomFrame);
  }

  // Re-assign refs
  const freshBtnPlay = $('btn-play');
  const freshFrameSlider = $('frame-slider');
  const freshSpeedSlider = $('speed-slider');

  freshBtnPlay.addEventListener('click', togglePlay);
  freshFrameSlider.addEventListener('input', () => {
    state.frameIndex = parseInt(freshFrameSlider.value, 10);
    updateAnimFrame(state.frameIndex, false);
  });
  freshSpeedSlider.addEventListener('input', () => {
    state.speedMultiplier = parseInt(freshSpeedSlider.value, 10);
    speedLabel.textContent = `${state.speedMultiplier}×`;
  });
}

function renderBestFrame() {
  const { frames, vmax } = state.data;
  const frameNum = state.selectedFrameNumber ?? state.data.best_frame_number;
  const rawFrame = frames[String(frameNum)];

  const analyzed = state.noiseFilterActive
    ? computeFrameMetrics(applyNoiseFilter(rawFrame), state.data.vmin, vmax, frameNum, state.data.position)
    : computeFrameMetrics(rawFrame, state.data.vmin, vmax, frameNum, state.data.position);

  drawHeatmap(bestCanvas, state.noiseFilterActive ? applyNoiseFilter(rawFrame) : rawFrame);
  drawBbox(bestOverlay, analyzed.contact_patch.bbox);

  $('p-width').textContent = analyzed.contact_patch.width_mm;
  $('p-length').textContent = analyzed.contact_patch.length_mm;
  updateMetrics(analyzed.metrics);
  $('best-frame-tag').textContent = `Frame ${frameNum}`;
}

function selectCustomFrame() {
  const requested = Number(bestFrameInput.value);
  if (!state.data || Number.isNaN(requested)) return;
  const idx = state.data.frame_numbers.indexOf(requested);
  if (idx === -1) {
    alert(`Frame ${requested} not found.`);
    return;
  }
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
  const fn = frame_numbers[idx];
  const rawFrame = frames[String(fn)];

  if (state.noiseFilterActive) {
    const filtered = applyNoiseFilter(rawFrame);
    const analyzed = computeFrameMetrics(filtered, vmin, vmax, fn, position);
    drawHeatmap(animCanvas, filtered);
    updateMetrics(analyzed.metrics);
  } else {
    drawHeatmap(animCanvas, rawFrame);
    if (metrics && metrics[idx]) {
      updateMetrics(metrics[idx]);
    }
  }

  $('anim-frame-tag').textContent = `Frame ${fn}`;
  sliderLabel.textContent = `Frame ${fn} (${idx + 1}/${frame_numbers.length})`;
  if (updateSlider) $('frame-slider').value = idx;
}

function updateMetrics(m) {
  const setEl = (id, val) => {
    const el = $(id);
    if (el) el.textContent = val !== undefined && val !== null ? val : '—';
  };

  setEl('m-area', m.contact_area_mm2 ? m.contact_area_mm2.toLocaleString() : '—');
  setEl('m-ratio', m.contact_ratio_pct);
  setEl('m-fp-w', m.fp_width_mm);
  setEl('m-fp-l', m.fp_length_mm);
  setEl('m-s1', m.outer_shoulder_s1_mm);
  setEl('m-s2', m.inner_shoulder_s2_mm);
  setEl('m-skew', m.shoulder_skewness_pct);
  setEl('m-sq-ratio', m.squareness_s1_s2_pct);
  setEl('m-sq-avg', m.squareness_avg);
  setEl('m-ls-ratio', m.land_sea_ratio);
  setEl('m-avg', m.contact_pressure_psi || m.avg_pressure_psi);
  setEl('m-max-l', m.maximum_length_mm);
  setEl('m-max-w', m.maximum_width_mm);

  setEl('m-frame', m.frame_number);
  setEl('m-peak', m.peak_pressure_psi);
  setEl('m-load', m.estimated_load_kg ? m.estimated_load_kg.toLocaleString() : '—');

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
    // Get the heatmap canvas as a data URL
    const canvasData = bestCanvas.toDataURL('image/png');
    
    // Send export request to backend
    fetch(`/export/${state.jobId}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        speed: state.speed,
        canvas_data: canvasData
      })
    })
    .then(res => {
      if (!res.ok) throw new Error(`Export failed: ${res.statusText}`);
      return res.blob();
    })
    .then(blob => {
      // Create download link
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = `Apollo_TyrePressure_${state.speed}kmph_Report.pdf`;
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
      URL.revokeObjectURL(url);
    })
    .catch(err => {
      console.error('Export error:', err);
      alert(`Export failed: ${err.message}`);
    });
  } catch (err) {
    console.error('Export preparation error:', err);
    alert(`Export failed: ${err.message}`);
  }
}

function drawColorScale(doc, x, y, vmin, vmax) {
  // Draw color bar
  const barWidth = 8;
  const barHeight = 50;
  const steps = 10;
  
  for (let i = 0; i < steps; i++) {
    const ratio = i / steps;
    const hue = (1 - ratio) * 240; // Blue to Red
    const color = hslToRgb(hue, 100, 50);
    doc.setFillColor(...color);
    doc.rect(x, y + (i * barHeight / steps), barWidth, barHeight / steps, 'F');
  }
  
  // Border
  doc.setDrawColor(0, 0, 0);
  doc.rect(x, y, barWidth, barHeight);
  
  // Labels
  doc.setFontSize(8);
  doc.setTextColor(0, 0, 0);
  doc.text(String(vmax), x + barWidth + 2, y + 2);
  doc.text(String(Math.round((vmin + vmax) / 2)), x + barWidth + 2, y + barHeight / 2);
  doc.text(String(vmin), x + barWidth + 2, y + barHeight - 2);
  doc.setFontSize(7);
  doc.text('psi', x + barWidth + 2, y + barHeight + 3);
}

function hslToRgb(h, s, l) {
  h = h / 360;
  s = s / 100;
  l = l / 100;
  
  let r, g, b;
  if (s === 0) {
    r = g = b = l;
  } else {
    const hue2rgb = (p, q, t) => {
      if (t < 0) t += 1;
      if (t > 1) t -= 1;
      if (t < 1/6) return p + (q - p) * 6 * t;
      if (t < 1/2) return q;
      if (t < 2/3) return p + (q - p) * (2/3 - t) * 6;
      return p;
    };
    const q = l < 0.5 ? l * (1 + s) : l + s - l * s;
    const p = 2 * l - q;
    r = hue2rgb(p, q, h + 1/3);
    g = hue2rgb(p, q, h);
    b = hue2rgb(p, q, h - 1/3);
  }
  
  return [Math.round(r * 255), Math.round(g * 255), Math.round(b * 255)];
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
  if (status === 'done') statusPill.classList.add('done');
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

  state.data = null;
  state.jobId = null;
  state.frameIndex = 0;
  state.selectedFrameNumber = null;

  fileInput.value = '';
  progressWrap.classList.add('hidden');
  setStatus('idle', 'Idle');
  dashPage.classList.remove('active');
  uploadPage.classList.add('active');
}

function formatBytes(bytes) {
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1048576) return (bytes / 1024).toFixed(1) + ' KB';
  return (bytes / 1048576).toFixed(1) + ' MB';
}

// Initialize
updatePressureDisplay();
