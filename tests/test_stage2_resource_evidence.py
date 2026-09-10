"""Platform-side Pod resource evidence: Prometheus first, Coroot as backup."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from stage2_service.finalization import Stage2Finalizer
from stage2_service.harness_runtime import _coroot_application_id
from stage2_service.request_observation import request_observability, target_request_effect
from stage2_service.runtime_factory import KubernetesTrafficEvidence

TARGET = SimpleNamespace(namespace="otel-demo", name="cart-abc", uid="uid-current", component="cart")


def range_response(*values: float) -> dict:
    return {"status": "success", "data": {"result": [{"values": [[index, str(value)] for index, value in enumerate(values)]}]}}


EMPTY = {"status": "success", "data": {"result": []}}


def cpu_runtime() -> SimpleNamespace:
    return SimpleNamespace(
        trial_id="trial",
        main_fault={"fault_type": "cpu-load", "intensity": {"cpu_percent": 80}, "evidence_window": {"start": 1, "end": 2}},
        target=TARGET,
    )


def test_cpu_effect_is_read_by_pod_labels_first():
    queries: list[str] = []

    def prometheus(**kwargs):
        queries.append(kwargs["query"])
        return range_response(0.02, 6.3)

    evidence = KubernetesTrafficEvidence(SimpleNamespace(), None, prometheus_loader=prometheus)

    effect = evidence._physical_fault_effect("trial", cpu_runtime())

    assert effect["verified"] is True
    assert effect["source"] == "prometheus_pod_labels"
    assert 'pod="cart-abc"' in queries[0] and 'container=""' in queries[0]


def test_cpu_effect_falls_back_to_coroot_when_prometheus_has_no_series():
    coroot_queries: list[str] = []

    def coroot(**kwargs):
        coroot_queries.append(kwargs["query"])
        return range_response(0.0, 6.4)

    evidence = KubernetesTrafficEvidence(
        SimpleNamespace(), None, prometheus_loader=lambda **_kwargs: EMPTY, coroot_loader=coroot
    )

    effect = evidence._physical_fault_effect("trial", cpu_runtime())

    assert effect["verified"] is True
    assert effect["source"] == "coroot"
    assert [attempt["source"] for attempt in effect["attempts"]] == [
        "prometheus_pod_labels", "prometheus_cgroup_path", "coroot",
    ]
    assert 'container_id=~"/k8s/otel-demo/cart-abc/.*"' in coroot_queries[0]


def test_target_resource_value_uses_the_backup_when_prometheus_fails():
    def failing(**_kwargs):
        raise OSError("prometheus unreachable")

    evidence = KubernetesTrafficEvidence(
        SimpleNamespace(), None, prometheus_loader=failing, coroot_loader=lambda **_kwargs: range_response(1.2, 1.5)
    )

    assert evidence.target_resource_value({"namespace": "otel-demo", "name": "cart-abc"}, "target_cpu_cores") == 1.5
    assert evidence.target_resource_value(TARGET, "target_latency_ms") is None


def test_otlp_request_series_are_tied_to_the_target_pod():
    def metadata(path, params):
        if path == "labels":
            return {"data": ["k8s_namespace_name", "k8s_pod_name", "job"]}
        if path == "label/__name__/values":
            return {"data": ["http_server_request_duration_seconds_count"]}
        return {"data": [{"k8s_namespace_name": "otel-demo", "k8s_pod_name": "cart-abc", "job": "cart"}]}

    observability = request_observability(cpu_runtime(), 1, 2, metadata)

    assert observability["status"] == "observable"
    assert observability["target_series"][0]["namespace_label"] == "k8s_namespace_name"
    selectors: list[str] = []
    target_request_effect(
        cpu_runtime(), observability, 100, 200, None,
        lambda **kwargs: selectors.append(kwargs["query"]) or {"data": {"result": []}},
    )
    assert selectors and all('k8s_namespace_name="otel-demo"' in query for query in selectors)


def test_resource_recovery_inputs_sample_the_pod_before_the_fault():
    calls: list[tuple] = []

    class Evidence:
        def target_resource_value(self, target, metric, *, at=None):
            calls.append((target, metric, at))
            return 0.05

    finalizer = SimpleNamespace(recovery_evidence=Evidence())
    fault_contract = {"evidence_window": {"start": "2026-09-10T10:34:44+00:00"}}

    inputs = Stage2Finalizer._resource_recovery_inputs(
        finalizer, {"metric": "target_cpu_cores"}, SimpleNamespace(target=TARGET), fault_contract
    )
    untouched = Stage2Finalizer._resource_recovery_inputs(
        finalizer, {"metric": "target_latency_ms"}, SimpleNamespace(target=TARGET), fault_contract
    )

    assert inputs == {"target": TARGET, "resource_baseline": 0.05}
    fault_start = datetime.fromisoformat("2026-09-10T10:34:44+00:00").timestamp()
    assert calls[0][2] == fault_start - 30  # 30 seconds before the fault started
    assert untouched == {}


def test_coroot_application_id_names_the_targets_deployment(monkeypatch):
    monkeypatch.delenv("RESBENCH_COROOT_PROJECT_ID", raising=False)

    assert _coroot_application_id({"RESBENCH_COROOT_PROJECT_ID": "9auios5b"}, TARGET) == "9auios5b:otel-demo:Deployment:cart"
    assert _coroot_application_id({}, TARGET) == "otel-demo:Deployment:cart"
