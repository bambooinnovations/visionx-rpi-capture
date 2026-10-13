"""camera.rawisp: every layout x Bayer pattern decodes and demosaics correctly, native == numpy, mono works."""
from __future__ import annotations

import pytest

np = pytest.importorskip("numpy")
cv2 = pytest.importorskip("cv2")

from camera import rawformat as rf  # noqa: E402
from camera import rawisp  # noqa: E402

PATTERNS = ["GB", "RG", "GR", "BG"]
LAYOUT_BITS = [("u16", 10), ("u16", 12), ("u16", 14), ("u16", 16), ("gev12", 12), ("gev10", 10), ("mipi10", 10)]
W, H = 64, 48                                   # multiple of 4, small enough to be quick

NATIVE = rawisp.load_native() is not None
paths = [pytest.param(False, id="numpy")] + ([pytest.param(True, id="native")] if NATIVE else [])


def fmt_for(layout, bits, pattern):
    return rf.RawFormat(1, f"{pattern}{bits}_{layout}", False, pattern, bits, layout)


def scene(bits: int, seed: int = 3) -> np.ndarray:
    """A mosaic-worthy random image in 0 .. 2**bits-1 (any content works: the pipeline must be exact)."""
    return np.random.default_rng(seed).integers(0, 1 << bits, (H, W), dtype=np.uint16)


@pytest.mark.parametrize("native", paths)
@pytest.mark.parametrize("pattern", PATTERNS)
@pytest.mark.parametrize("layout,bits", LAYOUT_BITS)
def test_plain_pipeline_equals_unpack_then_opencv_demosaic(layout, bits, pattern, native):
    fmt = fmt_for(layout, bits, pattern)
    vals = scene(bits)
    raw = rf.pack_numpy(vals, fmt)
    pl = rawisp.RawPipeline(W, H, fmt, native=native)
    out = pl.process(raw)
    mosaic16 = (vals << (16 - bits)).astype(np.uint16)
    expected = cv2.cvtColor(mosaic16, getattr(cv2, f"COLOR_Bayer{rf.CV2_NAME[pattern]}2BGR"))
    assert out.dtype == np.uint16 and out.shape == (H, W, 3)
    assert (out == expected).all()


@pytest.mark.skipif(not NATIVE, reason="needs libfastisp.so (gcc)")
@pytest.mark.parametrize("pattern", PATTERNS)
@pytest.mark.parametrize("layout,bits", LAYOUT_BITS)
def test_native_unpack_equals_numpy_unpack(layout, bits, pattern):
    fmt = fmt_for(layout, bits, pattern)
    raw = rf.pack_numpy(scene(bits), fmt)
    a = rawisp.RawPipeline(W, H, fmt, native=True).unpack(raw).copy()
    b = rawisp.RawPipeline(W, H, fmt, native=False).unpack(raw).copy()
    assert (a == b).all()


@pytest.mark.skipif(not NATIVE, reason="needs libfastisp.so (gcc)")
@pytest.mark.parametrize("pattern", PATTERNS)
def test_native_filters_match_numpy_filters_for_every_pattern(pattern):
    fmt = fmt_for("gev12", 12, pattern)
    raw = rf.pack_numpy(scene(12), fmt)
    for opts, tol in (({"noise_filter": True}, 2), ({"sharpness": 60}, 80), ({"noise_filter": True, "sharpness": 100}, 120)):
        n = rawisp.RawPipeline(W, H, fmt, native=True); n.set_options(**opts)
        p = rawisp.RawPipeline(W, H, fmt, native=False); p.set_options(**opts)
        a, b = n.process(raw).astype(np.int32), p.process(raw).astype(np.int32)
        d = np.abs(a - b)[4:-4, 4:-4]                       # the 2-px border is reflected differently
        assert d.max() <= tol, (pattern, opts, d.max())


@pytest.mark.skipif(not NATIVE, reason="needs libfastisp.so (gcc)")
@pytest.mark.parametrize("pattern", PATTERNS)
def test_white_balance_gains_land_on_the_right_sites(pattern):
    fmt = fmt_for("gev12", 12, pattern)
    flat = np.full((H, W), 1000, np.uint16)
    raw = rf.pack_numpy(flat, fmt)
    pl = rawisp.RawPipeline(W, H, fmt, wb_gains=(2.0, 1.0, 0.5), native=True)
    mos = pl.unpack(raw)
    base = 1000 << 4
    for dy in range(2):
        for dx in range(2):
            colour = rf.PHASES[pattern][dy][dx]
            expect = int(base * {"R": 2.0, "G": 1.0, "B": 0.5}[colour])
            assert (mos[dy::2, dx::2] == expect).all(), (pattern, dy, dx, colour)


@pytest.mark.skipif(not NATIVE, reason="needs libfastisp.so (gcc)")
def test_black_level_is_subtracted_and_clipped_at_zero():
    fmt = fmt_for("gev12", 12, "GB")
    vals = np.full((H, W), 100, np.uint16); vals[0, 0] = 10
    mos = rawisp.RawPipeline(W, H, fmt, black_level=50, native=True).unpack(rf.pack_numpy(vals, fmt))
    assert mos[0, 0] == 0 and mos[0, 1] == (100 - 50) << 4


