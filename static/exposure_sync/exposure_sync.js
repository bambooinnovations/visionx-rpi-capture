'use strict';

const state = {
  cameraIds: [],
  referenceCameraId: null,
  enabled: false,
  hwTriggerActive: false,
  workingExposureUs: null,
  workingAnalogGain: null,
  liveRunning: false,
};

// ── API ──────────────────────────────────────────────────────────────────
async function apiFetch(method, path, body = null) {
  const opts = { method, headers: { 'Content-Type': 'application/json' } };
  if (body !== null) opts.body = JSON.stringify(body);
  const res = await fetch(path, opts);
  const ct = res.headers.get('content-type') || '';
  const json = ct.includes('application/json') ? await res.json().catch(() => ({})) : {};
  if (!res.ok && res.status !== 207) throw new Error(json.error || `HTTP ${res.status}`);
  return json;
}

// ── UI helpers ───────────────────────────────────────────────────────────
function showError(msg) {
  document.getElementById('error-text').textContent = msg;
  document.getElementById('error-banner').classList.remove('hidden');
}
function clearError() {
  document.getElementById('error-banner').classList.add('hidden');
}

let _successTimer = null;
function showSuccess(msg = 'Saved') {
  const el = document.getElementById('success-toast');
  el.textContent = msg;
  el.classList.remove('hidden', 'fading');
  clearTimeout(_successTimer);
  _successTimer = setTimeout(() => {
    el.classList.add('fading');
    setTimeout(() => el.classList.add('hidden'), 400);
  }, 2000);
}

function formatExposure(us) {
  if (us == null) return '—';
  const ms = us / 1000;
  return ms >= 1 ? `${ms.toFixed(1)} ms` : `${us.toFixed(0)} µs`;
}

function setStream(imgEl, url) {
  imgEl.onerror = async () => {
    imgEl.onerror = null;
    let msg = 'Stream unavailable.';
    try {
      const r = await fetch(url);
      if (r.status === 409) msg = 'Camera is in hardware-trigger mode.';
    } catch (_) {}
    showError(msg);
  };
  imgEl.src = url;
}
function clearStream(imgEl) { imgEl.onerror = null; imgEl.src = ''; }

// ── Reference picker ───────────────────────────────────────────────────────
function renderReferencePicker() {
  const el = document.getElementById('reference-picker');
  el.innerHTML = state.cameraIds.map(id => `
    <button class="btn-seg${id === state.referenceCameraId ? ' active' : ''}"
            onclick="selectReference(${id})">Camera ${id}</button>
  `).join('');
}

async function selectReference(camId) {
  try {
    await apiFetch('POST', '/api/exposure-sync/reference', { camera_id: camId });
    state.referenceCameraId = camId;
    renderReferencePicker();
    document.getElementById('btn-capture-ref').disabled = false;
    if (state.liveRunning) liveGrid.restart();
  } catch (e) {
    showError(e.message);
  }
}

// ── Live grid (step 2) ──────────────────────────────────────────────────────
const liveGrid = {
  start() {
    const grid = document.getElementById('live-grid');
    grid.innerHTML = state.cameraIds.map(id => `
      <div class="cam-tile">
        <div class="cam-tile-label">Camera ${id}${id === state.referenceCameraId ? ' (reference)' : ''}</div>
        <img id="live-img-${id}" alt="Camera ${id} live view">
      </div>
    `).join('');
    state.cameraIds.forEach(id => {
      const img = document.getElementById(`live-img-${id}`);
      setStream(img, `/api/cameras/settings/stream?camera_id=${id}&_t=${Date.now()}`);
    });
    state.liveRunning = true;
    document.getElementById('btn-live-start').classList.add('hidden');
    document.getElementById('btn-live-stop').classList.remove('hidden');
  },
  stop() {
    state.cameraIds.forEach(id => {
      const img = document.getElementById(`live-img-${id}`);
      if (img) clearStream(img);
    });
    document.getElementById('live-grid').innerHTML = '';
    state.liveRunning = false;
    document.getElementById('btn-live-start').classList.remove('hidden');
    document.getElementById('btn-live-stop').classList.add('hidden');
  },
  restart() {
    if (state.liveRunning) { this.stop(); this.start(); }
  },
};

// ── Step 3: capture reference ───────────────────────────────────────────────
async function captureReference() {
  if (state.referenceCameraId == null) return;
  try {
    const r = await apiFetch('POST', '/api/exposure-sync/capture-reference');
    state.workingExposureUs = r.exposure_us;
    state.workingAnalogGain = r.analog_gain;

    document.getElementById('reference-result').classList.remove('hidden');
    document.getElementById('reference-snapshot').src =
      `/api/cameras/settings/snapshot?camera_id=${state.referenceCameraId}&_t=${Date.now()}`;
    document.getElementById('readout-exposure').textContent = formatExposure(r.exposure_us);
    document.getElementById('readout-gain').textContent = r.analog_gain;
    document.getElementById('nudge-value').textContent = formatExposure(r.exposure_us);

    document.getElementById('btn-too-dark').disabled = false;
    document.getElementById('btn-too-bright').disabled = false;
    document.getElementById('btn-apply').disabled = false;
  } catch (e) {
    showError(e.message);
  }
}

