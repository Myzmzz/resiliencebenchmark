from __future__ import annotations

from types import SimpleNamespace

from resilience_agent.semantic_scan.kubernetes_scanner import (
    LiveKubernetesScanner,
    _KubernetesApiBundle,
)
from resilience_agent.semantic_scan.source_runtime import (
    ImageReference,
    LiveImageBinding,
    SourceSnapshot,
    build_source_runtime_manifest,
    compare_runtime_binding,
)


class FakeApi:
    def __init__(self, values=None):
        self.values = values or []

    def __getattr__(self, _name):
        return lambda *_args, **_kwargs: SimpleNamespace(items=list(self.values))


class FakeCustomApi:
    def list_namespaced_custom_object(self, **_kwargs):
        return {"items": []}


class FakeApiClient:
    @staticmethod
    def sanitize_for_serialization(value):
        return value


def fake_bundle(*, workloads, pods, services=None, configmaps=None):
    return _KubernetesApiBundle(
        api_client=FakeApiClient(),
        apps=SimpleNamespace(
            list_namespaced_deployment=lambda _ns: SimpleNamespace(items=workloads),
            list_namespaced_stateful_set=lambda _ns: SimpleNamespace(items=[]),
            list_namespaced_daemon_set=lambda _ns: SimpleNamespace(items=[]),
        ),
        batch=SimpleNamespace(
            list_namespaced_job=lambda _ns: SimpleNamespace(items=[]),
            list_namespaced_cron_job=lambda _ns: SimpleNamespace(items=[]),
        ),
        core=SimpleNamespace(
            list_namespaced_pod=lambda _ns: SimpleNamespace(items=pods),
            list_namespaced_service=lambda _ns: SimpleNamespace(items=services or []),
            list_namespaced_config_map=lambda _ns: SimpleNamespace(items=configmaps or []),
        ),
        discovery=FakeApi(),
        autoscaling=FakeApi(),
        policy=FakeApi(),
        networking=FakeApi(),
        custom=FakeCustomApi(),
    )


def test_live_kubernetes_scanner_preserves_runtime_fields_and_redacts_secret_values():
    deployment = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": "checkout",
            "namespace": "otel-demo",
            "uid": "deploy-uid",
            "resourceVersion": "42",
        },
        "spec": {
            "replicas": 1,
            "template": {
                "spec": {
                    "terminationGracePeriodSeconds": 20,
                    "containers": [
                        {
                            "name": "checkout",
                            "image": "ghcr.io/open-telemetry/demo:2.2.0",
                            "command": ["/app/wrapper.sh"],
                            "args": ["serve"],
                            "env": [
                                {"name": "GOMEMLIMIT", "value": "16MiB"},
                                {"name": "API_TOKEN", "value": "must-not-leak"},
                            ],
                            "envFrom": [
                                {"secretRef": {"name": "checkout-secret"}},
                            ],
                        }
                    ],
                }
            },
        },
    }
    pod = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": "checkout-abc",
            "namespace": "otel-demo",
            "uid": "pod-uid",
            "ownerReferences": [{"kind": "ReplicaSet", "name": "checkout-rs"}],
        },
        "spec": {"containers": [{"name": "checkout", "image": "demo"}]},
        "status": {
            "phase": "Running",
            "containerStatuses": [
                {
                    "name": "checkout",
                    "image": "demo",
                    "imageID": "docker-pullable://demo@sha256:live",
                    "ready": True,
                    "restartCount": 0,
                }
            ],
        },
    }
    configmap = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": "otel-collector", "namespace": "otel-demo"},
        "data": {
            "relay": "processors:\n  batch: {}\nexporters:\n  otlp:\n    timeout: 5000",
            "apiToken": "must-not-leak",
        },
    }
    scanner = LiveKubernetesScanner(
        "otel-demo",
        api_bundle=fake_bundle(
            workloads=[deployment],
            pods=[pod],
            configmaps=[configmap],
        ),
    )

    manifest = scanner.scan()
    result = scanner.get_resource("Deployment", "checkout")["matches"][0]["resource"]
    cm = scanner.get_configmap_value("otel-collector", key="relay")

    assert manifest.source_paths == ["kubernetes://otel-demo"]
    assert manifest.kinds["Deployment"] == 1
    assert result["uid"] == "deploy-uid"
    assert result["containers"][0]["command"] == ["/app/wrapper.sh"]
    assert result["containers"][0]["args"] == ["serve"]
    assert result["containers"][0]["env"][1]["value"] == "<redacted>"
    assert result["containers"][0]["env_from"][0]["secret_ref"]["name"] == "checkout-secret"
    assert scanner.get_resource("Pod", "checkout-abc")["matches"][0]["resource"]["status"][
        "container_statuses"
    ][0]["image_id"] == "docker-pullable://demo@sha256:live"
    assert cm["matches"][0]["resource"]["data"]["relay"].startswith("processors:")
    assert "apiToken" not in cm["matches"][0]["resource"]["data"]
    assert scanner.completeness["Gateway"]["status"] == "complete"


def test_source_runtime_manifest_classifies_exact_drift_and_unknown_without_service_names():
    official = ImageReference(
        image="ghcr.io/open-telemetry/demo@sha256:official",
        digest="sha256:official",
        config_digest="sha256:config",
        layers=["sha256:base", "sha256:app"],
        entrypoint=["/app/server"],
        command=[],
        source_revision="b74a7bc7bbe66099c61951f42b24dab8b6f02d18",
    )
    exact = LiveImageBinding(
        namespace="otel-demo",
        workload_kind="Deployment",
        workload_name="checkout",
        container="checkout",
        image="demo",
        image_id="docker-pullable://demo@sha256:official",
    )
    drift = LiveImageBinding(
        namespace="otel-demo",
        workload_kind="Deployment",
        workload_name="recommendation",
        container="recommendation",
        image="demo",
        config_digest="sha256:changed",
        layers=["sha256:base", "sha256:app", "sha256:wrapper"],
        entrypoint=["/app/wrapper.sh"],
        source_revision="b74a7bc7bbe66099c61951f42b24dab8b6f02d18",
    )
    unknown = LiveImageBinding(
        namespace="otel-demo",
        workload_kind="Deployment",
        workload_name="quote",
        container="quote",
        image="demo",
    )

    assert compare_runtime_binding(exact, official).status == "exact"
    assert compare_runtime_binding(drift, official).status == "official_source_with_runtime_drift"
    assert compare_runtime_binding(unknown, None).status == "unknown"

    manifest = build_source_runtime_manifest(
        SourceSnapshot(
            path="/data/mj/resbench-sources/otel-demo-2.2.0",
            expected_version="2.2.0",
            expected_commit="b74a7bc7bbe66099c61951f42b24dab8b6f02d18",
            observed_commit="b74a7bc7bbe66099c61951f42b24dab8b6f02d18",
            status="exact",
        ),
        [exact, drift, unknown],
        {"checkout": official, "recommendation": official},
    )

    assert manifest.counts == {
        "exact": 1,
        "official_source_with_runtime_drift": 1,
        "unknown": 1,
    }
    assert len(manifest.manifest_sha256) == 64
