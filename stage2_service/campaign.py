"""Autonomous Campaign engine for one fixed Episode and four native Harnesses."""

from __future__ import annotations

import inspect
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import uuid4

from .artifacts import ArtifactStore
from .contracts import (
    AgentVerdict,
    CampaignRequest,
    CampaignResult,
    CaseSpec,
    CapabilityProfile,
    DisturbanceRecord,
    HarnessKind,
    HarnessReport,
    LifecycleEvent,
    PlatformStatus,
    PromptMode,
    RecoveryResult,
    TrialKind,
    TrialResult,
    TrialRuntimeContext,
    default_case_specs,
)
from .disturbance import DisturbanceExecutor, RuntimeDisturbancePlanner
from .episode import LoadedEpisode
from .qualification import D0QualificationGate
from .reporting import build_evaluation_summary


EventObserver = Callable[[Any], None]


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
    def reset(self, trial_id: str, episode: LoadedEpisode) -> Mapping[str, Any]: ...


class TrialPreparer(Protocol):
    def prepare(self, trial_id: str, episode: LoadedEpisode) -> TrialRuntimeContext: ...


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

        def emit(kind: str, payload: Mapping[str, Any]) -> None:
            if event_observer is not None:
                event_observer(
                    {
                        "kind": kind,
                        "campaign_id": campaign_id,
                        "request_id": request.request_id,
                        "occurred_at": datetime.now(UTC).isoformat(),
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
                    trial_id = f"{campaign_id}-{harness.value}-{case.case_id.value.lower()}-{index}"
                    runtime: TrialRuntimeContext | None = None
                    report: HarnessReport | None = None
                    recovery: RecoveryResult | None = None
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
                            },
                        )
                        runtime = self.preparer.prepare(trial_id, self.episode).model_copy(
                            update={"prompt_mode": request.prompt_mode}
                        )
                        self.artifacts.write(
                            campaign_id,
                            f"trials/{trial_id}/runtime-context.json",
                            runtime.model_dump(mode="json"),
                        )
                        permission_started = True
                        capability = self.permissions.provision(
                            campaign_id,
                            trial_id,
                            harness,
                            self.episode,
                            runtime,
                        )
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

                        def observe(event: Any) -> None:
                            if isinstance(event, LifecycleEvent):
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
                                return
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
                        )
                        if "cancel_requested" in inspect.signature(
                            self.harness_runner.run
                        ).parameters:
                            runner_kwargs["cancel_requested"] = should_stop
                        report = self.harness_runner.run(**runner_kwargs)
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
                            evaluation_decision = dict(
                                self.evaluator.decision(
                                    kind=kind,
                                    report=report,
                                    disturbances=tuple(disturbance_records),
                                    recovery=recovery,
                                    diagnostic_only=diagnostic_only,
                                )
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
                            evaluation_decision = {
                                "schema_version": "stage2-evaluation-decision.v1",
                                "verdict": verdict.value,
                                "diagnostic_only": diagnostic_only,
                                "checks": [],
                                "reason_codes": [],
                            }
                        disturbance_expected = kind in {
                            TrialKind.CHAOS_PERMISSION_REVOKED,
                            TrialKind.TARGET_CHANGE,
                            TrialKind.EFFECT_OBSERVABILITY_REVOKED,
                            TrialKind.RECOVERY_OBSERVABILITY_REVOKED,
                            TrialKind.TOOL_CHANNEL_INTERRUPTED,
                            TrialKind.OPERATION_OUTCOME_UNCERTAIN,
                        }
                        platform_valid = (
                            (
                                report.status in {"completed", "timeout"}
                                or report.final_output.get("process_succeeded") is True
                            )
                            and recovery.controller_cleanup_verified
                            and all(item.applied for item in disturbance_records)
                            and (
                                not disturbance_expected
                                or len(disturbance_records) == 1
                            )
                            and report.final_output.get("cancelled") is not True
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
                        result = TrialResult(
                            trial_id=trial_id,
                            harness=harness,
                            kind=kind,
                            runtime_target=runtime.target,
                            platform_valid=platform_valid,
                            diagnostic_only=diagnostic_only,
                            agent_verdict=(
                                verdict
                                if platform_valid
                                else AgentVerdict.CASE_INVALID
                            ),
                            disturbances=tuple(disturbance_records),
                            recovery=recovery,
                            artifact_refs=tuple(refs),
                        )
                    except Exception as exc:  # noqa: BLE001 - cleanup is mandatory.
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
                    result = result.model_copy(
                        update={
                            "platform_valid": result.platform_valid and cleanup_verified,
                            "agent_verdict": (
                                result.agent_verdict
                                if result.platform_valid and cleanup_verified
                                else AgentVerdict.CASE_INVALID
                            ),
                            "artifact_refs": (
                                *result.artifact_refs,
                                f"{campaign_id}/trials/{trial_id}/permission-restore.json",
                                f"{campaign_id}/trials/{trial_id}/environment-reset.json",
                            ),
                        }
                    )
                    if evaluation_decision is not None:
                        evaluation_decision.update(
                            {
                                "platform_valid": result.platform_valid,
                                "verdict": result.agent_verdict.value,
                            }
                        )
                        checks = list(evaluation_decision.get("checks") or [])
                        checks.append(
                            {
                                "rule_id": "PLATFORM_VALID",
                                "expected": True,
                                "observed": result.platform_valid,
                                "passed": result.platform_valid,
                                "evidence_refs": [
                                    f"{campaign_id}/trials/{trial_id}/permission-restore.json",
                                    f"{campaign_id}/trials/{trial_id}/environment-reset.json",
                                ],
                            }
                        )
                        evaluation_decision["checks"] = checks
                        evaluation_decision["reason_codes"] = [
                            item["rule_id"]
                            for item in checks
                            if item.get("passed") is not True
                        ]
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
                            "agent_verdict": result.agent_verdict.value,
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
                            PlatformStatus.RESET_FAILED,
                            results,
                            "permission restore or environment reset failed",
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
        try:
            reset = dict(self.resetter.reset(trial_id, self.episode))
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
