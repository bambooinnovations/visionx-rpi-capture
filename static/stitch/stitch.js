'use strict';

const state = {
  cameras: [],
  activeCameraIds: [],
  cameraOrder: [],       // left→right display and stitch order
  overlapPct: 10,
  showOverlay: true,
  fps: 2,
  layoutSaveTimer: null,
  activeStreamTab: 'cameras',   // 'cameras' | 'stitch'
  stitchReady: false,           // true when stitch calibration is complete

  // Calibration wizard
  calPassPlan: [],
  calPassIndex: 0,
  calPreviewBlobUrl: null,
  calLoading: false,
};

// ── Error ──────────────────────────────────────────────────────────────
function showError(msg) {
  document.getElementById('error-text').textContent = msg;
  document.getElementById('error-banner').classList.remove('hidden');
}
function clearError() {
  document.getElementById('error-banner').classList.add('hidden');
}

// ── API ────────────────────────────────────────────────────────────────
async function apiFetch(path, { method = 'GET', body = null, allow404 = false } = {}) {
  const opts = { method, headers: { 'Content-Type': 'application/json' } };
  if (body !== null) opts.body = JSON.stringify(body);
  const res = await fetch(path, opts);
  if (!res.ok) {
    if (allow404 && res.status === 404) return res.json().catch(() => null);
    let msg = `HTTP ${res.status}`;
    try { const j = await res.json(); msg = j.error || msg; } catch (_) {}
    throw new Error(msg);
  }
  const ct = res.headers.get('content-type') || '';
  if (ct.includes('image/')) return res.blob();
  return res.json().catch(() => null);
}

// ── Stream tabs ────────────────────────────────────────────────────────
function switchStreamTab(tab) {
  if (tab === state.activeStreamTab) return;
  state.activeStreamTab = tab;

  const isCamera = tab === 'cameras';
  document.getElementById('tab-btn-cameras').classList.toggle('active', isCamera);
  document.getElementById('tab-btn-stitch').classList.toggle('active', !isCamera);
  document.getElementById('stream-panel-cameras').classList.toggle('hidden', !isCamera);
  document.getElementById('stream-panel-stitch').classList.toggle('hidden', isCamera);

  if (isCamera) {
    _stopStitchViewStream();
    buildStreams();
  } else {
    stopAllStreams();
    _startStitchViewStream();
  }
}

function _startStitchViewStream() {
  const img = document.getElementById('stitch-view-img');
  const loading = document.getElementById('stitch-view-loading');

  if (!state.stitchReady) {
    loading.innerHTML = '<span style="color:#64748b;font-size:13px">Not calibrated — run the calibration wizard first.</span>';
    loading.classList.remove('hidden');
    img.src = '';
    return;
  }

  loading.innerHTML = '<div class="stream-spinner"></div><span>Connecting…</span>';
  loading.classList.remove('hidden');
  img.onload = () => loading.classList.add('hidden');
  img.onerror = () => {
    loading.innerHTML = '<span class="stream-load-error">Stream error</span>';
  };
  img.src = `/rpi/mindvision/stitch/stream?max_width=960&fps=${state.fps}&quality=75`;
}

function _stopStitchViewStream() {
  const img = document.getElementById('stitch-view-img');
  img.onload = null;
  img.onerror = null;
  img.src = '';
  document.getElementById('stitch-view-loading').classList.remove('hidden');
  document.getElementById('stitch-view-loading').innerHTML =
    '<div class="stream-spinner"></div><span>Connecting…</span>';
}

// ── Stream management ──────────────────────────────────────────────────
function stopAllStreams() {
  document.querySelectorAll('.stream-panel .stream-img').forEach(img => {
    img.src = '';
  });
}

