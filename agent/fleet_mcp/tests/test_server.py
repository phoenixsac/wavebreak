"""Tests for the MCP server wrapper (tool list, annotations, error surfacing, wiring)."""

from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from agent import register
from agent.fleet_mcp import ledger as ledger_mod
from agent.fleet_mcp import server
from agent.fleet_mcp.backends import HawkbitBackend, HttpLab, PromBackend
from agent.fleet_mcp.config import Settings
from agent.fleet_mcp.mock_lab import MockLab

from .fakes import Env, build_fleet, make_devices

run = asyncio.run

TOOLS = {
    "get_fleet_inventory",
    "get_rollout_state",
    "plan_rollout",
    "rehearse",
    "get_bundle_diff",
    "start_wave",
    "observe_wave",
    "record_decision",
    "halt_rollout",
    "rollback",
    "verify_recovery",
}
READERS = {"get_fleet_inventory", "get_rollout_state", "get_bundle_diff", "observe_wave", "verify_recovery"}
WRITERS = {"plan_rollout", "rehearse", "record_decision"}
APPROVAL = {"start_wave", "halt_rollout", "rollback"}


@pytest.fixture(autouse=True)
def fast_sqlite(monkeypatch):
    """Skip fsync: durability is not under test."""
    real_connect = sqlite3.connect

    def connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        conn.execute("PRAGMA synchronous=OFF")
        return conn

    monkeypatch.setattr(ledger_mod.sqlite3, "connect", connect)


@pytest.fixture
def env(tmp_path) -> Env:
    return build_fleet(tmp_path, make_devices(3, 3))


@pytest.fixture
def mcp(env):
    return server.create_server(env.fleet, env.settings)


def tools_by_name(mcp) -> dict:
    return {t.name: t for t in run(mcp.list_tools())}


SUMMARY = "rev A pass (slope 0.01 MB/min, 0 restarts, 0 OOM); rev B held; wave 1 = a1"


def call(mcp, name: str, **args) -> dict:
    """Call a tool and return its JSON payload."""
    result = run(mcp.call_tool(name, args))
    content = result[0] if isinstance(result, tuple) else result
    return json.loads(content[0].text)


# tool list and annotations -------------------------------------------------------


def test_lists_the_eleven_tools(mcp):
    tools = run(mcp.list_tools())
    assert len(tools) == 11
    assert {t.name for t in tools} == TOOLS


def test_annotation_matrix(mcp):
    tools = tools_by_name(mcp)
    for name in APPROVAL:
        ann = tools[name].annotations
        assert ann.destructiveHint is True and ann.readOnlyHint is False, name
    for name in READERS:
        assert tools[name].annotations.readOnlyHint is True, name
        assert not tools[name].annotations.destructiveHint, name
    for name in WRITERS:
        ann = tools[name].annotations
        assert ann.destructiveHint is False and ann.readOnlyHint is False, name
    assert APPROVAL | READERS | WRITERS == TOOLS


def test_approval_tools_are_exactly_the_destructive_tools(mcp):
    destructive = {n for n, t in tools_by_name(mcp).items() if t.annotations.destructiveHint}
    assert set(server.APPROVAL_TOOLS) == destructive == APPROVAL


def test_register_approval_tools_in_sync():
    assert set(register.APPROVAL_TOOLS) == set(server.APPROVAL_TOOLS)
    assert len(register.APPROVAL_TOOLS) == len(server.APPROVAL_TOOLS)


def test_every_tool_has_a_description_and_schema(mcp):
    for name, tool in tools_by_name(mcp).items():
        assert tool.description and tool.description.strip(), name
        assert tool.inputSchema["type"] == "object", name


# calling tools ---------------------------------------------------------------------


def test_call_tool_returns_content(mcp):
    result = run(mcp.call_tool("get_fleet_inventory", {}))
    content = result[0] if isinstance(result, tuple) else result
    assert content and content[0].type == "text"
    data = json.loads(content[0].text)
    assert data["total"] == 6 and data["by_hw_rev"] == {"A": 3, "B": 3}


