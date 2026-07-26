'use strict';

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

// ── Cameras ────────────────────────────────────────────────────────────
let _cameras = [];

function renderCameras(cameras) {
  _cameras = cameras;
  const grid  = document.getElementById('camera-grid');
  const badge = document.getElementById('camera-count');

  badge.textContent = `${cameras.length} camera${cameras.length !== 1 ? 's' : ''}`;
  badge.className = cameras.length > 0 ? 'pill pill-green' : 'pill pill-red';

  const dlBtn = document.getElementById('btn-download-config');
  if (dlBtn) dlBtn.style.display = cameras.length > 0 ? '' : 'none';

  if (cameras.length === 0) {
    grid.innerHTML = '<div class="loading-placeholder">No cameras detected.</div>';
    return;
  }

  grid.innerHTML = cameras.map(c => {
    const isMindVision = c.type === 'mindvision';

    const calibrateBtn = _hwTriggerActive
      ? `<button class="btn btn-primary" style="flex:1;justify-content:center" disabled
             title="Switch decoder to Calibration mode before calibrating">Calibrate</button>`
      : `<a href="/calibrate?camera=${c.camera_id}" class="btn btn-primary" style="flex:1;justify-content:center">Calibrate</a>`;

    const settingsBtn = isMindVision
      ? `<a href="/mindvision/${c.camera_id}/settings" class="btn btn-secondary" style="flex:1;justify-content:center">
          Settings
        </a>`
      : '';

    const configIcon = isMindVision
      ? `<button class="btn-info-icon" title="View raw config" onclick="openConfigModal(${c.camera_id})">
              <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <circle cx="12" cy="12" r="10"/>
                <path d="M12 16v-4"/>
                <path d="M12 8h.01" stroke-width="2.5"/>
              </svg>
            </button>`
      : '';

    return `
    <div class="camera-card">
      <div class="camera-card-header">
        <div class="cam-header-top">
          <span class="cam-id-badge">Cam ${c.camera_id}</span>
          <div class="cam-header-pills">
            <span class="pill ${c.status === 'open' ? 'pill-green' : 'pill-red'}">${c.status}</span>
            ${_hwTriggerActive && isMindVision ? `<span class="pill pill-yellow">HW Trigger</span>` : ''}
            ${configIcon}
          </div>
        </div>
        <span class="cam-model">${c.model || c.product_name || 'Unknown model'}</span>
      </div>
      <div class="camera-meta">
        ${c.serial_number ? `<div class="camera-meta-row"><span>Serial</span><span>${c.serial_number}</span></div>` : ''}
        ${c.port_type     ? `<div class="camera-meta-row"><span>Port</span><span>${c.port_type}</span></div>` : ''}
        ${c.product_name && c.product_name !== c.model ? `<div class="camera-meta-row"><span>Product</span><span>${c.product_name}</span></div>` : ''}
      </div>
      <div class="camera-card-footer">
        ${calibrateBtn}
        ${settingsBtn}
      </div>
    </div>`;
  }).join('');
}

// ── Camera Config Modal ────────────────────────────────────────────────

async function openConfigModal(cameraId) {
  const overlay  = document.getElementById('config-modal-overlay');
  const title    = document.getElementById('modal-title');
  const subtitle = document.getElementById('modal-subtitle');
  const body     = document.getElementById('modal-body');

  title.textContent    = `Camera ${cameraId} — Full Config`;
  subtitle.textContent = '';
  body.innerHTML       = '<div class="modal-loading">Loading…</div>';
  overlay.classList.remove('hidden');
  document.body.style.overflow = 'hidden';

  try {
    const data = await apiFetch(`/api/cameras/config/full?camera_id=${cameraId}`);
    body.innerHTML = '';
    body.appendChild(buildGroupedConfig(data));
  } catch (err) {
    body.innerHTML = `<div class="modal-error">Failed to load: ${err.message}</div>`;
  }
}

function fmtValue(val, unit) {
  if (val === null || val === undefined) {
    return '<span class="cfg-null">—</span>';
  }
  if (typeof val === 'boolean') {
    return `<span class="cfg-val cfg-bool-${val}">${val}</span>`;
  }
  const numStr = typeof val === 'number'
    ? (Number.isInteger(val) ? String(val) : val.toFixed(3))
    : String(val);
  const unitSpan = unit ? ` <span class="cfg-unit">${unit}</span>` : '';
  const cls = typeof val === 'number' ? ' cfg-num' : '';
  return `<span class="cfg-val${cls}">${numStr}</span>${unitSpan}`;
}

