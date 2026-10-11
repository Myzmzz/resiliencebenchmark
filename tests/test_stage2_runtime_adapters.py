from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

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


def test_mcp_token_registry_accepts_controller_generated_d0_trial_identity(
    tmp_path: Path,
):
    registry = McpTokenStateRegistry(tmp_path)
    trial_id = "d0-otel-accounting-20260908-bladeai-opus-003-bladeai"

    paths = registry.initialize(trial_id, {"telemetry_ro": "x" * 48})

    assert Path(paths["telemetry_ro"]).is_file()


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


def test_cart_recovery_is_not_blocked_by_unrelated_cold_start_endpoints():
    evidence = KubernetesTrafficEvidence(
        Gate(),
        Episode(),
        stats_loader=lambda _url: {
            "state": "running",
            "user_count": 5,
            "stats": [
                {
                    "name": "Aggregated",
                    "num_requests": 100,
                    "num_failures": 60,
                    "total_rps": 1.5,
                    "current_rps": 1.5,
                    "current_fail_per_sec": 0.2,
                    "response_time_percentile_0.95": 5000,
                },
                {
                    "name": "/api/cart",
                    "method": "GET",
                    "num_requests": 10,
                    "num_failures": 0,
                    "current_rps": 0.3,
                    "current_fail_per_sec": 0.0,
                    "avg_response_time": 20,
                },
            ],
        },
    ).current()

    assert evidence["business_scope"] == "cart"
    assert evidence["business_healthy"] is True
    assert evidence["success_rate"] == 0.4
    assert evidence["target_success_rate"] == 1.0


def test_cart_effect_without_actual_fault_window_is_not_inferred_from_late_counters():
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

    effect = evidence.effect_since(
        "campaign-test",
        SimpleNamespace(
            main_fault={"fault_type": "network-delay"},
            target=SimpleNamespace(name="cart-abc", uid="uid-current"),
        ),
    )

    assert effect["verified"] is False
    assert effect["reason"] == "actual fault window is not established"


def test_cpu_effect_uses_fault_specific_prometheus_evidence():
    snapshots = iter(
        [
            {
                "state": "running",
                "user_count": 1,
                "stats": [
                    {
                        "name": "Aggregated",
                        "num_requests": 10,
                        "num_failures": 0,
                        "total_rps": 1.0,
                        "current_rps": 1.0,
                        "response_time_percentile_0.95": 80,
                    }
                ],
            },
            {
                "state": "running",
                "user_count": 1,
                "stats": [
                    {
                        "name": "Aggregated",
                        "num_requests": 20,
                        "num_failures": 0,
                        "total_rps": 1.0,
                        "current_rps": 1.0,
                        "response_time_percentile_0.95": 80,
                    }
                ],
            },
        ]
    )
    evidence = KubernetesTrafficEvidence(
        Gate(),
        Episode(),
        stats_loader=lambda _url: next(snapshots),
        prometheus_loader=lambda **_kwargs: {
            "status": "success",
            "data": {
                "result": [
                    {"values": [[1, "0.02"], [2, "0.78"]]},
                ]
            },
        },
        prometheus_metadata_loader=lambda *_args: {"data": []},
    )
    evidence.record_baseline("campaign-cpu", evidence.current())

    effect = evidence.effect_since(
        "campaign-cpu",
        SimpleNamespace(
            trial_id="campaign-cpu",
            main_fault={
                "fault_type": "cpu-load",
                "intensity": {"cpu_percent": 80},
                "evidence_window": {"start": 1, "end": 2},
            },
            target=SimpleNamespace(namespace="otel-demo", name="cart-abc", uid="uid-current"),
        ),
    )

    assert effect["verified"] is True
    assert effect["physical_effect"]["peak_value"] == 0.78


def test_recovery_window_uses_final_metric_without_request_count_floor():
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
                    "num_requests": 7,
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
        timeout_seconds=1,
        stability_samples=1,
        baseline={"target_success_rate": 1.0},
        recovery_condition={
            "metric": "target_success_rate",
            "operator": "at_or_above",
            "threshold": 0.99,
        },
    )

    assert recovered["business_healthy"] is True
    assert recovered["num_requests"] == 7
    assert recovered["recovery_condition_evidence"]["matched"] is True
    assert recovered["recovery_condition_evidence"]["request_delta"] == 7
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
        timeout_seconds=1, stability_samples=1
    )

    assert recovered["business_healthy"] is True
    assert len(attempts) == 2


