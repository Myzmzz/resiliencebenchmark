"""Episode (evaluation unit) data models."""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class EpisodeApplication(BaseModel):
    """Episode application configuration."""

    name: str
    namespace: str
    candidate_services: List[str] = Field(alias="candidate_services")
    release_ref: str = Field(alias="release_ref")


class HealthPrerequisites(BaseModel):
    """Health prerequisites configuration."""

    prerequisites: List[str]


class EnvironmentSnapshot(BaseModel):
    """Environment snapshot configuration."""

    snapshot_id: str = Field(alias="snapshot_id")
    health_prerequisites: Optional[List[str]] = Field(
        None, alias="health_prerequisites"
    )
    reset_contract: Optional[List[str]] = Field(None, alias="reset_contract")


class WorkloadConfig(BaseModel):
    """Workload configuration."""

    profile: str
    slo: List[str]


class ObservabilityConfig(BaseModel):
    """Observability configuration."""

    metrics: List[str]
    traces: List[str]
    logs: List[str]
    kubernetes: List[str]


class SourceAccess(BaseModel):
    """Source access configuration."""

    mode: str
    allowed_paths: List[str] = Field(alias="allowed_paths")
    forbidden_paths: List[str] = Field(alias="forbidden_paths")


class ActionSpace(BaseModel):
    """Action space configuration."""

    allowed_trigger_classes: List[str] = Field(alias="allowed_trigger_classes")
    allowed_target_scope: List[str] = Field(alias="allowed_target_scope")
    forbidden_actions: List[str] = Field(alias="forbidden_actions")


class BudgetConfig(BaseModel):
    """Budget configuration."""

    max_experiments: int = Field(alias="max_experiments")
    max_duration_minutes: int = Field(alias="max_duration_minutes")
    max_concurrent_faults: int = Field(alias="max_concurrent_faults")


class Episode(BaseModel):
    """Episode (evaluation unit) configuration."""

    schema_version: str = Field(alias="schema_version")
    episode_id: str = Field(alias="episode_id")
    title: str
    status: str
    application: EpisodeApplication
    agent_goal: str = Field(alias="agent_goal")
    environment_snapshot: EnvironmentSnapshot = Field(alias="environment_snapshot")
    workload: WorkloadConfig
    observability: ObservabilityConfig
    source_access: SourceAccess = Field(alias="source_access")
    action_space: ActionSpace = Field(alias="action_space")
    budget: BudgetConfig
    safety_constraints: List[str] = Field(alias="safety_constraints")
    expected_agent_output: List[str] = Field(alias="expected_agent_output")
    leakage_controls: List[str] = Field(alias="leakage_controls")

    class Config:
        populate_by_name = True