function buildGroupedConfig(data) {
  const wrap = document.createElement('div');

  (data.groups || []).forEach(group => {
    const section = document.createElement('div');
    section.className = 'cfg-section';

    const header = document.createElement('div');
    header.className = 'cfg-section-header';
    header.textContent = group.name;
    section.appendChild(header);

    const table = document.createElement('table');
    table.className = 'cfg-table';
    table.innerHTML = `<thead><tr><th>Parameter</th><th>Value</th></tr></thead>`;
    const tbody = document.createElement('tbody');

    (group.params || []).forEach(param => {
      const tr = document.createElement('tr');
      tr.innerHTML = `
        <td class="cfg-key">${param.label}</td>
        <td>${fmtValue(param.value, param.unit)}</td>`;
      tbody.appendChild(tr);
    });

    table.appendChild(tbody);
    section.appendChild(table);
    wrap.appendChild(section);
  });

  return wrap;
}

function closeConfigModal(event) {
  if (event && event.target !== document.getElementById('config-modal-overlay')) return;
  document.getElementById('config-modal-overlay').classList.add('hidden');
  document.body.style.overflow = '';
}

document.addEventListener('keydown', e => {
  if (e.key === 'Escape') {
    closeConfigModal();
    closeDecoderModal();
  }
});

function downloadAllConfig() {
  window.location.href = '/api/cameras/config/download';
}

// ── Decoder card ───────────────────────────────────────────────────────

let _decoderPollTimer  = null;
let _hwTriggerActive   = false;

function _decoderDot(status) {
  const now = Date.now() / 1000;
  if (!status.running) return 'gray';
  if (status.last_message_at && (now - status.last_message_at) < 5) return 'green';
  return 'yellow';
}

function updateDecoderCard(status) {
  const dot      = document.getElementById('decoder-connected-dot');
  const label    = document.getElementById('decoder-connected-label');
  const speed    = document.getElementById('decoder-speed');
  const checkBtn = document.getElementById('decoder-check-btn');
  const checkbox = document.getElementById('decoder-trigger-checkbox');

  const color = _decoderDot(status);
  dot.className = `status-dot status-dot-${color}`;

  if (!status.running) {
    const notDetected = status.port_present === false;
    label.textContent = notDetected ? 'Not detected' : 'Not started';
    if (checkBtn) checkBtn.style.display = notDetected ? '' : 'none';
    speed.style.display = 'none';
    if (checkbox) { checkbox.checked = false; checkbox.disabled = true; }
    _setHwTriggerActive(false);
    return;
  }

  if (checkBtn) checkBtn.style.display = 'none';
  const now = Date.now() / 1000;
  const fresh = status.last_message_at && (now - status.last_message_at) < 5;
  label.textContent = fresh ? 'Connected' : 'No data';

  if (checkbox) { checkbox.checked = !!status.trigger_enabled; checkbox.disabled = false; }

  speed.style.display = '';
  const spd = typeof status.speed_cms === 'number' ? status.speed_cms.toFixed(1) : '—';
  speed.textContent = `${spd} cm/s`;

  _setHwTriggerActive(status.running && !!status.trigger_enabled);
}

function _setHwTriggerActive(active) {
  if (active === _hwTriggerActive) return;
  _hwTriggerActive = active;
  renderCameras(_cameras);
}

async function decoderToggleMode(enable) {
  const checkbox = document.getElementById('decoder-trigger-checkbox');
  if (checkbox) checkbox.disabled = true;
  try {
    const url = enable ? '/api/decoder/mode/hw-trigger' : '/api/decoder/mode/calibration';
    await fetch(url, { method: 'POST' });
  } catch (_) {}
  await pollDecoder();
}

async function decoderCheckAgain() {
  const btn = document.getElementById('decoder-check-btn');
  if (btn) { btn.disabled = true; btn.textContent = 'Checking…'; }
  try {
    await fetch('/api/decoder/detect', { method: 'POST' });
  } catch (_) {}
  if (btn) { btn.disabled = false; btn.textContent = 'Check Again'; }
  await pollDecoder();
  devRefreshAfterDecoderOp();
}

let _decoderPollInterval = 10000;

