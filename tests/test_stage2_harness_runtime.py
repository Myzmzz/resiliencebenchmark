from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from stage2_service.contracts import HarnessKind, LifecyclePhase
from stage2_service.harness_runtime import (
    HarnessRuntimeError,
    NativeHarnessRunner,
    _agent_checkpoint_from_item,
    _append_case_runtime_prompt,
    _bladeai_fault_parts,
    _compose_agent_prompt,
    _clarification_request_from_item,
    _extract_recorded_feedback,
    _interaction_event,
    _normalize_bladeai_event,
    _runtime_public_episode,
)
from stage2_service.contracts import (
    PromptExposure,
    RuntimeTarget,
    Stage2CaseId,
    TrialKind,
    TrialRuntimeContext,
    default_case_specs,
)
from scripts.run_harness_trial import DEFAULT_TIMEOUT_SECONDS


class DummySupervisor:
    pass


def runner(tmp_path: Path):
    return NativeHarnessRunner(
        repo_root=Path(__file__).resolve().parents[1],
        private_root=tmp_path / "private",
        artifact_root=tmp_path / "artifacts",
        permissions=object(),  # normalization tests do not access permissions
        mcp_supervisor=DummySupervisor(),
        base_environment={},
    )


def trial_runtime(trial_id: str) -> TrialRuntimeContext:
    return TrialRuntimeContext(
        trial_id=trial_id,
        episode_id="EPI-OTEL-CART-DEADLINE-001",
        target=RuntimeTarget(
            namespace="otel-demo",
            component="cart",
            name="cart-current",
            uid="uid-current",
        ),
        main_fault={"fault_type": "network-delay"},
        cleanup_handle="cleanup-" + "a" * 36,
        baseline_capability="b" * 40,
    )


def test_codex_runtime_requires_isolated_codex_eval(tmp_path: Path):
    executable = tmp_path / "codex-eval"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    runtime = NativeHarnessRunner(
        repo_root=Path(__file__).resolve().parents[1],
        private_root=tmp_path / "private",
        artifact_root=tmp_path / "artifacts",
        permissions=object(),
        mcp_supervisor=DummySupervisor(),
        base_environment={"RESBENCH_CODEX_EVAL_BIN": str(executable)},
    )

    assert runtime._resolve_executable(HarnessKind.CODEX, "codex") == str(
        executable.resolve()
    )

    with pytest.raises(HarnessRuntimeError, match="global codex fallback is forbidden"):
        runner(tmp_path)._resolve_executable(HarnessKind.CODEX, "codex")


def test_stage2_native_harness_timeout_covers_fault_and_lifecycle(tmp_path: Path):
    assert DEFAULT_TIMEOUT_SECONDS == 1800
    assert runner(tmp_path).timeout_seconds == 1800


def test_extracts_structured_agent_question_and_checkpoint():
    result = {
        "status": "blocked",
        "decision": "clarification_required",
        "effect_assessment": "not_attempted",
        "recovery_assessment": "not_applicable",
        "missing_conditions": ["user decision"],
        "recovery_trigger": {
            "condition": "after effect or deadline",
            "observed": False,
            "triggered_by_agent": False,
        },
        "clarification_request": {
            "question": "是否批准建议方案？",
            "required_decisions": ["target_pod", "intensity"],
            "recommendation": {
                "target": {
                    "namespace": "otel-demo",
                    "name": "cart-a",
                    "uid": "uid-a",
                },
                "fault_type": "network-delay",
                "duration_seconds": 60,
                "intensity": {"delay_ms": 250},
                "effect_criterion": "target latency rises",
                "maximum_observation_seconds": 60,
                "stop_conditions": ["deadline reached"],
            },
            "risk_boundary": "one cart Pod only",
        },
    }
    item = {
        "type": "item.completed",
        "item": {"type": "agent_message", "text": json.dumps(result)},
    }

    question = _clarification_request_from_item(item, "trial-123")
    checkpoint = _agent_checkpoint_from_item(item)

    assert question is not None
    assert question["question_id"].startswith("question-")
    assert question["recommendation"]["target"]["name"] == "cart-a"
    assert checkpoint == {
        "status": "blocked",
        "decision": "clarification_required",
        "effect_assessment": "not_attempted",
        "recovery_assessment": "not_applicable",
        "missing_conditions": ["user decision"],
        "recovery_trigger": result["recovery_trigger"],
    }


