"""Shared fixtures: fake clock, ledger, and a plan builder."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from agent.fleet_mcp.ledger import Ledger


class FakeClock:
    """Callable clock returning an aware UTC datetime that only moves via `advance`."""

    def __init__(self) -> None:
        self.now = datetime(2026, 9, 26, 12, 0, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


DEVICES = {
    "d1": {"hw_rev": "A", "region": "eu"},
    "d2": {"hw_rev": "B", "region": "eu"},
    "d3": {"hw_rev": "A", "region": "us"},
    "d4": {"hw_rev": "B", "region": "us"},
    "d5": {"hw_rev": "A", "region": "eu"},
}
WAVES = [["d1", "d2"], ["d3", "d4", "d5"]]


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def ledger(tmp_path, clock) -> Ledger:
    return Ledger(tmp_path / "sub" / "ledger.sqlite", clock=clock)


@pytest.fixture
def plan_id(ledger) -> str:
    return ledger.create_plan("v1.2", "v1.1", [list(w) for w in WAVES], dict(DEVICES))
