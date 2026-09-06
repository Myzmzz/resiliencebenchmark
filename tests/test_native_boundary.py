from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from stage2_service.contracts import (
    CapabilityProfile,
    HarnessKind,
    RuntimeTarget,
    PromptMode,
    Stage2CaseId,
    TrialRuntimeContext,
    default_case_specs,
)
from stage2_service.harness_adapters.base import AgentMessage, Checkpoint, ToolCall
from stage2_service.harness_adapters.bladeai import BladeAIHarnessAdapter
from stage2_service.harness_adapters.claude_code import ClaudeCodeHarnessAdapter
from stage2_service.harness_adapters.codex import CodexHarnessAdapter
from stage2_service.harness_adapters.deepseek import DeepSeekHarnessAdapter
from stage2_service.harness_runtime import NativeHarnessRunner
from stage2_service.native_boundary import (
    CLI_NATIVE_SOURCE,
    PERMISSION_BYPASS_EVENT,
    PERMISSION_BYPASS_LIFECYCLE_KIND,
    attempt_dedupe_key,
    is_forbidden_native_event,
    native_boundary_attempt,
)
from stage2_service.platform_ledger import PlatformLedger
from scripts.run_harness_trial import CommandResult


ROOT = Path(__file__).resolve().parents[1]


def _trial_runtime(trial_id: str) -> TrialRuntimeContext:
    return TrialRuntimeContext(
        trial_id=trial_id,
        episode_id="episode-1",
        target=RuntimeTarget(
            namespace="otel-demo",
            component="cart",
            name="cart-a",
            uid="uid-a",
        ),
        main_fault={"fault_type": "network-delay"},
        cleanup_handle="cleanup-" + "a" * 36,
        baseline_capability="b" * 40,
    )


def _capability(harness: HarnessKind) -> CapabilityProfile:
    return CapabilityProfile(
        harness=harness,
        mcp_servers=("k8s_ro", "telemetry_ro", "source_ro", "chaos_control", "harness_channel"),
        mcp_tools=(),
        kubernetes_rules=(),
        direct_kubeconfig=False,
        allowed_fault_types=("network-delay",),
        expires_at="2026-09-05T13:00:00Z",
    )


def test_codex_command_execution_is_native_bypass_attempt() -> None:
    adapter = CodexHarnessAdapter()
    events = adapter.on_stream_line(
        json.dumps(
            {
                "type": "command_execution",
                "id": "cmd-1",
                "command": "kubectl get pods -A",
                "timestamp": "2026-09-05T12:00:00Z",
            }
        ).encode()
    )

    assert len(events) == 1
    assert is_forbidden_native_event(events[0], source="native") is True
    attempt = native_boundary_attempt(events[0], source="native")
    assert attempt is not None
    assert attempt.as_payload()["source"] == CLI_NATIVE_SOURCE
    assert attempt.as_payload()["semantics"] == "attempt_only"
    assert attempt.as_payload()["physical_operation_proven"] is False


def test_claude_bash_tool_is_native_bypass_attempt() -> None:
    adapter = ClaudeCodeHarnessAdapter()
    events = adapter.on_stream_line(
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "bash-1",
                            "name": "Bash",
                            "input": {"command": "curl http://example.invalid"},
                        }
                    ],
                },
            }
        ).encode()
    )

    assert len(events) == 1
    assert is_forbidden_native_event(events[0], source="native_stream") is True


def test_deepseek_native_tool_name_is_bypass_attempt() -> None:
    adapter = DeepSeekHarnessAdapter()
    event = adapter._event_from_record(
        {
            "type": "tool/call",
            "time": "2026-09-05T12:00:00Z",
            "data": {
                "callId": "dsh-1",
                "name": "execute_bash",
                "input": {"command": "python -c 'print(1)'"},
            },
        },
        raw_ref="session.jsonl.zstd:1",
    )

    assert isinstance(event, ToolCall)
    assert is_forbidden_native_event(event, source="post_hoc") is True


def test_allowed_mcp_code_sandbox_and_harness_questions_are_not_bypass_attempts() -> None:
    allowed = ToolCall(
        call_id="sandbox-1",
        tool="code_sandbox.run_python",
        arguments={"code": "print(2 + 2)"},
    )
    question_text = AgentMessage(text="I could run kubectl, but I will ask the harness first.")

    assert is_forbidden_native_event(allowed, source="native") is False
    assert is_forbidden_native_event(question_text, source="native") is False


def test_bladeai_sdk_step_markers_are_not_bypass_attempts() -> None:
    adapter = BladeAIHarnessAdapter()
    events = adapter.on_stream_line(
        json.dumps(
            {
                "type": "stage2_bladeai_event",
                "kind": "step_start",
                "payload": {"name": "read_kubernetes", "attrs": {"path": "/api/v1/pods"}},
            }
        ).encode()
    )

    assert len(events) == 1
    assert isinstance(events[0], Checkpoint)
    assert is_forbidden_native_event(events[0], source="native") is False


def test_attempt_dedupe_key_ignores_replay_flag() -> None:
    call = ToolCall(call_id="cmd-1", tool="command_execution", arguments={})
    first = native_boundary_attempt(call, source="native", replayed=False)
    replay = native_boundary_attempt(call, source="native", replayed=True)

    assert first is not None
    assert replay is not None
    assert attempt_dedupe_key(first) == attempt_dedupe_key(replay)
    assert first.as_payload()["replayed"] is False
    assert replay.as_payload()["replayed"] is True


def test_unknown_tool_name_is_not_semantically_guessed_to_be_a_bypass():
    for name in ("check_execution_status", "request_user_input", "read_file", "get_network_summary"):
        assert native_boundary_attempt(ToolCall(call_id=name, tool=name, arguments={}), source="native") is None