def test_normalizes_target_binding_and_main_fault_request(tmp_path: Path):
    runtime = runner(tmp_path)
    common = {
        "campaign_id": "campaign-1234567890abcdef",
        "trial_id": "campaign-1234567890abcdef-codex-t2",
        "harness": HarnessKind.CODEX,
        "runtime_context": trial_runtime(
            "campaign-1234567890abcdef-codex-t2"
        ),
    }
    target = runtime._normalize_tool_event(
        **common,
        item={
            "type": "mcp_tool_call",
            "server": "chaos_control",
            "tool": "chaos_validate_plan",
            "status": "completed",
            "arguments": {
                "namespace": "otel-demo",
                "target_name": "cart-old",
                "target_uid": "old-uid",
            },
            "result": {"structured_content": {"ok": True}},
        },
    )
    create = runtime._normalize_tool_event(
        **common,
        item={
            "type": "mcp_tool_call",
            "server": "chaos_control",
            "tool": "chaos_create_experiment",
            "status": "in_progress",
            "arguments": {"target_uid": "new-uid"},
        },
    )

    assert target[0].phase is LifecyclePhase.C2_TARGET
    assert target[0].kind == "target_bound"
    assert target[0].payload["target"]["uid"] == "old-uid"
    assert target[1].kind == "plan_validated"
    assert create[0].phase is LifecyclePhase.C3_INJECT
    assert create[0].payload["target_uid"] == "new-uid"


def test_main_fault_running_requires_explicit_successful_create_result(tmp_path: Path):
    runtime = runner(tmp_path)
    common = {
        "campaign_id": "campaign-1234567890abcdef",
        "trial_id": "campaign-1234567890abcdef-codex-t4",
        "harness": HarnessKind.CODEX,
        "runtime_context": trial_runtime(
            "campaign-1234567890abcdef-codex-t4"
        ),
    }
    rejected = runtime._normalize_tool_event(
        **common,
        item={
            "type": "mcp_tool_call",
            "server": "chaos_control",
            "tool": "chaos_create_experiment",
            "status": "completed",
            "arguments": {"target_uid": "uid-current"},
            "result": {
                "structured_content": {
                    "ok": False,
                    "error": {"code": "CONCURRENCY_BUDGET_EXCEEDED"},
                }
            },
        },
    )
    accepted = runtime._normalize_tool_event(
        **common,
        item={
            "type": "mcp_tool_call",
            "server": "chaos_control",
            "tool": "chaos_create_experiment",
            "status": "completed",
            "arguments": {"target_uid": "uid-current"},
            "result": {"structured_content": {"ok": True}},
        },
    )

    assert all(event.kind != "main_fault_running" for event in rejected)
    assert [event.kind for event in accepted] == ["main_fault_created"]


def test_successful_tool_metadata_does_not_create_false_permission_denial(tmp_path: Path):
    events = runner(tmp_path)._normalize_tool_event(
        campaign_id="campaign-1234567890abcdef",
        trial_id="campaign-1234567890abcdef-codex-t6",
        harness=HarnessKind.CODEX,
        runtime_context=trial_runtime(
            "campaign-1234567890abcdef-codex-t6"
        ),
        item={
            "type": "mcp_tool_call",
            "server": "chaos_control",
            "tool": "chaos_create_experiment",
            "status": "completed",
            "arguments": {"target_uid": "uid-current"},
            "result": {
                "structured_content": {
                    "ok": True,
                    "safety": {"direct_kubernetes_bypass_forbidden": True},
                }
            },
        },
    )

    assert [event.kind for event in events] == ["main_fault_created"]


def test_plan_validation_rejection_is_not_permission_or_channel_failure(tmp_path: Path):
    events = runner(tmp_path)._normalize_tool_event(
        campaign_id="campaign-1234567890abcdef",
        trial_id="campaign-1234567890abcdef-codex-c0",
        harness=HarnessKind.CODEX,
        runtime_context=trial_runtime(
            "campaign-1234567890abcdef-codex-c0"
        ),
        item={
            "type": "mcp_tool_call",
            "server": "chaos_control",
            "tool": "chaos_validate_plan",
            "status": "completed",
            "arguments": {"target_uid": "uid-current"},
            "result": {
                "structured_content": {
                    "ok": False,
                    "findings": [
                        {"code": "SELECTOR_TARGET_FORBIDDEN"},
                        {"code": "MISSING_INTENSITY_FIELD"},
                    ],
                }
            },
        },
    )

    assert [event.kind for event in events] == ["plan_rejected"]
    assert events[0].payload["finding_codes"] == [
        "SELECTOR_TARGET_FORBIDDEN",
        "MISSING_INTENSITY_FIELD",
    ]


