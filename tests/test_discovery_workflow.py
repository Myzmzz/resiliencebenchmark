from __future__ import annotations

from pathlib import Path

from controller.discovery_workflow import AnalysisBundle, DiscoveryWorkflow
from controller.run_contracts import (
    AnalysisMode,
    HarnessSelection,
    ProgressionPolicy,
    RunMode,
    RunSpec,
    RunTerminalStatus,
    ScanScope,
)
from controller.run_service import RunControlService
from controller.system_snapshot import SystemScanner

REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPO_ROOT.parent / "benchmark-sources" / "materialized"


def _spec(*, mode: RunMode = RunMode.DRY_RUN, request_id: str = "workflow-001") -> RunSpec:
    return RunSpec(
        request_id=request_id,
        requester="benchmark-admin",
        mode=mode,
        analysis_mode=AnalysisMode.DETERMINISTIC,
        scan=ScanScope(
            application="otel-demo",
            namespace="otel-demo",
            source_lock_id="otel-demo-2.2.0",
            evidence_roots=["benchmark-config", "benchmark-workload"],
        ),
        harness=HarnessSelection(
            harness_id="codex",
            model_alias="gpt-5.6",
            track="native",
        ),
        progression=ProgressionPolicy(
            profile_id="standard-l3",
            max_levels=3,
            retry_budget_per_level=1,
            total_retry_budget=3,
        ),
    )


def _service(tmp_path: Path) -> RunControlService:
    return RunControlService.create(
        database_path=tmp_path / "runtime" / "control.sqlite3",
        artifacts_root=tmp_path / "artifacts" / "runs",
    )


class ReadyAnalysis:
    def analyze(self, snapshot, spec) -> AnalysisBundle:
        return AnalysisBundle(
            candidates={"candidates": [{"candidate_id": "cand-1"}]},
            episode_designs={
                "episodes": [
                    {
                        "episode_id": "episode-1",
                        "readiness": {
                            "ready_for_execution": True,
                            "execution_blockers": [],
                        },
                    }
                ]
            },
            manifest={"reasoning_mode": spec.analysis_mode.value},
        )


class EmptyAnalysis:
    def analyze(self, snapshot, spec) -> AnalysisBundle:
        return AnalysisBundle(
            candidates={"candidates": []},
            episode_designs={"episodes": []},
            manifest={"reasoning_mode": spec.analysis_mode.value},
        )


class FailingAnalysis:
    def analyze(self, snapshot, spec) -> AnalysisBundle:
        error = RuntimeError("model gateway failed")
        error.trace = {"stage": "model_defect_analysis", "status": "transport_failed"}
        raise error


def test_dry_run_completes_discovery_preview_and_persists_every_handoff(tmp_path: Path) -> None:
    service = _service(tmp_path)
    run = service.create_run(_spec())
    lease = service.claim_next_run("worker-001")
    assert lease is not None
    workflow = DiscoveryWorkflow(
        service,
        SystemScanner(REPO_ROOT, SOURCE_ROOT),
        ReadyAnalysis(),
    )

    finished = workflow.process_claimed(lease)

    assert finished.terminal_status is RunTerminalStatus.COMPLETED
    event_types = [event.event_type for event in service.list_events(run.run_id)]
    assert "SYSTEM_SNAPSHOT_RECORDED" in event_types
    assert "CANDIDATES_RECORDED" in event_types
    assert "EPISODE_DESIGNS_RECORDED" in event_types
    assert "EPISODE_QUALIFICATION_RECORDED" in event_types
    assert event_types[-1] == "RUN_TERMINATED"
    run_dir = tmp_path / "artifacts" / "runs" / run.run_id
    assert (run_dir / "stages" / "system-snapshot.json").is_file()
    assert (run_dir / "stages" / "episode-qualification.json").is_file()


def test_zero_candidates_is_not_misreported_as_success(tmp_path: Path) -> None:
    service = _service(tmp_path)
    run = service.create_run(_spec(request_id="workflow-empty"))
    workflow = DiscoveryWorkflow(
        service,
        SystemScanner(REPO_ROOT, SOURCE_ROOT),
        EmptyAnalysis(),
    )

    finished = workflow.process(run.run_id)

    assert finished.terminal_status is RunTerminalStatus.NO_EXECUTABLE_EPISODE


def test_execute_mode_without_live_runtime_fails_closed_before_mutation(tmp_path: Path) -> None:
    service = _service(tmp_path)
    run = service.create_run(_spec(mode=RunMode.EXECUTE, request_id="workflow-execute"))
    workflow = DiscoveryWorkflow(
        service,
        SystemScanner(REPO_ROOT, SOURCE_ROOT),
        ReadyAnalysis(),
    )

    finished = workflow.process(run.run_id)

    assert finished.terminal_status is RunTerminalStatus.CASE_INVALID
    assert service.store.get_mutation_lease() is None


def test_discovery_failure_retains_safe_diagnostic_artifact(tmp_path: Path) -> None:
    service = _service(tmp_path)
    run = service.create_run(_spec(request_id="workflow-failure"))
    workflow = DiscoveryWorkflow(
        service,
        SystemScanner(REPO_ROOT, SOURCE_ROOT),
        FailingAnalysis(),
    )

    finished = workflow.process(run.run_id)
    failure = service.read_json_artifact(run.run_id, "internal/discovery-failure.json")

    assert finished.terminal_status is RunTerminalStatus.FAILED
    assert failure["error_type"] == "RuntimeError"
    assert failure["trace"]["status"] == "transport_failed"
