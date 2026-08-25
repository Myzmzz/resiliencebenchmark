"""Kubernetes 客户端服务。

连接 K8s API Server 并获取集群状态数据。
支持从 kubeconfig 或 in-cluster 配置加载。
"""

from datetime import UTC, datetime
from typing import List, Optional

from backend.models.experiment import (
    ClusterStatistics,
    ComponentStatus,
    ExperimentEnvironment,
    NodeInfo,
    PodStatus,
)


class KubernetesClient:
    """Kubernetes API 客户端（Mock 实现）。

    当前为 Mock 数据，实际部署时需要替换为真实的 kubernetes-python 客户端。
    """

    def __init__(self, kubeconfig_path: Optional[str] = None):
        """初始化 K8s 客户端。

        Args:
            kubeconfig_path: kubeconfig 文件路径，None 时使用默认路径或 in-cluster 配置
        """
        self.kubeconfig_path = kubeconfig_path
        self._connected = True

    def get_cluster_status(self) -> ExperimentEnvironment:
        """获取集群完整状态。"""
        # Mock 数据 - 实际实现需要调用 K8s API
        pods = self._list_pods()
        nodes = self._group_pods_by_node(pods)
        namespaces = self._list_namespaces()

        failed_pods = [p for p in pods if p.phase in ("Failed", "Unknown") or p.ready.split("/")[0] != p.ready.split("/")[1]]

        return ExperimentEnvironment(
            api_server="https://10.0.0.12:6443",
            kubernetes_version="v1.29.3",
            last_sync=datetime.now(UTC),
            connection_status="连接正常" if self._connected else "连接失败",
            statistics=ClusterStatistics(
                node_count=len(nodes),
                namespace_count=len(namespaces),
                pod_count=len(pods),
                failed_pod_count=len(failed_pods),
            ),
            nodes=nodes,
            namespaces=namespaces,
            components=self._get_components(),
        )

    def _list_pods(self) -> List[PodStatus]:
        """列出所有 Pod（Mock 数据）。"""
        base_time = datetime.now(UTC)

        return [
            PodStatus(
                name="checkout-7d9c8b6f5-x2k9p",
                namespace="commerce",
                node="worker-01",
                phase="Running",
                ready="1/1",
                restarts=0,
                ip="10.244.1.12",
                created_at=base_time,
            ),
            PodStatus(
                name="payment-6b8df4c7d-p7m2q",
                namespace="commerce",
                node="worker-01",
                phase="Running",
                ready="1/1",
                restarts=1,
                ip="10.244.1.18",
                created_at=base_time,
            ),
            PodStatus(
                name="inventory-5cf7db968-i8r4n",
                namespace="inventory",
                node="worker-01",
                phase="Running",
                ready="1/1",
                restarts=0,
                ip="10.244.1.27",
                created_at=base_time,
            ),
            PodStatus(
                name="recommendation-9d6f7b5c-ct3xz",
                namespace="ml",
                node="worker-01",
                phase="Pending",
                ready="0/1",
                restarts=0,
                ip=None,
                created_at=base_time,
            ),
            PodStatus(
                name="reporting-748c96f7-jq1tp",
                namespace="reporting",
                node="worker-01",
                phase="Failed",
                ready="0/1",
                restarts=5,
                ip="10.244.1.33",
                created_at=base_time,
            ),
            # worker-02 pods
            PodStatus(
                name="frontend-6d8f7c5b-f9k2p",
                namespace="commerce",
                node="worker-02",
                phase="Running",
                ready="1/1",
                restarts=0,
                ip="10.244.2.10",
                created_at=base_time,
            ),
            PodStatus(
                name="api-gateway-5c7d9b8f-a4n7q",
                namespace="commerce",
                node="worker-02",
                phase="Running",
                ready="1/1",
                restarts=0,
                ip="10.244.2.15",
                created_at=base_time,
            ),
            # worker-03 pods
            PodStatus(
                name="database-7f8c9d6b-k5m3p",
                namespace="commerce",
                node="worker-03",
                phase="Running",
                ready="1/1",
                restarts=0,
                ip="10.244.3.10",
                created_at=base_time,
            ),
            PodStatus(
                name="cache-redis-6c9d8f7b-n2p4q",
                namespace="commerce",
                node="worker-03",
                phase="Running",
                ready="1/1",
                restarts=0,
                ip="10.244.3.15",
                created_at=base_time,
            ),
        ]

    def _list_namespaces(self) -> List[str]:
        """列出所有命名空间（Mock 数据）。"""
        return [
            "default",
            "kube-system",
            "kube-public",
            "kube-node-lease",
            "commerce",
            "inventory",
            "ml",
            "reporting",
            "monitoring",
            "chaos-engineering",
            "observability",
            "train-ticket",
        ]

    def _group_pods_by_node(self, pods: List[PodStatus]) -> List[NodeInfo]:
        """按节点分组 Pod。"""
        node_map: dict[str, List[PodStatus]] = {}
        for pod in pods:
            if pod.node not in node_map:
                node_map[pod.node] = []
            node_map[pod.node].append(pod)

        nodes = []
        for node_name, node_pods in node_map.items():
            # 判断节点状态（如果有 Failed pod 则为 NotReady，否则为 Ready）
            has_issues = any(p.phase in ("Failed", "Unknown") for p in node_pods)
            status = "NotReady" if has_issues else "Ready"

            nodes.append(
                NodeInfo(
                    name=node_name,
                    status=status,
                    pod_count=len(node_pods),
                    pods=node_pods,
                )
            )

        return sorted(nodes, key=lambda n: n.name)

    def _get_components(self) -> List[ComponentStatus]:
        """获取混沌工程组件状态（Mock 数据）。"""
        return [
            ComponentStatus(
                name="ChaosBlade",
                version="1.8.2",
                running="2 / 2",
                health="运行正常",
            ),
            ComponentStatus(
                name="ChaosBlade Operator",
                version="1.8.2",
                running="1 / 1",
                health="运行正常",
            ),
            ComponentStatus(
                name="Prometheus",
                version="2.48.1",
                running="1 / 1",
                health="运行正常",
            ),
            ComponentStatus(
                name="Jaeger",
                version="1.54.0",
                running="1 / 1",
                health="需要关注",
            ),
            ComponentStatus(
                name="Loki",
                version="2.9.2",
                running="1 / 1",
                health="运行正常",
            ),
        ]
