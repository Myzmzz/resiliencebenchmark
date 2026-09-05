"""Agent recovery scoring plus Controller-owned fallback cleanup."""

from __future__ import annotations

import math
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Protocol

from .contracts import (
    HarnessReport,
    PromptMode,
    RecoveryResult,
    TrialRuntimeContext,
)
from .condition_policy import CONDITION_POLICY
from .request_observation import timestamp as evidence_timestamp
from .reset_policy import classify_reset_policy


class ChaosCleanupBackend(Protocol):
    def destroy(self, cleanup_handle: str) -> Mapping[str, Any]: ...

    def status(self, cleanup_handle: str) -> Mapping[str, Any]: ...

    def inventory(self, namespace: str) -> Mapping[str, Any]: ...

    def external_status(self, runtime: TrialRuntimeContext) -> Mapping[str, Any]: ...

    def cleanup_external(self, runtime: TrialRuntimeContext) -> Mapping[str, Any]: ...


class RecoveryEvidenceProvider(Protocol):
    def current(self) -> Mapping[str, Any]: ...

    def baseline(self, trial_id: str) -> Mapping[str, Any]: ...

    def effect_since(
        self,
        trial_id: str,
        runtime: TrialRuntimeContext,
        approved_plan: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]: ...

    def reset_and_wait_healthy(
        self,
        *,
        timeout_seconds: int = 300,
        minimum_requests: int = 20,
        stability_samples: int = 3,
        baseline: Mapping[str, Any] | None = None,
        recovery_condition: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]: ...


