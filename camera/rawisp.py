"""Develop raw MindVision frames (any bit depth, packing and Bayer pattern, colour or mono) to 16-bit images.

raw bytes -> uint16 mosaic at 16-bit scale (black level + WB gains folded in) -> noise filter / sharpness
-> demosaic (bilinear, same as the SDK) -> optional CCM -> mono / LUT / invert / rotate / mirror -> uint16 image.

Nothing is quantised to 8 bit until the caller asks. For the format and pattern of the MV-SUS507GC every control
here was fitted and verified against the SDK ISP on the same raw frame (scripts/isp12/parity.py). Other formats
are described by camera.rawformat and are only trusted once camera.mindvision.verify_decode has compared a live
frame against the SDK's own output.

The hot loops live in fastisp.c (built on first use with gcc/OpenMP, loaded via ctypes). Without it the numpy
implementation is used, with the same results (up to the 2-px border).
"""
from __future__ import annotations

import ctypes
import struct
import subprocess
import threading
import zlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import cv2
import numpy as np

from camera import rawformat as rf
from camera.rawformat import RawFormat

_HERE = Path(__file__).resolve().parent
_native = None
_native_tried = False
_LAYOUT_ID = {"u8": 0, "u16": 1, "gev12": 2, "gev10": 3, "mipi10": 4}


def load_native(rebuild: bool = False):
    """Return the ctypes handle for libfastisp.so (building it with gcc if needed), or None."""
    global _native, _native_tried
    if _native_tried and not rebuild:
        return _native
    _native_tried = True
    so, src = _HERE / "libfastisp.so", _HERE / "fastisp.c"
    try:
        if rebuild or not so.exists() or so.stat().st_mtime < src.stat().st_mtime:
            subprocess.run(["gcc", "-O3", "-march=native", "-fopenmp", "-shared", "-fPIC", str(src), "-o", str(so)],
                           check=True, capture_output=True)
        lib = ctypes.CDLL(str(so))
        vp, i = ctypes.c_void_p, ctypes.c_int
        lib.fastisp_set_threads.argtypes = [i]
        lib.fastisp_unpack.argtypes = [vp, vp, i, i, i, i, i, vp]
        lib.fastisp_unpack.restype = i
        lib.fastisp_noise_filter.argtypes = [vp, vp, i, i, i]
        lib.fastisp_sharpen.argtypes = [vp, vp, i, i, i]
        lib.fastisp_lut3.argtypes = [vp, ctypes.c_size_t, vp, vp, vp]
        _native = lib
    except Exception:
        _native = None
    return _native


def set_threads(n: int):
    """OpenMP threads used inside one pipeline call (also OpenCV's)."""
    lib = load_native()
    if lib:
        lib.fastisp_set_threads(int(n))
    cv2.setNumThreads(int(n))


def _ptr(a: np.ndarray):
    return a.ctypes.data_as(ctypes.c_void_p)


_PNG_SIG = b"\x89PNG\r\n\x1a\n"


def png_with_exif(png: bytes, exif: bytes) -> bytes:
    """Return `png` with an eXIf chunk (PNG 1.5) carrying `exif`.

    cv2.imencode writes 16-bit colour PNGs but cannot embed EXIF, and Pillow can embed EXIF but cannot save
    16-bit colour, so the chunk is spliced in by hand. `exif` may be a JPEG-style APP1 payload (piexif.dump
    output, which starts with the 6-byte "Exif" header) or a bare TIFF block; the PNG chunk wants the bare TIFF
    block. The chunk goes straight after IHDR, ahead of IDAT as the spec requires.
    """
    if not png.startswith(_PNG_SIG):
        raise ValueError("not a PNG")
    if exif.startswith(b"Exif\x00\x00"):
        exif = exif[6:]
    if exif[:4] not in (b"II*\x00", b"MM\x00*"):
        raise ValueError("EXIF data does not start with a TIFF header")
    ihdr_len = struct.unpack(">I", png[8:12])[0]
    if png[12:16] != b"IHDR":
        raise ValueError("PNG has no leading IHDR chunk")
    split = 8 + 12 + ihdr_len                       # signature + (length, type, data, crc) of IHDR
    chunk = struct.pack(">I", len(exif)) + b"eXIf" + exif + struct.pack(">I", zlib.crc32(b"eXIf" + exif) & 0xFFFFFFFF)
    return png[:split] + chunk + png[split:]


