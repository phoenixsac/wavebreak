"""MCP server of wavebreak-fleet (docs/agent-design.md sections 10 and 10.1).

Thin wrappers over `Fleet`: each tool converts `PreconditionError` to `ToolError` so the harness
sees the stable refusal code. The three approval tools are destructive and must be listed in the
agent's `require_approval_for_tools` (A15).
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Mapping
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from wavebreak_clients.hawkbit import HawkbitClient
from wavebreak_clients.observability import Loki, Prometheus

from .backends import HawkbitBackend, HttpLab, PromBackend
from .config import Settings
from .errors import PreconditionError
from .fleet import Fleet
from .ledger import Ledger
from .mock_lab import MockLab

APPROVAL_TOOLS = ("start_wave", "halt_rollout", "rollback")

READ_ONLY = ToolAnnotations(readOnlyHint=True)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False)
DESTRUCTIVE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False
)

PlanId = Annotated[str, Field(description="Plan id returned by plan_rollout, like plan-20260926-ab12.")]
EvidenceId = Annotated[
    str, Field(description="Evidence id (ev-xxxxxxxx) returned by rehearse or observe_wave.")
]


def build_fleet(settings: Settings | None = None, env: Mapping[str, str] | None = None) -> Fleet:
    """Wire the real backends (or the mock lab when `settings.lab_mock`) into a Fleet. No I/O happens here."""
    settings = settings or Settings.from_env(env)
    e = os.environ if env is None else env
    field = HawkbitBackend(
        HawkbitClient(settings.hawkbit_url, settings.hawkbit_username, settings.hawkbit_password),
        settings.ds_name,
    )
    metrics = PromBackend(Prometheus(settings.prometheus_url), Loki(settings.loki_url))
    ledger = Ledger(settings.ledger_path)
    if not settings.lab_mock:
        return Fleet(
            settings, ledger, field, metrics, HttpLab(settings.lab_url, settings.lab_api_token, timeout=120)
        )
    scale = float(e.get("FLEET_LAB_MOCK_TIME_SCALE") or 1)
    if scale <= 0:
        raise ValueError("FLEET_LAB_MOCK_TIME_SCALE must be positive")
    lab = MockLab(settings.lab_mock_scenarios, clock=lambda: time.time() * scale)
    return Fleet(
        settings, ledger, field, metrics, lab, lab_sleep=lambda seconds: asyncio.sleep(seconds / scale)
    )


def _refusal(exc: PreconditionError) -> ToolError:
    return ToolError(str(exc))


def create_server(fleet: Fleet, settings: Settings) -> FastMCP:
    """Build the FastMCP server with the 11 fleet tools."""
    mcp = FastMCP("wavebreak-fleet", host=settings.mcp_host, port=settings.mcp_port)

    @mcp.tool(annotations=READ_ONLY)
    def get_fleet_inventory() -> dict[str, Any]:
        """List the field fleet: counts by version, hw_rev, region and version x hw_rev, plus a device list (first 100).
        No preconditions. Call it first, before plan_rollout."""
        try:
            return fleet.get_fleet_inventory()
        except PreconditionError as exc:
            raise _refusal(exc) from exc

    @mcp.tool(annotations=READ_ONLY)
    def get_rollout_state(
        plan_id: Annotated[str | None, Field(description="Plan id; default is the latest plan.")] = None,
    ) -> dict[str, Any]:
        """Show phase, waves (status, rollout ids), evidence with age and freshness, decisions, approvals, actions, errors, per-device installed versions and the tools allowed now (next_allowed).
        Call it at the start of every phase; it is the source of truth for progress."""
        try:
            return fleet.get_rollout_state(plan_id)
        except PreconditionError as exc:
            raise _refusal(exc) from exc

    @mcp.tool(annotations=WRITE)
    def plan_rollout(
        version: Annotated[
            str, Field(description="Target firmware version, like v1.2; must exist in hawkBit.")
        ],
        waves: Annotated[
            list[int | str] | None,
            Field(description='Wave sizes; last may be "rest". Default [2, 5, "rest"].'),
        ] = None,
        stratify_by: Annotated[
            str, Field(description="Attribute to stratify canaries by: hw_rev, region or version.")
        ] = "hw_rev",
        exclude_hw_revs: Annotated[
            list[str] | None,
            Field(
                description='PARTIAL ROLLOUT: hw_revs to hold out of this plan, like ["B"]. Needs exclusion_evidence_id.'
            ),
        ] = None,
        exclusion_evidence_id: Annotated[
            str | None,
            Field(
                description="Rehearsal evidence id of this version that FAILED for every hw_rev in exclude_hw_revs (from the blocked plan)."
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Create a stratified wave plan for `version`; devices already on it are excluded and from_version is the most common installed version.
        Supersedes an unstarted plan and is refused while another plan is running (ACTIVE_PLAN_EXISTS). Returns plan_id and the waves; next step is rehearse.
        Partial rollout: when a rehearsal failed for only some hw_revs, call again with exclude_hw_revs and that rehearsal's evidence id; the held cohort is recorded in the ledger and listed as held in get_rollout_state. It stays on from_version."""
        try:
            return fleet.plan_rollout(version, waves, stratify_by, exclude_hw_revs, exclusion_evidence_id)
        except PreconditionError as exc:
            raise _refusal(exc) from exc

    @mcp.tool(annotations=WRITE)
    async def rehearse(
        plan_id: PlanId,
        hw_revs: Annotated[
            list[str] | None, Field(description="hw_revs to rehearse; default all in the plan.")
        ] = None,
        minutes: Annotated[
            float | None, Field(description="Window in minutes; default 2, capped at the lab maximum.")
        ] = None,
    ) -> dict[str, Any]:
        """Install the plan's version on one lab device per hw_rev and watch it for the window; needs phase PLANNED or REHEARSED.
        Returns rehearsal evidence (pass or fail per hw_rev) and moves the plan to REHEARSED or BLOCKED. Covers only the window, so later-onset faults are missed. Next step is start_wave 1 with the evidence_id."""
        try:
            return await fleet.rehearse(plan_id, hw_revs, minutes)
        except PreconditionError as exc:
            raise _refusal(exc) from exc

    @mcp.tool(annotations=READ_ONLY)
    def get_bundle_diff(
        from_version: Annotated[str, Field(description="Older bundle version, like v1.1.")],
        to_version: Annotated[str, Field(description="Newer bundle version, like v1.2.")],
    ) -> dict[str, Any]:
        """Summarise the file changes between two firmware bundles (changed files with short diffs).
        Use it as a root-cause hint after a regression."""
        try:
            return fleet.get_bundle_diff(from_version, to_version)
        except PreconditionError as exc:
            raise _refusal(exc) from exc

    @mcp.tool(annotations=DESTRUCTIVE)
    def start_wave(
        plan_id: PlanId,
        wave: Annotated[int, Field(description="Wave number, 1-based.")],
        evidence_id: Annotated[
            str,
            Field(
                description="Wave 1: latest passing rehearsal evidence. Wave n>1: latest healthy observation of wave n-1."
            ),
        ],
    ) -> dict[str, Any]:
        """Start the hawkBit rollout for one wave; the evidence must be the latest of its kind, passing and fresh, and the phase REHEARSED (wave 1) or WAVE_OBSERVED (wave n>1).
        Requires human approval; irreversible for the field devices. Returns the rollout id and targets; next step is observe_wave."""
        try:
            return fleet.start_wave(plan_id, wave, evidence_id)
        except PreconditionError as exc:
            raise _refusal(exc) from exc

    @mcp.tool(annotations=READ_ONLY)
    async def observe_wave(
        plan_id: PlanId,
        wave: Annotated[int, Field(description="The latest started wave.")],
        minutes: Annotated[
            float | None, Field(description="Soak time in minutes; default 2, capped at the observe maximum.")
        ] = None,
    ) -> dict[str, Any]:
        """Soak the wave, then compare updated devices with control devices of the same hw_rev; blocks for the window.
        Needs phase WAVE_RUNNING or WAVE_OBSERVED (HALTED only refreshes evidence). Returns observation evidence with a verdict (healthy, regression, inconclusive) and the affected hw_revs; writes to the ledger."""
        try:
            return await fleet.observe_wave(plan_id, wave, minutes)
        except PreconditionError as exc:
            raise _refusal(exc) from exc

    @mcp.tool(annotations=WRITE)
    def record_decision(
        plan_id: PlanId,
        decision: Annotated[str, Field(description="Short decision, like 'halt and roll back rev B'.")],
        rationale: Annotated[str, Field(description="Why, in one or two sentences.")],
        evidence_ids: Annotated[
            list[str] | None,
            Field(description="Evidence ids the decision rests on; must belong to the plan."),
        ] = None,
    ) -> dict[str, Any]:
        """Write a decision and its rationale to the ledger audit trail.
        Every evidence id must exist and belong to the plan. Returns ok and the event id."""
        try:
            return fleet.record_decision(plan_id, decision, rationale, evidence_ids)
        except PreconditionError as exc:
            raise _refusal(exc) from exc

    @mcp.tool(annotations=DESTRUCTIVE)
    def halt_rollout(
        plan_id: PlanId,
        evidence_id: Annotated[str, Field(description="Latest regression observation of the latest wave.")],
    ) -> dict[str, Any]:
        """Stop all started hawkBit rollouts of the plan; needs phase WAVE_OBSERVED and fresh regression evidence of the latest wave.
        Requires human approval; irreversible for the field devices. Returns the stopped rollouts; next step is rollback of the affected cohort."""
        try:
            return fleet.halt_rollout(plan_id, evidence_id)
        except PreconditionError as exc:
            raise _refusal(exc) from exc

    @mcp.tool(annotations=DESTRUCTIVE)
    def rollback(
        plan_id: PlanId,
        to_version: Annotated[str, Field(description="The plan's from_version (last known good).")],
        evidence_id: Annotated[
            str, Field(description="Regression observation used to halt, or a newer one.")
        ],
        cohort: Annotated[
            dict[str, str] | None,
            Field(
                description='Attribute filter over updated devices, like {"hw_rev": "B"}. Give cohort or targets.'
            ),
        ] = None,
        targets: Annotated[
            list[str] | None, Field(description="Explicit device ids. Give cohort or targets, not both.")
        ] = None,
    ) -> dict[str, Any]:
        """Assign the last known good version to the affected updated devices; needs phase HALTED, fresh regression evidence, and targets that are updated devices of an affected hw_rev.
        Requires human approval; irreversible for the field devices. Returns per-device assignments; next step is verify_recovery."""
        try:
            return fleet.rollback(plan_id, to_version, evidence_id, cohort, targets)
        except PreconditionError as exc:
            raise _refusal(exc) from exc

    @mcp.tool(annotations=READ_ONLY)
    async def verify_recovery(
        plan_id: PlanId,
        targets: Annotated[
            list[str] | None, Field(description="Rolled-back device ids; default all rolled-back devices.")
        ] = None,
        minutes: Annotated[
            float | None, Field(description="Soak time in minutes; default 2, capped at the observe maximum.")
        ] = None,
    ) -> dict[str, Any]:
        """Wait, then check that rolled-back devices run the old version without restarts or OOM kills; needs phase ROLLED_BACK.
        Returns verification evidence (recovered or not_recovered per device); the plan becomes VERIFIED only if all recovered. Writes to the ledger."""
        try:
            return await fleet.verify_recovery(plan_id, targets, minutes)
        except PreconditionError as exc:
            raise _refusal(exc) from exc

    return mcp
