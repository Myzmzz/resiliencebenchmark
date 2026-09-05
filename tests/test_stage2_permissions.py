from __future__ import annotations

from pathlib import Path

import pytest
from kubernetes.client.rest import ApiException

from stage2_service.contracts import (
    BladeAINativePermissions,
    HarnessKind,
    McpServerName,
    PermissionProfile,
)
from stage2_service.permissions import NullPermissionBackend, Stage2PermissionManager
from stage2_service.kubernetes_permissions import KubernetesPermissionBackend
from stage2_service.permission_profiles import load_permission_profiles
from stage2_service.runtime_adapters import McpTokenStateRegistry


class MainFault:
    fault_type = "network-delay"


class Internal:
    main_fault = MainFault()


class Episode:
    internal = Internal()


class Runtime:
    pass


class FailingCleanupPermissionBackend(NullPermissionBackend):
    def cleanup_trial(self, trial_id: str):
        raise RuntimeError(f"cleanup failed for {trial_id}")


def test_permission_manager_gives_only_bladeai_a_direct_read_kubeconfig_profile(tmp_path: Path):
    manager = Stage2PermissionManager(
        private_root=tmp_path / "private",
        token_registry=McpTokenStateRegistry(tmp_path / "tokens"),
        permission_backend=NullPermissionBackend(),
    )
    campaign = "campaign-1234567890abcdef"
    codex_trial = f"{campaign}-codex-t1"
    blade_trial = f"{campaign}-bladeai-t1"

    codex = manager.provision(
        campaign,
        codex_trial,
        HarnessKind.CODEX,
        Episode(),
        Runtime(),
        PermissionProfile(),
    )
    blade = manager.provision(
        campaign,
        blade_trial,
        HarnessKind.BLADEAI,
        Episode(),
        Runtime(),
        PermissionProfile(),
    )

    assert codex.direct_kubeconfig is False
    assert blade.direct_kubeconfig is True
    assert not any(rule.api_group == "metrics.k8s.io" for rule in codex.kubernetes_rules)
    assert any(rule.api_group == "metrics.k8s.io" for rule in blade.kubernetes_rules)
    assert manager.runtime_context(codex_trial)["mcp_token"]
    assert manager.restore(codex_trial)["verified"] is True
    assert manager.restore(blade_trial)["verified"] is True


def test_permission_profile_selects_mcp_servers_and_bladeai_native_rules(tmp_path: Path):
    manager = Stage2PermissionManager(
        private_root=tmp_path / "private",
        token_registry=McpTokenStateRegistry(tmp_path / "tokens"),
        permission_backend=NullPermissionBackend(),
    )
    trial_id = "campaign-1234567890abcdef-bladeai-static"
    profile = PermissionProfile(
        profile_id="p1-no-telemetry-mcp",
        mcp_servers=(McpServerName.K8S_RO, McpServerName.SOURCE_RO),
        bladeai_native=BladeAINativePermissions(
            kubernetes_read=True,
            kubernetes_metrics=False,
            chaosblade_execute=False,
        ),
    )

    capability = manager.provision(
        "campaign-1234567890abcdef",
        trial_id,
        HarnessKind.BLADEAI,
        Episode(),
        Runtime(),
        profile,
    )
    runtime = manager.runtime_context(trial_id)

    assert capability.permission_profile_id == profile.profile_id
    assert capability.mcp_servers == (
        McpServerName.K8S_RO,
        McpServerName.SOURCE_RO,
    )
    assert "telemetry_prom_metric_range" not in capability.mcp_tools
    assert set(runtime["mcp_token_state_files"]) == {"k8s_ro", "source_ro"}
    assert capability.native_permissions == profile.bladeai_native
    assert not any(
        rule.api_group == "metrics.k8s.io" for rule in capability.kubernetes_rules
    )
    assert not any(
        rule.api_group == "chaosblade.io" for rule in capability.kubernetes_rules
    )
    restored = manager.restore(trial_id)
    assert restored["verified"] is True
    assert restored["mcp_tokens"]["removed_servers"] == ["k8s_ro", "source_ro"]
    assert not (tmp_path / "tokens" / trial_id).exists()


