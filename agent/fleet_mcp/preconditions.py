"""Server-side tool preconditions (docs/agent-design.md section 10.1).

Each check raises `PreconditionError` with a stable code. When the plan exists, an `error` event
`{tool, code, message}` is written to the ledger before raising.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from .errors import PreconditionError
from .ledger import Event, Ledger, Plan, parse_ts


def _refuse(ledger: Ledger, plan: Plan | None, tool: str, code: str, message: str) -> PreconditionError:
    """Log the refusal (if the plan exists) and return the error to raise."""
    if plan is not None:
        ledger.add_event(plan.plan_id, "error", {"tool": tool, "code": code, "message": message})
    return PreconditionError(code, message)


def _load_plan(ledger: Ledger, plan_id: str) -> Plan:
    return ledger.get_plan(plan_id)  # PLAN_NOT_FOUND: no plan, so no error event


def _require_phase(ledger: Ledger, plan: Plan, tool: str, phases: Iterable[str]) -> None:
    allowed = tuple(phases)
    if plan.phase not in allowed:
        raise _refuse(
            ledger, plan, tool, "BAD_PHASE", f"phase is {plan.phase}, {tool} needs {' or '.join(allowed)}"
        )


def _clamp_minutes(ledger: Ledger, plan: Plan, tool: str, minutes: float, max_minutes: float) -> float:
    if isinstance(minutes, bool) or not isinstance(minutes, (int, float)) or minutes <= 0:
        raise _refuse(
            ledger, plan, tool, "BAD_ARGUMENT", f"minutes must be a positive number, got {minutes!r}"
        )
    return float(min(minutes, max_minutes))


def _latest_wave(ledger: Ledger, plan: Plan, tool: str) -> int:
    started = ledger.started_waves(plan.plan_id)
    if not started:
        raise _refuse(ledger, plan, tool, "WAVE_ORDER", "no wave has been started")
    return max(started)


def check_evidence(
    ledger: Ledger,
    plan: Plan,
    evidence_id: str | None,
    kind: str,
    wave: int | None,
    allowed_verdicts: Iterable[str],
    now: datetime,
    max_age_s: float,
    tool: str,
) -> Event:
    """Validate evidence: exists, same plan, kind, wave, latest of its kind, verdict, fresh.

    `wave=None` skips the wave check and compares against the latest evidence of the kind for the plan.
    """

    def fail(code: str, msg: str) -> PreconditionError:
        return _refuse(ledger, plan, tool, code, msg)

    ev = ledger.get_evidence(evidence_id) if evidence_id else None
    if ev is None:
        raise fail("EVIDENCE_MISSING", f"evidence {evidence_id!r} not found")
    if ev.plan_id != plan.plan_id:
        raise fail("EVIDENCE_WRONG_PLAN", f"{ev.evidence_id} belongs to plan {ev.plan_id}")
    if ev.type != kind:
        raise fail("EVIDENCE_WRONG_KIND", f"{ev.evidence_id} is {ev.type} evidence, need {kind}")
    if wave is not None and ev.wave != wave:
        raise fail("EVIDENCE_WRONG_WAVE", f"{ev.evidence_id} is for wave {ev.wave}, need wave {wave}")
    latest = ledger.latest_evidence(plan.plan_id, kind, wave)
    if latest is not None and latest.evidence_id != ev.evidence_id:
        raise fail("EVIDENCE_SUPERSEDED", f"{ev.evidence_id} superseded by newer {latest.evidence_id}")
    allowed = tuple(allowed_verdicts)
    verdict = ev.payload.get("verdict")
    if verdict not in allowed:
        raise fail("EVIDENCE_VERDICT", f"{ev.evidence_id} verdict is {verdict}, need {' or '.join(allowed)}")
    age = (now - parse_ts(ev.ts)).total_seconds()
    if age > max_age_s:
        raise fail("EVIDENCE_STALE", f"{ev.evidence_id} is {age:.0f}s old, max {max_age_s:.0f}s")
    return ev


def check_start_wave(
    ledger: Ledger, plan_id: str, wave: int, evidence_id: str | None, now: datetime, max_age_s: float
) -> tuple[Plan, Event]:
    """Preconditions of `start_wave`; wave numbers are 1-based."""
    tool = "start_wave"
    plan = _load_plan(ledger, plan_id)
    if isinstance(wave, bool) or not isinstance(wave, int) or not 1 <= wave <= len(plan.waves):
        raise _refuse(ledger, plan, tool, "BAD_ARGUMENT", f"wave must be an int in 1..{len(plan.waves)}")
    if wave == 1:
        _require_phase(ledger, plan, tool, ["REHEARSED"])
        ev = check_evidence(ledger, plan, evidence_id, "rehearsal", None, ["pass"], now, max_age_s, tool)
        return plan, ev
    _require_phase(ledger, plan, tool, ["WAVE_OBSERVED"])
    started = ledger.started_waves(plan_id)
    if wave in started:
        raise _refuse(ledger, plan, tool, "WAVE_ORDER", f"wave {wave} is already started")
    if wave - 1 not in started:
        raise _refuse(ledger, plan, tool, "WAVE_ORDER", f"wave {wave - 1} has not been started")
    ev = check_evidence(ledger, plan, evidence_id, "observation", wave - 1, ["healthy"], now, max_age_s, tool)
    return plan, ev


def check_observe_wave(
    ledger: Ledger, plan_id: str, wave: int, minutes: float, max_minutes: float
) -> tuple[Plan, float]:
    """Preconditions of `observe_wave`; returns the plan and the clamped minutes.

    HALTED is allowed so regression evidence can be refreshed before `rollback`; the caller must not
    change the phase in that case.
    """
    tool = "observe_wave"
    plan = _load_plan(ledger, plan_id)
    _require_phase(ledger, plan, tool, ["WAVE_RUNNING", "WAVE_OBSERVED", "HALTED"])
    latest = _latest_wave(ledger, plan, tool)
    if wave != latest:
        raise _refuse(
            ledger, plan, tool, "WAVE_ORDER", f"wave {wave} is not the latest started wave ({latest})"
        )
    return plan, _clamp_minutes(ledger, plan, tool, minutes, max_minutes)


def check_halt(
    ledger: Ledger, plan_id: str, evidence_id: str | None, now: datetime, max_age_s: float
) -> tuple[Plan, Event]:
    """Preconditions of `halt_rollout`."""
    tool = "halt_rollout"
    plan = _load_plan(ledger, plan_id)
    _require_phase(ledger, plan, tool, ["WAVE_OBSERVED"])
    latest = _latest_wave(ledger, plan, tool)
    ev = check_evidence(
        ledger, plan, evidence_id, "observation", latest, ["regression"], now, max_age_s, tool
    )
    return plan, ev


def _updated_devices(ledger: Ledger, plan: Plan) -> set[str]:
    return {
        d for payload in ledger.started_waves(plan.plan_id).values() for d in payload.get("device_ids", [])
    }


def check_rollback(
    ledger: Ledger,
    plan_id: str,
    cohort: dict | None,
    targets: list[str] | None,
    to_version: str,
    evidence_id: str | None,
    now: datetime,
    max_age_s: float,
) -> tuple[Plan, Event, list[str]]:
    """Preconditions of `rollback`; returns the plan, the evidence and the resolved sorted targets."""
    tool = "rollback"
    plan = _load_plan(ledger, plan_id)
    if (cohort is None) == (targets is None):
        raise _refuse(ledger, plan, tool, "BAD_ARGUMENT", "give exactly one of cohort or targets")
    if cohort is not None and (not isinstance(cohort, dict) or not cohort):
        raise _refuse(
            ledger, plan, tool, "BAD_ARGUMENT", "cohort must be a non-empty object like {hw_rev: B}"
        )
    if targets is not None and (not isinstance(targets, list) or not targets):
        raise _refuse(ledger, plan, tool, "BAD_ARGUMENT", "targets must be a non-empty list of device ids")
    _require_phase(ledger, plan, tool, ["HALTED"])
    if to_version != plan.from_version:
        raise _refuse(
            ledger,
            plan,
            tool,
            "WRONG_TARGET_VERSION",
            f"to_version {to_version!r} is not the last known good {plan.from_version!r}",
        )
    latest = _latest_wave(ledger, plan, tool)
    ev = check_evidence(
        ledger, plan, evidence_id, "observation", latest, ["regression"], now, max_age_s, tool
    )
    updated = _updated_devices(ledger, plan)
    if cohort is not None:
        resolved = sorted(
            d for d in updated if all(plan.devices.get(d, {}).get(k) == v for k, v in cohort.items())
        )
        if not resolved:
            raise _refuse(
                ledger, plan, tool, "TARGET_NOT_ELIGIBLE", f"cohort {cohort} matches no updated device"
            )
    else:
        resolved = sorted(set(targets))
    affected = set(ev.payload.get("affected_hw_revs", []))
    bad = [d for d in resolved if d not in updated or plan.devices.get(d, {}).get("hw_rev") not in affected]
    if bad:
        raise _refuse(
            ledger,
            plan,
            tool,
            "TARGET_NOT_ELIGIBLE",
            f"not updated devices of an affected hw_rev ({', '.join(sorted(affected)) or 'none'}): "
            + ", ".join(bad),
        )
    return plan, ev, resolved


def check_verify_recovery(
    ledger: Ledger, plan_id: str, targets: list[str] | None, minutes: float, max_minutes: float
) -> tuple[Plan, list[str], float]:
    """Preconditions of `verify_recovery`; `targets=None` means every rolled-back device."""
    tool = "verify_recovery"
    plan = _load_plan(ledger, plan_id)
    _require_phase(ledger, plan, tool, ["ROLLED_BACK"])
    rolled = ledger.rolled_back_devices(plan_id)
    chosen = sorted(set(targets)) if targets else sorted(rolled)
    bad = [d for d in chosen if d not in rolled]
    if bad or not chosen:
        raise _refuse(
            ledger,
            plan,
            tool,
            "TARGET_NOT_ELIGIBLE",
            "not rolled-back devices: " + (", ".join(bad) or "none"),
        )
    return plan, chosen, _clamp_minutes(ledger, plan, tool, minutes, max_minutes)


def check_rehearse(
    ledger: Ledger, plan_id: str, hw_revs: list[str] | None, minutes: float, max_minutes: float
) -> tuple[Plan, list[str], float]:
    """Preconditions of `rehearse`; `hw_revs` None or empty means all of the plan's hw_revs.

    REHEARSED is allowed so stale rehearsal evidence can be refreshed before `start_wave`.
    """
    tool = "rehearse"
    plan = _load_plan(ledger, plan_id)
    _require_phase(ledger, plan, tool, ["PLANNED", "REHEARSED"])
    plan_revs = sorted({d["hw_rev"] for d in plan.devices.values()})
    chosen = sorted(set(hw_revs)) if hw_revs else plan_revs
    bad = [h for h in chosen if h not in plan_revs]
    if bad:
        raise _refuse(
            ledger,
            plan,
            tool,
            "BAD_ARGUMENT",
            f"hw_revs not in plan: {', '.join(bad)} (plan has {', '.join(plan_revs)})",
        )
    return plan, chosen, _clamp_minutes(ledger, plan, tool, minutes, max_minutes)


def check_record_decision(ledger: Ledger, plan_id: str, evidence_ids: list[str]) -> Plan:
    """Preconditions of `record_decision`: every evidence id exists and belongs to the plan."""
    tool = "record_decision"
    plan = _load_plan(ledger, plan_id)
    for eid in evidence_ids or []:
        ev = ledger.get_evidence(eid)
        if ev is None:
            raise _refuse(ledger, plan, tool, "EVIDENCE_MISSING", f"evidence {eid!r} not found")
        if ev.plan_id != plan.plan_id:
            raise _refuse(ledger, plan, tool, "EVIDENCE_WRONG_PLAN", f"{eid} belongs to plan {ev.plan_id}")
    return plan
