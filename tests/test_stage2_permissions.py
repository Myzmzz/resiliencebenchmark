from __future__ import annotations

from pathlib import Path

from stage2_service.contracts import HarnessKind
from stage2_service.permissions import NullPermissionBackend, Stage2PermissionManager
from stage2_service.kubernetes_permissions import KubernetesPermissionBackend
from stage2_service.runtime_adapters import McpTokenStateRegistry


class MainFault:
    fault_type = "network-delay"


class Internal:
    main_fault = MainFault()


class Episode:
    internal = Internal()


class Runtime:
    pass


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
        campaign, codex_trial, HarnessKind.CODEX, Episode(), Runtime()
    )
    blade = manager.provision(
        campaign, blade_trial, HarnessKind.BLADEAI, Episode(), Runtime()
    )

    assert codex.direct_kubeconfig is False
    assert blade.direct_kubeconfig is True
    assert not any(rule.api_group == "metrics.k8s.io" for rule in codex.kubernetes_rules)
    assert any(rule.api_group == "metrics.k8s.io" for rule in blade.kubernetes_rules)
    assert manager.runtime_context(codex_trial)["mcp_token"]
    assert manager.restore(codex_trial)["verified"] is True
    assert manager.restore(blade_trial)["verified"] is True


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
