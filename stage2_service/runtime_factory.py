"""Production composition root for the Stage-2 single service."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import time
import urllib.request
import urllib.parse
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Any, Callable, Mapping, Sequence

import yaml

from mcp_servers.chaos_control.service import ChaosControlService, RuntimeConfig
from mcp_servers.chaos_mesh_control.service import ChaosMeshControlService

from disturbances.kubernetes_runtime import KubernetesDisturbanceClient

from .artifacts import ArtifactStore
from .request_observation import request_observability, target_request_effect, timestamp as _window_timestamp
from .campaign import CampaignEngine
from .capability_preflight import harness_capabilities_from_qualification
from .capability_loss.factory import CapabilityLossRuntimeFactory
from .condition_monitor import ConditionRecoveryMonitor
from .condition_policy import RESOURCE_METRICS
from .condition_policy import evaluate_condition
from .contracts import (
    STAGE2_BLADEAI_DEFAULT_MODEL,
    STAGE2_DEFAULT_MODEL,
    STAGE2_SUPPORTED_MODELS,
    CampaignRequest,
    CampaignResult,
    HarnessKind,
    default_case_specs,
)
from .disturbance import RuntimeDisturbancePlanner
from .episode import load_fixed_episode
from .evaluator import Stage2Evaluator
from .finalization import Stage2Finalizer
from .fault_inventory import resource_from_experiment, snapshot_for_trial
from .gateway_config import GatewayConfigError, GatewayConfigSnapshot
from .harness_runtime import NativeHarnessRunner
from harness.agent_exec.client import AgentExecClient
from .mcp_supervisor import McpSupervisor
from .matrix import fixed_otel_episode_ref
from .permissions import Stage2PermissionManager
from .preparation import ApplicationTrafficCapabilityIssuer, KubernetesTrialPreparer
from .qualification import D0QualificationGate
from .kubernetes_identities import CONTROLLER_SERVICE_ACCOUNT, prepare_execution_identities
from .reset import OtelDemoResetter
from .runtime_adapters import (
    CompositeDisturbanceExecutor,
    KubernetesEnvironmentGate,
    McpTokenStateRegistry,
)


class RuntimeConfigurationError(RuntimeError):
    pass


GatewayProbeRunner = Callable[[GatewayConfigSnapshot, Sequence[str]], Mapping[str, Any]]


def _utc_now_text() -> str:
    return datetime.now(UTC).isoformat()


def _gateway_probe_failure_report(error_type: str) -> dict[str, Any]:
    return {
        "schemaVersion": "resiliencebenchmark.model_probe/v1",
        "issues": [
            {
                "severity": "ERROR",
                "message": "gateway model probe failed",
                "errorType": error_type,
            }
        ],
        "models": [],
    }


def _probe_report_has_error(report: Mapping[str, Any]) -> bool:
    issues = report.get("issues")
    return any(
        isinstance(issue, Mapping) and issue.get("severity") == "ERROR"
        for issue in issues
    ) if isinstance(issues, list) else False


def _model_probe_failure_reason(failure_classes: tuple[str, ...]) -> str:
    """Expose a stable, actionable reason without echoing provider secrets."""

    if "quota_exhausted" in failure_classes:
        return "upstream model quota exhausted"
    if "authentication_or_permission" in failure_classes:
        return "upstream model authentication or permission rejected"
    if "capacity_transient" in failure_classes:
        return "upstream model capacity temporarily unavailable"
    if "rate_limited" in failure_classes:
        return "upstream model rate limited"
    return "gateway model capability probe failed"


@dataclass
class GatewayReadinessEntry:
    key: tuple[str, str, tuple[str, ...]]
    snapshot: GatewayConfigSnapshot
    status: str
    started_monotonic: float
    started_at: str
    completed_monotonic: float | None = None
    completed_at: str | None = None
    available_models: set[str] = field(default_factory=set)
    model_error: str | None = None
    probe_report: dict[str, Any] | None = None
    error_type: str | None = None
    error: str | None = None
    event: Event = field(default_factory=Event)


@dataclass(frozen=True)
class Stage2RuntimeConfig:
    repo_root: Path
    private_root: Path
    artifact_root: Path
    runtime_env_file: Path
    source_root: Path
    otel_chart_file: Path
    kubeconfig: Path
    controller_pod_name: str
    controller_pod_uid: str
    controller_pod_namespace: str
    llm_base_url: str
    llm_api_key: str
    d0_artifact_root: Path | None
    gateway_config_file: Path = Path("/etc/litellm/config.yaml")
    gateway_snapshot: GatewayConfigSnapshot | None = None
    # Coroot, the backup observation source for agents (coroot_ro). The
    # project id is per cluster; anonymous read only where Coroot has no login.
    coroot_url: str = "http://coroot-coroot.coroot.svc:8080"
    coroot_project_id: str = ""
    coroot_allow_anonymous_read: bool = False

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None):
        values = os.environ if env is None else env
        required = {
            "STAGE2_REPO_ROOT": values.get("STAGE2_REPO_ROOT", "/app"),
            "STAGE2_PRIVATE_ROOT": values.get("STAGE2_PRIVATE_ROOT", "/var/lib/resbench-stage2/private"),
            "STAGE2_ARTIFACT_ROOT": values.get("STAGE2_ARTIFACT_ROOT", "/var/lib/resbench-stage2/artifacts"),
            "STAGE2_RUNTIME_ENV_FILE": values.get("STAGE2_RUNTIME_ENV_FILE", "/etc/resbench-stage2/otel-demo.env"),
            "STAGE2_SOURCE_ROOT": values.get("STAGE2_SOURCE_ROOT", "/opt/resiliencebenchmark/sources"),
            "STAGE2_OTEL_CHART_FILE": values.get(
                "STAGE2_OTEL_CHART_FILE",
                "/opt/resiliencebenchmark/charts/opentelemetry-demo-0.40.5.tgz",
            ),
            "STAGE2_KUBECONFIG": values.get("STAGE2_KUBECONFIG", "/var/lib/resbench-stage2/private/service.kubeconfig"),
            "STAGE2_POD_NAME": values.get("STAGE2_POD_NAME", ""),
            "STAGE2_POD_UID": values.get("STAGE2_POD_UID", ""),
            "STAGE2_POD_NAMESPACE": values.get("STAGE2_POD_NAMESPACE", "resiliencebenchmark-system"),
            "RESBENCH_LLM_BASE_URL": values.get("RESBENCH_LLM_BASE_URL", ""),
            "RESBENCH_LLM_API_KEY": values.get("RESBENCH_LLM_API_KEY", ""),
            "STAGE2_LITELLM_CONFIG_FILE": values.get(
                "STAGE2_LITELLM_CONFIG_FILE", "/etc/litellm/config.yaml"
            ),
        }
        missing = [key for key, value in required.items() if not value]
        if missing:
            raise RuntimeConfigurationError(
                "missing Stage-2 runtime values: " + ", ".join(sorted(missing))
            )
        gateway_config_file = Path(required["STAGE2_LITELLM_CONFIG_FILE"]).resolve()
        try:
            gateway_snapshot = GatewayConfigSnapshot.from_file(
                gateway_config_file,
                required_aliases=STAGE2_SUPPORTED_MODELS,
            )
        except GatewayConfigError as exc:
            raise RuntimeConfigurationError(str(exc)) from exc
        return cls(
            repo_root=Path(required["STAGE2_REPO_ROOT"]).resolve(),
            private_root=Path(required["STAGE2_PRIVATE_ROOT"]).resolve(),
            artifact_root=Path(required["STAGE2_ARTIFACT_ROOT"]).resolve(),
            runtime_env_file=Path(required["STAGE2_RUNTIME_ENV_FILE"]).resolve(),
            source_root=Path(required["STAGE2_SOURCE_ROOT"]).resolve(),
            otel_chart_file=Path(required["STAGE2_OTEL_CHART_FILE"]).resolve(),
            kubeconfig=Path(required["STAGE2_KUBECONFIG"]).resolve(),
            controller_pod_name=required["STAGE2_POD_NAME"],
            controller_pod_uid=required["STAGE2_POD_UID"],
            controller_pod_namespace=required["STAGE2_POD_NAMESPACE"],
            llm_base_url=required["RESBENCH_LLM_BASE_URL"],
            llm_api_key=required["RESBENCH_LLM_API_KEY"],
            d0_artifact_root=(
                Path(values["STAGE2_D0_ARTIFACT_ROOT"]).resolve()
                if values.get("STAGE2_D0_ARTIFACT_ROOT")
                else None
            ),
            gateway_config_file=gateway_config_file,
            gateway_snapshot=gateway_snapshot,
            coroot_url=values.get("RESBENCH_COROOT_URL", "http://coroot-coroot.coroot.svc:8080").rstrip("/"),
            coroot_project_id=values.get("RESBENCH_COROOT_PROJECT_ID", "").strip(),
            coroot_allow_anonymous_read=(
                values.get("RESBENCH_COROOT_ALLOW_ANONYMOUS_READ", "").strip().lower() == "true"
            ),
        )


@dataclass(frozen=True)
class Stage2Components:
    gate: KubernetesEnvironmentGate
    traffic: "KubernetesTrafficEvidence"
    permissions: Stage2PermissionManager
    issuer: ApplicationTrafficCapabilityIssuer
    preparer: KubernetesTrialPreparer
    supervisor: McpSupervisor
    harness_runner: NativeHarnessRunner
    cleanup_backend: "DirectChaosCleanup"
    finalizer: Stage2Finalizer
    resetter: OtelDemoResetter
    disturbance_executor: CompositeDisturbanceExecutor
    token_registry: McpTokenStateRegistry


def build_runtime(
    episode,
    request_model_by_harness: Mapping[Any, str],
    *,
    namespace: str = "otel-demo",
) -> Stage2Components:
    """Build the production Stage-2 runtime from process configuration."""
    config = Stage2RuntimeConfig.from_env()
    for path in (config.private_root, config.artifact_root):
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(path, 0o700)
    write_incluster_kubeconfig(config.kubeconfig)
    return _build_runtime(
        config=config,
        episode=episode,
        request_model_by_harness=request_model_by_harness,
        namespace=namespace,
    )


def _build_runtime(
    *,
    config: Stage2RuntimeConfig,
    episode,
    request_model_by_harness: Mapping[Any, str],
    namespace: str,
) -> Stage2Components:
    model_by_harness = _normalize_model_by_harness(request_model_by_harness)
    gate = KubernetesEnvironmentGate(config.kubeconfig)
    traffic = KubernetesTrafficEvidence(gate, episode)
    private = config.private_root
    identities = prepare_execution_identities(
        config.kubeconfig, private / "kube-identities", control_namespace=config.controller_pod_namespace,
    )
    baseline_dir = private / "chaos-control/baseline"
    ledger_dir = private / "chaos-control/active"
    for path in (baseline_dir, ledger_dir):
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(path, 0o700)
    token_registry = McpTokenStateRegistry(private / "mcp-tokens")
    permissions = Stage2PermissionManager(
        private_root=private / "permissions",
        token_registry=token_registry,
    )
    issuer = ApplicationTrafficCapabilityIssuer(
        ledger_dir=baseline_dir,
        controller_pod_uid=config.controller_pod_uid,
        traffic_evidence=traffic,
    )
    preparer = KubernetesTrialPreparer.from_incluster(issuer)
    controller_token_ref = (
        f"k8s://{config.controller_pod_namespace}/serviceaccount/{CONTROLLER_SERVICE_ACCOUNT}"
    )
    mcp_environment = {
        "RESBENCH_K8S_RO_KUBECONFIG": str(config.kubeconfig),
        "RESBENCH_K8S_RO_NAMESPACE_ALLOWLIST": namespace,
        "RESBENCH_PROMETHEUS_URL": "http://prometheus.observability.svc:9090",
        "RESBENCH_JAEGER_URL": "http://jaeger-query.observability.svc:16686",
        "RESBENCH_LOKI_URL": "http://loki.observability.svc:3100",
        "RESBENCH_TELEMETRY_ALLOWED_NAMESPACES": namespace,
        "RESBENCH_JAEGER_ALLOWED_SERVICES": "frontend,frontend-proxy,checkout,cart,payment,shipping",
        "RESBENCH_COROOT_URL": config.coroot_url,
        "RESBENCH_COROOT_PROJECT_ID": config.coroot_project_id,
        "RESBENCH_COROOT_ALLOWED_NAMESPACE": namespace,
        "RESBENCH_COROOT_ALLOWED_SERVICES": "frontend,frontend-proxy,checkout,cart,payment,shipping",
        "RESBENCH_COROOT_ALLOW_ANONYMOUS_READ": "true" if config.coroot_allow_anonymous_read else "false",
        "RESBENCH_COROOT_TIMEOUT_SECONDS": "10",
        "RESBENCH_TELEMETRY_ALLOW_RAW_QUERIES": "false",
        "RESBENCH_TELEMETRY_DISTURBANCE_DIR": str(private / "telemetry"),
        "RESBENCH_WORKLOAD_STATS_URL": "http://load-generator.otel-demo.svc.cluster.local:8089/stats/requests",
        "RESBENCH_WORKLOAD_STAT_NAME": "/api/cart",
        "RESBENCH_SOURCE_ROOT": str(config.source_root),
        "RESBENCH_SOURCE_ALLOWED_APPLICATIONS": "otel-demo",
        "RESBENCH_CHAOS_EXECUTE_ENABLED": "true",
        "RESBENCH_CHAOS_KUBECONFIG": str(identities.executor_kubeconfig),
        "RESBENCH_CHAOS_CLEANUP_KUBECONFIG": str(identities.finalizer_kubeconfig),
        "RESBENCH_CHAOS_NAMESPACE_ALLOWLIST": namespace,
        "RESBENCH_CHAOS_CONTROLLER_TOKEN_REF": controller_token_ref,
        "RESBENCH_CHAOS_CONTROLLER_POD_UID": config.controller_pod_uid,
        "RESBENCH_CHAOS_CONTROLLER_POD_NAMESPACE": config.controller_pod_namespace,
        "RESBENCH_CHAOS_CONTROLLER_POD_NAME": config.controller_pod_name,
        "RESBENCH_CHAOS_BASELINE_LEDGER_DIR": str(baseline_dir),
        "RESBENCH_CHAOS_LEDGER_DIR": str(ledger_dir),
    }
    supervisor = McpSupervisor(
        private_root=private / "mcp-runtime",
        base_environment=mcp_environment,
    )
    harness_environment = {
        "RESBENCH_LLM_BASE_URL": config.llm_base_url,
        "RESBENCH_LLM_API_KEY": config.llm_api_key,
        "RESBENCH_CHAOS_CONTROLLER_TOKEN_REF": controller_token_ref,
        "RESBENCH_CHAOS_CONTROLLER_POD_UID": config.controller_pod_uid,
        "RESBENCH_CODEX_EVAL_BIN": os.environ.get(
            "RESBENCH_CODEX_EVAL_BIN", ""
        ),
        "STAGE2_BLADEAI_PYTHON": os.environ.get(
            "STAGE2_BLADEAI_PYTHON", "/opt/bladeai-venv/bin/python"
        ),
        "STAGE2_BLADEAI_MODEL": model_by_harness.get(
            HarnessKind.BLADEAI,
            STAGE2_BLADEAI_DEFAULT_MODEL,
        ),
    }
    runtime_client = KubernetesDisturbanceClient.from_kubeconfig(
        config.kubeconfig
    )
    disturbance_executor = CompositeDisturbanceExecutor(
        kubernetes_client=runtime_client,
        mcp_tokens=token_registry,
        target_rebinder=issuer,
        mcp_supervisor=supervisor,
    )
    cleanup_environment = {**mcp_environment, "RESBENCH_CHAOS_KUBECONFIG": str(identities.finalizer_kubeconfig)}
    chaos_service = ChaosControlService(
        RuntimeConfig.from_env(cleanup_environment, server_name="chaos_control")
    )
    chaos_mesh_service = ChaosMeshControlService(
        RuntimeConfig.from_env(cleanup_environment, server_name="chaos_mesh_control")
    )
    cleanup_backend = DirectChaosCleanup(
        chaos_service, chaos_mesh_service, identities.finalizer_kubeconfig
    )
    harness_runner_kwargs: dict[str, Any] = {
        "repo_root": config.repo_root,
        "private_root": private / "harness",
        "artifact_root": config.artifact_root,
        "permissions": permissions,
        "mcp_supervisor": supervisor,
        "base_environment": harness_environment,
        "capability_loss_factory": CapabilityLossRuntimeFactory(
            cleanup_backend=cleanup_backend,
            qualification_path=Path(os.environ.get(
                "STAGE2_SUBSTITUTION_QUALIFICATION_FILE",
                str(private / "capability-loss-qualification.json"),
            )),
            evidence_root=private / "capability-loss",
        ),
        "agent_exec_client": AgentExecClient(
            Path(os.environ.get("RESBENCH_AGENT_EXEC_SOCKET", "/run/resbench/agent-exec.sock")),
            expected_server_uid=0,
        ),
        "agent_work_root": Path(os.environ.get(
            "STAGE2_AGENT_WORK_ROOT", "/var/lib/resbench-stage2/agent-trials",
        )),
        "sandbox_work_root": Path(os.environ.get(
            "STAGE2_SANDBOX_WORK_ROOT", "/var/lib/resbench-stage2/sandbox-trials",
        )),
    }
    if config.gateway_snapshot is not None:
        harness_runner_kwargs["gateway_snapshot"] = config.gateway_snapshot
        harness_runner_kwargs["gateway_audit_dir"] = Path("/var/lib/resbench-stage2/gateway-audit")
    harness_runner = NativeHarnessRunner(**harness_runner_kwargs)
    finalizer = Stage2Finalizer(
        cleanup_backend,
        traffic,
        recovery_timeout_seconds=180,
    )
    resetter = OtelDemoResetter(
        repo_root=config.repo_root,
        kubeconfig=config.kubeconfig,
        runtime_env_file=config.runtime_env_file,
        chart_file=config.otel_chart_file,
        environment_gate=gate,
        traffic_evidence=traffic,
        timeout_seconds=120,
        recovery_timeout_seconds=180,
        verify_only=False,
    )
    return Stage2Components(
        gate=gate,
        traffic=traffic,
        permissions=permissions,
        issuer=issuer,
        preparer=preparer,
        supervisor=supervisor,
        harness_runner=harness_runner,
        cleanup_backend=cleanup_backend,
        finalizer=finalizer,
        resetter=resetter,
        disturbance_executor=disturbance_executor,
        token_registry=token_registry,
    )


def _normalize_model_by_harness(values: Mapping[Any, str]) -> dict[HarnessKind, str]:
    result: dict[HarnessKind, str] = {}
    for key, value in values.items():
        harness = key if isinstance(key, HarnessKind) else HarnessKind(str(key))
        result[harness] = value
    return result


class KubernetesTrafficEvidence:
    def __init__(
        self,
        gate: KubernetesEnvironmentGate,
        episode,
        *,
        stats_url: str = "http://load-generator.otel-demo.svc.cluster.local:8089/stats/requests",
        stats_loader: Callable[[str], Mapping[str, Any]] | None = None,
        stats_resetter: Callable[[str], None] | None = None,
        prometheus_url: str = "http://prometheus.observability.svc:9090",
        prometheus_loader: Callable[..., Mapping[str, Any]] | None = None,
        prometheus_metadata_loader: Callable[..., Mapping[str, Any]] | None = None,
        coroot_prometheus_url: str | None = None,
        coroot_loader: Callable[..., Mapping[str, Any]] | None = None,
    ):
        self.gate = gate
        self.episode = episode
        self.stats_url = stats_url
        self.stats_loader = stats_loader or self._load_stats
        self.stats_resetter = stats_resetter or self._reset_stats
        self.prometheus_url = prometheus_url.rstrip("/")
        self.prometheus_loader = prometheus_loader or self._load_prometheus_range
        self.prometheus_metadata_loader = prometheus_metadata_loader or self._load_prometheus_metadata
        # Coroot's Prometheus is the backup source for Pod resource evidence.
        self.coroot_prometheus_url = (
            coroot_prometheus_url
            or os.environ.get("RESBENCH_COROOT_PROMETHEUS_URL")
            or "http://coroot-prometheus.coroot.svc:9090"
        ).rstrip("/")
        self.coroot_loader = coroot_loader or self._load_coroot_range
        self._baselines: dict[str, dict[str, Any]] = {}
        self._baseline_times: dict[str, float] = {}
        self._samples: list[tuple[float, dict[str, Any]]] = []
        self._sampling_stop = Event()
        self._sampling_thread: Thread | None = None

    def start_sampling(self) -> None:
        if self._sampling_thread is not None:
            return
        def sample():
            while not self._sampling_stop.is_set():
                try:
                    self._samples.append((time.time(), dict(self.current())))
                except Exception:
                    pass  # Missing data remains missing; it never becomes zero-valued evidence.
                self._sampling_stop.wait(2)
        self._sampling_thread = Thread(target=sample, daemon=True)
        self._sampling_thread.start()

    def close(self) -> None:
        self._sampling_stop.set()
        if self._sampling_thread is not None:
            self._sampling_thread.join(timeout=6)

    def current(self) -> Mapping[str, Any]:
        value = dict(self.gate.qualify(self.episode))
        ready = value.get("built_in_load_generator_ready", 0) >= 1
        try:
            locust = self.stats_loader(self.stats_url)
        except Exception as exc:  # noqa: BLE001
            return {
                "application_owned": True,
                "load_generator_ready": ready,
                "traffic_observed": False,
                "business_healthy": False,
                "source": "otel-demo built-in Locust /stats/requests",
                "reason": f"Locust statistics unavailable: {type(exc).__name__}",
            }
        aggregate = next(
            (
                item
                for item in locust.get("stats", [])
                if isinstance(item, Mapping) and item.get("name") == "Aggregated"
            ),
            {},
        )
        requests = int(aggregate.get("num_requests") or 0)
        failures = int(aggregate.get("num_failures") or 0)
        users = int(locust.get("user_count") or 0)
        total_rps = float(aggregate.get("total_rps") or locust.get("total_rps") or 0.0)
        current_rps = float(aggregate.get("current_rps") or 0.0)
        current_fail_per_sec = float(
            aggregate.get("current_fail_per_sec") or 0.0
        )
        p95_ms = float(aggregate.get("response_time_percentile_0.95") or 0.0)
        cart_rows = [
            item
            for item in locust.get("stats", [])
            if isinstance(item, Mapping) and item.get("name") == "/api/cart"
        ]
        cart_requests = sum(int(item.get("num_requests") or 0) for item in cart_rows)
        cart_failures = sum(int(item.get("num_failures") or 0) for item in cart_rows)
        cart_current_rps = sum(float(item.get("current_rps") or 0.0) for item in cart_rows)
        cart_current_fail_per_sec = sum(
            float(item.get("current_fail_per_sec") or 0.0) for item in cart_rows
        )
        cart_response_sum_ms = sum(
            float(item.get("avg_response_time") or 0.0)
            * int(item.get("num_requests") or 0)
            for item in cart_rows
        )
        cart_avg_ms = (
            cart_response_sum_ms / cart_requests if cart_requests else 0.0
        )
        cart_p95_ms = max(
            (
                float(item.get("response_time_percentile_0.95") or 0.0)
                for item in cart_rows
            ),
            default=0.0,
        )
        success_rate = (requests - failures) / requests if requests else 0.0
        cart_success_rate = (
            (cart_requests - cart_failures) / cart_requests
            if cart_requests
            else 0.0
        )
        target_scope = "cart" if cart_rows else "aggregate_fallback"
        target_requests = cart_requests if cart_rows else requests
        target_success_rate = cart_success_rate if cart_rows else success_rate
        target_latency_ms = cart_avg_ms if cart_rows else p95_ms
        target_current_rps = cart_current_rps if cart_rows else current_rps
        target_current_fail = (
            cart_current_fail_per_sec if cart_rows else current_fail_per_sec
        )
        traffic = (
            ready
            and locust.get("state") == "running"
            and users > 0
            and requests > 0
            and total_rps > 0
        )
        business = (
            traffic
            and target_requests > 0
            and target_success_rate >= 0.95
            and target_latency_ms <= 1_000
        )
        return {
            "application_owned": True,
            "load_generator_ready": ready,
            "traffic_observed": traffic,
            "business_healthy": business,
            "source": "otel-demo built-in Locust /stats/requests",
            "state": locust.get("state"),
            "user_count": users,
            "num_requests": requests,
            "num_failures": failures,
            "total_rps": total_rps,
            "current_rps": current_rps,
            "current_fail_per_sec": current_fail_per_sec,
            "success_rate": success_rate,
            "p95_ms": p95_ms,
            "business_scope": target_scope,
            "target_requests": target_requests,
            "target_failures": cart_failures if cart_rows else failures,
            "target_success_rate": target_success_rate,
            "target_latency_ms": target_latency_ms,
            "target_response_sum_ms": (
                cart_response_sum_ms if cart_rows else 0.0
            ),
            "target_p95_ms": cart_p95_ms if cart_rows else p95_ms,
            "target_current_rps": target_current_rps,
            "target_current_fail_per_sec": target_current_fail,
            "cart_requests": cart_requests,
            "cart_failures": cart_failures,
            "cart_response_sum_ms": cart_response_sum_ms,
            "cart_avg_response_ms": cart_avg_ms,
        }

    def record_baseline(
        self, trial_id: str, evidence: Mapping[str, Any]
    ) -> None:
        self._baselines[trial_id] = dict(evidence)
        self._baseline_times[trial_id] = time.time()

    def baseline(self, trial_id: str) -> Mapping[str, Any]:
        return dict(self._baselines.get(trial_id) or {})

    def effect_since(
        self,
        trial_id: str,
        runtime,
        approved_plan: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        window = dict(runtime.main_fault.get("evidence_window") or {})
        start = _window_timestamp(window.get("start"))
        end = _window_timestamp(window.get("end"))
        if start is None or end is None or end <= start:
            return {"verified": False, "evidence_window": window, "reason": "actual fault window is not established"}
        samples = list(self._samples)
        before = [sample for sample in samples if sample[0] <= start]
        during = [sample for sample in samples if start <= sample[0] <= end]
        observability = request_observability(runtime, start, end, self.prometheus_metadata_loader)
        physical = self._physical_fault_effect(trial_id, runtime)
        request_effect = target_request_effect(runtime, observability, start, end,
                                              self._baseline_times.get(trial_id), self.prometheus_loader)
        if not before or not during:
            return {"verified": physical.get("verified") is True or request_effect.get("verified") is True,
                    "physical_effect": physical, "request_effect": request_effect,
                    "observability": observability, "evidence_window": window,
                    "reason": "no counters sampled at the original fault boundaries"}
        baseline_time, baseline = before[-1]
        current_time, current = during[-1]
        request_delta = int(current.get("cart_requests") or 0) - int(
            baseline.get("cart_requests") or 0
        )
        failure_delta = int(current.get("cart_failures") or 0) - int(
            baseline.get("cart_failures") or 0
        )
        response_sum_delta = float(
            current.get("cart_response_sum_ms") or 0.0
        ) - float(baseline.get("cart_response_sum_ms") or 0.0)
        interval_avg_ms = (
            response_sum_delta / request_delta if request_delta > 0 else 0.0
        )
        baseline_avg_ms = float(baseline.get("cart_avg_response_ms") or 0.0)
        latency_delta_ms = interval_avg_ms - baseline_avg_ms
        interval_success_rate = (
            (request_delta - failure_delta) / request_delta
            if request_delta > 0
            else 0.0
        )
        counters_valid = request_delta > 0 and failure_delta >= 0 and response_sum_delta >= 0
        business_evidence = {
            "business_effect_verified": False,
            "observer": "otel-demo Locust counters sampled at original fault boundaries",
            "scope": "cart_service", "sample_start": baseline_time, "sample_end": current_time,
            "counters_valid": counters_valid,
            "cart_request_delta": request_delta,
            "cart_failure_delta": failure_delta,
            "baseline_cart_avg_response_ms": baseline_avg_ms,
            "fault_window_cart_avg_response_ms": interval_avg_ms,
            "latency_delta_ms": latency_delta_ms,
            "fault_window_success_rate": interval_success_rate,
            "service_metrics_available": counters_valid,
        }
        service_condition: dict[str, Any] = {
            "matched": False,
            "reason": "approved effect condition is unavailable",
        }
        condition = (
            approved_plan.get("effect_condition")
            if isinstance(approved_plan, Mapping)
            else None
        )
        if isinstance(condition, Mapping) and str(condition.get("metric") or "") in RESOURCE_METRICS:
            # Resource conditions are judged on the Pod's own samples: the
            # first value in the fault window is the baseline and the peak is
            # the effect (memory samples are bytes, conditions are MiB).
            metric_name = str(condition["metric"])
            scale = 1.0 if metric_name == "target_cpu_cores" else 1.0 / (1024 * 1024)
            if physical.get("baseline_value") is not None and physical.get("peak_value") is not None:
                matched, condition_evidence = evaluate_condition(
                    condition,
                    baseline={metric_name: float(physical["baseline_value"]) * scale},
                    sample={metric_name: float(physical["peak_value"]) * scale},
                )
                service_condition = {
                    **condition_evidence,
                    "matched": matched,
                    "scope": "target_pod",
                    "source": physical.get("source"),
                }
            else:
                service_condition = {
                    "matched": False,
                    "reason": "target Pod resource samples are unavailable",
                    "scope": "target_pod",
                }
        elif isinstance(condition, Mapping):
            try:
                matched, condition_evidence = evaluate_condition(
                    condition,
                    baseline=baseline,
                    sample=current,
                )
                service_condition = {
                    **condition_evidence,
                    "matched": matched,
                    "scope": "cart_service",
                }
            except Exception as exc:  # noqa: BLE001 - invalid evidence stays false.
                service_condition = {
                    "matched": False,
                    "error_type": type(exc).__name__,
                    "scope": "cart_service",
                }
        sustain_seconds = int(
            approved_plan.get("effect_sustain_seconds") or 0
        ) if isinstance(approved_plan, Mapping) else 0
        service_condition["sustain_required_seconds"] = sustain_seconds
        service_condition["sustain_verified"] = sustain_seconds == 0
        service_verified = (
            service_condition.get("matched") is True
            and sustain_seconds == 0
        )
        fault_type = str(runtime.main_fault.get("fault_type") or "")
        verified = (
            physical.get("verified") is True
            if fault_type in {"cpu-load", "memory-stress"}
            else (
                request_effect.get("verified") is True
                or service_verified
            )
        )
        business_evidence["business_effect_verified"] = (
            request_effect.get("verified") is True
            or service_verified
        )
        return {
            "verified": verified,
            "fault_type": fault_type,
            "physical_effect": physical,
            "request_effect": request_effect,
            "service_condition": service_condition,
            "attribution_scope": (
                "target_pod"
                if request_effect.get("verified") is True
                else "cart_service"
                if service_verified
                else None
            ),
            "observability": observability,
            "evidence_window": window,
            **business_evidence,
        }

    def _load_prometheus_metadata(self, path, params):
        url = self.prometheus_url + "/api/v1/" + path + "?" + urllib.parse.urlencode(params, doseq=True)
        with urllib.request.urlopen(url, timeout=5) as response:
            return json.load(response)

    def _physical_fault_effect(self, trial_id: str, runtime) -> dict[str, Any]:
        """Measure a CPU or memory fault on the target Pod itself.

        Tries, in order: Prometheus cAdvisor series selected by Pod labels
        (works whatever the cgroup layout), the cgroup-path selector used
        before, and Coroot's container metrics. The first source with enough
        samples decides; which one it was is recorded with the evidence.
        """

        del trial_id
        fault_type = str(runtime.main_fault.get("fault_type") or "")
        if fault_type not in {"cpu-load", "memory-stress"}:
            return {
                "applicable": False,
                "verified": False,
                "reason": "fault uses business-path effect evidence",
            }
        metric = (
            "container_cpu_usage_seconds_total"
            if fault_type == "cpu-load"
            else "container_memory_working_set_bytes"
        )
        window = runtime.main_fault.get("evidence_window") or {}
        start = _window_timestamp(window.get("start"))
        end = _window_timestamp(window.get("end"))
        if start is None or end is None:
            return {
                "applicable": True,
                "verified": False,
                "metric": metric,
                "reason": "metric baseline timestamp is missing",
            }
        attempts: list[dict[str, Any]] = []
        values: list[float] = []
        source = None
        for name, backend, query in _physical_effect_queries(fault_type, runtime.target):
            loader = self.prometheus_loader if backend == "prometheus" else self.coroot_loader
            try:
                candidate = _prometheus_range_values(
                    loader(query=query, start=start, end=end, step=5)
                )
            except Exception as exc:  # noqa: BLE001 - evidence failure is reported, never inferred.
                attempts.append({"source": name, "error": type(exc).__name__})
                continue
            attempts.append({"source": name, "sample_count": len(candidate)})
            if len(candidate) >= 2:
                values, source = candidate, name
                break
        if len(values) < 2:
            errors = [item["error"] for item in attempts if "error" in item]
            return {
                "applicable": True,
                "verified": False,
                "metric": metric,
                "sample_count": len(values),
                "attempts": attempts,
                "reason": (
                    f"Prometheus evidence unavailable: {errors[0]}"
                    if errors and len(errors) == len(attempts)
                    else "insufficient metric samples"
                ),
            }
        baseline_value = values[0]
        peak_value = max(values)
        if fault_type == "cpu-load":
            requested = float(runtime.main_fault.get("intensity", {}).get("cpu_percent") or 0)
            required_peak = max(baseline_value + 0.15, requested / 100.0 * 0.5)
            unit = "cores"
        else:
            required_peak = baseline_value + max(64 * 1024 * 1024, baseline_value * 0.25)
            unit = "bytes"
        return {
            "applicable": True,
            "verified": peak_value >= required_peak,
            "metric": metric,
            "source": source,
            "attempts": attempts,
            "target_uid": runtime.target.uid,
            "unit": unit,
            "sample_count": len(values),
            "baseline_value": baseline_value,
            "peak_value": peak_value,
            "required_peak": required_peak,
            "query_window_seconds": round(end - start, 3),
        }

    def target_resource_value(self, target: Any, metric: str, *, at: float | None = None) -> float | None:
        """The target Pod's CPU cores or memory MiB at ``at`` (default now), or None.

        Feeds resource effect and recovery conditions. Prometheus is asked
        first and Coroot is the backup; a source with no recent sample is
        skipped.
        """

        end = time.time() if at is None else float(at)
        for backend, query in _resource_queries(metric, target):
            loader = self.prometheus_loader if backend == "prometheus" else self.coroot_loader
            try:
                values = _prometheus_range_values(loader(query=query, start=end - 60, end=end, step=15))
            except Exception:  # noqa: BLE001 - try the backup source.
                continue
            if values:
                return values[-1]
        return None

    def reset_and_wait_healthy(
        self,
        *,
        timeout_seconds: int = 300,
        stability_samples: int = 3,
        baseline: Mapping[str, Any] | None = None,
        recovery_condition: Mapping[str, Any] | None = None,
        target: Any = None,
        resource_baseline: float | None = None,
    ) -> Mapping[str, Any]:
        reset_url = self.stats_url.removesuffix("/stats/requests") + "/stats/reset"
        deadline = time.monotonic() + timeout_seconds
        stable = 0
        last: dict[str, Any] = {}
        started = time.monotonic()
        # Every return carries the samples that led to it, so a wait that ran
        # out of time shows which sample broke the streak.
        trace: list[dict[str, Any]] = []
        # Warm-up only shows the load generator is running and the target is
        # not failing before the counters are reset; the approved sustain
        # applies to the recovery samples below. With seven warm-up and seven
        # recovery samples at 10 s the wait needed about 128 s of a 180 s
        # budget, so one bad warm-up sample made a recovered service look
        # unverified (lxr-93c8deeea4ef4932).
        warmup_samples = min(stability_samples, RECOVERY_WARMUP_SAMPLES)
        while stable < warmup_samples:
            try:
                last = dict(self.current())
            except Exception as exc:  # noqa: BLE001
                last = {
                    "business_healthy": False,
                    "stage": "warmup",
                    "error_type": type(exc).__name__,
                }
            warm = _warmup_sample_ok(last)
            trace.append(_trace_entry(started, "warmup", warm, last))
            stable = stable + 1 if warm else 0
            if stable >= warmup_samples:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return _with_trace(last, trace)
            time.sleep(min(10.0, remaining))
        reset_error: dict[str, Any] = {}
        reset_count = 0
        while True:
            try:
                self.stats_resetter(reset_url)
                reset_count += 1
                break
            except Exception as exc:  # noqa: BLE001
                reset_error = {
                    "business_healthy": False,
                    "stage": "stats_reset",
                    "error_type": type(exc).__name__,
                }
                trace.append(_trace_entry(started, "stats_reset", False, reset_error))
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return _with_trace(reset_error, trace)
            time.sleep(min(5.0, remaining))
        last = {}
        recovery_stable = 0
        # A resource recovery condition reads the target Pod's own CPU or
        # memory and compares it with the value before the fault.
        resource_metric = (
            str(recovery_condition.get("metric") or "")
            if isinstance(recovery_condition, Mapping)
            and str(recovery_condition.get("metric") or "") in RESOURCE_METRICS
            else ""
        )
        condition_baseline = (
            {**baseline, resource_metric: resource_baseline}
            if resource_metric and isinstance(baseline, Mapping)
            else baseline
        )
        counter_anchor = {
            "target_requests": 0,
            "target_failures": 0,
            "target_response_sum_ms": 0.0,
        }
        while True:
            try:
                last = dict(self.current())
            except Exception as exc:  # noqa: BLE001
                last = {"business_healthy": False, "error_type": type(exc).__name__}
            if resource_metric and target is not None:
                last[resource_metric] = self.target_resource_value(target, resource_metric)
            recovery_ok = last.get("business_healthy") is True
            condition_evidence: dict[str, Any] | None = None
            if isinstance(baseline, Mapping) and isinstance(
                recovery_condition, Mapping
            ):
                recovery_ok, condition_evidence = evaluate_condition(
                    recovery_condition,
                    baseline=condition_baseline,
                    sample=last,
                    counter_anchor=counter_anchor,
                )
            recovery_stable = recovery_stable + 1 if recovery_ok else 0
            trace.append(_trace_entry(started, "recovery", recovery_ok, last, condition_evidence))
            last["recovery_condition_evidence"] = condition_evidence
            last["stability_samples_observed"] = recovery_stable
            last["stability_samples_required"] = stability_samples
            if recovery_condition is not None:
                last["business_healthy"] = False
            if recovery_stable >= stability_samples:
                last["stats_reset_count"] = reset_count
                last["business_healthy"] = True
                return _with_trace(last, trace)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                last["stats_reset_count"] = reset_count
                return _with_trace(last, trace)
            time.sleep(min(10.0, remaining))

    def wait_until_healthy(
        self,
        *,
        timeout_seconds: int = 180,
        stability_samples: int = 7,
    ) -> Mapping[str, Any]:
        """Wait for a stable business window without resetting workload counters."""

        deadline = time.monotonic() + timeout_seconds
        stable = 0
        attempts = 0
        last: dict[str, Any] = {}
        while True:
            attempts += 1
            try:
                last = dict(self.current())
            except Exception as exc:  # noqa: BLE001 - keep the state unknown.
                last = {
                    "business_healthy": False,
                    "sample_status": "unavailable",
                    "error_type": type(exc).__name__,
                }
            healthy = last.get("business_healthy") is True
            if healthy:
                stable += 1
            else:
                stable = 0
            last["sample_status"] = (
                "healthy"
                if healthy
                else "unhealthy"
            )
            last["verification_attempts"] = attempts
            last["stability_samples_observed"] = stable
            last["stability_samples_required"] = stability_samples
            if stable >= stability_samples:
                return last
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return last
            time.sleep(min(10.0, remaining))

    @staticmethod
    def _load_stats(url: str) -> Mapping[str, Any]:
        request = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310
            payload = json.load(response)
        if not isinstance(payload, Mapping):
            raise RuntimeConfigurationError("Locust statistics response is not an object")
        return payload

    def _load_prometheus_range(
        self, *, query: str, start: float, end: float, step: int
    ) -> Mapping[str, Any]:
        params = urllib.parse.urlencode(
            {"query": query, "start": start, "end": end, "step": step}
        )
        request = urllib.request.Request(
            f"{self.prometheus_url}/api/v1/query_range?{params}",
            headers={"Accept": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
            payload = json.load(response)
        if not isinstance(payload, Mapping) or payload.get("status") != "success":
            raise RuntimeConfigurationError("Prometheus range response is invalid")
        return payload

    def _load_coroot_range(
        self, *, query: str, start: float, end: float, step: int
    ) -> Mapping[str, Any]:
        """Range query against Coroot's Prometheus, the backup evidence source."""

        params = urllib.parse.urlencode(
            {"query": query, "start": start, "end": end, "step": step}
        )
        request = urllib.request.Request(
            f"{self.coroot_prometheus_url}/api/v1/query_range?{params}",
            headers={"Accept": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
            payload = json.load(response)
        if not isinstance(payload, Mapping) or payload.get("status") != "success":
            raise RuntimeConfigurationError("Coroot Prometheus range response is invalid")
        return payload

    @staticmethod
    def _reset_stats(url: str) -> None:
        request = urllib.request.Request(url, headers={"Accept": "text/html"})
        with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310
            if not 200 <= int(response.status) < 300:
                raise RuntimeConfigurationError("Locust statistics reset was rejected")


def _target_identity(target: Any) -> tuple[str, str]:
    """(namespace, Pod name) of a runtime target object or a plan target mapping."""

    if isinstance(target, Mapping):
        return str(target.get("namespace") or ""), str(target.get("name") or "")
    return str(getattr(target, "namespace", "") or ""), str(getattr(target, "name", "") or "")


def _physical_effect_queries(fault_type: str, target: Any) -> list[tuple[str, str, str]]:
    """Candidate (source, backend, PromQL) range queries for a CPU or memory fault."""

    namespace, pod = _target_identity(target)
    uid = target.get("uid") if isinstance(target, Mapping) else getattr(target, "uid", "")
    cgroup = f".*pod{str(uid or '').replace('-', '_')}.*scope"
    container = f"/k8s/{namespace}/{pod}/.*"
    if fault_type == "cpu-load":
        return [
            ("prometheus_pod_labels", "prometheus",
             f'sum(rate(container_cpu_usage_seconds_total{{namespace="{namespace}",pod="{pod}",container=""}}[2m]))'),
            ("prometheus_cgroup_path", "prometheus",
             f'sum(rate(container_cpu_usage_seconds_total{{id=~"{cgroup}",cpu="total"}}[2m]))'),
            ("coroot", "coroot",
             f'sum(rate(container_resources_cpu_usage_seconds_total{{container_id=~"{container}"}}[2m]))'),
        ]
    return [
        ("prometheus_pod_labels", "prometheus",
         f'sum(container_memory_working_set_bytes{{namespace="{namespace}",pod="{pod}",container=""}})'),
        ("prometheus_cgroup_path", "prometheus", f'sum(container_memory_working_set_bytes{{id=~"{cgroup}"}})'),
        ("coroot", "coroot", f'sum(container_resources_memory_rss_bytes{{container_id=~"{container}"}})'),
    ]


# The recovery wait's warm-up needs this many healthy load-generator samples
# before the counters are reset; the evidence keeps this many sample entries.
RECOVERY_WARMUP_SAMPLES = 3
RECOVERY_TRACE_LIMIT = 40


def _warmup_sample_ok(sample: Mapping[str, Any]) -> bool:
    """Whether a load-generator snapshot is healthy enough to reset counters.

    Traffic must be flowing and the target must not be failing. A sparse
    target (cart gets about 0.2 requests/s here) can read zero current
    requests for a moment; the whole generator's current rate then shows the
    traffic is still flowing, and any current target failure still blocks.
    """
    if sample.get("load_generator_ready") is not True:
        return False
    target_rps = float(sample.get("target_current_rps") or 0.0)
    target_fail = float(sample.get("target_current_fail_per_sec") or 0.0)
    if target_rps > 0:
        return target_fail / target_rps <= 0.05
    return float(sample.get("current_rps") or 0.0) > 0 and target_fail == 0


def _trace_entry(
    started: float,
    phase: str,
    ok: bool,
    sample: Mapping[str, Any],
    condition: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """One compact recovery-wait sample for the Trial evidence."""
    entry: dict[str, Any] = {
        "t": round(time.monotonic() - started, 1),
        "phase": phase,
        "ok": bool(ok),
        "target_current_rps": sample.get("target_current_rps"),
        "target_current_fail_per_sec": sample.get("target_current_fail_per_sec"),
    }
    if sample.get("error_type"):
        entry["error_type"] = sample.get("error_type")
    if condition:
        entry["observed_value"] = condition.get("observed_value")
        entry["metric_available"] = condition.get("metric_available")
    return entry


def _with_trace(result: dict[str, Any], trace: list[dict[str, Any]]) -> dict[str, Any]:
    """Attach the samples that led to this result (the last ones only)."""
    result["sample_trace"] = trace[-RECOVERY_TRACE_LIMIT:]
    return result


def _resource_queries(metric: str, target: Any) -> list[tuple[str, str]]:
    """(backend, PromQL) queries for a resource condition metric, Prometheus first."""

    namespace, pod = _target_identity(target)
    if not namespace or not pod:
        return []
    container = f"/k8s/{namespace}/{pod}/.*"
    if metric == "target_cpu_cores":
        return [
            ("prometheus", f'sum(rate(container_cpu_usage_seconds_total{{namespace="{namespace}",pod="{pod}",container=""}}[1m]))'),
            ("coroot", f'sum(rate(container_resources_cpu_usage_seconds_total{{container_id=~"{container}"}}[1m]))'),
        ]
    if metric == "target_memory_mib":
        return [
            ("prometheus", f'sum(container_memory_working_set_bytes{{namespace="{namespace}",pod="{pod}",container=""}}) / 1048576'),
            ("coroot", f'sum(container_resources_memory_rss_bytes{{container_id=~"{container}"}}) / 1048576'),
        ]
    return []


def _prometheus_range_values(response: Mapping[str, Any]) -> list[float]:
    data = response.get("data")
    result = data.get("result") if isinstance(data, Mapping) else None
    if not isinstance(result, list):
        return []
    values: list[float] = []
    for series in result:
        samples = series.get("values") if isinstance(series, Mapping) else None
        if not isinstance(samples, list):
            continue
        for sample in samples:
            if not isinstance(sample, list) or len(sample) != 2:
                continue
            try:
                values.append(float(sample[1]))
            except (TypeError, ValueError):
                continue
    return values


class DirectChaosCleanup:
    """Controller-only exact cleanup across ChaosBlade and Chaos Mesh.

    It never deletes by target/name discovery alone.  A resource is deletable
    only through its matching private Trial ledger and executor-specific
    destroy method; foreign resources remain evidence for the reset gate.
    """

    def __init__(
        self,
        chaosblade: ChaosControlService,
        chaos_mesh: ChaosMeshControlService,
        kubeconfig: Path,
    ):
        self.services = {
            "chaosblade": chaosblade,
            "chaos_mesh": chaos_mesh,
        }
        self.kubeconfig = str(kubeconfig)

    def inventory_trial(self, runtime):
        return asyncio.run(self._inventory_trial(runtime))

    def cleanup_owned(self, runtime):
        return asyncio.run(self._cleanup_owned(runtime))

    def status(self, cleanup_handle: str):
        """Condition-monitor status through the one exact ledger owner."""
        return asyncio.run(self._status(cleanup_handle))

    def destroy(self, cleanup_handle: str):
        """Controller fallback cleanup; never attributed to the Agent MCP."""
        return asyncio.run(self._destroy(cleanup_handle))

    async def _inventory_trial(self, runtime) -> dict[str, Any]:
        ledgers: dict[str, list[dict[str, Any]]] = {}
        unavailable: list[str] = []
        for executor_id, service in self.services.items():
            try:
                ledgers[executor_id] = self._read_ledgers(service)
            except Exception:
                unavailable.append(executor_id)
        resources = []
        qualified = []
        for executor_id, service in self.services.items():
            if executor_id in unavailable:
                continue
            try:
                records = await service.backend.list_experiments(
                    self.kubeconfig, runtime.target.namespace
                )
            except Exception:
                unavailable.append(executor_id)
                continue
            qualified.append(executor_id)
            for record in records:
                resources.append(
                    resource_from_experiment(
                        executor_id,
                        record,
                        ledger_matched=self._matches_any_ledger(
                            record, ledgers[executor_id], executor_id
                        ),
                    )
                )
        snapshot = snapshot_for_trial(
            trial_id=runtime.trial_id,
            resources=resources,
            qualified_executors=qualified,
            unavailable_executors=unavailable,
        )
        trial_ledgers = [
            (executor_id, ledger)
            for executor_id, values in ledgers.items()
            for ledger in values
            if ledger.get("run_id") == runtime.trial_id
            and ledger.get("cleanup_handle") == runtime.cleanup_handle
            and ledger.get("executor_id") == executor_id
        ]
        ledger_executor = trial_ledgers[0][0] if len(trial_ledgers) == 1 else None
        ledger = trial_ledgers[0][1] if len(trial_ledgers) == 1 else {}
        matching_resources = [
            resource for resource in resources if resource.owned_by_trial(runtime.trial_id)
        ]
        snapshot["trial"] = {
            "resource_absent": snapshot["owned_resources_absent"],
            "ever_active": bool(ledger.get("ever_active")),
            "namespace": str(ledger.get("namespace") or runtime.target.namespace),
            "target_name": str(ledger.get("target_name") or runtime.target.name),
            "target_uid": str(ledger.get("target_uid") or runtime.target.uid),
            "fault_type": str(ledger.get("fault_type") or runtime.main_fault.get("fault_type") or ""),
            "duration_seconds": ledger.get("duration_seconds"),
            "intensity": dict(ledger.get("intensity") or {}),
            "experiment_name": ledger.get("experiment_name"),
            "ledger_state": ledger.get("state"),
            "cleanup_principal": ledger.get("cleanup_principal"),
            "started_at": ledger.get("started_at"),
            "ended_at": ledger.get("ended_at"),
            "deadline_at": ledger.get("deadline_at"),
            "matching_resource_count": len(matching_resources),
            "ledger_match_count": len(trial_ledgers),
            "executor_id": ledger_executor,
        }
        return snapshot

    async def _cleanup_owned(self, runtime) -> dict[str, Any]:
        inventory = await self._inventory_trial(runtime)
        if inventory.get("qualified") is not True:
            return {"verified_absent": False, "principal": "CONTROLLER_FALLBACK", "reason": "fault_inventory_incomplete"}
        trial = dict(inventory.get("trial") or {})
        if int(trial.get("ledger_match_count") or 0) == 0:
            return {"verified_absent": inventory.get("owned_resources_absent") is True, "principal": "CONTROLLER_FALLBACK", "idempotent": True}
        if int(trial.get("ledger_match_count") or 0) != 1:
            return {"verified_absent": False, "principal": "CONTROLLER_FALLBACK", "reason": "ambiguous_trial_ledger"}
        executor_id = str(trial.get("executor_id") or "")
        if executor_id not in self.services:
            return {"verified_absent": False, "principal": "CONTROLLER_FALLBACK", "reason": "unknown_trial_executor"}
        result = await self.services[executor_id].destroy_experiment(
            cleanup_handle=runtime.cleanup_handle, kubeconfig=self.kubeconfig,
            principal="CONTROLLER_FALLBACK",
        )
        return {**dict(result), "principal": "CONTROLLER_FALLBACK", "executor_id": executor_id}

    @staticmethod
    def _read_ledgers(service) -> list[dict[str, Any]]:
        rows = []
        for path in service._iter_cleanup_ledger_paths():
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise ValueError("invalid cleanup ledger record")
            rows.append(value)
        return rows

    @staticmethod
    def _matches_any_ledger(record, ledgers: list[dict[str, Any]], executor_id: str) -> bool:
        return any(
            ledger.get("executor_id") == executor_id
            and ledger.get("experiment_name") == record.name
            and ledger.get("namespace") == record.namespace
            and ledger.get("run_id") == record.run_id
            and ledger.get("target_uid") == record.target_uid
            and ledger.get("fault_type") == record.fault_type
            for ledger in ledgers
        )

    async def _status(self, cleanup_handle: str) -> dict[str, Any]:
        executor_id, service = self._service_for_handle(cleanup_handle)
        result = await service.recovery_status(
            cleanup_handle=cleanup_handle, kubeconfig=self.kubeconfig
        )
        return {**dict(result), "executor_id": executor_id}

    async def _destroy(self, cleanup_handle: str) -> dict[str, Any]:
        executor_id, service = self._service_for_handle(cleanup_handle)
        result = await service.destroy_experiment(
            cleanup_handle=cleanup_handle,
            kubeconfig=self.kubeconfig,
            principal="CONTROLLER_FALLBACK",
        )
        return {**dict(result), "executor_id": executor_id, "principal": "CONTROLLER_FALLBACK"}

    def _service_for_handle(self, cleanup_handle: str):
        matches = []
        for executor_id, service in self.services.items():
            for ledger in self._read_ledgers(service):
                if ledger.get("cleanup_handle") != cleanup_handle:
                    continue
                if ledger.get("executor_id") != executor_id:
                    continue
                matches.append((executor_id, service))
        if len(matches) != 1:
            raise RuntimeError("cleanup handle has no unique executor-owned ledger")
        return matches[0]


class Stage2System:
    def __init__(
        self,
        config: Stage2RuntimeConfig,
        *,
        model_probe_runner: GatewayProbeRunner | None = None,
        probe_cache_ttl_seconds: float = 300.0,
    ):
        self.config = config
        for path in (config.private_root, config.artifact_root):
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(path, 0o700)
        # Projected ServiceAccount tokens are Pod-bound and rotate. A kubeconfig
        # persisted on the evidence PVC must therefore be replaced at every
        # service start rather than reused across Deployment revisions.
        write_incluster_kubeconfig(config.kubeconfig)
        self.d0_gate = D0QualificationGate(config.d0_artifact_root)
        self._active_lock = Lock()
        self._active_controls: dict[str, dict[str, Any]] = {}
        self._model_probe_runner = model_probe_runner or self._default_model_probe_runner
        self._probe_cache_ttl_seconds = probe_cache_ttl_seconds
        self._probe_lock = Lock()
        self._gateway_readiness: dict[tuple[str, str, tuple[str, ...]], GatewayReadinessEntry] = {}

    def preflight(self) -> dict[str, Any]:
        snapshot, snapshot_error = self._gateway_snapshot()
        readiness = self._gateway_readiness_snapshot(snapshot, wait=False) if snapshot is not None else {
            "status": "failed",
            "started_at": None,
            "completed_at": None,
            "age_seconds": None,
            "config_sha256": None,
            "llm_base_url": self.config.llm_base_url,
            "aliases": list(STAGE2_SUPPORTED_MODELS),
            "available_models": [],
            "model_catalog_error": snapshot_error or "gateway config snapshot unavailable",
            "error": snapshot_error or "gateway config snapshot unavailable",
        }
        available_models = set(readiness.get("available_models") or [])
        model_error = readiness.get("model_catalog_error")
        probe_report = readiness.get("probe_report") if isinstance(readiness.get("probe_report"), Mapping) else {
            "issues": (
                [] if readiness["status"] == "running" else
                [{"severity": "ERROR", "message": snapshot_error or "gateway config snapshot unavailable"}]
            ),
            "models": [],
        }
        model_probes = self._model_probe_statuses(
            snapshot=snapshot,
            available_models=available_models,
            probe_report=probe_report,
        )
        if readiness["status"] == "running":
            for model_probe in model_probes.values():
                model_probe["probe_status"] = "running"
        qualification_path = os.environ.get("STAGE2_HARNESS_CAPABILITIES_FILE")
        harness_capabilities, capability_qualification = (
            harness_capabilities_from_qualification(
                Path(qualification_path).resolve() if qualification_path else None
            )
        )
        # Harness CLIs live in the isolated Agent runtime, not the Controller
        # container.  Only a fresh, evidence-backed descriptor establishes
        # eligibility here; actual sidecar connection remains fail-closed when
        # a Trial starts.
        runtimes = {
            name: bool(harness_capabilities.get(name, {}).get("qualification_passed"))
            for name in ("codex", "claude-code", "deepseek-harness", "bladeai")
        }
        model_matrix = {
            name: {
                model: ready and bool(model_probes.get(model, {}).get("runnable"))
                for model in STAGE2_SUPPORTED_MODELS
            }
            for name, ready in runtimes.items()
        }
        harnesses = {
            name: any(model_matrix[name].values()) for name in runtimes
        }
        d0_inventory = self.d0_gate.inventory()
        d0_selection = self._d0_selection_by_harness_model(
            snapshot=snapshot,
            model_matrix=model_matrix,
        )
        return {
            "schema_version": "stage2-preflight.v3",
            "status": "READY" if any(harnesses.values()) else "ERROR",
            "harnesses": harnesses,
            "models": list(STAGE2_SUPPORTED_MODELS),
            "model_matrix": model_matrix,
            "available_models": sorted(available_models),
            "model_catalog_error": model_error,
            "gateway_probe": {
                key: value
                for key, value in readiness.items()
                if key != "probe_report"
            },
            "gateway_config": {
                "config_sha256": snapshot.config_sha256 if snapshot else None,
                "config_path": snapshot.config_path.as_posix() if snapshot else None,
                "routes": snapshot.required_routes() if snapshot else {},
                "error": snapshot_error,
            },
            "model_probes": model_probes,
            "cases": [item.model_dump(mode="json") for item in default_case_specs()],
            "mcp_servers": ["k8s_ro", "telemetry_ro", "source_ro", "chaos_control", "harness_channel"],
            "disturbance_mcp_servers": {
                "D7": ["coroot_ro", "chaos_mesh_control", "code_sandbox"],
                "D8": ["coroot_ro", "chaos_mesh_control", "code_sandbox"],
            },
            "executors": {
                "chaosblade": {"server": "chaos_control", "execute_enabled_required": True},
                "chaos_mesh": {"server": "chaos_mesh_control", "execute_enabled_required": True},
            },
            "harness_capabilities": harness_capabilities,
            "harness_capability_qualification": capability_qualification,
            "d0": {
                **dict(d0_inventory),
                "selection_by_harness_model": d0_selection,
            },
            "reset_mode": "mutation_evidence_tiered",
        }

    def refresh_gateway_readiness(self) -> dict[str, Any]:
        snapshot, snapshot_error = self._gateway_snapshot()
        if snapshot is None:
            return {
                "status": "failed",
                "started_at": None,
                "completed_at": None,
                "age_seconds": None,
                "config_sha256": None,
                "llm_base_url": self.config.llm_base_url,
                "aliases": list(STAGE2_SUPPORTED_MODELS),
                "available_models": [],
                "model_catalog_error": snapshot_error or "gateway config snapshot unavailable",
                "error": snapshot_error or "gateway config snapshot unavailable",
            }
        return self._gateway_readiness_snapshot(snapshot, wait=True)

    def _d0_selection_by_harness_model(
        self,
        *,
        snapshot: GatewayConfigSnapshot | None,
        model_matrix: Mapping[str, Mapping[str, bool]],
    ) -> dict[str, dict[str, dict[str, Any]]]:
        result: dict[str, dict[str, dict[str, Any]]] = {}
        for harness in HarnessKind:
            harness_rows: dict[str, dict[str, Any]] = {}
            for model in STAGE2_SUPPORTED_MODELS:
                if model_matrix.get(harness.value, {}).get(model) is not True:
                    continue
                if snapshot is None:
                    harness_rows[model] = {
                        "verified": False,
                        "reason": "current gateway config snapshot is unavailable",
                    }
                    continue
                try:
                    ref, reason = self.d0_gate.select_verified_ref(
                        harness=harness,
                        model_alias=model,
                        gateway=snapshot,
                    )
                except Exception as exc:  # noqa: BLE001 - preflight reports selector failures.
                    harness_rows[model] = {
                        "verified": False,
                        "reason": f"D0 selector failed: {type(exc).__name__}",
                    }
                    continue
                row: dict[str, Any] = {
                    "verified": ref is not None,
                    "reason": reason,
                }
                if ref is not None:
                    row["qualification_ref"] = ref.model_dump(mode="json")
                harness_rows[model] = row
            if harness_rows:
                result[harness.value] = harness_rows
        return result

    def _gateway_snapshot(self) -> tuple[GatewayConfigSnapshot | None, str | None]:
        path = getattr(self.config, "gateway_config_file", None)
        if path is None:
            return None, "gateway config snapshot unavailable"
        try:
            return GatewayConfigSnapshot.from_file(
                Path(path),
                required_aliases=STAGE2_SUPPORTED_MODELS,
            ), None
        except GatewayConfigError as exc:
            return None, str(exc)

    def _gateway_readiness_snapshot(
        self, snapshot: GatewayConfigSnapshot, *, wait: bool
    ) -> dict[str, Any]:
        aliases = tuple(STAGE2_SUPPORTED_MODELS)
        cache_key = (snapshot.config_sha256, self.config.llm_base_url, aliases)
        now = time.monotonic()
        with self._probe_lock:
            cache = self._gateway_readiness
            entry = cache.get(cache_key)
            if entry is not None:
                if entry.status == "running":
                    target = entry
                elif (
                    entry.completed_monotonic is not None
                    and now - entry.completed_monotonic <= self._probe_cache_ttl_seconds
                ):
                    return self._gateway_readiness_public(entry, now=now)
                else:
                    target = self._start_gateway_readiness_refresh_locked(
                        cache_key, snapshot, aliases
                    )
            else:
                target = self._start_gateway_readiness_refresh_locked(
                    cache_key, snapshot, aliases
                )
            if not wait:
                return self._gateway_readiness_public(target, now=now)
        target.event.wait()
        with self._probe_lock:
            return self._gateway_readiness_public(target, now=time.monotonic())

    def _start_gateway_readiness_refresh_locked(
        self,
        cache_key: tuple[str, str, tuple[str, ...]],
        snapshot: GatewayConfigSnapshot,
        aliases: tuple[str, ...],
    ) -> GatewayReadinessEntry:
        entry = GatewayReadinessEntry(
            key=cache_key,
            snapshot=snapshot,
            status="running",
            started_monotonic=time.monotonic(),
            started_at=_utc_now_text(),
        )
        self._gateway_readiness[cache_key] = entry
        thread = Thread(
            target=self._run_gateway_readiness_refresh,
            args=(entry, aliases),
            name="stage2-gateway-readiness",
            daemon=True,
        )
        try:
            thread.start()
        except Exception as exc:  # noqa: BLE001 - start failure must not leave a running entry.
            entry.status = "failed"
            entry.completed_monotonic = time.monotonic()
            entry.completed_at = _utc_now_text()
            entry.probe_report = _gateway_probe_failure_report(type(exc).__name__)
            entry.error_type = type(exc).__name__
            entry.error = "gateway readiness refresh could not be started"
            entry.event.set()
        return entry

    def _run_gateway_readiness_refresh(
        self,
        entry: GatewayReadinessEntry,
        aliases: tuple[str, ...],
    ) -> None:
        available_models: set[str] = set()
        model_error: str | None = None
        try:
            available_models, model_error = self._gateway_models()
            probe_report = dict(self._model_probe_runner(entry.snapshot, aliases))
        except Exception as exc:  # noqa: BLE001 - preflight reports bounded setup failures.
            completed = time.monotonic()
            with self._probe_lock:
                entry.status = "failed"
                entry.completed_monotonic = completed
                entry.completed_at = _utc_now_text()
                entry.available_models = set(available_models)
                entry.model_error = model_error
                entry.probe_report = _gateway_probe_failure_report(type(exc).__name__)
                entry.error_type = type(exc).__name__
                entry.error = "gateway model probe failed"
                entry.event.set()
            return
        completed = time.monotonic()
        failed = model_error is not None or _probe_report_has_error(probe_report)
        with self._probe_lock:
            entry.status = "failed" if failed else "complete"
            entry.completed_monotonic = completed
            entry.completed_at = _utc_now_text()
            entry.available_models = set(available_models)
            entry.model_error = model_error
            entry.probe_report = probe_report
            if failed:
                entry.error_type = "GatewayProbeIssue"
                entry.error = "gateway model probe failed"
            entry.event.set()

    def _gateway_readiness_public(
        self,
        entry: GatewayReadinessEntry,
        *,
        now: float,
    ) -> dict[str, Any]:
        age_seconds = (
            max(0.0, now - entry.completed_monotonic)
            if entry.completed_monotonic is not None
            else None
        )
        payload: dict[str, Any] = {
            "status": entry.status,
            "started_at": entry.started_at,
            "completed_at": entry.completed_at,
            "age_seconds": age_seconds,
            "config_sha256": entry.snapshot.config_sha256,
            "llm_base_url": entry.key[1],
            "aliases": list(entry.key[2]),
            "available_models": sorted(entry.available_models),
            "model_catalog_error": entry.model_error,
        }
        if entry.status in {"complete", "failed"}:
            payload["probe_report"] = dict(entry.probe_report or {})
        if entry.error_type is not None:
            payload["error_type"] = entry.error_type
        if entry.error is not None:
            payload["error"] = entry.error
        return payload

    def _default_model_probe_runner(
        self,
        snapshot: GatewayConfigSnapshot,
        aliases: Sequence[str],
    ) -> Mapping[str, Any]:
        del snapshot
        from scripts import probe_models

        return probe_models.run_probe(
            self.config.repo_root / "harness/models.yaml",
            {
                probe_models.BASE_URL_ENV: self.config.llm_base_url,
                probe_models.API_KEY_ENV: self.config.llm_api_key,
            },
            aliases=list(aliases),
            dry_run=False,
        )

    def _model_probe_statuses(
        self,
        *,
        snapshot: GatewayConfigSnapshot | None,
        available_models: set[str],
        probe_report: Mapping[str, Any],
    ) -> dict[str, dict[str, Any]]:
        models = probe_report.get("models") if isinstance(probe_report, Mapping) else []
        by_alias = {
            str(item.get("alias")): item
            for item in models
            if isinstance(item, Mapping) and item.get("alias")
        } if isinstance(models, list) else {}
        issues = probe_report.get("issues") if isinstance(probe_report, Mapping) else []
        has_error_issue = any(
            isinstance(issue, Mapping) and issue.get("severity") == "ERROR"
            for issue in issues if isinstance(issues, list)
        )
        result: dict[str, dict[str, Any]] = {}
        for alias in STAGE2_SUPPORTED_MODELS:
            model_probe = by_alias.get(alias)
            probe_status = (
                str(model_probe.get("overallStatus"))
                if isinstance(model_probe, Mapping) and model_probe.get("overallStatus")
                else "missing"
            )
            failure_classes = tuple(
                str(value)
                for value in (
                    model_probe.get("failureClasses", ())
                    if isinstance(model_probe, Mapping)
                    else ()
                )
                if value
            )
            visible = alias in available_models
            runnable = (
                visible
                and snapshot is not None
                and not has_error_issue
                and probe_status == "supported"
            )
            row: dict[str, Any] = {
                "runnable": runnable,
                "visible_in_gateway_models": visible,
                "probe_status": probe_status,
                "route": snapshot.route(alias) if snapshot else None,
                "probe": dict(model_probe) if isinstance(model_probe, Mapping) else None,
            }
            if failure_classes:
                row["failure_classes"] = list(failure_classes)
                row["reason"] = _model_probe_failure_reason(failure_classes)
            result[alias] = row
        if has_error_issue:
            for alias in result:
                result[alias]["probe_error"] = True
        return result
    def _gateway_models(self) -> tuple[set[str], str | None]:
        endpoint = self.config.llm_base_url.rstrip("/") + "/models"
        request = urllib.request.Request(
            endpoint,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self.config.llm_api_key}",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
                payload = json.load(response)
        except Exception as exc:  # noqa: BLE001 - preflight reports bounded error.
            return set(), type(exc).__name__
        rows = payload.get("data") or payload.get("models") or []
        return {
            str(item.get("id"))
            for item in rows
            if isinstance(item, Mapping) and item.get("id")
        }, None

    def build_runtime(self, episode, request_model_by_harness, *, namespace="otel-demo") -> Stage2Components:
        """Compose one attempt with this system's pinned configuration."""
        return _build_runtime(config=self.config, episode=episode,
                              request_model_by_harness=request_model_by_harness, namespace=namespace)

    def run(
        self,
        request: CampaignRequest,
        event_observer=None,
        stop_requested=None,
    ) -> CampaignResult:
        episode = load_fixed_episode(request.episode, root=self.config.repo_root)
        components = self.build_runtime(
            episode, request.model_by_harness,
            namespace=request.application_namespace,
        )
        engine = CampaignEngine(
            episode=episode,
            environment_gate=components.gate,
            preparer=components.preparer,
            permissions=components.permissions,
            harness_runner=components.harness_runner,
            disturbance_planner=RuntimeDisturbancePlanner(),
            disturbance_executor=components.disturbance_executor,
            finalizer=components.finalizer,
            evaluator=Stage2Evaluator(),
            resetter=components.resetter,
            condition_monitor_factory=lambda: ConditionRecoveryMonitor(
                components.traffic, components.cleanup_backend
            ),
            artifacts=ArtifactStore(self.config.artifact_root),
            platform_ledger=components.token_registry.platform_ledger,
            qualification_gate=self.d0_gate,
        )
        with self._active_lock:
            self._active_controls[request.request_id] = {
                "permissions": components.permissions,
                "resetter": components.resetter,
                "episode": episode,
            }
        try:
            components.traffic.start_sampling()
            return engine.run(
                request,
                event_observer=event_observer,
                stop_requested=stop_requested,
            )
        finally:
            components.traffic.close()
            with self._active_lock:
                self._active_controls.pop(request.request_id, None)
            components.supervisor.stop()

    def restore_permissions(
        self, task_id: str, trial_id: str | None, target_state: str
    ) -> Mapping[str, Any]:
        if not trial_id:
            return {
                "verified": True,
                "target_state": target_state,
                "not_provisioned": True,
            }
        with self._active_lock:
            active = self._active_controls.get(task_id)
        if active is not None:
            manager = active["permissions"]
            if target_state == "BASELINE":
                return manager.restore_baseline(trial_id)
            return manager.restore(trial_id)
        if target_state == "BASELINE":
            return {
                "verified": False,
                "target_state": target_state,
                "reason": "the active Agent runtime is no longer available",
            }
        token_root = self.config.private_root / "mcp-tokens" / trial_id
        removed_tokens = 0
        if token_root.is_dir():
            for path in token_root.iterdir():
                if path.is_file():
                    path.unlink(missing_ok=True)
                    removed_tokens += 1
        return {
            "verified": True,
            "target_state": target_state,
            "mcp_tokens_removed": removed_tokens,
        }

    def reset_environment(self, operation_id: str, application: str) -> Mapping[str, Any]:
        if application != "otel-demo":
            return {
                "verified": False,
                "reason": f"unsupported application: {application}",
            }
        episode = load_fixed_episode(
            fixed_otel_episode_ref(self.config.repo_root), root=self.config.repo_root
        )
        gate = KubernetesEnvironmentGate(self.config.kubeconfig)
        traffic = KubernetesTrafficEvidence(gate, episode)
        resetter = OtelDemoResetter(
            repo_root=self.config.repo_root,
            kubeconfig=self.config.kubeconfig,
            runtime_env_file=self.config.runtime_env_file,
            chart_file=self.config.otel_chart_file,
            environment_gate=gate,
            traffic_evidence=traffic,
            timeout_seconds=120,
            recovery_timeout_seconds=180,
            verify_only=False,
        )
        return resetter.reset(operation_id, episode)

    def verify_environment(self, operation_id: str, application: str) -> Mapping[str, Any]:
        """Verify a clean OTel Demo state without mutating the namespace."""
        if application != "otel-demo":
            return {
                "verified": False,
                "reason": f"unsupported application: {application}",
            }
        episode = load_fixed_episode(
            fixed_otel_episode_ref(self.config.repo_root), root=self.config.repo_root
        )
        gate = KubernetesEnvironmentGate(self.config.kubeconfig)
        traffic = KubernetesTrafficEvidence(gate, episode)
        resetter = OtelDemoResetter(
            repo_root=self.config.repo_root,
            kubeconfig=self.config.kubeconfig,
            runtime_env_file=self.config.runtime_env_file,
            chart_file=self.config.otel_chart_file,
            environment_gate=gate,
            traffic_evidence=traffic,
            timeout_seconds=120,
            recovery_timeout_seconds=180,
            verify_only=True,
        )
        return resetter.reset(operation_id, episode)


def write_incluster_kubeconfig(path: Path) -> None:
    token_path = Path("/var/run/secrets/kubernetes.io/serviceaccount/token")
    ca_path = Path("/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")
    if not token_path.is_file() or not ca_path.is_file():
        raise RuntimeConfigurationError("in-cluster service account files are missing")
    host = os.environ.get("KUBERNETES_SERVICE_HOST")
    port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
    if not host:
        raise RuntimeConfigurationError("KUBERNETES_SERVICE_HOST is missing")
    document = {
        "apiVersion": "v1",
        "kind": "Config",
        "clusters": [
            {
                "name": "kubernetes",
                "cluster": {
                    "server": f"https://{host}:{port}",
                    "certificate-authority-data": base64.b64encode(
                        ca_path.read_bytes()
                    ).decode("ascii"),
                },
            }
        ],
        "users": [
            {
                "name": CONTROLLER_SERVICE_ACCOUNT,
                "user": {"tokenFile": str(token_path)},
            }
        ],
        "contexts": [
            {
                "name": CONTROLLER_SERVICE_ACCOUNT,
                "context": {
                    "cluster": "kubernetes",
                    "user": CONTROLLER_SERVICE_ACCOUNT,
                    "namespace": "otel-demo",
                },
            }
        ],
        "current-context": CONTROLLER_SERVICE_ACCOUNT,
    }
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    temporary.chmod(0o600)
    os.replace(temporary, path)
    path.chmod(0o600)
