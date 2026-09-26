"""Scenario tests for the Fleet service layer (docs/agent-design.md section 10.1). No network, no real sleeps."""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

from agent.fleet_mcp import ledger as ledger_mod
from agent.fleet_mcp.errors import PreconditionError
from agent.fleet_mcp.models import Device

from .fakes import Env, build_fleet, make_devices

run = asyncio.run


@pytest.fixture(autouse=True)
def fast_sqlite(monkeypatch):
    """Skip fsync: these tests issue many ledger writes and durability is not under test."""
    real_connect = sqlite3.connect

    def connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        conn.execute("PRAGMA synchronous=OFF")
        return conn

    monkeypatch.setattr(ledger_mod.sqlite3, "connect", connect)


@pytest.fixture
def env(tmp_path) -> Env:
    return build_fleet(tmp_path, make_devices(3, 3))


# helpers -----------------------------------------------------------------------


def refused(code: str, fn, *args, **kwargs) -> PreconditionError:
    """Call fn and expect a PreconditionError with `code`."""
    with pytest.raises(PreconditionError) as exc:
        fn(*args, **kwargs)
    assert exc.value.code == code, exc.value
    return exc.value


def refused_async(code: str, coro_fn, *args, **kwargs) -> PreconditionError:
    return refused(code, lambda: run(coro_fn(*args, **kwargs)))


def event_kinds(env: Env, pid: str) -> list[tuple]:
    """(type, action kind, wave) of every non-error event, in order."""
    return [(e.type, e.payload.get("kind"), e.wave) for e in env.ledger.events(pid) if e.type != "error"]


def error_codes(env: Env, pid: str) -> list[str]:
    return [e.payload["code"] for e in env.ledger.events(pid, types=["error"])]


def plan_and_rehearse(env: Env, waves=(2, "rest"), version="v1.2", minutes=None):
    pid = env.fleet.plan_rollout(version, list(waves))["plan_id"]
    return pid, run(env.fleet.rehearse(pid, minutes=minutes))


def wave_of(env: Env, pid: str, n: int, hw: str) -> list[str]:
    return [d for d in env.ledger.get_plan(pid).waves[n - 1] if d.startswith(hw.lower())]


def start_first_wave(env: Env, waves=(2, "rest")):
    pid, rev = plan_and_rehearse(env, waves)
    env.fleet.start_wave(pid, 1, rev["evidence_id"])
    return pid


def regression_setup(env: Env):
    """Plan, rehearse, start wave 1, make its rev B updated device leak; return (pid, b_updated_id)."""
    pid = start_first_wave(env)
    (b_upd,) = wave_of(env, pid, 1, "B")
    env.metrics.set(b_upd, "v1.2", mem_slope_mb_per_min=8.0, oom_kills=1)
    return pid, b_upd


def observe(env: Env, pid: str, wave: int = 1, minutes: float | None = None) -> dict:
    return run(env.fleet.observe_wave(pid, wave, minutes))


def halted(env: Env):
    """Regression on rev B in wave 1, observed and halted; return (pid, b_updated_id, observation)."""
    pid, b_upd = regression_setup(env)
    obs = observe(env, pid)
    env.fleet.halt_rollout(pid, obs["evidence_id"])
    return pid, b_upd, obs


# 1. happy path -------------------------------------------------------------------


def test_happy_path(env):
    plan = env.fleet.plan_rollout("v1.2", [2, "rest"])
    pid = plan["plan_id"]
    assert plan["phase"] == "PLANNED" and plan["from_version"] == "v1.1" and plan["next"] == "rehearse"
    assert plan["waves"][0]["by_hw_rev"] == {"A": 1, "B": 1}
    assert plan["waves"][1]["size"] == 4 and plan["excluded"] == []

    rev = run(env.fleet.rehearse(pid))
    assert rev["verdict"] == "pass" and rev["phase"] == "REHEARSED"
    assert set(rev["per_hw_rev"]) == {"A", "B"} and "later onset" in rev["caveat"]
    assert env.lab.alive == 0 and env.lab._devices == {}

    wave1 = list(env.ledger.get_plan(pid).waves[0])
    started = env.fleet.start_wave(pid, 1, rev["evidence_id"])
    assert started["phase"] == "WAVE_RUNNING" and started["rollout_id"] == 1
    assert env.field.start_wave_calls == [(f"{pid}-wave1", "v1.2", wave1)]
    (approval,) = env.ledger.events(pid, types=["approval"])
    assert approval.payload == {
        "tool": "start_wave",
        "approved_via": "harness",
        "evidence_id": rev["evidence_id"],
    }

    obs1 = observe(env, pid, 1)
    assert obs1["verdict"] == "healthy" and obs1["phase"] == "WAVE_OBSERVED"
    assert obs1["affected_hw_revs"] == [] and obs1["per_hw_rev"]["A"]["control_type"] == "control"
    assert "start_wave 2" in obs1["next"]

    wave2 = list(env.ledger.get_plan(pid).waves[1])
    started2 = env.fleet.start_wave(pid, 2, obs1["evidence_id"])
    assert started2["rollout_id"] == 2
    assert env.field.start_wave_calls[1] == (f"{pid}-wave2", "v1.2", wave2)
    obs2 = observe(env, pid, 2)
    assert obs2["verdict"] == "healthy" and obs2["phase"] == "COMPLETE"
    assert "complete" in obs2["next"]

    assert env.ledger.get_plan(pid).phase == "COMPLETE"
    assert event_kinds(env, pid) == [
        ("action", "plan_created", None),
        ("rehearsal", None, None),
        ("approval", None, 1),
        ("action", "wave_started", 1),
        ("observation", None, 1),
        ("approval", None, 2),
        ("action", "wave_started", 2),
        ("observation", None, 2),
    ]
    assert error_codes(env, pid) == []
    assert set(env.time.sleeps) == {15}  # soaks are chunked by the poll interval and only advance FakeTime


