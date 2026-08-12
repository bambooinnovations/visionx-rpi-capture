# MindVision Stitch Capture Pipeline

This document describes the capture pipeline for the MindVision multi-camera setup: how a hardware trigger travels from the Arduino encoder signal to the final upload, and the design decisions behind it.

The diagram and most of this doc describe the pipeline with `hw_trigger.use_stitch = true` (stitched composite as the primary upload). With the default `use_stitch = false`, step 2 of the stitch pool is skipped and each camera's raw image is uploaded individually instead — see [`use_stitch` picks the primary output](#use_stitch-picks-the-primary-output) below.

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
          _stitch_pool (4 workers)
          ┌──────────────────────────────────────┐
          │ 1. apply colour correction            │
          │ 2. cv2.remap (undistort + warp) × N  │
          │    (maps precomputed on first trigger)│
          │ 3. blend onto canvas                  │
          │ 4. upload stitch  ◄───────────────────┼── completes fully here
          │    └─ on failure: write to disk       │
          └──────────────────────────────────────┘
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

### Precomputed warp geometry

The stitch computation was historically the dominant bottleneck (~2.3 s per trigger), consisting of:

1. `cv2.undistort` × N cameras — full camera-frame pass through the lens distortion model
2. `cv2.warpPerspective(bgr, H, canvas_size)` × N cameras — perspective warp to canvas
3. `cv2.warpPerspective(src_mask, H, canvas_size)` × N cameras — weight mask warp, **identical every trigger** for fixed calibration and frame shape

All 3N passes are now replaced by a single `cv2.remap` per camera. On the first trigger (and whenever calibration changes), `_build_stitch_geometry` precomputes per-camera `(map_x, map_y, mask)` arrays that map every canvas output pixel directly to the corresponding distorted camera pixel, composing the inverse perspective warp with the forward lens-distortion model in one step. These arrays are held in a module-level cache and reused for every subsequent trigger.

The cache is keyed on `(stitch_cal_mtime, camera_order, scale, per_camera_frame_shapes)` and is automatically rebuilt whenever the stitch calibration file changes on disk.

### `use_stitch` picks the primary output

`hw_trigger.use_stitch` (default **`false`**) decides what the *primary* upload is:

- **`true`** — the stitched composite (all cameras combined) is computed and uploaded to `destination_url`, exactly as described above.
- **`false`** (default) — stitching is skipped entirely (no calibration load, no `cv2` remap/blend). Each camera's raw JPEG is uploaded individually and directly to `destination_url`, tagged with a `camera_id` field in the POST body and named `<ts>_cam<id>.jpg`.

Either way, this primary upload runs synchronously inside the `_stitch_pool` worker handling that trigger — the pool's name refers to where the work happens, not that it always stitches. That keeps the primary path (stitched or per-camera) at the same high priority regardless of the toggle.

`send_raw_images = true` is a **separate, always-secondary** debug channel — individual camera images POSTed to a different `raw_destination_url`. It exists independently of `use_stitch` and always runs after the primary upload, through the slow single-worker pool below. With `use_stitch=false` and `send_raw_images=true` at the same time, the same per-camera images get uploaded twice: once as primary to `destination_url`, once as debug to `raw_destination_url`.

This distinction drives the two-pool design:

| Pool           | Workers | Purpose                                                                                              |
| -------------- | ------- | ------------------------------------------------------------------------------------------------------ |
| `_stitch_pool` | 4       | Compute + upload the primary output (stitched or per-camera, depending on `use_stitch`). Dedicated.  |
| `_raw_pool`    | 1       | Upload debug raw images (`send_raw_images`). Single-threaded so it cannot saturate the network link. |

Worker count rationale: each stitch job takes ~720ms end-to-end (72ms decode + 510ms stitch compute + 35ms encode + 50ms upload) on a Raspberry Pi 5 with 2 cameras. At the typical trigger rate of ~0.91/s, 4 workers gives ~5.6/s capacity — over 500% headroom. The 3-camera production setup adds one extra remap + accum pass (~180ms), giving an estimated ~900ms per job and still ~4.4/s capacity with 4 workers. With `use_stitch=false` the per-camera loop skips the remap/blend cost entirely, so headroom is even larger.

Debug raw uploads for trigger N are not submitted to the raw pool until the primary upload for trigger N has fully returned. This means:

- Debug raw traffic never overlaps with the primary upload on the same trigger.
- The network link is clear for primary uploads from newer triggers.
- A backed-up debug raw queue has no effect on primary upload latency.

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
| `destination_url`    | `""`              | URL to POST the primary image(s) to (stitched or per-camera, see `use_stitch`). Empty = skip upload. |
| `destination_api_key`| `""`              | Sent as `Authorization: Bearer <key>`.                                                   |
| `use_stitch`         | `false`           | `true` = upload one stitched composite. `false` = upload each camera's raw image separately, tagged with `camera_id`. |
| `raw_destination_url`| `""`              | URL to POST raw camera images to (debug). Empty = skip.                                  |
| `send_raw_images`    | `false`           | Enable debug raw image uploads (secondary, always alongside the primary upload above). Off by default. |
| `save_local`         | `false`           | Write to disk during normal operation. Off by default; written only on upload failure.   |
| `local_save_dir`     | `data/hw_captures`| Directory for disk-fallback saves and retry.                                             |
| `local_max_files`    | `200`             | Max files kept in `local_save_dir` (oldest deleted first). `0` = unlimited.              |
| `local_max_mb`       | `500`             | Max total size of `local_save_dir`. `0` = unlimited.                                     |
| `retry_attempts`     | `3`               | Upload retry attempts before falling back to disk.                                       |
| `timeout_seconds`    | `10`              | Per-request upload timeout in seconds.                                                   |
| `max_queue_age_s`    | `5.0`             | Drop trigger events older than this (seconds) before a camera worker picks them up.      |