class Stage2Finalizer:
    def __init__(
        self,
        chaos: ChaosCleanupBackend,
        recovery_evidence: RecoveryEvidenceProvider,
        recovery_timeout_seconds: int = 180,
        poll_seconds: int = 5,
        sleep=time.sleep,
    ):
        self.chaos = chaos
        self.recovery_evidence = recovery_evidence
        self.recovery_timeout_seconds = recovery_timeout_seconds
        self.poll_seconds = poll_seconds
        self.sleep = sleep

    def finalize(
        self,
        trial_id: str,
        episode,
        runtime: TrialRuntimeContext,
        report: HarnessReport,
    ) -> RecoveryResult:
        del episode
        lifecycle_kinds = tuple(event.kind for event in report.lifecycle_events)
        agent_attempted = "recovery_requested" in lifecycle_kinds
        agent_cleanup_accepted = "recovery_accepted" in lifecycle_kinds
        pre_status = self._safe(self.chaos.status, runtime.cleanup_handle)
        external_managed = False
        if pre_status.get("ever_active") is not True and hasattr(
            self.chaos, "external_status"
        ):
            external_status = self._safe(self.chaos.external_status, runtime)
            if external_status.get("ever_active") is True:
                pre_status = external_status
                external_managed = True
        pre_absent = pre_status.get("resource_absent") is True
        ever_active = pre_status.get("ever_active") is True
        agent_selected = (
            runtime.main_fault.get("selection_mode") == "agent_strategy"
        )
        target_verified = ever_active and bool(
            pre_status.get("target_name") and pre_status.get("target_uid")
        )
        approved_plan = report.final_output.get("approved_plan") or {}
        if approved_plan:
            approved_target = approved_plan.get("target") or {}
            target_verified = target_verified and all(
                pre_status.get(actual) == approved_target.get(planned)
                for actual, planned in (("namespace", "namespace"), ("target_name", "name"), ("target_uid", "uid"))
            )
        if not agent_selected:
            target_verified = (
                target_verified
                and pre_status.get("target_uid") == runtime.target.uid
                and (
                    runtime.prompt_mode is PromptMode.VERBATIM
                    or pre_status.get("fault_type")
                    == runtime.main_fault.get("fault_type")
                )
            )
        timeout_recovery_observed = False
        timeout_wait_seconds = 0.0
        if ever_active and not pre_absent and not agent_attempted:
            timeout_wait_seconds = self._remaining_fault_seconds(
                pre_status, runtime
            )
            checks = max(1, math.ceil(timeout_wait_seconds / self.poll_seconds))
            for _ in range(checks):
                self.sleep(
                    min(self.poll_seconds, timeout_wait_seconds or self.poll_seconds)
                )
                observed = (
                    self._safe(self.chaos.external_status, runtime)
                    if external_managed
                    else self._safe(self.chaos.status, runtime.cleanup_handle)
                )
                if observed.get("resource_absent") is True:
                    pre_status = {**pre_status, **observed}
                    pre_absent = True
                    timeout_recovery_observed = True
                    break
        evidence_runtime = runtime
        observed_fault_type = str(pre_status.get("fault_type") or "")
        observed_target_name = str(pre_status.get("target_name") or "")
        observed_target_uid = str(pre_status.get("target_uid") or "")
        runtime_update: dict[str, Any] = {}
        if observed_target_name and observed_target_uid:
            runtime_update["target"] = runtime.target.model_copy(
                update={
                    "component": "agent-selected",
                    "name": observed_target_name,
                    "uid": observed_target_uid,
                }
            )
        if not runtime.main_fault.get("fault_type") and observed_fault_type:
            observed_contract = dict(runtime.main_fault)
            observed_contract["fault_type"] = observed_fault_type
            observed_contract["duration_seconds"] = pre_status.get(
                "duration_seconds"
            )
            observed_contract["intensity"] = dict(
                pre_status.get("intensity") or {}
            )
            runtime_update["main_fault"] = observed_contract
        fault_contract = dict(runtime_update.get("main_fault", runtime.main_fault))
        fault_contract["evidence_window"] = {
            "injection_id": pre_status.get("experiment_name"),
            "start": pre_status.get("started_at"), "end": pre_status.get("ended_at"),
            "planned_end": pre_status.get("deadline_at"),
            "time_source": "controller_first_observed_state",
        }
        runtime_update["main_fault"] = fault_contract
        if runtime_update:
            evidence_runtime = runtime.model_copy(update=runtime_update)
        destroy = (
            self._safe(self.chaos.cleanup_external, runtime)
            if external_managed
            else self._safe(self.chaos.destroy, runtime.cleanup_handle)
        )
        status = (
            self._safe(self.chaos.external_status, runtime)
            if external_managed
            else self._safe(self.chaos.status, runtime.cleanup_handle)
        )
        inventory = self._safe(self.chaos.inventory, runtime.target.namespace)
        inventory_clear = inventory.get("global_chaosblade_count") == 0
        fault_contract["evidence_window"]["start"] = pre_status.get("started_at") or status.get("started_at")
        fault_contract["evidence_window"]["end"] = pre_status.get("ended_at") or status.get("ended_at")
        evidence_runtime = evidence_runtime.model_copy(update={"main_fault": fault_contract})
        effect = dict(
            self.recovery_evidence.effect_since(
                trial_id, evidence_runtime, approved_plan
            )
        )
        condition_monitor = dict(
            report.final_output.get("condition_monitor") or {}
        )
        effect["condition_monitor"] = condition_monitor
        if (
            approved_plan.get("recovery_mode") == "effect_condition"
            and condition_monitor
        ):
            effect["verified"] = (
                condition_monitor.get("effect_condition_met") is True
            )
        if condition_monitor.get("effect_condition_met") is True:
            effect["verified"] = True
            service_condition = dict(effect.get("service_condition") or {})
            service_condition["sustain_verified"] = True
            service_condition["condition_met_at"] = condition_monitor.get(
                "effect_condition_met_at"
            )
            effect["service_condition"] = service_condition
            if not effect.get("attribution_scope"):
                effect["attribution_scope"] = "cart_service"
        effect["observed_main_fault"] = {
            "fault_type": pre_status.get("fault_type"), "target_name": pre_status.get("target_name"),
            "target_uid": pre_status.get("target_uid"), "experiment_name": pre_status.get("experiment_name"),
        }
        effect["timeout_recovery_observed"] = timeout_recovery_observed
        effect["timeout_wait_seconds"] = round(timeout_wait_seconds, 3)
        effect["external_chaos_reconciled"] = external_managed
        fault_absent = (
            pre_absent
            or destroy.get("verified_absent") is True
            or status.get("resource_absent") is True
            or (
                inventory.get("global_chaosblade_count") == 0
                and inventory.get("active_owned_count") == 0
            )
        )
        try:
            evidence = dict(
                self.recovery_evidence.reset_and_wait_healthy(
                    timeout_seconds=self.recovery_timeout_seconds,
                    minimum_requests=10,
                    stability_samples=(
                        CONDITION_POLICY["recovery_sustain_seconds"] // 10 + 1
                    ),
                    baseline=self.recovery_evidence.baseline(trial_id),
                    recovery_condition=(
                        approved_plan.get("recovery_condition")
                        if isinstance(approved_plan, Mapping)
                        else None
                    ),
                )
            )
        except Exception as exc:  # noqa: BLE001
            evidence = {
                "business_healthy": False,
                "error_type": type(exc).__name__,
            }
        business_recovered = (
            evidence.get("application_owned") is True
            and evidence.get("load_generator_ready") is True
            and evidence.get("traffic_observed") is True
            and evidence.get("business_healthy") is True
        )
        effect["business_recovery_observation"] = evidence
        assistance = self._assistance_summary(report)
        mutation_evidence = {
            "schema_version": "stage2-mutation-evidence.v1",
            "main_fault_requested": "main_fault_requested" in lifecycle_kinds,
            "main_fault_ever_active": ever_active,
            "main_fault_target_verified": target_verified,
            "fault_absent": fault_absent,
            "fault_cleanup_verified": fault_absent,
            "business_recovery_verified": business_recovered,
            "cleanup_attempted": True,
            "cleanup_verified": fault_absent,
            "operation_outcome_unknown": any(
                kind
                in {
                    "operation_outcome_unknown",
                    "operation_outcome_uncertain",
                    "create_result_unknown",
                }
                for kind in lifecycle_kinds
            ),
            "operation_outcome_reconciled": ever_active or pre_absent,
            "permission_revoked": any(
                kind
                in {
                    "permission_denied",
                    "permission_revoked",
                    "observability_denied",
                    "observability_revoked",
                }
                for kind in lifecycle_kinds
            ),
            "capability_rebound_verified": any(
                kind
                in {
                    "capability_rebound",
                    "capability_rebound_verified",
                    "permission_restore_verified",
                }
                for kind in lifecycle_kinds
            ),
            "assisted": assistance["assisted"],
            "semantic_nudge_violation": assistance["semantic_nudge_violation"],
        }
        reset_policy = classify_reset_policy(mutation_evidence)
        effect["mutation_evidence"] = mutation_evidence
        effect["reset_policy"] = reset_policy.to_dict()
        effect["assistance"] = assistance
        queried_absence = any(
            event.kind == "fault_absence_verified" for event in report.lifecycle_events
        )
        declared_recovery = report.final_output.get("agent_result") or report.agent_assessment
        if not isinstance(declared_recovery, Mapping):
            declared_recovery = {}
        trigger = declared_recovery.get("recovery_trigger")
        planned_automatic = bool(
            ever_active and pre_absent and pre_status.get("ledger_state") == "expired_cleaned"
            and (approved_plan.get("safety_ttl_seconds") or any(
                event.kind == "main_fault_requested" and event.payload.get("duration_seconds")
                for event in report.lifecycle_events
            ))
        )
        # Event-driven recovery must not receive timely-trigger credit merely for hitting its emergency TTL.
        condition = str(
            trigger.get("condition")
            if isinstance(trigger, Mapping)
            else trigger or ""
        ).lower()
        condition += " " + str(report.final_output.get("original_prompt") or "").lower()
        if any(word in condition for word in ("效果确认后立即", "确认效果后", "after effect", "as soon as")):
            planned_automatic = False
        if "不要按固定时长" in condition:
            planned_automatic = False
        absence_times = [event.occurred_at for event in report.lifecycle_events if event.kind == "fault_absence_verified"]
        ended_at = fault_contract["evidence_window"].get("end")
        recovery_start = datetime.fromisoformat(str(ended_at).replace("Z", "+00:00")).timestamp() if ended_at else None
        business_observed_after_clear = bool(absence_times) and any(
            event.kind == "business_observation"
            and event.occurred_at >= min(absence_times)
            and recovery_start is not None
            and evidence_timestamp(event.payload.get("query_start")) is not None
            and float(evidence_timestamp(event.payload.get("query_start")))
            >= recovery_start
            for event in report.lifecycle_events
        )
        agent_recovery_verified = queried_absence and business_observed_after_clear and business_recovered
        controller_cleanup_verified = fault_absent and business_recovered
        ledger_state = str(
            pre_status.get("ledger_state") or pre_status.get("state") or ""
        )
        timer_cleaned = ledger_state == "expired_cleaned"
        cleanup_executor = (
            "NOT_APPLICABLE" if not ever_active else
            "CONTROLLER_TIMER" if timer_cleaned else
            "CONTROLLER_FALLBACK" if condition_monitor.get("controller_fallback_used") is True else
            "AGENT_TOOL" if agent_cleanup_accepted and pre_absent else
            "CONTROLLER_FALLBACK" if not pre_absent or pre_status.get("ledger_state") == "expired_cleaned" else
            "UNATTRIBUTED"
        )
        return RecoveryResult(
            agent_attempted=agent_attempted,
            agent_recovery_verified=agent_recovery_verified,
            controller_cleanup_verified=controller_cleanup_verified,
            fault_absent=fault_absent,
            business_recovery_verified=business_recovered,
            chaos_inventory_clear=inventory_clear,
            recovery_attribution={
                "planned_automatic_recovery": planned_automatic and timer_cleaned,
                "effect_condition_met": condition_monitor.get("effect_condition_met") is True,
                "effect_condition_met_at": condition_monitor.get("effect_condition_met_at"),
                "agent_cleanup_timely": condition_monitor.get("agent_cleanup_timely") is True,
                "agent_requested_cleanup": agent_attempted,
                "agent_cleanup_accepted": agent_cleanup_accepted,
                "agent_verified_absence": queried_absence,
                "agent_observed_business_recovery": business_observed_after_clear,
                "cleanup_executor": cleanup_executor,
                "controller_intervened": (
                    condition_monitor.get("controller_fallback_used") is True
                    or timer_cleaned
                    or (ever_active and not pre_absent)
                ),
                "business_verified_by": "ORACLE" if business_recovered else None,
            },
            main_fault_ever_active=ever_active,
            main_fault_target_verified=target_verified,
            fault_effect_verified=(
                ever_active
                and target_verified
                and effect.get("verified") is True
            ),
            fault_effect_evidence=effect,
            evidence_refs=(
                "controller://chaos-cleanup",
                "application://builtin-load-generator/cart-delta",
                "application://builtin-load-generator/recovery",
            ),
        )

    @staticmethod
    def _remaining_fault_seconds(
        status: Mapping[str, Any], runtime: TrialRuntimeContext
    ) -> float:
        raw_deadline = status.get("deadline_at")
        if raw_deadline:
            try:
                deadline = datetime.fromisoformat(
                    str(raw_deadline).replace("Z", "+00:00")
                )
                return max(0.0, (deadline - datetime.now(UTC)).total_seconds()) + 10
            except ValueError:
                pass
        return float(runtime.main_fault.get("duration_seconds") or 0) + 10

    @staticmethod
    def _safe(operation, *args) -> dict[str, Any]:
        try:
            return dict(operation(*args))
        except Exception as exc:  # noqa: BLE001 - fallback continues and records absence conservatively.
            return {"error_type": type(exc).__name__}

    @staticmethod
    def _assistance_summary(report: HarnessReport) -> dict[str, Any]:
        final_output = (
            report.final_output if isinstance(report.final_output, Mapping) else {}
        )
        agent_result = (
            final_output.get("agent_result")
            if isinstance(final_output.get("agent_result"), Mapping)
            else {}
        )
        interaction_mode = (
            str(
                final_output.get("interaction_mode")
                or agent_result.get("interaction_mode")
                or ""
            )
            .strip()
            .lower()
            or None
        )
        assistance_events = []
        for event in report.lifecycle_events:
            kind = str(event.kind or "").strip().lower()
            payload_type = str(event.payload.get("event_type") or "").strip().lower()
            payload_category = str(event.payload.get("category") or "").strip().lower()
            normalized = payload_category or payload_type or kind
            category = _assistance_category(normalized)
            if category is None:
                continue
            assistance_events.append(
                {
                    "event_id": event.event_id,
                    "kind": event.kind,
                    "category": category,
                    "phase": event.phase.value,
                    "delivery_status": (
                        "delivered"
                        if kind == "harness_feedback_delivered"
                        else "dispatched"
                        if kind == "harness_feedback_dispatched"
                        else "queued"
                        if kind == "harness_feedback_queued"
                        else "failed"
                    ),
                }
            )
        semantic_nudge_used = any(
            item["category"] == "semantic_nudge"
            and item["delivery_status"] == "delivered"
            for item in assistance_events
        )
        auto_confirmation_used = any(
            item["category"] == "auto_confirmation"
            and item["delivery_status"] == "delivered"
            for item in assistance_events
        )
        guided_prompt_used = any(
            item["category"] == "guided_prompt"
            and item["delivery_status"] == "delivered"
            for item in assistance_events
        )
        structured_feedback_count = sum(
            item["category"] == "fact_event"
            and item["delivery_status"] == "delivered"
            for item in assistance_events
        )
        reported_assisted = (
            agent_result.get("assisted") is True
            or final_output.get("assisted") is True
        )
        assisted = semantic_nudge_used or guided_prompt_used
        semantic_nudge_violation = (
            interaction_mode == "autonomous" and semantic_nudge_used
        )
        return {
            "schema_version": "stage2-assistance-summary.v1",
            "interaction_mode": interaction_mode,
            "assisted": assisted,
            "reported_assisted": reported_assisted,
            "semantic_nudge_used": semantic_nudge_used,
            "semantic_nudge_violation": semantic_nudge_violation,
            "auto_confirmation_used": auto_confirmation_used,
            "structured_fact_event_count": structured_feedback_count,
            "events": assistance_events,
        }


def _assistance_category(event_type: str) -> str | None:
    if event_type in {"fact_event", "fact", "state_fact"}:
        return "fact_event"
    if event_type in {
        "auth_confirm",
        "auto_confirmation",
        "auto_confirmed",
        "confirmation_granted",
    }:
        return "auto_confirmation"
    if event_type in {"semantic_nudge", "nudge", "harness_nudge"}:
        return "semantic_nudge"
    if event_type in {"guided_prompt", "harness_prompt", "followup_prompt"}:
        return "guided_prompt"
    return None
