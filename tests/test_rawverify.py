"""camera.rawverify: the check that decides whether a raw format may be used, on synthetic frames."""
from __future__ import annotations

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("cv2")

from camera import rawformat as rf  # noqa: E402
from camera import rawisp, rawverify  # noqa: E402

W, H = 64, 48


def fmt_of(layout, bits, pattern="GB", mono=False):
    return rf.RawFormat(0x1000 + bits, f"X{bits}_{layout}", mono, None if mono else pattern, bits, layout)


def make_grab(truth: rf.RawFormat, declared: rf.RawFormat | None = None, values=None, ref_from=None, **over):
    """A grab() callable: the 'camera' sends `truth`-encoded bytes, the 'SDK' returns the right image."""
    declared = declared or truth
    vals = values if values is not None else np.random.default_rng(5).integers(0, 1 << truth.bits, (H, W), dtype=np.uint16)
    raw = rf.pack_numpy(vals, truth)
    pl = rawisp.RawPipeline(W, H, truth, native=False)
    ref = rawisp.to8(pl.process(raw)).copy()

    def grab():
        return rawverify.Grab(raw=raw.copy(), ref8=ref.copy(), width=over.get("width", W), height=H,
                              media_code=over.get("media_code", declared.code),
                              nbytes=over.get("nbytes", raw.size))
    return grab


@pytest.mark.parametrize("layout,bits", [("gev12", 12), ("gev10", 10), ("mipi10", 10), ("u16", 12), ("u16", 10)])
@pytest.mark.parametrize("pattern", ["GB", "RG", "GR", "BG"])
def test_a_correct_decode_is_accepted(layout, bits, pattern):
    f = fmt_of(layout, bits, pattern)
    v = rawverify.verify_decode(make_grab(f), f, native=False)
    assert v.ok and v.pattern == pattern and v.mae < 0.5, v


@pytest.mark.parametrize("declared,actual", [("RG", "GB"), ("GB", "BG"), ("GR", "RG")])
def test_a_wrong_declared_pattern_is_corrected(declared, actual):
    truth = fmt_of("gev12", 12, actual)
    claimed = fmt_of("gev12", 12, declared)
    v = rawverify.verify_decode(make_grab(truth, declared=claimed), claimed, native=False)
    assert v.ok and v.pattern == actual
    assert actual in v.reason and declared in v.reason


def test_known_limit_the_low_bits_cannot_be_checked_against_an_8_bit_reference():
    """gev12 and gev10 differ only in how the bits BELOW the top 8 are placed (byte 0 is P[11:4] vs P[9:2]).
    An 8-bit reference cannot see that, so the check passes. This is why RawFormat.low_bits_hardware_verified
    exists and why the settings report it separately: only layouts that were checked on a real camera are
    known to have correct low bits. (The SDK's 16-bit output is no help: it is not an exact copy of the
    sensor values, measured on an MV-SUS507GC.)"""
    truth, claimed = fmt_of("gev12", 12), fmt_of("gev10", 10)
    claimed = rf.RawFormat(truth.code, claimed.name, False, "GB", 10, "gev10")
    v = rawverify.verify_decode(make_grab(truth, declared=claimed), claimed, native=False)
    assert v.ok
    assert not v.low_bits_verified                     # the verdict itself never claims low-bit certainty


def test_a_wrong_container_alignment_is_caught_by_the_top_bits():
    # A 12-bit u16 container sent MSB-aligned (value << 4) decodes 16x too bright if read LSB-aligned.
    f = fmt_of("u16", 12)
    vals = np.random.default_rng(2).integers(0, 4096, (H, W), dtype=np.uint16)
    msb_aligned = (vals << 4).astype("<u2").view(np.uint8)
    ref = rawisp.to8(rawisp.RawPipeline(W, H, f, native=False).process(rf.pack_numpy(vals, f))).copy()
    grab = lambda: rawverify.Grab(msb_aligned.copy(), ref.copy(), W, H, f.code, msb_aligned.size)
    v = rawverify.verify_decode(grab, f, native=False)
    assert not v.ok and not v.inconclusive


