import io
import os
import tarfile

from conftest import make_bundle
from ota_agent.install import (
    APP_UNIT,
    FLUENT_UNIT,
    parse_prom_sample,
    read_env_file,
    sha256_file,
)


def test_healthy_install_flips_slot_and_updates_labels(device):
    installer, systemd, tmp = device
    bundle = make_bundle(tmp, "v1.1")
    res = installer.install(bundle, expected_sha256=sha256_file(bundle))
    assert res.ok, res.message
    assert (res.version, res.previous_version, res.slot) == ("v1.1", "v1.0", "b")
    assert os.readlink(installer.paths.current) == "slot_b"
    assert installer.installed_version() == "v1.1"
    labels = read_env_file(installer.paths.labels_env)
    assert labels == {"DEVICE_ID": "edge-001", "HW_REV": "B", "REGION": "eu-west", "FW_VERSION": "v1.1"}
    assert systemd.restarts == [APP_UNIT, FLUENT_UNIT]
    assert installer.last_result()["version"] == "v1.1"


def test_second_install_goes_back_to_slot_a(device):
    installer, _, tmp = device
    assert installer.install(make_bundle(tmp, "v1.1")).slot == "b"
    res = installer.install(make_bundle(tmp, "v1.2"))
    assert res.ok and res.slot == "a" and res.previous_version == "v1.1"
    assert installer.installed_version() == "v1.2"


def test_sha_mismatch_does_not_touch_app(device):
    installer, systemd, tmp = device
    res = installer.install(make_bundle(tmp, "v1.1"), expected_sha256="00" * 32)
    assert not res.ok and "sha256 mismatch" in res.message
    assert os.readlink(installer.paths.current) == "slot_a"
    assert systemd.restarts == []


def test_crash_during_window_rolls_back(device):
    installer, systemd, tmp = device
    systemd.behaviour["v9.9"] = "crash"
    res = installer.install(make_bundle(tmp, "v9.9"))
    assert not res.ok
    assert "rolled back to v1.0" in res.message
    assert os.readlink(installer.paths.current) == "slot_a"
    assert systemd.restarts == [APP_UNIT, APP_UNIT]
    assert not installer.paths.labels_env.exists()
    assert installer.last_result()["ok"] is False


def test_no_heartbeat_rolls_back_after_grace(device):
    installer, systemd, tmp = device
    systemd.behaviour["v9.9"] = "silent"
    res = installer.install(make_bundle(tmp, "v9.9"))
    assert not res.ok and "no heartbeat" in res.message
    assert res.details["window_s"] >= installer.health_window_s + installer.heartbeat_grace_s
    assert installer.installed_version() == "v1.0"


def test_health_window_is_at_least_configured(device):
    installer, _, tmp = device
    res = installer.install(make_bundle(tmp, "v1.1"))
    assert res.details["window_s"] >= 15


def test_bundle_with_top_level_dir(device):
    installer, _, tmp = device
    assert installer.install(make_bundle(tmp, "v1.1", top_dir=True)).ok
    assert installer.installed_version() == "v1.1"


def test_rejects_path_traversal(device):
    installer, systemd, tmp = device
    bundle = tmp / "evil.tar"
    with tarfile.open(bundle, "w") as tar:
        info = tarfile.TarInfo("../../etc/passwd")
        info.size = 1
        tar.addfile(info, io.BytesIO(b"x"))
    res = installer.install(bundle)
    assert not res.ok and "unsafe path" in res.message
    assert systemd.restarts == []


def test_rejects_non_wavebreak_manifest(device):
    installer, _, tmp = device
    bundle = make_bundle(tmp, "v1.1", extra={"manifest.json": '{"name": "other", "version": "1"}'})
    res = installer.install(bundle)
    assert not res.ok and "not a wavebreak-app release" in res.message


def test_rejects_garbage_file(device):
    installer, _, tmp = device
    junk = tmp / "junk.tar"
    junk.write_bytes(b"not a tar")
    res = installer.install(junk)
    assert not res.ok and "cannot extract" in res.message


def test_fluent_bit_restart_failure_is_not_fatal(device):
    installer, systemd, tmp = device
    systemd.fail_restart_units.add(FLUENT_UNIT)
    assert installer.install(make_bundle(tmp, "v1.1")).ok


def test_parse_prom_sample():
    text = (
        "# HELP x\n"
        'wavebreak_app_info{fw_version="v1.2",sensor="4K"} 1\n'
        "wavebreak_app_frames_total 125\n"
        "wavebreak_app_fps 5\n"
    )
    assert parse_prom_sample(text) == {"fw_version": "v1.2", "frames_total": 125.0}
