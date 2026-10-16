/**
 * app.js â€“ Apollo Tyre Dashboard
 * Supports .csv and .xsn files, live multi-engine noise removal (Fourier / Gaussian /
 * Butterworth / spatial), per-frame exact bounding edge and explicit report-frame selection.
 */

'use strict';

// â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
// NOISE-FILTER MODEL (shared by the landing page and the dashboard)
// â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

const FILTER_DEFAULTS = {
  filter_type:                  'fft_butterworth_lp',
  fft_cutoff:                   0.80,
  fft_filter_strength:          2,
  gaussian_sigma:               1.0,
  median_size:                  3,
  bilateral_sigma_color:        25,
  bilateral_sigma_space:        2.0,
  notch_sensitivity:            6.0,
  temporal_filter_enabled:      true,
  temporal_spike_threshold:     3.0,
  spatial_filter_enabled:       true,
  spatial_mad_threshold:        4.0,
  patch_filter_enabled:         true,
  min_patch_area_mm2:           12,
  patch_preserve_threshold_psi: 20,
  min_component_px:             12,
  morph_open:                   0,
  morph_close:                  0,
  keep_largest_only:            false,
  min_pressure:                 5,
  rotation_k:                   1,
  show_edge:                    true,   // client-side only
};

const FFT_TYPES = ['fft_butterworth_lp', 'fft_gaussian_lp', 'fft_ideal_lp'];

const FILTER_GROUPS = [
  { group: 'No filtering',   items: [['none', 'None (raw calibrated data)']] },
  { group: 'Spatial',        items: [['gaussian', 'Gaussian blur'], ['median', 'Median'], ['bilateral', 'Bilateral (edge-preserving)']] },
  { group: 'Fourier domain', items: [
      ['fft_butterworth_lp', 'Butterworth low-pass'],
      ['fft_gaussian_lp',    'Gaussian low-pass'],
      ['fft_ideal_lp',       'Ideal (brick-wall) low-pass'],
      ['fft_notch',          'Spectral notch (periodic noise)'],
  ] },
];

const FILTER_LABELS = {
  none: 'No filter', gaussian: 'Gaussian blur', median: 'Median', bilateral: 'Bilateral',
  fft_butterworth_lp: 'Fourier Butterworth LP', fft_gaussian_lp: 'Fourier Gaussian LP',
  fft_ideal_lp: 'Fourier Ideal LP', fft_notch: 'Fourier Notch',
};

const FILTER_INFO = {
  none:               'No smoothing is applied. Only spike repair, noise floor and patch clean-up act on the data.',
  gaussian:           'Spatial Gaussian blur. Smooth, no ringing; softens tread-block edges as Ïƒ grows.',
  median:             'Spatial median. Removes salt-and-pepper sensel noise while keeping edges well.',
  bilateral:          'Edge-preserving smoothing: averages similar pressures only, so edges stay sharp.',
  fft_butterworth_lp: 'Fourier-domain Butterworth low-pass: maximally flat pass-band, order sets roll-off steepness.',
  fft_gaussian_lp:    'Fourier-domain Gaussian low-pass: smooth roll-off, completely free of ringing.',
  fft_ideal_lp:       'Fourier-domain brick-wall low-pass: sharpest cut-off but can ring (Gibbs) near edges.',
  fft_notch:          'Detects strong periodic peaks in the spectrum (stripe / cross-talk noise) and notches them out.',
};

const FILTER_SECTIONS = [
  { title: '1 Â· Noise-removal engine', controls: [
    { key: 'filter_type', kind: 'select', label: 'Filter type' },
    { key: 'fft_cutoff', kind: 'range', label: 'Cutoff frequency', min: 5, max: 100, step: 1, scale: 100, dec: 0, unit: '% Nyquist',
      show: FFT_TYPES, hint: 'Fraction of the sensor Nyquist frequency that is kept. Lower = smoother (more detail removed).' },
    { key: 'fft_filter_strength', kind: 'range', label: 'Butterworth order', min: 1, max: 8, step: 1, dec: 0,
      show: ['fft_butterworth_lp'], hint: 'Roll-off steepness. High orders approach an ideal filter (ringing).' },
    { key: 'gaussian_sigma', kind: 'range', label: 'Gaussian Ïƒ', min: 2, max: 60, step: 1, scale: 10, dec: 1, unit: 'sensels', show: ['gaussian'] },
    { key: 'median_size', kind: 'select', num: true, label: 'Median kernel', show: ['median'],
      options: [[3, '3 Ã— 3'], [5, '5 Ã— 5'], [7, '7 Ã— 7'], [9, '9 Ã— 9']] },
    { key: 'bilateral_sigma_color', kind: 'range', label: 'Range Ïƒ (pressure)', min: 2, max: 120, step: 1, dec: 0, unit: 'PSI', show: ['bilateral'],
      hint: 'Pressure difference up to which neighbours are averaged.' },
    { key: 'bilateral_sigma_space', kind: 'range', label: 'Spatial Ïƒ', min: 5, max: 80, step: 1, scale: 10, dec: 1, unit: 'sensels', show: ['bilateral'] },
    { key: 'notch_sensitivity', kind: 'range', label: 'Notch sensitivity', min: 20, max: 200, step: 1, scale: 10, dec: 1, unit: 'Ã— bg', show: ['fft_notch'],
      hint: 'Spectral peak / background ratio. Lower = more peaks are notched out.' },
  ] },
  { title: '2 Â· Despeckle (spike repair)', controls: [
    { key: 'temporal_filter_enabled', kind: 'check', label: 'Temporal anti-speckle (frame to frame)' },
    { key: 'temporal_spike_threshold', kind: 'range', label: 'Temporal threshold', min: 10, max: 80, step: 1, scale: 10, dec: 1, unit: 'Ïƒ', dependsOn: 'temporal_filter_enabled' },
    { key: 'spatial_filter_enabled', kind: 'check', label: 'Spatial MAD outlier filter (3Ã—3)' },
    { key: 'spatial_mad_threshold', kind: 'range', label: 'MAD threshold', min: 15, max: 100, step: 1, scale: 10, dec: 1, unit: 'Ã— MAD', dependsOn: 'spatial_filter_enabled' },
  ] },
  { title: '3 Â· Noise floor, patches & morphology', controls: [
    { key: 'min_pressure', kind: 'range', label: 'Noise floor', min: 0, max: 60, step: 1, dec: 0, unit: 'PSI',
      hint: 'Pressures below this value are treated as no contact.' },
    { key: 'patch_filter_enabled', kind: 'check', label: 'Remove tiny isolated patches' },
    { key: 'min_patch_area_mm2', kind: 'range', label: 'Minimum patch area', min: 0, max: 150, step: 1, dec: 0, unit: 'mmÂ²', dependsOn: 'patch_filter_enabled' },
    { key: 'patch_preserve_threshold_psi', kind: 'range', label: 'â€¦kept if peak above', min: 5, max: 100, step: 1, dec: 0, unit: 'PSI', dependsOn: 'patch_filter_enabled' },
    { key: 'min_component_px', kind: 'range', label: 'Min. contact component', min: 1, max: 100, step: 1, dec: 0, unit: 'sensels' },
    { key: 'morph_open', kind: 'range', label: 'Opening (remove specks)', min: 0, max: 7, step: 1, dec: 0, unit: 'px', offBelow: 2 },
    { key: 'morph_close', kind: 'range', label: 'Closing (bridge gaps)', min: 0, max: 7, step: 1, dec: 0, unit: 'px', offBelow: 2 },
    { key: 'keep_largest_only', kind: 'check', label: 'Keep only the largest contact patch' },
  ] },
  { title: '4 Â· View', controls: [
    { key: 'rotation_k', kind: 'select', num: true, label: 'Tyre orientation',
      options: [[1, 'Rolling bottom â†’ top (90Â° CCW)'], [0, 'As recorded (no rotation)'], [2, '180Â°'], [3, '90Â° CW']] },
    { key: 'show_edge', kind: 'check', client: true, label: 'Show exact bounding edge' },
  ] },
];

