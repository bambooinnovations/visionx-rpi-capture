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
function renderCameras(cameras) {
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

  grid.innerHTML = cameras.map(c => `
    <div class="camera-card">
      <div class="camera-card-header">
        <span class="cam-id-badge">Cam ${c.camera_id}</span>
        <span class="cam-model">${c.model || c.product_name || 'Unknown model'}</span>
        <span class="pill ${c.status === 'open' ? 'pill-green' : 'pill-red'}">${c.status}</span>
        <button class="btn-info-icon" title="View raw config" onclick="openConfigModal(${c.camera_id})">
          <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <circle cx="12" cy="12" r="10"/>
            <path d="M12 16v-4"/>
            <path d="M12 8h.01" stroke-width="2.5"/>
          </svg>
        </button>
      </div>
      <div class="camera-meta">
        ${c.serial_number ? `<div class="camera-meta-row"><span>Serial</span><span>${c.serial_number}</span></div>` : ''}
        ${c.port_type     ? `<div class="camera-meta-row"><span>Port</span><span>${c.port_type}</span></div>` : ''}
        ${c.product_name && c.product_name !== c.model ? `<div class="camera-meta-row"><span>Product</span><span>${c.product_name}</span></div>` : ''}
      </div>
      <div class="camera-card-footer">
        <a href="/calibrate?camera=${c.camera_id}" class="btn btn-primary" style="flex:1;justify-content:center">
          Calibrate
        </a>
        <a href="/mindvision/${c.camera_id}/settings" class="btn btn-secondary" style="flex:1;justify-content:center">
          Settings
        </a>
      </div>
    </div>`
  ).join('');
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

let _decoderPollTimer = null;

function _decoderDot(status) {
  const now = Date.now() / 1000;
  if (!status.running) return 'gray';
  if (status.last_message_at && (now - status.last_message_at) < 5) return 'green';
  return 'yellow';
}

function updateDecoderCard(status) {
  const dot   = document.getElementById('decoder-connected-dot');
  const label = document.getElementById('decoder-connected-label');
  const badge = document.getElementById('decoder-trigger-badge');
  const speed = document.getElementById('decoder-speed');

  const color = _decoderDot(status);
  dot.className = `status-dot status-dot-${color}`;

  if (!status.running) {
    label.textContent = 'Not started';
    badge.style.display = 'none';
    speed.style.display = 'none';
    return;
  }

  const now = Date.now() / 1000;
  const fresh = status.last_message_at && (now - status.last_message_at) < 5;
  label.textContent = fresh ? 'Connected' : 'No data';

  badge.style.display = '';
  if (status.trigger_enabled) {
    badge.textContent = 'Triggering';
    badge.className = 'pill pill-green';
  } else {
    badge.textContent = 'Stopped';
    badge.className = 'pill pill-yellow';
  }

  speed.style.display = '';
  const spd = typeof status.speed_cms === 'number' ? status.speed_cms.toFixed(1) : '—';
  speed.textContent = `${spd} cm/s`;
}

let _decoderPollInterval = 10000;

async function pollDecoder() {
  let running = false;
  try {
    const status = await apiFetch('/api/decoder/status');
    updateDecoderCard(status);
    running = !!status.running;
  } catch (_) {
    updateDecoderCard({ running: false });
  }
  const next = running ? 2000 : 10000;
  if (next !== _decoderPollInterval) {
    _decoderPollInterval = next;
    if (_decoderPollTimer) clearInterval(_decoderPollTimer);
    _decoderPollTimer = setInterval(pollDecoder, next);
  }
}

function startDecoderPolling() {
  pollDecoder();
  _decoderPollTimer = setInterval(pollDecoder, _decoderPollInterval);
}

// ── Decoder Modal ──────────────────────────────────────────────────────

let _decoderModalRefreshTimer = null;

function openDecoderModal() {
  document.getElementById('decoder-modal-overlay').classList.remove('hidden');
  document.body.style.overflow = 'hidden';
  refreshDecoderModal();
  _decoderModalRefreshTimer = setInterval(refreshDecoderModalStats, 2000);
}

function closeDecoderModal(event) {
  if (event && event.target !== document.getElementById('decoder-modal-overlay')) return;
  document.getElementById('decoder-modal-overlay').classList.add('hidden');
  document.body.style.overflow = '';
  if (_decoderModalRefreshTimer) { clearInterval(_decoderModalRefreshTimer); _decoderModalRefreshTimer = null; }
}

async function refreshDecoderModal() {
  await Promise.all([refreshDecoderModalStats(), refreshDecoderModalConfig()]);
}

async function refreshDecoderModalStats() {
  const el = document.getElementById('decoder-modal-stats');
  try {
    const s = await apiFetch('/api/decoder/status');
    const now = Date.now() / 1000;
    const fresh = s.last_message_at && (now - s.last_message_at) < 5;
    const connColor = _decoderDot(s);
    el.innerHTML = `
      <table class="cfg-table">
        <tbody>
          <tr><td class="cfg-key">Listener</td><td><span class="cfg-val cfg-bool-${s.running}">${s.running ? 'Running' : 'Stopped'}</span></td></tr>
          <tr><td class="cfg-key">Arduino</td><td><span class="cfg-val" style="color:var(--${connColor === 'green' ? 'success' : connColor === 'yellow' ? 'warning' : 'text-muted'})">${fresh ? 'Connected' : s.running ? 'No data' : '—'}</span></td></tr>
          <tr><td class="cfg-key">Triggering</td><td><span class="cfg-val cfg-bool-${s.trigger_enabled}">${s.trigger_enabled}</span></td></tr>
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
          : `<button class="btn btn-primary btn-sm" onclick="decoderStart()">Start listener</button>`}
        ${s.running && s.trigger_enabled
          ? `<button class="btn btn-secondary btn-sm" onclick="decoderTriggerDisable()">Disable triggering</button>`
          : s.running
            ? `<button class="btn btn-secondary btn-sm" onclick="decoderTriggerEnable()">Enable triggering</button>`
            : ''}
        ${s.running ? `<button class="btn btn-primary btn-sm" onclick="decoderFireTrigger()">Fire trigger</button>` : ''}
      </div>`;
  } catch (err) {
    el.innerHTML = `<div class="modal-error">Failed: ${err.message}</div>`;
  }
}

async function refreshDecoderModalConfig() {
  const el = document.getElementById('decoder-modal-config');
  try {
    const d = await apiFetch('/api/decoder/config');
    const cfg = d.arduino_config || {};
    const defs = d.defaults || {};

    const rows = [
      { key: 'trigger_interval',        label: 'Trigger interval',        unit: 'counts', type: 'int' },
      { key: 'counts_per_cm',           label: 'Counts per cm',           unit: 'counts/cm', type: 'float' },
      { key: 'pulse_width_ms',          label: 'Pulse width',             unit: 'ms', type: 'int' },
      { key: 'speed_report_interval_ms',label: 'Speed heartbeat interval',unit: 'ms', type: 'int' },
    ];

    el.innerHTML = rows.map(r => {
      const current = cfg[r.key] ?? defs[r.key] ?? '';
      return `
        <div class="decoder-cfg-row">
          <label class="decoder-cfg-label">${r.label}</label>
          <div class="decoder-cfg-input-wrap">
            <input class="decoder-cfg-input" id="dcfg-${r.key}" type="number" value="${current}" step="${r.type === 'float' ? '0.1' : '1'}">
            <span class="cfg-unit">${r.unit}</span>
            <button class="btn btn-primary btn-sm" onclick="decoderApplyCfg('${r.key}', '${r.type}')">Apply</button>
          </div>
        </div>`;
    }).join('') + `
      <div style="margin-top:10px">
        <button class="btn btn-secondary btn-sm" onclick="decoderResetConfig()">Reset to defaults</button>
      </div>`;
  } catch (err) {
    el.innerHTML = `<div class="modal-error">Failed: ${err.message}</div>`;
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

async function decoderTriggerEnable() {
  try { await fetch('/api/decoder/trigger/enable', { method: 'POST' }); } catch (_) {}
  setTimeout(() => { refreshDecoderModalStats(); pollDecoder(); }, 600);
}

async function decoderTriggerDisable() {
  try { await fetch('/api/decoder/trigger/disable', { method: 'POST' }); } catch (_) {}
  setTimeout(() => { refreshDecoderModalStats(); pollDecoder(); }, 600);
}

async function decoderFireTrigger() {
  try {
    const res = await fetch('/api/decoder/trigger/fire', { method: 'POST' });
    if (!res.ok) { const d = await res.json(); alert(d.error || 'Failed to fire trigger'); return; }
  } catch (err) { alert('Error: ' + err.message); return; }
  setTimeout(() => { refreshDecoderModalStats(); pollDecoder(); }, 600);
}

async function decoderApplyCfg(key, type) {
  const input = document.getElementById(`dcfg-${key}`);
  if (!input) return;
  const raw = input.value.trim();
  const value = type === 'float' ? parseFloat(raw) : parseInt(raw, 10);
  if (isNaN(value)) { alert(`Invalid value for ${key}`); return; }
  try {
    const res = await fetch('/api/decoder/config', {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ [key]: value }),
    });
    const data = await res.json();
    if (!res.ok) { alert(data.error || 'Failed'); return; }
    setTimeout(refreshDecoderModalConfig, 600);
  } catch (err) {
    alert('Error: ' + err.message);
  }
}

async function decoderResetConfig() {
  if (!confirm('Reset all Arduino parameters to factory defaults?')) return;
  try {
    await fetch('/api/decoder/config', { method: 'DELETE' });
  } catch (_) {}
  setTimeout(refreshDecoderModalConfig, 800);
}

// ── Init ───────────────────────────────────────────────────────────────
async function refreshAll() {
  clearError();
  try {
    const cameras = await apiFetch('/api/cameras');
    renderCameras(Array.isArray(cameras) ? cameras : []);
  } catch (e) {
    showError('Failed to load cameras: ' + e.message);
    renderCameras([]);
  }
}

document.addEventListener('DOMContentLoaded', () => {
  refreshAll();
  startDecoderPolling();
});
