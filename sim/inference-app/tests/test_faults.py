"""Fault-release behaviour: v1.2 leak, v1.3 config crash, v1.4 fix, and the
healthy releases running clean.

- v1.2 adds a 4K enhancement pass kept in `self.enhance_buffer`; on rev B
  ("4K" sensor) it grows without bound. Rev A never touches the path.
- v1.3 renames `detection_threshold` to `confidence_threshold` in the bundle
  config but ships no device-side migration, so the periodic config reload
  reads the still-old device file and raises KeyError.
- v1.4 bounds `enhance_buffer` to `enhance_max_items` (default 4), fixing the
  v1.2 leak on rev B.

All tests drive `step()` directly with a FakeClock; nothing sleeps or reaches
the real filesystem outside tmp_path.
"""

from __future__ import annotations

import json

import pytest
from conftest import DEVICE_CONFIG_TEXT, FakeClock, build_app

RELOAD_INTERVAL_S = 45  # config_reload_interval_s in every bundle config.yaml


def _read_metric(text: str, name: str) -> int:
    line = next(line_ for line_ in text.splitlines() if line_.startswith(f"{name} "))
    return int(line.split()[-1])


def _log_records(log) -> list[dict]:
    return [json.loads(line) for line in log.getvalue().splitlines()]


# --------------------------------------------------------------------------
# 1. v1.2 leak: unbounded enhance_buffer on rev B, empty on rev A.
# --------------------------------------------------------------------------


def test_v1_2_rev_b_enhance_buffer_grows_without_bound(tmp_path):
    clock = FakeClock()
    _module, app, _log = build_app("v1.2", tmp_path, hw_rev="B", clock=clock)

    for n in (10, 25):
        for _ in range(n):
            app.step()
    total = 35
    assert len(app.enhance_buffer) == total


def test_v1_2_rev_a_enhance_buffer_stays_empty(tmp_path):
    clock = FakeClock()
    _module, app, _log = build_app("v1.2", tmp_path, hw_rev="A", clock=clock)

    for _ in range(35):
        app.step()
    assert app.enhance_buffer == []


def test_v1_2_cache_items_metric_grows_unbounded_on_rev_b(tmp_path):
    clock = FakeClock()
    _module, app, _log = build_app("v1.2", tmp_path, hw_rev="B", clock=clock)
    metrics_path = tmp_path / "metrics" / "inference_app.prom"

    def cache_items_after(n_steps: int) -> int:
        for _ in range(n_steps):
            app.step()
        clock.advance(6)  # cross metrics_interval_s (default 5) to force a write
        app.step()
        return _read_metric(metrics_path.read_text(), "wavebreak_app_cache_items")

    first = cache_items_after(5)
    second = cache_items_after(30)
    assert second > first


# --------------------------------------------------------------------------
# 2. v1.4 fix: enhance_buffer bounded to enhance_max_items (default 4).
# --------------------------------------------------------------------------


def test_v1_4_rev_b_enhance_buffer_bounded(tmp_path):
    clock = FakeClock()
    _module, app, _log = build_app("v1.4", tmp_path, hw_rev="B", clock=clock)

    for _ in range(50):
        app.step()

    max_items = int(app.config.get("enhance_max_items", 4))
    assert max_items == 4
    assert len(app.enhance_buffer) == max_items


# --------------------------------------------------------------------------
# 3. v1.3 crash: startup works; first reload raises KeyError. v1.1 does not.
# --------------------------------------------------------------------------


def test_v1_3_startup_and_first_frame_work(tmp_path):
    clock = FakeClock()
    _module, app, _log = build_app("v1.3", tmp_path, clock=clock)
    app.step()  # startup config load already happened in __init__; first frame is fine
    assert app.frames_total == 1


def test_v1_3_first_config_reload_raises_keyerror_on_device_config(tmp_path):
    clock = FakeClock()
    _module, app, log = build_app("v1.3", tmp_path, clock=clock)
    app.step()

    clock.advance(RELOAD_INTERVAL_S + 1)
    with pytest.raises(KeyError):
        app.step()

    records = _log_records(log)
    failed = [r for r in records if r["event"] == "config_reload_failed"]
    assert len(failed) == 1
    # Note: the app only logs and re-raises; nothing here calls sys.exit or
    # os._exit. The KeyError propagates out of step()/run()/main() uncaught,
    # so a real process would die from an unhandled exception (traceback +
    # exit code 1 from the interpreter), not from an explicit exit call.
    assert "confidence_threshold" in failed[0]["error"]


def test_v1_1_config_reload_with_same_device_config_does_not_fail(tmp_path):
    clock = FakeClock()
    _module, app, log = build_app("v1.1", tmp_path, clock=clock, device_config_text=DEVICE_CONFIG_TEXT)
    app.step()

    clock.advance(RELOAD_INTERVAL_S + 1)
    app.step()  # must not raise

    records = _log_records(log)
    assert any(r["event"] == "config_reload" for r in records)
    assert not any(r["event"] == "config_reload_failed" for r in records)


# --------------------------------------------------------------------------
# 4. Healthy versions on both revisions: many frames + a reload, no raise.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("version", ["v1.0", "v1.1", "v1.4"])
@pytest.mark.parametrize("hw_rev", ["A", "B"])
def test_healthy_versions_run_many_frames_and_reload_without_raising(version, hw_rev, tmp_path):
    clock = FakeClock()
    _module, app, _log = build_app(version, tmp_path, hw_rev=hw_rev, clock=clock)

    for _ in range(50):
        app.step()
    clock.advance(RELOAD_INTERVAL_S + 1)  # crosses the reload boundary
    for _ in range(50):
        app.step()

    assert app.frames_total == 100
