"""Tests for the server-side tool preconditions (docs/agent-design.md section 10.1)."""

from __future__ import annotations

import sqlite3

import pytest

from agent.fleet_mcp import ledger as ledger_mod
from agent.fleet_mcp import preconditions as pc
from agent.fleet_mcp.errors import PreconditionError

MAX_AGE = 900
WAVES = [["d1", "d2"], ["d3", "d4", "d5"]]
OTHER_DEVICES = {"x1": {"hw_rev": "A", "region": "eu"}}


@pytest.fixture(autouse=True)
def fast_sqlite(monkeypatch):
    """These tests issue thousands of ledger writes; skip fsync (durability is not under test)."""
    real_connect = sqlite3.connect

    def connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        conn.execute("PRAGMA synchronous=OFF")
        return conn

    monkeypatch.setattr(ledger_mod.sqlite3, "connect", connect)


# helpers -----------------------------------------------------------------------


def error_events(ledger, plan_id):
    return ledger.events(plan_id, types=["error"])


def assert_refused(ledger, plan_id, tool, code, fn, *args, **kwargs):
    """Call fn, expect PreconditionError(code) and exactly one new matching `error` event."""
    before = len(error_events(ledger, plan_id))
    with pytest.raises(PreconditionError) as exc:
        fn(*args, **kwargs)
    assert exc.value.code == code
    after = error_events(ledger, plan_id)
    assert len(after) == before + 1
    assert after[-1].type == "error"
    assert after[-1].payload["tool"] == tool
    assert after[-1].payload["code"] == code
    assert after[-1].payload["message"] == exc.value.message
    return exc.value


def assert_no_errors(ledger, plan_id):
    assert error_events(ledger, plan_id) == []


def foreign_evidence(ledger, kind, verdict, wave=None):
    """Evidence owned by a second plan. Call right after the plan_id fixture (still PLANNED).

    Creating the second plan supersedes (BLOCKS) the first; the test then sets whatever phase it needs.
    """
    other = ledger.create_plan("v1.3", "v1.2", [["x1"]], dict(OTHER_DEVICES))
    return ledger.new_evidence(other, kind, verdict, {}, wave=wave)


def start(ledger, clock, plan_id, wave):
    """Record a wave_started action for `wave` (1-based) with the plan's wave devices."""
    ledger.add_event(
        plan_id,
        "action",
        {
            "kind": "wave_started",
            "rollout_id": wave,
            "device_ids": list(WAVES[wave - 1]),
            "started_at": clock().isoformat(),
        },
        wave=wave,
    )


def observation(ledger, plan_id, wave, verdict, affected=()):
    return ledger.new_evidence(
        plan_id, "observation", verdict, {"affected_hw_revs": list(affected)}, wave=wave
    )


def rehearsed(ledger, plan_id, verdict="pass"):
    ledger.set_phase(plan_id, "REHEARSED")
    return ledger.new_evidence(plan_id, "rehearsal", verdict, {})


def wave1_observed(ledger, clock, plan_id, verdict="healthy", affected=()):
    """Phase WAVE_OBSERVED, wave 1 started; returns the wave-1 observation."""
    ledger.set_phase(plan_id, "WAVE_OBSERVED")
    start(ledger, clock, plan_id, 1)
    return observation(ledger, plan_id, 1, verdict, affected)


def halted(ledger, clock, plan_id, affected=("B",), both_waves=False):
    """Phase HALTED with regression evidence on the latest started wave."""
    ledger.set_phase(plan_id, "HALTED")
    start(ledger, clock, plan_id, 1)
    wave = 1
    if both_waves:
        start(ledger, clock, plan_id, 2)
        wave = 2
    return observation(ledger, plan_id, wave, "regression", affected)


def rolled_back(ledger, plan_id, device_ids):
    ledger.set_phase(plan_id, "ROLLED_BACK")
    ledger.add_event(plan_id, "action", {"kind": "rollback", "device_ids": list(device_ids)})


# PLAN_NOT_FOUND ----------------------------------------------------------------


@pytest.mark.parametrize(
    "call",
    [
        lambda le, now: pc.check_start_wave(le, "nope", 1, "ev-x", now, MAX_AGE),
        lambda le, now: pc.check_observe_wave(le, "nope", 1, 2, 4),
        lambda le, now: pc.check_halt(le, "nope", "ev-x", now, MAX_AGE),
        lambda le, now: pc.check_rollback(le, "nope", None, ["d1"], "v1.1", "ev-x", now, MAX_AGE),
        lambda le, now: pc.check_verify_recovery(le, "nope", None, 2, 4),
        lambda le, now: pc.check_rehearse(le, "nope", None, 2, 5),
        lambda le, now: pc.check_record_decision(le, "nope", []),
    ],
    ids=["start", "observe", "halt", "rollback", "verify", "rehearse", "decision"],
)
def test_plan_not_found_everywhere(ledger, clock, call):
    with pytest.raises(PreconditionError) as exc:
        call(ledger, clock())
    assert exc.value.code == "PLAN_NOT_FOUND"


