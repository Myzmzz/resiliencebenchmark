"""Native ChaosBlade intensity, normalised to the Controller's contract.

Lifted verbatim out of ``stage2_service/bladeai_shim.py`` (which WP-F deletes)
because the mapping is a **semantic asset**, not part of the in-process hook
layer: it is the only place that states how a native ``blade`` flag such as
``--cpu-percent 80`` corresponds to the platform's ``{"cpu_percent": 80}``, and
how to tell an intensity the Agent chose from one ChaosBlade defaulted.

The shim still carries its own copy until WP-E's residue sweep is accepted and
WP-F removes it; this module is the copy the black-box path uses, so deleting
the shim later cannot take the mapping down with it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class BladeShimError(ValueError):
    """A requested CLI operation is outside the controlled shim contract."""


CHAOSBLADE_FAULT_SCENARIOS = {
    ("pod", "cpu", "fullload"): "cpu-load",
    ("pod", "cpu", "load"): "cpu-load",
    ("pod", "mem", "load"): "memory-stress",
    ("pod", "memory", "load"): "memory-stress",
    ("pod", "network", "delay"): "network-delay",
    ("pod", "network", "loss"): "network-loss",
    ("pod", "network", "drop"): "network-loss",
}
# The one native numeric flag each fault type accepts.
NATIVE_INTENSITY_FLAGS = {
    "network-delay": ("--time", "delay_ms"),
    "network-loss": ("--percent", "loss_percent"),
    "cpu-load": ("--cpu-percent", "cpu_percent"),
    "memory-stress": ("--mem-percent", "mem_percent"),
}
# ChaosBlade's documented default for an intensity flag a plan leaves out
# (pod-cpu fullload: ``--cpu-percent`` defaults to 100). Such a plan is not
# refused: the default is used, recorded as the intensity source, and costs
# plan-validation credit (user rule, 2026-09-10). Fault types without a
# documented default must still state their intensity.
NATIVE_INTENSITY_TOOL_DEFAULTS = {
    "cpu-load": "100",
}
# Native flags whose value the Controller fixes. A command may omit them or
# repeat exactly these values; any other value is refused.
CONTROLLER_FIXED_NATIVE_FLAGS = {
    "network-delay": {"--interface": "eth0", "--offset": "0"},
    "network-loss": {"--interface": "eth0"},
}


def canonical_native_intensity(fault_type: str, flags: Mapping[str, Any], *, action: str) -> dict[str, int]:
    """Map only documented native numeric knobs to Controller canonical fields."""
    try:
        native_key, canonical = NATIVE_INTENSITY_FLAGS[fault_type]
    except KeyError as exc:
        raise BladeShimError("native fault has no Controller-equivalent intensity mapping") from exc
    fixed_flags = CONTROLLER_FIXED_NATIVE_FLAGS.get(fault_type, {})
    if "--interface" in fixed_flags:
        interface = flags.pop("--interface", fixed_flags["--interface"])
        if interface != fixed_flags["--interface"]:
            raise BladeShimError("network interface must be Controller-fixed eth0")
    if "--offset" in fixed_flags:
        offset = flags.pop("--offset", fixed_flags["--offset"])
        if (
            isinstance(offset, bool)
            or not isinstance(offset, (str, int))
            or not str(offset).isdigit()
            or int(offset) != int(fixed_flags["--offset"])
        ):
            raise BladeShimError("network delay offset must be the Controller-fixed 0")
    if fault_type == "network-loss" and action == "drop":
        if flags:
            raise BladeShimError("network drop maps only to Controller 100 percent loss")
        return {"loss_percent": 100}
    if not flags:
        default = NATIVE_INTENSITY_TOOL_DEFAULTS.get(fault_type)
        if default is None:
            raise BladeShimError(
                f"the plan does not state {native_key} (the {fault_type} intensity) "
                "and ChaosBlade documents no default for it"
            )
        flags = {native_key: default}
    if set(flags) != {native_key}:
        raise BladeShimError("native fault parameters are not exactly representable by Controller policy")
    value = flags[native_key]
    # CLI flags are strings; the SDK's structured proposal may contain JSON
    # integers. Both serialize to the same native argument without conversion.
    if isinstance(value, bool) or not isinstance(value, (str, int)) or not str(value).isdigit() or int(value) <= 0:
        raise BladeShimError("native fault intensity must be a positive integer without units")
    return {canonical: int(value)}


def native_intensity_source(fault_type: str, flags: Mapping[str, Any], *, action: str) -> str:
    """Where a plan's intensity comes from: ``agent_plan`` or ``tool_default``.

    ``tool_default`` means the plan left the intensity flag out and ChaosBlade's
    documented default (NATIVE_INTENSITY_TOOL_DEFAULTS) stands in, which is
    recorded and costs plan-validation credit.
    """
    native_key = NATIVE_INTENSITY_FLAGS.get(fault_type, ("", ""))[0]
    implied_by_action = fault_type == "network-loss" and action == "drop"
    if (
        native_key
        and not implied_by_action
        and native_key not in flags
        and fault_type in NATIVE_INTENSITY_TOOL_DEFAULTS
    ):
        return "tool_default"
    return "agent_plan"


def canonical_fault_type(scope: str, target: str, action: str) -> tuple[str, str]:
    """Map the SDK's ``fault_intent`` to a Stage-2 fault type and its native action.

    Returns ``(fault_type, native_action)``. The SDK normally writes ChaosBlade's
    own action (``{"scope": "pod", "target": "cpu", "action": "fullload"}``), but
    it has also written the same fault as ``"cpu-load"`` -- the Stage-2 name --
    and may use the skill spelling ``"pod-cpu-fullload"``. Those all name one
    fault, so the action is normalised first (lower case, ``_`` to ``-``, a
    leading ``<scope>-`` and ``<target>-`` removed), and a Stage-2 name is
    accepted when it belongs to the same scope and resource. Anything else --
    a network action on the CPU resource, a Pod delete, a node-scoped fault --
    is still refused. The table is the shim's, so both paths accept one set.
    """
    scope_key = scope.strip().lower()
    target_key = target.strip().lower()
    spelled = action.strip().lower().replace("_", "-")
    native_action = spelled
    for prefix in (f"{scope_key}-", f"{target_key}-"):
        if native_action.startswith(prefix):
            native_action = native_action[len(prefix):]
    fault_type = CHAOSBLADE_FAULT_SCENARIOS.get((scope_key, target_key, native_action))
    if fault_type is not None:
        return fault_type, native_action
    same_resource = {
        value
        for (known_scope, known_target, _action), value in CHAOSBLADE_FAULT_SCENARIOS.items()
        if (known_scope, known_target) == (scope_key, target_key)
    }
    if spelled in same_resource:
        return spelled, spelled
    raise BladeShimError("BladeAI fault_intent is outside the authorized Stage-2 fault space")
