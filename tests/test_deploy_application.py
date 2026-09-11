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


# Helm --output json release of a server dry run: two chart objects, one
# cluster-scoped object, a test hook (never run on install) and an install hook.
HELM_DRY_RUN_RELEASE = {
    "name": "otel-demo",
    "namespace": "otel-demo",
    "manifest": (
        "---\n# Source: opentelemetry-demo/templates/component.yaml\n"
        "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: cart\n"
        "  labels:\n    app.kubernetes.io/name: cart\n"
        "---\n# Source: opentelemetry-demo/templates/component.yaml\n"
        "apiVersion: v1\nkind: Service\nmetadata:\n  name: frontend-proxy\nspec:\n  type: NodePort\n"
        "---\n# Source: opentelemetry-demo/charts/opentelemetry-collector/templates/clusterrole.yaml\n"
        "apiVersion: rbac.authorization.k8s.io/v1\nkind: ClusterRole\nmetadata:\n  name: otel-collector\n"
    ),
    "hooks": [
        {
            "name": "grafana-test",
            "kind": "Pod",
            "events": ["test"],
            "manifest": "apiVersion: v1\nkind: Pod\nmetadata:\n  name: grafana-test\n",
        },
        {
            "name": "schema-migrate",
            "kind": "Job",
            "events": ["post-install", "post-upgrade"],
            "manifest": "apiVersion: batch/v1\nkind: Job\nmetadata:\n  name: schema-migrate\n",
        },
    ],
}
FORBIDDEN_NAMESPACE_PATCH = (
    'Error from server (Forbidden): namespaces "otel-demo" is forbidden: User '
    '"system:serviceaccount:resiliencebenchmark-system:resbench-stage2-controller" '
    'cannot patch resource "namespaces" in API group "" in the namespace "otel-demo"'
)
CLUSTER_SCOPED_KINDS = {"ClusterRole", "ClusterRoleBinding", "Namespace"}
KUBECTL_WRITE_VERBS = {"apply", "create", "annotate", "label", "patch", "scale", "delete"}


class OtelClusterRunner:
    """Answers the OTel Demo apply commands like a cluster that allows them.

    ``fail_when(argv, stdin)`` selects the command that returns ``failure``.
    """

    def __init__(self, fail_when=None, failure: deploy.CommandResult | None = None):
        self.calls: list[dict] = []
        self.fail_when = fail_when
        self.failure = failure or deploy.CommandResult(1, "", FORBIDDEN_NAMESPACE_PATCH)

    def run(self, argv: list[str], *, stdin: str | None = None, timeout: int = 300):
        self.calls.append({"argv": list(argv), "stdin": stdin, "timeout": timeout})
        if self.fail_when is not None and self.fail_when(argv, stdin):
            return self.failure
        if argv[:2] == ["helm", "upgrade"]:
            dry_run = deploy.SERVER_DRY_RUN in argv
            return deploy.CommandResult(0, json.dumps(HELM_DRY_RUN_RELEASE) if dry_run else "STATUS: deployed\n", "")
        if "get" in argv and "namespace" in argv:
            return deploy.CommandResult(0, "namespace/otel-demo\n", "")
        if "get" in argv and "deployments,statefulsets" in argv:
            return deploy.CommandResult(0, json.dumps({"items": []}), "")
        if "apply" in argv and "json" in argv:
            # Like the API server: namespaced objects come back with their namespace.
            items = yaml.safe_load(stdin)["items"]
            for item in items:
                if item["kind"] not in CLUSTER_SCOPED_KINDS:
                    item["metadata"]["namespace"] = "otel-demo"
            return deploy.CommandResult(0, json.dumps({"apiVersion": "v1", "kind": "List", "items": items}), "")
        return deploy.CommandResult(0, "", "")


