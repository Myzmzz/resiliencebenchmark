"""Native-runner integration with a real Unix audit bridge, no live cluster."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from mcp_servers.runtime_audit import audit_client_from_env, audited_sync_call
from scripts.run_harness_trial import CommandResult
from stage2_service.contracts import (
    CapabilityProfile, HarnessKind, LifecycleEvent, PromptMode, RuntimeTarget,
    Stage2CaseId, TrialRuntimeContext, default_case_specs,
)
from stage2_service.harness_runtime import NativeHarnessRunner
from stage2_service.platform_ledger import PlatformLedger


ROOT = Path(__file__).resolve().parents[1]


class Supervisor:
    environment = None
    stopped = False

    def start_trial(self, **kwargs):
        self.environment = kwargs["runtime_environment"]
        return {
            "RESBENCH_BLADEAI_K8S_MCP_SSE_URL": "http://127.0.0.1:18181/sse",
            "RESBENCH_BLADEAI_TELEMETRY_MCP_SSE_URL": "http://127.0.0.1:18182/sse",
            "RESBENCH_BLADEAI_SOURCE_MCP_SSE_URL": "http://127.0.0.1:18183/sse",
            "RESBENCH_BLADEAI_CHAOS_CONTROL_MCP_SSE_URL": "http://127.0.0.1:18184/sse",
            "RESBENCH_BLADEAI_HARNESS_CHANNEL_MCP_SSE_URL": "http://127.0.0.1:18185/sse",
        }

    def stop(self):
        self.stopped = True


def native_tool(harness, call_id, tool, arguments, payload):
    server, name = tool.split(".", 1)
    if harness is HarnessKind.CODEX:
        return [
            {"type": "item.started", "item": {"id": call_id, "type": "mcp_tool_call",
             "server": server, "tool": name, "arguments": arguments, "status": "in_progress"}},
            {"type": "item.completed", "item": {"id": call_id, "type": "mcp_tool_call",
             "server": server, "tool": name, "arguments": arguments, "status": "completed",
             "result": {"structured_content": payload}}},
        ]
    if harness is HarnessKind.CLAUDE_CODE:
        return [
            {"message": {"role": "assistant", "content": [{"type": "tool_use", "id": call_id,
             "name": f"mcp__{server}__{name}", "input": arguments}]}},
            {"message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": call_id,
             "content": [{"type": "text", "text": json.dumps(payload)}]}]}},
        ]
    # DSH has no live tool stream; the realtime bridge must work without it.
    return []


def final_line(harness, full_contract=False):
    assessment = {"status": "blocked", "decision": "safe_stop",
                  "effect_assessment": "unverified", "recovery_assessment": "unverified",
                  "missing_conditions": ["fixture"], "remaining_risk": "fixture"}
    if full_contract:
        assessment.update({
            "interaction_mode": "guided", "assisted": False, "assistance_events": [],
            "clarification_request": None,
            "retry_summary": {"operation_id": None, "attempts": 0, "bounded": True, "outcome_reconciled": False},
            "recovery_trigger": {"condition": "not_applicable", "observed": False, "triggered_by_agent": False},
            "strategy_selection": {"fault_type": "network-delay", "rationale": "fixture", "evidence_summary": "fixture"},
            "suspected_defect": "fixture", "evidence": [], "actions_taken": [], "recovery_check": "unverified",
        })
    text = json.dumps(assessment)
    if harness is HarnessKind.CODEX:
        return {"type": "item.completed", "item": {"id": "final", "type": "agent_message", "text": text}}
    if harness is HarnessKind.CLAUDE_CODE:
        return {"message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}
    if harness is HarnessKind.BLADEAI:
        return {"type": "stage2_bladeai_result", "status": "degraded", "summary": text}
    return json.loads(text)


@pytest.mark.parametrize("harness", list(HarnessKind))
def test_exec_rejection_remains_primary_failure_when_no_model_request_was_sent(tmp_path, monkeypatch, harness):
    from harness.agent_exec.client import AgentExecClientError

    trial_id = f"campaign-startup-{harness.value}"
    ledger = PlatformLedger(tmp_path / "ledger")
    supervisor = Supervisor()
    permissions = SimpleNamespace(runtime_context=lambda _: {
        "mcp_token": "fixture-token", "mcp_token_state_files": {},
        "harness_channel_token": "fixture-channel-token", "platform_ledger_root": str(ledger.root),
    })
    runtime = TrialRuntimeContext(
        trial_id=trial_id, episode_id="episode",
        target=RuntimeTarget(namespace="otel-demo", component="cart", name="cart-a", uid="uid-a"),
        main_fault={"selection_mode": "agent_strategy"},
        cleanup_handle="cleanup-" + "a" * 36, baseline_capability="b" * 40,
    )
    capability = CapabilityProfile(
        harness=harness, mcp_servers=(), mcp_tools=(), kubernetes_rules=(), direct_kubeconfig=False,
        allowed_fault_types=("network-delay",), expires_at=datetime.now(UTC) + timedelta(hours=1),
    )

    def rejected(*_args, **_kwargs):
        raise AgentExecClientError("agent runtime rejected request: fixture-secret")

    runner = NativeHarnessRunner(
        repo_root=ROOT, private_root=tmp_path / "private", artifact_root=tmp_path / "artifacts",
        permissions=permissions, mcp_supervisor=supervisor,
        agent_exec_client=SimpleNamespace(),
        gateway_snapshot=SimpleNamespace(config_sha256="c" * 64, route=lambda model: {"model_alias": model}),
        base_environment={"RESBENCH_LLM_BASE_URL": "http://127.0.0.1:4000/v1", "RESBENCH_LLM_API_KEY": "fixture-secret"},
        responder_factory=lambda *_args, **_kwargs: SimpleNamespace(history=[]),
    )
    monkeypatch.setattr(runner, "_require_workspace_group", lambda *_args: None)
    monkeypatch.setattr(runner, "_resolve_executable", lambda *_args: "/fixture/native-agent")
    monkeypatch.setattr("stage2_service.harness_runtime.subprocess_streaming_runner", rejected)
    report = runner.run(
        campaign_id="campaign-startup", trial_id=trial_id, harness=harness,
        model_alias="fixture-model", episode=None, runtime_context=runtime, capability=capability,
        case=default_case_specs((Stage2CaseId.C0,))[0], base_prompt="fixture", prompt_mode=PromptMode.VERBATIM,
        event_observer=lambda *_args: None,
    )
    assert report.status == "failed"
    assert report.final_output["harness_error_code"] == "AGENT_EXEC_FAILED"
    assert report.final_output["harness_error"]["error_type"] == "AgentExecClientError"
    assert "fixture-secret" not in report.final_output["harness_error"]["reason"]
    assert report.final_output["harness_error"]["gateway_evidence_missing"] is True
    assert report.final_output["gateway_request_ids"] == []
    assert report.final_output["gateway_evidence_verified"] is False
    assert supervisor.stopped


@pytest.mark.parametrize("harness", list(HarnessKind))
@pytest.mark.parametrize("full_contract", [False, True])
def test_live_audit_drives_actions_once_without_relying_on_native_tool_stream(tmp_path, monkeypatch, harness, full_contract):
    trial_id = f"campaign-1234567890abcdef-{harness.value}-d5-1"
    ledger = PlatformLedger(tmp_path / "ledger")
    supervisor = Supervisor()
    permissions = SimpleNamespace(runtime_context=lambda _: {
        "mcp_token": "fixture-token", "mcp_token_state_files": {},
        "harness_channel_token": "fixture-channel-token", "platform_ledger_root": str(ledger.root),
    })
    runtime = TrialRuntimeContext(
        trial_id=trial_id, episode_id="EPI-OTEL-CART-DEADLINE-001",
        target=RuntimeTarget(namespace="otel-demo", component="cart", name="cart-a", uid="uid-a"),
        main_fault={"selection_mode": "agent_strategy", "max_fault_duration_seconds": 1200},
        cleanup_handle="cleanup-" + "a" * 36, baseline_capability="b" * 40,
    )
    capability = CapabilityProfile(
        harness=harness, mcp_servers=(), mcp_tools=(), kubernetes_rules=(), direct_kubeconfig=False,
        allowed_fault_types=("network-delay",), expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    state = {"backend_called": False, "window_disabled": False, "before_seen": False}
    observed = []

    def observer(event):
        observed.append(event)
        if not isinstance(event, LifecycleEvent):
            return
        if event.kind == "main_fault_requested":
            assert not state["backend_called"], "before-call must precede backend mutation"
            assert event.payload["source"] == "mcp_server"
            state["before_seen"] = True
        if event.kind == "effect_check_started":
            state["window_disabled"] = True

    def fake_process(_argv, _stdin, child_env, _timeout, stdout_observer, *_args, **_kwargs):
        assert "RESBENCH_MCP_AUDIT_AUTHORITY" not in child_env
        assert "RESBENCH_MCP_AUDIT_SOCKET" not in child_env
        if harness is HarnessKind.BLADEAI:
            request = json.loads(Path(_argv[-1]).read_text())
            assert request["mode"] == "task"
            assert request["intent"] == "fixture"
            assert "managed_fault" not in request and "target" not in request
            kube = json.loads(Path(request["kubeconfig"]).read_text())
            assert kube["clusters"][0]["cluster"]["server"] == "http://127.0.0.1:18481"
            assert "RESBENCH_BLADEAI_PROXY_KUBECONFIG" not in child_env
            assert child_env["BLADE_AI_MODEL_NAME"] == "fixture-model"
            assert child_env["BLADE_AI_BLADE_PATH"].endswith("blade-shim/blade")
            mcp = json.loads(Path(child_env["BLADE_AI_MCP_CONFIG_PATH"]).read_text())["mcpServers"]
            assert set(mcp) == {"k8s_ro", "telemetry_ro", "source_ro", "chaos_control", "harness_channel"}
            assert mcp["chaos_control"]["enabled"] is True
        client = audit_client_from_env(supervisor.environment)
        assert client is not None
        output = []

        def emit(value):
            raw = json.dumps(value).encode() + b"\n"
            output.append(raw)
            stdout_observer(raw)

        tool = "chaos_control.chaos_create_experiment"
        arguments = {"namespace": "otel-demo", "target_uid": "uid-a", "fault_type": "network-delay"}
        for value in native_tool(harness, "stdout-spoof", tool, arguments, {"ok": True, "created": {"phase": "Running"}}):
            emit(value)
        assert not state["before_seen"], "stdout cannot create authoritative action facts"

        def backend():
            assert state["before_seen"]
            state["backend_called"] = True
            return {"ok": True, "created": {"phase": "Running"}}

        result = audited_sync_call(client, "chaos_control", "chaos_create_experiment", arguments, backend)
        for value in native_tool(harness, "real-native-call", tool, arguments, result):
            emit(value)

        notice = ledger.enqueue_notice(trial_id=trial_id, notice_type="CHANNEL_RESTORED", payload={"fixture": True})
        delivery = ledger.claim_notice(trial_id=trial_id, claimed_by="in_band", lease_seconds=60)
        assert delivery is not None

        def observation():
            assert state["window_disabled"], "same-call policy transition must precede observation"
            return {"ok": True, "controller_notices": [{"delivery_id": delivery.delivery_id,
                    "notice": {"notice_id": notice.notice_id}}]}

        result = audited_sync_call(client, "telemetry_ro", "telemetry_prom_metric_range", {"query": "fixture"}, observation)
        assert not [event for event in ledger.query(trial_id=trial_id) if event.event_type == "NOTICE_DELIVERED"], "server response is not a receipt"
        for value in native_tool(harness, "observation", "telemetry_ro.telemetry_prom_metric_range", {"query": "fixture"}, result):
            emit(value)
        receipts = [event for event in ledger.query(trial_id=trial_id) if event.event_type == "NOTICE_DELIVERED"]
        assert len(receipts) == (1 if harness in {HarnessKind.CODEX, HarnessKind.CLAUDE_CODE} else 0)
        emit(final_line(harness, full_contract))
        return CommandResult(returncode=0, stdout=b"".join(output), stderr=b"")

    monkeypatch.setattr("stage2_service.harness_runtime.subprocess_streaming_runner", fake_process)
    runner = NativeHarnessRunner(
        local_test_execution=True,
        repo_root=ROOT, private_root=tmp_path / "private", artifact_root=tmp_path / "artifacts",
        permissions=permissions, mcp_supervisor=supervisor, base_environment={},
        responder_factory=lambda *_args, **_kwargs: SimpleNamespace(history=[]),
    )
    monkeypatch.setattr(runner, "_resolve_executable", lambda *_args: "/fixture/native-agent")
    report = runner.run(
        campaign_id="campaign-1234567890abcdef", trial_id=trial_id, harness=harness,
        model_alias="fixture-model", episode=None, runtime_context=runtime, capability=capability,
        case=default_case_specs((Stage2CaseId.D5,))[0], base_prompt="fixture", prompt_mode=PromptMode.VERBATIM,
        event_observer=observer,
    )
    assert report.status == "completed", report.final_output
    kinds = [event.kind for event in report.lifecycle_events]
    assert kinds.count("main_fault_requested") == 1
    assert kinds.count("main_fault_running") == 1
    assert kinds.count("effect_check_started") == 1
    assert report.final_output["adapter_integrity"]["call_count"] == 2
    if full_contract:
        assert report.final_output["validation_error"] is None
        assert report.final_output["agent_result_ref"] == "agent-result.json"
    else:
        assert report.final_output["validation_error"] == "RESULT_CONTRACT_INVALID"
        assert "agent_result_ref" not in report.final_output
        assert "agent_result" not in report.final_output
        assert report.agent_assessment["effect_assessment"] == "unverified"
        assert not (tmp_path / "artifacts" / "campaign-1234567890abcdef" / trial_id / "agent-result.json").exists()
    assert supervisor.stopped
    assert not Path(supervisor.environment["RESBENCH_MCP_AUDIT_SOCKET"]).exists()
