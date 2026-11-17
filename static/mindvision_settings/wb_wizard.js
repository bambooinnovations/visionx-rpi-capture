// Guided white balance wizard (settings page). Loaded after settings.js, whose helpers it reuses
// (apiFetch, applyChanges, onSettingChange, setExposureMs, ...).
//
// Follows the vendor's procedure: draw a box on a white patch, bring its G level to ~200 with the exposure,
// run one-push white balance on that box (D65), then tune saturation. Everything stays a draft until the
// operator uses the page's "Save to production"; Cancel puts back what the wizard changed.

const WB = {
  open: false,
  step: 1,
  box: {x: 0.35, y: 0.35, w: 0.3, h: 0.3},   // fractions of the image
  busy: false,        // a tune / auto-exposure run owns the camera; the live loop pauses
  inflight: false,
  timer: null,
  ready: false,       // the box exposure is usable for white balance (not dark, not clipped)
  tuned: false,        // white balance has run for the current box and exposure
  tempChanged: false,  // the camera's colour temperature was switched to D65, so Cancel must put it back
  orig: null,         // page settings to restore on Cancel
  gTarget: 200,
  seq: 0,
};

const $ = id => document.getElementById(id);
const $wb = $;

// ── Open / close ──────────────────────────────────────────────────────

async function openWbWizard() {
  if (WB.open) return;
  clearError();
  let guide;
  try {
    guide = await apiFetch(`${API}/white-balance/guide?camera_id=${CAMERA_ID}`);
  } catch (e) {
    showError('Guided white balance unavailable: ' + e.message);
    return;
  }
  if (guide.monochrome) {
    showError('White balance is not applicable to monochrome cameras');
    return;
  }
  if (guide.color_temps.d65_index == null) {
    const names = guide.color_temps.presets.map(p => p.name).join(', ') || 'none';
    showError(`This camera has no D65 colour-temperature preset (it offers: ${names})`);
    return;
  }
  WB.gTarget = guide.g_target || 200;

  WB.orig = {
    ae: $('ae-enabled').checked,
    exposureMs: getExposureMs(),
    saturation: $('saturation').value,
    r: $('r-gain').value, g: $('g-gain').value, b: $('b-gain').value,
    colorTemp: guide.color_temps.current,
  };
  WB.open = true;
  WB.tuned = false;
  WB.tempChanged = false;
  WB.ready = false;
  WB.busy = false;
  $wb('wb-img').removeAttribute('src');
  $wb('wb-error').classList.add('hidden');
  $wb('wb-result').classList.add('hidden');
  $wb('wb-result').replaceChildren();
  setWbStatus('wb-hint', 'Waiting for the camera…', '');
  $wb('wb-g-value').textContent = '–';
  $wb('wb-rgb').textContent = 'R – · G – · B –';

  // White balance needs a fixed exposure, so the wizard works in manual exposure.
  if (WB.orig.ae) {
    $('ae-enabled').checked = false;
    updateAutoRows();
  }
  syncWbExposure();
  syncWbSaturation();
  renderWbBox();
  showWbStep(1);
  $wb('wb-wizard').classList.remove('hidden');
  clearTimeout(liveTimer);
  await applyChanges();
  scheduleWbMeasure(0);
}

function closeWbWizard() {
  WB.open = false;
  WB.seq++;
  clearTimeout(WB.timer);
  $wb('wb-wizard').classList.add('hidden');
}

async function cancelWbWizard() {
  if (!WB.open) return;
  WB.busy = true;           // keep the live loop off the camera while it is put back
  closeWbWizard();
  const o = WB.orig;
  $('ae-enabled').checked = o.ae;
  updateAutoRows();
  setExposureMs(o.exposureMs);
  for (const [id, v] of [['r-gain', o.r], ['g-gain', o.g], ['b-gain', o.b]]) {
    $(id).value = v;
    $(id + '-value').value = v;
  }
  $('saturation').value = o.saturation;
  $('saturation-value').value = o.saturation;
  clearTimeout(liveTimer);
  try {
    await applyChanges();
    if (WB.tempChanged) {
      await apiFetch(`${API}/white-balance/color-temp?camera_id=${CAMERA_ID}`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(o.colorTemp),
      });
    }
  } catch (e) {
    showError('Could not fully put back the settings: ' + e.message);
  }
  WB.busy = false;
}

