"""Minimal test server for hardware-trigger image ingestion.

Start with:  uv run --group test-server python test_server/server.py
Then open:   http://<pi-ip>:8888/

POST /upload        — stitched capture endpoint; accepts any multipart form fields alongside the image
POST /upload-raw    — raw per-camera capture endpoint; same behaviour as /upload
GET  /              — real-time monitor page (WebSocket-updated, no refresh needed)
GET  /images/{filename} — serve a received image
"""
from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse

SAVE_DIR = Path(__file__).parent / "received"
SAVE_DIR.mkdir(exist_ok=True)


def _extract_exif(jpeg_bytes: bytes) -> dict:
    """Return a flat dict of human-readable EXIF values from a JPEG blob."""
    try:
        import piexif

        exif = piexif.load(jpeg_bytes)
        out: dict = {}

        ifd0 = exif.get("0th", {})
        exif_ifd = exif.get("Exif", {})

        def _str(val):
            if isinstance(val, bytes):
                return val.decode(errors="replace").rstrip("\x00")
            return val

        make = _str(ifd0.get(piexif.ImageIFD.Make, ""))
        model = _str(ifd0.get(piexif.ImageIFD.Model, ""))
        serial = _str(ifd0.get(piexif.ImageIFD.CameraSerialNumber, ""))
        dt = _str(ifd0.get(piexif.ImageIFD.DateTime, ""))

        if make:
            out["make"] = make
        if model:
            out["model"] = model
        if serial:
            out["serial_number"] = serial
        if dt:
            out["datetime"] = dt

        exp = exif_ifd.get(piexif.ExifIFD.ExposureTime)
        if exp and isinstance(exp, tuple) and exp[1]:
            us = exp[0]
            ms = us / 1000
            out["exposure_time"] = f"{us} µs ({ms:.2f} ms)"

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
    for ws in _clients:
        try:
            await ws.send_text(json.dumps(payload))
        except Exception:
            dead.append(ws)
    for ws in dead:
        _clients.remove(ws)


async def _handle_upload(request: Request, kind: str):
    form = await request.form()

    # Find the image file — accept any field name, fall back to first file found.
    image_field = form.get("image") or next(
        (v for v in form.values() if hasattr(v, "read")), None
    )
    if image_field is None:
        return {"error": "no image file found in form data"}, 400

    content = await image_field.read()
    image_id = str(uuid.uuid4())[:8]
    filename = f"{int(time.time())}_{image_id}.jpg"
    (SAVE_DIR / filename).write_bytes(content)

    meta = {k: v for k, v in form.items() if not hasattr(v, "read")}
    exif_info = _extract_exif(content)

    payload = {
        "id": image_id,
        "kind": kind,
        "received_at": datetime.now(timezone.utc).isoformat(),
        "filename": filename,
        "original_filename": image_field.filename,
        "content_type": image_field.content_type,
        "file_size_bytes": len(content),
        "meta": meta,
        "exif": exif_info,
        "headers": dict(request.headers),
        "image_url": f"/images/{filename}",
    }

    await _broadcast(payload)
    return {"status": "received", **payload}


@app.post("/upload")
async def upload(request: Request):
    return await _handle_upload(request, "stitch")


@app.post("/upload-raw")
async def upload_raw(request: Request):
    return await _handle_upload(request, "raw")