async function pollDecoder() {
  let running = false;
  let portPresent = true;
  try {
    const status = await apiFetch('/api/decoder/status');
    updateDecoderCard(status);
    running = !!status.running;
    portPresent = status.port_present !== false;
  } catch (_) {
    updateDecoderCard({ running: false });
  }

  if (_decoderPollTimer) { clearInterval(_decoderPollTimer); _decoderPollTimer = null; }

  const simRunning = !!status?.simulator_running;

  // Stop polling entirely when nothing is active and the Arduino is not detected.
  if (!portPresent && !running && !simRunning) return;

  const next = (running || simRunning) ? 2000 : 10000;
  _decoderPollInterval = next;
  _decoderPollTimer = setInterval(pollDecoder, next);
}

function startDecoderPolling() {
  pollDecoder();
}

// ── Decoder Modal ──────────────────────────────────────────────────────

let _decoderModalRefreshTimer = null;
let _decoderModalActiveTab = 'status';

function switchDecoderModalTab(tab) {
  _decoderModalActiveTab = tab;
  const overlay = document.getElementById('decoder-modal-overlay');
  overlay.querySelectorAll('.dcfg-tab').forEach(b => b.classList.toggle('active', b.dataset.tab === tab));
  overlay.querySelectorAll('.dcfg-pane').forEach(p => p.classList.toggle('active', p.dataset.tab === tab));
  if (tab === 'config') refreshDecoderModalConfig();
}

function openDecoderModal() {
  document.getElementById('decoder-modal-overlay').classList.remove('hidden');
  document.body.style.overflow = 'hidden';
  // Always open on Status tab and start live refresh
  switchDecoderModalTab('status');
  refreshDecoderModalStats();
  _decoderModalRefreshTimer = setInterval(refreshDecoderModalStats, 2000);
}

function closeDecoderModal(event) {
  if (event && event.target !== document.getElementById('decoder-modal-overlay')) return;
  document.getElementById('decoder-modal-overlay').classList.add('hidden');
  document.body.style.overflow = '';
  if (_decoderModalRefreshTimer) { clearInterval(_decoderModalRefreshTimer); _decoderModalRefreshTimer = null; }
}

async function refreshDecoderModalStats() {
  const el = document.getElementById('decoder-modal-stats');
  try {
    const s = await apiFetch('/api/decoder/status');
    const now = Date.now() / 1000;
    const fresh = s.last_message_at && (now - s.last_message_at) < 5;
    const connColor = _decoderDot(s);
    const portLabel = s.port_present === false
      ? `<span class="cfg-val" style="color:var(--danger)">Not detected</span>`
      : `<span class="cfg-val" style="color:var(--success)">Detected</span>`;
    el.innerHTML = `
      <table class="cfg-table">
        <tbody>
          <tr><td class="cfg-key">Arduino port</td><td>${portLabel}</td></tr>
          <tr><td class="cfg-key">Listener</td><td><span class="cfg-val cfg-bool-${s.running}">${s.running ? 'Running' : 'Stopped'}</span></td></tr>
          <tr><td class="cfg-key">Arduino</td><td><span class="cfg-val" style="color:var(--${connColor === 'green' ? 'success' : connColor === 'yellow' ? 'warning' : 'text-muted'})">${fresh ? 'Connected' : s.running ? 'No data' : '—'}</span></td></tr>
          <tr><td class="cfg-key">Speed</td><td><span class="cfg-val cfg-num">${typeof s.speed_cms === 'number' ? s.speed_cms.toFixed(2) : '—'}</span><span class="cfg-unit">cm/s</span></td></tr>
          <tr><td class="cfg-key">Encoder count</td><td><span class="cfg-val cfg-num">${s.encoder_count ?? '—'}</span></td></tr>
          <tr><td class="cfg-key">Uptime</td><td><span class="cfg-val">${s.uptime_seconds != null ? s.uptime_seconds + 's' : '—'}</span></td></tr>
          <tr><td class="cfg-key">Triggers received</td><td><span class="cfg-val cfg-num">${s.triggers_received ?? 0}</span></td></tr>
          <tr><td class="cfg-key">Captures OK</td><td><span class="cfg-val cfg-num">${s.captures_ok ?? 0}</span></td></tr>
          <tr><td class="cfg-key">Captures failed</td><td><span class="cfg-val cfg-num">${s.captures_failed ?? 0}</span></td></tr>
        </tbody>
      </table>
      <div style="margin-top:12px;display:flex;gap:8px;flex-wrap:wrap">
        ${s.running
          ? `<button class="btn btn-danger btn-sm" onclick="decoderStop()">Stop listener</button>`
          : `<button class="btn btn-primary btn-sm" onclick="decoderStart()" ${s.port_present === false ? 'disabled title="Arduino not connected on /dev/ttyACM0"' : ''}>Start listener</button>`}
      </div>`;
  } catch (err) {
    el.innerHTML = `<div class="modal-error">Failed: ${err.message}</div>`;
  }
}

