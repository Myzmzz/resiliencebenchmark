from __future__ import annotations

import json
from pathlib import Path

from controller.run_contracts import (
    AnalysisMode,
    HarnessSelection,
    ProgressionPolicy,
    RunMode,
    RunSpec,
    ScanScope,
)
from controller.run_service import RunControlService
from controller.runtime_multi_level import RuntimeMultiLevelRunner


def _record(tmp_path: Path):
    service = RunControlService.create(
        database_path=tmp_path / "runtime" / "control.sqlite3",
        artifacts_root=tmp_path / "artifacts" / "runs",
    )
    return service.create_run(
        RunSpec(
            request_id="runtime-multi-001",
            requester="benchmark-admin",
            mode=RunMode.EXECUTE,
            analysis_mode=AnalysisMode.DETERMINISTIC,
            scan=ScanScope(
                application="otel-demo",
                namespace="otel-demo",
                source_lock_id="otel-demo-2.2.0",
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
    )


def test_runtime_runner_passes_only_locked_files_and_resumable_store(tmp_path: Path) -> None:
    record = _record(tmp_path)
    run_root = tmp_path / "artifacts" / "runs" / record.run_id / "locked"
    run_root.mkdir(parents=True)
    multi = {"episode_id": "EPI-1", "levels": [{"level_id": "L1"}]}
    public = {"episode_id": "EPI-1"}
    (run_root / "multi-level-episode.json").write_text(json.dumps(multi))
    (run_root / "public-episode.json").write_text(json.dumps(public))
    captured = {}

    def run_episode(repo_root, **kwargs):
        captured.update(kwargs)
        return {"status": "PASS", "level_results": []}

    runner = RuntimeMultiLevelRunner(
        repo_root=tmp_path,
        run_artifacts_root=tmp_path / "artifacts" / "runs",
        private_state_root=tmp_path / "private",
        trial_runner=object(),
        level_evaluator=object(),
        injector_factory=object(),
        trial_preparer=object(),
        trial_finalizer=object(),
        run_episode=run_episode,
    )

    result = runner(record, multi, public, {"qualified": True})

    assert result["status"] == "PASS"
    assert captured["run_id"] == record.run_id
    assert captured["agent_id"] == "codex:gpt-5.6"
    assert captured["episode_file"] == run_root / "multi-level-episode.json"
    assert captured["continue_after_failure"] is False
