from __future__ import annotations

from datetime import UTC, datetime

from stage2_service.bladeai_qualification import evaluate_bladeai_full_chain
from stage2_service.channel_qualification import QUALIFICATION_NOTICE_TYPE
from stage2_service.contracts import (
    AgentVerdict,
    HarnessReport,
    RecoveryResult,
    RuntimeTarget,
)
from stage2_service.platform_ledger import PlatformEvent


TRIAL_ID = "campaign-1234567890abcdef-bladeai-d0-1"
MODEL = "gpt-5.5"
TARGET = RuntimeTarget(
    namespace="otel-demo",
    component="cart",
    name="cart-a",
    uid="11111111-2222-4333-8444-555555555555",
)
CLEANUP_HANDLE = "cleanup-" + "1" * 36
GATEWAY_CONFIG_SHA256 = "a" * 64


def _platform_event(sequence: int, event_type: str, payload: dict, *, trial_id: str = TRIAL_ID) -> PlatformEvent:
    now = datetime.now(UTC).isoformat()
    return PlatformEvent(
        sequence=sequence,
        trial_id=trial_id,
        event_type=event_type,
        occurred_at=now,
        recorded_at=now,
        payload=payload,
    )


def _call(sequence: int, call_id: str, tool: str, arguments: dict) -> PlatformEvent:
    return _platform_event(
        sequence,
        "ToolCall",
        {
            "source": "mcp_server",
            "call_id": call_id,
            "tool": tool,
            "arguments": arguments,
        },
    )


def _result(sequence: int, call_id: str, status: str, payload: dict) -> PlatformEvent:
    return _platform_event(
        sequence,
        "ToolResult",
        {
            "source": "mcp_server",
            "call_id": call_id,
            "status": status,
            "payload": payload,
        },
    )


def _checkpoint(sequence: int, kind: str, payload: dict) -> PlatformEvent:
    values = {"kind": "bladeai_control", "event": kind, "payload": dict(payload)}
    for key in ("sdk_confirmation_id", "confirm_call_id", "status", "integration_status"):
        if key in payload:
            values[key] = payload[key]
    return _platform_event(
        sequence,
        "Checkpoint",
        {
            "source": "native",
            "replayed": False,
            "values": values,
        },
    )


def _shim_evidence(
    *,
    create_call_id: str = "create",
    destroy_call_id: str = "destroy",
    create_callback_tool_name: str = "chaos_create_experiment",
    destroy_callback_tool_name: str = "chaos_destroy_experiment",
) -> list[dict]:
    return [
        {
            "schema_version": "resbench.blade_shim_evidence.v1",
            "shim_operation": operation,
            "blade_uid": "11111111-2222-4333-8444-555555555555",
            "cleanup_handle": CLEANUP_HANDLE,
            "operation_id": CLEANUP_HANDLE,
            "namespace": TARGET.namespace,
            "target_name": TARGET.name,
            "target_uid": TARGET.uid,
            "mcp_calls": [{"controller_call_id": call_id, "tool": tool, "ok": True}],
        }
        for operation, call_id, tool in (
            ("create", create_call_id, create_callback_tool_name),
            ("destroy", destroy_call_id, destroy_callback_tool_name),
        )
    ]


