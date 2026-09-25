"""Tests for wavebreak_clients.observability using a real local HTTP server.

A local http.server.ThreadingHTTPServer serves recorded fixture JSON so we
exercise the real urllib request path end to end: URL paths, query
parameters, and headers actually sent over the wire are recorded and
asserted on.
"""

from __future__ import annotations

import json
import sys
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from wavebreak_clients._http import HttpError
from wavebreak_clients.observability import Loki, Prometheus, QueryError

FIXTURES = Path(__file__).parent / "fixtures"

Router = Callable[[str, dict[str, list[str]], dict[str, str]], tuple[int, Any]]


def load_fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


class RecordingServer(ThreadingHTTPServer):
    """HTTP server that records every request and delegates response via a router."""

    allow_reuse_address = True

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.requests: list[dict[str, Any]] = []
        self.router: Router | None = None


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args: Any) -> None:  # silence test output
        pass

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query, keep_blank_values=True)
        headers = dict(self.headers.items())
        self.server.requests.append({"path": parsed.path, "params": params, "headers": headers})

        assert self.server.router is not None, "test must set server.router"
        status, body = self.server.router(parsed.path, params, headers)

        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@pytest.fixture
def server():
    httpd = RecordingServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def base_url(server: RecordingServer) -> str:
    return f"http://127.0.0.1:{server.server_port}"


# --- Prometheus -------------------------------------------------------------


def test_prometheus_query_success(server):
    fixture = load_fixture("prom_query_vector.json")
    server.router = lambda path, params, headers: (200, fixture)

    prom = Prometheus(base_url(server))
    result = prom.query('up{device_id="edge-003"}')

    assert result == fixture["data"]["result"]
    req = server.requests[0]
    assert req["path"] == "/api/v1/query"
    assert req["params"]["query"] == ['up{device_id="edge-003"}']


def test_prometheus_query_with_datetime_time(server):
    fixture = load_fixture("prom_query_vector.json")
    server.router = lambda path, params, headers: (200, fixture)

    prom = Prometheus(base_url(server))
    when = datetime(2023, 9, 25, 0, 0, 0, tzinfo=UTC)
    prom.query("up", time=when)

    sent_time = float(server.requests[0]["params"]["time"][0])
    assert sent_time == pytest.approx(when.timestamp())


def test_prometheus_query_range_params_and_time_conversion(server):
    fixture = load_fixture("prom_query_range_matrix.json")
    server.router = lambda path, params, headers: (200, fixture)

    prom = Prometheus(base_url(server))
    start = datetime(2023, 9, 25, 0, 0, 0, tzinfo=UTC)
    result = prom.query_range(
        "max_over_time(wavebreak_app_cgroup_memory_bytes[5m])",
        start=start,
        end=1695600030.0,
        step="15s",
    )

    assert result == fixture["data"]["result"]
    req = server.requests[0]
    assert req["path"] == "/api/v1/query_range"
    assert float(req["params"]["start"][0]) == pytest.approx(start.timestamp())
    assert float(req["params"]["end"][0]) == pytest.approx(1695600030.0)
    assert req["params"]["step"] == ["15s"]


def test_prometheus_query_range_time_from_rfc3339_string(server):
    fixture = load_fixture("prom_query_range_matrix.json")
    server.router = lambda path, params, headers: (200, fixture)

    prom = Prometheus(base_url(server))
    prom.query_range("up", start="2023-09-25T00:00:00Z", end="2023-09-25T00:00:30Z")

    req = server.requests[0]
    assert float(req["params"]["start"][0]) == pytest.approx(1695600000.0)
    assert float(req["params"]["end"][0]) == pytest.approx(1695600030.0)


def test_prometheus_series_multiple_match_params(server):
    fixture = load_fixture("prom_series.json")
    server.router = lambda path, params, headers: (200, fixture)

    prom = Prometheus(base_url(server))
    result = prom.series(["up{device_id=~'edge.*'}", "wavebreak_app_fps"])

    assert result == fixture["data"]
    req = server.requests[0]
    assert req["path"] == "/api/v1/series"
    assert req["params"]["match[]"] == ["up{device_id=~'edge.*'}", "wavebreak_app_fps"]


def test_prometheus_label_values(server):
    fixture = load_fixture("prom_label_values.json")
    server.router = lambda path, params, headers: (200, fixture)

    prom = Prometheus(base_url(server))
    result = prom.label_values("device_id")

    assert result == fixture["data"]
    assert server.requests[0]["path"] == "/api/v1/label/device_id/values"