// ── Build stream panels ────────────────────────────────────────────────
function buildStreams() {
  const row = document.getElementById('stream-row');
  const statusEl = document.getElementById('stream-status');

  stopAllStreams();
  row.innerHTML = '';

  // Show active cameras in the configured left→right order.
  const activeSet = new Set(state.activeCameraIds);
  const ids = state.cameraOrder.length
    ? state.cameraOrder.filter(id => activeSet.has(id))
    : state.activeCameraIds;
  if (ids.length === 0) {
    row.innerHTML = '<div class="stream-empty">Select cameras below to start streams.</div>';
    statusEl.textContent = 'No cameras selected';
    return;
  }

  statusEl.textContent = `Streaming ${ids.length} camera${ids.length !== 1 ? 's' : ''}`;

  ids.forEach((camId, idx) => {
    const panel = document.createElement('div');
    panel.className = 'stream-panel';
    panel.dataset.camId = camId;

    const label = document.createElement('div');
    label.className = 'cam-label';
    label.textContent = `Camera ${camId}`;
    panel.appendChild(label);

    const loading = document.createElement('div');
    loading.className = 'stream-loading';
    loading.innerHTML = '<div class="stream-spinner"></div><span>Connecting…</span>';
    panel.appendChild(loading);

    const img = document.createElement('img');
    img.className = 'stream-img';
    img.alt = `Camera ${camId} stream`;
    img.addEventListener('load', () => loading.classList.add('hidden'), { once: true });
    img.addEventListener('error', () => {
      loading.innerHTML = '<span class="stream-load-error">No signal</span>';
    }, { once: true });
    panel.appendChild(img);

    if (idx < ids.length - 1) {
      const right = document.createElement('div');
      right.className = 'overlap-overlay right';
      right.innerHTML = '<span class="overlap-label">overlap</span>';
      panel.appendChild(right);
    }
    if (idx > 0) {
      const left = document.createElement('div');
      left.className = 'overlap-overlay left';
      left.innerHTML = '<span class="overlap-label">overlap</span>';
      panel.appendChild(left);
    }

    row.appendChild(panel);

    setTimeout(() => {
      img.src = `/rpi/stream?camera_id=${camId}&fps=${state.fps}`;
    }, 10 + idx * 20);
  });

  updateOverlay();
}

// ── Update overlay width/visibility ────────────────────────────────────
function updateOverlay() {
  state.showOverlay = document.getElementById('overlay-toggle').checked;
  const pct = state.overlapPct;
  document.querySelectorAll('.overlap-overlay').forEach(el => {
    el.style.width = state.showOverlay ? pct + '%' : '0';
  });
}

function updateOverlap(val) {
  state.overlapPct = parseInt(val, 10) || 10;
  document.getElementById('overlap-val').textContent = state.overlapPct + '%';
  updateOverlay();
}

// ── FPS ────────────────────────────────────────────────────────────────
function setFps(fps) {
  state.fps = fps;
  document.querySelectorAll('.fps-btn').forEach(b => {
    b.classList.toggle('active', parseInt(b.textContent, 10) === fps);
  });
  buildStreams();
}

// ── Camera checkboxes ──────────────────────────────────────────────────
function buildCameraChecks() {
  const container = document.getElementById('camera-checks');
  if (state.cameras.length === 0) {
    container.innerHTML = '<span class="text-muted">No cameras detected.</span>';
    return;
  }

  container.innerHTML = state.cameras.map((c, idx) => `
    <label class="cam-check-label">
      <input type="checkbox" value="${c.camera_id}" ${idx < 2 ? 'checked' : ''}
             onchange="onCameraCheck(this)">
      Camera ${c.camera_id}
      ${c.model ? `<span style="font-size:12px;color:var(--text-muted)">${c.model}</span>` : ''}
    </label>`
  ).join('');

  // Initialise activeCameraIds to the first 2 cameras.
  state.activeCameraIds = state.cameras.slice(0, 2).map(c => c.camera_id);
}

function onCameraCheck(changedCb) {
  const allCbs = [...document.querySelectorAll('#camera-checks input[type=checkbox]')];
  const checked = allCbs.filter(cb => cb.checked);

  // Enforce max 2: uncheck the one checked longest ago (first checked, not changedCb).
  if (checked.length > 2) {
    const toUncheck = checked.find(cb => cb !== changedCb);
    if (toUncheck) toUncheck.checked = false;
  }

  const checkedIds = new Set(
    allCbs.filter(cb => cb.checked).map(cb => parseInt(cb.value, 10))
  );
  state.activeCameraIds = state.cameras
    .map(c => c.camera_id)
    .filter(id => checkedIds.has(id));
  buildStreams();
}