const FILTER_PRESETS = {
  off:      { filter_type: 'none', temporal_filter_enabled: false, spatial_filter_enabled: false, patch_filter_enabled: false },
  mild:     { filter_type: 'fft_butterworth_lp', fft_cutoff: 0.92, fft_filter_strength: 2 },
  balanced: {},
  strong:   { filter_type: 'fft_butterworth_lp', fft_cutoff: 0.45, fft_filter_strength: 3, spatial_mad_threshold: 3,
              min_patch_area_mm2: 30, morph_open: 3, morph_close: 3, keep_largest_only: true },
};

// â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
// STATE
// â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

const state = {
  jobId:               null,
  data:                null,
  frameIndex:          0,
  selectedFrameNumber: null,   // frame shown in the large "Best Contact Frame" panel
  viewCustom:          false,
  reportFrames:        [],     // frames the report is calculated from (1-3)
  reportCustom:        false,
  playing:             false,
  lastFrameTs:         0,
  speedMultiplier:     5,
  minPressure:         5,
  maxPressure:         220,
  speed:               0,
  position:            'FR',
  filters:             { ...FILTER_DEFAULTS },
  gridW:               256,
  gridH:               256,
  senselMm:            1.6,
};

// â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
// DOM REFS
// â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

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

const animCanvas    = $('anim-canvas');
const animOverlay   = $('anim-overlay');
const bestCanvas    = $('best-canvas');
const bestOverlay   = $('best-overlay');
const colorbarCvs   = $('colorbar-canvas');

const pressureMeta  = $('pressure-meta');
const speedMeta     = $('speed-meta');
const posMeta       = $('pos-meta');
const fftMeta       = $('fft-meta');

let dashboardBound  = false;
let inspectorBound  = false;

// â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
// NOISE-FILTER PANEL BUILDER (instantiated on BOTH pages)
// â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

const filterControls  = [];   // {spec, input, valueEl, row}
const filterStatusEls = [];
const filterInfoEls   = [];

function h(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls)  e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

