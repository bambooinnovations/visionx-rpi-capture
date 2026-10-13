"""camera.rawformat: format registry, pattern mapping and the reference decoders (no camera needed)."""
from __future__ import annotations

import types
from types import SimpleNamespace

import pytest

np = pytest.importorskip("numpy")

from camera import rawformat as rf  # noqa: E402


def fake_sdk(names: list[str]) -> types.ModuleType:
    m = types.ModuleType("mvsdk")
    for i, n in enumerate(names):
        setattr(m, "CAMERA_MEDIA_TYPE_" + n, 0x01000000 + i)
    return m


ALL_NAMES = [
    "MONO8", "MONO8S", "MONO1P", "MONO4P", "MONO10", "MONO10_PACKED", "MONO12", "MONO12_PACKED", "MONO14", "MONO16",
    "BAYGR8", "BAYRG8", "BAYGB8", "BAYBG8",
    "BAYGR10", "BAYRG10", "BAYGB10", "BAYBG10", "BAYGR12", "BAYRG12", "BAYGB12", "BAYBG12",
    "BAYGR10_PACKED", "BAYRG10_PACKED", "BAYGB12_PACKED", "BAYBG12_PACKED",
    "BAYGR10_MIPI", "BAYRG10_MIPI", "BAYGR16", "RGB8", "BGR8", "YUV422", "H264_MV",
]


@pytest.fixture(scope="module")
def reg():
    return rf.build_registry(fake_sdk(ALL_NAMES))


def by_name(reg, name):
    return next(f for f in reg.values() if f.name == name)


@pytest.mark.parametrize("name,mono,pattern,bits,layout", [
    ("BAYGB12_PACKED", False, "GB", 12, "gev12"),
    ("BAYBG12_PACKED", False, "BG", 12, "gev12"),
    ("BAYGR10_PACKED", False, "GR", 10, "gev10"),
    ("BAYRG10_MIPI", False, "RG", 10, "mipi10"),
    ("BAYRG12", False, "RG", 12, "u16"),
    ("BAYGR16", False, "GR", 16, "u16"),
    ("BAYGB8", False, "GB", 8, "u8"),
    ("MONO12", True, None, 12, "u16"),
    ("MONO12_PACKED", True, None, 12, "gev12"),
    ("MONO10_PACKED", True, None, 10, "gev10"),
    ("MONO14", True, None, 14, "u16"),
    ("MONO8", True, None, 8, "u8"),
])
def test_names_are_parsed(reg, name, mono, pattern, bits, layout):
    f = by_name(reg, name)
    assert (f.mono, f.pattern, f.bits, f.layout) == (mono, pattern, bits, layout)


@pytest.mark.parametrize("name", ["MONO8S", "MONO1P", "MONO4P", "RGB8", "BGR8", "YUV422", "H264_MV"])
def test_formats_we_cannot_decode_are_ignored(reg, name):
    assert not any(f.name == name for f in reg.values())


def test_frame_bytes():
    f = lambda layout: rf.RawFormat(1, "X", False, "GB", 12, layout)
    assert f("u8").frame_bytes(8, 4) == 32
    assert f("u16").frame_bytes(8, 4) == 64
    assert f("gev12").frame_bytes(8, 4) == 48
    assert f("gev10").frame_bytes(8, 4) == 48
    assert f("mipi10").frame_bytes(8, 4) == 40
    assert f("gev12").frame_bytes(2448, 2048) == 7_520_256          # the number the MV-SUS507GC actually sends
    assert f("gev12").frame_bytes(5488, 3672) == 30_227_904


def test_width_constraints():
    assert rf.RawFormat(1, "X", False, "GB", 10, "mipi10", ).width_ok(5488)
    assert not rf.RawFormat(1, "X", False, "GB", 10, "mipi10").width_ok(5486)
    assert not rf.RawFormat(1, "X", False, "GB", 12, "gev12").width_ok(5487)


# ── candidates / depths ──────────────────────────────────────────────────────────────────────────────────────

def cap_with(reg, names):
    ids = {f.name: f.code for f in reg.values()}
    descs = [SimpleNamespace(iMediaType=ids[n]) for n in names]
    return SimpleNamespace(iMediaTypeDesc=len(descs), pMediaTypeDesc=descs)


def test_camera_formats_and_depths(reg):
    cap = cap_with(reg, ["BAYGB8", "BAYGB12_PACKED"])
    formats = rf.camera_formats(cap, reg)
    assert [f.name for _, f in formats] == ["BAYGB8", "BAYGB12_PACKED"]
    assert rf.available_depths(formats) == [8, 12]


def test_unknown_entries_are_skipped():
    reg = rf.build_registry(fake_sdk(["BAYGB8"]))
    cap = SimpleNamespace(iMediaTypeDesc=2, pMediaTypeDesc=[SimpleNamespace(iMediaType=0x01000000),
                                                             SimpleNamespace(iMediaType=0xDEAD)])
    assert [i for i, _ in rf.camera_formats(cap, reg)] == [0]


