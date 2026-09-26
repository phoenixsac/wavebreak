#!/usr/bin/env python3
"""Start and inspect the deterministic container fleet."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys

import yaml

ROOT = Path(__file__).resolve().parents[2]
FLEET_FILE = ROOT / "sim/fleet/fleet.yaml"
IMAGE = "wavebreak-device:local"
NETWORK = "wavebreak_field"


def load_env() -> dict[str, str]:
    """Read simple KEY=VALUE entries from .env without evaluating shell code."""
    values: dict[str, str] = {}
    path = ROOT / ".env"
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip("'\"")
    values.update(os.environ)
    return values


def run(args: list[str], *, check: bool = True, capture: bool = False) -> str:
    result = subprocess.run(args, cwd=ROOT, check=check, text=True,
                            stdout=subprocess.PIPE if capture else None)
    return result.stdout.strip() if capture else ""


def profile_config(profile: str) -> tuple[dict, dict]:
    fleet = yaml.safe_load(FLEET_FILE.read_text())
    if profile not in fleet["profiles"]:
        raise SystemExit(f"unknown profile {profile!r}; choose {', '.join(fleet['profiles'])}")
    return fleet["defaults"], fleet["profiles"][profile]


def device_config(index: int, defaults: dict) -> tuple[str, str, str]:
    b_share = float(defaults["hw_rev_mix"]["B"])
    is_b = int(index * b_share + 0.999999) > int((index - 1) * b_share + 0.999999)
    rev = "B" if is_b else "A"
    regions = defaults["regions"]
    return f"{defaults['id_prefix']}-{index:03d}", rev, regions[(index - 1) % len(regions)]


def ensure_image() -> None:
    exists = subprocess.run(["docker", "image", "inspect", IMAGE], cwd=ROOT,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    if not exists:
        run(["docker", "build", "-f", "sim/device/Dockerfile", "-t", IMAGE, "."])


def ensure_network() -> None:
    exists = subprocess.run(["docker", "network", "inspect", NETWORK], cwd=ROOT,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    if not exists:
        raise SystemExit(f"Docker network {NETWORK} is missing; run `make up PROFILE=lite` first")


def up(profile: str, runtime: str) -> None:
    if runtime != "container":
        raise SystemExit("this MVP supports RUNTIME=container only")
    defaults, cfg = profile_config(profile)
    env = load_env()
    ensure_network()
    ensure_image()
    count = int(cfg["devices"])
    for index in range(1, count + 1):
        device_id, hw_rev, region = device_config(index, defaults)
        name = device_id
        found = subprocess.run(["docker", "inspect", name], cwd=ROOT,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
        if found:
            run(["docker", "start", name], check=False)
            continue
        variables = {
            "DEVICE_ID": device_id, "HW_REV": hw_rev, "REGION": region,
            "RUNTIME": "container", "OTA_MODE": "ddi", "TELEMETRY": "1",
            "APP_MEMORY_MAX": str(cfg["memory_max"]),
            "WAVEBREAK_FRAME_SCALE": str(cfg.get("frame_scale", 1.0)),
            "HAWKBIT_DDI_URL": env.get("HAWKBIT_DDI_URL", "http://hawkbit:8080"),
            "HAWKBIT_TENANT": env.get("HAWKBIT_TENANT", "DEFAULT"),
            "DDI_AUTH_MODE": "target" if env.get("HAWKBIT_DDI_AUTH") == "target" else "gateway",
            "HAWKBIT_GATEWAY_TOKEN": env.get("HAWKBIT_GATEWAY_TOKEN", ""),
            "HAWKBIT_TARGET_TOKEN": env.get("HAWKBIT_TARGET_TOKEN", ""),
            "PROM_HOST": "prometheus", "PROM_PORT": "9090",
            "LOKI_HOST": "loki", "LOKI_PORT": "3100",
        }
        cmd = ["docker", "run", "-d", "--name", name, "--hostname", name,
               "--label", "com.wavebreak.fleet=true", "--label", f"com.wavebreak.profile={profile}",
               "--network", NETWORK, "--restart", "unless-stopped", "--memory", "256m",
               "--memory-swap", "256m", "--cgroupns=host", "-v", "/sys/fs/cgroup:/sys/fs/cgroup:rw",
               "--tmpfs", "/run", "--tmpfs", "/run/lock"]
        for key, value in variables.items():
            cmd.extend(["--env", f"{key}={value}"])
        cmd.append(IMAGE)
        run(cmd)
    print(f"started {count} {profile} container devices")


def containers(all_states: bool = True) -> list[str]:
    args = ["docker", "ps", "-aq" if all_states else "-q", "--filter", "label=com.wavebreak.fleet=true"]
    output = run(args, capture=True)
    return output.splitlines() if output else []


def down(profile: str | None) -> None:
    ids = containers()
    if profile:
        ids = [cid for cid in ids if run(["docker", "inspect", "-f",
               "{{index .Config.Labels \"com.wavebreak.profile\"}}", cid], capture=True) == profile]
    if ids:
        run(["docker", "rm", "-f", *ids])
    print(f"removed {len(ids)} fleet containers")


def status() -> None:
    ids = containers()
    if not ids:
        print("fleet is empty")
        return
    run(["docker", "ps", "-a", "--filter", "label=com.wavebreak.fleet=true",
         "--format", "table {{.Names}}\t{{.Status}}\t{{.Image}}"])


def logs(device_id: str, follow: bool) -> None:
    run(["docker", "logs", *( ["--follow"] if follow else []), device_id])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("up", "down", "status", "logs"))
    parser.add_argument("--profile", default=os.environ.get("PROFILE", "lite"))
    parser.add_argument("--runtime", default=os.environ.get("RUNTIME", "container"))
    parser.add_argument("--device", help="device ID for logs")
    parser.add_argument("--follow", action="store_true")
    args = parser.parse_args()
    if args.action == "up":
        up(args.profile, args.runtime)
    elif args.action == "down":
        down(args.profile)
    elif args.action == "status":
        status()
    elif args.action == "logs":
        if not args.device:
            parser.error("logs requires --device ID")
        logs(args.device, args.follow)


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as exc:
        sys.exit(exc.returncode)