# check_start_wave: wave 1 ------------------------------------------------------


def test_start_wave1_happy_path(ledger, clock, plan_id):
    ev = rehearsed(ledger, plan_id)
    plan, got = pc.check_start_wave(ledger, plan_id, 1, ev.evidence_id, clock(), MAX_AGE)
    assert plan.plan_id == plan_id
    assert got.evidence_id == ev.evidence_id
    assert_no_errors(ledger, plan_id)


def test_start_wave1_evidence_exactly_max_age_is_fresh(ledger, clock, plan_id):
    ev = rehearsed(ledger, plan_id)
    clock.advance(MAX_AGE)
    pc.check_start_wave(ledger, plan_id, 1, ev.evidence_id, clock(), MAX_AGE)


@pytest.mark.parametrize("phase", ["PLANNED", "WAVE_RUNNING", "WAVE_OBSERVED", "HALTED", "COMPLETE"])
def test_start_wave1_bad_phase(ledger, clock, plan_id, phase):
    ev = rehearsed(ledger, plan_id)
    ledger.set_phase(plan_id, phase)
    assert_refused(
        ledger, plan_id, "start_wave", "BAD_PHASE",
        pc.check_start_wave, ledger, plan_id, 1, ev.evidence_id, clock(), MAX_AGE,
    )  # fmt: skip


@pytest.mark.parametrize("evidence_id", [None, "", "ev-deadbeef"])
def test_start_wave1_evidence_missing(ledger, clock, plan_id, evidence_id):
    rehearsed(ledger, plan_id)
    assert_refused(
        ledger, plan_id, "start_wave", "EVIDENCE_MISSING",
        pc.check_start_wave, ledger, plan_id, 1, evidence_id, clock(), MAX_AGE,
    )  # fmt: skip


def test_start_wave1_evidence_wrong_plan(ledger, clock, plan_id):
    foreign = foreign_evidence(ledger, "rehearsal", "pass")
    ledger.set_phase(plan_id, "REHEARSED")
    assert_refused(
        ledger, plan_id, "start_wave", "EVIDENCE_WRONG_PLAN",
        pc.check_start_wave, ledger, plan_id, 1, foreign.evidence_id, clock(), MAX_AGE,
    )  # fmt: skip


def test_start_wave1_evidence_wrong_kind(ledger, clock, plan_id):
    rehearsed(ledger, plan_id)
    obs = observation(ledger, plan_id, 1, "healthy")
    assert_refused(
        ledger, plan_id, "start_wave", "EVIDENCE_WRONG_KIND",
        pc.check_start_wave, ledger, plan_id, 1, obs.evidence_id, clock(), MAX_AGE,
    )  # fmt: skip


def test_start_wave1_evidence_verdict_fail(ledger, clock, plan_id):
    ev = rehearsed(ledger, plan_id, verdict="fail")
    assert_refused(
        ledger, plan_id, "start_wave", "EVIDENCE_VERDICT",
        pc.check_start_wave, ledger, plan_id, 1, ev.evidence_id, clock(), MAX_AGE,
    )  # fmt: skip


def test_start_wave1_evidence_stale(ledger, clock, plan_id):
    ev = rehearsed(ledger, plan_id)
    clock.advance(MAX_AGE + 1)
    assert_refused(
        ledger, plan_id, "start_wave", "EVIDENCE_STALE",
        pc.check_start_wave, ledger, plan_id, 1, ev.evidence_id, clock(), MAX_AGE,
    )  # fmt: skip


def test_start_wave1_evidence_superseded(ledger, clock, plan_id):
    old = rehearsed(ledger, plan_id)
    clock.advance(10)
    ledger.new_evidence(plan_id, "rehearsal", "pass", {})
    assert_refused(
        ledger, plan_id, "start_wave", "EVIDENCE_SUPERSEDED",
        pc.check_start_wave, ledger, plan_id, 1, old.evidence_id, clock(), MAX_AGE,
    )  # fmt: skip


# check_start_wave: wave n > 1 --------------------------------------------------


def test_start_wave2_happy_path(ledger, clock, plan_id):
    ev = wave1_observed(ledger, clock, plan_id)
    plan, got = pc.check_start_wave(ledger, plan_id, 2, ev.evidence_id, clock(), MAX_AGE)
    assert plan.plan_id == plan_id and got.evidence_id == ev.evidence_id
    assert_no_errors(ledger, plan_id)


@pytest.mark.parametrize("phase", ["REHEARSED", "WAVE_RUNNING", "HALTED", "ROLLED_BACK"])
def test_start_wave2_bad_phase(ledger, clock, plan_id, phase):
    ev = wave1_observed(ledger, clock, plan_id)
    ledger.set_phase(plan_id, phase)
    assert_refused(
        ledger, plan_id, "start_wave", "BAD_PHASE",
        pc.check_start_wave, ledger, plan_id, 2, ev.evidence_id, clock(), MAX_AGE,
    )  # fmt: skip


