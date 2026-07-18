'use strict';

// ── State ──────────────────────────────────────────────────────────────────
const state = {
  cameras: [],
  currentView: 'dashboard',
  currentWizard: null,
  currentStep: 0,
  activeCameraId: 0,
  activeTab: 'camera',   // 'camera' | 'stitch'
  isLoading: false,

  // Lens wizard
  lensBufferedFrames: 0,

};

// ── API ────────────────────────────────────────────────────────────────────
async function apiFetch(method, path, body = null, { allow404 = false } = {}) {
  const opts = {
    method,
    headers: { 'Content-Type': 'application/json' },
  };
  if (body !== null) opts.body = JSON.stringify(body);
  const res = await fetch(path, opts);
  if (!res.ok) {
    if (allow404 && res.status === 404) {
      return res.json().catch(() => null);
    }
    let msg = `HTTP ${res.status}`;
    try {
      const j = await res.json();
      msg = j.error || j.message || msg;
    } catch (_) {}
    throw new Error(msg);
  }
  const ct = res.headers.get('content-type') || '';
  if (ct.includes('application/json')) return res.json();
  if (ct.includes('image/')) return res.blob();
  return res.text();
}

// ── Stream management ──────────────────────────────────────────────────────
function setStream(imgEl, url) {
  imgEl.onerror = async () => {
    imgEl.onerror = null;
    let msg = 'Stream unavailable.';
    try {
      const r = await fetch(url);
      if (r.status === 409) msg = 'Camera is in Hardware Trigger mode. Disable it on the main page to stream.';
    } catch (_) {}
    showError(msg);
  };
  imgEl.src = url;
}
function clearStream(imgEl) { imgEl.onerror = null; imgEl.src = ''; }

// ── Error display ──────────────────────────────────────────────────────────
function showError(msg) {
  document.getElementById('error-text').textContent = msg;
  document.getElementById('error-banner').classList.remove('hidden');
}
function clearError() {
  document.getElementById('error-banner').classList.add('hidden');
}

// ── Navigation ─────────────────────────────────────────────────────────────
function showView(viewId) {
  document.querySelectorAll('.view').forEach(v => v.classList.add('hidden'));
  document.getElementById(viewId).classList.remove('hidden');
  state.currentView = viewId;
}

function showStep(stepIndex) {
  const viewId = 'view-' + state.currentWizard;
  document.querySelectorAll(`#${viewId} .step`).forEach((s, i) => {
    s.classList.toggle('hidden', i !== stepIndex);
  });
  state.currentStep = stepIndex;
  _updateStepDots(state.currentWizard, stepIndex);
}

function _updateStepDots(wizardName, activeIndex) {
  const container = document.getElementById(`${wizardName}-step-dots`);
  if (!container) return;
  container.querySelectorAll('.step-dot').forEach((dot, i) => {
    dot.classList.toggle('active', i === activeIndex);
    dot.classList.toggle('done', i < activeIndex);
  });
}

function _buildStepDots(wizardName, count) {
  const container = document.getElementById(`${wizardName}-step-dots`);
  if (!container) return;
  container.innerHTML = '';
  for (let i = 0; i < count; i++) {
    const d = document.createElement('div');
    d.className = 'step-dot' + (i === 0 ? ' active' : '');
    container.appendChild(d);
  }
}

function goToDashboard() {
  liveView.stopIfRunning();
  const lensImg = document.getElementById('lens-stream-img');
  if (lensImg) clearStream(lensImg);

  state.currentWizard = null;
  showView('view-dashboard');
  refreshDashboard();
}

// ── Camera Tab Bar ─────────────────────────────────────────────────────────
function _activeCamera() {
  return state.cameras.find(c => c.camera_id === state.activeCameraId);
}

function _isMindVision(cam) {
  // Unknown/missing type defaults to MindVision — matches this page's
  // original behaviour before non-MindVision cameras existed.
  return !cam || cam.type === 'mindvision';
}

