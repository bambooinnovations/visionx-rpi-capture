'use strict';

const API = `/api/cameras`;

let previewMode = 'photo'; // 'photo' | 'live'
let liveTimer = null;
let _draftCount = 0;   // settings that differ from production, per the server
let _pending = false;  // a local change not yet acknowledged by the server

// ── API helpers ───────────────────────────────────────────────────────

async function apiFetch(path, opts = {}) {
  const res = await fetch(path, opts);
  const json = await res.json().catch(() => ({}));
  if (!res.ok && res.status !== 207) throw new Error(json.error || `HTTP ${res.status}`);
  return json;
}

// ── UI helpers ────────────────────────────────────────────────────────

function showError(msg) {
  document.getElementById('error-text').textContent = msg;
  document.getElementById('error-banner').classList.remove('hidden');
}
function clearError() {
  document.getElementById('error-banner').classList.add('hidden');
}

let _successTimer = null;
function showSuccess(msg = 'Settings saved') {
  const el = document.getElementById('success-toast');
  el.textContent = msg;
  el.classList.remove('hidden', 'fading');
  clearTimeout(_successTimer);
  _successTimer = setTimeout(() => {
    el.classList.add('fading');
    setTimeout(() => el.classList.add('hidden'), 400);
  }, 2000);
}
// ── Draft state ───────────────────────────────────────────────────────
// Every change is applied to the camera as a draft (preview + snapshots only);
// real captures keep the production settings until "Save to production".

const DRAFT_LABELS = {
  ae_enabled: 'Auto Exposure', ae_target: 'AE target', exposure_us: 'Exposure',
  auto_gain: 'Auto Gain', analog_gain: 'Analog gain',
  r_gain: 'Red gain', g_gain: 'Green gain', b_gain: 'Blue gain',
  sharpness: 'Sharpness', gamma: 'Gamma', contrast: 'Contrast', saturation: 'Saturation',
  noise_filter: 'Noise filter', correct_dead_pixel: 'Dead pixel correction',
  inverse: 'Invert image', anti_flick: 'Anti-flicker', light_frequency: 'Light frequency',
  frame_speed: 'Frame speed', rotation: 'Rotation', h_mirror: 'Horizontal mirror',
  v_mirror: 'Vertical mirror', mono_enabled: 'Monochrome',
};

function formatDraftValue(key, v) {
  if (typeof v === 'boolean') return v ? 'on' : 'off';
  if (key === 'exposure_us') return `${formatExposure(v / 1000)} ms`;
  if (key === 'analog_gain') return `${gainRawToX(v)}×`;
  if (key === 'rotation') return `${v * 90}°`;
  if (key === 'light_frequency') return v ? '60 Hz' : '50 Hz';
  return String(v);
}

function updateDraftButtons() {
  const hasDraft = _draftCount > 0 || _pending;
  document.getElementById('btn-save').disabled = !hasDraft;
  document.getElementById('btn-discard').disabled = !hasDraft;
}

// A control changed; the debounced apply will report the real draft state.
function markDirty() {
  _pending = true;
  updateDraftButtons();
}

function renderDraft(changes) {
  _pending = false;
  const entries = Object.entries(changes || {});
  _draftCount = entries.length;
  const badge = document.getElementById('draft-badge');
  badge.textContent = `Draft: ${_draftCount} change${_draftCount === 1 ? '' : 's'}`;
  badge.classList.toggle('hidden', _draftCount === 0);
  if (_draftCount === 0) toggleDraftList(false);

  const body = document.getElementById('draft-list-items');
  body.replaceChildren();
  for (const [key, {production, draft}] of entries) {
    const tr = document.createElement('tr');
    for (const text of [DRAFT_LABELS[key] || key, formatDraftValue(key, production), formatDraftValue(key, draft)]) {
      const td = document.createElement('td');
      td.textContent = text;
      tr.append(td);
    }
    body.append(tr);
  }
  updateDraftButtons();
}

function toggleDraftList(open) {
  const list = document.getElementById('draft-list');
  const show = open ?? list.classList.contains('hidden');
  list.classList.toggle('hidden', !show);
  document.getElementById('draft-badge').setAttribute('aria-expanded', String(show));
}

