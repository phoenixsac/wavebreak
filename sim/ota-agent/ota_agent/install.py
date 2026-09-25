"""Install core shared by DDI (field) and local (lab) modes.

Procedure: verify sha256 → extract bundle to the inactive slot → flip /opt/app/current →
restart inference-app → health window (unit active, no restarts, heartbeat advancing on the
new fw_version) → on success rewrite labels.env and restart Fluent Bit; on failure flip back.

Everything touching the system (paths, systemctl, clock) is injectable for tests.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import tarfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import log

APP_UNIT = "inference-app.service"
FLUENT_UNIT = "fluent-bit.service"
BUNDLE_NAME = "wavebreak-app"


class InstallError(Exception):
    """Install aborted before the running app was touched."""


@dataclass
class Paths:
    app_root: Path = Path("/opt/app")
    metrics_file: Path = Path("/var/lib/node_exporter/textfile/inference_app.prom")
    identity_env: Path = Path("/etc/wavebreak/identity.env")
    labels_env: Path = Path("/etc/wavebreak/labels.env")
    state_dir: Path = Path("/var/lib/ota-agent")

    @property
    def current(self) -> Path:
        return self.app_root / "current"

    def slot(self, name: str) -> Path:
        return self.app_root / f"slot_{name}"

    @property
    def state_file(self) -> Path:
        return self.state_dir / "state.json"

    @property
    def lock_file(self) -> Path:
        return self.state_dir / "install.lock"


@dataclass
class InstallResult:
    ok: bool
    version: str | None
    previous_version: str | None
    slot: str | None
    message: str
    duration_s: float = 0.0
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


class Systemd:
    """Thin systemctl wrapper. Tests substitute a fake with the same methods."""

    def restart(self, unit: str) -> None:
        subprocess.run(["systemctl", "restart", "--no-block", unit], check=True, capture_output=True)

    def active_state(self, unit: str) -> str:
        return self.show(unit, "ActiveState") or "unknown"

    def n_restarts(self, unit: str) -> int:
        try:
            return int(self.show(unit, "NRestarts") or 0)
        except ValueError:
            return 0

    def show(self, unit: str, prop: str) -> str:
        out = subprocess.run(
            ["systemctl", "show", "-p", prop, "--value", unit], capture_output=True, text=True,
            check=False,
        )
        return out.stdout.strip()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def read_env_file(path: Path) -> dict[str, str]:
    """Parse KEY=VALUE lines (optional quotes, # comments)."""
    env: dict[str, str] = {}
    try:
        text = path.read_text()
    except FileNotFoundError:
        return env
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def parse_prom_sample(text: str) -> dict:
    """Extract fw_version and frames_total from the app's textfile metrics."""
    out: dict = {}
    for line in text.splitlines():
        if line.startswith("wavebreak_app_info{"):
            labels = line[line.index("{") + 1 : line.index("}")]
            for part in labels.split(","):
                if "=" in part:
                    k, v = part.split("=", 1)
                    if k.strip() == "fw_version":
                        out["fw_version"] = v.strip().strip('"')
        elif line.startswith("wavebreak_app_frames_total"):
            try:
                out["frames_total"] = float(line.split()[-1])
            except ValueError:
                pass
    return out


def safe_extract(bundle: Path, dest: Path) -> None:
    """Extract a tar bundle, rejecting absolute paths, '..', links and devices.

    Python 3.11.2 (Debian bookworm) lacks tarfile extraction filters, so validate by hand.
    """
    with tarfile.open(bundle) as tar:
        members = tar.getmembers()
        for m in members:
            name = m.name
            if name.startswith("/") or ".." in Path(name).parts:
                raise InstallError(f"unsafe path in bundle: {name}")
            if not (m.isfile() or m.isdir()):
                raise InstallError(f"unsupported member type in bundle: {name}")
        for m in members:
            target = dest / m.name
            if m.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            src = tar.extractfile(m)
            assert src is not None
            with src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)
            os.chmod(target, 0o755 if m.mode & 0o111 else 0o644)