function _buildCameraTabs() {
  const bar = document.getElementById('camera-tab-bar');

  document.getElementById('no-camera-message')?.classList.toggle('hidden', state.cameras.length > 0);
  document.getElementById('camera-cards-section')?.classList.toggle('hidden', state.cameras.length === 0);

  if (!bar) return;

  if (state.cameras.length === 0) {
    bar.classList.add('hidden');
    return;
  }
  bar.classList.remove('hidden');

  const tabs = state.cameras.map(c => {
    const active = state.activeTab === 'camera' && state.activeCameraId === c.camera_id;
    const label = _isMindVision(c) ? `Camera ${c.camera_id}` : `Camera ${c.camera_id} (view only)`;
    return `<button class="cam-tab${active ? ' active' : ''}"
              onclick="_switchCamera(${c.camera_id})">
              ${label}
            </button>`;
  });

  bar.innerHTML = tabs.join('');
}

function _switchCamera(camId) {
  liveView.stopIfRunning();
  const lensImg = document.getElementById('lens-stream-img');
  if (lensImg) clearStream(lensImg);

  state.activeTab = 'camera';
  state.activeCameraId = camId;
  state.currentWizard = null;

  _buildCameraTabs();
  showView('view-dashboard');

  document.getElementById('camera-cards-section').classList.remove('hidden');
  document.getElementById('stitch-section')?.classList.add('hidden');

  refreshDashboard();

  const url = new URL(window.location);
  url.searchParams.set('camera', camId);
  window.history.replaceState({}, '', url);
}

// ── Camera selector helpers ────────────────────────────────────────────────
function _populateSelect(selId, cameras, selectedId) {
  const sel = document.getElementById(selId);
  if (!sel) return;
  sel.innerHTML = cameras.map(c =>
    `<option value="${c.camera_id}" ${c.camera_id === selectedId ? 'selected' : ''}>
      Camera ${c.camera_id}${c.model ? ' — ' + c.model : ''}
    </option>`
  ).join('');
}

function _selectedCamera(selId) {
  const sel = document.getElementById(selId);
  return sel ? (parseInt(sel.value, 10) || 0) : 0;
}

// ── RMS quality helper ─────────────────────────────────────────────────────
function _rmsClass(rms) {
  if (rms === null || rms === undefined) return 'unknown';
  if (rms < 0.5) return 'success';
  if (rms < 1.0) return 'warning';
  return 'error';
}
function _rmsLabel(rms) {
  if (rms === null || rms === undefined) return '—';
  const r = parseFloat(rms).toFixed(3);
  if (rms < 0.5) return `${r} (Good)`;
  if (rms < 1.0) return `${r} (Acceptable — consider recollecting)`;
  return `${r} (Poor — recollect with more varied positions)`;
}

// ── Loading guard ──────────────────────────────────────────────────────────
function _setLoading(on) {
  state.isLoading = on;
  document.querySelectorAll('.btn').forEach(b => {
    if (on) b.dataset._wasDisabled = b.disabled ? '1' : '';
    b.disabled = on ? true : (b.dataset._wasDisabled === '1');
  });
}

// ── Dashboard ──────────────────────────────────────────────────────────────
function _setSimpleMode(simple) {
  document.getElementById('pill-wb')?.classList.toggle('hidden', simple);
  document.getElementById('lbl-focus-peak')?.classList.toggle('hidden', simple);
  document.getElementById('lbl-clip-highlight')?.classList.toggle('hidden', simple);
  document.getElementById('btn-wb-run')?.classList.toggle('hidden', simple);
  document.getElementById('camera-card-grid')?.classList.toggle('hidden', simple);
  const hint = document.getElementById('live-idle-hint');
  if (hint) {
    hint.textContent = simple
      ? 'Live view only — nothing to tune for this camera'
      : 'Adjust aperture and focus, then set white balance';
  }
}

