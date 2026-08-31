"""Autonomous Campaign engine for one fixed Episode and four native Harnesses."""

from __future__ import annotations

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
    RecoveryResult,
    TrialKind,
    TrialResult,
    TrialRuntimeContext,
    default_case_specs,
)
from .disturbance import DisturbanceExecutor, RuntimeDisturbancePlanner
from .episode import LoadedEpisode


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

    def run(
        self,
        request: CampaignRequest,
        event_observer: EventObserver | None = None,
    ) -> CampaignResult:
        started_at = datetime.now(UTC)
        campaign_id = f"campaign-{uuid4().hex[:16]}"
        results: list[TrialResult] = []
        selected_cases = _selected_cases(request)

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
                control_passed = True
                for index, case in enumerate(selected_cases, start=1):
                    kind = case.trial_kind
                    trial_id = f"{campaign_id}-{harness.value}-{case.case_id.value.lower()}-{index}"
                    runtime: TrialRuntimeContext | None = None
                    report: HarnessReport | None = None
                    recovery: RecoveryResult | None = None
                    permission_started = False
                    disturbance_records: list[DisturbanceRecord] = []
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
                        runtime = self.preparer.prepare(trial_id, self.episode)
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

                        def observe(event: Any) -> None:
                            if isinstance(event, LifecycleEvent):
                                emit(
                                    "lifecycle_event",
                                    {
                                        "trial_id": event.trial_id,
                                        "case_id": case.case_id.value,
                                        "phase": event.phase.value,
                                        "event_kind": event.kind,
                                        "payload": event.payload,
                                    },
                                )
                            plan = self.disturbance_planner.plan(kind, event)
                            if plan is None or disturbance_records:
                                return
                            record = self.disturbance_executor.apply(plan)
                            disturbance_records.append(record)
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

                        report = self.harness_runner.run(
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
                        )
                        recovery = self.finalizer.finalize(
                            trial_id, self.episode, runtime, report
                        )
                        disturbance_records = [
                            self.disturbance_executor.rollback(record)
                            for record in disturbance_records
                        ]
                        diagnostic_only = (
                            kind is not TrialKind.CONTROL and not control_passed
                        )
                        verdict = self.evaluator.evaluate(
                            kind=kind,
                            report=report,
                            disturbances=tuple(disturbance_records),
                            recovery=recovery,
                            diagnostic_only=diagnostic_only,
                        )
                        if kind is TrialKind.CONTROL:
                            control_passed = verdict is AgentVerdict.PASS
                        disturbance_expected = kind in {
                            TrialKind.CHAOS_PERMISSION_REVOKED,
                            TrialKind.TARGET_CHANGE,
                            TrialKind.EFFECT_OBSERVABILITY_REVOKED,
                            TrialKind.RECOVERY_OBSERVABILITY_REVOKED,
                        }
                        platform_valid = (
                            (
                                report.status == "completed"
                                or report.final_output.get("process_succeeded") is True
                            )
                            and recovery.controller_cleanup_verified
                            and all(item.applied for item in disturbance_records)
                            and (
                                not disturbance_expected
                                or len(disturbance_records) == 1
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
                        results.append(result)
                        emit(
                            "trial_finished",
                            {
                                "trial_id": trial_id,
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
                    except Exception as exc:  # noqa: BLE001 - cleanup is mandatory.
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
                    if restore.get("verified") is not True or reset.get("verified") is not True:
                        return self._finish(
                            campaign_id,
                            request,
                            started_at,
                            PlatformStatus.RESET_FAILED,
                            results,
                            "permission restore or environment reset failed",
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

    @staticmethod
    def _finish(
        campaign_id: str,
        request: CampaignRequest,
        started_at: datetime,
        status: PlatformStatus,
        results: list[TrialResult],
        error: str | None = None,
    ) -> CampaignResult:
        return CampaignResult(
            campaign_id=campaign_id,
            request_id=request.request_id,
            platform_status=status,
            trials=tuple(results),
            started_at=started_at,
            finished_at=datetime.now(UTC),
            error=error,
        )


def _selected_cases(request: CampaignRequest) -> tuple[CaseSpec, ...]:
    if request.case_bundle is None:
        return default_case_specs(request.cases)
    by_id = {case.case_id: case for case in request.case_bundle.cases}
    return tuple(by_id[case_id] for case_id in request.cases)
