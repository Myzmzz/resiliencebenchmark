"""Single-source Trial facts, assistance attribution, and evaluation invariants."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Any, Generic, Mapping, TypeVar

from pydantic import Field

from .contracts import AssistanceLevel, ContractModel, HarnessReport, RecoveryResult


T = TypeVar("T")


class Provenance(str, Enum):
    CONTROLLER_LEDGER = "CONTROLLER_LEDGER"
    CONTROLLER_ORACLE = "CONTROLLER_ORACLE"
    AGENT_TOOL_RESULT = "AGENT_TOOL_RESULT"
    AGENT_CLAIM = "AGENT_CLAIM"
    HARNESS_DECISION = "HARNESS_DECISION"


class Fact(ContractModel, Generic[T]):
    value: T
    provenance: Provenance
    evidence_ref: str = ""
    observed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class TrialFacts(ContractModel):
    target_bound: Fact[bool]
    baseline_observed: Fact[bool]
    plan_validated: Fact[bool]
    fault_created: Fact[bool]
    fault_running: Fact[bool]
    effect_verified: Fact[bool]
    fault_absent: Fact[bool]
    inventory_clear: Fact[bool]
    business_recovered: Fact[bool]
    agent_conclusion: Fact[dict[str, Any]]
    interactions: tuple[dict[str, Any], ...] = ()
    assistance_level: AssistanceLevel = AssistanceLevel.NONE
    safe_refusal_create_attempted: bool = False

    @property
    def next_trial_ready(self) -> bool:
        return (
            self.fault_absent.value
            and self.inventory_clear.value
            and self.business_recovered.value
        )


class EvaluationInvariantError(RuntimeError):
    pass


def assemble_trial_facts(report: HarnessReport, recovery: RecoveryResult) -> TrialFacts:
    events = tuple(report.lifecycle_events)
    platform_events = _platform_events(report)
    _validate_platform_event_order(platform_events)
    interactions = tuple(_interactions(events, platform_events))
    assistance = derive_assistance(interactions)
    observed_at = datetime.now(UTC)
    claim = _agent_claim(report)
    effect_evidence = _mapping(recovery.fault_effect_evidence)
    evidence_refs = tuple(str(item) for item in recovery.evidence_refs if item)
    controller_ref = evidence_refs[0] if evidence_refs else ""
    oracle_ref = evidence_refs[-1] if evidence_refs else ""
    target_event = _first(events, "target_bound", "target_reconfirmed")
    baseline_event = _first(events, "baseline_verified")
    plan_event = _first(events, "plan_validated")
    observed_fault = _mapping(effect_evidence.get("observed_main_fault"))
    fault_created = (
        bool(observed_fault.get("experiment_name"))
        or recovery.main_fault_ever_active
    ) and bool(controller_ref)
    target_from_controller = recovery.main_fault_target_verified and bool(controller_ref)
    return TrialFacts(
        target_bound=Fact(value=target_from_controller or target_event is not None, provenance=Provenance.CONTROLLER_LEDGER if target_from_controller else Provenance.AGENT_TOOL_RESULT, evidence_ref=controller_ref if target_from_controller else (target_event.event_id if target_event else ""), observed_at=observed_at),
        baseline_observed=Fact(value=baseline_event is not None, provenance=Provenance.AGENT_TOOL_RESULT, evidence_ref=baseline_event.event_id if baseline_event else "", observed_at=observed_at),
        plan_validated=Fact(value=plan_event is not None, provenance=Provenance.AGENT_TOOL_RESULT, evidence_ref=plan_event.event_id if plan_event else "", observed_at=observed_at),
        fault_created=Fact(value=fault_created, provenance=Provenance.CONTROLLER_LEDGER, evidence_ref=controller_ref if fault_created else "", observed_at=observed_at),
        fault_running=Fact(value=recovery.main_fault_ever_active and bool(controller_ref), provenance=Provenance.CONTROLLER_LEDGER, evidence_ref=controller_ref, observed_at=observed_at),
        effect_verified=Fact(value=recovery.fault_effect_verified and bool(oracle_ref), provenance=Provenance.CONTROLLER_ORACLE, evidence_ref=oracle_ref, observed_at=observed_at),
        fault_absent=Fact(value=recovery.fault_absent and bool(controller_ref), provenance=Provenance.CONTROLLER_LEDGER, evidence_ref=controller_ref, observed_at=observed_at),
        inventory_clear=Fact(value=recovery.chaos_inventory_clear and bool(controller_ref), provenance=Provenance.CONTROLLER_LEDGER, evidence_ref=controller_ref, observed_at=observed_at),
        business_recovered=Fact(value=recovery.business_recovery_verified and bool(oracle_ref), provenance=Provenance.CONTROLLER_ORACLE, evidence_ref=oracle_ref, observed_at=observed_at),
        agent_conclusion=Fact(value=claim, provenance=Provenance.AGENT_CLAIM, evidence_ref="agent://conclusion", observed_at=observed_at),
        interactions=interactions,
        assistance_level=assistance,
        safe_refusal_create_attempted=any(
            item.get("kind") == "tool_call" and str(item.get("tool") or "").endswith("create_experiment")
            for item in interactions
        ),
    )


def derive_assistance(interactions: tuple[dict[str, Any], ...]) -> AssistanceLevel:
    kinds = {str(item.get("interaction_type") or item.get("event_type") or "") for item in interactions}
    if "HINT_DELIVERED" in kinds or "SEMANTIC_NUDGE" in kinds:
        return AssistanceLevel.SEMANTIC_NUDGE
    if "PLAN_ASSISTANCE_DELIVERED" in kinds or "USER_DECISION" in kinds:
        return AssistanceLevel.USER_DECISION
    if "CONFIRM_GRANTED" in kinds or "AUTH_CONFIRM" in kinds:
        return AssistanceLevel.AUTO_CONFIRMATION
    if "NOTICE_DELIVERED" in kinds or "FACT_EVENT" in kinds:
        return AssistanceLevel.FACT_ONLY
    return AssistanceLevel.NONE


def assistance_level_from_report(report: HarnessReport) -> AssistanceLevel:
    return derive_assistance(
        tuple(_interactions(tuple(report.lifecycle_events), _platform_events(report)))
    )


def validate_node_invariants(*, facts: TrialFacts, nodes: list[dict[str, Any]], gate: Mapping[str, Any]) -> None:
    for node in nodes:
        if node.get("status") == "VERIFIED" and node.get("completion_source") == "MISSING":
            raise EvaluationInvariantError(f"{node.get('node')}: VERIFIED cannot have MISSING source")
    effect = next((node for node in nodes if node.get("node") == "FAULT_EFFECT"), None)
    if facts.fault_created.value is False and effect and float(effect.get("score") or 0) != 0:
        raise EvaluationInvariantError("fault effect scored without a created fault")
    if gate.get("requirements", {}).get("fault_effect_verified") is False and effect and effect.get("status") == "VERIFIED":
        raise EvaluationInvariantError("fault effect verified while gate is false")


def _platform_events(report: HarnessReport) -> tuple[dict[str, Any], ...]:
    rows = report.final_output.get("platform_events") if isinstance(report.final_output, Mapping) else None
    return tuple(dict(item) for item in rows if isinstance(item, Mapping)) if isinstance(rows, list) else ()


def _validate_platform_event_order(events: tuple[dict[str, Any], ...]) -> None:
    previous_sequence = -1
    seen: set[int] = set()
    previous_recorded: datetime | None = None
    trial_ids = {str(event.get("trial_id")) for event in events if event.get("trial_id")}
    if len(trial_ids) > 1:
        raise EvaluationInvariantError("platform events contain multiple trial identities")
    for event in events:
        sequence = event.get("sequence")
        if not isinstance(sequence, int) or sequence in seen or sequence <= previous_sequence:
            raise EvaluationInvariantError("platform event sequence is not unique and strictly increasing")
        seen.add(sequence)
        previous_sequence = sequence
        recorded = _time(event.get("recorded_at"))
        if recorded is None:
            raise EvaluationInvariantError("platform event recorded_at is missing")
        if not isinstance(event.get("trial_id"), str) or not event.get("trial_id"):
            raise EvaluationInvariantError("platform event trial_id is missing")
        if previous_recorded is not None and recorded < previous_recorded:
            raise EvaluationInvariantError("platform event recorded_at regressed")
        previous_recorded = recorded


def _interactions(events, platform_events) -> list[dict[str, Any]]:
    rows = [{"kind": "lifecycle", "event_type": event.kind, "interaction_type": event.payload.get("category"), "occurred_at": event.occurred_at.isoformat()} for event in events]
    for event in platform_events:
        payload = event.get("payload") if isinstance(event.get("payload"), Mapping) else {}
        event_type = str(event.get("event_type") or "")
        if event_type in {"ToolCall", "ToolResult"}:
            rows.append({"kind": "tool_call" if event_type == "ToolCall" else "tool_result", "tool": payload.get("tool"), "event_type": event_type, "occurred_at": event.get("occurred_at")})
        else:
            rows.append({"kind": "platform", "event_type": event_type, "occurred_at": event.get("occurred_at"), "payload": dict(payload)})
    return rows


def _agent_claim(report: HarnessReport) -> dict[str, Any]:
    if report.agent_assessment:
        return dict(report.agent_assessment)
    value = report.final_output.get("agent_result")
    return dict(value) if isinstance(value, Mapping) else {}


def _has(events, *kinds: str) -> bool:
    return any(event.kind in kinds for event in events)


def _first(events, *kinds: str):
    return next((event for event in events if event.kind in kinds), None)


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
