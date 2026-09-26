"""v1.1 adds wavebreak_app_frame_latency_seconds; v1.0 does not have it."""

import pytest

from conftest import FakeClock, build_app


def _metrics_text(version, tmp_path):
    clock = FakeClock()
    _module, app, _log = build_app(version, tmp_path, hw_rev="A", clock=clock)
    app.step()
    clock.advance(6)
    app.step()
    return (tmp_path / "metrics" / "inference_app.prom").read_text()


def test_v1_0_has_no_latency_metric(tmp_path):
    text = _metrics_text("v1.0", tmp_path)
    assert "wavebreak_app_frame_latency_seconds" not in text


@pytest.mark.parametrize("version", ["v1.1", "v1.2", "v1.3", "v1.4"])
def test_later_releases_have_latency_metric(version, tmp_path):
    text = _metrics_text(version, tmp_path)
    assert "# TYPE wavebreak_app_frame_latency_seconds gauge" in text
    assert "wavebreak_app_frame_latency_seconds " in text


def test_v1_1_frame_stats_log_has_latency_field(tmp_path):
    import json

    clock = FakeClock()
    _module, app, log = build_app("v1.1", tmp_path, clock=clock)
    app.step()
    clock.advance(6)
    app.step()
    records = [json.loads(line) for line in log.getvalue().splitlines()]
    frame_stats = next(rec for rec in records if rec["event"] == "frame_stats")
    assert "latency_s" in frame_stats