def test_observe_minutes_clamped_and_default(env):
    pid = start_first_wave(env)
    before = env.time.now()
    obs = observe(env, pid, 1, minutes=60)
    assert obs["window_minutes"] == 4.0
    assert env.time.now() - before == pytest.approx(240)


# 2. regression on rev B -----------------------------------------------------------


def test_regression_halt_rollback_verify(env):
    pid, b_upd = regression_setup(env)
    (a_upd,) = wave_of(env, pid, 1, "A")
    obs = observe(env, pid)
    assert obs["verdict"] == "regression" and obs["affected_hw_revs"] == ["B"]
    assert obs["per_hw_rev"]["A"]["verdict"] == "healthy"
    b = obs["per_hw_rev"]["B"]
    assert b["control_type"] == "control" and b["oom_updated"] == 1
    assert {s["name"] for s in b["signals"]} >= {"oom", "memory_slope"}
    assert obs["phase"] == "WAVE_OBSERVED" and "halt_rollout" in obs["next"]

    refused("EVIDENCE_VERDICT", env.fleet.start_wave, pid, 2, obs["evidence_id"])
    assert len(env.field.start_wave_calls) == 1

    halt = env.fleet.halt_rollout(pid, obs["evidence_id"])
    assert halt["phase"] == "HALTED"
    assert env.field.stop_calls == [[1]]  # the wave-1 rollout id

    refused("TARGET_NOT_ELIGIBLE", env.fleet.rollback, pid, "v1.1", obs["evidence_id"], targets=[a_upd])
    assert env.field.assign_calls == []
    assert env.ledger.get_plan(pid).phase == "HALTED"

    rb = env.fleet.rollback(pid, "v1.1", obs["evidence_id"], cohort={"hw_rev": "B"})
    assert rb["phase"] == "ROLLED_BACK" and rb["targets"] == [b_upd] and rb["failed"] == []
    assert env.field.assign_calls == [(b_upd, "v1.1")]

    ver = run(env.fleet.verify_recovery(pid))
    assert ver["verdict"] == "recovered" and ver["phase"] == "VERIFIED"
    assert ver["per_device"][b_upd]["recovered"] is True
    assert ver["per_device"][b_upd]["installed_version"] == "v1.1"

    types = [e.type for e in env.ledger.events(pid)]
    assert types.count("rehearsal") == 1 and types.count("observation") == 1
    assert types.count("verification") == 1
    approvals = env.ledger.events(pid, types=["approval"])
    assert [a.payload["tool"] for a in approvals] == ["start_wave", "halt_rollout", "rollback"]
    kinds = [e.payload["kind"] for e in env.ledger.events(pid, types=["action"])]
    assert kinds == ["plan_created", "wave_started", "halt", "rollback"]

    state = env.fleet.get_rollout_state(pid)
    assert state["phase"] == "VERIFIED"
    assert [w["status"] for w in state["waves"]] == ["observed", "pending"]
    assert state["waves"][0]["observation_verdict"] == "regression"
    assert state["devices"][b_upd]["installed_version"] == "v1.1"
    assert [a["kind"] for a in state["actions"]][-1] == "rollback"
    assert "plan_rollout" in state["next_allowed"]


def test_rollback_rejects_wrong_version_and_mixed_arguments(env):
    pid, _b_upd, obs = halted(env)
    refused(
        "WRONG_TARGET_VERSION", env.fleet.rollback, pid, "v1.0", obs["evidence_id"], cohort={"hw_rev": "B"}
    )
    refused("BAD_ARGUMENT", env.fleet.rollback, pid, "v1.1", obs["evidence_id"])
    refused("BAD_ARGUMENT", env.fleet.rollback, pid, "v1.1", obs["evidence_id"], {"hw_rev": "B"}, ["b1"])
    assert env.field.assign_calls == []


# 3. rehearsal scenarios -----------------------------------------------------------


def test_rehearsal_crash_blocks_plan(tmp_path):
    env = build_fleet(tmp_path, make_devices(3, 3), scenarios={"v1.3": "crash"})
    pid, rev = plan_and_rehearse(env, version="v1.3")
    assert rev["verdict"] == "fail" and rev["phase"] == "BLOCKED"
    assert rev["per_hw_rev"]["A"]["unit_state"] == "failed"
    assert any("restarts" in r for r in rev["per_hw_rev"]["A"]["reasons"])
    assert "BLOCKED" in rev["next"]
    assert env.ledger.get_plan(pid).phase == "BLOCKED"
    refused("BAD_PHASE", env.fleet.start_wave, pid, 1, rev["evidence_id"])
    assert env.field.start_wave_calls == []
    assert env.lab._devices == {}


