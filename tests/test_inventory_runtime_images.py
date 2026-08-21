from __future__ import annotations

from pathlib import Path

import pytest

from scripts import inventory_runtime_images as inventory


DIGEST = "sha256:" + "a" * 64


def fake_runner(argv: list[str]):
    namespace = argv[argv.index("-n") + 1]
    return {
        "items": [
            {
                "metadata": {"name": f"{namespace}-pod"},
                "spec": {"containers": [{"name": "app", "image": "private.example:85/project/app:v1"}]},
                "status": {
                    "containerStatuses": [
                        {
                            "name": "app",
                            "imageID": f"docker-pullable://private.example:85/project/app@{DIGEST}",
                            "ready": True,
                            "restartCount": 0,
                        }
                    ]
                },
            }
        ]
    }


def test_build_report_records_digest_without_registry_endpoint(tmp_path):
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("apiVersion: v1\n", encoding="utf-8")

    report = inventory.build_report(["otel-demo"], kubeconfig, fake_runner)
    encoded = str(report)
    item = report["spec"]["observations"][0]

    assert report["spec"]["summary"]["qualified"] is True
    assert report["spec"]["namespaceSummary"]["otel-demo"]["qualified"] is True
    assert item["imageDigest"] == DIGEST
    assert item["imageRef"] == "<registry>/app"
    assert "private.example" not in encoded


def test_report_fails_qualification_when_digest_or_readiness_missing(tmp_path):
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("apiVersion: v1\n", encoding="utf-8")

    def incomplete(_argv):
        return {
            "items": [
                {
                    "metadata": {"name": "pod"},
                    "spec": {"containers": [{"name": "app", "image": "app:latest"}]},
                    "status": {"containerStatuses": [{"name": "app", "imageID": "", "ready": False}]},
                }
            ]
        }

    report = inventory.build_report(["train-ticket"], kubeconfig, incomplete)

    assert report["spec"]["summary"] == {
        "containers": 1,
        "missingRuntimeDigest": 1,
        "unreadyContainers": 1,
        "qualified": False,
    }
    assert report["spec"]["namespaceSummary"]["train-ticket"]["qualified"] is False


@pytest.mark.parametrize("namespace", ["", "../default", "default;delete", "UPPER"])
def test_namespace_validation_rejects_unsafe_values(namespace):
    with pytest.raises(inventory.InventoryError):
        inventory.validate_namespace(namespace)


def test_redacted_image_ref_rejects_credential_like_or_malformed_refs():
    assert inventory.redacted_image_ref("https://user:pass@example/repo?token=x") == "<redacted-image-ref>"
    assert inventory.redacted_image_ref("repo/app@sha256:" + "b" * 64) == "<registry>/app"
    assert inventory.normalize_image_digest("containerd://sha256:" + "c" * 64) == "sha256:" + "c" * 64


def test_kubectl_argv_is_fixed_and_read_only(tmp_path):
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("apiVersion: v1\n", encoding="utf-8")
    calls = []

    def runner(argv):
        calls.append(argv)
        return {"items": []}

    report = inventory.build_report(["sock-shop"], kubeconfig, runner)

    assert report["spec"]["summary"]["qualified"] is False
    assert calls[0][-6:] == ["get", "pods", "-n", "sock-shop", "-o", "json"]
    assert not any(item in calls[0] for item in ["apply", "delete", "patch", "exec"])


def test_successful_init_container_is_inventoried_as_qualified(tmp_path):
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("apiVersion: v1\n", encoding="utf-8")

    def with_init(_argv):
        return {
            "items": [
                {
                    "metadata": {"name": "pod"},
                    "spec": {
                        "containers": [{"name": "app", "image": "repo/app:v1"}],
                        "initContainers": [{"name": "setup", "image": "repo/setup:v1"}],
                    },
                    "status": {
                        "containerStatuses": [
                            {"name": "app", "imageID": f"containerd://{DIGEST}", "ready": True}
                        ],
                        "initContainerStatuses": [
                            {
                                "name": "setup",
                                "imageID": "containerd://sha256:" + "b" * 64,
                                "ready": False,
                                "state": {"terminated": {"exitCode": 0}},
                            }
                        ],
                    },
                }
            ]
        }

    report = inventory.build_report(["otel-demo"], kubeconfig, with_init)

    assert report["spec"]["summary"]["qualified"] is True
    assert report["spec"]["summary"]["containers"] == 2
    assert {item["containerKind"] for item in report["spec"]["observations"]} == {
        "application",
        "init",
    }


def test_failed_init_container_blocks_qualification(tmp_path):
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("apiVersion: v1\n", encoding="utf-8")

    def failed_init(_argv):
        return {
            "items": [
                {
                    "metadata": {"name": "pod"},
                    "spec": {"initContainers": [{"name": "setup", "image": "repo/setup:v1"}]},
                    "status": {
                        "initContainerStatuses": [
                            {
                                "name": "setup",
                                "imageID": "containerd://sha256:" + "b" * 64,
                                "ready": False,
                                "state": {"terminated": {"exitCode": 1}},
                            }
                        ]
                    },
                }
            ]
        }

    report = inventory.build_report(["otel-demo"], kubeconfig, failed_init)

    assert report["spec"]["summary"]["qualified"] is False
    assert report["spec"]["summary"]["unreadyContainers"] == 1


def test_ephemeral_debug_container_is_visible_and_blocks_clean_baseline(tmp_path):
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("apiVersion: v1\n", encoding="utf-8")

    def with_debug(_argv):
        return {
            "items": [
                {
                    "metadata": {"name": "pod"},
                    "spec": {
                        "containers": [{"name": "app", "image": "repo/app:v1"}],
                        "ephemeralContainers": [{"name": "debug", "image": "repo/debug:v1"}],
                    },
                    "status": {
                        "containerStatuses": [
                            {"name": "app", "imageID": f"containerd://{DIGEST}", "ready": True}
                        ],
                        "ephemeralContainerStatuses": [
                            {
                                "name": "debug",
                                "imageID": "containerd://sha256:" + "c" * 64,
                                "ready": False,
                            }
                        ],
                    },
                }
            ]
        }

    report = inventory.build_report(["otel-demo"], kubeconfig, with_debug)

    assert report["spec"]["summary"]["qualified"] is False
    assert any(
        item["containerKind"] == "ephemeral"
        for item in report["spec"]["observations"]
    )
