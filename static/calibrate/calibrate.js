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
      // Return the parsed JSON anyway — caller handles the "calibrated: false" shape
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
function setStream(imgEl, url) { imgEl.src = url; }
function clearStream(imgEl) { imgEl.src = ''; }

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

  const onDashboard = viewId === 'view-dashboard';
  document.getElementById('btn-back').classList.toggle('hidden', onDashboard);
  document.getElementById('header-title').textContent = onDashboard ? 'Calibration' : '';
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
  // Stop any active streams before leaving
  const focusImg = document.getElementById('focus-stream-img');
  const lensImg = document.getElementById('lens-stream-img');
  if (focusImg) clearStream(focusImg);
  if (lensImg) clearStream(lensImg);

  state.currentWizard = null;
  showView('view-dashboard');
  refreshDashboard();
}

// ── Camera Tab Bar ─────────────────────────────────────────────────────────
function _buildCameraTabs() {
  const bar = document.getElementById('camera-tab-bar');
  if (!bar) return;

  if (state.cameras.length === 0) {
    bar.classList.add('hidden');
    return;
  }
  bar.classList.remove('hidden');

  const tabs = state.cameras.map(c => {
    const active = state.activeTab === 'camera' && state.activeCameraId === c.camera_id;
    return `<button class="cam-tab${active ? ' active' : ''}"
              onclick="_switchCamera(${c.camera_id})">
              Camera ${c.camera_id}
            </button>`;
  });

  bar.innerHTML = tabs.join('');
}

