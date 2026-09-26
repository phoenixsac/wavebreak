"""PromBackend against fake Prometheus / Loki."""

from __future__ import annotations

import pytest

from agent.fleet_mcp.backends import MetricsUnavailable, PromBackend, _counter_increase
from agent.fleet_mcp.models import Device

MB = 1024 * 1024
A = Device("d1", "A", "eu", "v1.1")
B = Device("d2", "B", "eu", "v1.1")


def series(did, values, t0=1000.0, step=15.0):
    return {"metric": {"device_id": did}, "values": [[t0 + i * step, str(v)] for i, v in enumerate(values)]}


class FakeProm:
    def __init__(self, data):
        self.data = data  # metric name -> matrix result
        self.queries = []

    def query_range(self, promql, start, end, step="15s"):
        self.queries.append((promql, start, end, step))
        return self.data.get(promql.split("{")[0], [])


class FakeLoki:
    def __init__(self, result=None, error=None):
        self.result, self.error, self.queries = result or [], error, []

    def query(self, logql, time=None, limit=100):
        self.queries.append((logql, time))
        if self.error:
            raise self.error
        return self.result


def leak_data():
    return {
        "wavebreak_app_cgroup_memory_bytes": [series("d1", [(200 + 10 * i) * MB for i in range(9)])],
        "wavebreak_app_restarts_total": [series("d1", [0, 0, 1, 1, 1, 2, 2, 2, 2])],
        "wavebreak_app_fps": [series("d1", [10, 30, 20])],
    }


def test_stats_slope_restarts_fps_oom():
    prom, loki = FakeProm(leak_data()), FakeLoki([{"metric": {"device_id": "d1"}, "value": [0, "2"]}])
    out = PromBackend(prom, loki).device_stats([A, B], "v1.2", 1000.0, 1120.0)
    s = out["d1"]
    assert s.installed and s.samples == 9 and s.window_s == 120.0
    assert s.mem_slope_mb_per_min == pytest.approx(40.0)  # 10 MB per 15 s
    assert s.restarts == 2 and s.oom_kills == 2 and s.fps_median == 20 and s.hw_rev == "A"
    assert len(prom.queries) == 3 and len(loki.queries) == 1
    assert prom.queries[0][3] == "15s"


def test_missing_device_not_installed():
    out = PromBackend(FakeProm(leak_data()), FakeLoki()).device_stats([A, B], "v1.2", 0, 100)
    assert out["d2"].__dict__ == {
        "device_id": "d2",
        "hw_rev": "B",
        "installed": False,
        "samples": 0,
        "window_s": 0,
        "mem_slope_mb_per_min": None,
        "restarts": 0,
        "oom_kills": 0,
        "fps_median": None,
    }


def test_no_devices_no_queries():
    prom = FakeProm({})
    assert PromBackend(prom, FakeLoki()).device_stats([], "v1", 0, 1) == {}
    assert prom.queries == []


def test_counter_reset_is_not_a_restart():
    assert _counter_increase([(0, 0), (1, 1), (2, 1), (3, 0), (4, 1)]) == 2
    assert _counter_increase([(0, 5), (1, 0)]) == 0
    assert _counter_increase([(0, 5)]) == 0


def test_restarts_use_increments_after_reset():
    data = leak_data()
    data["wavebreak_app_restarts_total"] = [series("d1", [3, 4, 0, 1])]
    out = PromBackend(FakeProm(data), FakeLoki()).device_stats([A], "v1.2", 1000, 1100)
    assert out["d1"].restarts == 2


def test_loki_error_is_metrics_unavailable():
    backend = PromBackend(FakeProm(leak_data()), FakeLoki(error=RuntimeError("down")))
    with pytest.raises(MetricsUnavailable) as exc:
        backend.device_stats([A], "v1.2", 1000, 1120)
    assert exc.value.source == "loki" and "down" in exc.value.detail
    assert str(exc.value).startswith("loki unavailable: ")


def test_prometheus_error_is_metrics_unavailable():
    class BrokenProm(FakeProm):
        def query_range(self, promql, start, end, step="15s"):
            raise TimeoutError("timed out")

    with pytest.raises(MetricsUnavailable) as exc:
        PromBackend(BrokenProm({}), FakeLoki()).device_stats([A], "v1.2", 1000, 1120)
    assert exc.value.source == "prometheus" and "timed out" in exc.value.detail


def test_loki_empty_result_means_zero_oom():
    out = PromBackend(FakeProm(leak_data()), FakeLoki(result=[])).device_stats([A], "v1.2", 1000, 1120)
    assert out["d1"].oom_kills == 0 and out["d1"].samples == 9


def test_flat_memory_slope_zero_and_no_fps():
    data = {"wavebreak_app_cgroup_memory_bytes": [series("d1", [200 * MB] * 6)]}
    s = PromBackend(FakeProm(data), FakeLoki()).device_stats([A], "v1", 1000, 1075)["d1"]
    assert s.mem_slope_mb_per_min == pytest.approx(0.0) and s.fps_median is None


def test_query_escaping_and_loki_window():
    tricky = Device('a.b|c"', "A", "eu", "v1")
    prom, loki = FakeProm({}), FakeLoki()
    PromBackend(prom, loki).device_stats([tricky, A], 'v"1', 1000.0, 1240.9)
    q = prom.queries[0][0]
    assert q == ('wavebreak_app_cgroup_memory_bytes{device_id=~"a\\\\.b\\\\|c\\"|d1",fw_version="v\\"1"}')
    logql, at = loki.queries[0]
    assert logql == (
        'sum by (device_id) (count_over_time({device_id=~"a\\\\.b\\\\|c\\"|d1"} '
        "|= \"Failed with result 'oom-kill'\" [240s]))"
    )
    assert at == 1240.9
