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
