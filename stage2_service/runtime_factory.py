"""Production composition root for the Stage-2 single service."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

import yaml

from mcp_servers.chaos_control.service import ChaosControlService, RuntimeConfig

from disturbances.kubernetes_runtime import KubernetesDisturbanceClient

from .artifacts import ArtifactStore
from .campaign import CampaignEngine
from .contracts import CampaignRequest, CampaignResult
from .disturbance import RuntimeDisturbancePlanner
from .episode import load_fixed_episode
from .evaluator import Stage2Evaluator
from .finalization import Stage2Finalizer
from .harness_runtime import NativeHarnessRunner
from .kubernetes_permissions import KubernetesPermissionBackend
from .mcp_supervisor import McpSupervisor
from .permissions import Stage2PermissionManager
from .preparation import ApplicationTrafficCapabilityIssuer, KubernetesTrialPreparer
from .reset import OtelDemoResetter
from .runtime_adapters import (
    CompositeDisturbanceExecutor,
    KubernetesEnvironmentGate,
    McpTokenStateRegistry,
)


class RuntimeConfigurationError(RuntimeError):
    pass


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
        }
        missing = [key for key, value in required.items() if not value]
        if missing:
            raise RuntimeConfigurationError(
                "missing Stage-2 runtime values: " + ", ".join(sorted(missing))
            )
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
        )


class KubernetesTrafficEvidence:
    def __init__(
        self,
        gate: KubernetesEnvironmentGate,
        episode,
        *,
        stats_url: str = "http://load-generator.otel-demo.svc.cluster.local:8089/stats/requests",
        stats_loader: Callable[[str], Mapping[str, Any]] | None = None,
        stats_resetter: Callable[[str], None] | None = None,
    ):
        self.gate = gate
        self.episode = episode
        self.stats_url = stats_url
        self.stats_loader = stats_loader or self._load_stats
        self.stats_resetter = stats_resetter or self._reset_stats
        self._baselines: dict[str, dict[str, Any]] = {}

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
        cart_response_sum_ms = sum(
            float(item.get("avg_response_time") or 0.0)
            * int(item.get("num_requests") or 0)
            for item in cart_rows
        )
        cart_avg_ms = (
            cart_response_sum_ms / cart_requests if cart_requests else 0.0
        )
        success_rate = (requests - failures) / requests if requests else 0.0
        traffic = (
            ready
            and locust.get("state") == "running"
            and users > 0
            and requests > 0
            and total_rps > 0
        )
        business = (
            value.get("qualified") is True
            and traffic
            and success_rate >= 0.95
            and p95_ms <= 1_000
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
            "cart_requests": cart_requests,
            "cart_failures": cart_failures,
            "cart_response_sum_ms": cart_response_sum_ms,
            "cart_avg_response_ms": cart_avg_ms,
        }

    def record_baseline(
        self, trial_id: str, evidence: Mapping[str, Any]
    ) -> None:
        self._baselines[trial_id] = dict(evidence)

    def effect_since(self, trial_id: str) -> Mapping[str, Any]:
        baseline = self._baselines.get(trial_id)
        current = dict(self.current())
        if baseline is None:
            return {"verified": False, "reason": "trial baseline is missing"}
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
        verified = (
            request_delta >= 3
            and (
                latency_delta_ms >= 100.0
                or interval_success_rate < 0.95
            )
        )
        return {
            "verified": verified,
            "observer": "otel-demo built-in Locust cart delta",
            "cart_request_delta": request_delta,
            "cart_failure_delta": failure_delta,
            "baseline_cart_avg_response_ms": baseline_avg_ms,
            "fault_window_cart_avg_response_ms": interval_avg_ms,
            "latency_delta_ms": latency_delta_ms,
            "fault_window_success_rate": interval_success_rate,
            "minimum_samples": 3,
        }

    def reset_and_wait_healthy(
        self,
        *,
        timeout_seconds: int = 300,
        minimum_requests: int = 20,
        stability_samples: int = 3,
    ) -> Mapping[str, Any]:
        reset_url = self.stats_url.removesuffix("/stats/requests") + "/stats/reset"
        deadline = time.monotonic() + timeout_seconds
        stable = 0
        last: dict[str, Any] = {}
        while stable < stability_samples:
            try:
                last = dict(self.current())
            except Exception as exc:  # noqa: BLE001
                last = {
                    "business_healthy": False,
                    "stage": "warmup",
                    "error_type": type(exc).__name__,
                }
            current_rps = float(last.get("current_rps") or 0.0)
            current_fail = float(last.get("current_fail_per_sec") or 0.0)
            stable = (
                stable + 1
                if last.get("load_generator_ready") is True
                and current_rps > 0
                and current_fail / current_rps <= 0.05
                else 0
            )
            if stable >= stability_samples:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return last
            time.sleep(min(10.0, remaining))
        reset_error: dict[str, Any] = {}
        while True:
            try:
                self.stats_resetter(reset_url)
                break
            except Exception as exc:  # noqa: BLE001
                reset_error = {
                    "business_healthy": False,
                    "stage": "stats_reset",
                    "error_type": type(exc).__name__,
                }
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return reset_error
            time.sleep(min(5.0, remaining))
        last = {}
        while True:
            try:
                last = dict(self.current())
            except Exception as exc:  # noqa: BLE001
                last = {"business_healthy": False, "error_type": type(exc).__name__}
            if (
                int(last.get("num_requests") or 0) >= minimum_requests
                and last.get("business_healthy") is True
            ):
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

    @staticmethod
    def _reset_stats(url: str) -> None:
        request = urllib.request.Request(url, headers={"Accept": "text/html"})
        with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310
            if not 200 <= int(response.status) < 300:
                raise RuntimeConfigurationError("Locust statistics reset was rejected")


class DirectChaosCleanup:
    def __init__(self, service: ChaosControlService, kubeconfig: Path):
        self.service = service
        self.kubeconfig = str(kubeconfig)

    def destroy(self, cleanup_handle: str):
        return asyncio.run(
            self.service.destroy_experiment(
                cleanup_handle=cleanup_handle, kubeconfig=self.kubeconfig
            )
        )

    def status(self, cleanup_handle: str):
        return asyncio.run(
            self.service.recovery_status(
                cleanup_handle=cleanup_handle, kubeconfig=self.kubeconfig
            )
        )

    def inventory(self, namespace: str):
        return asyncio.run(
            self.service.inventory_run(
                namespace=namespace, kubeconfig=self.kubeconfig
            )
        )


class Stage2System:
    def __init__(self, config: Stage2RuntimeConfig):
        self.config = config
        for path in (config.private_root, config.artifact_root):
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(path, 0o700)
        # Projected ServiceAccount tokens are Pod-bound and rotate. A kubeconfig
        # persisted on the evidence PVC must therefore be replaced at every
        # service start rather than reused across Deployment revisions.
        write_incluster_kubeconfig(config.kubeconfig)

    def run(
        self, request: CampaignRequest, event_observer=None, stop_requested=None
    ) -> CampaignResult:
        episode = load_fixed_episode(request.episode, root=self.config.repo_root)
        gate = KubernetesEnvironmentGate(self.config.kubeconfig)
        traffic = KubernetesTrafficEvidence(gate, episode)
        private = self.config.private_root
        baseline_dir = private / "chaos-control/baseline"
        ledger_dir = private / "chaos-control/active"
        for path in (baseline_dir, ledger_dir):
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(path, 0o700)
        permission_backend = KubernetesPermissionBackend.from_incluster()
        token_registry = McpTokenStateRegistry(private / "mcp-tokens")
        permissions = Stage2PermissionManager(
            private_root=private / "permissions",
            token_registry=token_registry,
            permission_backend=permission_backend,
        )
        issuer = ApplicationTrafficCapabilityIssuer(
            ledger_dir=baseline_dir,
            controller_pod_uid=self.config.controller_pod_uid,
            traffic_evidence=traffic,
        )
        preparer = KubernetesTrialPreparer.from_incluster(issuer)
        controller_token_ref = (
            "k8s://resiliencebenchmark-system/serviceaccount/resbench-stage2"
        )
        mcp_environment = {
            "RESBENCH_K8S_RO_KUBECONFIG": str(self.config.kubeconfig),
            "RESBENCH_K8S_RO_NAMESPACE_ALLOWLIST": "otel-demo",
            "RESBENCH_PROMETHEUS_URL": "http://prometheus.observability.svc:9090",
            "RESBENCH_JAEGER_URL": "http://jaeger-query.observability.svc:16686",
            "RESBENCH_LOKI_URL": "http://loki.observability.svc:3100",
            "RESBENCH_TELEMETRY_ALLOWED_NAMESPACES": "otel-demo",
            "RESBENCH_JAEGER_ALLOWED_SERVICES": "frontend,frontend-proxy,checkout,cart,payment,shipping",
            "RESBENCH_TELEMETRY_ALLOW_RAW_QUERIES": "false",
            "RESBENCH_TELEMETRY_DISTURBANCE_DIR": str(private / "telemetry"),
            "RESBENCH_SOURCE_ROOT": str(self.config.source_root),
            "RESBENCH_SOURCE_ALLOWED_APPLICATIONS": "otel-demo",
            "RESBENCH_CHAOS_EXECUTE_ENABLED": "true",
            "RESBENCH_CHAOS_KUBECONFIG": str(self.config.kubeconfig),
            "RESBENCH_CHAOS_NAMESPACE_ALLOWLIST": "otel-demo",
            "RESBENCH_CHAOS_CONTROLLER_TOKEN_REF": controller_token_ref,
            "RESBENCH_CHAOS_CONTROLLER_POD_UID": self.config.controller_pod_uid,
            "RESBENCH_CHAOS_CONTROLLER_POD_NAMESPACE": self.config.controller_pod_namespace,
            "RESBENCH_CHAOS_CONTROLLER_POD_NAME": self.config.controller_pod_name,
            "RESBENCH_CHAOS_BASELINE_LEDGER_DIR": str(baseline_dir),
            "RESBENCH_CHAOS_LEDGER_DIR": str(ledger_dir),
        }
        supervisor = McpSupervisor(
            private_root=private / "mcp-runtime",
            base_environment=mcp_environment,
        )
        harness_environment = {
            "RESBENCH_LLM_BASE_URL": self.config.llm_base_url,
            "RESBENCH_LLM_API_KEY": self.config.llm_api_key,
            "RESBENCH_CHAOS_CONTROLLER_TOKEN_REF": controller_token_ref,
            "RESBENCH_CHAOS_CONTROLLER_POD_UID": self.config.controller_pod_uid,
            "RESBENCH_CODEX_EVAL_BIN": os.environ.get(
                "RESBENCH_CODEX_EVAL_BIN", ""
            ),
            "STAGE2_BLADEAI_PYTHON": os.environ.get(
                "STAGE2_BLADEAI_PYTHON", "/opt/bladeai-venv/bin/python"
            ),
            "STAGE2_BLADEAI_MODEL": request.model_by_harness.get(
                next(
                    harness
                    for harness in request.harnesses
                    if harness.value == "bladeai"
                ),
                "gpt-5.6",
            )
            if any(harness.value == "bladeai" for harness in request.harnesses)
            else "gpt-5.6",
        }
        harness_runner = NativeHarnessRunner(
            repo_root=self.config.repo_root,
            private_root=private / "harness",
            artifact_root=self.config.artifact_root / "harness",
            permissions=permissions,
            mcp_supervisor=supervisor,
            base_environment=harness_environment,
            # Keep the complete Trial within five minutes: Agent 180s,
            # controller cleanup/recovery 60s, and reset verification 60s.
            timeout_seconds=180,
        )
        runtime_client = KubernetesDisturbanceClient.from_kubeconfig(
            self.config.kubeconfig
        )
        disturbance_executor = CompositeDisturbanceExecutor(
            kubernetes_client=runtime_client,
            mcp_tokens=token_registry,
            rbac_permissions=permission_backend,
        )
        chaos_service = ChaosControlService(RuntimeConfig.from_env(mcp_environment))
        finalizer = Stage2Finalizer(
            DirectChaosCleanup(chaos_service, self.config.kubeconfig),
            traffic,
            recovery_timeout_seconds=60,
        )
        resetter = OtelDemoResetter(
            repo_root=self.config.repo_root,
            kubeconfig=self.config.kubeconfig,
            runtime_env_file=self.config.runtime_env_file,
            chart_file=self.config.otel_chart_file,
            environment_gate=gate,
            traffic_evidence=traffic,
            timeout_seconds=120,
            recovery_timeout_seconds=60,
            verify_only=True,
        )
        engine = CampaignEngine(
            episode=episode,
            environment_gate=gate,
            preparer=preparer,
            permissions=permissions,
            harness_runner=harness_runner,
            disturbance_planner=RuntimeDisturbancePlanner(),
            disturbance_executor=disturbance_executor,
            finalizer=finalizer,
            evaluator=Stage2Evaluator(),
            resetter=resetter,
            artifacts=ArtifactStore(self.config.artifact_root),
        )
        return engine.run(
            request,
            event_observer=event_observer,
            stop_requested=stop_requested,
        )


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
                "name": "resbench-stage2",
                "user": {"token": token_path.read_text(encoding="utf-8").strip()},
            }
        ],
        "contexts": [
            {
                "name": "resbench-stage2",
                "context": {
                    "cluster": "kubernetes",
                    "user": "resbench-stage2",
                    "namespace": "otel-demo",
                },
            }
        ],
        "current-context": "resbench-stage2",
    }
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    temporary.chmod(0o600)
    os.replace(temporary, path)
    path.chmod(0o600)
