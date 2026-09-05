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

from stage2_service.auto_reply import HarnessResponder
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
    "duration_seconds": 45, "maximum_observation_seconds": 45,
    "effect_criterion": "目标请求延迟明显增加", "stop_conditions": ["到期自动恢复"],
}


class ModelAdapter:
    def __call__(self, instructions, context):
        if "read-only conversation interpreter" in instructions:
            if context["messages"][-1] == "[unreadable final answer]":
                return {"questions": [], "assessment": {}}
            return {"questions": [], "assessment": {
                "status": "completed", "decision": "safe_stop", "effect_assessment": "unverified",
                "recovery_assessment": "unverified", "remaining_risk": "请求级效果未验证",
            }}
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
            "duration_seconds": 45, "intensity": {"delay_ms": 300}, "ledger_state": "expired_cleaned",
            "started_at": "2026-09-04T14:00:00+00:00", "ended_at": "2026-09-04T14:00:45+00:00",
            "deadline_at": "2026-09-04T14:00:45+00:00", "experiment_name": "original-injection",
        }

    def destroy(self, _handle):
        return {"verified_absent": True}

    def inventory(self, _namespace):
        return {"global_chaosblade_count": 0}


class EvidenceAdapter:
    window = None

    def effect_since(self, _trial, runtime):
        self.window = runtime.main_fault["evidence_window"]
        return {"verified": False, "evidence_window": self.window,
                "observability": {"status": "limited", "reason": "request series have no Pod label"}}

    def reset_and_wait_healthy(self, **_kwargs):
        return {"application_owned": True, "load_generator_ready": True, "traffic_observed": True, "business_healthy": True}


@pytest.mark.parametrize("scenario", ["latest", "custom", "plain", "repair", "startup", "exhausted"])
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
            model_call=ModelAdapter(), namespace=ns, max_fault_seconds=fault_budget, max_observation_seconds=1200,
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
    assert report.status == "completed", report.final_output
    answers = [event.payload for event in report.lifecycle_events if event.kind == "user_decision_received"]
    assert len(answers) == 1
    assert answers[0]["responder"] == "HARNESS"
    assert answers[0]["answer_mode"] == ("custom" if scenario == "custom" else "approve_recommendation")
    assert answers[0]["approved_plan"]["intensity"]["delay_ms"] == 300
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
    assert len(report.final_output["retry_history"]) == (1 if scenario in {"repair", "startup"} else 0)
    evidence = EvidenceAdapter()
    recovery = Stage2Finalizer(CleanupAdapter(), evidence, sleep=lambda _: None).finalize(runtime.trial_id, None, runtime, report)
    assert evidence.window["start"] == "2026-09-04T14:00:00+00:00"
    assert evidence.window["end"] == "2026-09-04T14:00:45+00:00"
    decision = Stage2Evaluator().decision(
        kind=TrialKind.CONTROL, report=report, disturbances=(), recovery=recovery,
        diagnostic_only=True, decision_policy=DecisionPolicy.CLARIFY_MISSING,
    )
    assert decision["experiment_completed"] is True
    assert decision["verdict"] == "PASS"
    assert decision["agent_verdict"] == ("PARTIAL" if scenario == "plain" else "FAIL_EVIDENCE")
    if scenario == "custom":
        target = next(node for node in decision["node_results"] if node["node"] == "TARGET_IDENTITY")
        assert target["completion_source"] == "USER_DIRECTED"
        assert target["score"] == 2
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
