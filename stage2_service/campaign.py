"""Autonomous Campaign engine for one fixed Episode and four native Harnesses."""

from __future__ import annotations

import inspect
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import uuid4

from controller.safety import (
    ChaosBladeAction,
    TargetIdentity,
    default_policy,
    validate_action,
)

from .artifacts import ArtifactStore
from .contracts import (
    AgentOutcome,
    AgentVerdict,
    AssistanceLevel,
    CampaignRequest,
    CampaignResult,
    CaseSpec,
    CapabilityProfile,
    DisturbanceRecord,
    DisturbanceType,
    ExperimentVerdict,
    FeedbackCategory,
    HarnessKind,
    HarnessReport,
    LifecycleEvent,
    InteractionMode,
    NextTrialReadiness,
    PlatformStatus,
    PromptMode,
    RecoveryResult,
    RecoveryStatus,
    Stage2CaseId,
    TrialKind,
    TrialPlatformStatus,
    TrialValidity,
    TrialResult,
    TrialRuntimeContext,
    default_case_specs,
)
from .disturbance import DisturbanceExecutor, RuntimeDisturbancePlanner
from .episode import LoadedEpisode
from .qualification import D0QualificationGate
from .reporting import build_evaluation_summary, build_trial_report


EventObserver = Callable[[Any], Any]


class EnvironmentGate(Protocol):
    def qualify(self, episode: LoadedEpisode) -> Mapping[str, Any]: ...


class PermissionManager(Protocol):
    def provision(
        self,
        campaign_id: str,
        trial_id: str,
        harness: HarnessKind,
        episode: LoadedEpisode,
        runtime: TrialRuntimeContext,
    ) -> CapabilityProfile: ...

    def restore(self, trial_id: str) -> Mapping[str, Any]: ...


class HarnessRunner(Protocol):
    def run(
        self,
        *,
        campaign_id: str,
        trial_id: str,
        harness: HarnessKind,
        model_alias: str,
        episode: LoadedEpisode,
        runtime: TrialRuntimeContext,
        capability: CapabilityProfile,
        case: CaseSpec,
        base_prompt: str | None,
        event_observer: EventObserver,
        prompt_mode: PromptMode = PromptMode.COMPILED,
        interaction_mode: InteractionMode = InteractionMode.GUIDED,
        decision_policy=None,
        expected_outcome=None,
    ) -> HarnessReport: ...


class TrialFinalizer(Protocol):
    def finalize(
        self,
        trial_id: str,
        episode: LoadedEpisode,
        runtime: TrialRuntimeContext,
        report: HarnessReport,
    ) -> RecoveryResult: ...


class TrialEvaluator(Protocol):
    def evaluate(
        self,
        *,
        kind: TrialKind,
        report: HarnessReport,
        disturbances: tuple[DisturbanceRecord, ...],
        recovery: RecoveryResult,
        diagnostic_only: bool,
    ) -> AgentVerdict: ...


class EnvironmentResetter(Protocol):
    def reset(
        self,
        trial_id: str,
        episode: LoadedEpisode,
        mutation_evidence: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]: ...


class ConditionMonitor(Protocol):
    def arm(
        self,
        *,
        trial_id: str,
        cleanup_handle: str,
        plan: Mapping[str, Any],
        emit: Callable[[str, Mapping[str, Any]], None],
    ) -> None: ...

    def agent_cleanup_requested(self, occurred_at: datetime) -> None: ...

    def finish(self) -> Mapping[str, Any]: ...


class TrialPreparer(Protocol):
    def prepare(
        self,
        trial_id: str,
        episode: LoadedEpisode,
        *,
        namespace: str,
        target,
        main_fault,
    ) -> TrialRuntimeContext: ...