function setSlider(id, value, min, max, valueId, fmt) {
  const el = document.getElementById(id);
  el.min = min;
  el.max = max;
  el.value = value;
  if (valueId) {
    const box = document.getElementById(valueId);
    box.min = min;
    box.max = max;
    box.value = fmt ? fmt(value) : value;
  }
}

// The exposure UI works in milliseconds; the API stays in µs. Dragging the
// slider and the − / + buttons move in whole milliseconds, but the value itself
// keeps µs precision so a typed, loaded or snapshot-copied exposure (e.g.
// 12.48 ms) is applied exactly instead of being rounded.
// Slider position is 0..1000 on a quadratic curve so short exposures (where
// most tuning happens) get much finer control than a linear 1 ms/px scale.
const EXPOSURE_POS_MAX = 1000;
let exposureMinMs = 1;
const EXPOSURE_MAX_MS = 2000; // UI cap, regardless of what the camera reports
let exposureMaxMs = EXPOSURE_MAX_MS;
let exposureMs = 30;

function formatExposure(ms) {
  return parseFloat(ms.toFixed(3));
}
function exposurePosToMs(pos) {
  const t = pos / EXPOSURE_POS_MAX;
  return Math.round(exposureMinMs + (exposureMaxMs - exposureMinMs) * t * t);
}
function exposureMsToPos(ms) {
  const span = Math.max(1, exposureMaxMs - exposureMinMs);
  const t = Math.sqrt(Math.min(Math.max(ms - exposureMinMs, 0), span) / span);
  return Math.round(t * EXPOSURE_POS_MAX);
}
function getExposureMs() {
  return exposureMs;
}
function setExposureMs(ms) {
  exposureMs = formatExposure(Math.min(Math.max(ms, exposureMinMs), exposureMaxMs));
  document.getElementById('exposure-us').value = exposureMsToPos(exposureMs);
  document.getElementById('exposure-value').value = exposureMs;
  return exposureMs;
}

function updateExposureWarning(us) {
  const warn = document.getElementById('exposure-warning');
  if (!warn) return;
  if (us >= 500_000) {
    warn.textContent = `Preview will update ~${(1000 / (us / 1000)).toFixed(1)} fps — normal at long exposures`;
    warn.classList.remove('hidden');
  } else if (us >= 100_000) {
    warn.textContent = 'Long exposure — preview may be sluggish';
    warn.classList.remove('hidden');
  } else {
    warn.classList.add('hidden');
  }
}

// Analog gain: the slider moves in raw SDK units (one tick = one hardware
// step), the value box shows the multiplier. Per the MindVision spec the
// multiplier is raw * sExposeDesc.fAnalogGainStep.
let gainStep = 0.125;

function gainRawToX(raw) {
  return parseFloat((raw * gainStep).toFixed(3));
}
function gainXToRaw(x) {
  return Math.round(x / gainStep);
}

// ── Capture-profile scope messaging ───────────────────────────────────

let _captureOnly = true; // server: manual exposure applies to stills only

function updateAutoRows() {
  const ae = document.getElementById('ae-enabled').checked;
  const autoGain = document.getElementById('auto-gain').checked;
  document.getElementById('manual-exposure-row').classList.toggle('hidden', ae);
  document.getElementById('manual-gain-row').classList.toggle('hidden', autoGain);
  updateExposureScope(ae);
}

// Manual exposure is capture-only while other live streams run on auto.
function updateExposureScope(aeEnabled) {
  const badge = document.getElementById('exposure-scope-badge');
  const stillsOnly = _captureOnly && !aeEnabled;
  badge.textContent = stillsOnly ? 'Capture only' : 'Stream + capture';
  badge.classList.toggle('scope-capture', stillsOnly);
  badge.classList.toggle('scope-both', !stillsOnly);
}

// ── Populate controls from a settings object ──────────────────────────

