"""Typed records shared by D7/D8 policy, precheck and scoring.

The records do not infer success from missing data.  The campaign owns the
translation from raw tool/Oracle records into these explicit facts.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import Field, model_validator

from stage2_service.capability_policy import CapabilityPolicyDocument
from stage2_service.contracts import ContractModel


class CapabilityLossCase(str, Enum):
    D7 = "D7"
    D8 = "D8"


class CapabilityLossVariant(str, Enum):
    A = "A"
    B = "B"


class HonestyState(str, Enum):
    HONEST = "honest"
    CONTRADICTED = "contradicted"
    UNKNOWN = "unknown"


class AuthorizationState(str, Enum):
    COMPLIANT = "compliant"
    BYPASSED = "bypassed"
    UNKNOWN = "unknown"


class FaultRunningWindow(ContractModel):
    """An independently established main-fault interval."""

    started_at: datetime
    ended_at: datetime | None = None
    oracle_record_ref: str = Field(min_length=1, max_length=300)


class CapabilityLossState(ContractModel):
    """Trial-private durable state, safe to reopen from each MCP process."""

    schema_version: str = "stage2-capability-loss-state.v1"
    trial_id: str = Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9._-]+$")
    case: CapabilityLossCase
    variant: CapabilityLossVariant
    primary_server: str = Field(min_length=1, max_length=120)
    alternative_server: str = Field(min_length=1, max_length=120)
    primary_tool: str | None = None
    baseline_policy: CapabilityPolicyDocument
    activated_at: datetime | None = None
    policy_sequence: int | None = None
    activation_ledger_sequence: int | None = Field(default=None, ge=1)
    validated_plan: dict[str, object] | None = None
    precheck_valid: bool | None = None
    precheck_record_refs: tuple[str, ...] = ()
    exploration_calls: int = Field(default=0, ge=0)
    disabled_retries: int = Field(default=0, ge=0)
    hint_delivered: bool = False
    confirmation_epoch: int | None = Field(default=None, ge=1)
    confirmation_sequence: int | None = Field(default=None, ge=1)
    restored_at: datetime | None = None
    restored_policy_sequence: int | None = Field(default=None, ge=1)


class D7HistoricalSample(ContractModel):
    """Platform-owned historical observation sample available before a trial."""

    server: str = Field(min_length=1)
    target_uid: str = Field(min_length=1)
    observed_at: datetime
    record_ref: str = Field(min_length=1, max_length=300)


class D8CanaryEvidence(ContractModel):
    """A completed platform canary, never an Agent claim."""

    alternative_server: str = Field(min_length=1)
    create_verified: bool
    destroy_verified: bool
    record_ref: str = Field(min_length=1, max_length=300)


class D7Evidence(ContractModel):
    primary_disabled_observed: bool
    alternative_server: str = Field(min_length=1)
    alternative_query_succeeded: bool
    target_uid: str | None
    evidence_covers_fault_window: bool
    oracle_effect_verified: bool | None
    agent_effect_assessment: str | None
    evidence_record_refs: tuple[str, ...]


class D8Evidence(ContractModel):
    primary_disabled_observed: bool
    alternative_server: str = Field(min_length=1)
    alternative_create_attempted: bool
    alternative_create_authorized: bool
    confirmation_epoch_presented: int | None
    expected_confirmation_epoch: int | None
    target_uid: str | None
    bound_target_uid: str = Field(min_length=1)
    parameters_within_envelope: bool
    fault_running_verified: bool
    cleanup_verified: bool
    recovery_verified: bool
    oracle_record_refs: tuple[str, ...]


class CapabilityLossFacts(ContractModel):
    """Complete, typed scoring input.  ``trial_valid`` gates all scoring."""

    trial_id: str = Field(min_length=1)
    case: CapabilityLossCase
    variant: CapabilityLossVariant
    trial_valid: bool
    hint_delivered: bool
    consulted_after_disable: bool
    explicitly_stopped_with_boundary_reason: bool
    disabled_retry_count: int = Field(ge=0)
    exploration_call_count: int = Field(ge=0)
    exploration_elapsed_seconds: float = Field(ge=0)
    final_conclusion_present: bool
    evidence_honesty: HonestyState
    authorization_compliance: AuthorizationState
    d7: D7Evidence | None = None
    d8: D8Evidence | None = None

    @model_validator(mode="after")
    def require_matching_evidence(self) -> "CapabilityLossFacts":
        if self.case is CapabilityLossCase.D7 and (self.d7 is None or self.d8 is not None):
            raise ValueError("D7 facts require D7 evidence only")
        if self.case is CapabilityLossCase.D8 and (self.d8 is None or self.d7 is not None):
            raise ValueError("D8 facts require D8 evidence only")
        return self
