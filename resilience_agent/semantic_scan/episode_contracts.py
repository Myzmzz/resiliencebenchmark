"""Internal and agent-visible contracts for one generated benchmark Episode."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .contracts import EvidenceRef, MechanismStep


class EpisodeModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EpisodeVerdict(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    INCONCLUSIVE = "INCONCLUSIVE"
    CASE_INVALID = "CASE_INVALID"


class EpisodeIdentity(EpisodeModel):
    episode_id: str = Field(pattern=r"^EPI-[A-Z0-9-]{8,80}$")
    version: str = Field(pattern=r"^v[0-9]+(?:\.[0-9]+){0,2}$")
    application: str = Field(min_length=1, max_length=120)
    snapshot_id: str = Field(min_length=3, max_length=160)


class DefectBasis(EpisodeModel):
    template_id: str = Field(pattern=r"^RD-[0-9]{2}$")
    defect_name: str
    evidence_description: str
    evidence: list[EvidenceRef] = Field(min_length=1, max_length=80)
    mechanism_chain: list[MechanismStep] = Field(min_length=1, max_length=20)
    supported_fault_types: list[str] = Field(min_length=1, max_length=12)
    injection_component: str = Field(min_length=1, max_length=160)
    confidence: float = Field(ge=0.0, le=1.0)
    confidence_level: str = Field(min_length=3, max_length=40)
    residual_hypotheses: list[str] = Field(default_factory=list, max_length=20)


class RuntimeBinding(EpisodeModel):
    status: Literal["live", "qualified", "fixture"]
    namespace: str = Field(min_length=1, max_length=253)
    component: str = Field(min_length=1, max_length=160)
    pod_name: str = Field(min_length=1, max_length=253)
    pod_uid: str = Field(min_length=8, max_length=160)
    bound_at: datetime
    binding_expiry: list[str] = Field(min_length=1, max_length=12)
    image_identity: str | None = Field(default=None, max_length=500)
    expected_image_identity: str | None = Field(default=None, max_length=500)
    runtime_image_drift: bool = False
    execution_qualified: bool = True

    @model_validator(mode="after")
    def validate_runtime_qualification(self) -> RuntimeBinding:
        if (
            self.image_identity
            and self.expected_image_identity
            and self.image_identity != self.expected_image_identity
        ):
            self.runtime_image_drift = True
        if self.runtime_image_drift:
            self.execution_qualified = False
        return self


class EffectCriterion(EpisodeModel):
    criterion_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,63}$")
    description: str = Field(min_length=8, max_length=800)
    evidence_source: str = Field(min_length=3, max_length=160)


class MainFault(EpisodeModel):
    tool: Literal["ChaosBlade"] = "ChaosBlade"
    fault_type: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,63}$")
    fault_semantics: Literal["persistent", "one_shot"]
    target: dict[str, Any]
    parameters: dict[str, Any]
    duration_seconds: int = Field(ge=600, le=3600)
    duration_semantics: Literal["active_fault_window", "observation_window"]
    command_template: str = Field(min_length=20, max_length=4000)
    cleanup_command: str = Field(min_length=10, max_length=2000)
    effect_verification: list[EffectCriterion] = Field(min_length=1, max_length=12)


class ExecutionPolicy(EpisodeModel):
    max_experiments: int = Field(default=5, ge=1, le=10)
    abort_conditions: list[str] = Field(min_length=1, max_length=20)


class McpAllowlist(EpisodeModel):
    servers: list[str] = Field(default_factory=list, max_length=20)
    tools: list[str] = Field(default_factory=list, max_length=80)


class CodeGraphResource(EpisodeModel):
    graph_ref: str = Field(min_length=3, max_length=500)
    graph_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_snapshot: str = Field(min_length=3, max_length=300)
    entrypoints: list[str] = Field(min_length=1, max_length=50)


class AgentResources(EpisodeModel):
    mcp_allowlist: McpAllowlist
    codegraph: CodeGraphResource


class OracleGate(EpisodeModel):
    gate_id: Literal[
        "target_and_operation_safety",
        "main_fault_effect",
        "fixed_slo_violation",
        "result_integrity",
        "cleanup_and_business_recovery",
    ]
    description: str = Field(min_length=8, max_length=1000)
    required_evidence: list[str] = Field(min_length=1, max_length=12)


class EpisodeOracle(EpisodeModel):
    gates: list[OracleGate] = Field(min_length=5, max_length=5)
    verdicts: list[EpisodeVerdict] = Field(min_length=4, max_length=4)

    @model_validator(mode="after")
    def validate_complete_gate_set(self) -> EpisodeOracle:
        expected = {
            "target_and_operation_safety",
            "main_fault_effect",
            "fixed_slo_violation",
            "result_integrity",
            "cleanup_and_business_recovery",
        }
        if {item.gate_id for item in self.gates} != expected:
            raise ValueError("Episode Oracle must contain every gate exactly once")
        if set(self.verdicts) != set(EpisodeVerdict):
            raise ValueError("Episode Oracle must expose the four registered verdicts")
        return self


class InternalEpisode(EpisodeModel):
    schema_version: Literal["resilience-episode.v2"] = "resilience-episode.v2"
    identity: EpisodeIdentity
    defect_basis: DefectBasis
    runtime_binding: RuntimeBinding
    main_fault: MainFault
    execution_policy: ExecutionPolicy
    agent_resources: AgentResources
    oracle: EpisodeOracle


class PublicActionSpace(EpisodeModel):
    allowed_fault_types: list[str] = Field(min_length=1, max_length=12)
    target_scope: str = Field(min_length=8, max_length=600)
    forbidden_actions: list[str] = Field(min_length=1, max_length=20)


class PublicEpisodeTask(EpisodeModel):
    schema_version: Literal["resilience-episode-public.v1"] = (
        "resilience-episode-public.v1"
    )
    identity: EpisodeIdentity
    title: str = Field(min_length=5, max_length=200)
    objective: str = Field(min_length=20, max_length=2000)
    environment_snapshot: dict[str, Any]
    action_space: PublicActionSpace
    execution_budget: dict[str, Any]
    agent_resources: AgentResources
    expected_output: list[str] = Field(min_length=1, max_length=20)
    safety_constraints: list[str] = Field(min_length=1, max_length=20)


class EpisodeGenerationItem(EpisodeModel):
    finding_id: str
    template_id: str
    component: str | None = None
    fault_type: str | None = None
    episode_id: str | None = None
    internal_ref: str | None = None
    public_ref: str | None = None
    status: Literal[
        "generated",
        "skipped_unactionable",
        "skipped_no_supported_chaosblade_actuator",
        "binding_failed",
        "runtime_drift",
        "blocked",
    ]
    skipped_reason: Literal[
        "none",
        "duplicate_finding",
        "candidate_not_actionable",
        "no_supported_chaosblade_actuator",
        "no_runtime_binding",
        "compile_error",
    ] = "none"
    blockers: list[str] = Field(default_factory=list)


class EpisodeGenerationReport(EpisodeModel):
    schema_version: Literal["episode-generation-report.v1"] = (
        "episode-generation-report.v1"
    )
    source_scan_run_id: str
    generated_at: datetime
    source_match_count: int = Field(ge=0)
    episode_eligible_count: int = Field(ge=0)
    generated_count: int = Field(ge=0)
    materialized_count: int = Field(ge=0)
    skipped_count: int = Field(ge=0)
    no_binding_count: int = Field(ge=0)
    runtime_drift_count: int = Field(ge=0)
    one_to_one_verified: bool
    items: list[EpisodeGenerationItem]
