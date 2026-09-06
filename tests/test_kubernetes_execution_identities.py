"""Identity-file and manifest checks; live API authorization is a separate gate."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from stage2_service.kubernetes_identities import (
    CONTROLLER_SERVICE_ACCOUNT, EXECUTOR_SERVICE_ACCOUNT, FINALIZER_SERVICE_ACCOUNT,
    KubernetesIdentityError, prepare_execution_identities,
)


ROOT = Path(__file__).resolve().parents[1]
CONTROL_NS = "resiliencebenchmark-system"


def controller_config(tmp_path):
    token = tmp_path / "projected-token"
    token.write_text("unit-test-secret-v1")
    payload = {
        "apiVersion": "v1", "kind": "Config",
        "clusters": [{"name": "cluster", "cluster": {"server": "https://127.0.0.1:6443", "certificate-authority-data": "Y2E="}}],
        "users": [{"name": "controller", "user": {"tokenFile": str(token)}}],
        "contexts": [{"name": "controller", "context": {"cluster": "cluster", "user": "controller", "namespace": "otel-demo"}}],
        "current-context": "controller",
    }
    source = tmp_path / "controller.kubeconfig"
    source.write_text(yaml.safe_dump(payload))
    source.chmod(0o600)
    return source, token, payload


def test_identity_files_have_fixed_users_and_do_not_copy_rotating_tokens(tmp_path):
    source, token, original = controller_config(tmp_path)
    identities = prepare_execution_identities(source, tmp_path / "identities", control_namespace=CONTROL_NS)
    for path, name in ((identities.executor_kubeconfig, EXECUTOR_SERVICE_ACCOUNT),
                       (identities.finalizer_kubeconfig, FINALIZER_SERVICE_ACCOUNT)):
        contents = path.read_text()
        data = yaml.safe_load(contents)
        assert data["users"][0]["user"] == {"tokenFile": str(token), "as": f"system:serviceaccount:{CONTROL_NS}:{name}"}
        assert "unit-test-secret" not in contents
        assert path.stat().st_mode & 0o777 == 0o600
    token.write_text("unit-test-secret-v2")
    assert "unit-test-secret" not in identities.executor_kubeconfig.read_text()
    assert yaml.safe_load(source.read_text()) == original
    assert prepare_execution_identities(source, tmp_path / "identities", control_namespace=CONTROL_NS) == identities


@pytest.mark.parametrize("auth", [{"token": "inline-token"}, {"tokenFile": "/missing", "as": "admin"}, {"exec": {"command": "sh"}}])
def test_delegation_rejects_inline_credentials_plugins_or_an_already_impersonated_source(tmp_path, auth):
    source, _token, payload = controller_config(tmp_path)
    payload["users"][0]["user"] = auth
    source.write_text(yaml.safe_dump(payload))
    with pytest.raises(KubernetesIdentityError):
        prepare_execution_identities(source, tmp_path / "identities", control_namespace=CONTROL_NS)


def test_delegation_rejects_unsafe_files_and_arbitrary_namespace(tmp_path):
    source, _token, _payload = controller_config(tmp_path)
    source.chmod(0o644)
    with pytest.raises(KubernetesIdentityError, match="private"):
        prepare_execution_identities(source, tmp_path / "identities", control_namespace=CONTROL_NS)
    source.chmod(0o600)
    with pytest.raises(KubernetesIdentityError, match="namespace"):
        prepare_execution_identities(source, tmp_path / "identities", control_namespace="../admin")
    output = tmp_path / "identities"
    output.mkdir()
    foreign = tmp_path / "unrelated"
    foreign.write_text("preserve")
    (output / "executor.kubeconfig").symlink_to(foreign)
    with pytest.raises(KubernetesIdentityError):
        prepare_execution_identities(source, output, control_namespace=CONTROL_NS)
    assert foreign.read_text() == "preserve"


def _documents():
    return [doc for path in (ROOT / "deploy/stage2/stage2.yaml", ROOT / "deploy/stage2/execution-identities.yaml")
            for doc in yaml.safe_load_all(path.read_text()) if doc]


def _allows(account, group, resource, verb, namespace=None, name=None):
    documents = _documents()
    roles = {(doc["kind"], doc["metadata"].get("namespace"), doc["metadata"]["name"]): doc
             for doc in documents if doc["kind"] in {"Role", "ClusterRole"}}
    for binding in documents:
        if binding["kind"] not in {"RoleBinding", "ClusterRoleBinding"}:
            continue
        if not any(subject.get("kind") == "ServiceAccount" and subject.get("name") == account
                   and subject.get("namespace") == CONTROL_NS for subject in binding.get("subjects", [])):
            continue
        binding_ns = binding["metadata"].get("namespace")
        if binding["kind"] == "RoleBinding" and binding_ns != namespace:
            continue
        ref = binding["roleRef"]
        role = roles[(ref["kind"], binding_ns if ref["kind"] == "Role" else None, ref["name"])]
        for rule in role["rules"]:
            if all(value in rule[key] or "*" in rule[key] for key, value in
                   (("apiGroups", group), ("resources", resource), ("verbs", verb))):
                if not rule.get("resourceNames") or name in rule["resourceNames"]:
                    return True
    return False


@pytest.mark.parametrize("group,resource,namespace", [
    ("chaosblade.io", "chaosblades", None),
    ("chaos-mesh.org", "networkchaos", "otel-demo"),
    ("chaos-mesh.org", "podchaos", "otel-demo"),
    ("chaos-mesh.org", "stresschaos", "otel-demo"),
])
def test_manifest_separates_create_and_cleanup_rights(group, resource, namespace):
    assert _allows(EXECUTOR_SERVICE_ACCOUNT, group, resource, "create", namespace)
    assert not _allows(EXECUTOR_SERVICE_ACCOUNT, group, resource, "delete", namespace)
    assert _allows(FINALIZER_SERVICE_ACCOUNT, group, resource, "delete", namespace)
    assert not _allows(FINALIZER_SERVICE_ACCOUNT, group, resource, "create", namespace)
    assert not _allows(CONTROLLER_SERVICE_ACCOUNT, group, resource, "create", namespace)
    assert not _allows(CONTROLLER_SERVICE_ACCOUNT, group, resource, "delete", namespace)
    assert not _allows("resbench-stage2-agent", group, resource, "create", namespace)


def test_manifest_delegation_has_only_two_named_accounts_and_no_agent_tokens():
    for account in (EXECUTOR_SERVICE_ACCOUNT, FINALIZER_SERVICE_ACCOUNT):
        assert _allows(CONTROLLER_SERVICE_ACCOUNT, "", "serviceaccounts", "impersonate", CONTROL_NS, account)
        assert not _allows(account, "chaos-mesh.org", "networkchaos", "create", "kube-system")
    assert not _allows(CONTROLLER_SERVICE_ACCOUNT, "", "serviceaccounts", "impersonate", CONTROL_NS, "admin")
    accounts = [doc for doc in _documents() if doc["kind"] == "ServiceAccount"]
    assert all(doc["automountServiceAccountToken"] is False for doc in accounts)
    assert not any(doc["kind"] == "Secret" for doc in _documents())
    for filename in ("stage2.yaml", "stage2-integration.yaml", "stage2-matrix-job.yaml"):
        docs = yaml.safe_load_all((ROOT / "deploy/stage2" / filename).read_text())
        for doc in docs:
            if doc and doc["kind"] in {"Deployment", "Job"}:
                assert doc["spec"]["template"]["spec"]["serviceAccountName"] == CONTROLLER_SERVICE_ACCOUNT


def test_identity_probe_uses_readonly_reviews_and_does_not_count_transport_failure_as_denial():
    import json
    import subprocess
    from scripts.qualify_execution_identities import qualify_identities

    calls = []
    accounts = {"controller": CONTROLLER_SERVICE_ACCOUNT, "executor": EXECUTOR_SERVICE_ACCOUNT, "finalizer": FINALIZER_SERVICE_ACCOUNT}

    def run(argv, **kwargs):
        calls.append(argv)
        assert argv[3] == "auth"
        role = Path(argv[2]).name
        if argv[4] == "whoami":
            return subprocess.CompletedProcess(argv, 0, json.dumps({"status": {"userInfo": {"username": f"system:serviceaccount:{CONTROL_NS}:{accounts[role]}"}}}), "")
        verb, resource = argv[5:7]
        namespace = argv[8] if "--namespace" in argv else None
        group = "chaos-mesh.org" if resource.endswith("chaos-mesh.org") else "chaosblade.io" if resource.endswith("chaosblade.io") else ""
        res = resource.split(".", 1)[0].split("/", 1)[0]
        name = resource.split("/", 1)[1] if "/" in resource else None
        allowed = _allows(accounts[role], group, res, verb, namespace, name)
        return subprocess.CompletedProcess(argv, 0 if allowed else 1, "yes\n" if allowed else "no\n", "")

    configs = {role: Path("/fake") / role for role in accounts}
    result = qualify_identities(configs, control_namespace=CONTROL_NS, runner=run)
    assert result["status"] == "passed"
    assert result["fault_mutations_performed"] is False

    def disconnected(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, "", "connection refused")

    failed = qualify_identities(configs, control_namespace=CONTROL_NS, runner=disconnected)
    assert failed["status"] == "failed"
    assert all(row["observed"] is None and not row["passed"] for row in failed["checks"])
