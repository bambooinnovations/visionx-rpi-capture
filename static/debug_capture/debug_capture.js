'use strict';

const state = {
  cameraIds: [],
  cameraModes: {},        // id -> mode string
  selected: new Set(),    // camera ids checked for capture
  destinationConfigured: false,
  liveRunning: false,
  captures: {},           // id -> base64 jpeg (post-capture, pre-upload)
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

function isBlocked(camId) {
  return state.cameraModes[camId] === 'hardware_trigger';
}

// ── Live view / selection ──────────────────────────────────────────────────
function renderLiveGrid() {
  const grid = document.getElementById('live-grid');
  grid.innerHTML = state.cameraIds.map(id => {
    const blocked = isBlocked(id);
    const checked = state.selected.has(id) && !blocked;
    const body = blocked
      ? `<div class="cam-tile-blocked">Hardware-trigger mode — can't capture on demand</div>`
      : `<img id="live-img-${id}" alt="Camera ${id} live view">`;
    return `
      <div class="cam-tile">
        <div class="cam-tile-label">
          <label>
            <input type="checkbox" ${checked ? 'checked' : ''} ${blocked ? 'disabled' : ''}
                   onchange="toggleSelected(${id}, this.checked)">
            Camera ${id}
          </label>
        </div>
        ${body}
      </div>
    `;
  }).join('');

  if (state.liveRunning) {
    state.cameraIds.forEach(id => {
      if (isBlocked(id)) return;
      const img = document.getElementById(`live-img-${id}`);
      if (img) setStream(img, `/api/cameras/settings/stream?camera_id=${id}&_t=${Date.now()}`);
    });
  }

  updateCaptureButton();
}

function toggleSelected(camId, checked) {
  if (checked) state.selected.add(camId);
  else state.selected.delete(camId);
  updateCaptureButton();
}

function updateCaptureButton() {
  const anySelected = [...state.selected].some(id => !isBlocked(id));
  document.getElementById('btn-capture').disabled = !anySelected;
}

const liveGrid = {
  start() {
    state.liveRunning = true;
    renderLiveGrid();
    document.getElementById('btn-live-start').classList.add('hidden');
    document.getElementById('btn-live-stop').classList.remove('hidden');
  },
  stop() {
    state.cameraIds.forEach(id => {
      const img = document.getElementById(`live-img-${id}`);
      if (img) clearStream(img);
    });
    state.liveRunning = false;
    document.getElementById('btn-live-start').classList.remove('hidden');
    document.getElementById('btn-live-stop').classList.add('hidden');
  },
};

// ── Capture ─────────────────────────────────────────────────────────────────
async function captureSelected() {
  const camIds = [...state.selected].filter(id => !isBlocked(id));
  if (camIds.length === 0) return;

  try {
    const r = await apiFetch('POST', '/api/debug/capture', { camera_ids: camIds });
    liveGrid.stop();

    state.captures = {};
    const grid = document.getElementById('review-grid');
    grid.innerHTML = camIds.map(id => {
      const img = r.images ? r.images[id] : null;
      const err = r.errors ? r.errors[id] : null;
      if (img) state.captures[id] = img;
      const body = img
        ? `<img src="data:image/jpeg;base64,${img}" alt="Camera ${id} capture">`
        : `<div class="cam-tile-blocked">${err || 'Capture failed'}</div>`;
      return `
        <div class="cam-tile" id="review-tile-${id}">
          <div class="cam-tile-label"><span>Camera ${id}</span></div>
          ${body}
          <div id="review-result-${id}"></div>
        </div>
      `;
    }).join('');

    document.getElementById('card-live').classList.add('hidden');
    document.getElementById('card-review').classList.remove('hidden');
    document.getElementById('btn-upload-all').disabled = !state.destinationConfigured;
  } catch (e) {
    showError(e.message);
  }
}

function retake() {
  state.captures = {};
  document.getElementById('card-review').classList.add('hidden');
  document.getElementById('card-live').classList.remove('hidden');
  liveGrid.start();
}

// ── Upload ──────────────────────────────────────────────────────────────────
async function uploadAll() {
  const entries = Object.entries(state.captures);
  if (entries.length === 0) return;

  const btn = document.getElementById('btn-upload-all');
  btn.disabled = true;

  const results = await Promise.all(entries.map(async ([camId, image_base64]) => {
    try {
      const r = await apiFetch('POST', '/api/debug/upload', { camera_id: parseInt(camId, 10), image_base64 });
      return [camId, r.uploaded, r.error];
    } catch (e) {
      return [camId, false, e.message];
    }
  }));

  let anyFailed = false;
  results.forEach(([camId, ok, error]) => {
    const el = document.getElementById(`review-result-${camId}`);
    if (!el) return;
    el.className = `cam-tile-result ${ok ? 'ok' : 'fail'}`;
    el.textContent = ok ? '✓ Uploaded' : `✗ ${error || 'Upload failed'}`;
    if (ok) delete state.captures[camId];
    else anyFailed = true;
  });

  // Leave it clickable if anything failed so the operator can retry without recapturing.
  btn.disabled = !anyFailed && Object.keys(state.captures).length === 0;
}

// ── Init ──────────────────────────────────────────────────────────────────────
async function refreshState() {
  try {
    const s = await apiFetch('GET', '/api/debug/state');
    state.cameraIds = s.camera_ids;
    state.cameraModes = {};
    for (const id of s.camera_ids) {
      state.cameraModes[id] = (s.cameras[id] || {}).mode;
      if (!isBlocked(id)) state.selected.add(id);
    }
    state.destinationConfigured = s.destination_configured;

    document.getElementById('dest-warning').classList.toggle('hidden', s.destination_configured);
    renderLiveGrid();
  } catch (e) {
    showError(e.message);
  }
}

document.addEventListener('DOMContentLoaded', () => {
  refreshState();
});
