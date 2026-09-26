"""Stable refusal errors for the fleet MCP server (docs/agent-design.md section 10.1)."""

from __future__ import annotations

CODES = (
    "PLAN_NOT_FOUND",
    "BAD_PHASE",
    "EVIDENCE_MISSING",
    "EVIDENCE_WRONG_PLAN",
    "EVIDENCE_WRONG_KIND",
    "EVIDENCE_WRONG_WAVE",
    "EVIDENCE_STALE",
    "EVIDENCE_SUPERSEDED",
    "EVIDENCE_VERDICT",
    "EVIDENCE_WRONG_VERSION",
    "WAVE_ORDER",
    "WRONG_TARGET_VERSION",
    "TARGET_NOT_ELIGIBLE",
    "ACTIVE_PLAN_EXISTS",
    "UNKNOWN_VERSION",
    "BAD_ARGUMENT",
)


class PreconditionError(Exception):
    """A tool call was refused; `code` is stable and machine-readable."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message)
        self.code = code
        self.message = message

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"
