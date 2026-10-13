# 12-bit pipeline: verification and benchmark scripts

The production code is `camera/rawisp.py` (pipeline) and `camera/fastisp.c` (native hot loops, built with
gcc/OpenMP on first use). These scripts check it against the SDK's own ISP and measure it. They need a real
camera, so run them **on the Pi with the app stopped** (the app holds the cameras):

```bash
cd ~/Desktop/visionx-rpi-capture
.venv/bin/python scripts/isp12/parity.py 1        # camera index 1
```

| Script | What it answers |
|---|---|
| `parity.py` | Does our output match the SDK for every control we implement (mirror, rotate, invert, mono, LUT, noise filter, sharpness, combinations)? Mean difference in 8-bit levels; ~0.14 is the floor from the SDK truncating. |
| `fullsweep.py` | Which SDK ISP controls change the output relative to a plain demosaic (diverge) and which are already applied to the raw frame (match)? |
| `sdkcompare.py` | Same raw frame through the SDK and through our demosaic: bilinear vs edge-aware, rounding vs truncation. |
| `explore.py` | Dumps the camera's ISP state (gains, matrix, LUT, ...) and checks the 12-bit packed byte layout. |
| `verify_live.py` | **Run this when a new camera model arrives.** Lists every raw format the camera offers, switches to each and prints the verification verdict (what the settings page will offer and whether each format was confirmed), plus a negative control that must correct a deliberately wrong Bayer pattern. |
| `bench.py` | Native vs numpy equivalence, per-config cost, and live sustained fps (SDK 8-bit, in-thread, frame-parallel `StreamProcessor`). |

All settings changes are runtime-only and are reset by the scripts. Arguments are `[camera_index] [exposure_us]`.

## What was found (Pi 5, MV-SUS507GC, 2448x2048)

- The SDK's colour output is a bilinear demosaic, truncated to 8 bits. Gain, white balance, gamma, contrast and
  saturation are already applied to the raw frame by the camera/driver, so the 12-bit raw carries them.
- Mirror, rotate (the SDK rotates, then mirrors), invert, monochrome (BT.601 limited-range luma), LUT, noise
  filter and sharpness are applied by the SDK ISP and are re-implemented in `camera/rawisp.py`.
- Sharpness is approximate (0.5 levels at strength 25, 1.6 at 100): the SDK has a dead zone for weak edges that
  a linear kernel doesn't capture.
- Bit depth is the camera setting `bit_depth`. The options come from the raw formats the camera lists
  (`camera/rawformat.py` describes any of them: Bayer pattern, 8-16 bit, packed or unpacked, colour or mono);
  captures above 8 bits are saved as 16-bit PNG with EXIF.
- A format is verified against the SDK's own ISP before it is used (`camera/rawverify.py`). That confirms size,
  media type, container, Bayer pattern and the top 8 bits. The placement of the lower bits cannot be verified in
  software: the SDK's 16-bit output is not an exact copy of the sensor values (7-11 counts off at native pixel
  sites, measured), so only layouts checked on hardware (12-bit packed on the MV-SUS507GC) are marked as such.
- The SDK's ISP can output `BGR16`/`RGB16` (the header only documents 8-bit), but it takes ~240 ms per 5 MP frame
  against ~12 ms for `camera/rawisp.py`, and has fewer distinct levels because it interpolates at integer precision.
