"""capture_image in single-trigger mode (stream paused by the kiosk before capture).

Regression tests for the ckdl-qc3 2026-10-28 incident: with the stream just
ended the camera is in software-trigger mode and produces exactly ONE frame
per trigger. _discard_until_exposure_settled() consumed that only frame when
the dead stream's auto-exposure override was still registered (swapped=True),
and the real grab then starved into SDK timeout -12, surfacing as a 503
"No camera detected" on the kiosk.
"""
from __future__ import annotations

import threading
import types
from contextlib import contextmanager

import pytest

np = pytest.importorskip("numpy")
cv2 = pytest.importorskip("cv2")

import camera.mindvision as mv  # noqa: E402


def make_cam(monkeypatch, tmp_path, *, streaming: bool, swapped: bool, grabs: list):
    """A MindVisionCamera stub wired just far enough for capture_image.

    `grabs` is the per-call script for _grab_frame: each entry is the frame to
    return (None simulates an SDK -12 timeout).
    """
    cam = mv.MindVisionCamera.__new__(mv.MindVisionCamera)
    cam._h_camera = object()
    cam._lock = threading.Lock()
    cam._streaming = streaming
    cam._mode = mv.CameraMode.CAPTURE
    cam._capture_size = None
    cam._camera_index = 0
    calls = {"triggers": 0, "discards": 0, "grabs": 0}

    monkeypatch.setattr(
        mv, "mvsdk",
        types.SimpleNamespace(
            CameraSoftTrigger=lambda h: calls.__setitem__("triggers", calls["triggers"] + 1)
        ),
        raising=False,  # the vendor SDK only imports on the Pi
    )

    @contextmanager
    def fake_swap():
        yield swapped

    @contextmanager
    def fake_draft():
        yield False

    head = object()

    def fake_grab(timeout_ms=1000):
        calls["grabs"] += 1
        frame = grabs.pop(0) if grabs else None
        return (frame, head if frame is not None else None)

    cam.production_applied = fake_draft
    cam.capture_exposure_applied = fake_swap
    cam._raw_path_options = lambda: None
    cam._grab_frame = fake_grab
    cam.exposure_grab_timeout_ms = lambda: 10
    cam._discard_until_exposure_settled = lambda **kw: calls.__setitem__(
        "discards", calls["discards"] + 1
    )
    cam._snapshot_capture_state = lambda h: {}
    cam._build_exif = lambda captured_at, state=None: None
    cam._encode_jpeg = lambda frame, quality, exif_bytes, resize: b"\xff\xd8jpeg"
    return cam, calls


FRAME = np.zeros((4, 4, 3), dtype=np.uint8)


def test_trigger_mode_does_not_discard_the_only_frame(monkeypatch, tmp_path):
    """Not streaming + AE override swapped: the discard step must be skipped,
    so the single triggered frame reaches the real grab."""
    cam, calls = make_cam(monkeypatch, tmp_path, streaming=False, swapped=True, grabs=[FRAME])
    path, metrics = cam.capture_image(output_folder=tmp_path)
    assert path.exists()
    assert calls["discards"] == 0
    assert calls["triggers"] == 1


def test_trigger_mode_retries_with_fresh_trigger_on_starved_grab(monkeypatch, tmp_path):
    """A lost trigger frame costs one retry (new trigger + grab), not a 503."""
    cam, calls = make_cam(
        monkeypatch, tmp_path, streaming=False, swapped=False, grabs=[None, FRAME]
    )
    path, metrics = cam.capture_image(output_folder=tmp_path)
    assert path.exists()
    assert calls["triggers"] == 2
    assert calls["grabs"] == 2


def test_streaming_capture_still_discards_stale_exposure_frames(monkeypatch, tmp_path):
    """Streaming path unchanged: swapped exposure still flushes stale frames."""
    cam, calls = make_cam(monkeypatch, tmp_path, streaming=True, swapped=True, grabs=[FRAME])
    path, metrics = cam.capture_image(output_folder=tmp_path)
    assert path.exists()
    assert calls["discards"] == 1
    assert calls["triggers"] == 0
