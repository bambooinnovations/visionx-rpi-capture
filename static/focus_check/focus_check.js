'use strict';

const FPS = 4;
const POLL_MS = 250;
const HISTORY = 120;
const GOOD_PCT = 95;
const NEAR_PCT = 80;

const state = {
  cameras: [],
  cameraId: 0,
  history: [],
  lastTs: 0,
  poll: null,
  sound: false,
};

const $ = id => document.getElementById(id);

function showError(msg) {
  $('error-text').textContent = msg;
  $('error-banner').classList.remove('hidden');
}
function clearError() { $('error-banner').classList.add('hidden'); }

// ── Stream ──────────────────────────────────────────────────────────────
function startStream() {
  const img = $('fc-img');
  const url = `/api/cameras/focus-check/stream?camera_id=${state.cameraId}&fps=${FPS}`;
  state.history = [];
  state.lastTs = 0;
  $('stream-placeholder').style.display = '';
  img.onload = () => { $('stream-placeholder').style.display = 'none'; };
  img.onerror = async () => {
    img.onerror = null;
    let msg = 'Stream unavailable.';
    try {
      const r = await fetch(url);
      if (r.status === 409) msg = 'Camera is in Hardware Trigger mode. Disable it on the main page to stream.';
    } catch (_) {}
    showError(msg);
  };
  img.src = url;

  clearInterval(state.poll);
  state.poll = setInterval(pollMetrics, POLL_MS);
}

function stopStream() {
  clearInterval(state.poll);
  $('fc-img').src = '';
  setTone(null);
}

async function pollMetrics() {
  let m;
  try {
    const r = await fetch(`/api/cameras/focus-check/metrics?camera_id=${state.cameraId}`);
    if (!r.ok) return;
    m = await r.json();
  } catch (_) { return; }
  if (m.ts === state.lastTs) return;   // no new frame yet
  state.lastTs = m.ts;
  render(m);
}

async function resetBest() {
  state.history = [];
  try { await fetch(`/api/cameras/focus-check/reset?camera_id=${state.cameraId}`, { method: 'POST' }); } catch (_) {}
}

// ── Rendering ───────────────────────────────────────────────────────────
function verdictFor(pct) {
  if (pct >= GOOD_PCT) return ['good', 'FOCUSED'];
  if (pct >= NEAR_PCT) return ['near', 'ALMOST'];
  return ['bad', 'OUT OF FOCUS'];
}

function trendOf() {
  const h = state.history;
  if (h.length < 4) return null;
  const recent = h.slice(-3).reduce((a, b) => a + b, 0) / 3;
  const before = h.slice(-8, -3);
  if (!before.length) return null;
  const prev = before.reduce((a, b) => a + b, 0) / before.length;
  if (recent - prev > 2) return 'up';
  if (recent - prev < -2) return 'down';
  return 'flat';
}

function render(m) {
  const readout = $('readout');

  if (m.pct_of_best == null || m.smoothed_px == null) {
    readout.dataset.state = 'wait';
    $('verdict').textContent = m.reason || 'Waiting for board…';
    $('pct').textContent = '--';
    $('trend').textContent = ' ';
    $('trend').className = 'fc-trend';
    $('meter-fill').style.width = '0';
    $('st-edge').textContent = '--';
    $('st-best').textContent = m.best_px != null ? `${m.best_px.toFixed(2)} px` : '--';
    $('st-board').textContent = m.corners ? `${m.corners} corners` : '--';
    $('note').textContent = '';
    renderZones(null);
    setTone(null);
    return;
  }

  state.history.push(m.pct_of_best);
  if (state.history.length > HISTORY) state.history.shift();

  const [st, label] = verdictFor(m.pct_of_best);
  readout.dataset.state = st;
  $('verdict').textContent = label;
  $('pct').textContent = Math.round(m.pct_of_best);
  $('meter-fill').style.width = `${m.pct_of_best}%`;

  const t = trendOf();
  const trendEl = $('trend');
  if (st === 'good') { trendEl.textContent = '● AT BEST FOCUS — hold here'; trendEl.className = 'fc-trend up'; }
  else if (t === 'up') { trendEl.textContent = '↑ Getting sharper — keep turning'; trendEl.className = 'fc-trend up'; }
  else if (t === 'down') { trendEl.textContent = '↓ Getting blurrier — turn back'; trendEl.className = 'fc-trend down'; }
  else { trendEl.textContent = 'Turn the focus ring slowly'; trendEl.className = 'fc-trend flat'; }

  $('st-edge').textContent = `${m.smoothed_px.toFixed(2)} px`;
  $('st-best').textContent = `${m.best_px.toFixed(2)} px`;
  $('st-board').textContent = `${m.corners} corners · ${m.square_px.toFixed(0)} px/sq`;

  let note = '';
  if (m.tracked) note = 'Board too blurry to re-detect — measuring at its last known position. Do not move the board.';
  else if (m.square_px < 20) note = 'Board is small in the frame — bring it closer or use a larger print for a finer reading.';
  $('note').textContent = note;

  renderZones(m);
  drawSpark();
  setTone(m.pct_of_best);
}

