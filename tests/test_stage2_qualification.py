from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

from stage2_service.contracts import D0QualificationRef, HarnessKind
from stage2_service.qualification import D0QualificationGate


def test_required_gate_verifies_manifest_host_and_agent_pass(tmp_path):
    campaign_id = "d0-otel-accounting-20260901-qualified"
    root = tmp_path / campaign_id
    root.mkdir()
    (root / "campaign.json").write_text(
        json.dumps(
            {
                "campaign_id": campaign_id,
                "status": "QUALIFIED",
                "host": {"verified": True},
                "models": {"codex": "gpt-5.6-sol"},
                "results": [
                    {
                        "agent": "codex",
                        "status": "PASS",
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
    (root / "manifest.sha256").write_text("abc  campaign.json\n", encoding="utf-8")
    digest = hashlib.sha256((root / "manifest.sha256").read_bytes()).hexdigest()
    request = SimpleNamespace(
        harnesses=(HarnessKind.CODEX,),
        qualification_mode="required",
        qualification_refs={
            HarnessKind.CODEX: D0QualificationRef(
                campaign_id=campaign_id,
                manifest_sha256=digest,
                agent_status="PASS",
                model_alias="gpt-5.6-sol",
            )
        },
        model_by_harness={HarnessKind.CODEX: "gpt-5.6-sol"},
    )

    result = D0QualificationGate(tmp_path).qualify(request)

    assert result["execution_allowed"] is True
    assert result["formal_eligible"] is True
    assert result["scored"] is True


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
