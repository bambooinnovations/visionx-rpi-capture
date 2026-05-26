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
  const grid = document.getElementById('camera-grid');
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

// ── Config Modal ───────────────────────────────────────────────────────

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
    const data = await apiFetch(`/rpi/mindvision/config/full?camera_id=${cameraId}`);
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
  if (e.key === 'Escape') closeConfigModal();
});

function downloadAllConfig() {
  window.location.href = '/rpi/mindvision/config/download';
}

// ── Init ───────────────────────────────────────────────────────────────
async function refreshAll() {
  clearError();
  try {
    const cameras = await apiFetch('/rpi/mindvision/cameras');
    renderCameras(Array.isArray(cameras) ? cameras : []);
  } catch (e) {
    showError('Failed to load cameras: ' + e.message);
    renderCameras([]);
  }
}

document.addEventListener('DOMContentLoaded', refreshAll);
