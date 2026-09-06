from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from harness.d0.common import write_manifest
from stage2_service.contracts import D0QualificationRef, HarnessKind
from stage2_service.gateway_config import GatewayConfigSnapshot
from stage2_service.qualification import D0QualificationGate


GATEWAY_CONFIG_SHA256 = "c" * 64
GATEWAY_ROUTE = {
    "model_alias": "gpt-5.5",
    "provider": "openai",
    "upstream_model": "gpt-5.5",
    "api_base_host": "gateway.example",
    "api_base_scheme": "https",
    "api_base_path": "/v1",
    "credential_env_ref": "UPSTREAM_API_KEY",
}


def _write_qualified_campaign(
    tmp_path: Path,
    *,
    campaign_id: str = "d0-otel-accounting-20260901-qualified",
    agent: str = "codex",
    model_alias: str = "gpt-5.5",
    gateway_trial_id: str = "d0-trial-codex",
    gateway_request_ids: tuple[str, ...] = ("req-1",),
    gateway_route: dict[str, str] | None = None,
    gateway_config_sha256: str = GATEWAY_CONFIG_SHA256,
    finished_at: str = "2026-09-06T10:00:00Z",
) -> tuple[D0QualificationRef, Path]:
    root = tmp_path / campaign_id
    route = gateway_route or GATEWAY_ROUTE
    receipt_ref = f"native/d0-campaign/{gateway_trial_id}/gateway-requests.json"
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
                    "gateway_config_sha256": gateway_config_sha256,
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
                "finished_at": finished_at,
                "host": {"verified": True},
                "models": {agent: model_alias},
                "results": [
                    {
                        "agent": agent,
                        "status": "PASS",
                        "model_alias": model_alias,
                        "gateway_route": route,
                        "gateway_config_sha256": gateway_config_sha256,
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
            gateway_route=route,
            gateway_config_sha256=gateway_config_sha256,
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


def _gateway_snapshot(tmp_path: Path) -> GatewayConfigSnapshot:
    path = tmp_path / "litellm.yaml"
    path.write_text(
        """
model_list:
  - model_name: gpt-5.5
    litellm_params:
      model: openai/gpt-5.5
      api_base: https://gateway.example/v1
      api_key: os.environ/UPSTREAM_API_KEY
""".strip(),
        encoding="utf-8",
    )
    return GatewayConfigSnapshot.from_file(path, required_aliases=("gpt-5.5",))


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


def test_select_verified_d0_ref_needs_only_current_harness_model(tmp_path):
    snapshot = _gateway_snapshot(tmp_path)
    ref, _root = _write_qualified_campaign(
        tmp_path,
        gateway_route=snapshot.route("gpt-5.5"),
        gateway_config_sha256=snapshot.config_sha256,
    )

    selected, reason = D0QualificationGate(tmp_path).select_verified_ref(
        harness=HarnessKind.CODEX,
        model_alias="gpt-5.5",
        gateway=snapshot,
    )

    assert selected == ref
    assert reason == "qualified"


def test_select_verified_d0_ref_skips_other_combination_gaps(tmp_path):
    snapshot = _gateway_snapshot(tmp_path)
    ref, _root = _write_qualified_campaign(
        tmp_path,
        agent="codex",
        model_alias="gpt-5.5",
        gateway_trial_id="d0-trial-codex-only",
        gateway_request_ids=("codex-req-1",),
        gateway_route=snapshot.route("gpt-5.5"),
        gateway_config_sha256=snapshot.config_sha256,
    )

    selected, reason = D0QualificationGate(tmp_path).select_verified_ref(
        harness=HarnessKind.CODEX,
        model_alias="gpt-5.5",
        gateway=snapshot,
    )

    assert selected == ref
    assert reason == "qualified"


def test_select_verified_d0_ref_ignores_stale_route_and_bad_records(tmp_path):
    snapshot = _gateway_snapshot(tmp_path)
    stale_route = {**snapshot.route("gpt-5.5"), "api_base_host": "old.example"}
    _write_qualified_campaign(
        tmp_path,
        campaign_id="d0-otel-accounting-20260906-stale-route",
        gateway_trial_id="d0-trial-stale",
        gateway_request_ids=("stale-req-1",),
        gateway_route=stale_route,
        gateway_config_sha256=snapshot.config_sha256,
        finished_at="2026-09-06T12:00:00Z",
    )
    bad_ref, bad_root = _write_qualified_campaign(
        tmp_path,
        campaign_id="d0-otel-accounting-20260906-bad-receipt",
        gateway_trial_id="d0-trial-bad",
        gateway_request_ids=("bad-req-1",),
        gateway_route=snapshot.route("gpt-5.5"),
        gateway_config_sha256=snapshot.config_sha256,
        finished_at="2026-09-06T13:00:00Z",
    )
    receipt_path = bad_root / "codex" / bad_ref.gateway_evidence_ref
    rows = json.loads(receipt_path.read_text(encoding="utf-8"))
    rows[0]["request_id"] = "tampered"
    receipt_path.write_text(json.dumps(rows), encoding="utf-8")
    good_ref, _good_root = _write_qualified_campaign(
        tmp_path,
        campaign_id="d0-otel-accounting-20260906-good",
        gateway_trial_id="d0-trial-good",
        gateway_request_ids=("good-req-1",),
        gateway_route=snapshot.route("gpt-5.5"),
        gateway_config_sha256=snapshot.config_sha256,
        finished_at="2026-09-06T11:00:00Z",
    )

    selected, reason = D0QualificationGate(tmp_path).select_verified_ref(
        harness=HarnessKind.CODEX,
        model_alias="gpt-5.5",
        gateway=snapshot,
    )

    assert selected == good_ref
    assert reason == "qualified"


def test_select_verified_d0_ref_picks_latest_verified_candidate(tmp_path):
    snapshot = _gateway_snapshot(tmp_path)
    _write_qualified_campaign(
        tmp_path,
        campaign_id="d0-otel-accounting-20260906-older",
        gateway_trial_id="d0-trial-older",
        gateway_request_ids=("older-req-1",),
        gateway_route=snapshot.route("gpt-5.5"),
        gateway_config_sha256=snapshot.config_sha256,
        finished_at="2026-09-06T09:00:00Z",
    )
    newer, _root = _write_qualified_campaign(
        tmp_path,
        campaign_id="d0-otel-accounting-20260906-newer",
        gateway_trial_id="d0-trial-newer",
        gateway_request_ids=("newer-req-1",),
        gateway_route=snapshot.route("gpt-5.5"),
        gateway_config_sha256=snapshot.config_sha256,
        finished_at="2026-09-06T12:00:00Z",
    )

    selected, reason = D0QualificationGate(tmp_path).select_verified_ref(
        harness=HarnessKind.CODEX,
        model_alias="gpt-5.5",
        gateway=snapshot,
    )

    assert selected == newer
    assert reason == "qualified"


def test_select_verified_d0_ref_returns_none_when_no_current_verified_record(tmp_path):
    snapshot = _gateway_snapshot(tmp_path)
    stale_route = {**snapshot.route("gpt-5.5"), "api_base_host": "old.example"}
    _write_qualified_campaign(
        tmp_path,
        campaign_id="d0-otel-accounting-20260906-stale-only",
        gateway_trial_id="d0-trial-stale-only",
        gateway_request_ids=("stale-only-req-1",),
        gateway_route=stale_route,
        gateway_config_sha256=snapshot.config_sha256,
        finished_at="2026-09-06T12:00:00Z",
    )

    selected, reason = D0QualificationGate(tmp_path).select_verified_ref(
        harness=HarnessKind.CODEX,
        model_alias="gpt-5.5",
        gateway=snapshot,
    )

    assert selected is None
    assert reason == "no verified D0 qualification matches current gateway route"


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
