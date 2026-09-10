from __future__ import annotations

from datetime import UTC, datetime
import threading
import time

import pytest
from fastapi.testclient import TestClient

from stage2_service.api import CampaignSupervisor, create_app
from stage2_service.contracts import CORE_STAGE2_CASE_IDS, CampaignResult, PlatformStatus
from stage2_service.runtime_lock import RuntimeLock

from .test_stage2_campaign import _request


class Runner:
    def run(self, request):
        return CampaignResult(
            campaign_id="campaign-1234567890abcdef",
            request_id=request.request_id,
            platform_status=PlatformStatus.COMPLETED,
            trials=(),
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC),
        )


class EventRunner:
    def run(self, request, event_observer=None):
        if event_observer is not None:
            event_observer(
                {
                    "kind": "campaign_started",
                    "request_id": request.request_id,
                    "payload": {"cases": [item.value for item in request.cases]},
                }
            )
        return CampaignResult(
            campaign_id="campaign-abcdef1234567890",
            request_id=request.request_id,
            platform_status=PlatformStatus.COMPLETED,
            trials=(),
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC),
        )


class CancellableRunner:
    def run(self, request, event_observer=None, stop_requested=None):
        while stop_requested is not None and not stop_requested():
            time.sleep(0.01)
        return CampaignResult(
            campaign_id="campaign-cancelled0001",
            request_id=request.request_id,
            platform_status=PlatformStatus.BLOCKED,
            trials=(),
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC),
            error="operator stop requested",
        )


class CountingRunner(Runner):
    def __init__(self):
        self.calls = 0

    def run(self, request):
        self.calls += 1
        return super().run(request)


class FailingRunner:
    def run(self, request):
        raise RuntimeError("runner failed")


def _supervisor(runner, tmp_path):
    return CampaignSupervisor(
        runner,
        runtime_lock=RuntimeLock(tmp_path / "stage2-active-run.lock"),
    )


def test_single_service_accepts_and_returns_campaign_result(tmp_path):
    client = TestClient(create_app(_supervisor(Runner(), tmp_path)))
    request = _request().model_dump(mode="json")

    response = client.post("/api/v1/campaigns", json=request)
    assert response.status_code == 202
    request_id = response.json()["request_id"]

    for _ in range(20):
        result = client.get(f"/api/v1/campaigns/{request_id}")
        if result.json()["status"] != "RUNNING":
            break
    assert result.status_code == 200
    assert result.json()["status"] == "COMPLETED"
    listed = client.get("/api/v1/campaigns").json()["campaigns"]
    assert listed[0]["request_id"] == request_id
    assert listed[0]["status"] == "COMPLETED"


def test_campaign_endpoint_returns_409_when_runtime_lock_is_held_and_runner_is_not_called(tmp_path):
    lock_path = tmp_path / "stage2-active-run.lock"
    runner = CountingRunner()
    client = TestClient(
        create_app(
            CampaignSupervisor(runner, runtime_lock=RuntimeLock(lock_path))
        )
    )

    with RuntimeLock(lock_path).acquire(owner="qualification-cli"):
        response = client.post("/api/v1/campaigns", json=_request().model_dump(mode="json"))

    assert response.status_code == 409
    assert "Stage-2 runtime is already active" in response.json()["detail"]
    assert runner.calls == 0


def test_campaign_supervisor_releases_runtime_lock_after_runner_exception(tmp_path):
    lock_path = tmp_path / "stage2-active-run.lock"
    supervisor = CampaignSupervisor(FailingRunner(), runtime_lock=RuntimeLock(lock_path))
    request = _request()

    supervisor.submit(request)
    with pytest.raises(RuntimeError, match="runner failed"):
        supervisor.wait_result(request.request_id, timeout=5)

    with RuntimeLock(lock_path).acquire(owner="after-runner-exception"):
        pass


def test_campaign_supervisor_releases_runtime_lock_after_result_sink_exception(tmp_path):
    lock_path = tmp_path / "stage2-active-run.lock"
    supervisor = CampaignSupervisor(Runner(), runtime_lock=RuntimeLock(lock_path))
    request = _request()

    supervisor.submit(
        request,
        result_sink=lambda _result: (_ for _ in ()).throw(ValueError("sink failed")),
    )
    with pytest.raises(ValueError, match="sink failed"):
        supervisor.wait_result(request.request_id, timeout=5)

    with RuntimeLock(lock_path).acquire(owner="after-sink-exception"):
        pass


