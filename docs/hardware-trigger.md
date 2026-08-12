# Hardware Trigger Mode (MindVision)

Hardware trigger mode is used when a physical signal — not a software call — fires the camera sensor. An Arduino reads a quadrature encoder (or manual button) and pulses pin D9 directly to the camera's trigger input. At the same moment it sends a JSON line over serial so the Pi knows a frame was captured and can collect it.

```
Encoder/button → Arduino D9 ──► Camera trigger pin  (frame captured by hardware)
                 Arduino TX  ──► Pi serial RX        (JSON notification → collect & upload)
```

## 1. Configure `configuration.toml`

Uncomment and fill in the `[hw_trigger]` section:

```toml
[hw_trigger]
serial_port         = "/dev/ttyACM0"                      # serial port the Arduino is connected to
serial_baud         = 115200                              # must match the Arduino sketch (default 115200)
destination_url     = "https://yoursite.com/api/captures" # where to POST triggered images (leave blank to skip upload)
destination_api_key = ""                                  # sent as Authorization: Bearer <key>; leave blank if not needed
save_local          = true                                # also write a copy to local_save_dir
local_save_dir      = "data/hw_captures"                  # created automatically
local_max_files     = 200                                 # oldest files deleted first (0 = unlimited)
local_max_mb        = 500                                 # oldest files deleted first (0 = unlimited)
use_stitch          = false                               # false (default) = upload each camera's raw image, tagged with camera_id; true = upload one stitched composite
```

