"""Tests for the pure verdict rules in assess.py."""

from __future__ import annotations

import pytest

from agent.fleet_mcp.assess import (
    MB,
    assess_hw_rev,
    assess_rehearsal,
    assess_rehearsal_device,
    assess_wave,
    inconclusive_hw_rev,
    slope_mb_per_min,
)
from agent.fleet_mcp.models import DeviceStats, Thresholds

TH = Thresholds()


def st(
    device_id: str = "u1",
    *,
    installed: bool = True,
    samples: int = 10,
    window_s: float = 120.0,
    slope: float | None = 0.0,
    restarts: int = 0,
    oom: int = 0,
    fps: float | None = 30.0,
    hw_rev: str = "A",
) -> DeviceStats:
    """A clean, fully evaluable device unless overridden."""
    return DeviceStats(device_id, hw_rev, installed, samples, window_s, slope, restarts, oom, fps)


def run(updated, control, control_type="control", failures=()):
    """assess_hw_rev for hw_rev A with default thresholds."""
    return assess_hw_rev("A", updated, control, control_type, list(failures), TH)


def fired(result: dict) -> set[str]:
    return {s["name"] for s in result["signals"]}


# slope_mb_per_min -------------------------------------------------------------


def test_slope_known_line():
    # 2 MB per minute, offset epoch times
    pts = [(1000.0 + 30 * i, 5 * MB + i * MB) for i in range(6)]
    assert slope_mb_per_min(pts) == pytest.approx(2.0)


def test_slope_negative_and_flat():
    assert slope_mb_per_min([(0, 10 * MB), (60, 9 * MB), (120, 8 * MB)]) == pytest.approx(-1.0)
    assert slope_mb_per_min([(0, MB), (60, MB), (120, MB)]) == pytest.approx(0.0)


def test_slope_least_squares_uneven():
    # hand-computed least squares: Sxy/Sxx = 180 / 7200 MB/s = 1.5 MB/min
    pts = [(0, 0), (60, MB), (120, 3 * MB)]
    assert slope_mb_per_min(pts) == pytest.approx(1.5)


@pytest.mark.parametrize("pts", [[], [(0.0, 1.0)]])
def test_slope_needs_two_points(pts):
    assert slope_mb_per_min(pts) is None


def test_slope_zero_time_span():
    assert slope_mb_per_min([(5.0, 1.0), (5.0, 2.0), (5.0, 3.0)]) is None


# assess_hw_rev: healthy / passthrough -----------------------------------------


def test_healthy_when_everything_evaluable_and_clean():
    r = run([st("u1"), st("u2")], [st("c1")])
    assert r["verdict"] == "healthy"
    assert r["signals"] == [] and r["reasons"] == []
    assert r["hw_rev"] == "A"
    assert r["control_type"] == "control"
    assert r["updated_devices"] == ["u1", "u2"]
    assert r["control_devices"] == ["c1"]
    assert r["restarts_updated"] == 0 and r["oom_updated"] == 0
    assert r["mem_slope_updated"] == 0.0 and r["mem_slope_control"] == 0.0


def test_baseline_control_type_passes_through():
    r = run([st("u1")], [st("u1")], control_type="baseline")
    assert r["control_type"] == "baseline"
    assert r["verdict"] == "healthy"


def test_result_reports_median_slope_and_totals():
    r = run(
        [st("u1", slope=1.0, restarts=1, oom=0), st("u2", slope=3.0, restarts=1, oom=2), st("u3", slope=2.0)],
        [st("c1", slope=0.0, oom=1)],
    )
    assert r["mem_slope_updated"] == 2.0
    assert r["restarts_updated"] == 2
    assert r["oom_updated"] == 2


# oom ---------------------------------------------------------------------------


def test_oom_fires_when_control_has_none():
    r = run([st(oom=1)], [st("c1")])
    assert r["verdict"] == "regression"
    assert "oom" in fired(r)
    sig = next(s for s in r["signals"] if s["name"] == "oom")
    assert sig["updated"] == 1 and sig["control"] == 0


