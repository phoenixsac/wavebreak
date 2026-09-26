"""Service layer of the wavebreak-fleet server (docs/agent-design.md sections 9, 10, 10.1, 11, 12).

`Fleet` holds all business logic and knows nothing about MCP. Backends, clock and sleeps are
injected, so every path is testable without network or real waiting.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from .assess import assess_hw_rev, assess_rehearsal, assess_rehearsal_device, assess_wave
from .backends import FieldBackend, LabBackend, MetricsBackend
from .bundle_diff import bundle_diff
from .config import Settings
from .errors import PreconditionError
from .ledger import Ledger, Plan, parse_ts
from .models import Device, DeviceStats
from .planner import pick_from_version, plan_waves
from .preconditions import (
    check_halt,
    check_observe_wave,
    check_record_decision,
    check_rehearse,
    check_rollback,
    check_start_wave,
    check_verify_recovery,
)

DEFAULT_WAVES: list[int | str] = [2, 5, "rest"]
DEFAULT_MINUTES = 2.0
LIST_CAP = 50
INVENTORY_CAP = 100
BASELINE_WINDOW_S = 600.0
REHEARSAL_CAVEAT = "Rehearsal covers only the observed window; faults with later onset are not detected."


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _round(value: Any) -> Any:
    """Round floats to 3 decimals in nested dict/list structures."""
    if isinstance(value, float):
        return round(value, 3)
    if isinstance(value, dict):
        return {k: _round(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_round(v) for v in value]
    return value


def _cap(items: list, n: int = LIST_CAP) -> tuple[list, int]:
    """First `n` items and the number of dropped ones."""
    return items[:n], max(0, len(items) - n)


def _cap_dict(d: dict, n: int = LIST_CAP) -> tuple[dict, int]:
    keys = list(d)[:n]
    return {k: d[k] for k in keys}, max(0, len(d) - n)


def _missing_stats(device: Device) -> DeviceStats:
    return DeviceStats(device.id, device.hw_rev, False, 0, 0.0, None, 0, 0, None)


class Fleet:
    """Business logic of the 11 fleet tools."""

    def __init__(
        self,
        settings: Settings,
        ledger: Ledger,
        field: FieldBackend,
        metrics: MetricsBackend,
        lab: LabBackend,
        *,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], Any] = asyncio.sleep,
        lab_sleep: Callable[[float], Any] | None = None,
    ) -> None:
        self.settings = settings
        self.ledger = ledger
        self.field = field
        self.metrics = metrics
        self.lab = lab
        self._clock = clock or _utcnow
        self._sleep = sleep
        self._lab_sleep = lab_sleep or sleep

    # helpers ---------------------------------------------------------------

    def _now(self) -> datetime:
        return self._clock().astimezone(UTC)

    def _call(self, plan_id: str | None, what: str, fn: Callable, *args, code: str = "BACKEND_ERROR"):
        """Run a backend call; wrap failures as PreconditionError(code) and log an `error` event."""
        try:
            return fn(*args)
        except PreconditionError:
            raise
        except Exception as exc:
            message = f"{what}: {exc}"
            if plan_id is not None:
                self.ledger.add_event(plan_id, "error", {"tool": what, "code": code, "message": message})
            raise PreconditionError(code, message) from exc

    async def _sleep_chunks(self, seconds: float) -> None:
        """Wait `seconds` in chunks of the poll interval so the call stays cancellable."""
        chunk = self.settings.poll_interval_s if self.settings.poll_interval_s > 0 else 15.0
        remaining = float(seconds)
        while remaining > 0:
            step = min(chunk, remaining)
            await self._sleep(step)
            remaining -= step

    @staticmethod
    def _default_minutes(minutes: float | None, cap: float) -> float:
        """Requested minutes, or min(2, cap) when not given."""
        return min(DEFAULT_MINUTES, cap) if minutes is None else minutes

    def _approval(self, plan_id: str, tool: str, evidence_id: str, wave: int | None = None) -> None:
        self.ledger.add_event(
            plan_id,
            "approval",
            {"tool": tool, "approved_via": "harness", "evidence_id": evidence_id},
            wave=wave,
        )

    # reads -----------------------------------------------------------------

    def get_fleet_inventory(self) -> dict:
        """Counts by version, hw_rev, region and version x hw_rev, plus a capped device list."""
        devices = self._call(None, "inventory", self.field.inventory)
        by_vh: dict[str, Counter] = {}
        for d in devices:
            by_vh.setdefault(d.version, Counter())[d.hw_rev] += 1
        shown, dropped = _cap(list(devices), INVENTORY_CAP)
        out = {
            "total": len(devices),
            "by_version": dict(Counter(d.version for d in devices)),
            "by_hw_rev": dict(Counter(d.hw_rev for d in devices)),
            "by_region": dict(Counter(d.region for d in devices)),
            "by_version_hw_rev": {v: dict(c) for v, c in by_vh.items()},
            "devices": [
                {"id": d.id, "hw_rev": d.hw_rev, "region": d.region, "version": d.version} for d in shown
            ],
        }
        if dropped:
            out["truncated"] = dropped
        return out

    def get_bundle_diff(self, from_version: str, to_version: str) -> dict:
        """Summarised diff between two firmware bundles."""
        return bundle_diff(self.settings.bundles_dir, from_version, to_version)

    def get_rollout_state(self, plan_id: str | None = None) -> dict:
        """Full picture of one plan (default: the latest); compaction-safe source of truth."""
        plan = self.ledger.get_plan(plan_id) if plan_id else self.ledger.latest_plan()
        if plan is None:
            return {"plan": None, "message": "no rollout plans yet; call plan_rollout"}
        led, pid = self.ledger, plan.plan_id
        started = led.started_waves(pid)
        observations = led.events(pid, types=["observation"])
        latest_obs = {e.wave: e for e in observations}
        now = self._now()
        out = {
            "plan_id": pid,
            "version": plan.version,
            "from_version": plan.from_version,
            "phase": plan.phase,
            "waves": self._wave_rows(plan, started, latest_obs),
            "evidence": [
                self._evidence_row(e, now)
                for e in led.events(pid, types=["rehearsal", "observation", "verification"], limit=10)
            ],
            "decisions": [
                {
                    "event_id": e.id,
                    "ts": e.ts,
                    "decision": e.payload.get("decision"),
                    "rationale": str(e.payload.get("rationale", ""))[:300],
                    "evidence_ids": e.payload.get("evidence_ids", []),
                }
                for e in led.events(pid, types=["decision"], limit=10)
            ],
            "approvals": [
                {"ts": e.ts, "wave": e.wave, **{k: v for k, v in e.payload.items()}}
                for e in led.events(pid, types=["approval"], limit=10)
            ],
            "actions": [self._action_row(e) for e in led.events(pid, types=["action"], limit=10)],
            "recent_errors": [
                {"ts": e.ts, **{k: e.payload.get(k) for k in ("tool", "code", "message")}}
                for e in led.events(pid, types=["error"], limit=5)
            ],
            "next_allowed": self._next_allowed(plan, started),
        }
        out["devices"], extra = self._device_rows(plan)
        out.update(extra)
        return out

    def _wave_rows(self, plan: Plan, started: dict[int, dict], latest_obs: dict) -> list[dict]:
        rows = []
        for n, ids in enumerate(plan.waves, start=1):
            info, obs = started.get(n), latest_obs.get(n)
            shown, dropped = _cap(ids)
            row = {
                "wave": n,
                "devices": shown,
                "status": "pending" if info is None else ("observed" if obs else "running"),
                "rollout_id": info.get("rollout_id") if info else None,
                "started_at": info.get("started_at") if info else None,
                "observation_evidence_id": obs.evidence_id if obs else None,
                "observation_verdict": obs.payload.get("verdict") if obs else None,
            }
            if dropped:
                row["truncated"] = dropped
            rows.append(row)
        return rows

    def _evidence_row(self, e, now: datetime) -> dict:
        age = (now - parse_ts(e.ts)).total_seconds()
        return {
            "evidence_id": e.evidence_id,
            "kind": e.type,
            "wave": e.wave,
            "verdict": e.payload.get("verdict"),
            "ts": e.ts,
            "age_s": round(age),
            "fresh": age <= self.settings.evidence_max_age_s,
        }

    @staticmethod
    def _action_row(e) -> dict:
        p = e.payload
        row: dict = {"ts": e.ts, "kind": p.get("kind"), "wave": e.wave}
        for key in ("rollout_id", "started_at", "to_version", "evidence_id"):
            if key in p:
                row[key] = p[key]
        if "device_ids" in p:
            row["n_devices"] = len(p["device_ids"])
        if p.get("kind") == "halt":
            row["results"] = _cap(p.get("results", []), 5)[0]
        return row

    def _device_rows(self, plan: Plan) -> tuple[dict, dict]:
        wave_of = {d: n for n, ids in enumerate(plan.waves, start=1) for d in ids}
        ids, dropped = _cap(sorted(plan.devices), INVENTORY_CAP)
        extra: dict = {}
        try:
            installed = self.field.installed_versions(ids)
        except Exception as exc:  # noqa: BLE001 - hawkBit being down must not hide the ledger state
            installed = None
            extra["hawkbit_error"] = f"{type(exc).__name__}: {exc}"
        rows = {}
        for d in ids:
            meta = plan.devices[d]
            row = {"hw_rev": meta.get("hw_rev"), "region": meta.get("region"), "planned_wave": wave_of.get(d)}
            if installed is not None:
                row["installed_version"] = installed.get(d)
            rows[d] = row
        if dropped:
            extra["devices_truncated"] = dropped
        return rows, extra

    @staticmethod
    def _next_allowed(plan: Plan, started: dict[int, dict]) -> list[str]:
        phase = plan.phase
        allowed: list[str] = []
        if phase in ("PLANNED", "REHEARSED"):
            allowed.append("rehearse")
        if phase == "REHEARSED":
            allowed.append("start_wave")
        if phase in ("WAVE_RUNNING", "WAVE_OBSERVED", "HALTED"):
            allowed.append("observe_wave")
        if phase == "WAVE_OBSERVED":
            if started and max(started) < len(plan.waves):
                allowed.append("start_wave")
            allowed.append("halt_rollout")
        if phase == "HALTED":
            allowed.append("rollback")
        if phase == "ROLLED_BACK":
            allowed.append("verify_recovery")
        if phase in ("PLANNED", "REHEARSED", "BLOCKED", "COMPLETE", "VERIFIED"):
            allowed.append("plan_rollout")
        allowed.append("record_decision")
        return allowed

    # plan ------------------------------------------------------------------

    def plan_rollout(
        self, version: str, waves: list[int | str] | None = None, stratify_by: str = "hw_rev"
    ) -> dict:
        """Create a stratified rollout plan (supersedes a PLANNED/REHEARSED one)."""
        try:
            self.field.distribution_set_id(version)
        except LookupError as exc:
            raise PreconditionError(
                "UNKNOWN_VERSION", f"version {version!r} is not published: {exc}"
            ) from exc
        except Exception as exc:
            raise PreconditionError("BACKEND_ERROR", f"distribution_set_id: {exc}") from exc
        inventory = self._call(None, "inventory", self.field.inventory)
        eligible = [d for d in inventory if d.version not in (version, "none")]
        if not eligible:
            raise PreconditionError("BAD_ARGUMENT", "no eligible devices")
        from_version = pick_from_version({d.id: d.version for d in eligible})
        candidates = [d for d in eligible if d.version == from_version]
        wave_devices, warnings = plan_waves(candidates, list(waves or DEFAULT_WAVES), stratify_by)
        planned = {d.id for w in wave_devices for d in w}
        excluded = self._excluded(inventory, version, from_version, planned)
        meta = {d.id: {"hw_rev": d.hw_rev, "region": d.region} for w in wave_devices for d in w}
        plan_id = self.ledger.create_plan(
            version, from_version, [[d.id for d in w] for w in wave_devices], meta
        )
        wave_rows = []
        for n, w in enumerate(wave_devices, start=1):
            shown, dropped = _cap([d.id for d in w])
            row = {
                "wave": n,
                "size": len(w),
                "devices": shown,
                "by_hw_rev": dict(sorted(Counter(d.hw_rev for d in w).items())),
            }
            if dropped:
                row["truncated"] = dropped
            wave_rows.append(row)
        shown_ex, dropped_ex = _cap(excluded)
        out = {
            "plan_id": plan_id,
            "version": version,
            "from_version": from_version,
            "phase": "PLANNED",
            "waves": wave_rows,
            "excluded": shown_ex,
            "warnings": warnings,
            "next": "rehearse",
        }
        if dropped_ex:
            out["excluded_truncated"] = dropped_ex
        return out

    @staticmethod
    def _excluded(inventory: list[Device], version: str, from_version: str, planned: set[str]) -> list[dict]:
        out = []
        for d in inventory:
            if d.id in planned:
                continue
            if d.version == version:
                reason = f"already on {version}"
            elif d.version == "none":
                reason = "no installed version reported"
            elif d.version != from_version:
                reason = f"on {d.version}, plan upgrades from {from_version}"
            else:
                reason = "beyond the requested wave sizes"
            out.append({"id": d.id, "reason": reason})
        return out

    # rehearse --------------------------------------------------------------

    async def rehearse(
        self, plan_id: str, hw_revs: list[str] | None = None, minutes: float | None = None
    ) -> dict:
        """Rehearse the plan's version on lab devices (one per hw_rev) and record rehearsal evidence."""
        s = self.settings
        want = self._default_minutes(minutes, s.lab_max_minutes)
        plan, revs, mins = check_rehearse(self.ledger, plan_id, hw_revs, want, s.lab_max_minutes)
        created: list[str] = []
        cleanup_errors: list[str] = []
        try:
            per_hw = await self._run_rehearsal(plan, revs, mins, created, cleanup_errors)
        finally:
            self._destroy_lab(created, cleanup_errors)
        verdict = assess_rehearsal(per_hw)
        payload = {
            "per_hw_rev": per_hw,
            "window_minutes": mins,
            "hw_revs": revs,
            "version": plan.version,
            "from_version": plan.from_version,
        }
        ev = self.ledger.new_evidence(plan_id, "rehearsal", verdict, payload)
        phase = self.ledger.set_phase(
            plan_id, "REHEARSED" if verdict == "pass" else "BLOCKED", allowed_from=("PLANNED", "REHEARSED")
        ).phase
        out = {
            "evidence_id": ev.evidence_id,
            "verdict": verdict,
            "window_minutes": mins,
            "per_hw_rev": _round(per_hw),
            "phase": phase,
            "caveat": REHEARSAL_CAVEAT,
            "next": (
                "start_wave 1 with this evidence_id (needs approval)"
                if verdict == "pass"
                else "rehearsal failed: plan is BLOCKED; report and do not roll out"
            ),
        }
        not_done = sorted({d["hw_rev"] for d in plan.devices.values()} - set(revs))
        if not_done:
            out["not_rehearsed"] = not_done
        if cleanup_errors:
            out["cleanup_errors"] = cleanup_errors
        return out

    async def _run_rehearsal(
        self, plan: Plan, revs: list[str], mins: float, created: list[str], cleanup_errors: list[str]
    ) -> dict:
        """Rehearse the hw_revs in batches of `lab_parallel` lab devices; each batch is destroyed before the next.

        `created` holds the lab devices still alive so the caller can always clean up after an error.
        """
        size = max(1, self.settings.lab_parallel)
        out: dict = {}
        for i in range(0, len(revs), size):
            out.update(await self._rehearse_batch(plan, revs[i : i + size], mins, created))
            self._destroy_lab(created, cleanup_errors)
        return out

    def _destroy_lab(self, created: list[str], cleanup_errors: list[str]) -> None:
        """Destroy every lab device in `created` (emptying it); errors are collected, not raised."""
        while created:
            lab_id = created.pop()
            try:
                self.lab.destroy(lab_id)
            except Exception as exc:  # noqa: BLE001 - cleanup must not mask the result
                cleanup_errors.append(f"{lab_id}: {exc}")

    async def _rehearse_batch(self, plan: Plan, revs: list[str], mins: float, created: list[str]) -> dict:
        """Create/install/poll one lab device per hw_rev in `revs` and assess each."""
        pid, lab = plan.plan_id, self.lab
        devices: dict[str, dict] = {}
        for hw in revs:
            lab_id = self._call(
                pid, "lab.create_device", lab.create_device, hw, plan.from_version, code="LAB_ERROR"
            )
            created.append(lab_id)
            inst = self._call(pid, "lab.install", lab.install, lab_id, plan.version, code="LAB_ERROR")
            base = self._call(pid, "lab.summary", lab.summary, lab_id, code="LAB_ERROR")
            devices[hw] = {"id": lab_id, "install": inst, "base": base, "base_t": lab.now()}
        t0 = lab.now()
        final: dict[str, dict] = {}
        while lab.now() - t0 < mins * 60:
            await self._lab_sleep(self.settings.poll_interval_s)
            for hw, dev in devices.items():
                final[hw] = self._call(pid, "lab.summary", lab.summary, dev["id"], code="LAB_ERROR")
        for hw, dev in devices.items():
            if hw not in final:
                final[hw] = self._call(pid, "lab.summary", lab.summary, dev["id"], code="LAB_ERROR")
        return {
            hw: assess_rehearsal_device(self._rehearsal_input(dev, final[hw]), self.settings.thresholds)
            for hw, dev in devices.items()
        }

    @staticmethod
    def _rehearsal_input(dev: dict, final: dict) -> dict:
        base, base_t = dev["base"], dev["base_t"]
        return {
            "restarts": max(0, int(final.get("restarts") or 0) - int(base.get("restarts") or 0)),
            "oom_kills": max(0, int(final.get("oom_kills") or 0) - int(base.get("oom_kills") or 0)),
            "memory_samples": [m for m in final.get("memory_samples") or [] if m["t"] >= base_t],
            "unit_state": final.get("unit_state"),
            "last_install": {
                "result": dev["install"].get("result"),
                "detail": dev["install"].get("detail", ""),
            },
        }

    # start_wave ------------------------------------------------------------

    def start_wave(self, plan_id: str, wave: int, evidence_id: str) -> dict:
        """Start one hawkBit rollout for the wave's devices (approval-gated)."""
        plan, _ev = check_start_wave(
            self.ledger, plan_id, wave, evidence_id, self._now(), self.settings.evidence_max_age_s
        )
        self._approval(plan_id, "start_wave", evidence_id, wave)
        device_ids = list(plan.waves[wave - 1])
        res = self._call(
            plan_id, "start_wave", self.field.start_wave, f"{plan_id}-wave{wave}", plan.version, device_ids
        )
        started_at = self._now().isoformat()
        self.ledger.add_event(
            plan_id,
            "action",
            {
                "kind": "wave_started",
                "rollout_id": res["rollout_id"],
                "device_ids": device_ids,
                "started_at": started_at,
                "evidence_id": evidence_id,
            },
            wave=wave,
        )
        phase = self.ledger.set_phase(
            plan_id, "WAVE_RUNNING", allowed_from=("REHEARSED", "WAVE_OBSERVED")
        ).phase
        shown, dropped = _cap(list(res.get("targets") or device_ids))
        out = {
            "plan_id": plan_id,
            "wave": wave,
            "rollout_id": res["rollout_id"],
            "targets": shown,
            "started_at": started_at,
            "phase": phase,
            "next": "observe_wave",
        }
        if dropped:
            out["targets_truncated"] = dropped
        return out

    # observe_wave ----------------------------------------------------------

    async def observe_wave(self, plan_id: str, wave: int, minutes: float | None = None) -> dict:
        """Soak, then compare updated vs control devices per hw_rev and record observation evidence."""
        s = self.settings
        want = self._default_minutes(minutes, s.observe_max_minutes)
        plan, mins = check_observe_wave(self.ledger, plan_id, wave, want, s.observe_max_minutes)
        await self._sleep_chunks(mins * 60)
        started = self.ledger.started_waves(plan_id)
        start = parse_ts(started[wave]["started_at"]).timestamp()
        end = self._now().timestamp()
        wave_ids = list(started[wave]["device_ids"])
        inventory = self._call(plan_id, "inventory", self.field.inventory)
        failures = set(self._call(plan_id, "install_failures", self.field.install_failures, wave_ids, start))
        started_ids = {d for p in started.values() for d in p.get("device_ids", [])}
        results = [
            self._assess_hw_rev(plan, hw, wave_ids, inventory, started_ids, failures, start, end)
            for hw in sorted({plan.devices[d]["hw_rev"] for d in wave_ids})
        ]
        wave_res = assess_wave(results)
        verdict = wave_res["verdict"]
        install_status = self._call(plan_id, "action_status", self.field.action_status, wave_ids)
        status_shown, status_dropped = _cap_dict(dict(install_status))
        per_hw = _round(wave_res["per_hw_rev"])
        payload = {
            "wave": wave,
            "window_minutes": mins,
            "start": round(start, 1),
            "end": round(end, 1),
            "affected_hw_revs": wave_res["affected_hw_revs"],
            "per_hw_rev": per_hw,
            "install_status": status_shown,
        }
        ev = self.ledger.new_evidence(plan_id, "observation", verdict, payload, wave=wave)
        phase = self._observe_phase(plan_id, plan, wave, verdict)
        out = {
            "evidence_id": ev.evidence_id,
            "wave": wave,
            "window_minutes": mins,
            "verdict": verdict,
            "affected_hw_revs": wave_res["affected_hw_revs"],
            "per_hw_rev": per_hw,
            "install_status": status_shown,
            "phase": phase,
            "next": self._observe_next(verdict, wave, len(plan.waves), phase, plan.from_version),
        }
        if status_dropped:
            out["install_status_truncated"] = status_dropped
        return out

    def _assess_hw_rev(
        self,
        plan: Plan,
        hw: str,
        wave_ids: list[str],
        inventory: list[Device],
        started_ids: set[str],
        failures: set[str],
        start: float,
        end: float,
    ) -> dict:
        pid = plan.plan_id
        updated = [
            Device(d, hw, plan.devices[d].get("region", "unknown"), plan.version)
            for d in wave_ids
            if plan.devices[d]["hw_rev"] == hw
        ]
        upd_raw = self._call(
            pid, "device_stats", self.metrics.device_stats, updated, plan.version, start, end
        )
        upd = [upd_raw.get(d.id) or _missing_stats(d) for d in updated]
        ctl_devices = [
            d
            for d in inventory
            if d.hw_rev == hw and d.id not in started_ids and d.version == plan.from_version
        ]
        control: list[DeviceStats] = []
        if ctl_devices:
            ctl_raw = self._call(
                pid, "device_stats", self.metrics.device_stats, ctl_devices, plan.from_version, start, end
            )
            control = [s for s in ctl_raw.values() if s.installed]
        control_type = "control"
        if not control:
            control_type = "baseline"
            pre = self._call(
                pid,
                "device_stats",
                self.metrics.device_stats,
                updated,
                plan.from_version,
                start - BASELINE_WINDOW_S,
                start,
            )
            control = [s for s in pre.values() if s.installed]
        mine = [d.id for d in updated if d.id in failures]
        return assess_hw_rev(hw, upd, control, control_type, mine, self.settings.thresholds)

    def _observe_phase(self, plan_id: str, plan: Plan, wave: int, verdict: str) -> str:
        """Advance the phase after an observation; HALTED (and anything else) is left alone."""
        current = self.ledger.get_plan(plan_id).phase
        if current not in ("WAVE_RUNNING", "WAVE_OBSERVED"):
            return current
        target = "COMPLETE" if verdict == "healthy" and wave == len(plan.waves) else "WAVE_OBSERVED"
        return self.ledger.set_phase(plan_id, target, allowed_from=("WAVE_RUNNING", "WAVE_OBSERVED")).phase

    @staticmethod
    def _observe_next(verdict: str, wave: int, n_waves: int, phase: str, from_version: str) -> str:
        if verdict == "regression":
            if phase == "HALTED":
                return (
                    f"rollback the affected cohort to {from_version} with this evidence_id (needs approval)"
                )
            return "show evidence card, then halt_rollout with this evidence_id (needs approval)"
        if phase == "HALTED":
            return "plan is HALTED; rollback needs regression evidence: observe_wave again or record_decision"
        if verdict == "inconclusive":
            return "call observe_wave again to extend the soak"
        if wave >= n_waves:
            return "rollout complete: write final report"
        return f"start_wave {wave + 1} with this evidence_id (needs approval)"

    # halt / rollback / verify ----------------------------------------------

    def halt_rollout(self, plan_id: str, evidence_id: str) -> dict:
        """Stop every started rollout of the plan (approval-gated)."""
        plan, _ev = check_halt(
            self.ledger, plan_id, evidence_id, self._now(), self.settings.evidence_max_age_s
        )
        self._approval(plan_id, "halt_rollout", evidence_id)
        started = self.ledger.started_waves(plan_id)
        rollout_ids = [p["rollout_id"] for _, p in sorted(started.items())]
        results = self._call(plan_id, "stop_rollouts", self.field.stop_rollouts, rollout_ids)
        self.ledger.add_event(
            plan_id, "action", {"kind": "halt", "results": results, "evidence_id": evidence_id}
        )
        phase = self.ledger.set_phase(plan_id, "HALTED", allowed_from=("WAVE_OBSERVED",)).phase
        return {
            "plan_id": plan_id,
            "stopped_rollouts": results,
            "phase": phase,
            "next": f"rollback the affected cohort to {plan.from_version} with the same evidence_id (needs approval)",
        }

    def rollback(
        self,
        plan_id: str,
        to_version: str,
        evidence_id: str,
        cohort: dict | None = None,
        targets: list[str] | None = None,
    ) -> dict:
        """Assign `to_version` (the plan's from_version) to the affected updated devices (approval-gated)."""
        plan, _ev, resolved = check_rollback(
            self.ledger,
            plan_id,
            cohort,
            targets,
            to_version,
            evidence_id,
            self._now(),
            self.settings.evidence_max_age_s,
        )
        self._approval(plan_id, "rollback", evidence_id)
        actions: list[dict] = []
        failed: list[dict] = []
        for device_id in resolved:
            try:
                res = self.field.assign(device_id, plan.from_version)
                actions.append({"device_id": device_id, "result": (res or {}).get("result", "assigned")})
            except Exception as exc:  # noqa: BLE001 - per-device failures are reported, not fatal
                failed.append({"device_id": device_id, "error": str(exc)})
        if not actions:
            message = f"assign failed for all {len(failed)} target(s): {failed[0]['error']}"
            self.ledger.add_event(
                plan_id, "error", {"tool": "rollback", "code": "BACKEND_ERROR", "message": message}
            )
            raise PreconditionError("BACKEND_ERROR", message)
        self.ledger.add_event(
            plan_id,
            "action",
            {
                "kind": "rollback",
                "device_ids": [a["device_id"] for a in actions],
                "to_version": plan.from_version,
                "results": actions,
                "failed": failed,
                "evidence_id": evidence_id,
            },
        )
        phase = self.ledger.set_phase(plan_id, "ROLLED_BACK", allowed_from=("HALTED",)).phase
        shown, dropped = _cap(actions)
        out = {
            "plan_id": plan_id,
            "to_version": plan.from_version,
            "targets": resolved[:LIST_CAP],
            "actions": shown,
            "failed": failed[:LIST_CAP],
            "phase": phase,
            "next": "verify_recovery",
        }
        if dropped or len(resolved) > LIST_CAP:
            out["truncated"] = max(dropped, len(resolved) - LIST_CAP)
        return out

    async def verify_recovery(
        self, plan_id: str, targets: list[str] | None = None, minutes: float | None = None
    ) -> dict:
        """Check the rolled-back devices run `from_version` healthily; VERIFIED only if all recovered."""
        s = self.settings
        want = self._default_minutes(minutes, s.observe_max_minutes)
        plan, chosen, mins = check_verify_recovery(self.ledger, plan_id, targets, want, s.observe_max_minutes)
        await self._sleep_chunks(mins * 60)
        rollbacks = [
            e for e in self.ledger.events(plan_id, types=["action"]) if e.payload.get("kind") == "rollback"
        ]
        rollback_ts = parse_ts(rollbacks[-1].ts).timestamp()
        now = self._now().timestamp()
        installed = self._call(plan_id, "installed_versions", self.field.installed_versions, chosen)
        devices = [
            Device(d, plan.devices[d]["hw_rev"], plan.devices[d].get("region", "unknown"), plan.from_version)
            for d in chosen
        ]
        stats = self._call(
            plan_id, "device_stats", self.metrics.device_stats, devices, plan.from_version, rollback_ts, now
        )
        per_device = {
            d.id: self._recovery_row(plan, d, installed.get(d.id), stats.get(d.id)) for d in devices
        }
        verdict = "recovered" if all(r["recovered"] for r in per_device.values()) else "not_recovered"
        shown, dropped = _cap_dict(per_device)
        payload = {"per_device": shown, "targets": chosen[:LIST_CAP], "window_minutes": mins}
        ev = self.ledger.new_evidence(plan_id, "verification", verdict, payload)
        phase = plan.phase
        if verdict == "recovered":
            phase = self.ledger.set_phase(plan_id, "VERIFIED", allowed_from=("ROLLED_BACK",)).phase
        out = {
            "evidence_id": ev.evidence_id,
            "verdict": verdict,
            "per_device": shown,
            "phase": phase,
            "next": (
                "rollout ended: write final report"
                if verdict == "recovered"
                else "not recovered: call verify_recovery again after a longer soak or investigate the devices"
            ),
        }
        if dropped:
            out["truncated"] = dropped
        return out

    @staticmethod
    def _recovery_row(plan: Plan, device: Device, installed: str | None, st: DeviceStats | None) -> dict:
        st = st or _missing_stats(device)
        recovered = (
            installed == plan.from_version
            and st.installed
            and st.restarts == 0
            and st.oom_kills == 0
            and (st.fps_median is None or st.fps_median > 0)
        )
        return {
            "installed_version": installed,
            "recovered": bool(recovered),
            "restarts": st.restarts,
            "oom_kills": st.oom_kills,
            "fps": None if st.fps_median is None else round(st.fps_median, 2),
        }

    # decisions -------------------------------------------------------------

    def record_decision(
        self, plan_id: str, decision: str, rationale: str, evidence_ids: list[str] | None = None
    ) -> dict:
        """Append a decision (with the evidence it rests on) to the ledger."""
        check_record_decision(self.ledger, plan_id, evidence_ids or [])
        if not str(decision).strip():
            message = "decision must not be empty"
            self.ledger.add_event(
                plan_id, "error", {"tool": "record_decision", "code": "BAD_ARGUMENT", "message": message}
            )
            raise PreconditionError("BAD_ARGUMENT", message)
        ev = self.ledger.add_event(
            plan_id,
            "decision",
            {"decision": decision, "rationale": rationale, "evidence_ids": list(evidence_ids or [])},
        )
        return {"ok": True, "event_id": ev.id}
