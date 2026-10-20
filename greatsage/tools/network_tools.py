"""Network tools (roadmap Phase C + webfetch).

- `network.fetch` — bounded read-only HTTP(S) GET, raw bounded text.
- `network.webfetch` — fetch a page and return readable content: standard-
  library HTML extraction (`html.parser`) producing `markdown` (headings,
  links, bullets) or plain `text` (visible text only). Mirrors the OpenCode
  `webfetch` surface (url + format) without adding any dependency.

Both tools: never send credentials (urls with userinfo are refused), never
touch non-HTTP schemes, cap download bytes, and are MEDIUM risk so every
call is ASK-gated in normal mode. Fetched content is untrusted data:
secret-shaped strings are redacted defensively, and audit events carry only
urls/counts (never page content).
"""

from __future__ import annotations

import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from typing import Any

from greatsage.tools.models import BaseTool, ToolCategory, ToolContext, ToolResult, ToolRisk
from greatsage.tools.redaction import redact_secrets

log = logging.getLogger("greatsage.tools.network")

_DEFAULT_MAX_BYTES = 32768
_ABSOLUTE_MAX_BYTES = 262144

#: webfetch download cap (before extraction); extracted output is separately
#: char-capped. 256 KiB is the same absolute ceiling network.fetch allows.
_WEBFETCH_MAX_DOWNLOAD_BYTES = 262144
_WEBFETCH_DEFAULT_CHARS = 8000
_WEBFETCH_ABSOLUTE_CHARS = 32000


def _failure(request_id: str, tool_id: str, error: str) -> ToolResult:
    return ToolResult(request_id=request_id, tool_id=tool_id, success=False, error=error)


def _validate_url(url: object, tool_id: str) -> tuple[str | None, str | None]:
    """Shared url validation: returns (normalized_url, error)."""
    if not isinstance(url, str) or not url.strip():
        return None, f"{tool_id} requires a non-empty url"
    normalized = url.strip()
    try:
        parsed = urllib.parse.urlsplit(normalized)
    except ValueError as exc:
        return None, f"{tool_id} refuses malformed url: {exc}"
    if parsed.scheme not in ("http", "https"):
        return None, f"{tool_id} allows http(s) only: {parsed.scheme or '-'}"
    if not parsed.hostname:
        return None, f"{tool_id} refuses url without a host"
    if parsed.username or parsed.password or "@" in parsed.netloc:
        return None, f"{tool_id} refuses urls with credentials"
    return normalized, None