def unsupported_controls(fmt: RawFormat, options: dict) -> list[str]:
    """Controls in `options` (set_options keywords) that cannot be reproduced for `fmt`'s sensor type.

    The SDK's noise-filter and sharpness kernels were fitted on a Bayer sensor; what they do on a mono sensor is
    unknown, so a mono capture with either active must go through the SDK's own 8-bit path instead.
    """
    bad = []
    if fmt.mono:
        if options.get("noise_filter"):
            bad.append("noise_filter")
        if options.get("sharpness"):
            bad.append("sharpness")
    return bad


class RawPipeline:
    """One camera's raw -> 16-bit developer. Not thread-safe: buffers are reused between calls."""

    # sharpness strength s measured at SDK levels 25/50/75/100 -> 0.165/0.356/0.552/0.738 (0 at level 0)
    _SHARP_LEVELS = (0, 25, 50, 75, 100)
    _SHARP_S = (0.0, 0.165, 0.356, 0.552, 0.738)
    # noise filter: green sites 9/16 + 3/32*diag(+-1,+-1) + 1/64*axial(+-2); red/blue 11/16 + 1/16*axial(+-2) + 1/64*corner
    _NF_G = np.array([[0, 0, 1 / 64, 0, 0], [0, 3 / 32, 0, 3 / 32, 0], [1 / 64, 0, 9 / 16, 0, 1 / 64],
                      [0, 3 / 32, 0, 3 / 32, 0], [0, 0, 1 / 64, 0, 0]], np.float32)
    _NF_RB = np.array([[1 / 64, 0, 1 / 16, 0, 1 / 64], [0, 0, 0, 0, 0], [1 / 16, 0, 11 / 16, 0, 1 / 16],
                       [0, 0, 0, 0, 0], [1 / 64, 0, 1 / 16, 0, 1 / 64]], np.float32)

    def __init__(self, width: int, height: int, fmt: RawFormat, black_level: int = 0,
                 wb_gains=(1.0, 1.0, 1.0), ccm=None, native: bool | None = None, pattern: str | None = None):
        if not fmt.width_ok(width):
            raise ValueError(f"{fmt.name} needs a width divisible by {4 if fmt.layout == 'mipi10' else 2}, got {width}")
        self.w, self.h, self.fmt = width, height, fmt
        self.mono = fmt.mono
        self.pattern = None if fmt.mono else (pattern or fmt.pattern)
        self.black16 = black_level << 4          # black level is in 12-bit counts
        self._lib = load_native() if native in (None, True) else None
        self.native = self._lib is not None
        self._bayer = np.empty((height, width), np.uint16)
        self._bufs: dict[str, np.ndarray] = {}   # filter / colour buffers, allocated on first use (20 MP = 40-120 MB each)
        self._cv_code = None if self.mono else getattr(cv2, f"COLOR_Bayer{rf.CV2_NAME[self.pattern]}2BGR")
        self.set_color(wb_gains, ccm)
        self.set_options()

    def _buf(self, name: str, shape: tuple) -> np.ndarray:
        b = self._bufs.get(name)
        if b is None or b.shape != shape:
            b = self._bufs[name] = np.empty(shape, np.uint16)
        return b

    @property
    def channels(self) -> int:
        return 1 if self.mono else 3

    # -- configuration -------------------------------------------------------------------------------------
    def _phase_gains(self, gains: dict[str, float]) -> np.ndarray:
        if self.mono:
            return np.full(4, 256, np.int32)
        ph = rf.PHASES[self.pattern]
        return np.array([round(gains[ph[0][0]] * 256), round(gains[ph[0][1]] * 256),
                         round(gains[ph[1][0]] * 256), round(gains[ph[1][1]] * 256)], np.int32)

    def set_color(self, wb_gains=(1.0, 1.0, 1.0), ccm=None):
        """wb_gains: (R, G, B) multipliers; ccm: optional 3x3 applied to RGB (rows = out R, G, B). No-ops for mono."""
        r, g, b = (float(x) for x in wb_gains)
        self._wb = (r, g, b)
        self._gq = self._phase_gains({"R": r, "G": g, "B": b})
        wb = np.diag(np.asarray(self._wb, np.float32))
        ccm_m = np.eye(3, dtype=np.float32) if ccm is None else np.asarray(ccm, np.float32).reshape(3, 3)
        self._ccm_bgr = self._fast_matrix(np.ascontiguousarray(ccm_m[::-1, ::-1], np.float32))
        self._ccm_identity = np.allclose(self._ccm_bgr, np.eye(3), atol=1e-3)
        # numpy path: WB and CCM folded into one matrix applied after demosaic
        self._m_bgr = self._fast_matrix(np.ascontiguousarray((ccm_m @ wb)[::-1, ::-1], np.float32))
        self._identity = np.allclose(self._m_bgr, np.eye(3), atol=1e-3)

    @staticmethod
    def _fast_matrix(m):
        # OpenCV's 16U transform has a ~4x slower path for purely diagonal matrices (measured 89 ms vs 23 ms on a
        # Pi 5 at 5 MP); a 1e-4 off-diagonal nudge (<0.01 % of full scale) keeps the fast path.
        if not np.allclose(m, np.eye(3), atol=1e-3) and np.count_nonzero(m - np.diag(np.diag(m))) == 0:
            m = m + np.float32(1e-4) * (1 - np.eye(3, dtype=np.float32))
        return m

    @classmethod
    def sharp_strength(cls, level: int) -> float:
        return float(np.interp(level, cls._SHARP_LEVELS, cls._SHARP_S))

    def set_options(self, mirror_h=False, mirror_v=False, rotate=0, invert=False, mono=False,
                    lut=None, sharpness=0, noise_filter=False):
        """SDK ISP controls the camera does not bake into the raw frame (see scripts/isp12/fullsweep.py).

        rotate: SDK iRot (0..3).  lut: None or {"all"|"r"|"g"|"b": 4096-entry list, 12-bit in/out}.
        `mono` is the SDK's colour->grey conversion; it has no meaning on a mono sensor.
        """
        self.mirror_h, self.mirror_v, self.rotate = bool(mirror_h), bool(mirror_v), int(rotate)
        self.invert, self.mono_convert = bool(invert), bool(mono) and not self.mono
        self.sharpness, self.noise_filter = int(sharpness), bool(noise_filter)
        s = self.sharp_strength(self.sharpness) if self.sharpness else 0.0
        self._sharp_sq = round(s * 256)
        k = np.zeros((5, 5), np.float32)
        k[2, 2] = 1 + 4 * s
        k[0, 2] = k[4, 2] = k[2, 0] = k[2, 4] = -s
        self._sharp_k = k if self.sharpness else None
        # SDK monochrome = BT.601 limited-range luma (fitted: 0.097 B + 0.502 G + 0.254 R + 15.2 on the 8-bit scale)
        self._mono_m = np.tile(np.array([0.0979, 0.5041, 0.2568, 16 * 256], np.float32), (3, 1))
        self._lut16 = None
        if lut and not self.mono:
            x = np.arange(65536, dtype=np.float32) / 16.0
            base = np.arange(4096, dtype=np.float32)
            tabs = {}
            for ch, key in ((0, "b"), (1, "g"), (2, "r")):
                src = lut.get(key) if lut.get(key) is not None else lut.get("all")
                tabs[ch] = np.ascontiguousarray(
                    np.clip(np.interp(x, base, np.asarray(src, np.float32)) * 16.0, 0, 65535).astype(np.uint16)) \
                    if src is not None else None
            self._lut16 = tabs

    def unsupported_controls(self) -> list[str]:
        """Active controls this pipeline cannot reproduce for this sensor (the caller should fall back to 8-bit)."""
        return unsupported_controls(self.fmt, {"noise_filter": self.noise_filter, "sharpness": self.sharpness})

    # -- stages: unpack -------------------------------------------------------------------------------------
    def unpack(self, raw_u8: np.ndarray) -> np.ndarray:
        """Raw bytes -> uint16 mosaic at 16-bit scale. The native path also applies black level and WB gains here;
        the numpy path leaves both to demosaic()/color()."""
        if raw_u8.size != self.fmt.frame_bytes(self.w, self.h):
            raise ValueError(f"{self.fmt.name}: got {raw_u8.size} bytes, expected {self.fmt.frame_bytes(self.w, self.h)}")
        if self.native:
            rc = self._lib.fastisp_unpack(_ptr(raw_u8), _ptr(self._bayer), self.w, self.h,
                                          _LAYOUT_ID[self.fmt.layout], self.fmt.bits, self.black16, _ptr(self._gq))
            if rc != 0:
                raise ValueError(f"native unpack does not support layout {self.fmt.layout}")
            return self._bayer
        self._bayer[...] = rf.unpack_numpy(raw_u8, self.w, self.h, self.fmt)
        return self._bayer

    # -- stages: mosaic filters -----------------------------------------------------------------------------
    def prefilter(self, bayer16: np.ndarray) -> np.ndarray:
        """Noise filter then sharpness on the mosaic (order verified in parity.py)."""
        if self.mono:
            return bayer16
        out = bayer16
        g_diag = 1 if rf.PHASES[self.pattern][0][0] == "G" else 0
        if self.native:
            if self.noise_filter:
                b2 = self._buf("b2", bayer16.shape)
                self._lib.fastisp_noise_filter(_ptr(out), _ptr(b2), self.w, self.h, g_diag)
                out = b2
            if self._sharp_sq:
                dst = self._buf("b3", bayer16.shape) if out is not self._bufs.get("b3") else self._buf("b2", bayer16.shape)
                self._lib.fastisp_sharpen(_ptr(out), _ptr(dst), self.w, self.h, int(self._sharp_sq))
                out = dst
            return out
        if self.noise_filter:
            fg = cv2.filter2D(out, -1, self._NF_G, borderType=cv2.BORDER_REFLECT_101)
            res = cv2.filter2D(out, -1, self._NF_RB, borderType=cv2.BORDER_REFLECT_101)
            gy, gx = (0, 0) if g_diag else (0, 1)
            res[gy::2, gx::2] = fg[gy::2, gx::2]
            res[1 - gy::2, 1 - gx::2] = fg[1 - gy::2, 1 - gx::2]
            out = res
        if self._sharp_k is not None:
            out = cv2.filter2D(out, -1, self._sharp_k, borderType=cv2.BORDER_REFLECT_101)
        return out

    # -- stages: demosaic / colour --------------------------------------------------------------------------
    def demosaic(self, bayer16: np.ndarray) -> np.ndarray:
        bayer16 = self.prefilter(bayer16)
        if not self.native and self.black16:
            bayer16 = cv2.subtract(bayer16, self.black16)
        if self.mono:
            return bayer16
        return cv2.cvtColor(bayer16, self._cv_code, dst=self._buf("rgb", (self.h, self.w, 3)))

    def color(self, img16: np.ndarray) -> np.ndarray:
        """Native path: WB is already in the mosaic, so only a real CCM costs anything here."""
        if self.mono:
            return img16
        if self.native:
            if self._ccm_identity:
                return img16
            return cv2.transform(img16, self._ccm_bgr, dst=self._buf("out", img16.shape))
        if self._identity:
            return img16
        return cv2.transform(img16, self._m_bgr, dst=self._buf("out", img16.shape))

    # -- stages: output controls ----------------------------------------------------------------------------
    def finish(self, img16: np.ndarray) -> np.ndarray:
        """monochrome -> LUT -> invert -> rotate -> mirror (the order is verified against the SDK in parity.py)."""
        out = img16
        if self.mono_convert:
            out = cv2.transform(out, self._mono_m, dst=self._buf("mono", out.shape))
        if self._lut16 and any(t is not None for t in self._lut16.values()):
            if self.native:
                if not any(out is b for b in self._bufs.values()):
                    out = out.copy()                      # never write into a caller-owned array
                t = self._lut16
                self._lib.fastisp_lut3(_ptr(out), out.shape[0] * out.shape[1],
                                       _ptr(t[0]) if t[0] is not None else None,
                                       _ptr(t[1]) if t[1] is not None else None,
                                       _ptr(t[2]) if t[2] is not None else None)
            else:
                out = out.copy() if out is img16 else out
                for ch, tab in self._lut16.items():
                    if tab is not None:
                        out[..., ch] = tab[out[..., ch]]
        if self.invert:
            out = cv2.bitwise_not(out)
        if self.rotate:                                  # the SDK rotates first, then mirrors the rotated frame
            out = cv2.rotate(out, {1: cv2.ROTATE_90_CLOCKWISE, 2: cv2.ROTATE_180, 3: cv2.ROTATE_90_COUNTERCLOCKWISE}[self.rotate])
        if self.mirror_h or self.mirror_v:
            out = cv2.flip(out, -1 if (self.mirror_h and self.mirror_v) else (1 if self.mirror_h else 0))
        return out

    def process(self, raw_u8: np.ndarray) -> np.ndarray:
        """Result may alias an internal buffer that the next call overwrites: copy or consume it first."""
        return self.finish(self.color(self.demosaic(self.unpack(raw_u8))))


