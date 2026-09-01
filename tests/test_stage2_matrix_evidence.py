from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from stage2_service.api import CampaignSupervisor, create_app
from stage2_service.contracts import CampaignResult, PlatformStatus
from stage2_service.matrix_evidence import MatrixEvidenceStore


MATRIX_ID = "matrix-otel-test-0001"
CAMPAIGN_ID = "campaign-1234567890abcdef"
TRIAL_ID = f"{CAMPAIGN_ID}-codex-c0-1"


class Runner:
    def run(self, request):
        now = datetime.now(UTC)
        return CampaignResult(
            campaign_id=CAMPAIGN_ID,
            request_id=request.request_id,
            platform_status=PlatformStatus.COMPLETED,
            trials=(),
            started_at=now,
            finished_at=now,
        )


def _write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _seal(root: Path) -> None:
    rows = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        if path.name == "manifest.sha256":
            continue
        rows.append(
            f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.relative_to(root).as_posix()}"
        )
    (root / "manifest.sha256").write_text("\n".join(rows) + "\n", encoding="utf-8")


def _artifacts(root: Path) -> None:
    matrix = root / MATRIX_ID
    campaign = root / CAMPAIGN_ID
    trial = {
        "trial_id": TRIAL_ID,
        "harness": "codex",
        "kind": "C0",
        "runtime_target": {
            "namespace": "otel-demo",
            "component": "cart",
            "kind": "Pod",
            "name": "cart-a",
            "uid": "uid-a",
        },
        "platform_valid": True,
        "diagnostic_only": False,
        "agent_verdict": "PASS",
        "disturbances": [],
        "recovery": {
            "agent_attempted": True,
            "agent_recovery_verified": True,
            "controller_cleanup_verified": True,
            "fault_absent": True,
            "business_recovery_verified": True,
            "main_fault_ever_active": True,
            "fault_effect_verified": True,
        },
        "artifact_refs": [],
    }
    _write_json(
        matrix / "report.json",
        {
            "matrix_id": MATRIX_ID,
            "system": "otel-demo",
            "expected_trial_count": 1,
            "completed_trial_count": 1,
            "generated_at": "2026-09-01T00:00:03Z",
            "campaigns": [{"campaign_id": CAMPAIGN_ID}],
            "score_table": [],
        },
    )
    _write_json(matrix / "request.json", {"matrix_id": MATRIX_ID, "prompt": "run"})
    (matrix / "events.jsonl").write_text(
        "\n".join(
            json.dumps(item)
            for item in (
                {
                    "campaign_id": CAMPAIGN_ID,
                    "kind": "trial_started",
                    "occurred_at": "2026-09-01T00:00:00Z",
                    "payload": {"trial_id": TRIAL_ID},
                },
                {
                    "campaign_id": CAMPAIGN_ID,
                    "kind": "trial_finished",
                    "occurred_at": "2026-09-01T00:00:03Z",
                    "payload": {"trial_id": TRIAL_ID},
                },
            )
        )
        + "\n",
        encoding="utf-8",
    )
    result = {
        "campaign_id": CAMPAIGN_ID,
        "request_id": f"{MATRIX_ID}-gpt-5-6-sol-codex",
        "platform_status": "COMPLETED",
        "harnesses": ["codex"],
        "model_by_harness": {"codex": "gpt-5.6-sol"},
        "trials": [trial],
    }
    _write_json(campaign / "campaign" / "result.json", result)
    _write_json(campaign / "trials" / TRIAL_ID / "result.json", trial)
    _write_json(
        campaign / "trials" / TRIAL_ID / "harness-report.json",
        {"status": "completed", "final_output": {}, "lifecycle_events": []},
    )
    _write_json(campaign / "trials" / TRIAL_ID / "recovery.json", trial["recovery"])
    output = campaign / TRIAL_ID
    output.mkdir(parents=True)
    (output / "stdout.txt").write_text("agent response", encoding="utf-8")
    (output / "stderr.txt").write_text("", encoding="utf-8")
    _seal(campaign)
    _seal(matrix)


def test_matrix_evidence_store_recomputes_integrity_and_trial_detail(tmp_path):
    _artifacts(tmp_path)
    store = MatrixEvidenceStore(tmp_path)

    overview = store.overview(MATRIX_ID)
    detail = store.trial_detail(MATRIX_ID, TRIAL_ID)

    assert overview["summary"] == {
        "expected_trials": 1,
        "completed_trials": 1,
        "platform_valid": 1,
        "platform_invalid": 0,
        "diagnostic_only": 0,
        "fault_active": 1,
        "effect_verified": 1,
        "agent_recovery_verified": 1,
        "controller_cleanup_verified": 1,
        "business_recovery_verified": 1,
        "verdict_counts": {"PASS": 1},
    }
    assert overview["integrity"]["all_valid"] is True
    assert overview["source_matrices"] == [MATRIX_ID]
    assert overview["trials"][0]["duration_seconds"] == 3.0
    assert detail["agent"]["stdout"]["text"] == "agent response"
    assert [item["kind"] for item in detail["controller"]["events"]] == [
        "trial_started",
        "trial_finished",
    ]


def test_matrix_evidence_api_serves_overview_detail_and_artifact(tmp_path):
    _artifacts(tmp_path)
    client = TestClient(
        create_app(CampaignSupervisor(Runner()), artifact_root=tmp_path)
    )

    listed = client.get("/api/v1/matrices")
    overview = client.get(f"/api/v1/matrices/{MATRIX_ID}")
    detail = client.get(f"/api/v1/matrices/{MATRIX_ID}/trials/{TRIAL_ID}")
    report = client.get(f"/api/v1/matrices/{MATRIX_ID}/artifacts/report.json")

    assert listed.status_code == 200
    assert listed.json()["matrices"][0]["completed_trial_count"] == 1
    assert overview.status_code == 200
    assert overview.json()["summary"]["fault_active"] == 1
    assert detail.status_code == 200
    assert detail.json()["summary"]["trial_id"] == TRIAL_ID
    assert report.status_code == 200
    assert report.json()["matrix_id"] == MATRIX_ID
    assert client.get("/api/v1/matrices/not-safe").status_code == 404
