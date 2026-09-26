"""Plain data models shared by the pure fleet MCP modules."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Device:
    """A field device as seen in the fleet inventory."""

    id: str
    hw_rev: str
    region: str
    version: str


@dataclass
class DeviceStats:
    """Summary metrics of one device over one observation window."""

    device_id: str
    hw_rev: str
    installed: bool
    samples: int
    window_s: float
    mem_slope_mb_per_min: float | None
    restarts: int
    oom_kills: int
    fps_median: float | None


@dataclass(frozen=True)
class Thresholds:
    """Verdict thresholds (docs/agent-design.md sections 10.1 and 12)."""

    mem_slope_margin_mb_per_min: float = 1.0
    crash_restarts: int = 3
    crash_window_s: int = 300
    restart_rate_factor: float = 2.0
    fps_ratio: float = 0.8
    min_samples: int = 3
    slope_min_samples: int = 5
    slope_min_window_s: int = 60
    rehearsal_slope_mb_per_min: float = 1.0
