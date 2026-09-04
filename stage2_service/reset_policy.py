"""Reset tier classification from observed mutation evidence."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ResetTier(str, Enum):
    T0_NO_WRITE = "T0_NO_WRITE"
    T1_CAPABILITY = "T1_CAPABILITY"
    T2_FAULT_OR_TARGET = "T2_FAULT_OR_TARGET"
    T3_FULL_REINSTALL = "T3_FULL_REINSTALL"


class ResetAction(str, Enum):
    VERIFY_BASELINE = "verify_baseline"
    RESTORE_PERMISSIONS = "restore_permissions"
    REBIND_CAPABILITIES = "rebind_capabilities"
    CLEANUP_FAULTS = "cleanup_faults"
    VERIFY_TARGET = "verify_target"
    VERIFY_BUSINESS = "verify_business"
    FULL_REINSTALL = "full_reinstall"


@dataclass(frozen=True)
class ResetPolicyDecision:
    tier: ResetTier
    verified: bool
    allows_next_trial: bool
    required_actions: tuple[ResetAction, ...]
    reason_codes: tuple[str, ...] = ()
    evidence_summary: Mapping[str, Any] = field(default_factory=dict)
    schema_version: str = "stage2-reset-policy.v1"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "tier": self.tier.value,
            "verified": self.verified,
            "allows_next_trial": self.allows_next_trial,
            "required_actions": [item.value for item in self.required_actions],
            "reason_codes": list(self.reason_codes),
            "evidence_summary": dict(self.evidence_summary),
        }

    def model_dump(self, *_, **__) -> dict[str, Any]:
        return self.to_dict()


def classify_reset_policy(evidence: Mapping[str, Any] | None) -> ResetPolicyDecision:
    """Choose the minimum reset tier that can safely prepare the next trial."""

    data = dict(evidence or {})
    explicit = _explicit_tier(data)
    summary = _summarize(data)
    tier = explicit or _infer_tier(summary)
    required_actions = _required_actions(tier)
    reason_codes = _reason_codes(tier, summary)
    verified = _verified(tier, data, summary)
    return ResetPolicyDecision(
        tier=tier,
        verified=verified,
        allows_next_trial=verified,
        required_actions=required_actions,
        reason_codes=reason_codes,
        evidence_summary=summary,
    )


def _explicit_tier(data: Mapping[str, Any]) -> ResetTier | None:
    raw = _first_string(data, ("reset_tier", "required_reset_tier", "mutation_tier"))
    if not raw:
        return None
    normalized = raw.upper()
    aliases = {
        "T0": ResetTier.T0_NO_WRITE,
        "T0_NO_WRITE": ResetTier.T0_NO_WRITE,
        "NO_WRITE": ResetTier.T0_NO_WRITE,
        "T1": ResetTier.T1_CAPABILITY,
        "T1_CAPABILITY": ResetTier.T1_CAPABILITY,
        "CAPABILITY": ResetTier.T1_CAPABILITY,
        "T2": ResetTier.T2_FAULT_OR_TARGET,
        "T2_FAULT_OR_TARGET": ResetTier.T2_FAULT_OR_TARGET,
        "FAULT_OR_TARGET": ResetTier.T2_FAULT_OR_TARGET,
        "T3": ResetTier.T3_FULL_REINSTALL,
        "T3_FULL_REINSTALL": ResetTier.T3_FULL_REINSTALL,
        "FULL_REINSTALL": ResetTier.T3_FULL_REINSTALL,
    }
    return aliases.get(normalized)


def _infer_tier(summary: Mapping[str, Any]) -> ResetTier:
    if summary["unknown_or_failed_rollback"]:
        return ResetTier.T3_FULL_REINSTALL
    if summary["fault_or_target_mutated"]:
        return ResetTier.T2_FAULT_OR_TARGET
    if summary["capability_mutated"]:
        return ResetTier.T1_CAPABILITY
    return ResetTier.T0_NO_WRITE


def _required_actions(tier: ResetTier) -> tuple[ResetAction, ...]:
    actions = {
        ResetTier.T0_NO_WRITE: (ResetAction.VERIFY_BASELINE,),
        ResetTier.T1_CAPABILITY: (
            ResetAction.RESTORE_PERMISSIONS,
            ResetAction.REBIND_CAPABILITIES,
            ResetAction.VERIFY_BASELINE,
        ),
        ResetTier.T2_FAULT_OR_TARGET: (
            ResetAction.CLEANUP_FAULTS,
            ResetAction.VERIFY_TARGET,
            ResetAction.VERIFY_BUSINESS,
        ),
        ResetTier.T3_FULL_REINSTALL: (
            ResetAction.FULL_REINSTALL,
            ResetAction.VERIFY_BASELINE,
            ResetAction.VERIFY_BUSINESS,
        ),
    }
    return actions[tier]


def _reason_codes(tier: ResetTier, summary: Mapping[str, Any]) -> tuple[str, ...]:
    reasons: list[str] = []
    if summary["unknown_or_failed_rollback"]:
        reasons.append("UNKNOWN_OR_FAILED_ROLLBACK")
    if summary["fault_or_target_mutated"]:
        reasons.append("FAULT_OR_TARGET_MUTATION_OBSERVED")
    if summary["capability_mutated"]:
        reasons.append("CAPABILITY_MUTATION_OBSERVED")
    if tier is ResetTier.T0_NO_WRITE and not reasons:
        reasons.append("NO_WRITE_MUTATION_OBSERVED")
    if not summary["tier_verification_present"]:
        reasons.append("RESET_TIER_VERIFICATION_MISSING")
    return tuple(reasons)


def _verified(
    tier: ResetTier, data: Mapping[str, Any], summary: Mapping[str, Any]
) -> bool:
    if summary["unknown_or_failed_rollback"]:
        return False
    if tier is ResetTier.T0_NO_WRITE:
        return _truthy(
            data,
            (
                "baseline_verified",
                "environment_verified",
                "qualified",
                "business_healthy",
            ),
        )
    if tier is ResetTier.T1_CAPABILITY:
        restored = _truthy(
            data,
            (
                "permission_restore_verified",
                "permissions_restored",
                "capability_rebound_verified",
                "capability_restored",
            ),
        )
        baseline = _truthy(
            data,
            (
                "baseline_verified",
                "environment_verified",
                "qualified",
                "business_healthy",
            ),
        )
        return restored and baseline
    if tier is ResetTier.T2_FAULT_OR_TARGET:
        return _truthy(data, ("fault_absent", "fault_cleanup_verified")) and _truthy(
            data, ("business_recovery_verified", "business_healthy")
        )
    return _truthy(data, ("full_reinstall_verified", "reinstalled_and_verified"))


def _summarize(data: Mapping[str, Any]) -> dict[str, Any]:
    outcome_uncertain = _truthy(
        data,
        (
            "operation_outcome_unknown",
            "operation_outcome_uncertain",
            "mutation_outcome_unknown",
            "result_unknown",
        ),
    )
    outcome_reconciled = _truthy(
        data,
        (
            "operation_outcome_reconciled",
            "outcome_reconciled",
            "mutation_outcome_reconciled",
        ),
    )
    rollback_failed = _rollback_failed(data)
    fault_or_target = _truthy(
        data,
        (
            "main_fault_ever_active",
            "fault_created",
            "fault_injected",
            "target_replaced",
            "target_changed",
            "pod_replaced",
            "pod_deleted",
        ),
    ) or _contains_any_string(
        data,
        (
            "target_change",
            "pod_replace",
            "pod_replaced",
            "chaosblade",
            "fault",
            "cpu-load",
            "network-delay",
        ),
    )
    capability = _truthy(
        data,
        (
            "permission_revoked",
            "permissions_revoked",
            "permission_restore_verified",
            "capability_rebound",
            "capability_rebound_verified",
            "observability_revoked",
            "tool_channel_interrupted",
        ),
    ) or _contains_any_string(
        data,
        (
            "permission_change",
            "observability_change",
            "tool_channel_interruption",
            "mcp_policy",
            "kubernetes_rbac",
            "mcp_transport",
        ),
    )
    verified_keys = (
        "verified",
        "reset_verified",
        "baseline_verified",
        "environment_verified",
        "permission_restore_verified",
        "capability_rebound_verified",
        "fault_absent",
        "business_recovery_verified",
        "full_reinstall_verified",
    )
    return {
        "unknown_or_failed_rollback": rollback_failed
        or (outcome_uncertain and not outcome_reconciled),
        "operation_outcome_uncertain": outcome_uncertain,
        "operation_outcome_reconciled": outcome_reconciled,
        "rollback_failed": rollback_failed,
        "fault_or_target_mutated": fault_or_target,
        "capability_mutated": capability,
        "tier_verification_present": any(
            _path_exists(data, key) for key in verified_keys
        ),
    }


def _rollback_failed(data: Mapping[str, Any]) -> bool:
    attempted = _truthy(data, ("rollback_attempted", "cleanup_attempted"))
    failed = _falsey(data, ("rolled_back", "rollback_verified", "cleanup_verified"))
    return attempted and failed


def _truthy(data: Mapping[str, Any], keys: Iterable[str]) -> bool:
    return any(_value_at(data, key) is True for key in keys)


def _falsey(data: Mapping[str, Any], keys: Iterable[str]) -> bool:
    return any(_value_at(data, key) is False for key in keys)


def _path_exists(data: Mapping[str, Any], key: str) -> bool:
    sentinel = object()
    return _value_at(data, key, sentinel) is not sentinel


def _first_string(data: Mapping[str, Any], keys: Iterable[str]) -> str | None:
    for key in keys:
        value = _value_at(data, key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _value_at(data: Mapping[str, Any], dotted: str, default: Any = None) -> Any:
    current: Any = data
    for part in dotted.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return default
        current = current[part]
    return current


def _contains_any_string(data: Mapping[str, Any], needles: tuple[str, ...]) -> bool:
    for value in _walk(data):
        if isinstance(value, str):
            lowered = value.lower()
            if any(needle in lowered for needle in needles):
                return True
    return False


def _walk(value: Any) -> Iterable[Any]:
    if isinstance(value, Mapping):
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _walk(child)
    else:
        yield value
