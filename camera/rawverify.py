"""Prove that our decode of a raw format matches what the SDK's own ISP makes of the same frame.

Only one raw layout (Bayer GB, 12-bit packed) was ever checked on real hardware. Every other format a camera can
offer is decoded from its published layout, so before such a format is trusted for captures, one live frame is
decoded two ways and the results compared:

  * the SDK's CameraImageProcess() output (8-bit), which is what the camera vendor considers correct
  * our RawPipeline output, reduced to 8 bits

A wrong bit layout, byte order or Bayer pattern changes the image completely (mean error of tens of levels), a right
decode agrees to well under one level. If the pattern the SDK declares does not match but another one does, the
other one is used.  The check is pure logic: the caller supplies `grab()`, so it is unit-tested with synthetic
frames and needs no camera.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

import numpy as np

from camera import rawformat as rf
from camera.rawisp import RawPipeline, to8

# Mean |ours - SDK| (8-bit levels) that still counts as the same image. A correct decode measured 0.15-0.5, the
# worst control combination 1.7; wrong decodes are 10-100+.
THRESHOLD_MAE = 4.0
MIN_STD = 3.0                    # a flat or blown-out scene cannot tell a right decode from a wrong one
MEAN_RANGE = (8.0, 247.0)


@dataclass
class Grab:
    """One frame as seen both ways. `raw` is a private copy of the camera's bytes (flat uint8)."""
    raw: np.ndarray
    ref8: np.ndarray             # the SDK ISP's 8-bit output for this very frame, (h, w[, c])
    width: int                   # frame header size of the raw frame
    height: int
    media_code: int
    nbytes: int                  # head.uBytes of the raw frame


@dataclass
class Verdict:
    ok: bool
    reason: str
    pattern: str | None = None   # the Bayer pattern to decode with (None for mono)
    mae: float | None = None
    inconclusive: bool = False   # True when the scene could not decide (not a failure of the format)
    # What this check can and cannot prove. It compares at 8 bits, so it verifies the frame size, the media type,
    # the container/byte order, the Bayer pattern and the top 8 bits. The placement of the bits BELOW the top 8
    # cannot be checked without a trusted reference, so this is always False; see RawFormat.low_bits_hardware_verified.
    low_bits_verified: bool = False
    tried: dict = field(default_factory=dict)   # pattern -> MAE, for diagnostics

    def as_dict(self) -> dict:
        return {"ok": self.ok, "reason": self.reason, "pattern": self.pattern,
                "mae": None if self.mae is None else round(self.mae, 3), "inconclusive": self.inconclusive,
                "low_bits_verified": self.low_bits_verified}


def _informative(ref8: np.ndarray) -> bool:
    return float(ref8.std()) >= MIN_STD and MEAN_RANGE[0] <= float(ref8.mean()) <= MEAN_RANGE[1]


def _mae(a8: np.ndarray, b8: np.ndarray) -> float:
    if b8.ndim == 3 and b8.shape[2] == 1:
        b8 = b8[..., 0]
    if a8.shape != b8.shape:
        return float("inf")
    return float(np.abs(a8.astype(np.int16) - b8.astype(np.int16)).mean())


def _decode_mae(grab: Grab, fmt: rf.RawFormat, pattern: str | None, options: dict, native: bool | None) -> float:
    pl = RawPipeline(grab.width, grab.height, fmt, pattern=pattern, native=native)
    pl.set_options(**options)
    return _mae(to8(pl.process(grab.raw)), grab.ref8)


def verify_decode(grab: Callable[[], Grab], fmt: rf.RawFormat, options: dict | None = None,
                  attempts: int = 6, native: bool | None = None, max_errors: int = 2) -> Verdict:
    """Decide whether `fmt` can be decoded correctly. `options` are the RawPipeline controls that are active on
    the camera (mirror/rotate/invert/colour-to-grey); sharpness and the noise filter should be off while checking."""
    options = dict(options or {})
    last = Verdict(False, "no frame could be grabbed", inconclusive=True)
    errors = 0
    for _ in range(attempts):
        try:
            g = grab()
        except Exception as exc:                         # camera timeouts etc.
            last = Verdict(False, f"could not grab a frame: {exc}", inconclusive=True)
            errors += 1
            if errors >= max_errors:                     # the camera is not delivering frames: stop waiting
                break
            continue
        if g.media_code != fmt.code:
            return Verdict(False, f"the camera is sending media type {g.media_code:#x}, not {fmt.name} ({fmt.code:#x})")
        if not fmt.width_ok(g.width):
            return Verdict(False, f"{fmt.label} cannot have a frame width of {g.width}")
        if g.nbytes != fmt.frame_bytes(g.width, g.height):
            return Verdict(False, f"a {g.width}x{g.height} frame is {g.nbytes} bytes, but {fmt.label} should be "
                                  f"{fmt.frame_bytes(g.width, g.height)}: the packing is not what we expect")
        if not _informative(g.ref8):
            last = Verdict(False, "the scene is too flat, dark or bright to verify the decode; "
                                  "point the camera at something with detail and retry", inconclusive=True)
            continue

        if fmt.mono:
            mae = _decode_mae(g, fmt, None, options, native)
            ok = mae <= THRESHOLD_MAE
            return Verdict(ok, f"matches the SDK (mean error {mae:.2f} levels)" if ok else
                           f"decoded image differs from the SDK's by {mae:.1f} levels on average", None, mae,
                           tried={None: mae})
        tried = {fmt.pattern: _decode_mae(g, fmt, fmt.pattern, options, native)}
        if tried[fmt.pattern] > THRESHOLD_MAE:           # the declared pattern is wrong, or the layout is
            for p in rf.PHASES:
                if p != fmt.pattern:
                    tried[p] = _decode_mae(g, fmt, p, options, native)
        best = min(tried, key=tried.get)
        if tried[best] <= THRESHOLD_MAE:
            note = "" if best == fmt.pattern else f" (the pattern is {best}, not the declared {fmt.pattern})"
            return Verdict(True, f"matches the SDK (mean error {tried[best]:.2f} levels){note}", best, tried[best], tried=tried)
        return Verdict(False, f"decoded image differs from the SDK's by {tried[best]:.1f} levels on average, "
                              f"for every Bayer pattern: the bit layout is not what we expect",
                       None, tried[best], tried=tried)
    return last
