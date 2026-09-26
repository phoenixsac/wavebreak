"""Smoke: rehearse against the REAL lab controller with one lab device (no hawkBit writes).

Usage: PYTHONPATH=. .venv/bin/python agent/smoke/rehearse_real.py [version=v1.3] [hw_rev=B] [minutes=1.5]
Reads LAB_API_TOKEN etc. from ~/wavebreak/.env (read-only) and uses a throwaway ledger in run/.
"""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

from agent.fleet_mcp.backends import HttpLab
from agent.fleet_mcp.config import Settings
from agent.fleet_mcp.server import build_fleet

version = sys.argv[1] if len(sys.argv) > 1 else "v1.3"
hw_rev = sys.argv[2] if len(sys.argv) > 2 else "B"
minutes = float(sys.argv[3]) if len(sys.argv) > 3 else 1.5

env = dict(os.environ)
envfile = Path.home() / "wavebreak" / ".env"
for line in envfile.read_text().splitlines():
    if "=" in line and not line.lstrip().startswith("#"):
        k, v = line.split("=", 1)
        env.setdefault(k.strip(), v.strip())
ledger = Path("run/smoke-ledger.sqlite")
ledger.unlink(missing_ok=True)
env.update(FLEET_LEDGER_PATH=str(ledger), FLEET_POLL_INTERVAL_S="10")
settings = Settings.from_env(env)
fleet = build_fleet(settings, env)
assert isinstance(fleet.lab, HttpLab), "expected the real lab client"


def snapshot(tag: str) -> None:
    mem = subprocess.run(["free", "-m"], capture_output=True, text=True).stdout.splitlines()[1].split()
    n = subprocess.run(["docker", "ps", "-q"], capture_output=True, text=True).stdout.split()
    lab = subprocess.run(
        ["docker", "ps", "--filter", "name=lab-", "--format", "{{.Names}}"], capture_output=True, text=True
    ).stdout.split()
    print(f"[{tag}] containers={len(n)} lab={lab} mem_available_mb={mem[6]} used_mb={mem[2]}")


orig = fleet.lab.summary
seen = {"n": 0}


def watched(device_id):
    seen["n"] += 1
    if seen["n"] in (2, 6):
        snapshot(f"during rehearsal, summary #{seen['n']}")
    return orig(device_id)


fleet.lab.summary = watched

snapshot("before")
plan = fleet.plan_rollout(version, waves=[2, "rest"])
print("plan:", json.dumps({k: plan[k] for k in ("plan_id", "from_version", "waves", "excluded")}))
result = asyncio.run(fleet.rehearse(plan["plan_id"], [hw_rev], minutes))
print("rehearse:", json.dumps(result, indent=1)[:2500])
snapshot("after")
print("lab devices left:", fleet.lab_devices_left if hasattr(fleet, "lab_devices_left") else "n/a")