def test_start_wave2_already_started(ledger, clock, plan_id):
    ev = wave1_observed(ledger, clock, plan_id)
    start(ledger, clock, plan_id, 2)
    assert_refused(
        ledger, plan_id, "start_wave", "WAVE_ORDER",
        pc.check_start_wave, ledger, plan_id, 2, ev.evidence_id, clock(), MAX_AGE,
    )  # fmt: skip


def test_start_wave2_previous_wave_not_started(ledger, clock, plan_id):
    ledger.set_phase(plan_id, "WAVE_OBSERVED")
    ev = observation(ledger, plan_id, 1, "healthy")
    assert_refused(
        ledger, plan_id, "start_wave", "WAVE_ORDER",
        pc.check_start_wave, ledger, plan_id, 2, ev.evidence_id, clock(), MAX_AGE,
    )  # fmt: skip


def test_start_wave2_evidence_missing(ledger, clock, plan_id):
    wave1_observed(ledger, clock, plan_id)
    assert_refused(
        ledger, plan_id, "start_wave", "EVIDENCE_MISSING",
        pc.check_start_wave, ledger, plan_id, 2, None, clock(), MAX_AGE,
    )  # fmt: skip


def test_start_wave2_evidence_wrong_plan(ledger, clock, plan_id):
    foreign = foreign_evidence(ledger, "observation", "healthy", wave=1)
    wave1_observed(ledger, clock, plan_id)
    assert_refused(
        ledger, plan_id, "start_wave", "EVIDENCE_WRONG_PLAN",
        pc.check_start_wave, ledger, plan_id, 2, foreign.evidence_id, clock(), MAX_AGE,
    )  # fmt: skip


def test_start_wave2_evidence_wrong_kind(ledger, clock, plan_id):
    wave1_observed(ledger, clock, plan_id)
    rh = ledger.new_evidence(plan_id, "rehearsal", "pass", {})
    assert_refused(
        ledger, plan_id, "start_wave", "EVIDENCE_WRONG_KIND",
        pc.check_start_wave, ledger, plan_id, 2, rh.evidence_id, clock(), MAX_AGE,
    )  # fmt: skip


def test_start_wave2_evidence_wrong_wave(ledger, clock, plan_id):
    wave1_observed(ledger, clock, plan_id)
    ev = observation(ledger, plan_id, 2, "healthy")
    assert_refused(
        ledger, plan_id, "start_wave", "EVIDENCE_WRONG_WAVE",
        pc.check_start_wave, ledger, plan_id, 2, ev.evidence_id, clock(), MAX_AGE,
    )  # fmt: skip


@pytest.mark.parametrize("verdict", ["regression", "inconclusive"])
def test_start_wave2_evidence_verdict(ledger, clock, plan_id, verdict):
    ev = wave1_observed(ledger, clock, plan_id, verdict=verdict)
    assert_refused(
        ledger, plan_id, "start_wave", "EVIDENCE_VERDICT",
        pc.check_start_wave, ledger, plan_id, 2, ev.evidence_id, clock(), MAX_AGE,
    )  # fmt: skip


def test_start_wave2_evidence_stale(ledger, clock, plan_id):
    ev = wave1_observed(ledger, clock, plan_id)
    clock.advance(MAX_AGE + 1)
    assert_refused(
        ledger, plan_id, "start_wave", "EVIDENCE_STALE",
        pc.check_start_wave, ledger, plan_id, 2, ev.evidence_id, clock(), MAX_AGE,
    )  # fmt: skip


def test_start_wave2_evidence_superseded_by_newer_observation(ledger, clock, plan_id):
    old = wave1_observed(ledger, clock, plan_id, verdict="healthy")
    clock.advance(10)
    observation(ledger, plan_id, 1, "regression", ["B"])
    assert_refused(
        ledger, plan_id, "start_wave", "EVIDENCE_SUPERSEDED",
        pc.check_start_wave, ledger, plan_id, 2, old.evidence_id, clock(), MAX_AGE,
    )  # fmt: skip


def test_start_wave2_newer_observation_of_other_wave_does_not_supersede(ledger, clock, plan_id):
    ev = wave1_observed(ledger, clock, plan_id)
    observation(ledger, plan_id, 2, "regression", ["B"])
    pc.check_start_wave(ledger, plan_id, 2, ev.evidence_id, clock(), MAX_AGE)


@pytest.mark.parametrize("wave", [0, 3, -1, "1", 1.0, True, None])
def test_start_wave_bad_argument(ledger, clock, plan_id, wave):
    ev = rehearsed(ledger, plan_id)
    assert_refused(
        ledger, plan_id, "start_wave", "BAD_ARGUMENT",
        pc.check_start_wave, ledger, plan_id, wave, ev.evidence_id, clock(), MAX_AGE,
    )  # fmt: skip


