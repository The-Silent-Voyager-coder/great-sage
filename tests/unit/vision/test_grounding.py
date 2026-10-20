"""Grounding tests: stub honesty + real Ollama VLM path (fake HTTP server)."""

from __future__ import annotations

import struct
import threading

import pytest

from greatsage.vision.capture import encode_bmp
from greatsage.vision.grounding import (
    STUB_MARKER,
    GroundingStub,
    OllamaGrounding,
    bmp_to_png,
    build_grounding,
)
from greatsage.vision.limits import VisionLimits, default_limits
from greatsage.vision.models import VisionBackend, new_capture_id
from tests.unit.intelligence.conftest import FakeHttpServer


def test_stub_summary_marked() -> None:
    desc = GroundingStub(limits=default_limits()).describe(new_capture_id(), 320, 200)
    assert desc.summary.startswith(STUB_MARKER)
    assert "no text was recognized" in desc.summary
    assert len(desc.regions) == 8


def test_regions_normalized_and_zero_confidence() -> None:
    desc = GroundingStub(limits=default_limits()).describe(
        new_capture_id(), 320, 200, max_regions=4
    )
    assert len(desc.regions) == 4
    for region in desc.regions:
        region.validate()
        assert region.confidence == 0.0
        assert region.label.startswith("stub-cell-")


def test_max_regions_clamped_to_limit() -> None:
    tiny = VisionLimits(max_regions=2)
    desc = GroundingStub(limits=tiny).describe(new_capture_id(), 320, 200, max_regions=99)
    assert len(desc.regions) == 2


def test_truncation_flag() -> None:
    tiny = VisionLimits(max_description_chars=40)
    desc = GroundingStub(limits=tiny).describe(new_capture_id(), 320, 200)
    assert desc.truncated
    assert desc.summary.endswith("…[truncated]")
    assert len(desc.summary) <= 40


# --- BMP -> PNG (stdlib) ------------------------------------------------


def _png_dims(png: bytes) -> tuple[int, int]:
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    return struct.unpack(">II", png[16:24])


def test_bmp_to_png_roundtrip_bottom_up() -> None:
    png = bmp_to_png(encode_bmp(24, 10))
    assert _png_dims(png) == (24, 10)


def test_bmp_to_png_handles_top_down() -> None:
    bmp = bytearray(encode_bmp(8, 4))
    struct.pack_into("<i", bmp, 22, -4)  # negative height: top-down DIB
    png = bmp_to_png(bytes(bmp))
    assert _png_dims(png) == (8, 4)


def test_bmp_to_png_rejects_non_bmp() -> None:
    with pytest.raises(ValueError):
        bmp_to_png(b"nope")


# --- Ollama grounding (fake HTTP server) -------------------------------


@pytest.fixture
def fake_server():
    server = FakeHttpServer()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _tags_with(model: str) -> dict:
    return {"models": [{"name": model}]}


def test_ollama_is_available(fake_server) -> None:
    fake_server.route("GET", "/api/tags", 200, _tags_with("moondream:latest"))
    assert OllamaGrounding(base_url=fake_server.url).is_available()


def test_ollama_unavailable_when_model_absent(fake_server) -> None:
    fake_server.route("GET", "/api/tags", 200, _tags_with("llama3.1:8b"))
    assert not OllamaGrounding(base_url=fake_server.url).is_available()


def test_ollama_unavailable_without_server() -> None:
    assert not OllamaGrounding(base_url="http://127.0.0.1:1", timeout_seconds=1.0).is_available()


def test_ollama_describe_parses_json_regions(fake_server) -> None:
    content = (
        'sure! {"summary": "a desktop with a taskbar", "regions": ['
        '{"label": "taskbar", "x": 0.0, "y": 0.9, "width": 1.0, "height": 0.1, '
        '"confidence": 0.8},'
        '{"label": "out of range", "x": 2.0, "y": 0.0, "width": 1.0, "height": 1.0}'
        "]}"
    )
    fake_server.route("POST", "/api/chat", 200, {"message": {"content": content}})
    grounding = OllamaGrounding(base_url=fake_server.url)
    desc = grounding.describe(
        new_capture_id(), 320, 200, image=encode_bmp(320, 200)
    )
    assert desc.backend is VisionBackend.OLLAMA
    assert desc.summary.startswith("[ollama:moondream]")
    assert "taskbar" in desc.summary
    assert len(desc.regions) == 1  # out-of-range region dropped, never clamped silently
    region = desc.regions[0]
    assert region.label == "taskbar"
    assert region.confidence == 0.8
    # images + strict JSON prompt are sent
    body = fake_server.requests[-1]["body"]
    assert '"images"' in body
    assert "strictly in JSON" in body


def test_ollama_describe_malformed_reply_degrades_honestly(fake_server) -> None:
    fake_server.route(
        "POST", "/api/chat", 200, {"message": {"content": "I see a screen."}}
    )
    grounding = OllamaGrounding(base_url=fake_server.url)
    desc = grounding.describe(new_capture_id(), 64, 40, image=encode_bmp(64, 40))
    assert desc.summary.startswith("[ollama:moondream]")
    assert "I see a screen." in desc.summary
    assert desc.regions == ()  # no fabricated coordinates


