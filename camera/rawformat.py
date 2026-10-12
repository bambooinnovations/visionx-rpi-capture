"""Describe and decode the raw pixel formats a MindVision camera can send.

The SDK reports each format as a media-type code (CameraDefine.h). Everything here is derived from the SDK's own
constant *names* (CAMERA_MEDIA_TYPE_BAYRG12_PACKED, ...), so a camera that offers a format we have never seen is
described automatically instead of being special-cased.

Layouts (all 8-bit-or-more, 16-bit-scale output = value << (16 - bits)):

  u8            one byte per pixel
  u16           16-bit little-endian container, value in the low `bits` bits  (BAYRG10, BAYRG12, MONO14, ...)
  gev12         2 pixels in 3 bytes: b0=P0[11:4], b1=P1[3:0]<<4|P0[3:0], b2=P1[11:4]      (*12_PACKED)
                (verified against the SDK ISP on MV-SUS507GC, "Bayer GB 12bit Packed")
  gev10         2 pixels in 3 bytes: b0=P0[9:2],  b1=P1[1:0]<<2|P0[1:0],  b2=P1[9:2]      (*10_PACKED, 12 bits/pixel)
  mipi10        4 pixels in 5 bytes: b0..b3=P0..P3[9:2], b4=P3[1:0]<<6|P2[1:0]<<4|P1[1:0]<<2|P0[1:0]   (*10_MIPI)

Only gev12 has been checked on real hardware. The others follow the published layouts but are NOT trusted until
camera.mindvision verifies a live frame against the SDK's own ISP (see verify_decode there).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import numpy as np

# 2x2 colour layout of each Bayer pattern, rows then columns: pattern -> ((c00, c01), (c10, c11)).
# The SDK/GenICam name is the top-left 2x2 in row-major order (BAYGB = [G B / R G]).
PHASES: dict[str, tuple[tuple[str, str], tuple[str, str]]] = {
    "GB": (("G", "B"), ("R", "G")),
    "RG": (("R", "G"), ("G", "B")),
    "GR": (("G", "R"), ("B", "G")),
    "BG": (("B", "G"), ("G", "R")),
}

# OpenCV names a Bayer code after the 2x2 window that starts at pixel (1, 1), so the code for an SDK pattern is the
# pattern of that shifted window.  (Confirmed on hardware for GB -> COLOR_BayerGR2BGR; the other three are checked
# synthetically in tests/test_rawformat.py.)
CV2_NAME: dict[str, str] = {"GB": "GR", "RG": "BG", "GR": "GB", "BG": "RG"}

# Layouts whose full bit placement was checked against a real camera. gev12: the nibble-order test and the
# per-pixel parity runs on the MV-SUS507GC (scripts/isp12). Everything else follows the published layout only.
HARDWARE_VERIFIED_LAYOUTS = frozenset({"gev12"})

_NAME_RE = re.compile(r"^CAMERA_MEDIA_TYPE_(MONO|BAY(GR|RG|GB|BG))(8|10|12|14|16)(_PACKED|_MIPI)?$")


@dataclass(frozen=True)
class RawFormat:
    code: int
    name: str            # e.g. "BAYRG12_PACKED"
    mono: bool
    pattern: str | None  # "GB"/"RG"/"GR"/"BG", None for mono
    bits: int            # significant bits per pixel: 8, 10, 12, 14 or 16
    layout: str          # "u8" | "u16" | "gev12" | "gev10" | "mipi10"

    @property
    def packed(self) -> bool:
        return self.layout in ("gev12", "gev10", "mipi10")

    def frame_bytes(self, width: int, height: int) -> int:
        """Exact size of one full frame in this layout (what head.uBytes must equal)."""
        n = width * height
        return {"u8": n, "u16": 2 * n, "gev12": n * 3 // 2, "gev10": n * 3 // 2, "mipi10": n * 5 // 4}[self.layout]

    def width_ok(self, width: int) -> bool:
        return width % (4 if self.layout == "mipi10" else 2) == 0

    @property
    def low_bits_hardware_verified(self) -> bool:
        """True when the placement of the bits below the top 8 was confirmed on a real camera (not just from the
        published layout). Only the 12-bit packed layout has been (MV-SUS507GC); see HARDWARE_VERIFIED_LAYOUTS."""
        return self.layout in HARDWARE_VERIFIED_LAYOUTS

    @property
    def label(self) -> str:
        kind = "Mono" if self.mono else f"Bayer {self.pattern}"
        suffix = {"gev12": " Packed", "gev10": " Packed", "mipi10": " MIPI"}.get(self.layout, "")
        return f"{kind} {self.bits}bit{suffix}"


def parse_name(name: str, code: int) -> RawFormat | None:
    m = _NAME_RE.match(name)
    if not m:
        return None
    mono = m.group(1) == "MONO"
    bits, suffix = int(m.group(3)), m.group(4)
    if bits == 8:
        layout = "u8"
    elif suffix == "_MIPI":
        layout = "mipi10" if bits == 10 else None
    elif suffix == "_PACKED":
        layout = {10: "gev10", 12: "gev12"}.get(bits)
    else:
        layout = "u16"
    if layout is None:
        return None
    return RawFormat(code=code, name=name[len("CAMERA_MEDIA_TYPE_"):], mono=mono,
                     pattern=None if mono else m.group(2), bits=bits, layout=layout)


def build_registry(constants) -> dict[int, RawFormat]:
    """{media-type code: RawFormat} for every CAMERA_MEDIA_TYPE_* constant on `constants` (the mvsdk module)."""
    reg: dict[int, RawFormat] = {}
    for attr in dir(constants):
        if attr.startswith("CAMERA_MEDIA_TYPE_"):
            fmt = parse_name(attr, getattr(constants, attr))
            if fmt is not None:
                reg[fmt.code] = fmt
    return reg


def camera_formats(cap, registry: dict[int, RawFormat]) -> list[tuple[int, RawFormat]]:
    """[(index into cap.pMediaTypeDesc, RawFormat)] for every entry the camera offers that we can describe."""
    out = []
    if cap is None:
        return out
    for i in range(cap.iMediaTypeDesc):
        fmt = registry.get(cap.pMediaTypeDesc[i].iMediaType)
        if fmt is not None:
            out.append((i, fmt))
    return out


# Preference when a camera offers several formats at the same bit depth: packed ones use less USB bandwidth.
_LAYOUT_PREFERENCE = {"gev12": 0, "gev10": 0, "mipi10": 1, "u16": 2, "u8": 3}


def candidates_for_depth(formats: list[tuple[int, RawFormat]], bits: int) -> list[tuple[int, RawFormat]]:
    """Formats at `bits`, best first (packed before unpacked, the pattern the camera lists first before the rest)."""
    found = [(i, f) for i, f in formats if f.bits == bits]
    return sorted(found, key=lambda x: (_LAYOUT_PREFERENCE[x[1].layout], x[0]))


def available_depths(formats: list[tuple[int, RawFormat]]) -> list[int]:
    """Bit depths this camera offers, ascending. 8 is always first when the camera has an 8-bit format."""
    return sorted({f.bits for _, f in formats})


# ── reference decoders (numpy) ───────────────────────────────────────────────────────────────────────────────

def unpack_numpy(raw: np.ndarray, width: int, height: int, fmt: RawFormat) -> np.ndarray:
    """Decode a raw frame to uint16 (height, width) at 16-bit scale. Slow reference; fastisp.c is the fast path."""
    raw = np.ascontiguousarray(raw).reshape(-1)
    n = width * height
    if raw.size != fmt.frame_bytes(width, height):
        raise ValueError(f"{fmt.name}: {raw.size} bytes, expected {fmt.frame_bytes(width, height)} for {width}x{height}")
    shift = 16 - fmt.bits
    out = np.empty(n, np.uint16)
    if fmt.layout == "u8":
        out[:] = raw.astype(np.uint16) << 8
    elif fmt.layout == "u16":
        v = raw.view("<u2").astype(np.uint16) & ((1 << fmt.bits) - 1)
        out[:] = v << shift
    elif fmt.layout == "gev12":
        b = raw.reshape(-1, 3).astype(np.uint16)
        out[0::2] = (b[:, 0] << 8) | ((b[:, 1] & 0x0F) << 4)
        out[1::2] = (b[:, 2] << 8) | ((b[:, 1] >> 4) << 4)
    elif fmt.layout == "gev10":
        b = raw.reshape(-1, 3).astype(np.uint16)
        out[0::2] = (b[:, 0] << 8) | ((b[:, 1] & 0x03) << 6)
        out[1::2] = (b[:, 2] << 8) | (((b[:, 1] >> 2) & 0x03) << 6)
    elif fmt.layout == "mipi10":
        b = raw.reshape(-1, 5).astype(np.uint16)
        for k in range(4):
            out[k::4] = (b[:, k] << 8) | (((b[:, 4] >> (2 * k)) & 0x03) << 6)
    else:  # pragma: no cover
        raise ValueError(fmt.layout)
    return out.reshape(height, width)


def pack_numpy(values: np.ndarray, fmt: RawFormat) -> np.ndarray:
    """Inverse of unpack_numpy for test data: `values` are (h, w) integers in 0..2**bits-1. Returns the raw bytes."""
    h, w = values.shape
    v = values.reshape(-1).astype(np.uint16)
    if fmt.layout == "u8":
        return v.astype(np.uint8)
    if fmt.layout == "u16":
        return v.astype("<u2").view(np.uint8)
    if fmt.layout == "gev12":
        p0, p1 = v[0::2], v[1::2]
        out = np.empty((p0.size, 3), np.uint8)
        out[:, 0], out[:, 1], out[:, 2] = p0 >> 4, ((p1 & 0xF) << 4) | (p0 & 0xF), p1 >> 4
        return out.reshape(-1)
    if fmt.layout == "gev10":
        p0, p1 = v[0::2], v[1::2]
        out = np.empty((p0.size, 3), np.uint8)
        out[:, 0], out[:, 1], out[:, 2] = p0 >> 2, ((p1 & 0x3) << 2) | (p0 & 0x3), p1 >> 2
        return out.reshape(-1)
    if fmt.layout == "mipi10":
        q = v.reshape(-1, 4)
        out = np.empty((q.shape[0], 5), np.uint8)
        for k in range(4):
            out[:, k] = q[:, k] >> 2
        out[:, 4] = sum(((q[:, k] & 0x3) << (2 * k)) for k in range(4))
        return out.reshape(-1)
    raise ValueError(fmt.layout)