def test_recovery_window_waits_for_health_without_resetting_cold_start_evidence(monkeypatch):
    resets = []
    rows = iter(
        [
            # Stable traffic before the first reset.
            (25, 0, 80),
            # First fresh interval still contains cold-start failures.
            (20, 10, 500),
            # Second fresh interval is healthy.
            (20, 0, 90),
        ]
    )

    def load(_url):
        requests, failures, p95 = next(rows)
        return {
            "state": "running",
            "user_count": 5,
            "stats": [
                {
                    "name": "Aggregated",
                    "num_requests": requests,
                    "num_failures": failures,
                    "total_rps": 1.0,
                    "current_rps": 1.0,
                    "current_fail_per_sec": 0.0,
                    "response_time_percentile_0.95": p95,
                }
            ],
        }

    monkeypatch.setattr("stage2_service.runtime_factory.time.sleep", lambda _seconds: None)
    evidence = KubernetesTrafficEvidence(
        Gate(),
        Episode(),
        stats_loader=load,
        stats_resetter=resets.append,
    )

    recovered = evidence.reset_and_wait_healthy(
        timeout_seconds=1, stability_samples=1
    )

    assert recovered["business_healthy"] is True
    assert recovered["stats_reset_count"] == 1
    assert len(resets) == 1


def test_cpu_effect_condition_is_judged_on_the_pods_own_cpu():
    evidence = KubernetesTrafficEvidence(
        Gate(),
        Episode(),
        prometheus_loader=lambda **_kwargs: {
            "status": "success",
            "data": {"result": [{"values": [[1, "0.02"], [2, "0.78"]]}]},
        },
        prometheus_metadata_loader=lambda *_args: {"data": []},
    )
    evidence._samples = [
        (1, {"cart_requests": 100, "cart_failures": 0, "cart_response_sum_ms": 1000, "cart_avg_response_ms": 10}),
        (2, {"cart_requests": 110, "cart_failures": 0, "cart_response_sum_ms": 1100}),
    ]

    effect = evidence.effect_since(
        "campaign-cpu",
        SimpleNamespace(
            trial_id="campaign-cpu",
            main_fault={"fault_type": "cpu-load", "intensity": {"cpu_percent": 80}, "evidence_window": {"start": 1, "end": 2}},
            target=SimpleNamespace(namespace="otel-demo", name="cart-abc", uid="uid-current"),
        ),
        {"effect_condition": {"metric": "target_cpu_cores", "operator": "increase_by_at_least", "threshold": 0.5}},
    )

    condition = effect["service_condition"]
    assert condition["matched"] is True
    assert condition["scope"] == "target_pod"
    assert (condition["baseline_value"], condition["observed_value"]) == (0.02, 0.78)


def test_cpu_recovery_condition_compares_the_pod_with_its_pre_fault_cpu():
    evidence = KubernetesTrafficEvidence(
        Gate(),
        Episode(),
        stats_loader=lambda _url: {
            "state": "running",
            "user_count": 5,
            "stats": [
                {
                    "name": "Aggregated",
                    "num_requests": 7,
                    "num_failures": 0,
                    "total_rps": 1.0,
                    "current_rps": 1.0,
                    "current_fail_per_sec": 0.0,
                    "response_time_percentile_0.95": 90,
                }
            ],
        },
        stats_resetter=lambda _url: None,
        prometheus_loader=lambda **_kwargs: {
            "status": "success",
            "data": {"result": [{"values": [[1, "0.06"]]}]},
        },
    )

    recovered = evidence.reset_and_wait_healthy(
        timeout_seconds=1,
        stability_samples=1,
        baseline={"target_success_rate": 1.0},
        recovery_condition={"metric": "target_cpu_cores", "operator": "within_baseline_delta", "threshold": 0.3},
        target=SimpleNamespace(namespace="otel-demo", name="cart-abc", uid="uid-current"),
        resource_baseline=0.05,
    )

    condition = recovered["recovery_condition_evidence"]
    assert condition["matched"] is True
    assert (condition["baseline_value"], condition["observed_value"]) == (0.05, 0.06)


def _locust_stats(*rows):
    return {"state": "running", "user_count": 5, "stats": list(rows)}


