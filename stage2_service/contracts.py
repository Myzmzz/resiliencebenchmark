"""Strict contracts for the Stage-2 monolithic service."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


IDENTIFIER = r"^[a-z0-9][a-z0-9._-]{1,127}$"
MODEL_ALIAS = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
ModelAlias = Annotated[str, Field(pattern=MODEL_ALIAS)]


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class HarnessKind(str, Enum):
    CODEX = "codex"
    CLAUDE_CODE = "claude-code"
    DEEPSEEK = "deepseek-harness"
    BLADEAI = "bladeai"


class McpServerName(str, Enum):
    K8S_RO = "k8s_ro"
    TELEMETRY_RO = "telemetry_ro"
    SOURCE_RO = "source_ro"
    CHAOS_CONTROL = "chaos_control"


class ModelProtocol(str, Enum):
    OPENAI_RESPONSES = "openai_responses"
    OPENAI_CHAT_COMPLETIONS = "openai_chat_completions"
    ANTHROPIC_MESSAGES = "anthropic_messages"


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


class HarnessModelSelection(ContractModel):
    harness: HarnessKind
    models: tuple[ModelAlias, ...] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def validate_models(self) -> HarnessModelSelection:
        if len(set(self.models)) != len(self.models):
            raise ValueError("models must be unique within one Harness selection")
        return self


class BladeAINativePermissions(ContractModel):
    """Static native permissions exposed only to a BladeAI Trial."""

    kubernetes_read: bool = True
    kubernetes_metrics: bool = True
    chaosblade_execute: bool = True


class PermissionProfile(ContractModel):
    """Static tool surface applied before every Trial process starts."""

    schema_version: Literal["stage2-permission-profile.v1"] = (
        "stage2-permission-profile.v1"
    )
    profile_id: str = Field(
        default="p0-full-authorized",
        pattern=r"^[a-z0-9][a-z0-9._-]{1,127}$",
    )
    mcp_servers: tuple[McpServerName, ...] = (
        McpServerName.K8S_RO,
        McpServerName.TELEMETRY_RO,
        McpServerName.SOURCE_RO,
        McpServerName.CHAOS_CONTROL,
    )
    bladeai_native: BladeAINativePermissions = Field(
        default_factory=BladeAINativePermissions
    )

    @model_validator(mode="after")
    def validate_mcp_servers(self) -> PermissionProfile:
        if len(set(self.mcp_servers)) != len(self.mcp_servers):
            raise ValueError("mcp_servers must be unique")
        return self


class ModelIdentity(ContractModel):
    requested_alias: ModelAlias
    upstream_model: ModelAlias
    provider_protocol: ModelProtocol
    registry_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    provider_reported_model: str | None = None
    verification: Literal[
        "registry_frozen", "provider_verified", "provider_mismatch"
    ] = "registry_frozen"


class CampaignRequest(ContractModel):
    schema_version: Literal["stage2-campaign-request.v2"] = (
        "stage2-campaign-request.v2"
    )
    request_id: str = Field(pattern=IDENTIFIER)
    episode: FixedEpisodeRef
    model_matrix: tuple[HarnessModelSelection, ...] = Field(
        min_length=1, max_length=128
    )
    permission_profile: PermissionProfile = Field(default_factory=PermissionProfile)
    trial_kinds: tuple[TrialKind, ...] = (
        TrialKind.CONTROL,
        TrialKind.TARGET_CHANGE,
        TrialKind.PERMISSION_CHANGE,
    )
    cluster_name: Literal["kubernetes"] = "kubernetes"
    application_namespace: Literal["otel-demo"] = "otel-demo"
    control_namespace: Literal["resiliencebenchmark-system"] = (
        "resiliencebenchmark-system"
    )

    @model_validator(mode="after")
    def validate_harness_matrix(self) -> CampaignRequest:
        harnesses = [selection.harness for selection in self.model_matrix]
        if len(set(harnesses)) != len(harnesses):
            raise ValueError("Harness selections must be unique in the model matrix")
        if not self.trial_kinds:
            raise ValueError("trial_kinds must not be empty")
        if len(set(self.trial_kinds)) != len(self.trial_kinds):
            raise ValueError("trial_kinds must be unique")
        if self.trial_kinds[0] is not TrialKind.CONTROL:
            raise ValueError("trial_kinds must begin with control")
        return self


class KubernetesRule(ContractModel):
    api_group: str
    resource: str
    verbs: tuple[str, ...]
    namespace: str | None = None


class CapabilityProfile(ContractModel):
    schema_version: Literal["stage2-capability-profile.v2"] = (
        "stage2-capability-profile.v2"
    )
    permission_profile_id: str
    harness: HarnessKind
    mcp_servers: tuple[McpServerName, ...]
    mcp_tools: tuple[str, ...]
    kubernetes_rules: tuple[KubernetesRule, ...]
    direct_kubeconfig: bool
    native_permissions: BladeAINativePermissions
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
    backend: Literal[
        "kubernetes", "mcp_policy", "kubernetes_rbac", "composite"
    ]
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
    provider_reported_model: str | None = None
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
    permission_profile_id: str
    model_identity: ModelIdentity
    kind: TrialKind
    runtime_target: RuntimeTarget
    platform_valid: bool
    diagnostic_only: bool
    agent_verdict: AgentVerdict
    disturbances: tuple[DisturbanceRecord, ...]
    recovery: RecoveryResult
    artifact_refs: tuple[str, ...]


class ModelEvaluationSummary(ContractModel):
    harness: HarnessKind
    requested_alias: ModelAlias
    upstream_model: ModelAlias
    provider_protocol: ModelProtocol
    registry_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    total_trials: int = Field(ge=0)
    valid_trials: int = Field(ge=0)
    pass_count: int = Field(ge=0)
    fail_count: int = Field(ge=0)
    inconclusive_count: int = Field(ge=0)
    case_invalid_count: int = Field(ge=0)
    pass_rate: float | None = Field(default=None, ge=0, le=1)


class CampaignResult(ContractModel):
    schema_version: Literal["stage2-campaign-result.v1"] = (
        "stage2-campaign-result.v1"
    )
    campaign_id: str
    request_id: str
    platform_status: PlatformStatus
    trials: tuple[TrialResult, ...]
    model_summaries: tuple[ModelEvaluationSummary, ...] = ()
    started_at: datetime
    finished_at: datetime
    error: str | None = None