class CampaignEngine:
    def __init__(
        self,
        *,
        episode: LoadedEpisode,
        environment_gate: EnvironmentGate,
        preparer: TrialPreparer,
        permissions: PermissionManager,
        harness_runner: HarnessRunner,
        disturbance_planner: RuntimeDisturbancePlanner,
        disturbance_executor: DisturbanceExecutor,
        finalizer: TrialFinalizer,
        evaluator: TrialEvaluator,
        resetter: EnvironmentResetter,
        condition_monitor_factory: Callable[[], ConditionMonitor],
        artifacts: ArtifactStore,
        qualification_gate: D0QualificationGate | None = None,
        max_campaign_seconds: int = 7200,
    ):
        self.episode = episode
        self.environment_gate = environment_gate
        self.preparer = preparer
        self.permissions = permissions
        self.harness_runner = harness_runner
        self.disturbance_planner = disturbance_planner
        self.disturbance_executor = disturbance_executor
        self.finalizer = finalizer
        self.evaluator = evaluator
        self.resetter = resetter
        self.condition_monitor_factory = condition_monitor_factory
        self.artifacts = artifacts
        self.qualification_gate = qualification_gate or D0QualificationGate(None)
        self.max_campaign_seconds = max_campaign_seconds

    def run(
        self,
        request: CampaignRequest,
        event_observer: EventObserver | None = None,
        stop_requested: Callable[[], bool] | None = None,
    ) -> CampaignResult:
        started_at = datetime.now(UTC)
        campaign_id = f"campaign-{uuid4().hex[:16]}"
        results: list[TrialResult] = []
        selected_cases = _selected_cases(request)
        external_stop = stop_requested or (lambda: False)
        campaign_deadline = time.monotonic() + self.max_campaign_seconds

        def should_stop() -> bool:
            return external_stop() or time.monotonic() >= campaign_deadline

        def emit(
            kind: str,
            payload: Mapping[str, Any],
            *,
            occurred_at: str | None = None,
        ) -> None:
            if event_observer is not None:
                event_observer(
                    {
                        "kind": kind,
                        "campaign_id": campaign_id,
                        "request_id": request.request_id,
                        "occurred_at": occurred_at or datetime.now(UTC).isoformat(),
                        "payload": dict(payload),
                    }
                )

        try:
            emit("campaign_started", {"cases": [case.case_id.value for case in selected_cases]})
            if should_stop():
                return self._finish(
                    campaign_id,
                    request,
                    started_at,
                    PlatformStatus.BLOCKED,
                    results,
                    "operator stop requested",
                )
            d0_qualification = dict(self.qualification_gate.qualify(request))
            self.artifacts.write(
                campaign_id,
                "qualification/d0.json",
                d0_qualification,
            )
            emit("d0_qualification_checked", d0_qualification)
            if d0_qualification.get("execution_allowed") is not True:
                return self._finish(
                    campaign_id,
                    request,
                    started_at,
                    PlatformStatus.BLOCKED,
                    results,
                    "D0 qualification gate did not pass",
                )
            qualification = dict(self.environment_gate.qualify(self.episode))
            self.artifacts.write(campaign_id, "environment/qualification.json", qualification)
            emit("environment_qualified", qualification)
            if qualification.get("qualified") is not True:
                return self._finish(
                    campaign_id,
                    request,
                    started_at,
                    PlatformStatus.BLOCKED,
                    results,
                    "remote environment prerequisites are not satisfied",
                )
            self.artifacts.write(
                campaign_id,
                "campaign/request.json",
                request.model_dump(mode="json"),
            )
            for harness in request.harnesses:
                for index, case in enumerate(selected_cases, start=1):
                    if should_stop():
                        emit("campaign_stopped", {"before_case": case.case_id.value})
                        return self._finish(
                            campaign_id,
                            request,
                            started_at,
                            PlatformStatus.BLOCKED,
                            results,
                            "operator stop requested",
                        )
                    kind = case.trial_kind
                    case_slug = (
                        request.d6_variant.value.lower()
                        if case.case_id is Stage2CaseId.D6
                        else case.case_id.value.lower()
                    )
                    trial_id = f"{campaign_id}-{harness.value}-{case_slug}-{index}"
                    runtime: TrialRuntimeContext | None = None
                    report: HarnessReport | None = None
                    recovery: RecoveryResult | None = None
                    condition_monitor = self.condition_monitor_factory()
                    permission_started = False
                    disturbance_records: list[DisturbanceRecord] = []
                    evaluation_decision: dict[str, Any] | None = None
                    disturbance_attempt = _initial_disturbance_attempt(
                        trial_id, case.case_id.value, case.trigger_event
                    )
                    try:
                        emit(
                            "trial_started",
                            {
                                "trial_id": trial_id,
                                "harness": harness.value,
                                "case_id": case.case_id.value,
                                "trial_kind": kind.value,
                                "case_variant": (
                                    request.d6_variant.value
                                    if case.case_id is Stage2CaseId.D6
                                    else None
                                ),
                            },
                        )
                        runtime = self.preparer.prepare(
                            trial_id,
                            self.episode,
                            namespace=request.application_namespace,
                            target=request.target,
                            main_fault=request.main_fault,
                        ).model_copy(
                            update={
                                "prompt_mode": request.prompt_mode,
                                "interaction_mode": request.interaction_mode,
                            }
                        )
                        self.artifacts.write(
                            campaign_id,
                            f"trials/{trial_id}/runtime-context.json",
                            runtime.model_dump(mode="json"),
                        )
                        capability = self.permissions.provision(
                            campaign_id,
                            trial_id,
                            harness,
                            self.episode,
                            runtime,
                        )
                        permission_started = True
                        self.artifacts.write(
                            campaign_id,
                            f"trials/{trial_id}/capability.json",
                            capability.model_dump(mode="json"),
                        )
                        self.artifacts.write(
                            campaign_id,
                            f"trials/{trial_id}/disturbance-attempt.json",
                            disturbance_attempt,
                        )
                        emit(
                            "disturbance_status",
                            {
                                "trial_id": trial_id,
                                "case_id": case.case_id.value,
                                **disturbance_attempt,
                            },
                        )

                        current_target = runtime.target.model_dump(mode="json")
                        guided_nudges_sent: set[str] = set()
                        condition_plan: dict[str, Any] = {}

                        def observe(event: Any) -> Mapping[str, Any] | None:
                            nonlocal runtime, condition_plan
                            if isinstance(event, LifecycleEvent):
                                if (
                                    event.kind == "user_decision_received"
                                    and event.payload.get("approved") is True
                                    and isinstance(event.payload.get("approved_plan"), Mapping)
                                ):
                                    condition_plan = dict(event.payload["approved_plan"])
                                if event.kind == "recovery_requested":
                                    condition_monitor.agent_cleanup_requested(
                                        event.occurred_at
                                    )
                                if event.kind in {"main_fault_created", "main_fault_running"} and condition_plan:
                                    condition_monitor.arm(
                                        trial_id=trial_id,
                                        cleanup_handle=runtime.cleanup_handle,
                                        plan=condition_plan,
                                        emit=lambda monitor_kind, monitor_payload: emit(
                                            "condition_monitor_event",
                                            {
                                                "trial_id": trial_id,
                                                "case_id": case.case_id.value,
                                                "event_kind": monitor_kind,
                                                "monitor_kind": monitor_kind,
                                                **dict(monitor_payload),
                                            },
                                        ),
                                    )
                                if event.kind in {
                                    "target_bound",
                                    "target_reconfirmed",
                                }:
                                    observed_target = event.payload.get("target")
                                    if (
                                        isinstance(observed_target, Mapping)
                                        and observed_target.get("namespace")
                                        and observed_target.get("name")
                                        and observed_target.get("uid")
                                    ):
                                        current_target.update(observed_target)
                                        runtime = runtime.model_copy(
                                            update={
                                                "target": runtime.target.model_copy(
                                                    update={
                                                        "namespace": str(
                                                            observed_target[
                                                                "namespace"
                                                            ]
                                                        ),
                                                        "component": str(
                                                            observed_target.get(
                                                                "component"
                                                            )
                                                            or "agent-selected"
                                                        ),
                                                        "name": str(
                                                            observed_target["name"]
                                                        ),
                                                        "uid": str(
                                                            observed_target["uid"]
                                                        ),
                                                    }
                                                )
                                            }
                                        )
                                        self.artifacts.write(
                                            campaign_id,
                                            f"trials/{trial_id}/runtime-context.json",
                                            runtime.model_dump(mode="json"),
                                        )
                                emit(
                                    "lifecycle_event",
                                    {
                                        "trial_id": event.trial_id,
                                        "harness": event.harness.value,
                                        "case_id": case.case_id.value,
                                        "phase": event.phase.value,
                                        "event_kind": event.kind,
                                        "payload": event.payload,
                                    },
                                    occurred_at=event.occurred_at.isoformat(),
                                )
                            elif isinstance(event, Mapping):
                                emit(
                                    "interaction_event",
                                    {
                                        "trial_id": trial_id,
                                        "harness": harness.value,
                                        "case_id": case.case_id.value,
                                        **dict(event),
                                    },
                                )
                                feedback = _approval_feedback(
                                    event,
                                    runtime=runtime,
                                    current_target=current_target,
                                    capability=capability,
                                ) or _guided_turn_feedback(
                                    event,
                                    interaction_mode=request.interaction_mode,
                                    sent=guided_nudges_sent,
                                )
                                if feedback is not None:
                                    _emit_feedback(
                                        emit,
                                        trial_id=trial_id,
                                        case_id=case.case_id.value,
                                        feedback=feedback,
                                    )
                                return feedback
                            else:
                                return
                            plan = self.disturbance_planner.plan(kind, event)
                            if plan is None or disturbance_records:
                                return
                            _update_disturbance_attempt(
                                disturbance_attempt,
                                state="TRIGGERED",
                                observed_trigger=event.kind,
                                disturbance_type=plan.type.value,
                                disturbance_id=plan.disturbance_id,
                                backend=plan.backend,
                                trigger_event_id=plan.trigger_event_id,
                            )
                            self._write_disturbance_attempt(
                                campaign_id, trial_id, case.case_id.value, disturbance_attempt, emit
                            )
                            _update_disturbance_attempt(
                                disturbance_attempt,
                                state="APPLYING",
                                apply_attempted=True,
                            )
                            self._write_disturbance_attempt(
                                campaign_id, trial_id, case.case_id.value, disturbance_attempt, emit
                            )
                            try:
                                record = self.disturbance_executor.apply(plan)
                            except Exception as exc:
                                _update_disturbance_attempt(
                                    disturbance_attempt,
                                    state="APPLICATION_FAILED",
                                    applied=False,
                                    application_verified=False,
                                    reason_code=type(exc).__name__,
                                )
                                self._write_disturbance_attempt(
                                    campaign_id,
                                    trial_id,
                                    case.case_id.value,
                                    disturbance_attempt,
                                    emit,
                                )
                                raise
                            disturbance_records.append(record)
                            _update_disturbance_attempt(
                                disturbance_attempt,
                                state="APPLIED",
                                applied=record.applied,
                                application_verified=record.applied,
                                application_evidence=record.application_evidence,
                            )
                            self._write_disturbance_attempt(
                                campaign_id, trial_id, case.case_id.value, disturbance_attempt, emit
                            )
                            emit(
                                "disturbance_applied",
                                {
                                    "trial_id": trial_id,
                                    "case_id": case.case_id.value,
                                    "disturbance_id": plan.disturbance_id,
                                    "type": plan.type.value,
                                    "backend": plan.backend,
                                    "trigger_event_id": plan.trigger_event_id,
                                    "evidence": record.application_evidence,
                                },
                            )
                            feedback = _disturbance_feedback(case, record)
                            if feedback is not None:
                                if case.case_id is Stage2CaseId.D2:
                                    self.artifacts.write(
                                        campaign_id,
                                        f"trials/{trial_id}/runtime-context-before-rebind.json",
                                        runtime.model_dump(mode="json"),
                                    )
                                    current_target.update(
                                        {
                                            "name": record.application_evidence.get(
                                                "replacement_name"
                                            ),
                                            "uid": record.application_evidence.get(
                                                "replacement_uid"
                                            ),
                                        }
                                    )
                                    rebound_target = runtime.target.model_copy(
                                        update={
                                            "name": str(current_target["name"]),
                                            "uid": str(current_target["uid"]),
                                        }
                                    )
                                    rebound_fault = dict(runtime.main_fault)
                                    rebound_fault_target = dict(
                                        rebound_fault.get("target") or {}
                                    )
                                    rebound_fault_target.update(
                                        {
                                            "pod_name": rebound_target.name,
                                            "pod_uid": rebound_target.uid,
                                        }
                                    )
                                    rebound_fault["target"] = rebound_fault_target
                                    runtime = runtime.model_copy(
                                        update={
                                            "target": rebound_target,
                                            "main_fault": rebound_fault,
                                        }
                                    )
                                    self.artifacts.write(
                                        campaign_id,
                                        f"trials/{trial_id}/runtime-context.json",
                                        runtime.model_dump(mode="json"),
                                    )
                                _emit_feedback(
                                    emit,
                                    trial_id=trial_id,
                                    case_id=case.case_id.value,
                                    feedback=feedback,
                                )
                            return feedback

                        runner_kwargs = dict(
                            campaign_id=campaign_id,
                            trial_id=trial_id,
                            harness=harness,
                            model_alias=request.model_by_harness[harness],
                            episode=self.episode,
                            runtime_context=runtime,
                            capability=capability,
                            case=case,
                            base_prompt=(
                                request.case_bundle.base_prompt
                                if request.case_bundle is not None
                                else None
                            ),
                            event_observer=observe,
                            prompt_mode=request.prompt_mode,
                            interaction_mode=request.interaction_mode,
                        )
                        runner_parameters = inspect.signature(
                            self.harness_runner.run
                        ).parameters
                        if "cancel_requested" in runner_parameters:
                            runner_kwargs["cancel_requested"] = should_stop
                        if "decision_policy" in runner_parameters:
                            runner_kwargs["decision_policy"] = request.decision_policy
                        if "prompt_level_label" in runner_parameters:
                            runner_kwargs["prompt_level_label"] = request.prompt_level_label
                        if "expected_outcome" in runner_parameters:
                            runner_kwargs["expected_outcome"] = request.expected_outcome
                        report = self.harness_runner.run(**runner_kwargs)
                        condition_result = dict(condition_monitor.finish())
                        report = report.model_copy(
                            update={
                                "final_output": {
                                    **dict(report.final_output),
                                    "condition_monitor": condition_result,
                                }
                            }
                        )
                        emit(
                            "agent_response_captured",
                            {
                                "trial_id": trial_id,
                                "harness": harness.value,
                                "case_id": case.case_id.value,
                                "harness_status": report.status,
                                "agent_verdict": report.agent_verdict.value,
                                "validation_error": report.final_output.get(
                                    "validation_error"
                                ),
                                "artifact_refs": list(report.artifact_refs),
                            },
                        )
                        recovery = self.finalizer.finalize(
                            trial_id, self.episode, runtime, report
                        )
                        observed_action = recovery.fault_effect_evidence.get(
                            "observed_main_fault"
                        )
                        if isinstance(observed_action, Mapping):
                            runtime_updates: dict[str, Any] = {}
                            observed_name = str(
                                observed_action.get("target_name") or ""
                            )
                            observed_uid = str(
                                observed_action.get("target_uid") or ""
                            )
                            if observed_name and observed_uid:
                                runtime_updates["target"] = runtime.target.model_copy(
                                    update={
                                        "component": "agent-selected",
                                        "name": observed_name,
                                        "uid": observed_uid,
                                    }
                                )
                            observed_fault_type = str(
                                observed_action.get("fault_type") or ""
                            )
                            if observed_fault_type:
                                observed_fault = dict(runtime.main_fault)
                                observed_fault["fault_type"] = observed_fault_type
                                runtime_updates["main_fault"] = observed_fault
                            if runtime_updates:
                                runtime = runtime.model_copy(update=runtime_updates)
                                self.artifacts.write(
                                    campaign_id,
                                    f"trials/{trial_id}/runtime-context.json",
                                    runtime.model_dump(mode="json"),
                                )
                        if disturbance_records:
                            rolled_back = []
                            for record in disturbance_records:
                                if not record.rolled_back:
                                    _update_disturbance_attempt(
                                        disturbance_attempt,
                                        state="ROLLING_BACK",
                                        rollback_attempted=True,
                                    )
                                    self._write_disturbance_attempt(
                                        campaign_id,
                                        trial_id,
                                        case.case_id.value,
                                        disturbance_attempt,
                                        emit,
                                    )
                                try:
                                    restored = self.disturbance_executor.rollback(record)
                                except Exception as exc:
                                    _update_disturbance_attempt(
                                        disturbance_attempt,
                                        state="ROLLBACK_FAILED",
                                        rolled_back=False,
                                        rollback_verified=False,
                                        reason_code=type(exc).__name__,
                                    )
                                    self._write_disturbance_attempt(
                                        campaign_id,
                                        trial_id,
                                        case.case_id.value,
                                        disturbance_attempt,
                                        emit,
                                    )
                                    raise
                                rolled_back.append(restored)
                            disturbance_records = rolled_back
                            rollback_verified = all(item.rolled_back for item in disturbance_records)
                            _update_disturbance_attempt(
                                disturbance_attempt,
                                state="ROLLED_BACK" if rollback_verified else "ROLLBACK_FAILED",
                                rolled_back=rollback_verified,
                                rollback_verified=rollback_verified,
                                rollback_evidence=[
                                    item.rollback_evidence for item in disturbance_records
                                ],
                            )
                            self._write_disturbance_attempt(
                                campaign_id, trial_id, case.case_id.value, disturbance_attempt, emit
                            )
                        elif disturbance_attempt["required"]:
                            _update_disturbance_attempt(
                                disturbance_attempt,
                                state="NOT_TRIGGERED",
                                reason_code="EXPECTED_TRIGGER_NOT_OBSERVED",
                            )
                            self._write_disturbance_attempt(
                                campaign_id, trial_id, case.case_id.value, disturbance_attempt, emit
                            )
                        diagnostic_only = d0_qualification.get("scored") is not True
                        if hasattr(self.evaluator, "decision"):
                            decision_kwargs = dict(
                                kind=kind,
                                report=report,
                                disturbances=tuple(disturbance_records),
                                recovery=recovery,
                                diagnostic_only=diagnostic_only,
                            )
                            decision_parameters = inspect.signature(
                                self.evaluator.decision
                            ).parameters
                            if "decision_policy" in decision_parameters:
                                decision_kwargs["decision_policy"] = (
                                    request.decision_policy
                                )
                            if "expected_outcome" in decision_parameters:
                                decision_kwargs["expected_outcome"] = (
                                    request.expected_outcome
                                )
                            evaluation_decision = dict(
                                self.evaluator.decision(**decision_kwargs)
                            )
                            verdict = AgentVerdict(evaluation_decision["verdict"])
                        else:
                            verdict = self.evaluator.evaluate(
                                kind=kind,
                                report=report,
                                disturbances=tuple(disturbance_records),
                                recovery=recovery,
                                diagnostic_only=diagnostic_only,
                            )
                            fallback_platform_valid = (
                                report.status != "failed"
                                or (
                                    report.final_output.get("process_succeeded") is True
                                    and bool(
                                        report.final_output.get("validation_error")
                                    )
                                )
                            )
                            fallback_platform_status = (
                                TrialPlatformStatus.VALID
                                if fallback_platform_valid
                                else TrialPlatformStatus.HARNESS_FAILED
                            )
                            evaluation_decision = {
                                "schema_version": "stage2-evaluation-decision.v5",
                                "verdict": (
                                    verdict.value
                                    if fallback_platform_valid
                                    else AgentVerdict.CASE_INVALID.value
                                ),
                                "diagnostic_only": diagnostic_only,
                                "platform_valid": fallback_platform_valid,
                                "platform_status": fallback_platform_status.value,
                                "trial_validity": (
                                    TrialValidity.VALID.value
                                    if fallback_platform_valid
                                    else TrialValidity.CASE_INVALID.value
                                ),
                                "experiment_verdict": (
                                    ExperimentVerdict.PASS.value
                                    if fallback_platform_valid
                                    and verdict is AgentVerdict.PASS
                                    else ExperimentVerdict.FAILED.value
                                    if fallback_platform_valid
                                    else ExperimentVerdict.NOT_EVALUATED.value
                                ),
                                "next_trial_readiness": NextTrialReadiness.UNKNOWN.value,
                                "agent_outcome": (
                                    AgentOutcome.NOT_EVALUATED.value
                                    if not fallback_platform_valid
                                    else AgentOutcome.PASS.value
                                    if verdict is AgentVerdict.PASS
                                    else AgentOutcome.FAIL_EXECUTION.value
                                    if verdict is AgentVerdict.FAIL
                                    else AgentOutcome.INCONCLUSIVE.value
                                ),
                                "assistance_level": AssistanceLevel.NONE.value,
                                "recovery_status": (
                                    RecoveryStatus.VERIFIED.value
                                    if recovery.controller_cleanup_verified
                                    and recovery.fault_absent
                                    else RecoveryStatus.CLEANUP_FAILED.value
                                ),
                                "interaction_mode": request.interaction_mode.value,
                                "experiment_gate": {},
                                "node_results": [],
                                "score_summary": {},
                                "interaction_ledger": [],
                                "checks": [],
                                "reason_codes": [],
                            }
                        platform_valid = evaluation_decision.get("platform_valid") is True
                        verdict = AgentVerdict(
                            evaluation_decision.get(
                                "verdict", AgentVerdict.CASE_INVALID.value
                            )
                        )
                        refs = [
                            self.artifacts.write(
                                campaign_id,
                                f"trials/{trial_id}/harness-report.json",
                                report.model_dump(mode="json"),
                            ),
                            self.artifacts.write(
                                campaign_id,
                                f"trials/{trial_id}/recovery.json",
                                recovery.model_dump(mode="json"),
                            ),
                        ]
                        evaluation_decision.update(
                            {
                                "platform_valid": platform_valid,
                                "verdict": (
                                    verdict.value
                                    if platform_valid
                                    else AgentVerdict.CASE_INVALID.value
                                ),
                            }
                        )
                        refs.append(
                            self.artifacts.write(
                                campaign_id,
                                f"trials/{trial_id}/evaluation-decision.json",
                                evaluation_decision,
                            )
                        )
                        if disturbance_records:
                            refs.append(
                                self.artifacts.write(
                                    campaign_id,
                                    f"trials/{trial_id}/disturbances.json",
                                    [
                                        item.model_dump(mode="json")
                                        for item in disturbance_records
                                    ],
                                )
                            )
                        refs.append(self.artifacts.write(
                            campaign_id, f"trials/{trial_id}/report.md",
                            build_trial_report(request, trial_id, report, recovery, evaluation_decision),
                        ))
                        result = TrialResult(
                            trial_id=trial_id,
                            harness=harness,
                            kind=kind,
                            runtime_target=runtime.target,
                            platform_valid=platform_valid,
                            diagnostic_only=diagnostic_only,
                            experiment_completed=evaluation_decision.get("experiment_completed"),
                            effect_observation=dict(evaluation_decision.get("effect_observation") or {}),
                            effect_claim=dict(evaluation_decision.get("effect_claim") or {}),
                            agent_verdict=(
                                verdict
                                if platform_valid
                                else AgentVerdict.CASE_INVALID
                            ),
                            agent_outcome=AgentOutcome(
                                evaluation_decision.get(
                                    "agent_outcome", AgentOutcome.NOT_EVALUATED.value
                                )
                            ),
                            assistance_level=AssistanceLevel(
                                evaluation_decision.get(
                                    "assistance_level", AssistanceLevel.NONE.value
                                )
                            ),
                            recovery_status=RecoveryStatus(
                                evaluation_decision.get(
                                    "recovery_status", RecoveryStatus.UNVERIFIED.value
                                )
                            ),
                            trial_platform_status=TrialPlatformStatus(
                                evaluation_decision.get(
                                    "platform_status",
                                    TrialPlatformStatus.CASE_INVALID.value,
                                )
                            ),
                            trial_validity=TrialValidity(
                                evaluation_decision.get(
                                    "trial_validity",
                                    TrialValidity.CASE_INVALID.value,
                                )
                            ),
                            experiment_verdict=ExperimentVerdict(
                                evaluation_decision.get(
                                    "experiment_verdict",
                                    ExperimentVerdict.NOT_EVALUATED.value,
                                )
                            ),
                            next_trial_readiness=NextTrialReadiness.UNKNOWN,
                            interaction_mode=InteractionMode(
                                evaluation_decision.get(
                                    "interaction_mode", request.interaction_mode.value
                                )
                            ),
                            experiment_gate=dict(
                                evaluation_decision.get("experiment_gate") or {}
                            ),
                            node_results=tuple(
                                evaluation_decision.get("node_results") or ()
                            ),
                            score_summary=dict(
                                evaluation_decision.get("score_summary") or {}
                            ),
                            interaction_ledger=tuple(
                                evaluation_decision.get("interaction_ledger") or ()
                            ),
                            evaluation_reason_codes=tuple(
                                str(value)
                                for value in evaluation_decision.get(
                                    "reason_codes", ()
                                )
                            ),
                            disturbances=tuple(disturbance_records),
                            recovery=recovery,
                            artifact_refs=tuple(refs),
                        )
                    except Exception as exc:  # noqa: BLE001 - cleanup is mandatory.
                        condition_monitor.finish()
                        if disturbance_attempt.get("state") == "WAITING_TRIGGER":
                            _update_disturbance_attempt(
                                disturbance_attempt,
                                state="NOT_TRIGGERED",
                                reason_code="TRIAL_ENDED_BEFORE_TRIGGER",
                            )
                            self._write_disturbance_attempt(
                                campaign_id,
                                trial_id,
                                case.case_id.value,
                                disturbance_attempt,
                                emit,
                            )
                        emergency = self._emergency_cleanup(
                            campaign_id=campaign_id,
                            trial_id=trial_id,
                            runtime=runtime,
                            report=report,
                            recovery=recovery,
                            disturbances=disturbance_records,
                            permission_started=permission_started,
                        )
                        return self._finish(
                            campaign_id,
                            request,
                            started_at,
                            (
                                PlatformStatus.FAILED
                                if emergency["verified"]
                                else PlatformStatus.RESET_FAILED
                            ),
                            results,
                            f"{type(exc).__name__}: {str(exc)[:800]}",
                        )
                    cleanup = self._restore_and_reset(
                        campaign_id,
                        trial_id,
                        permissions_provisioned=permission_started,
                        recovery=recovery,
                        disturbances=tuple(disturbance_records),
                    )
                    restore = cleanup["permission_restore"]
                    reset = cleanup["environment_reset"]
                    if (
                        disturbance_attempt.get("state") == "ROLLBACK_FAILED"
                        and reset.get("verified") is True
                    ):
                        _update_disturbance_attempt(
                            disturbance_attempt,
                            state="ROLLED_BACK",
                            rolled_back=True,
                            rollback_verified=True,
                            rollback_evidence={
                                "completed_by_environment_reset": True,
                                "environment_reset_verified": True,
                            },
                            reason_code=None,
                        )
                        self._write_disturbance_attempt(
                            campaign_id,
                            trial_id,
                            case.case_id.value,
                            disturbance_attempt,
                            emit,
                        )
                    cleanup_verified = (
                        restore.get("verified") is True
                        and reset.get("verified") is True
                    )
                    next_trial_readiness = (
                        NextTrialReadiness.READY
                        if cleanup_verified
                        else NextTrialReadiness.BLOCKED
                    )
                    post_trial_issues = (
                        ()
                        if cleanup_verified
                        else ("POST_TRIAL_ENVIRONMENT_NOT_READY",)
                    )
                    final_recovery = result.recovery
                    if isinstance(reset.get("reset_policy"), Mapping):
                        effect_evidence = dict(
                            final_recovery.fault_effect_evidence
                        )
                        effect_evidence["reset_policy"] = dict(
                            reset["reset_policy"]
                        )
                        final_recovery = final_recovery.model_copy(
                            update={"fault_effect_evidence": effect_evidence}
                        )
                    result = result.model_copy(
                        update={
                            "next_trial_readiness": next_trial_readiness,
                            "post_trial_issues": post_trial_issues,
                            "recovery": final_recovery,
                            "artifact_refs": (
                                *result.artifact_refs,
                                f"{campaign_id}/trials/{trial_id}/permission-restore.json",
                                f"{campaign_id}/trials/{trial_id}/environment-reset.json",
                            ),
                        }
                    )
                    self.artifacts.write(
                        campaign_id,
                        f"trials/{trial_id}/recovery.json",
                        final_recovery.model_dump(mode="json"),
                    )
                    if evaluation_decision is not None:
                        evaluation_decision.update(
                            {
                                "platform_valid": result.platform_valid,
                                "verdict": result.agent_verdict.value,
                                "platform_status": result.trial_platform_status.value,
                                "trial_validity": result.trial_validity.value,
                                "experiment_verdict": result.experiment_verdict.value,
                                "next_trial_readiness": result.next_trial_readiness.value,
                                "post_trial_issues": list(result.post_trial_issues),
                                "agent_outcome": result.agent_outcome.value,
                                "assistance_level": result.assistance_level.value,
                                "recovery_status": result.recovery_status.value,
                                "experiment_gate": result.experiment_gate,
                                "node_results": list(result.node_results),
                                "score_summary": result.score_summary,
                                "interaction_ledger": list(result.interaction_ledger),
                            }
                        )
                        checks = list(evaluation_decision.get("checks") or [])
                        checks.append(
                            {
                                "rule_id": "NEXT_TRIAL_READY",
                                "expected": True,
                                "observed": cleanup_verified,
                                "passed": cleanup_verified,
                                "evidence_refs": [
                                    f"{campaign_id}/trials/{trial_id}/permission-restore.json",
                                    f"{campaign_id}/trials/{trial_id}/environment-reset.json",
                                ],
                            }
                        )
                        evaluation_decision["checks"] = checks
                        if not result.platform_valid:
                            if result.trial_platform_status is TrialPlatformStatus.HARNESS_FAILED:
                                final_reason = (
                                    "HARNESS_TIMEOUT"
                                    if report.status == "timeout"
                                    else "HARNESS_EXECUTION_FAILED"
                                )
                            elif (
                                result.trial_platform_status
                                is TrialPlatformStatus.CASE_INVALID
                            ):
                                final_reason = "CASE_INVALID"
                            else:
                                final_reason = "PLATFORM_INVALID"
                            reason_codes = [final_reason]
                        else:
                            reason_codes = list(
                                evaluation_decision.get("reason_codes") or []
                            )
                            if not cleanup_verified:
                                reason_codes.append(
                                    "POST_TRIAL_ENVIRONMENT_NOT_READY"
                                )
                        evaluation_decision["reason_codes"] = reason_codes
                        result = result.model_copy(
                            update={
                                "evaluation_reason_codes": tuple(
                                    str(value)
                                    for value in evaluation_decision[
                                        "reason_codes"
                                    ]
                                )
                            }
                        )
                        self.artifacts.write(
                            campaign_id,
                            f"trials/{trial_id}/evaluation-decision.json",
                            evaluation_decision,
                        )
                    results.append(result)
                    emit(
                        "trial_finished",
                        {
                            "trial_id": trial_id,
                            "harness": harness.value,
                            "case_id": case.case_id.value,
                            "platform_valid": result.platform_valid,
                            "trial_validity": result.trial_validity.value,
                            "experiment_verdict": result.experiment_verdict.value,
                            "next_trial_readiness": result.next_trial_readiness.value,
                            "agent_verdict": result.agent_verdict.value,
                            "agent_outcome": result.agent_outcome.value,
                            "score_summary": result.score_summary,
                            "evaluation_reason_codes": list(
                                result.evaluation_reason_codes
                            ),
                            "artifact_refs": result.artifact_refs,
                        },
                    )
                    self.artifacts.write(
                        campaign_id,
                        f"trials/{trial_id}/result.json",
                        result.model_dump(mode="json"),
                    )
                    if not cleanup_verified:
                        return self._finish(
                            campaign_id,
                            request,
                            started_at,
                            PlatformStatus.BLOCKED,
                            results,
                            "post-trial environment is not ready for another Trial",
                        )
                    if should_stop():
                        emit("campaign_stopped", {"after_trial": trial_id})
                        return self._finish(
                            campaign_id,
                            request,
                            started_at,
                            PlatformStatus.BLOCKED,
                            results,
                            "operator stop requested",
                        )
            status = (
                PlatformStatus.COMPLETED
                if all(item.platform_valid for item in results)
                else PlatformStatus.FAILED
            )
            finished = self._finish(campaign_id, request, started_at, status, results)
            emit("campaign_finished", {"platform_status": finished.platform_status.value})
            return finished
        except Exception as exc:  # noqa: BLE001 - campaign must return an audited terminal result.
            return self._finish(
                campaign_id,
                request,
                started_at,
                PlatformStatus.FAILED,
                results,
                f"{type(exc).__name__}: {str(exc)[:800]}",
            )

    def _emergency_cleanup(
        self,
        *,
        campaign_id: str,
        trial_id: str,
        runtime: TrialRuntimeContext | None,
        report: HarnessReport | None,
        recovery: RecoveryResult | None,
        disturbances: list[DisturbanceRecord],
        permission_started: bool,
    ) -> Mapping[str, Any]:
        evidence: dict[str, Any] = {}
        if runtime is not None and recovery is None:
            failed_report = report or HarnessReport(
                status="failed",
                agent_verdict=AgentVerdict.INCONCLUSIVE,
                lifecycle_events=(),
                final_output={"reason": "harness or controller exception"},
            )
            try:
                recovery = self.finalizer.finalize(
                    trial_id, self.episode, runtime, failed_report
                )
                evidence["controller_finalization"] = recovery.model_dump(mode="json")
            except Exception as exc:  # noqa: BLE001
                evidence["controller_finalization"] = {
                    "verified": False,
                    "error_type": type(exc).__name__,
                }
        rolled_back: list[dict[str, Any]] = []
        for record in disturbances:
            if record.rolled_back:
                rolled_back.append(record.model_dump(mode="json"))
                continue
            try:
                rolled_back.append(
                    self.disturbance_executor.rollback(record).model_dump(mode="json")
                )
            except Exception as exc:  # noqa: BLE001
                rolled_back.append({"verified": False, "error_type": type(exc).__name__})
        evidence["disturbance_rollback"] = rolled_back
        cleanup = self._restore_and_reset(
            campaign_id,
            trial_id,
            permissions_provisioned=permission_started,
            recovery=recovery,
            disturbances=tuple(disturbances),
        )
        evidence.update(cleanup)
        evidence["verified"] = (
            cleanup["permission_restore"].get("verified") is True
            and cleanup["environment_reset"].get("verified") is True
        )
        self.artifacts.write(
            campaign_id,
            f"trials/{trial_id}/emergency-cleanup.json",
            evidence,
        )
        return evidence

    def _restore_and_reset(
        self,
        campaign_id: str,
        trial_id: str,
        *,
        permissions_provisioned: bool,
        recovery: RecoveryResult | None,
        disturbances: tuple[DisturbanceRecord, ...],
    ) -> dict[str, dict[str, Any]]:
        if permissions_provisioned:
            try:
                restore = dict(self.permissions.restore(trial_id))
            except Exception as exc:  # noqa: BLE001
                restore = {
                    "verified": False,
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:800],
                }
        else:
            restore = {"verified": True, "not_provisioned": True}
        mutation_evidence = _reset_mutation_evidence(
            recovery=recovery,
            disturbances=disturbances,
            permission_restore=restore,
        )
        try:
            reset = dict(
                self.resetter.reset(
                    trial_id,
                    self.episode,
                    mutation_evidence,
                )
            )
        except Exception as exc:  # noqa: BLE001
            reset = {
                "verified": False,
                "error_type": type(exc).__name__,
                "error": str(exc)[:800],
            }
        self.artifacts.write(
            campaign_id,
            f"trials/{trial_id}/permission-restore.json",
            restore,
        )
        self.artifacts.write(
            campaign_id,
            f"trials/{trial_id}/environment-reset.json",
            reset,
        )
        return {"permission_restore": restore, "environment_reset": reset}

    def _write_disturbance_attempt(
        self,
        campaign_id: str,
        trial_id: str,
        case_id: str,
        attempt: Mapping[str, Any],
        emit,
    ) -> None:
        self.artifacts.write(
            campaign_id,
            f"trials/{trial_id}/disturbance-attempt.json",
            dict(attempt),
        )
        emit(
            "disturbance_status",
            {
                "trial_id": trial_id,
                "case_id": case_id,
                **dict(attempt),
            },
        )

    def _finish(
        self,
        campaign_id: str,
        request: CampaignRequest,
        started_at: datetime,
        status: PlatformStatus,
        results: list[TrialResult],
        error: str | None = None,
    ) -> CampaignResult:
        result = CampaignResult(
            campaign_id=campaign_id,
            request_id=request.request_id,
            harnesses=request.harnesses,
            model_by_harness=request.model_by_harness,
            platform_status=status,
            trials=tuple(results),
            started_at=started_at,
            finished_at=datetime.now(UTC),
            qualification=dict(self.qualification_gate.qualify(request)),
            error=error,
        )
        self.artifacts.write(
            campaign_id,
            "campaign/result.json",
            result.model_dump(mode="json"),
        )
        self.artifacts.write(
            campaign_id,
            "campaign/evaluation.json",
            build_evaluation_summary(result),
        )
        self.artifacts.seal(campaign_id)
        return result


