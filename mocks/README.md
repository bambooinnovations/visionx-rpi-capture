# Hardware mocks

Run the hardware-trigger pipeline with no hardware:

```bash
python -m mocks.e2e            # ~20 s, exit code 1 if anything fails
python -m mocks.e2e --keep-logs
```

| Mock | Replaces | Notes |
|---|---|---|
| `fake_arduino.py` | the encoder/trigger board | Same JSON-lines protocol as `arduino/camera_trigger/camera_trigger.ino`, over TCP. The app connects to `socket://127.0.0.1:<port>`. |
| `fake_mindvision.py` | the MindVision cameras | Subclass of `MindVisionCamera`; only the SDK calls are replaced. Frames are synthetic, but EXIF and JPEG encoding use the real code. Enable with `[camera] type = "mock_mindvision"`. |
| `fake_upload_server.py` | the platform upload endpoint | Records uploads, can return HTTP 500 for the next N. |
| `e2e.py` | | Starts all of the above plus the real app in a temp dir (your `configuration.toml`, `data/` and `runtime_config.json` are untouched), then checks a simulated roll. |

Use the fake Arduino by hand (for example against a running app, to watch the `/monitor` page move):

```bash
python -m mocks.fake_arduino --port 7000 --speed 20
# then: POST /api/decoder/start  {"port": "socket://127.0.0.1:7000"}
```

What it proves: the software (protocol, trigger logic, capture, EXIF distance, uploads, retries, reconnects, monitor page).
What it cannot prove: the Arduino sketch, wiring, encoder counts, the camera's trigger line, lighting.

Not simulated: the settings, lens and exposure pages, which talk to the MindVision SDK directly.
