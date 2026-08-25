"""基础设施资源数据模型。"""

from datetime import datetime
from typing import Any, Dict, Literal, Optional

from pydantic import BaseModel, Field

ResourceType = Literal["kubernetes", "ssh_host", "registry", "model_gateway"]
ResourceStatus = Literal["qualified", "partial", "pending", "error"]


class InfrastructureResource(BaseModel):
    """单个基础设施资源的状态快照。"""

    type: ResourceType
    name: str
    status: ResourceStatus
    endpoint: str
    metrics: Dict[str, int] = Field(default_factory=dict)
    last_qualified: Optional[datetime] = None
    details: Optional[Any] = None
