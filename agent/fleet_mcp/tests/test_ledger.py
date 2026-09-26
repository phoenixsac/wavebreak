import sqlite3

import pytest

from agent.fleet_mcp.errors import PreconditionError
from agent.fleet_mcp.ledger import Ledger


def test_schema_and_parent_dir(ledger):
    assert ledger.path.parent.is_dir()
    con = sqlite3.connect(ledger.path)
    cols = {t: [r[1] for r in con.execute(f"PRAGMA table_info({t})")] for t in ("rollouts", "events")}
    assert cols["rollouts"] == [
        "plan_id",
        "version",
        "from_version",
        "waves_json",
        "phase",
        "created_at",
        "updated_at",
    ]
    assert cols["events"] == ["id", "plan_id", "ts", "type", "wave", "evidence_id", "payload_json"]
    assert con.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_error_str():
    e = PreconditionError("BAD_PHASE", "nope")
    assert str(e) == "BAD_PHASE: nope" and e.code == "BAD_PHASE" and e.message == "nope"


def test_create_and_get_plan(ledger, plan_id):
    assert plan_id.startswith("plan-20260926-") and len(plan_id) == len("plan-20260926-abcd")
    p = ledger.get_plan(plan_id)
    assert p.phase == "PLANNED" and p.version == "v1.2" and p.from_version == "v1.1"
    assert p.waves[0] == ["d1", "d2"] and p.devices["d3"] == {"hw_rev": "A", "region": "us"}
    assert p.created_at == "2026-09-26T12:00:00+00:00"
    evs = ledger.events(plan_id)
    assert [(e.type, e.payload) for e in evs] == [("action", {"kind": "plan_created"})]
    assert ledger.latest_plan().plan_id == plan_id and ledger.active_plan().plan_id == plan_id


def test_plan_not_found(ledger):
    with pytest.raises(PreconditionError) as ei:
        ledger.get_plan("nope")
    assert ei.value.code == "PLAN_NOT_FOUND"
    assert ledger.latest_plan() is None and ledger.active_plan() is None


def test_planned_plan_is_superseded_by_new_plan(ledger, plan_id):
    new_id = ledger.create_plan("v1.3", "v1.1", [["d1"]], {"d1": {"hw_rev": "A", "region": "eu"}})
    assert ledger.get_plan(plan_id).phase == "BLOCKED"
    assert ledger.active_plan().plan_id == new_id
    assert any(e.payload.get("kind") == "superseded" for e in ledger.events(plan_id))


def test_active_plan_refused_until_terminal(ledger, plan_id):
    ledger.set_phase(plan_id, "WAVE_RUNNING")
    with pytest.raises(PreconditionError) as ei:
        ledger.create_plan("v1.3", "v1.1", [["d1"]], {"d1": {"hw_rev": "A", "region": "eu"}})
    assert ei.value.code == "ACTIVE_PLAN_EXISTS"
    for phase in ("BLOCKED", "COMPLETE", "VERIFIED"):
        ledger.set_phase(plan_id, phase)
        assert ledger.active_plan() is None
        new = ledger.create_plan("v1.3", "v1.1", [["d1"]], {"d1": {"hw_rev": "A", "region": "eu"}})
        assert ledger.latest_plan().plan_id == new
        ledger.set_phase(new, "BLOCKED")
        plan_id = new


def test_non_terminal_phases_block_new_plan(ledger, plan_id):
    for phase in ("WAVE_RUNNING", "WAVE_OBSERVED", "HALTED", "ROLLED_BACK"):
        ledger.set_phase(plan_id, phase)
        with pytest.raises(PreconditionError):
            ledger.create_plan("v", "u", [], {})