// ── Step 4: nudge (pure arithmetic — no hardware write) ────────────────────
async function nudge(direction) {
  if (state.workingExposureUs == null) return;
  try {
    const r = await apiFetch('POST', '/api/exposure-sync/nudge', {
      current_exposure_us: state.workingExposureUs,
      direction,
    });
    state.workingExposureUs = r.exposure_us;
    document.getElementById('nudge-value').textContent = formatExposure(r.exposure_us);
    document.getElementById('readout-exposure').textContent = formatExposure(r.exposure_us);
  } catch (e) {
    showError(e.message);
  }
}

// ── Step 5: apply to followers (in-memory only) ─────────────────────────────
async function applyToFollowers() {
  if (state.workingExposureUs == null || state.workingAnalogGain == null) return;
  try {
    const r = await apiFetch('POST', '/api/exposure-sync/apply', {
      exposure_us: state.workingExposureUs,
      analog_gain: state.workingAnalogGain,
    });
    const lines = Object.entries(r.results).map(([camId, res]) => {
      const errKeys = Object.keys(res.errors || {});
      return errKeys.length
        ? `Camera ${camId}: error — ${errKeys.map(k => res.errors[k]).join(', ')}`
        : `Camera ${camId}: applied`;
    });
    const resultEl = document.getElementById('apply-result');
    resultEl.textContent = lines.join(' · ');
    resultEl.classList.remove('hidden');

    document.getElementById('btn-preview').disabled = false;
    document.getElementById('btn-save').disabled = false;
  } catch (e) {
    showError(e.message);
  }
}

// ── Step 6: preview all three ────────────────────────────────────────────────
async function previewAll() {
  try {
    const r = await apiFetch('POST', '/api/exposure-sync/preview');
    const grid = document.getElementById('preview-grid');
    grid.innerHTML = state.cameraIds.map(id => {
      const img = r.images[id];
      const err = r.errors[id];
      const label = id === state.referenceCameraId ? `Camera ${id} (reference)` : `Camera ${id}`;
      const body = img
        ? `<img src="data:image/jpeg;base64,${img}" alt="Camera ${id} preview">`
        : `<div class="info-box info-box-warning">${err || 'No image'}</div>`;
      return `<div class="cam-tile"><div class="cam-tile-label">${label}</div>${body}</div>`;
    }).join('');
  } catch (e) {
    showError(e.message);
  }
}

// ── Step 7: save ─────────────────────────────────────────────────────────────
async function saveCalibration() {
  if (state.referenceCameraId == null || state.workingExposureUs == null) return;
  try {
    await apiFetch('POST', '/api/exposure-sync/save', {
      camera_id: state.referenceCameraId,
      exposure_us: state.workingExposureUs,
      analog_gain: state.workingAnalogGain,
    });
    showSuccess('Calibration saved');
    await refreshState();
  } catch (e) {
    showError(e.message);
  }
}

// ── Enable toggle ─────────────────────────────────────────────────────────────
async function onToggleEnabled() {
  const checkbox = document.getElementById('enabled-toggle');
  const desired = checkbox.checked;
  try {
    await apiFetch('POST', '/api/exposure-sync/enabled', { enabled: desired });
    state.enabled = desired;
    renderEnabledStatus();
  } catch (e) {
    checkbox.checked = !desired;
    showError(e.message);
  }
}

function renderEnabledStatus() {
  const pill = document.getElementById('pill-enabled');
  pill.textContent = state.enabled ? 'Enabled' : 'Disabled';
  pill.className = 'pill ' + (state.enabled ? 'pill-green' : 'pill-unknown');

  document.getElementById('enabled-label').textContent =
    state.enabled ? 'Exposure sync enabled' : 'Exposure sync disabled';
  document.getElementById('enabled-toggle').checked = state.enabled;

  document.getElementById('hw-active-warning').classList.toggle('hidden', !state.hwTriggerActive);
  document.getElementById('enabled-toggle').disabled = state.hwTriggerActive;
}

// ── Init ──────────────────────────────────────────────────────────────────────
async function refreshState() {
  const [camList, syncState] = await Promise.all([
    apiFetch('GET', '/api/cameras'),
    apiFetch('GET', '/api/exposure-sync/state'),
  ]);

  state.cameraIds = camList.map(c => c.camera_id).sort((a, b) => a - b);
  state.referenceCameraId = syncState.reference_camera_id;
  state.enabled = syncState.enabled;
  state.hwTriggerActive = syncState.hw_trigger_active;
  state.workingExposureUs = syncState.exposure_us;
  state.workingAnalogGain = syncState.analog_gain;

  renderReferencePicker();
  renderEnabledStatus();

  const summary = document.getElementById('saved-summary');
  if (syncState.reference_camera_id != null && syncState.exposure_us != null) {
    const when = syncState.calibrated_at
      ? ` at ${new Date(syncState.calibrated_at * 1000).toLocaleString()}`
      : '';
    summary.textContent =
      `Last saved: reference camera ${syncState.reference_camera_id}, ` +
      `${formatExposure(syncState.exposure_us)}, gain ${syncState.analog_gain}${when}`;
    summary.classList.remove('hidden');
  } else {
    summary.classList.add('hidden');
  }

  document.getElementById('btn-capture-ref').disabled = state.referenceCameraId == null;
}

document.addEventListener('DOMContentLoaded', () => {
  refreshState().catch(e => showError(e.message));
});