// ── Camera layout (drag-to-reorder) ───────────────────────────────────
function reconcileCameraOrder() {
  const allIds = state.cameras.map(c => c.camera_id);
  const existing = state.cameraOrder.filter(id => allIds.includes(id));
  const added = allIds.filter(id => !existing.includes(id)).sort((a, b) => a - b);
  state.cameraOrder = [...existing, ...added];
}

let _dragSrcIdx = null;

function buildLayoutChips() {
  const container = document.getElementById('layout-chips');
  if (state.cameraOrder.length <= 1) {
    container.innerHTML = state.cameraOrder.length === 0
      ? '<span class="text-muted">No cameras detected.</span>'
      : `<span class="layout-chip" style="cursor:default">Camera ${state.cameraOrder[0]}</span>`;
    return;
  }

  container.innerHTML = state.cameraOrder.map((camId, idx) => `
    <div class="layout-chip" draggable="true"
         data-idx="${idx}"
         ondragstart="onLayoutDragStart(event,${idx})"
         ondragover="onLayoutDragOver(event,${idx})"
         ondrop="onLayoutDrop(event,${idx})"
         ondragend="onLayoutDragEnd()">
      <span class="layout-chip-handle">⠿</span>
      Camera ${camId}
    </div>
    ${idx < state.cameraOrder.length - 1 ? '<span class="layout-arrow">→</span>' : ''}
  `).join('');
}

function onLayoutDragStart(e, idx) {
  _dragSrcIdx = idx;
  e.dataTransfer.effectAllowed = 'move';
  e.dataTransfer.setData('text/plain', idx);
  setTimeout(() => {
    const chips = document.querySelectorAll('.layout-chip');
    if (chips[idx]) chips[idx].classList.add('dragging');
  }, 0);
}

function onLayoutDragOver(e, idx) {
  e.preventDefault();
  e.dataTransfer.dropEffect = 'move';
  document.querySelectorAll('.layout-chip').forEach((c, i) => {
    c.classList.toggle('drag-over', i === idx && i !== _dragSrcIdx);
  });
}

function onLayoutDrop(e, toIdx) {
  e.preventDefault();
  if (_dragSrcIdx === null || _dragSrcIdx === toIdx) return;
  const order = [...state.cameraOrder];
  const [moved] = order.splice(_dragSrcIdx, 1);
  order.splice(toIdx, 0, moved);
  state.cameraOrder = order;
  buildLayoutChips();
  buildStreams();
  scheduleLayoutSave();
}

function onLayoutDragEnd() {
  _dragSrcIdx = null;
  document.querySelectorAll('.layout-chip').forEach(c => {
    c.classList.remove('dragging', 'drag-over');
  });
}

function scheduleLayoutSave() {
  clearTimeout(state.layoutSaveTimer);
  state.layoutSaveTimer = setTimeout(saveLayout, 400);
}

async function saveLayout() {
  try {
    const res = await fetch('/rpi/mindvision/stitch/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ camera_order: state.cameraOrder }),
    });
    if (!res.ok) {
      const j = await res.json().catch(() => ({}));
      showError('Failed to save layout: ' + (j.error || `HTTP ${res.status}`));
    }
  } catch (e) {
    showError('Failed to save layout: ' + e.message);
  }
}

