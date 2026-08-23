"""Independent Controller safety gate for disturbance trigger requests.

This layer deliberately reuses the immutable boundaries from ``safety.py``
without treating a disturbance as an Agent-requested ChaosBlade action.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from .safety import ControllerPolicy, RUN_ID_RE, validate_policy


@dataclass(frozen=True)
class DisturbanceAuthorizationRequest:
    run_id: str
    level_id: str
    disturbance_id: str
    disturbance_type: str
    backend: str
    target: Mapping[str, Any]
    parameters: Mapping[str, Any]
    labels: Mapping[str, str] = field(default_factory=dict)
    active_mutating_disturbances: int = 0


@dataclass(frozen=True)
class DisturbanceAuthorizationDecision:
    allowed: bool
    reasons: tuple[str, ...] = ()


class ControllerDisturbanceSafetyGate:
    """Fail-closed authorization using the same namespace/identity boundaries."""

    MUTATING_BACKENDS = frozenset(
        {"kubernetes", "chaos_effect_proxy", "workload_interceptor", "safety_pressure"}
    )

    def __init__(
        self,
        policy: ControllerPolicy,
        *,
        allowed_types: set[str] | frozenset[str],
    ) -> None:
        self.policy = policy
        self.allowed_types = frozenset(allowed_types)

    def authorize(self, request: DisturbanceAuthorizationRequest) -> DisturbanceAuthorizationDecision:
        reasons = [finding.code for finding in validate_policy(self.policy).findings]
        if not RUN_ID_RE.fullmatch(request.run_id):
            reasons.append("INVALID_RUN_ID")
        if not request.level_id:
            reasons.append("MISSING_LEVEL_ID")
        if request.disturbance_type not in self.allowed_types:
            reasons.append("DISTURBANCE_TYPE_NOT_ALLOWED")
        if request.labels.get("benchmark.run_id") != request.run_id:
            reasons.append("MISSING_RUN_ID_LABEL")
        namespace = str(request.target.get("namespace") or "")
        if namespace not in self.policy.namespace_allowlist:
            reasons.append("NAMESPACE_NOT_ALLOWED")
        if request.backend == "kubernetes":
            if request.target.get("kind") != "Pod":
                reasons.append("TARGET_KIND_NOT_ALLOWED")
            if not request.target.get("name"):
                reasons.append("MISSING_TARGET_NAME")
            if not request.target.get("uid"):
                reasons.append("MISSING_TARGET_UID")
        if (
            request.backend in self.MUTATING_BACKENDS
            and request.active_mutating_disturbances >= self.policy.max_concurrent_actions
        ):
            reasons.append("DISTURBANCE_CONCURRENCY_BUDGET_EXCEEDED")
        hard_limit = request.parameters.get("hard_limit_percent")
        target_percent = request.parameters.get("target_percent")
        if hard_limit is not None and float(hard_limit) > 80:
            reasons.append("SAFETY_HARD_LIMIT_EXCEEDED")
        if target_percent is not None and float(target_percent) > 80:
            reasons.append("SAFETY_TARGET_EXCEEDED")
        for key in ("cpu_limit_percent_of_original", "memory_limit_percent_of_original"):
            if key in request.parameters and not 20 <= float(request.parameters[key]) <= 100:
                reasons.append("RESOURCE_QUOTA_OUTSIDE_SAFE_RANGE")
        return DisturbanceAuthorizationDecision(allowed=not reasons, reasons=tuple(dict.fromkeys(reasons)))
