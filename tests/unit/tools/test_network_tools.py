"""Network + Home Assistant tool tests (roadmap Phase C, offline)."""

from __future__ import annotations

import io
import json
import urllib.error
from pathlib import Path
from typing import Any

import pytest

from greatsage.tools.homeassistant_tools import HomeAssistantCallTool, HomeAssistantStatesTool
from greatsage.tools.models import ToolCategory, ToolContext, ToolRisk
from greatsage.tools.network_tools import NetworkFetchTool, NetworkWebfetchTool


@pytest.fixture(autouse=True)
def _public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep URL validation offline; private-target tests override this."""
    monkeypatch.setattr(
        "greatsage.tools.network_security.resolve_host",
        lambda host, port=None: ("93.184.216.34",),
    )


def _context() -> ToolContext:
    return ToolContext(
        working_directory=Path("."),
        environment={},
        timeout_seconds=5.0,
        max_output_bytes=65536,
    )


class _FakeHeaders:
    def __init__(self, content_type: str = "text/html") -> None:
        self._content_type = content_type

    def get(self, name: str, default: str = "") -> str:
        return self._content_type if name.lower() == "content-type" else default


class _FakeResponse:
    def __init__(self, body: bytes, status: int = 200) -> None:
        self._body = body
        self.status = status
        self.headers = _FakeHeaders()

    def read(self, n: int = -1) -> bytes:
        return self._body if n is None or n < 0 else self._body[:n]

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None


def test_tool_declarations() -> None:
    fetch = NetworkFetchTool()
    assert fetch.category is ToolCategory.NETWORK
    assert fetch.risk_level is ToolRisk.MEDIUM
    states = HomeAssistantStatesTool()
    assert states.risk_level is ToolRisk.LOW
    call = HomeAssistantCallTool()
    assert call.risk_level is ToolRisk.HIGH


def test_fetch_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "greatsage.tools.network_tools._open_url",
        lambda req, timeout=None: _FakeResponse(b"hello world"),
    )
    result = NetworkFetchTool().execute({"url": "https://example.com/"}, _context())
    assert result.success is True
    assert result.output is not None
    assert result.output["text"] == "hello world"
    assert result.output["status"] == 200
    assert result.output["truncated"] is False


def test_fetch_truncates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "greatsage.tools.network_tools._open_url",
        lambda req, timeout=None: _FakeResponse(b"x" * 100),
    )
    result = NetworkFetchTool().execute(
        {"url": "https://example.com/", "max_bytes": 10}, _context()
    )
    assert result.success is True
    assert result.output is not None
    assert result.output["truncated"] is True
    assert len(result.output["text"]) == 10


def test_fetch_rejects_bad_urls() -> None:
    tool = NetworkFetchTool()
    assert tool.execute({"url": "ftp://example.com/x"}, _context()).success is False
    assert tool.execute({"url": "file:///etc/passwd"}, _context()).success is False
    assert tool.execute({"url": "https://user:pass@example.com/"}, _context()).success is False
    assert tool.execute({"url": "https://"}, _context()).success is False
    assert tool.execute({"url": ""}, _context()).success is False
    bad_size = {"url": "https://example.com", "max_bytes": "lots"}
    assert tool.execute(bad_size, _context()).success is False


@pytest.mark.parametrize(
    ("url", "resolved"),
    [
        ("http://127.0.0.1/", "127.0.0.1"),
        ("http://localhost/", "127.0.0.1"),
        ("http://[::1]/", "::1"),
        ("http://169.254.169.254/latest/meta-data/", "169.254.169.254"),
        ("http://10.0.0.1/", "10.0.0.1"),
    ],
)
def test_fetch_refuses_private_targets(
    monkeypatch: pytest.MonkeyPatch, url: str, resolved: str
) -> None:
    monkeypatch.setattr(
        "greatsage.tools.network_security.resolve_host",
        lambda host, port=None: (resolved,),
    )
    result = NetworkFetchTool().execute({"url": url}, _context())
    assert result.success is False
    assert "non-public" in (result.error or "")


def test_webfetch_refuses_private_target(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "greatsage.tools.network_security.resolve_host",
        lambda host, port=None: ("10.0.0.5",),
    )
    result = NetworkWebfetchTool().execute(
        {"url": "https://internal.example/"}, _context()
    )
    assert result.success is False
    assert "non-public" in (result.error or "")


def test_fetch_refuses_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "greatsage.tools.network_tools._open_url",
        lambda req, timeout=None: _FakeResponse(b"\x89PNG\x00binary"),
    )
    result = NetworkFetchTool().execute({"url": "https://example.com/a.png"}, _context())
    assert result.success is False


def test_fetch_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(req: object, timeout: object = None) -> _FakeResponse:
        raise urllib.error.HTTPError(str(req), 404, "nope", {}, io.BytesIO())

    monkeypatch.setattr("greatsage.tools.network_tools._open_url", boom)
    result = NetworkFetchTool().execute({"url": "https://example.com/missing"}, _context())
    assert result.success is False
    assert "404" in (result.error or "")


# --- webfetch: stdlib HTML extraction ------------------------------------

_HTML_PAGE = b"""<html><head><title>Example Docs</title>
<style>body{color:red}</style></head>
<body>
<h1>Welcome</h1>
<p>Great Sage can <a href="/guide">read docs</a> and summarize them.</p>
<script>alert('not visible')</script>
<ul><li>alpha</li><li>beta</li></ul>
<h2>Usage</h2>
<p>Call webfetch &amp; relax.</p>
</body></html>"""


def _html_response(body: bytes) -> _FakeResponse:
    response = _FakeResponse(body)
    response.headers = _FakeHeaders("text/html; charset=utf-8")
    return response


def test_webfetch_declarations() -> None:
    tool = NetworkWebfetchTool()
    assert tool.category is ToolCategory.NETWORK
    assert tool.risk_level is ToolRisk.MEDIUM
    assert tool.id == "network.webfetch"


def test_webfetch_markdown_extraction(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "greatsage.tools.network_tools._open_url",
        lambda req, timeout=None: _html_response(_HTML_PAGE),
    )
    result = NetworkWebfetchTool().execute({"url": "https://example.com/start"}, _context())
    assert result.success is True
    output = result.output or {}
    assert output["title"] == "Example Docs"
    assert output["format"] == "markdown"
    content = output["content"]
    assert "# Welcome" in content
    assert "## Usage" in content
    assert "[read docs](https://example.com/guide)" in content  # relative link resolved
    assert "- alpha" in content and "- beta" in content
    assert "Call webfetch & relax." in content  # entity decoded
    assert "alert" not in content  # script never visible
    assert "color:red" not in content  # style never visible
    assert output["truncated"] is False


def test_webfetch_text_format_drops_decorations(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "greatsage.tools.network_tools._open_url",
        lambda req, timeout=None: _html_response(_HTML_PAGE),
    )
    result = NetworkWebfetchTool().execute(
        {"url": "https://example.com/", "format": "text"}, _context()
    )
    assert result.success is True
    content = (result.output or {})["content"]
    assert "Welcome" in content
    assert "# " not in content  # no markdown headings
    assert "](" not in content  # no markdown links
    assert "read docs" in content  # link text preserved


def test_webfetch_char_cap_truncates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "greatsage.tools.network_tools._open_url",
        lambda req, timeout=None: _html_response(_HTML_PAGE),
    )
    result = NetworkWebfetchTool().execute(
        {"url": "https://example.com/", "max_chars": 40}, _context()
    )
    assert result.success is True
    output = result.output or {}
    assert output["truncated"] is True
    assert len(output["content"]) == 40


def test_webfetch_plain_text_passthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    response = _FakeResponse(b"plain body")
    response.headers = _FakeHeaders("text/plain")
    monkeypatch.setattr(
        "greatsage.tools.network_tools._open_url",
        lambda req, timeout=None: response,
    )
    result = NetworkWebfetchTool().execute({"url": "https://example.com/robots.txt"}, _context())
    assert result.success is True
    output = result.output or {}
    assert output["content"] == "plain body"
    assert output["format"] == "markdown"  # requested format, content untouched


def test_webfetch_rejects_unsupported_content_type(monkeypatch: pytest.MonkeyPatch) -> None:
    response = _FakeResponse(b"\x00\x01bin")
    response.headers = _FakeHeaders("application/octet-stream")
    monkeypatch.setattr(
        "greatsage.tools.network_tools._open_url",
        lambda req, timeout=None: response,
    )
    result = NetworkWebfetchTool().execute({"url": "https://example.com/file.bin"}, _context())
    assert result.success is False
    assert "content" in (result.error or "")


def test_webfetch_redacts_secret_shaped_content(monkeypatch: pytest.MonkeyPatch) -> None:
    page = b"<html><body><p>token sk-ABCDEFGHIJKLMNOPQRSTUV leaked</p></body></html>"
    monkeypatch.setattr(
        "greatsage.tools.network_tools._open_url", lambda req, timeout=None: _html_response(page)
    )
    result = NetworkWebfetchTool().execute({"url": "https://example.com/"}, _context())
    content = (result.output or {})["content"]
    assert "sk-ABCDEFGHIJKLMNOPQRSTUV" not in content
    assert "REDACTED" in content


def test_webfetch_rejects_bad_urls() -> None:
    tool = NetworkWebfetchTool()
    assert tool.execute({"url": "ftp://example.com/x"}, _context()).success is False
    assert tool.execute({"url": "https://user:pass@example.com/"}, _context()).success is False
    assert tool.execute({"url": "https://"}, _context()).success is False


def test_webfetch_rejects_bad_format_and_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    tool = NetworkWebfetchTool()
    bad_format = tool.execute({"url": "https://example.com", "format": "pdf"}, _context())
    assert bad_format.success is False

    def boom(req: object, timeout: object = None) -> _FakeResponse:
        raise urllib.error.HTTPError(str(req), 503, "down", {}, io.BytesIO())

    monkeypatch.setattr("greatsage.tools.network_tools._open_url", boom)
    result = tool.execute({"url": "https://example.com/"}, _context())
    assert result.success is False
    assert "503" in (result.error or "")


def test_extract_html_never_raises_on_garbage() -> None:
    from greatsage.tools.network_tools import extract_html

    title, content = extract_html("not <html ><<< broken", base_url="https://x/", markdown=True)
    assert isinstance(content, str)  # partial extraction wins, no crash
    assert title is None


def test_ha_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HA_BASE_URL", raising=False)
    monkeypatch.delenv("HA_TOKEN", raising=False)
    result = HomeAssistantStatesTool().execute({}, _context())
    assert result.success is False
    assert "not configured" in (result.error or "")
    result = HomeAssistantCallTool().execute(
        {"domain": "light", "service": "turn_on"}, _context()
    )
    assert result.success is False


def test_ha_states(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HA_BASE_URL", "http://ha.local:8123")
    monkeypatch.setenv("HA_TOKEN", "token-abc")
    seen: dict[str, Any] = {}

    def fake(req: object, timeout: object = None) -> _FakeResponse:
        seen["url"] = req.full_url  # type: ignore[union-attr]
        seen["auth"] = req.headers.get("Authorization")  # type: ignore[union-attr]
        body = json.dumps([{"entity_id": "light.lamp", "state": "on"}]).encode()
        return _FakeResponse(body)

    monkeypatch.setattr("urllib.request.urlopen", fake)
    result = HomeAssistantStatesTool().execute({}, _context())
    assert result.success is True
    assert seen["url"] == "http://ha.local:8123/api/states"
    assert seen["auth"] == "Bearer token-abc"

    result = HomeAssistantStatesTool().execute({"entity_id": "light.lamp"}, _context())
    assert result.success is True
    assert seen["url"].endswith("/api/states/light.lamp")
    assert HomeAssistantStatesTool().execute({"entity_id": "../evil"}, _context()).success is False


def test_ha_call(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HA_BASE_URL", "http://ha.local:8123")
    monkeypatch.setenv("HA_TOKEN", "token-abc")
    seen: dict[str, Any] = {}

    def fake(req: object, timeout: object = None) -> _FakeResponse:
        seen["url"] = req.full_url  # type: ignore[union-attr]
        seen["data"] = json.loads(req.data.decode())  # type: ignore[union-attr]
        return _FakeResponse(b"[]")

    monkeypatch.setattr("urllib.request.urlopen", fake)
    result = HomeAssistantCallTool().execute(
        {"domain": "light", "service": "turn_on",
         "entity_id": "light.lamp", "data": {"brightness": 100}},
        _context(),
    )
    assert result.success is True
    assert seen["url"] == "http://ha.local:8123/api/services/light/turn_on"
    assert seen["data"] == {"entity_id": "light.lamp", "brightness": 100}
    assert "token-abc" not in json.dumps(result.output)

    tool = HomeAssistantCallTool()
    assert tool.execute({"domain": "light/../x", "service": "turn_on"}, _context()).success is False
    assert tool.execute({"domain": "light"}, _context()).success is False
