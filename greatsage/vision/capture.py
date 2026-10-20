"""Screen-capture abstraction (Phase 8 + Eyes roadmap).

Stdlib only, zero-cost, local-first. Backends:

- `gdi` — real primary-monitor capture on Windows via ctypes (the same
  proven path as `gui.screenshot`), scaled (nearest-neighbor) to the
  requested dimensions and written as a top-down BMP.
- `stub` — deterministic synthetic BMP generator (no display server, no
  screenshot utility, no paid API); the honest fallback when real capture
  is unavailable or explicitly disabled.

Backend selection: `build_capture_backend()` prefers `gdi` on Windows and
falls back to `stub`; the `GREATSAGE_VISION_BACKEND` environment variable
(`auto` | `stub` | `gdi`, default `auto`) overrides it — tests pin `stub`
for hermetic runs.

Output is 24-bit BMP (universally readable, no codec dependency) with
bounded dimensions enforced before any byte is generated.
"""

from __future__ import annotations

import logging
import os
import struct
import sys
import time
from abc import ABC, abstractmethod
from typing import Any

from greatsage.exceptions import VisionTimeoutError, VisionValidationError
from greatsage.vision.limits import VisionLimits, default_limits

log = logging.getLogger("greatsage.vision.capture")

BMP_HEADER_SIZE = 54
BMP_BITS_PER_PIXEL = 24


