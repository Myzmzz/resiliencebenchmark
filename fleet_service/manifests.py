"""Render every Kubernetes object a fleet slot needs.

The controller objects come from the reviewed single-instance templates
(``deploy/stage2/stage2-integration.yaml`` and ``execution-identities.yaml``):
same containers, same security context, same volumes, with a per-slot name and
one extra environment variable binding the instance to its replica namespace.

Nothing here talks to a cluster. ``fleet_service.kube`` applies what this
returns, and ``POST /provision?dry_run=true`` returns it unapplied.
"""

from __future__ import annotations

import copy
from collections.abc import Sequence
from typing import Any

from .contracts import FleetConfig
from .guard import assert_operable_namespace, replica_namespace, slot_id


MANAGED_BY = "resbench-fleet"
SLOT_LABEL = "benchmark.slot"
NAMESPACE_LABEL = "benchmark.namespace"
CONTROLLER_SERVICE_ACCOUNT = "resbench-stage2-controller"
EXECUTOR_SERVICE_ACCOUNT = "resbench-stage2-executor"
FINALIZER_SERVICE_ACCOUNT = "resbench-stage2-finalizer"
AGENT_LOOPBACK_PORTS = (
    "18081,18082,18083,18084,18085,18086,18087,18088,"
    "18181,18182,18183,18184,18185,18186,18187,18188,18090,18481"
)


def slot_labels(config: FleetConfig, index: int) -> dict[str, str]:
    return {
        "app.kubernetes.io/name": f"resbench-stage2-{slot_id(index)}",
        "app.kubernetes.io/managed-by": MANAGED_BY,
        SLOT_LABEL: slot_id(index),
        NAMESPACE_LABEL: replica_namespace(config.namespace_prefix, index),
        "resiliencebenchmark.io/source-head": config.source_head,
    }


def replica_namespace_manifests(
    config: FleetConfig, index: int, api_server_endpoints: Sequence[str] = ()
) -> list[dict[str, Any]]:
    """Namespace, resource bounds and a default-deny-ish NetworkPolicy.

    The quota and LimitRange keep one replica's CPU fault inside its own
    namespace; the policy is a second fence around the Agent's own egress
    restrictions, not a replacement for them.
    """
    namespace = replica_namespace(config.namespace_prefix, index)
    assert_operable_namespace(config.namespace_prefix, namespace)
    labels = {
        "app.kubernetes.io/managed-by": MANAGED_BY,
        "resiliencebenchmark.io/application": config.sut_application,
        SLOT_LABEL: slot_id(index),
        NAMESPACE_LABEL: namespace,
    }
    return [
        {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": namespace, "labels": labels}},
        {
            "apiVersion": "v1",
            "kind": "LimitRange",
            "metadata": {"name": "resbench-replica-defaults", "namespace": namespace, "labels": labels},
            "spec": {
                "limits": [
                    {
                        "type": "Container",
                        "default": {"cpu": "1", "memory": "512Mi"},
                        "defaultRequest": {"cpu": "50m", "memory": "64Mi"},
                        "max": {"cpu": "2", "memory": "2Gi"},
                    }
                ]
            },
        },
        {
            "apiVersion": "v1",
            "kind": "ResourceQuota",
            "metadata": {"name": "resbench-replica-quota", "namespace": namespace, "labels": labels},
            "spec": {
                "hard": {
                    "requests.cpu": "2",
                    "requests.memory": "4Gi",
                    "limits.cpu": "8",
                    "limits.memory": "8Gi",
                    "count/deployments.apps": "12",
                    "pods": "30",
                }
            },
        },
        {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": {"name": "resbench-replica-boundary", "namespace": namespace, "labels": labels},
            "spec": {
                "podSelector": {},
                "policyTypes": ["Ingress", "Egress"],
                "ingress": [
                    {"from": [{"podSelector": {}}]},
                    {"from": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": config.control_namespace}}}]},
                    {"from": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "observability"}}}]},
                    {"from": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "coroot"}}}]},
                ],
                "egress": [
                    {"to": [{"podSelector": {}}]},
                    {"to": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "observability"}}}]},
                    {"to": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "coroot"}}}]},
                    {"to": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}}}],
                     "ports": [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}]},
                    {"to": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": config.control_namespace}}}]},
                    # The collector's k8sattributes processor reads the API
                    # server to put k8s.namespace.name on every span. Without
                    # it a replica's traces carry no namespace and the agent's
                    # namespace-scoped trace view returns nothing at all.
                    *(
                        [{
                            "to": [{"ipBlock": {"cidr": _cidr(address)}} for address in api_server_endpoints],
                            "ports": [{"protocol": "TCP", "port": 443}, {"protocol": "TCP", "port": 6443}],
                        }]
                        if api_server_endpoints
                        else []
                    ),
                ],
            },
        },
    ]


