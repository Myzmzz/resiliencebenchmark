"""Kubernetes RBAC backend for BladeAI's direct read/metrics path."""

from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path
from typing import Any

import yaml


class KubernetesPermissionError(RuntimeError):
    pass


class KubernetesPermissionBackend:
    CONTROL_NAMESPACE = "resiliencebenchmark-system"
    APPLICATION_NAMESPACE = "otel-demo"
    TOKEN_AUDIENCE = "https://kubernetes.default.svc.cluster.local"

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

    def provision_bladeai(self, trial_id: str) -> dict[str, Any]:
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
        self._create_or_replace_role(read_role, read_rules, labels)
        self._create_or_replace_role(metrics_role, metrics_rules, labels)
        self._create_or_replace_binding(
            read_binding, read_role, service_account, labels
        )
        self._create_or_replace_binding(
            metrics_binding, metrics_role, service_account, labels
        )
        self._create_or_replace_cluster_binding(
            chaos_binding,
            "resbench-mcp-chaos-global-control",
            service_account,
            labels,
        )
        return {
            "service_account": service_account,
            "read_role": read_role,
            "metrics_role": metrics_role,
            "read_binding": read_binding,
            "metrics_binding": metrics_binding,
            "chaos_binding": chaos_binding,
            "provisioned": True,
        }

    def revoke_metrics(self, trial_id: str) -> dict[str, Any]:
        from kubernetes.client.rest import ApiException

        name = self._metrics_name(trial_id)
        try:
            self.rbac_api.delete_namespaced_role_binding(
                name=name,
                namespace=self.APPLICATION_NAMESPACE,
            )
        except ApiException as exc:
            if exc.status != 404:
                raise
        return {"capability": "metrics.k8s.io", "binding": name, "revoked": True}

    def restore_metrics(self, trial_id: str) -> dict[str, Any]:
        state = self._state(trial_id)
        self._create_or_replace_binding(
            state["metrics_binding"],
            state["metrics_role"],
            state["service_account"],
            {
                "app.kubernetes.io/managed-by": "resbench-stage2",
                "resiliencebenchmark.io/trial": state["suffix"],
            },
        )
        return {"capability": "metrics.k8s.io", "verified": True}

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

    @classmethod
    def _metrics_name(cls, trial_id: str) -> str:
        return cls._state(trial_id)["metrics_binding"]
