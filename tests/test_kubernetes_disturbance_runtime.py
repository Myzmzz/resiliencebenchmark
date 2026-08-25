from __future__ import annotations

from types import SimpleNamespace

import pytest

from disturbances.kubernetes_runtime import (
    KubernetesDisturbanceClient,
    KubernetesRuntimeError,
)


def _pod(name: str, uid: str, owner_uid: str, *, ready: bool = True):
    return SimpleNamespace(
        metadata=SimpleNamespace(
            name=name,
            uid=uid,
            owner_references=[
                SimpleNamespace(kind="ReplicaSet", uid=owner_uid, controller=True)
            ],
        ),
        status=SimpleNamespace(
            conditions=[SimpleNamespace(type="Ready", status="True" if ready else "False")]
        ),
    )


class CoreApi:
    def __init__(self):
        self.original = _pod("checkout-old", "uid-old", "rs-1")
        self.replacement = _pod("checkout-new", "uid-new", "rs-1")
        self.deleted = None
        self.quota = SimpleNamespace(
            metadata=SimpleNamespace(uid="quota-uid", labels={"owner": "platform"}),
            spec=SimpleNamespace(
                hard={
                    "requests.cpu": "2",
                    "limits.memory": "2Gi",
                    "pods": "30",
                }
            ),
        )
        self.quota_patches = []

    def read_namespaced_pod(self, *, name, namespace):
        return self.original

    def delete_namespaced_pod(self, *, name, namespace, body):
        self.deleted = {"name": name, "namespace": namespace, "body": body}

    def list_namespaced_pod(self, *, namespace):
        return SimpleNamespace(items=[self.replacement])

    def read_namespaced_resource_quota(self, *, name, namespace):
        return self.quota

    def patch_namespaced_resource_quota(self, *, name, namespace, body):
        self.quota_patches.append(body)
        self.quota = SimpleNamespace(
            metadata=SimpleNamespace(
                uid="quota-uid",
                labels=body["metadata"]["labels"],
            ),
            spec=SimpleNamespace(hard=body["spec"]["hard"]),
        )
        return self.quota


def test_restart_uses_server_side_uid_precondition_and_waits_for_ready_owner_replacement() -> None:
    api = CoreApi()
    client = KubernetesDisturbanceClient(api, sleep=lambda _seconds: None)

    replacement = client.restart_exact_pod(
        namespace="otel-demo",
        name="checkout-old",
        expected_uid="uid-old",
        timeout_seconds=30,
        labels={"benchmark.run_id": "run-1"},
    )

    assert api.deleted["body"]["preconditions"] == {"uid": "uid-old"}
    assert replacement == {"name": "checkout-new", "uid": "uid-new"}


def test_restart_refuses_stale_uid_without_deleting_anything() -> None:
    api = CoreApi()
    client = KubernetesDisturbanceClient(api)

    with pytest.raises(KubernetesRuntimeError, match="UID drifted"):
        client.restart_exact_pod(
            namespace="otel-demo",
            name="checkout-old",
            expected_uid="stale-uid",
            timeout_seconds=30,
            labels={},
        )

    assert api.deleted is None


def test_quota_reduction_and_restore_preserve_unrelated_limits_and_labels() -> None:
    api = CoreApi()
    client = KubernetesDisturbanceClient(api)
    previous = client.read_resource_quota(namespace="otel-demo", name="episode-quota")

    applied = client.patch_resource_quota(
        namespace="otel-demo",
        name="episode-quota",
        cpu_percent=50,
        memory_percent=25,
        labels={"benchmark.run_id": "run-1"},
    )
    restored = client.restore_resource_quota(
        namespace="otel-demo",
        name="episode-quota",
        previous=previous,
    )

    assert applied["hard"] == {
        "requests.cpu": "1000m",
        "limits.memory": "536870912",
        "pods": "30",
    }
    assert applied["labels"]["owner"] == "platform"
    assert applied["labels"]["benchmark.run_id"] == "run-1"
    assert restored["hard"] == previous["hard"]
    assert restored["labels"] == {"owner": "platform"}