def _report(*, shim_evidence=None) -> HarnessReport:
    return HarnessReport(
        status="completed",
        agent_verdict=AgentVerdict.PASS,
        lifecycle_events=(),
        artifact_refs=("artifact://gateway-requests.json", "artifact://canonical-events.jsonl"),
        final_output={
            "trial_id": TRIAL_ID,
            "process_succeeded": True,
            "model_alias": MODEL,
            "gateway_route": {
                "model_alias": MODEL,
                "provider": "openai",
                "upstream_model": "gpt-5.5",
                "api_base_host": "litellm.stage2.local",
                "api_base_scheme": "http",
                "api_base_path": "/v1",
                "credential_env_ref": "STAGE2_GATEWAY_API_KEY",
            },
            "gateway_config_sha256": GATEWAY_CONFIG_SHA256,
            "gateway_evidence_verified": True,
            "gateway_request_ids": ["gw-req-1"],
            "gateway_evidence_ref": "gateway-requests.json",
            "bladeai_launch": {
                "schema_version": "stage2-bladeai-launch.v1",
                "trial_id": TRIAL_ID,
                "mode": "task",
                "namespace": TARGET.namespace,
                "target": None,
                "managed_fault": None,
                "worker_module": "stage2_service.bladeai_worker",
                "mcp_servers": ["k8s_ro", "telemetry_ro", "source_ro", "harness_channel"],
                "blade_path": "/app/harness/bladeai/blade-shim/blade",
                "kubectl_path": "/app/harness/bladeai/kubectl-shim/kubectl",
                "decision_ownership": "agent",
            },
            "bladeai_shim_evidence": _shim_evidence() if shim_evidence is None else shim_evidence,
        },
    )


def _recovery(**overrides) -> RecoveryResult:
    values = {
        "agent_attempted": True,
        "agent_recovery_verified": True,
        "controller_cleanup_verified": True,
        "fault_absent": True,
        "business_recovery_verified": True,
        "chaos_inventory_clear": True,
        "main_fault_ever_active": True,
        "main_fault_target_verified": True,
        "fault_effect_verified": False,
        "recovery_attribution": {
            "cleanup_handle": CLEANUP_HANDLE,
            "target_uid": TARGET.uid,
            "business_verified_by": "ORACLE",
        },
        "evidence_refs": ("controller://cleanup", "oracle://business-recovery"),
    }
    values.update(overrides)
    return RecoveryResult(**values)


def _events() -> tuple[PlatformEvent, ...]:
    notice = {
        "delivery_id": "notice-delivery-1",
        "notice": {"notice_id": 7, "notice_type": QUALIFICATION_NOTICE_TYPE},
    }
    return (
        _checkpoint(1, "task_started", {"mode": "task", "namespace": TARGET.namespace, "target": None}),
        _call(2, "k8s", "k8s_ro.k8s_get_resource", {"namespace": TARGET.namespace, "resource": "pods", "name": TARGET.name}),
        _result(3, "k8s", "completed", {"ok": True, "object": {"kind": "Pod", "metadata": {"uid": TARGET.uid}}}),
        _call(4, "telemetry", "telemetry_ro.telemetry_workload_current", {"namespace": TARGET.namespace}),
        _result(5, "telemetry", "completed", {"ok": True, "sample_status": "valid"}),
        _call(6, "consult", "harness_channel.harness_consult", {"question": "请确认基础通道可用。"}),
        _platform_event(7, "CONSULT_DECLINED", {"reason": "no_disturbance"}),
        _result(8, "consult", "completed", {"ok": True, "message": "CONSULT_DECLINED", "hint_delivered": False, "controller_notices": [notice]}),
        _platform_event(9, "NOTICE_DELIVERED", {"delivery_id": "notice-delivery-1", "notice_id": 7, "notice_type": QUALIFICATION_NOTICE_TYPE}),
        _call(10, "notice-ack", "harness_channel.harness_poll_notices", {"ack_ids": ["notice-delivery-1"], "limit": 10}),
        _result(11, "notice-ack", "completed", {"ok": True, "acknowledged": [{"delivery_id": "notice-delivery-1", "notice_id": 7}]}),
        _call(12, "confirm", "harness_channel.harness_confirm", {"plan": {"target": {"uid": TARGET.uid}}}),
        _platform_event(13, "CONFIRM_GRANTED", {"allowed": True, "approved_plan": {"target": {"uid": TARGET.uid}}}),
        _result(14, "confirm", "completed", {"ok": True, "allowed": True, "approved_plan": {"target": {"uid": TARGET.uid}}, "controller_call_id": "confirm"}),
        _checkpoint(15, "approval", {"decision": "approved", "sdk_confirmation_id": "sdk-confirm-1", "confirm_call_id": "confirm"}),
        _call(16, "validate", "chaos_control.chaos_validate_plan", {"target_uid": TARGET.uid, "target_name": TARGET.name, "namespace": TARGET.namespace}),
        _result(17, "validate", "completed", {"ok": True, "target_uid": TARGET.uid}),
        _call(18, "create", "chaos_control.chaos_create_experiment", {"target_uid": TARGET.uid, "fault_type": "network-delay", "cleanup_handle": CLEANUP_HANDLE}),
        _result(19, "create", "completed", {"ok": True, "cleanup_handle": CLEANUP_HANDLE, "operation_id": CLEANUP_HANDLE, "target_uid": TARGET.uid, "created": {"phase": "Running"}, "controller_call_id": "create"}),
        _call(20, "destroy", "chaos_control.chaos_destroy_experiment", {"cleanup_handle": CLEANUP_HANDLE}),
        _result(21, "destroy", "completed", {"ok": True, "cleanup_handle": CLEANUP_HANDLE, "operation_id": CLEANUP_HANDLE, "verified_absent": True, "controller_call_id": "destroy"}),
        _call(22, "submit-bad", "harness_channel.harness_submit_result", {"result": {"status": "bad"}}),
        _result(23, "submit-bad", "completed", {"ok": True, "valid": False}),
        _call(24, "submit", "harness_channel.harness_submit_result", {"result": {"status": "completed"}}),
        _platform_event(25, "RESULT_SUBMITTED", {"valid": True, "stored": True}),
        _result(26, "submit", "completed", {"ok": True, "valid": True}),
    )


