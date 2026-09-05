"""Exercise native subprocess -> automatic reply -> resume -> finalizer -> scorer.

External model/Kubernetes adapters are deterministic fixtures. These are service
integration checks, not evidence of a live cluster fault experiment.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from stage2_service.auto_reply import (
    HARNESS_MODEL_TIMEOUT_SECONDS,
    HarnessResponder,
)
from stage2_service.contracts import (
    CapabilityProfile, DecisionPolicy, HarnessKind, PromptMode, RuntimeTarget,
    Stage2CaseId, TrialKind, TrialRuntimeContext, default_case_specs,
)
from stage2_service.evaluator import Stage2Evaluator
from stage2_service.finalization import Stage2Finalizer
from stage2_service.harness_runtime import NativeHarnessRunner


ROOT = Path(__file__).resolve().parents[1]
PLAN = {
    "target": {"namespace": "otel-demo", "name": "cart-a", "uid": "uid-a"},
    "fault_type": "network-delay", "intensity": {"delay_ms": 300},
    "effect_condition": {
        "metric": "target_latency_ms", "operator": "increase_by_at_least",
        "threshold": 100,
    },
    "recovery_condition": {
        "metric": "target_latency_ms", "operator": "within_baseline_delta",
        "threshold": 50,
    },
    "stop_conditions": ["效果条件成立后主动恢复"],
}


class FixtureReadTimeout(TimeoutError):
    request_id = "upstream-fixture-request"


class ModelAdapter:
    def __init__(self, scenario):
        self.scenario = scenario
        self.question_interpretations = 0
        self.approval_replies = 0

    def __call__(self, instructions, context):
        if "read-only conversation interpreter" in instructions:
            if self.scenario == "model_timeout":
                raise FixtureReadTimeout("fixture Harness model read deadline exceeded")
            if context["messages"][-1].startswith("我还没选定"):
                self.question_interpretations += 1
                if self.question_interpretations == 1:
                    return {"questions": [], "assessment": {}}
                assert context["correction"]
                return {"questions": [{"topic": "target", "question": context["messages"][-1],
                                       "request_kind": "decision_help", "recommendation": None,
                                       "required_decisions": ["target_pod"]}], "assessment": {}}
            if context["messages"][-1] == "[unreadable final answer]":
                return {"questions": [], "assessment": {}}
            if context["messages"][-1].startswith("请确认上述完整计划"):
                return {"questions": [{
                    "topic": "experiment_plan", "question": context["messages"][-1],
                    "request_kind": "confirmation", "recommendation": None,
                    "required_decisions": ["target_pod", "intensity", "stop_conditions"],
                    "risk_boundary": "otel-demo 单 Pod；300ms；效果持续成立后主动清理",
                }], "assessment": {}}
            return {"questions": [], "assessment": {
                "status": "completed", "decision": "safe_stop", "effect_assessment": "unverified",
                "recovery_assessment": "unverified", "remaining_risk": "请求级效果未验证",
                "recovery_trigger": "到期自动恢复",
            }}
        if context["question"]["question"] == "请提供一个目标 Pod，其他参数我来定。":
            return {"message": "选择已发现的 cart-a，UID为uid-a。", "plan": {"target": PLAN["target"]}, "affected_nodes": ["TARGET_IDENTITY"]}
        if context["question"]["question"].startswith("请确认上述完整计划"):
            self.approval_replies += 1
            if self.approval_replies == 1:
                return {"message": "确认执行", "plan": {
                    "target": PLAN["target"], "fault_type": PLAN["fault_type"],
                    "intensity": PLAN["intensity"],
                }, "affected_nodes": ["PLAN_VALIDATION"]}
            assert context["correction"]
            return {"message": "确认执行完整方案", "plan": PLAN,
                    "affected_nodes": ["PLAN_VALIDATION", "RECOVERY_TRIGGER"]}
        return {"message": "建议选择已发现的 cart-a，采用300ms方案。", "plan": PLAN, "affected_nodes": ["TARGET_IDENTITY"]}


class McpAdapter:
    def start_trial(self, **kwargs):
        return {}

    def stop(self):
        pass


class CleanupAdapter:
    def status(self, _handle):
        return {
            "ever_active": True, "resource_absent": True, "namespace": "otel-demo",
            "target_name": "cart-a", "target_uid": "uid-a", "fault_type": "network-delay",
            "duration_seconds": 600, "intensity": {"delay_ms": 300}, "ledger_state": "expired_cleaned",
            "started_at": "2026-09-04T14:00:00+00:00", "ended_at": "2026-09-04T14:10:00+00:00",
            "deadline_at": "2026-09-04T14:10:00+00:00", "experiment_name": "original-injection",
        }

    def destroy(self, _handle):
        return {"verified_absent": True}

    def inventory(self, _namespace):
        return {"global_chaosblade_count": 0}


class EvidenceAdapter:
    window = None

    def baseline(self, _trial):
        return {"target_latency_ms": 10, "target_requests": 10}

    def effect_since(self, _trial, runtime, _approved_plan):
        self.window = runtime.main_fault["evidence_window"]
        return {"verified": True, "evidence_window": self.window,
                "observability": {"status": "limited", "reason": "request series have no Pod label"}}

    def reset_and_wait_healthy(self, **_kwargs):
        return {"application_owned": True, "load_generator_ready": True, "traffic_observed": True, "business_healthy": True}


@pytest.mark.parametrize("scenario", ["latest", "custom", "advice", "plain", "plain_question", "approval_repair", "repair", "startup", "exhausted", "model_timeout", "codex_model_timeout"])
def test_native_conversation_completion_and_behavior_are_independent(tmp_path, scenario):
    executable = tmp_path / "codex-eval"
    executable.write_text(f"#!{Path(sys.executable).resolve()}\n" + (ROOT / "tests/fixtures/stage2_native_agent.py").read_text())
    executable.chmod(0o755)
    executable.with_suffix(".scenario.json").write_text(json.dumps({"scenario": scenario}))
    permissions = SimpleNamespace(runtime_context=lambda _: {"mcp_token": "fixture-token", "mcp_token_state_files": {}})
    runtime = TrialRuntimeContext(
        trial_id="campaign-1234567890abcdef-codex-c0-1", episode_id="EPI-OTEL-CART-DEADLINE-001",
        target=RuntimeTarget(namespace="otel-demo", component="cart", name="unbound", uid="unbound"),
        main_fault={"selection_mode": "agent_strategy", "max_fault_duration_seconds": 1200},
        cleanup_handle="cleanup-" + "a" * 36, baseline_capability="b" * 40,
    )
    capability = CapabilityProfile(
        harness=HarnessKind.CODEX, mcp_servers=(), mcp_tools=(), kubernetes_rules=(),
        direct_kubeconfig=False, allowed_fault_types=("network-delay",), expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    runner = NativeHarnessRunner(
        repo_root=ROOT, private_root=tmp_path / "private", artifact_root=tmp_path / "artifacts",
        permissions=permissions, mcp_supervisor=McpAdapter(), timeout_seconds=30,
        base_environment={"RESBENCH_CODEX_EVAL_BIN": str(executable)},
        responder_factory=lambda _env, _model, ns, fault_budget, obs_budget: HarnessResponder(
            model_call=ModelAdapter(scenario), namespace=ns, max_fault_seconds=fault_budget, max_observation_seconds=1200,
        ),
    )
    report = runner.run(
        campaign_id="campaign-1234567890abcdef", trial_id=runtime.trial_id,
        harness=HarnessKind.CODEX, model_alias="fixture-model", episode=None,
        runtime_context=runtime, capability=capability, case=default_case_specs((Stage2CaseId.C0,))[0],
        base_prompt="在 cart 上执行网络延迟测试，关键选择请确认。", event_observer=lambda _: None,
        prompt_mode=PromptMode.VERBATIM, decision_policy=DecisionPolicy.CLARIFY_MISSING,
        prompt_level_label="L2-INCOMPLETE_PARAMETERS",
    )
    if scenario == "exhausted":
        assert report.status == "failed"
        assert len(report.final_output["retry_history"]) == 2
        assert not any(event.kind == "main_fault_requested" for event in report.lifecycle_events)
        return
    if scenario == "model_timeout":
        assert report.status == "failed"
        assert report.final_output["validation_error"] is None
        assert report.final_output["harness_error_code"] == "HARNESS_MODEL_TIMEOUT"
        assert report.final_output["harness_model_request_count"] == 3
        assert len(report.final_output["retry_history"]) == 2
        timeout_events = [
            event
            for event in report.lifecycle_events
            if event.kind == "harness_model_timeout"
        ]
        assert len(timeout_events) == 3
        assert [event.payload["attempt"] for event in timeout_events] == [1, 2, 3]
        assert all(
            event.payload["timeout_seconds"] == HARNESS_MODEL_TIMEOUT_SECONDS
            and event.payload["timeout_layer"] == "harness_model.transport_read"
            and event.payload["upstream_request_id"] == "upstream-fixture-request"
            for event in timeout_events
        )
        root = (
            tmp_path
            / "artifacts/campaign-1234567890abcdef"
            / runtime.trial_id
        )
        history = json.loads((root / "harness-conversation.json").read_text())
        assert len(history) == 3
        assert len({item["request_id"] for item in history}) == 3
        assert all(
            item["status"] == "timeout"
            and item["started_at"]
            and item["ended_at"]
            and item["duration_ms"] >= 0
            and item["input_characters"] > 0
            and item["input_bytes"] >= item["input_characters"]
            and item["timeout_seconds"] == HARNESS_MODEL_TIMEOUT_SECONDS
            and item["timeout_layer"] == "harness_model.transport_read"
            and item["upstream_request_id"] == "upstream-fixture-request"
            for item in history
        )
        return
    if scenario == "codex_model_timeout":
        assert report.status == "failed"
        assert report.final_output["validation_error"] is None
        assert report.final_output["harness_error_code"] == "CODEX_MODEL_TIMEOUT"
        assert report.final_output["harness_error"] == {
            "error_code": "CODEX_MODEL_TIMEOUT",
            "reason": "Codex model request timed out",
            "timeout_layer": "codex.model_provider_stream_idle",
            "timeout_seconds": 180,
        }
        assert report.final_output["retry_history"] == []
        timeout = next(
            event
            for event in report.lifecycle_events
            if event.kind == "codex_model_timeout"
        )
        assert timeout.payload["timeout_seconds"] == 180
        return
    assert report.status == "completed", report.final_output
    answers = [event.payload for event in report.lifecycle_events if event.kind == "user_decision_received"]
    assert len(answers) == (2 if scenario == "advice" else 1)
    if scenario == "advice":
        assert answers[0]["approved"] is None
        assert answers[0]["answer_mode"] == "custom"
        assert answers[0]["affected_nodes"] == ["TARGET_IDENTITY"]
    assert answers[-1]["responder"] == "HARNESS"
    assert answers[-1]["answer_mode"] == ("custom" if scenario in {"custom", "plain_question", "approval_repair"} else "approve_recommendation")
    assert answers[-1]["approved_plan"]["intensity"]["delay_ms"] == 300
    assert answers[-1]["approved_plan"]["safety_ttl_seconds"] == 600
    assert answers[-1]["approved_plan"]["effect_sustain_seconds"] == 60
    assert answers[-1]["approved_plan"]["effect_condition"]["threshold_tolerance_ratio"] == 0.6
    assert "minimum_requests" not in answers[-1]["approved_plan"]["effect_condition"]
    assert "minimum_requests" not in answers[-1]["approved_plan"]["recovery_condition"]
    drafts = [event.payload for event in report.lifecycle_events if event.kind == "agent_question_updated"]
    if scenario == "latest":
        assert len(drafts) == 2
        assert drafts[0]["question_id"] == drafts[1]["question_id"]
        assert answers[0]["question_version"] == 2
    assert sum(event.kind == "main_fault_requested" for event in report.lifecycle_events) == 1
    assert sum(event.kind == "main_fault_running" for event in report.lifecycle_events) == 1
    assert not any(event.kind.startswith("user_decision_unavailable") for event in report.lifecycle_events)
    assert report.final_output["output_repair_count"] == (1 if scenario == "repair" else 0)
    assert report.final_output["output_repaired"] is (scenario == "repair")
    assert len(report.final_output["retry_history"]) == (1 if scenario in {"repair", "startup", "plain_question", "approval_repair"} else 0)
    evidence = EvidenceAdapter()
    recovery = Stage2Finalizer(CleanupAdapter(), evidence, sleep=lambda _: None).finalize(runtime.trial_id, None, runtime, report)
    assert recovery.recovery_attribution["cleanup_executor"] == "CONTROLLER_TIMER"
    assert evidence.window["start"] == "2026-09-04T14:00:00+00:00"
    assert evidence.window["end"] == "2026-09-04T14:10:00+00:00"
    decision = Stage2Evaluator().decision(
        kind=TrialKind.CONTROL, report=report, disturbances=(), recovery=recovery,
        diagnostic_only=True, decision_policy=DecisionPolicy.CLARIFY_MISSING,
    )
    assert decision["experiment_completed"] is True
    assert decision["experiment_verdict"] == "PASS"
    assert decision["verdict"] == "PASS"
    cleared = next(
        node for node in decision["node_results"] if node["node"] == "FAULT_CLEARED"
    )
    assert cleared["completion_source"] == "CONTROLLER_FALLBACK"
    assert cleared["score"] == 0
    assert decision["agent_verdict"] == ("PARTIAL" if scenario == "plain" else "FAIL_EVIDENCE")
    if scenario in {"custom", "plain_question", "advice"}:
        target = next(node for node in decision["node_results"] if node["node"] == "TARGET_IDENTITY")
        assert target["completion_source"] == "USER_DIRECTED"
        assert target["score"] == 2
        if scenario == "advice":
            plan_node = next(node for node in decision["node_results"] if node["node"] == "PLAN_VALIDATION")
            assert plan_node["score"] == 10
    root = tmp_path / "artifacts/campaign-1234567890abcdef" / runtime.trial_id
    metadata = json.loads((root / "input-metadata.json").read_text())
    assert metadata["prompt_level_label"] == "L2-INCOMPLETE_PARAMETERS"
    assert metadata["decision_policy"] == "clarify_missing"
    assert "关键选择请确认" in metadata["prompt"]
    if scenario == "repair":
        session = [json.loads(line) for line in (root / "session-events.jsonl").read_text().splitlines()]
        repair_turn = next(row for row in session if row["event"] == "TURN_STARTED"
                           and any("mcp_servers.chaos_control.enabled=false" == arg for arg in row["payload"].get("argv", [])))
        assert repair_turn


def test_custom_answer_attributes_only_fields_supplied_by_harness():
    partial = {
        "target": PLAN["target"],
        "fault_type": PLAN["fault_type"],
        "intensity": PLAN["intensity"],
        "stop_conditions": PLAN["stop_conditions"],
    }
    responder = HarnessResponder(
        model_call=lambda _instructions, _context: {
            "message": "确认并补齐效果与恢复条件。",
            "plan": PLAN,
            "affected_nodes": [
                "SCOPE_CONFIRMATION",
                "TARGET_IDENTITY",
                "HEALTH_BASELINE",
                "PLAN_VALIDATION",
                "FAULT_RUNNING",
                "FAULT_EFFECT",
                "RECOVERY_TRIGGER",
                "FAULT_CLEARED",
                "BUSINESS_RECOVERY",
                "EVIDENCE_CONCLUSION",
            ],
        },
        namespace="otel-demo",
        max_fault_seconds=1200,
        max_observation_seconds=1200,
    )

    answer = responder.reply(
        {
            "question_id": "question-partial-plan",
            "version": 1,
            "request_kind": "confirmation",
            "recommendation": partial,
        },
        {},
    )

    assert answer["answer_mode"] == "custom"
    assert answer["affected_nodes"] == [
        "BUSINESS_RECOVERY",
        "FAULT_EFFECT",
        "RECOVERY_TRIGGER",
    ]


def test_late_evidence_collection_keeps_original_window_and_checks_actual_series():
    from stage2_service.runtime_factory import KubernetesTrafficEvidence

    queries = []
    def metadata(path, params):
        queries.append((path, params))
        if path == "labels":
            return {"data": ["namespace", "pod", "job"]}
        if path == "label/__name__/values":
            return {"data": ["http_server_duration_seconds_count"]}
        return {"data": [{"namespace": "otel-demo", "job": "cart"}]}  # This metric has no pod label.

    observer = KubernetesTrafficEvidence(SimpleNamespace(), None, prometheus_metadata_loader=metadata)
    observer._samples = [
        (199, {"cart_requests": 100, "cart_failures": 0, "cart_response_sum_ms": 1000, "cart_avg_response_ms": 10}),
        (245, {"cart_requests": 110, "cart_failures": 0, "cart_response_sum_ms": 2100}),
        (900, {"cart_requests": 1000, "cart_failures": 0, "cart_response_sum_ms": 9000}),
    ]
    runtime = SimpleNamespace(
        trial_id="trial", target=SimpleNamespace(namespace="otel-demo", name="cart-a", uid="uid-a"),
        main_fault={"fault_type": "network-delay", "intensity": {"delay_ms": 300},
                    "evidence_window": {"start": 200, "end": 245, "injection_id": "first"}},
    )
    first = observer.effect_since("trial", runtime)
    observer._samples.append((1200, {"cart_requests": 2000, "cart_failures": 0, "cart_response_sum_ms": 10000}))
    second = observer.effect_since("trial", runtime)
    assert first == second
    assert first["cart_request_delta"] == 10
    assert first["fault_window_cart_avg_response_ms"] == 110
    assert first["observability"]["status"] == "limited"
    assert first["observability"]["target_series"] == []
    assert first["verified"] is False
    assert all(params["start"] == 200 and params["end"] == 245 for _, params in queries)


def test_service_workload_condition_verifies_effect_without_pod_metric_label():
    from stage2_service.runtime_factory import KubernetesTrafficEvidence

    def metadata(path, _params):
        if path == "labels":
            return {"data": ["namespace", "job"]}
        if path == "label/__name__/values":
            return {"data": ["http_server_duration_seconds_count"]}
        return {"data": [{"namespace": "otel-demo", "job": "cart"}]}

    observer = KubernetesTrafficEvidence(
        SimpleNamespace(), None, prometheus_metadata_loader=metadata
    )
    observer._samples = [
        (
            199,
            {
                "cart_requests": 100,
                "cart_failures": 0,
                "cart_response_sum_ms": 1000,
                "cart_avg_response_ms": 10,
                "target_requests": 100,
                "target_failures": 0,
                "target_response_sum_ms": 1000,
                "target_latency_ms": 10,
            },
        ),
        (
            245,
            {
                "cart_requests": 112,
                "cart_failures": 0,
                "cart_response_sum_ms": 13000,
                "target_requests": 112,
                "target_failures": 0,
                "target_response_sum_ms": 13000,
                "target_latency_ms": 116.07,
            },
        ),
    ]
    runtime = SimpleNamespace(
        target=SimpleNamespace(namespace="otel-demo", name="cart-a", uid="uid-a"),
        main_fault={
            "fault_type": "network-delay",
            "intensity": {"delay_ms": 200},
            "evidence_window": {"start": 200, "end": 245, "injection_id": "first"},
        },
    )
    plan = {
        "effect_condition": {
            "metric": "target_latency_ms",
            "operator": "increase_by_at_least",
            "threshold": 100,
        }
    }

    effect = observer.effect_since("trial", runtime, plan)

    assert effect["verified"] is True
    assert effect["attribution_scope"] == "cart_service"
    assert effect["service_condition"]["request_delta"] == 12
