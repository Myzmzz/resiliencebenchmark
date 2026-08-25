"""Versioned contracts for one end-to-end benchmark run.

The public command deliberately contains references and policy identifiers only.
Runtime credentials, kubeconfig paths, shell commands, and Ground Truth are
resolved by the trusted control plane and cannot be supplied through this model.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

IDENTIFIER_PATTERN = r"^[a-z0-9][a-z0-9._-]{1,127}$"
ALIAS_PATTERN = r"^[a-z][a-z0-9_-]{0,31}$"


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RunMode(str, Enum):
    DRY_RUN = "dry_run"
    EXECUTE = "execute"


class AnalysisMode(str, Enum):
    DETERMINISTIC = "deterministic"
    MODEL = "model"


class RunPhase(str, Enum):
    CREATED = "CREATED"
    SCANNING = "SCANNING"
    MATCHING = "MATCHING"
    DESIGNING = "DESIGNING"
    QUALIFYING = "QUALIFYING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    BASELINING = "BASELINING"
    EXECUTING = "EXECUTING"
    RECOVERING = "RECOVERING"
    EVALUATING = "EVALUATING"
    SCORING = "SCORING"
    CLEANING_UP = "CLEANING_UP"


class RunTerminalStatus(str, Enum):
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    RESET_FAILED = "RESET_FAILED"
    ABORTED = "ABORTED"
    CASE_INVALID = "CASE_INVALID"
    NO_EXECUTABLE_EPISODE = "NO_EXECUTABLE_EPISODE"


ALLOWED_PHASE_TRANSITIONS: dict[RunPhase, frozenset[RunPhase]] = {
    RunPhase.CREATED: frozenset({RunPhase.SCANNING}),
    RunPhase.SCANNING: frozenset({RunPhase.MATCHING}),
    RunPhase.MATCHING: frozenset({RunPhase.DESIGNING}),
    RunPhase.DESIGNING: frozenset({RunPhase.QUALIFYING}),
    RunPhase.QUALIFYING: frozenset(
        {RunPhase.AWAITING_APPROVAL, RunPhase.BASELINING}
    ),
    RunPhase.AWAITING_APPROVAL: frozenset({RunPhase.BASELINING}),
    RunPhase.BASELINING: frozenset({RunPhase.EXECUTING}),
    RunPhase.EXECUTING: frozenset({RunPhase.RECOVERING}),
    RunPhase.RECOVERING: frozenset({RunPhase.EVALUATING}),
    RunPhase.EVALUATING: frozenset({RunPhase.SCORING}),
    RunPhase.SCORING: frozenset(),
    RunPhase.CLEANING_UP: frozenset(),
}


class ScanScope(ContractModel):
    application: str = Field(pattern=IDENTIFIER_PATTERN)
    namespace: str = Field(pattern=IDENTIFIER_PATTERN)
    source_lock_id: str = Field(pattern=IDENTIFIER_PATTERN)
    evidence_roots: list[str] = Field(default_factory=list, max_length=12)
    include_live_runtime: bool = True

    @model_validator(mode="after")
    def validate_evidence_roots(self) -> ScanScope:
        if len(set(self.evidence_roots)) != len(self.evidence_roots):
            raise ValueError("evidence_roots must be unique")
        import re

        invalid = [item for item in self.evidence_roots if not re.fullmatch(ALIAS_PATTERN, item)]
        if invalid:
            raise ValueError("evidence_roots must contain approved aliases, not paths")
        return self


class HarnessSelection(ContractModel):
    harness_id: str = Field(pattern=IDENTIFIER_PATTERN)
    model_alias: str = Field(pattern=IDENTIFIER_PATTERN)
    track: Literal["native", "bladeai-model"]


class ProgressionPolicy(ContractModel):
    profile_id: str = Field(pattern=IDENTIFIER_PATTERN)
    max_levels: int = Field(default=3, ge=1, le=8)
    retry_budget_per_level: int = Field(default=1, ge=0, le=3)
    total_retry_budget: int = Field(default=3, ge=0, le=24)

    @model_validator(mode="after")
    def validate_total_budget(self) -> ProgressionPolicy:
        minimum = self.max_levels * self.retry_budget_per_level
        if self.total_retry_budget < minimum:
            raise ValueError(
                "total_retry_budget must cover retry_budget_per_level for every level"
            )
        return self


class ScoringPolicy(ContractModel):
    policy_id: str = Field(default="episode-score-v1", pattern=IDENTIFIER_PATTERN)
    allow_provisional_efficiency: bool = True


class RunSpec(ContractModel):
    schema_version: Literal["run-spec.v1"] = "run-spec.v1"
    request_id: str = Field(pattern=IDENTIFIER_PATTERN)
    requester: str = Field(pattern=IDENTIFIER_PATTERN)
    mode: RunMode = RunMode.DRY_RUN
    analysis_mode: AnalysisMode = AnalysisMode.MODEL
    scan: ScanScope
    harness: HarnessSelection
    progression: ProgressionPolicy = Field(default_factory=ProgressionPolicy)
    scoring: ScoringPolicy = Field(default_factory=ScoringPolicy)
    auto_approve: bool = False


class RunEvent(ContractModel):
    schema_version: Literal["run-event.v1"] = "run-event.v1"
    event_id: str
    run_id: str
    sequence: int = Field(ge=1)
    event_type: str
    phase: RunPhase
    terminal_status: RunTerminalStatus | None = None
    occurred_at: datetime
    detail: dict[str, Any] = Field(default_factory=dict)


class RunRecord(ContractModel):
    schema_version: Literal["run-state.v1"] = "run-state.v1"
    run_id: str
    spec: RunSpec
    spec_sha256: str
    phase: RunPhase
    terminal_status: RunTerminalStatus | None = None
    desired_terminal_status: RunTerminalStatus | None = None
    abort_requested: bool = False
    revision: int = Field(ge=0)
    created_at: datetime
    updated_at: datetime
    last_error: str | None = None

    @property
    def is_terminal(self) -> bool:
        return self.terminal_status is not None


class MutationLease(ContractModel):
    run_id: str
    acquired_at: datetime
    heartbeat_at: datetime
    expires_at: datetime


class WorkerLease(ContractModel):
    run_id: str
    worker_id: str
    acquired_at: datetime
    heartbeat_at: datetime
    expires_at: datetime