# check_observe_wave ------------------------------------------------------------


@pytest.mark.parametrize("phase", ["WAVE_RUNNING", "WAVE_OBSERVED", "HALTED"])
def test_observe_allowed_phases(ledger, clock, plan_id, phase):
    ledger.set_phase(plan_id, phase)
    start(ledger, clock, plan_id, 1)
    plan, minutes = pc.check_observe_wave(ledger, plan_id, 1, 2, 4)
    assert plan.phase == phase  # the check does not change the phase
    assert minutes == 2.0
    assert_no_errors(ledger, plan_id)


@pytest.mark.parametrize("phase", ["PLANNED", "REHEARSED", "BLOCKED", "COMPLETE", "ROLLED_BACK", "VERIFIED"])
def test_observe_bad_phase(ledger, clock, plan_id, phase):
    start(ledger, clock, plan_id, 1)
    ledger.set_phase(plan_id, phase)
    assert_refused(
        ledger, plan_id, "observe_wave", "BAD_PHASE", pc.check_observe_wave, ledger, plan_id, 1, 2, 4
    )  # fmt: skip


def test_observe_wave_order_not_latest(ledger, clock, plan_id):
    ledger.set_phase(plan_id, "WAVE_RUNNING")
    start(ledger, clock, plan_id, 1)
    start(ledger, clock, plan_id, 2)
    assert_refused(
        ledger, plan_id, "observe_wave", "WAVE_ORDER", pc.check_observe_wave, ledger, plan_id, 1, 2, 4
    )  # fmt: skip
    plan, _ = pc.check_observe_wave(ledger, plan_id, 2, 2, 4)
    assert plan.plan_id == plan_id


def test_observe_wave_order_not_yet_started(ledger, clock, plan_id):
    ledger.set_phase(plan_id, "WAVE_RUNNING")
    start(ledger, clock, plan_id, 1)
    assert_refused(
        ledger, plan_id, "observe_wave", "WAVE_ORDER", pc.check_observe_wave, ledger, plan_id, 2, 2, 4
    )  # fmt: skip


def test_observe_wave_order_no_started_wave(ledger, plan_id):
    ledger.set_phase(plan_id, "WAVE_RUNNING")
    assert_refused(
        ledger, plan_id, "observe_wave", "WAVE_ORDER", pc.check_observe_wave, ledger, plan_id, 1, 2, 4
    )  # fmt: skip


@pytest.mark.parametrize("minutes, expected", [(10, 4.0), (4, 4.0), (2.5, 2.5), (3, 3.0), (0.1, 0.1)])
def test_observe_clamps_minutes(ledger, clock, plan_id, minutes, expected):
    ledger.set_phase(plan_id, "WAVE_RUNNING")
    start(ledger, clock, plan_id, 1)
    _, got = pc.check_observe_wave(ledger, plan_id, 1, minutes, 4)
    assert got == expected and isinstance(got, float)


@pytest.mark.parametrize("minutes", [0, -1, -0.5, "3", None, True])
def test_observe_bad_minutes(ledger, clock, plan_id, minutes):
    ledger.set_phase(plan_id, "WAVE_RUNNING")
    start(ledger, clock, plan_id, 1)
    assert_refused(
        ledger, plan_id, "observe_wave", "BAD_ARGUMENT",
        pc.check_observe_wave, ledger, plan_id, 1, minutes, 4,
    )  # fmt: skip


# check_halt --------------------------------------------------------------------


def test_halt_happy_path(ledger, clock, plan_id):
    ev = wave1_observed(ledger, clock, plan_id, verdict="regression", affected=["B"])
    plan, got = pc.check_halt(ledger, plan_id, ev.evidence_id, clock(), MAX_AGE)
    assert plan.plan_id == plan_id and got.evidence_id == ev.evidence_id
    assert_no_errors(ledger, plan_id)


@pytest.mark.parametrize("phase", ["REHEARSED", "WAVE_RUNNING", "HALTED", "ROLLED_BACK"])
def test_halt_bad_phase(ledger, clock, plan_id, phase):
    ev = wave1_observed(ledger, clock, plan_id, verdict="regression")
    ledger.set_phase(plan_id, phase)
    assert_refused(
        ledger, plan_id, "halt_rollout", "BAD_PHASE",
        pc.check_halt, ledger, plan_id, ev.evidence_id, clock(), MAX_AGE,
    )  # fmt: skip


def test_halt_no_started_wave(ledger, clock, plan_id):
    ledger.set_phase(plan_id, "WAVE_OBSERVED")
    ev = observation(ledger, plan_id, 1, "regression")
    assert_refused(
        ledger, plan_id, "halt_rollout", "WAVE_ORDER",
        pc.check_halt, ledger, plan_id, ev.evidence_id, clock(), MAX_AGE,
    )  # fmt: skip