def test_a_wrong_frame_size_is_rejected_with_the_expected_size_in_the_message():
    f = fmt_of("gev12", 12)
    v = rawverify.verify_decode(make_grab(f, nbytes=W * H * 2), f, native=False)
    assert not v.ok and str(f.frame_bytes(W, H)) in v.reason and "packing" in v.reason


def test_a_media_type_mismatch_is_rejected():
    f = fmt_of("gev12", 12)
    v = rawverify.verify_decode(make_grab(f, media_code=0x1234), f, native=False)
    assert not v.ok and "0x1234" in v.reason


def test_odd_widths_are_rejected_for_mipi():
    f = fmt_of("mipi10", 10)
    v = rawverify.verify_decode(make_grab(f, width=66), f, native=False)
    assert not v.ok and "width" in v.reason


def test_a_flat_scene_is_inconclusive_not_a_pass_and_not_a_failure_of_the_format():
    f = fmt_of("gev12", 12)
    flat = np.full((H, W), 2000, np.uint16)
    calls = []
    inner = make_grab(f, values=flat)
    v = rawverify.verify_decode(lambda: calls.append(1) or inner(), f, attempts=3, native=False)
    assert not v.ok and v.inconclusive and len(calls) == 3 and "detail" in v.reason


def test_a_blown_out_scene_is_inconclusive():
    f = fmt_of("gev12", 12)
    v = rawverify.verify_decode(make_grab(f, values=np.full((H, W), 4095, np.uint16)), f, attempts=2, native=False)
    assert v.inconclusive


def test_a_late_good_frame_is_used_after_flat_ones():
    f = fmt_of("gev12", 12)
    flat, good = make_grab(f, values=np.full((H, W), 2000, np.uint16)), make_grab(f)
    seq = iter([flat, flat, good])
    v = rawverify.verify_decode(lambda: next(seq)(), f, attempts=5, native=False)
    assert v.ok


def test_grab_errors_are_retried_then_reported_as_inconclusive():
    f = fmt_of("gev12", 12)
    good = make_grab(f)
    n = {"i": 0}

    def flaky():
        n["i"] += 1
        if n["i"] < 2:                                  # one transient error is retried
            raise TimeoutError("camera timed out")
        return good()
    assert rawverify.verify_decode(flaky, f, attempts=5, native=False).ok

    def dead():
        raise TimeoutError("camera timed out")
    v = rawverify.verify_decode(dead, f, attempts=2, native=False)
    assert not v.ok and v.inconclusive and "timed out" in v.reason


def test_a_camera_that_never_delivers_frames_is_given_up_on_after_two_errors():
    f = fmt_of("gev12", 12)
    calls = []

    def dead():
        calls.append(1)
        raise TimeoutError("camera timed out")
    v = rawverify.verify_decode(dead, f, attempts=6, native=False)
    assert v.inconclusive and len(calls) == 2           # not 6 x 3 s of waiting


@pytest.mark.parametrize("layout,bits", [("gev12", 12), ("u16", 12), ("gev10", 10)])
def test_mono_correct_and_incorrect(layout, bits):
    f = fmt_of(layout, bits, mono=True)
    assert rawverify.verify_decode(make_grab(f), f, native=False).ok
    # The same bytes read with the wrong packing have the wrong size (u16 is 2 B/px, packed 1.5 B/px): rejected.
    wrong = rf.RawFormat(f.code, "W", True, None, 12, "u16" if layout != "u16" else "gev12")
    v = rawverify.verify_decode(make_grab(f, declared=wrong), wrong, native=False)
    assert not v.ok and not v.inconclusive


def test_options_are_applied_to_our_side_of_the_comparison():
    # The SDK reference was produced rotated; our decode must be rotated the same way to agree.
    f = fmt_of("gev12", 12)
    vals = np.random.default_rng(9).integers(0, 4096, (H, W), dtype=np.uint16)
    raw = rf.pack_numpy(vals, f)
    pl = rawisp.RawPipeline(W, H, f, native=False)
    pl.set_options(rotate=1)
    ref = rawisp.to8(pl.process(raw)).copy()
    grab = lambda: rawverify.Grab(raw.copy(), ref.copy(), W, H, f.code, raw.size)
    assert rawverify.verify_decode(grab, f, {"rotate": 1}, native=False).ok
    assert not rawverify.verify_decode(grab, f, {}, native=False).ok
