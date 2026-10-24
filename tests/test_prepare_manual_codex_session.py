from __future__ import annotations

import json
import stat

from scripts.prepare_manual_codex_session import (
    issue_baseline_capability,
    private_write,
    validate_recovery_report,
)


def test_private_write_replaces_session_atomically(tmp_path) -> None:
    output = tmp_path / "runtime" / "session.json"

    private_write(output, {"version": 1})
    private_write(output, {"version": 2})

    assert json.loads(output.read_text(encoding="utf-8")) == {"version": 2}
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert stat.S_IMODE(output.parent.stat().st_mode) == 0o700


def test_issue_baseline_capability_writes_token_hash_ledger(tmp_path) -> None:
    summary = {
        "qualified": True,
        "measurementWindow": {
            "durationSeconds": 600,
            "measurementWindowSeconds": 300,
            "calibrationWindowEligible": True,
        },
    }

    token, expires_at = issue_baseline_capability(
        baseline_dir=tmp_path / "baseline",
        controller_id="manual-codex-controller",
        run_id="manual-run-001",
        target_name="accounting-abc",
        target_uid="pod-uid",
        summary=summary,
        ttl_seconds=1800,
    )

    ledgers = list((tmp_path / "baseline").glob("*.json"))
    assert len(token) >= 32
    assert len(ledgers) == 1
    assert token not in ledgers[0].read_text(encoding="utf-8")
    assert json.loads(ledgers[0].read_text(encoding="utf-8"))["target_uid"] == "pod-uid"
    assert expires_at


def test_validate_recovery_report_requires_every_gate(tmp_path) -> None:
    path = tmp_path / "recovery.json"
    path.write_text(
        json.dumps(
            {
                "qualified": True,
                "gates": {
                    "fault_effect_verified": True,
                    "controller_cleanup_verified": True,
                    "global_inventory_zero": True,
                    "target_identity_stable": True,
                    "target_ready_without_restart": True,
                    "business_recovery_verified": True,
                },
            }
        ),
        encoding="utf-8",
    )

    assert validate_recovery_report(path, maximum_age_seconds=900)["qualified"] is True
