"""Tests for the capture-profile store and its /api/cameras/profiles endpoints.

Run: python -m unittest discover -s tests
"""
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from flask import Flask

from camera.mindvision import SETTING_KEYS
from camera.profiles import (
    ProfileConflict, ProfileError, ProfileNotFound, ProfileStore,
)

SERIAL = "SN-A"


def full(**overrides):
    """A complete settings dict (every SETTING_KEY), as the settings page produces."""
    base = {k: 1 for k in SETTING_KEYS}
    base.update(overrides)
    return base


class StoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = ProfileStore(Path(self._tmp.name) / "p.db")

    def test_create_get_list(self):
        p = self.store.create(SERIAL, "Bright", full(exposure_us=5000.0, gamma=90), "desc")
        self.assertEqual(self.store.get(SERIAL, p["id"])["settings"]["gamma"], 90)
        self.assertEqual(p["revision"], 1)
        self.assertEqual([x["name"] for x in self.store.list(SERIAL)], ["Bright"])

    def test_scoped_by_serial(self):
        p = self.store.create(SERIAL, "A", full(gamma=100))
        self.assertEqual(self.store.list("SN-B"), [])
        with self.assertRaises(ProfileNotFound):
            self.store.get("SN-B", p["id"])
        # same name allowed on another camera
        self.store.create("SN-B", "A", full(gamma=100))

    def test_unique_name_per_camera(self):
        self.store.create(SERIAL, "A", full(gamma=100))
        with self.assertRaises(ProfileConflict):
            self.store.create(SERIAL, "A", full(gamma=100))

    def test_validation(self):
        partial = {"gamma": 100}
        for name, settings in [("", full()), ("x", {}), ("x", partial), ("x", full(bogus=1)),
                               ("x", full(gamma=[1])), ("x", None)]:
            with self.assertRaises(ProfileError):
                self.store.create(SERIAL, name, settings)

    def test_update_bumps_revision(self):
        p = self.store.create(SERIAL, "A", full(gamma=100))
        u = self.store.update(SERIAL, p["id"], settings=full(gamma=120))
        self.assertEqual((u["revision"], u["settings"]["gamma"], u["name"]), (2, 120, "A"))
        u = self.store.update(SERIAL, p["id"], name="B")
        self.assertEqual((u["revision"], u["name"]), (3, "B"))

    def test_update_name_conflict(self):
        self.store.create(SERIAL, "A", full(gamma=100))
        b = self.store.create(SERIAL, "B", full(gamma=100))
        with self.assertRaises(ProfileConflict):
            self.store.update(SERIAL, b["id"], name="A")

    def test_active_and_delete_clears_pointer(self):
        p = self.store.create(SERIAL, "A", full(gamma=100))
        self.assertIsNone(self.store.get_active(SERIAL))
        self.store.set_active(SERIAL, p["id"])
        self.assertEqual(self.store.get_active(SERIAL)["profile"]["id"], p["id"])
        self.store.delete(SERIAL, p["id"])
        self.assertIsNone(self.store.get_active(SERIAL))
        with self.assertRaises(ProfileNotFound):
            self.store.delete(SERIAL, p["id"])

    def test_set_active_unknown_profile(self):
        with self.assertRaises(ProfileNotFound):
            self.store.set_active(SERIAL, "nope")

    def test_import_roundtrip_and_errors(self):
        a = self.store.create(SERIAL, "A", full(gamma=100))
        exported = self.store.list(SERIAL)
        other = ProfileStore(Path(self._tmp.name) / "other.db")
        self.assertEqual(other.import_profiles(SERIAL, exported), {"imported": 1, "errors": {}})
        self.assertEqual(other.get(SERIAL, a["id"])["name"], "A")
        # re-import overwrites by id
        self.assertEqual(other.import_profiles(SERIAL, exported)["imported"], 1)
        self.assertEqual(other.get(SERIAL, a["id"])["revision"], 2)
        # name clash with a different id is skipped
        res = other.import_profiles(SERIAL, [{"id": "x", "name": "A", "settings": full(gamma=1)}])
        self.assertEqual(res["imported"], 0)
        self.assertIn("x", res["errors"])

    def test_persists_across_instances(self):
        p = self.store.create(SERIAL, "A", full(gamma=100))
        self.store.set_active(SERIAL, p["id"])
        again = ProfileStore(Path(self._tmp.name) / "p.db")
        self.assertEqual(again.get_active(SERIAL)["profile"]["name"], "A")


