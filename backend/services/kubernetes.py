"""Kubernetes 集群数据采集服务。"""

import os
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Dict, List, Optional

from backend.models.experiment import (
    ClusterSummary,
    ComponentHealth,
    ComponentStatus,
    ExperimentEnvironment,
    NodeInfo,
    PodInfo,
    PodPhase,
)


def get_experiment_environment() -> ExperimentEnvironment:
    """
    获取实验环境状态。

    只有显式配置 BENCHMARK_KUBECONFIG 时才读取真实集群；
    不继承通用 KUBECONFIG，避免前端状态页隐式访问其他集群。
    """
    kubeconfig = os.getenv("BENCHMARK_KUBECONFIG")

    if kubeconfig and Path(kubeconfig).exists():
        return _fetch_from_cluster(kubeconfig)
    else:
        return _unconfigured_environment()


def _fetch_from_cluster(kubeconfig: str) -> ExperimentEnvironment:
    """从真实 K8s 集群获取数据（需要 kubernetes 库）。"""
    try:
        from kubernetes import client, config

        config.load_kube_config(config_file=kubeconfig)
        v1 = client.CoreV1Api()
        version_api = client.VersionApi()

        # 获取版本信息
        version_info = version_api.get_code()
        k8s_version = f"v{version_info.major}.{version_info.minor}"

        # 获取 API Server 地址
        configuration = client.Configuration.get_default_copy()
        api_server = configuration.host

        # 获取节点列表
        nodes_list = v1.list_node()
        nodes: List[NodeInfo] = []
        node_pod_count: Dict[str, int] = defaultdict(int)

        # 获取所有 Pods
        pods_list = v1.list_pod_for_all_namespaces()
        pods: List[PodInfo] = []
        namespaces = set()
        abnormal_count = 0

        for pod in pods_list.items:
            phase: PodPhase = pod.status.phase or "Unknown"  # type: ignore
            if phase in ("Failed", "Unknown", "Pending"):
                abnormal_count += 1

            # 计算重启次数
            restarts = sum(
                cs.restart_count for cs in (pod.status.container_statuses or [])
            )

            # 计算 ready 状态
            ready_containers = sum(
                1 for cs in (pod.status.container_statuses or []) if cs.ready
            )
            total_containers = len(pod.spec.containers or [])
            ready_str = f"{ready_containers}/{total_containers}"

            node_name = pod.spec.node_name or "unassigned"
            node_pod_count[node_name] += 1
            namespaces.add(pod.metadata.namespace)

            pods.append(
                PodInfo(
                    name=pod.metadata.name,
                    namespace=pod.metadata.namespace,
                    node=node_name,
                    phase=phase,
                    restarts=restarts,
                    ready=ready_str,
                    ip=pod.status.pod_ip,
                    created_at=pod.metadata.creation_timestamp,
                )
            )

        # 构建节点信息
        for node in nodes_list.items:
            node_name = node.metadata.name
            status = "Ready" if any(
                c.type == "Ready" and c.status == "True"
                for c in (node.status.conditions or [])
            ) else "NotReady"

            nodes.append(
                NodeInfo(
                    name=node_name,
                    pod_count=node_pod_count.get(node_name, 0),
                    status=status,  # type: ignore
                )
            )

        # 获取混沌工程组件状态（查询特定命名空间的部署）
        components = _fetch_component_status(v1)

        summary = ClusterSummary(
            node_count=len(nodes),
            namespace_count=len(namespaces),
            pod_count=len(pods),
            abnormal_pod_count=abnormal_count,
        )

        return ExperimentEnvironment(
            api_server=api_server,
            k8s_version=k8s_version,
            last_sync=datetime.now(UTC),
            connection_status="连接正常",
            summary=summary,
            nodes=nodes,
            pods=pods,
            components=components,
        )

    except ImportError:
        return _error_environment()
    except Exception:
        # 连接失败，返回错误状态
        return _error_environment()


def _fetch_component_status(v1: "client.CoreV1Api") -> List[ComponentStatus]:  # type: ignore # noqa: F821
    """获取混沌工程组件状态。"""
    from kubernetes import client

    apps_v1 = client.AppsV1Api()
    components: List[ComponentStatus] = []

    # 定义需要检查的组件（命名空间 + 部署名称）
    component_map = {
        ("chaos-mesh", "chaos-dashboard"): "ChaosBlade",
        ("chaos-mesh", "chaos-controller-manager"): "ChaosBlade Operator",
        ("observability", "prometheus-server"): "Prometheus",
        ("observability", "jaeger"): "Jaeger",
        ("observability", "loki"): "Loki",
    }

    for (namespace, deployment_name), display_name in component_map.items():
        try:
            deployment = apps_v1.read_namespaced_deployment(deployment_name, namespace)
            replicas = deployment.spec.replicas or 0
            available = deployment.status.available_replicas or 0
            version = deployment.metadata.labels.get("version", "unknown")

            health: ComponentHealth = (
                "运行正常" if available == replicas else
                "需要关注" if available > 0 else
                "异常"
            )

            components.append(
                ComponentStatus(
                    name=display_name,
                    version=version,
                    instances=f"{available}/{replicas}",
                    health=health,
                )
            )
        except Exception:
            # 组件不存在或查询失败
            components.append(
                ComponentStatus(
                    name=display_name,
                    version="N/A",
                    instances="0/0",
                    health="异常",
                )
            )

    return components


def _unconfigured_environment() -> ExperimentEnvironment:
    """没有显式 Benchmark kubeconfig 时，不伪造集群状态。"""
    return ExperimentEnvironment(
        api_server="",
        k8s_version="",
        last_sync=datetime.now(UTC),
        connection_status="未配置",
        summary=ClusterSummary(
            node_count=0,
            namespace_count=0,
            pod_count=0,
            abnormal_pod_count=0,
        ),
        nodes=[],
        pods=[],
        components=[],
    )


def _error_environment() -> ExperimentEnvironment:
    """返回连接失败状态。"""
    return ExperimentEnvironment(
        api_server="",
        k8s_version="",
        last_sync=datetime.now(UTC),
        connection_status="连接失败",
        summary=ClusterSummary(
            node_count=0,
            namespace_count=0,
            pod_count=0,
            abnormal_pod_count=0,
        ),
        nodes=[],
        pods=[],
        components=[],
    )