def test_set_phase(ledger, clock, plan_id):
    clock.advance(30)
    p = ledger.set_phase(plan_id, "REHEARSED", allowed_from=["PLANNED"])
    assert p.phase == "REHEARSED" and p.updated_at > p.created_at
    with pytest.raises(PreconditionError) as ei:
        ledger.set_phase(plan_id, "WAVE_RUNNING", allowed_from=["PLANNED"])
    assert ei.value.code == "BAD_PHASE"
    assert ledger.get_plan(plan_id).phase == "REHEARSED"
    with pytest.raises(ValueError):
        ledger.set_phase(plan_id, "WHATEVER")
    with pytest.raises(PreconditionError) as ei:
        ledger.set_phase("nope", "BLOCKED")
    assert ei.value.code == "PLAN_NOT_FOUND"


def test_evidence_ids_and_latest(ledger, clock, plan_id):
    e1 = ledger.new_evidence(plan_id, "observation", "inconclusive", {"x": 1}, wave=1)
    clock.advance(1)
    e2 = ledger.new_evidence(plan_id, "observation", "healthy", {}, wave=1)
    e3 = ledger.new_evidence(plan_id, "observation", "regression", {}, wave=2)
    r = ledger.new_evidence(plan_id, "rehearsal", "pass", {})
    assert e1.evidence_id != e2.evidence_id
    assert e1.evidence_id.startswith("ev-") and len(e1.evidence_id) == 11
    assert e1.type == "observation" and e1.payload == {"x": 1, "verdict": "inconclusive"}
    assert ledger.get_evidence(e2.evidence_id) == e2
    assert ledger.get_evidence("ev-00000000") is None
    assert ledger.latest_evidence(plan_id, "observation", wave=1).evidence_id == e2.evidence_id
    assert ledger.latest_evidence(plan_id, "observation").evidence_id == e3.evidence_id
    assert ledger.latest_evidence(plan_id, "rehearsal").evidence_id == r.evidence_id
    assert ledger.latest_evidence(plan_id, "verification") is None
    with pytest.raises(ValueError):
        ledger.new_evidence(plan_id, "decision", "x", {})


def test_events_types_limit_order(ledger, plan_id):
    for i in range(5):
        ledger.add_event(plan_id, "decision", {"i": i})
    ledger.add_event(plan_id, "approval", {"a": 1})
    last3 = ledger.events(plan_id, limit=3)
    assert [e.type for e in last3] == ["decision", "decision", "approval"]
    assert [e.payload.get("i") for e in ledger.events(plan_id, types=["decision"], limit=2)] == [3, 4]
    assert [e.payload["i"] for e in ledger.events(plan_id, types=["decision"])] == [0, 1, 2, 3, 4]
    assert [e.id for e in ledger.events(plan_id)] == sorted(e.id for e in ledger.events(plan_id))
    with pytest.raises(ValueError):
        ledger.add_event(plan_id, "bogus", {})


def test_started_waves_and_rollbacks(ledger, plan_id):
    assert ledger.started_waves(plan_id) == {} and ledger.rolled_back_devices(plan_id) == set()
    pl = {"kind": "wave_started", "rollout_id": 7, "device_ids": ["d1", "d2"], "started_at": "t"}
    ledger.add_event(plan_id, "action", pl, wave=1)
    ledger.add_event(plan_id, "action", {"kind": "other"}, wave=2)
    assert ledger.started_waves(plan_id) == {1: pl}
    ledger.add_event(plan_id, "action", {"kind": "rollback", "device_ids": ["d2"]})
    ledger.add_event(plan_id, "action", {"kind": "rollback", "device_ids": ["d1", "d2"]})
    assert ledger.rolled_back_devices(plan_id) == {"d1", "d2"}


def test_error_events_and_default_clock(tmp_path):
    lg = Ledger(tmp_path / "l.sqlite")
    pid = lg.create_plan("v2", "v1", [["a"]], {"a": {"hw_rev": "A", "region": "eu"}})
    e = lg.add_event(pid, "error", {"tool": "t", "code": "BAD_PHASE", "message": "m"})
    assert lg.events(pid, types=["error"]) == [e]
    assert e.ts.endswith("+00:00")