function buildFilterPanel(container, compact) {
  if (!container) return;
  container.innerHTML = '';

  // Presets + status
  const top = h('div', 'filter-top');
  const presets = h('div', 'filter-presets');
  [['off', 'Off'], ['mild', 'Mild'], ['balanced', 'Balanced'], ['strong', 'Strong']].forEach(([key, label]) => {
    const b = h('button', 'ghost-btn small', label);
    b.type = 'button';
    b.title = key === 'balanced' ? 'Default settings' : `${label} preset`;
    b.addEventListener('click', () => applyFilterPreset(key));
    presets.appendChild(b);
  });
  const reset = h('button', 'ghost-btn small', 'Reset');
  reset.type = 'button';
  reset.title = 'Restore all filter defaults';
  reset.addEventListener('click', () => applyFilterPreset('balanced'));
  presets.appendChild(reset);
  top.appendChild(presets);

  const status = h('div', 'filter-status', compact ? 'Applied when you upload' : 'Live Â· applied to all frames');
  top.appendChild(status);
  filterStatusEls.push(status);
  container.appendChild(top);

  const info = h('div', 'filter-info');
  filterInfoEls.push(info);
  container.appendChild(info);

  FILTER_SECTIONS.forEach((sec, idx) => {
    const det = h('details', 'filter-section');
    det.open = compact ? idx === 0 : true;
    det.appendChild(h('summary', null, sec.title));
    const body = h('div', 'filter-section-body');

    sec.controls.forEach(spec => {
      const row = h('div', 'f-row f-' + spec.kind);
      let input, valueEl = null;

      if (spec.kind === 'check') {
        const lab = h('label', 'f-check-label');
        input = document.createElement('input');
        input.type = 'checkbox';
        lab.appendChild(input);
        lab.appendChild(h('span', null, spec.label));
        row.appendChild(lab);
        input.addEventListener('change', () => onFilterInput(spec, input));
      } else if (spec.kind === 'select') {
        row.appendChild(h('div', 'f-label', spec.label));
        input = document.createElement('select');
        input.className = 'select-control f-select';
        const opts = spec.key === 'filter_type'
          ? null : spec.options;
        if (opts === null) {
          FILTER_GROUPS.forEach(g => {
            const og = document.createElement('optgroup');
            og.label = g.group;
            g.items.forEach(([v, t]) => { const o = document.createElement('option'); o.value = v; o.textContent = t; og.appendChild(o); });
            input.appendChild(og);
          });
        } else {
          opts.forEach(([v, t]) => { const o = document.createElement('option'); o.value = v; o.textContent = t; input.appendChild(o); });
        }
        row.appendChild(input);
        input.addEventListener('change', () => onFilterInput(spec, input));
      } else {
        const head = h('div', 'f-label-row');
        head.appendChild(h('span', 'f-label', spec.label));
        valueEl = h('span', 'f-value', '');
        head.appendChild(valueEl);
        row.appendChild(head);
        input = document.createElement('input');
        input.type = 'range';
        input.className = 'slider-range f-range';
        input.min = spec.min; input.max = spec.max; input.step = spec.step;
        row.appendChild(input);
        input.addEventListener('input', () => onFilterInput(spec, input));
      }
      if (spec.hint) row.appendChild(h('div', 'f-hint', spec.hint));
      body.appendChild(row);
      filterControls.push({ spec, input, valueEl, row });
    });
    det.appendChild(body);
    container.appendChild(det);
  });
  syncFilterControls();
}

function filterValueText(spec, v) {
  if (spec.offBelow !== undefined && v < spec.offBelow) return 'off';
  const n = typeof v === 'number' ? v.toFixed(spec.dec ?? 0) : v;
  return spec.unit ? `${n} ${spec.unit}` : `${n}`;
}

function syncFilterControls() {
  const f = state.filters;
  for (const c of filterControls) {
    const { spec, input, valueEl, row } = c;
    const v = f[spec.key];
    if (spec.kind === 'check') {
      input.checked = !!v;
    } else if (spec.kind === 'select') {
      if (document.activeElement !== input) input.value = String(v);
    } else {
      const raw = Math.round(v * (spec.scale || 1));
      if (document.activeElement !== input || input.value === '') input.value = raw;
      if (valueEl) valueEl.textContent = filterValueText(spec, v);
    }
    const visible = !spec.show || spec.show.includes(f.filter_type);
    row.style.display = visible ? '' : 'none';
    row.classList.toggle('f-disabled', !!spec.dependsOn && !f[spec.dependsOn]);
  }
  const label = FILTER_LABELS[f.filter_type] || f.filter_type;
  filterInfoEls.forEach(e => { e.textContent = FILTER_INFO[f.filter_type] || ''; });
  const hint = $('landing-filter-hint');
  if (hint) hint.textContent = label;
}

function onFilterInput(spec, input) {
  let v;
  if (spec.kind === 'range')      v = parseFloat(input.value) / (spec.scale || 1);
  else if (spec.kind === 'check') v = input.checked;
  else                            v = spec.num ? Number(input.value) : input.value;
  state.filters[spec.key] = v;
  syncFilterControls();
  if (spec.client) { redrawEdges(); return; }
  scheduleRefilter();
}

function applyFilterPreset(name) {
  const keepView = { rotation_k: state.filters.rotation_k, show_edge: state.filters.show_edge };
  state.filters = { ...FILTER_DEFAULTS, ...keepView, ...(FILTER_PRESETS[name] || {}) };
  syncFilterControls();
  scheduleRefilter();
}

function setFilterStatus(text, busy) {
  filterStatusEls.forEach(e => {
    e.textContent = text;
    e.classList.toggle('busy', !!busy);
  });
}

// â”€â”€ Live refilter plumbing â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
let refilterTimer    = null;
let refilterSeq      = 0;
let refilterInflight = null;

function scheduleRefilter() {
  if (!state.jobId || !state.data) return;   // landing page: filters are sent with the upload
  setFilterStatus('Updating all framesâ€¦', true);
  clearTimeout(refilterTimer);
  refilterTimer = setTimeout(() => { refilterTimer = null; doRefilter(); }, 140);
}

