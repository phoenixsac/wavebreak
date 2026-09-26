"""Deterministic in-memory lab controller for rehearsals (`FLEET_LAB_MOCK=1`) and tests.

All observable values are computed from the injected clock, so a simulated clock makes
rehearsal windows instant. Scenarios per `version[:hw_rev]`; see docs/agent-design.md 10.1.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass

SCENARIOS = ("healthy", "leak", "crash", "install_fail")
MB = 1024 * 1024
SAMPLE_INTERVAL_S = 15
BASE_BYTES = 200 * MB
LEAK_START_S = 180
LEAK_MB_PER_MIN = 10
OOM_FIRST_S = 240
OOM_EVERY_S = 60
CRASH_FIRST_S = 45
CRASH_EVERY_S = 20


def parse_scenarios(text: str | None) -> dict[str, str]:
    """Parse a `FLEET_LAB_MOCK_SCENARIOS` JSON object ({"v1.3": "crash", "v1.2:B": "leak"})."""
    if text is None or not text.strip():
        return {}
    data = json.loads(text)
    if not isinstance(data, dict):
        raise TypeError("scenarios must be a JSON object")
    return _validated(data)


def _validated(data: dict) -> dict[str, str]:
    for key, value in data.items():
        if value not in SCENARIOS:
            raise ValueError(f"unknown scenario {value!r} for {key!r}; expected one of {SCENARIOS}")
    return {str(k): str(v) for k, v in data.items()}


@dataclass
class _Device:
    hw_rev: str
    created_version: str
    created_at: float
    installed_version: str | None = None
    installed_at: float = 0.0
    scenario: str = "healthy"


class MockLab:
    """LabBackend simulating healthy, leaking, crashing and install-failing firmware."""

    def __init__(
        self,
        scenarios: dict[str, str] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.scenarios = _validated(scenarios or {})
        self.clock = clock
        self._devices: dict[str, _Device] = {}
        self._counter = 0

    def now(self) -> float:
        return self.clock()

    def scenario_for(self, version: str, hw_rev: str) -> str:
        """Scenario lookup: "<version>:<hw_rev>", then "<version>", default healthy."""
        return self.scenarios.get(f"{version}:{hw_rev}") or self.scenarios.get(version) or "healthy"

    def _get(self, device_id: str) -> _Device:
        try:
            return self._devices[device_id]
        except KeyError:
            raise KeyError(f"unknown lab device {device_id}") from None

    def create_device(self, hw_rev: str, version: str) -> str:
        self._counter += 1
        device_id = f"mock-lab-{self._counter:03d}"
        self._devices[device_id] = _Device(hw_rev, version, self.clock())
        return device_id

    def install(self, device_id: str, version: str) -> dict:
        dev = self._get(device_id)
        dev.installed_version = version
        dev.installed_at = self.clock()
        dev.scenario = self.scenario_for(version, dev.hw_rev)
        result = "failure" if dev.scenario == "install_fail" else "success"
        return {"device_id": device_id, "version": version, "result": result}

    def destroy(self, device_id: str) -> None:
        self._get(device_id)
        del self._devices[device_id]

    def summary(self, device_id: str) -> dict:
        """Lab summary computed from elapsed time since install (or since creation)."""
        dev = self._get(device_id)
        if dev.installed_version is None:
            return self._render(device_id, dev.created_version, "healthy", dev.created_at, "none", "")
        if dev.scenario == "install_fail":
            return self._render(
                device_id,
                dev.created_version,
                "install_fail",
                dev.installed_at,
                "failure",
                "mock install failure",
            )
        return self._render(
            device_id, dev.installed_version, dev.scenario, dev.installed_at, "success", "mock install"
        )

    def _render(
        self, device_id: str, version: str, scenario: str, t0: float, result: str, detail: str
    ) -> dict:
        elapsed = max(0.0, self.clock() - t0)
        restarts, oom = _counters(scenario, elapsed)
        unit = "failed" if scenario == "crash" and restarts > 0 else "active"
        return {
            "device_id": device_id,
            "fw_version": version,
            "unit_state": unit,
            "restarts": restarts,
            "oom_kills": oom,
            "memory_samples": _samples(scenario, t0, elapsed),
            "last_install": {"result": result, "detail": detail},
        }


def _memory_bytes(scenario: str, elapsed: float) -> int:
    if scenario == "leak" and elapsed > LEAK_START_S:
        return int(BASE_BYTES + LEAK_MB_PER_MIN * MB * (elapsed - LEAK_START_S) / 60)
    return BASE_BYTES


def _samples(scenario: str, t0: float, elapsed: float) -> list[dict]:
    count = int(elapsed // SAMPLE_INTERVAL_S) + 1
    return [
        {"t": t0 + k * SAMPLE_INTERVAL_S, "bytes": _memory_bytes(scenario, k * SAMPLE_INTERVAL_S)}
        for k in range(count)
    ]


def _counters(scenario: str, elapsed: float) -> tuple[int, int]:
    """(restarts, oom_kills) at `elapsed` seconds after install."""
    if scenario == "leak" and elapsed >= OOM_FIRST_S:
        oom = 1 + int((elapsed - OOM_FIRST_S) // OOM_EVERY_S)
        return oom, oom
    if scenario == "crash" and elapsed >= CRASH_FIRST_S:
        return 1 + int((elapsed - CRASH_FIRST_S) // CRASH_EVERY_S), 0
    return 0, 0