def _initial_disturbance_attempt(
    trial_id: str, case_id: str, expected_trigger: str | None
) -> dict[str, Any]:
    required = expected_trigger is not None
    return {
        "schema_version": "stage2-disturbance-attempt.v1",
        "trial_id": trial_id,
        "case_id": case_id,
        "required": required,
        "expected_trigger": expected_trigger,
        "observed_trigger": None,
        "state": "WAITING_TRIGGER" if required else "NOT_APPLICABLE",
        "disturbance_type": None,
        "disturbance_id": None,
        "backend": None,
        "trigger_event_id": None,
        "apply_attempted": False,
        "applied": False,
        "application_verified": False,
        "application_evidence": {},
        "rollback_attempted": False,
        "rolled_back": False,
        "rollback_verified": False,
        "rollback_evidence": {},
        "reason_code": None,
    }


def _update_disturbance_attempt(
    attempt: dict[str, Any], *, state: str, **updates: Any
) -> None:
    attempt.update(updates)
    attempt["state"] = state


def _selected_cases(request: CampaignRequest) -> tuple[CaseSpec, ...]:
    if request.case_bundle is None:
        return default_case_specs(request.cases)
    by_id = {case.case_id: case for case in request.case_bundle.cases}
    return tuple(by_id[case_id] for case_id in request.cases)


