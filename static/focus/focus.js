'use strict';

const state = {
  cameras: [],
  activeCameraId: 0,
  fps: 2,
  streaming: false,
};

function showError(msg) {
  document.getElementById('error-text').textContent = msg;
  document.getElementById('error-banner').classList.remove('hidden');
}
function clearError() {
  document.getElementById('error-banner').classList.add('hidden');
}

async function apiFetch(path) {
  const res = await fetch(path);
  if (!res.ok) {
    let msg = `HTTP ${res.status}`;
    try { const j = await res.json(); msg = j.error || msg; } catch (_) {}
    throw new Error(msg);
  }
  return res.json().catch(() => null);
}

// ── Stream ─────────────────────────────────────────────────────────────
function startStream() {
  const img = document.getElementById('focus-stream-img');
  const placeholder = document.getElementById('stream-placeholder');

  img.src = '';
  img.onload = () => { placeholder.style.display = 'none'; };
  img.src = `/rpi/mindvision/calibration/stream?camera_id=${state.activeCameraId}&fps=${state.fps}&charuco=0`;
  state.streaming = true;
}

function stopStream() {
  const img = document.getElementById('focus-stream-img');
  img.src = '';
  state.streaming = false;
}

// ── FPS ────────────────────────────────────────────────────────────────
function setFps(fps) {
  state.fps = fps;
  document.querySelectorAll('.fps-btn').forEach(b => {
    b.classList.toggle('active', parseInt(b.dataset.fps, 10) === fps);
  });
  if (state.streaming) startStream();
}

// ── Camera switching ───────────────────────────────────────────────────
function switchCamera(camId) {
  state.activeCameraId = camId;
  document.querySelectorAll('.cam-btn').forEach(b => {
    b.classList.toggle('active', parseInt(b.dataset.camId, 10) === camId);
  });
  document.getElementById('focus-title').textContent = `Focus Stream — Camera ${camId}`;

  const backLink = document.getElementById('back-link');
  backLink.href = `/calibrate?camera=${camId}`;

  startStream();
}

function _buildCameraButtons() {
  const control = document.getElementById('camera-control');
  const container = document.getElementById('cam-btns');

  if (state.cameras.length <= 1) {
    control.classList.add('hidden');
    return;
  }

  control.classList.remove('hidden');
  container.innerHTML = state.cameras.map(c => `
    <button class="fps-btn cam-btn${c.camera_id === state.activeCameraId ? ' active' : ''}"
            data-cam-id="${c.camera_id}"
            onclick="switchCamera(${c.camera_id})">
      ${c.camera_id}
    </button>`
  ).join('');
}

// ── Init ───────────────────────────────────────────────────────────────
async function init() {
  clearError();

  try {
    const cams = await apiFetch('/rpi/mindvision/cameras');
    state.cameras = Array.isArray(cams) ? cams : [];
  } catch (_) {
    state.cameras = [];
  }

  const params = new URLSearchParams(window.location.search);
  const camParam = params.get('camera');
  if (camParam !== null) {
    const id = parseInt(camParam, 10);
    if (state.cameras.find(c => c.camera_id === id)) {
      state.activeCameraId = id;
    }
  } else if (state.cameras.length > 0) {
    state.activeCameraId = state.cameras[0].camera_id;
  }

  document.getElementById('focus-title').textContent =
    `Focus Stream — Camera ${state.activeCameraId}`;
  document.getElementById('back-link').href =
    `/calibrate?camera=${state.activeCameraId}`;

  _buildCameraButtons();
  startStream();
}

window.addEventListener('beforeunload', stopStream);
document.addEventListener('DOMContentLoaded', init);