AGGREGATE_ROW = {
    "name": "Aggregated",
    "num_requests": 20,
    "num_failures": 0,
    "total_rps": 1.0,
    "current_rps": 1.0,
    "current_fail_per_sec": 0.0,
    "response_time_percentile_0.95": 90,
}
SUCCESS_RECOVERY = {
    "baseline": {"target_success_rate": 1.0},
    "recovery_condition": {"metric": "target_success_rate", "operator": "at_or_above", "threshold": 0.99},
}


def test_recovery_warmup_needs_three_samples_even_when_recovery_needs_seven(monkeypatch):
    monkeypatch.setattr("stage2_service.runtime_factory.time.sleep", lambda _seconds: None)
    loads = []

    def load(url):
        loads.append(url)
        return _locust_stats(AGGREGATE_ROW)

    evidence = KubernetesTrafficEvidence(Gate(), Episode(), stats_loader=load, stats_resetter=lambda _url: None)
    recovered = evidence.reset_and_wait_healthy(timeout_seconds=60, stability_samples=7, **SUCCESS_RECOVERY)

    assert recovered["business_healthy"] is True
    assert len(loads) == 3 + 7
    assert [entry["phase"] for entry in recovered["sample_trace"]] == ["warmup"] * 3 + ["recovery"] * 7


def test_a_sparse_target_with_traffic_elsewhere_still_warms_up(monkeypatch):
    monkeypatch.setattr("stage2_service.runtime_factory.time.sleep", lambda _seconds: None)
    idle_cart = {"name": "/api/cart", "num_requests": 3, "num_failures": 0, "current_rps": 0.0, "current_fail_per_sec": 0.0}
    resets = []
    evidence = KubernetesTrafficEvidence(
        Gate(), Episode(), stats_loader=lambda _url: _locust_stats(idle_cart, AGGREGATE_ROW), stats_resetter=resets.append,
    )
    recovered = evidence.reset_and_wait_healthy(timeout_seconds=1, stability_samples=1, **SUCCESS_RECOVERY)

    assert recovered["business_healthy"] is True
    assert len(resets) == 1
    assert (recovered["sample_trace"][0]["phase"], recovered["sample_trace"][0]["ok"]) == ("warmup", True)


def test_a_failing_target_still_blocks_warmup_and_the_trace_says_why(monkeypatch):
    monkeypatch.setattr("stage2_service.runtime_factory.time.sleep", lambda _seconds: None)
    failing_cart = {"name": "/api/cart", "num_requests": 10, "num_failures": 5, "current_rps": 1.0, "current_fail_per_sec": 0.5}
    resets = []
    evidence = KubernetesTrafficEvidence(
        Gate(), Episode(), stats_loader=lambda _url: _locust_stats(failing_cart, AGGREGATE_ROW), stats_resetter=resets.append,
    )
    recovered = evidence.reset_and_wait_healthy(timeout_seconds=1, stability_samples=7, **SUCCESS_RECOVERY)

    assert recovered["business_healthy"] is False
    assert resets == []
    trace = recovered["sample_trace"]
    assert trace and {entry["phase"] for entry in trace} == {"warmup"}
    assert not any(entry["ok"] for entry in trace)
    assert trace[-1]["target_current_fail_per_sec"] == 0.5


class ChaosRunner(Runner):
    """Environment-gate runner whose ChaosBlade inventory is cluster-wide."""

    def __init__(self, chaos_items, **kwargs):
        super().__init__(**kwargs)
        self.chaos_items = chaos_items
        self.seen: list[list[str]] = []

    def run(self, argv, *, timeout=60):
        self.seen.append(list(argv))
        if "chaosblades.chaosblade.io" in argv:
            return subprocess.CompletedProcess(
                argv, 0, json.dumps({"items": self.chaos_items}), ""
            )
        return super().run(argv, timeout=timeout)


def _labelled_blade(namespace: str) -> dict:
    return {
        "metadata": {"name": f"cc-{namespace}", "labels": {"benchmark.namespace": namespace}},
    }


def _matcher_blade(namespace: str) -> dict:
    return {
        "metadata": {"name": f"unlabelled-{namespace}"},
        "spec": {"experiments": [{"matchers": [{"name": "namespace", "value": [namespace]}]}]},
    }


def _replica_episode(namespace: str):
    return SimpleNamespace(public=SimpleNamespace(environment_snapshot={"namespace": namespace}))