function _switchCamera(camId) {
  // Stop any active streams before switching
  const focusImg = document.getElementById('focus-stream-img');
  const lensImg = document.getElementById('lens-stream-img');
  if (focusImg) clearStream(focusImg);
  if (lensImg) clearStream(lensImg);

  state.activeTab = 'camera';
  state.activeCameraId = camId;
  state.currentWizard = null;

  _buildCameraTabs();
  showView('view-dashboard');

  // Show camera section, hide stitch section
  document.getElementById('camera-cards-section').classList.remove('hidden');
  document.getElementById('stitch-section')?.classList.add('hidden');

  refreshDashboard();

  // Update URL param without full navigation
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
async function refreshDashboard() {
  clearError();
  const camId = state.activeCameraId;
  try {
    const [cameras, wbRes, lensRes] = await Promise.allSettled([
      apiFetch('GET', '/rpi/mindvision/cameras'),
      apiFetch('GET', `/rpi/mindvision/white-balance?camera_id=${camId}`),
      apiFetch('GET', '/rpi/mindvision/lens'),
    ]);

    if (cameras.status === 'fulfilled' && Array.isArray(cameras.value)) {
      state.cameras = cameras.value;
      _updateCameraBadge(state.cameras.length);
    }

    // Only update camera cards if we're in camera mode
    if (state.activeTab === 'camera') {
      _renderWbCard(wbRes.status === 'fulfilled' ? wbRes.value : null);
      _renderFocusCard(state.cameras);
      _renderLensCard(lensRes.status === 'fulfilled' ? lensRes.value : null, camId);
    }

    // Update dashboard subtitle
    const subtitle = document.getElementById('dashboard-subtitle');
    if (subtitle && state.activeTab === 'camera') {
      subtitle.textContent = `Camera ${camId} — select a calibration to run or check status.`;
    }
  } catch (e) {
    showError('Failed to load status: ' + e.message);
  }
}

function _updateCameraBadge(count) {
  const badge = document.getElementById('camera-count-badge');
  badge.textContent = `${count} camera${count !== 1 ? 's' : ''} connected`;
  badge.classList.toggle('hidden', count === 0);
}

function _renderWbCard(data) {
  const pill = document.getElementById('pill-wb');
  const details = document.getElementById('wb-details');
  const btn = document.getElementById('btn-wb-start');

  if (!data || !data.calibrated) {
    pill.className = 'pill pill-unknown'; pill.textContent = 'Not set';
    details.innerHTML = '';
    btn.textContent = 'Calibrate';
    return;
  }

  pill.className = 'pill pill-green';
  pill.textContent = 'Calibrated';
  btn.textContent = 'Recalibrate';
  details.innerHTML = `
    <table>
      <tr><td>R gain</td><td>${data.r_gain}</td></tr>
      <tr><td>G gain</td><td>${data.g_gain}</td></tr>
      <tr><td>B gain</td><td>${data.b_gain}</td></tr>
    </table>`;
}

function _renderFocusCard(cameras) {
  const det = document.getElementById('focus-details');
  if (cameras.length === 0) {
    det.textContent = 'No cameras detected.'; return;
  }
  det.textContent = `${cameras.length} camera${cameras.length !== 1 ? 's' : ''} available.`;
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

// ── White Balance Wizard ───────────────────────────────────────────────────
const wb = {
  async start() {
    clearError();
    state.currentWizard = 'wb';
    _buildStepDots('wb', 2);
    const lbl = document.getElementById('wb-cam-label');
    if (lbl) lbl.textContent = `Camera ${state.activeCameraId}`;
    showView('view-wb');
    showStep(0);
    await wb._loadCurrentGains();
  },

  async _loadCurrentGains() {
    const camId = state.activeCameraId;
    const box = document.getElementById('wb-current-gains');
    try {
      const data = await apiFetch('GET', `/rpi/mindvision/white-balance?camera_id=${camId}`);
      if (data && data.calibrated && data.r_gain !== undefined) {
        box.classList.remove('hidden');
        box.innerHTML = `<strong>Current gains</strong><br>R: ${data.r_gain} &nbsp; G: ${data.g_gain} &nbsp; B: ${data.b_gain}`;
      } else {
        box.classList.add('hidden');
      }
    } catch (_) {
      box.classList.add('hidden');
    }
  },

  async run() {
    if (state.isLoading) return;
    const camId = state.activeCameraId;
    _setLoading(true);
    clearError();
    try {
      const data = await apiFetch('POST', `/rpi/mindvision/calibrate-wb?camera_id=${camId}`);
      wb._showResult(data);
      showStep(1);
    } catch (e) {
      showError('White balance calibration failed: ' + e.message);
    } finally {
      _setLoading(false);
    }
  },

  _showResult(data) {
    const r = data.r_gain, g = data.g_gain, b = data.b_gain;
    const unreasonable = (v) => v < 30 || v > 400;
    const warn = unreasonable(r) || unreasonable(g) || unreasonable(b);
    document.getElementById('wb-result').innerHTML = `
      <div class="result-block ${warn ? 'warning' : 'success'}">
        <h4>${warn ? '⚠ Calibration done — gains look unusual' : '✓ White balance calibrated'}</h4>
        <table>
          <tr><td>R gain</td><td>${r}</td></tr>
          <tr><td>G gain</td><td>${g}</td></tr>
          <tr><td>B gain</td><td>${b}</td></tr>
        </table>
        ${warn ? '<p style="margin-top:8px;font-size:13px;color:#92400e">Gains outside 30–400 range. Try again with a more neutral white/grey surface.</p>' : ''}
      </div>`;
  },
};

// ── Focus — navigate to dedicated page ────────────────────────────────────
// NOTE: "focus" is a built-in on every HTMLElement, so it wins the scope
// chain in inline onclick handlers. Using a plain function declaration instead
// so it lands on window and is not shadowed.
function openFocusStream() {
  window.location.href = `/focus?camera=${state.activeCameraId}`;
}

// ── Lens Distortion Wizard ─────────────────────────────────────────────────
// Positions the guide box visits in sequence (cx, cy as 0–1 fractions of frame).
// Spread across the frame so each collected frame covers a different area.
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
      const data = await apiFetch('GET', '/rpi/mindvision/lens');
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
      await apiFetch('DELETE', '/rpi/mindvision/lens', { camera_id: camId });
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
      // All guided positions done — free mode: no guide box, collect anywhere.
      return `/rpi/mindvision/lens/stream?camera_id=${camId}&fps=2&max_width=960&guide_pct=0`;
    }
    const [cx, cy] = LENS_POSITIONS[lens._posIdx];
    return `/rpi/mindvision/lens/stream?camera_id=${camId}&fps=2&max_width=960&guide_pct=${lens.guidePct}&cx=${cx}&cy=${cy}`;
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

    // Wire guide-size slider (only once per page load).
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
      const data = await apiFetch('POST', '/rpi/mindvision/lens/collect', { camera_id: camId });
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
      const data = await apiFetch('DELETE', '/rpi/mindvision/lens/last', { camera_id: camId });
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
      const data = await apiFetch('POST', '/rpi/mindvision/lens/compute', { camera_id: camId });
      lens._showResult(camId, data);
      showStep(3);
    } catch (e) {
      showError('Compute failed: ' + e.message);
      // Go back to collection step so user can retry
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
    const cams = await apiFetch('GET', '/rpi/mindvision/cameras');
    state.cameras = Array.isArray(cams) ? cams : [];
  } catch (_) {
    state.cameras = [];
  }

  // Read ?camera=N query param from URL
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