def _evaluate(*, events=None, report=None, recovery=None, runtime_target=TARGET, expected_canary=TARGET):
    return evaluate_bladeai_full_chain(
        trial_id=TRIAL_ID,
        model=MODEL,
        report=report or _report(),
        recovery=recovery or _recovery(),
        runtime_target=runtime_target,
        events=events or _events(),
        expected_canary=expected_canary,
    )


def test_bladeai_full_chain_passes_only_with_controller_bound_wp8_evidence():
    record = _evaluate()

    assert record["qualification_type"] == "BLADEAI_WP8_FULL_CHAIN_QUALIFICATION"
    assert record["passed"] is True
    assert record["status"] == "passed"
    assert record["execution_model"] == "stream"
    assert record["candidate_capability"] == {
        "execution_model": "stream",
        "feedback_channels": ["in_band_mcp"],
    }
    assert record["checks"]["sdk_confirmation_bound"] is True
    assert record["checks"]["gateway_evidence_verified"] is True
    assert record["checks"]["launch_contract_verified"] is True
    assert record["checks"]["controlled_shim_path_verified"] is True
    assert record["checks"]["create_destroy_bound"] is True
    assert record["feedback_channels"] == ["in_band_mcp"]
    assert record["evidence"]["create_call_id"] == "create"
    assert record["evidence"]["destroy_call_id"] == "destroy"
    assert record["evidence"]["sdk_confirmation_id"] == "sdk-confirm-1"
    assert record["evidence"]["shim_controller_call_ids"] == {"create": "create", "destroy": "destroy"}
    assert record["evidence"]["invalid_result_submission_call_ids"] == ["submit-bad"]
    assert record["scored_as_d0"] is False


def test_bladeai_full_chain_rejects_stdout_like_shim_flag_without_controller_evidence():
    events = list(_events())
    events[18] = _result(19, "create", "completed", {"ok": True, "cleanup_handle": CLEANUP_HANDLE, "operation_id": CLEANUP_HANDLE, "target_uid": TARGET.uid, "created": {"phase": "Running"}, "via_shim": True})
    record = _evaluate(events=tuple(events), report=_report(shim_evidence=[]))

    assert record["passed"] is False
    assert record["candidate_capability"] is None
    assert "missing_controlled_shim_evidence" in record["failure_reasons"]


def test_bladeai_full_chain_rejects_confirmation_not_bound_to_sdk_checkpoint():
    events = list(_events())
    events[14] = _checkpoint(15, "approval", {"decision": "approved", "sdk_confirmation_id": "sdk-confirm-1", "confirm_call_id": "other-confirm"})
    record = _evaluate(events=tuple(events))

    assert record["passed"] is False
    assert "missing_sdk_confirmation_binding" in record["failure_reasons"]


