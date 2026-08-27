'use strict';

const API = `/api/cameras`;

let initialSettings = {};
let previewMode = 'live'; // 'live' | 'manual' | 'photo'
let liveMode = true;
let liveTimer = null;

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
function markDirty() {
  document.getElementById('unsaved-badge').classList.remove('hidden');
}
function markClean() {
  document.getElementById('unsaved-badge').classList.add('hidden');
}

function setSlider(id, value, min, max, valueId, fmt) {
  const el = document.getElementById(id);
  el.min = min;
  el.max = max;
  el.value = value;
  if (valueId) document.getElementById(valueId).textContent = fmt ? fmt(value) : value;
}

function formatExposure(us) {
  const ms = us / 1000;
  return ms >= 1 ? `${ms.toFixed(1)} ms` : `${us.toFixed(0)} µs`;
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

// ── Populate controls from a settings object ──────────────────────────

function populateUI(s) {
  // Exposure
  const aeEl = document.getElementById('ae-enabled');
  aeEl.checked = s.ae_enabled;
  document.getElementById('manual-exposure-row').classList.toggle('hidden', s.ae_enabled);

  setSlider('exposure-us', s.exposure_us,
    s.exposure_min_us || 26, s.exposure_max_us || 1_000_000,
    'exposure-value', formatExposure);
  updateExposureWarning(s.exposure_us);

  setSlider('ae-target', s.ae_target, 0, 255, 'ae-target-value');

  // Gain
  setSlider('analog-gain', s.analog_gain,
    s.analog_gain_min || 16, s.analog_gain_max || 128,
    'analog-gain-value');

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
    exposure_us: parseFloat(document.getElementById('exposure-us').value),
    ae_target:   parseInt(document.getElementById('ae-target').value),
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
    initialSettings = {...s};
    populateUI(s);
    markClean();
  } catch (e) {
    showError('Failed to load camera settings: ' + e.message);
  }
}

// ── Apply (live or on-click) ──────────────────────────────────────────

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
  } catch (e) {
    showError('Apply error: ' + e.message);
  }
}

// ── Save to SDK config file ───────────────────────────────────────────

async function saveSettings() {
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
      initialSettings = {...s};
      markClean();
      clearError();
      showSuccess('Settings saved');
    }
  } catch (e) {
    showError('Save error: ' + e.message);
  }
}

// ── Reset to last saved state ─────────────────────────────────────────

async function resetChanges() {
  populateUI(initialSettings);
  markClean();
  if (liveMode) await applyChanges();
}

// ── Auto white balance ────────────────────────────────────────────────

async function autoTuneWB() {
  const btn = document.getElementById('btn-wb');
  btn.disabled = true;
  btn.textContent = 'Calibrating…';
  clearError();
  try {
    const res = await apiFetch(`${API}/calibrate-wb?camera_id=${CAMERA_ID}`, {method: 'POST'});
    // calibrate-wb saves via CameraSaveParameter internally — update sliders + baseline
    const gains = {r_gain: res.r_gain, g_gain: res.g_gain, b_gain: res.b_gain};
    document.getElementById('r-gain').value = gains.r_gain;
    document.getElementById('g-gain').value = gains.g_gain;
    document.getElementById('b-gain').value = gains.b_gain;
    document.getElementById('r-gain-value').textContent = gains.r_gain;
    document.getElementById('g-gain-value').textContent = gains.g_gain;
    document.getElementById('b-gain-value').textContent = gains.b_gain;
    // Merge into initialSettings since WB cal already saved
    initialSettings = {...initialSettings, ...gains};
    markClean();
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
  if (!liveMode) return;
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

async function takeSnapshot() {
  const btn = document.getElementById('btn-action');
  const img = document.getElementById('snapshot-img');
  const placeholder = document.getElementById('snapshot-placeholder');

  btn.disabled = true;
  btn.textContent = 'Capturing…';
  placeholder.querySelector('p').textContent = 'Capturing…';
  placeholder.classList.remove('hidden');
  img.classList.add('hidden');

  try {
    const res = await fetch(`${API}/settings/snapshot?camera_id=${CAMERA_ID}`);
    if (!res.ok) {
      const j = await res.json().catch(() => ({}));
      throw new Error(j.error || `HTTP ${res.status}`);
    }
    const blob = await res.blob();
    if (_snapshotObjectUrl) URL.revokeObjectURL(_snapshotObjectUrl);
    _snapshotObjectUrl = URL.createObjectURL(blob);
    img.src = _snapshotObjectUrl;
    img.classList.remove('hidden');
    placeholder.classList.add('hidden');
    clearError();
  } catch (e) {
    showError('Snapshot failed: ' + e.message);
    placeholder.querySelector('p').textContent = 'Press "Take Snapshot" to capture a frame';
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
    document.getElementById('manual-exposure-row').classList.toggle('hidden', this.checked);
    onSettingChange();
  });

  // Exposure slider — update display on every drag tick, but only trigger
  // live-apply on release ('change') to avoid hammering the camera with
  // intermediate values that stall the preview at long exposures.
  document.getElementById('exposure-us').addEventListener('input', function () {
    const us = parseFloat(this.value);
    document.getElementById('exposure-value').textContent = formatExposure(us);
    updateExposureWarning(us);
    markDirty();
  });
  document.getElementById('exposure-us').addEventListener('change', function () {
    if (!liveMode) return;
    clearTimeout(liveTimer);
    liveTimer = setTimeout(applyChanges, 100);
  });

  // Simple value-display sliders
  [
    ['ae-target',    'ae-target-value',    v => v],
    ['analog-gain',  'analog-gain-value',  v => v],
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
      document.getElementById(valueId).textContent = fmt(this.value);
      onSettingChange();
    });
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

  // Preview mode radio
  document.querySelectorAll('[name="preview-mode"]').forEach(radio => {
    radio.addEventListener('change', function () {
      const prev = previewMode;
      previewMode = this.value;
      liveMode = previewMode === 'live';

      const btn = document.getElementById('btn-action');
      if (previewMode === 'manual') {
        btn.textContent = 'Apply';
        btn.onclick = applyChanges;
        btn.style.visibility = 'visible';
      } else if (previewMode === 'photo') {
        btn.textContent = 'Take Snapshot';
        btn.onclick = takeSnapshot;
        btn.style.visibility = 'visible';
      } else {
        btn.style.visibility = 'hidden';
      }

      if (previewMode === 'photo') {
        stopStream();
        document.getElementById('snapshot-placeholder').classList.remove('hidden');
        document.getElementById('snapshot-img').classList.add('hidden');
        document.getElementById('preview-hint').textContent =
          'Click "Take Snapshot" to capture a frame — works at any exposure time.';
      } else if (prev === 'photo') {
        document.getElementById('preview-hint').textContent =
          'Changes visible in stream within a few frames.';
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
  startStream();
  checkStitchWbLock();
});
