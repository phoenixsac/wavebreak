"""SQLite ledger (docs/agent-design.md section 11): rollout plans plus an append-only event log."""

from __future__ import annotations

import json
import secrets
import sqlite3
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .errors import PreconditionError

PHASES = (
    "PLANNED",
    "REHEARSED",
    "BLOCKED",
    "WAVE_RUNNING",
    "WAVE_OBSERVED",
    "COMPLETE",
    "HALTED",
    "ROLLED_BACK",
    "VERIFIED",
)
TERMINAL_PHASES = ("BLOCKED", "COMPLETE", "VERIFIED")
SUPERSEDABLE_PHASES = ("PLANNED", "REHEARSED")  # nothing started in the field; a new plan replaces it
EVENT_TYPES = ("rehearsal", "observation", "decision", "approval", "action", "verification", "error")
EVIDENCE_KINDS = ("rehearsal", "observation", "verification")

SCHEMA = """
CREATE TABLE IF NOT EXISTS rollouts (
  plan_id TEXT PRIMARY KEY,
  version TEXT NOT NULL,
  from_version TEXT NOT NULL,
  waves_json TEXT NOT NULL,
  phase TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  plan_id TEXT NOT NULL,
  ts TEXT NOT NULL,
  type TEXT NOT NULL,
  wave INTEGER,
  evidence_id TEXT,
  payload_json TEXT NOT NULL
);
"""


def parse_ts(ts: str) -> datetime:
    """Parse a ledger timestamp into an aware UTC datetime."""
    return datetime.fromisoformat(ts).astimezone(UTC)


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class Plan:
    """A rollout plan. `waves[0]` is wave 1; `devices` maps id -> {hw_rev, region}."""

    plan_id: str
    version: str
    from_version: str
    waves: list[list[str]]
    devices: dict[str, dict]
    phase: str
    created_at: str
    updated_at: str


@dataclass
class Event:
    """One ledger event; `ts` is an ISO-8601 UTC string."""

    id: int
    plan_id: str
    ts: str
    type: str
    wave: int | None
    evidence_id: str | None
    payload: dict = field(default_factory=dict)