function renderZones(m) {
  const wrap = $('zones');
  if (!m || !m.zones || !m.zones.length) {
    wrap.innerHTML = Array(9).fill('<div class="fc-zone none">–</div>').join('');
    $('zones-hint').textContent = '';
    return;
  }
  const best = m.best_px || 1;
  const vals = [];
  wrap.innerHTML = m.zones.flat().map(v => {
    if (v == null) return '<div class="fc-zone none">–</div>';
    vals.push(v);
    const ratio = Math.min(1, best / v);
    const hue = Math.round(ratio * ratio * 120);          // 0 red … 120 green
    return `<div class="fc-zone" style="background:hsl(${hue} 70% 50%)">${v.toFixed(1)}</div>`;
  }).join('');

  const hint = $('zones-hint');
  if (vals.length >= 3) {
    const spread = Math.max(...vals) / Math.min(...vals);
    hint.textContent = spread > 1.4
      ? 'Sharpness varies across the field — check the board is flat and square to the camera (or lens/sensor tilt).'
      : 'Numbers are edge width in px (lower = sharper). Even across the field.';
  } else {
    hint.textContent = 'Only part of the field has board coverage.';
  }
}

function drawSpark() {
  const c = $('spark');
  const g = c.getContext('2d');
  const w = c.width, h = c.height;
  g.clearRect(0, 0, w, h);
  const y = p => h - 4 - (h - 8) * (p / 100);

  g.strokeStyle = '#ffffff55'; g.setLineDash([6, 6]); g.lineWidth = 1;
  g.beginPath(); g.moveTo(0, y(GOOD_PCT)); g.lineTo(w, y(GOOD_PCT)); g.stroke();
  g.setLineDash([]);

  const hs = state.history;
  if (hs.length < 2) return;
  g.strokeStyle = '#60a5fa'; g.lineWidth = 3; g.lineJoin = 'round';
  g.beginPath();
  hs.forEach((p, i) => {
    const x = (i / (HISTORY - 1)) * w;
    if (i) g.lineTo(x, y(p)); else g.moveTo(x, y(p));
  });
  g.stroke();
}

// ── Audio: pitch follows sharpness so you can watch the lens, not the screen ─
let audio = null;

function setTone(pct) {
  if (!audio) return;
  if (pct == null || !state.sound) { audio.gain.gain.setTargetAtTime(0, audio.ctx.currentTime, 0.05); return; }
  const f = 220 * Math.pow(2, 2.5 * Math.pow(pct / 100, 2));   // 220 Hz … ~1245 Hz, steeper near peak
  audio.osc.frequency.setTargetAtTime(f, audio.ctx.currentTime, 0.05);
  audio.gain.gain.setTargetAtTime(0.12, audio.ctx.currentTime, 0.05);
}

function toggleSound() {
  state.sound = !state.sound;
  if (state.sound && !audio) {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    gain.gain.value = 0;
    osc.type = 'sine';
    osc.connect(gain).connect(ctx.destination);
    osc.start();
    audio = { ctx, osc, gain };
  }
  if (audio && audio.ctx.state === 'suspended') audio.ctx.resume();
  const b = $('btn-sound');
  b.textContent = state.sound ? '🔊 Sound on' : '🔇 Sound off';
  b.classList.toggle('on', state.sound);
  if (!state.sound) setTone(null);
}

// ── Misc UI ─────────────────────────────────────────────────────────────
function toggleFullscreen() {
  if (document.fullscreenElement) document.exitFullscreen();
  else document.documentElement.requestFullscreen().catch(() => {});
}

function switchCamera(id) {
  state.cameraId = id;
  buildCameraButtons();
  $('fc-title').textContent = `Focus Check — Camera ${id}`;
  $('back-link').href = `/calibrate?camera=${id}`;
  startStream();
}

function buildCameraButtons() {
  const box = $('camera-control');
  if (state.cameras.length <= 1) { box.classList.add('hidden'); return; }
  box.classList.remove('hidden');
  box.innerHTML = state.cameras.map(c =>
    `<button class="${c.camera_id === state.cameraId ? 'active' : ''}" onclick="switchCamera(${c.camera_id})">${c.camera_id}</button>`
  ).join('');
}

async function init() {
  clearError();
  try {
    const r = await fetch('/api/cameras');
    const j = r.ok ? await r.json() : [];
    state.cameras = Array.isArray(j) ? j : [];
  } catch (_) { state.cameras = []; }

  const p = new URLSearchParams(location.search).get('camera');
  if (p !== null && state.cameras.some(c => c.camera_id === parseInt(p, 10))) {
    state.cameraId = parseInt(p, 10);
  } else if (state.cameras.length) {
    state.cameraId = state.cameras[0].camera_id;
  }

  $('fc-title').textContent = `Focus Check — Camera ${state.cameraId}`;
  $('back-link').href = `/calibrate?camera=${state.cameraId}`;
  buildCameraButtons();
  renderZones(null);
  startStream();
}

window.addEventListener('beforeunload', stopStream);
document.addEventListener('DOMContentLoaded', init);