// ── Stitch status ──────────────────────────────────────────────────────
async function loadStitchStatus() {
  const pill = document.getElementById('pill-cal-status');
  const body = document.getElementById('cal-status-body');
  const stitchTab = document.getElementById('tab-btn-stitch');

  try {
    const data = await apiFetch('/rpi/mindvision/stitch/calibrate', { allow404: true });

    const wasReady = state.stitchReady;
    state.stitchReady = !!(data && data.ready_to_stitch);
    stitchTab.disabled = !state.stitchReady;

    if (!data || !data.calibrated) {
      pill.className = 'pill pill-red';
      pill.textContent = 'Not calibrated';
      body.textContent = 'No stitch calibration data. Run the wizard to calibrate.';
      return;
    }

    const camEntries = Object.entries(data.cameras || {});
    pill.className = state.stitchReady ? 'pill pill-green' : 'pill pill-yellow';
    pill.textContent = state.stitchReady ? 'Ready' : 'Partial';

    const canvas = data.canvas || {};
    body.innerHTML = `
      <table class="cal-status-table">
        <tr><td>Cameras calibrated</td><td>${data.cameras_calibrated.join(', ')}</td></tr>
        ${data.cameras_missing && data.cameras_missing.length
          ? `<tr><td>Cameras missing</td><td>${data.cameras_missing.join(', ')}</td></tr>` : ''}
        ${canvas.width ? `<tr><td>Canvas size</td><td>${canvas.width} × ${canvas.height} px</td></tr>` : ''}
      </table>
      ${camEntries.length ? `<div style="margin-top:8px;display:flex;gap:6px;flex-wrap:wrap">
        ${camEntries.map(([id, v]) =>
          `<span class="pill pill-green" style="font-size:11px">Cam ${id}: ${v.corners_detected} corners</span>`
        ).join('')}
      </div>` : ''}`;

    // If stitch tab is active and calibration just became ready, start the stream.
    if (state.activeStreamTab === 'stitch' && state.stitchReady && !wasReady) {
      _startStitchViewStream();
    }
  } catch (_) {
    pill.className = 'pill pill-unknown';
    pill.textContent = 'Unknown';
    body.textContent = 'Could not load stitch status.';
  }
}

// ── Calibration wizard ─────────────────────────────────────────────────

function _calShowStep(idx) {
  document.querySelectorAll('.cal-step').forEach((s, i) => {
    s.classList.toggle('hidden', i !== idx);
  });
  document.querySelectorAll('#cal-step-dots .step-dot').forEach((d, i) => {
    d.classList.toggle('active', i === idx);
    d.classList.toggle('done', i < idx);
  });
}

function _calBuildStepDots(count) {
  const container = document.getElementById('cal-step-dots');
  container.innerHTML = '';
  for (let i = 0; i < count; i++) {
    const d = document.createElement('div');
    d.className = 'step-dot' + (i === 0 ? ' active' : '');
    container.appendChild(d);
  }
}

function _calSetLoading(on) {
  state.calLoading = on;
  document.querySelectorAll('#cal-wizard-view .btn').forEach(b => {
    if (on) b.dataset._was = b.disabled ? '1' : '';
    b.disabled = on ? true : (b.dataset._was === '1');
  });
}

