"""Minimal test server for hardware-trigger image ingestion.

Start with:  uv run --group test-server python test_server/server.py
Then open:   http://<pi-ip>:8888/

POST /upload             — stitched capture endpoint
POST /upload-raw         — raw per-camera capture endpoint
GET  /                   — real-time monitor page
GET  /mode               — {"lean": bool}
POST /mode               — {"lean": bool}  toggle lean mode at runtime
GET  /images/{filename}  — serve a received image (normal mode only)

Lean mode:
  - image bytes freed immediately after size is measured (no memory retention)
  - EXIF parsing skipped entirely
  - headers stripped from payload
  - broadcast fired as a background task so HTTP response returns instantly
  - browser renders compact one-line cards and caps the feed at 100 entries
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

_parser = argparse.ArgumentParser(add_help=False)
_parser.add_argument("--lean", action="store_true", help="start in lean mode")
_parser.add_argument("--port", type=int, default=8888)
_args, _ = _parser.parse_known_args()

PORT: int   = _args.port
_lean: bool = _args.lean

SAVE_DIR = Path(__file__).parent / "received"
SAVE_DIR.mkdir(exist_ok=True)


def _extract_exif(jpeg_bytes: bytes) -> dict:
    try:
        import piexif
        exif     = piexif.load(jpeg_bytes)
        out: dict = {}
        ifd0     = exif.get("0th", {})
        exif_ifd = exif.get("Exif", {})

        def _str(val):
            return val.decode(errors="replace").rstrip("\x00") if isinstance(val, bytes) else val

        make   = _str(ifd0.get(piexif.ImageIFD.Make, ""))
        model  = _str(ifd0.get(piexif.ImageIFD.Model, ""))
        serial = _str(ifd0.get(piexif.ImageIFD.CameraSerialNumber, ""))
        dt     = _str(ifd0.get(piexif.ImageIFD.DateTime, ""))
        if make:   out["make"]          = make
        if model:  out["model"]         = model
        if serial: out["serial_number"] = serial
        if dt:     out["datetime"]      = dt

        exp = exif_ifd.get(piexif.ExifIFD.ExposureTime)
        if exp and isinstance(exp, tuple) and exp[1]:
            us = exp[0]
            out["exposure_time"] = f"{us} µs ({us / 1000:.2f} ms)"

        iso = exif_ifd.get(piexif.ExifIFD.ISOSpeedRatings)
        if iso is not None:
            out["analog_gain"] = f"{iso}%"

        body_serial = _str(exif_ifd.get(piexif.ExifIFD.BodySerialNumber, ""))
        if body_serial and body_serial != serial:
            out["body_serial"] = body_serial

        return out
    except Exception:
        return {}


app = FastAPI(title="VisionX HW Trigger Test Server")

_clients: list[WebSocket] = []


async def _broadcast(payload: dict) -> None:
    dead = []
    msg  = json.dumps(payload)
    for ws in _clients:
        try:
            await ws.send_text(msg)
        except Exception:
            dead.append(ws)
    for ws in dead:
        _clients.remove(ws)


async def _handle_upload(request: Request, kind: str):
    form = await request.form()

    image_field = form.get("image") or next(
        (v for v in form.values() if hasattr(v, "read")), None
    )
    if image_field is None:
        return JSONResponse({"error": "no image file found in form data"}, status_code=400)

    meta    = {k: v for k, v in form.items() if not hasattr(v, "read")}
    content = await image_field.read()
    file_size = len(content)

    image_id = str(uuid.uuid4())[:8]

    if _lean:
        # Free the image bytes immediately — don't hold them across the broadcast.
        del content

        payload: dict = {
            "type":              "capture",
            "id":                image_id,
            "kind":              kind,
            "received_at":       datetime.now(timezone.utc).isoformat(),
            "original_filename": image_field.filename,
            "file_size_bytes":   file_size,
            "meta":              meta,
        }
        # Fire-and-forget: HTTP response returns to the RPi without waiting
        # for potentially-slow WebSocket sends to finish.
        asyncio.create_task(_broadcast(payload))
        return JSONResponse({"status": "received", "id": image_id})

    else:
        filename = f"{int(time.time())}_{image_id}.jpg"
        exif_info = _extract_exif(content)
        (SAVE_DIR / filename).write_bytes(content)
        del content

        payload = {
            "type":              "capture",
            "id":                image_id,
            "kind":              kind,
            "received_at":       datetime.now(timezone.utc).isoformat(),
            "filename":          filename,
            "original_filename": image_field.filename,
            "content_type":      image_field.content_type,
            "file_size_bytes":   file_size,
            "meta":              meta,
            "exif":              exif_info,
            "headers":           dict(request.headers),
            "image_url":         f"/images/{filename}",
        }
        await _broadcast(payload)
        return JSONResponse({"status": "received", **{k: v for k, v in payload.items() if k != "type"}})


@app.post("/upload")
async def upload(request: Request):
    return await _handle_upload(request, "stitch")


@app.post("/upload-raw")
async def upload_raw(request: Request):
    return await _handle_upload(request, "raw")


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/mode")
async def get_mode():
    return {"lean": _lean}


@app.post("/mode")
async def set_mode(request: Request):
    global _lean
    body  = await request.json()
    _lean = bool(body.get("lean", _lean))
    await _broadcast({"type": "mode", "lean": _lean})
    return {"lean": _lean}


@app.get("/images/{filename}")
async def serve_image(filename: str):
    path = SAVE_DIR / filename
    if not path.exists():
        return HTMLResponse("Not found", status_code=404)
    return FileResponse(path, media_type="image/jpeg")


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    _clients.append(websocket)
    await websocket.send_text(json.dumps({"type": "mode", "lean": _lean}))
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        if websocket in _clients:
            _clients.remove(websocket)


_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>VisionX HW Trigger Monitor</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: monospace; background: #0f0f0f; color: #d4d4d4; padding: 24px; }

  header { display: flex; align-items: center; gap: 16px; margin-bottom: 20px; }
  h1 { color: #4ade80; font-size: 1.1rem; letter-spacing: 0.05em; }
  #count { font-size: 0.75rem; color: #555; }
  #status { font-size: 0.75rem; color: #666; margin-left: auto; }
  #status.connected { color: #4ade80; }

  .lean-toggle { display: flex; align-items: center; gap: 8px; margin-left: 8px; }
  .lean-toggle label { font-size: 0.72rem; color: #888; cursor: pointer; user-select: none; }
  .lean-toggle label.active { color: #facc15; }
  .switch { position: relative; width: 32px; height: 18px; cursor: pointer; }
  .switch input { opacity: 0; width: 0; height: 0; }
  .slider {
    position: absolute; inset: 0;
    background: #333; border-radius: 18px; transition: background 0.2s;
  }
  .slider::before {
    content: ""; position: absolute;
    width: 12px; height: 12px; left: 3px; top: 3px;
    background: #888; border-radius: 50%;
    transition: transform 0.2s, background 0.2s;
  }
  input:checked + .slider { background: #854d0e; }
  input:checked + .slider::before { transform: translateX(14px); background: #facc15; }

  #feed { display: flex; flex-direction: column; gap: 6px; }

  /* ── Normal mode card ── */
  .card {
    display: grid;
    grid-template-columns: 240px 1fr;
    gap: 16px;
    background: #1a1a1a;
    border: 1px solid #2a2a2a;
    border-left: 3px solid #4ade80;
    border-radius: 4px;
    padding: 14px;
  }
  .card.raw { border-left-color: #fb923c; }

  /* ── Lean mode card — single compact row, no animation ── */
  .card-lean {
    display: flex; align-items: baseline; gap: 10px;
    padding: 5px 10px;
    background: #141414;
    border-left: 2px solid #166534;
    border-radius: 2px;
    font-size: 0.72rem;
  }
  .card-lean.raw { border-left-color: #7c2d12; }
  .card-lean .cl-time  { color: #555; flex-shrink: 0; }
  .card-lean .cl-badge { flex-shrink: 0; }
  .cl-badge-stitch { color: #4ade80; }
  .cl-badge-raw    { color: #fb923c; }
  .card-lean .cl-size  { color: #6b7280; flex-shrink: 0; }
  .card-lean .cl-meta  { color: #9ca3af; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .card-lean .cl-id    { color: #374151; flex-shrink: 0; }

  .badge {
    display: inline-block; font-size: 0.6rem; font-weight: bold;
    letter-spacing: 0.08em; padding: 2px 6px; border-radius: 3px;
    vertical-align: middle; margin-left: 6px;
  }
  .badge-stitch { background: #14532d; color: #4ade80; }
  .badge-raw    { background: #431407; color: #fb923c; }

  @keyframes pop {
    from { opacity: 0; transform: translateY(-6px); }
    to   { opacity: 1; transform: translateY(0); }
  }
  .card { animation: pop 0.15s ease; }

  .thumb {
    width: 240px; height: 180px; object-fit: cover;
    border-radius: 3px; border: 1px solid #333; cursor: zoom-in; display: block;
  }
  .meta-wrap { min-width: 0; }
  .capture-id { font-size: 0.7rem; color: #4ade80; margin-bottom: 10px; }
  .section-label {
    font-size: 0.65rem; color: #555;
    text-transform: uppercase; letter-spacing: 0.08em; margin: 10px 0 4px;
  }
  .section-label:first-of-type { margin-top: 0; }
  table { border-collapse: collapse; font-size: 0.72rem; width: 100%; }
  td { padding: 2px 12px 2px 0; vertical-align: top; }
  td.k { color: #a78bfa; white-space: nowrap; width: 160px; }
  td.v { word-break: break-all; }
  .toggle {
    display: inline-block; margin-top: 10px;
    font-size: 0.68rem; color: #555; cursor: pointer; user-select: none;
  }
  .toggle:hover { color: #999; }
  .collapsible {
    display: none; margin-top: 6px; padding: 8px;
    background: #111; border-radius: 3px;
    font-size: 0.67rem; color: #888;
    white-space: pre-wrap; word-break: break-all; line-height: 1.6;
  }
  .empty { color: #333; font-size: 0.85rem; text-align: center; padding: 60px 0; }
</style>
</head>
<body>
<header>
  <h1>VisionX HW Trigger Monitor</h1>
  <div class="lean-toggle">
    <label id="lean-label" for="lean-cb">lean</label>
    <label class="switch">
      <input type="checkbox" id="lean-cb" onchange="onLeanToggle(this.checked)">
      <span class="slider"></span>
    </label>
  </div>
  <span id="count"></span>
  <span id="status">connecting…</span>
</header>
<div id="feed">
  <div class="empty" id="placeholder">Waiting for hardware trigger captures…</div>
</div>

<script>
const MAX_CARDS = 100;

let received = 0;
const feed      = document.getElementById("feed");
const statusEl  = document.getElementById("status");
const counter   = document.getElementById("count");
const leanCb    = document.getElementById("lean-cb");
const leanLabel = document.getElementById("lean-label");

function applyLeanUI(lean) {
  leanCb.checked      = lean;
  leanLabel.className = lean ? "active" : "";
}

async function onLeanToggle(lean) {
  applyLeanUI(lean);
  await fetch("/mode", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ lean }),
  }).catch(() => {});
}

function fmtSize(bytes) {
  return bytes >= 1048576
    ? (bytes / 1048576).toFixed(2) + " MB"
    : (bytes / 1024).toFixed(1) + " KB";
}
function row(k, v) {
  return `<tr><td class="k">${k}</td><td class="v">${v ?? "—"}</td></tr>`;
}
function collapsible(label, contentId) {
  return `<span class="toggle" onclick="
    const el = document.getElementById('${contentId}');
    const open = el.style.display === 'block';
    el.style.display = open ? 'none' : 'block';
    this.textContent = (open ? '▶' : '▼') + ' ${label}';
  ">▶ ${label}</span>`;
}

function trimFeed() {
  while (feed.children.length > MAX_CARDS) {
    feed.removeChild(feed.lastChild);
  }
}

function addLeanCard(d) {
  const isRaw    = d.kind === "raw";
  const metaParts = Object.entries(d.meta || {}).map(([k, v]) => `${k}=${v}`);
  const time     = d.received_at.slice(11, 19); // HH:MM:SS

  const el = document.createElement("div");
  el.className = "card-lean" + (isRaw ? " raw" : "");
  el.innerHTML =
    `<span class="cl-time">${time}</span>` +
    `<span class="cl-badge ${isRaw ? "cl-badge-raw" : "cl-badge-stitch"}">${isRaw ? "RAW" : "STI"}</span>` +
    `<span class="cl-size">${fmtSize(d.file_size_bytes)}</span>` +
    `<span class="cl-meta">${metaParts.join("  ") || "—"}</span>` +
    `<span class="cl-id">#${d.id}</span>`;

  feed.insertBefore(el, feed.firstChild);
  trimFeed();
}

function addNormalCard(d) {
  const uid        = d.id;
  const isRaw      = d.kind === "raw";
  const badgeClass = isRaw ? "badge-raw" : "badge-stitch";
  const badgeLabel = isRaw ? "RAW" : "STITCH";

  const serverRows = [
    row("received_at",       d.received_at),
    row("file_size",         fmtSize(d.file_size_bytes)),
    row("content_type",      d.content_type),
    row("original_filename", d.original_filename),
    row("saved_as",          d.filename),
  ].join("");

  const metaEntries = Object.entries(d.meta || {});
  const metaRows    = metaEntries.length
    ? metaEntries.map(([k, v]) => row(k, v)).join("")
    : row("(none)", "—");

  const headersText = Object.entries(d.headers || {})
    .map(([k, v]) => `${k}: ${v}`).join("\\n");

  const exifEntries = Object.entries(d.exif || {});
  const exifSection = exifEntries.length
    ? `<div class="section-label">camera exif</div>
       <table>${exifEntries.map(([k, v]) => row(k, v)).join("")}</table>`
    : "";

  const el = document.createElement("div");
  el.className = "card" + (isRaw ? " raw" : "");
  el.innerHTML =
    `<img class="thumb" src="${d.image_url}" loading="lazy"
          alt="capture ${uid}" onclick="window.open(this.src)"
          title="Click to open full size">
     <div class="meta-wrap">
       <div class="capture-id">#${uid}<span class="badge ${badgeClass}">${badgeLabel}</span></div>
       <div class="section-label">server</div>
       <table>${serverRows}</table>
       ${exifSection}
       <div class="section-label">form fields</div>
       <table>${metaRows}</table>
       ${collapsible("request headers", "hdr-" + uid)}
       <pre class="collapsible" id="hdr-${uid}">${headersText}</pre>
     </div>`;

  feed.insertBefore(el, feed.firstChild);
  trimFeed();
}

function addCard(d) {
  document.getElementById("placeholder")?.remove();
  received++;
  counter.textContent = `(${received} received)`;
  if (d.image_url) {
    addNormalCard(d);
  } else {
    addLeanCard(d);
  }
}

function connect() {
  const ws = new WebSocket(`ws://${location.host}/ws`);
  ws.onopen    = () => { statusEl.textContent = "● connected"; statusEl.className = "connected"; };
  ws.onmessage = (e) => {
    const d = JSON.parse(e.data);
    if      (d.type === "mode")    applyLeanUI(d.lean);
    else if (d.type === "capture") addCard(d);
  };
  ws.onclose   = () => {
    statusEl.textContent = "○ disconnected — reconnecting…";
    statusEl.className   = "";
    setTimeout(connect, 2000);
  };
}
connect();
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
async def monitor():
    return _HTML


if __name__ == "__main__":
    print(f"[server] starting in {'lean' if _lean else 'normal'} mode on http://0.0.0.0:{PORT}")
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
