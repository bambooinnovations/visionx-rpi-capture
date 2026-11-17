# Backend-controlled capture

The backend, not an operator at the monitor page, can run a capture on the Pi. It starts a **task** with an id, the Pi
tags every photo with that id until the task is stopped and its uploads have drained, and a websocket streams
everything the monitor page shows. Uploads keep using the existing upload path; the task endpoints and the websocket
only control and observe.

Endpoint and payload reference: [api.md](api.md#capture-tasks-backend-driven).

Code:

| Part | File |
| ---- | ---- |
| State machine, persistence, recovery | `capture_task.py` |
| HTTP endpoints, websocket hub | `blueprints/task.py` |
| Trigger tagging, per-task counters, discard, trigger gate | `camera/mindvision_trigger.py` |
| Websocket client | `static/monitor/monitor.js` |

Tests: `tests/test_capture_task.py`, and the task scenarios of the mock rig (`uv run python -m mocks.e2e`).

## Lifecycle

```
            start(id)                  stop(id)                    nothing pending
  idle ───────────────▶ running ───────────────▶ draining ───────────────────────▶ idle
    ▲                    │    │                     │
    │   reconnect /      │    └──── stop(id, force) ┴──▶ idle (pending work discarded)
    │   Pi restart       ▼
    │               interrupted ── stop(id) ──▶ draining
    └──────────────────────── stop(id, force) ──▶ idle
```

| State | `start(id)` | `stop(id)` | `stop(id, force)` |
| ----- | ----------- | ---------- | ----------------- |
| `idle` | → `running` | 200 if `id` is the task that last ran, else 409 | same |
| `running` | same id: 200, no change; other id: 409 | → `draining`, or straight to `idle` if nothing is pending | → `idle`, discarding |
| `draining` | 409 | 202 while uploads remain, 200 once drained | → `idle`, discarding |
| `interrupted` | 409 | as from `running` | → `idle`, discarding |

- One task at a time. A different id while the task is not `idle` is always 409, with the active id in the body.
- `stop` for an id that is not the active one is 409, so a stale backend cannot stop a newer run.
- Both commands are idempotent; the backend can retry them blindly.
- `stop` ends *capturing* immediately. Waiting for uploads is the `draining` phase. The call itself only lingers for
  `wait_s` (default 2 s, at most 30 s) in case the queue is already nearly empty; it never blocks on the network.

### Interrupted tasks

A running task becomes `interrupted` when its distance baseline can no longer be trusted:

- the **Pi restarts** while the task is running, or
- the **serial link to the Arduino reconnects** while the task is running. Reopening the port resets the Arduino, so
  its encoder starts again at 0 and the run would otherwise silently contain two distance baselines.

An interrupted task captures nothing new: triggers are off, and the listener drops any trigger that still arrives (see
*Trigger gate*). Photos already captured keep uploading with their original task id. `start` is refused until the
backend closes the task with `stop` (optionally `force`), so a new run can never inherit a broken baseline.

## Starting a task

Order matters, so that no photo can carry a stale distance or a missing id:

1. Validate the state and the id (1–128 characters of `A-Z a-z 0-9 . _ : -`). Reject otherwise.
2. Bring the hardware up: every camera in hardware-trigger mode, the decoder listener running, the Arduino connected.
   If any step fails, the cameras this call switched go back to capture mode and a decoder it started is stopped; cameras
   and a decoder that were already set up are left alone.
3. Disable triggers, send `reset_count`, and wait for the Arduino's acknowledgement. Without it the encoder count, and so
   the distance, is stale and the start fails (`reset_unconfirmed`).
4. Zero the per-task counters, open the trigger gate, begin tagging with the task id, and persist `running`.
5. Enable triggers.

## Stopping a task

1. Disable triggers and wait for the Arduino's acknowledgement. Serial is ordered, so every trigger fired before the
   disable reaches the Pi before the acknowledgement and is already counted as in flight when the drain check runs.
2. Close the trigger gate, persist `draining` (flushed before the response), and kick the disk retry.
3. Once nothing is pending, persist `idle` and publish `task.finished` with a summary.

Cameras stay in hardware-trigger mode and the decoder keeps running after a stop, because draining needs the upload
pools. Only the manual `/api/decoder/stop` returns the cameras to capture mode.

### Pending work

A task's pending work is everything that still has to reach the destination:

- triggers waiting in a camera queue or being captured,
- stitch jobs and raw jobs (queued or running),
- failed uploads saved to disk for retry. They belong to the task through the `task_id` in their sidecar file, so left-overs
  from an older run never hold a new task in `draining`.

Pending counters are released in a future done-callback, so a cancelled job releases its count too.

### Draining and the drain timeout

A draining task keeps retrying failed uploads until they succeed. If the destination stays down, the task would never
finish, so `[task] drain_timeout_s` (default 600 s, `0` = wait forever) bounds it, counted from `stop` (or from boot for a
drain resumed after a restart).

When the timeout expires the task is closed as `idle` with `drain_timed_out: true` and `leftover` (the pending counts at
that moment) in its summary, and a `task.drain_timeout` event is published. **Nothing is discarded.** Uploads still in
flight finish, and failed uploads stay on disk and keep retrying under their original task id, subject to the existing
`local_max_files` / `local_max_mb` limits. The backend reconciles from the summary.

### Force stop

`force: true` discards everything not yet uploaded and returns `idle`:

1. Clear the camera and collector queues.
2. Cancel queued upload jobs and delete their spill files.
3. Delete the task's failed uploads from disk.
4. Give jobs already on the wire a moment to finish, then close the task.

A request already on the wire cannot be recalled. Those jobs finish that one request and then make no further attempt and
no local-save fallback: every trigger carries a generation number, and everything at or below the discarded generation
is skipped. They are reported as `aborted_jobs`; the response lists `queued_captures`, `upload_jobs`, `aborted_jobs` and
`disk_files` so the backend can reconcile.

## Tagging

The task id is stamped onto each trigger at the moment the listener receives it, not when the photo is uploaded, so frames
still in flight while draining keep the right id. It then appears:

- in the image EXIF (`task_id` in the UserComment JSON, next to `trigger_number`, `encoder_count` and `distance_mm`),
- as the `task_id` form field of the upload request,
- in the sidecar file of a failed upload, so retries carry it too.

It is not part of the file name.

Per-task counters (`triggers`, `captured`, `capture_failed`, `uploaded`, `upload_failed`, `discarded`) live in the
listener. `captured` counts camera images and `uploaded` counts upload requests, so they reconcile only in per-camera mode
(when stitching, one request covers every camera of a trigger).

## Trigger gate

`set_trigger_enabled false` asks the Arduino to stop, but an Arduino that has just reset comes back with triggers on and
may fire before that command lands. The listener therefore also has a **gate**: while it is closed, triggers are dropped on
receipt (speed and encoder readings still update, so the monitor stays live). The gate is open while a task is `running`
and when idle; it is closed while `draining`, `interrupted`, and from boot when a task is recovered. While the task is
closed to triggers, a watcher re-sends the disable every 2 s.

## Persistence and recovery

State is kept in `data/task_state.json`: `{task_id, state, started_at, stopped_at, finished_at, counters, last}`, written
atomically (temp file, fsync, rename) by one writer at a time. Transitions are flushed before the HTTP response or event
goes out; while running, the counters are checkpointed every 5 s so an interrupted task still shows how far it got.

On boot:

- `idle`: nothing to do.
- `draining`: resume draining the on-disk queue, with a fresh drain timeout.
- `running`: becomes `interrupted` (see above).
- `interrupted`: stays interrupted.

## Interaction with manual controls

While a task is not `idle`, controls that would disturb it answer 409 `task_active`: `/api/decoder/reset-count`,
`/api/decoder/stop`, `/api/decoder/mode/calibration`, and setting `/api/system/mode` to `regular`.
`/api/decoder/mode/hw-trigger` is refused while the task is `draining` or `interrupted`, since it would re-enable triggers.
The monitor page disables its "Start new roll" button during a task.

## Websocket

`GET /ws/task`, read-only: commands stay on HTTP. Plain JSON text frames in one envelope:

```json
{ "v": 1, "type": "state", "seq": 123, "ts": 1731772800123, "data": {} }
```

`ts` is epoch milliseconds and `seq` increases per connection.

| `type` | Sent | Purpose |
| ------ | ---- | ------- |
| `state` | on connect, then every 0.5 s while a task is active (1 s idle) | A **full** snapshot. Losing one is harmless. |
| `event` | on the transition | Edge notifications: `task.started`, `task.stopping`, `task.finished`, `task.drain_timeout`, `task.discarded`, `task.interrupted`, `arduino.connected`, `arduino.disconnected`, `capture.failed`, `upload.failed` |
| `ping` | every 10 s | Liveness. Clients need not answer; a dead connection is noticed when a send fails. |

Because `state` is always complete, a reconnecting client needs no replay: it connects, receives a `state`, and is current.
Events are conveniences; anything that matters is also visible in `state`.

### State snapshot

```jsonc
{
  "task":   { "id": "t-123", "state": "running",   // idle | running | draining | interrupted
              "started_at": 1731772000, "stopped_at": null, "finished_at": null,
              "pending": { "capture": 0, "stitch": 4, "raw": 0, "disk_retry": 0, "total": 4 },  // null when idle
              "arduino_reconnects": 0,
              "triggers": 120, "captured": 118, "capture_failed": 2,
              "uploaded": 90, "upload_failed": 0, "discarded": 0,
              "last": null },                        // summary of the previous task
  "motion": { "speed_cms": 31.4, "distance_mm": 12450.0, "encoder_count": 98214 },
  "arduino":{ "serial_connected": true, "last_message_at": 1731772799.5, "trigger_enabled": true,
              "listener_running": true, "port_present": true, "simulator_running": false },
  "queues": { "camera": {"0": 0, "1": 1}, "camera_maxsize": 30, "capture_inflight": 1,
              "collector": 0, "stitch": 4, "stitch_mb": 11.2, "raw": 0, "raw_mb": 0.0,
              "disk_retry": 0, "disk_spill": 0, "pending_total": 5 },
  "totals": { "triggers": 1200, "captures_ok": 1180, "captures_failed": 20,
              "uploads_ok": 1100, "uploads_failed": 3, "uptime_seconds": 3600 },
  "active_style": "default"
}
```

This covers everything `monitor.js` renders. The `task` counters are per task; `totals` keep their since-listener-start
meaning for the monitor page. `GET /api/task/status` returns the same snapshot.

### Serving it

- The socket uses `flask-sock` on the existing gunicorn `gthread` setup. **Each open socket holds one of the 8 worker
  threads**, as each monitor tab's queue SSE stream did before, so connections are capped at 4 (a fifth is closed with
  code 1013) and the monitor page uses the socket instead of SSE plus polling while it is healthy.
- One broadcaster thread builds the snapshot once per tick and fans it out to per-client bounded queues that drop their
  oldest message when a client is slow.
- If the socket cannot connect, the monitor page falls back to polling `/api/decoder/status` and the SSE stream.

## Not covered

- **Authentication** on the task endpoints and the websocket. The socket being read-only keeps this easy to add.
- **Hardware.** Everything has been verified against the mock rig (fake Arduino, cameras and upload server), not on the
  real Pi, Arduino and cameras, and not under gunicorn on Linux.
- **Live preview** while `start` switches the cameras into hardware-trigger mode has not been examined.