async function finishWbWizard() {
  WB.busy = true;
  closeWbWizard();
  clearTimeout(liveTimer);
  await applyChanges();
  await loadSettings();   // the page controls take the values the camera now holds
  WB.busy = false;
  showSuccess('White balance applied as a draft. Check it, then Save to production.');
}

// ── Steps ─────────────────────────────────────────────────────────────

function showWbStep(n) {
  WB.step = n;
  for (let i = 1; i <= 3; i++) {
    $wb('wb-panel-' + i).classList.toggle('hidden', i !== n);
    const tab = $wb('wb-step-tab-' + i);
    tab.classList.toggle('active', i === n);
    tab.classList.toggle('done', i < n);
  }
  $wb('wb-back').classList.toggle('hidden', n === 1);
  $wb('wb-next').classList.toggle('hidden', n === 3);
  $wb('wb-finish').classList.toggle('hidden', n !== 3);
  // The box and exposure are locked in once white balance has run, so a later change can't leave gains that
  // no longer match what is on screen.
  $wb('wb-view').style.pointerEvents = n === 1 ? '' : 'none';
  $wb('wb-view').style.cursor = n === 1 ? '' : 'default';
  updateWbButtons();
}

function updateWbButtons() {
  const next = $wb('wb-next');
  if (WB.step === 1) next.disabled = !WB.ready || WB.busy;
  else if (WB.step === 2) next.disabled = !WB.tuned || WB.busy;
  $wb('wb-run').disabled = WB.busy;
  $wb('wb-auto-exposure').disabled = WB.busy;
  $wb('wb-back').disabled = WB.busy;
}

function setWbStatus(id, text, kind) {
  const el = $wb(id);
  el.textContent = text;
  el.classList.remove('wb-status-ok', 'wb-status-warn', 'wb-status-bad');
  if (kind) el.classList.add('wb-status-' + kind);
}

function showWbError(msg) {
  const el = $wb('wb-error');
  el.textContent = msg;
  el.classList.toggle('hidden', !msg);
}

// ── Live measuring ────────────────────────────────────────────────────

function scheduleWbMeasure(delay = 400) {
  clearTimeout(WB.timer);
  if (!WB.open) return;
  WB.timer = setTimeout(wbMeasureLoop, delay);
}

async function wbMeasureOnce() {
  const seq = WB.seq;
  const res = await apiFetch(`${API}/white-balance/measure?camera_id=${CAMERA_ID}`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({box: WB.box}),
  });
  if (seq !== WB.seq) return null;   // the wizard was closed meanwhile
  renderWbMeasure(res);
  return res.stats;
}

async function wbMeasureLoop() {
  if (!WB.open) return;
  if (!WB.busy && !WB.inflight) {
    WB.inflight = true;
    try {
      await wbMeasureOnce();
      showWbError('');
    } catch (e) {
      WB.ready = false;
      updateWbButtons();
      showWbError(e.message);
    } finally {
      WB.inflight = false;
    }
  }
  scheduleWbMeasure(WB.step === 1 ? 300 : 700);
}

function renderWbMeasure(res) {
  if (res.image) $wb('wb-img').src = res.image;
  const s = res.stats;
  $wb('wb-g-value').textContent = Math.round(s.g);
  $wb('wb-rgb').textContent = `R ${Math.round(s.r)} · G ${Math.round(s.g)} · B ${Math.round(s.b)}`;
  const kind = {ok: 'ok', low: 'warn', high: 'warn', dark: 'bad', clipped: 'bad'}[s.exposure.state];
  setWbStatus('wb-hint', s.exposure.message, kind);
  WB.ready = s.exposure.state !== 'dark' && s.exposure.state !== 'clipped';
  updateWbButtons();
}

// ── Box: draw / move / resize (fractions of the image) ───────────────

function renderWbBox() {
  const b = WB.box, el = $wb('wb-box');
  el.style.left = (b.x * 100) + '%';
  el.style.top = (b.y * 100) + '%';
  el.style.width = (b.w * 100) + '%';
  el.style.height = (b.h * 100) + '%';
}

