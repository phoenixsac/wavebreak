"""Pure verdict rules (docs/agent-design.md sections 10.1 and 12). No I/O."""

from __future__ import annotations

from statistics import median

from .models import DeviceStats, Thresholds

MB = 1024 * 1024
# TODO(verify): lab install result values; anything else fails closed in rehearsal.
INSTALL_OK = ("success", "ok", "installed", "succeeded")


def slope_mb_per_min(samples: list[tuple[float, float]]) -> float | None:
    """Least-squares slope of (epoch_seconds, bytes) samples in MB/min (MB = 1024*1024 bytes)."""
    n = len(samples)
    if n < 2:
        return None
    mt = sum(t for t, _ in samples) / n
    mb = sum(b for _, b in samples) / n
    sxx = sum((t - mt) ** 2 for t, _ in samples)
    if sxx == 0:
        return None
    sxy = sum((t - mt) * (b - mb) for t, b in samples)
    return sxy / sxx * 60.0 / MB


def _signal(name: str, updated, control, detail: str) -> dict:
    return {"name": name, "updated": updated, "control": control, "detail": detail}


def _valid_slopes(stats: list[DeviceStats], th: Thresholds) -> list[float]:
    return [
        s.mem_slope_mb_per_min
        for s in stats
        if s.mem_slope_mb_per_min is not None
        and s.samples >= th.slope_min_samples
        and s.window_s >= th.slope_min_window_s
    ]


def _median(values: list[float]) -> float | None:
    return median(values) if values else None


def _rate_per_min(stats: list[DeviceStats]) -> float | None:
    window = sum(s.window_s for s in stats)
    if window <= 0:
        return None
    return sum(s.restarts for s in stats) / window * 60.0


def _oom_signal(upd: list[DeviceStats], ctl: list[DeviceStats]) -> dict | None:
    u, c = sum(s.oom_kills for s in upd), sum(s.oom_kills for s in ctl)
    if u > 0 and c == 0:
        return _signal("oom", u, c, "OOM kills on updated devices, none in control")
    return None


def _crash_signal(upd: list[DeviceStats], ctl: list[DeviceStats], th: Thresholds) -> dict | None:
    looping = [s.device_id for s in upd if s.restarts >= th.crash_restarts]
    if not looping:
        return None
    c = max((s.restarts for s in ctl), default=0)
    return _signal(
        "crash_loop",
        max(s.restarts for s in upd),
        c,
        f">= {th.crash_restarts} restarts in window on {', '.join(sorted(looping))}",
    )


def _slope_signal(upd: list[DeviceStats], ctl: list[DeviceStats], th: Thresholds) -> dict | None:
    u, c = _median(_valid_slopes(upd, th)), _median(_valid_slopes(ctl, th))
    if u is None or c is None or u <= c + th.mem_slope_margin_mb_per_min:
        return None
    return _signal(
        "memory_slope",
        round(u, 3),
        round(c, 3),
        f"updated slope exceeds control by more than {th.mem_slope_margin_mb_per_min} MB/min",
    )


def _restarts_signal(upd: list[DeviceStats], ctl: list[DeviceStats], th: Thresholds) -> dict | None:
    if sum(s.restarts for s in upd) < 1:
        return None
    u, c = _rate_per_min(upd), _rate_per_min(ctl)
    if u is None or c is None or u <= th.restart_rate_factor * c:
        return None
    return _signal(
        "restarts",
        round(u, 3),
        round(c, 3),
        f"restart rate per min exceeds {th.restart_rate_factor}x control",
    )


def _fps_signal(upd: list[DeviceStats], ctl: list[DeviceStats], th: Thresholds) -> dict | None:
    u = _median([s.fps_median for s in upd if s.fps_median is not None])
    c = _median([s.fps_median for s in ctl if s.fps_median is not None])
    if u is None or c is None or u >= th.fps_ratio * c:
        return None
    return _signal("fps", round(u, 3), round(c, 3), f"updated median fps below {th.fps_ratio:.0%} of control")


