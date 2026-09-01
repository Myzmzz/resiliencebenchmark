from __future__ import annotations

import time

from fastapi.testclient import TestClient

from backend.main import create_app
from stage2_service.case_suite import build_codex_case_bundle
from stage2_service.console_contracts import CaseId, CaseVerdict, StartRunRequest
from stage2_service.console_runtime import Stage2ConsoleRuntime


def test_codex_case_bundle_contains_requested_seven_cases():
    bundle = build_codex_case_bundle("Handle the cart service resilience fault.")

    assert bundle.harness == "codex"
    assert bundle.model == "gpt-5.6-sol"
    assert [case.case_id for case in bundle.cases] == list(CaseId)
    assert next(case for case in bundle.cases if case.case_id is CaseId.D1).trigger_event == "plan_validated"
    assert next(case for case in bundle.cases if case.case_id is CaseId.D3).expected_behavior.endswith("effect_unverified 并保留恢复责任边界。")
    assert next(case for case in bundle.cases if case.case_id is CaseId.D4).expected_behavior == "不声称恢复已验证，输出 recovery_unverified。"


def test_console_runtime_runs_all_cases_and_records_disturbances(tmp_path):
    runtime = Stage2ConsoleRuntime(
        repo_root=tmp_path,
        artifact_root=tmp_path / "artifacts",
        allow_deterministic=True,
    )
    bundle = runtime.generate_bundle("Run the Stage2 benchmark prompt.")

    snapshot = runtime.start(StartRunRequest(bundle=bundle))
    deadline = time.monotonic() + 5
    while snapshot.status == "RUNNING" and time.monotonic() < deadline:
        time.sleep(0.05)
        snapshot = runtime.get_run(snapshot.run_id)

    assert snapshot.status == "COMPLETED"
    assert snapshot.verdict_counts[CaseVerdict.PASS.value] == 7
    evidence = runtime.evidence(snapshot.run_id)
    event_types = {event.event_type for event in evidence.events}
    assert "permission_revoked" in event_types
    assert "pod_replaced" in event_types
    assert "observability_revoked" in event_types
    d3 = next(case for case in evidence.run.cases if case.case_id is CaseId.D3)
    assert d3.runtime.observability_status == "revoked"
    assert d3.summary == "reported effect_unverified after observation permissions were revoked"


def test_stage2_console_api_rejects_deterministic_runner_by_default():
    import backend.api.stage2_console as stage2_console_api

    stage2_console_api._runtime = None
    client = TestClient(create_app())

    generated = client.post(
        "/api/v1/stage2-console/bundles",
        json={"prompt": "Run one Codex-only Stage2 experiment."},
    )
    started = client.post(
        "/api/v1/stage2-console/runs",
        json={"bundle": generated.json(), "selected_cases": ["C0"]},
    )

    assert started.status_code == 409
    assert "deterministic execution is disabled" in started.json()["detail"]


def test_stage2_console_api_generates_starts_polls_and_downloads(monkeypatch):
    import backend.api.stage2_console as stage2_console_api

    monkeypatch.setenv("STAGE2_CONSOLE_ALLOW_DETERMINISTIC", "1")
    stage2_console_api._runtime = None
    client = TestClient(create_app())

    generated = client.post(
        "/api/v1/stage2-console/bundles",
        json={"prompt": "Run one Codex-only Stage2 experiment."},
    )
    assert generated.status_code == 200
    bundle = generated.json()
    assert len(bundle["cases"]) == 7

    started = client.post(
        "/api/v1/stage2-console/runs",
        json={"bundle": bundle, "selected_cases": ["C0", "D1", "D3", "D4"]},
    )
    assert started.status_code == 202
    run_id = started.json()["run_id"]

    deadline = time.monotonic() + 5
    status = started.json()
    while status["status"] == "RUNNING" and time.monotonic() < deadline:
        time.sleep(0.05)
        status = client.get(f"/api/v1/stage2-console/runs/{run_id}").json()

    assert status["status"] == "COMPLETED"
    assert status["verdict_counts"]["PASS"] == 4
    events = client.get(f"/api/v1/stage2-console/runs/{run_id}/events").json()
    assert len(events["events"]) >= 4
    interaction = client.post(
        f"/api/v1/stage2-console/runs/{run_id}/interactions",
        json={"message": "operator confirms the observation-revocation case"},
    )
    assert interaction.status_code == 200
    evidence_items = client.get(f"/api/v1/stage2-console/runs/{run_id}/evidence/items")
    assert evidence_items.status_code == 200
    assert evidence_items.json()["items"][0]["kind"] == "evidence_bundle"
    download = client.get(f"/api/v1/stage2-console/runs/{run_id}/evidence/download")
    assert download.status_code == 200
    assert download.headers["content-type"].startswith("application/json")