function doRefilter() {
  const seq = ++refilterSeq;
  const jobId = state.jobId;
  const p = (async () => {
    try {
      const res  = await fetch(`/refilter/${jobId}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ filters: state.filters, seq }),
      });
      const json = await res.json();
      if (json.stale || seq !== refilterSeq || jobId !== state.jobId) return;
      if (json.error) { setFilterStatus('Filter error: ' + json.error, false); return; }
      decodeResult(json);
      applyResult(json, true);
      setFilterStatus('Live Â· applied to all frames', false);
    } catch (err) {
      console.error('refilter failed', err);
      setFilterStatus('Filter update failed', false);
    }
  })();
  refilterInflight = p;
  p.finally(() => { if (refilterInflight === p) refilterInflight = null; });
  return p;
}

async function flushRefilter() {
  if (refilterTimer) { clearTimeout(refilterTimer); refilterTimer = null; await doRefilter(); }
  else if (refilterInflight) { await refilterInflight; }
}

// â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
// RESULT DECODING
// â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

function b64ToBytes(s) {
  const bin = atob(s);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

function decodeResult(data) {
  if (data.frames_encoding === 'b64-u8') {
    for (const k of Object.keys(data.frames)) data.frames[k] = b64ToBytes(data.frames[k]);
    data.frames_encoding = 'u8';
  }
  const pd = data.pixel_data;
  if (pd && pd.encoding === 'b64') {
    for (const [name, dt] of Object.entries(pd._dtypes || {})) {
      const s = pd[name];
      if (!s) { pd[name] = null; continue; }
      const b = b64ToBytes(s);
      pd[name] = dt.endsWith('u2') ? new Uint16Array(b.buffer)
               : dt.endsWith('f4') ? new Float32Array(b.buffer) : b;
    }
    pd.encoding = 'typed';
  }
  return data;
}

// â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
// EVENT LISTENERS â€“ UPLOAD PAGE
// â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

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

// â”€â”€ Auto-detect metadata from filename â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
function autoDetectFileMetadata(file) {
  const fname = file.name;

  const speedMatch = fname.match(/(\d+(?:\.\d+)?)\s*(?:kmph|km\/h|kmh)/i);
  if (speedMatch && speedMatch[1]) {
    if (speedInput) speedInput.value = parseInt(speedMatch[1], 10);
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
          if (speedInput) speedInput.value = parseInt(headerSpeed[1], 10);
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

// â”€â”€ Preset handler â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
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
if (speedInput) speedInput.addEventListener('input', () => { state.speed = parseInt(speedInput.value) || 0; });

function updatePressureDisplay() {
  minVal.textContent = `${state.minPressure} PSI`;
  maxVal.textContent = `${state.maxPressure} PSI`;
}

// â”€â”€ Theme toggle â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
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
// â”€â”€ File operations â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
newFileBtn.addEventListener('click', resetToUpload);
exportBtn.addEventListener('click', exportResults);

// â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
// UPLOAD FLOW
// â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

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
  state.speed = speedInput ? (parseInt(speedInput.value, 10) || 0) : state.speed;

  const fd = new FormData();
  fd.append('file', file);
  fd.append('tyre_type', state.minPressure === 5 && state.maxPressure === 100 ? 'PCR' : 'TBR');
  fd.append('min_pressure', state.minPressure);
  fd.append('max_pressure', state.maxPressure);
  fd.append('speed', state.speed);
  fd.append('position', state.position);
  fd.append('filters', JSON.stringify(state.filters));

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
      setProgress(40, 'Processing', 'Analysing pressure dataâ€¦');
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
        setProgress(95, 'Loading', 'Rendering dashboardâ€¦');
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
  decodeResult(data);

  state.viewCustom   = false;
  state.reportCustom = false;
  state.frameIndex   = data.best_frame_index;
  applyResult(data, false);

  setProgress(100, 'Complete', `${data.frame_numbers.length} frames`);
  setTimeout(() => { showDashboard(data); setStatus('done', 'Ready'); }, 300);
}

/** Store a (re)computed result and refresh everything that depends on it. */
function applyResult(data, live) {
  state.data = data;
  if (data.speed !== undefined) state.speed = data.speed;
  if (data.position) state.position = data.position;
  state.gridW = data.grid_cols || state.gridW;
  state.gridH = data.grid_rows || state.gridH;
  if (data.sensel_w_mm && data.sensel_h_mm) state.senselMm = (data.sensel_w_mm + data.sensel_h_mm) / 2;

  const valid = new Set(data.frame_numbers);
  if (!live || !state.viewCustom || !valid.has(state.selectedFrameNumber)) {
    state.selectedFrameNumber = data.best_frame_number;
  }
  state.reportFrames = state.reportFrames.filter(f => valid.has(f));
  if (!state.reportCustom || state.reportFrames.length === 0 && !state.reportCustom) {
    state.reportFrames = [data.best_frame_number];
  }
  if (live) {
    state.frameIndex = Math.min(state.frameIndex, data.frame_numbers.length - 1);
    refreshDashboard(false);
  }
}

// â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
// DASHBOARD
// â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

function showDashboard(data) {
  uploadPage.classList.remove('active');
  dashPage.classList.add('active');

  const { frame_numbers, vmax, vmin } = data;
  console.log('[XSN-DIAG] showDashboard() frames:', frame_numbers.length, 'best:', data.best_frame_number,
              'grid:', data.grid_rows + 'x' + data.grid_cols, 'filter:', data.filter_type);

  drawColorbar(colorbarCvs, vmax);
  $('vmin-label').textContent = vmin;
  $('vmid-label').textContent = Math.round((vmin + vmax) / 2);
  $('vmax-label').textContent = vmax;

  animOverlay.classList.remove('hidden');
  bindDashboardOnce();
  refreshDashboard(true);
  openFilterDrawer(true);
}

function refreshDashboard(initial) {
  const data = state.data;
  if (!data) return;
  const { frame_numbers, vmin, vmax, tyre_type, speed, position } = data;
  const isXsn = !!data.is_xsn;
  void isXsn;
  const filterLabel = FILTER_LABELS[data.filter_type] || data.filter_type;

  tyreTypeBadge.textContent = tyre_type;
  if (positionBadge) positionBadge.textContent = position || state.position;
  if (fftBadge)      fftBadge.textContent = filterLabel;
  if (pressureMeta)  pressureMeta.textContent = `${vmin}â€“${vmax} PSI`;
  if (speedMeta)     speedMeta.textContent = `${speed} km/h`;
  if (posMeta)       posMeta.textContent = position || state.position;
  if (fftMeta)       fftMeta.textContent = filterLabel;

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
  const psiUnitLabel = $('psi-unit-label');
  if (psiUnitLabel) psiUnitLabel.textContent = 'PSI';

  // Keep both canvases at the true (rotated) aspect ratio of the sensor grid
  const ar = `${state.gridW} / ${state.gridH}`;
  [$('anim-canvas-wrap'), $('best-canvas-wrap')].forEach(w => { if (w) w.style.aspectRatio = ar; });

  const slider = $('frame-slider');
  slider.min = 0;
  slider.max = frame_numbers.length - 1;
  if (initial) state.frameIndex = data.best_frame_index;
  slider.value = state.frameIndex;

  const bfi = $('best-frame-input');
  if (bfi) {
    bfi.min = Math.min(...frame_numbers);
    bfi.max = Math.max(...frame_numbers);
    bfi.value = state.selectedFrameNumber;
  }

  renderBestFrame();
  updateAnimFrame(state.frameIndex, false);
  renderReportPanel();
  populateFrameDiagnostics();
  populateCalibrationAudit();
}

function bindDashboardOnce() {
  if (dashboardBound) return;
  dashboardBound = true;

  $('btn-play').addEventListener('click', togglePlay);
  $('frame-slider').addEventListener('input', () => {
    state.frameIndex = parseInt($('frame-slider').value, 10);
    updateAnimFrame(state.frameIndex, false);
  });
  $('speed-slider').addEventListener('input', () => {
    state.speedMultiplier = parseInt($('speed-slider').value, 10);
    $('speed-label').textContent = `${state.speedMultiplier}Ã—`;
  });
  const bfb = $('best-frame-btn');
  if (bfb) bfb.addEventListener('click', selectCustomFrame);

  const diagBtn = $('diag-csv-btn');
  if (diagBtn) diagBtn.addEventListener('click', exportDiagnosticsCSV);

  // report panel
  $('report-add-btn').addEventListener('click', () => {
    const v = parseInt($('report-frame-input').value, 10);
    if (Number.isNaN(v)) { alert('Type a frame number first.'); return; }
    addReportFrame(v);
  });
  $('report-add-current-btn').addEventListener('click', () => {
    addReportFrame(state.data.frame_numbers[state.frameIndex]);
  });
  $('report-reset-btn').addEventListener('click', () => {
    state.reportCustom = false;
    state.reportFrames = [state.data.best_frame_number];
    renderReportPanel();
  });
  $('report-export-btn').addEventListener('click', exportResults);

  // filter drawer
  $('filter-drawer-toggle').addEventListener('click', () => openFilterDrawer(!document.body.classList.contains('drawer-open')));
  $('filter-drawer-close').addEventListener('click', () => openFilterDrawer(false));

  initPixelInspector();

  // Redraw the (display-resolution) edge whenever the canvases are resized
  if (window.ResizeObserver) {
    const ro = new ResizeObserver(() => redrawEdges());
    [$('anim-canvas-wrap'), $('best-canvas-wrap')].forEach(w => w && ro.observe(w));
  }
  window.addEventListener('resize', redrawEdges);
}

function openFilterDrawer(open) {
  document.body.classList.toggle('drawer-open', !!open && dashPage.classList.contains('active'));
  setTimeout(redrawEdges, 260);
}

// â”€â”€ Exact bounding edge (per frame) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
function drawEdge(overlay, fn) {
  if (!state.data) return;
  const contours = (state.filters.show_edge && state.data.outlines) ? state.data.outlines[String(fn)] : [];
  drawTyreOutline(overlay, contours || [], state.gridW, state.gridH);
}

function redrawEdges() {
  if (!state.data) return;
  drawEdge(bestOverlay, state.selectedFrameNumber ?? state.data.best_frame_number);
  drawEdge(animOverlay, state.data.frame_numbers[state.frameIndex]);
}

function renderBestFrame() {
  const { frames, metrics, frame_numbers } = state.data;
  const frameNum = state.selectedFrameNumber ?? state.data.best_frame_number;
  const frameIdx = frame_numbers.indexOf(frameNum);
  const m = metrics[frameIdx >= 0 ? frameIdx : state.data.best_frame_index];
  if (m) updateMetrics(m);

  drawHeatmap(bestCanvas, frames[String(frameNum)], state.gridW, state.gridH);
  drawEdge(bestOverlay, frameNum);

  $('p-width').textContent  = m?.bbox_width_mm  ?? 'â€”';
  $('p-length').textContent = m?.bbox_length_mm ?? 'â€”';
  $('best-frame-tag').textContent = frameNum === state.data.best_frame_number
    ? `Best contact Â· Frame ${frameNum}` : `Frame ${frameNum}`;
}

function selectCustomFrame() {
  const requested = Number($('best-frame-input').value);
  if (!state.data || Number.isNaN(requested)) return;
  const idx = state.data.frame_numbers.indexOf(requested);
  if (idx === -1) { alert(`Frame ${requested} not found.`); return; }
  state.selectedFrameNumber = requested;
  state.viewCustom = (requested !== state.data.best_frame_number);
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
  const { frame_numbers, frames, metrics } = state.data;
  const fn = frame_numbers[idx];

  drawHeatmap(animCanvas, frames[String(fn)], state.gridW, state.gridH);
  drawEdge(animOverlay, fn);          // the edge is recomputed per frame, so it follows the tyre

  if (metrics && metrics[idx]) updateMetrics(metrics[idx]);

  $('anim-frame-tag').textContent = `Frame ${fn}`;
  $('slider-label').textContent   = `Frame ${fn} (${idx + 1}/${frame_numbers.length})`;
  if (updateSlider) $('frame-slider').value = idx;
}

function updateMetrics(m) {
  const setEl = (id, val) => {
    const el = $(id);
    if (el) el.textContent = (val !== undefined && val !== null) ? val : 'â€”';
  };

  setEl('m-area',     m.contact_area_mm2 ? m.contact_area_mm2.toLocaleString() : 'â€”');
  setEl('m-ratio',    m.contact_ratio_pct);
  setEl('m-fp-w',     m.fp_width_mm);
  setEl('m-fp-l',     m.fp_length_mm);
  setEl('m-s1',       m.outer_shoulder_s1_mm);
  setEl('m-s2',       m.inner_shoulder_s2_mm);
  setEl('m-skew',     m.shoulder_skewness_pct);
  setEl('m-sq-s1',  m.squareness_s1_pct);
  setEl('m-sq-s2',  m.squareness_s2_pct);
  setEl('m-sq-avg',  m.squareness_avg_pct);
  setEl('m-ls-ratio', m.land_sea_ratio);
  setEl('m-avg',      m.contact_pressure_psi || m.avg_pressure_psi);
  setEl('m-max-l',    m.maximum_length_mm);
  setEl('m-max-w',    m.maximum_width_mm);
  setEl('m-frame',    m.frame_number);
  setEl('m-peak',     m.peak_pressure_psi);
  setEl('m-load',     m.estimated_load_kg ? m.estimated_load_kg.toLocaleString() : 'â€”');

  const covBar = $('coverage-bar');
  if (covBar) covBar.style.width = `${Math.min(m.contact_pct || 0, 100)}%`;
}

// â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
// REPORT FRAME SELECTION  (nothing is generated automatically)
// â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

function addReportFrame(fn) {
  if (!state.data) return;
  if (!state.data.frame_numbers.includes(fn)) { alert(`Frame ${fn} not found.`); return; }
  if (state.reportFrames.includes(fn)) return;
  if (state.reportFrames.length >= 3) { alert('A report can use at most 3 frames. Remove one first.'); return; }
  if (!state.reportCustom) state.reportFrames = [];   // first manual pick replaces the default best frame
  state.reportCustom = true;
  state.reportFrames.push(fn);
  renderReportPanel();
}

function removeReportFrame(fn) {
  state.reportFrames = state.reportFrames.filter(f => f !== fn);
  state.reportCustom = true;
  renderReportPanel();
}

function renderReportPanel() {
  const data = state.data;
  if (!data) return;
  const chips = $('report-chips');
  chips.innerHTML = '';

  state.reportFrames.forEach(fn => {
    const chip = h('div', 'report-chip');
    const cv = document.createElement('canvas');
    drawHeatmap(cv, data.frames[String(fn)], state.gridW, state.gridH);
    cv.style.aspectRatio = `${state.gridW} / ${state.gridH}`;
    cv.title = 'Click to view this frame in the large panel';
    cv.addEventListener('click', () => {
      state.selectedFrameNumber = fn;
      state.viewCustom = fn !== data.best_frame_number;
      $('best-frame-input').value = fn;
      renderBestFrame();
    });
    chip.appendChild(cv);
    const cap = h('div', 'report-chip-cap');
    cap.appendChild(h('span', null, fn === data.best_frame_number ? `Frame ${fn} Â· best` : `Frame ${fn}`));
    const rm = h('button', 'chip-remove', 'Ã—');
    rm.type = 'button';
    rm.title = 'Remove from report';
    rm.addEventListener('click', () => removeReportFrame(fn));
    cap.appendChild(rm);
    chip.appendChild(cap);
    chips.appendChild(chip);
  });
  if (state.reportFrames.length === 0) {
    chips.appendChild(h('div', 'report-empty', 'No frame selected â€” add at least one frame to generate the report.'));
  }

  $('report-count').textContent = `${state.reportFrames.length} / 3 frames`;
  $('report-export-btn').disabled = state.reportFrames.length === 0;
  exportBtn.disabled = state.reportFrames.length === 0;

  // Mean preview of the headline numbers the report will contain
  const rows = state.reportFrames
    .map(fn => data.metrics[data.frame_numbers.indexOf(fn)]).filter(Boolean);
  const mean = k => rows.length ? rows.reduce((a, r) => a + (r[k] || 0), 0) / rows.length : null;
  const fmt = (v, d = 1) => v === null ? 'â€”' : v.toLocaleString(undefined, { maximumFractionDigits: d });
  const sum = $('report-summary');
  sum.innerHTML = '';
  if (rows.length) {
    sum.appendChild(h('div', 'report-summary-title',
      rows.length > 1 ? `Mean of ${rows.length} selected frames` : 'Selected frame values'));
    [['Contact area', fmt(mean('contact_area_mm2'), 0), 'mmÂ²'], ['FP width', fmt(mean('fp_width_mm')), 'mm'],
     ['FP length', fmt(mean('fp_length_mm')), 'mm'], ['Squareness avg', fmt(mean('squareness_avg_pct')), '%'],
     ['Contact pressure', fmt(mean('contact_pressure_psi')), 'PSI'], ['Peak', fmt(mean('peak_pressure_psi')), 'PSI']
    ].forEach(([l, v, u]) => {
      const c = h('div', 'report-stat');
      c.appendChild(h('div', 'report-stat-l', l));
      c.appendChild(h('div', 'report-stat-v', `${v} ${u}`));
      sum.appendChild(c);
    });
  }
}

// â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
// EXPORT FUNCTIONALITY
// â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

async function exportResults() {
  if (!state.data || !state.jobId) {
    alert('No analysis data available to export.');
    return;
  }
  if (state.reportFrames.length === 0) {
    alert('Select at least one frame (up to 3) for the report first.');
    $('report-panel').scrollIntoView({ behavior: 'smooth', block: 'center' });
    return;
  }
  const frames = state.reportFrames.slice();
  if (!confirm(`Generate the Footprint Analysis Report (Dynamic Model) from ${frames.length} frame(s): ${frames.join(', ')}?`)) return;

  try {
    await flushRefilter();     // make sure the server uses the filter settings currently on screen
    const res = await fetch(`/export/${state.jobId}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ speed: state.speed, frames }),
    });
    if (!res.ok) {
      let msg = res.statusText;
      try { msg = (await res.json()).error || msg; } catch { /* ignore */ }
      throw new Error(msg);
    }
    const blob = await res.blob();
    const url  = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href  = url;
    link.download = `Apollo_Footprint_Analysis_Report_${state.speed}kmph.pdf`;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    URL.revokeObjectURL(url);
  } catch (err) {
    console.error('Export error:', err);
    alert(`Export failed: ${err.message}`);
  }
}

