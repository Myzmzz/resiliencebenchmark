from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from scripts import deploy_application as deploy


class FakeRunner:
    def __init__(self, controllers: list[dict] | None = None):
        self.controllers = controllers or []
        self.calls: list[dict] = []

    def run(self, argv: list[str], *, stdin: str | None = None, timeout: int = 300):
        self.calls.append({"argv": list(argv), "stdin": stdin, "timeout": timeout})
        if "scale" in argv:
            resource = argv[argv.index("scale") + 1]
            name = resource.split("/", 1)[1]
            replica_arg = next(item for item in argv if item.startswith("--replicas="))
            replicas = int(replica_arg.split("=", 1)[1])
            for item in self.controllers:
                if item["metadata"]["name"] == name:
                    item["spec"]["replicas"] = replicas
        if "deployments,statefulsets" in argv and "get" in argv:
            return deploy.CommandResult(0, json.dumps({"items": self.controllers}), "")
        if any(item.startswith("statefulset/") for item in argv) and "get" in argv:
            return deploy.CommandResult(
                0,
                json.dumps({"spec": {"selector": {"matchLabels": {"app": "tsdb-mysql"}}}}),
                "",
            )
        if argv[-4:-2] == ["namespace", "otel-demo"] or ("namespace" in argv and "get" in argv):
            return deploy.CommandResult(0, "namespace/otel-demo\n", "")
        if "configmap" in argv and "get" in argv:
            return deploy.CommandResult(0, json.dumps({"data": {"active-system": "otel-demo"}}), "")
        return deploy.CommandResult(0, "", "")


def controller(name: str, replicas: int, *, standby: str | None = None, kind: str = "Deployment") -> dict:
    annotations = {} if standby is None else {deploy.STANDBY_ANNOTATION: standby}
    return {
        "apiVersion": "apps/v1",
        "kind": kind,
        "metadata": {"name": name, "annotations": annotations},
        "spec": {"replicas": replicas},
    }


