"""The intensity mapping lifted out of the shim WP-F deletes.

Pins behaviour to the shim's own, so the copy cannot silently drift before the
original is removed.
"""

from __future__ import annotations

import pytest

from stage2_service.bladeai_shim import (
    canonical_native_intensity as shim_intensity,
    native_intensity_source as shim_source,
)
from stage2_service.harness_adapters.bladeai_intensity import (
    BladeShimError,
    canonical_native_intensity,
    native_intensity_source,
)

CASES = [
    ("cpu-load", {"--cpu-percent": "80"}, "fullload"),
    ("cpu-load", {}, "fullload"),
    ("network-delay", {"--time": "70"}, "delay"),
    ("network-loss", {"--percent": "30"}, "loss"),
    ("network-loss", {}, "drop"),
    ("memory-stress", {"--mem-percent": "50"}, "load"),
]


@pytest.mark.parametrize("fault_type,flags,action", CASES)
def test_matches_the_shim_it_was_lifted_from(fault_type, flags, action) -> None:
    assert canonical_native_intensity(fault_type, dict(flags), action=action) == \
        shim_intensity(fault_type, dict(flags), action=action)
    assert native_intensity_source(fault_type, dict(flags), action=action) == \
        shim_source(fault_type, dict(flags), action=action)


def test_agent_chosen_intensity_is_distinguished_from_a_tool_default() -> None:
    """A defaulted intensity costs plan-validation credit, so it must show."""
    assert canonical_native_intensity("cpu-load", {"--cpu-percent": "80"}, action="fullload") \
        == {"cpu_percent": 80}
    assert native_intensity_source("cpu-load", {"--cpu-percent": "80"}, action="fullload") \
        == "agent_plan"
    # ChaosBlade defaults --cpu-percent to 100 when the plan omits it.
    assert canonical_native_intensity("cpu-load", {}, action="fullload") == {"cpu_percent": 100}
    assert native_intensity_source("cpu-load", {}, action="fullload") == "tool_default"


def test_unmappable_intensity_is_refused_rather_than_guessed() -> None:
    with pytest.raises(BladeShimError):
        canonical_native_intensity("pod-kill", {}, action="kill")
    with pytest.raises(BladeShimError):
        canonical_native_intensity("cpu-load", {"--cpu-percent": "0"}, action="fullload")
    with pytest.raises(BladeShimError):
        canonical_native_intensity("cpu-load", {"--cpu-percent": "80%"}, action="fullload")


def test_controller_fixed_flags_are_enforced() -> None:
    assert canonical_native_intensity(
        "network-delay", {"--time": "70", "--interface": "eth0", "--offset": "0"}, action="delay"
    ) == {"delay_ms": 70}
    with pytest.raises(BladeShimError):
        canonical_native_intensity(
            "network-delay", {"--time": "70", "--interface": "eth1"}, action="delay"
        )
