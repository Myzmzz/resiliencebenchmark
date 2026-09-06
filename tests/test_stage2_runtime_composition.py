from __future__ import annotations

from pathlib import Path
from threading import Lock
from types import SimpleNamespace

import yaml

from stage2_service import runtime_factory
from stage2_service.contracts import (
    CampaignRequest,
    FixedEpisodeRef,
    HarnessKind,
    MainFaultSpec,
    TargetSpec,
)
from stage2_service.harness_runtime import NativeHarnessRunner
from stage2_service.runtime_factory import Stage2Components, Stage2RuntimeConfig


class DummyPreparer:
    def __init__(self, issuer):
        self.issuer = issuer


class DummyKubernetesClient:
    def __init__(self, kubeconfig: Path):
        self.kubeconfig = kubeconfig


def _config(tmp_path: Path) -> Stage2RuntimeConfig:
    kubeconfig = tmp_path / "private" / "service.kubeconfig"
    kubeconfig.parent.mkdir(parents=True)
    token_file = kubeconfig.parent / "projected-token"
    token_file.write_text("test-only-token")
    kubeconfig.write_text(yaml.safe_dump({
        "apiVersion": "v1", "kind": "Config",
        "clusters": [{"name": "kubernetes", "cluster": {"server": "https://127.0.0.1:6443", "certificate-authority-data": "Y2E="}}],
        "users": [{"name": "controller", "user": {"tokenFile": str(token_file)}}],
        "contexts": [{"name": "controller", "context": {"cluster": "kubernetes", "user": "controller", "namespace": "otel-demo"}}],
        "current-context": "controller",
    }))
    kubeconfig.chmod(0o600)
    runtime_env = tmp_path / "otel-demo.env"
    runtime_env.write_text("", encoding="utf-8")
    chart = tmp_path / "otel-demo.tgz"
    chart.write_bytes(b"chart")
    source_root = tmp_path / "sources"
    source_root.mkdir()
    artifacts = tmp_path / "artifacts"
    return Stage2RuntimeConfig(
        repo_root=Path(__file__).resolve().parents[1],
        private_root=tmp_path / "private",
        artifact_root=artifacts,
        runtime_env_file=runtime_env,
        source_root=source_root,
        otel_chart_file=chart,
        kubeconfig=kubeconfig,
        controller_pod_name="stage2-controller",
        controller_pod_uid="controller-uid",
        controller_pod_namespace="resiliencebenchmark-system",
        llm_base_url="http://llm-gateway.local",
        llm_api_key="test-key",
        d0_artifact_root=None,
    )


def _request() -> CampaignRequest:
    return CampaignRequest(
        request_id="runtime-composition-001",
        episode=FixedEpisodeRef(
            internal_path="tasks/episodes/otel-demo/EPI-OTEL-CART-DEADLINE-001/episode-internal.yaml",
            public_path="tasks/episodes/otel-demo/EPI-OTEL-CART-DEADLINE-001/episode-public.yaml",
            episode_id="EPI-OTEL-CART-DEADLINE-001",
            internal_sha256="a" * 64,
            public_sha256="b" * 64,
        ),
        harnesses=(HarnessKind.BLADEAI,),
        model_by_harness={HarnessKind.BLADEAI: "qwen3.8-max"},
        target=TargetSpec(namespace="otel-demo", component="cart"),
        main_fault=MainFaultSpec(
            fault_type="network-delay",
            duration_seconds=180,
            intensity={"delay_ms": 1000},
        ),
    )


