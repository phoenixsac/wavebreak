"""Test doubles for the fleet service layer: simulated time, field and metrics backends, fleet builder."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent.fleet_mcp.config import Settings
from agent.fleet_mcp.fleet import Fleet
from agent.fleet_mcp.ledger import Ledger
from agent.fleet_mcp.mock_lab import MockLab
from agent.fleet_mcp.models import Device, DeviceStats

BUNDLES_DIR = Path(__file__).resolve().parents[3] / "sim" / "bundles"
KNOWN_VERSIONS = ("v1.0", "v1.1", "v1.2", "v1.3", "v1.4")


class FakeTime:
    """Simulated epoch clock; only `sleep` and `advance` move it."""

    def __init__(self, start: float = 1_800_000_000.0) -> None:
        self.t = float(start)
        self.sleeps: list[float] = []

    def now(self) -> float:
        """Epoch seconds."""
        return self.t

    def datetime_now(self) -> datetime:
        """Aware UTC datetime of `now()`."""
        return datetime.fromtimestamp(self.t, UTC)

    def advance(self, seconds: float) -> None:
        self.t += seconds

    async def sleep(self, seconds: float) -> None:
        """Advance the clock instead of waiting."""
        self.sleeps.append(seconds)
        self.t += seconds


class FakeField:
    """In-memory FieldBackend that records every mutating call.

    `errors` maps a method name to an exception raised whenever that method is called.
    `installed` overrides `installed_versions` per device; `statuses` overrides `action_status`;
    `failures` lists device ids reported by `install_failures`; `assign_fail` lists device ids whose
    `assign` raises (or "*" for all).
    """

    def __init__(self, devices: list[Device], known_versions: tuple[str, ...] = KNOWN_VERSIONS) -> None:
        self.devices: dict[str, Device] = {d.id: d for d in devices}
        self.known_versions = set(known_versions)
        self.errors: dict[str, Exception] = {}
        self.installed: dict[str, str | None] = {}
        self.statuses: dict[str, str] = {}
        self.failures: set[str] = set()
        self.assign_fail: set[str] = set()
        self.start_wave_calls: list[tuple[str, str, list[str]]] = []
        self.stop_calls: list[list[int]] = []
        self.assign_calls: list[tuple[str, str]] = []
        self._rollout_seq = 0

    def _maybe_raise(self, name: str) -> None:
        if name in self.errors:
            raise self.errors[name]

    def inventory(self) -> list[Device]:
        self._maybe_raise("inventory")
        return sorted(self.devices.values(), key=lambda d: d.id)

    def distribution_set_id(self, version: str) -> int:
        self._maybe_raise("distribution_set_id")
        if version not in self.known_versions:
            raise LookupError(f"no distribution set {version}")
        return 100 + sorted(self.known_versions).index(version)

    def start_wave(self, name: str, version: str, device_ids: list[str]) -> dict:
        self._maybe_raise("start_wave")
        self.start_wave_calls.append((name, version, list(device_ids)))
        self._rollout_seq += 1
        return {"rollout_id": self._rollout_seq, "targets": list(device_ids)}

    def stop_rollouts(self, rollout_ids: list[int]) -> list[dict]:
        self._maybe_raise("stop_rollouts")
        self.stop_calls.append(list(rollout_ids))
        return [{"rollout_id": r, "result": "stopped"} for r in rollout_ids]

    def assign(self, device_id: str, version: str) -> dict:
        self._maybe_raise("assign")
        if "*" in self.assign_fail or device_id in self.assign_fail:
            raise RuntimeError(f"assign refused for {device_id}")
        self.assign_calls.append((device_id, version))
        old = self.devices[device_id]
        self.devices[device_id] = Device(old.id, old.hw_rev, old.region, version)
        return {"result": "assigned"}

    def installed_versions(self, device_ids: list[str]) -> dict[str, str | None]:
        self._maybe_raise("installed_versions")
        return {d: self.installed.get(d, self.devices[d].version) for d in device_ids}

    def action_status(self, device_ids: list[str]) -> dict[str, str]:
        self._maybe_raise("action_status")
        return {d: self.statuses.get(d, "finished") for d in device_ids}

    def install_failures(self, device_ids: list[str], since_epoch_s: float) -> list[str]:
        self._maybe_raise("install_failures")
        return [d for d in device_ids if d in self.failures]


class FakeMetrics:
    """MetricsBackend returning healthy stats unless overridden per (device_id, fw_version).

    `overrides[(id, version)]` holds DeviceStats field overrides. `push_call(...)` queues one-shot
    overrides applied to the next `device_stats` call only (keyed by device id). `unavailable` makes
    every call raise `MetricsUnavailable`; `empty` makes every device report no data (not installed).
    """

    def __init__(self) -> None:
        self.unavailable: Exception | None = None
        self.empty = False
        self.overrides: dict[tuple[str, str], dict[str, Any]] = {}
        self.calls: list[dict] = []
        self._queued: list[dict[str, dict[str, Any]]] = []

    def set(self, device_id: str, fw_version: str, **fields: Any) -> None:
        """Override DeviceStats fields for a device at a firmware version."""
        self.overrides.setdefault((device_id, fw_version), {}).update(fields)

    def push_call(self, per_device: dict[str, dict[str, Any]]) -> None:
        """Queue overrides (device id -> fields) for the next call only."""
        self._queued.append(per_device)

    def device_stats(
        self, devices: list[Device], fw_version: str, start: float, end: float
    ) -> dict[str, DeviceStats]:
        self.calls.append(
            {"ids": [d.id for d in devices], "fw_version": fw_version, "start": start, "end": end}
        )
        if self.unavailable is not None:
            raise self.unavailable
        once = self._queued.pop(0) if self._queued else {}
        out = {}
        for d in devices:
            fields: dict[str, Any] = {
                "installed": True,
                "samples": 20,
                "window_s": end - start,
                "mem_slope_mb_per_min": 0.0,
                "restarts": 0,
                "oom_kills": 0,
                "fps_median": 30.0,
            }
            if self.empty:
                fields.update(
                    installed=False, samples=0, window_s=0.0, mem_slope_mb_per_min=None, fps_median=None
                )
            fields.update(self.overrides.get((d.id, fw_version), {}))
            fields.update(once.get(d.id, {}))
            out[d.id] = DeviceStats(device_id=d.id, hw_rev=d.hw_rev, **fields)
        return out


class TrackingLab(MockLab):
    """MockLab that counts live devices and can inject failures.

    `fail_create_on`: 1-based create_device call number that raises. `fail_summary`: raise on summary.
    `max_samples`: keep only the first N memory samples in every summary (a too-short soak).
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.max_alive = 0
        self.create_calls = 0
        self.destroyed: list[str] = []
        self.fail_create_on: int | None = None
        self.fail_summary = False
        self.max_samples: int | None = None

    @property
    def alive(self) -> int:
        return len(self._devices)

    def create_device(self, hw_rev: str, version: str) -> str:
        self.create_calls += 1
        if self.fail_create_on == self.create_calls:
            raise RuntimeError("lab controller unreachable")
        device_id = super().create_device(hw_rev, version)
        self.max_alive = max(self.max_alive, self.alive)
        return device_id

    def summary(self, device_id: str) -> dict:
        if self.fail_summary:
            raise RuntimeError("lab summary failed")
        out = super().summary(device_id)
        if self.max_samples is not None:
            out["memory_samples"] = out["memory_samples"][: self.max_samples]
        return out

    def destroy(self, device_id: str) -> None:
        super().destroy(device_id)
        self.destroyed.append(device_id)