def _otel_apply_args(tmp_path: Path, **overrides) -> SimpleNamespace:
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("apiVersion: v1\n", encoding="utf-8")
    runtime_env = tmp_path / "otel-demo.env"
    runtime_env.write_text(
        "HARBOR_REGISTRY=harbor.example:85\n"
        "OTEL_DEMO_POSTGRES_PASSWORD=postgres-fixture\n"
        "OTEL_DEMO_OPENAI_API_KEY=openai-fixture\n"
        "OTEL_DEMO_GRAFANA_ADMIN_PASSWORD=grafana-fixture\n",
        encoding="utf-8",
    )
    runtime_env.chmod(0o600)
    values = {
        "application": "otel-demo",
        "mode": "apply",
        "namespace": None,
        "kubeconfig": kubeconfig,
        "runtime_env_file": runtime_env,
        "secret_source_namespace": None,
        "fresh": False,
        "execute": False,
        "server_dry_run": True,
        "timeout": 120,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _otel_chart_env(tmp_path: Path) -> dict[str, str]:
    chart = tmp_path / "opentelemetry-demo-0.40.5.tgz"
    chart.write_bytes(b"pinned chart fixture")
    return {"OTEL_DEMO_CHART_FILE": str(chart)}


def test_otel_server_dry_run_is_the_reinstall_with_every_write_dry_run(tmp_path):
    env = _otel_chart_env(tmp_path)
    dry = OtelClusterRunner()
    report = deploy.execute(_otel_apply_args(tmp_path), runner=dry, env=env)
    real = OtelClusterRunner()
    deploy.execute(_otel_apply_args(tmp_path, execute=True, server_dry_run=False), runner=real, env=env)

    # Same Helm invocation (chart, values, namespace, flags) plus the dry run.
    helm_dry = [call for call in dry.calls if call["argv"][0] == "helm"]
    helm_real = [call for call in real.calls if call["argv"][0] == "helm"]
    assert len(helm_dry) == len(helm_real) == 1
    assert helm_real[0]["argv"] == [
        "helm", "upgrade", "--install", "otel-demo", str(Path(env["OTEL_DEMO_CHART_FILE"]).resolve()),
        "--namespace", "otel-demo", "--create-namespace", "--values", "-", "--timeout", "120s",
        "--wait", "--version", "0.40.5", "--server-side=true", "--force-conflicts",
    ]
    assert helm_dry[0]["argv"] == helm_real[0]["argv"] + ["--dry-run=server", "--output", "json"]
    assert helm_dry[0]["stdin"] == helm_real[0]["stdin"]
    # Every kubectl write is a server-side dry run; nothing waits or deletes.
    for call in dry.calls:
        argv = call["argv"]
        if argv[0] == "kubectl" and "can-i" not in argv and KUBECTL_WRITE_VERBS & set(argv):
            assert "--dry-run=server" in argv, argv
        assert "rollout" not in argv and "delete" not in argv, argv
    # The namespace server-side apply that --create-namespace performs (the incident's failing call).
    namespace_call = next(
        call for call in dry.calls
        if call["argv"][0] == "kubectl" and yaml.safe_load(call["stdin"] or "{}").get("kind") == "Namespace"
    )
    assert yaml.safe_load(namespace_call["stdin"])["metadata"] == {"name": "otel-demo", "labels": {"name": "otel-demo"}}
    assert {"--server-side", "--field-manager=helm", "--dry-run=server"} <= set(namespace_call["argv"])
    assert "--force-conflicts" not in namespace_call["argv"]
    # Every rendered object plus install hooks, with Helm's ownership metadata.
    objects_call = next(call for call in dry.calls if call["argv"][0] == "kubectl" and "--force-conflicts" in call["argv"])
    assert {"--server-side", "--field-manager=helm", "--dry-run=server", "-n", "otel-demo"} <= set(objects_call["argv"])
    items = yaml.safe_load(objects_call["stdin"])["items"]
    assert [item["metadata"]["name"] for item in items] == ["cart", "frontend-proxy", "otel-collector", "schema-migrate"]
    for item in items:
        assert item["metadata"]["labels"]["app.kubernetes.io/managed-by"] == "Helm"
        assert item["metadata"]["annotations"]["meta.helm.sh/release-name"] == "otel-demo"
        assert item["metadata"]["annotations"]["meta.helm.sh/release-namespace"] == "otel-demo"
    # Create permission for every object type the reinstall re-creates.
    can_i = sorted(call["argv"][call["argv"].index("can-i") + 1:] for call in dry.calls if "can-i" in call["argv"])
    assert can_i == sorted(
        [
            ["create", "clusterrole.rbac.authorization.k8s.io", "--all-namespaces", "--quiet"],
            ["create", "deployment.apps", "-n", "otel-demo", "--quiet"],
            ["create", "job.batch", "-n", "otel-demo", "--quiet"],
            ["create", "service", "-n", "otel-demo", "--quiet"],
        ]
    )
    # The supplemental manifest is the same client-side apply, dry-run.
    supplemental = [
        next(
            call for call in calls
            if call["argv"][0] == "kubectl" and "--server-side" not in call["argv"]
            and "kind: PersistentVolumeClaim" in (call["stdin"] or "")
        )
        for calls in (dry.calls, real.calls)
    ]
    assert supplemental[0]["argv"][-4:] == ["apply", "--dry-run=server", "-f", "-"]
    assert supplemental[1]["argv"][-3:] == ["apply", "-f", "-"]
    assert supplemental[0]["stdin"] == supplemental[1]["stdin"]
    assert report["modeExecution"] == "server-dry-run"
    assert report["result"] == "server-dry-run-passed"
    assert len(report["serverDryRun"]["checks"]) == 5
    assert report["serverDryRun"]["notSimulated"] == list(deploy.SERVER_DRY_RUN_GAPS)
    assert "postgres-fixture" not in json.dumps(report)


def test_otel_server_dry_run_fails_on_the_forbidden_namespace_patch(tmp_path):
    runner = OtelClusterRunner(
        fail_when=lambda argv, stdin: "--server-side" in argv and "kind: Namespace" in (stdin or "")
    )

    with pytest.raises(deploy.DeployError, match='cannot patch resource "namespaces"'):
        deploy.execute(_otel_apply_args(tmp_path), runner=runner, env=_otel_chart_env(tmp_path))

    assert not any(call["argv"][0] == "kubectl" and "--force-conflicts" in call["argv"] for call in runner.calls)
    assert not any("can-i" in call["argv"] for call in runner.calls)


def test_otel_server_dry_run_requires_create_permission_for_recreated_objects(tmp_path):
    runner = OtelClusterRunner(
        fail_when=lambda argv, _stdin: "can-i" in argv and "clusterrole.rbac.authorization.k8s.io" in argv,
        failure=deploy.CommandResult(1, "", ""),
    )

    with pytest.raises(deploy.DeployError, match="cannot create clusterrole.rbac.authorization.k8s.io in cluster scope"):
        deploy.execute(_otel_apply_args(tmp_path), runner=runner, env=_otel_chart_env(tmp_path))


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"execute": True}, "mutually exclusive"),
        ({"fresh": True}, "supports only"),
        ({"application": "sock-shop"}, "supports only"),
        ({"mode": "activate"}, "supports only"),
        ({"namespace": "rb-otel-demo-copy"}, "supports only"),
        ({"kubeconfig": None}, "requires --kubeconfig"),
    ],
)
def test_server_dry_run_rejects_combinations_it_does_not_mirror(tmp_path, overrides, message):
    runner = OtelClusterRunner()

    with pytest.raises(deploy.DeployError, match=message):
        deploy.execute(_otel_apply_args(tmp_path, **overrides), runner=runner, env=_otel_chart_env(tmp_path))

    assert runner.calls == []


def test_cli_parses_server_dry_run_separately_from_execute():
    args = deploy.build_parser().parse_args(
        ["--application", "otel-demo", "--mode", "apply", "--server-dry-run", "--kubeconfig", "/k"]
    )

    assert args.server_dry_run is True
    assert args.execute is False


def test_helm_release_objects_fail_closed_on_unparsable_output():
    with pytest.raises(deploy.DeployError, match="release JSON"):
        deploy.helm_release_objects("NAME: otel-demo\nSTATUS: pending-upgrade\n", release="otel-demo", namespace="otel-demo")
