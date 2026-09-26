"""HttpLab with request_json monkeypatched."""

from __future__ import annotations

import pytest

from agent.fleet_mcp import backends
from agent.fleet_mcp.backends import HttpLab
from wavebreak_clients._http import HttpError


class Recorder:
    def __init__(self, responses):
        self.responses, self.calls = responses, []

    def __call__(self, method, url, **kw):
        self.calls.append((method, url, kw))
        r = self.responses[(method, url.split("://")[1].split("/", 1)[1])]
        if isinstance(r, Exception):
            raise r
        return r


def lab(monkeypatch, responses):
    rec = Recorder(responses)
    monkeypatch.setattr(backends, "request_json", rec)
    return HttpLab("http://lab:8090/", "tok", timeout=5), rec


@pytest.mark.parametrize(
    "resp",
    [
        {"devices": [{"id": "lab-1", "hw_rev": "A", "fw_version": "v1.1"}]},
        {"id": "lab-1"},
        {"device_id": "lab-1"},
        [{"id": "lab-1"}],
        ["lab-1"],
    ],
)
def test_create_variants(monkeypatch, resp):
    hl, rec = lab(monkeypatch, {("POST", "lab/devices"): resp})
    assert hl.create_device("A", "v1.1") == "lab-1"
    method, url, kw = rec.calls[0]
    assert (method, url) == ("POST", "http://lab:8090/lab/devices")
    assert kw["json_body"] == {"count": 1, "hw_rev": "A", "version": "v1.1"}
    assert kw["bearer"] == "tok" and kw["timeout"] == 5


def test_create_without_id_raises(monkeypatch):
    hl, _ = lab(monkeypatch, {("POST", "lab/devices"): {"devices": []}})
    with pytest.raises(RuntimeError):
        hl.create_device("A", "v1.1")


def test_install_success(monkeypatch):
    resp = {"ok": True, "id": "lab-1", "fw_version": "v1.2", "install": {"x": 1}}
    hl, rec = lab(monkeypatch, {("POST", "lab/devices/lab-1/install"): resp})
    out = hl.install("lab-1", "v1.2")
    assert out["result"] == "success" and out["device_id"] == "lab-1" and out["version"] == "v1.2"
    assert out["install"] == {"x": 1}
    assert rec.calls[0][2]["json_body"] == {"version": "v1.2"}


def test_install_422_is_failure_result(monkeypatch):
    err = HttpError(422, "u", "checksum mismatch")
    hl, _ = lab(monkeypatch, {("POST", "lab/devices/lab-1/install"): err})
    assert hl.install("lab-1", "v1.2") == {
        "device_id": "lab-1",
        "version": "v1.2",
        "result": "failure",
        "detail": "checksum mismatch",
    }


def test_install_other_http_error_propagates(monkeypatch):
    hl, _ = lab(monkeypatch, {("POST", "lab/devices/lab-1/install"): HttpError(500, "u", "x")})
    with pytest.raises(HttpError):
        hl.install("lab-1", "v1.2")


def test_summary_real_shape(monkeypatch):
    raw = {
        "id": "lab-1",
        "hw_rev": "A",
        "fw_version": "v1.2",
        "restarts": 2,
        "oom_kills": 1,
        "unit_state": "active",
        "container_state": "running",
        "memory_bytes": 5.0,
        "memory_trend": [
            {"ts": "1970-01-01T00:00:10Z", "memory_bytes": 1000.0},
            {"ts": "1970-01-01T00:00:25+00:00", "memory_bytes": None},
            {"ts": "1970-01-01T00:00:40", "memory_bytes": 2048.7},
        ],
        "last_install_result": {"ok": False, "version": "v1.2", "message": "bad sig"},
    }
    hl, rec = lab(monkeypatch, {("GET", "lab/devices/lab-1/summary"): raw})
    assert hl.summary("lab-1") == {
        "device_id": "lab-1",
        "fw_version": "v1.2",
        "unit_state": "active",
        "restarts": 2,
        "oom_kills": 1,
        "memory_samples": [{"t": 10.0, "bytes": 1000}, {"t": 40.0, "bytes": 2048}],
        "last_install": {"result": "failure", "detail": "bad sig"},
    }
    assert rec.calls[0][2]["bearer"] == "tok"


def test_summary_nulls_and_defaults(monkeypatch):
    raw = {
        "id": "lab-1",
        "fw_version": "v1.1",
        "unit_state": None,
        "last_install_result": None,
        "memory_trend": [],
    }
    hl, _ = lab(monkeypatch, {("GET", "lab/devices/lab-1/summary"): raw})
    s = hl.summary("lab-1")
    assert s["unit_state"] == "unknown" and s["restarts"] == 0 and s["oom_kills"] == 0
    assert s["last_install"] == {"result": None, "detail": ""} and s["memory_samples"] == []


def test_summary_ok_true_and_older_spellings(monkeypatch):
    raw = {
        "version": "v1.2",
        "unit": "failed",
        "oom": 3,
        "restarts": 3,
        "memory": [[100, 4096], {"t": 115, "bytes": 8192}, "garbage"],
        "last_install": {"result": "success", "detail": "fine"},
    }
    hl, _ = lab(monkeypatch, {("GET", "lab/devices/lab-1/summary"): raw})
    s = hl.summary("lab-1")
    assert s["oom_kills"] == 3 and s["unit_state"] == "failed" and s["fw_version"] == "v1.2"
    assert s["memory_samples"] == [{"t": 100.0, "bytes": 4096}, {"t": 115.0, "bytes": 8192}]
    assert s["last_install"] == {"result": "success", "detail": "fine"}
    hl2, _ = lab(
        monkeypatch,
        {("GET", "lab/devices/lab-1/summary"): {"last_install_result": {"ok": True, "message": "m"}}},
    )
    assert hl2.summary("lab-1")["last_install"] == {"result": "success", "detail": "m"}


def test_destroy_and_now(monkeypatch):
    hl, rec = lab(monkeypatch, {("DELETE", "lab/devices/lab-1"): {"status": "deleted"}})
    assert hl.destroy("lab-1") is None
    assert rec.calls[0][:2] == ("DELETE", "http://lab:8090/lab/devices/lab-1")
    monkeypatch.setattr(backends.time, "time", lambda: 123.0)
    assert hl.now() == 123.0