def test_rehearsal_leak_onset_later_than_window(tmp_path):
    scen = {"v1.2:B": "leak"}
    short = build_fleet(tmp_path / "s", make_devices(2, 2), scenarios=scen)
    _pid, ok = plan_and_rehearse(short, minutes=2)
    assert ok["verdict"] == "pass" and ok["per_hw_rev"]["B"]["verdict"] == "pass"

    long = build_fleet(tmp_path / "l", make_devices(2, 2), scenarios=scen)
    pid, bad = plan_and_rehearse(long, minutes=5)
    assert bad["verdict"] == "fail" and bad["phase"] == "BLOCKED"
    assert bad["per_hw_rev"]["B"]["verdict"] == "fail" and bad["per_hw_rev"]["A"]["verdict"] == "pass"
    assert bad["per_hw_rev"]["B"]["oom_kills"] >= 1
    assert long.lab._devices == {} and short.lab._devices == {}
    assert long.ledger.get_plan(pid).phase == "BLOCKED"


def test_rehearse_minutes_clamped_to_lab_max(env):
    pid = env.fleet.plan_rollout("v1.2", [2, "rest"])["plan_id"]
    out = run(env.fleet.rehearse(pid, minutes=99))
    assert out["window_minutes"] == 5.0


def test_rehearse_hw_rev_subset_and_bad_argument(env):
    pid = env.fleet.plan_rollout("v1.2", [2, "rest"])["plan_id"]
    refused_async("BAD_ARGUMENT", env.fleet.rehearse, pid, ["Z"])
    out = run(env.fleet.rehearse(pid, ["A"]))
    assert list(out["per_hw_rev"]) == ["A"] and out["not_rehearsed"] == ["B"]
    assert env.lab.create_calls == 1


# 4. stale evidence ----------------------------------------------------------------


def test_stale_rehearsal_evidence_then_rerehearse(env):
    pid, rev = plan_and_rehearse(env)
    env.time.advance(1000)
    refused("EVIDENCE_STALE", env.fleet.start_wave, pid, 1, rev["evidence_id"])
    assert env.field.start_wave_calls == []
    rev2 = run(env.fleet.rehearse(pid))  # REHEARSED phase allows a re-run
    assert rev2["phase"] == "REHEARSED" and rev2["evidence_id"] != rev["evidence_id"]
    refused("EVIDENCE_SUPERSEDED", env.fleet.start_wave, pid, 1, rev["evidence_id"])
    assert env.fleet.start_wave(pid, 1, rev2["evidence_id"])["phase"] == "WAVE_RUNNING"


def test_stale_regression_evidence_in_halted_is_refreshed_by_observe(env):
    pid, b_upd, obs = halted(env)
    env.time.advance(1000)
    refused("EVIDENCE_STALE", env.fleet.rollback, pid, "v1.1", obs["evidence_id"], cohort={"hw_rev": "B"})
    fresh = observe(env, pid, 1)
    assert fresh["verdict"] == "regression" and fresh["phase"] == "HALTED"
    assert env.ledger.get_plan(pid).phase == "HALTED"
    assert "rollback" in fresh["next"]
    refused(
        "EVIDENCE_SUPERSEDED", env.fleet.rollback, pid, "v1.1", obs["evidence_id"], cohort={"hw_rev": "B"}
    )
    rb = env.fleet.rollback(pid, "v1.1", fresh["evidence_id"], cohort={"hw_rev": "B"})
    assert rb["phase"] == "ROLLED_BACK" and rb["targets"] == [b_upd]


# 5. inconclusive ------------------------------------------------------------------


def test_inconclusive_when_device_not_installed(env):
    pid = start_first_wave(env)
    (a_upd,) = wave_of(env, pid, 1, "A")
    env.metrics.set(a_upd, "v1.2", installed=False)
    obs = observe(env, pid)
    assert obs["verdict"] == "inconclusive" and obs["phase"] == "WAVE_OBSERVED"
    a = obs["per_hw_rev"]["A"]
    assert a["verdict"] == "inconclusive" and a["reasons"] and "not installed" in a["reasons"][0]
    assert "extend the soak" in obs["next"]
    refused("EVIDENCE_VERDICT", env.fleet.start_wave, pid, 2, obs["evidence_id"])
    # extending the soak once the device installed gives a healthy verdict and supersedes the old one
    env.metrics.set(a_upd, "v1.2", installed=True)
    obs2 = observe(env, pid)
    assert obs2["verdict"] == "healthy"
    assert env.fleet.start_wave(pid, 2, obs2["evidence_id"])["wave"] == 2


def test_install_failure_reported_by_hawkbit_is_a_regression(env):
    pid = start_first_wave(env)
    (a_upd,) = wave_of(env, pid, 1, "A")
    env.field.failures.add(a_upd)
    env.field.statuses[a_upd] = "error"
    obs = observe(env, pid)
    assert obs["verdict"] == "regression" and obs["affected_hw_revs"] == ["A"]
    assert obs["install_status"][a_upd] == "error"
    assert [s["name"] for s in obs["per_hw_rev"]["A"]["signals"]] == ["install"]


# 6. baseline control --------------------------------------------------------------