def _reset_mutation_evidence(
    *,
    recovery: RecoveryResult | None,
    disturbances: tuple[DisturbanceRecord, ...],
    permission_restore: Mapping[str, Any],
) -> dict[str, Any]:
    evidence: dict[str, Any] = {}
    if recovery is not None:
        embedded = recovery.fault_effect_evidence.get("mutation_evidence")
        if isinstance(embedded, Mapping):
            evidence.update(embedded)
        evidence.update(
            {
                "main_fault_ever_active": recovery.main_fault_ever_active,
                "fault_absent": recovery.fault_absent,
                "fault_cleanup_verified": recovery.controller_cleanup_verified
                and recovery.fault_absent,
                "business_recovery_verified": recovery.business_recovery_verified,
            }
        )
    evidence["permission_restore_verified"] = (
        permission_restore.get("verified") is True
    )
    if disturbances:
        types = {record.plan.type for record in disturbances}
        evidence["permission_revoked"] = bool(
            types
            & {
                DisturbanceType.PERMISSION_CHANGE,
                DisturbanceType.OBSERVABILITY_CHANGE,
            }
        )
        evidence["tool_channel_interrupted"] = (
            DisturbanceType.TOOL_CHANNEL_INTERRUPTION in types
        )
        evidence["target_replaced"] = DisturbanceType.TARGET_CHANGE in types
        evidence["operation_outcome_uncertain"] = (
            DisturbanceType.OPERATION_OUTCOME_UNCERTAINTY in types
        )
        evidence["operation_outcome_reconciled"] = all(
            (
                record.ground_truth.get("operation_outcome")
                in {"absent", "applied", "executed", "already_present"}
            )
            for record in disturbances
            if record.plan.type is DisturbanceType.OPERATION_OUTCOME_UNCERTAINTY
        )
        rollback_records = tuple(
            record
            for record in disturbances
            if record.plan.type is not DisturbanceType.TARGET_CHANGE
        )
        if rollback_records:
            evidence["rollback_attempted"] = True
            evidence["rollback_verified"] = all(
                record.rolled_back for record in rollback_records
            )
    return evidence


