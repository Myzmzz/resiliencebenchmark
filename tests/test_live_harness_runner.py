from __future__ import annotations

from pathlib import Path

from controller.runtime_secrets import PrivateRuntimeSecretStore
from controller.trial_preparation import TrialRuntimeContextStore
from harness.live_runner import LiveHarnessTrialRunner
from progression.controller import TrialTicket


def _ticket() -> TrialTicket:
    return TrialTicket(
        run_id="run-live-test",
        episode_id="episode-1",
        level_id="L2",
        attempt=1,
        trial_id="run-live-test-L2-a1",
    )


def test_live_runner_forwards_events_before_return_and_keeps_disturbance_hidden(
    tmp_path: Path,
) -> None:
    lifecycle = []
    executor_arguments = {}

    def emit(event):
        lifecycle.append(event)
        return []

    def executor(**kwargs):
        executor_arguments.update(kwargs)
        observer = kwargs["event_observer"]
        observer(
            {
                "type": "mcp_tool_call",
                "server": "chaos_control",
                "tool": "chaos_create_experiment",
                "status": "completed",
                "result": {"state": "Running"},
            }
        )
        observer(
            {
                "type": "mcp_tool_call",
                "server": "telemetry_ro",
                "tool": "telemetry_prom_metric_range",
                "status": "in_progress",
            }
        )
        return {"status": "completed", "runTraceRef": "trace.json"}

    public_episode = tmp_path / "episode-public.yaml"
    public_episode.write_text("schema_version: episode-public.v0.1\n", encoding="utf-8")
    runner = LiveHarnessTrialRunner(
        repo_root=tmp_path,
        public_episode_file=public_episode,
        harness_name="codex",
        model_alias="gpt-5.6",
        artifact_root=tmp_path / "artifacts",
        trial_executor=executor,
    )
    level = {
        "level_id": "L2",
        "disturbances": [{"type": "target_drift", "replay_seed": 123}],
    }

    report = runner(_ticket(), level, emit)

    assert report["status"] == "completed"
    assert report["mainFaultAppliedObserved"] is True
    assert any(event["kind"] == "main_fault_applied" for event in report["lifecycleEvents"])
    assert executor_arguments["trial_id"] == "run-live-test-L2-a1"
    assert "disturbances" not in executor_arguments
    assert [event.kind for event in lifecycle] == [
        "trial_started",
        "tool_result",
        "main_fault_applied",
        "observation_started",
        "tool_call",
        "trial_finished",
    ]


def test_live_runner_emits_failure_lifecycle_when_executor_raises(tmp_path: Path) -> None:
    lifecycle = []

    def executor(**_kwargs):
        raise RuntimeError("harness crashed")

    runner = LiveHarnessTrialRunner(
        repo_root=tmp_path,
        public_episode_file=tmp_path / "episode.yaml",
        harness_name="codex",
        model_alias="gpt-5.6",
        artifact_root=tmp_path / "artifacts",
        trial_executor=executor,
    )

    try:
        runner(_ticket(), {"level_id": "L2"}, lambda event: lifecycle.append(event) or [])
    except RuntimeError:
        pass

    assert lifecycle[-1].kind == "trial_finished"
    assert lifecycle[-1].payload["status"] == "runner_failed"


def test_live_runner_injects_ephemeral_capability_without_adding_it_to_level(
    tmp_path: Path,
) -> None:
    contexts = TrialRuntimeContextStore(tmp_path / "private" / "contexts")
    secrets = PrivateRuntimeSecretStore(tmp_path / "private" / "secrets")
    token_ref = secrets.put(
        "run-live-test-L2-a1",
        "baseline-gate-token",
        "baseline-token-with-at-least-thirty-two-characters",
    )
    contexts.save(
        "run-live-test-L2-a1",
        {
            "baseline_gate_token_ref": token_ref,
            "cleanup_handle": "cleanup-run-live-test-l2-a1",
            "target": {
                "namespace": "otel-demo",
                "kind": "Pod",
                "name": "frontend-abc",
                "uid": "pod-uid",
                "component": "frontend",
            },
        },
    )
    captured = {}

    def executor(**kwargs):
        captured.update(kwargs["parent_env"])
        return {"status": "completed"}

    runner = LiveHarnessTrialRunner(
        repo_root=tmp_path,
        public_episode_file=tmp_path / "episode.yaml",
        harness_name="codex",
        model_alias="gpt-5.6",
        artifact_root=tmp_path / "artifacts",
        trial_context_store=contexts,
        secret_store=secrets,
        controller_token_ref="runtime://controller/token",
        controller_pod_uid="controller-uid",
        main_fault={"type": "network-delay", "parameters": {"delay_ms": 100}},
        trial_executor=executor,
    )

    runner(_ticket(), {"level_id": "L2", "disturbances": []}, lambda _event: [])

    assert captured["RESBENCH_BASELINE_GATE_TOKEN"].startswith("baseline-token")
    assert captured["RESBENCH_CLEANUP_HANDLE"].startswith("cleanup-")
    assert "frontend-abc" in captured["RESBENCH_AUTHORIZED_TARGET_JSON"]
    assert captured["RESBENCH_AUTHORIZED_RUN_ID"] == "run-live-test"