def _replica_rbac(config: FleetConfig, index: int) -> list[dict[str, Any]]:
    """The Controller's, executor's and finalizer's rights inside one replica.

    Copied from ``deploy/stage2/stage2.yaml`` and
    ``deploy/stage2/execution-identities.yaml``; only the namespace changes.
    Every slot binds the same shared ServiceAccounts, so the bindings are
    per-namespace and never edited in place by another slot.
    """
    namespace = replica_namespace(config.namespace_prefix, index)
    labels = slot_labels(config, index)
    control = config.control_namespace

    def role(name: str, rules: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "Role",
            "metadata": {"name": name, "namespace": namespace, "labels": labels},
            "rules": rules,
        }

    def binding(name: str, role_name: str, subjects: list[str]) -> dict[str, Any]:
        return {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "RoleBinding",
            "metadata": {"name": name, "namespace": namespace, "labels": labels},
            "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": role_name},
            "subjects": [
                {"kind": "ServiceAccount", "name": subject, "namespace": control} for subject in subjects
            ],
        }

    control_rules = [
        {
            "apiGroups": [""],
            "resources": [
                "pods", "pods/log", "pods/exec", "pods/eviction", "services", "services/proxy",
                "endpoints", "configmaps", "secrets", "serviceaccounts", "persistentvolumeclaims",
                "events", "replicationcontrollers", "limitranges", "resourcequotas",
            ],
            "verbs": ["get", "list", "watch", "create", "update", "patch", "delete"],
        },
        {
            "apiGroups": ["apps"],
            "resources": [
                "deployments", "deployments/scale", "replicasets", "replicasets/scale",
                "statefulsets", "statefulsets/scale", "daemonsets",
            ],
            "verbs": ["get", "list", "watch", "create", "update", "patch", "delete"],
        },
        {"apiGroups": ["batch"], "resources": ["jobs", "cronjobs"],
         "verbs": ["get", "list", "watch", "create", "update", "patch", "delete"]},
        {"apiGroups": ["networking.k8s.io"], "resources": ["ingresses", "networkpolicies"],
         "verbs": ["get", "list", "watch", "create", "update", "patch", "delete"]},
        {"apiGroups": ["autoscaling"], "resources": ["horizontalpodautoscalers"],
         "verbs": ["get", "list", "watch", "create", "update", "patch", "delete"]},
        {"apiGroups": ["policy"], "resources": ["poddisruptionbudgets"],
         "verbs": ["get", "list", "watch", "create", "update", "patch", "delete"]},
        {"apiGroups": ["rbac.authorization.k8s.io"], "resources": ["roles", "rolebindings"],
         "verbs": ["get", "list", "watch", "create", "update", "patch", "delete"]},
        {"apiGroups": ["discovery.k8s.io"], "resources": ["endpointslices"], "verbs": ["get", "list", "watch"]},
        {"apiGroups": ["chaos-mesh.org"], "resources": ["networkchaos", "podchaos", "stresschaos"],
         "verbs": ["get", "list"]},
        {"apiGroups": ["metrics.k8s.io"], "resources": ["pods"], "verbs": ["get", "list"]},
    ]
    executor_rules = [
        {"apiGroups": ["chaos-mesh.org"], "resources": ["networkchaos", "podchaos", "stresschaos"],
         "verbs": ["get", "list", "create"]},
        {"apiGroups": [""], "resources": ["pods"], "verbs": ["get", "list", "patch"]},
    ]
    finalizer_rules = [
        {"apiGroups": ["chaos-mesh.org"], "resources": ["networkchaos", "podchaos", "stresschaos"],
         "verbs": ["get", "list", "delete", "patch"]},
        {"apiGroups": [""], "resources": ["pods"], "verbs": ["get", "list", "patch"]},
    ]
    read_rules = [
        {"apiGroups": [""], "resources": ["pods", "pods/log", "services", "endpoints", "configmaps", "events"],
         "verbs": ["get", "list", "watch"]},
        {"apiGroups": ["apps"], "resources": ["deployments", "replicasets", "statefulsets", "daemonsets"],
         "verbs": ["get", "list", "watch"]},
        {"apiGroups": ["metrics.k8s.io"], "resources": ["pods"], "verbs": ["get", "list"]},
    ]
    suffix = f"-{namespace}"
    return [
        role(f"resbench-stage2-controller-control{suffix}", control_rules),
        binding(f"resbench-stage2-controller-control{suffix}",
                f"resbench-stage2-controller-control{suffix}", [CONTROLLER_SERVICE_ACCOUNT]),
        role(f"resbench-stage2-executor{suffix}", executor_rules),
        binding(f"resbench-stage2-executor{suffix}", f"resbench-stage2-executor{suffix}",
                [EXECUTOR_SERVICE_ACCOUNT]),
        role(f"resbench-stage2-finalizer{suffix}", finalizer_rules),
        binding(f"resbench-stage2-finalizer{suffix}", f"resbench-stage2-finalizer{suffix}",
                [FINALIZER_SERVICE_ACCOUNT]),
        role(f"resbench-mcp-read{suffix}", read_rules),
        binding(f"resbench-mcp-read{suffix}", f"resbench-mcp-read{suffix}", [CONTROLLER_SERVICE_ACCOUNT]),
    ]