async function refreshDashboard() {
  clearError();
  const camId = state.activeCameraId;
  const isMindVision = _isMindVision(_activeCamera());
  _setSimpleMode(!isMindVision);

  try {
    if (!isMindVision) {
      const cams = await apiFetch('GET', '/api/system/cameras').catch(() => null);
      if (Array.isArray(cams)) state.cameras = cams;
      return;
    }

    const [cameras, wbRes, lensRes] = await Promise.allSettled([
      apiFetch('GET', '/api/system/cameras'),
      apiFetch('GET', `/api/cameras/white-balance?camera_id=${camId}`),
      apiFetch('GET', '/api/lens'),
    ]);

    if (cameras.status === 'fulfilled' && Array.isArray(cameras.value)) {
      state.cameras = cameras.value;
    }

    if (state.activeTab === 'camera') {
      _renderWbCard(wbRes.status === 'fulfilled' ? wbRes.value : null);
      _renderLensCard(lensRes.status === 'fulfilled' ? lensRes.value : null, camId);
    }

  } catch (e) {
    showError('Failed to load status: ' + e.message);
  }
}


function _renderWbCard(data) {
  const pill = document.getElementById('pill-wb');
  if (!pill) return;
  if (!data || !data.calibrated) {
    pill.className = 'pill pill-unknown';
    pill.textContent = 'WB Unknown';
  } else {
    pill.className = 'pill pill-green';
    pill.textContent = 'WB Set';
  }
}

function _renderLensCard(data, camId) {
  const pill = document.getElementById('pill-lens');
  const details = document.getElementById('lens-details');
  const btn = document.getElementById('btn-lens-start');

  const entry = data
    ? (data[String(camId !== undefined ? camId : state.activeCameraId)] ||
       data[camId !== undefined ? camId : state.activeCameraId])
    : null;

  if (!entry) {
    pill.className = 'pill pill-unknown'; pill.textContent = 'Unknown';
    details.innerHTML = ''; return;
  }

  pill.className = entry.calibrated ? 'pill pill-green' : 'pill pill-red';
  pill.textContent = entry.calibrated ? 'Calibrated' : 'Not calibrated';
  btn.textContent = entry.calibrated ? 'Recalibrate' : 'Calibrate';

  if (entry.calibrated) {
    details.innerHTML = `
      <table>
        <tr><td>RMS error</td><td>${_rmsLabel(entry.rms)}</td></tr>
        <tr><td>Frames used</td><td>${entry.frames_used}</td></tr>
        ${entry.buffered_frames ? `<tr><td>Buffered</td><td>${entry.buffered_frames} frames</td></tr>` : ''}
      </table>`;
  } else if (entry.buffered_frames > 0) {
    details.innerHTML = `<span style="font-size:13px;color:var(--text-muted)">${entry.buffered_frames} frames buffered — ready to compute</span>`;
  } else {
    details.innerHTML = '';
  }
}

// ── Live View ──────────────────────────────────────────────────────────────
const liveView = {
  _streaming: false,

  _streamUrl(camId) {
    if (!_isMindVision(state.cameras.find(c => c.camera_id === camId))) {
      return `/rpi/stream?camera_id=${camId}`;
    }
    const focusPeak = document.getElementById('chk-focus-peak');
    const clipEl    = document.getElementById('chk-clip-highlight');
    const overlay   = focusPeak && focusPeak.checked ? 1 : 0;
    const clip      = clipEl    && clipEl.checked    ? 1 : 0;
    return `/api/cameras/calibration/stream?camera_id=${camId}&fps=2&charuco=0&show_overlay=${overlay}&clip_highlight=${clip}`;
  },

  start() {
    const img = document.getElementById('live-view-img');
    const camId = state.activeCameraId;
    const url = this._streamUrl(camId);

    img.onerror = async () => {
      img.onerror = null;
      liveView._setIdle();
      let msg = 'Stream unavailable.';
      try {
        const r = await fetch(url);
        if (r.status === 409) msg = 'Camera is in Hardware Trigger mode. Disable it in Settings to stream.';
      } catch (_) {}
      showError(msg);
    };
    img.src = url;
    this._streaming = true;
    this._setActive();
  },

  stop() {
    const img = document.getElementById('live-view-img');
    img.onerror = null;
    img.src = '';
    this._streaming = false;
    this._setIdle();
  },

  stopIfRunning() {
    if (this._streaming) this.stop();
  },

  restartStream() {
    if (!this._streaming) return;
    const img = document.getElementById('live-view-img');
    img.onerror = null;
    img.src = this._streamUrl(state.activeCameraId);
  },

  fullscreen() {
    const wrap = document.getElementById('live-stream-wrap');
    if (!wrap) return;
    if (wrap.requestFullscreen) wrap.requestFullscreen();
    else if (wrap.webkitRequestFullscreen) wrap.webkitRequestFullscreen();
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
    this._streaming = false;
  },
};