class Pipeline12(RawPipeline):
    """Compatibility wrapper for the scripts: a 12-bit packed Bayer pipeline given an OpenCV Bayer code."""

    def __init__(self, width: int, height: int, bayer_code: int, black_level: int = 0,
                 wb_gains=(1.0, 1.0, 1.0), ccm=None, native: bool | None = None):
        pattern = next(p for p, c in rf.CV2_NAME.items() if getattr(cv2, f"COLOR_Bayer{c}2BGR") == bayer_code)
        super().__init__(width, height, RawFormat(0, "BAY12_PACKED", False, pattern, 12, "gev12"),
                         black_level, wb_gains, ccm, native)


def to8(img16: np.ndarray) -> np.ndarray:
    """Plain 16 -> 8 bit (>>8) for comparing against the SDK's 8-bit output."""
    return (img16 >> 8).astype(np.uint8)


class StreamProcessor:
    """Frame-parallel wrapper: N workers, each with its own RawPipeline (and so its own buffers).

        sp = StreamProcessor(w, h, fmt, workers=2, omp_threads=2, sharpness=40)
        sp.submit(raw_u8, consume)   # consume(img16, tag) runs on the worker thread; copy/encode inside it
        sp.drain()

    submit() blocks when every worker is busy (natural back-pressure for a camera grab loop).
    """

    def __init__(self, width, height, fmt, workers=2, omp_threads=2, black_level=0,
                 wb_gains=(1.0, 1.0, 1.0), ccm=None, **options):
        set_threads(omp_threads)
        if isinstance(fmt, int):                        # an OpenCV Bayer code, as the scripts pass
            self._free = [Pipeline12(width, height, fmt, black_level, wb_gains, ccm) for _ in range(workers)]
        else:
            self._free = [RawPipeline(width, height, fmt, black_level, wb_gains, ccm) for _ in range(workers)]
        for p in self._free:
            p.set_options(**options)
        self._cv = threading.Condition()
        self._pool = ThreadPoolExecutor(max_workers=workers)
        self._pending = 0

    def submit(self, raw_u8: np.ndarray, consume, tag=None):
        with self._cv:
            while not self._free:
                self._cv.wait()
            pl = self._free.pop()
            self._pending += 1
        self._pool.submit(self._run, pl, raw_u8, consume, tag)

    def _run(self, pl, raw_u8, consume, tag):
        try:
            consume(pl.process(raw_u8), tag)
        finally:
            with self._cv:
                self._free.append(pl)
                self._pending -= 1
                self._cv.notify_all()

    def drain(self):
        with self._cv:
            while self._pending:
                self._cv.wait()

    def close(self):
        self.drain()
        self._pool.shutdown()