async function refreshDecoderModalConfig() {
  const el = document.getElementById('decoder-modal-config');
  try {
    const d = await apiFetch('/api/decoder/config');
    const phys        = d.physical_config  || {};
    const physDefs    = d.physical_defaults || {};
    const cfg         = d.arduino_config   || {};
    const arduinoDefs = d.arduino_defaults  || {};

    const row = (key, label, unit, type, value) => `
      <div class="dcfg-row">
        <label class="dcfg-label" for="dcfg-${key}">${label}</label>
        <input class="dcfg-input" id="dcfg-${key}" type="number" value="${value}"
          step="${type === 'float' ? '0.1' : '1'}">
        <span class="dcfg-unit">${unit}</span>
      </div>`;

    const rowRO = (label, unit, value) => `
      <div class="dcfg-row">
        <span class="dcfg-label">${label}</span>
        <span class="dcfg-readonly">${value}</span>
        <span class="dcfg-unit">${unit}</span>
      </div>`;

    el.innerHTML = `
      <div class="dcfg-section-label">Wheel &amp; encoder</div>
      ${row('wheel_diameter_mm',   'Wheel diameter',     'mm',  'float', phys['wheel_diameter_mm']   ?? physDefs['wheel_diameter_mm']   ?? '')}
      ${row('encoder_ppr',         'Encoder resolution', 'PPR', 'int',   phys['encoder_ppr']         ?? physDefs['encoder_ppr']         ?? '')}
      ${row('capture_interval_mm', 'Capture interval',   'mm',  'float', phys['capture_interval_mm'] ?? physDefs['capture_interval_mm'] ?? '')}

      <div class="dcfg-section-label">Computed (read-only)</div>
      ${rowRO('Counts per cm',    'counts/cm', cfg['counts_per_cm']    ?? arduinoDefs['counts_per_cm']    ?? '—')}
      ${rowRO('Trigger interval', 'counts',    cfg['trigger_interval'] ?? arduinoDefs['trigger_interval'] ?? '—')}

      <div class="dcfg-section-label">Timing</div>
      ${row('pulse_width_ms',           'Pulse width',           'ms', 'int', cfg['pulse_width_ms']           ?? arduinoDefs['pulse_width_ms']           ?? '')}
      ${row('speed_report_interval_ms', 'Speed report interval', 'ms', 'int', cfg['speed_report_interval_ms'] ?? arduinoDefs['speed_report_interval_ms'] ?? '')}

      <div class="dcfg-actions">
        <button class="btn btn-primary btn-sm" onclick="decoderApplyAllCfg()">Apply</button>
        <button class="btn btn-secondary btn-sm" onclick="decoderResetCount()">Reset encoder count</button>
        <button class="btn btn-secondary btn-sm" onclick="decoderResetConfig()">Reset to defaults</button>
      </div>
      <div id="dcfg-toast" class="dcfg-toast hidden"></div>`;
  } catch (err) {
    el.innerHTML = `<div class="modal-error">Failed: ${err.message}</div>`;
  }
}