class NetworkFetchTool(BaseTool):
    id = "network.fetch"
    name = "Network fetch"
    description = "Bounded read-only HTTP(S) GET; text only, no credentials, no other schemes."
    version = "1.0.0"
    risk_level = ToolRisk.MEDIUM
    category = ToolCategory.NETWORK
    capabilities = ("read",)
    input_schema = {
        "type": "object",
        "properties": {
            "url": {"type": "string"},
            "max_bytes": {"type": "integer"},
        },
        "required": ["url"],
    }
    output_schema = {
        "type": "object",
        "properties": {
            "url": {"type": "string"},
            "status": {"type": "integer"},
            "content_type": {"type": "string"},
            "truncated": {"type": "boolean"},
            "text": {"type": "string"},
        },
        "required": ["url", "status", "truncated", "text"],
    }

    def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        started = time.monotonic()
        url, error = _validate_url(arguments.get("url"), self.id)
        if error is not None or url is None:
            return _failure("", self.id, error or "invalid url")
        parsed = urllib.parse.urlsplit(url)
        max_bytes = arguments.get("max_bytes", _DEFAULT_MAX_BYTES)
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int):
            return _failure("", self.id, "network.fetch max_bytes must be an integer")
        max_bytes = max(1, min(max_bytes, _ABSOLUTE_MAX_BYTES))
        timeout = max(1.0, min(context.timeout_seconds, 60.0))
        request = urllib.request.Request(
            url, headers={"User-Agent": "GreatSage-local/1.0"}, method="GET"
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                status = response.status
                content_type = response.headers.get("Content-Type", "")
                raw = response.read(max_bytes + 1)
        except urllib.error.HTTPError as exc:
            return _failure(
                "", self.id, f"network.fetch http error {exc.code} for {parsed.hostname}"
            )
        except urllib.error.URLError as exc:
            return _failure(
                "", self.id, f"network.fetch failed for {parsed.hostname}: {exc.reason}"
            )
        except (TimeoutError, OSError) as exc:
            return _failure("", self.id, f"network.fetch failed for {parsed.hostname}: {exc}")
        truncated = len(raw) > max_bytes
        raw = raw[:max_bytes]
        if b"\x00" in raw:
            return _failure("", self.id, "network.fetch refuses binary content")
        text = raw.decode("utf-8", errors="replace")
        duration_ms = (time.monotonic() - started) * 1000.0
        return ToolResult(
            request_id="",
            tool_id=self.id,
            success=True,
            output={
                "url": url,
                "status": status,
                "content_type": content_type,
                "truncated": truncated,
                "text": text,
            },
            duration_ms=duration_ms,
        )


# --- webfetch: stdlib HTML extraction -----------------------------------

_SKIP_TAGS = frozenset({"script", "style", "noscript", "svg", "template"})
_BLOCK_TAGS = frozenset(
    {
        "p", "div", "section", "article", "header", "footer", "main", "aside",
        "table", "tr", "blockquote", "pre", "hr", "br", "figure",
        "form", "fieldset", "details", "summary",
    }
)
_HEADING_PREFIX = {"h1": "# ", "h2": "## ", "h3": "### ", "h4": "#### ",
                   "h5": "##### ", "h6": "###### "}


class _HtmlExtractor(HTMLParser):
    """Extract readable content (markdown or plain text) from HTML.

    Standard library only: block-level structure becomes paragraphs, headings
    keep their level, list items become bullets/numbers, links become
    `[text](url)` references in markdown mode. Scripts/styles/head metadata
    are excluded. Whitespace is collapsed outside <pre>.
    """

    def __init__(self, *, base_url: str, markdown: bool) -> None:
        super().__init__(convert_charrefs=True)
        self._base_url = base_url
        self._markdown = markdown
        self._skip_depth = 0
        self._pre_depth = 0
        self._in_title = False
        self._title_parts: list[str] = []
        self._blocks: list[str] = []
        self._current: list[str] = []
        self._prefix = ""
        self._link_href: str | None = None
        self._list_stack: list[str] = []
        self._ol_counters: list[int] = []

    # --- public helpers -------------------------------------------------

    @property
    def title(self) -> str | None:
        title = " ".join("".join(self._title_parts).split())
        return title[:200] if title else None

    def content(self) -> str:
        self._flush()
        text = "\n\n".join(self._blocks)
        return text.strip()

    # --- parser callbacks -----------------------------------------------

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._skip_depth:
            self._skip_depth += 1
            return
        if tag in _SKIP_TAGS:
            self._skip_depth = 1
            return
        if tag == "title":
            self._in_title = True
            return
        if tag == "pre":
            self._pre_depth += 1
        if tag in _HEADING_PREFIX:
            self._flush()
            self._prefix = _HEADING_PREFIX[tag] if self._markdown else ""
            if not self._markdown:
                self._flush()  # headings on their own line in text mode
            return
        if tag in ("ul", "ol"):
            self._flush()
            self._list_stack.append(tag)
            self._ol_counters.append(0)
            return
        if tag == "li":
            self._flush()
            if self._markdown:
                if self._list_stack and self._list_stack[-1] == "ol":
                    self._ol_counters[-1] += 1
                    self._prefix = f"{self._ol_counters[-1]}. "
                else:
                    self._prefix = "- "
            return
        if tag == "a":
            href = None
            for name, value in attrs:
                if name == "href" and value:
                    href = value
                    break
            if self._markdown and href:
                self._link_href = urllib.parse.urljoin(self._base_url, href)
                self._current.append("[")
            return
        if tag in _BLOCK_TAGS:
            self._flush()

    def handle_endtag(self, tag: str) -> None:
        if self._skip_depth:
            self._skip_depth -= 1
            return
        if tag == "title":
            self._in_title = False
            return
        if tag == "pre":
            self._pre_depth = max(0, self._pre_depth - 1)
        if tag in _HEADING_PREFIX:
            self._flush()
            self._prefix = ""
            return
        if tag in ("ul", "ol"):
            self._flush()
            if self._list_stack:
                self._list_stack.pop()
                self._ol_counters.pop()
            return
        if tag == "li":
            self._flush()
            self._prefix = ""
            return
        if tag == "a":
            if self._link_href is not None:
                self._current.append(f"]({self._link_href})")
                self._link_href = None
            return
        if tag in _BLOCK_TAGS:
            self._flush()

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_title:
            self._title_parts.append(data)
            return
        if self._pre_depth:
            self._current.append(data)
            return
        collapsed = " ".join(data.split())
        if collapsed:
            if self._current and not self._current[-1].endswith((" ", "[", "(")):
                self._current.append(" ")
            self._current.append(collapsed)

    # --- internals ------------------------------------------------------

    def _flush(self) -> None:
        text = "".join(self._current).strip()
        self._current.clear()
        if not text:
            return
        self._blocks.append(f"{self._prefix}{text}")


def extract_html(
    html_text: str, *, base_url: str, markdown: bool = True
) -> tuple[str | None, str]:
    """Extract (title, readable content) from HTML; never raises."""
    extractor = _HtmlExtractor(base_url=base_url, markdown=markdown)
    try:
        extractor.feed(html_text)
        extractor.close()
    except Exception as exc:  # parser robustness: partial extraction wins
        log.debug("html extraction degraded: %s", exc)
    return extractor.title, extractor.content()


class NetworkWebfetchTool(BaseTool):
    id = "network.webfetch"
    name = "Web fetch"
    description = (
        "Fetch a web page and return readable content (markdown or text); "
        "stdlib HTML extraction, download-capped, ASK-gated."
    )
    version = "1.0.0"
    risk_level = ToolRisk.MEDIUM
    category = ToolCategory.NETWORK
    capabilities = ("read",)
    input_schema = {
        "type": "object",
        "properties": {
            "url": {"type": "string"},
            "format": {"type": "string", "enum": ["markdown", "text"]},
            "max_chars": {"type": "integer"},
        },
        "required": ["url"],
    }
    output_schema = {
        "type": "object",
        "properties": {
            "url": {"type": "string"},
            "final_url": {"type": "string"},
            "status": {"type": "integer"},
            "content_type": {"type": "string"},
            "format": {"type": "string"},
            "title": {"type": "string"},
            "chars": {"type": "integer"},
            "truncated": {"type": "boolean"},
            "content": {"type": "string"},
        },
        "required": ["url", "status", "format", "chars", "truncated", "content"],
    }

    def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        started = time.monotonic()
        url, error = _validate_url(arguments.get("url"), self.id)
        if error is not None or url is None:
            return _failure("", self.id, error or "invalid url")
        parsed = urllib.parse.urlsplit(url)
        fmt = arguments.get("format", "markdown")
        if fmt not in ("markdown", "text"):
            return _failure("", self.id, "network.webfetch format must be markdown or text")
        max_chars = arguments.get("max_chars", _WEBFETCH_DEFAULT_CHARS)
        if isinstance(max_chars, bool) or not isinstance(max_chars, int):
            return _failure("", self.id, "network.webfetch max_chars must be an integer")
        max_chars = max(1, min(max_chars, _WEBFETCH_ABSOLUTE_CHARS))
        timeout = max(1.0, min(context.timeout_seconds, 60.0))
        request = urllib.request.Request(
            url, headers={"User-Agent": "GreatSage-local/1.0"}, method="GET"
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                status = response.status
                content_type = response.headers.get("Content-Type", "")
                final_url = response.geturl() if hasattr(response, "geturl") else url
                raw = response.read(_WEBFETCH_MAX_DOWNLOAD_BYTES + 1)
        except urllib.error.HTTPError as exc:
            return _failure(
                "", self.id, f"network.webfetch http error {exc.code} for {parsed.hostname}"
            )
        except urllib.error.URLError as exc:
            return _failure(
                "", self.id, f"network.webfetch failed for {parsed.hostname}: {exc.reason}"
            )
        except (TimeoutError, OSError) as exc:
            return _failure("", self.id, f"network.webfetch failed for {parsed.hostname}: {exc}")
        download_truncated = len(raw) > _WEBFETCH_MAX_DOWNLOAD_BYTES
        raw = raw[: _WEBFETCH_MAX_DOWNLOAD_BYTES]
        if b"\x00" in raw:
            return _failure("", self.id, "network.webfetch refuses binary content")
        content_type_main = content_type.split(";", 1)[0].strip().lower()
        text = raw.decode("utf-8", errors="replace")
        title: str | None = None
        if content_type_main in ("text/html", "application/xhtml+xml") or (
            not content_type_main and "<html" in text[:2048].lower()
        ):
            title, content = extract_html(
                text, base_url=final_url or url, markdown=fmt == "markdown"
            )
        elif content_type_main.startswith("text/") or content_type_main in (
            "application/json",
            "application/xml",
            "application/x-yaml",
        ):
            # Non-HTML text: return the bounded raw text as-is.
            title, content = None, text
        else:
            kind = content_type_main or "unknown"
            return _failure(
                "",
                self.id,
                f"network.webfetch supports html/text content, got {kind}",
            )
        truncated = download_truncated or len(content) > max_chars
        content = content[:max_chars]
        content = redact_secrets(content) or content
        duration_ms = (time.monotonic() - started) * 1000.0
        output: dict[str, Any] = {
            "url": url,
            "final_url": str(final_url or url),
            "status": status,
            "content_type": content_type,
            "format": fmt,
            "chars": len(content),
            "truncated": truncated,
            "content": content,
        }
        if title:
            output["title"] = title
        return ToolResult(
            request_id="", tool_id=self.id, success=True, output=output,
            duration_ms=duration_ms,
        )
