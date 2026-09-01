from __future__ import annotations

from datetime import UTC, datetime
import threading
import time

from fastapi.testclient import TestClient

from stage2_service.api import CampaignSupervisor, create_app
from stage2_service.contracts import CampaignResult, PlatformStatus

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


def test_single_service_accepts_and_returns_campaign_result():
    client = TestClient(create_app(CampaignSupervisor(Runner())))
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


def test_generates_codex_case_bundle_and_preflight_contract():
    client = TestClient(create_app(CampaignSupervisor(Runner())))

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
    expected_cases = ["C0", "P1", "P2", "D1", "D2", "D3", "D4"]
    assert [item["case_id"] for item in bundle.json()["cases"]] == expected_cases
    assert [item["case_id"] for item in preflight.json()["cases"]] == expected_cases
    assert preflight.status_code == 200
    assert preflight.json()["harnesses"]["codex"] is True
    assert preflight.json()["models"] == ["gpt-5.6-sol", "claude-opus-5"]
    assert preflight.json()["model_matrix"]["codex"] == {
        "gpt-5.6-sol": True,
        "claude-opus-5": True,
    }


def test_stage2_frontend_health_contract_uses_the_active_repo():
    client = TestClient(create_app(CampaignSupervisor(Runner())))

    response = client.get("/api/v1/meta/health")

    assert response.status_code == 200
    assert response.json()["service"] == "ok"
    assert response.json()["repo"]["factory_config_found"] is True


def test_campaign_events_are_available_for_sse_timeline():
    client = TestClient(create_app(CampaignSupervisor(EventRunner())))
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


def test_stop_endpoint_signals_running_campaign():
    client = TestClient(create_app(CampaignSupervisor(CancellableRunner())))
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


def test_health_remains_responsive_while_sse_waits_for_campaign_events():
    client = TestClient(create_app(CampaignSupervisor(CancellableRunner())))
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


def test_cleanup_endpoint_is_audited_and_idempotent_after_completion():
    client = TestClient(create_app(CampaignSupervisor(Runner())))
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
        create_app(CampaignSupervisor(Runner()), frontend_root=tmp_path)
    )

    assert client.get("/asset.txt").text == "asset"
    assert "stage2-ui" in client.get("/evaluation/stage2-console").text
    assert client.get("/healthz").json() == {"status": "ok"}
