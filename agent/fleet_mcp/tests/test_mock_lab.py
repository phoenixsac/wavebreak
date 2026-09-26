"""MockLab scenarios on a simulated clock."""

from __future__ import annotations

import pytest

from agent.fleet_mcp.assess import slope_mb_per_min
from agent.fleet_mcp.mock_lab import MB, MockLab, parse_scenarios


class Clock:
    def __init__(self, t=10_000.0):
        self.t = t

    def __call__(self):
        return self.t


def installed(scenarios, version="v1.2", hw="A"):
    clock = Clock()
    lab = MockLab(scenarios, clock)
    did = lab.create_device(hw, "v1.1")
    res = lab.install(did, version)
    return lab, clock, did, res


def at(lab, clock, did, elapsed, t0=10_000.0):
    clock.t = t0 + elapsed
    return lab.summary(did)


def test_ids_and_now():
    clock = Clock(5.0)
    lab = MockLab(clock=clock)
    assert lab.create_device("A", "v1.1") == "mock-lab-001"
    assert lab.create_device("B", "v1.1") == "mock-lab-002"
    assert lab.now() == 5.0


def test_before_install_is_healthy_at_create_version():
    clock = Clock()
    lab = MockLab({"v1.1": "leak"}, clock)
    did = lab.create_device("A", "v1.1")
    clock.t += 600
    s = lab.summary(did)
    assert (s["fw_version"], s["restarts"], s["oom_kills"], s["unit_state"]) == ("v1.1", 0, 0, "active")
    assert s["last_install"]["result"] == "none"


def test_healthy():
    lab, clock, did, res = installed({})
    assert res == {"device_id": did, "version": "v1.2", "result": "success"}
    s = at(lab, clock, did, 300)
    assert s["fw_version"] == "v1.2" and s["unit_state"] == "active"
    assert s["restarts"] == 0 and s["oom_kills"] == 0
    assert len(s["memory_samples"]) == 21
    assert {m["bytes"] for m in s["memory_samples"]} == {200 * MB}
    assert s["memory_samples"][0]["t"] == 10_000.0 and s["memory_samples"][1]["t"] == 10_015.0
    assert s["last_install"]["result"] == "success"


def test_leak_flat_before_180s_then_grows_then_oom():
    lab, clock, did, _ = installed({"v1.2": "leak"})
    s = at(lab, clock, did, 180)
    assert {m["bytes"] for m in s["memory_samples"]} == {200 * MB}
    assert s["restarts"] == 0 and s["oom_kills"] == 0
    s = at(lab, clock, did, 239)
    assert s["oom_kills"] == 0 and s["memory_samples"][-1]["bytes"] > 200 * MB
    s = at(lab, clock, did, 240)
    assert (s["oom_kills"], s["restarts"], s["unit_state"]) == (1, 1, "active")
    assert at(lab, clock, did, 299)["oom_kills"] == 1
    assert at(lab, clock, did, 300)["oom_kills"] == 2
    s = at(lab, clock, did, 360)
    assert s["oom_kills"] == s["restarts"] == 3
    last = s["memory_samples"][-1]
    assert last["bytes"] == 200 * MB + 30 * MB
    tail = [(m["t"], m["bytes"]) for m in s["memory_samples"] if m["t"] >= 10_180]
    assert slope_mb_per_min(tail) == pytest.approx(10.0)
    assert isinstance(last["bytes"], int)


def test_crash_restarts_and_unit_state():
    lab, clock, did, _ = installed({"v1.2": "crash"})
    s = at(lab, clock, did, 44)
    assert (s["restarts"], s["unit_state"]) == (0, "active")
    s = at(lab, clock, did, 45)
    assert (s["restarts"], s["unit_state"]) == (1, "failed")
    assert at(lab, clock, did, 65)["restarts"] == 2
    assert at(lab, clock, did, 105)["restarts"] == 4
    assert at(lab, clock, did, 105)["oom_kills"] == 0


def test_install_fail():
    lab, clock, did, res = installed({"v1.2": "install_fail"})
    assert res["result"] == "failure"
    s = at(lab, clock, did, 120)
    assert s["last_install"]["result"] == "failure" and s["last_install"]["detail"]
    assert s["unit_state"] == "active" and s["restarts"] == 0 and s["fw_version"] == "v1.1"


def test_scenario_lookup_order():
    lab = MockLab({"v1.2": "crash", "v1.2:B": "leak", "v1.3": "install_fail"})
    assert lab.scenario_for("v1.2", "B") == "leak"
    assert lab.scenario_for("v1.2", "A") == "crash"
    assert lab.scenario_for("v1.3", "B") == "install_fail"
    assert lab.scenario_for("v1.4", "B") == "healthy"
    lab2, clock, did, _ = installed({"v1.2": "crash", "v1.2:B": "leak"}, hw="B")
    assert at(lab2, clock, did, 250)["oom_kills"] == 1


def test_reinstall_restarts_timeline():
    lab, clock, did, _ = installed({"v1.2": "crash"})
    at(lab, clock, did, 100)
    lab.install(did, "v1.1")
    assert lab.summary(did)["restarts"] == 0


def test_parse_scenarios():
    assert parse_scenarios(None) == {} and parse_scenarios("  ") == {}
    assert parse_scenarios('{"v1.3":"crash","v1.2:B":"leak"}') == {"v1.3": "crash", "v1.2:B": "leak"}
    with pytest.raises(TypeError):
        parse_scenarios("[1]")
    with pytest.raises(ValueError):
        parse_scenarios('{"v1":"bogus"}')
    with pytest.raises(ValueError):
        MockLab({"v1": "bogus"})


def test_destroy_and_unknown_ids():
    lab = MockLab()
    did = lab.create_device("A", "v1.1")
    lab.destroy(did)
    for call in (lab.destroy, lab.summary):
        with pytest.raises(KeyError):
            call(did)
    with pytest.raises(KeyError):
        lab.install(did, "v1.2")
    assert lab.create_device("A", "v1.1") == "mock-lab-002"