def test_baseline_control_when_no_control_devices_remain(tmp_path):
    env = build_fleet(tmp_path, make_devices(2, 2))
    pid = start_first_wave(env, waves=(2, "rest"))
    obs1 = observe(env, pid, 1)
    assert obs1["verdict"] == "healthy" and obs1["per_hw_rev"]["B"]["control_type"] == "control"
    env.fleet.start_wave(pid, 2, obs1["evidence_id"])
    (b2,) = wave_of(env, pid, 2, "B")
    env.metrics.set(b2, "v1.2", mem_slope_mb_per_min=8.0, oom_kills=1)  # v1.1 stats stay flat
    n_calls = len(env.metrics.calls)
    obs2 = observe(env, pid, 2)
    assert obs2["verdict"] == "regression" and obs2["affected_hw_revs"] == ["B"]
    assert obs2["per_hw_rev"]["B"]["control_type"] == "baseline"
    assert obs2["per_hw_rev"]["A"]["control_type"] == "baseline"
    assert obs2["per_hw_rev"]["A"]["verdict"] == "healthy"
    assert {"oom", "memory_slope"} <= {s["name"] for s in obs2["per_hw_rev"]["B"]["signals"]}
    baseline_calls = [c for c in env.metrics.calls[n_calls:] if c["fw_version"] == "v1.1"]
    assert baseline_calls and all(c["end"] - c["start"] == 600 for c in baseline_calls)


# 7. plan_rollout ------------------------------------------------------------------


def mixed_fleet() -> list[Device]:
    devs = make_devices(3, 3)  # 6 on v1.1
    devs.append(Device("done1", "A", "eu", "v1.2"))
    devs.append(Device("old1", "B", "us", "v1.0"))
    devs.append(Device("none1", "A", "us", "none"))
    return devs


def test_plan_excludes_devices_with_reasons(tmp_path):
    env = build_fleet(tmp_path, mixed_fleet())
    plan = env.fleet.plan_rollout("v1.2")
    assert plan["from_version"] == "v1.1"
    reasons = {e["id"]: e["reason"] for e in plan["excluded"]}
    assert reasons == {
        "done1": "already on v1.2",
        "none1": "no installed version reported",
        "old1": "on v1.0, plan upgrades from v1.1",
    }
    planned = {d for w in plan["waves"] for d in w["devices"]}
    assert planned == {d.id for d in make_devices(3, 3)}


def test_plan_wave_argument_variants(tmp_path):
    env = build_fleet(tmp_path, make_devices(6, 6))
    default = env.fleet.plan_rollout("v1.2")
    assert [w["size"] for w in default["waves"]] == [2, 5, 5]
    small = env.fleet.plan_rollout("v1.2", [1, 1])
    assert [w["size"] for w in small["waves"]] == [1, 1]
    assert any("not_stratified" in w for w in small["warnings"])
    assert len(small["excluded"]) == 10
    assert {e["reason"] for e in small["excluded"]} == {"beyond the requested wave sizes"}
    rest = env.fleet.plan_rollout("v1.2", ["rest"])
    assert [w["size"] for w in rest["waves"]] == [12]
    region = env.fleet.plan_rollout("v1.2", [4, "rest"], stratify_by="region")
    assert [w["size"] for w in region["waves"]] == [4, 8]
    refused("BAD_ARGUMENT", env.fleet.plan_rollout, "v1.2", ["rest", 2])
    refused("BAD_ARGUMENT", env.fleet.plan_rollout, "v1.2", [0])
    refused("BAD_ARGUMENT", env.fleet.plan_rollout, "v1.2", [2], "bogus")


def test_plan_unknown_version_and_backend_error(env):
    err = refused("UNKNOWN_VERSION", env.fleet.plan_rollout, "v9.9")
    assert "v9.9" in err.message
    env.field.errors["distribution_set_id"] = RuntimeError("hawkbit down")
    refused("BACKEND_ERROR", env.fleet.plan_rollout, "v1.2")


def test_plan_bad_argument_when_no_eligible_devices(tmp_path):
    env = build_fleet(tmp_path, [Device("x1", "A", "eu", "v1.2"), Device("x2", "B", "eu", "none")])
    refused("BAD_ARGUMENT", env.fleet.plan_rollout, "v1.2")


def test_plan_refused_while_wave_running_and_superseded_when_unstarted(env):
    first = env.fleet.plan_rollout("v1.2", [2, "rest"])["plan_id"]
    second = env.fleet.plan_rollout("v1.2", [3, "rest"])["plan_id"]
    assert second != first
    assert env.ledger.get_plan(first).phase == "BLOCKED"
    assert ("action", "superseded", None) in event_kinds(env, first)
    assert env.fleet.get_rollout_state()["plan_id"] == second

    rev = run(env.fleet.rehearse(second))
    third = env.fleet.plan_rollout("v1.2")["plan_id"]  # REHEARSED plans are superseded too
    assert env.ledger.get_plan(second).phase == "BLOCKED" and third != second

    rev = run(env.fleet.rehearse(third))
    env.fleet.start_wave(third, 1, rev["evidence_id"])
    err = refused("ACTIVE_PLAN_EXISTS", env.fleet.plan_rollout, "v1.2")
    assert third in err.message
    assert env.ledger.get_plan(third).phase == "WAVE_RUNNING"


# 8. reads and decisions ------------------------------------------------------------


def test_get_rollout_state_without_plan(env):
    out = env.fleet.get_rollout_state()
    assert out["plan"] is None and "plan_rollout" in out["message"]
    refused("PLAN_NOT_FOUND", env.fleet.get_rollout_state, "plan-nope")


def test_get_rollout_state_planned(env):
    pid = env.fleet.plan_rollout("v1.2", [2, "rest"])["plan_id"]
    s = env.fleet.get_rollout_state()
    assert s["plan_id"] == pid and s["phase"] == "PLANNED"
    assert [w["status"] for w in s["waves"]] == ["pending", "pending"]
    assert s["next_allowed"] == ["rehearse", "plan_rollout", "record_decision"]
    assert s["evidence"] == [] and s["approvals"] == [] and s["recent_errors"] == []
    assert s["devices"]["a1"]["installed_version"] == "v1.1" and s["devices"]["a1"]["planned_wave"] in (1, 2)