def _plan_from_row(row: sqlite3.Row) -> Plan:
    blob = json.loads(row["waves_json"])
    return Plan(
        plan_id=row["plan_id"],
        version=row["version"],
        from_version=row["from_version"],
        waves=[list(w) for w in blob["waves"]],
        devices=blob["devices"],
        phase=row["phase"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _event_from_row(row: sqlite3.Row) -> Event:
    return Event(
        id=row["id"],
        plan_id=row["plan_id"],
        ts=row["ts"],
        type=row["type"],
        wave=row["wave"],
        evidence_id=row["evidence_id"],
        payload=json.loads(row["payload_json"]),
    )


class Ledger:
    """Ledger facade; a fresh SQLite connection is opened per call."""

    def __init__(self, path: str | Path, clock: Callable[[], datetime] | None = None) -> None:
        self.path = Path(path)
        self._clock = clock or _utcnow
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=10)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
        finally:
            conn.close()

    def _now(self) -> str:
        return self._clock().astimezone(UTC).isoformat()

    @contextmanager
    def _conn(self, write: bool = False) -> Iterator[sqlite3.Connection]:
        """Open a connection; commit on success. `write=True` takes the write lock up front."""
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        finally:
            conn.close()

    # plans -----------------------------------------------------------------

    def create_plan(
        self, version: str, from_version: str, waves: list[list[str]], devices: dict[str, dict]
    ) -> str:
        """Create a PLANNED plan; PLANNED/REHEARSED plans are superseded, other non-terminal plans refuse."""
        blob = json.dumps({"waves": waves, "devices": devices}, sort_keys=True)
        marks = ",".join("?" * len(TERMINAL_PHASES))
        with self._conn(write=True) as c:
            for row in c.execute(
                f"SELECT plan_id, phase FROM rollouts WHERE phase NOT IN ({marks})", TERMINAL_PHASES
            ).fetchall():
                if row["phase"] in SUPERSEDABLE_PHASES:  # no field impact yet: replace it
                    c.execute(
                        "UPDATE rollouts SET phase='BLOCKED', updated_at=? WHERE plan_id=?",
                        (self._now(), row["plan_id"]),
                    )
                    self._insert_event(c, row["plan_id"], "action", {"kind": "superseded"}, None, None)
                    continue
                raise PreconditionError(
                    "ACTIVE_PLAN_EXISTS", f"plan {row['plan_id']} is still active (phase {row['phase']})"
                )
            now = self._now()
            plan_id = self._new_plan_id(c)
            c.execute(
                "INSERT INTO rollouts VALUES (?,?,?,?,?,?,?)",
                (plan_id, version, from_version, blob, "PLANNED", now, now),
            )
            self._insert_event(c, plan_id, "action", {"kind": "plan_created"}, None, None)
        return plan_id

    def _new_plan_id(self, c: sqlite3.Connection) -> str:
        day = self._clock().astimezone(UTC).strftime("%Y%m%d")
        while True:
            plan_id = f"plan-{day}-{secrets.token_hex(2)}"
            if not c.execute("SELECT 1 FROM rollouts WHERE plan_id=?", (plan_id,)).fetchone():
                return plan_id

    def get_plan(self, plan_id: str) -> Plan:
        """Return the plan or raise PLAN_NOT_FOUND."""
        with self._conn() as c:
            row = c.execute("SELECT * FROM rollouts WHERE plan_id=?", (plan_id,)).fetchone()
        if row is None:
            raise PreconditionError("PLAN_NOT_FOUND", f"no plan {plan_id!r}")
        return _plan_from_row(row)

    def latest_plan(self) -> Plan | None:
        """Most recently created plan, if any."""
        with self._conn() as c:
            row = c.execute("SELECT * FROM rollouts ORDER BY created_at DESC, rowid DESC LIMIT 1").fetchone()
        return _plan_from_row(row) if row else None

    def active_plan(self) -> Plan | None:
        """The non-terminal plan, if any."""
        marks = ",".join("?" * len(TERMINAL_PHASES))
        with self._conn() as c:
            row = c.execute(
                f"SELECT * FROM rollouts WHERE phase NOT IN ({marks}) "
                "ORDER BY created_at DESC, rowid DESC LIMIT 1",
                TERMINAL_PHASES,
            ).fetchone()
        return _plan_from_row(row) if row else None

    def set_phase(self, plan_id: str, to: str, allowed_from: Iterable[str] | None = None) -> Plan:
        """Move the plan to phase `to`; BAD_PHASE if the current phase is not in `allowed_from`."""
        if to not in PHASES:
            raise ValueError(f"unknown phase {to!r}")
        allowed = tuple(allowed_from) if allowed_from is not None else None
        with self._conn(write=True) as c:
            row = c.execute("SELECT * FROM rollouts WHERE plan_id=?", (plan_id,)).fetchone()
            if row is None:
                raise PreconditionError("PLAN_NOT_FOUND", f"no plan {plan_id!r}")
            if allowed is not None and row["phase"] not in allowed:
                raise PreconditionError(
                    "BAD_PHASE", f"plan is in phase {row['phase']}, expected one of {', '.join(allowed)}"
                )
            c.execute("UPDATE rollouts SET phase=?, updated_at=? WHERE plan_id=?", (to, self._now(), plan_id))
            row = c.execute("SELECT * FROM rollouts WHERE plan_id=?", (plan_id,)).fetchone()
        return _plan_from_row(row)

    # events ----------------------------------------------------------------

    def _insert_event(
        self,
        c: sqlite3.Connection,
        plan_id: str,
        type_: str,
        payload: dict,
        wave: int | None,
        evidence_id: str | None,
    ) -> Event:
        if type_ not in EVENT_TYPES:
            raise ValueError(f"unknown event type {type_!r}")
        ts = self._now()
        cur = c.execute(
            "INSERT INTO events (plan_id, ts, type, wave, evidence_id, payload_json) VALUES (?,?,?,?,?,?)",
            (plan_id, ts, type_, wave, evidence_id, json.dumps(payload, sort_keys=True)),
        )
        return Event(cur.lastrowid, plan_id, ts, type_, wave, evidence_id, dict(payload))

    def add_event(
        self,
        plan_id: str,
        type: str,
        payload: dict,
        wave: int | None = None,
        evidence_id: str | None = None,
    ) -> Event:
        """Append an event."""
        with self._conn(write=True) as c:
            return self._insert_event(c, plan_id, type, payload, wave, evidence_id)

    def new_evidence(
        self, plan_id: str, kind: str, verdict: str, payload: dict, wave: int | None = None
    ) -> Event:
        """Append an evidence event (`ev-<8 hex>`); `verdict` is stored in the payload."""
        if kind not in EVIDENCE_KINDS:
            raise ValueError(f"unknown evidence kind {kind!r}")
        body = {**payload, "verdict": verdict}
        with self._conn(write=True) as c:
            while True:
                evidence_id = f"ev-{secrets.token_hex(4)}"
                if not c.execute("SELECT 1 FROM events WHERE evidence_id=?", (evidence_id,)).fetchone():
                    break
            return self._insert_event(c, plan_id, kind, body, wave, evidence_id)

    def get_evidence(self, evidence_id: str) -> Event | None:
        """Evidence event by id, or None."""
        with self._conn() as c:
            row = c.execute("SELECT * FROM events WHERE evidence_id=?", (evidence_id,)).fetchone()
        return _event_from_row(row) if row else None

    def latest_evidence(self, plan_id: str, kind: str, wave: int | None = None) -> Event | None:
        """Newest evidence of `kind` for the plan (optionally for one wave)."""
        sql = "SELECT * FROM events WHERE plan_id=? AND type=? AND evidence_id IS NOT NULL"
        args: list = [plan_id, kind]
        if wave is not None:
            sql += " AND wave=?"
            args.append(wave)
        with self._conn() as c:
            row = c.execute(sql + " ORDER BY id DESC LIMIT 1", args).fetchone()
        return _event_from_row(row) if row else None

    def events(
        self, plan_id: str, types: Iterable[str] | None = None, limit: int | None = None
    ) -> list[Event]:
        """Events in chronological order; with `limit`, the last n."""
        sql = "SELECT * FROM events WHERE plan_id=?"
        args: list = [plan_id]
        if types is not None:
            tl = list(types)
            sql += f" AND type IN ({','.join('?' * len(tl))})"
            args += tl
        sql += " ORDER BY id DESC"
        if limit is not None:
            sql += " LIMIT ?"
            args.append(int(limit))
        with self._conn() as c:
            rows = c.execute(sql, args).fetchall()
        return [_event_from_row(r) for r in reversed(rows)]

    def _actions(self, plan_id: str, kind: str) -> list[Event]:
        return [e for e in self.events(plan_id, types=["action"]) if e.payload.get("kind") == kind]

    def started_waves(self, plan_id: str) -> dict[int, dict]:
        """Wave number -> `wave_started` action payload (rollout_id, device_ids, started_at)."""
        return {e.wave: e.payload for e in self._actions(plan_id, "wave_started") if e.wave is not None}

    def rolled_back_devices(self, plan_id: str) -> set[str]:
        """Device ids covered by `rollback` action events."""
        out: set[str] = set()
        for e in self._actions(plan_id, "rollback"):
            out.update(e.payload.get("device_ids", []))
        return out
