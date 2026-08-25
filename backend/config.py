"""后端全局配置。"""

import os
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, Field


class Settings(BaseModel):
    """应用配置,从环境变量读取。"""

    repo_path: Path = Field(
        default_factory=lambda: Path(
            os.getenv(
                "BENCHMARK_REPO_PATH",
                # backend/ 位于仓库根目录之下,默认即所在仓库
                str(Path(__file__).resolve().parent.parent),
            )
        )
    )

    @property
    def factory_config_path(self) -> Path:
        """benchmarkfactory.yaml 的完整路径。"""
        return self.repo_path / "benchmarkfactory.yaml"


@lru_cache
def get_settings() -> Settings:
    """单例模式获取配置。"""
    return Settings()
