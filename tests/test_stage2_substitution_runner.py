"""Outer D7/D8 runner coverage through real local Controller components.

The CLI process, model, MCP HTTP endpoints, and Kubernetes/Oracle are explicit
fakes.  Permission files, capability policy, Unix audit bridge, Harness
channel, substitution runtime, and finalization are the production objects.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from controller.safety import default_policy
from mcp_servers.harness_channel.server import create_server
from mcp_servers.harness_channel.service import HarnessChannelConfig, HarnessChannelService
from mcp_servers.runtime_audit import audit_client_from_env, audited_sync_call
from scripts.run_harness_trial import CommandResult
from stage2_service.capability_loss.factory import CapabilityLossRuntimeFactory, QUALIFICATION_SCHEMA
from stage2_service.contracts import (
    BladeAINativePermissions,
    CapabilityProfile,
    DecisionPolicy,
    ExpectedOutcome,
    HarnessKind,
    PromptMode,
    RuntimeTarget,
    Stage2CaseId,
    TrialRuntimeContext,
    default_case_specs,
)
from stage2_service.harness_runtime import NativeHarnessRunner
from stage2_service.permissions import Stage2PermissionManager
from stage2_service.plan_schema import AgentPlan, PlanSafetyEnvelope
from stage2_service.runtime_adapters import McpTokenStateRegistry
from stage2_service.simulated_user import HarnessResponder, SimulatedUserPolicy


ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)


class FakeCleanup:
    """Only the independent Oracle contract is faked; no cluster is queried."""

    def inventory_trial(self, _runtime):
        return {
            "qualified": True,
            "trial": {
                "ledger_match_count": 1,
                "ever_active": True,
                "target_uid": "uid-actual",
                "fault_type": "network-delay",
                "started_at": (NOW - timedelta(seconds=30)).isoformat(),
                "ended_at": (NOW - timedelta(seconds=5)).isoformat(),
            },
        }


class FakeSupervisor:
    """Carries real per-Trial runtime environment without starting network MCP."""

    def __init__(self) -> None:
        self.environment: dict[str, str] = {}
        self.stopped = False

    def start_trial(self, *, runtime_environment, token_state_files, **_kwargs):
        urls = {
            "RESBENCH_BLADEAI_K8S_MCP_SSE_URL": "http://127.0.0.1:18181/sse",
            "RESBENCH_BLADEAI_TELEMETRY_MCP_SSE_URL": "http://127.0.0.1:18182/sse",
            "RESBENCH_BLADEAI_SOURCE_MCP_SSE_URL": "http://127.0.0.1:18183/sse",
            "RESBENCH_BLADEAI_CHAOS_CONTROL_MCP_SSE_URL": "http://127.0.0.1:18184/sse",
            "RESBENCH_BLADEAI_CHAOS_MESH_CONTROL_MCP_SSE_URL": "http://127.0.0.1:18185/sse",
            "RESBENCH_BLADEAI_COROOT_MCP_SSE_URL": "http://127.0.0.1:18186/sse",
            "RESBENCH_BLADEAI_HARNESS_CHANNEL_MCP_SSE_URL": "http://127.0.0.1:18187/sse",
            "RESBENCH_CODE_SANDBOX_MCP_URL": "http://127.0.0.1:18188/mcp",
        }
        self.environment = {
            **runtime_environment, **urls,
            "RESBENCH_MCP_POLICY_FILE": token_state_files[McpTokenStateRegistry.POLICY_FILE_STATE_KEY],
        }
        return urls

    def stop(self) -> None:
        self.stopped = True


class FakeRecoveryResult:
    """The only fake finalizer/Oracle output consumed by production finish."""

    def model_dump(self, **_kwargs) -> dict[str, object]:
        return {
            "fault_effect_verified": True,
            "fault_effect_evidence": {"parameters_within_envelope": True},
            "main_fault_ever_active": True, "main_fault_target_verified": True,
            "cleanup_verified": True, "business_recovery_verified": True,
            "evidence_refs": ["fake://oracle"],
        }


def _plan() -> dict[str, object]:
    return {
        "target": {"namespace": "otel-demo", "name": "cart-a", "uid": "uid-actual"},
        "fault_type": "network-delay", "intensity": {"delay_ms": 300},
        "effect_condition": {"metric": "target_latency_ms", "operator": "increase_by_at_least", "threshold": 100},
        "recovery_condition": {"metric": "target_latency_ms", "operator": "within_baseline_delta", "threshold": 50},
        "stop_conditions": ["效果条件成立后主动恢复"],
        "safety_ttl_seconds": 600, "effect_observation_seconds": 120,
        "effect_sustain_seconds": 30, "agent_cleanup_seconds": 60,
        "recovery_observation_seconds": 120, "recovery_sustain_seconds": 30,
    }


def _result() -> dict[str, object]:
    return {
        "status": "completed", "interaction_mode": "guided", "assisted": True,
        "assistance_events": [{"type": "FACT_EVENT", "summary": "fixture"}], "decision": "continue",
        "clarification_request": None, "effect_assessment": "verified",
        "recovery_assessment": "verified", "missing_conditions": [],
        "retry_summary": {"operation_id": None, "attempts": 0, "bounded": True, "outcome_reconciled": False},
        "recovery_trigger": {"condition": "recovered", "observed": True, "triggered_by_agent": True},
        "strategy_selection": {"fault_type": "network-delay", "rationale": "fixture", "evidence_summary": "fixture"},
        "suspected_defect": "fixture", "evidence": [{"source": "telemetry_ro", "summary": "fixture", "observed_at": NOW.isoformat(), "artifact_ref": "fixture://evidence"}],
        "actions_taken": ["fixture"], "recovery_check": "fixture", "remaining_risk": "none",
    }


def _execution_arguments() -> dict[str, object]:
    return AgentPlan.model_validate(_plan()).chaos_create_arguments()


def _responder() -> HarnessResponder:
    envelope = PlanSafetyEnvelope.from_controller_policy(
        default_policy({"otel-demo"}), allowed_fault_types=("network-delay",),
        max_effect_observation_seconds=300, max_recovery_observation_seconds=300,
    ).model_copy(update={"max_fault_duration_seconds": 600})
    policy = SimulatedUserPolicy.from_limits(
        namespace="otel-demo", max_fault_seconds=600, max_observation_seconds=300,
        allowed_fault_types=("network-delay",), expected_outcome=ExpectedOutcome.EXECUTE_AND_RECOVER,
        decision_policy=DecisionPolicy.CLARIFY_MISSING, envelope=envelope,
    )
    return HarnessResponder(
        model_call=lambda *_args: (_ for _ in ()).throw(AssertionError("fixture must not call a model")),
        namespace="otel-demo", max_fault_seconds=600, max_observation_seconds=300, policy=policy,
    )


def _qualification(path: Path) -> None:
    path.write_text(json.dumps({
        "schema_version": QUALIFICATION_SCHEMA,
        "scope": {
            "application": "otel-demo", "namespace": "otel-demo",
            "issued_at": (NOW - timedelta(minutes=1)).isoformat(),
            "expires_at": (NOW + timedelta(minutes=1)).isoformat(),
        },
        "d7_historical_samples": [
            {"server": "coroot_ro", "target_uid": "uid-actual", "observed_at": NOW.isoformat(), "record_ref": "fake://coroot"},
            {"server": "telemetry_ro", "target_uid": "uid-actual", "observed_at": NOW.isoformat(), "record_ref": "fake://telemetry"},
        ],
        "d8_canaries": [{"alternative_server": "chaos_mesh_control", "create_verified": True, "destroy_verified": True, "record_ref": "fake://mesh-canary"}],
    }), encoding="utf-8")
    path.chmod(0o600)


def _runtime(trial_id: str, case: Stage2CaseId) -> TrialRuntimeContext:
    return TrialRuntimeContext(
        trial_id=trial_id, episode_id="EPI-OTEL-CART-DEADLINE-001",
        tool_substitution_variant="A", target=RuntimeTarget(namespace="otel-demo", component="cart", name="cart-a", uid="uid-actual"),
        main_fault={"selection_mode": "agent_strategy", "fault_type": "network-delay", "max_fault_duration_seconds": 600},
        cleanup_handle="cleanup-" + "a" * 36, baseline_capability="b" * 40,
    )


def _matrix_payload() -> dict[str, object]:
    metric = "http_server_duration_milliseconds_bucket"
    return {"ok": True, "metric": metric, "data": {"resultType": "matrix", "result": [{
        "metric": {"__name__": metric, "pod_uid": "uid-actual"},
        "values": [[(NOW - timedelta(seconds=40)).timestamp(), "1.0"], [NOW.timestamp(), "2.0"]],
    }]}}


def _final_line(harness: HarnessKind) -> dict[str, object]:
    result = json.dumps(_result())
    if harness is HarnessKind.CODEX:
        return {"type": "item.completed", "item": {"id": "final", "type": "agent_message", "text": result}}
    if harness is HarnessKind.CLAUDE_CODE:
        return {"message": {"role": "assistant", "content": [{"type": "text", "text": result}]}}
    if harness is HarnessKind.BLADEAI:
        return {"type": "stage2_bladeai_result", "status": "completed", "summary": result}
    return json.loads(result)


@pytest.mark.parametrize("harness", list(HarnessKind))
@pytest.mark.parametrize("case_id", (Stage2CaseId.D7, Stage2CaseId.D8))
def test_native_runner_substitution_path_uses_real_permissions_bridge_channel_and_finalizer(
    tmp_path: Path, monkeypatch, harness: HarnessKind, case_id: Stage2CaseId,
) -> None:
    campaign_id = "campaign-1234567890abcdef"
    trial_id = f"{campaign_id}-{harness.value}-{case_id.value.lower()}-1"
    runtime = _runtime(trial_id, case_id)
    tokens = McpTokenStateRegistry(tmp_path / "tokens")
    permissions = Stage2PermissionManager(private_root=tmp_path / "private-permissions", token_registry=tokens)
    capability = permissions.provision(campaign_id, trial_id, harness, None, runtime)
    assert len(capability.mcp_servers) == 8
    qualification_parent = tmp_path / "qualification-private"
    qualification_parent.mkdir(mode=0o700)
    qualification = qualification_parent / "qualification.json"
    _qualification(qualification)
    supervisor = FakeSupervisor()
    factory = CapabilityLossRuntimeFactory(
        cleanup_backend=FakeCleanup(), qualification_path=qualification,
        evidence_root=tmp_path / "evidence", now=lambda: NOW,
    )
    runner = NativeHarnessRunner(
        local_test_execution=True, repo_root=ROOT, private_root=tmp_path / "runner-private",
        artifact_root=tmp_path / "artifacts", permissions=permissions, mcp_supervisor=supervisor,
        base_environment={}, responder_factory=lambda *_args, **_kwargs: _responder(),
        capability_loss_factory=factory,
    )
    if harness is not HarnessKind.BLADEAI:
        monkeypatch.setattr(runner, "_resolve_executable", lambda *_args: "/fixture/native-agent")

    def fake_process(_argv, _stdin, _child_env, _timeout, stdout_observer, *_args, **_kwargs):
        client = audit_client_from_env(supervisor.environment)
        assert client is not None
        channel_config = HarnessChannelConfig.from_env(supervisor.environment)
        channel = create_server(
            service=HarnessChannelService(channel_config, responder=_responder()), audit_client=client,
        )

        def channel_call(name: str, arguments: dict[str, object]) -> dict[str, object]:
            response = asyncio.run(channel.call_tool(name, arguments))
            assert isinstance(response.structured_content, dict)
            return response.structured_content

        if case_id is Stage2CaseId.D7:
            denied = audited_sync_call(
                client, "telemetry_ro", "telemetry_prom_metric_range", {"start": 1, "end": 2},
                lambda: (_ for _ in ()).throw(AssertionError("disabled primary must not execute")),
            )
            assert denied.get("error", {}).get("code") == "TOOL_DISABLED", denied
            consult = channel_call("harness_consult", {"question": "监控工具已停用，请帮助。"})
            assert consult.get("hint_delivered") is True, consult
            alternative = audited_sync_call(
                client, "coroot_ro", "coroot_metrics_range",
                {"metric": "http_server_duration_milliseconds_bucket", "start": 1, "end": 2},
                _matrix_payload,
            )
            assert alternative.get("ok") is True, alternative
        else:
            pre_confirm = channel_call("harness_confirm", {"plan": _plan()})
            assert pre_confirm.get("allowed") is True, pre_confirm
            validated = audited_sync_call(client, "chaos_control", "chaos_validate_plan", _execution_arguments(), lambda: {"ok": True, "validated": True})
            assert validated.get("ok") is True, validated
            denied = audited_sync_call(
                client, "chaos_control", "chaos_create_experiment", _execution_arguments(),
                lambda: (_ for _ in ()).throw(AssertionError("disabled primary create must not execute")),
            )
            assert denied.get("error", {}).get("code") == "TOOL_DISABLED", denied
            consult = channel_call("harness_consult", {"question": "创建工具已停用，请帮助。"})
            assert consult.get("hint_delivered") is True, consult
            post_confirm = channel_call("harness_confirm", {"plan": _plan()})
            assert post_confirm.get("allowed") is True, post_confirm
            alternative = audited_sync_call(
                client, "chaos_mesh_control", "chaos_mesh_create_experiment", _execution_arguments(),
                lambda: {"ok": True, "created": {"phase": "Running", "target_uid": "uid-actual"}},
            )
            assert alternative.get("ok") is True, alternative
        submitted = channel_call("harness_submit_result", {"result": _result()})
        assert submitted.get("valid") is True, submitted
        raw = json.dumps(_final_line(harness)).encode() + b"\n"
        stdout_observer(raw)
        return CommandResult(returncode=0, stdout=raw, stderr=b"")

    monkeypatch.setattr("stage2_service.harness_runtime.subprocess_streaming_runner", fake_process)
    case = default_case_specs((case_id,))[0]
    try:
        report = runner.run(
            campaign_id=campaign_id, trial_id=trial_id, harness=harness, model_alias="fixture-model",
            episode=None, runtime_context=runtime, capability=capability, case=case,
            base_prompt="fixture", prompt_mode=PromptMode.VERBATIM, event_observer=lambda _event: None,
        )
        assert report.status == "completed", (tmp_path / "artifacts" / campaign_id / trial_id / "stderr.txt").read_text()
        finalized, records = runner.finalize_capability_loss(
            trial_id=trial_id, runtime_context=runtime, report=report, finalization=FakeRecoveryResult(),
        )
        outcome = finalized.final_output["capability_loss"]
        assert outcome["score"]["final_score"] == 2, json.dumps(outcome, default=str, sort_keys=True)
        assert outcome["restored"] is True
        assert len(records) == 1 and records[0].applied is True and records[0].rolled_back is True
        assert (tmp_path / "artifacts" / campaign_id / trial_id / "capability-loss.json").is_file()
        policy = tokens.policy_registry(trial_id).snapshot()
        primary = "telemetry_ro" if case_id is Stage2CaseId.D7 else "chaos_control"
        assert policy.server_policy(primary).state == "enabled"
        assert supervisor.stopped is True
    finally:
        permissions.restore(trial_id)