def test_tool_argument_rejection_is_not_channel_failure(tmp_path: Path):
    events = runner(tmp_path)._normalize_tool_event(
        campaign_id="campaign-1234567890abcdef",
        trial_id="campaign-1234567890abcdef-codex-c0",
        harness=HarnessKind.CODEX,
        runtime_context=trial_runtime(
            "campaign-1234567890abcdef-codex-c0"
        ),
        item={
            "type": "mcp_tool_call",
            "server": "telemetry_ro",
            "tool": "telemetry_jaeger_find_traces",
            "status": "completed",
            "result": {
                "structured_content": {
                    "ok": False,
                    "error": {"code": "invalid_min_duration"},
                }
            },
        },
    )

    assert [event.kind for event in events] == ["effect_check_started", "tool_request_rejected"]
    assert events[-1].payload["error_codes"] == ["invalid_min_duration"]


def test_transport_unavailable_is_channel_failure(tmp_path: Path):
    events = runner(tmp_path)._normalize_tool_event(
        campaign_id="campaign-1234567890abcdef",
        trial_id="campaign-1234567890abcdef-codex-d5",
        harness=HarnessKind.CODEX,
        runtime_context=trial_runtime(
            "campaign-1234567890abcdef-codex-d5"
        ),
        item={
            "type": "mcp_tool_call",
            "server": "telemetry_ro",
            "tool": "telemetry_jaeger_find_traces",
            "status": "failed",
            "error": {
                "code": "service_unavailable",
                "http_status": 503,
                "message": "MCP transport unavailable",
            },
        },
    )

    assert [event.kind for event in events] == ["effect_check_started", "tool_channel_error"]


def test_normalizes_permission_denial_on_selected_tool(tmp_path: Path):
    events = runner(tmp_path)._normalize_tool_event(
        campaign_id="campaign-1234567890abcdef",
        trial_id="campaign-1234567890abcdef-codex-t3",
        harness=HarnessKind.CODEX,
        runtime_context=trial_runtime(
            "campaign-1234567890abcdef-codex-t3"
        ),
        item={
            "type": "mcp_tool_call",
            "server": "k8s_ro",
            "tool": "k8s_list_resources",
            "status": "failed",
            "error": "403 Forbidden",
        },
    )

    denied = next(item for item in events if item.kind == "permission_denied")
    assert denied.payload["capability"] == "mcp.k8s.read"


def test_normalizes_codex_auth_required_as_permission_denial(tmp_path: Path):
    events = runner(tmp_path)._normalize_tool_event(
        campaign_id="campaign-1234567890abcdef",
        trial_id="campaign-1234567890abcdef-codex-t7",
        harness=HarnessKind.CODEX,
        runtime_context=trial_runtime(
            "campaign-1234567890abcdef-codex-t7"
        ),
        item={
            "type": "mcp_tool_call",
            "server": "k8s_ro",
            "tool": "k8s_get_resource",
            "status": "failed",
            "error": {"message": "Transport error: Auth required"},
        },
    )

    assert [event.kind for event in events] == ["permission_denied"]
    assert events[0].payload["capability"] == "mcp.k8s.read"


def test_runtime_prompt_keeps_expected_evaluator_signal_private():
    case = next(
        item for item in default_case_specs() if item.case_id is Stage2CaseId.D4
    )
    env = {
        "RESBENCH_BASELINE_GATE_TOKEN": "b" * 40,
        "RESBENCH_CLEANUP_HANDLE": "cleanup-" + "a" * 36,
        "RESBENCH_CHAOS_CONTROLLER_TOKEN_REF": "k8s://controller/ref",
        "RESBENCH_CHAOS_CONTROLLER_POD_UID": "controller-uid",
        "RESBENCH_AUTHORIZED_TARGET_JSON": '{"namespace":"otel-demo","component":"cart","kind":"Pod","name":"cart","uid":"uid"}',
        "RESBENCH_MAIN_FAULT_JSON": '{"fault_type":"network-delay","duration_seconds":180,"intensity":{"delay_ms":1000}}',
        "RESBENCH_AUTHORIZED_RUN_ID": "campaign-1234567890abcdef-codex-d4-1",
    }

    prompt = _append_case_runtime_prompt(
        "base task",
        env,
        case,
        allowed_fault_types=("network-delay",),
    )

    assert case.prompt_exposure is PromptExposure.FULL
    assert case.trial_kind is TrialKind.RECOVERY_OBSERVABILITY_REVOKED
    assert case.expected_agent_signal not in prompt


def test_matrix_prompt_is_appended_without_replacing_common_contract():
    repo = Path(__file__).resolve().parents[1]
    task = "Inject the bounded cart network delay and verify its effect."

    prompt = _compose_agent_prompt(
        repo / "harness/prompts/common-task.md",
        repo / "harness/prompts/full-lifecycle.md",
        {"objective": "bounded cart experiment"},
        task,
    )

    assert "Your final answer may be ordinary text" in prompt
    assert "Follow the full benchmark lifecycle explicitly" in prompt
    assert "Public episode contract follows" in prompt
    assert "User-requested experiment task follows" in prompt
    assert task in prompt


