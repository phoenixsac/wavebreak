"""Stratified wave planning (docs/agent-design.md section 10.1). Pure and deterministic."""

from __future__ import annotations

from collections import Counter
from itertools import zip_longest

from .errors import PreconditionError
from .models import Device

STRATA_ATTRS = ("hw_rev", "region", "version")


def _validate_sizes(sizes: list) -> None:
    if not sizes:
        raise PreconditionError("BAD_ARGUMENT", "sizes must not be empty")
    for i, s in enumerate(sizes):
        if s == "rest":
            if i != len(sizes) - 1:
                raise PreconditionError("BAD_ARGUMENT", '"rest" is only allowed as the last wave size')
        elif isinstance(s, bool) or not isinstance(s, int) or s < 1:
            raise PreconditionError("BAD_ARGUMENT", f"wave size must be a positive int or 'rest', got {s!r}")


def _region_round_robin(devices: list[Device]) -> list[Device]:
    """Order devices by region round-robin (regions sorted), then id within a region."""
    by_region: dict[str, list[Device]] = {}
    for d in sorted(devices, key=lambda d: d.id):
        by_region.setdefault(d.region, []).append(d)
    columns = [by_region[r] for r in sorted(by_region)]
    return [d for row in zip_longest(*columns) for d in row if d is not None]


def plan_waves(
    devices: list[Device], sizes: list[int | str], stratify_by: str = "hw_rev"
) -> tuple[list[list[Device]], list[str]]:
    """Split `devices` into waves; returns (waves, warnings).

    Each wave cycles over the sorted strata taking one device at a time (cycle restarts at the first
    stratum for every wave). `"rest"` (last size only) takes everything left; without it, devices
    beyond the sum of sizes are not in the plan. Empty waves are dropped.
    """
    _validate_sizes(list(sizes))
    if stratify_by not in STRATA_ATTRS:
        raise PreconditionError("BAD_ARGUMENT", f"stratify_by must be one of {', '.join(STRATA_ATTRS)}")
    strata: dict[str, list[Device]] = {}
    for d in devices:
        strata.setdefault(getattr(d, stratify_by), []).append(d)
    queues = [_region_round_robin(strata[k]) for k in sorted(strata)]
    k_strata = len(queues)
    idx = [0] * k_strata
    remaining = len(devices)
    waves: list[list[Device]] = []
    warnings: list[str] = []
    for size in sizes:
        want = remaining if size == "rest" else min(size, remaining)
        wave: list[Device] = []
        while len(wave) < want:
            for i, q in enumerate(queues):
                if len(wave) >= want:
                    break
                if idx[i] < len(q):
                    wave.append(q[idx[i]])
                    idx[i] += 1
        remaining -= len(wave)
        if not wave:
            continue
        waves.append(wave)
        if len(wave) < k_strata:
            warnings.append(f"not_stratified: wave {len(waves)} has size {len(wave)} < {k_strata} strata")
    return waves, warnings


def pick_from_version(devices_versions: dict[str, str]) -> str:
    """Most common installed version; ties resolve to the highest version string."""
    if not devices_versions:
        raise PreconditionError("BAD_ARGUMENT", "no devices to pick a from_version from")
    counts = Counter(devices_versions.values())
    return max(counts, key=lambda v: (counts[v], v))
