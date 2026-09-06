"""Plan, policy-gate, naming and time helpers for controlled chaos."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import math
import re
from typing import Any, Mapping

from controller.safety import ChaosBladeAction, TargetIdentity, default_policy
from stage2_service.condition_policy import (
    CONDITION_POLICY,
    EFFECT_THRESHOLD_TOLERANCE_RATIO,
)
from stage2_service.plan_schema import (
    AgentPlan,
    PlanSafetyEnvelope,
)

from mcp_servers.chaos_core.contracts import (
    FAULT_TYPE_LABEL,
    LEDGER_VERSION,
    NAMESPACE_LABEL,
    OWNER_LABEL,
    OWNER_VALUE,
    RUN_ID_LABEL,
    SAFE_NAME_RE,
    TARGET_UID_LABEL,
    ChaosControlError,
    ExperimentRecord,
    RuntimeConfig,
)


def _action(
    run_id: str,
    namespace: str,
    target_name: str,
    target_uid: str,
    fault_type: str,
    duration_seconds: int,
    intensity: Mapping[str, Any],
    selector: Mapping[str, str] | None,
) -> ChaosBladeAction:
    labels = {
        RUN_ID_LABEL: run_id,
        TARGET_UID_LABEL: target_uid,
        NAMESPACE_LABEL: namespace,
        FAULT_TYPE_LABEL: fault_type,
        OWNER_LABEL: OWNER_VALUE,
    }
    target = TargetIdentity(namespace=namespace, kind="Pod", name=target_name, uid=target_uid, selector=selector)
    return ChaosBladeAction(
        run_id=run_id,
        namespace=namespace,
        target=target,
        fault_type=fault_type,
        duration_seconds=duration_seconds,
        intensity=dict(intensity),
        labels=labels,
    )


def _experiment_name(run_id: str, fault_type: str) -> str:
    digest = hashlib.sha256(f"{run_id}:{fault_type}".encode()).hexdigest()[:10]
    base = re.sub(r"[^a-z0-9-]+", "-", f"cc-{run_id}-{fault_type}".lower()).strip("-")
    return f"{base[:45]}-{digest}"


def _record_payload(record: ExperimentRecord) -> dict[str, Any]:
    return {
        "name": record.name,
        "namespace": record.namespace,
        "run_id": record.run_id,
        "target_name": record.target_name,
        "target_uid": record.target_uid,
        "fault_type": record.fault_type,
        "phase": record.phase,
        "owned": record.owned,
        "terminal": record.terminal,
        "labels": {
            RUN_ID_LABEL: record.labels.get(RUN_ID_LABEL),
            TARGET_UID_LABEL: record.labels.get(TARGET_UID_LABEL),
            NAMESPACE_LABEL: record.labels.get(NAMESPACE_LABEL),
            FAULT_TYPE_LABEL: record.labels.get(FAULT_TYPE_LABEL),
            OWNER_LABEL: record.labels.get(OWNER_LABEL),
        },
    }


def _finding_payload(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


def _policy_payload(policy: Any) -> dict[str, Any]:
    return {
        "namespace_allowlist": sorted(policy.namespace_allowlist),
        "max_concurrent_actions": policy.max_concurrent_actions,
        "max_fault_duration_seconds": policy.max_fault_duration_seconds,
        "intensity_limits": "none",
        "allowed_fault_types": sorted(policy.fault_type_contracts),
    }


def _create_ledger_payload(
    *,
    created_at: datetime,
    deadline_at: datetime,
    name: str,
    run_id: str,
    namespace: str,
    target_name: str,
    target_uid: str,
    fault_type: str,
    duration_seconds: int,
    intensity: Mapping[str, Any],
    controller_token_ref: str,
    baseline_token_hash: str,
    cleanup_handle: str,
) -> dict[str, Any]:
    return {
        "version": LEDGER_VERSION,
        "created_at": _datetime_iso(created_at),
        "updated_at": _datetime_iso(created_at),
        "state": "pending_apply",
        "experiment_name": name,
        "namespace": namespace,
        "run_id": run_id,
        "target_name": target_name,
        "target_uid": target_uid,
        "fault_type": fault_type,
        "duration_seconds": duration_seconds,
        "intensity": dict(intensity),
        "deadline_at": _datetime_iso(deadline_at),
        "controller_token_ref": controller_token_ref,
        "baseline_gate_token_sha256": baseline_token_hash,
        "cleanup_handle": cleanup_handle,
        "operation_id": cleanup_handle,
    }


def _agent_plan_envelope(config: RuntimeConfig) -> PlanSafetyEnvelope:
    policy = default_policy(set(config.namespace_allowlist))
    allowed_fault_types = (
        tuple(sorted(config.allowed_fault_types))
        if config.allowed_fault_types
        else tuple(sorted(policy.fault_type_contracts))
    )
    return PlanSafetyEnvelope.from_controller_policy(
        policy,
        allowed_fault_types=allowed_fault_types,
    )


def _agent_plan_input(raw_plan: Mapping[str, Any]) -> dict[str, Any]:
    plan = {
        key: deepcopy(raw_plan[key])
        for key in (
            "target",
            "fault_type",
            "intensity",
            "effect_condition",
            "recovery_condition",
            "stop_conditions",
            "safety_ttl_seconds",
            "effect_observation_seconds",
            "effect_sustain_seconds",
            "agent_cleanup_seconds",
            "recovery_observation_seconds",
            "recovery_sustain_seconds",
        )
        if key in raw_plan
    }
    for key in ("effect_condition", "recovery_condition"):
        if isinstance(plan.get(key), Mapping):
            condition = dict(plan[key])
            condition.pop("threshold_tolerance_ratio", None)
            condition.pop("minimum_requests", None)
            plan[key] = condition
    for key, value in CONDITION_POLICY.items():
        if key == "recovery_mode":
            continue
        plan.setdefault(key, value)
    return plan


def _synthetic_agent_plan(raw_plan: Mapping[str, Any]) -> dict[str, Any]:
    """Validate create-only arguments through AgentPlan without approving them."""

    plan = {
        **dict(raw_plan),
        "effect_condition": {
            "metric": "target_latency_ms",
            "operator": "increase_by_at_least",
            "threshold": 0,
        },
        "recovery_condition": {
            "metric": "target_latency_ms",
            "operator": "within_baseline_delta",
            "threshold": 0,
        },
        "stop_conditions": ["controller create request schema validation"],
    }
    for key, value in CONDITION_POLICY.items():
        if key == "recovery_mode":
            continue
        plan.setdefault(key, value)
    return plan


def _request_plan_from_approved(
    approved: Mapping[str, Any],
    *,
    namespace: str,
    target_name: str,
    target_uid: str,
    fault_type: str,
    duration_seconds: int,
    intensity: Any,
) -> dict[str, Any]:
    plan = deepcopy(dict(approved))
    plan.update(
        {
            "target": {
                "namespace": namespace,
                "name": target_name,
                "uid": target_uid,
            },
            "fault_type": fault_type,
            "safety_ttl_seconds": duration_seconds,
            "intensity": deepcopy(intensity),
        }
    )
    return plan


def _mapping_or_raw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return dict(value)
    return deepcopy(value)


def _assert_controller_owned_plan_fields(raw_plan: Mapping[str, Any]) -> None:
    issues: list[dict[str, str]] = []
    recovery_mode = raw_plan.get("recovery_mode")
    if recovery_mode is not None and recovery_mode != CONDITION_POLICY["recovery_mode"]:
        issues.append(
            _plan_field_issue(
                "CONTROLLER_POLICY_MISMATCH",
                "recovery_mode",
                f"recovery_mode must be {CONDITION_POLICY['recovery_mode']}.",
            )
        )
    for name in ("effect_condition", "recovery_condition"):
        condition = raw_plan.get(name)
        if not isinstance(condition, Mapping):
            continue
        if "minimum_requests" in condition:
            issues.append(
                _plan_field_issue(
                    "CONTROLLER_POLICY_FIELD_FORBIDDEN",
                    f"{name}.minimum_requests",
                    "minimum_requests is not part of the current condition policy.",
                )
            )
        tolerance = condition.get("threshold_tolerance_ratio")
        if name == "effect_condition" and tolerance is not None:
            if (
                isinstance(tolerance, bool)
                or not isinstance(tolerance, (int, float))
                or not math.isclose(
                    float(tolerance),
                    EFFECT_THRESHOLD_TOLERANCE_RATIO,
                )
            ):
                issues.append(
                    _plan_field_issue(
                        "CONTROLLER_POLICY_MISMATCH",
                        f"{name}.threshold_tolerance_ratio",
                        (
                            "effect threshold_tolerance_ratio must match the "
                            "Controller fixed policy."
                        ),
                    )
                )
        if name == "recovery_condition" and tolerance is not None:
            issues.append(
                _plan_field_issue(
                    "CONTROLLER_POLICY_FIELD_FORBIDDEN",
                    f"{name}.threshold_tolerance_ratio",
                    "recovery_condition does not accept threshold_tolerance_ratio.",
                )
            )
    if issues:
        raise ChaosControlError(
            "PLAN_SCHEMA_INVALID",
            "Controller-owned plan policy fields were modified.",
            next_step="Use the Controller-issued timing and condition policy exactly; do not add minimum_requests.",
            details={"label": "approved_plan", "issues": issues},
        )


def _plan_field_issue(code: str, path: str, message: str) -> dict[str, str]:
    return {
        "code": code,
        "path": path,
        "message": message,
        "correction": "Use the Controller-issued approved_plan without modifying policy-owned fields.",
    }


def _mismatched_plan_fields(
    approved_plan: AgentPlan,
    requested_plan: AgentPlan,
) -> list[str]:
    approved = approved_plan.model_dump(mode="json")
    requested = requested_plan.model_dump(mode="json")
    return sorted(
        key
        for key in (
            "target",
            "fault_type",
            "intensity",
            "safety_ttl_seconds",
        )
        if approved.get(key) != requested.get(key)
    )


def _create_uncertainty_variant(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    normalized = value.strip().upper()
    if normalized in {"D6A", "D6_A"}:
        return "D6-A"
    if normalized in {"D6B", "D6_B"}:
        return "D6-B"
    if normalized in {"D6-A", "D6-B"}:
        return normalized
    raise ChaosControlError(
        "INVALID_OPERATION_UNCERTAINTY_VARIANT",
        "Create outcome uncertainty variant must be D6-A or D6-B.",
        next_step="Disable the uncertainty injection or set RESBENCH_CHAOS_CREATE_UNCERTAINTY_VARIANT to D6-A or D6-B.",
    )


def _uncertainty_already_injected(ledger: Mapping[str, Any] | None) -> bool:
    return bool(ledger and ledger.get("uncertainty_injected") is True)


def _is_retriable_absent_uncertainty(ledger: Mapping[str, Any] | None) -> bool:
    return bool(
        ledger
        and ledger.get("uncertainty_injected") is True
        and ledger.get("uncertainty_variant") == "D6-A"
        and ledger.get("operation_outcome") == "absent"
        and ledger.get("state") == "operation_outcome_unknown"
    )


def _unknown_outcome_error(ledger: Mapping[str, Any], *, active_owned_count: int = 0) -> ChaosControlError:
    operation_id = str(ledger["operation_id"])
    return ChaosControlError(
        "OPERATION_OUTCOME_UNKNOWN",
        "The create request outcome is intentionally hidden for this D6 operation-outcome uncertainty Trial.",
        next_step=(
            "Call chaos_operation_status with this operation_id before retrying. "
            "Retry chaos_create_experiment only if the status reports operation_outcome=absent."
        ),
        details={
            "operation_id": operation_id,
            "cleanup_handle": str(ledger["cleanup_handle"]),
            "run_id": str(ledger["run_id"]),
            "namespace": str(ledger["namespace"]),
            "experiment_name": str(ledger["experiment_name"]),
            "operation_outcome": "unknown",
            "status_lookup_tool": "chaos_operation_status",
            "status_lookup_tools": (
                "chaos_operation_status",
                "chaos_get_experiment",
                "chaos_inventory_run",
            ),
            "active_owned_count_before_create": active_owned_count,
        },
    )


def _record_matches_ledger(record: ExperimentRecord, ledger: Mapping[str, Any]) -> bool:
    return (
        record.owned
        and record.run_id == ledger["run_id"]
        and record.target_uid == ledger["target_uid"]
        and record.namespace == ledger["namespace"]
        and record.name == ledger["experiment_name"]
    )


def _validate_resource_name(value: str, field_name: str) -> None:
    if not SAFE_NAME_RE.fullmatch(value):
        raise ChaosControlError(
            "INVALID_RESOURCE_NAME",
            f"{field_name} must be a Kubernetes DNS-like resource name.",
            next_step=f"Pass a concrete {field_name}; selectors and shell fragments are not accepted.",
        )


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _duration_delta(duration_seconds: int) -> timedelta:
    return timedelta(seconds=duration_seconds)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _datetime_iso(value: datetime) -> str:
    return _as_utc(value).isoformat()


def _error_code(exc: BaseException) -> str:
    if isinstance(exc, ChaosControlError):
        return exc.code
    return type(exc).__name__


def _ledger_executor_id(ledger: Mapping[str, Any]) -> str:
    value = ledger.get("executor_id")
    if not isinstance(value, str) or not value:
        raise ChaosControlError(
            "LEDGER_EXECUTOR_MISSING",
            "Cleanup ledger has no executor identity; ownership cannot be inferred.",
            next_step="Stop automatic cleanup and reconcile the resource from Controller inventory.",
        )
    return value


def _now_iso() -> str:
    return _now_utc().isoformat()