// ── White Balance (inline) ─────────────────────────────────────────────────
const wb = {
  async run() {
    if (state.isLoading) return;
    const camId = state.activeCameraId;
    const btn = document.getElementById('btn-wb-run');
    const result = document.getElementById('wb-inline-result');
    _setLoading(true);
    clearError();
    result.classList.add('hidden');
    try {
      const data = await apiFetch('POST', `/api/cameras/calibrate-wb?camera_id=${camId}`);
      const r = data.r_gain, g = data.g_gain, b = data.b_gain;
      const warn = [r, g, b].some(v => v < 30 || v > 400);
      result.className = `wb-inline-result ${warn ? 'wb-warn' : 'wb-ok'}`;
      result.textContent = warn
        ? '⚠ Done — gains look unusual. Try a more neutral surface.'
        : '✓ White balance set';
      result.classList.remove('hidden');
      _renderWbCard({ calibrated: true, r_gain: r, g_gain: g, b_gain: b });
    } catch (e) {
      showError('White balance failed: ' + e.message);
    } finally {
      _setLoading(false);
    }
  },
};

// ── Lens Distortion Wizard ─────────────────────────────────────────────────
const LENS_POSITIONS = [
  [0.50, 0.50],  // centre
  [0.25, 0.25],  // top-left
  [0.75, 0.25],  // top-right
  [0.25, 0.75],  // bottom-left
  [0.75, 0.75],  // bottom-right
  [0.50, 0.20],  // top-centre
  [0.50, 0.80],  // bottom-centre
  [0.20, 0.50],  // mid-left
  [0.80, 0.50],  // mid-right
  [0.38, 0.38],  // inner top-left
  [0.62, 0.62],  // inner bottom-right
  [0.38, 0.62],  // inner bottom-left
  [0.62, 0.38],  // inner top-right
  [0.30, 0.50],  // left of centre
  [0.70, 0.50],  // right of centre
];