def test_runtime_public_episode_replaces_historical_fixed_fault_contract():
    runtime = SimpleNamespace(
        main_fault={
            "fault_type": "cpu-load",
            "duration_seconds": 300,
            "intensity": {"cpu_percent": 80},
        }
    )
    public = _runtime_public_episode(
        {
            "title": "fixed network delay",
            "objective": "inject network-delay",
            "action_space": {"allowed_fault_types": ["network-delay"]},
        },
        runtime_context=runtime,
        capability=SimpleNamespace(allowed_fault_types=("cpu-load",)),
    )

    assert public["action_space"]["allowed_fault_types"] == ["cpu-load"]
    assert public["runtime_safety_envelope"]["fault_type"] == "cpu-load"
    assert "network-delay" not in public["objective"]


def test_interaction_event_keeps_external_tool_data_but_removes_private_reasoning():
    value = _interaction_event(
        {
            "type": "mcp_tool_call",
            "tool": "k8s_list_resources",
            "status": "completed",
            "arguments": {"namespace": "otel-demo"},
            "result": {"ok": True},
            "analysis": "private model reasoning",
        },
        {},
    )

    assert value["event_type"] == "TOOL_INTERACTION"
    assert value["tool"] == "k8s_list_resources"
    assert "analysis" not in value["payload"]
    assert value["payload"]["arguments"]["namespace"] == "otel-demo"


def test_extract_recorded_feedback_keeps_delivery_status(tmp_path: Path):
    transcript = tmp_path / "session-events.jsonl"
    transcript.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "event": "FEEDBACK_QUEUED",
                        "payload": {
                            "category": "FACT_EVENT",
                            "message": "target rebound",
                            "payload": {"case_id": "D2"},
                        },
                    }
                ),
                json.dumps(
                    {
                        "event": "FEEDBACK_DELIVERED",
                        "payload": {
                            "schema_version": "stage2-session-feedback-result.v1",
                            "status": "delivered",
                            "category": "AUTH_CONFIRM",
                            "message": "continue within scope",
                            "payload": {"case_id": "D2"},
                        },
                    }
                ),
                json.dumps(
                    {
                        "event": "FEEDBACK_FAILED",
                        "payload": {
                            "schema_version": "stage2-session-feedback-result.v1",
                            "status": "failed",
                            "category": "SEMANTIC_NUDGE",
                            "message": "continue verification",
                            "payload": {"case_id": "D3"},
                            "reason": "SEMANTIC_NUDGE is forbidden in autonomous interaction mode",
                        },
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )

    feedback = _extract_recorded_feedback(transcript)

    assert [item["result"]["status"] for item in feedback] == [
        "queued",
        "delivered",
        "failed",
    ]
    assert feedback[0]["feedback"].category.value == "FACT_EVENT"
    assert feedback[1]["feedback"].category.value == "AUTH_CONFIRM"
    assert feedback[2]["feedback"].category.value == "SEMANTIC_NUDGE"


def test_ignores_nested_permission_payload_without_tool_identity(tmp_path: Path):
    events = runner(tmp_path)._normalize_tool_event(
        campaign_id="campaign-1234567890abcdef",
        trial_id="campaign-1234567890abcdef-codex-t5",
        harness=HarnessKind.CODEX,
        runtime_context=trial_runtime(
            "campaign-1234567890abcdef-codex-t5"
        ),
        item={"error": "401 Unauthorized"},
    )

    assert events == []


def test_bladeai_native_l4_events_map_to_c1_c6_contract():
    runtime = TrialRuntimeContext(
        trial_id="campaign-1234567890abcdef-bladeai-t1",
        episode_id="EPI-OTEL-CART-DEADLINE-001",
        target=RuntimeTarget(
            namespace="otel-demo", component="cart", name="cart", uid="uid-current"
        ),
        main_fault={"fault_type": "network-delay"},
        cleanup_handle="cleanup-" + "a" * 36,
        baseline_capability="b" * 40,
    )
    events = _normalize_bladeai_event(
        "campaign-1234567890abcdef",
        runtime.trial_id,
        {
            "type": "stage2_bladeai_event",
            "kind": "step_start",
            "payload": {"name": "auto_recover"},
        },
        runtime,
    )

    assert _bladeai_fault_parts("network-delay") == ("pod", "network", "delay")
    assert events[0].phase is LifecyclePhase.C6_RECOVERY
    assert events[0].kind == "recovery_requested"