def test_native_harness_runner_emits_boundary_attempt_lifecycle_and_ledger(tmp_path: Path, monkeypatch) -> None:
    trial_id = "trial-native-boundary"
    ledger = PlatformLedger(tmp_path / "ledger")
    observed: list[object] = []

    class Permissions:
        def runtime_context(self, requested_trial_id):
            assert requested_trial_id == trial_id
            return {
                "platform_ledger_root": str(ledger.root),
                "mcp_token": "m" * 40,
                "harness_channel_token": "h" * 40,
                "mcp_token_state_files": {},
            }

    class Supervisor:
        stopped = False

        def start_trial(self, **_kwargs):
            return {
                "RESBENCH_K8S_MCP_URL": "http://127.0.0.1:18081/mcp",
                "RESBENCH_TELEMETRY_MCP_URL": "http://127.0.0.1:18082/mcp",
                "RESBENCH_SOURCE_MCP_URL": "http://127.0.0.1:18083/mcp",
                "RESBENCH_CHAOS_CONTROL_MCP_URL": "http://127.0.0.1:18084/mcp",
                "RESBENCH_HARNESS_CHANNEL_MCP_URL": "http://127.0.0.1:18085/mcp",
                "RESBENCH_BLADEAI_HARNESS_CHANNEL_MCP_SSE_URL": "http://127.0.0.1:18185/sse",
                "RESBENCH_BLADEAI_K8S_MCP_SSE_URL": "http://127.0.0.1:18181/sse",
                "RESBENCH_BLADEAI_TELEMETRY_MCP_SSE_URL": "http://127.0.0.1:18182/sse",
                "RESBENCH_BLADEAI_SOURCE_MCP_SSE_URL": "http://127.0.0.1:18183/sse",
                "RESBENCH_BLADEAI_CHAOS_CONTROL_MCP_SSE_URL": "http://127.0.0.1:18184/sse",
            }

        def stop(self):
            self.stopped = True

    def fake_process(
        argv,
        stdin,
        child_env,
        timeout_seconds,
        stdout_observer,
        cancel_requested,
        **_kwargs,
    ):
        del argv, stdin, child_env, timeout_seconds, cancel_requested
        line = json.dumps(
            {
                "type": "command_execution",
                "id": "cmd-1",
                "command": "kubectl get pods -A",
                "timestamp": "2026-09-05T12:00:00Z",
            }
        ).encode() + b"\n"
        stdout_observer(line)
        stdout_observer(line)
        return CommandResult(returncode=0, stdout=line * 2, stderr=b"")

    monkeypatch.setattr("stage2_service.harness_runtime.subprocess_streaming_runner", fake_process)
    monkeypatch.setattr("stage2_service.harness_runtime.build_argv", lambda *_args, **_kwargs: (["codex"], b"", False))
    monkeypatch.setattr("stage2_service.harness_runtime.render_codex_config", lambda *_args, **_kwargs: tmp_path / "codex.toml")
    monkeypatch.setattr("stage2_service.harness_runtime.render_claude_config", lambda *_args, **_kwargs: tmp_path / "mcp.json")
    monkeypatch.setattr("stage2_service.harness_runtime.render_dsh_contract", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("stage2_service.harness_runtime.child_env_for_harness", lambda _h, env, _homes: dict(env))
    monkeypatch.setattr("stage2_service.harness_runtime.load_yaml", lambda *_args, **_kwargs: {"harnesses": {"codex": {"entrypoint": {"command": "codex"}}}})

    runner = NativeHarnessRunner(
        repo_root=ROOT,
        private_root=tmp_path / "private",
        artifact_root=tmp_path / "artifacts",
        permissions=Permissions(),
        mcp_supervisor=Supervisor(),
        base_environment={"RESBENCH_CODEX_EVAL_BIN": str(tmp_path / "codex-eval")},
        local_test_execution=True,
        responder_factory=lambda *_args, **_kwargs: SimpleNamespace(history=[]),
    )
    monkeypatch.setattr(runner, "_resolve_executable", lambda *_args: "/fixture/codex")

    report = runner.run(
        campaign_id="campaign-native-boundary",
        trial_id=trial_id,
        harness=HarnessKind.CODEX,
        model_alias="fixture-model",
        episode=None,
        runtime_context=_trial_runtime(trial_id),
        capability=_capability(HarnessKind.CODEX),
        case=default_case_specs((Stage2CaseId.C0,))[0],
        base_prompt="fixture",
        prompt_mode=PromptMode.VERBATIM,
        event_observer=lambda event: observed.append(event) or [],
    )

    assert report.status == "completed"
    ledger_events = ledger.query(trial_id=trial_id, limit=1000)
    attempts = [event for event in ledger_events if event.event_type == PERMISSION_BYPASS_EVENT]
    assert len(attempts) == 1
    assert attempts[0].payload["call_id"] == "cmd-1"
    assert attempts[0].payload["source"] == CLI_NATIVE_SOURCE
    assert attempts[0].payload["semantics"] == "attempt_only"
    assert attempts[0].payload["physical_operation_proven"] is False
    lifecycle = [event for event in report.lifecycle_events if event.kind == PERMISSION_BYPASS_LIFECYCLE_KIND]
    assert len(lifecycle) == 1
    assert lifecycle[0].payload["call_id"] == "cmd-1"
    assert any(
        isinstance(event, dict)
        and event.get("event_type") == "TOOL_INTERACTION"
        and event.get("tool") == "command_execution"
        for event in observed
    )
