"""Strict contracts for the Stage-2 monolithic service."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


IDENTIFIER = r"^[a-z0-9][a-z0-9._-]{1,127}$"


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class HarnessKind(str, Enum):
    CODEX = "codex"
    CLAUDE_CODE = "claude-code"
    DEEPSEEK = "deepseek-harness"
    BLADEAI = "bladeai"


class LifecyclePhase(str, Enum):
    C1_PLAN = "C1_PLAN"
    C2_TARGET = "C2_TARGET"
    C3_INJECT = "C3_INJECT"
    C4_EFFECT = "C4_EFFECT"
    C5_SAFETY = "C5_SAFETY"
    C6_RECOVERY = "C6_RECOVERY"


class TrialKind(str, Enum):
    CONTROL = "control"
    TARGET_CHANGE = "target_change"
    PERMISSION_CHANGE = "permission_change"


class DisturbanceType(str, Enum):
    TARGET_CHANGE = "target_change"
    PERMISSION_CHANGE = "permission_change"


class AgentVerdict(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    INCONCLUSIVE = "INCONCLUSIVE"
    CASE_INVALID = "CASE_INVALID"


class PlatformStatus(str, Enum):
    COMPLETED = "COMPLETED"
    BLOCKED = "BLOCKED"
    RESET_FAILED = "RESET_FAILED"
    FAILED = "FAILED"


class FixedEpisodeRef(ContractModel):
    internal_path: str
    public_path: str
    episode_id: str = Field(pattern=r"^EPI-[A-Z0-9-]{8,80}$")
    internal_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    public_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class CampaignRequest(ContractModel):
    schema_version: Literal["stage2-campaign-request.v1"] = (
        "stage2-campaign-request.v1"
    )
    request_id: str = Field(pattern=IDENTIFIER)
    episode: FixedEpisodeRef
    harnesses: tuple[HarnessKind, ...] = (
        HarnessKind.CODEX,
        HarnessKind.CLAUDE_CODE,
        HarnessKind.DEEPSEEK,
        HarnessKind.BLADEAI,
    )
    model_by_harness: dict[HarnessKind, str]
    cluster_name: Literal["kubernetes"] = "kubernetes"
    application_namespace: Literal["otel-demo"] = "otel-demo"
    control_namespace: Literal["resiliencebenchmark-system"] = (
        "resiliencebenchmark-system"
    )

    @model_validator(mode="after")
    def validate_harness_matrix(self) -> CampaignRequest:
        if len(set(self.harnesses)) != len(self.harnesses):
            raise ValueError("campaign harnesses must be unique")
        missing = set(self.harnesses) - set(self.model_by_harness)
        if missing:
            raise ValueError("every harness requires one frozen model alias")
        return self


class KubernetesRule(ContractModel):
    api_group: str
    resource: str
    verbs: tuple[str, ...]
    namespace: str | None = None


class CapabilityProfile(ContractModel):
    schema_version: Literal["stage2-capability-profile.v1"] = (
        "stage2-capability-profile.v1"
    )
    harness: HarnessKind
    mcp_servers: tuple[str, ...]
    mcp_tools: tuple[str, ...]
    kubernetes_rules: tuple[KubernetesRule, ...]
    direct_kubeconfig: bool
    allowed_fault_types: tuple[str, ...]
    expires_at: datetime


class RuntimeTarget(ContractModel):
    namespace: str
    component: str
    kind: Literal["Pod"] = "Pod"
    name: str
    uid: str


class TrialRuntimeContext(ContractModel):
    schema_version: Literal["stage2-trial-runtime-context.v1"] = (
        "stage2-trial-runtime-context.v1"
    )
    trial_id: str
    episode_id: str
    target: RuntimeTarget
    main_fault: dict[str, Any]
    cleanup_handle: str = Field(pattern=r"^cleanup-[a-f0-9]{36}$")
    baseline_capability: str = Field(min_length=32)
    issued_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class LifecycleEvent(ContractModel):
    schema_version: Literal["stage2-lifecycle-event.v1"] = (
        "stage2-lifecycle-event.v1"
    )
    event_id: str
    campaign_id: str
    trial_id: str
    harness: HarnessKind
    phase: LifecyclePhase
    kind: str
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    payload: dict[str, Any] = Field(default_factory=dict)


class DisturbancePlan(ContractModel):
    schema_version: Literal["stage2-disturbance-plan.v1"] = (
        "stage2-disturbance-plan.v1"
    )
    disturbance_id: str
    trial_id: str
    type: DisturbanceType
    phase: LifecyclePhase
    trigger_event_id: str
    committed_dependency: str
    backend: Literal["kubernetes", "mcp_policy", "kubernetes_rbac"]
    parameters: dict[str, Any]
    expected_behaviors: tuple[str, ...]
    failure_conditions: tuple[str, ...]
    rollback: dict[str, Any]


class DisturbanceRecord(ContractModel):
    plan: DisturbancePlan
    applied: bool
    application_evidence: dict[str, Any]
    rolled_back: bool = False
    rollback_evidence: dict[str, Any] = Field(default_factory=dict)


class HarnessReport(ContractModel):
    status: Literal["completed", "failed", "timeout"]
    agent_verdict: AgentVerdict
    lifecycle_events: tuple[LifecycleEvent, ...]
    artifact_refs: tuple[str, ...] = ()
    final_output: dict[str, Any] = Field(default_factory=dict)


class RecoveryResult(ContractModel):
    agent_attempted: bool
    agent_recovery_verified: bool
    controller_cleanup_verified: bool
    fault_absent: bool
    business_recovery_verified: bool
    main_fault_ever_active: bool = False
    main_fault_target_verified: bool = False
    fault_effect_verified: bool = False
    fault_effect_evidence: dict[str, Any] = Field(default_factory=dict)
    evidence_refs: tuple[str, ...] = ()


class TrialResult(ContractModel):
    trial_id: str
    harness: HarnessKind
    kind: TrialKind
    runtime_target: RuntimeTarget
    platform_valid: bool
    diagnostic_only: bool
    agent_verdict: AgentVerdict
    disturbances: tuple[DisturbanceRecord, ...]
    recovery: RecoveryResult
    artifact_refs: tuple[str, ...]


class CampaignResult(ContractModel):
    schema_version: Literal["stage2-campaign-result.v1"] = (
        "stage2-campaign-result.v1"
    )
    campaign_id: str
    request_id: str
    platform_status: PlatformStatus
    trials: tuple[TrialResult, ...]
    started_at: datetime
    finished_at: datetime
    error: str | None = None