def test_prometheus_query_error_raises_query_error(server):
    fixture = load_fixture("prom_query_error.json")
    server.router = lambda path, params, headers: (400, fixture)

    prom = Prometheus(base_url(server))
    with pytest.raises(QueryError) as exc_info:
        prom.query("invalid{")

    assert exc_info.value.status == "error"
    assert exc_info.value.error_type == "bad_data"
    assert "parse error" in exc_info.value.error


def test_prometheus_non_json_error_raises_http_error(server):
    server.router = lambda path, params, headers: (503, {"unexpected": "shape"})
    # Force a non-JSON-status body by overriding router to send raw text via
    # a status field-less dict; the client should fall back to HttpError.
    prom = Prometheus(base_url(server))
    with pytest.raises(HttpError) as exc_info:
        prom.query("up")
    assert exc_info.value.status == 503


# --- Loki ---------------------------------------------------------------


def test_loki_query_range_streams_and_ns_time_conversion(server):
    fixture = load_fixture("loki_query_range_streams.json")
    server.router = lambda path, params, headers: (200, fixture)

    loki = Loki(base_url(server))
    start = datetime(2023, 9, 25, 0, 0, 0, tzinfo=UTC)
    end = datetime(2023, 9, 25, 0, 1, 0, tzinfo=UTC)
    result = loki.query_range('{device_id="edge-003"} |= "oom-kill"', start, end)

    assert result == fixture["data"]["result"]
    req = server.requests[0]
    assert req["path"] == "/loki/api/v1/query_range"
    assert req["params"]["query"] == ['{device_id="edge-003"} |= "oom-kill"']
    assert int(req["params"]["start"][0]) == int(start.timestamp() * 1_000_000_000)
    assert int(req["params"]["end"][0]) == int(end.timestamp() * 1_000_000_000)
    assert req["params"]["limit"] == ["500"]
    assert req["params"]["direction"] == ["backward"]


def test_loki_flatten_streams_sorted_by_ts():
    fixture = load_fixture("loki_query_range_streams.json")
    flat = Loki.flatten_streams(fixture["data"]["result"])

    assert [entry["ts"] for entry in flat] == sorted(entry["ts"] for entry in flat)
    assert flat[0]["line"] == "starting inference-app v1.2"
    assert flat[0]["labels"] == {"device_id": "edge-003", "app": "inference-app"}
    assert flat[-1]["line"] == "oom-kill: process inference-app killed"
    assert all(isinstance(entry["ts"], int) for entry in flat)


def test_loki_instant_query_for_metric(server):
    fixture = load_fixture("loki_query_instant_vector.json")
    server.router = lambda path, params, headers: (200, fixture)

    loki = Loki(base_url(server))
    result = loki.query('count_over_time({app="inference-app"} |= "oom-kill" [10m])')

    assert result == fixture["data"]["result"]
    assert server.requests[0]["path"] == "/loki/api/v1/query"


def test_loki_labels_and_label_values(server):
    labels_fixture = load_fixture("loki_labels.json")
    values_fixture = load_fixture("loki_label_values.json")

    def router(path, params, headers):
        if path == "/loki/api/v1/labels":
            return 200, labels_fixture
        return 200, values_fixture

    server.router = router
    loki = Loki(base_url(server))

    assert loki.labels() == labels_fixture["data"]
    assert loki.label_values("app") == values_fixture["data"]
    assert server.requests[-1]["path"] == "/loki/api/v1/label/app/values"


# --- Grafana proxy --------------------------------------------------------


def test_prometheus_from_grafana_routes_through_proxy_with_bearer(server):
    fixture = load_fixture("prom_query_vector.json")
    server.router = lambda path, params, headers: (200, fixture)

    prom = Prometheus.from_grafana(base_url(server), "prom-uid", "secret-token")
    prom.query("up")

    req = server.requests[0]
    assert req["path"] == "/api/datasources/proxy/uid/prom-uid/api/v1/query"
    assert req["headers"]["Authorization"] == "Bearer secret-token"


def test_loki_from_grafana_routes_through_proxy_with_bearer(server):
    fixture = load_fixture("loki_query_range_streams.json")
    server.router = lambda path, params, headers: (200, fixture)

    loki = Loki.from_grafana(base_url(server), "loki-uid", "secret-token")
    loki.query_range('{device_id="edge-003"}', 1695600000, 1695600030)

    req = server.requests[0]
    assert req["path"] == "/api/datasources/proxy/uid/loki-uid/loki/api/v1/query_range"
    assert req["headers"]["Authorization"] == "Bearer secret-token"
