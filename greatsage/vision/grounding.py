"""OCR-free grounding stub + local VLM grounding (Phase 8 + Eyes roadmap).

Two grounds, both local-first and zero-cost:

- `GroundingStub` — deterministic layout grid, explicitly marked
  `[stub-no-ocr]`; no OCR engine, no model inference, no network.
- `OllamaGrounding` — real grounding through a local Ollama vision model
  (default `moondream`): the capture is converted to PNG (stdlib) and sent
  with a bounded JSON prompt; returned regions are validated and clamped,
  and a malformed reply degrades to a summary-only description (never
  fabricated coordinates).

`build_grounding()` picks the real ground when the model is locally
present, otherwise the stub; `GREATSAGE_VISION_GROUNDING`
(`auto` | `stub` | `ollama`) overrides — tests pin `stub`.
"""

from __future__ import annotations

import base64
import json
import logging
import math
import os
import struct
import zlib

from greatsage.intelligence import transport
from greatsage.vision.limits import VisionLimits, default_limits
from greatsage.vision.models import VisionBackend, VisionDescription, VisionRegion

log = logging.getLogger("greatsage.vision.grounding")

STUB_MARKER = "[stub-no-ocr]"
TRUNCATION_SUFFIX = "…[truncated]"

GROUNDING_MODEL_DEFAULT = "moondream"
GROUNDING_URL_DEFAULT = "http://127.0.0.1:11434"


def _grid_dims(count: int) -> tuple[int, int]:
    """Near-square grid holding at least `count` cells."""
    cols = max(1, math.ceil(math.sqrt(count)))
    rows = max(1, math.ceil(count / cols))
    return (rows, cols)


class GroundingStub:
    """Deterministic layout grounding; honest about being a stub."""

    name = "stub"

    def __init__(self, limits: VisionLimits | None = None) -> None:
        self._limits = limits or default_limits()

    def describe(
        self,
        capture_id: str,
        width: int,
        height: int,
        *,
        image: bytes | None = None,
        max_regions: int | None = None,
        max_chars: int | None = None,
    ) -> VisionDescription:
        """Build a bounded stub description for a capture (image ignored)."""
        _ = image  # interface parity with real grounds; the stub needs no pixels
        region_cap = min(
            max_regions if max_regions is not None else self._limits.max_regions,
            self._limits.max_regions,
        )
        char_cap = min(
            max_chars if max_chars is not None else self._limits.max_description_chars,
            self._limits.max_description_chars,
        )
        region_cap = max(1, region_cap)
        rows, cols = _grid_dims(region_cap)
        regions: list[VisionRegion] = []
        index = 0
        for row in range(rows):
            for col in range(cols):
                if index >= region_cap:
                    break
                regions.append(
                    VisionRegion(
                        label=f"stub-cell-r{row}c{col}",
                        x=round(col / cols, 4),
                        y=round(row / rows, 4),
                        width=round(1.0 / cols, 4),
                        height=round(1.0 / rows, 4),
                        confidence=0.0,
                    )
                )
                index += 1
        summary = (
            f"{STUB_MARKER} {width}x{height} bmp frame split into "
            f"{len(regions)} layout cells ({rows}x{cols} grid); "
            "no text was recognized and no model inference ran."
        )
        truncated = False
        if len(summary) > char_cap:
            summary = summary[: max(0, char_cap - len(TRUNCATION_SUFFIX))] + TRUNCATION_SUFFIX
            truncated = True
        description = VisionDescription(
            capture_id=capture_id,
            backend=VisionBackend.STUB,
            summary=summary,
            regions=tuple(regions),
            truncated=truncated,
        )
        description.validate()
        log.debug(
            "grounded capture",
            extra={"component": "vision", "capture_id": capture_id},
        )
        return description


# --- real VLM grounding (local Ollama) ---------------------------------

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _png_chunk(tag: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + tag
        + payload
        + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
    )


