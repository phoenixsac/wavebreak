"""Thin clients for the Wavebreak observability stack (Prometheus + Loki).

Both classes talk to the service directly by default, or can be routed
through a Grafana datasource proxy via ``from_grafana(...)`` so callers only
need one bearer token. stdlib only.

Typical PromQL queries for this project::

    max_over_time(wavebreak_app_cgroup_memory_bytes[5m])   # by device_id
    increase(wavebreak_app_restarts_total[10m])
    wavebreak_app_fps

Typical LogQL queries::

    {device_id="edge-003"} |= "oom-kill"
    {app="ota-agent"} |= "install failed"
    count_over_time({app="inference-app"} |= "oom-kill" [10m])   # instant/metric query

Example::

    prom = Prometheus("http://localhost:9090")
    result = prom.query('up{device_id="edge-003"}')

    loki = Loki.from_grafana("http://localhost:3000", "loki-uid", token)
    lines = loki.flatten_streams(loki.query_range('{device_id="edge-003"}', start, end))
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from wavebreak_clients._http import HttpError, request


class QueryError(Exception):
    """Raised when a Prometheus/Loki response has status != "success"."""

    def __init__(self, status: str, error_type: str | None, error: str | None) -> None:
        self.status = status
        self.error_type = error_type
        self.error = error
        super().__init__(f"query status={status!r} errorType={error_type!r} error={error!r}")


def _to_epoch_seconds(value: datetime | float | str) -> float:
    """Convert a datetime, epoch number, or RFC3339 string to epoch seconds.

    Naive datetimes are treated as UTC. Strings that parse as a plain
    number are treated as an epoch already; otherwise they are parsed as
    RFC3339 (accepting a trailing "Z").
    """
    if isinstance(value, datetime):
        dt = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return dt.timestamp()
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            dt = datetime.fromisoformat(value)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return dt.timestamp()
    raise TypeError(f"unsupported time value: {value!r}")


def _to_epoch_ns(value: datetime | float | str) -> int:
    """Convert a datetime, epoch number, or RFC3339 string to epoch nanoseconds."""
    return round(_to_epoch_seconds(value) * 1_000_000_000)


class _GrafanaProxyMixin:
    """Shared base_url / Grafana-proxy plumbing for Prometheus and Loki."""

    def __init__(
        self,
        base_url: str,
        *,
        grafana_url: str | None = None,
        datasource_uid: str | None = None,
        grafana_token: str | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._grafana_proxy_base: str | None = None
        self._bearer: str | None = None
        if grafana_url is not None:
            if datasource_uid is None or grafana_token is None:
                raise ValueError("grafana_url requires datasource_uid and grafana_token")
            self._grafana_proxy_base = (
                f"{grafana_url.rstrip('/')}/api/datasources/proxy/uid/{datasource_uid}"
            )
            self._bearer = grafana_token

    @classmethod
    def from_grafana(cls, grafana_url: str, datasource_uid: str, token: str) -> Any:
        """Build a client that routes requests through a Grafana datasource proxy."""
        return cls(
            base_url="",
            grafana_url=grafana_url,
            datasource_uid=datasource_uid,
            grafana_token=token,
        )

    def _url(self, path: str) -> str:
        root = self._grafana_proxy_base if self._grafana_proxy_base is not None else self._base_url
        return f"{root}{path}"

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """GET and decode the JSON body.

        Prometheus and Loki both report query errors as a JSON body with a
        "status" field, sometimes alongside a non-2xx HTTP status (e.g. 400
        for bad_data, 422 for execution errors). Callers use this field
        (via QueryError, raised by each subclass) rather than the HTTP
        status alone. A response without a usable JSON "status" field falls
        back to a plain HttpError.
        """
        url = self._url(path)
        status, _headers, body = request("GET", url, params=params, bearer=self._bearer)
        try:
            parsed = json.loads(body.decode("utf-8")) if body else None
        except (json.JSONDecodeError, UnicodeDecodeError):
            parsed = None
        if isinstance(parsed, dict) and "status" in parsed:
            return parsed
        if status >= 400:
            raise HttpError(status, url, body.decode("utf-8", errors="replace"))
        return parsed if parsed is not None else {}


class Prometheus(_GrafanaProxyMixin):
    """Client for the Prometheus HTTP API (direct, or via Grafana proxy)."""

    def __init__(
        self,
        base_url: str = "http://localhost:9090",
        *,
        grafana_url: str | None = None,
        datasource_uid: str | None = None,
        grafana_token: str | None = None,
    ) -> None:
        super().__init__(
            base_url,
            grafana_url=grafana_url,
            datasource_uid=datasource_uid,
            grafana_token=grafana_token,
        )

    def _check_status(self, response: dict[str, Any]) -> None:
        status = response.get("status")
        if status != "success":
            raise QueryError(status, response.get("errorType"), response.get("error"))

    def _query_result(self, response: dict[str, Any]) -> list[dict[str, Any]]:
        """Extract data.result (vector/matrix shape: query, query_range)."""
        self._check_status(response)
        return response.get("data", {}).get("result", [])

    def _data(self, response: dict[str, Any]) -> list[Any]:
        """Extract data (flat list shape: series, label values)."""
        self._check_status(response)
        return response.get("data", [])

    def query(
        self, promql: str, time: datetime | float | str | None = None
    ) -> list[dict[str, Any]]:
        """Run an instant PromQL query. Returns data.result (a vector/scalar list)."""
        params: dict[str, Any] = {"query": promql}
        if time is not None:
            params["time"] = _to_epoch_seconds(time)
        response = self._get("/api/v1/query", params)
        return self._query_result(response)

    def query_range(
        self,
        promql: str,
        start: datetime | float | str,
        end: datetime | float | str,
        step: str = "15s",
    ) -> list[dict[str, Any]]:
        """Run a ranged PromQL query. Returns data.result (a matrix list)."""
        params = {
            "query": promql,
            "start": _to_epoch_seconds(start),
            "end": _to_epoch_seconds(end),
            "step": step,
        }
        response = self._get("/api/v1/query_range", params)
        return self._query_result(response)

    def series(
        self,
        match: list[str],
        start: datetime | float | str | None = None,
        end: datetime | float | str | None = None,
    ) -> list[dict[str, Any]]:
        """List time series (label sets) matching the given match[] selectors."""
        params: dict[str, Any] = {"match[]": match}
        if start is not None:
            params["start"] = _to_epoch_seconds(start)
        if end is not None:
            params["end"] = _to_epoch_seconds(end)
        response = self._get("/api/v1/series", params)
        return self._data(response)

    def label_values(self, label: str) -> list[str]:
        """List known values for a label name."""
        response = self._get(f"/api/v1/label/{label}/values")
        return self._data(response)


class Loki(_GrafanaProxyMixin):
    """Client for the Loki HTTP API (direct, or via Grafana proxy)."""

    def __init__(
        self,
        base_url: str = "http://localhost:3100",
        *,
        grafana_url: str | None = None,
        datasource_uid: str | None = None,
        grafana_token: str | None = None,
    ) -> None:
        super().__init__(
            base_url,
            grafana_url=grafana_url,
            datasource_uid=datasource_uid,
            grafana_token=grafana_token,
        )

    def _check_status(self, response: dict[str, Any]) -> None:
        status = response.get("status")
        if status != "success":
            raise QueryError(status, response.get("errorType"), response.get("error"))

    def _query_result(self, response: dict[str, Any]) -> list[dict[str, Any]]:
        """Extract data.result (streams/vector shape: query_range, query)."""
        self._check_status(response)
        return response.get("data", {}).get("result", [])

    def _data(self, response: dict[str, Any]) -> list[Any]:
        """Extract data (flat list shape: labels, label values)."""
        self._check_status(response)
        return response.get("data", [])

    def query_range(
        self,
        logql: str,
        start: datetime | float | str,
        end: datetime | float | str,
        limit: int = 500,
        direction: str = "backward",
    ) -> list[dict[str, Any]]:
        """Run a ranged LogQL query. Returns data.result (a list of streams)."""
        params = {
            "query": logql,
            "start": _to_epoch_ns(start),
            "end": _to_epoch_ns(end),
            "limit": limit,
            "direction": direction,
        }
        response = self._get("/loki/api/v1/query_range", params)
        return self._query_result(response)

    def query(
        self,
        logql: str,
        time: datetime | float | str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Run an instant LogQL query (for metric queries, e.g. count_over_time)."""
        params: dict[str, Any] = {"query": logql, "limit": limit}
        if time is not None:
            params["time"] = _to_epoch_ns(time)
        response = self._get("/loki/api/v1/query", params)
        return self._query_result(response)

    def labels(self) -> list[str]:
        """List known label names."""
        response = self._get("/loki/api/v1/labels")
        return self._data(response)

    def label_values(self, label: str) -> list[str]:
        """List known values for a label name."""
        response = self._get(f"/loki/api/v1/label/{label}/values")
        return self._data(response)

    @staticmethod
    def flatten_streams(result: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Flatten a Loki streams result into a flat, time-sorted line list.

        Input: [{"stream": {labels}, "values": [[ts_ns_str, line], ...]}, ...]
        Output: [{"ts": ts_ns (int), "labels": {...}, "line": str}, ...] sorted by ts.
        """
        lines: list[dict[str, Any]] = []
        for stream in result:
            labels = stream.get("stream", {})
            for ts_str, line in stream.get("values", []):
                lines.append({"ts": int(ts_str), "labels": labels, "line": line})
        lines.sort(key=lambda entry: entry["ts"])
        return lines
