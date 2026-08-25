"""实验环境数据模型。"""

from datetime import datetime
from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field

PodPhase = Literal["Running", "Pending", "Succeeded", "Failed", "Unknown"]
ComponentHealth = Literal["运行正常", "需要关注", "异常"]


class PodInfo(BaseModel):
    """Pod 信息。"""

    name: str
    namespace: str
    node: str
    phase: PodPhase
    restarts: int
    ready: str  # "1/1" 格式
    ip: Optional[str] = None
    created_at: datetime


class NodeInfo(BaseModel):
    """节点信息。"""

    name: str
    pod_count: int
    status: Literal["Ready", "NotReady"]


class ComponentStatus(BaseModel):
    """资源能力组件状态。"""

    name: str
    version: str
    instances: str  # "2/2" 格式
    health: ComponentHealth


class ClusterSummary(BaseModel):
    """集群统计摘要。"""

    node_count: int
    namespace_count: int
    pod_count: int
    abnormal_pod_count: int


class ExperimentEnvironment(BaseModel):
    """实验环境完整状态。"""

    api_server: str
    k8s_version: str
    last_sync: datetime
    connection_status: Literal["连接正常", "连接失败", "未配置"]
    summary: ClusterSummary
    nodes: List[NodeInfo] = Field(default_factory=list)
    pods: List[PodInfo] = Field(default_factory=list)
    components: List[ComponentStatus] = Field(default_factory=list)