def test_get_rollout_state_progress_and_evidence_freshness(env):
    pid, rev = plan_and_rehearse(env)
    s = env.fleet.get_rollout_state(pid)
    assert s["phase"] == "REHEARSED" and s["next_allowed"][:2] == ["rehearse", "start_wave"]
    assert s["evidence"][0]["fresh"] is True and s["evidence"][0]["age_s"] == 0

    env.fleet.start_wave(pid, 1, rev["evidence_id"])
    s = env.fleet.get_rollout_state(pid)
    assert s["waves"][0]["status"] == "running" and s["waves"][0]["rollout_id"] == 1
    assert s["next_allowed"] == ["observe_wave", "record_decision"]
    assert s["approvals"][0]["tool"] == "start_wave" and s["actions"][-1]["kind"] == "wave_started"
    assert s["actions"][-1]["n_devices"] == 2

    obs = observe(env, pid)
    s = env.fleet.get_rollout_state(pid)
    assert s["waves"][0]["status"] == "observed"
    assert s["waves"][0]["observation_evidence_id"] == obs["evidence_id"]
    assert s["next_allowed"] == ["observe_wave", "start_wave", "halt_rollout", "record_decision"]

    env.time.advance(1000)
    rows = {e["evidence_id"]: e for e in env.fleet.get_rollout_state(pid)["evidence"]}
    assert rows[obs["evidence_id"]]["fresh"] is False and rows[obs["evidence_id"]]["age_s"] == 1000
    assert rows[rev["evidence_id"]]["age_s"] == 1120  # rehearsal finished 120 s before the observation


def test_get_rollout_state_tolerates_hawkbit_error(env):
    pid = env.fleet.plan_rollout("v1.2", [2, "rest"])["plan_id"]
    env.field.errors["installed_versions"] = RuntimeError("hawkbit down")
    s = env.fleet.get_rollout_state(pid)
    assert "hawkbit down" in s["hawkbit_error"] and s["phase"] == "PLANNED"
    assert "installed_version" not in s["devices"]["a1"] and s["devices"]["a1"]["hw_rev"] == "A"


def test_record_decision_and_refusals(env):
    pid, rev = plan_and_rehearse(env)
    out = env.fleet.record_decision(pid, "proceed", "rehearsal passed", [rev["evidence_id"]])
    assert out["ok"] is True and out["event_id"]
    (dec,) = env.ledger.events(pid, types=["decision"])
    assert dec.payload == {
        "decision": "proceed",
        "rationale": "rehearsal passed",
        "evidence_ids": [rev["evidence_id"]],
    }
    state = env.fleet.get_rollout_state(pid)
    assert state["decisions"][0]["decision"] == "proceed"

    refused("BAD_ARGUMENT", env.fleet.record_decision, pid, "   ", "why")
    refused("EVIDENCE_MISSING", env.fleet.record_decision, pid, "x", "y", ["ev-00000000"])
    refused("PLAN_NOT_FOUND", env.fleet.record_decision, "plan-nope", "x", "y")
    assert error_codes(env, pid) == ["BAD_ARGUMENT", "EVIDENCE_MISSING"]

    second = env.fleet.plan_rollout("v1.2", [2, "rest"])["plan_id"]  # supersedes the first
    refused("EVIDENCE_WRONG_PLAN", env.fleet.record_decision, second, "x", "y", [rev["evidence_id"]])
    assert error_codes(env, second) == ["EVIDENCE_WRONG_PLAN"]


def test_get_fleet_inventory_counts(tmp_path):
    env = build_fleet(tmp_path, mixed_fleet())
    inv = env.fleet.get_fleet_inventory()
    assert inv["total"] == 9
    assert inv["by_version"] == {"v1.1": 6, "v1.2": 1, "v1.0": 1, "none": 1}
    assert inv["by_hw_rev"] == {"A": 5, "B": 4}
    assert inv["by_region"] == {"eu": 5, "us": 4}
    assert inv["by_version_hw_rev"]["v1.1"] == {"A": 3, "B": 3}
    assert len(inv["devices"]) == 9 and "truncated" not in inv


def test_get_fleet_inventory_caps_device_list(tmp_path):
    env = build_fleet(tmp_path, make_devices(60, 60))
    inv = env.fleet.get_fleet_inventory()
    assert inv["total"] == 120 and len(inv["devices"]) == 100 and inv["truncated"] == 20


def test_get_bundle_diff_real_bundles(env):
    diff = env.fleet.get_bundle_diff("v1.1", "v1.2")
    assert diff["from"] == "v1.1" and diff["to"] == "v1.2"
    paths = [f["path"] for f in diff["files"]]
    assert "app/inference_app.py" in paths
    entry = next(f for f in diff["files"] if f["path"] == "app/inference_app.py")
    assert entry["status"] == "changed" and entry["lines_added"] + entry["lines_removed"] > 0
    refused("UNKNOWN_VERSION", env.fleet.get_bundle_diff, "v1.1", "v9.9")


# 9. backend failures -------------------------------------------------------------