const lens = {
  guidePct: 40,
  framesMin: 10,
  framesTarget: 15,
  _posIdx: 0,
  async start() {
    clearError();
    liveView.stopIfRunning();
    state.currentWizard = 'lens';
    _buildStepDots('lens', 4);
    const lbl = document.getElementById('lens-cam-label');
    if (lbl) lbl.textContent = `Camera ${state.activeCameraId}`;
    showView('view-lens');
    showStep(0);
    await lens._loadStatus();
  },

  async _loadStatus() {
    const camId = state.activeCameraId;
    const box = document.getElementById('lens-current-status');
    try {
      const data = await apiFetch('GET', '/api/lens');
      const entry = data[String(camId)] || data[camId];
      if (!entry) { box.innerHTML = 'No status available.'; return; }
      if (entry.frames_min != null)    lens.framesMin    = entry.frames_min;
      if (entry.frames_target != null) lens.framesTarget = entry.frames_target;
      box.innerHTML = `
        <table>
          <tr><td>Camera ${camId}</td><td>${entry.calibrated
            ? `<span class="pill pill-green">Calibrated — RMS ${parseFloat(entry.rms).toFixed(3)}</span>`
            : '<span class="pill pill-red">Not calibrated</span>'}</td></tr>
          <tr><td>Buffered frames</td><td>${entry.buffered_frames}</td></tr>
          ${entry.calibrated_at ? `<tr><td>Calibrated at</td><td>${new Date(entry.calibrated_at).toLocaleString()}</td></tr>` : ''}
        </table>`;
      state.lensBufferedFrames = entry.buffered_frames;
      const clearBtn = document.getElementById('btn-lens-clear');
      clearBtn.classList.toggle('hidden', !entry.calibrated && entry.buffered_frames === 0);
    } catch (e) {
      box.innerHTML = '<span style="color:var(--text-muted)">Could not load status.</span>';
    }
  },

  async confirmClear() {
    const camId = state.activeCameraId;
    if (!confirm(`Delete lens calibration data for Camera ${camId}? This cannot be undone.`)) return;
    _setLoading(true);
    try {
      await apiFetch('DELETE', '/api/lens', { camera_id: camId });
      state.lensBufferedFrames = 0;
      await lens._loadStatus();
    } catch (e) {
      showError('Failed to clear lens data: ' + e.message);
    } finally {
      _setLoading(false);
    }
  },

  _streamUrl(camId) {
    if (lens._posIdx >= LENS_POSITIONS.length) {
      return `/api/lens/stream?camera_id=${camId}&fps=2&max_width=960&guide_pct=0`;
    }
    const [cx, cy] = LENS_POSITIONS[lens._posIdx];
    return `/api/lens/stream?camera_id=${camId}&fps=2&max_width=960&guide_pct=${lens.guidePct}&cx=${cx}&cy=${cy}`;
  },

  startCollecting() {
    lens._posIdx = 0;
    const camId = state.activeCameraId;
    showStep(1);
    const img = document.getElementById('lens-stream-img');
    setStream(img, lens._streamUrl(camId));
    lens._updateProgress(state.lensBufferedFrames);
    lens._setUndoEnabled(state.lensBufferedFrames);
    document.getElementById('lens-collect-feedback').textContent = '';
    const skipBtn = document.getElementById('btn-lens-skip');
    if (skipBtn) skipBtn.disabled = false;

    const slider = document.getElementById('lens-guide-size');
    const sliderVal = document.getElementById('lens-guide-size-value');
    if (slider && !slider._wired) {
      slider._wired = true;
      let _debounce = null;
      slider.addEventListener('input', () => {
        lens.guidePct = parseInt(slider.value, 10);
        if (sliderVal) sliderVal.textContent = lens.guidePct + '%';
        clearTimeout(_debounce);
        _debounce = setTimeout(() => {
          const id = state.activeCameraId;
          setStream(document.getElementById('lens-stream-img'), lens._streamUrl(id));
        }, 300);
      });
    }
  },

  _updateProgress(count) {
    const min = lens.framesMin;
    const target = lens.framesTarget;
    const pct = Math.min(100, Math.round((count / target) * 100));

    const frameCount = document.getElementById('lens-frame-count');
    if (frameCount) frameCount.textContent = `${count} frame${count !== 1 ? 's' : ''} collected`;

    const frameHint = document.getElementById('lens-frame-hint');
    if (frameHint) {
      if (count >= target) frameHint.textContent = 'Ready to compute';
      else if (lens._posIdx >= LENS_POSITIONS.length) frameHint.textContent = `${target - count} more — move board freely`;
      else frameHint.textContent = `${target - count} more — follow the guide box`;
    }

    const fill = document.getElementById('lens-progress-fill');
    if (fill) {
      fill.style.width = pct + '%';
      fill.classList.toggle('ready', count >= target);
    }

    const computeBtn = document.getElementById('btn-lens-compute');
    if (computeBtn) {
      computeBtn.disabled = count < min;
      computeBtn.classList.toggle('btn-success', count >= target);
    }

    const undoBtn = document.getElementById('btn-lens-undo');
    if (undoBtn) undoBtn.disabled = count <= 0;
  },

  _setUndoEnabled(count) {
    const btn = document.getElementById('btn-lens-undo');
    if (btn) btn.disabled = count <= 0;
  },

  async collectFrame() {
    if (state.isLoading) return;
    const camId = state.activeCameraId;
    const feedback = document.getElementById('lens-collect-feedback');
    _setLoading(true);
    clearError();
    try {
      const data = await apiFetch('POST', '/api/lens/collect', { camera_id: camId });
      state.lensBufferedFrames = data.buffered_frames;
      lens._updateProgress(data.buffered_frames);
      lens._setUndoEnabled(data.buffered_frames);
      feedback.className = 'feedback-line ok';
      feedback.textContent = `✓ Frame accepted — ${data.corners_detected} corners detected`;
      lens._posIdx++;
      setStream(document.getElementById('lens-stream-img'), lens._streamUrl(camId));
    } catch (e) {
      feedback.className = 'feedback-line error';
      if (e.message.includes('Too few')) {
        feedback.textContent = '✕ Too few corners — reposition the board so more of it is visible';
      } else {
        feedback.textContent = '✕ ' + e.message;
      }
    } finally {
      _setLoading(false);
    }
  },

  skipPosition() {
    lens._posIdx++;
    const camId = state.activeCameraId;
    setStream(document.getElementById('lens-stream-img'), lens._streamUrl(camId));
    const skipBtn = document.getElementById('btn-lens-skip');
    if (skipBtn) skipBtn.disabled = lens._posIdx >= LENS_POSITIONS.length;
  },

  async undoLast() {
    if (state.isLoading) return;
    const camId = state.activeCameraId;
    const feedback = document.getElementById('lens-collect-feedback');
    _setLoading(true);
    clearError();
    try {
      const data = await apiFetch('DELETE', '/api/lens/last', { camera_id: camId });
      state.lensBufferedFrames = data.buffered_frames;
      lens._updateProgress(data.buffered_frames);
      lens._setUndoEnabled(data.buffered_frames);
      lens._posIdx = Math.max(0, lens._posIdx - 1);
      setStream(document.getElementById('lens-stream-img'), lens._streamUrl(camId));
      feedback.className = 'feedback-line';
      feedback.textContent = `↩ Last frame removed — ${data.buffered_frames} frame${data.buffered_frames !== 1 ? 's' : ''} remaining`;
    } catch (e) {
      showError('Undo failed: ' + e.message);
    } finally {
      _setLoading(false);
    }
  },

  async compute() {
    if (state.isLoading) return;
    const camId = state.activeCameraId;
    clearStream(document.getElementById('lens-stream-img'));
    showStep(2);
    clearError();
    try {
      const data = await apiFetch('POST', '/api/lens/compute', { camera_id: camId });
      lens._showResult(camId, data);
      showStep(3);
    } catch (e) {
      showError('Compute failed: ' + e.message);
      const img = document.getElementById('lens-stream-img');
      setStream(img, lens._streamUrl(camId));
      showStep(1);
    }
  },

  _showResult(camId, data) {
    const rms = data.rms;
    const cls = _rmsClass(rms);
    document.getElementById('lens-result').innerHTML = `
      <div class="result-block ${cls}">
        <h4>${cls === 'success' ? '✓ Calibration successful' : cls === 'warning' ? '⚠ Calibration done — consider improving' : '✕ Poor calibration — recollect'}</h4>
        <table>
          <tr><td>Camera</td><td>${camId}</td></tr>
          <tr><td>RMS error</td><td>${_rmsLabel(rms)}</td></tr>
          <tr><td>Frames used</td><td>${data.frames_used}</td></tr>
        </table>
      </div>`;
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

  const params = new URLSearchParams(window.location.search);
  const camParam = params.get('camera');
  if (camParam !== null) {
    const requestedId = parseInt(camParam, 10);
    const valid = state.cameras.find(c => c.camera_id === requestedId);
    if (valid) state.activeCameraId = requestedId;
  } else if (state.cameras.length > 0) {
    state.activeCameraId = state.cameras[0].camera_id;
  }

  state.activeTab = 'camera';
  _buildCameraTabs();
  showView('view-dashboard');
  await refreshDashboard();
}

document.addEventListener('DOMContentLoaded', init);
