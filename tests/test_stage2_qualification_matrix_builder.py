from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.build_stage2_qualification_matrix import HARNESSES, MODELS, build


def write_campaign(root: Path, model: str, *, failing: str | None = None) -> str:
    campaign_id = f"d0-otel-accounting-{model.replace('.', '-')}-qualified"
    campaign = root / campaign_id
    campaign.mkdir()
    (campaign / "campaign.json").write_text(
        json.dumps(
            {
                "campaign_id": campaign_id,
                "host": {"verified": True},
                "models": {harness: model for harness in HARNESSES},
                "results": [
                    {
                        "agent": harness,
                        "status": "NO_INJECTION" if harness == failing else "PASS",
                    }
                    for harness in HARNESSES
                ],
            }
        ),
        encoding="utf-8",
    )
    (campaign / "manifest.sha256").write_text(
        "a" * 64 + "  campaign.json\n", encoding="utf-8"
    )
    return campaign_id


def test_builds_model_bound_refs_only_from_eight_passes(tmp_path):
    assignments = {model: write_campaign(tmp_path, model) for model in MODELS}

    result = build(tmp_path, assignments)

    assert result["schema_version"] == "stage2-qualification-matrix.v1"
    assert set(result["models"]) == set(MODELS)
    for model in MODELS:
        assert set(result["models"][model]) == set(HARNESSES)
        assert all(
            ref["model_alias"] == model
            for ref in result["models"][model].values()
        )


def test_rejects_non_pass_d0_result(tmp_path):
    assignments = {
        model: write_campaign(
            tmp_path,
            model,
            failing="deepseek-harness" if model == MODELS[0] else None,
        )
        for model in MODELS
    }

    with pytest.raises(ValueError, match="deepseek-harness=NO_INJECTION"):
        build(tmp_path, assignments)
