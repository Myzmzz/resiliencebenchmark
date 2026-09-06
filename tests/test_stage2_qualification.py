from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from harness.d0.common import write_manifest
from stage2_service.contracts import D0QualificationRef, HarnessKind
from stage2_service.qualification import D0QualificationGate


GATEWAY_CONFIG_SHA256 = "c" * 64
GATEWAY_ROUTE = {
    "model_alias": "gpt-5.5",
    "provider": "openai",
    "upstream_model": "gpt-5.5",
    "api_base_host": "gateway.example",
    "api_base_scheme": "https",
    "api_base_path": "/v1",
}


def _write_qualified_campaign(
    tmp_path: Path,
    *,
    campaign_id: str = "d0-otel-accounting-20260901-qualified",
    agent: str = "codex",
    model_alias: str = "gpt-5.5",
    gateway_trial_id: str = "d0-trial-codex",
    gateway_request_ids: tuple[str, ...] = ("req-1",),
) -> tuple[D0QualificationRef, Path]:
    root = tmp_path / campaign_id
    receipt_ref = "native/d0-campaign/d0-trial-codex/gateway-requests.json"
    receipt_path = root / agent / receipt_ref
    receipt_path.parent.mkdir(parents=True)
    receipt_path.write_text(
        json.dumps(
            [
                {
                    "trial_id": gateway_trial_id,
                    "harness": agent,
                    "model_alias": model_alias,
                    "request_id": request_id,
                    "gateway_config_sha256": GATEWAY_CONFIG_SHA256,
                    "outcome": "received",
                }
                for request_id in gateway_request_ids
            ]
        ),
        encoding="utf-8",
    )
    (root / "campaign.json").write_text(
        json.dumps(
            {
                "campaign_id": campaign_id,
                "status": "QUALIFIED",
                "host": {"verified": True},
                "models": {agent: model_alias},
                "results": [
                    {
                        "agent": agent,
                        "status": "PASS",
                        "model_alias": model_alias,
                        "gateway_route": GATEWAY_ROUTE,
                        "gateway_config_sha256": GATEWAY_CONFIG_SHA256,
                        "gateway_evidence_verified": True,
                        "gateway_request_ids": list(gateway_request_ids),
                        "gateway_evidence_ref": receipt_ref,
                        "gateway_trial_id": gateway_trial_id,
                        "post_recovery_convergence": {"verified": True},
                        "controller_deadline": {"agent_thread_stopped": True},
                        "adapter": {"failure_code": ""},
                        "foreign_crs_observed": [],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    write_manifest(root)
    digest = hashlib.sha256((root / "manifest.sha256").read_bytes()).hexdigest()
    return (
        D0QualificationRef(
            campaign_id=campaign_id,
            manifest_sha256=digest,
            agent_status="PASS",
            model_alias=model_alias,
            gateway_route=GATEWAY_ROUTE,
            gateway_config_sha256=GATEWAY_CONFIG_SHA256,
            gateway_evidence_verified=True,
            gateway_request_ids=gateway_request_ids,
            gateway_evidence_ref=receipt_ref,
            gateway_trial_id=gateway_trial_id,
        ),
        root,
    )


def _request_for(ref: D0QualificationRef, *, model_alias: str = "gpt-5.5"):
    request = SimpleNamespace(
        harnesses=(HarnessKind.CODEX,),
        qualification_mode="required",
        qualification_refs={HarnessKind.CODEX: ref},
        model_by_harness={HarnessKind.CODEX: model_alias},
    )
    return request


def test_required_gate_verifies_manifest_files_gateway_receipts_and_agent_pass(tmp_path):
    ref, _root = _write_qualified_campaign(tmp_path)

    result = D0QualificationGate(tmp_path).qualify(_request_for(ref))

    assert result["execution_allowed"] is True
    assert result["formal_eligible"] is True
    assert result["scored"] is True
    assert result["agents"]["codex"]["gateway_evidence_verified"] is True


@pytest.mark.parametrize(
    ("mutation", "expected_reason"),
    [
        ("campaign_tampered", "D0 Manifest entry digest mismatch"),
        ("receipt_missing", "D0 Manifest listed file is missing"),
        ("receipt_tampered", "D0 Manifest entry digest mismatch"),
        (
            "duplicate_agent",
            "D0 campaign must contain exactly one result for the requested agent",
        ),
        (
            "route_mismatch",
            "D0 gateway route does not match the qualification reference",
        ),
        ("model_mismatch", "D0 model identity does not match the formal Trial"),
        (
            "trial_mismatch",
            "D0 gateway trial id does not match the qualification reference",
        ),
        ("path_escape", "D0 gateway receipt path escaped campaign artifact root"),
        ("receipt_symlink", "D0 Manifest listed path contains a symbolic link"),
    ],
)
def test_required_gate_rejects_bound_evidence_mutations(
    tmp_path, mutation, expected_reason
):
    ref, root = _write_qualified_campaign(tmp_path)
    campaign_path = root / "campaign.json"
    campaign = json.loads(campaign_path.read_text(encoding="utf-8"))
    receipt_path = root / "codex" / ref.gateway_evidence_ref

    if mutation == "campaign_tampered":
        campaign["host"]["verified"] = False
        campaign_path.write_text(json.dumps(campaign), encoding="utf-8")
    elif mutation == "receipt_missing":
        receipt_path.unlink()
    elif mutation == "receipt_tampered":
        rows = json.loads(receipt_path.read_text(encoding="utf-8"))
        rows[0]["request_id"] = "wrong-req"
        receipt_path.write_text(json.dumps(rows), encoding="utf-8")
    elif mutation == "duplicate_agent":
        campaign["results"].append(dict(campaign["results"][0]))
        campaign_path.write_text(json.dumps(campaign), encoding="utf-8")
        write_manifest(root)
        ref = ref.model_copy(
            update={
                "manifest_sha256": hashlib.sha256(
                    (root / "manifest.sha256").read_bytes()
                ).hexdigest()
            }
        )
    elif mutation == "route_mismatch":
        campaign["results"][0]["gateway_route"] = {
            **GATEWAY_ROUTE,
            "upstream_model": "another-model",
        }
        campaign_path.write_text(json.dumps(campaign), encoding="utf-8")
        write_manifest(root)
        ref = ref.model_copy(
            update={
                "manifest_sha256": hashlib.sha256(
                    (root / "manifest.sha256").read_bytes()
                ).hexdigest()
            }
        )
    elif mutation == "model_mismatch":
        campaign["models"]["codex"] = "another-model"
        campaign_path.write_text(json.dumps(campaign), encoding="utf-8")
        write_manifest(root)
        ref = ref.model_copy(
            update={
                "manifest_sha256": hashlib.sha256(
                    (root / "manifest.sha256").read_bytes()
                ).hexdigest()
            }
        )
    elif mutation == "trial_mismatch":
        campaign["results"][0]["gateway_trial_id"] = "another-trial"
        campaign_path.write_text(json.dumps(campaign), encoding="utf-8")
        write_manifest(root)
        ref = ref.model_copy(
            update={
                "manifest_sha256": hashlib.sha256(
                    (root / "manifest.sha256").read_bytes()
                ).hexdigest()
            }
        )
    elif mutation == "path_escape":
        ref = ref.model_copy(
            update={"gateway_evidence_ref": "../gateway-requests.json"}
        )
    elif mutation == "receipt_symlink":
        target = root / "actual-gateway-requests.json"
        receipt_path.rename(target)
        receipt_path.symlink_to(target)

    result = D0QualificationGate(tmp_path).qualify(_request_for(ref))

    assert result["execution_allowed"] is False
    assert result["formal_eligible"] is False
    assert result["scored"] is False
    assert result["agents"]["codex"]["verified"] is False
    assert result["agents"]["codex"]["reason"] == expected_reason


def test_diagnostic_gate_allows_execution_but_never_scores_missing_d0():
    request = SimpleNamespace(
        harnesses=(HarnessKind.CLAUDE_CODE,),
        qualification_mode="diagnostic",
        qualification_refs={},
        model_by_harness={HarnessKind.CLAUDE_CODE: "claude-opus-5"},
    )

    result = D0QualificationGate(None).qualify(request)

    assert result["execution_allowed"] is True
    assert result["formal_eligible"] is False
    assert result["scored"] is False