def test_oom_does_not_fire_when_control_also_has_oom():
    r = run([st(oom=1)], [st("c1", oom=1)])
    assert "oom" not in fired(r)
    assert r["verdict"] == "healthy"


def test_oom_fires_without_control_devices():
    assert "oom" in fired(run([st(oom=2)], []))


# crash_loop --------------------------------------------------------------------


def test_crash_loop_fires_at_three_restarts():
    r = run([st("u1", restarts=3), st("u2")], [st("c1")])
    assert "crash_loop" in fired(r)
    sig = next(s for s in r["signals"] if s["name"] == "crash_loop")
    assert sig["updated"] == 3 and "u1" in sig["detail"]


def test_crash_loop_does_not_fire_at_two_restarts():
    r = run([st(restarts=2)], [st("c1", restarts=1)])
    assert "crash_loop" not in fired(r)


def test_crash_loop_fires_even_when_control_restarts_too():
    assert "crash_loop" in fired(run([st(restarts=3)], [st("c1", restarts=5)]))


# memory_slope ------------------------------------------------------------------


def test_memory_slope_fires_above_margin():
    r = run([st(slope=3.0)], [st("c1", slope=0.5)])
    assert fired(r) == {"memory_slope"}
    sig = r["signals"][0]
    assert sig["updated"] == 3.0 and sig["control"] == 0.5


def test_memory_slope_margin_is_strictly_greater_than_one_mb_per_min():
    assert "memory_slope" not in fired(run([st(slope=1.5)], [st("c1", slope=0.5)]))
    assert "memory_slope" not in fired(run([st(slope=1.2)], [st("c1", slope=0.5)]))
    assert "memory_slope" in fired(run([st(slope=1.6)], [st("c1", slope=0.5)]))


def test_memory_slope_uses_medians():
    upd = [st("u1", slope=0.0), st("u2", slope=0.0), st("u3", slope=50.0)]
    assert "memory_slope" not in fired(run(upd, [st("c1", slope=0.0)]))


@pytest.mark.parametrize(
    "kwargs", [{"samples": 4}, {"window_s": 59.0}, {"slope": None}], ids=["samples", "window", "none"]
)
def test_memory_slope_skipped_when_updated_not_evaluable(kwargs):
    r = run([st(**{"slope": 9.0, **kwargs})], [st("c1", slope=0.0)])
    assert "memory_slope" not in fired(r)


def test_memory_slope_needs_five_samples_and_sixty_seconds_boundary():
    ok = run([st(slope=9.0, samples=5, window_s=60.0)], [st("c1", slope=0.0)])
    assert "memory_slope" in fired(ok)


def test_memory_slope_skipped_when_control_not_evaluable():
    r = run([st(slope=9.0)], [st("c1", slope=0.0, samples=4)])
    assert "memory_slope" not in fired(r)


def test_memory_slope_skipped_without_control():
    assert "memory_slope" not in fired(run([st(slope=9.0)], []))


# restarts ----------------------------------------------------------------------


def test_restarts_fire_when_control_has_none():
    r = run([st(restarts=2)], [st("c1")])
    assert fired(r) == {"restarts"}


def test_restarts_fire_when_rate_more_than_twice_control():
    # updated 1/min, control 0.2/min
    r = run([st(restarts=2, window_s=120)], [st("c1", restarts=1, window_s=300)])
    assert "restarts" in fired(r)


def test_restarts_do_not_fire_at_exactly_twice_control_rate():
    # updated 2 per 120 s = 1/min; control 1 per 120 s = 0.5/min
    r = run([st(restarts=2, window_s=120)], [st("c1", restarts=1, window_s=120)])
    assert "restarts" not in fired(r)
    assert r["verdict"] == "healthy"


def test_restarts_do_not_fire_without_restarts_or_control():
    assert "restarts" not in fired(run([st(restarts=0)], [st("c1")]))
    assert "restarts" not in fired(run([st(restarts=2)], []))


# fps ---------------------------------------------------------------------------