def _controller_containers(config: FleetConfig, index: int) -> list[dict[str, Any]]:
    namespace = replica_namespace(config.namespace_prefix, index)
    private_root = "/var/lib/resbench-stage2/fleet/private"
    artifact_root = "/var/lib/resbench-stage2/fleet/artifacts"
    stage2_env = [
        {"name": "STAGE2_POD_NAME", "valueFrom": {"fieldRef": {"fieldPath": "metadata.name"}}},
        {"name": "STAGE2_POD_UID", "valueFrom": {"fieldRef": {"fieldPath": "metadata.uid"}}},
        {"name": "STAGE2_POD_NAMESPACE", "valueFrom": {"fieldRef": {"fieldPath": "metadata.namespace"}}},
        {"name": "STAGE2_PRIVATE_ROOT", "value": private_root},
        {"name": "STAGE2_ARTIFACT_ROOT", "value": artifact_root},
        {"name": "STAGE2_D0_ARTIFACT_ROOT", "value": "/var/lib/resbench-stage2/fleet/d0"},
        {"name": "STAGE2_KUBECONFIG", "value": f"{private_root}/service.kubeconfig"},
        {"name": "STAGE2_HARNESS_CAPABILITIES_FILE", "value": f"{private_root}/harness-capabilities.json"},
        {"name": "STAGE2_LITELLM_CONFIG_FILE", "value": "/etc/litellm/config.yaml"},
        {"name": "RESBENCH_GATEWAY_AUDIT_DIR", "value": "/var/lib/resbench-stage2/gateway-audit"},
        # The whole point of a slot: this Controller is bound to one replica.
        {"name": "RESBENCH_APPLICATION", "value": namespace},
        {"name": "RESBENCH_APPLICATION_NAMESPACE", "value": namespace},
        {"name": "RESBENCH_APPLICATION_COMPONENT", "value": "cart"},
        {"name": "RESBENCH_CONTROL_NAMESPACE", "value": config.control_namespace},
        {"name": "RESBENCH_COROOT_ALLOW_ANONYMOUS_READ",
         "value": "true" if config.coroot_allow_anonymous_read else "false"},
        {"name": "RESBENCH_LLM_BASE_URL",
         "valueFrom": {"secretKeyRef": {"name": "resbench-stage2-gateway-client", "key": "llm-base-url"}}},
        {"name": "RESBENCH_LLM_API_KEY",
         "valueFrom": {"secretKeyRef": {"name": "resbench-stage2-gateway-client", "key": "llm-api-key"}}},
    ]
    if config.coroot_project_id:
        stage2_env.insert(-2, {"name": "RESBENCH_COROOT_PROJECT_ID", "value": config.coroot_project_id})
    return [
        {
            "name": "litellm",
            "image": config.litellm_image,
            "imagePullPolicy": "IfNotPresent",
            "command": ["/app/.venv/bin/litellm"],
            "args": ["--config", "/etc/litellm/config.yaml", "--host", "127.0.0.1", "--port", "4000"],
            "env": [
                {"name": "STAGE2_LITELLM_CONFIG_FILE", "value": "/etc/litellm/config.yaml"},
                {"name": "RESBENCH_GATEWAY_AUDIT_DIR", "value": "/var/lib/resbench-stage2/gateway-audit"},
            ],
            "envFrom": [{"secretRef": {"name": "litellm-upstream"}}],
            "ports": [{"name": "litellm", "containerPort": 4000}],
            "volumeMounts": [
                {"name": "litellm-config", "mountPath": "/etc/litellm/config.yaml", "subPath": "config.yaml", "readOnly": True},
                {"name": "litellm-config", "mountPath": "/etc/litellm/gateway_audit.py", "subPath": "gateway_audit.py", "readOnly": True},
                {"name": "gateway-audit", "mountPath": "/var/lib/resbench-stage2/gateway-audit"},
            ],
            "startupProbe": {
                "exec": {"command": ["/app/.venv/bin/python", "-c",
                                     "from urllib.request import urlopen; urlopen('http://127.0.0.1:4000/health/liveliness', timeout=2).close()"]},
                "periodSeconds": 5, "failureThreshold": 24,
            },
            "readinessProbe": {
                "exec": {"command": ["/app/.venv/bin/python", "-c",
                                     "from urllib.request import urlopen; urlopen('http://127.0.0.1:4000/health/readiness', timeout=2).close()"]},
                "periodSeconds": 15, "timeoutSeconds": 5,
            },
            "resources": {"requests": {"cpu": "250m", "memory": "512Mi"}, "limits": {"cpu": "1", "memory": "2Gi"}},
        },
        {
            "name": "stage2",
            "image": config.controller_image,
            "imagePullPolicy": "IfNotPresent",
            "ports": [{"name": "http", "containerPort": 8080}],
            "env": stage2_env,
            "volumeMounts": [
                {"name": "data", "mountPath": "/var/lib/resbench-stage2"},
                {"name": "runtime", "mountPath": "/etc/resbench-stage2", "readOnly": True},
                {"name": "litellm-config", "mountPath": "/etc/litellm/config.yaml", "subPath": "config.yaml", "readOnly": True},
                {"name": "gateway-audit", "mountPath": "/var/lib/resbench-stage2/gateway-audit"},
                {"name": "tmp", "mountPath": "/tmp"},
                {"name": "trial-work", "mountPath": "/var/lib/resbench-stage2/agent-trials"},
                {"name": "agent-exec-ipc", "mountPath": "/run/resbench"},
                {"name": "sandbox-work", "mountPath": "/var/lib/resbench-stage2/sandbox-trials"},
                {"name": "controller-service-account", "mountPath": "/var/run/secrets/kubernetes.io/serviceaccount", "readOnly": True},
            ],
            "readinessProbe": {"httpGet": {"path": "/healthz", "port": "http"}, "initialDelaySeconds": 10,
                               "periodSeconds": 10, "timeoutSeconds": 5},
            "livenessProbe": {"httpGet": {"path": "/healthz", "port": "http"}, "initialDelaySeconds": 30,
                              "periodSeconds": 20, "timeoutSeconds": 5, "failureThreshold": 6},
            "resources": {
                "requests": config.resources.requests.model_dump(mode="json"),
                "limits": config.resources.limits.model_dump(mode="json"),
            },
        },
        {
            "name": "agent-runtime",
            "image": config.agent_image,
            "imagePullPolicy": "IfNotPresent",
            "command": ["/opt/agent/.venv/bin/python", "-m", "harness.agent_exec"],
            "args": [
                "--socket", "/run/resbench/agent-exec.sock",
                "--trial-root", "/var/lib/resbench-stage2/agent-trials",
                "--sandbox-trial-root", "/var/lib/resbench-stage2/sandbox-trials",
                "--cgroup-root", "/run/resbench-cgroups",
                "--controller-uid", "10001", "--agent-uid", "10002", "--agent-gid", "10004",
                "--shared-trial-gid", "10004", "--sandbox-uid", "10003", "--sandbox-gid", "10003",
                "--socket-gid", "10001", "--memory-max", "536870912", "--pids-max", "64",
                "--cpu-max", "100000 100000",
            ],
            "env": [
                {"name": "RESBENCH_AGENT_EXEC_POD_UID", "valueFrom": {"fieldRef": {"fieldPath": "metadata.uid"}}},
                {"name": "RESBENCH_AGENT_EXEC_EGRESS_POLICY", "value": "required"},
                {"name": "RESBENCH_AGENT_EXEC_AGENT_UID", "value": "10002"},
                {"name": "RESBENCH_AGENT_EXEC_ALLOWED_LOOPBACK_PORTS", "value": AGENT_LOOPBACK_PORTS},
            ],
            "securityContext": {
                "runAsUser": 0, "runAsGroup": 0, "runAsNonRoot": False, "readOnlyRootFilesystem": True,
                "allowPrivilegeEscalation": False,
                "capabilities": {"drop": ["ALL"],
                                 "add": ["NET_ADMIN", "SYS_ADMIN", "SETUID", "SETGID", "SETPCAP", "KILL", "CHOWN", "FOWNER"]},
            },
            "volumeMounts": [
                {"name": "trial-work", "mountPath": "/var/lib/resbench-stage2/agent-trials"},
                {"name": "agent-exec-ipc", "mountPath": "/run/resbench"},
                {"name": "sandbox-work", "mountPath": "/var/lib/resbench-stage2/sandbox-trials"},
                {"name": "agent-tmp", "mountPath": "/tmp"},
                {"name": "delegated-cgroup", "mountPath": "/run/resbench-cgroups"},
                {"name": "host-cgroup-namespace", "mountPath": "/run/resbench-host/cgroupns", "readOnly": True},
            ],
            "readinessProbe": {
                "exec": {"command": ["/opt/agent/.venv/bin/python", "-c",
                                     "import socket; s=socket.socket(socket.AF_UNIX); s.settimeout(1); s.connect('/run/resbench/agent-exec.sock'); s.close()"]},
                "initialDelaySeconds": 3, "periodSeconds": 5, "timeoutSeconds": 2, "failureThreshold": 1,
            },
            "resources": {"requests": {"cpu": "250m", "memory": "512Mi"}, "limits": {"cpu": "1", "memory": "1Gi"}},
        },
    ]


