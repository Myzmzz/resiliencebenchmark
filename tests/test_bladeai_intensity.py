"""The intensity mapping that outlived the in-process shim.

WP-F removed the shim; this module is now the only definition, read by the
black-box adapter, the simulated user and the harness channel alike.  The cases
below are the ones that were pinned against the shim's own implementation while
both existed, kept as the behavioural contract.
"""

from __future__ import annotations

import pytest

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


# The values the shim produced for each case, recorded while both
# implementations existed and verified equal at that time.
EXPECTED = {
    ("cpu-load", "fullload", True): ({"cpu_percent": 80}, "agent_plan"),
    ("cpu-load", "fullload", False): ({"cpu_percent": 100}, "tool_default"),
    ("network-delay", "delay", True): ({"delay_ms": 70}, "agent_plan"),
    ("network-loss", "loss", True): ({"loss_percent": 30}, "agent_plan"),
    ("network-loss", "drop", False): ({"loss_percent": 100}, "agent_plan"),
    ("memory-stress", "load", True): ({"mem_percent": 50}, "agent_plan"),
}


@pytest.mark.parametrize("fault_type,flags,action", CASES)
def test_reproduces_the_mapping_the_shim_had(fault_type, flags, action) -> None:
    expected_intensity, expected_source = EXPECTED[(fault_type, action, bool(flags))]
    assert canonical_native_intensity(fault_type, dict(flags), action=action) == expected_intensity
    assert native_intensity_source(fault_type, dict(flags), action=action) == expected_source


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
