"""Typed Stage-2 Agent plan parsing and safety-envelope validation."""

from __future__ import annotations

from collections.abc import Mapping
from enum import Enum
import math
import re
from typing import Any

from pydantic import ConfigDict, Field, ValidationError, field_validator, model_validator

from controller.safety import ControllerPolicy

from .condition_policy import (
    EFFECT_OPERATORS,
    RECOVERY_OPERATORS,
    WORKLOAD_METRICS,
)
from .contracts import ContractModel, MainFaultSpec


DNS_LABEL_RE = r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$"
POD_NAME_RE = r"^[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?$"
UID_RE = r"^[A-Za-z0-9][A-Za-z0-9_.:/#@-]{1,255}$"


class FaultType(str, Enum):
    NETWORK_DELAY = "network-delay"
    NETWORK_LOSS = "network-loss"
    CPU_LOAD = "cpu-load"
    MEMORY_STRESS = "memory-stress"

    @classmethod
    def canonical(cls, raw: Any) -> "FaultType":
        if isinstance(raw, cls):
            return raw
        if not isinstance(raw, str):
            raise ValueError("fault_type must be a string")
        value = _compact_alias(raw)
        aliases = {
            "networkdelay": cls.NETWORK_DELAY,
            "latency": cls.NETWORK_DELAY,
            "delay": cls.NETWORK_DELAY,
            "networklatency": cls.NETWORK_DELAY,
            "latencyfault": cls.NETWORK_DELAY,
            "延迟": cls.NETWORK_DELAY,
            "网络延迟": cls.NETWORK_DELAY,
            "networkloss": cls.NETWORK_LOSS,
            "packetloss": cls.NETWORK_LOSS,
            "loss": cls.NETWORK_LOSS,
            "丢包": cls.NETWORK_LOSS,
            "丢包率": cls.NETWORK_LOSS,
            "网络丢包": cls.NETWORK_LOSS,
            "cpuload": cls.CPU_LOAD,
            "cpu": cls.CPU_LOAD,
            "cpu负载": cls.CPU_LOAD,
            "内存": cls.MEMORY_STRESS,
            "内存压力": cls.MEMORY_STRESS,
            "memorystress": cls.MEMORY_STRESS,
            "memory": cls.MEMORY_STRESS,
        }
        if value in aliases:
            return aliases[value]
        try:
            return cls(raw)
        except ValueError as exc:
            raise ValueError("fault_type is outside the Stage-2 action space") from exc


class AgentTarget(ContractModel):
    """Agent-selected exact Pod target, matching chaos_control tool arguments."""

    namespace: str = Field(min_length=1, max_length=63, pattern=DNS_LABEL_RE)
    name: str = Field(min_length=1, max_length=253, pattern=POD_NAME_RE)
    uid: str = Field(min_length=1, max_length=255, pattern=UID_RE)
    kind: str = "Pod"

    @field_validator("kind")
    @classmethod
    def exact_pod_only(cls, value: str) -> str:
        if value != "Pod":
            raise ValueError("target.kind must be Pod")
        return value


class Condition(ContractModel):
    """Agent-owned workload condition without Controller timing policy."""

    metric: str
    operator: str
    threshold: float

    @field_validator("metric", "operator")
    @classmethod
    def non_empty_string(cls, value: Any) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("condition field must be a non-empty string")
        return value.strip()

    @field_validator("threshold", mode="before")
    @classmethod
    def strict_threshold(cls, value: Any) -> float:
        number = _strict_number(value)
        if number is None or number < 0:
            raise ValueError("threshold must be a finite non-negative number")
        return number


class IntensityFieldEnvelope(ContractModel):
    unit: str
    min_value: float = 0.0
    max_value: float | None = None

    @field_validator("min_value", "max_value", mode="before")
    @classmethod
    def optional_strict_number(cls, value: Any) -> float | None:
        if value is None:
            return None
        number = _strict_number(value)
        if number is None:
            raise ValueError("envelope bound must be a finite number")
        return number


class FaultEnvelope(ContractModel):
    intensity_fields: dict[str, IntensityFieldEnvelope]


