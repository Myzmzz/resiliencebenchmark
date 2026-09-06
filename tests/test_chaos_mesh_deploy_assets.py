from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1] / "deploy/chaos-mesh"


def test_mesh_bootstrap_rbac_does_not_grant_fault_writes():
    documents = list(yaml.safe_load_all((ROOT / "controller-bootstrap-rbac.yaml").read_text()))
    cluster_role = next(d for d in documents if d["kind"] == "ClusterRole")
    assert cluster_role["rules"] == [{
        "apiGroups": ["chaos-mesh.org"],
        "resources": ["remoteclusters"],
        "verbs": ["get", "list", "watch"],
    }]
    role = next(d for d in documents if d["kind"] == "Role")
    assert role["metadata"]["namespace"] == "chaos-mesh"
    assert role["rules"] == [{"apiGroups": [""], "resources": ["events"], "verbs": ["create", "patch"]}]
    for binding in (d for d in documents if d["kind"].endswith("Binding")):
        assert binding["subjects"] == [{"kind": "ServiceAccount", "name": "chaos-controller-manager", "namespace": "chaos-mesh"}]


def test_old_cluster_mesh_values_keep_namespace_and_tool_scope():
    values = yaml.safe_load((ROOT / "values-old-cluster.yaml").read_text())
    assert values["clusterScoped"] is False
    assert values["controllerManager"]["targetNamespace"] == "otel-demo"
    assert values["chaosDaemon"]["runtime"] == "docker"
    assert values["chaosDaemon"]["socketPath"] == "/var/run/docker.sock"
    assert values["webhook"]["CRDS"] == ["networkchaos", "podchaos", "stresschaos"]
    assert values["dashboard"]["create"] is False
    assert values["dnsServer"]["create"] is False
    assert values["chaosDaemon"]["mtls"]["enabled"] is True
    assert yaml.safe_load((ROOT / "namespace.yaml").read_text())["metadata"]["name"] == "chaos-mesh"
