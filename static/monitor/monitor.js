'use strict';

// ── Small fetch/format helpers (standalone — this page doesn't load portal/index.js) ──

async function apiFetch(path, opts) {
  const res = await fetch(path, opts);
  let data = null;
  try { data = await res.json(); } catch (_) {}
  if (!res.ok) throw new Error((data && data.error) || `HTTP ${res.status}`);
  return data;
}

function escapeHtml(str) {
  return String(str).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function fmtUptime(seconds) {
  if (seconds == null) return '—';
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = Math.floor(seconds % 60);
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m ${s}s`;
  return `${s}s`;
}

// ── Clock ────────────────────────────────────────────────────────────────

function tickClock() {
  const el = document.getElementById('mon-clock');
  if (el) el.textContent = new Date().toLocaleTimeString();
}
setInterval(tickClock, 1000);
tickClock();

// ── Status polling ───────────────────────────────────────────────────────

let _decoderUnavailable = false;

function showUnavailable(message) {
  if (_decoderUnavailable) return;
  _decoderUnavailable = true;
  const main = document.getElementById('mon-main');
  main.innerHTML = `<div class="mon-unavailable"><strong>Decoder unavailable</strong>${escapeHtml(message)}</div>`;
}

function healthState(status) {
  if (!status.running) return { cls: 'down', text: status.port_present === false ? 'Not detected' : 'Stopped' };
  const now = Date.now() / 1000;
  const fresh = status.last_message_at && (now - status.last_message_at) < 5;
  if (fresh) return { cls: 'ok', text: 'Running · Connected' };
  return { cls: 'warn', text: 'Running · No data from Arduino' };
}

function renderStatus(s) {
  const health = healthState(s);
  const dot = document.getElementById('mon-health-dot');
  const text = document.getElementById('mon-health-text');
  const sub = document.getElementById('mon-health-sub');
  dot.className = `mon-health-dot ${health.cls}`;
  text.textContent = health.text;
  sub.textContent = s.last_message_at ? `last message ${Math.max(0, Math.round(Date.now() / 1000 - s.last_message_at))}s ago` : '';

  document.getElementById('mon-speed').innerHTML =
    `${typeof s.speed_cms === 'number' ? s.speed_cms.toFixed(1) : '—'}<span class="mon-tile-unit">cm/s</span>`;
  document.getElementById('mon-triggers').textContent = s.triggers_received ?? '—';
  document.getElementById('mon-encoder-count').textContent = `encoder: ${s.encoder_count ?? '—'}`;

  const capOk = s.captures_ok ?? 0;
  const capFail = s.captures_failed ?? 0;
  const capTile = document.getElementById('mon-captures-tile');
  document.getElementById('mon-captures').textContent = capOk;
  document.getElementById('mon-captures-sub').textContent = `${capOk} ok / ${capFail} failed`;
  capTile.className = `mon-tile${capFail > 0 ? ' bad' : ' ok'}`;

  const upOk = s.uploads_ok ?? 0;
  const upFail = s.uploads_failed ?? 0;
  const upTile = document.getElementById('mon-uploads-tile');
  document.getElementById('mon-uploads').textContent = upOk;
  document.getElementById('mon-uploads-sub').textContent = `${upOk} ok / ${upFail} failed`;
  upTile.className = `mon-tile${upFail > 0 ? ' bad' : ' ok'}`;

  document.getElementById('mon-uptime').textContent = fmtUptime(s.uptime_seconds);

  document.getElementById('mon-active-style').textContent = s.active_style || '—';
}

async function pollStatus() {
  try {
    const s = await apiFetch('/api/decoder/status');
    renderStatus(s);
  } catch (err) {
    showUnavailable(err.message);
  }
}

// ── Queue depths (SSE, same stream the dashboard's Dev Mode uses) ─────────

let _queueStream = null;

function renderQueueDepths(d) {
  const el = document.getElementById('mon-queues');
  if (!el) return;
  const chips = [];
  for (const [id, depth] of Object.entries(d.camera_queues || {})) {
    chips.push([`Cam ${id} capture`, depth]);
  }
  chips.push(['Collector pending', d.collector_pending]);
  chips.push(['Stitch upload', d.stitch_pending]);
  chips.push(['Raw upload', d.raw_pending]);
  chips.push(['Disk retry', d.disk_retry]);
  chips.push(['Disk spill', d.disk_spill]);

  el.innerHTML = chips.map(([label, depth]) => `
    <span class="mon-queue-chip${depth > 0 ? ' nonzero' : ''}">
      <span class="mon-queue-chip-label">${escapeHtml(label)}</span>
      <span class="mon-queue-chip-val">${depth ?? 0}</span>
    </span>`).join('');
}

function startQueueStream() {
  if (_queueStream) return;
  try {
    _queueStream = new EventSource('/api/decoder/queues/stream');
    _queueStream.onmessage = (e) => {
      try { renderQueueDepths(JSON.parse(e.data)); } catch (_) {}
    };
    _queueStream.onerror = () => {
      // Browser auto-retries SSE connections; nothing to do here.
    };
  } catch (_) {}
}

document.addEventListener('DOMContentLoaded', () => {
  pollStatus();
  startQueueStream();

  setInterval(pollStatus, 2000);
});
