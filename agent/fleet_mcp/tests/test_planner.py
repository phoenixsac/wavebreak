import pytest

from agent.fleet_mcp.errors import PreconditionError
from agent.fleet_mcp.models import Device
from agent.fleet_mcp.planner import pick_from_version, plan_waves


def fleet(n_a=6, n_b=6, regions=("eu", "us")):
    out = []
    for hw, n in (("A", n_a), ("B", n_b)):
        for i in range(n):
            out.append(Device(f"{hw.lower()}{i:02d}", hw, regions[i % len(regions)], "v1.1"))
    return out


def ids(waves):
    return [[d.id for d in w] for w in waves]


def test_default_waves_stratified():
    waves, warns = plan_waves(fleet(), [2, 5, "rest"])
    assert [len(w) for w in waves] == [2, 5, 5]
    assert sorted(d.hw_rev for d in waves[0]) == ["A", "B"]
    assert warns == []
    assert sorted(d.id for w in waves for d in w) == sorted(d.id for d in fleet())


def test_region_spread_within_stratum():
    waves, _ = plan_waves(fleet(), [4, "rest"])
    a_regions = [d.region for d in waves[0] if d.hw_rev == "A"]
    assert sorted(a_regions) == ["eu", "us"]
    # region round-robin then id: A gets eu first (sorted), then us
    assert ids(waves)[0] == ["a00", "b00", "a01", "b01"]


def test_size_one_with_two_strata_warns():
    waves, warns = plan_waves(fleet(), [1, "rest"])
    assert len(waves[0]) == 1 and waves[0][0].hw_rev == "A"
    assert warns == ["not_stratified: wave 1 has size 1 < 2 strata"]


def test_rest_and_dropped_empty_waves():
    devs = fleet(1, 1)
    waves, warns = plan_waves(devs, [2, 5, "rest"])
    assert len(waves) == 1 and warns == []
    waves, _ = plan_waves(fleet(3, 0), ["rest"])
    assert len(waves) == 1 and len(waves[0]) == 3


def test_without_rest_leaves_devices_out():
    waves, _ = plan_waves(fleet(), [2, 3])
    assert sum(len(w) for w in waves) == 5


def test_uneven_strata_continue_round_robin():
    waves, _ = plan_waves(fleet(5, 1), [4, "rest"])
    assert ids(waves)[0] == ["a00", "b00", "a01", "a02"]


def test_deterministic_regardless_of_input_order():
    devs = fleet()
    a, _ = plan_waves(devs, [2, 5, "rest"])
    b, _ = plan_waves(list(reversed(devs)), [2, 5, "rest"])
    assert ids(a) == ids(b)


def test_stratify_by_region():
    waves, _ = plan_waves(fleet(), [2, "rest"], stratify_by="region")
    assert sorted(d.region for d in waves[0]) == ["eu", "us"]


@pytest.mark.parametrize(
    "sizes", [[], [0], [-1, "rest"], ["rest", 2], [2.5], ["all"], [True], ["rest", "rest"], [None]]
)
def test_bad_sizes(sizes):
    with pytest.raises(PreconditionError) as ei:
        plan_waves(fleet(), sizes)
    assert ei.value.code == "BAD_ARGUMENT"


def test_bad_stratify_by():
    with pytest.raises(PreconditionError) as ei:
        plan_waves(fleet(), [1], stratify_by="color")
    assert ei.value.code == "BAD_ARGUMENT"


def test_pick_from_version():
    assert pick_from_version({"a": "v1.1", "b": "v1.1", "c": "v1.0"}) == "v1.1"
    assert pick_from_version({"a": "v1.1", "b": "v1.2"}) == "v1.2"
    with pytest.raises(PreconditionError):
        pick_from_version({})