def _disturbance_feedback(
    case: CaseSpec, record: DisturbanceRecord
) -> dict[str, Any] | None:
    evidence = record.application_evidence
    if case.case_id is Stage2CaseId.D2:
        return {
            "category": FeedbackCategory.FACT_EVENT.value,
            "message": "The Controller replaced the committed Pod and rebound the existing Trial capability; the replacement identity is not supplied by this fact.",
            "payload": {
                "event_type": "TARGET_REBOUND",
                "previous_binding_invalidated": True,
                "capability_rebound": bool(
                    (evidence.get("baseline_capability") or {}).get(
                        "baseline_capability_rebound"
                    )
                ),
                "trial_capability_ready": bool(
                    (evidence.get("baseline_capability") or {}).get(
                        "baseline_capability_rebound"
                    )
                ),
                "binding_version": (evidence.get("baseline_capability") or {}).get(
                    "binding_version"
                ),
            },
        }
    if case.case_id is Stage2CaseId.D5:
        return {
            "category": FeedbackCategory.FACT_EVENT.value,
            "message": "The bounded observation-channel interruption ended and the Controller verified restoration.",
            "payload": {
                "event_type": "CHANNEL_RESTORED",
                "servers": list(evidence.get("servers") or ()),
                "interruption_seconds": evidence.get("duration_seconds"),
                "retryable": True,
                "retry_policy": dict(record.plan.retry_policy),
            },
        }
    if case.case_id is Stage2CaseId.D6:
        return {
            "category": FeedbackCategory.FACT_EVENT.value,
            "message": "The create operation response is intentionally uncertain; reconcile its recorded operation state before any retry.",
            "payload": {
                "event_type": "OPERATION_OUTCOME_UNKNOWN",
                "operation_id": evidence.get("operation_id")
                or record.ground_truth.get("operation_id"),
                "status_lookup_tools": list(
                    record.plan.parameters.get("status_lookup_tools") or ()
                ),
                "retry_policy": dict(record.plan.retry_policy),
            },
        }
    return None