def bundle_root(extracted: Path) -> Path:
    """Bundle files are at the tar root; tolerate a single top-level directory."""
    if (extracted / "manifest.json").is_file():
        return extracted
    children = [p for p in extracted.iterdir()]
    if len(children) == 1 and (children[0] / "manifest.json").is_file():
        return children[0]
    raise InstallError("manifest.json not found in bundle")


def read_manifest(root: Path) -> dict:
    try:
        manifest = json.loads((root / "manifest.json").read_text())
    except (OSError, ValueError) as e:
        raise InstallError(f"invalid manifest.json: {e}") from e
    if manifest.get("name") != BUNDLE_NAME or not manifest.get("version"):
        raise InstallError(f"manifest is not a {BUNDLE_NAME} release: {manifest}")
    entry = manifest.get("entrypoint", "app/inference_app.py")
    if not (root / entry).is_file():
        raise InstallError(f"entrypoint {entry} missing from bundle")
    return manifest


class Installer:
    def __init__(
        self,
        paths: Paths | None = None,
        systemd: Systemd | None = None,
        *,
        health_window_s: float = 15.0,
        heartbeat_grace_s: float = 10.0,
        poll_s: float = 1.0,
        clock=time.monotonic,
        sleep=time.sleep,
    ) -> None:
        self.paths = paths or Paths()
        self.systemd = systemd or Systemd()
        self.health_window_s = health_window_s
        self.heartbeat_grace_s = heartbeat_grace_s
        self.poll_s = poll_s
        self.clock = clock
        self.sleep = sleep

    # --- state -----------------------------------------------------------------------

    def current_slot(self) -> str | None:
        try:
            target = os.readlink(self.paths.current)
        except OSError:
            return None
        name = Path(target).name
        return name.removeprefix("slot_") if name.startswith("slot_") else None

    def installed_version(self) -> str | None:
        try:
            return json.loads((self.paths.current / "manifest.json").read_text()).get("version")
        except (OSError, ValueError):
            return None

    def last_result(self) -> dict | None:
        try:
            return json.loads(self.paths.state_file.read_text()).get("last_install")
        except (OSError, ValueError):
            return None

    def _save_result(self, result: InstallResult) -> None:
        self.paths.state_dir.mkdir(parents=True, exist_ok=True)
        state = {"last_install": {**result.to_dict(), "ts": time.time()}}
        tmp = self.paths.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(state))
        os.replace(tmp, self.paths.state_file)

    # --- filesystem steps ---------------------------------------------------------------

    def _flip(self, slot: str) -> None:
        tmp = self.paths.app_root / "current.tmp"
        if tmp.is_symlink() or tmp.exists():
            tmp.unlink()
        os.symlink(f"slot_{slot}", tmp)
        os.replace(tmp, self.paths.current)

    def _stage(self, bundle: Path, slot: str) -> dict:
        staging = self.paths.app_root / f".staging-{os.getpid()}"
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        try:
            safe_extract(bundle, staging)
            root = bundle_root(staging)
            manifest = read_manifest(root)
            dest = self.paths.slot(slot)
            shutil.rmtree(dest, ignore_errors=True)
            os.rename(root, dest)
            return manifest
        except (tarfile.TarError, OSError) as e:
            raise InstallError(f"cannot extract bundle: {e}") from e
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def write_labels(self, fw_version: str | None) -> None:
        ident = read_env_file(self.paths.identity_env)
        labels = {
            "DEVICE_ID": ident.get("DEVICE_ID", "unknown"),
            "HW_REV": ident.get("HW_REV", "unknown"),
            "REGION": ident.get("REGION", "unknown"),
            "FW_VERSION": fw_version or "unknown",
        }
        self.paths.labels_env.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.paths.labels_env.with_suffix(".tmp")
        tmp.write_text("".join(f"{k}={v}\n" for k, v in labels.items()))
        os.replace(tmp, self.paths.labels_env)

    # --- health ----------------------------------------------------------------------

    def _read_metrics(self) -> dict:
        try:
            return parse_prom_sample(self.paths.metrics_file.read_text())
        except OSError:
            return {}

    def health_check(self, version: str) -> tuple[bool, str, dict]:
        """Watch the app for the health window.

        Healthy = unit active at the end, NRestarts unchanged, and at least two samples of the
        new fw_version with frames_total advancing. If the window ends before two samples were
        seen (metrics are written every 5 s), wait up to heartbeat_grace_s more for them.
        """
        start = self.clock()
        restarts0 = self.systemd.n_restarts(APP_UNIT)
        frames: list[float] = []
        states: list[str] = []
        deadline = start + self.health_window_s
        hard_deadline = deadline + self.heartbeat_grace_s
        while True:
            self.sleep(self.poll_s)
            now = self.clock()
            states.append(self.systemd.active_state(APP_UNIT))
            sample = self._read_metrics()
            fresh = sample.get("fw_version") == version and "frames_total" in sample
            if fresh and (not frames or sample["frames_total"] != frames[-1]):
                frames.append(sample["frames_total"])
            advancing = len(frames) >= 2 and frames[-1] > frames[0]
            if now >= deadline and (advancing or now >= hard_deadline):
                break
        restarts = self.systemd.n_restarts(APP_UNIT) - restarts0
        final_state = states[-1] if states else "unknown"
        details = {
            "final_state": final_state,
            "restarts_during_window": restarts,
            "heartbeat_samples": frames,
            "window_s": round(self.clock() - start, 1),
        }
        if final_state != "active":
            return False, f"inference-app not active after health window (state {final_state})", details
        if restarts > 0:
            return False, f"inference-app restarted {restarts} time(s) during health window", details
        if not advancing:
            return False, f"no heartbeat from {version} (frames_total samples {frames})", details
        return True, f"healthy: frames_total {frames[0]:.0f} -> {frames[-1]:.0f}", details

    # --- main entry ------------------------------------------------------------------

    def install(self, bundle: Path, expected_sha256: str | None = None) -> InstallResult:
        """Install a bundle. Never raises for install failures; returns an InstallResult."""
        self.paths.state_dir.mkdir(parents=True, exist_ok=True)
        with open(self.paths.lock_file, "w") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return InstallResult(False, None, self.installed_version(), self.current_slot(),
                                     "another install is in progress")
            result = self._install_locked(Path(bundle), expected_sha256)
        self._save_result(result)
        log.event("install_result", ok=result.ok, version=result.version,
                  previous_version=result.previous_version, slot=result.slot, msg=result.message,
                  duration_s=result.duration_s)
        return result

    def _install_locked(self, bundle: Path, expected_sha256: str | None) -> InstallResult:
        t0 = self.clock()
        prev_version = self.installed_version()
        prev_slot = self.current_slot() or "a"
        target_slot = "b" if prev_slot == "a" else "a"

        def fail(msg: str, version: str | None = None, **details) -> InstallResult:
            return InstallResult(False, version, prev_version, prev_slot, msg,
                                 round(self.clock() - t0, 1), details)

        if expected_sha256:
            actual = sha256_file(bundle)
            if actual.lower() != expected_sha256.lower():
                return fail(f"sha256 mismatch: expected {expected_sha256}, got {actual}")
        try:
            manifest = self._stage(bundle, target_slot)
        except InstallError as e:
            return fail(str(e))
        version = manifest["version"]
        log.event("install_start", version=version, previous_version=prev_version,
                  slot=target_slot)

        self._flip(target_slot)
        self.systemd.restart(APP_UNIT)
        ok, msg, details = self.health_check(version)
        if not ok:
            log.event("install_rollback", version=version, reason=msg, slot=prev_slot)
            self._flip(prev_slot)
            self.systemd.restart(APP_UNIT)
            return fail(f"{msg}; rolled back to {prev_version} (slot {prev_slot})", version, **details)

        self.write_labels(version)
        try:
            self.systemd.restart(FLUENT_UNIT)
        except (subprocess.CalledProcessError, OSError) as e:
            log.event("fluent_bit_restart_failed", level="warning", error=str(e))
        return InstallResult(True, version, prev_version, target_slot, msg,
                             round(self.clock() - t0, 1), details)
