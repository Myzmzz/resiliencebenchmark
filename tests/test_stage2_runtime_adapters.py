from __future__ import annotations

import json
import subprocess
from pathlib import Path

from stage2_service.runtime_adapters import KubernetesEnvironmentGate, McpTokenStateRegistry
from stage2_service.runtime_factory import KubernetesTrafficEvidence


class Runner:
    def __init__(self, *, load_replicas=1, load_ready=1, chaos=0):
        self.load_replicas = load_replicas
        self.load_ready = load_ready
        self.chaos = chaos

    def run(self, argv, *, timeout=60):
        del timeout
        if "deployments" in argv:
            payload = {
                "items": [
                    {
                        "metadata": {"name": "frontend"},
                        "spec": {"replicas": 1},
                        "status": {"readyReplicas": 1},
                    },
                    {
                        "metadata": {"name": "load-generator"},
                        "spec": {"replicas": self.load_replicas},
                        "status": {"readyReplicas": self.load_ready},
                    },
                ]
            }
        else:
            payload = {"items": [{} for _ in range(self.chaos)]}
        return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")


class Public:
    environment_snapshot = {"namespace": "otel-demo"}


class Episode:
    public = Public()


def test_environment_gate_requires_application_owned_load_generator(tmp_path: Path):
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("apiVersion: v1\n", encoding="utf-8")

    qualified = KubernetesEnvironmentGate(kubeconfig, runner=Runner()).qualify(Episode())
    blocked = KubernetesEnvironmentGate(
        kubeconfig, runner=Runner(load_replicas=0, load_ready=0)
    ).qualify(Episode())

    assert qualified["qualified"] is True
    assert blocked["qualified"] is False
    assert blocked["built_in_load_generator_desired"] == 0


def test_mcp_permission_revocation_rotates_only_selected_server(tmp_path: Path):
    registry = McpTokenStateRegistry(tmp_path)
    trial_id = "campaign-1234567890abcdef-codex-t3"
    original = "a" * 48
    paths = registry.initialize(
        trial_id,
        {"chaos_control": original, "k8s_ro": "b" * 48},
    )

    evidence = registry.revoke(trial_id, "mcp.chaos.create")
    assert evidence["server"] == "chaos_control"
    assert Path(paths["chaos_control"]).read_text(encoding="utf-8") != original
    assert Path(paths["k8s_ro"]).read_text(encoding="utf-8") == "b" * 48

    restored = registry.restore(trial_id, "mcp.chaos.create")
    assert restored["verified"] is True
    assert Path(paths["chaos_control"]).read_text(encoding="utf-8") == original


class Gate:
    def qualify(self, _episode):
        return {
            "qualified": True,
            "desired_replicas": 23,
            "ready_replicas": 23,
            "built_in_load_generator_ready": 1,
        }


def test_application_traffic_evidence_uses_locust_requests_not_pod_readiness():
    healthy = KubernetesTrafficEvidence(
        Gate(),
        Episode(),
        stats_loader=lambda _url: {
            "state": "running",
            "user_count": 5,
            "stats": [
                {
                    "name": "Aggregated",
                    "num_requests": 100,
                    "num_failures": 2,
                    "total_rps": 1.5,
                    "response_time_percentile_0.95": 120,
                }
            ],
        },
    ).current()
    no_requests = KubernetesTrafficEvidence(
        Gate(),
        Episode(),
        stats_loader=lambda _url: {
            "state": "running",
            "user_count": 5,
            "stats": [{"name": "Aggregated", "num_requests": 0}],
        },
    ).current()

    assert healthy["traffic_observed"] is True
    assert healthy["business_healthy"] is True
    assert healthy["success_rate"] == 0.98
    assert no_requests["load_generator_ready"] is True
    assert no_requests["traffic_observed"] is False


def test_cart_fault_effect_is_computed_from_post_baseline_request_delta():
    snapshots = iter(
        [
            {
                "state": "running",
                "user_count": 5,
                "stats": [
                    {
                        "name": "Aggregated",
                        "num_requests": 100,
                        "num_failures": 0,
                        "total_rps": 1.0,
                        "avg_response_time": 20,
                        "response_time_percentile_0.95": 50,
                    },
                    {
                        "name": "/api/cart",
                        "method": "GET",
                        "num_requests": 10,
                        "num_failures": 0,
                        "avg_response_time": 20,
                    },
                ],
            },
            {
                "state": "running",
                "user_count": 5,
                "stats": [
                    {
                        "name": "Aggregated",
                        "num_requests": 130,
                        "num_failures": 0,
                        "total_rps": 1.0,
                        "avg_response_time": 80,
                        "response_time_percentile_0.95": 1300,
                    },
                    {
                        "name": "/api/cart",
                        "method": "GET",
                        "num_requests": 13,
                        "num_failures": 0,
                        "avg_response_time": 300,
                    },
                ],
            },
        ]
    )
    evidence = KubernetesTrafficEvidence(
        Gate(), Episode(), stats_loader=lambda _url: next(snapshots)
    )
    baseline = evidence.current()
    evidence.record_baseline("campaign-test", baseline)

    effect = evidence.effect_since("campaign-test")

    assert effect["verified"] is True
    assert effect["cart_request_delta"] == 3
    assert effect["latency_delta_ms"] > 100


def test_recovery_window_resets_only_locust_statistics_and_waits_for_requests():
    reset_urls = []
    evidence = KubernetesTrafficEvidence(
        Gate(),
        Episode(),
        stats_loader=lambda _url: {
            "state": "running",
            "user_count": 5,
            "stats": [
                {
                    "name": "Aggregated",
                    "num_requests": 25,
                    "num_failures": 0,
                    "total_rps": 1.0,
                    "current_rps": 1.0,
                    "current_fail_per_sec": 0.0,
                    "response_time_percentile_0.95": 90,
                }
            ],
        },
        stats_resetter=reset_urls.append,
    )

    recovered = evidence.reset_and_wait_healthy(
        timeout_seconds=1, minimum_requests=20, stability_samples=1
    )

    assert recovered["business_healthy"] is True
    assert reset_urls == [
        "http://load-generator.otel-demo.svc.cluster.local:8089/stats/reset"
    ]


def test_recovery_window_retries_stats_reset_during_load_generator_startup(monkeypatch):
    attempts = []

    def reset(url):
        attempts.append(url)
        if len(attempts) == 1:
            raise TimeoutError("load generator is not accepting HTTP yet")

    monkeypatch.setattr("stage2_service.runtime_factory.time.sleep", lambda _seconds: None)
    evidence = KubernetesTrafficEvidence(
        Gate(),
        Episode(),
        stats_loader=lambda _url: {
            "state": "running",
            "user_count": 5,
            "stats": [
                {
                    "name": "Aggregated",
                    "num_requests": 20,
                    "num_failures": 0,
                    "total_rps": 1.0,
                    "current_rps": 1.0,
                    "current_fail_per_sec": 0.0,
                    "response_time_percentile_0.95": 80,
                }
            ],
        },
        stats_resetter=reset,
    )

    recovered = evidence.reset_and_wait_healthy(
        timeout_seconds=1, minimum_requests=20, stability_samples=1
    )

    assert recovered["business_healthy"] is True
    assert len(attempts) == 2
