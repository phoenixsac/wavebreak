"""fw_version comes from manifest.json "version"; sensor from HW_REV."""

import pytest
from conftest import ALL_VERSIONS, BUNDLES_DIR, load_bundle_module


@pytest.mark.parametrize("version", ALL_VERSIONS)
def test_fw_version_matches_manifest(version):
    module = load_bundle_module(version)
    assert module.read_fw_version(BUNDLES_DIR / version) == version


def test_sensor_from_hw_rev():
    module = load_bundle_module("v1.0")
    assert module.sensor_for_hw_rev("A") == "1080p"
    assert module.sensor_for_hw_rev("B") == "4K"


def test_build_identity_defaults_region_unknown():
    module = load_bundle_module("v1.0")
    identity = module.build_identity(BUNDLES_DIR / "v1.0", {"DEVICE_ID": "edge-001", "HW_REV": "B"})
    assert identity.region == "unknown"
    assert identity.sensor == "4K"
    assert identity.fw_version == "v1.0"
