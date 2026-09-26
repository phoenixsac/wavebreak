from agent.fleet_mcp.config import Settings
from agent.fleet_mcp.models import Thresholds


def test_defaults():
    s = Settings.from_env({})
    assert s.hawkbit_url == "http://localhost:8080"
    assert (s.hawkbit_username, s.hawkbit_password) == ("admin", "admin")
    assert s.prometheus_url == "http://localhost:9090" and s.loki_url == "http://localhost:3100"
    assert s.lab_url == "http://localhost:8090"
    assert s.ledger_path == "run/fleet-ledger.sqlite"
    assert (s.mcp_host, s.mcp_port, s.lab_mock) == ("127.0.0.1", 8792, False)
    assert (s.evidence_max_age_s, s.lab_max_minutes, s.observe_max_minutes) == (900, 5, 4)
    assert s.poll_interval_s == 15 and s.bundles_dir == "sim/bundles" and s.ds_name == "wavebreak-app"
    assert s.thresholds == Thresholds()


def test_overrides():
    env = {
        "HAWKBIT_URL": "http://hb:1",
        "FLEET_MCP_PORT": "9000",
        "FLEET_LAB_MOCK": "1",
        "FLEET_EVIDENCE_MAX_AGE_S": "60",
        "FLEET_LAB_MOCK_SCENARIOS": '{"v1.3": "crash"}',
        "FLEET_TH_CRASH_RESTARTS": "5",
        "FLEET_TH_FPS_RATIO": "0.5",
        "FLEET_TH_MEM_SLOPE_MARGIN_MB_PER_MIN": "2.5",
    }
    s = Settings.from_env(env)
    assert s.hawkbit_url == "http://hb:1" and s.mcp_port == 9000 and s.lab_mock is True
    assert s.evidence_max_age_s == 60.0
    assert s.lab_mock_scenarios == {"v1.3": "crash"}
    assert s.thresholds.crash_restarts == 5 and s.thresholds.fps_ratio == 0.5
    assert s.thresholds.mem_slope_margin_mb_per_min == 2.5
    assert s.thresholds.min_samples == 3