See [capture-pipeline.md](capture-pipeline.md#use_stitch-picks-the-primary-output) for how `use_stitch` changes the upload path and priority.

`destination_url` and `destination_api_key` can also be changed live without a restart:

```bash
curl -X PATCH http://localhost:8080/api/system/config \
     -H 'Content-Type: application/json' \
     -d '{"hw_trigger.destination_url": "https://yoursite.com/api/captures"}'
```

## 2. Start the listener

```bash
curl -X POST http://localhost:8080/api/decoder/start
```

This switches every connected MindVision camera to `hardware_trigger` mode (SDK waits for the physical pin, no software trigger is issued) and starts reading JSON lines from the serial port. Each trigger event grabs the already-captured frame from the SDK buffer and uploads/saves it.

To override the serial port or baud rate at start time without changing the config file:

```bash
curl -X POST http://localhost:8080/api/decoder/start \
     -H 'Content-Type: application/json' \
     -d '{"port": "/dev/ttyUSB1", "baud": 115200}'
```

## 3. Check status

```bash
curl http://localhost:8080/api/decoder/status
```

```json
{
  "running": true,
  "uptime_seconds": 42.3,
  "triggers_received": 12,
  "captures_ok": 12,
  "captures_failed": 0,
  "uploads_ok": 12,
  "uploads_failed": 0,
  "active_style": "thin_fabric"
}
```

## 4. Stop the listener

```bash
curl -X POST http://localhost:8080/api/decoder/stop
```

This stops the serial listener and reverts all cameras back to `capture` mode (software trigger).

## Decoder endpoints

| Method | Path                         | Description                                             |
| ------ | ---------------------------- | ------------------------------------------------------- |
| POST   | `/api/decoder/start`         | Start listening; switches cameras to `hardware_trigger` |
| POST   | `/api/decoder/stop`          | Stop listening; reverts cameras to `capture` mode       |
| GET    | `/api/decoder/status`        | Running state, uptime, and capture/upload counters      |
| GET    | `/api/decoder/server-health` | Check reachability of the configured `health_check_url` |

Full decoder endpoint reference: [api.md](api.md#decoder-arduino)

## Persisted config files

These live under `data/` on the Pi (gitignored — per-device state, not committed) and survive restarts:

| File                        | Written by                                                        | Contents                                                                 |
| ---------------------------- | ------------------------------------------------------------------ | -------------------------------------------------------------------------- |
| `data/arduino_config.json`   | `PATCH /api/decoder/config`, `POST /api/decoder/speed-presets/<style>/activate` | The currently-active Arduino params (physical + derived), plus `active_style` if set via a preset. Pushed to the Arduino on connect. |
| `data/speed_presets.json`    | `PATCH /api/decoder/speed-presets/<style>`                        | Named presets of physical params (`wheel_diameter_mm`, `encoder_ppr`, `capture_interval_mm`) — see [Speed presets](api.md#speed-presets-styles). Saving a preset does **not** push to the Arduino; only `.../activate` does. |
| `runtime_config.json`        | `PATCH /api/system/config`                                        | Runtime overrides for `hw_trigger.*`/`stream.*` keys (e.g. `use_stitch`, `destination_url`). Lives at the repo root, not under `data/`. |

## Speed presets ("styles")

Instead of resending `wheel_diameter_mm`/`encoder_ppr`/`capture_interval_mm` every time you switch product/fabric setups, save each as a named preset and activate it by name:

```bash
curl -X PATCH http://localhost:8080/api/decoder/speed-presets/thin_fabric \
     -H 'Content-Type: application/json' \
     -d '{"wheel_diameter_mm": 64.7, "encoder_ppr": 600, "capture_interval_mm": 8.0}'

curl -X POST http://localhost:8080/api/decoder/speed-presets/thin_fabric/activate
```

Full reference: [api.md](api.md#speed-presets-styles)

## Queue monitoring (SSE)

The pipeline exposes an SSE endpoint that streams queue depths once per second — useful for spotting backlogs during a scan:

```bash
curl -N http://localhost:8080/api/queues/stream
```

Example event payload:

```json
{
  "camera_queues":       {"0": 0, "1": 0, "2": 0},
  "camera_queue_maxsize": 30,
  "collector_pending":   0,
  "stitch_pending":      1,
  "stitch_pending_mb":   18.4,
  "raw_pending":         3,
  "raw_pending_mb":      55.2,
  "disk_retry":          0,
  "disk_spill":          0
}
```

| Field | Meaning |
| ----- | ------- |
| `camera_queues` | Pending trigger events waiting to be captured, per camera |
| `camera_queue_maxsize` | Configured cap — triggers are dropped when this is reached |
| `collector_pending` | Triggers waiting for all cameras to report a frame |
| `stitch_pending` | Jobs in the stitch pool (compute + upload) |
| `stitch_pending_mb` | RAM held by pending stitch jobs |
| `raw_pending` | Jobs in the raw upload pool |
| `raw_pending_mb` | RAM held by pending raw-upload jobs |
| `disk_retry` | Images on disk waiting to be retried after a failed upload |
| `disk_spill` | Images temporarily spilled to disk because a memory budget was exceeded |

---

## Memory and queue protection

### Trigger queue cap

Each camera has a bounded in-memory queue (`trigger_queue_maxsize`, default **30**). If the camera capture worker falls behind and the queue fills up, new triggers are **dropped** (not captured) and a `hw_trigger_queue_full_dropped` warning is logged. Under normal operation this queue should stay at 0–1.

30 events is deliberately generous: at a typical 200–500 ms capture time the queue holds several seconds of burst tolerance. Raise or lower it via PATCH if your scan pattern differs:

```bash
curl -X PATCH http://localhost:8080/api/system/config \
     -H 'Content-Type: application/json' \
     -d '{"hw_trigger.trigger_queue_maxsize": 50}'
```

> **Note:** `trigger_queue_maxsize` is read once when the listener starts. Patching it takes effect on the next `POST /api/decoder/start`.

### Stitch memory budget

The stitch pool holds raw JPEG bytes for every pending trigger in RAM (one set of camera frames per trigger). When the total RAM held by pending stitch jobs would exceed `stitch_memory_budget_mb` (default **1024 MB**), the incoming capture is **spilled to disk** (`CAPTURE_TMP_DIR/hw_trigger/spill/`) instead of kept in RAM. The stitch worker reads and deletes the files transparently. A `hw_trigger_stitch_memory_budget_exceeded` warning is logged when this happens.

```bash
curl -X PATCH http://localhost:8080/api/system/config \
     -H 'Content-Type: application/json' \
     -d '{"hw_trigger.stitch_memory_budget_mb": 256}'
```

### Raw upload memory budget

The raw pool (debug uploads of individual camera frames) has its own budget, `raw_memory_budget_mb` (default **2048 MB**). It follows the same spill-to-disk pattern as the stitch pool.

On an 8 GB RPi with 3 cameras at ~5 MB per JPEG the 2 GB default accommodates roughly 130 triggers queued in the raw pool before spilling. Adjust for your camera resolution and available RAM:

```bash
curl -X PATCH http://localhost:8080/api/system/config \
     -H 'Content-Type: application/json' \
     -d '{"hw_trigger.raw_memory_budget_mb": 1024}'
```

### Stale trigger discard

Triggers that have waited longer than `max_queue_age_s` (default **5.0 s**) in a camera queue are discarded rather than captured. This prevents a burst of stale captures firing all at once after a temporary slowdown.

```bash
curl -X PATCH http://localhost:8080/api/system/config \
     -H 'Content-Type: application/json' \
     -d '{"hw_trigger.max_queue_age_s": 3.0}'
```

---

## Arduino sketch

The sketch lives at `arduino/decoder_trigger.ino`. Key wiring:

| Pin | Connection                                   |
| --- | -------------------------------------------- |
| D2  | Encoder channel A                            |
| D3  | Encoder channel B                            |
| D4  | Manual trigger button (other end to GND)     |
| D9  | Camera trigger output (LOW = trigger active) |

Serial output is 115200 baud. Each trigger event emits one JSON line:

```json
{"type":"trigger","source":"encoder","count":118,"trigger":1,"speed_cms":5.20}
{"type":"trigger","source":"manual","count":0,"trigger":1,"speed_cms":0.00}
```

`source` is `"encoder"` for distance-based triggers or `"manual"` for the button. `count` is the cumulative encoder count, `trigger` is the trigger sequence number, and `speed_cms` is the belt speed in cm/s at the moment of the trigger.
