#!/usr/bin/env python3
"""Wavebreak inference-app v1.3 — simulated camera-AI inference loop.

v1.1 adds `wavebreak_app_frame_latency_seconds` (gauge, last frame processing
time) and a `latency_s` field on the "frame_stats" log event.

v1.3 renames the detection-threshold config key to `confidence_threshold`
(bundle config.yaml updated to match) without a device-side migration.
Startup only reads the bundle config, so it works. The periodic reload also
reads the device config file (`/etc/wavebreak/app.conf`), which still carries
the old `detection_threshold` key on every device -> KeyError on first
reload.

Runtime contract (depended on by the systemd unit and ota-agent):
    python3 /opt/app/current/app/inference_app.py --bundle-dir /opt/app/current

Every frame_interval_s a frame is generated (sized by sensor: rev A "1080p",
rev B "4K"), "processed" (cheap checksum + threshold detections), and kept in
a bounded recent-frames cache. Every metrics_interval_s a Prometheus textfile
is written atomically and a "frame_stats" JSON line is logged. Every
config_reload_interval_s the bundle config and the device config file are
re-read. All logging is one JSON object per line on stdout, flushed
immediately. Python 3.11+, stdlib only.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

DEFAULT_METRICS_DIR = Path("/var/lib/node_exporter/textfile")
DEFAULT_DEVICE_CONFIG = Path("/etc/wavebreak/app.conf")
DEFAULT_BOOT_TIME_FILE = Path("/run/wavebreak/boot_time")


# --------------------------------------------------------------------------
# Tiny flat-YAML parser: `key: value` per line, ints/floats/bools/strings,
# bare or quoted, with `#` comments. No pyyaml (device-side is stdlib only).
# --------------------------------------------------------------------------


def _strip_comment(line: str) -> str:
    """Drop a trailing `# comment`, ignoring `#` inside quotes."""
    in_quote: str | None = None
    for i, ch in enumerate(line):
        if in_quote:
            if ch == in_quote:
                in_quote = None
        elif ch in ("'", '"'):
            in_quote = ch
        elif ch == "#":
            return line[:i].rstrip()
    return line


def _parse_scalar(value: str) -> Any:
    if not value:
        return ""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    low = value.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    return value


def parse_flat_yaml(text: str) -> dict[str, Any]:
    """Parse the flat YAML subset used by bundle config.yaml and app.conf."""
    result: dict[str, Any] = {}
    for raw_line in text.splitlines():
        line = _strip_comment(raw_line.strip())
        if not line or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        if not key:
            continue
        result[key] = _parse_scalar(value.strip())
    return result


def load_config_file(path: Path) -> dict[str, Any]:
    """Read and parse a flat-YAML file; missing/unreadable file -> empty dict."""
    try:
        text = path.read_text()
    except OSError:
        return {}
    return parse_flat_yaml(text)


# --------------------------------------------------------------------------
# Identity and logging
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Identity:
    device_id: str
    hw_rev: str
    region: str
    fw_version: str
    sensor: str


def sensor_for_hw_rev(hw_rev: str) -> str:
    return "4K" if hw_rev == "B" else "1080p"


def read_fw_version(bundle_dir: Path) -> str:
    manifest = json.loads((bundle_dir / "manifest.json").read_text())
    return str(manifest["version"])


def build_identity(bundle_dir: Path, env: dict[str, str]) -> Identity:
    hw_rev = env.get("HW_REV", "A")
    return Identity(
        device_id=env.get("DEVICE_ID", "unknown"),
        hw_rev=hw_rev,
        region=env.get("REGION", "unknown"),
        fw_version=read_fw_version(bundle_dir),
        sensor=sensor_for_hw_rev(hw_rev),
    )


def format_iso8601(epoch_seconds: float) -> str:
    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).isoformat()


class JsonLogger:
    """One JSON object per line on stdout, flushed immediately."""

    def __init__(self, stream: Any, identity: Identity, clock: Callable[[], float]) -> None:
        self.stream = stream
        self.identity = identity
        self.clock = clock

    def log(self, level: str, event: str, msg: str, **fields: Any) -> None:
        record = {
            "ts": format_iso8601(self.clock()),
            "level": level,
            "event": event,
            "msg": msg,
            "device_id": self.identity.device_id,
            "hw_rev": self.identity.hw_rev,
            "region": self.identity.region,
            "fw_version": self.identity.fw_version,
            **fields,
        }
        self.stream.write(json.dumps(record) + "\n")
        self.stream.flush()


# --------------------------------------------------------------------------
# Metrics sources
# --------------------------------------------------------------------------


def read_rss_bytes(status_path: Path) -> int:
    """VmRSS from /proc/self/status, in bytes."""
    try:
        text = status_path.read_text()
    except OSError:
        return 0
    for line in text.splitlines():
        if line.startswith("VmRSS:"):
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                return int(parts[1]) * 1024
    return 0


def read_cgroup_memory_bytes(cgroup_path: Path, cgroup_root: Path) -> int | None:
    """memory.current for this process's cgroup v2 slice, or None if unreadable."""
    try:
        text = cgroup_path.read_text()
    except OSError:
        return None
    suffix = None
    for line in text.splitlines():
        if line.startswith("0::"):
            suffix = line.split(":", 2)[2]
            break
    if not suffix:
        return None
    mem_file = cgroup_root / suffix.lstrip("/") / "memory.current"
    try:
        return int(mem_file.read_text().strip())
    except (OSError, ValueError):
        return None


