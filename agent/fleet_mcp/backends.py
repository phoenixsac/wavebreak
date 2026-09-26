"""Backend protocols and real implementations for the fleet MCP server.

Three seams keep the tool logic testable: `FieldBackend` (hawkBit), `MetricsBackend`
(Prometheus + Loki) and `LabBackend` (lab controller). Stdlib only; HTTP goes through
`wavebreak_clients`. See docs/agent-design.md sections 10.1 and 12.
"""

from __future__ import annotations

import math
import re
import time
import urllib.parse
from collections.abc import Callable
from datetime import UTC, datetime
from itertools import pairwise
from statistics import median
from typing import Any, Protocol

from wavebreak_clients._http import HttpError, request_json

from .assess import slope_mb_per_min
from .models import Device, DeviceStats

# Rollout states from which hawkBit accepts a stop.
_STOPPABLE = frozenset({"creating", "ready", "starting", "running", "paused", "waiting_for_approval"})
_WAVE_DEAD = frozenset({"deleted", "finished"})
_OOM_LINE = "Failed with result 'oom-kill'"


class MetricsUnavailable(Exception):
    """Prometheus or Loki could not be queried; evidence built on it must be inconclusive."""

    def __init__(self, source: str, detail: str) -> None:
        super().__init__(source, detail)
        self.source = source
        self.detail = detail

    def __str__(self) -> str:
        return f"{self.source} unavailable: {self.detail}"


class FieldBackend(Protocol):
    """Field-fleet operations (hawkBit)."""

    def inventory(self) -> list[Device]: ...
    def distribution_set_id(self, version: str) -> int: ...  # raises LookupError if unknown
    def start_wave(self, name: str, version: str, device_ids: list[str]) -> dict: ...
    def stop_rollouts(self, rollout_ids: list[int]) -> list[dict]: ...
    def assign(self, device_id: str, version: str) -> dict: ...
    def installed_versions(self, device_ids: list[str]) -> dict[str, str | None]: ...
    def action_status(self, device_ids: list[str]) -> dict[str, str]: ...
    def install_failures(self, device_ids: list[str], since_epoch_s: float) -> list[str]: ...


class MetricsBackend(Protocol):
    """Per-device metrics over a window.

    `device_stats` may raise `MetricsUnavailable` when the metrics source cannot be queried; callers
    must treat that as missing evidence (inconclusive), never as healthy.
    """

    def device_stats(
        self, devices: list[Device], fw_version: str, start: float, end: float
    ) -> dict[str, DeviceStats]: ...


class LabBackend(Protocol):
    """Lab controller operations (rehearsal devices)."""

    def now(self) -> float: ...  # lab clock, epoch seconds (may be simulated)
    def create_device(self, hw_rev: str, version: str) -> str: ...
    def install(self, device_id: str, version: str) -> dict: ...
    def summary(self, device_id: str) -> dict: ...
    def destroy(self, device_id: str) -> None: ...


# --------------------------------------------------------------------------- hawkBit


