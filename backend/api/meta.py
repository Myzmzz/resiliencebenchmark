"""元信息端点：健康检查与仓库有效性。"""

from fastapi import APIRouter
from pydantic import BaseModel

from backend.config import get_settings

router = APIRouter(prefix="/meta", tags=["meta"])

SERVICE_VERSION = "0.1.0"


class RepoCheck(BaseModel):
    """被解析仓库的可用性检查结果。"""

    path: str
    exists: bool
    factory_config_found: bool


class HealthResponse(BaseModel):
    """服务健康状态。仓库无效不代表服务不健康，用标志位表达。"""

    service: str
    version: str
    repo: RepoCheck


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    settings = get_settings()
    return HealthResponse(
        service="ok",
        version=SERVICE_VERSION,
        repo=RepoCheck(
            path=str(settings.repo_path),
            exists=settings.repo_path.is_dir(),
            factory_config_found=settings.factory_config_path.is_file(),
        ),
    )