# ── controls on a non-default pattern ────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("native", paths)
def test_geometry_and_invert(native):
    fmt = fmt_for("gev12", 12, "RG")
    raw = rf.pack_numpy(scene(12), fmt)
    base = rawisp.RawPipeline(W, H, fmt, native=native).process(raw).copy()
    pl = rawisp.RawPipeline(W, H, fmt, native=native)
    pl.set_options(invert=True)
    assert (pl.process(raw) == 65535 - base).all()
    pl.set_options(mirror_h=True)
    assert (pl.process(raw) == base[:, ::-1]).all()
    pl.set_options(mirror_v=True)
    assert (pl.process(raw) == base[::-1]).all()
    pl.set_options(rotate=1)                                  # SDK iRot=1 -> clockwise on screen
    assert (pl.process(raw) == np.rot90(base, 3)).all()
    pl.set_options(rotate=1, mirror_h=True)                   # rotate first, then mirror the rotated frame
    assert (pl.process(raw) == np.rot90(base, 3)[:, ::-1]).all()


@pytest.mark.parametrize("native", paths)
def test_a_real_ccm_is_applied_and_identity_is_free(native):
    fmt = fmt_for("gev12", 12, "GB")
    raw = rf.pack_numpy(scene(12), fmt)
    plain = rawisp.RawPipeline(W, H, fmt, native=native).process(raw).copy()
    swap_rb = [[0, 0, 1], [0, 1, 0], [1, 0, 0]]
    out = rawisp.RawPipeline(W, H, fmt, ccm=swap_rb, native=native).process(raw)
    assert np.abs(out.astype(np.int32) - plain[..., ::-1].astype(np.int32)).max() <= 1


# ── mono sensors ─────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("native", paths)
@pytest.mark.parametrize("layout,bits", [("u16", 12), ("gev12", 12), ("gev10", 10), ("u16", 10)])
def test_mono_is_a_single_channel_16_bit_image(layout, bits, native):
    fmt = rf.RawFormat(1, "MONO", True, None, bits, layout)
    vals = scene(bits)
    pl = rawisp.RawPipeline(W, H, fmt, native=native)
    out = pl.process(rf.pack_numpy(vals, fmt))
    assert out.shape == (H, W) and out.dtype == np.uint16
    assert (out == vals << (16 - bits)).all()
    pl.set_options(invert=True, mirror_h=True)
    assert (pl.process(rf.pack_numpy(vals, fmt)) == (65535 - (vals << (16 - bits)))[:, ::-1]).all()


def test_mono_reports_the_controls_it_cannot_reproduce():
    fmt = rf.RawFormat(1, "MONO", True, None, 12, "gev12")
    pl = rawisp.RawPipeline(W, H, fmt, native=False)
    assert pl.unsupported_controls() == []
    pl.set_options(noise_filter=True, sharpness=40)
    assert sorted(pl.unsupported_controls()) == ["noise_filter", "sharpness"]


def test_colour_to_grey_option_is_ignored_on_a_mono_sensor():
    fmt = rf.RawFormat(1, "MONO", True, None, 12, "gev12")
    pl = rawisp.RawPipeline(W, H, fmt, native=False)
    pl.set_options(mono=True)
    assert pl.process(rf.pack_numpy(scene(12), fmt)).shape == (H, W)


# ── input validation ─────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("native", paths)
def test_wrong_frame_size_is_rejected(native):
    fmt = fmt_for("gev12", 12, "GB")
    pl = rawisp.RawPipeline(W, H, fmt, native=native)
    with pytest.raises(ValueError):
        pl.process(np.zeros(W * H * 3 // 2 - 3, np.uint8))


def test_mipi_needs_width_divisible_by_four():
    with pytest.raises(ValueError):
        rawisp.RawPipeline(66, 8, fmt_for("mipi10", 10, "GB"), native=False)


# ── compatibility wrapper used by scripts/isp12 ──────────────────────────────────────────────────────────────

def test_pipeline12_wrapper_maps_the_opencv_code_to_the_sdk_pattern():
    p = rawisp.Pipeline12(W, H, cv2.COLOR_BayerGR2BGR, native=False)       # what the MV-SUS507GC uses
    assert p.pattern == "GB" and p.fmt.layout == "gev12" and p.fmt.bits == 12


def test_buffers_are_allocated_lazily():
    fmt = fmt_for("gev12", 12, "GB")
    pl = rawisp.RawPipeline(W, H, fmt, native=False)
    raw = rf.pack_numpy(scene(12), fmt)
    pl.process(raw)
    assert set(pl._bufs) == {"rgb"}                           # no filter or colour-matrix buffers until needed
    pl.set_options(noise_filter=True, sharpness=30)
    pl.process(raw)
    assert set(pl._bufs) == {"rgb"}                           # numpy path filters via cv2 (own allocations)