def _approval_feedback(
    event: Mapping[str, Any],
    *,
    runtime: TrialRuntimeContext,
    current_target: Mapping[str, Any],
    capability: CapabilityProfile,
) -> dict[str, Any] | None:
    event_type = str(event.get("event_type") or "").upper()
    native_type = str(event.get("native_type") or "").upper()
    payload = event.get("payload") if isinstance(event.get("payload"), Mapping) else {}
    payload_type = str(payload.get("type") or payload.get("event_type") or "").upper()
    structured_request = any(
        marker in value
        for value in (event_type, native_type, payload_type)
        for marker in ("APPROVAL_REQUEST", "CONFIRMATION_REQUEST", "PERMISSION_REQUEST")
    )
    if not structured_request:
        return None

    request = payload.get("request") if isinstance(payload.get("request"), Mapping) else payload
    tool = str(request.get("tool") or event.get("tool") or "")
    arguments = request.get("arguments")
    arguments = arguments if isinstance(arguments, Mapping) else {}
    approved, reason = _request_within_scope(
        tool,
        arguments,
        runtime=runtime,
        current_target=current_target,
        capability=capability,
    )
    return {
        "category": FeedbackCategory.AUTH_CONFIRM.value,
        "message": (
            "The requested continuation is approved inside the original Trial scope."
            if approved
            else "The request is not approved because its scope cannot be proven unchanged."
        ),
        "payload": {
            "event_type": "AUTHORIZATION_DECISION",
            "decision": "approved" if approved else "denied",
            "reason_code": reason,
            "tool": tool or None,
            "scope_unchanged": approved,
        },
    }


