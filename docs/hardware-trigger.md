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
```

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
  "uploads_failed": 0
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