// â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
// UI HELPERS
// â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

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
  clearTimeout(refilterTimer);
  refilterTimer = null;
  refilterSeq++;
  $('play-icon').classList.remove('hidden');
  $('pause-icon').classList.add('hidden');
  $('btn-play').classList.remove('playing');

  state.data                = null;
  state.jobId               = null;
  state.frameIndex          = 0;
  state.selectedFrameNumber = null;
  state.viewCustom          = false;
  state.reportFrames        = [];
  state.reportCustom        = false;
  state.gridW               = 256;
  state.gridH               = 256;
  state.senselMm            = 1.6;

  fileInput.value = '';
  progressWrap.classList.add('hidden');
  setStatus('idle', 'Idle');
  dashPage.classList.remove('active');
  uploadPage.classList.add('active');
  document.body.classList.remove('drawer-open');
  setFilterStatus('Applied when you upload', false);

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

// PIXEL INSPECTOR
// â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

function initPixelInspector() {
  const bestCanvasWrap = $('best-canvas-wrap');
  if (bestCanvasWrap) {
    bestCanvasWrap.style.cursor = 'crosshair';
    bestCanvasWrap.addEventListener('click', (e) => {
      if (!state.data || !state.data.pixel_data) return;
      const rect = bestCanvasWrap.getBoundingClientRect();
      const x = e.clientX - rect.left;
      const y = e.clientY - rect.top;
      const scaleX = state.gridW / rect.width;
      const scaleY = state.gridH / rect.height;
      // Direct mapping â€” no flipud inversion needed
      const canvasCol = Math.floor(x * scaleX);
      const canvasRow = Math.floor(y * scaleY);
      const dataRow = canvasRow;
      const dataCol = canvasCol;
      
      $('inspect-row').value = dataRow;
      $('inspect-col').value = dataCol;
      if (state.data.best_frame_number !== undefined) {
        $('inspect-frame').value = state.data.best_frame_number;
      }
      inspectPixel(dataRow, dataCol);
    });
  }
  
  const inspectBtn = $('inspect-btn');
  if (inspectBtn) {
    inspectBtn.addEventListener('click', () => {
      const row = parseInt($('inspect-row').value, 10);
      const col = parseInt($('inspect-col').value, 10);
      if (!isNaN(row) && !isNaN(col)) {
        inspectPixel(row, col);
      }
    });
  }
}