class PlanSafetyEnvelope(ContractModel):
    """Caller-supplied limits for one Trial's Agent plan."""

    allowed_namespaces: tuple[str, ...]
    allowed_fault_types: tuple[str, ...]
    fault_contracts: dict[str, FaultEnvelope]
    max_fault_duration_seconds: int = Field(ge=1, strict=True)
    max_threshold_by_metric: dict[str, float] = Field(default_factory=dict)
    max_effect_observation_seconds: int | None = None
    max_recovery_observation_seconds: int | None = None
    max_agent_cleanup_seconds: int | None = None
    require_target_uid: bool = True

    @classmethod
    def from_controller_policy(
        cls,
        policy: ControllerPolicy,
        *,
        allowed_fault_types: tuple[str, ...] | None = None,
        max_threshold_by_metric: Mapping[str, float] | None = None,
        max_effect_observation_seconds: int | None = None,
        max_recovery_observation_seconds: int | None = None,
        max_agent_cleanup_seconds: int | None = None,
    ) -> "PlanSafetyEnvelope":
        selected_faults = allowed_fault_types or tuple(policy.fault_type_contracts)
        fault_contracts: dict[str, FaultEnvelope] = {}
        for fault_type in selected_faults:
            contract = policy.fault_type_contracts.get(fault_type)
            if contract is None:
                continue
            fault_contracts[fault_type] = FaultEnvelope(
                intensity_fields={
                    name: IntensityFieldEnvelope(
                        unit=field.unit,
                        min_value=_optional_numeric_attr(field, "min_value", default=0.0),
                        max_value=_optional_numeric_attr(field, "max_value", default=None),
                    )
                    for name, field in contract.intensity_fields.items()
                }
            )
        return cls(
            allowed_namespaces=tuple(sorted(policy.namespace_allowlist)),
            allowed_fault_types=tuple(selected_faults),
            fault_contracts=fault_contracts,
            max_fault_duration_seconds=policy.max_fault_duration_seconds,
            max_threshold_by_metric=dict(max_threshold_by_metric or {}),
            max_effect_observation_seconds=max_effect_observation_seconds,
            max_recovery_observation_seconds=max_recovery_observation_seconds,
            max_agent_cleanup_seconds=max_agent_cleanup_seconds,
            require_target_uid=policy.require_target_uid,
        )

    @field_validator("allowed_namespaces", "allowed_fault_types")
    @classmethod
    def non_empty_tuple(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("plan safety envelope must not be empty")
        return value


class PlanValidationIssue(ContractModel):
    code: str
    path: str
    message: str
    correction: str


class PlanValidationResult(ContractModel):
    ok: bool
    plan: "AgentPlan | None" = None
    issues: tuple[PlanValidationIssue, ...] = ()


class AgentPlan(ContractModel):
    """Strict typed shape shared by Harness replies and Controller validation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    target: AgentTarget
    fault_type: FaultType
    intensity: dict[str, float]
    effect_condition: Condition
    recovery_condition: Condition
    stop_conditions: tuple[str, ...]
    safety_ttl_seconds: int = Field(ge=1, strict=True)
    effect_observation_seconds: int = Field(ge=1, strict=True)
    effect_sustain_seconds: int = Field(ge=0, strict=True)
    agent_cleanup_seconds: int = Field(ge=1, strict=True)
    recovery_observation_seconds: int = Field(ge=1, strict=True)
    recovery_sustain_seconds: int = Field(ge=0, strict=True)

    @field_validator("fault_type", mode="before")
    @classmethod
    def canonical_fault_type(cls, value: Any) -> FaultType:
        return FaultType.canonical(value)

    @field_validator("intensity", mode="before")
    @classmethod
    def strict_intensity(cls, value: Any) -> dict[str, float]:
        if not isinstance(value, Mapping):
            raise ValueError("intensity must be an object")
        normalized: dict[str, float] = {}
        for key, raw in value.items():
            if not isinstance(key, str) or not key:
                raise ValueError("intensity keys must be non-empty strings")
            number = _strict_number(raw)
            if number is None or number < 0:
                raise ValueError(f"intensity.{key} must be a finite non-negative number")
            normalized[key] = number
        return normalized

    @field_validator("stop_conditions", mode="before")
    @classmethod
    def strict_stop_conditions(cls, value: Any) -> tuple[str, ...]:
        if not isinstance(value, (list, tuple)):
            raise ValueError("stop_conditions must be a non-empty list")
        normalized = tuple(str(item).strip() for item in value if str(item).strip())
        if not normalized:
            raise ValueError("stop_conditions must be a non-empty list")
        return normalized

    @model_validator(mode="after")
    def validate_condition_operators(self) -> "AgentPlan":
        if self.effect_condition.metric not in WORKLOAD_METRICS:
            raise ValueError("effect_condition.metric is not a supported workload metric")
        if self.recovery_condition.metric not in WORKLOAD_METRICS:
            raise ValueError("recovery_condition.metric is not a supported workload metric")
        if self.effect_condition.operator not in EFFECT_OPERATORS:
            raise ValueError("effect_condition.operator is invalid for effect checks")
        if self.recovery_condition.operator not in RECOVERY_OPERATORS:
            raise ValueError("recovery_condition.operator is invalid for recovery checks")
        return self

    def main_fault_spec(self) -> MainFaultSpec:
        return MainFaultSpec(
            fault_type=self.fault_type.value,
            duration_seconds=self.safety_ttl_seconds,
            intensity=dict(self.intensity),
        )

    def chaos_create_arguments(self) -> dict[str, Any]:
        return {
            "namespace": self.target.namespace,
            "target_name": self.target.name,
            "target_uid": self.target.uid,
            "fault_type": self.fault_type.value,
            "duration_seconds": self.safety_ttl_seconds,
            "intensity": dict(self.intensity),
        }


def validate_agent_plan(raw: Mapping[str, Any] | None, envelope: PlanSafetyEnvelope) -> PlanValidationResult:
    if not isinstance(raw, Mapping):
        return _invalid(
            "PLAN_SCHEMA_INVALID",
            "",
            "Agent plan must be a JSON object.",
            "Return one object containing target, fault_type, intensity, conditions, stop_conditions, and timing fields.",
        )
    raw_issues = _raw_repair_issues(raw, envelope)
    try:
        plan = AgentPlan.model_validate(raw)
    except ValidationError as exc:
        return PlanValidationResult(
            ok=False,
            issues=_dedupe_issues((*raw_issues, *_issues_from_validation_error(exc))),
        )
    except ValueError as exc:
        return PlanValidationResult(
            ok=False,
            issues=_dedupe_issues(
                (
                    *raw_issues,
                    _issue(
                        "PLAN_SCHEMA_INVALID",
                        "",
                        str(exc),
                        "Return the plan using the Stage-2 AgentPlan fields.",
                    ),
                )
            ),
        )

    issues: list[PlanValidationIssue] = list(raw_issues)
    if plan.target.namespace not in envelope.allowed_namespaces:
        issues.append(
            _issue(
                "NAMESPACE_NOT_ALLOWED",
                "target.namespace",
                "Target namespace is outside the Trial safety envelope.",
                "Choose a Pod in one of: " + ", ".join(envelope.allowed_namespaces),
            )
        )
    if envelope.require_target_uid and not plan.target.uid:
        issues.append(
            _issue(
                "MISSING_TARGET_UID",
                "target.uid",
                "Target UID is required before mutation.",
                "Re-read the exact Pod and include metadata.uid.",
            )
        )

    fault_type = plan.fault_type.value
    if fault_type not in envelope.allowed_fault_types or fault_type not in envelope.fault_contracts:
        issues.append(
            _issue(
                "FAULT_TYPE_NOT_ALLOWED",
                "fault_type",
                "Fault type is outside the Trial action space.",
                "Choose one of: " + ", ".join(envelope.allowed_fault_types),
            )
        )
    else:
        _validate_intensity_against_envelope(plan, envelope.fault_contracts[fault_type], issues)

    if plan.safety_ttl_seconds > envelope.max_fault_duration_seconds:
        issues.append(
            _issue(
                "SAFETY_TTL_EXCEEDED",
                "safety_ttl_seconds",
                "Safety TTL exceeds the Trial safety envelope.",
                f"Use safety_ttl_seconds <= {envelope.max_fault_duration_seconds}.",
            )
        )
    _validate_timing_limit(
        plan.effect_observation_seconds,
        envelope.max_effect_observation_seconds,
        "effect_observation_seconds",
        issues,
    )
    _validate_timing_limit(
        plan.recovery_observation_seconds,
        envelope.max_recovery_observation_seconds,
        "recovery_observation_seconds",
        issues,
    )
    _validate_timing_limit(
        plan.agent_cleanup_seconds,
        envelope.max_agent_cleanup_seconds,
        "agent_cleanup_seconds",
        issues,
    )
    _validate_condition_envelope("effect_condition", plan.effect_condition, envelope, issues)
    _validate_condition_envelope("recovery_condition", plan.recovery_condition, envelope, issues)

    if issues:
        return PlanValidationResult(ok=False, issues=_dedupe_issues(issues))
    return PlanValidationResult(ok=True, plan=plan)


def _raw_repair_issues(
    raw: Mapping[str, Any],
    envelope: PlanSafetyEnvelope,
) -> tuple[PlanValidationIssue, ...]:
    """Collect independent repair hints even when Pydantic stops early."""

    issues: list[PlanValidationIssue] = []
    target = raw.get("target")
    if isinstance(target, Mapping):
        namespace = target.get("namespace")
        if isinstance(namespace, str) and namespace not in envelope.allowed_namespaces:
            issues.append(
                _issue(
                    "NAMESPACE_NOT_ALLOWED",
                    "target.namespace",
                    "Target namespace is outside the Trial safety envelope.",
                    "Choose a Pod in one of: " + ", ".join(envelope.allowed_namespaces),
                )
            )
        uid = target.get("uid")
        if envelope.require_target_uid and (not isinstance(uid, str) or not uid.strip()):
            issues.append(
                _issue(
                    "MISSING_TARGET_UID",
                    "target.uid",
                    "Target UID is required before mutation.",
                    "Re-read the exact Pod and include metadata.uid.",
                )
            )

    fault_type = raw.get("fault_type")
    if fault_type is not None:
        try:
            canonical = FaultType.canonical(fault_type).value
        except ValueError:
            issues.append(
                _issue(
                    "FAULT_TYPE_NOT_ALLOWED",
                    "fault_type",
                    "Fault type is outside the Stage-2 action space.",
                    "Use a canonical fault_type such as network-delay, network-loss, cpu-load, or memory-stress.",
                )
            )
        else:
            if canonical not in envelope.allowed_fault_types:
                issues.append(
                    _issue(
                        "FAULT_TYPE_NOT_ALLOWED",
                        "fault_type",
                        "Fault type is outside the Trial action space.",
                        "Choose one of: " + ", ".join(envelope.allowed_fault_types),
                    )
                )

    _raw_condition_issues("effect_condition", raw.get("effect_condition"), EFFECT_OPERATORS, issues)
    _raw_condition_issues("recovery_condition", raw.get("recovery_condition"), RECOVERY_OPERATORS, issues)
    return _dedupe_issues(issues)


def _raw_condition_issues(
    name: str,
    raw: Any,
    operators: frozenset[str],
    issues: list[PlanValidationIssue],
) -> None:
    if not isinstance(raw, Mapping):
        return
    metric = raw.get("metric")
    if isinstance(metric, str) and metric not in WORKLOAD_METRICS:
        issues.append(
            _issue(
                "INVALID_CONDITION_METRIC",
                f"{name}.metric",
                "Condition metric is not supported.",
                "Use target_latency_ms, target_success_rate, or target_current_rps.",
            )
        )
    operator = raw.get("operator")
    if isinstance(operator, str) and operator not in operators:
        issues.append(
            _issue(
                "INVALID_EFFECT_OPERATOR" if name == "effect_condition" else "INVALID_RECOVERY_OPERATOR",
                f"{name}.operator",
                "Condition operator is not supported for this phase.",
                "Use an operator supported for this condition phase.",
            )
        )
    threshold = raw.get("threshold")
    strict_threshold = _strict_number(threshold)
    if strict_threshold is None or strict_threshold < 0:
        issues.append(
            _issue(
                "INVALID_CONDITION_THRESHOLD",
                f"{name}.threshold",
                "Condition threshold must be a finite non-negative number.",
                "Use a finite non-negative JSON number for the condition threshold.",
            )
        )


def _validate_intensity_against_envelope(
    plan: AgentPlan,
    fault: FaultEnvelope,
    issues: list[PlanValidationIssue],
) -> None:
    expected = set(fault.intensity_fields)
    observed = set(plan.intensity)
    for name in sorted(expected - observed):
        issues.append(
            _issue(
                "MISSING_INTENSITY_FIELD",
                f"intensity.{name}",
                "Intensity is missing a field required by the selected fault type.",
                "Provide exactly these intensity fields: " + ", ".join(sorted(expected)),
            )
        )
    for name in sorted(observed - expected):
        issues.append(
            _issue(
                "UNKNOWN_INTENSITY_FIELD",
                f"intensity.{name}",
                "Intensity includes a field that is not valid for the selected fault type.",
                "Provide exactly these intensity fields: " + ", ".join(sorted(expected)),
            )
        )
    for name in sorted(expected & observed):
        value = plan.intensity[name]
        field = fault.intensity_fields[name]
        if value < field.min_value:
            issues.append(
                _issue(
                    "INTENSITY_BELOW_ENVELOPE",
                    f"intensity.{name}",
                    "Intensity is below the Trial envelope.",
                    f"Use {name} >= {field.min_value:g}.",
                )
            )
        if field.max_value is not None and value > field.max_value:
            issues.append(
                _issue(
                    "INTENSITY_EXCEEDS_ENVELOPE",
                    f"intensity.{name}",
                    "Intensity exceeds the Trial envelope.",
                    f"Use {name} <= {field.max_value:g}.",
                )
            )


def _validate_condition_envelope(
    name: str,
    condition: Condition,
    envelope: PlanSafetyEnvelope,
    issues: list[PlanValidationIssue],
) -> None:
    maximum = envelope.max_threshold_by_metric.get(condition.metric)
    if maximum is not None and condition.threshold > maximum:
        issues.append(
            _issue(
                "THRESHOLD_EXCEEDS_ENVELOPE",
                f"{name}.threshold",
                "Condition threshold exceeds the Trial envelope.",
                f"Use {condition.metric} threshold <= {maximum:g}.",
            )
        )


def _validate_timing_limit(
    value: int,
    maximum: int | None,
    path: str,
    issues: list[PlanValidationIssue],
) -> None:
    if maximum is not None and value > maximum:
        issues.append(
            _issue(
                "TIMING_BUDGET_EXCEEDED",
                path,
                "Timing field exceeds the Trial envelope.",
                f"Use {path} <= {maximum}.",
            )
        )


def _issues_from_validation_error(exc: ValidationError) -> tuple[PlanValidationIssue, ...]:
    issues: list[PlanValidationIssue] = []
    for error in exc.errors():
        path = ".".join(str(item) for item in error.get("loc", ()))
        message = str(error.get("msg") or "invalid Agent plan field")
        code = _schema_issue_code(path, message)
        issues.append(_issue(code, path, message, _correction_for(code, path)))
    return tuple(issues)


def _dedupe_issues(issues: tuple[PlanValidationIssue, ...] | list[PlanValidationIssue]) -> tuple[PlanValidationIssue, ...]:
    seen: set[tuple[str, str]] = set()
    deduped: list[PlanValidationIssue] = []
    for issue in issues:
        key = (issue.code, issue.path)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(issue)
    return tuple(deduped)


def _schema_issue_code(path: str, message: str) -> str:
    if "Field required" in message:
        return "MISSING_PLAN_FIELD"
    if path == "fault_type":
        return "FAULT_TYPE_NOT_ALLOWED"
    if path.startswith("intensity"):
        return "INVALID_INTENSITY_VALUE"
    if path.endswith("threshold"):
        return "INVALID_CONDITION_THRESHOLD"
    if path == "effect_condition.operator" or "effect_condition.operator" in message:
        return "INVALID_EFFECT_OPERATOR"
    if path == "recovery_condition.operator" or "recovery_condition.operator" in message:
        return "INVALID_RECOVERY_OPERATOR"
    if path.endswith("metric"):
        return "INVALID_CONDITION_METRIC"
    if path.endswith("uid"):
        return "MISSING_TARGET_UID"
    return "PLAN_SCHEMA_INVALID"


def _correction_for(code: str, path: str) -> str:
    if code == "FAULT_TYPE_NOT_ALLOWED":
        return "Use a canonical fault_type such as network-delay, network-loss, cpu-load, or memory-stress."
    if code == "INVALID_INTENSITY_VALUE":
        return "Use a finite non-negative JSON number for the exact intensity field required by the fault type."
    if code == "INVALID_CONDITION_THRESHOLD":
        return "Use a finite non-negative JSON number for the condition threshold."
    if code in {"INVALID_EFFECT_OPERATOR", "INVALID_RECOVERY_OPERATOR"}:
        return "Use an operator supported for this condition phase."
    if code == "INVALID_CONDITION_METRIC":
        return "Use target_latency_ms, target_success_rate, or target_current_rps."
    if code == "MISSING_TARGET_UID":
        return "Re-read the exact Pod and include metadata.uid."
    return f"Repair AgentPlan field {path or '<root>'}."


def _optional_numeric_attr(value: Any, name: str, *, default: float | None) -> float | None:
    raw = getattr(value, name, default)
    if raw is None:
        return None
    number = _strict_number(raw)
    return default if number is None else number


def _invalid(code: str, path: str, message: str, correction: str) -> PlanValidationResult:
    return PlanValidationResult(
        ok=False,
        issues=(_issue(code, path, message, correction),),
    )


def _issue(code: str, path: str, message: str, correction: str) -> PlanValidationIssue:
    return PlanValidationIssue(
        code=code,
        path=path,
        message=message,
        correction=correction,
    )


def _strict_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    return number


def _compact_alias(raw: str) -> str:
    return re.sub(r"[\s_\-]+", "", raw.strip().lower())
