"""D0 composition tests; CLI, model and cluster are explicit test doubles."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from harness.d0.adapters import NativeD0Adapter
from harness.d0.behavior import derive_agent_behavior
from harness.d0.campaign import D0Campaign, D0CampaignConfig
from harness.d0.common import FIXED_PROMPT, append_jsonl
from stage2_service.contracts import AgentVerdict, HarnessKind, HarnessReport, PromptMode
from stage2_service.gateway_config import GatewayConfigSnapshot
from stage2_service.runtime_factory import Stage2RuntimeConfig


ROOT = Path(__file__).resolve().parents[1]
GATEWAY_HASH = "a" * 64
GATEWAY_ROUTE = {
    "model_alias": "gpt-5.5",
    "provider": "openai",
    "upstream_model": "gpt-5.5",
    "api_base_host": "gateway.example",
    "api_base_scheme": "https",
    "api_base_path": "/v1",
    "credential_env_ref": "UPSTREAM_API_KEY",
}


@pytest.mark.parametrize("harness", list(HarnessKind))
def test_all_d0_agents_use_one_runtime_and_only_authenticated_tool_evidence(tmp_path, harness):
    calls = []
    runtime = SimpleNamespace(model_copy=lambda **_kwargs: runtime)
    capability = object()
    trial_dir = tmp_path / "d0-campaign" / harness.value
    trial_dir.mkdir(parents=True)

    class Runner:
        artifact_root = tmp_path
        timeout_seconds = 0

        def run(self, **kwargs):
            calls.append(("run", kwargs))
            assert kwargs["harness"] is harness
            assert kwargs["base_prompt"] == FIXED_PROMPT
            assert kwargs["prompt_mode"] is PromptMode.VERBATIM
            assert kwargs["runtime_context"] is runtime
            assert kwargs["capability"] is capability
            assert kwargs["cancel_requested"]() is False
            observe = kwargs["event_observer"]
            for tool, source, native in (
                ("chaos_control.chaos_create_experiment", "native_stream", "tool_call"),
                ("k8s_ro.k8s_list_resources", "mcp_server", "tool_call"),
                ("chaos_control.chaos_create_experiment", "mcp_server", "tool_call"),
                ("chaos_control.chaos_get_experiment", "mcp_server", "tool_call"),
                ("chaos_control.chaos_destroy_experiment", "mcp_server", "tool_call"),
                ("chaos_control.chaos_recovery_status", "mcp_server", "tool_call"),
                ("harness_channel.harness_confirm", "mcp_server", "tool_result"),
            ):
                observe({"event_type": "TOOL_INTERACTION", "native_type": native,
                         "tool": tool, "payload": {"source": source, "call_id": tool,
                         "result": {"ok": True, "allowed": True}}})
            observe({"event_type": "AGENT_MESSAGE", "native_type": "agent_message", "payload": {
                "source": "native_stream", "structured": {"tool": "chaos_create_experiment"}}})
            ref = f"d0-campaign/{kwargs['trial_id']}/session-events.jsonl"
            path = self.artifact_root / ref
            path.parent.mkdir(parents=True)
            path.write_text('{"event":"native_turn_completed"}\n')
            runtime_request_ref = f"d0-campaign/{kwargs['trial_id']}/runtime-request.redacted.json"
            (self.artifact_root / runtime_request_ref).write_text(
                json.dumps(
                    {
                        "trial_id": kwargs["trial_id"],
                        "model": "gpt-5.5",
                        "gateway_route": GATEWAY_ROUTE,
                        "gateway_config_sha256": GATEWAY_HASH,
                    }
                ),
                encoding="utf-8",
            )
            gateway_ref = f"d0-campaign/{kwargs['trial_id']}/gateway-requests.json"
            (self.artifact_root / gateway_ref).write_text(
                json.dumps(
                    [
                        {
                            "trial_id": kwargs["trial_id"],
                            "harness": harness.value,
                            "model_alias": "gpt-5.5",
                            "request_id": "req-1",
                            "gateway_config_sha256": GATEWAY_HASH,
                            "outcome": "received",
                        }
                    ]
                ),
                encoding="utf-8",
            )
            return HarnessReport(status="completed", agent_verdict=AgentVerdict.INCONCLUSIVE,
                                 lifecycle_events=(), artifact_refs=(runtime_request_ref, gateway_ref, ref),
                                 final_output={
                                     "trial_id": kwargs["trial_id"],
                                     "model_alias": "gpt-5.5",
                                     "gateway_route": GATEWAY_ROUTE,
                                     "gateway_config_sha256": GATEWAY_HASH,
                                     "gateway_evidence_verified": True,
                                     "gateway_request_ids": ["req-1"],
                                     "gateway_evidence_ref": "gateway-requests.json",
                                 })

    components = SimpleNamespace(
        traffic=SimpleNamespace(start_sampling=lambda: calls.append("sampling"), close=lambda: calls.append("traffic_closed")),
        preparer=SimpleNamespace(prepare=lambda *args, **kwargs: (
            calls.append(("prepare", kwargs)), runtime)[1]),
        permissions=SimpleNamespace(provision=lambda *args: capability, restore=lambda trial: calls.append(("revoked", trial))),
        supervisor=SimpleNamespace(stop=lambda: calls.append("mcp_stopped")),
        harness_runner=Runner(),
        cleanup_backend=SimpleNamespace(kubeconfig=str(tmp_path / "custom-runtime-finalizer.kubeconfig")),
    )
    builder_calls = []

    def build(episode, models):
        builder_calls.append(models)
        return components

    adapter = NativeD0Adapter(name=harness.value, repo_root=ROOT, model_alias="gpt-5.5",
                             runtime_builder=build, timeout_seconds=720)
    result = adapter.run(prompt=FIXED_PROMPT, trial_id="d0-one", artifact_dir=trial_dir,
                         event_sink=lambda row: append_jsonl(trial_dir / "all-events.jsonl", row))
    assert builder_calls == [{harness: "gpt-5.5"}]
    assert adapter.cleanup_kubeconfig == tmp_path / "custom-runtime-finalizer.kubeconfig"
    assert calls[1][1] == {"namespace": "otel-demo", "target": None, "main_fault": None}
    assert result.status == "finished" and result.tool_calls == 5
    assert result.confirmations == 1 and result.native_session_trace_captured
    assert result.agent_recovery_requested
    assert result.model_alias == "gpt-5.5"
    assert result.gateway_config_sha256 == GATEWAY_HASH
    assert result.gateway_route == GATEWAY_ROUTE
    assert result.gateway_evidence_verified is True
    assert result.gateway_request_ids == ("req-1",)
    assert result.gateway_evidence_ref == "native/d0-campaign/d0-one/gateway-requests.json"
    assert result.gateway_trial_id == "d0-one"
    assert calls[-3:] == ["mcp_stopped", "traffic_closed", ("revoked", "d0-one")]
    behavior = derive_agent_behavior(trial_dir)
    assert behavior["agent_target_discovered"]
    assert behavior["agent_effect_check_observed"]
    assert behavior["agent_recovery_check_observed"]
    assert len(behavior["recognized_tool_events"]) == 5


def test_native_d0_adapter_fails_without_gateway_evidence(tmp_path):
    runtime = SimpleNamespace(model_copy=lambda **_kwargs: runtime)
    capability = object()
    trial_dir = tmp_path / "d0-campaign" / "codex"
    trial_dir.mkdir(parents=True)

    class Runner:
        artifact_root = tmp_path
        timeout_seconds = 0

        def run(self, **kwargs):
            ref = f"d0-campaign/{kwargs['trial_id']}/session-events.jsonl"
            path = self.artifact_root / ref
            path.parent.mkdir(parents=True)
            path.write_text('{"event":"native_turn_completed"}\n')
            return HarnessReport(
                status="completed",
                agent_verdict=AgentVerdict.INCONCLUSIVE,
                lifecycle_events=(),
                artifact_refs=(ref,),
                final_output={
                    "model_alias": "gpt-5.5",
                    "gateway_route": GATEWAY_ROUTE,
                    "gateway_config_sha256": GATEWAY_HASH,
                },
            )

    components = SimpleNamespace(
        traffic=SimpleNamespace(start_sampling=lambda: None, close=lambda: None),
        preparer=SimpleNamespace(prepare=lambda *args, **kwargs: runtime),
        permissions=SimpleNamespace(provision=lambda *args: capability, restore=lambda _trial: None),
        supervisor=SimpleNamespace(stop=lambda: None),
        harness_runner=Runner(),
        cleanup_backend=SimpleNamespace(kubeconfig=str(tmp_path / "custom-runtime-finalizer.kubeconfig")),
    )

    adapter = NativeD0Adapter(
        name="codex",
        repo_root=ROOT,
        model_alias="gpt-5.5",
        runtime_builder=lambda _episode, _models: components,
        timeout_seconds=720,
    )

    result = adapter.run(
        prompt=FIXED_PROMPT,
        trial_id="d0-missing-gateway",
        artifact_dir=trial_dir,
        event_sink=lambda row: append_jsonl(trial_dir / "all-events.jsonl", row),
    )

    assert result.status == "failed"
    assert result.failure_code == "GATEWAY_EVIDENCE_MISSING"
    assert result.gateway_route == GATEWAY_ROUTE
    assert result.gateway_evidence_verified is False
    assert result.gateway_request_ids == ()
    assert result.gateway_evidence_ref == ""


def test_native_d0_adapter_rejects_invalid_gateway_request_ids(tmp_path):
    native_root = tmp_path / "native"
    gateway_ref = "d0-campaign/d0-invalid/gateway-requests.json"
    gateway_path = native_root / gateway_ref
    gateway_path.parent.mkdir(parents=True)
    gateway_path.write_text(
        json.dumps(
            [
                {
                    "trial_id": "d0-invalid",
                    "harness": "codex",
                    "model_alias": "gpt-5.5",
                    "request_id": "req-1",
                    "gateway_config_sha256": GATEWAY_HASH,
                    "outcome": "received",
                }
            ]
        ),
        encoding="utf-8",
    )
    report = HarnessReport(
        status="completed",
        agent_verdict=AgentVerdict.INCONCLUSIVE,
        lifecycle_events=(),
        artifact_refs=(gateway_ref,),
        final_output={
            "trial_id": "d0-invalid",
            "model_alias": "gpt-5.5",
            "gateway_route": GATEWAY_ROUTE,
            "gateway_config_sha256": GATEWAY_HASH,
            "gateway_evidence_verified": True,
            "gateway_request_ids": ["req-1", 123],
            "gateway_evidence_ref": "gateway-requests.json",
        },
    )
    adapter = NativeD0Adapter(
        name="codex",
        repo_root=ROOT,
        model_alias="gpt-5.5",
        runtime_builder=lambda _episode, _models: None,
        timeout_seconds=720,
    )

    metadata = adapter._gateway_metadata(
        report=report,
        native_root=native_root,
        trial_id="d0-invalid",
        harness="codex",
    )

    assert metadata["gateway_evidence_verified"] is False
    assert metadata["gateway_request_ids"] == ()
    assert metadata["gateway_evidence_ref"] == ""


def test_default_d0_registry_contains_four_identical_boundary_adapters(tmp_path, monkeypatch):
    monkeypatch.delenv("STAGE2_HARNESS_CAPABILITIES_FILE", raising=False)
    campaign = D0Campaign(D0CampaignConfig(repo_root=ROOT, artifact_root=tmp_path, kubeconfig=tmp_path / "cluster"))
    assert set(campaign.adapters) == {kind.value for kind in HarnessKind}
    assert all(type(value) is NativeD0Adapter for value in campaign.adapters.values())
    assert not any(campaign._runtime_descriptors()[kind.value]["available"] for kind in HarnessKind)


def test_d0_runtime_builder_preserves_gateway_snapshot_loaded_by_from_env(tmp_path, monkeypatch):
    from stage2_service import runtime_factory

    config_path = tmp_path / "litellm.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "model_list": [
                    {
                        "model_name": alias,
                        "litellm_params": {
                            "model": f"openai/{alias}",
                            "api_base": "https://gateway.example/v1",
                            "api_key": "os.environ/UPSTREAM_API_KEY",
                        },
                    }
                    for alias in (
                        "gpt-5.5",
                        "claude-opus-5",
                        "deepseek-v4-pro-0813",
                        "deepseek-v4-flash-0731",
                        "qwen3.8-max",
                        "qwen3.8-flash",
                    )
                ]
            }
        ),
        encoding="utf-8",
    )
    snapshot = GatewayConfigSnapshot.from_file(
        config_path,
        required_aliases=(
            "gpt-5.5",
            "claude-opus-5",
            "deepseek-v4-pro-0813",
            "deepseek-v4-flash-0731",
            "qwen3.8-max",
            "qwen3.8-flash",
        ),
    )
    base_config = Stage2RuntimeConfig(
        repo_root=ROOT,
        private_root=tmp_path / "private",
        artifact_root=tmp_path / "artifacts",
        runtime_env_file=tmp_path / "runtime.env",
        source_root=tmp_path / "sources",
        otel_chart_file=tmp_path / "chart.tgz",
        kubeconfig=tmp_path / "service.kubeconfig",
        controller_pod_name="controller",
        controller_pod_uid="controller-uid",
        controller_pod_namespace="resiliencebenchmark-system",
        llm_base_url="http://127.0.0.1:4000/v1",
        llm_api_key="runtime-key",
        d0_artifact_root=None,
        gateway_config_file=config_path,
        gateway_snapshot=snapshot,
    )
    captured = {}

    class FakeSystem:
        def __init__(self, config):
            captured["config"] = config

        def build_runtime(self, episode, models):
            captured["episode"] = episode
            captured["models"] = models
            return "components"

    monkeypatch.setattr(runtime_factory.Stage2RuntimeConfig, "from_env", lambda env: base_config)
    monkeypatch.setattr(runtime_factory, "Stage2System", FakeSystem)

    campaign = D0Campaign(
        D0CampaignConfig(repo_root=ROOT, artifact_root=tmp_path / "d0", kubeconfig=tmp_path / "cluster"),
        environment={"STAGE2_LITELLM_CONFIG_FILE": str(config_path)},
        adapters={},
    )

    result = campaign._build_runtime("episode", {HarnessKind.CODEX: "gpt-5.5"})

    assert result == "components"
    assert captured["config"].gateway_snapshot is snapshot
    assert captured["config"].gateway_config_file == config_path
    assert captured["config"].private_root == tmp_path / "d0/.runtime-private"
    assert captured["config"].artifact_root == tmp_path / "d0/.native-runtime"


def test_d0_oracle_does_not_attribute_other_uid_cpu_or_ignore_mesh(tmp_path):
    from harness.d0.observer import KubectlD0Observer

    observer = KubectlD0Observer(kubeconfig=tmp_path / "cluster", artifact_dir=tmp_path, trial_id="run-1")
    observer.state.baseline_cpu = {"accounting": 5}
    observer.state.baseline_uid = {"accounting": "original-uid"}
    observer._apply({
        "ts": "2026-09-05T12:00:00Z",
        "pods": [{"name": "accounting", "uid": "original-uid", "cpu_millicores": 900}],
        "chaosblades": [{"name": "our-wrong-target", "run_id": "run-1", "owner": "chaos_control", "target_uid": "other-uid"}],
        "chaos_mesh": [{"resource": "networkchaos.chaos-mesh.org", "name": "foreign-mesh"}],
    })
    assert observer.state.new_cr_names == {"our-wrong-target"}
    assert observer.state.effect_confirmed_at is None
    assert observer.state.foreign_cr_names == {"chaos_mesh/networkchaos.chaos-mesh.org/foreign-mesh"}


def test_d0_unknown_cpu_is_not_a_healthy_baseline(tmp_path, monkeypatch):
    from harness.d0.observer import KubectlD0Observer

    observer = KubectlD0Observer(kubeconfig=tmp_path / "cluster", artifact_dir=tmp_path, trial_id="run-1")
    samples = iter([
        {"pods": [{"name": "accounting", "uid": "u", "ready": True, "cpu_millicores": value}],
         "chaosblades": [], "chaos_mesh": []}
        for value in (None, 5)
    ])
    monkeypatch.setattr(observer, "snapshot", lambda: next(samples))
    monkeypatch.setattr("harness.d0.observer.time.sleep", lambda _seconds: None)
    result = observer.prepare(convergence_timeout_seconds=1)
    assert result["pods"][0]["cpu_millicores"] == 5
    assert observer.state.baseline_uid == {"accounting": "u"}


@pytest.mark.parametrize("percent,phase,expected", [(40, "Success", False), (80, "Success", True), (None, "Success", False), (80, "Error", False)])
def test_d0_effect_requires_the_requested_cpu_intensity(tmp_path, percent, phase, expected):
    from harness.d0.observer import KubectlD0Observer

    observer = KubectlD0Observer(kubeconfig=tmp_path / "cluster", artifact_dir=tmp_path, trial_id="run")
    observer.state.baseline_cpu = {"accounting": 5}
    observer.state.baseline_uid = {"accounting": "uid"}
    observer._apply({
        "ts": "2026-09-05T12:00:00Z", "pods": [{"name": "accounting", "uid": "uid", "cpu_millicores": 800}],
        "chaosblades": [{"name": "cr", "run_id": "run", "owner": "chaos_control", "target_uid": "uid", "fault_type": "cpu-load", "cpu_percent": percent, "phase": phase}],
    })
    assert bool(observer.state.effect_confirmed_at) is expected


def test_d0_replay_does_not_adopt_untagged_blade_resources(tmp_path):
    import json
    from harness.d0.recompute import recompute_trial

    trial = tmp_path / "bladeai"
    trial.mkdir()
    (trial / "result.json").write_text(json.dumps({"adapter": {"status": "finished"}}))
    before = {"ts": "2026-09-05T12:00:00Z", "phase": "before",
              "pods": [{"name": "accounting", "uid": "uid", "ready": True, "cpu_millicores": 5}], "chaosblades": []}
    append_jsonl(trial / "oracle-samples.jsonl", before)
    append_jsonl(trial / "oracle-samples.jsonl", {
        **before, "ts": "2026-09-05T12:00:10Z", "phase": "watch",
        "pods": [{**before["pods"][0], "cpu_millicores": 900}],
        "chaosblades": [{"name": "a" * 16, "target_names": ["accounting"]}],
    })
    result = recompute_trial(trial, "bladeai")
    assert result["status"] == "CASE_INVALID"
    assert result["injection_observed"] is False
    assert result["foreign_crs_observed"] == ["a" * 16]


def test_d0_cleanup_commands_select_finalizer_identity_without_primary_fallback(tmp_path):
    import subprocess
    from harness.d0.observer import KubectlD0Observer

    commands = []

    def runner(argv, timeout):
        commands.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    observer = KubectlD0Observer(kubeconfig=tmp_path / "read.kubeconfig", cleanup_kubeconfig=tmp_path / "cleanup.kubeconfig",
                               artifact_dir=tmp_path, trial_id="run", runner=runner)
    observer._run(["get", "pods"])
    observer._run(["delete", "chaosblades.chaosblade.io", "owned"], identity="finalizer")
    assert commands[0][2] == str(tmp_path / "read.kubeconfig")
    assert commands[1][2] == str(tmp_path / "cleanup.kubeconfig")
    observer.cleanup_kubeconfig = None
    with pytest.raises(RuntimeError, match="separate finalizer"):
        observer._run(["delete", "chaosblades.chaosblade.io", "owned"], identity="finalizer")
    assert len(commands) == 2
    observer.cleanup_kubeconfig = lambda: tmp_path / "custom-cleanup.kubeconfig"
    observer._run(["delete", "chaosblades.chaosblade.io", "owned"], identity="finalizer")
    assert commands[-1][2] == str(tmp_path / "custom-cleanup.kubeconfig")
