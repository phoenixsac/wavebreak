"""Shared HTTP helper for the wavebreak_clients package.

stdlib-only (urllib), used by hawkbit / observability / lab clients so they
don't each reimplement query-string building, auth headers, timeouts, and
error handling.

Typical usage::

    status, headers, body = request("GET", url, params={"query": "up"})
    data = request_json("GET", url, params={"query": "up"}, timeout=10)
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


class HttpError(Exception):
    """Raised by request_json() when the server responds with status >= 400.

    Attributes:
        status: HTTP status code.
        url: the final request URL (including query string).
        body_text: response body, truncated to 500 characters.
    """

    def __init__(self, status: int, url: str, body_text: str) -> None:
        self.status = status
        self.url = url
        self.body_text = body_text[:500]
        super().__init__(f"HTTP {status} for {url}: {self.body_text}")


def _build_url(url: str, params: dict[str, Any] | None) -> str:
    """Append a query string built from params (None values dropped)."""
    if not params:
        return url
    clean = {k: v for k, v in params.items() if v is not None}
    if not clean:
        return url
    query = urllib.parse.urlencode(clean, doseq=True)
    separator = "&" if urllib.parse.urlparse(url).query else "?"
    return f"{url}{separator}{query}"


def _build_body_and_headers(
    json_body: Any,
    data: bytes | dict[str, Any] | None,
    headers: dict[str, str] | None,
) -> tuple[bytes | None, dict[str, str]]:
    """Turn json_body/data into request bytes and merge in content headers."""
    out_headers: dict[str, str] = dict(headers or {})
    if json_body is not None:
        body = json.dumps(json_body).encode("utf-8")
        out_headers.setdefault("Content-Type", "application/json")
        return body, out_headers
    if isinstance(data, dict):
        body = urllib.parse.urlencode(data).encode("utf-8")
        out_headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
        return body, out_headers
    if isinstance(data, (bytes, bytearray)):
        return bytes(data), out_headers
    return None, out_headers


def request(
    method: str,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    json_body: Any = None,
    data: bytes | dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    auth: tuple[str, str] | None = None,
    bearer: str | None = None,
    timeout: float = 30,
) -> tuple[int, dict[str, str], bytes]:
    """Perform an HTTP request and return (status, response_headers, body_bytes).

    Does not raise on non-2xx status; callers that want that behaviour
    should use request_json(), or check the returned status themselves.

    Args:
        method: HTTP method, e.g. "GET", "POST".
        url: base URL, without query string (params are appended).
        params: query-string parameters; None values are dropped; list
            values produce repeated keys (e.g. match[]=a&match[]=b).
        json_body: if given, JSON-encoded and sent as the body with
            Content-Type: application/json.
        data: raw bytes body, or a dict to be form-urlencoded. Ignored if
            json_body is given.
        headers: extra request headers.
        auth: (username, password) for HTTP Basic auth.
        bearer: token for an Authorization: Bearer header.
        timeout: socket timeout in seconds.
    """
    full_url = _build_url(url, params)
    body, req_headers = _build_body_and_headers(json_body, data, headers)

    if auth is not None:
        user, password = auth
        token = base64.b64encode(f"{user}:{password}".encode()).decode("ascii")
        req_headers["Authorization"] = f"Basic {token}"
    if bearer is not None:
        req_headers["Authorization"] = f"Bearer {bearer}"

    req = urllib.request.Request(full_url, data=body, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp_body = resp.read()
            resp_headers = dict(resp.headers.items())
            return resp.status, resp_headers, resp_body
    except urllib.error.HTTPError as exc:
        resp_body = exc.read()
        resp_headers = dict(exc.headers.items()) if exc.headers else {}
        return exc.code, resp_headers, resp_body


def request_json(
    method: str,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    json_body: Any = None,
    data: bytes | dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    auth: tuple[str, str] | None = None,
    bearer: str | None = None,
    timeout: float = 30,
) -> Any:
    """Like request(), but decodes a JSON body and raises HttpError on status >= 400."""
    status, _headers, body = request(
        method,
        url,
        params=params,
        json_body=json_body,
        data=data,
        headers=headers,
        auth=auth,
        bearer=bearer,
        timeout=timeout,
    )
    full_url = _build_url(url, params)
    if status >= 400:
        raise HttpError(status, full_url, body.decode("utf-8", errors="replace"))
    if not body:
        return None
    return json.loads(body.decode("utf-8"))
