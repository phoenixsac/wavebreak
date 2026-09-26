"""HawkbitBackend against a fake HawkbitClient."""

from __future__ import annotations

import pytest

from agent.fleet_mcp import backends
from agent.fleet_mcp.backends import HawkbitBackend
from agent.fleet_mcp.models import Device
from wavebreak_clients._http import HttpError


class FakeClient:
    api = "http://hb/rest/v1"
    auth = ("u", "p")
    timeout = 7

    def __init__(self, statuses=None):
        self.targets = [{"controllerId": "d2"}, {"controllerId": "d1"}, {"controllerId": "d3"}]
        self.attrs = {"d1": {"hw_rev": "A", "region": "eu"}, "d2": {"hw_rev": "B"}, "d3": {}}
        self.installed = {"d1": {"version": "v1.1"}, "d2": {}}  # d3 -> 404
        self.statuses = list(statuses or ["ready"])
        self.calls = []
        self.stop_errors = set()
        self.rollout_status = {}

    def list_targets(self, query=None, *, limit=500):
        self.calls.append(("list_targets", limit))
        return self.targets

    def target_attributes(self, tid):
        return self.attrs[tid]

    def installed_distribution_set(self, tid):
        if tid not in self.installed:
            raise HttpError(404, "u", "nope")
        return self.installed[tid]

    def distribution_set_id(self, name, version):
        if version == "v9":
            raise LookupError("no such ds")
        return 42

    def create_wave(self, **kw):
        self.calls.append(("create_wave", kw))
        return {"id": 7}

    def rollout(self, rid):
        if rid in self.rollout_status:
            return {"status": self.rollout_status[rid]}
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        return {"status": status}

    def start_rollout(self, rid):
        self.calls.append(("start", rid))

    def stop_rollout(self, rid):
        if rid in self.stop_errors:
            raise RuntimeError("boom")
        self.calls.append(("stop", rid))

    def assign_distribution_set(self, tid, ds_id):
        self.calls.append(("assign", tid, ds_id))


def make(client=None, **kw):
    sleeps = []
    return HawkbitBackend(client or FakeClient(), sleep=sleeps.append, **kw), sleeps


def test_inventory_sorted_defaults_and_404():
    b, _ = make()
    assert b.inventory() == [
        Device("d1", "A", "eu", "v1.1"),
        Device("d2", "B", "unknown", "none"),
        Device("d3", "unknown", "unknown", "none"),
    ]


def test_start_wave_polls_until_ready_then_starts():
    client = FakeClient(["creating", "creating", "ready"])
    b, sleeps = make(client)
    out = b.start_wave("w1", "v1.2", ["d1", "d2"])
    assert out == {"rollout_id": 7, "targets": ["d1", "d2"]}
    assert sleeps == [1, 1]
    create = next(c for c in client.calls if c[0] == "create_wave")[1]
    assert create["target_filter_query"] == "controllerId=in=(d1,d2)"
    assert create["distribution_set_id"] == 42
    assert client.calls[-1] == ("start", 7)


def test_start_wave_unknown_version_lookup_error():
    b, _ = make()
    with pytest.raises(LookupError):
        b.start_wave("w", "v9", ["d1"])


@pytest.mark.parametrize("status", ["deleted", "finished"])
def test_start_wave_dead_rollout(status):
    client = FakeClient(["creating", status])
    b, _ = make(client)
    with pytest.raises(RuntimeError, match=status):
        b.start_wave("w", "v1.2", ["d1"])
    assert ("start", 7) not in client.calls


def test_start_wave_timeout_does_not_start():
    client = FakeClient(["creating"])
    b, sleeps = make(client, ready_timeout_s=3)
    with pytest.raises(RuntimeError, match="not ready"):
        b.start_wave("w", "v1.2", ["d1"])
    assert sleeps == [1, 1, 1]
    assert ("start", 7) not in client.calls


def test_start_wave_rejects_unsafe_ids():
    b, _ = make()
    with pytest.raises(ValueError):
        b.start_wave("w", "v1.2", ["a,b"])


def test_stop_rollouts_statuses_skip_and_error():
    client = FakeClient()
    client.rollout_status = {1: "running", 2: "finished", 3: "paused", 4: "ready"}
    client.stop_errors = {4}
    b, _ = make(client)
    assert b.stop_rollouts([1, 2, 3, 4]) == [
        {"rollout_id": 1, "result": "stopped"},
        {"rollout_id": 2, "result": "skipped: finished"},
        {"rollout_id": 3, "result": "stopped"},
        {"rollout_id": 4, "result": "error: boom"},
    ]
    assert ("stop", 2) not in client.calls


def test_stop_rollouts_lookup_error_is_reported():
    client = FakeClient()
    b, _ = make(client)
    client.rollout = lambda rid: (_ for _ in ()).throw(HttpError(404, "u", "gone"))
    (res,) = b.stop_rollouts([5])
    assert res["result"].startswith("error:")


def test_assign_and_installed_versions():
    client = FakeClient()
    b, _ = make(client)
    assert b.assign("d1", "v1.1") == {"device_id": "d1", "version": "v1.1", "result": "assigned"}
    assert ("assign", "d1", 42) in client.calls
    assert b.installed_versions(["d1", "d2", "d3"]) == {"d1": "v1.1", "d2": None, "d3": None}
    with pytest.raises(LookupError):
        b.assign("d1", "v9")


def _patch_actions(monkeypatch, actions):
    seen = []

    def fake(method, url, **kw):
        seen.append((method, url, kw))
        did = url.split("/targets/")[1].split("/")[0]
        return {"content": actions.get(did, [])}

    monkeypatch.setattr(backends, "request_json", fake)
    return seen


def test_action_status_and_failures(monkeypatch):
    actions = {
        "d1": [{"id": 3, "status": "running", "createdAt": 2_000_000}, {"id": 2, "status": "finished"}],
        "d2": [{"id": 5, "status": "finished", "detailStatus": "error", "createdAt": 3_000_000}],
        "d3": [{"id": 6, "status": "error", "createdAt": 500_000}],  # too old
        "d4": [],
    }
    seen = _patch_actions(monkeypatch, actions)
    b, _ = make()
    assert b.action_status(["d1", "d4"]) == {"d1": "running", "d4": "none"}
    assert b.install_failures(["d1", "d2", "d3", "d4"], 1000.0) == ["d2"]
    method, url, kw = seen[0]
    assert (method, url) == ("GET", "http://hb/rest/v1/targets/d1/actions")
    assert kw["params"] == {"sort": "id:DESC", "limit": 5}
    assert kw["auth"] == ("u", "p") and kw["timeout"] == 7


def test_failure_case_insensitive(monkeypatch):
    _patch_actions(monkeypatch, {"d1": [{"status": "ERROR", "createdAt": 10_000}]})
    b, _ = make()
    assert b.install_failures(["d1"], 10.0) == ["d1"]
    assert b.install_failures(["d1"], 10.001) == []
