"""基础设施资源 API 端点。"""

from fastapi import APIRouter, Depends

from backend.config import Settings, get_settings
from backend.models.infrastructure import InfrastructureResource
from backend.parsers.environment_status import parse_environment_status

router = APIRouter(prefix="/infrastructure", tags=["infrastructure"])


@router.get("/resources", response_model=list[InfrastructureResource])
def get_infrastructure_resources(
    settings: Settings = Depends(get_settings),
) -> list[InfrastructureResource]:
    """
    获取基础设施资源列表。

    从 ENVIRONMENT_STATUS.md 解析 K8s 集群、SSH 主机、镜像仓库和模型网关。
    如果文件不存在，返回空列表。
    """
    env_status_path = settings.repo_path / "ENVIRONMENT_STATUS.md"
    return parse_environment_status(env_status_path)