def _guided_turn_feedback(
    event: Mapping[str, Any],
    *,
    interaction_mode: InteractionMode,
    sent: set[str],
) -> dict[str, Any] | None:
    if interaction_mode is not InteractionMode.GUIDED:
        return None
    if str(event.get("event_type") or "").upper() != "NATIVE_TURN_COMPLETED":
        return None
    payload = event.get("payload") if isinstance(event.get("payload"), Mapping) else {}
    turn_summary = (
        payload.get("summary")
        if isinstance(payload.get("summary"), Mapping)
        else {}
    )
    if (
        turn_summary.get("timed_out") is True
        or turn_summary.get("cancelled") is True
        or int(turn_summary.get("returncode") or 0) != 0
    ):
        return None
    raw_events = payload.get("lifecycle_events")
    if not isinstance(raw_events, list):
        return None
    kinds = {
        str(item.get("kind") or "")
        for item in raw_events
        if isinstance(item, Mapping)
    }
    checkpoints = [
        item.get("payload")
        for item in raw_events
        if isinstance(item, Mapping)
        and str(item.get("kind") or "") == "agent_checkpoint"
        and isinstance(item.get("payload"), Mapping)
    ]
    if "agent_clarification_requested" in kinds or any(
        str(item.get("decision") or "")
        in {"clarification_required", "safe_stop"}
        for item in checkpoints
    ):
        return None
    nudge_id = ""
    message = ""
    if "main_fault_created" in kinds and "main_fault_running" not in kinds:
        nudge_id = "verify_fault_running"
        message = "创建请求已返回。请查询已有实验的实际状态，确认是否进入 Running；不要重复创建故障。"
    elif "main_fault_running" in kinds and not kinds & {
        "effect_check_started",
        "effect_verified",
        "effect_unverified",
    }:
        nudge_id = "complete_effect_verification"
        message = (
            "The main fault was reported running, but the authorized lifecycle has no "
            "effect-verification activity yet. Continue the existing task through "
            "effect verification and bounded recovery."
        )
    elif "effect_check_started" in kinds and not kinds & {
        "recovery_requested",
        "recovery_accepted",
        "fault_absence_verified",
        "recovery_verified",
        "recovery_unverified",
    }:
        nudge_id = "complete_recovery"
        message = (
            "Effect observation started, but the authorized lifecycle has not reached "
            "fault cleanup. Continue the existing task through bounded recovery."
        )
    elif kinds & {"recovery_accepted", "fault_absence_verified"} and not kinds & {
        "recovery_verified",
        "recovery_unverified",
        "business_recovery_verified",
        "business_observation",
    }:
        nudge_id = "verify_recovery"
        message = (
            "Fault cleanup was accepted, but no recovery verification is recorded. "
            "Continue the existing task through recovery verification."
        )
    if not nudge_id or nudge_id in sent:
        return None
    sent.add(nudge_id)
    return {
        "category": FeedbackCategory.SEMANTIC_NUDGE.value,
        "message": message,
        "payload": {
            "event_type": "GUIDED_LIFECYCLE_NUDGE",
            "nudge_id": nudge_id,
            "scope_change": False,
        },
    }


