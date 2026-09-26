"""Metrics file format (HELP/TYPE + value) and atomic write."""

import json

from conftest import FakeClock, build_app


def test_render_metric_help_type_value():
    from conftest import load_bundle_module

    module = load_bundle_module("v1.0")
    lines = module.render_metric("foo_metric", "gauge", "help text", "foo_metric 5")
    assert lines == ["# HELP foo_metric help text", "# TYPE foo_metric gauge", "foo_metric 5"]


def test_metrics_file_written_atomically_and_no_tmp_left_behind(tmp_path):
    clock = FakeClock()
    _module, app, _log = build_app("v1.0", tmp_path, hw_rev="B", clock=clock)

    app.step()  # first frame; metrics not due yet (elapsed 0 < 5s default)
    clock.advance(6)
    app.step()  # crosses metrics_interval_s -> writes the file

    metrics_dir = tmp_path / "metrics"
    target = metrics_dir / "inference_app.prom"
    assert target.exists()
    leftover_tmp = [p for p in metrics_dir.iterdir() if p.name != "inference_app.prom"]
    assert leftover_tmp == []

    text = target.read_text()
    assert '# HELP wavebreak_app_info' in text
    assert '# TYPE wavebreak_app_info gauge' in text
    assert 'wavebreak_app_info{fw_version="v1.0",sensor="4K"} 1' in text
    assert "# TYPE wavebreak_app_frames_total counter" in text
    assert "wavebreak_app_frames_total 2" in text
    assert "wavebreak_app_fps" in text
    assert "wavebreak_app_cache_items 2" in text
    assert "wavebreak_device_boot_time_seconds 1700000000.0" in text


def test_frame_stats_log_line_matches_metrics(tmp_path):
    clock = FakeClock()
    _module, app, log = build_app("v1.0", tmp_path, hw_rev="A", clock=clock)
    app.step()
    clock.advance(6)
    app.step()

    lines = [json.loads(line) for line in log.getvalue().splitlines()]
    frame_stats = [rec for rec in lines if rec["event"] == "frame_stats"]
    assert len(frame_stats) == 1
    rec = frame_stats[0]
    assert rec["frames_total"] == 2
    assert rec["cache_items"] == 2
    assert rec["device_id"] == "edge-001"
    assert rec["hw_rev"] == "A"
    assert rec["fw_version"] == "v1.0"
    assert "ts" in rec and "level" in rec


def test_read_rss_bytes_parses_vmrss_kb(tmp_path):
    from conftest import load_bundle_module

    module = load_bundle_module("v1.0")
    status = tmp_path / "status"
    status.write_text("Name:\tpython3\nVmRSS:\t   2048 kB\nVmSize:\t 4096 kB\n")
    assert module.read_rss_bytes(status) == 2048 * 1024


def test_read_rss_bytes_missing_file_returns_zero(tmp_path):
    from conftest import load_bundle_module

    module = load_bundle_module("v1.0")
    assert module.read_rss_bytes(tmp_path / "missing") == 0


def test_read_cgroup_memory_bytes_reads_memory_current(tmp_path):
    from conftest import load_bundle_module

    module = load_bundle_module("v1.0")
    cgroup_file = tmp_path / "cgroup"
    cgroup_file.write_text("0::/system.slice/inference-app.service\n")
    cgroup_root = tmp_path / "sys-fs-cgroup"
    mem_dir = cgroup_root / "system.slice" / "inference-app.service"
    mem_dir.mkdir(parents=True)
    (mem_dir / "memory.current").write_text("12345678\n")

    value = module.read_cgroup_memory_bytes(cgroup_file, cgroup_root)
    assert value == 12345678


def test_read_cgroup_memory_bytes_unreadable_returns_none(tmp_path):
    from conftest import load_bundle_module

    module = load_bundle_module("v1.0")
    assert module.read_cgroup_memory_bytes(tmp_path / "missing-cgroup", tmp_path) is None


def test_metrics_file_omits_cgroup_line_when_unreadable(tmp_path):
    clock = FakeClock()
    _module, app, _log = build_app(
        "v1.0", tmp_path, clock=clock,
        cgroup_path=tmp_path / "no-such-cgroup-file",
    )
    app.step()
    clock.advance(6)
    app.step()
    text = (tmp_path / "metrics" / "inference_app.prom").read_text()
    assert "wavebreak_app_cgroup_memory_bytes" not in text


def test_read_boot_time_prefers_file_then_falls_back_to_proc_stat(tmp_path):
    from conftest import load_bundle_module

    module = load_bundle_module("v1.0")
    boot_time_file = tmp_path / "boot_time"
    proc_stat = tmp_path / "stat"
    proc_stat.write_text("cpu 0 0 0 0\nbtime 1690000000\n")

    boot_time_file.write_text("1700000000.5")
    assert module.read_boot_time(boot_time_file, proc_stat) == 1700000000.5

    boot_time_file.unlink()
    assert module.read_boot_time(boot_time_file, proc_stat) == 1690000000.0