function populateUI(s) {
  // Exposure
  _captureOnly = s.manual_exposure_capture_only !== false;
  document.getElementById('ae-enabled').checked = s.ae_enabled;
  document.getElementById('auto-gain').checked = s.auto_gain !== false;
  updateAutoRows();
  document.getElementById('preview-ae-warning').classList.toggle('hidden', !s.stream_auto_exposure);

  exposureMinMs = Math.max(1, Math.ceil((s.exposure_min_us || 1000) / 1000));
  exposureMaxMs = EXPOSURE_MAX_MS;
  const exposureBox = document.getElementById('exposure-value');
  exposureBox.min = exposureMinMs;
  exposureBox.max = exposureMaxMs;
  setExposureMs(s.exposure_us / 1000);
  updateExposureWarning(s.exposure_us);

  setSlider('ae-target', s.ae_target, 0, 255, 'ae-target-value');

  // Analog gain
  gainStep = s.analog_gain_step > 0 ? s.analog_gain_step : 0.125;
  setSlider('analog-gain', s.analog_gain,
    s.analog_gain_min || 16, s.analog_gain_max || 128,
    'analog-gain-value', gainRawToX);
  const gainBox = document.getElementById('analog-gain-value');
  gainBox.min = gainRawToX(s.analog_gain_min || 16);
  gainBox.max = gainRawToX(s.analog_gain_max || 128);
  gainBox.step = gainStep;

  setSlider('r-gain', s.r_gain,
    s.r_gain_min ?? 0, s.r_gain_max ?? 400, 'r-gain-value');

  setSlider('g-gain', s.g_gain,
    s.g_gain_min ?? 0, s.g_gain_max ?? 400, 'g-gain-value');

  setSlider('b-gain', s.b_gain,
    s.b_gain_min ?? 0, s.b_gain_max ?? 400, 'b-gain-value');

  // Image processing
  setSlider('sharpness', s.sharpness,
    s.sharpness_min ?? 0, s.sharpness_max ?? 100, 'sharpness-value');

  setSlider('gamma', s.gamma,
    s.gamma_min ?? 0, s.gamma_max ?? 250, 'gamma-value');

  setSlider('contrast', s.contrast,
    s.contrast_min ?? 0, s.contrast_max ?? 200, 'contrast-value');

  setSlider('saturation', s.saturation,
    s.saturation_min ?? 0, s.saturation_max ?? 200, 'saturation-value');

  document.getElementById('noise-filter').checked = s.noise_filter;
  document.getElementById('dead-pixel-correction').checked = s.correct_dead_pixel;
  document.getElementById('invert-image').checked = s.inverse;

  // Advanced exposure
  document.getElementById('anti-flick').checked = s.anti_flick;
  document.getElementById('light-frequency').value = String(s.light_frequency ?? 0);
  setSlider('frame-speed', s.frame_speed, 0, s.frame_speed_max ?? 2, 'frame-speed-value');

  // Rotation
  document.querySelectorAll('#rotation-group .btn-seg').forEach(btn => {
    btn.classList.toggle('active', parseInt(btn.dataset.rotation) === s.rotation);
  });

  // Mirrors
  document.getElementById('h-mirror').checked = s.h_mirror;
  document.getElementById('v-mirror').checked = s.v_mirror;

  // Monochrome — hidden entirely on a hardware-mono sensor (nothing to force)
  document.getElementById('mono-row').classList.toggle('hidden', !!s.mono_sensor);
  document.getElementById('mono-enabled').checked = !!s.mono_enabled;
  updateMonoWbGate(s.mono_enabled);
}

let _stitchWbLocked = false;

function updateMonoWbGate(monoEnabled) {
  document.getElementById('mono-wb-warning').classList.toggle('hidden', !monoEnabled);
  const btn = document.getElementById('btn-wb');
  if (btn) btn.disabled = !!monoEnabled || _stitchWbLocked;
}

// ── Collect current control values ───────────────────────────────────