def assess_hw_rev(
    hw_rev: str,
    updated: list[DeviceStats],
    control: list[DeviceStats],
    control_type: str,
    install_failures: list[str],
    th: Thresholds,
) -> dict:
    """Verdict for one hw_rev: healthy, regression or inconclusive.

    `updated` holds every wave device of this hw_rev (installed or not); metric signals use only the
    installed ones. `control` is same-hw_rev devices still on the old version, or, when there are
    none, each updated device's pre-update stats (`control_type` "baseline"). Restart counts are
    assumed to already cover a window of at most `th.crash_window_s`; the caller passes per-window
    counts. Signals needing a control value (memory_slope, restarts, fps) are skipped when the control
    has none; oom and crash_loop do not need one. `signals` lists only the signals that fired; `reasons` explains an inconclusive verdict. A wave is
    healthy only if every updated device installed, has enough samples and its memory slope is evaluable.
    """
    installed = [s for s in updated if s.installed]
    signals = [
        sig
        for sig in (
            _oom_signal(installed, control),
            _crash_signal(installed, control, th),
            _slope_signal(installed, control, th),
            _restarts_signal(installed, control, th),
            _fps_signal(installed, control, th),
        )
        if sig
    ]
    if install_failures:
        signals.append(
            _signal(
                "install",
                len(install_failures),
                0,
                f"install failed on {', '.join(sorted(install_failures))}",
            )
        )
    reasons: list[str] = []
    if not signals:
        if not updated:
            reasons.append("no updated devices")
        elif len(installed) < len(updated):
            reasons.append(f"{len(updated) - len(installed)} device(s) have not installed the version yet")
        if any(s.samples < th.min_samples for s in updated):
            reasons.append(f"fewer than {th.min_samples} samples on some device")
        if installed and len(_valid_slopes(installed, th)) < len(installed):
            reasons.append(
                f"memory slope not evaluable: need {th.slope_min_samples} samples over "
                f"{th.slope_min_window_s:.0f}s on every updated device"
            )
    verdict = "regression" if signals else ("inconclusive" if reasons else "healthy")
    return {
        "hw_rev": hw_rev,
        "verdict": verdict,
        "signals": signals,
        "reasons": reasons,
        "control_type": control_type,
        "updated_devices": sorted(s.device_id for s in updated),
        "control_devices": sorted(s.device_id for s in control),
        "mem_slope_updated": _median(_valid_slopes(installed, th)),
        "mem_slope_control": _median(_valid_slopes(control, th)),
        "restarts_updated": sum(s.restarts for s in installed),
        "oom_updated": sum(s.oom_kills for s in installed),
    }


def assess_wave(per_hw_rev: list[dict]) -> dict:
    """Combine per-hw_rev assessments: regression > inconclusive > healthy."""
    verdicts = [r["verdict"] for r in per_hw_rev]
    if "regression" in verdicts:
        verdict = "regression"
    elif "inconclusive" in verdicts or not verdicts:
        verdict = "inconclusive"
    else:
        verdict = "healthy"
    return {
        "verdict": verdict,
        "affected_hw_revs": sorted(r["hw_rev"] for r in per_hw_rev if r["verdict"] == "regression"),
        "per_hw_rev": {r["hw_rev"]: r for r in per_hw_rev},
    }


def assess_rehearsal_device(summary: dict, th: Thresholds) -> dict:
    """Pass/fail for one lab device summary (`memory_samples` is [{t, bytes}], t in seconds).

    Fails on: install not successful (or missing), unit not `active`, restarts >= 1, oom_kills > 0,
    or memory slope above `th.rehearsal_slope_mb_per_min` (needs >= 2 samples; else not checked).
    """
    install = (summary.get("last_install") or {}).get("result")
    restarts = int(summary.get("restarts") or 0)
    oom = int(summary.get("oom_kills") or 0)
    unit = summary.get("unit_state")
    pts = [(float(m["t"]), float(m["bytes"])) for m in summary.get("memory_samples") or []]
    slope = slope_mb_per_min(pts)
    reasons = []
    if install is None or str(install).lower() not in INSTALL_OK:
        reasons.append(f"install_failed: result={install}")
    if unit != "active":
        reasons.append(f"unit_not_active: {unit}")
    if restarts >= 1:
        reasons.append(f"restarts: {restarts}")
    if oom > 0:
        reasons.append(f"oom_kills: {oom}")
    if slope is not None and slope > th.rehearsal_slope_mb_per_min:
        reasons.append(f"memory_slope: {slope:.2f} MB/min > {th.rehearsal_slope_mb_per_min}")
    return {
        "verdict": "fail" if reasons else "pass",
        "reasons": reasons,
        "install_result": install,
        "restarts": restarts,
        "oom_kills": oom,
        "mem_slope_mb_per_min": None if slope is None else round(slope, 3),
        "unit_state": unit,
    }


def assess_rehearsal(per_hw_rev: dict[str, dict]) -> str:
    """Overall rehearsal verdict: pass only if every hw_rev passed (and there is at least one)."""
    ok = bool(per_hw_rev) and all(r.get("verdict") == "pass" for r in per_hw_rev.values())
    return "pass" if ok else "fail"
