"""BMP frame comparison tests (verified desktop actions)."""

from __future__ import annotations

import struct

import pytest

from greatsage.exceptions import VisionValidationError
from greatsage.vision.capture import encode_bmp, parse_bmp_dimensions
from greatsage.vision.diff import compare_bmp_frames, parse_bmp_frame


def _solid_bmp(
    width: int,
    height: int,
    color: tuple[int, int, int],
    *,
    top_down: bool = False,
) -> bytes:
    """Build a 24-bit solid BMP with a chosen row order."""
    row = bytes(color) * width
    stride = ((width * 3 + 3) // 4) * 4
    padding = b"\x00" * (stride - width * 3)
    rows: list[bytes] = []
    for _ in range(height):
        rows.append(row + padding)
    if not top_down:
        rows.reverse()
    pixels = b"".join(rows)
    signed_height = -height if top_down else height
    size = 54 + len(pixels)
    header = struct.pack("<2sIHHI", b"BM", size, 0, 0, 54)
    dib = struct.pack(
        "<IiiHHIIiiII",
        40,
        width,
        signed_height,
        1,
        24,
        0,
        len(pixels),
        0,
        0,
        0,
        0,
    )
    return header + dib + pixels


def _set_pixel(
    image: bytes,
    width: int,
    height: int,
    x: int,
    y: int,
    color: tuple[int, int, int],
) -> bytes:
    """Return a copy of a 24-bit bottom-up BMP with one pixel changed."""
    data = bytearray(image)
    stride = ((width * 3 + 3) // 4) * 4
    offset = 54 + (height - 1 - y) * stride + x * 3
    data[offset : offset + 3] = bytes(color)
    return bytes(data)


def test_parse_bmp_dimensions_accepts_top_down() -> None:
    image = _solid_bmp(8, 4, (10, 20, 30), top_down=True)
    assert parse_bmp_dimensions(image) == (8, 4)


def test_parse_frame_row_order() -> None:
    top = (0, 0, 255)  # red in BGR
    bottom = (0, 255, 0)  # green in BGR
    image = _solid_bmp(4, 2, top, top_down=True)
    # Rebuild with different rows: top red, bottom green.
    data = bytearray(image)
    stride = ((4 * 3 + 3) // 4) * 4
    data[54 : 54 + stride] = bytes(top) * 4 + b"\x00" * (stride - 12)
    data[54 + stride : 54 + 2 * stride] = bytes(bottom) * 4 + b"\x00" * (stride - 12)
    frame = parse_bmp_frame(bytes(data))
    assert frame.top_down is True
    assert frame.logical_row(0)[:3] == bytes(top)
    assert frame.logical_row(1)[:3] == bytes(bottom)


def test_identical_frames_are_unchanged() -> None:
    image = encode_bmp(32, 20)
    diff = compare_bmp_frames(image, image)
    assert diff.changed is False
    assert diff.changed_pixels == 0
    assert diff.change_ratio == 0.0
    assert diff.summary == "NOTHING CHANGED"
    assert diff.regions == ()


def test_changed_pixel_is_reported() -> None:
    before = encode_bmp(16, 10)
    after = _set_pixel(before, 16, 10, 3, 4, (1, 2, 3))
    diff = compare_bmp_frames(before, after)
    assert diff.changed is True
    assert diff.changed_pixels == 1
    assert diff.change_ratio == pytest.approx(1 / 160)
    assert diff.regions
    assert "SCREEN CHANGED" in diff.summary


def test_pixel_threshold_ignores_small_noise() -> None:
    before = encode_bmp(16, 10)
    after = _set_pixel(before, 16, 10, 3, 4, (1, 2, 3))
    ignored = compare_bmp_frames(before, after, pixel_threshold=255)
    assert ignored.changed is False
    detected = compare_bmp_frames(before, after, pixel_threshold=0)
    assert detected.changed is True


def test_dimension_change_is_reported() -> None:
    diff = compare_bmp_frames(encode_bmp(16, 10), encode_bmp(16, 12))
    assert diff.changed is True
    assert diff.dimensions_changed is True
    assert diff.change_ratio == 1.0
    assert "dimensions" in diff.summary


def test_region_cap() -> None:
    before = _solid_bmp(64, 48, (0, 0, 0))
    after = bytearray(before)
    # Change every cell in the 8x6 grid.
    for y in range(0, 48, 8):
        for x in range(0, 64, 8):
            after = bytearray(
                _set_pixel(bytes(after), 64, 48, x, y, (255, 255, 255))
            )
    diff = compare_bmp_frames(before, bytes(after), max_regions=3)
    assert diff.changed is True
    assert len(diff.regions) == 3
    assert diff.regions[0].changed_pixels >= diff.regions[-1].changed_pixels


def test_rejects_invalid_inputs() -> None:
    image = encode_bmp(8, 4)
    with pytest.raises(VisionValidationError):
        parse_bmp_frame(b"not a bmp")
    with pytest.raises(VisionValidationError):
        parse_bmp_frame(image[:40])
    with pytest.raises(VisionValidationError):
        compare_bmp_frames(image, image, pixel_threshold=-1)
    with pytest.raises(VisionValidationError):
        compare_bmp_frames(image, image, max_regions=99)
