"""Minimal stdlib HTTP transport for provider adapters.

This module uses only the standard library (full runtime dependency list —
docs/DEPENDENCY_POLICY.md). Exceptions raised
here are wrapped by adapters into ProviderError subclasses.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Iterator
from typing import Any


class HTTPErrorStatus(Exception):
    """Raised when the server responds with a status outside 2xx."""

    def __init__(self, status: int, body: str = "") -> None:
        self.status = status
        self.body = body
        super().__init__(f"HTTP {status}: {body[:300]}")


def _timeout_settings(timeout: float | None, default: float) -> float:
    if timeout is None or timeout <= 0:
        return default
    return timeout


def request_json(
    url: str,
    *,
    method: str = "GET",
    body: Any = None,
    headers: dict[str, str] | None = None,
    timeout: float | None = None,
    default_timeout: float = 30.0,
) -> Any:
    """Perform an HTTP request and parse the JSON response body.

    Raises HTTPErrorStatus for non-2xx responses, urllib.error.URLError for
    network failures (including timeouts), and ValueError for malformed JSON.
    """
    try:
        raw, status = _request_raw(
            url, method=method, body=body, headers=headers,
            timeout=timeout, default_timeout=default_timeout,
        )
    except urllib.error.HTTPError as exc:
        raise HTTPErrorStatus(exc.code, exc.read().decode("utf-8", errors="replace")) from exc
    if status < 200 or status >= 300:
        raise HTTPErrorStatus(status, raw[:300].decode("utf-8", errors="replace"))
    if not raw:
        return {}
    return json.loads(raw.decode("utf-8"))


def request_text(
    url: str,
    *,
    method: str = "GET",
    body: Any = None,
    headers: dict[str, str] | None = None,
    timeout: float | None = None,
    default_timeout: float = 30.0,
) -> str:
    """Like request_json but returns the raw text body."""
    try:
        raw, status = _request_raw(
            url, method=method, body=body, headers=headers,
            timeout=timeout, default_timeout=default_timeout,
        )
    except urllib.error.HTTPError as exc:
        raise HTTPErrorStatus(exc.code, exc.read().decode("utf-8", errors="replace")) from exc
    if status < 200 or status >= 300:
        raise HTTPErrorStatus(status, raw[:300].decode("utf-8", errors="replace"))
    return raw.decode("utf-8")


def _request_raw(
    url: str,
    *,
    method: str,
    body: Any,
    headers: dict[str, str] | None,
    timeout: float | None,
    default_timeout: float,
) -> tuple[bytes, int]:
    request_headers = {"Accept": "application/json"}
    if headers:
        request_headers.update(headers)
    data = None
    if body is not None:
        request_headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers=request_headers, method=method
    )
    act_timeout = _timeout_settings(timeout, default_timeout)
    with urllib.request.urlopen(req, timeout=act_timeout) as response:  # noqa: S310
        raw = response.read()
        status = getattr(response, "status", 200)
    return raw, status


def iter_sse(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: float | None = None,
    default_timeout: float = 30.0,
) -> Iterator[tuple[str, str]]:
    """Yield (event, data) pairs from a Server-Sent Events stream.

    Framing follows the SSE specification: ``event:`` names the event
    (default ``message``), ``data:`` lines accumulate, and a blank line
    dispatches the pair. Comments and malformed lines are skipped.

    Raises HTTPErrorStatus for non-2xx statuses and urllib.error.URLError
    for connection failures, socket timeouts while reading, or premature
    stream termination — the caller owns reconnection policy (bounded).
    """
    request_headers = {"Accept": "text/event-stream"}
    if headers:
        request_headers.update(headers)
    req = urllib.request.Request(url, headers=request_headers, method="GET")
    act_timeout = _timeout_settings(timeout, default_timeout)
    try:
        response = urllib.request.urlopen(req, timeout=act_timeout)  # noqa: S310
    except urllib.error.HTTPError as exc:
        raise HTTPErrorStatus(exc.code, exc.read().decode("utf-8", errors="replace")) from exc
    event_name = "message"
    data_lines: list[str] = []
    try:
        with response:
            while True:
                try:
                    line = response.readline()
                except TimeoutError as exc:
                    raise urllib.error.URLError("SSE read timed out") from exc
                except OSError as exc:
                    raise urllib.error.URLError(f"SSE stream lost: {exc}") from exc
                if not line:
                    if data_lines:
                        yield event_name, "\n".join(data_lines)
                    return
                text = line.decode("utf-8", errors="replace").rstrip("\r\n")
                if not text:
                    if data_lines:
                        yield event_name, "\n".join(data_lines)
                    event_name = "message"
                    data_lines = []
                    continue
                if text.startswith("event:"):
                    event_name = text[len("event:") :].strip()
                elif text.startswith("data:"):
                    data_lines.append(text[len("data:") :].strip())
    finally:
        try:
            response.close()
        except OSError:
            pass