def test_render_manifest_resolves_runtime_values_and_isolates_temp_namespace(tmp_path):
    path = tmp_path / "manifest.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "apiVersion": "v1",
                "kind": "List",
                "items": [
                    {
                        "apiVersion": "v1",
                        "kind": "Service",
                        "metadata": {"name": "entry"},
                        "spec": {
                            "type": "NodePort",
                            "externalTrafficPolicy": "Cluster",
                            "ports": [{"port": 80, "nodePort": 30080}],
                        },
                    },
                    {
                        "apiVersion": "apps/v1",
                        "kind": "Deployment",
                        "metadata": {"name": "app"},
                        "spec": {
                            "template": {
                                "spec": {
                                    "containers": [
                                        {
                                            "name": "app",
                                            "image": "${HARBOR_REGISTRY}/project/app:v1",
                                            "env": [{"name": "URL", "value": "http://entry.train-ticket.svc:80"}],
                                        }
                                    ]
                                }
                            }
                        },
                    },
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    rendered = deploy.render_manifest(
        path,
        {"HARBOR_REGISTRY": "harbor.example:85"},
        source_namespace="train-ticket",
        target_namespace="rb-train-ticket-test",
    )

    service, workload = rendered["items"]
    assert service["metadata"]["namespace"] == "rb-train-ticket-test"
    assert service["spec"]["type"] == "ClusterIP"
    assert "nodePort" not in service["spec"]["ports"][0]
    assert "externalTrafficPolicy" not in service["spec"]
    assert workload["spec"]["template"]["spec"]["containers"][0]["image"] == "harbor.example:85/project/app:v1"
    assert workload["spec"]["template"]["spec"]["containers"][0]["env"][0]["value"] == "http://entry.rb-train-ticket-test.svc:80"


def test_render_text_fails_closed_without_runtime_values():
    with pytest.raises(deploy.DeployError, match="missing runtime value"):
        deploy.render_text("image: ${HARBOR_REGISTRY}/app:v1", {})


def test_runtime_env_file_requires_private_permissions(tmp_path):
    path = tmp_path / "runtime.env"
    path.write_text("HARBOR_REGISTRY=harbor.example:85\n", encoding="utf-8")
    path.chmod(0o644)
    with pytest.raises(deploy.DeployError, match="must not be accessible"):
        deploy.runtime_environment({}, path)
    path.chmod(0o600)
    assert deploy.runtime_environment({}, path)["HARBOR_REGISTRY"] == "harbor.example:85"


def test_standby_records_replicas_before_scaling():
    fake = FakeRunner([controller("frontend", 2), controller("already-zero", 0, standby="1")])
    result = deploy.standby_application(fake, Path("/tmp/kubeconfig"), "otel-demo")
    commands = [call["argv"] for call in fake.calls]

    assert result == {"scaledToZero": 1, "alreadyStandby": 1}
    assert any(f"{deploy.STANDBY_ANNOTATION}=2" in item for argv in commands for item in argv)
    assert any("--replicas=0" in argv for argv in commands)


def test_activate_restores_annotated_replicas_and_waits():
    fake = FakeRunner([controller("gateway", 0, standby="3")])
    result = deploy.activate_application(fake, Path("/tmp/kubeconfig"), "train-ticket", "train-ticket", 60)
    commands = [call["argv"] for call in fake.calls]

    assert result == {"restored": 1, "intentionallyZero": 0}
    assert any("--replicas=3" in argv for argv in commands)
    assert any("rollout" in argv and "deployment/gateway" in argv for argv in commands)


def test_delete_boundary_rejects_shared_or_unsafe_namespaces():
    with pytest.raises(deploy.DeployError, match="protected namespace"):
        deploy.assert_delete_boundary("otel-demo", "observability")
    with pytest.raises(deploy.DeployError, match="must use"):
        deploy.assert_delete_boundary("otel-demo", "customer-production")
    deploy.assert_delete_boundary("otel-demo", "otel-demo")
    deploy.assert_delete_boundary("otel-demo", "rb-otel-demo-test")


def test_train_ticket_delete_refuses_protected_pvcs():
    with pytest.raises(deploy.DeployError, match="protects persistent-volume-claims"):
        deploy.assert_inventory_not_protected(
            "train-ticket",
            "train-ticket",
            [{"kind": "PersistentVolumeClaim", "name": "tsdb-data"}],
        )
    deploy.assert_inventory_not_protected(
        "train-ticket",
        "train-ticket",
        [{"kind": "Deployment", "name": "gateway"}],
    )
    deploy.assert_inventory_not_protected(
        "train-ticket",
        "rb-train-ticket-test",
        [{"kind": "PersistentVolumeClaim", "name": "tsdb-data"}],
        temporary_owned=True,
    )


def test_dry_run_is_structured_and_does_not_call_runner():
    fake = FakeRunner()
    args = SimpleNamespace(
        application="sock-shop",
        mode="apply",
        namespace=None,
        kubeconfig=None,
        runtime_env_file=None,
        secret_source_namespace=None,
        fresh=True,
        execute=False,
        timeout=120,
    )

    report = deploy.execute(args, runner=fake, env={})

    assert report["schemaVersion"] == "resiliencebenchmark.application_deploy/v1"
    assert report["modeExecution"] == "dry-run"
    assert "delete namespace/sock-shop" in report["actions"]
    assert fake.calls == []


def test_sock_shop_runtime_map_is_complete_after_rendering():
    path = Path("environment/kubernetes/sock-shop/harbor-image-map.json")
    rendered = deploy.render_text(path.read_text(encoding="utf-8"), {"HARBOR_REGISTRY": "harbor.example:85"})
    image_map = json.loads(rendered)
    assert len(image_map) == 14
    assert all(value.startswith("harbor.example:85/sock-shop/") for value in image_map.values())
    assert all("@sha256:" in value for value in image_map.values())


def test_otel_bundle_keeps_application_owned_load_generator_active():
    assert deploy.intentionally_standby("otel-demo") == set()
    values = deploy.load_yaml(
        deploy.REPO_ROOT / "environment/kubernetes/otel-demo/values.yaml"
    )
    assert values["components"]["load-generator"]["replicas"] == 1


def test_train_ticket_secret_contract_distinguishes_helm_owned_secrets():
    contract = deploy.required_secret_contract("train-ticket")
    owners = {item["name"]: item.get("provisionedBy") for item in contract}
    assert owners["nacos-mysql"] == "helm:nacos"
    assert owners["nacosdb-mysql"] == "helm:nacosdb"
    assert owners["tsdb-mysql"] == "helm:tsdb"
    assert owners["harbor-secret"] is None


def test_post_install_patch_sets_native_entrypoint_before_waiting():
    fake = FakeRunner()
    deploy.apply_post_install_patch(
        fake,
        Path("/tmp/kubeconfig"),
        "rb-train-ticket-test",
        {
            "kind": "StatefulSet",
            "name": "tsdb-mysql",
            "containers": [
                {"name": "mysql", "command": ["/docker-entrypoint.sh"], "args": ["mysqld"]}
            ],
        },
        60,
    )
    patch_call = next(call for call in fake.calls if "patch" in call["argv"])
    payload = json.loads(patch_call["argv"][patch_call["argv"].index("-p") + 1])
    assert payload["spec"]["template"]["spec"]["containers"][0]["command"] == ["/docker-entrypoint.sh"]
    assert any("delete" in call["argv"] and "pods" in call["argv"] for call in fake.calls)
    assert any("rollout" in call["argv"] for call in fake.calls)