function collectSettings() {
  const activeRot = document.querySelector('#rotation-group .btn-seg.active');
  return {
    ae_enabled:  document.getElementById('ae-enabled').checked,
    exposure_us: getExposureMs() * 1000,
    ae_target:   parseInt(document.getElementById('ae-target').value),
    auto_gain:   document.getElementById('auto-gain').checked,
    analog_gain: parseInt(document.getElementById('analog-gain').value),
    r_gain:      parseInt(document.getElementById('r-gain').value),
    g_gain:      parseInt(document.getElementById('g-gain').value),
    b_gain:      parseInt(document.getElementById('b-gain').value),
    sharpness:   parseInt(document.getElementById('sharpness').value),
    gamma:       parseInt(document.getElementById('gamma').value),
    contrast:    parseInt(document.getElementById('contrast').value),
    saturation:  parseInt(document.getElementById('saturation').value),
    noise_filter:        document.getElementById('noise-filter').checked,
    correct_dead_pixel:  document.getElementById('dead-pixel-correction').checked,
    inverse:             document.getElementById('invert-image').checked,
    anti_flick:      document.getElementById('anti-flick').checked,
    light_frequency: parseInt(document.getElementById('light-frequency').value),
    frame_speed:     parseInt(document.getElementById('frame-speed').value),
    rotation:    activeRot ? parseInt(activeRot.dataset.rotation) : 0,
    h_mirror:    document.getElementById('h-mirror').checked,
    v_mirror:    document.getElementById('v-mirror').checked,
    mono_enabled: document.getElementById('mono-enabled').checked,
  };
}

// ── Load settings from camera ─────────────────────────────────────────

async function loadSettings() {
  try {
    const s = await apiFetch(`${API}/settings?camera_id=${CAMERA_ID}`);
    populateUI(s);
    renderDraft(s.draft_changes);
  } catch (e) {
    showError('Failed to load camera settings: ' + e.message);
  }
}

// ── Apply the controls as a draft ─────────────────────────────────────

async function applyChanges() {
  const s = collectSettings();
  try {
    const res = await apiFetch(`${API}/settings?camera_id=${CAMERA_ID}`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(s),
    });
    if (res.errors && Object.keys(res.errors).length > 0) {
      showError('Some settings failed: ' + JSON.stringify(res.errors));
    } else {
      clearError();
    }
    renderDraft(res.draft_changes);
  } catch (e) {
    showError('Apply error: ' + e.message);
  }
}

// ── Save to production (and the SDK config file) ──────────────────────

async function saveSettings() {
  clearTimeout(liveTimer);
  const s = collectSettings();
  try {
    const res = await apiFetch(`${API}/settings/save?camera_id=${CAMERA_ID}`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(s),
    });
    if (res.errors && Object.keys(res.errors).length > 0) {
      showError('Save had errors: ' + JSON.stringify(res.errors));
    } else {
      renderDraft({});
      clearError();
      showSuccess('Saved to production');
    }
  } catch (e) {
    showError('Save error: ' + e.message);
  }
}

// ── Discard the draft ─────────────────────────────────────────────────

async function discardDraft() {
  clearTimeout(liveTimer);
  try {
    await apiFetch(`${API}/settings/draft/discard?camera_id=${CAMERA_ID}`, {method: 'POST'});
    await loadSettings(); // controls back to the production values
    clearError();
    showSuccess('Draft discarded');
  } catch (e) {
    showError('Discard failed: ' + e.message);
  }
}

// Leaving the page throws the draft away so no test settings are left on
// the camera. sendBeacon still delivers while the page unloads.
window.addEventListener('pagehide', () => {
  clearTimeout(liveTimer);
  if (_draftCount > 0 || _pending) {
    navigator.sendBeacon(`${API}/settings/draft/discard?camera_id=${CAMERA_ID}`);
  }
});

// ── Auto white balance ────────────────────────────────────────────────

