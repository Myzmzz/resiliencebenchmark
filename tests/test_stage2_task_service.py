from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from stage2_service.api import CampaignSupervisor, create_app
from stage2_service.contracts import CampaignResult, PlatformStatus
from stage2_service.task_service import (
    AbortTaskRequest,
    Stage2TaskCreateRequest,
    Stage2TaskService,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


class Runner:
    def run(self, request, event_observer=None, stop_requested=None):
        now = datetime.now(UTC)
        if event_observer is not None:
            event_observer(
                {
                    "kind": "campaign_started",
                    "campaign_id": "campaign-tasktest0001",
                    "request_id": request.request_id,
                    "occurred_at": now.isoformat(),
                    "payload": {"cases": [item.value for item in request.cases]},
                }
            )
        return CampaignResult(
            campaign_id="campaign-tasktest0001",
            request_id=request.request_id,
            harnesses=request.harnesses,
            model_by_harness=request.model_by_harness,
            platform_status=PlatformStatus.COMPLETED,
            trials=(),
            started_at=now,
            finished_at=datetime.now(UTC),
        )


class SlowRunner:
    def run(self, request, event_observer=None, stop_requested=None):
        now = datetime.now(UTC)
        if event_observer is not None:
            event_observer(
                {
                    "kind": "campaign_started",
                    "campaign_id": "campaign-taskslow0001",
                    "request_id": request.request_id,
                    "occurred_at": now.isoformat(),
                    "payload": {"cases": [item.value for item in request.cases]},
                }
            )
        for _ in range(200):
            if stop_requested is not None and stop_requested():
                break
            time.sleep(0.005)
        return CampaignResult(
            campaign_id="campaign-taskslow0001",
            request_id=request.request_id,
            harnesses=request.harnesses,
            model_by_harness=request.model_by_harness,
            platform_status=PlatformStatus.BLOCKED,
            trials=(),
            started_at=now,
            finished_at=datetime.now(UTC),
        )


class Controls:
    def __init__(self):
        self.resets = []
        self.restores = []

    def reset_environment(self, operation_id, application):
        self.resets.append((operation_id, application))
        return {"verified": True, "application": application}

    def restore_permissions(self, task_id, trial_id, target_state):
        self.restores.append((task_id, trial_id, target_state))
        return {"verified": True, "target_state": target_state}


def preflight():
    return {
        "model_matrix": {
            "codex": {"gpt-5.6-sol": True, "claude-opus-5": True},
            "bladeai": {"gpt-5.6-sol": True, "claude-opus-5": True},
            "claude-code": {"gpt-5.6-sol": True, "claude-opus-5": True},
            "deepseek-harness": {
                "gpt-5.6-sol": True,
                "claude-opus-5": True,
            },
        }
    }


def task_service(tmp_path, runner):
    supervisor = CampaignSupervisor(runner)
    controls = Controls()
    service = Stage2TaskService(
        supervisor=supervisor,
        artifact_root=tmp_path,
        repo_root=REPO_ROOT,
        preflight_provider=preflight,
        control_backend=controls,
    )
    return service, supervisor, controls


def request():
    return Stage2TaskCreateRequest(
        application="otel-demo",
        prompt="Inject the bounded cart fault and verify its effect.",
        model="gpt-5.6-sol",
        harness="codex",
    )


def test_creates_persistent_five_trial_task_and_reuses_idempotency_key(tmp_path):
    service, _supervisor, _controls = task_service(tmp_path, Runner())

    created = service.create(request(), idempotency_key="postman-001")
    repeated = service.create(request(), idempotency_key="postman-001")

    assert created["task_id"] == repeated["task_id"]
    for _ in range(100):
        status = service.get(created["task_id"])
        if status["terminal"]:
            break
        time.sleep(0.01)
    assert status["task_status"] == "COMPLETED"
    assert status["input"]["cases"] == ["C0", "D1", "D2", "D3", "D4"]
    assert len(status["trials"]) == 5
    assert status["events"][0]["actor"] == "HARNESS"
    assert (tmp_path / "tasks" / created["task_id"] / "request.json").is_file()


def test_api_exposes_create_and_query_contract(tmp_path):
    service, supervisor, _controls = task_service(tmp_path, Runner())
    client = TestClient(create_app(supervisor, task_service=service))

    response = client.post(
        "/api/v1/stage2/tasks",
        headers={"Idempotency-Key": "postman-api-001"},
        json=request().model_dump(mode="json"),
    )

    assert response.status_code == 202
    task_id = response.json()["task_id"]
    status = client.get(f"/api/v1/stage2/tasks/{task_id}")
    assert status.status_code == 200
    assert status.json()["input"]["application"] == "otel-demo"
    assert len(status.json()["suite"]["cases"]) == 5


def test_abort_stops_runner_then_restores_permissions_and_environment(tmp_path):
    service, _supervisor, controls = task_service(tmp_path, SlowRunner())
    created = service.create(request())

    action = service.abort(
        created["task_id"],
        AbortTaskRequest(),
    )

    assert action["state"] == "REQUESTED"
    for _ in range(200):
        status = service.get(created["task_id"])
        state = status["control_actions"].get("abort", {}).get("state")
        if state in {"SUCCEEDED", "PARTIAL", "FAILED"}:
            break
        time.sleep(0.01)
    assert state == "SUCCEEDED"
    assert status["task_status"] == "ABORTED"
    assert controls.restores[-1][2] == "REVOKED"
    assert controls.resets[-1][1] == "otel-demo"
