from __future__ import annotations

import json
from pathlib import Path

from harness.d0.adapters import AdapterResult
from harness.d0.campaign import D0Campaign
from harness.d0.common import utc_now
from scripts.build_stage2_qualification_matrix import HARNESSES, MODELS, build

GATEWAY_HASH = "f" * 64


def route_for(model: str) -> dict[str, str]:
    return {
        "model_alias": model,
        "provider": "openai",
        "upstream_model": model,
        "api_base_host": "gateway.example",
        "api_base_scheme": "https",
        "api_base_path": "/v1",
        "credential_env_ref": "UPSTREAM_API_KEY",
    }


def write_campaign(
    root: Path,
    model: str,
    harness: str,
    *,
    status: str = "PASS",
    converged: bool = True,
    include_gateway: bool = True,
) -> str:
    campaign_id = (
        f"d0-otel-accounting-{model.replace('.', '-')}-{harness}-qualified"
    )
    campaign = root / campaign_id
    campaign.mkdir()
    trial_id = f"{campaign_id}-{harness}"
    request_ids = [f"{campaign_id}-{harness}-req-1"]
    gateway_evidence_ref = f"native/{campaign_id}/{trial_id}/gateway-requests.json"
    if include_gateway:
        write_gateway_artifact(
            campaign / harness / gateway_evidence_ref,
            trial_id=trial_id,
            harness=harness,
            model=model,
            request_ids=request_ids,
        )
    (campaign / "campaign.json").write_text(
        json.dumps(
            {
                "campaign_id": campaign_id,
                "host": {"verified": True},
                "models": {harness: model},
                "results": [
                    ({
                        "agent": harness,
                        "model_alias": model,
                        "status": status,
                        "post_recovery_convergence": {"verified": converged},
                        "controller_deadline": {"agent_thread_stopped": True},
                        "adapter": {"failure_code": ""},
                        "foreign_crs_observed": [],
                    }
                    | (
                        {
                            "gateway_route": route_for(model),
                            "gateway_config_sha256": GATEWAY_HASH,
                            "gateway_evidence_verified": True,
                            "gateway_request_ids": request_ids,
                            "gateway_evidence_ref": gateway_evidence_ref,
                            "gateway_trial_id": trial_id,
                        }
                        if include_gateway
                        else {}
                    ))
                ],
            }
        ),
        encoding="utf-8",
    )
    (campaign / "manifest.sha256").write_text(
        "a" * 64 + "  campaign.json\n", encoding="utf-8"
    )
    return campaign_id


def write_gateway_artifact(
    path: Path,
    *,
    trial_id: str,
    harness: str,
    model: str,
    request_ids: list[str],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            [
                {
                    "trial_id": trial_id,
                    "harness": harness,
                    "model_alias": model,
                    "request_id": request_id,
                    "gateway_config_sha256": GATEWAY_HASH,
                    "outcome": "received",
                }
                for request_id in request_ids
            ]
        ),
        encoding="utf-8",
    )


def assignments(root: Path) -> dict[tuple[str, str], str]:
    return {
        (model, harness): write_campaign(root, model, harness)
        for model in MODELS
        for harness in HARNESSES
    }


class ProducerState:
    effect_monotonic = 1.0
    recovery_observed_at = "2026-09-01T00:05:00Z"
    effect_confirmed_at = "2026-09-01T00:00:00Z"
    new_cr_names = {"d0-cr"}
    maximum_cpu_millicores = 800
    samples = 6
    errors: list[str] = []
    foreign_cr_names: set[str] = set()


class ProducerObserver:
    state = ProducerState()

    @staticmethod
    def fault_duration_seconds() -> float:
        return 300.0


def write_campaign_from_d0_producer(root: Path, model: str, harness: str) -> str:
    campaign_id = f"d0-otel-accounting-{model.replace('.', '-')}-{harness}-producer"
    campaign = root / campaign_id
    campaign.mkdir()
    trial_id = f"{campaign_id}-{harness}"
    request_ids = [f"{campaign_id}-{harness}-req-1"]
    gateway_evidence_ref = f"native/{campaign_id}/{trial_id}/gateway-requests.json"
    write_gateway_artifact(
        campaign / harness / gateway_evidence_ref,
        trial_id=trial_id,
        harness=harness,
        model=model,
        request_ids=request_ids,
    )
    result = D0Campaign._result(
        harness,
        campaign / harness,
        ProducerObserver(),
        AdapterResult(
            status="finished",
            started_at=utc_now(),
            finished_at=utc_now(),
            process_status="completed",
            artifact_ref="harness-report.json",
            agent_recovery_requested=True,
            tool_calls=4,
            confirmations=1,
            model_alias=model,
            gateway_route=route_for(model),
            gateway_config_sha256=GATEWAY_HASH,
            gateway_evidence_verified=True,
            gateway_request_ids=tuple(request_ids),
            gateway_evidence_ref=gateway_evidence_ref,
            gateway_trial_id=trial_id,
        ),
        {"requested": False},
        {"verified": True},
        0.0,
        {
            "agent_recovery_requested": True,
            "agent_effect_check_observed": True,
            "agent_recovery_check_observed": True,
            "agent_target_discovered": True,
        },
        {"agent_thread_stopped": True, "foreign_interference_observed": False},
    )
    (campaign / "campaign.json").write_text(
        json.dumps(
            {
                "campaign_id": campaign_id,
                "host": {"verified": True},
                "models": {harness: model},
                "results": [result],
            }
        ),
        encoding="utf-8",
    )
    (campaign / "manifest.sha256").write_text(
        "a" * 64 + "  campaign.json\n", encoding="utf-8"
    )
    return campaign_id