@app.get("/health")
async def health():
    return {"status": "ok"}


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

  header { display: flex; align-items: baseline; gap: 16px; margin-bottom: 20px; }
  h1 { color: #4ade80; font-size: 1.1rem; letter-spacing: 0.05em; }
  #count { font-size: 0.75rem; color: #555; }
  #status { font-size: 0.75rem; color: #666; margin-left: auto; }
  #status.connected { color: #4ade80; }

  #feed { display: flex; flex-direction: column; gap: 14px; }

  .card {
    display: grid;
    grid-template-columns: 240px 1fr;
    gap: 16px;
    background: #1a1a1a;
    border: 1px solid #2a2a2a;
    border-left: 3px solid #4ade80;
    border-radius: 4px;
    padding: 14px;
    animation: pop 0.15s ease;
  }
  .card.raw { border-left-color: #fb923c; }

  .badge {
    display: inline-block;
    font-size: 0.6rem;
    font-weight: bold;
    letter-spacing: 0.08em;
    padding: 2px 6px;
    border-radius: 3px;
    vertical-align: middle;
    margin-left: 6px;
  }
  .badge-stitch { background: #14532d; color: #4ade80; }
  .badge-raw    { background: #431407; color: #fb923c; }
  @keyframes pop {
    from { opacity: 0; transform: translateY(-6px); }
    to   { opacity: 1; transform: translateY(0); }
  }

  .thumb {
    width: 240px;
    height: 180px;
    object-fit: cover;
    border-radius: 3px;
    border: 1px solid #333;
    cursor: zoom-in;
    display: block;
  }

  .meta-wrap { min-width: 0; }
  .capture-id { font-size: 0.7rem; color: #4ade80; margin-bottom: 10px; }

  .section-label {
    font-size: 0.65rem;
    color: #555;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    margin: 10px 0 4px;
  }
  .section-label:first-of-type { margin-top: 0; }

  table { border-collapse: collapse; font-size: 0.72rem; width: 100%; }
  td { padding: 2px 12px 2px 0; vertical-align: top; }
  td.k { color: #a78bfa; white-space: nowrap; width: 160px; }
  td.v { word-break: break-all; }

  .toggle {
    display: inline-block;
    margin-top: 10px;
    font-size: 0.68rem;
    color: #555;
    cursor: pointer;
    user-select: none;
  }
  .toggle:hover { color: #999; }
  .collapsible {
    display: none;
    margin-top: 6px;
    padding: 8px;
    background: #111;
    border-radius: 3px;
    font-size: 0.67rem;
    color: #888;
    white-space: pre-wrap;
    word-break: break-all;
    line-height: 1.6;
  }

  .empty { color: #333; font-size: 0.85rem; text-align: center; padding: 60px 0; }
</style>
</head>
<body>
<header>
  <h1>VisionX HW Trigger Monitor</h1>
  <span id="count"></span>
  <span id="status">connecting…</span>
</header>
<div id="feed">
  <div class="empty" id="placeholder">Waiting for hardware trigger captures…</div>
</div>

<script>
let received = 0;
const feed    = document.getElementById("feed");
const status  = document.getElementById("status");
const counter = document.getElementById("count");

function fmtSize(bytes) {
  return bytes >= 1048576
    ? (bytes / 1048576).toFixed(2) + " MB"
    : (bytes / 1024).toFixed(1) + " KB";
}

function row(k, v) {
  return `<tr><td class="k">${k}</td><td class="v">${v ?? "—"}</td></tr>`;
}

function toggle(label, contentId) {
  return `<span class="toggle" onclick="
    const el = document.getElementById('${contentId}');
    const open = el.style.display === 'block';
    el.style.display = open ? 'none' : 'block';
    this.textContent = (open ? '▶' : '▼') + ' ${label}';
  ">▶ ${label}</span>`;
}

function addCard(d) {
  document.getElementById("placeholder")?.remove();
  received++;
  counter.textContent = `(${received} received)`;

  const uid = d.id;

  // Server-added fields shown at top
  const serverRows = [
    row("received_at",       d.received_at),
    row("file_size",         fmtSize(d.file_size_bytes)),
    row("content_type",      d.content_type),
    row("original_filename", d.original_filename),
    row("saved_as",          d.filename),
  ].join("");

  // Whatever form fields were sent — dynamic, no assumptions
  const metaEntries = Object.entries(d.meta || {});
  const metaRows = metaEntries.length
    ? metaEntries.map(([k, v]) => row(k, v)).join("")
    : row("(none)", "—");

  const headersText = Object.entries(d.headers || {})
    .map(([k, v]) => `${k}: ${v}`).join("\\n");

  // EXIF section — only shown when the image carries embedded metadata
  const exifEntries = Object.entries(d.exif || {});
  const exifSection = exifEntries.length ? `
      <div class="section-label">camera exif</div>
      <table>${exifEntries.map(([k, v]) => row(k, v)).join("")}</table>` : "";

  const isRaw   = d.kind === "raw";
  const badgeClass = isRaw ? "badge-raw" : "badge-stitch";
  const badgeLabel = isRaw ? "RAW" : "STITCH";

  const card = document.createElement("div");
  card.className = "card" + (isRaw ? " raw" : "");
  card.innerHTML = `
    <img class="thumb" src="${d.image_url}" loading="lazy"
         alt="capture ${uid}" onclick="window.open(this.src)"
         title="Click to open full size">
    <div class="meta-wrap">
      <div class="capture-id">#${uid}<span class="badge ${badgeClass}">${badgeLabel}</span></div>

      <div class="section-label">server</div>
      <table>${serverRows}</table>

      ${exifSection}

      <div class="section-label">form fields</div>
      <table>${metaRows}</table>

      ${toggle("request headers", "hdr-" + uid)}
      <pre class="collapsible" id="hdr-${uid}">${headersText}</pre>
    </div>`;

  feed.insertBefore(card, feed.firstChild);
}

function connect() {
  const ws = new WebSocket(`ws://${location.host}/ws`);
  ws.onopen    = () => { status.textContent = "● connected"; status.className = "connected"; };
  ws.onmessage = (e) => addCard(JSON.parse(e.data));
  ws.onclose   = () => {
    status.textContent = "○ disconnected — reconnecting…";
    status.className = "";
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
    uvicorn.run(app, host="0.0.0.0", port=8888, log_level="info")
