"""Baseline-to-cleanup execution workflow for a qualified locked Episode."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from scoring.runtime import score_provisional_run

from .run_contracts import RunPhase, RunRecord, RunTerminalStatus, WorkerLease
from .run_service import RunControlService


class BaselineRunner(Protocol):
    def __call__(
        self, record: RunRecord, multi_level_episode: Mapping[str, Any]
    ) -> Mapping[str, Any]: ...


class MultiLevelRunner(Protocol):
    def __call__(
        self,
        record: RunRecord,
        multi_level_episode: Mapping[str, Any],
        public_episode: Mapping[str, Any],
        baseline: Mapping[str, Any],
    ) -> Mapping[str, Any]: ...


class RecoveryVerifier(Protocol):
    def __call__(
        self, record: RunRecord, execution_report: Mapping[str, Any]
    ) -> Mapping[str, Any]: ...


class IndependentOracle(Protocol):
    def __call__(
        self,
        record: RunRecord,
        multi_level_episode: Mapping[str, Any],
        execution_report: Mapping[str, Any],
        recovery_report: Mapping[str, Any],
    ) -> Mapping[str, Any]: ...


class CleanupVerifier(Protocol):
    def __call__(self, record: RunRecord) -> Mapping[str, Any]: ...


class ExecutionWorkflow:
    MULTI_LEVEL_REF = "locked/multi-level-episode.json"
    PUBLIC_EPISODE_REF = "locked/public-episode.json"
    BASELINE_REF = "execution/baseline-result.json"
    EXECUTION_REF = "execution/multi-level-run.json"
    RECOVERY_REF = "execution/recovery-result.json"
    EVALUATION_REF = "evaluation/oracle-result.json"
    SCORE_REF = "scoring/run-score.json"
    CLEANUP_REF = "execution/cleanup-result.json"

    def __init__(
        self,
        service: RunControlService,
        *,
        baseline_runner: BaselineRunner,
        multi_level_runner: MultiLevelRunner,
        recovery_verifier: RecoveryVerifier,
        oracle: IndependentOracle,
        cleanup_verifier: CleanupVerifier,
    ):
        self.service = service
        self.baseline_runner = baseline_runner
        self.multi_level_runner = multi_level_runner
        self.recovery_verifier = recovery_verifier
        self.oracle = oracle
        self.cleanup_verifier = cleanup_verifier

    def process_claimed(self, lease: WorkerLease) -> RunRecord:
        try:
            return self.process(lease.run_id)
        finally:
            self.service.release_worker_lease(lease.run_id, lease.worker_id)

    def process(self, run_id: str) -> RunRecord:
        record = self.service.get_run(run_id)
        if record.is_terminal:
            return record
        if record.phase is RunPhase.CLEANING_UP:
            return self._finish_cleanup(record)
        try:
            multi_level = self._required_artifact(run_id, self.MULTI_LEVEL_REF)
            public_episode = self._required_artifact(run_id, self.PUBLIC_EPISODE_REF)
            if record.phase is RunPhase.BASELINING:
                self.service.acquire_mutation_lease(run_id, ttl_seconds=600)
                baseline = self._run_or_load(
                    record,
                    self.BASELINE_REF,
                    "BASELINE_RECORDED",
                    lambda: self.baseline_runner(record, multi_level),
                )
                if baseline.get("qualified") is not True:
                    return self._request_and_finish(
                        run_id,
                        RunTerminalStatus.CASE_INVALID,
                        "formal baseline did not qualify",
                    )
                record = self.service.advance(
                    run_id,
                    RunPhase.EXECUTING,
                    detail={"baseline_evidence_refs": baseline.get("evidence_refs", [])},
                )
            else:
                baseline = self._required_artifact(run_id, self.BASELINE_REF)

            if record.phase is RunPhase.EXECUTING:
                execution = self._run_or_load(
                    record,
                    self.EXECUTION_REF,
                    "MULTI_LEVEL_RUN_RECORDED",
                    lambda: self.multi_level_runner(
                        record,
                        multi_level,
                        public_episode,
                        baseline,
                    ),
                )
                record = self.service.advance(
                    run_id,
                    RunPhase.RECOVERING,
                    detail={"execution_status": execution.get("status")},
                )
            else:
                execution = self._required_artifact(run_id, self.EXECUTION_REF)

            if record.phase is RunPhase.RECOVERING:
                recovery = self._run_or_load(
                    record,
                    self.RECOVERY_REF,
                    "RECOVERY_RECORDED",
                    lambda: self.recovery_verifier(record, execution),
                )
                if recovery.get("verified") is not True:
                    return self._request_and_finish(
                        run_id,
                        RunTerminalStatus.RESET_FAILED,
                        "target-side recovery was not verified",
                    )
                record = self.service.advance(
                    run_id,
                    RunPhase.EVALUATING,
                    detail={"recovery_evidence_refs": recovery.get("evidence_refs", [])},
                )
            else:
                recovery = self._required_artifact(run_id, self.RECOVERY_REF)

            if record.phase is RunPhase.EVALUATING:
                evaluation = self._run_or_load(
                    record,
                    self.EVALUATION_REF,
                    "ORACLE_EVALUATION_RECORDED",
                    lambda: self.oracle(record, multi_level, execution, recovery),
                )
                if evaluation.get("independent") is not True:
                    raise RuntimeError("Oracle result is not marked independent")
                record = self.service.advance(
                    run_id,
                    RunPhase.SCORING,
                    detail={"oracle_status": evaluation.get("status")},
                )
            else:
                evaluation = self._required_artifact(run_id, self.EVALUATION_REF)

            if record.phase is RunPhase.SCORING:
                score = self._score(record, multi_level, evaluation)
                self.service.record_json_artifact(
                    run_id,
                    artifact_ref=self.SCORE_REF,
                    payload=score,
                    event_type="RUN_SCORE_RECORDED",
                )
                passed = (
                    str(execution.get("status")) == "PASS"
                    and str(evaluation.get("status")) == "PASS"
                )
                return self._request_and_finish(
                    run_id,
                    RunTerminalStatus.COMPLETED if passed else RunTerminalStatus.FAILED,
                    "execution and independent evaluation completed",
                )
            return record
        except Exception as exc:  # noqa: BLE001 - convert stage failure into audited cleanup.
            current = self.service.get_run(run_id)
            if current.is_terminal:
                return current
            if current.phase is not RunPhase.CLEANING_UP:
                self.service.record_json_artifact(
                    run_id,
                    artifact_ref="internal/execution-failure.json",
                    payload={
                        "schema_version": "execution-failure.v1",
                        "error_type": type(exc).__name__,
                        "error": str(exc)[:1000],
                    },
                    event_type="EXECUTION_FAILURE_RECORDED",
                )
                self.service.request_cleanup(
                    run_id,
                    terminal_status=RunTerminalStatus.FAILED,
                    reason=f"execution workflow failed: {type(exc).__name__}",
                )
            return self._finish_cleanup(self.service.get_run(run_id))

    def _score(
        self,
        record: RunRecord,
        episode: Mapping[str, Any],
        evaluation: Mapping[str, Any],
    ) -> dict[str, Any]:
        if not record.spec.scoring.allow_provisional_efficiency:
            raise RuntimeError("single-run provisional scoring is disabled by RunSpec")
        level_results = evaluation.get("level_results")
        if not isinstance(level_results, list):
            raise TypeError("Oracle evaluation has no level_results")
        return score_provisional_run(
            run_id=record.run_id,
            episode_id=str(episode["episode_id"]),
            agent_id=f"{record.spec.harness.harness_id}:{record.spec.harness.model_alias}",
            level_results=level_results,
            total_levels=len(episode["levels"]),
            violations=evaluation.get("violations", []),
            evidence_refs=evaluation.get("evidence_refs", []),
            policy_id=record.spec.scoring.policy_id,
        ) | {
            "execution_profile": record.spec.progression.profile_id,
            "formal_run_eligible": record.spec.progression.profile_id.startswith(
                "standard-"
            ),
        }

    def _request_and_finish(
        self,
        run_id: str,
        terminal: RunTerminalStatus,
        reason: str,
    ) -> RunRecord:
        current = self.service.get_run(run_id)
        if current.phase is not RunPhase.CLEANING_UP:
            self.service.request_cleanup(
                run_id,
                terminal_status=terminal,
                reason=reason,
            )
        return self._finish_cleanup(self.service.get_run(run_id))

    def _finish_cleanup(self, record: RunRecord) -> RunRecord:
        cleanup = self._run_or_load(
            record,
            self.CLEANUP_REF,
            "CLEANUP_VERIFICATION_RECORDED",
            lambda: self.cleanup_verifier(record),
        )
        return self.service.finish_cleanup(
            record.run_id,
            verified=cleanup.get("verified") is True,
            detail={"evidence_refs": cleanup.get("evidence_refs", [])},
        )

    def _run_or_load(
        self,
        record: RunRecord,
        artifact_ref: str,
        event_type: str,
        operation,
    ) -> Mapping[str, Any]:
        existing = self.service.read_json_artifact(record.run_id, artifact_ref)
        if existing is not None:
            if not isinstance(existing, Mapping):
                raise RuntimeError(f"checkpoint {artifact_ref} is not a JSON object")
            return existing
        result = operation()
        if not isinstance(result, Mapping):
            raise TypeError(f"stage {event_type} did not return a mapping")
        payload = dict(result)
        self.service.record_json_artifact(
            record.run_id,
            artifact_ref=artifact_ref,
            payload=payload,
            event_type=event_type,
        )
        return payload

    def _required_artifact(self, run_id: str, artifact_ref: str) -> Mapping[str, Any]:
        value = self.service.read_json_artifact(run_id, artifact_ref)
        if not isinstance(value, Mapping):
            raise TypeError(f"required locked artifact is missing: {artifact_ref}")
        return value
