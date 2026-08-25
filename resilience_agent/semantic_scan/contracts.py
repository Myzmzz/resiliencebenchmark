"""Versioned contracts for semantic template matching and verification."""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DClass(str, Enum):
    D1 = "D1"
    D2 = "D2"
    D3 = "D3"
    D4 = "D4"
    D5 = "D5"
    D6 = "D6"
    UNCLASSIFIED = "UNCLASSIFIED"


class EvidenceKind(str, Enum):
    CODEGRAPH_NODE = "codegraph_node"
    CODEGRAPH_EDGE = "codegraph_edge"
    CODEGRAPH_CONTEXT = "codegraph_context"
    KUBERNETES_MANIFEST = "kubernetes_manifest"
    KUBERNETES_LIVE = "kubernetes_live"


class EvidenceRef(ContractModel):
    evidence_id: str = Field(pattern=r"^EV-[A-F0-9]{12}$")
    kind: EvidenceKind
    statement: str = Field(min_length=8, max_length=1200)
    path: str | None = Field(default=None, max_length=600)
    start_line: int | None = Field(default=None, ge=1)
    end_line: int | None = Field(default=None, ge=1)
    symbol: str | None = Field(default=None, max_length=300)
    relation: str | None = Field(default=None, max_length=120)
    source_hash: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def validate_lines(self) -> EvidenceRef:
        if (self.start_line is None) != (self.end_line is None):
            raise ValueError("start_line and end_line must be supplied together")
        if self.start_line is not None and self.end_line < self.start_line:
            raise ValueError("end_line must be greater than or equal to start_line")
        return self


class MechanismStep(ContractModel):
    order: int = Field(ge=1, le=20)
    cause: str = Field(min_length=3, max_length=500)
    relation: str = Field(min_length=2, max_length=160)
    effect: str = Field(min_length=3, max_length=500)
    evidence_ids: list[str] = Field(min_length=1, max_length=12)


class AlternativeCheck(ContractModel):
    hypothesis: str = Field(min_length=3, max_length=600)
    status: Literal["excluded", "present", "unresolved"]
    evidence_ids: list[str] = Field(default_factory=list, max_length=12)
    explanation: str = Field(min_length=3, max_length=1000)


class FaultType(ContractModel):
    fault_type: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,63}$")
    actuator: str = Field(min_length=3, max_length=160)
    rationale: str = Field(min_length=8, max_length=800)


class FaultInjectionTarget(ContractModel):
    component: str = Field(min_length=1, max_length=160)
    resource_kind: str = Field(min_length=1, max_length=80)
    resource_name: str | None = Field(default=None, max_length=253)
    namespace: str | None = Field(default=None, max_length=253)
    dependency: str | None = Field(default=None, max_length=253)
    selection_basis: str = Field(min_length=8, max_length=1000)


class ResidualHypothesis(ContractModel):
    hypothesis: str = Field(min_length=3, max_length=600)
    distinguishing_fault_type: str | None = Field(default=None, max_length=80)
    oracle_signal: str = Field(min_length=3, max_length=800)
    evidence_ids: list[str] = Field(default_factory=list, max_length=12)


class TemplateFinding(ContractModel):
    finding_id: str = Field(pattern=r"^RD-(?:0[1-9]|1[0-4])-F[0-9]{2,4}$")
    proposed_d_class: DClass
    evidence_explanation: str = Field(min_length=8, max_length=4000)
    evidence_ids: list[str] = Field(default_factory=list, max_length=40)
    mechanism_chain: list[MechanismStep] = Field(default_factory=list, max_length=20)
    available_fault_types: list[FaultType] = Field(default_factory=list, max_length=12)
    fault_injection_target: FaultInjectionTarget | None = None
    alternatives_checked: list[AlternativeCheck] = Field(default_factory=list, max_length=20)
    residual_hypotheses: list[ResidualHypothesis] = Field(default_factory=list, max_length=20)
    unresolved_questions: list[str] = Field(default_factory=list, max_length=20)
    locatable: bool = False
    injectable: bool = False
    oracle_observable: bool = False
    cleanup_available: bool = False
    confidence_claim: float = Field(ge=0.0, le=1.0)

    @model_validator(mode="after")
    def validate_candidate_shape(self) -> TemplateFinding:
        if not self.evidence_ids or not self.mechanism_chain:
            raise ValueError("finding requires evidence and a mechanism chain")
        if self.injectable and (
            not self.available_fault_types or self.fault_injection_target is None
        ):
            raise ValueError("injectable finding requires faults and an injection target")
        return self


class TemplateAgentOutput(ContractModel):
    """Structured response produced by one template-specialized subagent."""

    schema_version: Literal["template-agent-output.v1"] = "template-agent-output.v1"
    template_id: str = Field(pattern=r"^RD-(?:0[1-9]|1[0-4])$")
    defect_name: str = Field(min_length=3, max_length=200)
    scan_status: Literal["not_found", "candidate", "insufficient_evidence", "scan_failed"]
    findings: list[TemplateFinding] = Field(default_factory=list, max_length=20)
    explanation: str = Field(min_length=8, max_length=4000)

    @model_validator(mode="after")
    def validate_match_shape(self) -> TemplateAgentOutput:
        if self.scan_status == "candidate" and not self.findings:
            raise ValueError("candidate output requires at least one finding")
        if self.scan_status != "candidate" and self.findings:
            raise ValueError("non-candidate output must not contain findings")
        return self