def test_start_wave_backend_error_leaves_phase(env):
    pid, rev = plan_and_rehearse(env)
    env.field.errors["start_wave"] = RuntimeError("hawkbit down")
    err = refused("BACKEND_ERROR", env.fleet.start_wave, pid, 1, rev["evidence_id"])
    assert "hawkbit down" in err.message
    assert env.ledger.get_plan(pid).phase == "REHEARSED"
    (e,) = env.ledger.events(pid, types=["error"])
    assert e.payload["code"] == "BACKEND_ERROR" and e.payload["tool"] == "start_wave"
    assert not [x for x in env.ledger.events(pid, types=["action"]) if x.payload["kind"] == "wave_started"]
    del env.field.errors["start_wave"]  # retry with the same evidence works
    assert env.fleet.start_wave(pid, 1, rev["evidence_id"])["phase"] == "WAVE_RUNNING"


def test_lab_create_failure(env):
    pid = env.fleet.plan_rollout("v1.2", [2, "rest"])["plan_id"]
    env.lab.fail_create_on = 2  # rev A device already exists when rev B creation fails
    err = refused_async("LAB_ERROR", env.fleet.rehearse, pid)
    assert "lab controller unreachable" in err.message
    assert env.lab.create_calls == 2 and env.lab.alive == 0 and env.lab._devices == {}
    assert env.ledger.events(pid, types=["rehearsal"]) == []
    assert env.ledger.get_plan(pid).phase == "PLANNED"
    assert error_codes(env, pid) == ["LAB_ERROR"]


def test_lab_summary_failure_destroys_devices(env):
    pid = env.fleet.plan_rollout("v1.2", [2, "rest"])["plan_id"]
    env.lab.fail_summary = True
    refused_async("LAB_ERROR", env.fleet.rehearse, pid)
    assert env.lab._devices == {} and env.lab.destroyed == ["mock-lab-001"]
    assert env.ledger.get_plan(pid).phase == "PLANNED"


def test_rollback_all_assigns_fail(env):
    pid, _b_upd, obs = halted(env)
    env.field.assign_fail = {"*"}
    refused("BACKEND_ERROR", env.fleet.rollback, pid, "v1.1", obs["evidence_id"], cohort={"hw_rev": "B"})
    assert env.ledger.get_plan(pid).phase == "HALTED"
    assert "BACKEND_ERROR" in error_codes(env, pid)
    kinds = [e.payload["kind"] for e in env.ledger.events(pid, types=["action"])]
    assert "rollback" not in kinds


def test_rollback_partial_failure_is_reported(tmp_path):
    env = build_fleet(tmp_path, make_devices(3, 3))
    pid = start_first_wave(env, waves=(4, "rest"))  # wave 1: two A and two B devices
    b_ids = wave_of(env, pid, 1, "B")
    assert len(b_ids) == 2
    for b in b_ids:
        env.metrics.set(b, "v1.2", mem_slope_mb_per_min=8.0, oom_kills=1)
    obs = observe(env, pid)
    env.fleet.halt_rollout(pid, obs["evidence_id"])
    env.field.assign_fail = {b_ids[0]}
    rb = env.fleet.rollback(pid, "v1.1", obs["evidence_id"], cohort={"hw_rev": "B"})
    assert [a["device_id"] for a in rb["actions"]] == [b_ids[1]]
    assert rb["failed"][0]["device_id"] == b_ids[0] and rb["phase"] == "ROLLED_BACK"
    refused_async("TARGET_NOT_ELIGIBLE", env.fleet.verify_recovery, pid, [b_ids[0]])


def test_verify_recovery_not_recovered_keeps_phase(env):
    pid, b_upd, obs = halted(env)
    env.fleet.rollback(pid, "v1.1", obs["evidence_id"], cohort={"hw_rev": "B"})
    env.metrics.set(b_upd, "v1.1", restarts=2)
    ver = run(env.fleet.verify_recovery(pid))
    assert ver["verdict"] == "not_recovered" and ver["phase"] == "ROLLED_BACK"
    assert "not recovered" in ver["next"]


def test_halt_backend_error_leaves_phase(env):
    pid, _b_upd = regression_setup(env)
    obs = observe(env, pid)
    env.field.errors["stop_rollouts"] = RuntimeError("hawkbit down")
    refused("BACKEND_ERROR", env.fleet.halt_rollout, pid, obs["evidence_id"])
    assert env.ledger.get_plan(pid).phase == "WAVE_OBSERVED"


# 10. lab_parallel ------------------------------------------------------------------


@pytest.mark.parametrize(("parallel", "max_alive"), [(1, 1), (2, 2)])
def test_lab_parallel_limits_concurrent_lab_devices(tmp_path, parallel, max_alive):
    env = build_fleet(tmp_path, make_devices(2, 2), scenarios={"v1.2:B": "leak"}, lab_parallel=parallel)
    pid, rev = plan_and_rehearse(env, minutes=5)
    assert env.lab.max_alive == max_alive and env.lab.create_calls == 2
    assert env.lab.alive == 0 and len(env.lab.destroyed) == 2
    assert rev["verdict"] == "fail" and rev["per_hw_rev"]["B"]["verdict"] == "fail"
    assert env.ledger.get_plan(pid).phase == "BLOCKED"


def test_lab_parallel_gives_identical_evidence(tmp_path):
    outs = []
    for i, parallel in enumerate((1, 2)):
        env = build_fleet(
            tmp_path / str(i), make_devices(2, 2), scenarios={"v1.2:B": "leak"}, lab_parallel=parallel
        )
        outs.append(plan_and_rehearse(env, minutes=5)[1])
    one, two = outs
    for key in ("verdict", "per_hw_rev", "window_minutes", "phase", "next"):
        assert one[key] == two[key]


