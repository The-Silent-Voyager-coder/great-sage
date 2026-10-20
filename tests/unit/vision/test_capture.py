"""Capture tests: deterministic BMP, bounds, timeout plumbing, backend selection."""

from __future__ import annotations

import pytest

from greatsage.exceptions import VisionValidationError
from greatsage.vision.capture import (
    CaptureManager,
    GdiCaptureBackend,
    StubCaptureBackend,
    build_capture_backend,
    encode_bmp,
    parse_bmp_dimensions,
)
from greatsage.vision.limits import VisionLimits, default_limits


def test_stub_always_available() -> None:
    assert StubCaptureBackend().is_available()


def test_encode_bmp_magic_and_dims() -> None:
    image = encode_bmp(32, 20)
    assert image[0:2] == b"BM"
    assert parse_bmp_dimensions(image) == (32, 20)


def test_encode_bmp_deterministic() -> None:
    assert encode_bmp(16, 10, seed=3) == encode_bmp(16, 10, seed=3)
    assert encode_bmp(16, 10, seed=3) != encode_bmp(16, 10, seed=4)


def test_parse_rejects_non_bmp() -> None:
    with pytest.raises(VisionValidationError):
        parse_bmp_dimensions(b"not an image at all..............................")


def test_manager_enforces_dim_caps() -> None:
    manager = CaptureManager(limits=default_limits())
    with pytest.raises(VisionValidationError):
        manager.capture(100000, 10)
    with pytest.raises(VisionValidationError):
        manager.capture(0, 10)
    with pytest.raises(VisionValidationError):
        manager.capture(-4, 10)


def test_manager_enforces_byte_cap() -> None:
    tiny = VisionLimits(max_image_bytes=100)
    manager = CaptureManager(limits=tiny)
    with pytest.raises(VisionValidationError):
        manager.capture(64, 64)


def test_manager_returns_metadata() -> None:
    manager = CaptureManager(limits=default_limits())
    image, meta = manager.capture(64, 40)
    assert meta["width"] == 64
    assert meta["height"] == 40
    assert meta["size_bytes"] == len(image)
    assert meta["backend"] == "stub"


def test_manager_rejects_unavailable_backend() -> None:
    class Dead(StubCaptureBackend):
        name = "dead"

        def is_available(self) -> bool:
            return False

    manager = CaptureManager(limits=default_limits(), backend=Dead())
    with pytest.raises(VisionValidationError):
        manager.capture(32, 20)


# --- backend selection --------------------------------------------------


def test_build_capture_backend_env_stub(monkeypatch) -> None:
    monkeypatch.setenv("GREATSAGE_VISION_BACKEND", "stub")
    assert build_capture_backend().name == "stub"


def test_build_capture_backend_env_gdi(monkeypatch) -> None:
    monkeypatch.setenv("GREATSAGE_VISION_BACKEND", "gdi")
    assert build_capture_backend().name == "gdi"


@pytest.mark.skipif(
    __import__("sys").platform != "win32", reason="GDI capture is Windows-only"
)
def test_build_capture_backend_auto_prefers_gdi_on_windows(monkeypatch) -> None:
    monkeypatch.setenv("GREATSAGE_VISION_BACKEND", "auto")
    assert build_capture_backend().name == "gdi"


def test_gdi_unavailable_off_windows(monkeypatch) -> None:
    import sys

    monkeypatch.setattr(sys, "platform", "linux")
    assert not GdiCaptureBackend().is_available()
    # auto selection therefore falls back to the stub off-Windows
    monkeypatch.setenv("GREATSAGE_VISION_BACKEND", "auto")
    assert build_capture_backend().name == "stub"


# --- GDI scaling (faked screen source, no real capture) ----------------


def _fake_source(w: int, h: int, rows: list[tuple[int, int, int]]) -> tuple[int, int, bytes]:
    """Top-down 24-bit BMP in the same shape gui_tools writes (negative DIB)."""
    import struct

    stride = ((w * 3 + 3) // 4) * 4
    pixels = b"".join(bytes(color) * w + b"\x00" * (stride - w * 3) for color in rows)
    size = len(pixels)
    header = struct.pack("<2sIHHI", b"BM", 54 + size, 0, 0, 54)
    dib = struct.pack("<IiiHHIIiiII", 40, w, -h, 1, 24, 0, size, 0, 0, 0, 0)
    return w, h, header + dib + pixels


def _row_colors(image: bytes, width: int, height: int) -> list[bytes]:
    """BGR pixels grouped per row, in file (bottom-up) order."""
    stride = ((width * 3 + 3) // 4) * 4
    pixels = image[54:]
    return [pixels[y * stride : y * stride + width * 3] for y in range(height)]


def test_gdi_scales_and_writes_bottom_up(monkeypatch) -> None:
    red, green, blue, white = (0, 0, 255), (0, 255, 0), (255, 0, 0), (255, 255, 255)
    source_rows = [red, green, blue, white]
    monkeypatch.setattr(
        "greatsage.tools.gui_tools.capture_primary_monitor_bmp",
        lambda: _fake_source(8, 4, source_rows),
    )
    image = GdiCaptureBackend().capture(4, 2)
    assert parse_bmp_dimensions(image) == (4, 2)
    rows = _row_colors(image, 4, 2)
    # bottom-up file order: first file row is the bottom target row.
    # y=1 → source row 1*4//2 = 2 (blue); y=0 → source row 0 (red).
    assert rows[0] == bytes(blue) * 4
    assert rows[1] == bytes(red) * 4


def test_gdi_nearest_neighbor_columns(monkeypatch) -> None:
    # 2x1 source with two distinct pixels → 4x1 keeps column blocks.
    row = (10, 20, 30) + (40, 50, 60)
    monkeypatch.setattr(
        "greatsage.tools.gui_tools.capture_primary_monitor_bmp",
        lambda: _fake_source(2, 1, [row]),
    )
    image = GdiCaptureBackend().capture(4, 1)
    pixels = _row_colors(image, 4, 1)[0]
    assert pixels == bytes((10, 20, 30)) * 2 + bytes((40, 50, 60)) * 2


def test_gdi_capture_failure_is_validation_error(monkeypatch) -> None:
    def boom() -> tuple[int, int, bytes]:
        raise OSError("GetDC failed")

    monkeypatch.setattr("greatsage.tools.gui_tools.capture_primary_monitor_bmp", boom)
    with pytest.raises(VisionValidationError, match="screen capture failed"):
        GdiCaptureBackend().capture(8, 8)