const calWizard = {
  start() {
    if (state.cameras.length < 2) {
      showError('Stitching requires at least 2 cameras.');
      return;
    }

    // Build pass plan: adjacent pairs from cameraOrder (always 2 cameras per pass).
    const order = state.cameraOrder.filter(id => state.cameras.some(c => c.camera_id === id));
    if (order.length < 2) {
      showError('At least 2 cameras must be in the layout order.');
      return;
    }

    state.calPassPlan = [];
    for (let i = 0; i < order.length - 1; i++) {
      state.calPassPlan.push([order[i], order[i + 1]]);
    }
    state.calPassIndex = 0;

    const setupInfo = document.getElementById('cal-setup-info');
    if (state.calPassPlan.length === 1) {
      setupInfo.innerHTML = `<p>Place the ChArUco board where <strong>both cameras can see it simultaneously</strong>. One pass covers the full setup.</p>`;
    } else {
      setupInfo.innerHTML = `
        <p>Calibration requires <strong>${state.calPassPlan.length} passes</strong> — one per adjacent camera pair.</p>
        <ul>${state.calPassPlan.map((pair, i) =>
          `<li>Pass ${i + 1}: Camera ${pair[0]} + Camera ${pair[1]}</li>`
        ).join('')}</ul>
        <p>The centre camera anchors all cameras into one coordinate space.</p>`;
    }

    _calBuildStepDots(5);
    document.getElementById('cal-status-view').classList.add('hidden');
    document.getElementById('cal-wizard-view').classList.remove('hidden');
    _calShowStep(0);
    calWizard._loadCurrentStatus();
  },

  async _loadCurrentStatus() {
    const box = document.getElementById('cal-current-status');
    try {
      const data = await apiFetch('/rpi/mindvision/stitch/calibrate', { allow404: true });
      const entries = Object.entries((data && data.cameras) || {});
      if (!data || !data.calibrated || entries.length === 0) {
        box.innerHTML = '<span style="color:var(--text-muted)">No existing calibration.</span>';
        document.getElementById('btn-cal-clear').classList.add('hidden');
      } else {
        document.getElementById('btn-cal-clear').classList.remove('hidden');
        box.innerHTML = `<table>${entries.map(([id, v]) =>
          `<tr>
            <td>Cam ${id}</td>
            <td><span class="pill pill-green" style="font-size:11px">✓ ${v.corners_detected} corners (${v.inliers} inliers)</span></td>
          </tr>`
        ).join('')}</table>`;
      }
    } catch (_) {
      box.innerHTML = '<span style="color:var(--text-muted)">Could not load status.</span>';
    }
  },

  cancel() {
    if (state.calLoading) return;
    document.getElementById('cal-wizard-view').classList.add('hidden');
    document.getElementById('cal-status-view').classList.remove('hidden');
  },

  async confirmClear() {
    if (!confirm('Delete ALL stitch calibration data? This cannot be undone.')) return;
    _calSetLoading(true);
    try {
      await apiFetch('/rpi/mindvision/stitch/calibrate', { method: 'DELETE' });
      await calWizard._loadCurrentStatus();
      await loadStitchStatus();
    } catch (e) {
      showError('Failed to clear stitch data: ' + e.message);
    } finally {
      _calSetLoading(false);
    }
  },

  beginPasses() {
    state.calPassIndex = 0;
    calWizard._showPassStep();
  },

  _showPassStep() {
    const pass = state.calPassPlan[state.calPassIndex];
    const total = state.calPassPlan.length;
    const idx = state.calPassIndex;

    document.getElementById('cal-pass-title').textContent =
      `Pass ${idx + 1} of ${total} — Position Board`;

    const instr = document.getElementById('cal-pass-instruction');
    if (total === 1) {
      instr.innerHTML = `<p>Place the ChArUco board where <strong>Camera ${pass[0]}</strong> and <strong>Camera ${pass[1]}</strong> can both see it clearly.</p>
        <p>The board should fill a good portion of the shared overlap zone.</p>`;
    } else {
      instr.innerHTML = `<p>Place the board in the overlap zone between <strong>Camera ${pass[0]}</strong> and <strong>Camera ${pass[1]}</strong>.</p>
        <p>Pass ${idx + 1} of ${total}. ${idx < total - 1
          ? `Next: move the board to the overlap between Cameras ${state.calPassPlan[idx + 1].join(' + ')}.`
          : 'This is the final pass.'}</p>`;
    }

    const tagList = document.getElementById('cal-pass-cameras');
    tagList.innerHTML = pass.map(id => `<span class="camera-tag">Camera ${id}</span>`).join('');

    document.getElementById('cal-detect-img').src = '';
    _calShowStep(1);
  },

  async checkDetection() {
    const pass = state.calPassPlan[state.calPassIndex];
    const detectImg = document.getElementById('cal-detect-img');
    clearError();
    try {
      const blob = await apiFetch(`/rpi/mindvision/stitch/detect?camera_id=${pass[0]}&t=${Date.now()}`);
      const prev = detectImg.src;
      if (prev && prev.startsWith('blob:')) URL.revokeObjectURL(prev);
      detectImg.src = URL.createObjectURL(blob);
    } catch (e) {
      showError('Board detection failed: ' + e.message);
    }
  },

  async runPass() {
    if (state.calLoading) return;
    const pass = state.calPassPlan[state.calPassIndex];
    _calSetLoading(true);
    clearError();
    try {
      const postData = await apiFetch('/rpi/mindvision/stitch/calibrate', {
        method: 'POST',
        body: { cameras: pass },
      });
      let statusData = null;
      try { statusData = await apiFetch('/rpi/mindvision/stitch/calibrate'); } catch (_) {}
      calWizard._showPassResult(postData, statusData);
      _calShowStep(2);
    } catch (e) {
      showError('Calibration pass failed: ' + e.message);
    } finally {
      _calSetLoading(false);
    }
  },

  _showPassResult(postData, statusData) {
    const updated = postData.updated || [];
    const failed = postData.failed || {};
    const failedIds = Object.keys(failed);
    const statusCams = (statusData && statusData.cameras) || {};

    let html = `<div class="result-block ${failedIds.length === 0 ? 'success' : 'warning'}">
      <h4>${failedIds.length === 0
        ? `✓ Pass ${state.calPassIndex + 1} succeeded`
        : `⚠ Pass ${state.calPassIndex + 1} — some cameras failed`}</h4><table>`;

    updated.forEach(id => {
      const v = statusCams[String(id)] || {};
      const inlierPct = v.inliers && v.corners_detected
        ? ` (${Math.round(v.inliers / v.corners_detected * 100)}% inliers)` : '';
      html += `<tr><td>Camera ${id}</td><td>
        <span class="pill pill-green" style="font-size:11px">✓ ${v.corners_detected || '?'} corners${inlierPct}</span>
      </td></tr>`;
    });
    failedIds.forEach(id => {
      html += `<tr><td>Camera ${id}</td><td>
        <span class="pill pill-red" style="font-size:11px">✕ Failed</span>
      </td></tr>`;
      if (failed[id]) {
        html += `<tr><td colspan="2" style="color:var(--danger);font-size:12px;padding-bottom:4px">${failed[id]}</td></tr>`;
      }
    });
    html += '</table></div>';
    document.getElementById('cal-pass-result').innerHTML = html;

    const isLast = state.calPassIndex >= state.calPassPlan.length - 1;
    document.getElementById('btn-cal-next').classList.toggle('hidden', isLast);
    document.getElementById('btn-cal-preview').classList.toggle('hidden', !isLast);
  },

  nextPass() {
    state.calPassIndex++;
    calWizard._showPassStep();
  },

  async showPreview() {
    _calShowStep(3);
    const img = document.getElementById('cal-preview-img');
    clearError();
    try {
      const blob = await apiFetch('/rpi/mindvision/stitch/preview');
      if (state.calPreviewBlobUrl) URL.revokeObjectURL(state.calPreviewBlobUrl);
      state.calPreviewBlobUrl = URL.createObjectURL(blob);
      img.src = state.calPreviewBlobUrl;
    } catch (e) {
      showError('Could not load preview: ' + e.message);
    }
  },

  async complete() {
    const img = document.getElementById('cal-preview-img');
    img.src = '';
    if (state.calPreviewBlobUrl) {
      URL.revokeObjectURL(state.calPreviewBlobUrl);
      state.calPreviewBlobUrl = null;
    }
    _calShowStep(4);
    try {
      const data = await apiFetch('/rpi/mindvision/stitch/calibrate');
      const entries = Object.entries(data.cameras || {});
      const canvas = data.canvas || {};
      document.getElementById('cal-complete-info').innerHTML = `
        <div class="result-block success">
          <h4>✓ Stitching calibration complete</h4>
          <table>
            <tr><td>Cameras calibrated</td><td>${entries.length}</td></tr>
            ${canvas.width ? `<tr><td>Canvas size</td><td>${canvas.width} × ${canvas.height} px</td></tr>` : ''}
          </table>
        </div>`;
    } catch (_) {
      document.getElementById('cal-complete-info').innerHTML =
        '<div class="result-block success"><h4>✓ Stitching calibration complete</h4></div>';
    }
  },

  done() {
    document.getElementById('cal-wizard-view').classList.add('hidden');
    document.getElementById('cal-status-view').classList.remove('hidden');
    loadStitchStatus();
    loadWbCalStatus();
  },

  restart() {
    if (!confirm('Restart calibration? Existing data will not be cleared.')) return;
    state.calPassIndex = 0;
    calWizard._showPassStep();
  },
};