def test_halt_evidence_missing(ledger, clock, plan_id):
    wave1_observed(ledger, clock, plan_id, verdict="regression")
    assert_refused(
        ledger, plan_id, "halt_rollout", "EVIDENCE_MISSING",
        pc.check_halt, ledger, plan_id, "ev-00000000", clock(), MAX_AGE,
    )  # fmt: skip


def test_halt_evidence_wrong_plan(ledger, clock, plan_id):
    foreign = foreign_evidence(ledger, "observation", "regression", wave=1)
    wave1_observed(ledger, clock, plan_id, verdict="regression")
    assert_refused(
        ledger, plan_id, "halt_rollout", "EVIDENCE_WRONG_PLAN",
        pc.check_halt, ledger, plan_id, foreign.evidence_id, clock(), MAX_AGE,
    )  # fmt: skip


def test_halt_evidence_wrong_kind(ledger, clock, plan_id):
    wave1_observed(ledger, clock, plan_id, verdict="regression")
    rh = ledger.new_evidence(plan_id, "rehearsal", "fail", {})
    assert_refused(
        ledger, plan_id, "halt_rollout", "EVIDENCE_WRONG_KIND",
        pc.check_halt, ledger, plan_id, rh.evidence_id, clock(), MAX_AGE,
    )  # fmt: skip


@pytest.mark.parametrize("verdict", ["healthy", "inconclusive"])
def test_halt_verdict_must_be_regression(ledger, clock, plan_id, verdict):
    ev = wave1_observed(ledger, clock, plan_id, verdict=verdict)
    assert_refused(
        ledger, plan_id, "halt_rollout", "EVIDENCE_VERDICT",
        pc.check_halt, ledger, plan_id, ev.evidence_id, clock(), MAX_AGE,
    )  # fmt: skip


def test_halt_evidence_stale(ledger, clock, plan_id):
    ev = wave1_observed(ledger, clock, plan_id, verdict="regression")
    clock.advance(MAX_AGE + 1)
    assert_refused(
        ledger, plan_id, "halt_rollout", "EVIDENCE_STALE",
        pc.check_halt, ledger, plan_id, ev.evidence_id, clock(), MAX_AGE,
    )  # fmt: skip


def test_halt_evidence_wrong_wave(ledger, clock, plan_id):
    old = wave1_observed(ledger, clock, plan_id, verdict="regression")
    start(ledger, clock, plan_id, 2)
    observation(ledger, plan_id, 2, "regression", ["B"])
    assert_refused(
        ledger, plan_id, "halt_rollout", "EVIDENCE_WRONG_WAVE",
        pc.check_halt, ledger, plan_id, old.evidence_id, clock(), MAX_AGE,
    )  # fmt: skip


def test_halt_evidence_superseded(ledger, clock, plan_id):
    old = wave1_observed(ledger, clock, plan_id, verdict="regression")
    clock.advance(5)
    observation(ledger, plan_id, 1, "healthy")
    assert_refused(
        ledger, plan_id, "halt_rollout", "EVIDENCE_SUPERSEDED",
        pc.check_halt, ledger, plan_id, old.evidence_id, clock(), MAX_AGE,
    )  # fmt: skip


# check_rollback ----------------------------------------------------------------


def rollback_call(ledger, clock, plan_id, evidence_id, cohort=None, targets=("d2",), to_version="v1.1"):
    targets = None if targets is None else list(targets)
    return pc.check_rollback(ledger, plan_id, cohort, targets, to_version, evidence_id, clock(), MAX_AGE)


def test_rollback_happy_path_targets_sorted_and_deduped(ledger, clock, plan_id):
    ev = halted(ledger, clock, plan_id, affected=["A", "B"], both_waves=True)
    plan, got, resolved = rollback_call(ledger, clock, plan_id, ev.evidence_id, targets=["d5", "d1", "d5"])
    assert plan.plan_id == plan_id and got.evidence_id == ev.evidence_id
    assert resolved == ["d1", "d5"]
    assert_no_errors(ledger, plan_id)


def test_rollback_cohort_resolves_updated_devices_of_hw_rev(ledger, clock, plan_id):
    ev = halted(ledger, clock, plan_id, affected=["B"], both_waves=True)
    _, _, resolved = rollback_call(
        ledger, clock, plan_id, ev.evidence_id, cohort={"hw_rev": "B"}, targets=None
    )
    assert resolved == ["d2", "d4"]


def test_rollback_cohort_only_includes_updated_devices(ledger, clock, plan_id):
    ev = halted(ledger, clock, plan_id, affected=["B"], both_waves=False)  # d4 not updated
    _, _, resolved = rollback_call(
        ledger, clock, plan_id, ev.evidence_id, cohort={"hw_rev": "B"}, targets=None
    )
    assert resolved == ["d2"]


def test_rollback_cohort_multiple_keys(ledger, clock, plan_id):
    ev = halted(ledger, clock, plan_id, affected=["A", "B"], both_waves=True)
    _, _, resolved = rollback_call(
        ledger, clock, plan_id, ev.evidence_id, cohort={"hw_rev": "A", "region": "eu"}, targets=None
    )
    assert resolved == ["d1", "d5"]


