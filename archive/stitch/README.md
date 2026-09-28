# Archived: stitch feature

**Deprecated — not used or wired into the app.** Kept for reference only.

| Archived path | Original path |
|---|---|
| `blueprints/stitch.py` | `blueprints/stitch.py` (`/api/stitch/*`) |
| `templates/stitch.html` | `templates/stitch.html` (`/stitch` page) |
| `static/stitch/` | `static/stitch/` |
| `tools/benchmark_stitch.py` | `tools/benchmark_stitch.py` |

The ChArUco board helpers that `blueprints/pxcm.py` needed were moved out to
`camera/charuco.py`; the archived blueprint now imports them from there.

## What was disconnected
- `/stitch` route and `/api/stitch` blueprint registration (`app.py`)
- nav links and the home-page "Stitch Setup" card
- hardware-trigger stitched upload (`SerialTriggerListener` no longer receives the stitch helpers; `_get_use_stitch()` returns `False`)
- the "Use stitch" system setting, the stitch health subsystem and the stitch WB lock on the camera settings page

## Restoring (if ever needed)
Move the files back to their original paths, re-add the route and blueprint
registration in `app.py`, pass `load_calibration` / `_stitch_frames` to
`SerialTriggerListener`, and restore the nav links. `git log` has the old wiring.