def test_all_static_profiles_compile_for_all_four_harnesses(tmp_path: Path):
    manager = Stage2PermissionManager(
        private_root=tmp_path / "private",
        token_registry=McpTokenStateRegistry(tmp_path / "tokens"),
        permission_backend=NullPermissionBackend(),
    )
    profiles = load_permission_profiles()

    for profile_index, profile in enumerate(profiles):
        for harness_index, harness in enumerate(HarnessKind):
            trial_id = (
                f"campaign-1234567890abcdef-{profile_index}-{harness_index}-{harness.value}"
            )
            capability = manager.provision(
                "campaign-1234567890abcdef",
                trial_id,
                harness,
                Episode(),
                Runtime(),
                profile,
            )
            expected = tuple(profile.mcp_servers)
            if harness is HarnessKind.BLADEAI:
                expected = tuple(
                    server
                    for server in expected
                    if server is not McpServerName.CHAOS_CONTROL
                )
                assert capability.native_permissions == profile.bladeai_native
            else:
                assert capability.native_permissions == BladeAINativePermissions(
                    kubernetes_read=False,
                    kubernetes_metrics=False,
                    chaosblade_execute=False,
                )
            assert capability.mcp_servers == expected
            assert set(manager.runtime_context(trial_id)["mcp_token_state_files"]) == {
                server.value for server in expected
            }
            assert manager.restore(trial_id)["verified"] is True


def test_permission_cleanup_continues_when_bladeai_rbac_cleanup_fails(tmp_path: Path):
    manager = Stage2PermissionManager(
        private_root=tmp_path / "private",
        token_registry=McpTokenStateRegistry(tmp_path / "tokens"),
        permission_backend=FailingCleanupPermissionBackend(),
    )
    trial_id = "campaign-1234567890abcdef-bladeai-cleanup"
    manager.provision(
        "campaign-1234567890abcdef",
        trial_id,
        HarnessKind.BLADEAI,
        Episode(),
        Runtime(),
        PermissionProfile(),
    )
    runtime = manager.runtime_context(trial_id)
    kubeconfig = Path(runtime["bladeai_kubeconfig"])
    assert kubeconfig.is_file()

    cleanup = manager.restore(trial_id)

    assert cleanup["verified"] is False
    assert cleanup["backend"]["error_type"] == "RuntimeError"
    assert cleanup["mcp_tokens"]["verified"] is True
    assert cleanup["kubeconfig"]["verified"] is True
    assert not kubeconfig.exists()
    assert not (tmp_path / "tokens" / trial_id).exists()
    with pytest.raises(RuntimeError, match="permission runtime is missing"):
        manager.runtime_context(trial_id)


class RbacCapture:
    def create_namespaced_role_binding(self, *, namespace, body):
        self.namespace = namespace
        self.namespaced_body = body

    def create_cluster_role_binding(self, *, body):
        self.cluster_body = body


def test_bladeai_bindings_use_kubernetes_34_rbac_subject_model():
    rbac = RbacCapture()
    backend = KubernetesPermissionBackend(object(), rbac, object())

    backend._create_or_replace_binding("read", "role", "agent", {})
    backend._create_or_replace_cluster_binding("chaos", "cluster-role", "agent", {})

    assert type(rbac.namespaced_body.subjects[0]).__name__ == "RbacV1Subject"
    assert type(rbac.cluster_body.subjects[0]).__name__ == "RbacV1Subject"


def test_bladeai_static_native_profile_provisions_only_selected_bindings(monkeypatch):
    backend = KubernetesPermissionBackend(object(), object(), object())
    calls = []
    monkeypatch.setattr(
        backend, "_create_or_replace_sa", lambda name, labels: calls.append(("sa", name))
    )
    monkeypatch.setattr(
        backend,
        "_create_or_replace_role",
        lambda name, rules, labels: calls.append(("role", name)),
    )
    monkeypatch.setattr(
        backend,
        "_create_or_replace_binding",
        lambda name, role, service_account, labels: calls.append(("binding", name)),
    )
    monkeypatch.setattr(
        backend,
        "_create_or_replace_cluster_binding",
        lambda name, role, service_account, labels: calls.append(("cluster", name)),
    )
    monkeypatch.setattr(
        backend,
        "_delete_namespaced_binding",
        lambda name: calls.append(("delete_binding", name)),
    )
    monkeypatch.setattr(
        backend,
        "_delete_namespaced_role",
        lambda name: calls.append(("delete_role", name)),
    )
    monkeypatch.setattr(
        backend,
        "_delete_cluster_binding",
        lambda name: calls.append(("delete_cluster", name)),
    )

    result = backend.provision_bladeai(
        "campaign-1234567890abcdef-bladeai-static",
        BladeAINativePermissions(
            kubernetes_read=True,
            kubernetes_metrics=False,
            chaosblade_execute=False,
        ),
    )

    assert [kind for kind, _name in calls] == [
        "sa",
        "role",
        "binding",
        "delete_binding",
        "delete_role",
        "delete_cluster",
    ]
    assert not any(
        kind in {"role", "binding", "cluster"} and "metrics" in name
        for kind, name in calls
    )
    assert result["permissions"] == {
        "kubernetes_read": True,
        "kubernetes_metrics": False,
        "chaosblade_execute": False,
    }


