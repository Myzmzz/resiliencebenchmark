from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from controller.run_contracts import (
    HarnessSelection,
    ProgressionPolicy,
    RunMode,
    RunPhase,
    RunSpec,
    RunTerminalStatus,
    ScanScope,
)
from controller.run_service import RunControlService
from controller.run_store import LeaseConflictError, RunStore, TransitionError


def _spec(*, request_id: str = "request-001") -> RunSpec:
    return RunSpec(
        request_id=request_id,
        requester="benchmark-admin",
        mode=RunMode.DRY_RUN,
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


def _service(tmp_path) -> RunControlService:
    return RunControlService.create(
        database_path=tmp_path / "runtime" / "control-plane.sqlite3",
        artifacts_root=tmp_path / "artifacts" / "runs",
    )


def test_run_spec_rejects_secret_shaped_or_arbitrary_runtime_inputs() -> None:
    schema = RunSpec.model_json_schema()
    serialized = json.dumps(schema)
    assert "kubeconfig" not in serialized.lower()
    assert "token" not in serialized.lower()
    assert "environment" not in schema["properties"]

    with pytest.raises(ValidationError):
        _spec().model_copy(update={"requester": "bad requester with spaces"}).model_validate(
            {
                **_spec().model_dump(),
                "requester": "bad requester with spaces",
            }
        )


def test_create_is_idempotent_and_persists_auditable_artifacts(tmp_path) -> None:
    service = _service(tmp_path)

    first = service.create_run(_spec())
    second = service.create_run(_spec())

    assert first.run_id == second.run_id
    assert first.phase is RunPhase.CREATED
    run_dir = tmp_path / "artifacts" / "runs" / first.run_id
    assert json.loads((run_dir / "run-spec.json").read_text())["request_id"] == "request-001"
    assert json.loads((run_dir / "run-state.json").read_text())["phase"] == "CREATED"
    events = [json.loads(line) for line in (run_dir / "events.jsonl").read_text().splitlines()]
    assert [event["event_type"] for event in events] == ["RUN_CREATED"]


def test_same_request_id_with_different_spec_is_rejected(tmp_path) -> None:
    service = _service(tmp_path)
    service.create_run(_spec())
    conflicting = _spec().model_copy(
        update={
            "harness": HarnessSelection(
                harness_id="claude-code",
                model_alias="claude-opus-5",
                track="native",
            )
        }
    )

    with pytest.raises(ValueError, match="idempotency key"):
        service.create_run(conflicting)


def test_state_machine_rejects_skips_and_requires_cleanup_before_terminal(tmp_path) -> None:
    service = _service(tmp_path)
    run = service.create_run(_spec())

    with pytest.raises(TransitionError, match="CREATED to MATCHING"):
        service.advance(run.run_id, RunPhase.MATCHING)
    with pytest.raises(TransitionError, match="terminal status"):
        service.finish_cleanup(run.run_id, verified=True)

    scanning = service.advance(run.run_id, RunPhase.SCANNING)
    assert scanning.phase is RunPhase.SCANNING
    cleaning = service.request_cleanup(
        run.run_id,
        terminal_status=RunTerminalStatus.FAILED,
        reason="scanner contract failure",
    )
    assert cleaning.phase is RunPhase.CLEANING_UP
    assert cleaning.desired_terminal_status is RunTerminalStatus.FAILED

    finished = service.finish_cleanup(run.run_id, verified=True)
    assert finished.terminal_status is RunTerminalStatus.FAILED


def test_failed_cleanup_is_explicit_reset_failed(tmp_path) -> None:
    service = _service(tmp_path)
    run = service.create_run(_spec())
    service.request_cleanup(
        run.run_id,
        terminal_status=RunTerminalStatus.ABORTED,
        reason="operator abort",
    )

    finished = service.finish_cleanup(
        run.run_id,
        verified=False,
        detail={"residual": "chaosblade object still present"},
    )

    assert finished.terminal_status is RunTerminalStatus.RESET_FAILED
    assert finished.last_error == "cleanup verification failed"


def test_mutation_lease_is_single_writer_and_expiry_is_audited(tmp_path) -> None:
    service = _service(tmp_path)
    first = service.create_run(_spec(request_id="request-a"))
    second = service.create_run(_spec(request_id="request-b"))
    now = datetime(2026, 8, 23, 1, 0, tzinfo=UTC)

    service.acquire_mutation_lease(first.run_id, ttl_seconds=30, now=now)
    with pytest.raises(LeaseConflictError, match=first.run_id):
        service.acquire_mutation_lease(second.run_id, ttl_seconds=30, now=now)

    service.acquire_mutation_lease(
        second.run_id,
        ttl_seconds=30,
        now=now + timedelta(seconds=31),
    )
    lease = RunStore(tmp_path / "runtime" / "control-plane.sqlite3").get_mutation_lease()
    assert lease is not None
    assert lease.run_id == second.run_id


def test_abort_is_idempotent_and_always_routes_through_cleanup(tmp_path) -> None:
    service = _service(tmp_path)
    run = service.create_run(_spec())
    service.advance(run.run_id, RunPhase.SCANNING)

    first = service.request_abort(run.run_id, reason="operator requested stop")
    second = service.request_abort(run.run_id, reason="duplicate click")

    assert first.phase is RunPhase.CLEANING_UP
    assert second.phase is RunPhase.CLEANING_UP
    assert second.abort_requested is True
    assert second.desired_terminal_status is RunTerminalStatus.ABORTED


def test_service_recovers_state_and_events_from_sqlite(tmp_path) -> None:
    service = _service(tmp_path)
    run = service.create_run(_spec())
    service.advance(run.run_id, RunPhase.SCANNING, detail={"snapshot": "pending"})

    recovered = _service(tmp_path).get_run(run.run_id)
    events = _service(tmp_path).list_events(run.run_id)

    assert recovered.phase is RunPhase.SCANNING
    assert recovered.revision == 1
    assert [event.event_type for event in events] == ["RUN_CREATED", "PHASE_TRANSITIONED"]
    assert events[-1].detail == {"snapshot": "pending"}


def test_recorded_stage_artifact_is_scoped_hashed_and_audited(tmp_path) -> None:
    service = _service(tmp_path)
    run = service.create_run(_spec())

    service.record_json_artifact(
        run.run_id,
        artifact_ref="stages/system-snapshot.json",
        payload={"schema_version": "system-snapshot.v1", "snapshot_id": "snap-1"},
        event_type="SYSTEM_SNAPSHOT_RECORDED",
    )

    artifact = (
        tmp_path
        / "artifacts"
        / "runs"
        / run.run_id
        / "stages"
        / "system-snapshot.json"
    )
    assert json.loads(artifact.read_text())["snapshot_id"] == "snap-1"
    event = service.list_events(run.run_id)[-1]
    assert event.event_type == "SYSTEM_SNAPSHOT_RECORDED"
    assert event.detail["artifact_ref"] == "stages/system-snapshot.json"
    assert len(event.detail["sha256"]) == 64

    with pytest.raises(ValueError, match="artifact_ref"):
        service.record_json_artifact(
            run.run_id,
            artifact_ref="../outside.json",
            payload={},
            event_type="INVALID_ARTIFACT",
        )


def test_worker_claim_is_exclusive_and_expired_work_is_recoverable(tmp_path) -> None:
    service = _service(tmp_path)
    run = service.create_run(_spec())
    now = datetime(2026, 8, 23, 2, 0, tzinfo=UTC)

    first = service.claim_next_run("worker-001", ttl_seconds=60, now=now)
    assert first is not None
    assert first.run_id == run.run_id
    assert service.claim_next_run("worker-002", ttl_seconds=60, now=now) is None

    recovered = service.claim_next_run(
        "worker-002",
        ttl_seconds=60,
        now=now + timedelta(seconds=61),
    )
    assert recovered is not None
    assert recovered.run_id == run.run_id
