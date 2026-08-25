"""Application environment data models."""

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class NamespaceConfig(BaseModel):
    """Namespace configuration."""

    template: str
    live_reference: Optional[str] = Field(None, alias="liveReference")
    lifecycle: str


class ImageInfo(BaseModel):
    """Image lock information."""

    component: str
    repository: str
    tag: str
    digest: str


class CriticalPath(BaseModel):
    """Critical path definition."""

    id: str
    description: str
    synthetic_load_profile: Optional[str] = Field(None, alias="syntheticLoadProfile")


class SLO(BaseModel):
    """SLO definition."""

    id: str
    query_ref: str = Field(alias="queryRef")
    objective: str
    window: str


class ReadinessGap(BaseModel):
    """Known readiness gap."""

    observed_at: Optional[Any] = Field(None, alias="observedAt")
    severity: str
    item: str


class ReadinessInfo(BaseModel):
    """Readiness information."""

    current_status: str = Field(alias="currentStatus")
    known_gaps: List[ReadinessGap] = Field(default_factory=list, alias="knownGaps")
    resolved_issues: Optional[List[Dict[str, str]]] = Field(
        None, alias="resolvedIssues"
    )
    next_checks: Optional[List[str]] = Field(None, alias="nextChecks")


class ApplicationDetails(BaseModel):
    """Detailed application configuration."""

    source_snapshot: Optional[Dict[str, Any]] = Field(None, alias="sourceSnapshot")
    image_lock: Optional[Dict[str, Any]] = Field(None, alias="imageLock")
    workloads: Optional[Dict[str, Any]] = None
    slos: Optional[List[SLO]] = None
    observability: Optional[Dict[str, Any]] = None
    reset_contract: Optional[Dict[str, Any]] = Field(None, alias="resetContract")
    qualify_contract: Optional[Dict[str, Any]] = Field(None, alias="qualifyContract")
    readiness: Optional[ReadinessInfo] = None


class Application(BaseModel):
    """Application environment resource."""

    name: str
    display_name: str = Field(alias="displayName")
    benchmark_role: str = Field(alias="benchmarkRole")
    visibility: str
    namespace: NamespaceConfig
    image_count: int = Field(alias="imageCount")
    image_policy: str = Field(alias="imagePolicy")
    critical_paths_count: int = Field(alias="criticalPathsCount")
    slo_count: int = Field(alias="sloCount")
    status: str  # "qualified" | "partial" | "pending" | "inactive"
    readiness_status: str = Field(alias="readinessStatus")
    known_gaps: List[ReadinessGap] = Field(default_factory=list, alias="knownGaps")
    details: Optional[ApplicationDetails] = None

    class Config:
        populate_by_name = True