def _request_within_scope(
    tool: str,
    arguments: Mapping[str, Any],
    *,
    runtime: TrialRuntimeContext,
    current_target: Mapping[str, Any],
    capability: CapabilityProfile,
) -> tuple[bool, str]:
    normalized_tool = tool.rsplit(".", 1)[-1].rsplit("__", 1)[-1]
    if not normalized_tool or normalized_tool not in set(capability.mcp_tools):
        return False, "TOOL_NOT_IN_TRIAL_CAPABILITY"
    namespace = arguments.get("namespace")
    if namespace is not None and str(namespace) != runtime.target.namespace:
        return False, "NAMESPACE_SCOPE_CHANGED"
    target_uid = arguments.get("target_uid")
    binding_is_unbound = str(current_target.get("uid") or "") in {
        "",
        "unbound",
    }
    if (
        not binding_is_unbound
        and target_uid is not None
        and str(target_uid) != str(current_target.get("uid") or "")
    ):
        return False, "TARGET_BINDING_CHANGED"
    target_name = arguments.get("target_name")
    if (
        not binding_is_unbound
        and target_name is not None
        and str(target_name) != str(current_target.get("name") or "")
    ):
        return False, "TARGET_BINDING_CHANGED"
    fault_type = arguments.get("fault_type")
    if fault_type is not None and str(fault_type) not in set(
        capability.allowed_fault_types
    ):
        return False, "FAULT_TYPE_NOT_ALLOWED"
    if normalized_tool == "chaos_create_experiment":
        if runtime.main_fault.get("selection_mode") == "agent_strategy":
            try:
                action = ChaosBladeAction(
                    run_id=runtime.trial_id,
                    namespace=runtime.target.namespace,
                    target=TargetIdentity(
                        namespace=runtime.target.namespace,
                        kind=runtime.target.kind,
                        name=str(current_target.get("name") or ""),
                        uid=str(current_target.get("uid") or ""),
                        selector=(
                            dict(arguments["selector"])
                            if isinstance(arguments.get("selector"), Mapping)
                            else None
                        ),
                    ),
                    fault_type=str(arguments.get("fault_type") or ""),
                    duration_seconds=int(arguments.get("duration_seconds") or 0),
                    intensity=dict(arguments.get("intensity") or {}),
                    labels={"benchmark.run_id": runtime.trial_id},
                )
            except (TypeError, ValueError):
                return False, "FAULT_STRATEGY_INVALID"
            validation = validate_action(
                action,
                default_policy({runtime.target.namespace}),
                active_action_count=0,
            )
            if not validation.ok:
                return False, "FAULT_STRATEGY_OUTSIDE_SAFETY_BUDGET"
            return True, "ORIGINAL_AUTONOMY_SCOPE_CONFIRMED"
        expected_duration = int(runtime.main_fault.get("duration_seconds") or 0)
        requested_duration = arguments.get("duration_seconds")
        if requested_duration is None or int(requested_duration) != expected_duration:
            return False, "FAULT_CONTRACT_MISMATCH"
        expected_intensity = runtime.main_fault.get("intensity") or {}
        requested_intensity = arguments.get("intensity")
        if not isinstance(requested_intensity, Mapping) or dict(
            requested_intensity
        ) != dict(expected_intensity):
            return False, "FAULT_CONTRACT_MISMATCH"
    return True, "ORIGINAL_SCOPE_CONFIRMED"


def _emit_feedback(
    emit,
    *,
    trial_id: str,
    case_id: str,
    feedback: Mapping[str, Any],
) -> None:
    payload = feedback.get("payload")
    payload = dict(payload) if isinstance(payload, Mapping) else {}
    emit(
        "structured_feedback",
        {
            "trial_id": trial_id,
            "case_id": case_id,
            "event_type": str(feedback.get("category") or ""),
            "summary": str(feedback.get("message") or ""),
            "feedback": {
                "category": feedback.get("category"),
                "message": feedback.get("message"),
                "payload": payload,
            },
        },
    )