function wbPoint(ev) {
  const r = $wb('wb-view').getBoundingClientRect();
  return {
    x: Math.min(1, Math.max(0, (ev.clientX - r.left) / r.width)),
    y: Math.min(1, Math.max(0, (ev.clientY - r.top) / r.height)),
  };
}

function wireWbBox() {
  const view = $wb('wb-view');
  let drag = null;
  view.addEventListener('pointerdown', ev => {
    if (WB.step !== 1) return;
    const p = wbPoint(ev);
    if (ev.target === $wb('wb-handle')) {
      drag = {mode: 'resize'};
    } else if (ev.target === $wb('wb-box')) {
      drag = {mode: 'move', dx: p.x - WB.box.x, dy: p.y - WB.box.y};
    } else {
      drag = {mode: 'draw', x0: p.x, y0: p.y};
      WB.box = {x: p.x, y: p.y, w: 0, h: 0};
    }
    view.setPointerCapture(ev.pointerId);
    ev.preventDefault();
  });
  view.addEventListener('pointermove', ev => {
    if (!drag) return;
    const p = wbPoint(ev), b = WB.box;
    if (drag.mode === 'draw') {
      WB.box = {x: Math.min(drag.x0, p.x), y: Math.min(drag.y0, p.y),
                w: Math.abs(p.x - drag.x0), h: Math.abs(p.y - drag.y0)};
    } else if (drag.mode === 'move') {
      WB.box = {...b, x: Math.min(1 - b.w, Math.max(0, p.x - drag.dx)),
                      y: Math.min(1 - b.h, Math.max(0, p.y - drag.dy))};
    } else {
      WB.box = {...b, w: Math.max(0.02, p.x - b.x), h: Math.max(0.02, p.y - b.y)};
    }
    renderWbBox();
  });
  const end = () => { drag = null; };
  view.addEventListener('pointerup', end);
  view.addEventListener('pointercancel', end);
}

// ── Exposure ──────────────────────────────────────────────────────────

function syncWbExposure() {
  $wb('wb-exposure').value = exposureMsToPos(getExposureMs());
  $wb('wb-exposure-value').value = getExposureMs();
}

function setWbExposure(ms) {
  setExposureMs(ms);
  syncWbExposure();
  onSettingChange();
}

// Move the exposure until the box's G is close to the target. G is not linear in exposure (gamma, clipping), so
// each pass corrects by a damped ratio and re-measures; a clipped box is treated as brighter than it reads.
async function autoSetWbExposure() {
  if (WB.busy) return;
  WB.busy = true;
  updateWbButtons();
  showWbError('');
  try {
    clearTimeout(liveTimer);
    await applyChanges();
    for (let i = 0; i < 8; i++) {
      const s = await wbMeasureOnce();
      if (s == null) return;
      const clipped = s.exposure.state === 'clipped';
      if (!clipped && Math.abs(s.g - WB.gTarget) <= 6) break;
      const g = clipped ? 255 : Math.max(s.g, 1);
      let factor = Math.pow(WB.gTarget / g, 0.8);
      if (clipped) factor = Math.min(factor, 0.5);
      const before = getExposureMs();
      const ms = setExposureMs(before * factor);
      syncWbExposure();
      if (ms === before) {
        showWbError(ms <= exposureMinMs
          ? 'Already at the shortest exposure; the white patch is still too bright. Reduce the light or the aperture.'
          : 'Already at the longest exposure; the white patch is still too dark. Add light or open the aperture.');
        break;
      }
      await applyChanges();
    }
    await wbMeasureOnce();
  } catch (e) {
    showWbError(e.message);
  } finally {
    WB.busy = false;
    updateWbButtons();
  }
}

// ── White balance ─────────────────────────────────────────────────────