function inspectPixel(row, col) {
  if (!state.data || !state.data.pixel_data) {
    $('inspector-status').textContent = 'No pixel data available';
    return;
  }
  
  const pd = state.data.pixel_data;
  const rows = pd.grid_rows;
  const cols = pd.grid_cols;
  
  if (row < 0 || row >= rows || col < 0 || col >= cols) {
    $('inspector-status').textContent = `Out of bounds: R${row} C${col}`;
    return;
  }
  
  const idx = row * cols + col;
  
  // Extract values from pixel_data arrays
  const rawVal = pd.best_frame_raw ? pd.best_frame_raw[idx] : 'â€”';
  const calPsi = pd.best_frame_calibrated ? pd.best_frame_calibrated[idx] : 'â€”';
  const filteredPsi = pd.best_frame_filtered ? pd.best_frame_filtered[idx] : 'â€”';
  const finalPsi = pd.best_frame_final ? pd.best_frame_final[idx] : 'â€”';
  const isInvalid = pd.invalid_mask ? pd.invalid_mask[idx] : false;
  const isSaturated = pd.saturation_mask ? pd.saturation_mask[idx] : false;
  const isSpike = pd.spike_mask ? pd.spike_mask[idx] : false;
  const isContact = pd.contact_mask ? pd.contact_mask[idx] : false;
  
  $('inspector-status').textContent = `R${row} C${col} â€” Frame ${state.data.best_frame_number}`;
  
  // Update display
  $('px-raw').textContent = rawVal;
  $('px-dtype').textContent = 'uint16';
  $('px-invalid').textContent = isInvalid ? 'âš  YES' : 'No';
  $('px-invalid').style.color = isInvalid ? '#ef4444' : '';
  $('px-saturated').textContent = isSaturated ? 'âš  SATURATED' : 'No';
  $('px-saturated').style.color = isSaturated ? '#ef4444' : '';
  
  $('px-cal-method').textContent = pd.calibration_method || 'â€”';
  $('px-cal-range').textContent = `${pd.pressure_range || 'â€”'} (0â€“${pd.pressure_ceiling || 'â€”'} PSI)`;
  $('px-cal-psi').textContent = typeof calPsi === 'number' ? calPsi.toFixed(2) + ' PSI' : 'â€”';
  $('px-confidence').textContent = pd.calibration_confidence || 'â€”';
  
  $('px-temporal').textContent = 'N/A (best frame)';
  $('px-spatial').textContent = isSpike ? 'âš  Spike detected â†’ replaced' : 'No';
  $('px-spatial').style.color = isSpike ? '#f59e0b' : '';
  $('px-fft').textContent = typeof filteredPsi === 'number' ? filteredPsi.toFixed(2) + ' PSI' : 'â€”';
  $('px-patch').textContent = isContact ? 'Preserved (contact)' : 'Removed (no contact)';
  
  $('px-final').textContent = typeof finalPsi === 'number' ? finalPsi.toFixed(2) + ' PSI' : 'â€”';
  $('px-contact').textContent = isContact ? 'âœ“ Contact' : 'âœ— No contact';
  $('px-contact').style.color = isContact ? '#10b981' : '#ef4444';
  
  // 3x3 neighbourhood
  const nbData = pd.best_frame_final || pd.best_frame_filtered || pd.best_frame_calibrated;
  if (nbData) {
    for (let dr = -1; dr <= 1; dr++) {
      for (let dc = -1; dc <= 1; dc++) {
        const nr = row + dr;
        const nc = col + dc;
        const cellId = `nb-${dr+1}${dc+1}`;
        const cell = $(cellId);
        if (cell) {
          if (nr >= 0 && nr < rows && nc >= 0 && nc < cols) {
            const nIdx = nr * cols + nc;
            const val = nbData[nIdx];
            cell.textContent = typeof val === 'number' ? val.toFixed(1) : 'â€”';
          } else {
            cell.textContent = 'â€”';
          }
        }
      }
    }
    // Row/col headers
    $('nb-r0').textContent = `R${row-1}`;
    $('nb-r1').textContent = `R${row}`;
    $('nb-r2').textContent = `R${row+1}`;
    $('nb-c0').textContent = `C${col-1}`;
    $('nb-c1').textContent = `C${col}`;
    $('nb-c2').textContent = `C${col+1}`;
  }
  
  // Show the result panel
  $('inspector-result').classList.remove('hidden');
}

