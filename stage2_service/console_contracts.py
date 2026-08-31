"""Contracts for the Stage-2 disturbance console."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ConsoleModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CaseId(str, Enum):
    C0 = "C0"
    P1 = "P1"
    P2 = "P2"
    D1 = "D1"
    D2 = "D2"
    D3 = "D3"
    D4 = "D4"


class ConsolePhase(str, Enum):
    C1 = "C1"
    C2 = "C2"
    C3 = "C3"
    C4 = "C4"
    C5 = "C5"
    C6 = "C6"


class ConsoleStatus(str, Enum):
    IDLE = "IDLE"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CASE_INVALID = "CASE_INVALID"
    ABORTED = "ABORTED"


class CaseVerdict(str, Enum):
    PENDING = "PENDING"
    PASS = "PASS"
    FAIL = "FAIL"
    CASE_INVALID = "CASE_INVALID"
    SKIPPED = "SKIPPED"


class CaseDefinition(ConsoleModel):
    case_id: CaseId
    title: str
    objective: str
    prompt_delta: str
    disturbance: str
    trigger_phase: ConsolePhase | None
    trigger_event: str | None
    expected_behavior: str
    failure_condition: str
    max_seconds: int = Field(default=300, ge=30, le=300)


class CaseBundle(ConsoleModel):
    schema_version: Literal["stage2-codex-disturbance-bundle.v1"] = (
        "stage2-codex-disturbance-bundle.v1"
    )
    prompt: str = Field(min_length=1, max_length=20000)
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    harness: Literal["codex"] = "codex"
    model: Literal["gpt-5.6-sol"] = "gpt-5.6-sol"
    cases: list[CaseDefinition]

    @model_validator(mode="after")
    def validate_case_ids(self) -> "CaseBundle":
        seen = [case.case_id for case in self.cases]
        if len(seen) != len(set(seen)):
            raise ValueError("case ids must be unique")
        return self


class GenerateBundleRequest(ConsoleModel):
    prompt: str = Field(min_length=1, max_length=20000)


class StartRunRequest(ConsoleModel):
    bundle: CaseBundle
    selected_cases: list[CaseId] = Field(default_factory=lambda: list(CaseId))
    max_seconds_per_trial: int = Field(default=300, ge=30, le=300)

    @model_validator(mode="after")
    def validate_selection(self) -> "StartRunRequest":
        available = {case.case_id for case in self.bundle.cases}
        missing = set(self.selected_cases) - available
        if missing:
            raise ValueError("selected case is not present in bundle")
        return self


class InteractionRequest(ConsoleModel):
    message: str = Field(min_length=1, max_length=4000)


class RuntimeState(ConsoleModel):
    permissions: dict[str, bool] = Field(default_factory=dict)
    pod_name: str | None = None
    pod_uid: str | None = None
    fault_status: Literal["none", "planned", "running", "recovered", "unknown"] = "none"
    observability_status: Literal["available", "revoked", "unknown"] = "available"


class EnvironmentCheck(ConsoleModel):
    component: str
    status: Literal["ok", "warning", "error", "unknown"]
    detail: str
    evidence: dict[str, Any] = Field(default_factory=dict)


class PreflightStatus(ConsoleModel):
    schema_version: Literal["stage2-console-preflight.v1"] = (
        "stage2-console-preflight.v1"
    )
    checked_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    qualified: bool
    checks: list[EnvironmentCheck]


class ConsoleEvent(ConsoleModel):
    sequence: int
    run_id: str
    case_id: CaseId | None = None
    phase: ConsolePhase | None = None
    event_type: str
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    message: str
    payload: dict[str, Any] = Field(default_factory=dict)


class CaseRunSnapshot(ConsoleModel):
    case_id: CaseId
    status: ConsoleStatus
    verdict: CaseVerdict
    current_phase: ConsolePhase | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    runtime: RuntimeState = Field(default_factory=RuntimeState)
    evidence_refs: list[str] = Field(default_factory=list)
    summary: str = ""


class ConsoleRunSnapshot(ConsoleModel):
    schema_version: Literal["stage2-console-run.v1"] = "stage2-console-run.v1"
    run_id: str
    status: ConsoleStatus
    harness: Literal["codex"] = "codex"
    model: Literal["gpt-5.6-sol"] = "gpt-5.6-sol"
    started_at: datetime
    finished_at: datetime | None = None
    selected_cases: list[CaseId]
    cases: list[CaseRunSnapshot]
    runtime: RuntimeState = Field(default_factory=RuntimeState)
    verdict_counts: dict[str, int] = Field(default_factory=dict)
    event_count: int = 0


class EvidenceBundle(ConsoleModel):
    schema_version: Literal["stage2-console-evidence.v1"] = (
        "stage2-console-evidence.v1"
    )
    run: ConsoleRunSnapshot
    events: list[ConsoleEvent]
    preflight: PreflightStatus
    bundle: CaseBundle


class EvidenceItem(ConsoleModel):
    path: str
    kind: str
    size_bytes: int
    created_at: datetime
    summary: str