async function runWbTune() {
  if (WB.busy) return;
  WB.busy = true;
  updateWbButtons();
  showWbError('');
  $wb('wb-run').textContent = 'Balancing…';
  try {
    clearTimeout(liveTimer);
    await applyChanges();
    const res = await apiFetch(`${API}/white-balance/tune?camera_id=${CAMERA_ID}`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({box: WB.box}),
    });
    WB.tuned = true;
    WB.tempChanged = true;
    // Keep the page's gain controls in step with the camera so the next apply doesn't undo the result.
    for (const [id, v] of [['r-gain', res.gains.r_gain], ['g-gain', res.gains.g_gain], ['b-gain', res.gains.b_gain]]) {
      $(id).value = v;
      $(id + '-value').value = v;
    }
    renderWbResult(res);
    await wbMeasureOnce();
    // The camera now holds a draft (new gains and colour temperature); refresh the page's draft list.
    const s = await apiFetch(`${API}/settings?camera_id=${CAMERA_ID}`);
    renderDraft(s.draft_changes);
  } catch (e) {
    showWbError(e.message);
  } finally {
    WB.busy = false;
    $wb('wb-run').textContent = WB.tuned ? 'Run again' : 'Run white balance';
    updateWbButtons();
  }
}

function renderWbResult(res) {
  const box = $wb('wb-result');
  box.replaceChildren();
  const table = document.createElement('table');
  const head = table.insertRow();
  for (const t of ['', 'Red', 'Green', 'Blue']) {
    const th = document.createElement('th');
    th.textContent = t;
    head.append(th);
  }
  const addRow = (label, vals) => {
    const tr = table.insertRow();
    const th = document.createElement('th');
    th.textContent = label;
    tr.append(th);
    for (const v of vals) tr.insertCell().textContent = v;
  };
  addRow('Gain before', [res.before.r_gain, res.before.g_gain, res.before.b_gain].map(v => (v / 100).toFixed(2)));
  addRow('Gain now', [res.gains.r_gain, res.gains.g_gain, res.gains.b_gain].map(v => (v / 100).toFixed(2)));
  addRow('Patch level', [res.stats.r, res.stats.g, res.stats.b].map(v => Math.round(v)));
  box.append(table);
  const note = document.createElement('div');
  note.className = 'wb-status ' + (res.neutral ? 'wb-status-ok' : 'wb-status-warn');
  note.textContent = res.neutral
    ? `The white patch is neutral (R, G and B within ${res.spread.toFixed(1)} levels).`
    : `The patch is still tinted (R, G and B differ by ${res.spread.toFixed(1)} levels). Check the box sits fully on the white patch, or press Back and redraw it.`;
  box.append(note);
  box.classList.remove('hidden');
}

// ── Saturation ────────────────────────────────────────────────────────

function syncWbSaturation() {
  const slider = $wb('wb-saturation');
  slider.min = $('saturation').min;
  slider.max = $('saturation').max;
  slider.value = $('saturation').value;
  $wb('wb-saturation-value').value = $('saturation').value;
}

function setWbSaturation(v) {
  const lo = Number($('saturation').min), hi = Number($('saturation').max);
  v = Math.min(hi, Math.max(lo, Math.round(Number(v))));
  if (Number.isNaN(v)) return;
  $('saturation').value = v;
  $('saturation-value').value = v;
  $wb('wb-saturation').value = v;
  $wb('wb-saturation-value').value = v;
  onSettingChange();
}

// ── Wiring ────────────────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', () => {
  wireWbBox();
  $wb('wb-exposure').addEventListener('input', e => setWbExposure(exposurePosToMs(Number(e.target.value))));
  $wb('wb-exposure-value').addEventListener('change', e => {
    const ms = Number(e.target.value);
    if (ms > 0) setWbExposure(ms); else syncWbExposure();
  });
  $wb('wb-auto-exposure').addEventListener('click', autoSetWbExposure);
  $wb('wb-run').addEventListener('click', runWbTune);
  $wb('wb-saturation').addEventListener('input', e => setWbSaturation(e.target.value));
  $wb('wb-saturation-value').addEventListener('change', e => setWbSaturation(e.target.value));
  $wb('wb-next').addEventListener('click', () => { showWbStep(WB.step + 1); });
  $wb('wb-back').addEventListener('click', () => {
    // Going back to the box or exposure invalidates the white balance that was run, so it has to be run again.
    if (WB.step === 2) { WB.tuned = false; $wb('wb-run').textContent = 'Run white balance'; $wb('wb-result').classList.add('hidden'); }
    showWbStep(WB.step - 1);
  });
  $wb('wb-cancel').addEventListener('click', cancelWbWizard);
  $wb('wb-finish').addEventListener('click', finishWbWizard);
  document.addEventListener('keydown', e => { if (e.key === 'Escape' && WB.open) cancelWbWizard(); });
});