def controller_manifests(config: FleetConfig, index: int) -> list[dict[str, Any]]:
    """PVC, Service and Deployment of one Controller instance, plus its RBAC."""
    name = f"resbench-stage2-{slot_id(index)}"
    labels = slot_labels(config, index)
    control = config.control_namespace
    selector = {"app.kubernetes.io/name": name}
    pod_labels = dict(labels)
    deployment: dict[str, Any] = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": name, "namespace": control, "labels": labels},
        "spec": {
            "replicas": 1,
            "strategy": {"type": "Recreate"},
            "selector": {"matchLabels": selector},
            "template": {
                "metadata": {
                    "labels": pod_labels,
                    "annotations": {
                        "container.apparmor.security.beta.kubernetes.io/agent-runtime": "localhost/resbench-agent-runtime",
                        "resiliencebenchmark.io/fleet-slot": slot_id(index),
                    },
                },
                "spec": {
                    "serviceAccountName": CONTROLLER_SERVICE_ACCOUNT,
                    "automountServiceAccountToken": False,
                    "terminationGracePeriodSeconds": 60,
                    "securityContext": {
                        "runAsNonRoot": True, "runAsUser": 10001, "runAsGroup": 10001,
                        "fsGroup": 10001, "fsGroupChangePolicy": "OnRootMismatch",
                        "supplementalGroups": [10003, 10004],
                    },
                    "initContainers": [
                        {
                            "name": "agent-workspace-permissions",
                            "image": config.agent_image,
                            "imagePullPolicy": "IfNotPresent",
                            "command": ["/bin/sh", "-ec"],
                            "args": [
                                "mkdir -p /work /sandbox /ipc /gateway-audit && "
                                "chown 10001:10004 /work && chmod 2770 /work && "
                                "chown 10001:10003 /sandbox && chmod 2770 /sandbox && "
                                "chown 10001:10001 /gateway-audit && chmod 0700 /gateway-audit && "
                                "chown 10001:10001 /ipc && chmod 2770 /ipc"
                            ],
                            "securityContext": {
                                "runAsUser": 0, "runAsGroup": 0, "runAsNonRoot": False,
                                "allowPrivilegeEscalation": False,
                                "capabilities": {"drop": ["ALL"], "add": ["CHOWN", "FOWNER"]},
                            },
                            "volumeMounts": [
                                {"name": "trial-work", "mountPath": "/work"},
                                {"name": "sandbox-work", "mountPath": "/sandbox"},
                                {"name": "agent-exec-ipc", "mountPath": "/ipc"},
                                {"name": "gateway-audit", "mountPath": "/gateway-audit"},
                            ],
                        }
                    ],
                    "containers": _controller_containers(config, index),
                    "volumes": [
                        {"name": "gateway-audit", "emptyDir": {"sizeLimit": "64Mi"}},
                        {"name": "data", "persistentVolumeClaim": {"claimName": f"{name}-data"}},
                        {"name": "runtime", "secret": {"secretName": "resbench-stage2-runtime", "defaultMode": 0o400}},
                        {"name": "tmp", "emptyDir": {"sizeLimit": "2Gi"}},
                        {"name": "trial-work", "emptyDir": {"sizeLimit": "2Gi"}},
                        {"name": "agent-exec-ipc", "emptyDir": {"sizeLimit": "64Mi"}},
                        {"name": "sandbox-work", "emptyDir": {"sizeLimit": "512Mi"}},
                        {"name": "agent-tmp", "emptyDir": {"sizeLimit": "256Mi"}},
                        {
                            "name": "controller-service-account",
                            "projected": {
                                "defaultMode": 0o440,
                                "sources": [
                                    {"serviceAccountToken": {"path": "token",
                                                             "audience": "https://kubernetes.default.svc.cluster.local",
                                                             "expirationSeconds": 3600}},
                                    {"configMap": {"name": "kube-root-ca.crt", "items": [{"key": "ca.crt", "path": "ca.crt"}]}},
                                    {"downwardAPI": {"items": [{"path": "namespace", "fieldRef": {"fieldPath": "metadata.namespace"}}]}},
                                ],
                            },
                        },
                        {"name": "delegated-cgroup", "hostPath": {"path": "/sys/fs/cgroup/resbench-agent-exec", "type": "DirectoryOrCreate"}},
                        {"name": "host-cgroup-namespace", "hostPath": {"path": "/proc/1/ns/cgroup", "type": "File"}},
                        {"name": "litellm-config", "configMap": {"name": config.litellm_config_map}},
                    ],
                },
            },
        },
    }
    if config.node_spread and config.nodes:
        # Round-robin so one node's CPU pressure cannot stall every slot.
        node = config.nodes[(index - 1) % len(config.nodes)]
        deployment["spec"]["template"]["spec"]["nodeSelector"] = {"kubernetes.io/hostname": node}
    objects: list[dict[str, Any]] = [
        {
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": {"name": f"{name}-data", "namespace": control, "labels": labels},
            "spec": {
                "storageClassName": config.resources.storage_class,
                "accessModes": ["ReadWriteOnce"],
                "resources": {"requests": {"storage": config.resources.storage}},
            },
        },
        deployment,
        {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": name, "namespace": control, "labels": labels},
            "spec": {"selector": selector, "ports": [{"name": "http", "port": 8080, "targetPort": "http"}]},
        },
    ]
    objects.extend(_replica_rbac(config, index))
    return objects