def bmp_to_png(image: bytes) -> bytes:
    """Convert a 24-bit BMP (our capture format) to PNG using stdlib only.

    Ollama's image pipeline takes PNG/JPEG; BMP is not reliably decodable
    server-side, so captures are re-encoded here without any codec
    dependency. Handles bottom-up (positive height) and top-down (negative
    height) DIBs.
    """
    if len(image) < 54 or image[0:2] != b"BM":
        raise ValueError("not a BMP image (bad magic)")
    width = struct.unpack("<i", image[18:22])[0]
    signed_height = struct.unpack("<i", image[22:26])[0]
    bit_count = struct.unpack("<H", image[28:30])[0]
    if width <= 0 or signed_height == 0:
        raise ValueError(f"invalid BMP dimensions: {width}x{signed_height}")
    if bit_count != 24:
        raise ValueError(f"only 24-bit BMP is supported, got {bit_count}")
    height = abs(signed_height)
    stride = ((width * 3 + 3) // 4) * 4
    pixels = image[54:]
    if len(pixels) < stride * height:
        raise ValueError("truncated BMP pixel data")
    scanlines = bytearray()
    for index in range(height):
        # PNG rows are top-down; BMP file rows are bottom-up when positive.
        y = index if signed_height < 0 else height - 1 - index
        row = pixels[y * stride : y * stride + width * 3]
        scanlines.append(0)  # filter: none
        for x in range(width):
            b, g, r = row[x * 3 : x * 3 + 3]
            scanlines.extend((r, g, b))
    header = struct.pack(
        ">IIBBBBB", width, height, 8, 2, 0, 0, 0  # 8-bit, truecolor RGB
    )
    return (
        _PNG_SIGNATURE
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", zlib.compress(bytes(scanlines), 6))
        + _png_chunk(b"IEND", b"")
    )


_GROUNDING_PROMPT = (
    "You are a screen-vision assistant. Look at the screenshot and answer "
    "strictly in JSON, no other text: "
    '{{"summary": "<one or two sentences describing what is on screen>", '
    '"regions": [{{"label": "<short label>", "x": <0..1>, "y": <0..1>, '
    '"width": <0..1>, "height": <0..1>}}]}} '
    "List at most {region_cap} of the most salient UI regions with "
    "normalized coordinates (0,0 is the top-left corner). "
    "If unsure, use fewer regions."
)


def _extract_json(text: str) -> dict | list | None:
    """Best-effort: the first JSON value (object or array) in a model reply."""
    stripped = text.strip()
    if stripped[:1] in "{[":
        try:
            parsed = json.loads(stripped)
            if isinstance(parsed, (dict, list)):
                return parsed
        except ValueError:
            pass
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        while start != -1:
            depth = 0
            for index in range(start, len(text)):
                char = text[index]
                if char == opener:
                    depth += 1
                elif char == closer:
                    depth -= 1
                    if depth == 0:
                        try:
                            parsed = json.loads(text[start : index + 1])
                        except ValueError:
                            break
                        if isinstance(parsed, (dict, list)):
                            return parsed
            start = text.find(opener, start + 1)
    return None


def _scan_balanced_objects(text: str) -> list[dict]:
    """Every balanced JSON object in the reply, in order.

    Salvages truncated or interleaved replies (e.g. a region array cut off
    mid-way by the output-token cap): each complete object is a candidate.
    """
    objects: list[dict] = []
    start = text.find("{")
    while start != -1:
        depth = 0
        end = -1
        for index in range(start, len(text)):
            char = text[index]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    end = index
                    break
        if end == -1:
            break
        try:
            value = json.loads(text[start : end + 1])
        except ValueError:
            start = text.find("{", start + 1)
            continue
        if isinstance(value, dict):
            objects.append(value)
        start = text.find("{", end + 1)
    return objects


def _coerce_unit(value: object) -> float | None:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not 0.0 <= number <= 1.0:
        return None
    return round(number, 4)


def _region_box(item: dict) -> tuple[float, float, float, float] | None:
    """Extract (x, y, width, height) from one region item (0..1, validated).

    Accepts both the flat schema and the box shapes small VLMs actually
    emit — `regions`/`bbox`/`box` as [x1, y1, x2, y2] top-left/bottom-right
    — and derives width/height. Anything incomplete or out of range is
    dropped, never fabricated (coordinates are only produced when the model
    supplied valid numbers).
    """
    x = _coerce_unit(item.get("x"))
    y = _coerce_unit(item.get("y"))
    width = _coerce_unit(item.get("width"))
    height = _coerce_unit(item.get("height"))
    if x is not None and y is not None and width is not None and height is not None:
        return (x, y, width, height)
    for key in ("regions", "bbox", "box"):
        candidate = item.get(key)
        if (
            isinstance(candidate, list)
            and len(candidate) == 4
            and all(isinstance(entry, (int, float)) for entry in candidate)
        ):
            x1, y1, x2, y2 = (_coerce_unit(entry) for entry in candidate)
            if (
                x1 is not None
                and y1 is not None
                and x2 is not None
                and y2 is not None
                and x2 > x1
                and y2 > y1
            ):
                return (
                    x1 if x is None else x,
                    y1 if y is None else y,
                    round(x2 - x1, 4),
                    round(y2 - y1, 4),
                )
    return None


class OllamaGrounding:
    """Real grounding through a local Ollama vision model (default moondream).

    Bounded: one bounded chat call per describe (timeout-capped), region
    count and description length clamped to the vision limits, coordinates
    validated and dropped when out of range. A malformed or empty model
    reply degrades to a summary-only description — coordinates are never
    fabricated.
    """

    name = "ollama"

    def __init__(
        self,
        *,
        base_url: str = GROUNDING_URL_DEFAULT,
        model: str = GROUNDING_MODEL_DEFAULT,
        timeout_seconds: float = 60.0,
        limits: VisionLimits | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/") or GROUNDING_URL_DEFAULT
        self._model = model.strip() or GROUNDING_MODEL_DEFAULT
        self._timeout = timeout_seconds if timeout_seconds > 0 else 60.0
        self._limits = limits or default_limits()

    @property
    def model(self) -> str:
        return self._model

    def is_available(self) -> bool:
        """True when the grounding model is present locally (best-effort)."""
        try:
            data = transport.request_json(
                f"{self._base_url}/api/tags", timeout=3.0, default_timeout=3.0
            )
        except Exception:
            return False
        if not isinstance(data, dict):
            return False
        names = {
            str(item.get("name", "")).split(":")[0]
            for item in data.get("models", [])
            if isinstance(item, dict)
        }
        return self._model.split(":")[0] in names

    def describe(
        self,
        capture_id: str,
        width: int,
        height: int,
        *,
        image: bytes | None = None,
        max_regions: int | None = None,
        max_chars: int | None = None,
    ) -> VisionDescription:
        if image is None:
            raise ValueError("ollama grounding requires the capture image bytes")
        region_cap = max(
            1,
            min(
                max_regions if max_regions is not None else self._limits.max_regions,
                self._limits.max_regions,
            ),
        )
        char_cap = min(
            max_chars if max_chars is not None else self._limits.max_description_chars,
            self._limits.max_description_chars,
        )
        png = bmp_to_png(image)
        # Tiny VLMs free-run into repetition without an output-token bound:
        # num_predict caps generation (~5 regions + summary), temperature 0
        # keeps it deterministic.
        body = {
            "model": self._model,
            "stream": False,
            "options": {"temperature": 0, "num_predict": 500},
            "messages": [
                {
                    "role": "user",
                    "content": _GROUNDING_PROMPT.format(region_cap=min(region_cap, 5)),
                    "images": [base64.b64encode(png).decode("ascii")],
                }
            ],
        }
        data = transport.request_json(
            f"{self._base_url}/api/chat",
            method="POST",
            body=body,
            timeout=self._timeout,
            default_timeout=self._timeout,
        )
        raw_text = ""
        if isinstance(data, dict):
            message = data.get("message")
            if isinstance(message, dict):
                raw_text = str(message.get("content") or "")
        parsed = _extract_json(raw_text)
        summary_text = ""
        regions: list[VisionRegion] = []
        raw_regions: object = None
        if isinstance(parsed, dict):
            summary_text = str(parsed.get("summary") or "").strip()
            raw_regions = parsed.get("regions")
            if not isinstance(raw_regions, list) and not summary_text:
                # Bare region object(s); a truncated array still leaves every
                # complete object salvageable.
                raw_regions = _scan_balanced_objects(raw_text) or [parsed]
        elif isinstance(parsed, list):
            # Top-level region array (moondream's common shape, no wrapper).
            raw_regions = parsed
        if isinstance(raw_regions, list):
            for item in raw_regions[:region_cap]:
                if not isinstance(item, dict):
                    continue
                box = _region_box(item)
                if box is None:
                    continue
                confidence = _coerce_unit(item.get("confidence"))
                label = str(item.get("label") or "region")[:60].strip() or "region"
                region = VisionRegion(
                    label=label,
                    x=box[0],
                    y=box[1],
                    width=box[2],
                    height=box[3],
                    confidence=confidence if confidence is not None else 0.5,
                )
                region.validate()
                regions.append(region)
        if not summary_text:
            summary_text = raw_text.strip() or "(model returned no description)"
        summary = f"[ollama:{self._model}] {summary_text}"
        truncated = False
        if len(summary) > char_cap:
            summary = summary[: max(0, char_cap - len(TRUNCATION_SUFFIX))] + TRUNCATION_SUFFIX
            truncated = True
        description = VisionDescription(
            capture_id=capture_id,
            backend=VisionBackend.OLLAMA,
            summary=summary,
            regions=tuple(regions),
            truncated=truncated,
        )
        description.validate()
        log.debug(
            "grounded capture (ollama)",
            extra={"component": "vision", "capture_id": capture_id, "regions": len(regions)},
        )
        return description


def build_grounding(
    limits: VisionLimits | None = None,
    *,
    base_url: str = "",
) -> GroundingStub | OllamaGrounding:
    """Select the ground: real local VLM when present, stub otherwise.

    `GREATSAGE_VISION_GROUNDING` (`auto` | `stub` | `ollama`) and
    `GREATSAGE_VISION_GROUNDING_MODEL` override the automatic choice;
    tests pin `stub` so no test runs model inference.
    """
    preference = os.environ.get("GREATSAGE_VISION_GROUNDING", "auto").strip().lower()
    model = os.environ.get("GREATSAGE_VISION_GROUNDING_MODEL", "").strip()
    resolved = limits or default_limits()
    if preference == "stub":
        return GroundingStub(limits=resolved)
    grounding = OllamaGrounding(
        base_url=base_url or GROUNDING_URL_DEFAULT,
        model=model or GROUNDING_MODEL_DEFAULT,
        limits=resolved,
    )
    if preference == "ollama":
        return grounding
    if grounding.is_available():
        return grounding
    return GroundingStub(limits=resolved)