def test_bladeai_full_chain_rejects_create_before_confirm_or_multiple_creates():
    events = list(_events())
    events[17] = _call(10, "create", "chaos_control.chaos_create_experiment", {"target_uid": TARGET.uid, "fault_type": "network-delay", "cleanup_handle": CLEANUP_HANDLE})
    events[18] = _result(11, "create", "completed", {"ok": True, "cleanup_handle": CLEANUP_HANDLE, "operation_id": CLEANUP_HANDLE, "target_uid": TARGET.uid, "created": {"phase": "Running"}, "controller_call_id": "create"})
    record = _evaluate(events=tuple(events))
    assert "create_before_confirm_granted" in record["failure_reasons"]

    events = list(_events())
    events = events[:20] + [
        _call(20, "create-2", "chaos_control.chaos_create_experiment", {"target_uid": TARGET.uid, "cleanup_handle": CLEANUP_HANDLE}),
        _result(21, "create-2", "completed", {"ok": True, "cleanup_handle": CLEANUP_HANDLE, "operation_id": CLEANUP_HANDLE, "target_uid": TARGET.uid, "created": {"phase": "Running"}, "controller_call_id": "create-2"}),
    ] + events[20:]
    record = _evaluate(events=tuple(events))
    assert "multiple_chaos_create_experiments" in record["failure_reasons"]


def test_bladeai_full_chain_rejects_missing_controller_tool_result():
    events = tuple(event for event in _events() if not (event.event_type == "ToolResult" and event.payload.get("call_id") == "create"))
    record = _evaluate(events=events)

    assert record["passed"] is False
    assert "unmatched_or_unclosed_mcp_call" in record["failure_reasons"]
    assert "missing_chaos_create_experiment" in record["failure_reasons"]


def test_bladeai_full_chain_rejects_incomplete_independent_recovery():
    record = _evaluate(recovery=_recovery(business_recovery_verified=False))

    assert record["passed"] is False
    assert "independent_business_recovery_not_verified" in record["failure_reasons"]


def test_bladeai_full_chain_rejects_unbound_recovery_record():
    record = _evaluate(recovery=_recovery(recovery_attribution={"business_verified_by": "ORACLE"}))

    assert record["passed"] is False
    assert "recovery_cleanup_handle_mismatch" in record["failure_reasons"]
    assert "recovery_target_uid_mismatch" in record["failure_reasons"]


def test_bladeai_full_chain_rejects_canary_mismatch():
    other = TARGET.model_copy(update={"uid": "other-uid"})
    record = _evaluate(expected_canary=other)

    assert record["passed"] is False
    assert "expected_canary_mismatch" in record["failure_reasons"]


def test_bladeai_full_chain_rejects_agent_visible_write_mcp_in_launch_contract():
    report = _report().model_copy(
        update={
            "final_output": {
                **_report().final_output,
                "bladeai_launch": {
                    **_report().final_output["bladeai_launch"],
                    "mcp_servers": ["k8s_ro", "telemetry_ro", "source_ro", "harness_channel", "chaos_control"],
                },
            }
        }
    )
    record = _evaluate(report=report)

    assert record["passed"] is False
    assert "invalid_bladeai_launch_contract" in record["failure_reasons"]


def test_bladeai_full_chain_preserves_terminal_provider_error():
    report = _report().model_copy(
        update={
            "final_output": {
                **_report().final_output,
                "bladeai_result": {
                    "type": "stage2_bladeai_result",
                    "status": "failed",
                    "error": {
                        "code": "UNKNOWN",
                        "message": "Too many pending requests, please retry later",
                        "recoverable": False,
                    },
                },
            }
        }
    )

    record = _evaluate(report=report)

    assert record["passed"] is False
    assert "bladeai_terminal_error:UNKNOWN" in record["failure_reasons"]
    assert record["terminal_agent_error"] == {
        "code": "UNKNOWN",
        "message": "Too many pending requests, please retry later",
    }