@pytest.mark.parametrize(
    "cohort, targets",
    [({"hw_rev": "B"}, ["d2"]), (None, None), ({}, None), (None, [])],
    ids=["both", "neither", "empty-cohort", "empty-targets"],
)
def test_rollback_bad_argument(ledger, clock, plan_id, cohort, targets):
    ev = halted(ledger, clock, plan_id)
    assert_refused(
        ledger, plan_id, "rollback", "BAD_ARGUMENT",
        rollback_call, ledger, clock, plan_id, ev.evidence_id, cohort=cohort, targets=targets,
    )  # fmt: skip


@pytest.mark.parametrize("phase", ["WAVE_OBSERVED", "WAVE_RUNNING", "ROLLED_BACK", "VERIFIED"])
def test_rollback_bad_phase(ledger, clock, plan_id, phase):
    ev = halted(ledger, clock, plan_id)
    ledger.set_phase(plan_id, phase)
    assert_refused(
        ledger, plan_id, "rollback", "BAD_PHASE", rollback_call, ledger, clock, plan_id, ev.evidence_id
    )  # fmt: skip


@pytest.mark.parametrize("to_version", ["v1.0", "v1.2", ""])
def test_rollback_wrong_target_version(ledger, clock, plan_id, to_version):
    ev = halted(ledger, clock, plan_id)
    assert_refused(
        ledger, plan_id, "rollback", "WRONG_TARGET_VERSION",
        rollback_call, ledger, clock, plan_id, ev.evidence_id, to_version=to_version,
    )  # fmt: skip


def test_rollback_evidence_missing(ledger, clock, plan_id):
    halted(ledger, clock, plan_id)
    assert_refused(
        ledger, plan_id, "rollback", "EVIDENCE_MISSING", rollback_call, ledger, clock, plan_id, None
    )  # fmt: skip


def test_rollback_evidence_wrong_plan(ledger, clock, plan_id):
    foreign = foreign_evidence(ledger, "observation", "regression", wave=1)
    halted(ledger, clock, plan_id)
    assert_refused(
        ledger, plan_id, "rollback", "EVIDENCE_WRONG_PLAN",
        rollback_call, ledger, clock, plan_id, foreign.evidence_id,
    )  # fmt: skip


def test_rollback_evidence_wrong_kind(ledger, clock, plan_id):
    halted(ledger, clock, plan_id)
    rh = ledger.new_evidence(plan_id, "rehearsal", "fail", {})
    assert_refused(
        ledger, plan_id, "rollback", "EVIDENCE_WRONG_KIND",
        rollback_call, ledger, clock, plan_id, rh.evidence_id,
    )  # fmt: skip


def test_rollback_evidence_wrong_wave(ledger, clock, plan_id):
    old = halted(ledger, clock, plan_id)  # wave 1 evidence
    start(ledger, clock, plan_id, 2)  # latest wave is now 2
    assert_refused(
        ledger, plan_id, "rollback", "EVIDENCE_WRONG_WAVE",
        rollback_call, ledger, clock, plan_id, old.evidence_id,
    )  # fmt: skip


def test_rollback_evidence_verdict(ledger, clock, plan_id):
    halted(ledger, clock, plan_id)
    clock.advance(1)
    healthy = observation(ledger, plan_id, 1, "healthy")
    assert_refused(
        ledger, plan_id, "rollback", "EVIDENCE_VERDICT",
        rollback_call, ledger, clock, plan_id, healthy.evidence_id,
    )  # fmt: skip


def test_rollback_evidence_stale(ledger, clock, plan_id):
    ev = halted(ledger, clock, plan_id)
    clock.advance(MAX_AGE + 1)
    assert_refused(
        ledger, plan_id, "rollback", "EVIDENCE_STALE", rollback_call, ledger, clock, plan_id, ev.evidence_id
    )  # fmt: skip


def test_rollback_evidence_superseded_then_newer_regression_accepted(ledger, clock, plan_id):
    old = halted(ledger, clock, plan_id)
    clock.advance(5)
    newer = observation(ledger, plan_id, 1, "regression", ["B"])
    assert_refused(
        ledger, plan_id, "rollback", "EVIDENCE_SUPERSEDED",
        rollback_call, ledger, clock, plan_id, old.evidence_id,
    )  # fmt: skip
    _, got, resolved = rollback_call(ledger, clock, plan_id, newer.evidence_id)
    assert got.evidence_id == newer.evidence_id and resolved == ["d2"]


def test_rollback_target_not_updated(ledger, clock, plan_id):
    ev = halted(ledger, clock, plan_id, affected=["A", "B"])  # only wave 1 started
    assert_refused(
        ledger, plan_id, "rollback", "TARGET_NOT_ELIGIBLE",
        rollback_call, ledger, clock, plan_id, ev.evidence_id, targets=["d3"],
    )  # fmt: skip