class HawkbitBackend:
    """FieldBackend over `wavebreak_clients.hawkbit.HawkbitClient` (or a fake)."""

    def __init__(
        self,
        client: Any,
        ds_name: str = "wavebreak-app",
        *,
        sleep: Callable[[float], None] = time.sleep,
        ready_timeout_s: float = 60,
    ) -> None:
        self.client = client
        self.ds_name = ds_name
        self._sleep = sleep
        self.ready_timeout_s = ready_timeout_s

    def inventory(self) -> list[Device]:
        """All targets with hw_rev/region attributes and installed version ("none" if unknown)."""
        devices = []
        for target in self.client.list_targets(limit=500):
            tid = target["controllerId"]
            attrs = self._attributes(tid)
            devices.append(
                Device(
                    id=tid,
                    hw_rev=str(attrs.get("hw_rev") or "unknown"),
                    region=str(attrs.get("region") or "unknown"),
                    version=self._installed_version(tid) or "none",
                )
            )
        return sorted(devices, key=lambda d: d.id)

    def _attributes(self, target_id: str) -> dict:
        try:
            return self.client.target_attributes(target_id) or {}
        except HttpError as exc:
            if exc.status == 404:
                return {}
            raise

    def _installed_version(self, target_id: str) -> str | None:
        try:
            ds = self.client.installed_distribution_set(target_id)
        except HttpError as exc:
            if exc.status == 404:
                return None
            raise
        return (ds or {}).get("version") or None

    def distribution_set_id(self, version: str) -> int:
        """hawkBit distribution set id for `version`; LookupError if unknown."""
        return self.client.distribution_set_id(self.ds_name, version)

    def start_wave(self, name: str, version: str, device_ids: list[str]) -> dict:
        """Create a one-group rollout for the devices, wait until ready, start it."""
        for did in device_ids:
            if not did or re.search(r"[\s,()]", did):
                raise ValueError(f"device id not usable in a FIQL filter: {did!r}")
        ds_id = self.distribution_set_id(version)
        # TODO(verify): FIQL `controllerId=in=(a,b)` against the live hawkBit.
        created = self.client.create_wave(
            name=name,
            distribution_set_id=ds_id,
            target_filter_query=f"controllerId=in=({','.join(device_ids)})",
            description=f"wave {name}: {version} to {len(device_ids)} device(s)",
        )
        rollout_id = int(created["id"])
        self._wait_ready(rollout_id)
        self.client.start_rollout(rollout_id)
        return {"rollout_id": rollout_id, "targets": list(device_ids)}

    def _wait_ready(self, rollout_id: int) -> None:
        waited = 0.0
        while True:
            status = str(self.client.rollout(rollout_id).get("status", "")).lower()
            if status == "ready":
                return
            if status in _WAVE_DEAD:
                raise RuntimeError(f"rollout {rollout_id} is {status}; not started")
            if waited >= self.ready_timeout_s:
                raise RuntimeError(
                    f"rollout {rollout_id} not ready after {self.ready_timeout_s:g}s (status {status}); not started"
                )
            self._sleep(1)
            waited += 1

    def stop_rollouts(self, rollout_ids: list[int]) -> list[dict]:
        """Stop each rollout that is still stoppable; never raises."""
        results = []
        for rid in rollout_ids:
            try:
                status = str(self.client.rollout(rid).get("status", "")).lower()
                if status in _STOPPABLE:
                    self.client.stop_rollout(rid)
                    result = "stopped"
                else:
                    result = f"skipped: {status}"
            except Exception as exc:  # noqa: BLE001 - contract: report, never raise
                result = f"error: {exc}"
            results.append({"rollout_id": rid, "result": result})
        return results

    def assign(self, device_id: str, version: str) -> dict:
        """Forced assignment of `version` to one device."""
        self.client.assign_distribution_set(device_id, self.distribution_set_id(version))
        return {"device_id": device_id, "version": version, "result": "assigned"}

    def installed_versions(self, device_ids: list[str]) -> dict[str, str | None]:
        """Installed distribution set version per device (None if nothing installed)."""
        return {did: self._installed_version(did) for did in device_ids}

    def _actions(self, device_id: str) -> list[dict]:
        url = f"{self.client.api}/targets/{urllib.parse.quote(device_id, safe='')}/actions"
        result = request_json(
            "GET",
            url,
            params={"sort": "id:DESC", "limit": 5},
            auth=self.client.auth,
            timeout=self.client.timeout,
        )
        return (result or {}).get("content", [])

    def action_status(self, device_ids: list[str]) -> dict[str, str]:
        """Latest hawkBit action status per device; "none" if the device has no action."""
        out = {}
        for did in device_ids:
            actions = self._actions(did)
            if not actions:
                out[did] = "none"
            else:
                out[did] = str(actions[0].get("status") or actions[0].get("detailStatus") or "unknown")
        return out

    def install_failures(self, device_ids: list[str], since_epoch_s: float) -> list[str]:
        """Devices with a failed action created at or after `since_epoch_s`."""
        failed = []
        for did in device_ids:
            for act in self._actions(did):
                created_s = float(act.get("createdAt") or 0) / 1000.0
                if created_s >= since_epoch_s and _is_failed(act):
                    failed.append(did)
                    break
        return failed


def _is_failed(action: dict) -> bool:
    # TODO(verify): exact failed-action values (status / detailStatus) against the live hawkBit.
    return any(str(action.get(k) or "").lower() == "error" for k in ("status", "detailStatus"))


# ------------------------------------------------------------------- Prometheus / Loki


def _re_escape(value: str) -> str:
    """Escape RE2 metacharacters in a literal."""
    return re.sub(r"([\\.^$*+?()\[\]{}|])", r"\\\1", value)