def test_fps_fires_below_eighty_percent_of_control():
    r = run([st(fps=20.0)], [st("c1", fps=30.0)])
    assert fired(r) == {"fps"}


def test_fps_does_not_fire_at_or_above_eighty_percent():
    assert "fps" not in fired(run([st(fps=25.0)], [st("c1", fps=30.0)]))
    assert "fps" not in fired(run([st(fps=35.0)], [st("c1", fps=30.0)]))


def test_fps_skipped_when_either_side_missing():
    assert "fps" not in fired(run([st(fps=None)], [st("c1", fps=30.0)]))
    assert "fps" not in fired(run([st(fps=1.0)], [st("c1", fps=None)]))
    assert "fps" not in fired(run([st(fps=1.0)], []))


# install -----------------------------------------------------------------------


def test_install_failure_is_regression_even_if_not_installed():
    r = run([st("u1", installed=False, samples=0), st("u2")], [st("c1")], failures=["u1"])
    assert r["verdict"] == "regression"
    assert fired(r) == {"install"}
    sig = r["signals"][0]
    assert sig["updated"] == 1 and "u1" in sig["detail"]
    assert r["reasons"] == []


def test_no_install_failure_no_signal():
    assert "install" not in fired(run([st()], [st("c1")], failures=[]))


# metric signals only use installed devices -------------------------------------


def test_uninstalled_devices_do_not_contribute_metrics():
    r = run([st("u1"), st("u2", installed=False, oom=5, restarts=9)], [st("c1")])
    assert fired(r) == set()
    assert r["verdict"] == "inconclusive"


# inconclusive ------------------------------------------------------------------


def test_inconclusive_when_device_not_installed():
    r = run([st("u1"), st("u2", installed=False)], [st("c1")])
    assert r["verdict"] == "inconclusive"
    assert any("not installed" in x for x in r["reasons"])


def test_inconclusive_when_no_device_installed():
    r = run([st("u1", installed=False, samples=0)], [st("c1")])
    assert r["verdict"] == "inconclusive"
    assert r["reasons"]
    assert r["mem_slope_updated"] is None


def test_inconclusive_when_too_few_samples():
    r = run([st(samples=2, slope=None)], [st("c1")])
    assert r["verdict"] == "inconclusive"
    assert any("fewer than 3 samples" in x for x in r["reasons"])


def test_three_samples_is_enough_for_min_samples_but_not_slope():
    r = run([st(samples=3)], [st("c1")])
    assert r["verdict"] == "inconclusive"
    assert not any("fewer than" in x for x in r["reasons"])
    assert any("slope not evaluable" in x for x in r["reasons"])


def test_inconclusive_when_slope_not_evaluable_with_four_samples():
    r = run([st(samples=4)], [st("c1")])
    assert r["verdict"] == "inconclusive"
    assert any("memory slope not evaluable" in x for x in r["reasons"])


def test_inconclusive_when_window_too_short_for_slope():
    r = run([st(window_s=30.0)], [st("c1")])
    assert r["verdict"] == "inconclusive"
    assert any("slope not evaluable" in x for x in r["reasons"])


def test_inconclusive_when_no_updated_devices():
    r = run([], [st("c1")])
    assert r["verdict"] == "inconclusive"
    assert r["reasons"] == ["no updated devices"]


def test_signal_beats_inconclusive():
    r = run([st(oom=1, samples=2), st("u2", installed=False)], [st("c1")])
    assert r["verdict"] == "regression"
    assert r["reasons"] == []


# assess_wave -------------------------------------------------------------------


def test_assess_wave_a_healthy_b_regression():
    a = assess_hw_rev("A", [st("d1", hw_rev="A")], [st("c1", hw_rev="A")], "control", [], TH)
    b = assess_hw_rev("B", [st("d2", hw_rev="B", oom=1)], [st("c2", hw_rev="B")], "control", [], TH)
    w = assess_wave([a, b])
    assert w["verdict"] == "regression"
    assert w["affected_hw_revs"] == ["B"]
    assert set(w["per_hw_rev"]) == {"A", "B"}
    assert w["per_hw_rev"]["A"]["verdict"] == "healthy"
    assert w["per_hw_rev"]["B"]["verdict"] == "regression"


