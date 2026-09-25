import io
import json
import sys
import tarfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ota_agent.install import APP_UNIT, Installer, Paths


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


class FakeSystemd:
    """Simulates inference-app behaviour per installed version.

    behaviour[version] in {"healthy", "crash", "silent"}; each poll (active_state call) ticks the
    fake app: healthy writes a metrics sample with advancing frames_total every 5 fake seconds.
    """

    def __init__(self, paths: Paths, clock: FakeClock, behaviour: dict | None = None):
        self.paths = paths
        self.clock = clock
        self.behaviour = behaviour or {}
        self.restarts: list[str] = []
        self.nrestarts = 0
        self.started_at = clock()
        self.fail_restart_units: set[str] = set()

    def _version(self):
        try:
            return json.loads((self.paths.current / "manifest.json").read_text())["version"]
        except OSError:
            return None

    def restart(self, unit):
        self.restarts.append(unit)
        if unit in self.fail_restart_units:
            import subprocess

            raise subprocess.CalledProcessError(1, ["systemctl", "restart", unit])
        if unit == APP_UNIT:
            self.started_at = self.clock()

    def n_restarts(self, unit):
        return self.nrestarts

    def active_state(self, unit):
        v = self._version()
        mode = self.behaviour.get(v, "healthy")
        up = self.clock() - self.started_at
        if mode == "crash":
            self.nrestarts += 1
            return "activating"
        if mode == "healthy" and up >= 5:
            frames = int(up // 5) * 25
            self.paths.metrics_file.parent.mkdir(parents=True, exist_ok=True)
            self.paths.metrics_file.write_text(
                f'wavebreak_app_info{{fw_version="{v}",sensor="1080p"}} 1\n'
                f"wavebreak_app_frames_total {frames}\n"
            )
        return "active"


def make_bundle(tmp_path: Path, version: str, *, top_dir: bool = False, extra=None) -> Path:
    """Create a minimal release tarball."""
    path = tmp_path / f"wavebreak-app-{version}.tar"
    files = {
        "manifest.json": json.dumps(
            {"name": "wavebreak-app", "version": version, "entrypoint": "app/inference_app.py"}
        ),
        "config.yaml": "frame_interval_s: 0.2\n",
        "app/inference_app.py": "print('app')\n",
    }
    files.update(extra or {})
    with tarfile.open(path, "w") as tar:
        for name, content in files.items():
            data = content.encode()
            info = tarfile.TarInfo((f"{version}/" if top_dir else "") + name)
            info.size = len(data)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(data))
    return path


@pytest.fixture
def device(tmp_path):
    """A fake device filesystem with v1.0 installed in slot_a."""
    root = tmp_path / "dev"
    paths = Paths(
        app_root=root / "opt/app",
        metrics_file=root / "textfile/inference_app.prom",
        identity_env=root / "etc/identity.env",
        labels_env=root / "etc/labels.env",
        state_dir=root / "var/lib/ota-agent",
    )
    slot_a = paths.slot("a")
    (slot_a / "app").mkdir(parents=True)
    (slot_a / "manifest.json").write_text(
        json.dumps({"name": "wavebreak-app", "version": "v1.0"})
    )
    (slot_a / "app/inference_app.py").write_text("")
    (paths.app_root / "current").symlink_to("slot_a")
    paths.identity_env.parent.mkdir(parents=True)
    paths.identity_env.write_text("DEVICE_ID=edge-001\nHW_REV=B\nREGION=eu-west\n")
    clock = FakeClock()
    systemd = FakeSystemd(paths, clock)
    installer = Installer(paths, systemd, clock=clock, sleep=clock.sleep)
    return installer, systemd, tmp_path