def producer_assignments(root: Path) -> dict[tuple[str, str], str]:
    return {
        (model, harness): write_campaign_from_d0_producer(root, model, harness)
        for model in MODELS
        for harness in HARNESSES
    }


def gateway_artifact_path(root: Path, campaign_id: str, harness: str) -> Path:
    campaign = root / campaign_id / "campaign.json"
    result = json.loads(campaign.read_text(encoding="utf-8"))["results"][0]
    return root / campaign_id / harness / result["gateway_evidence_ref"]


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
        assert all(
            ref["gateway_config_sha256"] == GATEWAY_HASH
            and ref["gateway_route"] == route_for(model)
            and ref["gateway_evidence_verified"] is True
            and ref["gateway_request_ids"]
            and ref["gateway_evidence_ref"].endswith("/gateway-requests.json")
            and ref["gateway_trial_id"]
            for ref in result["models"][model].values()
        )


def test_builder_consumes_real_d0_producer_gateway_fields(tmp_path):
    result = build(tmp_path, producer_assignments(tmp_path))

    sample = result["models"][MODELS[0]]["codex"]
    assert sample["gateway_evidence_verified"] is True
    assert sample["gateway_request_ids"]
    assert sample["gateway_evidence_ref"].endswith("/gateway-requests.json")
    assert sample["gateway_trial_id"].endswith("-codex")


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


def test_rejects_legacy_d0_campaign_without_gateway_route_evidence(tmp_path):
    values = {
        (model, harness): write_campaign(
            tmp_path,
            model,
            harness,
            include_gateway=not (model == MODELS[0] and harness == "codex"),
        )
        for model in MODELS
        for harness in HARNESSES
    }

    try:
        build(tmp_path, values)
    except ValueError as exc:
        assert "D0 gateway route evidence is missing" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("legacy D0 evidence without route metadata must be rejected")


def test_rejects_d0_campaign_without_verified_gateway_evidence(tmp_path):
    values = assignments(tmp_path)
    campaign = tmp_path / values[(MODELS[0], "codex")] / "campaign.json"
    payload = json.loads(campaign.read_text(encoding="utf-8"))
    payload["results"][0]["gateway_evidence_verified"] = False
    campaign.write_text(json.dumps(payload), encoding="utf-8")

    try:
        build(tmp_path, values)
    except ValueError as exc:
        assert "D0 gateway request evidence is missing" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("unverified gateway evidence must be rejected")


def test_rejects_d0_campaign_with_empty_gateway_request_ids(tmp_path):
    values = assignments(tmp_path)
    campaign = tmp_path / values[(MODELS[0], "codex")] / "campaign.json"
    payload = json.loads(campaign.read_text(encoding="utf-8"))
    payload["results"][0]["gateway_request_ids"] = []
    campaign.write_text(json.dumps(payload), encoding="utf-8")

    try:
        build(tmp_path, values)
    except ValueError as exc:
        assert "D0 gateway request evidence is missing" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("empty gateway request ids must be rejected")


def test_rejects_d0_campaign_with_mismatched_gateway_artifact(tmp_path):
    values = assignments(tmp_path)
    key = (MODELS[0], "codex")
    path = gateway_artifact_path(tmp_path, values[key], key[1])
    rows = json.loads(path.read_text(encoding="utf-8"))
    rows[0]["trial_id"] = "wrong-trial"
    path.write_text(json.dumps(rows), encoding="utf-8")

    try:
        build(tmp_path, values)
    except ValueError as exc:
        assert "D0 gateway request evidence is missing" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("mismatched gateway artifact content must be rejected")


def test_rejects_d0_campaign_with_duplicate_gateway_request_ids(tmp_path):
    values = assignments(tmp_path)
    campaign = tmp_path / values[(MODELS[0], "codex")] / "campaign.json"
    payload = json.loads(campaign.read_text(encoding="utf-8"))
    request_id = payload["results"][0]["gateway_request_ids"][0]
    payload["results"][0]["gateway_request_ids"] = [request_id, request_id]
    campaign.write_text(json.dumps(payload), encoding="utf-8")

    try:
        build(tmp_path, values)
    except ValueError as exc:
        assert "D0 gateway request evidence is missing" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("duplicate gateway request ids must be rejected")


def test_rejects_d0_campaign_with_symlink_gateway_artifact(tmp_path):
    values = assignments(tmp_path)
    key = (MODELS[0], "codex")
    path = gateway_artifact_path(tmp_path, values[key], key[1])
    target = tmp_path / "outside-gateway-requests.json"
    target.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    path.unlink()
    path.symlink_to(target)

    try:
        build(tmp_path, values)
    except ValueError as exc:
        assert "D0 gateway request evidence is missing" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("symlink gateway artifact must be rejected")


def test_rejects_legacy_d0_campaign_with_old_sidecar_wrapper(tmp_path):
    values = assignments(tmp_path)
    campaign = tmp_path / values[(MODELS[0], "codex")] / "campaign.json"
    payload = json.loads(campaign.read_text(encoding="utf-8"))
    result = payload["results"][0]
    result.pop("gateway_evidence_verified")
    result.pop("gateway_request_ids")
    result.pop("gateway_evidence_ref")
    result.pop("gateway_trial_id")
    result["gateway_sidecar_evidence"] = {
        "verified": True,
        "audit_ref": "legacy-gateway.jsonl",
    }
    campaign.write_text(json.dumps(payload), encoding="utf-8")

    try:
        build(tmp_path, values)
    except ValueError as exc:
        assert "D0 gateway request evidence is missing" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("legacy sidecar proof wrapper must be rejected")
