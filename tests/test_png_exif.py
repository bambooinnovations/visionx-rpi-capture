"""png_with_exif: splice an EXIF block into a 16-bit PNG as an eXIf chunk."""
from __future__ import annotations

import struct
import zlib

import pytest

np = pytest.importorskip("numpy")
cv2 = pytest.importorskip("cv2")
piexif = pytest.importorskip("piexif")

from camera.rawisp import png_with_exif  # noqa: E402


def _chunks(png: bytes):
    pos = 8
    while pos < len(png):
        (length,) = struct.unpack(">I", png[pos:pos + 4])
        ctype = png[pos + 4:pos + 8]
        data = png[pos + 8:pos + 8 + length]
        (crc,) = struct.unpack(">I", png[pos + 8 + length:pos + 12 + length])
        yield ctype, data, crc
        pos += 12 + length


@pytest.fixture()
def png16() -> tuple[bytes, "np.ndarray"]:
    rng = np.random.default_rng(1)
    img = rng.integers(0, 65535, (40, 56, 3), dtype=np.uint16)
    ok, enc = cv2.imencode(".png", img)
    assert ok
    return enc.tobytes(), img


@pytest.fixture()
def exif_blob() -> bytes:
    return piexif.dump({
        "0th": {piexif.ImageIFD.Make: b"MindVision", piexif.ImageIFD.Model: b"MV-TEST"},
        "Exif": {piexif.ExifIFD.ExposureTime: (4000, 1_000_000),
                 piexif.ExifIFD.UserComment: b"ASCII\x00\x00\x00" + b'{"gain": 8}'},
        "GPS": {}, "1st": {},
    })


def test_chunk_is_placed_after_ihdr_before_idat_with_valid_crc(png16, exif_blob):
    png, _ = png16
    out = png_with_exif(png, exif_blob)
    types = [c[0] for c in _chunks(out)]
    assert types[0] == b"IHDR" and types[1] == b"eXIf"
    assert types.index(b"eXIf") < types.index(b"IDAT")
    assert types.count(b"eXIf") == 1 and types[-1] == b"IEND"
    for ctype, data, crc in _chunks(out):
        assert zlib.crc32(ctype + data) & 0xFFFFFFFF == crc, ctype


def test_chunk_holds_a_bare_tiff_block_not_the_jpeg_exif_header(png16, exif_blob):
    out = png_with_exif(png16[0], exif_blob)
    data = next(d for t, d, _ in _chunks(out) if t == b"eXIf")
    assert data[:4] in (b"II*\x00", b"MM\x00*")
    assert not data.startswith(b"Exif")


def test_pixels_are_untouched(png16, exif_blob):
    png, img = png16
    out = png_with_exif(png, exif_blob)
    decoded = cv2.imdecode(np.frombuffer(out, np.uint8), cv2.IMREAD_UNCHANGED)
    assert decoded.dtype == np.uint16 and (decoded == img).all()


def test_tags_read_back_through_pillow(png16, exif_blob):
    pil = pytest.importorskip("PIL.Image")
    import io
    out = png_with_exif(png16[0], exif_blob)
    exif = pil.open(io.BytesIO(out)).getexif()
    assert exif[271] == "MindVision" and exif[272] == "MV-TEST"   # Make, Model


def test_accepts_a_bare_tiff_block_too(png16, exif_blob):
    out = png_with_exif(png16[0], exif_blob[6:])
    assert any(t == b"eXIf" for t, _, _ in _chunks(out))


@pytest.mark.parametrize("bad_png", [b"", b"not a png", b"\x89PNG\r\n\x1a\n"])
def test_rejects_non_png(exif_blob, bad_png):
    with pytest.raises((ValueError, struct.error)):
        png_with_exif(bad_png, exif_blob)


def test_rejects_garbage_exif(png16):
    with pytest.raises(ValueError):
        png_with_exif(png16[0], b"definitely not exif")