def test_kubernetes_34_token_request_is_owned_by_core_v1_api():
    from kubernetes import client

    assert hasattr(client.CoreV1Api, "create_namespaced_service_account_token")
    assert not hasattr(
        client.AuthenticationV1Api, "create_namespaced_service_account_token"
    )
    assert (
        KubernetesPermissionBackend.TOKEN_AUDIENCE
        == "https://kubernetes.default.svc.cluster.local"
    )


class RbacLifecycleCapture:
    def __init__(self, *, fail_namespaced_delete: str | None = None):
        self.fail_namespaced_delete = fail_namespaced_delete
        self.deleted_namespaced: list[tuple[str, str]] = []
        self.deleted_cluster: list[str] = []
        self.created_namespaced = []
        self.created_cluster = []

    def delete_namespaced_role_binding(self, *, name, namespace):
        if name == self.fail_namespaced_delete:
            raise RuntimeError("injected namespaced delete failure")
        self.deleted_namespaced.append((namespace, name))

    def delete_cluster_role_binding(self, *, name):
        self.deleted_cluster.append(name)

    def create_namespaced_role_binding(self, *, namespace, body):
        self.created_namespaced.append((namespace, body))

    def create_cluster_role_binding(self, *, body):
        self.created_cluster.append(body)


def test_bladeai_read_metrics_and_chaos_permissions_are_independently_revocable():
    trial_id = "campaign-1234567890abcdef-bladeai-t2"
    state = KubernetesPermissionBackend._state(trial_id)
    rbac = RbacLifecycleCapture()
    backend = KubernetesPermissionBackend(object(), rbac, object())

    assert backend.revoke_read(trial_id)["capability"] == "k8s.read"
    assert backend.revoke_metrics(trial_id)["capability"] == "metrics.k8s.io"
    assert backend.revoke_chaos(trial_id)["capability"] == "native.blade.create"

    assert rbac.deleted_namespaced == [
        (backend.APPLICATION_NAMESPACE, state["read_binding"]),
        (backend.APPLICATION_NAMESPACE, state["metrics_binding"]),
    ]
    assert rbac.deleted_cluster == [state["chaos_binding"]]

    backend.restore_read(trial_id)
    backend.restore_metrics(trial_id)
    backend.restore_chaos(trial_id)

    restored_namespaced = [body for _, body in rbac.created_namespaced]
    assert [body.metadata.name for body in restored_namespaced] == [
        state["read_binding"],
        state["metrics_binding"],
    ]
    assert rbac.created_cluster[0].metadata.name == state["chaos_binding"]
    assert rbac.created_cluster[0].role_ref.name == backend.CHAOS_CLUSTER_ROLE
    assert rbac.created_cluster[0].subjects[0].name == state["service_account"]


def test_bladeai_observability_revoke_restores_read_when_metrics_revoke_fails():
    trial_id = "campaign-1234567890abcdef-bladeai-t3"
    state = KubernetesPermissionBackend._state(trial_id)
    rbac = RbacLifecycleCapture(
        fail_namespaced_delete=state["metrics_binding"]
    )
    backend = KubernetesPermissionBackend(object(), rbac, object())

    with pytest.raises(RuntimeError, match="injected namespaced delete failure"):
        backend.revoke_observability(trial_id)

    assert rbac.deleted_namespaced == [
        (backend.APPLICATION_NAMESPACE, state["read_binding"])
    ]
    assert [body.metadata.name for _, body in rbac.created_namespaced] == [
        state["read_binding"]
    ]


class MissingBindingsRbac:
    def delete_namespaced_role_binding(self, *, name, namespace):
        raise ApiException(status=404, reason=f"{namespace}/{name} absent")

    def delete_cluster_role_binding(self, *, name):
        raise ApiException(status=404, reason=f"{name} absent")


def test_bladeai_permission_revocation_is_idempotent_when_bindings_are_absent():
    trial_id = "campaign-1234567890abcdef-bladeai-t4"
    backend = KubernetesPermissionBackend(
        object(), MissingBindingsRbac(), object()
    )

    assert backend.revoke_read(trial_id)["revoked"] is True
    assert backend.revoke_metrics(trial_id)["revoked"] is True
    assert backend.revoke_chaos(trial_id)["revoked"] is True