def _pixel(x: int, y: int, width: int, height: int, seed: int) -> tuple[int, int, int]:
    """Deterministic gradient pixel (B, G, R order for BMP)."""
    red = (x * 255 // max(width - 1, 1) + seed) % 256
    green = (y * 255 // max(height - 1, 1) + seed * 2) % 256
    blue = ((x + y) * 255 // max(width + height - 2, 1) + seed * 3) % 256
    return (blue, green, red)


def encode_bmp(width: int, height: int, seed: int = 0) -> bytes:
    """Encode a deterministic 24-bit BMP frame (bottom-up, padded rows)."""
    row_stride = ((width * 3 + 3) // 4) * 4
    padding = row_stride - width * 3
    pixels = bytearray()
    for y in range(height - 1, -1, -1):
        for x in range(width):
            pixels.extend(_pixel(x, y, width, height, seed))
        pixels.extend(b"\x00" * padding)
    image_size = len(pixels)
    file_size = BMP_HEADER_SIZE + image_size
    header = struct.pack(
        "<2sIHHI",
        b"BM",
        file_size,
        0,
        0,
        BMP_HEADER_SIZE,
    )
    dib = struct.pack(
        "<IIIHHIIIIII",
        40,
        width,
        height,
        1,
        BMP_BITS_PER_PIXEL,
        0,
        image_size,
        2835,
        2835,
        0,
        0,
    )
    return header + dib + bytes(pixels)


def parse_bmp_dimensions(image: bytes) -> tuple[int, int]:
    """Read (width, height) from a BMP header; reject non-BMP input."""
    if len(image) < BMP_HEADER_SIZE or image[0:2] != b"BM":
        raise VisionValidationError("not a BMP image (bad magic)")
    width = struct.unpack("<i", image[18:22])[0]
    height = struct.unpack("<i", image[22:26])[0]
    if width <= 0 or height <= 0:
        raise VisionValidationError(f"invalid BMP dimensions: {width}x{height}")
    return (width, height)


class CaptureBackend(ABC):
    """Pluggable frame source; callers never touch OS capture directly."""

    name: str = "abstract"

    @abstractmethod
    def is_available(self) -> bool: ...

    @abstractmethod
    def capture(self, width: int, height: int) -> bytes:
        """Return one BMP frame of exactly width x height."""
        ...


class StubCaptureBackend(CaptureBackend):
    """Deterministic synthetic frames; always available, zero-cost."""

    name = "stub"

    def __init__(self, seed: int = 0) -> None:
        self._seed = seed

    def is_available(self) -> bool:
        return True

    def capture(self, width: int, height: int) -> bytes:
        return encode_bmp(width, height, seed=self._seed)


def _encode_scaled_bmp(
    source_top_down: bytes,
    source_width: int,
    source_height: int,
    width: int,
    height: int,
) -> bytes:
    """Nearest-neighbor scale top-down BGR rows to a bottom-up BMP frame."""
    source_stride = ((source_width * 3 + 3) // 4) * 4
    target_stride = ((width * 3 + 3) // 4) * 4
    padding = target_stride - width * 3
    columns = [x * source_width // width * 3 for x in range(width)]
    rows = bytearray()
    for y in range(height - 1, -1, -1):  # bottom-up file order
        source_row = y * source_height // height * source_stride
        row = source_top_down
        samples = [row[source_row + offset : source_row + offset + 3] for offset in columns]
        rows.extend(b"".join(samples))
        rows.extend(b"\x00" * padding)
    image_size = len(rows)
    file_size = BMP_HEADER_SIZE + image_size
    header = struct.pack("<2sIHHI", b"BM", file_size, 0, 0, BMP_HEADER_SIZE)
    dib = struct.pack(
        "<IIIHHIIIIII", 40, width, height, 1, BMP_BITS_PER_PIXEL, 0, image_size,
        2835, 2835, 0, 0,
    )
    return header + dib + bytes(rows)


class GdiCaptureBackend(CaptureBackend):
    """Real primary-monitor capture via ctypes GDI (Windows only).

    Frames are captured at screen resolution and scaled (nearest-neighbor)
    to the requested dimensions; the returned frame is always the exact
    requested size, in the same BMP format as the stub backend.
    """

    name = "gdi"

    def is_available(self) -> bool:
        if sys.platform != "win32":
            return False
        try:
            import ctypes

            return ctypes.windll.user32.GetSystemMetrics(0) > 0
        except Exception:
            return False

    def capture(self, width: int, height: int) -> bytes:
        from greatsage.tools.gui_tools import capture_primary_monitor_bmp

        try:
            source_width, source_height, bmp = capture_primary_monitor_bmp()
        except OSError as exc:
            raise VisionValidationError(f"screen capture failed: {exc}") from exc
        if source_width <= 0 or source_height <= 0:
            raise VisionValidationError("screen capture returned empty dimensions")
        # Our BMP writer uses a 54-byte offset (file header + 40-byte DIB).
        pixels = bmp[BMP_HEADER_SIZE:]
        return _encode_scaled_bmp(pixels, source_width, source_height, width, height)


def build_capture_backend() -> CaptureBackend:
    """Select the capture backend: real GDI when possible, stub otherwise.

    `GREATSAGE_VISION_BACKEND` (`auto` | `stub` | `gdi`) overrides the
    automatic choice; tests pin `stub` so no test ever captures a real
    screen.
    """
    preference = os.environ.get("GREATSAGE_VISION_BACKEND", "auto").strip().lower()
    if preference == "stub":
        return StubCaptureBackend()
    if preference == "gdi":
        return GdiCaptureBackend()
    gdi = GdiCaptureBackend()
    if gdi.is_available():
        return gdi
    return StubCaptureBackend()


def build_capture_manager(limits: VisionLimits | None = None) -> CaptureManager:
    """A CaptureManager wired with the selected backend (real when possible)."""
    return CaptureManager(limits=limits, backend=build_capture_backend())


class CaptureManager:
    """Bounded capture: dimension caps, byte caps, wall-clock timeout."""

    def __init__(
        self,
        limits: VisionLimits | None = None,
        backend: CaptureBackend | None = None,
    ) -> None:
        self._limits = limits or default_limits()
        self._backend = backend or StubCaptureBackend()

    @property
    def backend_name(self) -> str:
        return self._backend.name

    @property
    def backend_available(self) -> bool:
        try:
            return self._backend.is_available()
        except Exception:
            return False

    def capture(self, width: int, height: int) -> tuple[bytes, dict[str, Any]]:
        """Capture one bounded frame; returns (bmp bytes, metadata)."""
        if not isinstance(width, int) or not isinstance(height, int):
            raise VisionValidationError("capture dimensions must be integers")
        if width <= 0 or height <= 0:
            raise VisionValidationError(
                f"capture dimensions must be positive, got {width}x{height}"
            )
        if width > self._limits.max_width or height > self._limits.max_height:
            raise VisionValidationError(
                f"capture {width}x{height} exceeds "
                f"{self._limits.max_width}x{self._limits.max_height}"
            )
        if not self.backend_available:
            raise VisionValidationError(f"capture backend {self._backend.name!r} is not available")
        started = time.perf_counter()
        image = self._backend.capture(width, height)
        elapsed = time.perf_counter() - started
        if elapsed > self._limits.capture_timeout_seconds:
            raise VisionTimeoutError(
                f"capture exceeded {self._limits.capture_timeout_seconds}s (took {elapsed:.2f}s)"
            )
        if len(image) > self._limits.max_image_bytes:
            raise VisionValidationError(
                f"capture is {len(image)} bytes, over the {self._limits.max_image_bytes}-byte limit"
            )
        meta = {
            "backend": self._backend.name,
            "width": width,
            "height": height,
            "format": "bmp",
            "size_bytes": len(image),
            "duration_ms": round(elapsed * 1000, 3),
        }
        log.debug(
            "captured frame",
            extra={"component": "vision", "width": width, "height": height},
        )
        return (image, meta)