def _cidr(address: str) -> str:
    """A bare address becomes a single-host CIDR; an explicit CIDR is kept."""
    return address if "/" in address else f"{address}/32"


def slot_manifests(
    config: FleetConfig, index: int, api_server_endpoints: Sequence[str] = ()
) -> list[dict[str, Any]]:
    """Everything one slot owns: its replica namespace and its Controller."""
    return (
        replica_namespace_manifests(config, index, api_server_endpoints)
        + controller_manifests(config, index)
    )


def fleet_manifests(config: FleetConfig) -> list[dict[str, Any]]:
    objects: list[dict[str, Any]] = []
    for index in range(1, config.replicas + 1):
        objects.extend(slot_manifests(config, index))
    return objects


def controller_url(config: FleetConfig, index: int) -> str:
    return f"http://resbench-stage2-{slot_id(index)}.{config.control_namespace}.svc.cluster.local:8080"


def owned_object_summary(objects: list[dict[str, Any]]) -> list[dict[str, str]]:
    return [
        {
            "kind": str(item.get("kind")),
            "name": str((item.get("metadata") or {}).get("name")),
            "namespace": str((item.get("metadata") or {}).get("namespace") or ""),
        }
        for item in objects
    ]


def redacted(objects: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Manifests never carry secret values, only references; copy defensively."""
    return copy.deepcopy(objects)
