"""Environment-driven settings for the fleet MCP server (docs/agent-design.md section 10.1)."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field, fields, replace

from .models import Thresholds

_TRUE = {"1", "true", "yes", "on"}


def _bool(value: str) -> bool:
    """Parse a 0/1/true/false style flag."""
    return value.strip().lower() in _TRUE


def thresholds_from_env(env: Mapping[str, str]) -> Thresholds:
    """Override Thresholds defaults from `FLEET_TH_<UPPERCASE_FIELD>` variables."""
    base = Thresholds()
    changes = {}
    for f in fields(Thresholds):
        raw = env.get(f"FLEET_TH_{f.name.upper()}")
        if raw is not None and raw != "":
            changes[f.name] = type(getattr(base, f.name))(raw)
    return replace(base, **changes)


@dataclass(frozen=True)
class Settings:
    """All server settings; build with `Settings.from_env()`."""

    hawkbit_url: str = "http://localhost:8080"
    hawkbit_username: str = "admin"
    hawkbit_password: str = "admin"
    prometheus_url: str = "http://localhost:9090"
    loki_url: str = "http://localhost:3100"
    lab_url: str = "http://localhost:8090"
    lab_api_token: str = ""
    ledger_path: str = "run/fleet-ledger.sqlite"
    mcp_host: str = "127.0.0.1"
    mcp_port: int = 8792
    lab_mock: bool = False
    lab_mock_scenarios: dict = field(default_factory=dict)
    evidence_max_age_s: float = 900.0
    lab_max_minutes: float = 5.0
    lab_parallel: int = 2  # lab devices alive at once; hw_revs beyond this are rehearsed in later batches
    observe_max_minutes: float = 4.0
    poll_interval_s: float = 15.0
    bundles_dir: str = "sim/bundles"
    ds_name: str = "wavebreak-app"
    thresholds: Thresholds = field(default_factory=Thresholds)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        """Read settings from `env` (default os.environ); unset or empty vars use defaults."""
        e = os.environ if env is None else env
        d = cls()

        def get(name: str, default, cast=str):
            raw = e.get(name)
            return default if raw is None or raw == "" else cast(raw)

        scenarios = get("FLEET_LAB_MOCK_SCENARIOS", d.lab_mock_scenarios, json.loads)
        if not isinstance(scenarios, dict):
            raise TypeError("FLEET_LAB_MOCK_SCENARIOS must be a JSON object")
        return cls(
            hawkbit_url=get("HAWKBIT_URL", d.hawkbit_url),
            hawkbit_username=get("HAWKBIT_USERNAME", d.hawkbit_username),
            hawkbit_password=get("HAWKBIT_PASSWORD", d.hawkbit_password),
            prometheus_url=get("PROMETHEUS_URL", d.prometheus_url),
            loki_url=get("LOKI_URL", d.loki_url),
            lab_url=get("LAB_CONTROLLER_URL", d.lab_url),
            lab_api_token=get("LAB_API_TOKEN", d.lab_api_token),
            ledger_path=get("FLEET_LEDGER_PATH", d.ledger_path),
            mcp_host=get("FLEET_MCP_HOST", d.mcp_host),
            mcp_port=get("FLEET_MCP_PORT", d.mcp_port, int),
            lab_mock=get("FLEET_LAB_MOCK", d.lab_mock, _bool),
            lab_mock_scenarios=scenarios,
            evidence_max_age_s=get("FLEET_EVIDENCE_MAX_AGE_S", d.evidence_max_age_s, float),
            lab_max_minutes=get("FLEET_LAB_MAX_MINUTES", d.lab_max_minutes, float),
            lab_parallel=max(1, get("FLEET_LAB_PARALLEL", d.lab_parallel, int)),
            observe_max_minutes=get("FLEET_OBSERVE_MAX_MINUTES", d.observe_max_minutes, float),
            poll_interval_s=get("FLEET_POLL_INTERVAL_S", d.poll_interval_s, float),
            bundles_dir=get("BUNDLES_DIR", d.bundles_dir),
            ds_name=d.ds_name,
            thresholds=thresholds_from_env(e),
        )