def test_rollback_target_unknown_device(ledger, clock, plan_id):
    ev = halted(ledger, clock, plan_id)
    assert_refused(
        ledger, plan_id, "rollback", "TARGET_NOT_ELIGIBLE",
        rollback_call, ledger, clock, plan_id, ev.evidence_id, targets=["zzz"],
    )  # fmt: skip


def test_rollback_target_hw_rev_not_affected(ledger, clock, plan_id):
    ev = halted(ledger, clock, plan_id, affected=["B"])
    err = assert_refused(
        ledger, plan_id, "rollback", "TARGET_NOT_ELIGIBLE",
        rollback_call, ledger, clock, plan_id, ev.evidence_id, targets=["d2", "d1"],
    )  # fmt: skip
    assert "d1" in err.message and "d2" not in err.message.split(":", 1)[1]


def test_rollback_target_not_eligible_when_evidence_has_no_affected_hw_revs(ledger, clock, plan_id):
    ev = halted(ledger, clock, plan_id, affected=[])
    assert_refused(
        ledger, plan_id, "rollback", "TARGET_NOT_ELIGIBLE",
        rollback_call, ledger, clock, plan_id, ev.evidence_id, targets=["d2"],
    )  # fmt: skip


def test_rollback_cohort_of_unaffected_hw_rev(ledger, clock, plan_id):
    ev = halted(ledger, clock, plan_id, affected=["B"])
    assert_refused(
        ledger, plan_id, "rollback", "TARGET_NOT_ELIGIBLE",
        rollback_call, ledger, clock, plan_id, ev.evidence_id, cohort={"hw_rev": "A"}, targets=None,
    )  # fmt: skip


def test_rollback_cohort_spanning_unaffected_hw_rev(ledger, clock, plan_id):
    ev = halted(ledger, clock, plan_id, affected=["B"])
    assert_refused(
        ledger, plan_id, "rollback", "TARGET_NOT_ELIGIBLE",
        rollback_call, ledger, clock, plan_id, ev.evidence_id, cohort={"region": "eu"}, targets=None,
    )  # fmt: skip


def test_rollback_cohort_matching_nothing(ledger, clock, plan_id):
    ev = halted(ledger, clock, plan_id, affected=["B"])
    assert_refused(
        ledger, plan_id, "rollback", "TARGET_NOT_ELIGIBLE",
        rollback_call, ledger, clock, plan_id, ev.evidence_id, cohort={"hw_rev": "C"}, targets=None,
    )  # fmt: skip


# check_verify_recovery ---------------------------------------------------------


def test_verify_targets_none_means_all_rolled_back(ledger, plan_id):
    rolled_back(ledger, plan_id, ["d4", "d2"])
    plan, chosen, minutes = pc.check_verify_recovery(ledger, plan_id, None, 3, 4)
    assert plan.plan_id == plan_id
    assert chosen == ["d2", "d4"] and minutes == 3.0
    assert_no_errors(ledger, plan_id)


def test_verify_empty_targets_means_all_rolled_back(ledger, plan_id):
    rolled_back(ledger, plan_id, ["d2", "d4"])
    _, chosen, _ = pc.check_verify_recovery(ledger, plan_id, [], 3, 4)
    assert chosen == ["d2", "d4"]


def test_verify_rollback_events_accumulate(ledger, plan_id):
    rolled_back(ledger, plan_id, ["d2"])
    ledger.add_event(plan_id, "action", {"kind": "rollback", "device_ids": ["d4"]})
    _, chosen, _ = pc.check_verify_recovery(ledger, plan_id, None, 3, 4)
    assert chosen == ["d2", "d4"]


def test_verify_subset_sorted_and_deduped(ledger, plan_id):
    rolled_back(ledger, plan_id, ["d2", "d4"])
    _, chosen, _ = pc.check_verify_recovery(ledger, plan_id, ["d4", "d4"], 3, 4)
    assert chosen == ["d4"]


def test_verify_clamps_minutes(ledger, plan_id):
    rolled_back(ledger, plan_id, ["d2"])
    _, _, minutes = pc.check_verify_recovery(ledger, plan_id, None, 60, 4)
    assert minutes == 4.0


@pytest.mark.parametrize("phase", ["HALTED", "WAVE_OBSERVED", "VERIFIED", "PLANNED"])
def test_verify_bad_phase(ledger, plan_id, phase):
    rolled_back(ledger, plan_id, ["d2"])
    ledger.set_phase(plan_id, phase)
    assert_refused(
        ledger, plan_id, "verify_recovery", "BAD_PHASE",
        pc.check_verify_recovery, ledger, plan_id, None, 3, 4,
    )  # fmt: skip


def test_verify_target_not_rolled_back(ledger, plan_id):
    rolled_back(ledger, plan_id, ["d2"])
    assert_refused(
        ledger, plan_id, "verify_recovery", "TARGET_NOT_ELIGIBLE",
        pc.check_verify_recovery, ledger, plan_id, ["d2", "d4"], 3, 4,
    )  # fmt: skip


