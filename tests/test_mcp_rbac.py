from __future__ import annotations

from pathlib import Path

import yaml


RBAC_FILE = Path("environment/mcp/rbac.yaml")
APPLICATIONS = {"train-ticket", "sock-shop", "otel-demo"}


def load_items() -> list[dict]:
    payload = yaml.safe_load(RBAC_FILE.read_text(encoding="utf-8"))
    assert payload["apiVersion"] == "v1"
    assert payload["kind"] == "List"
    return payload["items"]


def test_rbac_objects_are_unique_and_service_accounts_do_not_automount_tokens():
    items = load_items()
    identities = [
        (item["kind"], item["metadata"].get("namespace", ""), item["metadata"]["name"])
        for item in items
    ]

    assert len(items) == 23
    assert len(identities) == len(set(identities))
    service_accounts = [item for item in items if item["kind"] == "ServiceAccount"]
    assert len(service_accounts) == 6
    assert all(item["automountServiceAccountToken"] is False for item in service_accounts)
    assert all(item["metadata"]["namespace"] == "resiliencebenchmark-system" for item in service_accounts)


def test_read_only_role_has_no_secret_exec_or_write_permissions():
    roles = {item["metadata"]["name"]: item for item in load_items() if item["kind"] == "ClusterRole"}
    read_role = roles["resbench-mcp-k8s-namespaced-read"]
    inventory_role = roles["resbench-mcp-k8s-inventory"]

    forbidden_resources = {"secrets", "serviceaccounts", "pods/exec", "roles", "rolebindings"}
    for role in (read_role, inventory_role):
        for rule in role["rules"]:
            assert not forbidden_resources.intersection(rule["resources"])
            assert set(rule["verbs"]) <= {"get", "list"}
            assert "*" not in rule["resources"]
            assert "*" not in rule["verbs"]


def test_application_service_accounts_are_bound_only_to_their_episode_namespace():
    bindings = [
        item
        for item in load_items()
        if item["kind"] == "RoleBinding" and item["metadata"]["name"] == "resbench-mcp-k8s-ro"
    ]

    assert {item["metadata"]["namespace"] for item in bindings} == APPLICATIONS
    for item in bindings:
        namespace = item["metadata"]["namespace"]
        assert item["roleRef"]["name"] == "resbench-mcp-k8s-namespaced-read"
        assert item["subjects"] == [
            {
                "kind": "ServiceAccount",
                "name": f"resbench-k8s-ro-{namespace}",
                "namespace": "resiliencebenchmark-system",
            }
        ]


def test_chaos_global_role_is_narrow_and_separate_from_read_only_identity():
    items = load_items()
    chaos_role = next(
        item
        for item in items
        if item["kind"] == "ClusterRole" and item["metadata"]["name"] == "resbench-mcp-chaos-global-control"
    )
    assert chaos_role["rules"] == [
        {
            "apiGroups": ["chaosblade.io"],
            "resources": ["chaosblades"],
            "verbs": ["get", "list", "create", "delete"],
        }
    ]

    chaos_bindings = [
        item
        for item in items
        if item["kind"] == "ClusterRoleBinding"
        and item["roleRef"]["name"] == "resbench-mcp-chaos-global-control"
    ]
    assert len(chaos_bindings) == 3
    assert all(subject["name"].startswith("resbench-chaos-control-") for item in chaos_bindings for subject in item["subjects"])