# 11. partial rollout (hold a failed hw_rev) -------------------------------------------


def blocked_leak_plan(tmp_path, n=2, waves=(1, "rest")):
    """v1.2 rehearsal fails for rev B only; returns (env, blocked plan id, failing rehearsal evidence id)."""
    env = build_fleet(tmp_path, make_devices(n, n), scenarios={"v1.2:B": "leak"})
    pid, rev = plan_and_rehearse(env, waves=waves, minutes=5)
    assert rev["verdict"] == "fail" and rev["phase"] == "BLOCKED"
    assert rev["per_hw_rev"]["A"]["verdict"] == "pass" and rev["per_hw_rev"]["B"]["verdict"] == "fail"
    return env, pid, rev["evidence_id"]


def test_partial_plan_holds_failed_cohort_and_records_it(tmp_path):
    env, blocked, ev = blocked_leak_plan(tmp_path)
    plan = env.fleet.plan_rollout("v1.2", [1, "rest"], exclude_hw_revs=["B"], exclusion_evidence_id=ev)
    pid = plan["plan_id"]
    assert pid != blocked and plan["phase"] == "PLANNED"
    assert [w["by_hw_rev"] for w in plan["waves"]] == [{"A": 1}, {"A": 1}]
    (held,) = plan["held"]
    assert held["hw_rev"] == "B" and held["device_ids"] == ["b1", "b2"] and held["evidence_id"] == ev
    assert "memory_slope" in held["reason"] or "oom" in held["reason"]
    assert {e["id"]: e["reason"] for e in plan["excluded"]} == {
        "b1": f"held: hw_rev B failed rehearsal {ev}",
        "b2": f"held: hw_rev B failed rehearsal {ev}",
    }
    stored = env.ledger.get_plan(pid)
    assert stored.held[0]["evidence_id"] == ev and set(stored.devices) == {"a1", "a2"}
    (hold_evt,) = env.ledger.events(pid, types=["hold"])
    assert hold_evt.payload["hw_revs"] == ["B"] and hold_evt.payload["source_plan_id"] == blocked
    assert hold_evt.payload["evidence_id"] == ev and hold_evt.payload["device_ids"] == ["b1", "b2"]
    state = env.fleet.get_rollout_state(pid)
    assert state["held"][0]["hw_rev"] == "B" and state["held"][0]["evidence_id"] == ev
    assert state["held"][0]["installed_versions"] == {"b1": "v1.1", "b2": "v1.1"}


def test_partial_rollout_end_to_end_leaves_held_cohort_untouched(tmp_path):
    env, _blocked, ev = blocked_leak_plan(tmp_path)
    pid = env.fleet.plan_rollout("v1.2", [1, "rest"], exclude_hw_revs=["B"], exclusion_evidence_id=ev)[
        "plan_id"
    ]
    rev = run(env.fleet.rehearse(pid, minutes=5))  # only rev A devices are in the plan
    assert rev["verdict"] == "pass" and set(rev["per_hw_rev"]) == {"A"}
    env.fleet.start_wave(pid, 1, rev["evidence_id"])
    obs = run(env.fleet.observe_wave(pid, 1, 2))
    assert obs["verdict"] == "healthy"
    env.fleet.start_wave(pid, 2, obs["evidence_id"])
    obs2 = run(env.fleet.observe_wave(pid, 2, 2))
    assert obs2["verdict"] == "healthy" and obs2["phase"] == "COMPLETE"
    started = [c for c in env.field.start_wave_calls]
    assert len(started) == 2
    assert {d for call in started for d in call[-1]} == {"a1", "a2"}
    state = env.fleet.get_rollout_state(pid)
    assert state["held"][0]["devices"] == ["b1", "b2"]
    assert state["held"][0]["installed_versions"] == {"b1": "v1.1", "b2": "v1.1"}


def test_partial_plan_requires_evidence(tmp_path):
    env, _blocked, _ev = blocked_leak_plan(tmp_path)
    refused("EVIDENCE_MISSING", env.fleet.plan_rollout, "v1.2", None, "hw_rev", ["B"], None)
    refused("EVIDENCE_MISSING", env.fleet.plan_rollout, "v1.2", None, "hw_rev", ["B"], "ev-nope")
    refused("BAD_ARGUMENT", env.fleet.plan_rollout, "v1.2", None, "hw_rev", [], "ev-nope")
    refused("BAD_ARGUMENT", env.fleet.plan_rollout, "v1.2", None, "hw_rev", None, "ev-nope")


def test_partial_plan_evidence_must_show_failure_of_each_held_hw_rev(tmp_path):
    env, _blocked, ev = blocked_leak_plan(tmp_path)
    refused("EVIDENCE_VERDICT", env.fleet.plan_rollout, "v1.2", None, "hw_rev", ["A"], ev)  # A passed
    refused("EVIDENCE_VERDICT", env.fleet.plan_rollout, "v1.2", None, "hw_rev", ["A", "B"], ev)
    refused("EVIDENCE_VERDICT", env.fleet.plan_rollout, "v1.2", None, "hw_rev", ["C"], ev)  # never rehearsed