class VerificationDecision(ContractModel):
    schema_version: Literal["verification-decision.v1"] = "verification-decision.v1"
    template_id: str = Field(pattern=r"^RD-(?:0[1-9]|1[0-4])$")
    finding_id: str = Field(pattern=r"^RD-(?:0[1-9]|1[0-4])-F[0-9]{2,4}$")
    verdict: Literal["confirmed", "rejected", "inconclusive"]
    evidence_reproducible: bool
    mechanism_static_support: Literal["strong", "partial", "weak"]
    safeguards_excluded: bool
    target_supported: bool
    fault_is_discriminating: bool
    cleanup_supported: bool
    verified_evidence_ids: list[str] = Field(default_factory=list, max_length=40)
    invalid_evidence_ids: list[str] = Field(default_factory=list, max_length=40)
    residual_hypotheses: list[ResidualHypothesis] = Field(default_factory=list, max_length=20)
    issues: list[str] = Field(default_factory=list, max_length=30)
    explanation: str = Field(min_length=8, max_length=3000)


class TemplateMatch(ContractModel):
    """Fixed final output requested by the benchmark control plane."""

    schema_version: Literal["resilience-template-match.v1"] = (
        "resilience-template-match.v1"
    )
    template_id: str = Field(pattern=r"^RD-(?:0[1-9]|1[0-4])$")
    finding_id: str = Field(pattern=r"^RD-(?:0[1-9]|1[0-4])-F[0-9]{2,4}$")
    defect_name: str
    d_class: DClass
    evidence_explanation: str
    evidence: list[EvidenceRef]
    mechanism_chain: list[MechanismStep]
    available_fault_types: list[FaultType]
    fault_injection_target: FaultInjectionTarget | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    confidence_level: Literal["low", "medium", "high"]
    candidate_status: Literal[
        "confirmed_candidate", "plausible_candidate", "unactionable_candidate"
    ]
    verifier_status: Literal["confirmed", "inconclusive", "rejected"]
    question_eligible: bool
    alternatives_checked: list[AlternativeCheck]
    residual_hypotheses: list[ResidualHypothesis] = Field(default_factory=list)
    provenance: dict[str, Any]


class TemplateCoverage(ContractModel):
    template_id: str = Field(pattern=r"^RD-(?:0[1-9]|1[0-4])$")
    status: Literal[
        "matched",
        "not_found",
        "not_matched",
        "insufficient_evidence",
        "scan_failed",
        "rejected",
    ]
    explanation: str = Field(min_length=3, max_length=1500)


class CodeGraphManifest(ContractModel):
    codegraph_version: str
    codebase_path: str
    source_identity: str
    initialized: bool
    file_count: int = Field(ge=0)
    node_count: int = Field(ge=0)
    edge_count: int = Field(ge=0)
    languages: list[str]
    index_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    status: dict[str, Any]
    parse_failures: list[dict[str, Any]] = Field(default_factory=list)
    coverage: dict[str, Any] = Field(default_factory=dict)


class KubernetesManifest(ContractModel):
    mode: Literal["live", "manifest"] = "manifest"
    source_paths: list[str]
    resource_count: int = Field(ge=0)
    kinds: dict[str, int]
    manifest_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    completeness: dict[str, Any] = Field(default_factory=dict)
    authoritative_for_namespace: bool = False


class SemanticScanReport(ContractModel):
    schema_version: Literal["semantic-resilience-scan.v1"] = (
        "semantic-resilience-scan.v1"
    )
    run_id: str
    generated_at: str
    codegraph: CodeGraphManifest
    kubernetes: KubernetesManifest
    template_registry_version: str
    model: dict[str, Any]
    matches: list[TemplateMatch]
    coverage: list[TemplateCoverage]
    question_eligible_count: int = Field(ge=0)
    scan_coverage: dict[str, Any] = Field(default_factory=dict)
    tool_traces: list[dict[str, Any]] = Field(default_factory=list)
    limitations: list[str]


class TemplatePlan(ContractModel):
    template_id: str = Field(pattern=r"^RD-(?:0[1-9]|1[0-4])$")
    codegraph_focus: list[str] = Field(min_length=1, max_length=12)
    kubernetes_focus: list[str] = Field(default_factory=list, max_length=12)
    priority: Literal["low", "medium", "high"]
    rationale: str = Field(min_length=8, max_length=800)


class CoordinatorPlan(ContractModel):
    schema_version: Literal["semantic-scan-plan.v1"] = "semantic-scan-plan.v1"
    plans: list[TemplatePlan] = Field(min_length=1, max_length=12)

    @model_validator(mode="after")
    def validate_templates(self) -> CoordinatorPlan:
        allowed = {
            "RD-01",
            "RD-02",
            "RD-05",
            "RD-06",
            "RD-07",
            "RD-08",
            "RD-09",
            "RD-10",
            "RD-11",
            "RD-12",
            "RD-13",
            "RD-14",
        }
        observed = [item.template_id for item in self.plans]
        if len(observed) != len(set(observed)) or not set(observed) <= allowed:
            raise ValueError("coordinator plan contains duplicate or inactive templates")
        return self
