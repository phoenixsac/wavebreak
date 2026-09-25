"""JSON-lines logging to stdout (journald picks it up). Identity fields come from the env."""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime


def _identity() -> dict:
    return {
        "device_id": os.environ.get("DEVICE_ID", "unknown"),
        "hw_rev": os.environ.get("HW_REV", "unknown"),
        "region": os.environ.get("REGION", "unknown"),
    }


def event(name: str, level: str = "info", **fields) -> None:
    rec = {
        "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
        "level": level,
        "event": name,
        "component": "ota-agent",
        **_identity(),
        **fields,
    }
    sys.stdout.write(json.dumps(rec, default=str) + "\n")
    sys.stdout.flush()
