'use strict';

const state = {
  cameras: [],
  boards: [],
  activeCameraId: null,
  activeBoardId: null,
  streaming: false,
  isLoading: false,
};

// ── API ────────────────────────────────────────────────────────────────────
async function apiFetch(method, path, body = null) {
  const opts = { method, headers: { 'Content-Type': 'application/json' } };
  if (body !== null) opts.body = JSON.stringify(body);
  const res = await fetch(path, opts);
  if (!res.ok) {
    let msg = `HTTP ${res.status}`;
    try {
      const j = await res.json();
      msg = j.error || j.message || msg;
    } catch (_) {}
    throw new Error(msg);
  }
  return res.json();
}

// ── Error display ────────────────────────────────────────────────────────────
function showError(msg) {
  document.getElementById('error-text').textContent = msg;
  document.getElementById('error-banner').classList.remove('hidden');
}
function clearError() {
  document.getElementById('error-banner').classList.add('hidden');
}

// ── pxcm ───────────────────────────────────────────────────────────────────
const pxcm = {
  _activeBoard() {
    return state.boards.find(b => b.id === state.activeBoardId) || null;
  },

  onCameraChange() {
    const sel = document.getElementById('pxcm-camera-select');
    pxcm.stopLive();
    pxcm._hideResult();

    if (sel.value === '') {
      state.activeCameraId = null;
      document.getElementById('pxcm-stream-section').classList.add('hidden');
      document.getElementById('pxcm-select-hint').classList.remove('hidden');
      return;
    }

    state.activeCameraId = parseInt(sel.value, 10);
    document.getElementById('pxcm-select-hint').classList.add('hidden');
    document.getElementById('pxcm-stream-section').classList.remove('hidden');
  },

  onBoardChange() {
    const sel = document.getElementById('pxcm-board-select');
    state.activeBoardId = sel.value;
    pxcm._renderBoardDetails();
  },

  _renderBoardDetails() {
    const box = document.getElementById('pxcm-board-details');
    const b = pxcm._activeBoard();
    if (!b) { box.innerHTML = ''; return; }
    box.innerHTML =
      `${b.board_cols}×${b.board_rows} squares · ${b.square_mm}mm checker · ` +
      `${b.marker_mm}mm marker · ${b.aruco_dict}`;
  },

  _streamUrl(camId) {
    return `/rpi/stream?camera_id=${camId}`;
  },

  startLive() {
    if (state.activeCameraId === null) return;
    const img = document.getElementById('live-view-img');
    const url = pxcm._streamUrl(state.activeCameraId);
    img.onerror = async () => {
      img.onerror = null;
      pxcm._setIdle();
      let msg = 'Stream unavailable.';
      try {
        const r = await fetch(url);
        if (r.status === 409) msg = 'Camera is in Hardware Trigger mode.';
      } catch (_) {}
      showError(msg);
    };
    img.src = url;
    state.streaming = true;
    pxcm._setActive();
  },

  stopLive() {
    const img = document.getElementById('live-view-img');
    img.onerror = null;
    img.src = '';
    state.streaming = false;
    pxcm._setIdle();
  },

  _setActive() {
    document.getElementById('live-stream-idle').classList.add('hidden');
    document.getElementById('live-view-img').classList.remove('hidden');
    document.getElementById('live-stream-bar').classList.remove('hidden');
  },

  _setIdle() {
    document.getElementById('live-stream-idle').classList.remove('hidden');
    document.getElementById('live-view-img').classList.add('hidden');
    document.getElementById('live-stream-bar').classList.add('hidden');
    state.streaming = false;
  },

  async capture() {
    if (state.isLoading || state.activeCameraId === null) return;
    const board = pxcm._activeBoard();
    if (!board) { showError('Select a calibration board first.'); return; }

    clearError();
    state.isLoading = true;
    const btn = document.getElementById('btn-pxcm-capture');
    btn.disabled = true;

    pxcm._showSpinner();

    try {
      const data = await apiFetch('POST', '/api/pxcm/measure', {
        camera_id: state.activeCameraId,
        board_cols: board.board_cols,
        board_rows: board.board_rows,
        square_mm: board.square_mm,
        marker_mm: board.marker_mm,
        aruco_dict: board.aruco_dict,
      });
      pxcm._showResult(data);
    } catch (e) {
      pxcm._hideResult();
      showError('Measurement failed: ' + e.message);
    } finally {
      state.isLoading = false;
      btn.disabled = false;
    }
  },

  _showSpinner() {
    const card = document.getElementById('pxcm-result-card');
    const body = document.getElementById('pxcm-result-body');
    card.classList.remove('hidden');
    body.innerHTML = `
      <div class="pxcm-spinner-box">
        <div class="pxcm-spinner"></div>
        <span>Capturing full-resolution frame and detecting board…</span>
      </div>`;
  },

  _showResult(data) {
    const body = document.getElementById('pxcm-result-body');
    body.innerHTML = `
      <div class="pxcm-headline">${data.px_per_cm.toFixed(2)}<small>px / cm</small></div>
      <table class="pxcm-stats">
        <tr><td>Pixels per mm</td><td>${data.px_per_mm.toFixed(3)}</td></tr>
        <tr><td>Sample std. dev (px/mm)</td><td>±${data.std_px_per_mm.toFixed(4)}</td></tr>
        <tr><td>Corners detected</td><td>${data.corners_detected}</td></tr>
        <tr><td>Neighbour pairs used</td><td>${data.pairs_used}</td></tr>
        <tr><td>Image size</td><td>${data.image_size[0]} × ${data.image_size[1]}</td></tr>
        <tr><td>Camera</td><td>${data.camera_id}</td></tr>
      </table>
      ${data.preview_jpeg_base64
        ? `<img class="pxcm-preview" src="data:image/jpeg;base64,${data.preview_jpeg_base64}" alt="Detected board overlay">`
        : ''}
    `;
  },

  _hideResult() {
    document.getElementById('pxcm-result-card').classList.add('hidden');
  },

  dismissResult() {
    pxcm._hideResult();
  },
};

// ── Init ───────────────────────────────────────────────────────────────────
async function init() {
  clearError();

  try {
    const cams = await apiFetch('GET', '/api/system/cameras');
    state.cameras = Array.isArray(cams) ? cams : [];
  } catch (_) {
    state.cameras = [];
  }

  document.getElementById('no-camera-message').classList.toggle('hidden', state.cameras.length > 0);
  document.getElementById('pxcm-main').classList.toggle('hidden', state.cameras.length === 0);

  const camSel = document.getElementById('pxcm-camera-select');
  camSel.innerHTML =
    '<option value="" selected>Select a camera…</option>' +
    state.cameras.map(c =>
      `<option value="${c.camera_id}">Camera ${c.camera_id}${c.model ? ' — ' + c.model : ''}</option>`
    ).join('');
  state.activeCameraId = null;

  try {
    const boards = await apiFetch('GET', '/api/pxcm/boards');
    state.boards = Array.isArray(boards) ? boards : [];
  } catch (_) {
    state.boards = [];
  }

  const boardSel = document.getElementById('pxcm-board-select');
  boardSel.innerHTML = state.boards.map(b => `<option value="${b.id}">${b.label}</option>`).join('');
  if (state.boards.length > 0) {
    state.activeBoardId = state.boards[0].id;
    boardSel.value = state.activeBoardId;
  }
  pxcm._renderBoardDetails();
}

document.addEventListener('DOMContentLoaded', init);
