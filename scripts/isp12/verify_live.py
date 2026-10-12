"""List every raw format a camera offers and check each one against the SDK's own ISP.

Run this when a new camera model arrives (on the Pi, with the app stopped):

    .venv/bin/python scripts/isp12/verify_live.py [camera_index] [--no-controls]

For each format the camera lists that camera.rawformat can describe, it switches the camera to that format, decodes
live frames both ways (SDK ISP vs camera.rawisp) and prints the verdict. Point the camera at a scene with some
detail (not blank, not blown out); a flat scene is reported as inconclusive.

The output tells you which bit depths the settings page will offer and whether each was confirmed. A format
passing means the frame size, media type, container, Bayer pattern and the top 8 bits of every pixel decode
correctly. It cannot prove the placement of the bits below the top 8 (see camera/rawverify.py); the
"low bits" column says whether that layout was ever confirmed on real hardware.

Unless --no-controls is given it also runs a negative control on the first Bayer format that passes: the same
frames are checked under a deliberately wrong declared Bayer pattern, which must be detected and corrected.
Nothing is saved to the camera; the original format is restored at the end.
"""
import sys
import time
import ctypes
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import numpy as np
import mvsdk
from camera import rawformat as rf, rawverify


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    controls = "--no-controls" not in sys.argv
    idx = int(args[0]) if args else 0
    dev = mvsdk.CameraEnumerateDevice()[idx]
    h = mvsdk.CameraInit(dev, 2, 0)
    cap = mvsdk.CameraGetCapability(h)
    mono_sensor = bool(cap.sIspCapacity.bMonoSensor)
    mvsdk.CameraSetIspOutFormat(h, mvsdk.CAMERA_MEDIA_TYPE_MONO8 if mono_sensor else mvsdk.CAMERA_MEDIA_TYPE_BGR8)
    mvsdk.CameraSetTriggerMode(h, 0)
    mvsdk.CameraSetAeState(h, 1)                       # auto exposure so the scene is usable on any model
    W, H = cap.sResolutionRange.iWidthMax, cap.sResolutionRange.iHeightMax
    out = mvsdk.CameraAlignMalloc(W * H * 3, 16)
    for name in ("CameraSetSharpness", "CameraSetNoiseFilter"):    # compare the decode only
        try:
            getattr(mvsdk, name)(h, 0)
        except Exception:
            pass

    registry = rf.build_registry(mvsdk)
    formats = rf.camera_formats(cap, registry)
    print(f"{dev.GetFriendlyName()}  serial {dev.GetSn()}  {'mono' if mono_sensor else 'colour'} sensor  {W}x{H}")
    print(f"formats the SDK lists: {cap.iMediaTypeDesc}; ones we can describe: {len(formats)}")
    skipped = [cap.pMediaTypeDesc[i].acDescription.decode(errors='replace') for i in range(cap.iMediaTypeDesc)
               if cap.pMediaTypeDesc[i].iMediaType not in registry]
    if skipped:
        print("  not decodable here:", ", ".join(skipped))
    original = mvsdk.CameraGetMediaType(h)

    def switch(i):
        mvsdk.CameraStop(h)
        try:
            mvsdk.CameraSetMediaType(h, i)
        finally:
            mvsdk.CameraPlay(h)
        time.sleep(0.4)
        for _ in range(4):
            try:
                r, _hd = mvsdk.CameraGetImageBuffer(h, 3000)
                mvsdk.CameraReleaseImageBuffer(h, r)
            except mvsdk.CameraException:
                break

    def grab() -> rawverify.Grab:
        raw, head = mvsdk.CameraGetImageBuffer(h, 3000)
        try:
            w, hh, code, nbytes = head.iWidth, head.iHeight, head.uiMediaType, head.uBytes
            raw_copy = np.frombuffer((ctypes.c_ubyte * nbytes).from_address(raw), np.uint8).copy()
            mvsdk.CameraImageProcess(h, raw, out, head)
            ch = 1 if head.uiMediaType == mvsdk.CAMERA_MEDIA_TYPE_MONO8 else 3
            ref = np.frombuffer((ctypes.c_ubyte * (head.iWidth * head.iHeight * ch)).from_address(out),
                                np.uint8).reshape(head.iHeight, head.iWidth, ch).copy()
        finally:
            mvsdk.CameraReleaseImageBuffer(h, raw)
        return rawverify.Grab(raw_copy, ref, w, hh, code, nbytes)

    print()
    print(f"{'#':>2}  {'format':24s} {'layout':7s} {'low bits':9s} result")
    first_ok = None
    for i, fmt in formats:
        if fmt.bits == 8:
            tag = "n/a (8-bit, SDK path)"
            print(f"{i:>2}  {fmt.label:24s} {fmt.layout:7s} {'-':9s} {tag}")
            continue
        try:
            switch(i)
            v = rawverify.verify_decode(grab, fmt)
        except Exception as exc:
            v = rawverify.Verdict(False, f"error: {exc}")
        low = "hardware" if fmt.low_bits_hardware_verified else "spec only"
        status = "PASS" if v.ok else ("INCONCLUSIVE" if v.inconclusive else "FAIL")
        print(f"{i:>2}  {fmt.label:24s} {fmt.layout:7s} {low:9s} {status}: {v.reason}")
        if v.ok and first_ok is None and not fmt.mono:
            first_ok = (i, fmt, v)

    if controls and first_ok is not None:
        i, fmt, v = first_ok
        wrong = "RG" if fmt.pattern != "RG" else "GB"
        print(f"\nnegative control on {fmt.label}: declare the pattern as {wrong} instead of {fmt.pattern}")
        switch(i)
        nv = rawverify.verify_decode(grab, replace(fmt, pattern=wrong))
        corrected = nv.ok and nv.pattern == fmt.pattern
        print(f"  -> {'PASS' if corrected else 'FAIL'}: ok={nv.ok} pattern={nv.pattern} ({nv.reason})")

    switch(original)
    mvsdk.CameraStop(h)
    mvsdk.CameraUnInit(h)
    return 0


if __name__ == "__main__":
    sys.exit(main())