class FakeCamera:
    """Just enough of MindVisionCamera for the profile routes."""
    _h_camera = 1
    camera_index = 0
    serial_number = SERIAL

    def __init__(self):
        self.state = full(gamma=100, exposure_us=1000.0)
        self.busy = False

    def current_settings(self):
        return dict(self.state)

    def apply_profile(self, settings):
        """All or nothing, like the real camera: a rejected value changes nothing."""
        if self.busy:
            raise RuntimeError("Camera is busy; try again")
        errors = {k: "out of range" for k, v in settings.items() if v == -1}
        if not errors:
            self.state.update(settings)
        return errors

    def profile_differences(self, settings):
        return {k: (v, self.state[k]) for k, v in settings.items()
                if k in self.state and self.state[k] != v}


class EndpointTests(unittest.TestCase):
    def setUp(self):
        import blueprints.mindvision as bp_mod
        import camera.profiles as profiles_mod

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        store = ProfileStore(Path(self._tmp.name) / "p.db")
        patcher = unittest.mock.patch.object(bp_mod, "_profiles", lambda: store)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.store, self.cam = store, FakeCamera()
        app = Flask(__name__)
        app.register_blueprint(bp_mod.create_blueprint({0: self.cam}, serial_listener=object()))
        self.client = app.test_client()

    def _create(self, name="A", **overrides):
        r = self.client.post("/api/cameras/profiles",
                             json={"name": name, "settings": full(**(overrides or {"gamma": 120}))})
        self.assertEqual(r.status_code, 201, r.get_json())
        return r.get_json()

    def test_create_list_get(self):
        p = self._create()
        r = self.client.get("/api/cameras/profiles").get_json()
        self.assertEqual(r["camera_serial"], SERIAL)
        self.assertEqual([x["id"] for x in r["profiles"]], [p["id"]])
        self.assertEqual(self.client.get(f"/api/cameras/profiles/{p['id']}").status_code, 200)

    def test_create_from_current(self):
        r = self.client.post("/api/cameras/profiles", json={"name": "Snap", "from_current": True})
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.get_json()["settings"], self.cam.state)

    def test_errors(self):
        self.assertEqual(self.client.post("/api/cameras/profiles", json={"name": "x"}).status_code, 400)
        partial = self.client.post("/api/cameras/profiles", json={"name": "x", "settings": {"gamma": 1}})
        self.assertEqual(partial.status_code, 400)
        self.assertIn("missing", partial.get_json()["error"])
        self._create("A")
        r = self.client.post("/api/cameras/profiles", json={"name": "A", "settings": full(gamma=1)})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self.client.get("/api/cameras/profiles/nope").status_code, 404)
        self.assertEqual(self.client.post("/api/cameras/profiles/apply", json={"profile_id": "nope"}).status_code, 404)
        self.assertEqual(self.client.get("/api/cameras/profiles?camera_id=9").status_code, 404)
        for body in (None, {}, {"profile_id": ""}, {"profile_id": 5}):
            self.assertEqual(self.client.post("/api/cameras/profiles/apply", json=body).status_code, 400)

    def test_apply_sets_active_and_tracks_drift(self):
        p = self._create(gamma=120)
        r = self.client.post("/api/cameras/profiles/apply", json={"profile_id": p["id"]})
        body = r.get_json()
        self.assertEqual((r.status_code, body["active_id"], body["modified"]), (200, p["id"], False))
        self.assertEqual(self.cam.state["gamma"], 120)
        # someone edits the camera by hand
        self.cam.state["gamma"] = 90
        active = self.client.get("/api/cameras/profiles/active").get_json()
        self.assertTrue(active["modified"])
        self.assertEqual(active["differences"]["gamma"], {"profile": 120, "current": 90})
        # editing the profile bumps revision; applying again clears the drift
        self.client.put(f"/api/cameras/profiles/{p['id']}", json={"settings": full(gamma=90)})
        active = self.client.get("/api/cameras/profiles/active").get_json()
        self.assertEqual((active["profile"]["revision"], active["modified"]), (2, False))

    def test_apply_busy(self):
        p = self._create(gamma=120)
        self.cam.busy = True
        self.assertEqual(self.client.post("/api/cameras/profiles/apply", json={"profile_id": p["id"]}).status_code, 409)

    def test_apply_rejected_changes_nothing(self):
        good = self._create("Good", gamma=120)
        self.client.post("/api/cameras/profiles/apply", json={"profile_id": good["id"]})
        before = dict(self.cam.state)
        bad = self._create("Bad", gamma=-1, exposure_us=2000.0)
        r = self.client.post("/api/cameras/profiles/apply", json={"profile_id": bad["id"]})
        self.assertEqual(r.status_code, 422)
        self.assertIn("gamma", r.get_json()["errors"])
        self.assertEqual(self.cam.state, before)
        # the previously applied profile is still the active one
        self.assertEqual(self.client.get("/api/cameras/profiles/active").get_json()["active_id"], good["id"])

    def test_id_routes_find_the_camera_from_the_profile(self):
        """Apply/get/update/delete need only the id, not camera_id."""
        import blueprints.mindvision as bp_mod
        cam1 = FakeCamera()
        cam1.camera_index, cam1.serial_number = 1, "SN-B"
        app = Flask(__name__)
        app.register_blueprint(bp_mod.create_blueprint({0: self.cam, 1: cam1}, serial_listener=object()))
        client = app.test_client()
        p = self.store.create("SN-B", "On cam 1", full(gamma=77))
        for url in (f"/api/cameras/profiles/{p['id']}", f"/api/cameras/profiles/{p['id']}?camera_id=0"):
            self.assertEqual(client.get(url).status_code, 200)
        r = client.post("/api/cameras/profiles/apply", json={"profile_id": p["id"]})  # no camera_id
        self.assertEqual((r.status_code, r.get_json()["camera_id"]), (200, 1))
        self.assertEqual(cam1.state["gamma"], 77)
        self.assertNotEqual(self.cam.state["gamma"], 77)  # camera 0 untouched
        self.assertEqual(client.get("/api/cameras/profiles/active?camera_id=1").get_json()["active_id"], p["id"])

    def test_profile_for_a_camera_that_is_not_connected(self):
        p = self.store.create("SN-GONE", "Orphan", full(gamma=5))
        r = self.client.post("/api/cameras/profiles/apply", json={"profile_id": p["id"]})
        self.assertEqual(r.status_code, 404)
        self.assertIn("not connected", r.get_json()["error"])

    def test_delete_active_clears_it(self):
        p = self._create()
        self.client.post("/api/cameras/profiles/apply", json={"profile_id": p["id"]})
        self.assertEqual(self.client.delete(f"/api/cameras/profiles/{p['id']}").status_code, 200)
        self.assertIsNone(self.client.get("/api/cameras/profiles/active").get_json()["active_id"])

    def test_export_import(self):
        self._create("A")
        exported = self.client.get("/api/cameras/profiles/export").get_json()
        self.assertEqual(len(exported["profiles"]), 1)
        r = self.client.post("/api/cameras/profiles/import", json={"profiles": exported["profiles"]})
        self.assertEqual((r.status_code, r.get_json()["imported"]), (200, 1))


if __name__ == "__main__":
    unittest.main()
