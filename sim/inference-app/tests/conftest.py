"""Shared test helpers: load each bundle's inference_app.py by path (no
package), and build an InferenceApp with injected clock/sleep/paths so tests
never sleep or touch the real filesystem outside tmp_path.
"""

from __future__ import annotations

import importlib.util
import io
import sys
from pathlib import Path
from types import ModuleType

BUNDLES_DIR = Path(__file__).resolve().parents[2] / "bundles"
ALL_VERSIONS = ["v1.0", "v1.1", "v1.2", "v1.3", "v1.4"]
HEALTHY_VERSIONS = ["v1.0", "v1.1", "v1.2", "v1.4"]

# Mirrors sim/device/files/etc/wavebreak/app.conf: devices carry the
# pre-v1.3 key name and never get it migrated.
DEVICE_CONFIG_TEXT = "detection_threshold: 0.6\nroi: full\n"

_MODULE_CACHE: dict[str, ModuleType] = {}


class FakeClock:
    """A callable, manually-advanced clock for deterministic tests."""

    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance(self, dt: float) -> None:
        self.value += dt


def load_bundle_module(version: str) -> ModuleType:
    """Import a bundle's inference_app.py by path under a unique module name."""
    if version in _MODULE_CACHE:
        return _MODULE_CACHE[version]
    path = BUNDLES_DIR / version / "app" / "inference_app.py"
    mod_name = f"wavebreak_inference_app_{version.replace('.', '_')}"
    spec = importlib.util.spec_from_file_location(mod_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    _MODULE_CACHE[version] = module
    return module


def build_app(
    version: str,
    tmp_path: Path,
    *,
    hw_rev: str = "A",
    device_id: str = "edge-001",
    region: str = "us-east",
    clock: FakeClock | None = None,
    device_config_text: str = DEVICE_CONFIG_TEXT,
    boot_time_text: str = "1700000000.0",
    overrides: dict | None = None,
    status_path: Path | None = None,
    cgroup_path: Path | None = None,
    cgroup_root: Path | None = None,
    proc_stat_path: Path | None = None,
):
    """Build (module, app, log_stream) for `version` with everything injected."""
    module = load_bundle_module(version)
    device_conf = tmp_path / "app.conf"
    device_conf.write_text(device_config_text)
    boot_time_file = tmp_path / "boot_time"
    boot_time_file.write_text(boot_time_text)
    log_stream = io.StringIO()

    kwargs = {}
    if status_path is not None:
        kwargs["status_path"] = status_path
    if cgroup_path is not None:
        kwargs["cgroup_path"] = cgroup_path
    if cgroup_root is not None:
        kwargs["cgroup_root"] = cgroup_root
    if proc_stat_path is not None:
        kwargs["proc_stat_path"] = proc_stat_path

    app = module.InferenceApp(
        bundle_dir=BUNDLES_DIR / version,
        metrics_dir=tmp_path / "metrics",
        device_config_path=device_conf,
        boot_time_file=boot_time_file,
        env={"DEVICE_ID": device_id, "HW_REV": hw_rev, "REGION": region},
        clock=clock or FakeClock(),
        sleep=lambda s: None,
        log_stream=log_stream,
        config_overrides=overrides,
        **kwargs,
    )
    return module, app, log_stream