// ── White balance calibration ──────────────────────────────────────────
async function loadWbCalStatus() {
  const pill    = document.getElementById('pill-wb-status');
  const body    = document.getElementById('wb-cal-status-body');
  const btnCal  = document.getElementById('btn-wb-cal');
  const btnClear= document.getElementById('btn-wb-clear');

  // Disable calibrate button until we know stitch is ready.
  btnCal.disabled = !state.stitchReady;

  try {
    const data = await apiFetch('/rpi/mindvision/stitch/calibrate-color', { allow404: true });

    if (!data || !data.calibrated) {
      pill.className = 'pill pill-red';
      pill.textContent = 'Not calibrated';
      body.textContent = 'No WB calibration. Point all cameras at a neutral white/grey surface then click Calibrate WB.';
      btnClear.classList.add('hidden');
      return;
    }

    pill.className = 'pill pill-green';
    pill.textContent = 'Calibrated';
    btnClear.classList.remove('hidden');

    const corr = data.corrections || {};
    const refId = data.reference_camera;
    const rows = Object.entries(corr).map(([id, c]) =>
      `<tr>
        <td>Camera ${id}${parseInt(id) === refId ? ' <span class="pill pill-green" style="font-size:11px">reference</span>' : ''}</td>
        <td style="text-align:right">
          <span style="color:#dc2626">R×${c.r}</span>
          <span style="color:#16a34a;margin:0 4px">G×${c.g}</span>
          <span style="color:#2563eb">B×${c.b}</span>
        </td>
      </tr>`
    ).join('');

    body.innerHTML = `
      <table class="cal-status-table">${rows}
        ${data.calibrated_at ? `<tr><td>Calibrated at</td><td style="text-align:right;font-size:12px;color:var(--text-muted)">${new Date(data.calibrated_at).toLocaleString()}</td></tr>` : ''}
      </table>`;
  } catch (_) {
    pill.className = 'pill pill-unknown';
    pill.textContent = 'Unknown';
    body.textContent = 'Could not load WB calibration status.';
  }
}