def test_assess_wave_all_healthy():
    a = assess_hw_rev("A", [st()], [st("c1")], "control", [], TH)
    w = assess_wave([a])
    assert w["verdict"] == "healthy" and w["affected_hw_revs"] == []


def test_assess_wave_inconclusive_beats_healthy():
    a = assess_hw_rev("A", [st()], [st("c1")], "control", [], TH)
    b = assess_hw_rev("B", [st("d2", installed=False)], [], "baseline", [], TH)
    w = assess_wave([a, b])
    assert w["verdict"] == "inconclusive" and w["affected_hw_revs"] == []


def test_assess_wave_regression_beats_inconclusive():
    a = assess_hw_rev("A", [st("d1", installed=False)], [], "baseline", [], TH)
    b = assess_hw_rev("B", [st("d2", hw_rev="B", restarts=3)], [], "baseline", [], TH)
    w = assess_wave([a, b])
    assert w["verdict"] == "regression" and w["affected_hw_revs"] == ["B"]


def test_assess_wave_empty_is_inconclusive():
    w = assess_wave([])
    assert w == {"verdict": "inconclusive", "affected_hw_revs": [], "per_hw_rev": {}}


def test_assess_wave_affected_sorted():
    rs = [assess_hw_rev(h, [st(f"d{h}", oom=1)], [], "baseline", [], TH) for h in ("C", "A", "B")]
    assert assess_wave(rs)["affected_hw_revs"] == ["A", "B", "C"]


# assess_rehearsal_device -------------------------------------------------------


def lab(**over) -> dict:
    """A clean lab summary with flat memory."""
    base = {
        "device_id": "lab-A",
        "fw_version": "v1.2",
        "unit_state": "active",
        "restarts": 0,
        "oom_kills": 0,
        "memory_samples": [{"t": t, "bytes": 100 * MB} for t in (0, 30, 60, 90)],
        "last_install": {"result": "success", "detail": ""},
    }
    base.update(over)
    return base


def rising(mb_per_min: float) -> list[dict]:
    return [{"t": t, "bytes": 100 * MB + t / 60 * mb_per_min * MB} for t in (0, 30, 60, 90)]


def test_rehearsal_device_pass():
    r = assess_rehearsal_device(lab(), TH)
    assert r["verdict"] == "pass"
    assert r["reasons"] == []
    assert r["install_result"] == "success"
    assert r["restarts"] == 0 and r["oom_kills"] == 0
    assert r["unit_state"] == "active"
    assert r["mem_slope_mb_per_min"] == 0.0


@pytest.mark.parametrize("result", ["ok", "installed", "SUCCESS", "Succeeded"])
def test_rehearsal_device_accepts_install_ok_values(result):
    assert assess_rehearsal_device(lab(last_install={"result": result}), TH)["verdict"] == "pass"


@pytest.mark.parametrize("last_install", [{"result": "failure", "detail": "boom"}, {}, None])
def test_rehearsal_device_fails_on_install_failed_or_missing(last_install):
    r = assess_rehearsal_device(lab(last_install=last_install), TH)
    assert r["verdict"] == "fail"
    assert any(x.startswith("install_failed") for x in r["reasons"])


def test_rehearsal_device_fails_when_last_install_key_absent():
    s = lab()
    del s["last_install"]
    r = assess_rehearsal_device(s, TH)
    assert r["verdict"] == "fail" and r["install_result"] is None


@pytest.mark.parametrize("unit", ["failed", "activating", None])
def test_rehearsal_device_fails_when_unit_not_active(unit):
    r = assess_rehearsal_device(lab(unit_state=unit), TH)
    assert r["verdict"] == "fail"
    assert any(x.startswith("unit_not_active") for x in r["reasons"])


def test_rehearsal_device_fails_on_restarts():
    r = assess_rehearsal_device(lab(restarts=1), TH)
    assert r["verdict"] == "fail"
    assert r["reasons"] == ["restarts: 1"]