def test_ollama_describe_region_cap(fake_server) -> None:
    items = [
        f'{{"label": "r{index}", "x": 0.1, "y": 0.1, '
        '"width": 0.1, "height": 0.1}'
        for index in range(10)
    ]
    content = '{"summary": "many", "regions": [' + ",".join(items) + "]}"
    fake_server.route("POST", "/api/chat", 200, {"message": {"content": content}})
    grounding = OllamaGrounding(base_url=fake_server.url)
    desc = grounding.describe(
        new_capture_id(), 64, 40, image=encode_bmp(64, 40), max_regions=3
    )
    assert len(desc.regions) == 3


def test_ollama_describe_accepts_box_shapes(fake_server) -> None:
    # Small VLMs (moondream) commonly emit `regions`/`bbox` as
    # [x1, y1, x2, y2] top-left/bottom-right rather than width/height.
    content = (
        '{"summary": "screen", "regions": ['
        '{"label": "panel", "x": 0.1, "y": 0.1, "regions": [0.2, 0.2, 0.5, 0.4]},'
        '{"label": "menu", "bbox": [0.0, 0.8, 1.0, 1.0]},'
        '{"label": "broken", "bbox": [0.9, 0.9, 0.1, 0.1]}'
        "]}"
    )
    fake_server.route("POST", "/api/chat", 200, {"message": {"content": content}})
    grounding = OllamaGrounding(base_url=fake_server.url)
    desc = grounding.describe(new_capture_id(), 64, 40, image=encode_bmp(64, 40))
    labels = [region.label for region in desc.regions]
    assert labels == ["panel", "menu"]  # inverted box dropped, never fixed silently
    panel = desc.regions[0]
    assert (panel.x, panel.y, panel.width, panel.height) == (0.1, 0.1, 0.5 - 0.2, 0.4 - 0.2)
    menu = desc.regions[1]
    assert (menu.x, menu.y, menu.width, menu.height) == (0.0, 0.8, 1.0, 0.2000)


def test_ollama_describe_accepts_top_level_region_array(fake_server) -> None:
    # moondream's most consistent shape on UI frames: a bare JSON array of
    # flat region objects, no summary wrapper.
    content = '[{"label": "", "x": 0.12, "y": 0.73, "width": 1.0, "height": 0.86}]'
    fake_server.route("POST", "/api/chat", 200, {"message": {"content": content}})
    grounding = OllamaGrounding(base_url=fake_server.url)
    desc = grounding.describe(new_capture_id(), 64, 40, image=encode_bmp(64, 40))
    assert len(desc.regions) == 1
    region = desc.regions[0]
    assert region.label == "region"  # empty model label replaced, never blank
    assert (region.x, region.y) == (0.12, 0.73)
    # summary falls back to the raw (parseable-but-descriptionless) reply
    assert desc.summary.startswith("[ollama:moondream]")


def test_ollama_describe_accepts_bare_region_object(fake_server) -> None:
    content = 'Here you go: {"label": "window", "x": 0.1, "y": 0.1, "width": 0.5, "height": 0.5}'
    fake_server.route("POST", "/api/chat", 200, {"message": {"content": content}})
    grounding = OllamaGrounding(base_url=fake_server.url)
    desc = grounding.describe(new_capture_id(), 64, 40, image=encode_bmp(64, 40))
    assert len(desc.regions) == 1
    assert desc.regions[0].label == "window"


def test_ollama_describe_salvages_truncated_region_array(fake_server) -> None:
    # Output-token cap can cut the array mid-way; every complete object is
    # still a valid region candidate (never silently lost).
    content = (
        '[{"label": "a", "x": 0.1, "y": 0.1, "width": 0.2, "height": 0.2},'
        '{"label": "b", "x": 0.3, "y": 0.3, "width": 0.2, "height": 0.2},'
        '{"label": "c", "x": 0.5, "y": 0.5, "width": 0.2, "height": 0.2},'
        '{"label": "d", "x": 0.7, "y": 0.7, "width": 0.2, "hei'  # truncated
    )
    fake_server.route("POST", "/api/chat", 200, {"message": {"content": content}})
    grounding = OllamaGrounding(base_url=fake_server.url)
    desc = grounding.describe(new_capture_id(), 64, 40, image=encode_bmp(64, 40))
    assert [region.label for region in desc.regions] == ["a", "b", "c"]


def test_ollama_describe_requires_image() -> None:
    grounding = OllamaGrounding()
    with pytest.raises(ValueError, match="requires the capture image"):
        grounding.describe(new_capture_id(), 64, 40)


# --- selection ----------------------------------------------------------


def test_build_grounding_env_stub(monkeypatch) -> None:
    monkeypatch.setenv("GREATSAGE_VISION_GROUNDING", "stub")
    assert build_grounding().name == "stub"


def test_build_grounding_env_ollama_forced(monkeypatch) -> None:
    monkeypatch.setenv("GREATSAGE_VISION_GROUNDING", "ollama")
    assert build_grounding().name == "ollama"


def test_build_grounding_auto_without_server(monkeypatch) -> None:
    monkeypatch.setenv("GREATSAGE_VISION_GROUNDING", "auto")
    grounding = build_grounding(base_url="http://127.0.0.1:1")
    assert grounding.name == "stub"


def test_build_grounding_auto_with_model(monkeypatch, fake_server) -> None:
    monkeypatch.setenv("GREATSAGE_VISION_GROUNDING", "auto")
    fake_server.route("GET", "/api/tags", 200, _tags_with("moondream"))
    grounding = build_grounding(base_url=fake_server.url)
    assert grounding.name == "ollama"
