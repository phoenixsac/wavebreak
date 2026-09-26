#!/usr/bin/env python3
"""Stable content hash for Docker image source inputs (independent of checkout timestamps)."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INPUTS = {
    "device": ("sim/device", "sim/ota-agent", "sim/inference-app", "sim/bundles/v1.0"),
    "lab": ("lab/controller", "wavebreak_clients"),
}


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in INPUTS:
        raise SystemExit(f"usage: {Path(sys.argv[0]).name} device|lab")
    digest = hashlib.sha256()
    files = sorted(
        file
        for directory in INPUTS[sys.argv[1]]
        for file in (ROOT / directory).rglob("*")
        if file.is_file()
    )
    for file in files:
        digest.update(file.relative_to(ROOT).as_posix().encode())
        digest.update(b"\0")
        digest.update(file.read_bytes())
        digest.update(b"\0")
    print(digest.hexdigest())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