const wbCal = {
  async calibrate() {
    if (!state.stitchReady) {
      showError('Complete stitch calibration first before calibrating white balance.');
      return;
    }
    const btn = document.getElementById('btn-wb-cal');
    btn.disabled = true;
    btn.textContent = 'Calibrating…';
    clearError();
    try {
      await apiFetch('/rpi/mindvision/stitch/calibrate-color', { method: 'POST' });
      await loadWbCalStatus();
    } catch (e) {
      showError('WB calibration failed: ' + e.message);
    } finally {
      btn.disabled = false;
      btn.textContent = 'Calibrate WB';
    }
  },

  async clear() {
    if (!confirm('Clear WB calibration? Cameras will render with uncorrected colours until you recalibrate.')) return;
    clearError();
    try {
      await apiFetch('/rpi/mindvision/stitch/calibrate-color', { method: 'DELETE' });
      await loadWbCalStatus();
    } catch (e) {
      showError('Failed to clear WB calibration: ' + e.message);
    }
  },
};

// ── Cleanup on page leave ──────────────────────────────────────────────
window.addEventListener('beforeunload', () => {
  stopAllStreams();
  _stopStitchViewStream();
});

// ── Init ───────────────────────────────────────────────────────────────
async function init() {
  clearError();

  const [cameras, cfg] = await Promise.all([
    apiFetch('/rpi/mindvision/cameras').catch(() => []),
    apiFetch('/rpi/mindvision/stitch/config').catch(() => null),
  ]);

  state.cameras = Array.isArray(cameras) ? cameras : [];

  if (cfg && Array.isArray(cfg.camera_order) && cfg.camera_order.length > 0) {
    state.cameraOrder = cfg.camera_order;
  }

  buildCameraChecks();
  reconcileCameraOrder();
  buildLayoutChips();
  buildStreams();
  await loadStitchStatus();
  await loadWbCalStatus();
}

document.addEventListener('DOMContentLoaded', init);
