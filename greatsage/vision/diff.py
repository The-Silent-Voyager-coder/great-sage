"""BMP frame comparison for verified desktop actions.

Pure stdlib and local-first: callers receive aggregate change metadata
(ratio, changed-pixel count, bounded regions) and never image bytes. The
helper is deliberately independent of the tool layer so both the vision
toolset and the GUI tools can share it without an import cycle.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from greatsage.exceptions import VisionValidationError

BMP_HEADER_SIZE = 54
_SUPPORTED_BITS = (24, 32)
_GRID_COLUMNS = 8
_GRID_ROWS = 6


@dataclass(frozen=True)
class BmpFrame:
    """One decoded BMP frame: dimensions plus raw row-padded pixels."""

    width: int
    height: int
    top_down: bool
    stride: int
    bytes_per_pixel: int
    pixels: bytes

    def logical_row(self, y: int) -> bytes:
        """Pixel bytes for top-down logical row `y` (padding stripped)."""
        stored_y = y if self.top_down else self.height - 1 - y
        start = stored_y * self.stride
        end = start + self.width * self.bytes_per_pixel
        return self.pixels[start:end]


@dataclass(frozen=True)
class DiffRegion:
    """One changed cell in pixel coordinates (top-down origin)."""

    x: int
    y: int
    width: int
    height: int
    changed_pixels: int

    def to_dict(self) -> dict[str, int]:
        return {
            "x": self.x,
            "y": self.y,
            "width": self.width,
            "height": self.height,
            "changed_pixels": self.changed_pixels,
        }


@dataclass(frozen=True)
class FrameDiff:
    """Result of comparing two BMP frames."""

    changed: bool
    change_ratio: float
    changed_pixels: int
    total_pixels: int
    width: int
    height: int
    dimensions_changed: bool
    regions: tuple[DiffRegion, ...]

    @property
    def summary(self) -> str:
        if self.dimensions_changed:
            return "SCREEN CHANGED: frame dimensions differ"
        if not self.changed:
            return "NOTHING CHANGED"
        return f"SCREEN CHANGED: {self.change_ratio:.4%} of pixels differ"


def parse_bmp_frame(image: bytes) -> BmpFrame:
    """Decode a 24/32-bit uncompressed BMP into a comparison-ready frame.

    Accepts both top-down (negative DIB height, the GDI capture format) and
    bottom-up (positive height, the stub capture format) row order.
    """
    if len(image) < BMP_HEADER_SIZE or image[0:2] != b"BM":
        raise VisionValidationError("not a BMP image (bad magic)")
    pixel_offset = struct.unpack("<I", image[10:14])[0]
    width = struct.unpack("<i", image[18:22])[0]
    signed_height = struct.unpack("<i", image[22:26])[0]
    bit_count = struct.unpack("<H", image[28:30])[0]
    compression = struct.unpack("<I", image[30:34])[0]
    if width <= 0 or signed_height == 0:
        raise VisionValidationError(f"invalid BMP dimensions: {width}x{signed_height}")
    if bit_count not in _SUPPORTED_BITS:
        raise VisionValidationError(
            f"only 24-bit and 32-bit BMP are supported, got {bit_count}"
        )
    if compression != 0:
        raise VisionValidationError(
            f"compressed BMP is not supported (compression={compression})"
        )
    height = abs(signed_height)
    bytes_per_pixel = bit_count // 8
    stride = ((width * bytes_per_pixel + 3) // 4) * 4
    needed = pixel_offset + stride * height
    if pixel_offset < BMP_HEADER_SIZE or needed > len(image):
        raise VisionValidationError("truncated BMP pixel data")
    return BmpFrame(
        width=width,
        height=height,
        top_down=signed_height < 0,
        stride=stride,
        bytes_per_pixel=bytes_per_pixel,
        pixels=image[pixel_offset:needed],
    )


def compare_bmp_frames(
    before: bytes,
    after: bytes,
    *,
    pixel_threshold: int = 8,
    max_regions: int = 6,
) -> FrameDiff:
    """Compare two BMP frames and describe what changed.

    `pixel_threshold` is the per-channel tolerance (0..255) used to ignore
    anti-aliasing/palette noise. A cell is reported as a changed region when
    at least one pixel inside it differs; regions are bounded to
    `max_regions` and ordered by changed-pixel count.
    """
    if not 0 <= pixel_threshold <= 255:
        raise VisionValidationError("pixel_threshold must be in 0..255")
    if not 0 <= max_regions <= 32:
        raise VisionValidationError("max_regions must be in 0..32")
    first = parse_bmp_frame(before)
    second = parse_bmp_frame(after)
    if first.bytes_per_pixel != second.bytes_per_pixel:
        raise VisionValidationError("frames use different pixel formats")
    if (first.width, first.height) != (second.width, second.height):
        total = second.width * second.height
        return FrameDiff(
            changed=True,
            change_ratio=1.0,
            changed_pixels=total,
            total_pixels=total,
            width=second.width,
            height=second.height,
            dimensions_changed=True,
            regions=(),
        )

    width, height = first.width, first.height
    channels = min(3, first.bytes_per_pixel)  # ignore alpha; color is what changed
    changed_pixels = 0
    cells: dict[tuple[int, int], int] = {}
    for y in range(height):
        row_a = first.logical_row(y)
        row_b = second.logical_row(y)
        if row_a == row_b:
            continue
        for x in range(width):
            offset = x * first.bytes_per_pixel
            differs = False
            for channel in range(channels):
                if abs(row_a[offset + channel] - row_b[offset + channel]) > pixel_threshold:
                    differs = True
                    break
            if not differs:
                continue
            changed_pixels += 1
            cell = (x * _GRID_COLUMNS // width, y * _GRID_ROWS // height)
            cells[cell] = cells.get(cell, 0) + 1

    total_pixels = width * height
    regions: list[DiffRegion] = []
    for (cell_x, cell_y), count in sorted(
        cells.items(), key=lambda item: item[1], reverse=True
    )[:max_regions]:
        x0 = cell_x * width // _GRID_COLUMNS
        x1 = (cell_x + 1) * width // _GRID_COLUMNS
        y0 = cell_y * height // _GRID_ROWS
        y1 = (cell_y + 1) * height // _GRID_ROWS
        regions.append(
            DiffRegion(
                x=x0,
                y=y0,
                width=max(1, x1 - x0),
                height=max(1, y1 - y0),
                changed_pixels=count,
            )
        )
    return FrameDiff(
        changed=changed_pixels > 0,
        change_ratio=changed_pixels / total_pixels if total_pixels else 0.0,
        changed_pixels=changed_pixels,
        total_pixels=total_pixels,
        width=width,
        height=height,
        dimensions_changed=False,
        regions=tuple(regions),
    )
