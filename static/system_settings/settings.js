'use strict';

// ── Notifications ─────────────────────────────────────────────────────

function showError(msg, { saveAnyway = false } = {}) {
  document.getElementById('error-text').textContent = msg;
  document.getElementById('error-banner').classList.remove('hidden');
  document.getElementById('save-anyway-btn').classList.toggle('hidden', !saveAnyway);
}
function clearError() {
  document.getElementById('error-banner').classList.add('hidden');
  document.getElementById('save-anyway-btn').classList.add('hidden');
}

let _toastTimer = null;
function showSuccess(msg = 'Saved') {
  const el = document.getElementById('success-toast');
  el.textContent = msg;
  el.classList.remove('hidden');
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => el.classList.add('hidden'), 2500);
}

// ── Field-level errors ────────────────────────────────────────────────

function setFieldError(id, msg) {
  const errEl = document.getElementById('err-' + id);
  const inpEl = document.getElementById(id);
  if (msg) {
    if (errEl) { errEl.textContent = msg; errEl.classList.remove('hidden'); }
    if (inpEl) inpEl.classList.add('sys-input--error');
  } else {
    if (errEl) errEl.classList.add('hidden');
    if (inpEl) inpEl.classList.remove('sys-input--error');
  }
}

function clearFieldErrors() {
  ['destination_url', 'raw_destination_url'].forEach(id => setFieldError(id, null));
}

// ── Validation ────────────────────────────────────────────────────────

function validateUrlFormat(url) {
  if (!url) return null; // empty = clear the field, allowed
  try {
    const u = new URL(url);
    if (!['http:', 'https:'].includes(u.protocol)) return 'URL must use http:// or https://';
    return null;
  } catch {
    return 'Not a valid URL — expected http://host/path';
  }
}

async function checkServerHealth(url) {
  const res = await fetch(`/api/system/check-url?${new URLSearchParams({ url })}`);
  return res.json(); // { ok: bool, error?: string }
}

// ── Save button state ─────────────────────────────────────────────────

function setSaving(on) {
  const btn = document.getElementById('save-btn');
  if (!btn) return;
  btn.disabled = on;
  btn.textContent = on ? 'Checking…' : 'Save';
}

// ── API helper ────────────────────────────────────────────────────────

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

    document.getElementById('destination_url').value     = config['hw_trigger.destination_url']     || '';
    document.getElementById('destination_api_key').value = '';
    document.getElementById('use_stitch').checked         = !!config['hw_trigger.use_stitch'];
    document.getElementById('send_raw_images').checked   = !!config['hw_trigger.send_raw_images'];
    document.getElementById('raw_destination_url').value = config['hw_trigger.raw_destination_url'] || '';

    set('ro-serial_port',     config['hw_trigger.serial_port']      ?? '—');
    set('ro-serial_baud',     config['hw_trigger.serial_baud']      ?? '—');
    set('ro-retry_attempts',  config['hw_trigger.retry_attempts']   ?? '—');
    set('ro-timeout_seconds', config['hw_trigger.timeout_seconds']  != null
                                ? config['hw_trigger.timeout_seconds'] + ' s' : '—');
    set('ro-save_local',      config['hw_trigger.save_local']       != null
                                ? (config['hw_trigger.save_local'] ? 'Yes' : 'No') : '—');
    set('ro-local_save_dir',  config['hw_trigger.local_save_dir']   ?? '—');
    set('ro-local_max_files', config['hw_trigger.local_max_files']  ?? '—');
    set('ro-local_max_mb',    config['hw_trigger.local_max_mb']     != null
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

async function saveUpload(force = false) {
  clearError();
  clearFieldErrors();

  const destUrl = document.getElementById('destination_url').value.trim();
  const rawUrl  = document.getElementById('raw_destination_url').value.trim();
  const rawOn   = document.getElementById('send_raw_images').checked;

  if (!force) {
    // Format validation
    const destErr = validateUrlFormat(destUrl);
    if (destErr) { setFieldError('destination_url', destErr); return; }

    if (rawOn) {
      const rawErr = validateUrlFormat(rawUrl);
      if (rawErr) { setFieldError('raw_destination_url', rawErr); return; }
    }

    // Health check (only if a URL is set)
    if (destUrl) {
      setSaving(true);
      let health;
      try {
        health = await checkServerHealth(destUrl);
      } catch {
        health = { ok: false, error: 'Could not reach server' };
      } finally {
        setSaving(false);
      }

      if (!health.ok) {
        showError(`Health check failed: ${health.error}`, { saveAnyway: true });
        return;
      }
    }
  }

  // Persist
  const patches = {
    'hw_trigger.destination_url':     destUrl,
    'hw_trigger.use_stitch':          document.getElementById('use_stitch').checked,
    'hw_trigger.send_raw_images':     rawOn,
    'hw_trigger.raw_destination_url': rawUrl,
  };
  const apiKey = document.getElementById('destination_api_key').value;
  if (apiKey) patches['hw_trigger.destination_api_key'] = apiKey;

  setSaving(true);
  try {
    await apiFetch('/api/system/config', {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(patches),
    });
    showSuccess('Saved');
    document.getElementById('destination_api_key').value = '';
  } catch (err) {
    showError('Save failed: ' + err.message);
  } finally {
    setSaving(false);
  }
}

function saveUploadForce() { saveUpload(true); }

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