def test_verify_nothing_rolled_back(ledger, plan_id):
    ledger.set_phase(plan_id, "ROLLED_BACK")
    assert_refused(
        ledger, plan_id, "verify_recovery", "TARGET_NOT_ELIGIBLE",
        pc.check_verify_recovery, ledger, plan_id, None, 3, 4,
    )  # fmt: skip


@pytest.mark.parametrize("minutes", [0, -2, "3", None])
def test_verify_bad_minutes(ledger, plan_id, minutes):
    rolled_back(ledger, plan_id, ["d2"])
    assert_refused(
        ledger, plan_id, "verify_recovery", "BAD_ARGUMENT",
        pc.check_verify_recovery, ledger, plan_id, None, minutes, 4,
    )  # fmt: skip


# check_rehearse ----------------------------------------------------------------


@pytest.mark.parametrize("phase", ["PLANNED", "REHEARSED"])
def test_rehearse_allowed_phases(ledger, plan_id, phase):
    ledger.set_phase(plan_id, phase)
    plan, revs, minutes = pc.check_rehearse(ledger, plan_id, None, 3, 5)
    assert plan.phase == phase and revs == ["A", "B"] and minutes == 3.0
    assert_no_errors(ledger, plan_id)


@pytest.mark.parametrize("phase", ["WAVE_RUNNING", "WAVE_OBSERVED", "HALTED", "ROLLED_BACK", "BLOCKED"])
def test_rehearse_bad_phase(ledger, plan_id, phase):
    ledger.set_phase(plan_id, phase)
    assert_refused(
        ledger, plan_id, "rehearse", "BAD_PHASE", pc.check_rehearse, ledger, plan_id, None, 3, 5
    )  # fmt: skip


def test_rehearse_default_and_empty_mean_all_hw_revs(ledger, plan_id):
    assert pc.check_rehearse(ledger, plan_id, None, 3, 5)[1] == ["A", "B"]
    assert pc.check_rehearse(ledger, plan_id, [], 3, 5)[1] == ["A", "B"]


def test_rehearse_subset_sorted_and_deduped(ledger, plan_id):
    assert pc.check_rehearse(ledger, plan_id, ["B", "B"], 3, 5)[1] == ["B"]
    assert pc.check_rehearse(ledger, plan_id, ["B", "A"], 3, 5)[1] == ["A", "B"]


@pytest.mark.parametrize("hw_revs", [["C"], ["A", "C", "D"]])
def test_rehearse_hw_revs_not_in_plan(ledger, plan_id, hw_revs):
    err = assert_refused(
        ledger, plan_id, "rehearse", "BAD_ARGUMENT", pc.check_rehearse, ledger, plan_id, hw_revs, 3, 5
    )  # fmt: skip
    assert "C" in err.message


def test_rehearse_clamps_minutes(ledger, plan_id):
    assert pc.check_rehearse(ledger, plan_id, None, 30, 5)[2] == 5.0
    assert pc.check_rehearse(ledger, plan_id, None, 5, 5)[2] == 5.0
    assert pc.check_rehearse(ledger, plan_id, None, 0.5, 5)[2] == 0.5


@pytest.mark.parametrize("minutes", [0, -1, "2", None, False])
def test_rehearse_bad_minutes(ledger, plan_id, minutes):
    assert_refused(
        ledger, plan_id, "rehearse", "BAD_ARGUMENT", pc.check_rehearse, ledger, plan_id, None, minutes, 5
    )  # fmt: skip


# check_record_decision ---------------------------------------------------------


def test_record_decision_ok(ledger, plan_id):
    ev = ledger.new_evidence(plan_id, "rehearsal", "pass", {})
    plan = pc.check_record_decision(ledger, plan_id, [ev.evidence_id])
    assert plan.plan_id == plan_id
    assert_no_errors(ledger, plan_id)


def test_record_decision_without_evidence_ok(ledger, plan_id):
    assert pc.check_record_decision(ledger, plan_id, []).plan_id == plan_id
    assert pc.check_record_decision(ledger, plan_id, None).plan_id == plan_id


def test_record_decision_evidence_missing(ledger, plan_id):
    ev = ledger.new_evidence(plan_id, "rehearsal", "pass", {})
    assert_refused(
        ledger, plan_id, "record_decision", "EVIDENCE_MISSING",
        pc.check_record_decision, ledger, plan_id, [ev.evidence_id, "ev-00000000"],
    )  # fmt: skip


def test_record_decision_evidence_wrong_plan(ledger, plan_id):
    foreign = foreign_evidence(ledger, "rehearsal", "pass")
    assert_refused(
        ledger, plan_id, "record_decision", "EVIDENCE_WRONG_PLAN",
        pc.check_record_decision, ledger, plan_id, [foreign.evidence_id],
    )  # fmt: skip