// â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
// FRAME DIAGNOSTICS
// â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

function populateFrameDiagnostics() {
  const diag = state.data?.frame_diagnostics;
  const tbody = $('diag-tbody');
  if (!tbody || !diag || !diag.length) return;
  
  tbody.innerHTML = '';
  for (const d of diag) {
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td>${d.frame_index ?? 'â€”'}</td>
      <td>${d.raw_min != null ? d.raw_min.toFixed(0) : 'â€”'}</td>
      <td>${d.raw_max != null ? d.raw_max.toFixed(0) : 'â€”'}</td>
      <td>${d.calibrated_min != null ? d.calibrated_min.toFixed(1) : 'â€”'}</td>
      <td>${d.calibrated_max != null ? d.calibrated_max.toFixed(1) : 'â€”'}</td>
      <td>${d.calibrated_mean != null ? d.calibrated_mean.toFixed(1) : 'â€”'}</td>
      <td>${d.calibrated_p95 != null ? d.calibrated_p95.toFixed(1) : 'â€”'}</td>
      <td>${d.active_sensels ?? 'â€”'}</td>
      <td>${d.contact_area_mm2 != null ? d.contact_area_mm2.toFixed(0) : 'â€”'}</td>
      <td>${d.invalid_count ?? 'â€”'}</td>
      <td>${d.saturation_count ?? 'â€”'}</td>
      <td>${d.spatial_spike_count ?? 'â€”'}</td>
      <td>${d.temporal_spike_count ?? 'â€”'}</td>
    `;
    tbody.appendChild(tr);
  }
}

function exportDiagnosticsCSV() {
  if (!state.data || !state.jobId) return;
  
  const diag = state.data.frame_diagnostics;
  if (!diag || !diag.length) {
    alert('No diagnostics data available.');
    return;
  }
  
  // Build CSV locally from the data we already have
  const headers = ['Frame', 'Raw Min', 'Raw Max', 'Cal. Min', 'Cal. Max', 'Cal. Mean', 'Cal. P95', 'Cal. P98', 'Active Sensels', 'Area mm2', 'Load N', 'Invalid', 'Saturated', 'Spatial Spikes', 'Temporal Spikes'];
  let csv = headers.join(',') + '\n';
  for (const d of diag) {
    csv += [
      d.frame_index, d.raw_min?.toFixed(0), d.raw_max?.toFixed(0),
      d.calibrated_min?.toFixed(2), d.calibrated_max?.toFixed(2), d.calibrated_mean?.toFixed(2),
      d.calibrated_p95?.toFixed(2), d.calibrated_p98?.toFixed(2),
      d.active_sensels, d.contact_area_mm2?.toFixed(1), d.total_load_n?.toFixed(1),
      d.invalid_count, d.saturation_count, d.spatial_spike_count, d.temporal_spike_count
    ].join(',') + '\n';
  }
  
  const blob = new Blob([csv], { type: 'text/csv' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `frame_diagnostics_${state.data.source_file || 'export'}.csv`;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

// â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
// CALIBRATION AUDIT
// â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

function populateCalibrationAudit() {
  const audit = state.data?.calibration_audit;
  if (!audit) return;
  
  const setAudit = (id, val) => {
    const el = $(id);
    if (el) el.textContent = val ?? 'â€”';
  };
  
  setAudit('audit-sensor', audit.sensor);
  setAudit('audit-vehicle', audit.vehicle_type);
  setAudit('audit-range', audit.pressure_range);
  setAudit('audit-source', audit.calibration_source);
  setAudit('audit-method', audit.calibration_method);
  setAudit('audit-raw-domain', audit.raw_domain);
  setAudit('audit-psi-domain', audit.psi_domain);
  setAudit('audit-scale', audit.scale);
  setAudit('audit-offset', audit.offset);
  setAudit('audit-saturated', audit.saturated_values);
  setAudit('audit-invalid', audit.invalid_values);
  setAudit('audit-spikes', audit.repaired_spikes);
  setAudit('audit-baseline', audit.baseline_frame != null ? `Frame ${audit.baseline_frame}` : 'None');
  setAudit('audit-conf-val', audit.calibration_confidence);
  
  const confBadge = $('audit-confidence');
  if (confBadge) {
    confBadge.textContent = audit.calibration_confidence || 'â€”';
    confBadge.setAttribute('data-level', audit.calibration_confidence || '');
  }
}

// â”€â”€ Initialise â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
updatePressureDisplay();
buildFilterPanel($('filter-panel-landing'), true);
buildFilterPanel($('filter-panel-dash'), false);

