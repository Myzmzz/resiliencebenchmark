from __future__ import annotations

from datetime import UTC, datetime

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


def test_stage2_api_lists_static_permission_profiles():
    client = TestClient(create_app(CampaignSupervisor(Runner())))

    response = client.get("/api/v1/permission-profiles")

    assert response.status_code == 200
    payload = response.json()
    assert payload["schema_version"] == "stage2-permission-profile-list.v1"
    assert {item["profile_id"] for item in payload["profiles"]} == {
        "p0-full-authorized",
        "p1-no-telemetry-mcp",
        "p2-no-k8s-mcp",
        "p3-read-only",
        "p4-no-native-k8s-metrics",
        "p5-no-mcp",
    }


def test_stage2_api_rejects_obsolete_single_model_request_contract():
    client = TestClient(create_app(CampaignSupervisor(Runner())))
    request = _request().model_dump(mode="json")
    request["schema_version"] = "stage2-campaign-request.v1"
    request["harnesses"] = ["codex"]
    request["model_by_harness"] = {"codex": "gpt-5.6"}
    request.pop("model_matrix")

    response = client.post("/api/v1/campaigns", json=request)

    assert response.status_code == 422


def test_stage2_api_rejects_static_or_disturbance_sequences_without_control_first():
    client = TestClient(create_app(CampaignSupervisor(Runner())))
    request = _request().model_dump(mode="json")
    request["trial_kinds"] = ["permission_change"]

    response = client.post("/api/v1/campaigns", json=request)

    assert response.status_code == 422