def test_full_flow_through_tools(mcp, env):
    plan = call(mcp, "plan_rollout", version="v1.2", waves=[2, "rest"])
    pid = plan["plan_id"]
    rev = call(mcp, "rehearse", plan_id=pid)
    assert rev["verdict"] == "pass"
    started = call(
        mcp, "start_wave", plan_id=pid, wave=1, evidence_id=rev["evidence_id"], evidence_summary=SUMMARY
    )
    assert started["phase"] == "WAVE_RUNNING"
    obs = call(mcp, "observe_wave", plan_id=pid, wave=1, minutes=2)
    assert obs["verdict"] == "healthy"
    call(
        mcp,
        "record_decision",
        plan_id=pid,
        decision="go",
        rationale="healthy",
        evidence_ids=[obs["evidence_id"]],
    )
    state = call(mcp, "get_rollout_state")
    assert state["plan_id"] == pid and state["phase"] == "WAVE_OBSERVED"
    assert [d["decision"] for d in state["decisions"]] == ["go"]
    diff = call(mcp, "get_bundle_diff", from_version="v1.1", to_version="v1.2")
    assert "app/inference_app.py" in [f["path"] for f in diff["files"]]


def test_refusal_surfaces_as_tool_error_with_code(mcp):
    with pytest.raises(ToolError) as exc:
        run(
            mcp.call_tool(
                "start_wave",
                {"plan_id": "plan-nope", "wave": 1, "evidence_id": "ev-x", "evidence_summary": SUMMARY},
            )
        )
    assert "PLAN_NOT_FOUND" in str(exc.value)


def test_refusals_of_other_tools_carry_their_codes(mcp, env):
    pid = call(mcp, "plan_rollout", version="v1.2")["plan_id"]
    cases = [
        (
            "start_wave",
            {"plan_id": pid, "wave": 1, "evidence_id": "ev-x", "evidence_summary": SUMMARY},
            "BAD_PHASE",
        ),
        ("halt_rollout", {"plan_id": pid, "evidence_id": "ev-x", "evidence_summary": SUMMARY}, "BAD_PHASE"),
        (
            "rollback",
            {
                "plan_id": pid,
                "to_version": "v1.1",
                "evidence_id": "ev-x",
                "targets": ["a1"],
                "evidence_summary": SUMMARY,
            },
            "BAD_PHASE",
        ),
        ("observe_wave", {"plan_id": pid, "wave": 1}, "BAD_PHASE"),
        ("verify_recovery", {"plan_id": pid}, "BAD_PHASE"),
        ("plan_rollout", {"version": "v9.9"}, "UNKNOWN_VERSION"),
        ("record_decision", {"plan_id": pid, "decision": " ", "rationale": "r"}, "BAD_ARGUMENT"),
        ("get_bundle_diff", {"from_version": "v1.1", "to_version": "v9.9"}, "UNKNOWN_VERSION"),
    ]
    for name, args, code in cases:
        with pytest.raises(ToolError) as exc:
            run(mcp.call_tool(name, args))
        assert code in str(exc.value), (name, str(exc.value))
    assert env.field.start_wave_calls == [] and env.field.stop_calls == []


# build_fleet ---------------------------------------------------------------------------


def test_build_fleet_with_mock_lab_needs_no_network(tmp_path):
    settings = Settings(lab_mock=True, ledger_path=str(tmp_path / "sub" / "ledger.sqlite"))
    fleet = server.build_fleet(settings, env={})
    assert isinstance(fleet.lab, MockLab)
    assert isinstance(fleet.field, HawkbitBackend) and isinstance(fleet.metrics, PromBackend)
    assert (tmp_path / "sub" / "ledger.sqlite").exists()
    assert fleet.get_rollout_state()["plan"] is None  # ledger works; no backend was contacted


