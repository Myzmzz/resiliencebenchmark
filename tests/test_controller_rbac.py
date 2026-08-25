from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]


def _items() -> list[dict]:
    document = yaml.safe_load((REPO_ROOT / "environment/mcp/rbac.yaml").read_text())
    return document["items"]


def test_controller_identity_is_otel_scoped_and_cannot_write_chaos_or_secrets() -> None:
    items = _items()
    service_account = next(
        item
        for item in items
        if item.get("kind") == "ServiceAccount"
        and item.get("metadata", {}).get("name") == "resbench-controller-otel-demo"
    )
    assert service_account["metadata"]["namespace"] == "resiliencebenchmark-system"
    assert service_account["automountServiceAccountToken"] is False

    controller_roles = [
        item
        for item in items
        if item.get("kind") in {"Role", "ClusterRole"}
        and str(item.get("metadata", {}).get("name", "")).startswith(
            "resbench-controller-"
        )
    ]
    assert {item["metadata"].get("namespace") for item in controller_roles if item["kind"] == "Role"} == {
        "otel-demo",
        "observability",
    }
    for role in controller_roles:
        for rule in role.get("rules", []):
            resources = set(rule.get("resources", []))
            verbs = set(rule.get("verbs", []))
            assert "secrets" not in resources
            assert "pods/exec" not in resources
            if "chaosblades" in resources:
                assert verbs <= {"get", "list"}


def test_controller_pod_delete_is_namespaced_but_no_deployment_patch_is_granted() -> None:
    role = next(
        item
        for item in _items()
        if item.get("kind") == "Role"
        and item.get("metadata", {}).get("name") == "resbench-controller-runtime"
    )
    pod_rule = next(rule for rule in role["rules"] if rule["resources"] == ["pods"])
    app_rule = next(rule for rule in role["rules"] if "deployments" in rule["resources"])
    assert "delete" in pod_rule["verbs"]
    assert set(app_rule["verbs"]) == {"get", "list", "watch"}