async function decoderApplyAllCfg() {
  const fields = [
    { key: 'wheel_diameter_mm',        type: 'float' },
    { key: 'encoder_ppr',              type: 'int'   },
    { key: 'capture_interval_mm',      type: 'float' },
    { key: 'pulse_width_ms',           type: 'int'   },
    { key: 'speed_report_interval_ms', type: 'int'   },
  ];

  const body = {};
  for (const { key, type } of fields) {
    const input = document.getElementById(`dcfg-${key}`);
    if (!input) continue;
    const value = type === 'float' ? parseFloat(input.value) : parseInt(input.value, 10);
    if (!isNaN(value)) body[key] = value;
  }
  if (!Object.keys(body).length) return;

  const toast = document.getElementById('dcfg-toast');
  const showToast = (msg, ok) => {
    if (!toast) return;
    toast.textContent = msg;
    toast.className = `dcfg-toast ${ok ? 'dcfg-toast-ok' : 'dcfg-toast-err'}`;
    clearTimeout(toast._timer);
    toast._timer = setTimeout(() => toast.classList.add('hidden'), 2500);
  };

  try {
    const res = await fetch('/api/decoder/config', {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    if (!res.ok) { showToast(data.error || 'Failed to apply config', false); return; }
    showToast('Config saved', true);
    setTimeout(refreshDecoderModalConfig, 600);
  } catch (err) {
    showToast('Error: ' + err.message, false);
  }
}

async function decoderStart() {
  try {
    await fetch('/api/decoder/start', { method: 'POST' });
  } catch (_) {}
  await refreshDecoderModalStats();
  pollDecoder();
}

async function decoderStop() {
  try {
    await fetch('/api/decoder/stop', { method: 'POST' });
  } catch (_) {}
  await refreshDecoderModalStats();
  pollDecoder();
}

async function decoderSetModeHwTrigger() {
  try {
    const res = await fetch('/api/decoder/mode/hw-trigger', { method: 'POST' });
    if (!res.ok) { const d = await res.json(); alert(d.error || 'Failed to switch to HW trigger mode'); return; }
  } catch (err) { alert('Error: ' + err.message); return; }
  setTimeout(() => { refreshDecoderModalStats(); pollDecoder(); }, 600);
}

async function decoderSetModeCalibration() {
  try {
    const res = await fetch('/api/decoder/mode/calibration', { method: 'POST' });
    if (!res.ok) { const d = await res.json(); alert(d.error || 'Failed to switch to calibration mode'); return; }
  } catch (err) { alert('Error: ' + err.message); return; }
  setTimeout(() => { refreshDecoderModalStats(); pollDecoder(); }, 600);
}

async function decoderFireTrigger() {
  try {
    const res = await fetch('/api/decoder/trigger/fire', { method: 'POST' });
    if (!res.ok) { const d = await res.json(); alert(d.error || 'Failed to fire trigger'); return; }
  } catch (err) { alert('Error: ' + err.message); return; }
  // Allow time for serial round-trip (Arduino queuing up to ~1s) + capture
  setTimeout(() => { refreshDecoderModalStats(); pollDecoder(); }, 1500);
}



async function decoderResetCount() {
  try {
    const res = await fetch('/api/decoder/reset-count', { method: 'POST' });
    if (!res.ok) { const d = await res.json(); alert(d.error || 'Failed'); return; }
  } catch (err) { alert('Error: ' + err.message); return; }
  setTimeout(() => { refreshDecoderModalStats(); pollDecoder(); }, 400);
}

async function decoderResetConfig() {
  if (!confirm('Reset all Arduino parameters to factory defaults?')) return;
  try {
    await fetch('/api/decoder/config', { method: 'DELETE' });
  } catch (_) {}
  setTimeout(refreshDecoderModalConfig, 800);
}

// ── Dev Mode ───────────────────────────────────────────────────────────
let _devCameraIds = [];
let _devSimPollTimer = null;
let _queueStream = null;

function startQueueStream() {
  if (_queueStream) return;
  _queueStream = new EventSource('/api/decoder/queues/stream');
  _queueStream.onmessage = (e) => {
    try { renderQueueDepths(JSON.parse(e.data)); } catch (_) {}
  };
}

function stopQueueStream() {
  if (_queueStream) { _queueStream.close(); _queueStream = null; }
  const el = document.getElementById('dev-queue-depths');
  if (el) el.innerHTML = '<div class="dev-queue-row"><span>—</span><span class="dev-queue-val">—</span></div>';
}

function renderQueueDepths(d) {
  const el = document.getElementById('dev-queue-depths');
  if (!el) return;
  const rows = [];
  for (const [id, depth] of Object.entries(d.camera_queues || {})) {
    rows.push(`<div class="dev-queue-row"><span>Cam ${id} capture</span><span class="dev-queue-val${depth > 0 ? ' dev-queue-nonzero' : ''}">${depth}</span></div>`);
  }
  const items = [
    ['Collector pending', d.collector_pending],
    ['Stitch upload',     d.stitch_pending],
    ['Raw upload',        d.raw_pending],
    ['Disk retry',        d.disk_retry],
  ];
  for (const [label, depth] of items) {
    rows.push(`<div class="dev-queue-row"><span>${label}</span><span class="dev-queue-val${depth > 0 ? ' dev-queue-nonzero' : ''}">${depth}</span></div>`);
  }
  el.innerHTML = rows.join('');
}

function initDevMode() {
  const active = localStorage.getItem('visionx_dev_mode') === 'true';
  _applyDevMode(active);
}

function toggleDevMode() {
  const active = localStorage.getItem('visionx_dev_mode') === 'true';
  const next = !active;
  localStorage.setItem('visionx_dev_mode', String(next));
  _applyDevMode(next);
}

function _applyDevMode(active) {
  const btn     = document.getElementById('dev-mode-btn');
  const section = document.getElementById('dev-tools-section');
  if (btn)     btn.classList.toggle('dev-btn-active', active);
  if (section) section.classList.toggle('hidden', !active);
  if (active) {
    refreshDevCamMode();
    devRefreshSimStatus();
    if (_devSimPollTimer) clearInterval(_devSimPollTimer);
    _devSimPollTimer = setInterval(devRefreshSimStatus, 2000);
    startQueueStream();
  } else {
    if (_devSimPollTimer) { clearInterval(_devSimPollTimer); _devSimPollTimer = null; }
    stopQueueStream();
  }
}

async function refreshDevCamMode() {
  const label = document.getElementById('dev-cam-mode-label');
  if (!label) return;
  try {
    const data = await apiFetch('/api/cameras/mode?camera_id=0');
    label.textContent = `Current: ${data.mode || '—'}`;
  } catch (_) {
    label.textContent = 'Current: —';
  }
}

async function devSetCamMode(mode) {
  const ids = _devCameraIds.length ? _devCameraIds : [0];
  await Promise.all(ids.map(id =>
    fetch(`/api/cameras/mode?camera_id=${id}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode }),
    }).catch(() => {})
  ));
  setTimeout(refreshDevCamMode, 300);
}

function devRefreshAfterDecoderOp() {
  const section = document.getElementById('dev-tools-section');
  if (section && !section.classList.contains('hidden')) {
    setTimeout(refreshDevCamMode, 600);
    setTimeout(devRefreshSimStatus, 600);
  }
}

async function devRefreshSimStatus() {
  const label = document.getElementById('dev-sim-status');
  if (!label) return;
  try {
    const s = await apiFetch('/api/decoder/status');
    if (s.simulator_running) {
      label.textContent = `Running · ${s.simulator_speed_cms?.toFixed(1)} cm/s · ${s.triggers_received ?? 0} triggers`;
      label.style.color = 'var(--success)';
    } else {
      label.textContent = 'Stopped';
      label.style.color = '';
    }
  } catch (_) {
    label.textContent = '—';
    label.style.color = '';
  }
}

async function decoderSimStart() {
  const speedInput = document.getElementById('dev-sim-speed');
  const speed_cms = parseFloat(speedInput?.value || '5');
  if (isNaN(speed_cms) || speed_cms <= 0) { alert('Enter a valid speed (cm/s)'); return; }
  try {
    const res = await fetch('/api/decoder/simulator/start', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ speed_cms }),
    });
    if (!res.ok) { const d = await res.json(); alert(d.error || 'Failed to start simulator'); return; }
  } catch (err) { alert('Error: ' + err.message); return; }
  devRefreshSimStatus();
  devRefreshAfterDecoderOp();
  pollDecoder();
}

async function decoderSimStop() {
  try {
    const res = await fetch('/api/decoder/simulator/stop', { method: 'POST' });
    if (!res.ok) { const d = await res.json(); alert(d.error || 'Failed to stop simulator'); return; }
  } catch (err) { alert('Error: ' + err.message); return; }
  devRefreshSimStatus();
  devRefreshAfterDecoderOp();
  pollDecoder();
}

// ── Init ───────────────────────────────────────────────────────────────
async function refreshAll() {
  clearError();
  try {
    const cameras = await apiFetch('/api/system/cameras');
    const list = Array.isArray(cameras) ? cameras : [];
    _devCameraIds = list.filter(c => c.type === 'mindvision').map(c => c.camera_id);
    renderCameras(list);
  } catch (e) {
    showError('Failed to load cameras: ' + e.message);
    renderCameras(_cameras.length ? _cameras : []);
  }
}

document.addEventListener('DOMContentLoaded', () => {
  initDevMode();
  refreshAll();
  startDecoderPolling();
});