def test_build_runtime_composes_native_runner_without_disabling_isolation(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        runtime_factory.KubernetesTrialPreparer,
        "from_incluster",
        lambda issuer: DummyPreparer(issuer),
    )
    monkeypatch.setattr(
        runtime_factory.KubernetesDisturbanceClient,
        "from_kubeconfig",
        lambda kubeconfig: DummyKubernetesClient(kubeconfig),
    )

    components = runtime_factory._build_runtime(
        config=_config(tmp_path),
        episode=SimpleNamespace(episode_id="EPI-OTEL-CART-DEADLINE-001"),
        request_model_by_harness={HarnessKind.BLADEAI: "qwen3.8-max"},
        namespace="otel-demo",
    )

    assert isinstance(components, Stage2Components)
    assert isinstance(components.harness_runner, NativeHarnessRunner)
    assert components.permissions.token_registry is components.token_registry
    assert components.harness_runner.permissions is components.permissions
    assert components.harness_runner.mcp_supervisor is components.supervisor
    assert components.disturbance_executor.mcp_tokens is components.token_registry
    assert components.disturbance_executor.target_rebinder is components.issuer
    assert components.disturbance_executor.mcp_supervisor is components.supervisor
    assert components.finalizer.chaos is components.cleanup_backend
    assert components.resetter.environment_gate is components.gate
    assert components.resetter.traffic_evidence is components.traffic
    assert components.preparer.issuer is components.issuer
    assert components.harness_runner.local_test_execution is False
    assert components.harness_runner.agent_exec_client is not None
    assert components.harness_runner.capability_loss_factory is not None
    assert components.supervisor.base_environment["RESBENCH_CHAOS_EXECUTE_ENABLED"] == "true"
    environment = components.supervisor.base_environment
    execution_path = environment["RESBENCH_CHAOS_KUBECONFIG"]
    cleanup_path = environment["RESBENCH_CHAOS_CLEANUP_KUBECONFIG"]
    assert execution_path != cleanup_path
    assert yaml.safe_load(Path(execution_path).read_text())["users"][0]["user"]["as"].endswith(":resbench-stage2-executor")
    assert yaml.safe_load(Path(cleanup_path).read_text())["users"][0]["user"]["as"].endswith(":resbench-stage2-finalizer")
    assert components.cleanup_backend.kubeconfig == cleanup_path
    assert all(service.config.kubeconfig == cleanup_path and service.config.cleanup_kubeconfig == cleanup_path
               for service in components.cleanup_backend.services.values())
    assert "RESBENCH_CHAOS_CLEANUP_KUBECONFIG" not in components.harness_runner.base_environment
    assert components.supervisor.base_environment["RESBENCH_K8S_RO_NAMESPACE_ALLOWLIST"] == "otel-demo"
    assert components.supervisor.base_environment["RESBENCH_TELEMETRY_ALLOWED_NAMESPACES"] == "otel-demo"
    assert components.harness_runner.base_environment["STAGE2_BLADEAI_MODEL"] == "qwen3.8-max"
    assert components.harness_runner.base_environment["RESBENCH_LLM_API_KEY"] == "test-key"
    assert components.harness_runner.agent_work_root == Path("/var/lib/resbench-stage2/agent-trials").resolve()
    assert components.harness_runner.sandbox_work_root == Path("/var/lib/resbench-stage2/sandbox-trials").resolve()


def test_stage2_system_run_consumes_built_components(tmp_path: Path, monkeypatch):
    request = _request()
    episode = SimpleNamespace(episode_id="EPI-OTEL-CART-DEADLINE-001")
    traffic_events: list[str] = []
    supervisor_events: list[str] = []
    captured: dict[str, object] = {}
    system = object.__new__(runtime_factory.Stage2System)
    system.config = _config(tmp_path)
    system.d0_gate = object()
    system._active_lock = Lock()
    system._active_controls = {}

    class Traffic:
        def start_sampling(self):
            traffic_events.append("start")

        def close(self):
            traffic_events.append("close")

    class Supervisor:
        def stop(self):
            supervisor_events.append("stop")

    components = Stage2Components(
        gate=object(),
        traffic=Traffic(),
        permissions=object(),
        issuer=object(),
        preparer=object(),
        supervisor=Supervisor(),
        harness_runner=object(),
        cleanup_backend=object(),
        finalizer=object(),
        resetter=object(),
        disturbance_executor=object(),
        token_registry=SimpleNamespace(platform_ledger=object()),
    )

    def fake_build_runtime(*, config, episode, request_model_by_harness, namespace):
        captured["build_config"] = config
        captured["build_episode"] = episode
        captured["models"] = request_model_by_harness
        captured["namespace"] = namespace
        return components

    class FakeCampaignEngine:
        def __init__(self, **kwargs):
            captured["engine_kwargs"] = kwargs

        def run(self, campaign_request, event_observer=None, stop_requested=None):
            captured["active_during_run"] = dict(system._active_controls)
            captured["run_request"] = campaign_request
            return "campaign-result"

    monkeypatch.setattr(runtime_factory, "load_fixed_episode", lambda _ref, root: episode)
    monkeypatch.setattr(runtime_factory, "_build_runtime", fake_build_runtime)
    monkeypatch.setattr(runtime_factory, "CampaignEngine", FakeCampaignEngine)

    result = system.run(request)

    assert result == "campaign-result"
    assert captured["build_config"] is system.config
    assert captured["build_episode"] is episode
    assert captured["models"] is request.model_by_harness
    assert captured["namespace"] == "otel-demo"
    engine_kwargs = captured["engine_kwargs"]
    assert engine_kwargs["environment_gate"] is components.gate
    assert engine_kwargs["preparer"] is components.preparer
    assert engine_kwargs["permissions"] is components.permissions
    assert engine_kwargs["harness_runner"] is components.harness_runner
    assert engine_kwargs["disturbance_executor"] is components.disturbance_executor
    assert engine_kwargs["finalizer"] is components.finalizer
    assert engine_kwargs["resetter"] is components.resetter
    assert engine_kwargs["platform_ledger"] is components.token_registry.platform_ledger
    assert captured["active_during_run"][request.request_id]["permissions"] is components.permissions
    assert captured["active_during_run"][request.request_id]["resetter"] is components.resetter
    assert captured["active_during_run"][request.request_id]["episode"] is episode
    assert system._active_controls == {}
    assert traffic_events == ["start", "close"]
    assert supervisor_events == ["stop"]
