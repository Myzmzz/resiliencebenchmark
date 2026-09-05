"""Kubernetes RBAC backend for BladeAI's direct read/metrics path."""

from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path
from typing import Any

import yaml

from .contracts import BladeAINativePermissions


class KubernetesPermissionError(RuntimeError):
    pass


class KubernetesPermissionBackend:
    CONTROL_NAMESPACE = "resiliencebenchmark-system"
    APPLICATION_NAMESPACE = "otel-demo"
    TOKEN_AUDIENCE = "https://kubernetes.default.svc.cluster.local"
    CHAOS_CLUSTER_ROLE = "resbench-mcp-chaos-global-control"

    def __init__(self, core_api: Any, rbac_api: Any, auth_api: Any):
        self.core_api = core_api
        self.rbac_api = rbac_api
        self.auth_api = auth_api

    @classmethod
    def from_incluster(cls):
        try:
            from kubernetes import client, config
        except ImportError as exc:
            raise KubernetesPermissionError(
                "kubernetes runtime dependency is required"
            ) from exc
        config.load_incluster_config()
        return cls(
            client.CoreV1Api(),
            client.RbacAuthorizationV1Api(),
            client.AuthenticationV1Api(),
        )

    def provision_bladeai(
        self, trial_id: str, permissions: BladeAINativePermissions
    ) -> dict[str, Any]:
        from kubernetes import client
        from kubernetes.client.rest import ApiException

        suffix = hashlib.sha256(trial_id.encode()).hexdigest()[:12]
        service_account = f"resbench-bladeai-{suffix}"
        read_role = f"resbench-bladeai-read-{suffix}"
        metrics_role = f"resbench-bladeai-metrics-{suffix}"
        read_binding = read_role
        metrics_binding = metrics_role
        chaos_binding = f"resbench-bladeai-chaos-{suffix}"
        labels = {
            "app.kubernetes.io/managed-by": "resbench-stage2",
            "resiliencebenchmark.io/trial": suffix,
        }
        self._create_or_replace_sa(service_account, labels)
        read_rules = [
            client.V1PolicyRule(
                api_groups=[""],
                resources=["pods", "pods/log", "services", "endpoints", "events"],
                verbs=["get", "list"],
            ),
            client.V1PolicyRule(
                api_groups=["apps"],
                resources=["deployments", "replicasets"],
                verbs=["get", "list"],
            ),
        ]
        metrics_rules = [
            client.V1PolicyRule(
                api_groups=["metrics.k8s.io"],
                resources=["pods"],
                verbs=["get", "list"],
            )
        ]
        if permissions.kubernetes_read:
            self._create_or_replace_role(read_role, read_rules, labels)
            self._create_or_replace_binding(
                read_binding, read_role, service_account, labels
            )
        else:
            self._delete_namespaced_binding(read_binding)
            self._delete_namespaced_role(read_role)
        if permissions.kubernetes_metrics:
            self._create_or_replace_role(metrics_role, metrics_rules, labels)
            self._create_or_replace_binding(
                metrics_binding, metrics_role, service_account, labels
            )
        else:
            self._delete_namespaced_binding(metrics_binding)
            self._delete_namespaced_role(metrics_role)
        if permissions.chaosblade_execute:
            self._create_or_replace_cluster_binding(
                chaos_binding,
                self.CHAOS_CLUSTER_ROLE,
                service_account,
                labels,
            )
        else:
            self._delete_cluster_binding(chaos_binding)
        return {
            "service_account": service_account,
            "read_role": read_role,
            "metrics_role": metrics_role,
            "read_binding": read_binding,
            "metrics_binding": metrics_binding,
            "chaos_binding": chaos_binding,
            "permissions": permissions.model_dump(mode="json"),
            "provisioned": True,
        }

    def revoke_read(self, trial_id: str) -> dict[str, Any]:
        state = self._state(trial_id)
        self._delete_namespaced_binding(state["read_binding"])
        return {
            "capability": "k8s.read",
            "binding": state["read_binding"],
            "revoked": True,
        }

    def restore_read(self, trial_id: str) -> dict[str, Any]:
        state = self._state(trial_id)
        self._create_or_replace_binding(
            state["read_binding"],
            state["read_role"],
            state["service_account"],
            self._labels(state),
        )
        return {
            "capability": "k8s.read",
            "binding": state["read_binding"],
            "verified": True,
        }

    def revoke_metrics(self, trial_id: str) -> dict[str, Any]:
        state = self._state(trial_id)
        self._delete_namespaced_binding(state["metrics_binding"])
        return {
            "capability": "metrics.k8s.io",
            "binding": state["metrics_binding"],
            "revoked": True,
        }

    def restore_metrics(self, trial_id: str) -> dict[str, Any]:
        state = self._state(trial_id)
        self._create_or_replace_binding(
            state["metrics_binding"],
            state["metrics_role"],
            state["service_account"],
            self._labels(state),
        )
        return {
            "capability": "metrics.k8s.io",
            "binding": state["metrics_binding"],
            "verified": True,
        }

    def revoke_chaos(self, trial_id: str) -> dict[str, Any]:
        state = self._state(trial_id)
        self._delete_cluster_binding(state["chaos_binding"])
        return {
            "capability": "native.blade.create",
            "binding": state["chaos_binding"],
            "revoked": True,
        }

    def restore_chaos(self, trial_id: str) -> dict[str, Any]:
        state = self._state(trial_id)
        self._create_or_replace_cluster_binding(
            state["chaos_binding"],
            self.CHAOS_CLUSTER_ROLE,
            state["service_account"],
            self._labels(state),
        )
        return {
            "capability": "native.blade.create",
            "binding": state["chaos_binding"],
            "verified": True,
        }

    def revoke_observability(self, trial_id: str) -> dict[str, Any]:
        read = self.revoke_read(trial_id)
        try:
            metrics = self.revoke_metrics(trial_id)
        except Exception:
            self.restore_read(trial_id)
            raise
        return {
            "capability": "bladeai.observability.all",
            "revoked": True,
            "read": read,
            "metrics": metrics,
        }

    def restore_observability(self, trial_id: str) -> dict[str, Any]:
        read = self.restore_read(trial_id)
        try:
            metrics = self.restore_metrics(trial_id)
        except Exception:
            self.revoke_read(trial_id)
            raise
        return {
            "capability": "bladeai.observability.all",
            "verified": True,
            "read": read,
            "metrics": metrics,
        }

    def _delete_namespaced_binding(self, name: str) -> None:
        from kubernetes.client.rest import ApiException

        try:
            self.rbac_api.delete_namespaced_role_binding(
                name=name,
                namespace=self.APPLICATION_NAMESPACE,
            )
        except ApiException as exc:
            if exc.status != 404:
                raise

    def _delete_cluster_binding(self, name: str) -> None:
        from kubernetes.client.rest import ApiException

        try:
            self.rbac_api.delete_cluster_role_binding(name=name)
        except ApiException as exc:
            if exc.status != 404:
                raise

    def _delete_namespaced_role(self, name: str) -> None:
        from kubernetes.client.rest import ApiException

        try:
            self.rbac_api.delete_namespaced_role(
                name=name,
                namespace=self.APPLICATION_NAMESPACE,
            )
        except ApiException as exc:
            if exc.status != 404:
                raise

    def cleanup_trial(self, trial_id: str) -> dict[str, Any]:
        from kubernetes.client.rest import ApiException

        state = self._state(trial_id)
        for name in (state["read_binding"], state["metrics_binding"]):
            try:
                self.rbac_api.delete_namespaced_role_binding(
                    name=name, namespace=self.APPLICATION_NAMESPACE
                )
            except ApiException as exc:
                if exc.status != 404:
                    raise
        for name in (state["read_role"], state["metrics_role"]):
            try:
                self.rbac_api.delete_namespaced_role(
                    name=name, namespace=self.APPLICATION_NAMESPACE
                )
            except ApiException as exc:
                if exc.status != 404:
                    raise
        try:
            self.core_api.delete_namespaced_service_account(
                name=state["service_account"], namespace=self.CONTROL_NAMESPACE
            )
        except ApiException as exc:
            if exc.status != 404:
                raise
        try:
            self.rbac_api.delete_cluster_role_binding(state["chaos_binding"])
        except ApiException as exc:
            if exc.status != 404:
                raise
        return {"trial_id": trial_id, "verified": True}

    def issue_kubeconfig(self, trial_id: str, output_path: Path) -> dict[str, Any]:
        from kubernetes import client

        service_account = self._state(trial_id)["service_account"]
        request = client.AuthenticationV1TokenRequest(
            spec=client.V1TokenRequestSpec(
                audiences=[self.TOKEN_AUDIENCE],
                expiration_seconds=7200,
            )
        )
        response = self.core_api.create_namespaced_service_account_token(
            name=service_account,
            namespace=self.CONTROL_NAMESPACE,
            body=request,
        )
        token = str(response.status.token)
        if len(token) < 32:
            raise KubernetesPermissionError("TokenRequest returned an invalid token")
        host = os.environ.get("KUBERNETES_SERVICE_HOST", "kubernetes.default.svc")
        port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
        ca_path = Path("/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")
        if not ca_path.is_file():
            raise KubernetesPermissionError("in-cluster CA file is missing")
        document = {
            "apiVersion": "v1",
            "kind": "Config",
            "clusters": [
                {
                    "name": "kubernetes",
                    "cluster": {
                        "server": f"https://{host}:{port}",
                        "certificate-authority-data": base64.b64encode(
                            ca_path.read_bytes()
                        ).decode("ascii"),
                    },
                }
            ],
            "users": [{"name": service_account, "user": {"token": token}}],
            "contexts": [
                {
                    "name": service_account,
                    "context": {
                        "cluster": "kubernetes",
                        "user": service_account,
                        "namespace": self.APPLICATION_NAMESPACE,
                    },
                }
            ],
            "current-context": service_account,
        }
        destination = output_path.resolve()
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = destination.with_suffix(".tmp")
        temporary.write_text(
            yaml.safe_dump(document, sort_keys=False), encoding="utf-8"
        )
        temporary.chmod(0o600)
        os.replace(temporary, destination)
        destination.chmod(0o600)
        return {
            "service_account": service_account,
            "path": str(destination),
            "mode": "0600",
        }

    def _create_or_replace_sa(self, name: str, labels: dict[str, str]) -> None:
        from kubernetes import client
        from kubernetes.client.rest import ApiException

        body = client.V1ServiceAccount(
            metadata=client.V1ObjectMeta(name=name, labels=labels),
            automount_service_account_token=False,
        )
        try:
            self.core_api.create_namespaced_service_account(
                namespace=self.CONTROL_NAMESPACE, body=body
            )
        except ApiException as exc:
            if exc.status != 409:
                raise
            self.core_api.replace_namespaced_service_account(
                name=name, namespace=self.CONTROL_NAMESPACE, body=body
            )

    def _create_or_replace_role(self, name, rules, labels) -> None:
        from kubernetes import client
        from kubernetes.client.rest import ApiException

        body = client.V1Role(
            metadata=client.V1ObjectMeta(name=name, labels=labels), rules=rules
        )
        try:
            self.rbac_api.create_namespaced_role(
                namespace=self.APPLICATION_NAMESPACE, body=body
            )
        except ApiException as exc:
            if exc.status != 409:
                raise
            self.rbac_api.replace_namespaced_role(
                name=name, namespace=self.APPLICATION_NAMESPACE, body=body
            )

    def _create_or_replace_binding(self, name, role, service_account, labels) -> None:
        from kubernetes import client
        from kubernetes.client.rest import ApiException

        body = client.V1RoleBinding(
            metadata=client.V1ObjectMeta(name=name, labels=labels),
            role_ref=client.V1RoleRef(
                api_group="rbac.authorization.k8s.io", kind="Role", name=role
            ),
            subjects=[
                client.RbacV1Subject(
                    kind="ServiceAccount",
                    name=service_account,
                    namespace=self.CONTROL_NAMESPACE,
                )
            ],
        )
        try:
            self.rbac_api.create_namespaced_role_binding(
                namespace=self.APPLICATION_NAMESPACE, body=body
            )
        except ApiException as exc:
            if exc.status != 409:
                raise
            self.rbac_api.replace_namespaced_role_binding(
                name=name, namespace=self.APPLICATION_NAMESPACE, body=body
            )

    def _create_or_replace_cluster_binding(
        self, name, cluster_role, service_account, labels
    ) -> None:
        from kubernetes import client
        from kubernetes.client.rest import ApiException

        body = client.V1ClusterRoleBinding(
            metadata=client.V1ObjectMeta(name=name, labels=labels),
            role_ref=client.V1RoleRef(
                api_group="rbac.authorization.k8s.io",
                kind="ClusterRole",
                name=cluster_role,
            ),
            subjects=[
                client.RbacV1Subject(
                    kind="ServiceAccount",
                    name=service_account,
                    namespace=self.CONTROL_NAMESPACE,
                )
            ],
        )
        try:
            self.rbac_api.create_cluster_role_binding(body=body)
        except ApiException as exc:
            if exc.status != 409:
                raise
            self.rbac_api.replace_cluster_role_binding(name=name, body=body)

    @staticmethod
    def _state(trial_id: str) -> dict[str, str]:
        suffix = hashlib.sha256(trial_id.encode()).hexdigest()[:12]
        return {
            "suffix": suffix,
            "service_account": f"resbench-bladeai-{suffix}",
            "read_role": f"resbench-bladeai-read-{suffix}",
            "metrics_role": f"resbench-bladeai-metrics-{suffix}",
            "read_binding": f"resbench-bladeai-read-{suffix}",
            "metrics_binding": f"resbench-bladeai-metrics-{suffix}",
            "chaos_binding": f"resbench-bladeai-chaos-{suffix}",
        }

    @staticmethod
    def _labels(state: dict[str, str]) -> dict[str, str]:
        return {
            "app.kubernetes.io/managed-by": "resbench-stage2",
            "resiliencebenchmark.io/trial": state["suffix"],
        }
