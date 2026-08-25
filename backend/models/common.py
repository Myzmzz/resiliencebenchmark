"""所有资源型 API 的统一响应封装。

设计文档 §5/§9：仓库大量内容处于 notYetClaimed 状态，"没有数据"是
常态而非异常；单文件损坏不应导致 500。
"""

from datetime import UTC, datetime
from typing import Generic, Literal, Optional, TypeVar

from pydantic import BaseModel, Field

ResourceStatus = Literal["ok", "not_ready", "parse_error"]

T = TypeVar("T")


class ResourceEnvelope(BaseModel, Generic[T]):
    """资源三态封装：数据、来源文件与解析时间一并返回。"""

    status: ResourceStatus
    data: Optional[T] = None
    source_files: list[str] = Field(default_factory=list)
    parsed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    error: Optional[str] = None

    class Config:
        arbitrary_types_allowed = True

    @classmethod
    def ok(cls, data: T, source_files: list[str]) -> "ResourceEnvelope[T]":
        return cls(status="ok", data=data, source_files=source_files)

    @classmethod
    def not_ready(cls, source_files: list[str]) -> "ResourceEnvelope[T]":
        return cls(status="not_ready", source_files=source_files)

    @classmethod
    def parse_error(cls, error: str, source_files: list[str]) -> "ResourceEnvelope[T]":
        return cls(status="parse_error", error=error, source_files=source_files)
