"""实验环境 API 端点。"""

from fastapi import APIRouter

from backend.models.experiment import ExperimentEnvironment
from backend.services.kubernetes import get_experiment_environment

router = APIRouter(prefix="/experiments", tags=["实验环境"])


@router.get("/environment", response_model=ExperimentEnvironment)
def get_environment() -> ExperimentEnvironment:
    """
    获取实验环境状态。

    返回 Kubernetes 集群的完整状态，包括：
    - 集群基本信息（API Server、版本）
    - 资源统计（节点、命名空间、Pod）
    - 节点列表
    - Pod 列表
    - 混沌工程组件状态

    如果没有配置 KUBECONFIG，返回模拟数据用于开发测试。
    """
    return get_experiment_environment()
