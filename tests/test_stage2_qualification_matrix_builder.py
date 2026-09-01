from __future__ import annotations

import json
from pathlib import Path

from scripts.build_stage2_qualification_matrix import HARNESSES, MODELS, build


def write_campaign(
    root: Path,
    model: str,
    harness: str,
    *,
    status: str = "PASS",
    converged: bool = True,
) -> str:
    campaign_id = (
        f"d0-otel-accounting-{model.replace('.', '-')}-{harness}-qualified"
    )
    campaign = root / campaign_id
    campaign.mkdir()
    (campaign / "campaign.json").write_text(
        json.dumps(
            {
                "campaign_id": campaign_id,
                "host": {"verified": True},
                "models": {harness: model},
                "results": [
                    {
                        "agent": harness,
                        "status": status,
                        "post_recovery_convergence": {"verified": converged},
                        "controller_deadline": {"agent_thread_stopped": True},
                        "adapter": {"failure_code": ""},
                        "foreign_crs_observed": [],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (campaign / "manifest.sha256").write_text(
        "a" * 64 + "  campaign.json\n", encoding="utf-8"
    )
    return campaign_id


def assignments(root: Path) -> dict[tuple[str, str], str]:
    return {
        (model, harness): write_campaign(root, model, harness)
        for model in MODELS
        for harness in HARNESSES
    }


def test_builds_eight_model_harness_bound_refs(tmp_path):
    result = build(tmp_path, assignments(tmp_path))

    assert result["schema_version"] == "stage2-qualification-matrix.v1"
    assert set(result["models"]) == set(MODELS)
    for model in MODELS:
        assert set(result["models"][model]) == set(HARNESSES)
        assert all(
            ref["model_alias"] == model
            for ref in result["models"][model].values()
        )


def test_accepts_real_behavior_outcome_when_platform_converged(tmp_path):
    values = assignments(tmp_path)
    key = (MODELS[0], "deepseek-harness")
    campaign = tmp_path / values[key] / "campaign.json"
    payload = json.loads(campaign.read_text(encoding="utf-8"))
    payload["results"][0]["status"] = "EFFECT_UNVERIFIED"
    campaign.write_text(json.dumps(payload), encoding="utf-8")

    result = build(tmp_path, values)

    assert result["models"][MODELS[0]]["deepseek-harness"]["agent_status"] == (
        "EFFECT_UNVERIFIED"
    )


def test_preserves_platform_invalid_pair_for_diagnostic_stage2(tmp_path):
    values = assignments(tmp_path)
    key = (MODELS[0], "deepseek-harness")
    campaign = tmp_path / values[key] / "campaign.json"
    payload = json.loads(campaign.read_text(encoding="utf-8"))
    payload["results"][0]["status"] = "CASE_INVALID"
    campaign.write_text(json.dumps(payload), encoding="utf-8")

    result = build(tmp_path, values)

    entry = result["models"][MODELS[0]]["deepseek-harness"]
    assert entry["agent_status"] == "CASE_INVALID"
    assert entry["evaluation_ready"] is False
    assert "diagnostic-only" in entry["invalid_reason"]