def _quote(value: str) -> str:
    """Double-quoted PromQL/LogQL string literal."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _id_regex(device_ids: list[str]) -> str:
    """Quoted, escaped alternation matching exactly the given device ids."""
    return _quote("|".join(_re_escape(d) for d in device_ids))


def _floats(series: dict) -> list[tuple[float, float]]:
    """Matrix series values as (t, value) pairs, dropping NaN/unparseable points."""
    points = []
    for t, raw in series.get("values", []):
        try:
            v = float(raw)
        except (TypeError, ValueError):
            continue
        if not math.isnan(v):
            points.append((float(t), v))
    return points


def _by_device(result: list[dict]) -> dict[str, list[list[tuple[float, float]]]]:
    out: dict[str, list[list[tuple[float, float]]]] = {}
    for series in result:
        did = series.get("metric", {}).get("device_id")
        if did is not None:
            out.setdefault(did, []).append(_floats(series))
    return out


def _counter_increase(points: list[tuple[float, float]]) -> int:
    """Sum of positive steps; counter resets (negative steps) add nothing."""
    total = 0.0
    for (_, a), (_, b) in pairwise(points):
        if b > a:
            total += b - a
    return round(total)


class PromBackend:
    """MetricsBackend over Prometheus (metrics) and Loki (OOM log lines)."""

    def __init__(self, prometheus: Any, loki: Any, *, step: str = "15s") -> None:
        self.prometheus = prometheus
        self.loki = loki
        self.step = step

    def device_stats(
        self, devices: list[Device], fw_version: str, start: float, end: float
    ) -> dict[str, DeviceStats]:
        """Stats for every requested device (three Prometheus queries, one Loki query).

        Raises `MetricsUnavailable` if Prometheus or Loki fails; a device without series gets
        `installed=False` stats.
        """
        if not devices:
            return {}
        ids = [d.id for d in devices]
        sel = f"{{device_id=~{_id_regex(ids)},fw_version={_quote(fw_version)}}}"

        def matrix(metric: str) -> dict[str, list[list[tuple[float, float]]]]:
            try:
                result = self.prometheus.query_range(f"{metric}{sel}", start, end, step=self.step)
                return _by_device(result)
            except Exception as exc:
                raise MetricsUnavailable("prometheus", f"{type(exc).__name__}: {exc}") from exc

        memory = matrix("wavebreak_app_cgroup_memory_bytes")
        restarts = matrix("wavebreak_app_restarts_total")
        fps = matrix("wavebreak_app_fps")
        oom = self._oom_kills(ids, start, end)
        return {
            d.id: _device_stats(
                d, memory.get(d.id), restarts.get(d.id, []), fps.get(d.id, []), oom.get(d.id, 0)
            )
            for d in devices
        }

    def _oom_kills(self, ids: list[str], start: float, end: float) -> dict[str, int]:
        """OOM kill lines per device; a Loki failure raises MetricsUnavailable, an empty result means none."""
        window = max(1, int(end - start))
        logql = (
            f"sum by (device_id) (count_over_time({{device_id=~{_id_regex(ids)}}} "
            f"|= {_quote(_OOM_LINE)} [{window}s]))"
        )
        try:
            result = self.loki.query(logql, end)
            return {
                r["metric"]["device_id"]: int(float(r["value"][1]))
                for r in result
                if "device_id" in r.get("metric", {})
            }
        except Exception as exc:
            raise MetricsUnavailable("loki", f"{type(exc).__name__}: {exc}") from exc


def _device_stats(
    device: Device,
    mem_series: list[list[tuple[float, float]]] | None,
    restart_series: list[list[tuple[float, float]]],
    fps_series: list[list[tuple[float, float]]],
    oom: int,
) -> DeviceStats:
    mem = sorted(p for s in (mem_series or []) for p in s)
    if not mem:
        return DeviceStats(device.id, device.hw_rev, False, 0, 0.0, None, 0, 0, None)
    fps = [v for s in fps_series for _, v in s]
    return DeviceStats(
        device_id=device.id,
        hw_rev=device.hw_rev,
        installed=True,
        samples=len(mem),
        window_s=mem[-1][0] - mem[0][0],
        mem_slope_mb_per_min=slope_mb_per_min(mem),
        restarts=sum(_counter_increase(s) for s in restart_series),
        oom_kills=oom,
        fps_median=median(fps) if fps else None,
    )


# ------------------------------------------------------------------------ lab controller


def _first(d: dict, *keys: str, default: Any = None) -> Any:
    for k in keys:
        if d.get(k) is not None:
            return d[k]
    return default


def _epoch(value: Any) -> float:
    """Epoch seconds from a number or an ISO-8601 string (naive means UTC)."""
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            dt = datetime.fromisoformat(value)
            return (dt if dt.tzinfo else dt.replace(tzinfo=UTC)).timestamp()
    return float(value)


def _sample(item: Any) -> dict | None:
    """One memory sample as {t, bytes}; None for null memory or unparseable items."""
    try:
        if isinstance(item, dict):
            t = _first(item, "ts", "t", "time", "timestamp")
            b = _first(item, "memory_bytes", "bytes", "value")
        else:
            t, b = item[0], item[1]
        if t is None or b is None:
            return None
        return {"t": _epoch(t), "bytes": int(float(b))}
    except (TypeError, ValueError, IndexError, KeyError):
        return None


def _last_install(raw: dict) -> dict:
    """Normalise `last_install_result` ({ok, version, message}) or the older {result, detail}."""
    entry = _first(raw, "last_install_result", "last_install", "install")
    if isinstance(entry, dict):
        if "ok" in entry:
            result = "success" if entry["ok"] else "failure"
        else:
            result = entry.get("result")
        detail = _first(entry, "message", "detail", default="")
        return {"result": result, "detail": str(detail)}
    if isinstance(entry, str) and entry:
        return {"result": entry, "detail": ""}
    return {"result": None, "detail": ""}


def _created_id(resp: Any) -> str:
    """Device id from a create response (dict, wrapped list, or plain list)."""
    if isinstance(resp, dict) and isinstance(resp.get("devices"), list):
        resp = resp["devices"]
    if isinstance(resp, list) and resp:
        resp = resp[0]
    if isinstance(resp, str) and resp:
        return resp
    if isinstance(resp, dict):
        value = _first(resp, "id", "device_id")
        if value:
            return str(value)
    raise RuntimeError(f"lab controller create response has no device id: {resp!r}")


class HttpLab:
    """LabBackend over the lab controller HTTP API (docs/architecture.md, Lab controller API).

    The controller (`lab/controller/app.py`) is work in progress; shapes follow its current code.
    """

    def __init__(self, base_url: str, token: str, *, timeout: float = 30) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _call(self, method: str, path: str, body: Any = None) -> Any:
        return request_json(
            method,
            f"{self.base_url}{path}",
            json_body=body,
            bearer=self.token,
            timeout=self.timeout,
        )

    @staticmethod
    def _path(device_id: str) -> str:
        return f"/lab/devices/{urllib.parse.quote(device_id, safe='')}"

    def now(self) -> float:
        return time.time()

    def create_device(self, hw_rev: str, version: str) -> str:
        resp = self._call("POST", "/lab/devices", {"count": 1, "hw_rev": hw_rev, "version": version})
        return _created_id(resp)

    def install(self, device_id: str, version: str) -> dict:
        """Install `version`; a failed install (HTTP 422) is a result, not an exception."""
        out = {"device_id": device_id, "version": version}
        try:
            resp = self._call("POST", f"{self._path(device_id)}/install", {"version": version})
        except HttpError as exc:
            if exc.status != 422:
                raise
            return {**out, "result": "failure", "detail": exc.body_text}
        extra = {k: v for k, v in resp.items() if k not in out} if isinstance(resp, dict) else {}
        ok = not (isinstance(resp, dict) and resp.get("ok") is False)
        return {**extra, **out, "result": "success" if ok else "failure"}

    def summary(self, device_id: str) -> dict:
        """Lab summary in the LabBackend shape (the controller only adds a trend sample per call)."""
        raw = self._call("GET", f"{self._path(device_id)}/summary")
        raw = raw if isinstance(raw, dict) else {}
        samples = _first(raw, "memory_trend", "memory_samples", "memory", default=[])
        parsed = [s for s in (_sample(i) for i in samples or []) if s is not None]
        return {
            "device_id": str(_first(raw, "id", "device_id", default=device_id)),
            "fw_version": str(_first(raw, "fw_version", "version", default="unknown")),
            "unit_state": str(_first(raw, "unit_state", "unit", "state", default="unknown")),
            "restarts": int(_first(raw, "restarts", "restart_count", default=0)),
            "oom_kills": int(_first(raw, "oom_kills", "oom", "oom_kill_count", default=0)),
            "memory_samples": parsed,
            "last_install": _last_install(raw),
        }

    def destroy(self, device_id: str) -> None:
        self._call("DELETE", self._path(device_id))