def test_generates_codex_case_bundle_and_preflight_contract(tmp_path):
    client = TestClient(create_app(_supervisor(Runner(), tmp_path)))

    bundle = client.post(
        "/api/v1/case-bundles",
        json={
            "schema_version": "stage2-case-generation-request.v1",
            "bundle_id": "local-codex-suite",
            "prompt": "Diagnose the cart service and run the bounded fault.",
        },
    )
    preflight = client.get("/api/v1/preflight")

    assert bundle.status_code == 200
    expected_cases = [case.value for case in CORE_STAGE2_CASE_IDS]
    assert [item["case_id"] for item in bundle.json()["cases"]] == expected_cases
    assert preflight.status_code == 503
    assert "harnesses" not in preflight.json()


def test_preflight_forwards_runtime_qualification_without_codex_default(tmp_path):
    observed = {
        "status": "ERROR",
        "harnesses": {name: False for name in ("codex", "claude-code", "deepseek-harness", "bladeai")},
        "harness_capability_qualification": {"status": "qualification_file_missing"},
    }
    client = TestClient(create_app(_supervisor(Runner(), tmp_path), preflight_provider=lambda: observed))
    response = client.get("/api/v1/preflight")
    assert response.status_code == 200
    assert response.json() == observed


def test_stage2_frontend_health_contract_uses_the_active_repo(tmp_path):
    client = TestClient(create_app(_supervisor(Runner(), tmp_path)))

    response = client.get("/api/v1/meta/health")

    assert response.status_code == 200
    assert response.json()["service"] == "ok"
    assert response.json()["repo"]["factory_config_found"] is True


def test_campaign_events_are_available_for_sse_timeline(tmp_path):
    client = TestClient(create_app(_supervisor(EventRunner(), tmp_path)))
    request = _request().model_dump(mode="json")

    accepted = client.post("/api/v1/campaigns", json=request)
    request_id = accepted.json()["request_id"]
    status_response = client.get(f"/api/v1/campaigns/{request_id}")

    assert status_response.status_code == 200
    payload = status_response.json()
    assert payload["status"] == "COMPLETED"
    assert payload["events"][0]["kind"] == "campaign_started"

    with client.stream("GET", f"/api/v1/campaigns/{request_id}/events") as stream:
        body = "".join(stream.iter_text())
    assert "text/event-stream" in stream.headers["content-type"]
    assert "event: event" in body
    assert "event: terminal" in body


def test_stop_endpoint_signals_running_campaign(tmp_path):
    client = TestClient(create_app(_supervisor(CancellableRunner(), tmp_path)))
    request = _request().model_dump(mode="json")
    accepted = client.post("/api/v1/campaigns", json=request)
    request_id = accepted.json()["request_id"]

    stopped = client.post(f"/api/v1/campaigns/{request_id}/stop")
    assert stopped.status_code == 200
    assert stopped.json()["stop_requested"] is True

    for _ in range(100):
        result = client.get(f"/api/v1/campaigns/{request_id}").json()
        if result["status"] != "RUNNING":
            break
        time.sleep(0.01)
    assert result["status"] == "BLOCKED"
    assert result["result"]["error"] == "operator stop requested"


def test_health_remains_responsive_while_sse_waits_for_campaign_events(tmp_path):
    client = TestClient(create_app(_supervisor(CancellableRunner(), tmp_path)))
    accepted = client.post("/api/v1/campaigns", json=_request().model_dump(mode="json"))
    request_id = accepted.json()["request_id"]
    received = []

    def consume_events():
        with client.stream("GET", f"/api/v1/campaigns/{request_id}/events") as stream:
            received.extend(stream.iter_text())

    consumer = threading.Thread(target=consume_events)
    consumer.start()
    for _ in range(5):
        assert client.get("/healthz").json() == {"status": "ok"}
    client.post(f"/api/v1/campaigns/{request_id}/stop")
    consumer.join(timeout=5)

    assert not consumer.is_alive()
    assert any("event: terminal" in item for item in received)


def test_cleanup_endpoint_is_audited_and_idempotent_after_completion(tmp_path):
    client = TestClient(create_app(_supervisor(Runner(), tmp_path)))
    accepted = client.post("/api/v1/campaigns", json=_request().model_dump(mode="json"))
    request_id = accepted.json()["request_id"]
    for _ in range(20):
        if client.get(f"/api/v1/campaigns/{request_id}").json()["status"] != "RUNNING":
            break

    response = client.post(f"/api/v1/campaigns/{request_id}/cleanup")

    assert response.status_code == 200
    assert response.json()["cleanup_status"] == "ALREADY_FINALIZED"


def test_stage2_service_serves_frontend_spa_from_same_origin(tmp_path):
    (tmp_path / "index.html").write_text("<html>stage2-ui</html>", encoding="utf-8")
    (tmp_path / "asset.txt").write_text("asset", encoding="utf-8")
    client = TestClient(
        create_app(_supervisor(Runner(), tmp_path), frontend_root=tmp_path)
    )

    assert client.get("/asset.txt").text == "asset"
    assert "stage2-ui" in client.get("/evaluation/stage2-console").text
    assert client.get("/healthz").json() == {"status": "ok"}