def test_environment_gate_ignores_another_replicas_chaosblade(tmp_path: Path, monkeypatch):
    """A sibling replica's injection must not block this replica's trial."""
    monkeypatch.setenv("RESBENCH_APPLICATION_NAMESPACE", "otel-demo-01")
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("apiVersion: v1\n", encoding="utf-8")
    runner = ChaosRunner([_labelled_blade("otel-demo-02"), _matcher_blade("otel-demo-03")])

    verdict = KubernetesEnvironmentGate(kubeconfig, runner=runner).qualify(
        Episode()
    )

    assert verdict["qualified"] is True
    assert verdict["active_chaosblade_count"] == 0
    assert verdict["foreign_chaosblade_count"] == 2
    assert verdict["cluster_chaosblade_count"] == 2


def test_environment_gate_still_blocks_on_this_replicas_chaosblade(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("RESBENCH_APPLICATION_NAMESPACE", "otel-demo-01")
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("apiVersion: v1\n", encoding="utf-8")
    runner = ChaosRunner([_labelled_blade("otel-demo-01"), _labelled_blade("otel-demo-02")])

    verdict = KubernetesEnvironmentGate(kubeconfig, runner=runner).qualify(
        Episode()
    )

    assert verdict["qualified"] is False
    assert verdict["active_chaosblade_count"] == 1


def test_environment_gate_never_prefix_matches_the_full_system(tmp_path: Path, monkeypatch):
    """``otel-demo`` and ``otel-demo-01`` share a prefix and must stay separate."""
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("apiVersion: v1\n", encoding="utf-8")
    runner = ChaosRunner([_labelled_blade("otel-demo-01")])

    # Bound to the full system: a replica's fault is someone else's.
    monkeypatch.delenv("RESBENCH_APPLICATION_NAMESPACE", raising=False)
    full = KubernetesEnvironmentGate(kubeconfig, runner=runner).qualify(Episode())
    assert full["qualified"] is True
    assert full["foreign_chaosblade_count"] == 1

    # Bound to the replica: the same fault is its own.
    monkeypatch.setenv("RESBENCH_APPLICATION_NAMESPACE", "otel-demo-01")
    replica = KubernetesEnvironmentGate(kubeconfig, runner=runner).qualify(Episode())
    assert replica["qualified"] is False


def test_environment_gate_blocks_on_an_unattributable_chaosblade(tmp_path: Path, monkeypatch):
    """A leftover with no target namespace still fails the gate, as before."""
    monkeypatch.setenv("RESBENCH_APPLICATION_NAMESPACE", "otel-demo-01")
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("apiVersion: v1\n", encoding="utf-8")
    runner = ChaosRunner([{"metadata": {"name": "orphan"}}])

    verdict = KubernetesEnvironmentGate(kubeconfig, runner=runner).qualify(
        Episode()
    )

    assert verdict["qualified"] is False
    assert verdict["unattributed_chaosblade_names"] == ["orphan"]


def test_environment_gate_reads_the_replica_the_frozen_episode_was_copied_to(tmp_path: Path, monkeypatch):
    """The Episode is hash-frozen on otel-demo; a replica is a copy of it.

    Comparing the snapshot with the replica namespace would block every
    replica trial, which is exactly what the first live run did.
    """
    monkeypatch.setenv("RESBENCH_APPLICATION_NAMESPACE", "otel-demo-01")
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("apiVersion: v1\n", encoding="utf-8")
    runner = ChaosRunner([])

    verdict = KubernetesEnvironmentGate(kubeconfig, runner=runner).qualify(Episode())

    assert verdict["qualified"] is True
    assert verdict["application_namespace"] == "otel-demo-01"
    # The cluster was read in the replica, not in the copied system.
    namespaces = [argv[argv.index("-n") + 1] for argv, in [(call,) for call in runner.seen] if "-n" in argv]
    assert namespaces == ["otel-demo-01"]


def test_environment_gate_rejects_an_episode_for_another_system(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("RESBENCH_APPLICATION_NAMESPACE", "otel-demo-01")
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("apiVersion: v1\n", encoding="utf-8")

    verdict = KubernetesEnvironmentGate(kubeconfig, runner=ChaosRunner([])).qualify(
        _replica_episode("sock-shop")
    )

    assert verdict["qualified"] is False
    assert verdict["reason"] == "fixed Episode namespace is not otel-demo"
