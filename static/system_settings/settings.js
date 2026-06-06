'use strict';

function showError(msg) {
  document.getElementById('error-text').textContent = msg;
  document.getElementById('error-banner').classList.remove('hidden');
}
function clearError() {
  document.getElementById('error-banner').classList.add('hidden');
}

let _toastTimer = null;
function showSuccess(msg = 'Saved') {
  const el = document.getElementById('success-toast');
  el.textContent = msg;
  el.classList.remove('hidden');
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => el.classList.add('hidden'), 2500);
}

async function apiFetch(path, opts = {}) {
  const res = await fetch(path, opts);
  const json = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(json.error || `HTTP ${res.status}`);
  return json;
}

// ── Load & render ─────────────────────────────────────────────────────

async function loadConfig() {
  try {
    const { config } = await apiFetch('/api/system/config');

    // Editable fields
    document.getElementById('destination_url').value     = config['hw_trigger.destination_url']     || '';
    document.getElementById('destination_api_key').value = '';  // always blank on load (masked server-side)
    document.getElementById('send_raw_images').checked   = !!config['hw_trigger.send_raw_images'];
    document.getElementById('raw_destination_url').value = config['hw_trigger.raw_destination_url'] || '';

    // Read-only fields
    set('ro-serial_port',      config['hw_trigger.serial_port']      ?? '—');
    set('ro-serial_baud',      config['hw_trigger.serial_baud']      ?? '—');
    set('ro-retry_attempts',   config['hw_trigger.retry_attempts']   ?? '—');
    set('ro-timeout_seconds',  config['hw_trigger.timeout_seconds']  != null
                                 ? config['hw_trigger.timeout_seconds'] + ' s' : '—');
    set('ro-save_local',       config['hw_trigger.save_local']       != null
                                 ? (config['hw_trigger.save_local'] ? 'Yes' : 'No') : '—');
    set('ro-local_save_dir',   config['hw_trigger.local_save_dir']   ?? '—');
    set('ro-local_max_files',  config['hw_trigger.local_max_files']  ?? '—');
    set('ro-local_max_mb',     config['hw_trigger.local_max_mb']     != null
                                 ? config['hw_trigger.local_max_mb'] + ' MB' : '—');

    onRawToggle();
  } catch (err) {
    showError('Failed to load config: ' + err.message);
  }
}

function set(id, val) {
  const el = document.getElementById(id);
  if (el) el.textContent = val;
}

function onRawToggle() {
  const on  = document.getElementById('send_raw_images').checked;
  const row = document.getElementById('raw-url-row');
  const inp = document.getElementById('raw_destination_url');
  row.classList.toggle('sys-field--disabled', !on);
  inp.disabled = !on;
}

// ── Save ──────────────────────────────────────────────────────────────

async function saveUpload() {
  clearError();
  const patches = {
    'hw_trigger.destination_url':     document.getElementById('destination_url').value.trim(),
    'hw_trigger.send_raw_images':     document.getElementById('send_raw_images').checked,
    'hw_trigger.raw_destination_url': document.getElementById('raw_destination_url').value.trim(),
  };

  const apiKey = document.getElementById('destination_api_key').value;
  if (apiKey) patches['hw_trigger.destination_api_key'] = apiKey;

  try {
    await apiFetch('/api/system/config', {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(patches),
    });
    showSuccess('Saved');
    // Clear password field after save so it stays masked
    document.getElementById('destination_api_key').value = '';
  } catch (err) {
    showError('Save failed: ' + err.message);
  }
}

async function resetField(key) {
  clearError();
  try {
    await apiFetch(`/api/system/config/${encodeURIComponent(key)}`, { method: 'DELETE' });
    showSuccess('Reset to default');
    await loadConfig();
  } catch (err) {
    showError('Reset failed: ' + err.message);
  }
}

// ── Boot ──────────────────────────────────────────────────────────────
loadConfig();