def test_build_fleet_mock_lab_passes_scenarios(tmp_path):
    settings = Settings(
        lab_mock=True, ledger_path=str(tmp_path / "l.sqlite"), lab_mock_scenarios={"v1.3": "crash"}
    )
    fleet = server.build_fleet(settings, env={})
    assert fleet.lab.scenario_for("v1.3", "A") == "crash"


def test_build_fleet_without_mock_uses_http_lab(tmp_path):
    settings = Settings(lab_mock=False, ledger_path=str(tmp_path / "l.sqlite"))
    assert isinstance(server.build_fleet(settings, env={}).lab, HttpLab)


def test_build_fleet_rejects_non_positive_time_scale(tmp_path):
    settings = Settings(lab_mock=True, ledger_path=str(tmp_path / "l.sqlite"))
    with pytest.raises(ValueError, match="TIME_SCALE"):
        server.build_fleet(settings, env={"FLEET_LAB_MOCK_TIME_SCALE": "0"})


def test_build_fleet_from_env(tmp_path):
    env = {"FLEET_LAB_MOCK": "1", "FLEET_LEDGER_PATH": str(tmp_path / "e.sqlite")}
    fleet = server.build_fleet(env=env)
    assert isinstance(fleet.lab, MockLab) and fleet.settings.ledger_path == env["FLEET_LEDGER_PATH"]


# evidence_summary (shown in the approval prompt) ----------------------------------------------------


@pytest.mark.parametrize("name", sorted(APPROVAL))
def test_gated_tools_require_evidence_summary_in_schema(mcp, name):
    schema = tools_by_name(mcp)[name].inputSchema
    assert "evidence_summary" in schema["required"]
    assert schema["properties"]["evidence_summary"]["minLength"] == server.MIN_SUMMARY_CHARS


@pytest.mark.parametrize("summary", [None, "", "   ", "ok", " short text "])
def test_start_wave_refuses_missing_or_trivial_summary_and_starts_nothing(mcp, env, summary):
    pid = call(mcp, "plan_rollout", version="v1.2", waves=[2, "rest"])["plan_id"]
    rev = call(mcp, "rehearse", plan_id=pid)
    args = {"plan_id": pid, "wave": 1, "evidence_id": rev["evidence_id"]}
    if summary is not None:
        args["evidence_summary"] = summary
    with pytest.raises(ToolError):
        run(mcp.call_tool("start_wave", args))
    assert env.field.start_wave_calls == []
    assert env.ledger.get_plan(pid).phase == "REHEARSED"


def test_summary_is_recorded_with_the_approval(mcp, env):
    pid = call(mcp, "plan_rollout", version="v1.2", waves=[2, "rest"])["plan_id"]
    rev = call(mcp, "rehearse", plan_id=pid)
    call(mcp, "start_wave", plan_id=pid, wave=1, evidence_id=rev["evidence_id"], evidence_summary=SUMMARY)
    (approval,) = env.ledger.events(pid, types=["approval"])
    assert approval.payload["evidence_summary"] == SUMMARY and approval.payload["tool"] == "start_wave"
    state = call(mcp, "get_rollout_state", plan_id=pid)
    assert state["approvals"][0]["evidence_summary"] == SUMMARY


def test_halt_and_rollback_require_summary(mcp, env):
    pid = call(mcp, "plan_rollout", version="v1.2")["plan_id"]
    for name, args in (
        ("halt_rollout", {"plan_id": pid, "evidence_id": "ev-x"}),
        ("rollback", {"plan_id": pid, "to_version": "v1.1", "evidence_id": "ev-x", "targets": ["a1"]}),
    ):
        with pytest.raises(ToolError) as exc:
            run(mcp.call_tool(name, args))
        assert "evidence_summary" in str(exc.value)
    assert env.field.stop_calls == []
