# MindVision Stitch Capture Pipeline

This document describes the capture pipeline for the MindVision dual-camera stitch setup: how a hardware trigger travels from the Arduino encoder signal to the final stitched image upload, and the design decisions behind it.

## Overview

```text
Arduino encoder pulse
        │
        ▼
Serial listener thread  (JSON line received)
        │  put(event) — non-blocking
        ├──────────────────────┐
        ▼                      ▼
 camera thread 0         camera thread 1
 (MindVision cam 0)      (MindVision cam 1)
 grabs immediately       grabs immediately
        │                      │
        └──────────┬───────────┘
                   ▼
         _TriggerCollector
         (waits for both cameras to report)
                   │
                   ▼
          _stitch_pool (2 workers)
          ┌──────────────────────────┐
          │ 1. compute stitch        │
          │ 2. upload stitch  ◄──────┼── completes fully here
          │    └─ on failure:        │
          │       write to disk      │
          └──────────────────────────┘
                   │ submit (only after stitch upload returns)
                   ▼
          _raw_pool (1 worker — serial)
          ┌──────────────────────────┐
          │ upload raw images        │  ← debug only, slow drain
          │ one at a time            │
          │ └─ on failure:           │
          │    write to disk         │
          └──────────────────────────┘
                   │
                   ▼
          disk retry thread
          (wakes every 60 s, retries
           stitch files first)
```

---

## Design Decisions

### Zero-delay camera capture

The original design used a shared `ThreadPoolExecutor` for captures. When all workers were busy processing uploads from previous triggers, new trigger events queued up and the MindVision camera grab was delayed by several seconds.

**Fix:** Each MindVision camera gets one dedicated thread that does nothing but block on its queue and fire the grab immediately. Camera threads are never occupied by stitch computation, disk I/O, or network uploads — they are always ready for the next trigger.

```text
trigger N received → put into queue[cam0], queue[cam1]
cam0 thread (always blocking) → grabs immediately
cam1 thread (always blocking) → grabs immediately
```

### Stitch is the primary output; raw images are debug only

The stitched image (both MindVision cameras combined) is the production output sent to the server. Individual raw camera images are sent only when `send_raw_images = true`, which is intended for debugging and calibration only.

This distinction drives the two-pool design:

| Pool           | Workers | Purpose                                                                      |
| -------------- | ------- | ---------------------------------------------------------------------------- |
| `_stitch_pool` | 2       | Compute stitch + upload stitch. Dedicated — no other work enters here.       |
| `_raw_pool`    | 1       | Upload raw images. Single-threaded so it cannot saturate the network link.   |

Raw uploads for trigger N are not submitted to the raw pool until the stitch upload for trigger N has fully returned. This means:

- Raw traffic never overlaps with stitch traffic on the same trigger.
- The network link is clear for stitch uploads from newer triggers.
- A backed-up raw queue has no effect on stitch latency.

### Memory-first; disk only on network failure

Writing to the SD card on every capture creates unnecessary I/O overhead. The RPi SD card does not handle frequent random writes well, and with MindVision captures arriving at ~1/s the SD card becomes a bottleneck.

**Default:** `save_local = false`. Images live as bytes in RAM until the upload completes. With 8 GB of RAM a significant backlog can be absorbed without writing anything to disk.

**Disk is only written when the upload fails completely** (all retry attempts exhausted). This is the emergency fallback for sustained network outages, not the normal path.

```toml
# configuration.toml
[hw_trigger]
save_local = false   # default — no SD card writes during normal operation
```

### Disk retry for network outages

When the network is persistently unavailable, failed images are written to `local_save_dir` (default `data/hw_captures`). A background thread (`_disk_retry_loop`) wakes every 60 seconds and retries these files:

- Stitch files (`*_stitch.jpg`) are retried before raw files, preserving priority.
- Successfully uploaded files are deleted from disk.
- The directory is kept within `local_max_files` / `local_max_mb` limits to prevent disk exhaustion.

### Stale trigger guard

If triggers arrive faster than the cameras can respond (e.g., after the trigger source stops and the camera is idle), queued events are dropped rather than attempting a grab that will time out. The threshold is configurable:

```toml
[hw_trigger]
max_queue_age_s = 5.0   # drop triggers older than this when a camera thread picks them up
```

This prevents the flood of MindVision timeout errors (`error_code=-12`) that would otherwise continue for many seconds after the trigger source stops.

---

## Configuration Reference

All options live under `[hw_trigger]` in `configuration.toml` and can be updated live via `PATCH /api/system/config` without a restart.

| Key                  | Default           | Description                                                                              |
| -------------------- | ----------------- | ---------------------------------------------------------------------------------------- |
| `destination_url`    | `""`              | URL to POST the stitched image to. Empty = skip upload.                                  |
| `destination_api_key`| `""`              | Sent as `Authorization: Bearer <key>`.                                                   |
| `raw_destination_url`| `""`              | URL to POST raw camera images to (debug). Empty = skip.                                  |
| `send_raw_images`    | `false`           | Enable raw image uploads. Debug only — off by default.                                   |
| `save_local`         | `false`           | Write to disk during normal operation. Off by default; written only on upload failure.   |
| `local_save_dir`     | `data/hw_captures`| Directory for disk-fallback saves and retry.                                             |
| `local_max_files`    | `200`             | Max files kept in `local_save_dir` (oldest deleted first). `0` = unlimited.              |
| `local_max_mb`       | `500`             | Max total size of `local_save_dir`. `0` = unlimited.                                     |
| `retry_attempts`     | `3`               | Upload retry attempts before falling back to disk.                                       |
| `timeout_seconds`    | `10`              | Per-request upload timeout in seconds.                                                   |
| `max_queue_age_s`    | `5.0`             | Drop trigger events older than this (seconds) before a camera worker picks them up.      |