def read_boot_time(boot_time_file: Path, proc_stat_path: Path) -> float:
    """Boot time as unix epoch seconds; fall back to /proc/stat btime."""
    try:
        return float(boot_time_file.read_text().strip())
    except (OSError, ValueError):
        pass
    try:
        for line in proc_stat_path.read_text().splitlines():
            if line.startswith("btime "):
                return float(line.split()[1])
    except (OSError, IndexError, ValueError):
        pass
    return 0.0


def render_metric(name: str, mtype: str, help_text: str, value_line: str) -> list[str]:
    return [f"# HELP {name} {help_text}", f"# TYPE {name} {mtype}", value_line]


# --------------------------------------------------------------------------
# The app
# --------------------------------------------------------------------------


class InferenceApp:
    """Simulated camera-AI inference loop.

    All I/O boundaries (paths, env, clock, sleep, log stream) are injectable
    so tests can drive `step()` / `run(max_frames=...)` without sleeping or
    touching the real filesystem.
    """

    def __init__(
        self,
        bundle_dir: Path,
        metrics_dir: Path = DEFAULT_METRICS_DIR,
        device_config_path: Path = DEFAULT_DEVICE_CONFIG,
        boot_time_file: Path = DEFAULT_BOOT_TIME_FILE,
        env: dict[str, str] | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        log_stream: Any = None,
        config_overrides: dict[str, Any] | None = None,
        status_path: Path = Path("/proc/self/status"),
        cgroup_path: Path = Path("/proc/self/cgroup"),
        cgroup_root: Path = Path("/sys/fs/cgroup"),
        proc_stat_path: Path = Path("/proc/stat"),
    ) -> None:
        self.bundle_dir = bundle_dir
        self.metrics_dir = metrics_dir
        self.device_config_path = device_config_path
        self.boot_time_file = boot_time_file
        self.env = dict(os.environ) if env is None else env
        self.clock = clock
        self.sleep = sleep
        self.config_overrides = dict(config_overrides or {})
        self.status_path = status_path
        self.cgroup_path = cgroup_path
        self.cgroup_root = cgroup_root
        self.proc_stat_path = proc_stat_path

        self.identity = build_identity(bundle_dir, self.env)
        self.logger = JsonLogger(log_stream or sys.stdout, self.identity, self.clock)
        self.frame_scale = float(self.env.get("WAVEBREAK_FRAME_SCALE", "1.0"))

        self.config: dict[str, Any] = {}
        self._load_startup_config()

        self.cache: deque[int] = deque(maxlen=int(self.config.get("cache_max_items", 8)))
        self.frames_total = 0
        self.frames_since_metrics = 0
        self.last_latency_s = 0.0
        self.last_detections = 0
        self._stop = False

        now = self.clock()
        self._last_metrics_time = now
        self._last_reload_time = now

    # --- config ----------------------------------------------------------

    def _load_bundle_config(self) -> dict[str, Any]:
        cfg = load_config_file(self.bundle_dir / "config.yaml")
        cfg.update(self.config_overrides)
        return cfg

    def _load_startup_config(self) -> None:
        """Startup reads only the bundle config (plus test overrides)."""
        self.config = self._load_bundle_config()

    def _reload_config(self) -> None:
        """Re-read the bundle config and the device config file.

        Applies `confidence_threshold` from the device file. Devices still
        carry the pre-rename `detection_threshold` key (no migration was
        shipped), so this raises KeyError on the first reload.
        """
        new_cfg = self._load_bundle_config()
        device_cfg = load_config_file(self.device_config_path)
        new_cfg["confidence_threshold"] = device_cfg["confidence_threshold"]
        self.config = new_cfg
        self.cache = deque(self.cache, maxlen=int(self.config.get("cache_max_items", 8)))

    def _maybe_reload_config(self) -> None:
        interval = float(self.config.get("config_reload_interval_s", 45))
        now = self.clock()
        if now - self._last_reload_time < interval:
            return
        self._last_reload_time = now
        try:
            self._reload_config()
        except Exception as exc:
            self.logger.log("error", "config_reload_failed", "config reload failed", error=str(exc))
            raise
        self.logger.log(
            "info", "config_reload", "config reloaded",
            confidence_threshold=self.config.get("confidence_threshold"),
        )

    # --- frame loop --------------------------------------------------------

    def _frame_bytes(self) -> int:
        key = "frame_bytes_4k" if self.identity.sensor == "4K" else "frame_bytes_1080p"
        default = 40960 if self.identity.sensor == "4K" else 16384
        base = self.config.get(key, default)
        return max(1, int(base * self.frame_scale))

    def _generate_frame(self) -> bytes:
        return os.urandom(self._frame_bytes())

    def _process_frame(self, frame: bytes) -> tuple[int, int]:
        """Cheap checksum + detection count over a stride sample of the frame."""
        step = max(1, len(frame) // 256)
        sample = frame[::step]
        checksum = sum(sample) % 65536
        threshold = float(self.config.get("confidence_threshold", 0.6))
        detections = sum(1 for b in sample if (b / 255.0) >= threshold)
        return checksum, detections

    def step(self) -> None:
        """Generate, process, and cache one frame; then run periodic work."""
        frame = self._generate_frame()
        start = self.clock()
        checksum, detections = self._process_frame(frame)
        self.last_latency_s = self.clock() - start
        self.last_detections = detections

        self.cache.append(checksum)
        self.frames_total += 1
        self.frames_since_metrics += 1

        self._maybe_write_metrics()
        self._maybe_reload_config()

    # --- metrics -----------------------------------------------------------

    def cache_items(self) -> int:
        return len(self.cache)

    def _metric_lines(self, fps: float) -> list[str]:
        rss = read_rss_bytes(self.status_path)
        cgroup_mem = read_cgroup_memory_bytes(self.cgroup_path, self.cgroup_root)
        boot_time = read_boot_time(self.boot_time_file, self.proc_stat_path)

        lines: list[str] = []
        lines += render_metric(
            "wavebreak_app_info", "gauge", "Static info about the running app build.",
            f'wavebreak_app_info{{fw_version="{self.identity.fw_version}",'
            f'sensor="{self.identity.sensor}"}} 1',
        )
        lines += render_metric(
            "wavebreak_app_rss_bytes", "gauge", "Process resident set size in bytes.",
            f"wavebreak_app_rss_bytes {rss}",
        )
        if cgroup_mem is not None:
            lines += render_metric(
                "wavebreak_app_cgroup_memory_bytes", "gauge", "Cgroup memory.current in bytes.",
                f"wavebreak_app_cgroup_memory_bytes {cgroup_mem}",
            )
        lines += render_metric(
            "wavebreak_app_frames_total", "counter", "Total frames processed.",
            f"wavebreak_app_frames_total {self.frames_total}",
        )
        lines += render_metric(
            "wavebreak_app_fps", "gauge", "Frames processed per second over the last interval.",
            f"wavebreak_app_fps {fps:.4f}",
        )
        lines += render_metric(
            "wavebreak_app_cache_items", "gauge", "Items held in the recent-frames cache.",
            f"wavebreak_app_cache_items {self.cache_items()}",
        )
        lines += render_metric(
            "wavebreak_app_frame_latency_seconds", "gauge", "Last frame processing time in seconds.",
            f"wavebreak_app_frame_latency_seconds {self.last_latency_s:.6f}",
        )
        lines += render_metric(
            "wavebreak_device_boot_time_seconds", "gauge", "Device boot time, unix epoch seconds.",
            f"wavebreak_device_boot_time_seconds {boot_time}",
        )
        return lines

    def _write_metrics_file(self, fps: float) -> None:
        self.metrics_dir.mkdir(parents=True, exist_ok=True)
        target = self.metrics_dir / "inference_app.prom"
        tmp = self.metrics_dir / f".inference_app.prom.{os.getpid()}.tmp"
        tmp.write_text("\n".join(self._metric_lines(fps)) + "\n")
        os.replace(tmp, target)

    def _frame_stats_fields(self, fps: float) -> dict[str, Any]:
        return {
            "frames_total": self.frames_total,
            "fps": round(fps, 3),
            "cache_items": self.cache_items(),
            "detections": self.last_detections,
            "latency_s": round(self.last_latency_s, 6),
        }

    def _maybe_write_metrics(self) -> None:
        interval = float(self.config.get("metrics_interval_s", 5))
        now = self.clock()
        elapsed = now - self._last_metrics_time
        if elapsed < interval:
            return
        fps = self.frames_since_metrics / elapsed if elapsed > 0 else 0.0
        self._write_metrics_file(fps)
        self.logger.log("info", "frame_stats", "frame stats", **self._frame_stats_fields(fps))
        self.frames_since_metrics = 0
        self._last_metrics_time = now

    # --- lifecycle -----------------------------------------------------------

    def handle_sigterm(self, signum: int | None = None, frame: Any = None) -> None:
        self._stop = True

    def _install_signal_handlers(self) -> None:
        signal.signal(signal.SIGTERM, self.handle_sigterm)

    def run(self, max_frames: int | None = None) -> None:
        self._install_signal_handlers()
        self.logger.log(
            "info", "startup", "inference-app starting",
            sensor=self.identity.sensor, frame_bytes=self._frame_bytes(),
        )
        frame_interval = float(self.config.get("frame_interval_s", 0.2))
        count = 0
        while not self._stop:
            self.step()
            count += 1
            if max_frames is not None and count >= max_frames:
                break
            if self._stop:
                break
            self.sleep(frame_interval)
        self.logger.log("info", "shutdown", "inference-app stopping", frames_total=self.frames_total)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def parse_overrides(pairs: list[str]) -> dict[str, Any]:
    overrides: dict[str, Any] = {}
    for pair in pairs:
        key, _, value = pair.partition("=")
        overrides[key.strip()] = _parse_scalar(value.strip())
    return overrides


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Wavebreak inference-app")
    parser.add_argument("--bundle-dir", required=True, type=Path)
    parser.add_argument("--metrics-dir", type=Path, default=DEFAULT_METRICS_DIR)
    parser.add_argument("--device-config", type=Path, default=DEFAULT_DEVICE_CONFIG)
    parser.add_argument("--boot-time-file", type=Path, default=DEFAULT_BOOT_TIME_FILE)
    parser.add_argument(
        "--config-override", action="append", default=[], metavar="key=value",
        help="Override a bundle config key, e.g. --config-override frame_interval_s=0.01",
    )
    parser.add_argument(
        "--max-frames", type=int, default=None,
        help="Stop after N frames instead of running forever (testing).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    app = InferenceApp(
        bundle_dir=args.bundle_dir,
        metrics_dir=args.metrics_dir,
        device_config_path=args.device_config,
        boot_time_file=args.boot_time_file,
        config_overrides=parse_overrides(args.config_override),
    )
    app.run(max_frames=args.max_frames)
    return 0


if __name__ == "__main__":
    sys.exit(main())