def test_partial_plan_evidence_must_be_rehearsal_of_same_version(tmp_path):
    env, blocked, ev = blocked_leak_plan(tmp_path)
    refused("EVIDENCE_WRONG_VERSION", env.fleet.plan_rollout, "v1.3", None, "hw_rev", ["B"], ev)
    env.ledger.add_event(blocked, "decision", {"decision": "x"})
    obs = env.ledger.new_evidence(blocked, "observation", "regression", {"version": "v1.2"}, wave=1)
    refused("EVIDENCE_WRONG_KIND", env.fleet.plan_rollout, "v1.2", None, "hw_rev", ["B"], obs.evidence_id)


def test_partial_plan_refuses_unknown_or_total_hold(tmp_path):
    env, _blocked, ev = blocked_leak_plan(tmp_path)
    # a hold of a hw_rev with no eligible device (evidence claims C failed): forge such evidence
    forged = env.ledger.new_evidence(
        env.ledger.latest_plan().plan_id,
        "rehearsal",
        "fail",
        {"version": "v1.2", "per_hw_rev": {"C": {"verdict": "fail", "reasons": ["x"]}}},
    )
    refused(
        "BAD_ARGUMENT",
        env.fleet.plan_rollout,
        "v1.2",
        None,
        "hw_rev",
        ["C"],
        forged.evidence_id,
    )
    # holding every hw_rev leaves nothing to roll out
    both = env.ledger.new_evidence(
        env.ledger.latest_plan().plan_id,
        "rehearsal",
        "fail",
        {
            "version": "v1.2",
            "per_hw_rev": {"A": {"verdict": "fail", "reasons": []}, "B": {"verdict": "fail", "reasons": []}},
        },
    )
    refused("BAD_ARGUMENT", env.fleet.plan_rollout, "v1.2", None, "hw_rev", ["A", "B"], both.evidence_id)
    assert ev  # the original evidence is untouched


def test_failed_hold_creates_no_plan(tmp_path):
    env, blocked, _ev = blocked_leak_plan(tmp_path)
    refused("EVIDENCE_MISSING", env.fleet.plan_rollout, "v1.2", None, "hw_rev", ["B"], None)
    assert env.ledger.latest_plan().plan_id == blocked


def test_partial_plan_still_refused_while_another_plan_runs(tmp_path):
    env = build_fleet(tmp_path, make_devices(2, 2), scenarios={"v1.3": "crash"})
    pid, rev = plan_and_rehearse(env, version="v1.2", minutes=5)
    env.fleet.start_wave(pid, 1, rev["evidence_id"])
    ev = env.ledger.new_evidence(
        pid, "rehearsal", "fail", {"version": "v1.2", "per_hw_rev": {"B": {"verdict": "fail", "reasons": []}}}
    )
    refused("ACTIVE_PLAN_EXISTS", env.fleet.plan_rollout, "v1.2", None, "hw_rev", ["B"], ev.evidence_id)


# 12. rehearsal coverage of start_wave -----------------------------------------------------


def test_start_wave_refused_when_rehearsal_skips_a_hw_rev(env):
    pid = env.fleet.plan_rollout("v1.2", [2, "rest"])["plan_id"]
    partial = run(env.fleet.rehearse(pid, hw_revs=["A"]))
    assert partial["verdict"] == "pass" and partial["phase"] == "REHEARSED"
    err = refused("REHEARSAL_COVERAGE", env.fleet.start_wave, pid, 1, partial["evidence_id"])
    assert "B" in err.message and "rehearse" in err.message and partial["evidence_id"] in err.message
    assert env.field.start_wave_calls == []
    assert error_codes(env, pid) == ["REHEARSAL_COVERAGE"]
    assert env.ledger.get_plan(pid).phase == "REHEARSED"
    full = run(env.fleet.rehearse(pid))
    assert full["verdict"] == "pass"
    env.fleet.start_wave(pid, 1, full["evidence_id"])
    assert len(env.field.start_wave_calls) == 1


def test_start_wave_coverage_needs_hw_revs_of_later_waves(tmp_path):
    env = build_fleet(tmp_path, make_devices(2, 2))
    pid = env.fleet.plan_rollout("v1.2", [1, "rest"], stratify_by="hw_rev")["plan_id"]
    plan = env.ledger.get_plan(pid)
    wave1_revs = {plan.devices[d]["hw_rev"] for d in plan.waves[0]}
    later_revs = {plan.devices[d]["hw_rev"] for w in plan.waves[1:] for d in w}
    assert later_revs - wave1_revs  # a hw_rev that only appears after wave 1
    covered = sorted(wave1_revs)
    ev = run(env.fleet.rehearse(pid, hw_revs=covered))
    err = refused("REHEARSAL_COVERAGE", env.fleet.start_wave, pid, 1, ev["evidence_id"])
    assert min(later_revs - wave1_revs) in err.message
    assert env.field.start_wave_calls == []


def test_start_wave_partial_plan_needs_only_kept_hw_revs(tmp_path):
    env, _blocked, ev = blocked_leak_plan(tmp_path)
    pid = env.fleet.plan_rollout("v1.2", [1, "rest"], exclude_hw_revs=["B"], exclusion_evidence_id=ev)[
        "plan_id"
    ]
    rev = run(env.fleet.rehearse(pid, hw_revs=["A"], minutes=5))
    assert rev["verdict"] == "pass" and "not_rehearsed" not in rev
    env.fleet.start_wave(pid, 1, rev["evidence_id"])
    assert {d for call in env.field.start_wave_calls for d in call[-1]} <= {"a1", "a2"}
