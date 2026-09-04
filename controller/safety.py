"""Safety checks for controller-owned ChaosBlade actions.

This module is intentionally side-effect free. It validates a proposed action
before any Kubernetes or ChaosBlade client is allowed to mutate the cluster.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import math
import re
from typing import Any, Mapping, Optional, Tuple


RUN_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,62}$")
MAX_FAULT_DURATION_SECONDS = 20 * 60


class LifecyclePhase(str, Enum):
    PREPARE = "prepare"
    QUALIFY = "qualify"
    BASELINE = "baseline"
    PLAN = "plan"
    EXECUTE = "execute"
    OBSERVE = "observe"
    RECOVER = "recover"
    EVALUATE = "evaluate"
    CLEANUP = "cleanup"


ALLOWED_TRANSITIONS: Mapping[LifecyclePhase, frozenset[LifecyclePhase]] = {
    LifecyclePhase.PREPARE: frozenset({LifecyclePhase.QUALIFY, LifecyclePhase.CLEANUP}),
    LifecyclePhase.QUALIFY: frozenset({LifecyclePhase.BASELINE, LifecyclePhase.CLEANUP}),
    LifecyclePhase.BASELINE: frozenset({LifecyclePhase.PLAN, LifecyclePhase.CLEANUP}),
    LifecyclePhase.PLAN: frozenset({LifecyclePhase.EXECUTE, LifecyclePhase.CLEANUP}),
    LifecyclePhase.EXECUTE: frozenset({LifecyclePhase.OBSERVE, LifecyclePhase.RECOVER, LifecyclePhase.CLEANUP}),
    LifecyclePhase.OBSERVE: frozenset({LifecyclePhase.RECOVER, LifecyclePhase.CLEANUP}),
    LifecyclePhase.RECOVER: frozenset({LifecyclePhase.EVALUATE, LifecyclePhase.CLEANUP}),
    LifecyclePhase.EVALUATE: frozenset({LifecyclePhase.CLEANUP}),
    LifecyclePhase.CLEANUP: frozenset(),
}


@dataclass(frozen=True)
class SafetyFinding:
    code: str
    message: str


@dataclass(frozen=True)
class ValidationResult:
    findings: tuple[SafetyFinding, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.findings

    def codes(self) -> tuple[str, ...]:
        return tuple(finding.code for finding in self.findings)


@dataclass(frozen=True)
class IntensityField:
    unit: str

    def accepts(self, raw_value: Any) -> bool:
        value = _coerce_number(raw_value, self.unit)
        return value is not None and math.isfinite(value)


@dataclass(frozen=True)
class FaultTypeContract:
    intensity_fields: Mapping[str, IntensityField] = field(default_factory=dict)


@dataclass(frozen=True)
class AbortGate:
    enabled: bool = True
    max_runtime_seconds: int = 1800
    heartbeat_timeout_seconds: int = MAX_FAULT_DURATION_SECONDS


@dataclass(frozen=True)
class CleanupGate:
    enabled: bool = True
    max_cleanup_seconds: int = 300
    require_run_id_label: bool = True
    require_target_uid: bool = True
    verify_absence: bool = True


@dataclass(frozen=True)
class ControllerPolicy:
    namespace_allowlist: frozenset[str]
    fault_type_contracts: Mapping[str, FaultTypeContract]
    max_fault_duration_seconds: int = MAX_FAULT_DURATION_SECONDS
    max_concurrent_actions: int = 1
    require_single_target: bool = True
    require_target_uid: bool = True
    require_run_id_label: bool = True
    abort_gate: AbortGate = field(default_factory=AbortGate)
    cleanup_gate: CleanupGate = field(default_factory=CleanupGate)


@dataclass(frozen=True)
class TargetIdentity:
    namespace: str
    kind: str
    name: str
    uid: str
    selector: Optional[Mapping[str, str]] = None


@dataclass(frozen=True)
class ChaosBladeAction:
    run_id: str
    namespace: str
    target: TargetIdentity
    fault_type: str
    duration_seconds: int
    intensity: Mapping[str, Any] = field(default_factory=dict)
    labels: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class RunLease:
    run_id: str
    phase: LifecyclePhase
    started_at: datetime
    last_agent_heartbeat_at: datetime | None
    active_action_ids: Tuple[str, ...] = ()


def default_policy(namespace_allowlist: set[str] | frozenset[str]) -> ControllerPolicy:
    """Return the structural safety policy for Stage-2 fault execution."""

    return ControllerPolicy(
        namespace_allowlist=frozenset(namespace_allowlist),
        fault_type_contracts={
            "cpu-load": FaultTypeContract(
                intensity_fields={"cpu_percent": IntensityField("percent")},
            ),
            "memory-stress": FaultTypeContract(
                intensity_fields={"mem_percent": IntensityField("percent")},
            ),
            "network-delay": FaultTypeContract(
                intensity_fields={"delay_ms": IntensityField("milliseconds")},
            ),
            "network-loss": FaultTypeContract(
                intensity_fields={"loss_percent": IntensityField("percent")},
            ),
            "pod-kill": FaultTypeContract(
                intensity_fields={"pod_count": IntensityField("count")},
            ),
        },
    )


def validate_policy(policy: ControllerPolicy) -> ValidationResult:
    findings: list[SafetyFinding] = []
    if not policy.namespace_allowlist:
        findings.append(_finding("NO_NAMESPACE_ALLOWLIST", "namespace allowlist must not be empty"))
    if not policy.fault_type_contracts:
        findings.append(_finding("NO_SUPPORTED_FAULT_TYPES", "at least one executable fault type is required"))
    if policy.max_fault_duration_seconds <= 0:
        findings.append(
            _finding(
                "INVALID_FAULT_TIMEOUT",
                "fault timeout must be positive",
            )
        )
    if policy.max_concurrent_actions != 1:
        findings.append(_finding("UNSAFE_CONCURRENCY", "controller only supports one active action per run"))
    if not policy.require_single_target:
        findings.append(_finding("MULTI_TARGET_ALLOWED", "single target enforcement must stay enabled"))
    if not policy.require_target_uid:
        findings.append(_finding("TARGET_UID_NOT_REQUIRED", "target UID is required to detect drift"))
    if not policy.require_run_id_label:
        findings.append(_finding("RUN_ID_LABEL_NOT_REQUIRED", "run_id label is required for cleanup"))
    if not policy.abort_gate.enabled:
        findings.append(_finding("ABORT_GATE_DISABLED", "abort gate must be enabled"))
    if not policy.cleanup_gate.enabled:
        findings.append(_finding("CLEANUP_GATE_DISABLED", "cleanup gate must be enabled"))
    if not policy.cleanup_gate.require_target_uid:
        findings.append(_finding("CLEANUP_WITHOUT_UID", "cleanup must verify target UID"))
    if not policy.cleanup_gate.require_run_id_label:
        findings.append(_finding("CLEANUP_WITHOUT_RUN_ID", "cleanup must scope by run_id label"))
    return ValidationResult(tuple(findings))


def validate_lifecycle_transition(current: LifecyclePhase, next_phase: LifecyclePhase) -> ValidationResult:
    if next_phase in ALLOWED_TRANSITIONS[current]:
        return ValidationResult()
    return ValidationResult(
        (
            _finding(
                "INVALID_LIFECYCLE_TRANSITION",
                f"cannot transition from {current.value} to {next_phase.value}",
            ),
        )
    )


def validate_action(
    action: ChaosBladeAction,
    policy: ControllerPolicy,
    *,
    active_action_count: int = 0,
) -> ValidationResult:
    findings: list[SafetyFinding] = list(validate_policy(policy).findings)

    if not RUN_ID_RE.fullmatch(action.run_id):
        findings.append(_finding("INVALID_RUN_ID", "run_id must be a stable DNS-label-like identifier"))
    if policy.require_run_id_label and action.labels.get("benchmark.run_id") != action.run_id:
        findings.append(_finding("MISSING_RUN_ID_LABEL", "action labels must include benchmark.run_id"))
    if action.namespace not in policy.namespace_allowlist:
        findings.append(_finding("NAMESPACE_NOT_ALLOWED", "action namespace is outside the allowlist"))
    if action.target.namespace != action.namespace:
        findings.append(_finding("TARGET_NAMESPACE_MISMATCH", "target namespace must match action namespace"))
    if action.target.kind != "Pod":
        findings.append(_finding("TARGET_KIND_NOT_ALLOWED", "only an exact Pod target is allowed"))
    if policy.require_single_target and action.target.selector:
        findings.append(_finding("SELECTOR_TARGET_FORBIDDEN", "selector-based targets are not single-target safe"))
    if policy.require_target_uid and not action.target.uid:
        findings.append(_finding("MISSING_TARGET_UID", "target UID is required before injection"))
    if not action.target.name:
        findings.append(_finding("MISSING_TARGET_NAME", "target name is required before injection"))
    if active_action_count >= policy.max_concurrent_actions:
        findings.append(_finding("CONCURRENCY_BUDGET_EXCEEDED", "another action is already active"))

    contract = policy.fault_type_contracts.get(action.fault_type)
    if contract is None:
        findings.append(_finding("FAULT_TYPE_NOT_ALLOWED", "fault type is outside the allowed action space"))
        return ValidationResult(tuple(findings))

    if action.duration_seconds <= 0:
        findings.append(_finding("INVALID_DURATION", "duration must be positive"))
    elif action.duration_seconds > policy.max_fault_duration_seconds:
        findings.append(
            _finding(
                "FAULT_TIMEOUT_EXCEEDED",
                f"duration exceeds the global {policy.max_fault_duration_seconds}-second fault timeout",
            )
        )

    unexpected = set(action.intensity) - set(contract.intensity_fields)
    if unexpected:
        findings.append(_finding("UNKNOWN_INTENSITY_FIELD", "intensity includes fields not allowed for this fault"))
    for key, field_contract in contract.intensity_fields.items():
        if key not in action.intensity:
            findings.append(_finding("MISSING_INTENSITY_FIELD", f"missing intensity field {key}"))
        elif not field_contract.accepts(action.intensity[key]):
            findings.append(
                _finding(
                    "INVALID_INTENSITY_VALUE",
                    f"intensity field {key} must be a finite numeric value",
                )
            )

    if not policy.cleanup_gate.verify_absence:
        findings.append(_finding("CLEANUP_NOT_VERIFIABLE", "cleanup must verify absence of this run's fault"))

    return ValidationResult(tuple(findings))


def should_cleanup_on_agent_loss(lease: RunLease, policy: ControllerPolicy, *, now: Optional[datetime] = None) -> bool:
    if not policy.abort_gate.enabled or not policy.cleanup_gate.enabled:
        return False
    if lease.phase in {LifecyclePhase.CLEANUP, LifecyclePhase.EVALUATE}:
        return False

    current_time = now or datetime.now(timezone.utc)
    heartbeat = lease.last_agent_heartbeat_at or lease.started_at
    heartbeat_age = (current_time - _as_utc(heartbeat)).total_seconds()
    runtime_age = (current_time - _as_utc(lease.started_at)).total_seconds()
    return (
        heartbeat_age > policy.abort_gate.heartbeat_timeout_seconds
        or runtime_age > policy.abort_gate.max_runtime_seconds
    )


def _coerce_number(raw_value: Any, unit: str) -> Optional[float]:
    if isinstance(raw_value, bool):
        return None
    if isinstance(raw_value, (int, float)):
        return float(raw_value)
    if not isinstance(raw_value, str):
        return None
    value = raw_value.strip()
    if unit == "percent" and value.endswith("%"):
        value = value[:-1]
    try:
        return float(value)
    except ValueError:
        return None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _finding(code: str, message: str) -> SafetyFinding:
    return SafetyFinding(code=code, message=message)