def test_rehearsal_device_fails_on_oom():
    r = assess_rehearsal_device(lab(oom_kills=1), TH)
    assert r["verdict"] == "fail"
    assert r["reasons"] == ["oom_kills: 1"]


def test_rehearsal_device_fails_when_slope_over_threshold():
    r = assess_rehearsal_device(lab(memory_samples=rising(3.0)), TH)
    assert r["verdict"] == "fail"
    assert r["mem_slope_mb_per_min"] == pytest.approx(3.0)
    assert any(x.startswith("memory_slope") for x in r["reasons"])


def test_rehearsal_device_slope_at_threshold_passes():
    r = assess_rehearsal_device(lab(memory_samples=rising(0.9)), TH)
    assert r["verdict"] == "pass"


def test_rehearsal_device_one_sample_is_inconclusive():
    r = assess_rehearsal_device(lab(memory_samples=[{"t": 0, "bytes": 1}]), TH)
    assert r["verdict"] == "inconclusive" and r["mem_slope_mb_per_min"] is None
    assert r["reasons"] == ["memory slope not evaluable: 1 sample(s)"]


def test_rehearsal_device_no_samples_key_is_inconclusive():
    s = lab()
    del s["memory_samples"]
    r = assess_rehearsal_device(s, TH)
    assert r["verdict"] == "inconclusive" and r["reasons"] == ["memory slope not evaluable: 0 sample(s)"]


def test_rehearsal_device_fail_reason_beats_few_samples():
    r = assess_rehearsal_device(lab(restarts=1, memory_samples=[{"t": 0, "bytes": 1}]), TH)
    assert r["verdict"] == "fail" and r["reasons"] == ["restarts: 1"]


def test_rehearsal_device_collects_all_reasons():
    r = assess_rehearsal_device(
        lab(unit_state="failed", restarts=2, oom_kills=1, last_install={"result": "failure"}), TH
    )
    assert len(r["reasons"]) == 4


def test_rehearsal_device_handles_null_counters():
    r = assess_rehearsal_device(lab(restarts=None, oom_kills=None), TH)
    assert r["verdict"] == "pass" and r["restarts"] == 0 and r["oom_kills"] == 0


# assess_rehearsal --------------------------------------------------------------


def test_assess_rehearsal_overall():
    ok = assess_rehearsal_device(lab(), TH)
    bad = assess_rehearsal_device(lab(restarts=1), TH)
    few = assess_rehearsal_device(lab(memory_samples=[]), TH)
    assert assess_rehearsal({"A": ok, "B": ok}) == "pass"
    assert assess_rehearsal({"A": ok, "B": bad}) == "fail"
    assert assess_rehearsal({"A": bad}) == "fail"
    assert assess_rehearsal({"A": ok, "B": few}) == "inconclusive"
    assert assess_rehearsal({"A": few, "B": bad, "C": ok}) == "fail"
    assert assess_rehearsal({}) == "inconclusive"


# fail closed: no comparison data ------------------------------------------------


def test_no_control_and_no_baseline_is_inconclusive():
    r = run([st("u1"), st("u2")], [], control_type="baseline")
    assert r["verdict"] == "inconclusive"
    assert "no control or baseline metrics: cannot compare updated devices" in r["reasons"]


def test_no_metrics_reason_wording():
    r = run([st("u1"), st("u2", installed=False)], [st("c1")])
    assert r["verdict"] == "inconclusive"
    assert "1 device(s) have no metrics yet (not installed, or Prometheus has no data)" in r["reasons"]


def test_inconclusive_hw_rev_shape_matches_assess_hw_rev():
    r = inconclusive_hw_rev("A", ["metrics_unavailable: x"], ["u2", "u1"])
    assert r["verdict"] == "inconclusive" and r["signals"] == [] and r["control_type"] == "unavailable"
    assert r["updated_devices"] == ["u1", "u2"]
    assert set(r) == set(run([st()], [st("c1")]))
    assert assess_wave([r])["verdict"] == "inconclusive"