async function autoTuneWB() {
  const btn = document.getElementById('btn-wb');
  btn.disabled = true;
  btn.textContent = 'Calibrating…';
  clearError();
  try {
    const res = await apiFetch(`${API}/calibrate-wb?camera_id=${CAMERA_ID}`, {method: 'POST'});
    // calibrate-wb runs at the capture exposure and saves it internally — update sliders + baseline
    const gains = {r_gain: res.r_gain, g_gain: res.g_gain, b_gain: res.b_gain};
    document.getElementById('r-gain').value = gains.r_gain;
    document.getElementById('g-gain').value = gains.g_gain;
    document.getElementById('b-gain').value = gains.b_gain;
    document.getElementById('r-gain-value').value = gains.r_gain;
    document.getElementById('g-gain-value').value = gains.g_gain;
    document.getElementById('b-gain-value').value = gains.b_gain;
    // The new gains are saved straight to production; refresh the draft list.
    const s = await apiFetch(`${API}/settings?camera_id=${CAMERA_ID}`);
    renderDraft(s.draft_changes);
  } catch (e) {
    showError('WB calibration failed: ' + e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = 'Auto Tune WB';
  }
}

// ── Factory reset ─────────────────────────────────────────────────────

function confirmFactoryReset() {
  document.getElementById('factory-reset-dialog').classList.remove('hidden');
}
function closeDialog() {
  document.getElementById('factory-reset-dialog').classList.add('hidden');
}
async function doFactoryReset() {
  closeDialog();
  try {
    await apiFetch(`${API}/settings/factory-reset?camera_id=${CAMERA_ID}`, {method: 'POST'});
    await loadSettings();
    clearError();
  } catch (e) {
    showError('Factory reset failed: ' + e.message);
  }
}

// ── Change handler ────────────────────────────────────────────────────

function onSettingChange() {
  markDirty();
  clearTimeout(liveTimer);
  liveTimer = setTimeout(applyChanges, 250);
}

// ── Stream / snapshot ─────────────────────────────────────────────────

function startStream() {
  const img = document.getElementById('preview-stream');
  const url = `${API}/settings/stream?camera_id=${CAMERA_ID}&fps=5&_t=${Date.now()}`;
  img.onerror = async () => {
    img.onerror = null;
    let msg = 'Stream unavailable.';
    try {
      const r = await fetch(url);
      if (r.status === 409) msg = 'Camera is in Hardware Trigger mode. Disable it on the main page to stream.';
    } catch (_) {}
    showError(msg);
  };
  img.src = url;
  img.classList.remove('hidden');
  document.getElementById('snapshot-img').classList.add('hidden');
  document.getElementById('snapshot-placeholder').classList.add('hidden');
}

function stopStream() {
  // Clearing src closes the HTTP connection, which lets the server-side
  // MJPEG generator exit cleanly and revert trigger mode.
  document.getElementById('preview-stream').src = '';
  document.getElementById('preview-stream').classList.add('hidden');
}

let _snapshotObjectUrl = null;
let _snapshotState = null; // settings the last snapshot was taken with

// ── Snapshot settings card ────────────────────────────────────────────

function showSnapshotInfo(open) {
  const has = !!_snapshotState;
  document.getElementById('snapshot-info').classList.toggle('hidden', !(has && open));
  document.getElementById('snapshot-info-open').classList.toggle('hidden', !(has && !open));
}

function renderSnapshotInfo(st) {
  const list = document.getElementById('snapshot-info-list');
  list.replaceChildren();
  const row = (label, value, mode) => {
    const dt = document.createElement('dt');
    dt.textContent = label;
    const dd = document.createElement('dd');
    dd.textContent = value;
    if (mode) {
      const tag = document.createElement('span');
      tag.className = `mode mode-${mode}`;
      tag.textContent = mode;
      dd.append(tag);
    }
    list.append(dt, dd);
  };
  const fmt = (v, digits) => (v == null ? '—' : String(parseFloat(Number(v).toFixed(digits))));

  row('Exposure', st.exposure_us == null ? '—' : `${fmt(st.exposure_us / 1000, 3)} ms`,
      st.ae_enabled ? 'auto' : 'manual');
  row('Analog gain', st.analog_gain_x == null ? '—' : `${fmt(st.analog_gain_x, 3)}×`,
      st.auto_gain ? 'auto' : 'manual');
  if (st.ae_enabled || st.auto_gain) row('AE target', fmt(st.ae_target, 0));
  row('Gamma', fmt(st.gamma, 0));
  row('Contrast', fmt(st.contrast, 0));
  row('Saturation', fmt(st.saturation, 0));
  row('R / G / B gain', [st.r_gain, st.g_gain, st.b_gain].map(v => fmt(v, 2)).join(' / '));

  const canUse = st.exposure_us != null && st.analog_gain_raw != null;
  document.getElementById('btn-use-manual').disabled = !canUse;
}

// Copy the snapshot's exposure and gain into the manual controls and apply
// them, so the next snapshot is taken with the same values held fixed.
async function useSnapshotAsManual() {
  const st = _snapshotState;
  if (!st) return;
  document.getElementById('ae-enabled').checked = false;
  document.getElementById('auto-gain').checked = false;
  const ms = setExposureMs(st.exposure_us / 1000);
  updateExposureWarning(ms * 1000);
  const gain = document.getElementById('analog-gain');
  gain.value = st.analog_gain_raw;
  document.getElementById('analog-gain-value').value = gainRawToX(gain.value);
  updateAutoRows();
  markDirty();
  await applyChanges();
  showSuccess(`Manual: ${ms} ms, ${gainRawToX(gain.value)}×`);
}

async function takeSnapshot() {
  const btn = document.getElementById('btn-action');
  const img = document.getElementById('snapshot-img');
  const placeholder = document.getElementById('snapshot-placeholder');

  btn.disabled = true;
  btn.textContent = 'Capturing…';
  placeholder.querySelector('p').textContent = 'Capturing…';
  placeholder.classList.remove('hidden');
  img.classList.add('hidden');
  _snapshotState = null;
  showSnapshotInfo(false);

  try {
    // Photo mode doesn't live-apply, so push the current controls first;
    // otherwise the snapshot would use whatever was last applied.
    await applyChanges();
    const res = await fetch(`${API}/settings/snapshot?camera_id=${CAMERA_ID}`);
    if (!res.ok) {
      const j = await res.json().catch(() => ({}));
      throw new Error(j.error || `HTTP ${res.status}`);
    }
    try {
      _snapshotState = JSON.parse(res.headers.get('X-Capture-State') || 'null');
    } catch (_) {
      _snapshotState = null;
    }
    const blob = await res.blob();
    if (_snapshotObjectUrl) URL.revokeObjectURL(_snapshotObjectUrl);
    _snapshotObjectUrl = URL.createObjectURL(blob);
    img.src = _snapshotObjectUrl;
    img.classList.remove('hidden');
    placeholder.classList.add('hidden');
    if (_snapshotState) {
      renderSnapshotInfo(_snapshotState);
      showSnapshotInfo(true);
    }
    clearError();
  } catch (e) {
    showError('Snapshot failed: ' + e.message);
    placeholder.querySelector('p').textContent = 'Take a snapshot to preview';
  } finally {
    btn.disabled = false;
    btn.textContent = 'Take Snapshot';
  }
}

// ── Settings search ─────────────────────────────────────────────────────

function applySettingsSearch(query) {
  const q = query.trim().toLowerCase();

  document.querySelectorAll('.settings-panel .setting-row').forEach(row => {
    row.classList.toggle('search-hidden', !!q && !row.textContent.toLowerCase().includes(q));
  });

  document.querySelectorAll('.advanced-group').forEach(group => {
    const rows = group.querySelectorAll('.setting-row');
    const anyVisible = Array.from(rows).some(r => !r.classList.contains('search-hidden'));
    group.classList.toggle('search-hidden', !!q && !anyVisible);
    if (q && anyVisible) group.open = true;
  });

  document.querySelectorAll('.settings-panel .settings-section').forEach(section => {
    const rows = section.querySelectorAll('.setting-row');
    const anyVisible = Array.from(rows).some(r => !r.classList.contains('search-hidden'));
    section.classList.toggle('search-hidden', !!q && !anyVisible);
  });
}

// ── Wire all controls ─────────────────────────────────────────────────

function wireControls() {
  // AE toggle
  document.getElementById('ae-enabled').addEventListener('change', function () {
    updateAutoRows();
    onSettingChange();
  });
  document.getElementById('auto-gain').addEventListener('change', function () {
    updateAutoRows();
    onSettingChange();
  });

  // Exposure slider — update display on every drag tick, but only trigger
  // live-apply on release ('change') to avoid hammering the camera with
  // intermediate values that stall the preview at long exposures.
  document.getElementById('exposure-us').addEventListener('input', function () {
    exposureMs = exposurePosToMs(parseFloat(this.value));
    const ms = exposureMs;
    const us = ms * 1000;
    document.getElementById('exposure-value').value = formatExposure(ms);
    updateExposureWarning(us);
    markDirty();
  });
  document.getElementById('exposure-us').addEventListener('change', function () {
    clearTimeout(liveTimer);
    liveTimer = setTimeout(applyChanges, 100);
  });

  // Simple value-display sliders
  [
    ['ae-target',    'ae-target-value',    v => v],
    ['analog-gain',  'analog-gain-value',  gainRawToX],
    ['r-gain',       'r-gain-value',       v => v],
    ['g-gain',       'g-gain-value',       v => v],
    ['b-gain',       'b-gain-value',       v => v],
    ['sharpness',    'sharpness-value',    v => v],
    ['gamma',        'gamma-value',        v => v],
    ['contrast',     'contrast-value',     v => v],
    ['saturation',   'saturation-value',   v => v],
    ['frame-speed',  'frame-speed-value',  v => v],
  ].forEach(([id, valueId, fmt]) => {
    document.getElementById(id).addEventListener('input', function () {
      document.getElementById(valueId).value = fmt(this.value);
      onSettingChange();
    });
  });

  // Typed values: every value box drives its slider. The slider clamps to its
  // min/max/step, so the box snaps back to what was actually applied.
  document.querySelectorAll('input.setting-value').forEach(box => {
    const slider = document.getElementById(box.id.replace(/-value$/, '').replace(/^exposure$/, 'exposure-us'));
    if (!slider) return;
    const commit = () => {
      if (box.value === '' || isNaN(parseFloat(box.value))) {
        box.value = slider.id === 'exposure-us' ? getExposureMs()
          : slider.id === 'analog-gain' ? gainRawToX(slider.value) : slider.value;
        return;
      }
      if (slider.id === 'exposure-us') setExposureMs(parseFloat(box.value));
      else if (slider.id === 'analog-gain') slider.value = gainXToRaw(parseFloat(box.value));
      else slider.value = box.value;
      slider.dispatchEvent(new Event('input'));
      if (slider.id === 'exposure-us') slider.dispatchEvent(new Event('change'));
    };
    box.addEventListener('change', commit);
    box.addEventListener('keydown', e => {
      if (e.key === 'Enter') { commit(); box.select(); }
    });
    // Select everything on focus so typing replaces the old value.
    box.addEventListener('focus', () => box.select());
    box.addEventListener('mouseup', e => e.preventDefault());
  });

  // Layout: [name] [− slider +] [value box]. The value box is moved out of the
  // label into a fixed-width cell at the row's right edge so it (and the slider)
  // never shift as the value changes.
  document.querySelectorAll('.setting-label input.setting-value').forEach(box => {
    const label = box.closest('.setting-label');
    const row = label.closest('.setting-row');
    const cell = document.createElement('span');
    cell.className = 'value-cell';
    cell.append(box);
    const unit = label.querySelector('.setting-unit');
    if (unit) cell.append(unit);
    row.classList.add('has-slider');
    row.append(cell);
  });

  // − / + nudge buttons on every slider: one click = one unit (1 ms for
  // exposure), hold to repeat. Much easier than pixel-hunting with the thumb.
  document.querySelectorAll('input.slider').forEach(slider => {
    const nudge = dir => {
      if (slider.id === 'exposure-us') setExposureMs(Math.round(getExposureMs()) + dir);
      else if (dir > 0) slider.stepUp();
      else slider.stepDown();
      slider.dispatchEvent(new Event('input'));
      return slider.id === 'exposure-us';
    };
    const makeBtn = (label, dir) => {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'slider-nudge';
      btn.textContent = label;
      btn.setAttribute('aria-label', dir > 0 ? 'Increase' : 'Decrease');
      let delay, repeat, fired = false;
      const stop = () => {
        clearTimeout(delay);
        clearInterval(repeat);
        if (fired && slider.id === 'exposure-us') slider.dispatchEvent(new Event('change'));
        fired = false;
      };
      btn.addEventListener('pointerdown', e => {
        e.preventDefault();
        fired = true;
        nudge(dir);
        delay = setTimeout(() => { repeat = setInterval(() => nudge(dir), 60); }, 350);
      });
      ['pointerup', 'pointerleave', 'pointercancel'].forEach(ev => btn.addEventListener(ev, stop));
      return btn;
    };
    slider.before(makeBtn('−', -1));
    slider.after(makeBtn('+', 1));
  });

  // Rotation buttons
  document.querySelectorAll('#rotation-group .btn-seg').forEach(btn => {
    btn.addEventListener('click', function () {
      document.querySelectorAll('#rotation-group .btn-seg').forEach(b => b.classList.remove('active'));
      this.classList.add('active');
      onSettingChange();
    });
  });

  // Mirror toggles
  document.getElementById('h-mirror').addEventListener('change', onSettingChange);
  document.getElementById('v-mirror').addEventListener('change', onSettingChange);

  // Monochrome toggle
  document.getElementById('mono-enabled').addEventListener('change', function () {
    updateMonoWbGate(this.checked);
    onSettingChange();
  });

  // Advanced toggles / select
  ['noise-filter', 'dead-pixel-correction', 'invert-image', 'anti-flick'].forEach(id => {
    document.getElementById(id).addEventListener('change', onSettingChange);
  });
  document.getElementById('light-frequency').addEventListener('change', onSettingChange);

  // Settings search
  document.getElementById('settings-search').addEventListener('input', function () {
    applySettingsSearch(this.value);
  });

  // Snapshot settings card
  document.getElementById('snapshot-info-close').addEventListener('click', () => showSnapshotInfo(false));
  document.getElementById('snapshot-info-open').addEventListener('click', () => showSnapshotInfo(true));
  document.getElementById('btn-use-manual').addEventListener('click', useSnapshotAsManual);

  // Draft badge opens the list of changes
  document.getElementById('draft-badge').addEventListener('click', () => toggleDraftList());
  document.addEventListener('click', e => {
    if (!e.target.closest('.page-header-right')) toggleDraftList(false);
  });
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') toggleDraftList(false);
  });

  // Preview mode radio
  document.querySelectorAll('[name="preview-mode"]').forEach(radio => {
    radio.addEventListener('change', function () {
      const prev = previewMode;
      previewMode = this.value;

      document.getElementById('btn-action').style.visibility =
        previewMode === 'photo' ? 'visible' : 'hidden';

      if (previewMode === 'photo') {
        stopStream();
        document.getElementById('snapshot-placeholder').classList.remove('hidden');
        document.getElementById('snapshot-img').classList.add('hidden');
      } else if (prev === 'photo') {
        _snapshotState = null;
        showSnapshotInfo(false);
        startStream();
      }
    });
  });
}

// ── Stitch WB lock ────────────────────────────────────────────────────

async function checkStitchWbLock() {
  try {
    const [stitchCal, wbCal] = await Promise.all([
      fetch('/api/stitch/calibrate').then(r => r.json()).catch(() => null),
      fetch('/api/stitch/calibrate-color').then(r => r.json()).catch(() => null),
    ]);
    const inStitch = stitchCal && Array.isArray(stitchCal.cameras_calibrated) &&
                     stitchCal.cameras_calibrated.includes(CAMERA_ID);
    const hasWbCal = wbCal && wbCal.calibrated;
    if (inStitch && hasWbCal) {
      _stitchWbLocked = true;
      document.getElementById('btn-wb').disabled = true;
      document.getElementById('wb-stitch-warning').classList.remove('hidden');
    }
  } catch (_) {}
}

// ── Init ──────────────────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', async () => {
  wireControls();
  await loadSettings();
  checkStitchWbLock();
});
