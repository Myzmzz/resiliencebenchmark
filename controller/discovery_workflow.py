"""Discovery-to-qualified-Episode workflow for one control-plane Run.

The workflow intentionally stops before fault execution. It establishes the
durable handoff that later execution workers consume and makes dry-run behavior
fully useful without allowing accidental cluster mutation.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from tasks.episode_promotion import (
    EpisodePromotionError,
    PromotedEpisode,
    PromotionQualification,
    promote_episode,
)

from .run_contracts import (
    AnalysisMode,
    RunMode,
    RunPhase,
    RunRecord,
    RunSpec,
    RunTerminalStatus,
    WorkerLease,
)
from .run_service import RunControlService
from .system_snapshot import (
    ObservationInventoryAdapter,
    RuntimeInventoryAdapter,
    SnapshotStatus,
    SystemScanner,
    SystemSnapshot,
)


class DiscoveryWorkflowError(RuntimeError):
    pass


@dataclass(frozen=True)
class AnalysisBundle:
    candidates: dict[str, Any]
    episode_designs: dict[str, Any]
    manifest: dict[str, Any]
    model_defect_assessment: dict[str, Any] | None = None
    model_episode_review: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "candidates": self.candidates,
            "episode_designs": self.episode_designs,
            "manifest": self.manifest,
            "model_defect_assessment": self.model_defect_assessment,
            "model_episode_review": self.model_episode_review,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> AnalysisBundle:
        return cls(
            candidates=dict(value["candidates"]),
            episode_designs=dict(value["episode_designs"]),
            manifest=dict(value["manifest"]),
            model_defect_assessment=(
                dict(value["model_defect_assessment"])
                if value.get("model_defect_assessment") is not None
                else None
            ),
            model_episode_review=(
                dict(value["model_episode_review"])
                if value.get("model_episode_review") is not None
                else None
            ),
        )


class AnalysisEngine(Protocol):
    def analyze(self, snapshot: SystemSnapshot, spec: RunSpec) -> AnalysisBundle: ...


class ResilienceAnalysisEngine:
    def __init__(
        self,
        repo_root: Path,
        source_root: Path,
        *,
        model_config_path: Path | None = None,
    ):
        self.repo_root = repo_root.resolve()
        self.source_root = source_root.resolve()
        self.model_config_path = (
            model_config_path.resolve()
            if model_config_path
            else self.repo_root / "resilience_agent/config/model.yaml"
        )

    def analyze(self, snapshot: SystemSnapshot, spec: RunSpec) -> AnalysisBundle:
        project_root = self.source_root / snapshot.source.lock_id
        evidence_roots = {
            item.alias: self.repo_root / item.repository_ref
            for item in snapshot.evidence_roots
        }
        context = _analysis_context(snapshot)
        if spec.analysis_mode is AnalysisMode.MODEL:
            from resilience_agent.agent import ResilienceAnalysisAgent
            from resilience_agent.model_client import (
                ResponsesModelClient,
                load_model_config,
            )

            result = ResilienceAnalysisAgent(
                ResponsesModelClient(load_model_config(self.model_config_path))
            ).run(
                project_root,
                system_context=context,
                evidence_roots=evidence_roots,
            )
            return AnalysisBundle(
                candidates=result.candidates,
                episode_designs=result.episode_designs,
                manifest=result.run_manifest,
                model_defect_assessment=result.model_defect_assessment,
                model_episode_review=result.model_episode_review,
            )

        from resilience_agent.agent import unique_limitations
        from resilience_agent.pipeline import run_pipeline

        candidates, episodes = run_pipeline(project_root, system_context=context)
        return AnalysisBundle(
            candidates=candidates,
            episode_designs=episodes,
            manifest={
                "schema_version": "discovery-analysis.v1",
                "reasoning_mode": "deterministic",
                "model": None,
                "limitations": unique_limitations(candidates, episodes),
            },
        )


class DiscoveryWorkflow:
    SNAPSHOT_REF = "stages/system-snapshot.json"
    ANALYSIS_BUNDLE_REF = "internal/analysis-bundle.json"
    CANDIDATES_REF = "stages/candidate-defects.json"
    EPISODES_REF = "stages/episode-designs.json"
    ANALYSIS_MANIFEST_REF = "stages/analysis-manifest.json"
    QUALIFICATION_REF = "stages/episode-qualification.json"
    LOCKED_INTERNAL_REF = "internal/locked-episode-plan.json"
    LOCKED_PUBLIC_REF = "locked/public-episode.json"
    LOCKED_MULTI_LEVEL_REF = "locked/multi-level-episode.json"

    def __init__(
        self,
        service: RunControlService,
        scanner: SystemScanner,
        analysis_engine: AnalysisEngine,
        *,
        runtime_adapter: RuntimeInventoryAdapter | None = None,
        observation_adapter: ObservationInventoryAdapter | None = None,
    ):
        self.service = service
        self.scanner = scanner
        self.analysis_engine = analysis_engine
        self.runtime_adapter = runtime_adapter
        self.observation_adapter = observation_adapter

    def process_claimed(self, lease: WorkerLease) -> RunRecord:
        try:
            return self.process(lease.run_id)
        finally:
            self.service.release_worker_lease(lease.run_id, lease.worker_id)

    def process(self, run_id: str) -> RunRecord:
        record = self.service.get_run(run_id)
        if record.is_terminal:
            return record
        try:
            if record.phase is RunPhase.CREATED:
                record = self.service.advance(run_id, RunPhase.SCANNING)
            if record.phase is RunPhase.SCANNING:
                snapshot = self._scan(record)
                record = self.service.advance(
                    run_id,
                    RunPhase.MATCHING,
                    detail={"snapshot_id": snapshot.snapshot_id},
                )
            else:
                snapshot = self._load_snapshot(run_id)

            if record.phase is RunPhase.MATCHING:
                bundle = self._analyze(snapshot, record.spec)
                candidate_count = len(bundle.candidates.get("candidates", []))
                if candidate_count == 0:
                    return self._terminate_without_mutation(
                        run_id,
                        RunTerminalStatus.NO_EXECUTABLE_EPISODE,
                        "no evidence-backed candidate was generated",
                    )
                record = self.service.advance(
                    run_id,
                    RunPhase.DESIGNING,
                    detail={"candidate_count": candidate_count},
                )
            else:
                bundle = self._load_bundle(run_id)

            if record.phase is RunPhase.DESIGNING:
                self._record_design_artifacts(run_id, bundle)
                episode_count = len(bundle.episode_designs.get("episodes", []))
                if episode_count == 0:
                    return self._terminate_without_mutation(
                        run_id,
                        RunTerminalStatus.NO_EXECUTABLE_EPISODE,
                        "no Episode met the design confidence threshold",
                    )
                record = self.service.advance(
                    run_id,
                    RunPhase.QUALIFYING,
                    detail={"episode_count": episode_count},
                )

            if record.phase is RunPhase.QUALIFYING:
                return self._qualify(record, snapshot, bundle)
            return record
        except Exception as exc:  # noqa: BLE001 - convert stage failure into audited cleanup.
            current = self.service.get_run(run_id)
            if current.is_terminal:
                return current
            failure = {
                "schema_version": "discovery-failure.v1",
                "run_id": run_id,
                "error_type": type(exc).__name__,
                "error": str(exc)[:1000],
                "trace": getattr(exc, "trace", None),
            }
            self.service.record_json_artifact(
                run_id,
                artifact_ref="internal/discovery-failure.json",
                payload=failure,
                event_type="DISCOVERY_FAILURE_RECORDED",
            )
            return self._terminate_without_mutation(
                run_id,
                RunTerminalStatus.FAILED,
                f"discovery workflow failed: {type(exc).__name__}: {str(exc)[:500]}",
            )

    def _scan(self, record: RunRecord) -> SystemSnapshot:
        existing = self.service.read_json_artifact(record.run_id, self.SNAPSHOT_REF)
        if existing is not None:
            return SystemSnapshot.model_validate(existing)
        snapshot = self.scanner.scan(
            record.run_id,
            record.spec,
            runtime_adapter=self.runtime_adapter,
            observation_adapter=self.observation_adapter,
        )
        self.service.record_json_artifact(
            record.run_id,
            artifact_ref=self.SNAPSHOT_REF,
            payload=snapshot.model_dump(mode="json"),
            event_type="SYSTEM_SNAPSHOT_RECORDED",
        )
        return snapshot

    def _analyze(self, snapshot: SystemSnapshot, spec: RunSpec) -> AnalysisBundle:
        existing = self.service.read_json_artifact(snapshot.run_id, self.ANALYSIS_BUNDLE_REF)
        if existing is not None:
            return AnalysisBundle.from_dict(existing)
        bundle = self.analysis_engine.analyze(snapshot, spec)
        self.service.record_json_artifact(
            snapshot.run_id,
            artifact_ref=self.ANALYSIS_BUNDLE_REF,
            payload=bundle.as_dict(),
            event_type="ANALYSIS_BUNDLE_RECORDED",
        )
        self.service.record_json_artifact(
            snapshot.run_id,
            artifact_ref=self.CANDIDATES_REF,
            payload=bundle.candidates,
            event_type="CANDIDATES_RECORDED",
        )
        return bundle

    def _record_design_artifacts(self, run_id: str, bundle: AnalysisBundle) -> None:
        self.service.record_json_artifact(
            run_id,
            artifact_ref=self.EPISODES_REF,
            payload=bundle.episode_designs,
            event_type="EPISODE_DESIGNS_RECORDED",
        )
        self.service.record_json_artifact(
            run_id,
            artifact_ref=self.ANALYSIS_MANIFEST_REF,
            payload=bundle.manifest,
            event_type="ANALYSIS_MANIFEST_RECORDED",
        )
        if bundle.model_defect_assessment is not None:
            self.service.record_json_artifact(
                run_id,
                artifact_ref="internal/model-defect-assessment.json",
                payload=bundle.model_defect_assessment,
                event_type="MODEL_DEFECT_ASSESSMENT_RECORDED",
            )
        if bundle.model_episode_review is not None:
            self.service.record_json_artifact(
                run_id,
                artifact_ref="internal/model-episode-review.json",
                payload=bundle.model_episode_review,
                event_type="MODEL_EPISODE_REVIEW_RECORDED",
            )

    def _qualify(
        self,
        record: RunRecord,
        snapshot: SystemSnapshot,
        bundle: AnalysisBundle,
    ) -> RunRecord:
        episodes = bundle.episode_designs.get("episodes", [])
        ready: list[PromotedEpisode] = []
        promotion_errors: list[str] = []
        if record.spec.mode is RunMode.EXECUTE:
            for episode in episodes:
                if not isinstance(episode, dict):
                    continue
                try:
                    ready.append(
                        promote_episode(
                            episode,
                            snapshot,
                            record.spec,
                            PromotionQualification(
                                independent_observers_qualified=(
                                    snapshot.observers.status is SnapshotStatus.QUALIFIED
                                ),
                                cleanup_path_qualified=True,
                            ),
                        )
                    )
                except EpisodePromotionError as exc:
                    promotion_errors.append(
                        f"{episode.get('episode_id', 'unknown')}: {exc!s}"
                    )
            if ready:
                selected = ready[0]
                self.service.record_json_artifact(
                    record.run_id,
                    artifact_ref=self.LOCKED_INTERNAL_REF,
                    payload=selected.internal_plan,
                    event_type="INTERNAL_EPISODE_LOCKED",
                )
                self.service.record_json_artifact(
                    record.run_id,
                    artifact_ref=self.LOCKED_PUBLIC_REF,
                    payload=selected.public_episode,
                    event_type="PUBLIC_EPISODE_LOCKED",
                )
                self.service.record_json_artifact(
                    record.run_id,
                    artifact_ref=self.LOCKED_MULTI_LEVEL_REF,
                    payload=selected.multi_level_episode,
                    event_type="MULTI_LEVEL_EPISODE_LOCKED",
                )
        selected_id = (
            ready[0].multi_level_episode["episode_id"]
            if ready
            else None
        )
        report = {
            "schema_version": "episode-qualification.v1",
            "run_id": record.run_id,
            "mode": record.spec.mode.value,
            "runtime_status": snapshot.runtime.status.value,
            "observer_status": snapshot.observers.status.value,
            "episode_count": len(episodes),
            "ready_for_execution_count": len(ready),
            "selected_episode_id": selected_id,
            "decision": (
                "preview_complete"
                if record.spec.mode is RunMode.DRY_RUN
                else "qualified" if ready else "blocked"
            ),
            "blockers": sorted(
                set(promotion_errors)
                if ready
                else {
                    blocker
                    for episode in episodes
                    if isinstance(episode, dict)
                    for blocker in episode.get("readiness", {}).get(
                        "execution_blockers", []
                    )
                }
                | set(promotion_errors)
            ),
        }
        self.service.record_json_artifact(
            record.run_id,
            artifact_ref=self.QUALIFICATION_REF,
            payload=report,
            event_type="EPISODE_QUALIFICATION_RECORDED",
        )
        if record.spec.mode is RunMode.DRY_RUN:
            return self._terminate_without_mutation(
                record.run_id,
                RunTerminalStatus.COMPLETED,
                "discovery and Episode preview completed without cluster mutation",
            )
        if (
            snapshot.runtime.status is not SnapshotStatus.QUALIFIED
            or snapshot.observers.status is not SnapshotStatus.QUALIFIED
            or not ready
        ):
            return self._terminate_without_mutation(
                record.run_id,
                RunTerminalStatus.CASE_INVALID,
                "runtime or Episode qualification did not pass",
            )
        return self.service.advance(
            record.run_id,
            RunPhase.BASELINING if record.spec.auto_approve else RunPhase.AWAITING_APPROVAL,
            detail={"selected_episode_id": selected_id},
        )

    def _terminate_without_mutation(
        self,
        run_id: str,
        terminal_status: RunTerminalStatus,
        reason: str,
    ) -> RunRecord:
        current = self.service.get_run(run_id)
        if current.phase is not RunPhase.CLEANING_UP:
            self.service.request_cleanup(
                run_id,
                terminal_status=terminal_status,
                reason=reason,
            )
        return self.service.finish_cleanup(
            run_id,
            verified=True,
            detail={"mutation_started": False, "reason": reason},
        )

    def _load_snapshot(self, run_id: str) -> SystemSnapshot:
        value = self.service.read_json_artifact(run_id, self.SNAPSHOT_REF)
        if value is None:
            raise DiscoveryWorkflowError("system snapshot checkpoint is missing")
        return SystemSnapshot.model_validate(value)

    def _load_bundle(self, run_id: str) -> AnalysisBundle:
        value = self.service.read_json_artifact(run_id, self.ANALYSIS_BUNDLE_REF)
        if value is None:
            raise DiscoveryWorkflowError("analysis bundle checkpoint is missing")
        return AnalysisBundle.from_dict(value)


def _analysis_context(snapshot: SystemSnapshot) -> dict[str, Any]:
    workload = snapshot.workload
    return {
        "application": snapshot.application,
        "namespace": snapshot.namespace,
        "snapshot_id": snapshot.snapshot_id,
        "release_ref": snapshot.source.commit,
        "workload": {
            "profile": workload.profile_ref,
            "baseline_window": f"{workload.evaluation_window_seconds}s",
            "fixture_ref": f"environment/workloads/{snapshot.application}/runtime-fixture.example.yaml",
            "slo": [
                f"success_rate >= {workload.minimum_success_rate}",
                f"error_rate <= {workload.maximum_error_rate}",
                f"p95_latency_ms <= {workload.maximum_p95_latency_ms}",
                *(
                    [f"throughput_rps >= {workload.minimum_throughput_rps}"]
                    if workload.minimum_throughput_rps is not None
                    else []
                ),
            ],
        },
        "budget": {"max_experiments": 2, "max_duration_minutes": 30},
        "independent_observers_qualified": False,
    }
