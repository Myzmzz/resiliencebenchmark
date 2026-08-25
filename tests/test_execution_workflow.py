from __future__ import annotations

from pathlib import Path

from controller.execution_workflow import ExecutionWorkflow
from controller.run_contracts import (
    AnalysisMode,
    HarnessSelection,
    ProgressionPolicy,
    RunMode,
    RunPhase,
    RunSpec,
    RunTerminalStatus,
    ScanScope,
)
from controller.run_service import RunControlService


def _spec() -> RunSpec:
    return RunSpec(
        request_id="execute-001",
        requester="benchmark-admin",
        mode=RunMode.EXECUTE,
        analysis_mode=AnalysisMode.MODEL,
        scan=ScanScope(
            application="otel-demo",
            namespace="otel-demo",
            source_lock_id="otel-demo-2.2.0",
            evidence_roots=["benchmark-app"],
        ),
        harness=HarnessSelection(
            harness_id="codex",
            model_alias="gpt-5.6",
            track="native",
        ),
        progression=ProgressionPolicy(
            profile_id="standard-l1",
            max_levels=1,
            retry_budget_per_level=1,
            total_retry_budget=1,
        ),
    )


def _service(tmp_path: Path) -> RunControlService:
    return RunControlService.create(
        database_path=tmp_path / "runtime" / "control.sqlite3",
        artifacts_root=tmp_path / "artifacts" / "runs",
    )


def _prepared_run(service: RunControlService):
    run = service.create_run(_spec())
    for phase in (
        RunPhase.SCANNING,
        RunPhase.MATCHING,
        RunPhase.DESIGNING,
        RunPhase.QUALIFYING,
        RunPhase.AWAITING_APPROVAL,
    ):
        run = service.advance(run.run_id, phase)
    service.record_json_artifact(
        run.run_id,
        artifact_ref=ExecutionWorkflow.MULTI_LEVEL_REF,
        payload={
            "episode_id": "EPI-EXEC-001",
            "levels": [{"level_id": "L1", "disturbances": []}],
        },
        event_type="MULTI_LEVEL_EPISODE_LOCKED",
    )
    service.record_json_artifact(
        run.run_id,
        artifact_ref=ExecutionWorkflow.PUBLIC_EPISODE_REF,
        payload={"episode_id": "EPI-EXEC-001"},
        event_type="PUBLIC_EPISODE_LOCKED",
    )
    return service.approve_run(run.run_id)


def _level_result():
    return {
        "level_id": "L1",
        "attempt": 1,
        "primary_status": "PASS",
        "metrics": {
            "duration_seconds": 100,
            "tokens_used": 1000,
            "tool_calls": 10,
        },
    }


class SuccessfulStages:
    def baseline(self, record, episode):
        return {
            "qualified": True,
            "baseline_gate_token_ref": "runtime-secret://baseline-token",
            "evidence_refs": ["baseline/summary.json"],
        }

    def execute(self, record, episode, public, baseline):
        return {"status": "PASS", "level_results": [_level_result()]}

    def recover(self, record, execution):
        return {"verified": True, "evidence_refs": ["recovery/summary.json"]}

    def oracle(self, record, episode, execution, recovery):
        return {
            "independent": True,
            "status": "PASS",
            "level_results": execution["level_results"],
            "violations": [],
            "evidence_refs": ["oracle/level-L1.json"],
        }

    def cleanup(self, record):
        return {"verified": True, "evidence_refs": ["cleanup/absence.json"]}


def test_execution_workflow_reaches_score_then_verified_cleanup(tmp_path: Path) -> None:
    service = _service(tmp_path)
    run = _prepared_run(service)
    stages = SuccessfulStages()
    workflow = ExecutionWorkflow(
        service,
        baseline_runner=stages.baseline,
        multi_level_runner=stages.execute,
        recovery_verifier=stages.recover,
        oracle=stages.oracle,
        cleanup_verifier=stages.cleanup,
    )

    finished = workflow.process(run.run_id)
    score = service.read_json_artifact(run.run_id, ExecutionWorkflow.SCORE_REF)

    assert finished.terminal_status is RunTerminalStatus.COMPLETED
    assert score["score_status"] == "provisional"
    assert score["execution_profile"] == "standard-l1"
    assert score["formal_run_eligible"] is True
    assert score["episode_score"]["final_score"] > 0
    assert service.store.get_mutation_lease() is None


def test_failed_baseline_never_invokes_fault_execution(tmp_path: Path) -> None:
    service = _service(tmp_path)
    run = _prepared_run(service)
    stages = SuccessfulStages()
    executed = []

    def failed_baseline(record, episode):
        return {"qualified": False, "evidence_refs": ["baseline/failed.json"]}

    def must_not_execute(*args):
        executed.append(True)
        raise AssertionError("fault execution must not start")

    workflow = ExecutionWorkflow(
        service,
        baseline_runner=failed_baseline,
        multi_level_runner=must_not_execute,
        recovery_verifier=stages.recover,
        oracle=stages.oracle,
        cleanup_verifier=stages.cleanup,
    )

    finished = workflow.process(run.run_id)

    assert finished.terminal_status is RunTerminalStatus.CASE_INVALID
    assert executed == []


def test_cleanup_failure_overrides_success_with_reset_failed(tmp_path: Path) -> None:
    service = _service(tmp_path)
    run = _prepared_run(service)
    stages = SuccessfulStages()
    workflow = ExecutionWorkflow(
        service,
        baseline_runner=stages.baseline,
        multi_level_runner=stages.execute,
        recovery_verifier=stages.recover,
        oracle=stages.oracle,
        cleanup_verifier=lambda _record: {
            "verified": False,
            "evidence_refs": ["cleanup/residual.json"],
        },
    )

    finished = workflow.process(run.run_id)

    assert finished.terminal_status is RunTerminalStatus.RESET_FAILED