def test_packed_is_preferred_over_unpacked_and_mipi(reg):
    cap = cap_with(reg, ["BAYRG10", "BAYRG10_MIPI", "BAYRG10_PACKED", "BAYRG8"])
    cands = rf.candidates_for_depth(rf.camera_formats(cap, reg), 10)
    assert [f.layout for _, f in cands] == ["gev10", "mipi10", "u16"]


def test_no_cap_gives_nothing(reg):
    assert rf.camera_formats(None, reg) == []


# ── decoders ─────────────────────────────────────────────────────────────────────────────────────────────────

LAYOUT_BITS = [("u8", 8), ("u16", 10), ("u16", 12), ("u16", 14), ("u16", 16), ("gev12", 12), ("gev10", 10), ("mipi10", 10)]


@pytest.mark.parametrize("layout,bits", LAYOUT_BITS)
def test_pack_unpack_roundtrip_at_16_bit_scale(layout, bits):
    rng = np.random.default_rng(7)
    w, h = 16, 6
    vals = rng.integers(0, 1 << bits, (h, w), dtype=np.uint16)
    fmt = rf.RawFormat(1, "X", False, "GB", bits, layout)
    raw = rf.pack_numpy(vals, fmt)
    assert raw.size == fmt.frame_bytes(w, h)
    out = rf.unpack_numpy(raw, w, h, fmt)
    assert out.dtype == np.uint16 and out.shape == (h, w)
    expected = vals << (8 if layout == "u8" else 16 - bits)
    assert (out == expected).all()


def test_gev12_matches_the_layout_verified_on_hardware():
    # b0=P0[11:4], b1=P1[3:0]<<4 | P0[3:0], b2=P1[11:4]
    fmt = rf.RawFormat(1, "X", False, "GB", 12, "gev12")
    raw = np.array([0xAB, 0x5C, 0xDE], np.uint8)                   # P0 = 0xABC, P1 = 0xDE5
    out = rf.unpack_numpy(raw, 2, 1, fmt)
    assert out.tolist() == [[0xABC0, 0xDE50]]


def test_mipi10_matches_the_csi2_layout():
    fmt = rf.RawFormat(1, "X", False, "GB", 10, "mipi10")
    # P0..P3 = 0x3FF, 0x000, 0x201, 0x100 ; b4 = P3[1:0]<<6 | P2[1:0]<<4 | P1[1:0]<<2 | P0[1:0] = 0<<6|1<<4|0<<2|3
    raw = np.array([0xFF, 0x00, 0x80, 0x40, 0b00010011], np.uint8)
    out = rf.unpack_numpy(raw, 4, 1, fmt)
    assert out.tolist() == [[0x3FF << 6, 0, 0x201 << 6, 0x100 << 6]]


def test_u16_ignores_bits_above_the_depth():
    fmt = rf.RawFormat(1, "X", False, "GB", 12, "u16")
    raw = np.array([0xFF, 0xFF], np.uint8)                          # 0xFFFF in a 12-bit container
    assert rf.unpack_numpy(raw, 1, 1, fmt).tolist() == [[0xFFF0]]


def test_wrong_size_is_rejected():
    fmt = rf.RawFormat(1, "X", False, "GB", 12, "gev12")
    with pytest.raises(ValueError):
        rf.unpack_numpy(np.zeros(10, np.uint8), 4, 4, fmt)


# ── Bayer pattern mapping to OpenCV ──────────────────────────────────────────────────────────────────────────

def _mosaic(pattern: str, h: int = 40, w: int = 48):
    """Smooth, linear colour ramps so bilinear demosaicing reproduces them exactly away from the edges."""
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    chans = {"R": 20 + 3.0 * xx, "G": 30 + 4.0 * yy, "B": 40 + 1.5 * (xx + yy)}
    mos = np.empty((h, w), np.float32)
    for dy in range(2):
        for dx in range(2):
            mos[dy::2, dx::2] = chans[rf.PHASES[pattern][dy][dx]][dy::2, dx::2]
    truth = np.stack([chans["B"], chans["G"], chans["R"]], -1)
    return mos.astype(np.uint16) * 100, (truth * 100).astype(np.float32)


@pytest.mark.parametrize("pattern", ["GB", "RG", "GR", "BG"])
def test_cv2_pattern_name_mapping(pattern):
    cv2 = pytest.importorskip("cv2")
    mos, truth = _mosaic(pattern)
    errs = {}
    for other in ["GB", "RG", "GR", "BG"]:
        code = getattr(cv2, f"COLOR_Bayer{rf.CV2_NAME[other]}2BGR")
        out = cv2.cvtColor(mos, code).astype(np.float32)
        errs[other] = float(np.abs(out[4:-4, 4:-4] - truth[4:-4, 4:-4]).mean())
    assert errs[pattern] < 50                                       # ramps up to ~20000 -> < 0.3 %
    assert all(errs[o] > 10 * max(errs[pattern], 1) for o in errs if o != pattern), errs