def make_devices(n_a: int = 3, n_b: int = 3, version: str = "v1.1") -> list[Device]:
    """`n_a` rev A and `n_b` rev B devices (ids a1.., b1..), regions alternating eu/us."""
    out = []
    for hw, n in (("A", n_a), ("B", n_b)):
        for i in range(1, n + 1):
            out.append(Device(f"{hw.lower()}{i}", hw, "eu" if i % 2 else "us", version))
    return out


def make_settings(tmp_path: Path, **overrides: Any) -> Settings:
    """Settings for tests: real bundles dir, tmp ledger, standard windows."""
    base: dict[str, Any] = {
        "poll_interval_s": 15,
        "evidence_max_age_s": 900,
        "lab_max_minutes": 5,
        "observe_max_minutes": 4,
        "bundles_dir": str(BUNDLES_DIR),
        "ledger_path": str(tmp_path / "ledger.sqlite"),
        "lab_mock": True,
    }
    base.update(overrides)
    return Settings(**base)


@dataclass
class Env:
    """A wired Fleet with all its doubles."""

    fleet: Fleet
    time: FakeTime
    field: FakeField
    metrics: FakeMetrics
    lab: TrackingLab
    ledger: Ledger
    settings: Settings
    extra: dict = field(default_factory=dict)


def build_fleet(
    tmp_path: Path,
    devices: list[Device] | None = None,
    *,
    scenarios: dict[str, str] | None = None,
    **settings_overrides: Any,
) -> Env:
    """Fleet on a tmp ledger, real MockLab and FakeTime as ledger/fleet/lab clock and every sleep."""
    ft = FakeTime()
    settings = make_settings(tmp_path, **settings_overrides)
    ledger = Ledger(settings.ledger_path, clock=ft.datetime_now)
    fld = FakeField(devices if devices is not None else make_devices())
    metrics = FakeMetrics()
    the_lab = TrackingLab(scenarios or {}, clock=ft.now)
    fleet = Fleet(
        settings, ledger, fld, metrics, the_lab, clock=ft.datetime_now, sleep=ft.sleep, lab_sleep=ft.sleep
    )
    return Env(fleet, ft, fld, metrics, the_lab, ledger, settings)
