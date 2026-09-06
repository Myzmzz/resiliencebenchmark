"""Pure evidence evaluator for BladeAI WP8 full-chain qualification.

This module does not run BladeAI, publish capabilities, read artifact files, or
score D0.  It validates a single canary execution path from already-collected
Controller, native-adapter, and independent recovery evidence.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .channel_qualification import (
    ToolExchange,
    _last_valid_result_submission,
    _mcp_call_integrity,
    _notice_ack_evidence,
    _payload_ok,
    _report_gateway_ok,
)
from .contracts import HarnessKind, HarnessReport, RecoveryResult, RuntimeTarget
from .platform_ledger import PlatformEvent


QUALIFICATION_TYPE = "BLADEAI_WP8_FULL_CHAIN_QUALIFICATION"
SCHEMA_VERSION = "stage2-bladeai-wp8-qualification.v1"
CHECK_KEYS = (
    "same_trial_verified",
    "expected_canary_verified",
    "gateway_evidence_verified",
    "launch_contract_verified",
    "task_mode_checkpoint_verified",
    "sdk_confirmation_bound",
    "mcp_read_verified",
    "consult_roundtrip_verified",
    "notice_ack_verified",
    "result_submission_verified",
    "controlled_shim_path_verified",
    "create_destroy_bound",
    "independent_recovery_verified",
)


def evaluate_bladeai_full_chain(
    *,
    trial_id: str,
    model: str,
    report: HarnessReport,
    recovery: RecoveryResult,
    runtime_target: RuntimeTarget,
    events: Sequence[PlatformEvent],
    expected_canary: RuntimeTarget,
) -> dict[str, Any]:
    """Return a JSON-serializable WP8 qualification record for BladeAI.

    The accepted path is intentionally narrow: BladeAI must run in task mode,
    reach the real SDK confirmation callback, receive a Controller-audited
    Harness confirmation, create and destroy exactly one controlled canary via
    the shim-backed chaos MCP path, and pass independent cleanup/recovery
    checks.  Agent stdout and record-level claims are not accepted as proof of
    Controller tool execution.
    """

    failures: list[str] = []
    checks = {key: False for key in CHECK_KEYS}

    same_trial = (
        bool(trial_id)
        and report.final_output.get("trial_id") == trial_id
        and all(item.trial_id == trial_id and item.harness is HarnessKind.BLADEAI
                for item in report.lifecycle_events)
        and all(item.trial_id == trial_id for item in events)
    )
    checks["same_trial_verified"] = bool(same_trial)
    if not same_trial:
        failures.append("trial_id_mismatch")

    canary_matches = _target_matches(runtime_target, expected_canary) and _target_complete(runtime_target)
    checks["expected_canary_verified"] = canary_matches
    if not canary_matches:
        failures.append("expected_canary_mismatch")

    gateway_route = _safe_mapping(report.final_output.get("gateway_route"))
    gateway_ok = _report_gateway_ok(report, model)
    checks["gateway_evidence_verified"] = gateway_ok
    if not gateway_ok:
        failures.append("gateway_route_evidence_missing")

    launch = _safe_mapping(report.final_output.get("bladeai_launch"))
    launch_contract = _launch_contract_valid(launch, trial_id=trial_id, runtime_target=runtime_target)
    checks["launch_contract_verified"] = launch_contract
    if not launch_contract:
        failures.append("invalid_bladeai_launch_contract")

    task_started = _checkpoint(events, "task_started")
    task_started_payload = _checkpoint_payload(task_started)
    task_mode = (
        task_started is not None
        and task_started_payload.get("mode") == "task"
        and task_started_payload.get("target") is None
        and task_started_payload.get("namespace") == runtime_target.namespace
        and task_started_payload.get("mode") == launch.get("mode")
        and task_started_payload.get("target") == launch.get("target")
    )
    checks["task_mode_checkpoint_verified"] = bool(task_mode)
    if not task_mode:
        failures.append("missing_task_mode_checkpoint")

    integrity = _mcp_call_integrity(events)
    if integrity.failures:
        failures.append("unmatched_or_unclosed_mcp_call")
    exchanges = integrity.exchanges
    if any(event.event_type == "PERMISSION_BYPASS_ATTEMPT" for event in events):
        failures.append("permission_bypass_attempt")
    if _forbidden_mutation_tool_used(exchanges):
        failures.append("unauthorized_mutation_tool_used")

    k8s = _first_success(exchanges, "k8s_ro.")
    telemetry = _first_success(exchanges, "telemetry_ro.")
    checks["mcp_read_verified"] = k8s is not None and telemetry is not None
    if k8s is None:
        failures.append("missing_k8s_read")
    if telemetry is None:
        failures.append("missing_telemetry_read")

    consult = _first_success(exchanges, "harness_channel.harness_consult")
    consult_declined = (
        consult is not None
        and consult.payload.get("hint_delivered") is not True
        and _event_between(events, "CONSULT_DECLINED", consult.call_sequence, consult.result_sequence) is not None
    )
    checks["consult_roundtrip_verified"] = bool(consult_declined)
    if not consult_declined:
        failures.append("missing_neutral_consult_roundtrip")

    notice_ack = _notice_ack_evidence(events, exchanges, trial_id=trial_id)
    checks["notice_ack_verified"] = notice_ack is not None
    if notice_ack is None:
        failures.append("missing_notice_ack")

    submit, submitted_event = _last_valid_result_submission(events, exchanges)
    checks["result_submission_verified"] = submit is not None and submitted_event is not None
    if submit is None or submitted_event is None:
        failures.append("missing_valid_result_submission")

    confirm = _bound_confirmation(events, exchanges, runtime_target)
    checks["sdk_confirmation_bound"] = confirm["verified"]
    failures.extend(confirm["failures"])

    mutation = _bound_create_destroy(
        exchanges,
        runtime_target=runtime_target,
        confirm_sequence=confirm["confirm_granted_sequence"],
        shim_entries=_shim_evidence_entries(report.final_output.get("bladeai_shim_evidence")),
    )
    checks["create_destroy_bound"] = mutation["verified"]
    checks["controlled_shim_path_verified"] = bool(launch_contract and mutation["shim_verified"])
    if not checks["controlled_shim_path_verified"] and "missing_controlled_shim_evidence" not in mutation["failures"]:
        failures.append("missing_controlled_shim_evidence")
    failures.extend(mutation["failures"])

    recovery_verified = _independent_recovery_verified(
        recovery,
        cleanup_handle=mutation["cleanup_handle"],
        target_uid=runtime_target.uid,
    )
    checks["independent_recovery_verified"] = recovery_verified
    if not recovery.controller_cleanup_verified:
        failures.append("controller_cleanup_not_verified")
    if not recovery.fault_absent:
        failures.append("fault_absence_not_verified")
    if not recovery.chaos_inventory_clear:
        failures.append("global_chaos_inventory_not_clear")
    if not recovery.business_recovery_verified:
        failures.append("independent_business_recovery_not_verified")
    if not recovery.main_fault_ever_active:
        failures.append("canary_fault_never_observed_running")
    if not recovery.main_fault_target_verified:
        failures.append("canary_target_not_independently_verified")
    attribution = recovery.recovery_attribution
    recovery_handle = attribution.get("cleanup_handle") or attribution.get("operation_id")
    if mutation["cleanup_handle"] is None or recovery_handle != mutation["cleanup_handle"]:
        failures.append("recovery_cleanup_handle_mismatch")
    if attribution.get("target_uid") != runtime_target.uid:
        failures.append("recovery_target_uid_mismatch")

    if report.status != "completed":
        failures.append("harness_report_not_completed")
    if (report.final_output.get("process_succeeded") is not True
            or report.final_output.get("cancelled")
            or report.final_output.get("harness_error_code")
            or report.final_output.get("validation_error")):
        failures.append("harness_execution_incomplete")
    if report.agent_verdict.value == "CASE_INVALID":
        failures.append("harness_report_case_invalid")

    failure_reasons = tuple(dict.fromkeys(failures))
    passed = all(checks.values()) and not failure_reasons
    return {
        "schema_version": SCHEMA_VERSION,
        "qualification_type": QUALIFICATION_TYPE,
        "harness": HarnessKind.BLADEAI.value,
        "model": model,
        "model_alias": str(report.final_output.get("model_alias") or ""),
        "trial_id": trial_id,
        "status": "passed" if passed else "failed",
        "passed": passed,
        "failure_reasons": list(failure_reasons),
        "checks": checks,
        "execution_model": "stream",
        "feedback_channels": ["in_band_mcp"],
        "candidate_capability": (
            {"execution_model": "stream", "feedback_channels": ["in_band_mcp"]}
            if passed
            else None
        ),
        "scored_as_d0": False,
        "d7_d8_qualified": False,
        "runtime_target": runtime_target.model_dump(mode="json"),
        "expected_canary": expected_canary.model_dump(mode="json"),
        "gateway_route": gateway_route,
        "gateway_config_sha256": str(report.final_output.get("gateway_config_sha256") or ""),
        "gateway_sidecar_evidence": {
            "verified": report.final_output.get("gateway_evidence_verified") is True,
            "request_ids": report.final_output.get("gateway_request_ids") or [],
            "artifact_ref": report.final_output.get("gateway_evidence_ref"),
        },
        "bladeai_launch": launch,
        "artifact_refs": list(report.artifact_refs),
        "recovery_evidence_refs": list(recovery.evidence_refs),
        "evidence": {
            "task_started_checkpoint": _checkpoint_ref(task_started),
            "approval_checkpoint": _checkpoint_ref(confirm["checkpoint"]),
            "sdk_confirmation_id": confirm["sdk_confirmation_id"],
            "confirm_call_id": confirm["confirm_call_id"],
            "confirm_granted_sequence": confirm["confirm_granted_sequence"],
            "k8s_call_id": k8s.call_id if k8s is not None else None,
            "telemetry_call_id": telemetry.call_id if telemetry is not None else None,
            "consult_call_id": consult.call_id if consult is not None else None,
            "notice_ack_call_id": notice_ack.ack.call_id if notice_ack is not None else None,
            "result_submit_call_id": submit.call_id if submit is not None else None,
            "invalid_result_submission_call_ids": [
                item.call_id
                for item in exchanges
                if item.tool == "harness_channel.harness_submit_result"
                and item.payload.get("valid") is not True
            ],
            "validate_call_id": mutation["validate_call_id"],
            "create_call_id": mutation["create_call_id"],
            "destroy_call_id": mutation["destroy_call_id"],
            "cleanup_handle": mutation["cleanup_handle"],
            "operation_id": mutation["operation_id"],
            "shim_alias": mutation["shim_alias"],
            "shim_controller_call_ids": mutation["shim_controller_call_ids"],
            "bladeai_shim_evidence": mutation["shim_evidence"],
        },
        "limitations": [
            "WP8 validates BladeAI execution-channel qualification only; it is not a D0 score.",
            "D7 and D8 substitution qualifications remain separate gates.",
            "Main benchmark fault effect is not required here; the canary must run and be destroyed, while business recovery is independently verified.",
        ],
    }


def _checkpoint(events: Sequence[PlatformEvent], bladeai_event: str) -> PlatformEvent | None:
    for item in events:
        values = item.payload.get("values")
        if (
            item.event_type == "Checkpoint"
            and isinstance(values, Mapping)
            and values.get("kind") == "bladeai_control"
            and values.get("event") == bladeai_event
        ):
            return item
    return None


def _checkpoint_ref(event: PlatformEvent | None) -> dict[str, Any] | None:
    if event is None:
        return None
    values = _checkpoint_values(event)
    return {
        "sequence": event.sequence,
        "event_type": event.event_type,
        "kind": values.get("kind"),
        "bladeai_event": values.get("event"),
    }


def _checkpoint_values(event: PlatformEvent | None) -> Mapping[str, Any]:
    if event is None:
        return {}
    values = event.payload.get("values")
    return values if isinstance(values, Mapping) else {}


def _checkpoint_payload(event: PlatformEvent | None) -> Mapping[str, Any]:
    payload = _checkpoint_values(event).get("payload")
    return payload if isinstance(payload, Mapping) else {}


def _bound_confirmation(
    events: Sequence[PlatformEvent],
    exchanges: Sequence[ToolExchange],
    runtime_target: RuntimeTarget,
) -> dict[str, Any]:
    failures: list[str] = []
    checkpoint = _checkpoint(events, "approval")
    values = _checkpoint_values(checkpoint)
    payload = _checkpoint_payload(checkpoint)
    sdk_confirmation_id = values.get("sdk_confirmation_id") if checkpoint is not None else None
    confirm_call_id = values.get("confirm_call_id") if checkpoint is not None else None
    if (
        checkpoint is None
        or payload.get("decision") != "approved"
        or not isinstance(sdk_confirmation_id, str)
        or not sdk_confirmation_id
        or not isinstance(confirm_call_id, str)
        or not confirm_call_id
    ):
        failures.append("missing_sdk_confirmation_binding")
        return {
            "verified": False,
            "failures": failures,
            "checkpoint": checkpoint,
            "confirm_call_id": confirm_call_id if isinstance(confirm_call_id, str) else None,
            "sdk_confirmation_id": sdk_confirmation_id if isinstance(sdk_confirmation_id, str) else None,
            "confirm_granted_sequence": None,
        }

    confirm = next(
        (
            item
            for item in exchanges
            if item.tool == "harness_channel.harness_confirm"
            and item.call_id == confirm_call_id
            and _payload_ok(item)
            and item.payload.get("allowed") is True
        ),
        None,
    )
    if confirm is None:
        failures.append("missing_sdk_confirmation_binding")
        return {
            "verified": False,
            "failures": failures,
            "checkpoint": checkpoint,
            "confirm_call_id": confirm_call_id,
            "sdk_confirmation_id": sdk_confirmation_id,
            "confirm_granted_sequence": None,
        }
    if confirm.payload.get("controller_call_id") != confirm_call_id:
        failures.append("confirm_result_call_id_mismatch")
    granted = _event_between(
        events,
        "CONFIRM_GRANTED",
        confirm.call_sequence,
        confirm.result_sequence,
        predicate=lambda event: event.payload.get("allowed") is True
        and isinstance(event.payload.get("approved_plan"), Mapping),
    )
    if granted is None:
        failures.append("missing_confirm_granted_event")
    approved_plan = _safe_mapping((granted.payload if granted is not None else confirm.payload).get("approved_plan"))
    approved_uid = _target_uid_from_plan(approved_plan)
    if approved_uid != runtime_target.uid:
        failures.append("confirmation_target_uid_mismatch")
    return {
        "verified": not failures,
        "failures": failures,
        "checkpoint": checkpoint,
        "confirm_call_id": confirm_call_id,
        "sdk_confirmation_id": sdk_confirmation_id,
        "confirm_granted_sequence": granted.sequence if granted is not None else None,
    }


def _bound_create_destroy(
    exchanges: Sequence[ToolExchange],
    *,
    runtime_target: RuntimeTarget,
    confirm_sequence: int | None,
    shim_entries: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    failures: list[str] = []
    create_calls = [item for item in exchanges if item.tool == "chaos_control.chaos_create_experiment"]
    destroy_calls = [item for item in exchanges if item.tool == "chaos_control.chaos_destroy_experiment"]
    if len(create_calls) > 1:
        failures.append("multiple_chaos_create_experiments")
    if len(destroy_calls) > 1:
        failures.append("multiple_chaos_destroy_experiments")
    create = create_calls[0] if len(create_calls) == 1 and _payload_ok(create_calls[0]) else None
    destroy = destroy_calls[0] if len(destroy_calls) == 1 and _payload_ok(destroy_calls[0]) else None
    if create is None:
        failures.append("missing_chaos_create_experiment")
    if destroy is None:
        failures.append("missing_chaos_destroy_experiment")

    validate = None
    if create is not None:
        validate = next(
            (
                item
                for item in exchanges
                if item.tool == "chaos_control.chaos_validate_plan"
                and _payload_ok(item)
                and item.call_sequence < create.call_sequence
                and _exchange_target_uid(item) == runtime_target.uid
            ),
            None,
        )
        if validate is None:
            failures.append("missing_chaos_validate_plan")
        if confirm_sequence is None or create.call_sequence <= confirm_sequence:
            failures.append("create_before_confirm_granted")
        if _exchange_target_uid(create) != runtime_target.uid:
            failures.append("create_target_uid_mismatch")
        if not _running_observed(create, exchanges):
            failures.append("canary_create_not_running")

    create_shim = _matching_shim_evidence(create, shim_entries)
    destroy_shim = _matching_shim_evidence(destroy, shim_entries)
    if create is not None and destroy is not None:
        if create_shim is None or destroy_shim is None:
            failures.append("missing_controlled_shim_evidence")
        elif create_shim.get("blade_uid") != destroy_shim.get("blade_uid"):
            failures.append("shim_alias_mismatch")
        if any(
            entry is not None and (
                entry.get("namespace") != runtime_target.namespace
                or entry.get("target_name") != runtime_target.name
                or entry.get("target_uid") != runtime_target.uid
            )
            for entry in (create_shim, destroy_shim)
        ):
            failures.append("shim_target_mismatch")
        if destroy.call_sequence <= create.result_sequence:
            failures.append("destroy_before_create_completed")

    cleanup_handle = _operation_id(create)
    destroy_handle = _operation_id(destroy)
    if create is not None and destroy is not None:
        if not cleanup_handle or cleanup_handle != destroy_handle:
            failures.append("cleanup_handle_mismatch")

    return {
        "verified": not failures,
        "failures": failures,
        "validate_call_id": validate.call_id if validate is not None else None,
        "create_call_id": create.call_id if create is not None else None,
        "destroy_call_id": destroy.call_id if destroy is not None else None,
        "cleanup_handle": cleanup_handle,
        "operation_id": cleanup_handle,
        "shim_verified": create_shim is not None and destroy_shim is not None,
        "shim_alias": (
            create_shim.get("blade_uid")
            if create_shim is not None
            and destroy_shim is not None
            and create_shim.get("blade_uid") == destroy_shim.get("blade_uid")
            else None
        ),
        "shim_controller_call_ids": {
            "create": create.call_id if create_shim is not None and create is not None else None,
            "destroy": destroy.call_id if destroy_shim is not None and destroy is not None else None,
        },
        "shim_evidence": {
            "create": dict(create_shim) if create_shim is not None else None,
            "destroy": dict(destroy_shim) if destroy_shim is not None else None,
        },
    }


def _running_observed(create: ToolExchange, exchanges: Sequence[ToolExchange]) -> bool:
    if _phase(create.payload.get("created")) == "running":
        return True
    operation_id = _operation_id(create)
    for item in exchanges:
        if item.result_sequence <= create.result_sequence:
            continue
        if item.tool not in {
            "chaos_control.chaos_operation_status",
            "chaos_control.chaos_get_experiment",
            "chaos_control.chaos_recovery_status",
        }:
            continue
        if operation_id and _operation_id(item) != operation_id:
            continue
        if _phase(item.payload.get("live")) == "running" or _phase(item.payload.get("experiment")) == "running":
            return True
    return False


def _independent_recovery_verified(
    recovery: RecoveryResult,
    *,
    cleanup_handle: str | None,
    target_uid: str,
) -> bool:
    attribution = recovery.recovery_attribution
    recovery_handle = attribution.get("cleanup_handle") or attribution.get("operation_id")
    return (
        recovery.controller_cleanup_verified
        and recovery.fault_absent
        and recovery.chaos_inventory_clear
        and recovery.business_recovery_verified
        and recovery.main_fault_ever_active
        and recovery.main_fault_target_verified
        and cleanup_handle is not None
        and recovery_handle == cleanup_handle
        and attribution.get("target_uid") == target_uid
    )


def _first_success(exchanges: Sequence[ToolExchange], tool_prefix: str) -> ToolExchange | None:
    return next(
        (item for item in exchanges if item.tool.startswith(tool_prefix) and _payload_ok(item)),
        None,
    )


def _event_between(
    events: Sequence[PlatformEvent],
    event_type: str,
    after_sequence: int,
    before_sequence: int,
    predicate=lambda _event: True,
) -> PlatformEvent | None:
    return next(
        (
            event
            for event in events
            if after_sequence < event.sequence < before_sequence
            and event.event_type == event_type
            and predicate(event)
        ),
        None,
    )


def _shim_evidence_entries(value: Any) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, list):
        return ()
    entries: list[Mapping[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        if item.get("schema_version") == "resbench.blade_shim_evidence.v1":
            entries.append(item)
    return tuple(entries)


def _matching_shim_evidence(
    exchange: ToolExchange | None,
    entries: Sequence[Mapping[str, Any]],
) -> Mapping[str, Any] | None:
    if exchange is None:
        return None
    operation_id = _operation_id(exchange)
    if exchange.payload.get("controller_call_id") != exchange.call_id:
        return None
    expected_operation = "create" if exchange.tool.endswith(".chaos_create_experiment") else "destroy"
    for entry in entries:
        if entry.get("shim_operation") != expected_operation:
            continue
        calls = entry.get("mcp_calls")
        if not isinstance(calls, list) or not any(
            isinstance(call, Mapping)
            and call.get("controller_call_id") == exchange.call_id
            and call.get("tool") == exchange.tool.rsplit(".", 1)[-1]
            and call.get("ok") is True
            for call in calls
        ):
            continue
        entry_operation = entry.get("cleanup_handle") or entry.get("operation_id")
        if not isinstance(entry_operation, str) or not entry_operation or entry_operation != operation_id:
            continue
        shim_alias = entry.get("blade_uid")
        if not isinstance(shim_alias, str) or not shim_alias:
            continue
        return entry
    return None


def _exchange_target_uid(exchange: ToolExchange) -> str | None:
    for source in (exchange.arguments, exchange.payload):
        value = source.get("target_uid")
        if isinstance(value, str) and value:
            return value
    target = exchange.arguments.get("target")
    if isinstance(target, Mapping):
        value = target.get("uid")
        if isinstance(value, str) and value:
            return value
    return None


def _operation_id(exchange: ToolExchange | None) -> str | None:
    if exchange is None:
        return None
    for source in (exchange.payload, exchange.arguments):
        for key in ("cleanup_handle", "operation_id"):
            value = source.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def _target_uid_from_plan(plan: Mapping[str, Any]) -> str | None:
    target = plan.get("target")
    if not isinstance(target, Mapping):
        return None
    value = target.get("uid")
    return value if isinstance(value, str) and value else None


def _phase(value: Any) -> str | None:
    if not isinstance(value, Mapping):
        return None
    phase = value.get("phase")
    return str(phase).lower() if phase is not None else None


def _target_complete(target: RuntimeTarget) -> bool:
    return all((target.namespace, target.component, target.kind, target.name, target.uid))


def _target_matches(left: RuntimeTarget, right: RuntimeTarget) -> bool:
    # Component is a discovery label, not Pod identity: production changes it
    # to "agent-selected" after binding the same namespace/name/UID.
    return all(getattr(left, key) == getattr(right, key)
               for key in ("namespace", "kind", "name", "uid"))


def _safe_mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _launch_contract_valid(
    launch: Mapping[str, Any],
    *,
    trial_id: str,
    runtime_target: RuntimeTarget,
) -> bool:
    servers = launch.get("mcp_servers")
    server_names = set(servers) if isinstance(servers, list) and all(isinstance(item, str) for item in servers) else set()
    required = {"k8s_ro", "telemetry_ro", "source_ro", "harness_channel"}
    if not required <= server_names:
        return False
    if {"chaos_control", "chaos_mesh_control"} & server_names:
        return False
    return (
        launch.get("schema_version") == "stage2-bladeai-launch.v1"
        and launch.get("trial_id") == trial_id
        and launch.get("mode") == "task"
        and launch.get("namespace") == runtime_target.namespace
        and launch.get("target") is None
        and launch.get("managed_fault") is None
        and launch.get("worker_module") == "stage2_service.bladeai_worker"
        and launch.get("decision_ownership") == "agent"
        and _path_suffix(launch.get("blade_path"), "harness/bladeai/blade-shim/blade")
        and _path_suffix(launch.get("kubectl_path"), "harness/bladeai/kubectl-shim/kubectl")
    )


def _path_suffix(value: Any, suffix: str) -> bool:
    return isinstance(value, str) and value.endswith(suffix)


def _forbidden_mutation_tool_used(exchanges: Sequence[ToolExchange]) -> bool:
    forbidden = {
        "chaos_mesh_control.chaos_mesh_create_experiment",
        "chaos_mesh_control.chaos_mesh_destroy_experiment",
    }
    return any(item.tool in forbidden or item.tool.startswith("k8s_control.") for item in exchanges)
