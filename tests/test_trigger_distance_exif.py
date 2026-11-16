"""Hardware-trigger distance is written into the capture EXIF."""
from __future__ import annotations

import json

import pytest

piexif = pytest.importorskip("piexif")
pytest.importorskip("numpy")

from camera.mindvision import MindVisionCamera  # noqa: E402
from camera.mindvision_trigger import SerialTriggerListener, distance_mm_from_count  # noqa: E402


def test_distance_from_count():
    assert distance_mm_from_count(1180, 118.0) == 100.0
    assert distance_mm_from_count(0, 118.0) == 0.0
    assert distance_mm_from_count(100, 0) is None


def test_exif_extra_uses_live_counts_per_cm():
    lst = SerialTriggerListener(cameras={})
    lst._arduino_state["arduino_config"] = {"counts_per_cm": 100.0}
    extra = lst._exif_extra({"trigger": 3, "count": 4000})
    assert extra == {"trigger_number": 3, "encoder_count": 4000, "distance_mm": 400.0}


def test_exif_extra_without_count_is_empty():
    assert SerialTriggerListener(cameras={})._exif_extra({}) == {}


def test_build_exif_embeds_distance():
    cam = MindVisionCamera.__new__(MindVisionCamera)
    cam._dev_info = None
    blob = cam._build_exif("2026-01-01T00:00:00+00:00", {"exposure_us": 1000},
                           {"distance_mm": 1200.0, "trigger_number": 3})
    assert blob
    exif = piexif.load(blob)
    assert exif["0th"][piexif.ImageIFD.ImageDescription] == b"distance_mm=1200.0"
    comment = json.loads(exif["Exif"][piexif.ExifIFD.UserComment][8:])
    assert comment["distance_mm"] == 1200.0 and comment["trigger_number"] == 3


def test_status_reports_distance_and_reset_zeroes_it():
    lst = SerialTriggerListener(cameras={})
    lst._arduino_state.update(arduino_config={"counts_per_cm": 100.0}, encoder_count=5000, speed_cms=4.0)
    assert lst.status()["distance_mm"] == 500.0
    lst.note_count_reset()
    st = lst.status()
    assert st["distance_mm"] == 0.0 and st["speed_cms"] == 0.0
