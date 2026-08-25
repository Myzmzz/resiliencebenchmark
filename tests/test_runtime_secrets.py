from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from controller.runtime_secrets import (
    BaselineCapabilityIssuer,
    PrivateRuntimeSecretStore,
    RuntimeSecretError,
)


def _summary() -> dict:
    return {
        "qualified": True,
        "requests": 2280,
        "failures": 0,
        "measurementWindow": {
            "durationSeconds": 600,
            "measurementWindowSeconds": 300,
            "calibrationWindowEligible": True,
        },
    }


def test_baseline_capability_keeps_raw_token_out_of_public_result_and_ledger(
    tmp_path: Path,
) -> None:
    secrets = PrivateRuntimeSecretStore(tmp_path / "private" / "secrets")
    issuer = BaselineCapabilityIssuer(
        baseline_ledger_dir=tmp_path / "private" / "baseline-ledger",
        secret_store=secrets,
        controller_pod_uid="controller-uid",
    )

    public = issuer.issue(
        trial_id="run-1-L1-a1",
        run_id="run-1",
        namespace="otel-demo",
        target_name="checkout-abc",
        target_uid="pod-uid",
        summary=_summary(),
    )
    token = secrets.get(public["baseline_gate_token_ref"])
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    ledger = json.loads(
        (tmp_path / "private" / "baseline-ledger" / f"{token_hash}.json").read_text()
    )

    assert token not in json.dumps(public)
    assert token not in json.dumps(ledger)
    assert ledger["target_uid"] == "pod-uid"
    assert ledger["passed"] is True


def test_short_smoke_cannot_issue_formal_baseline_capability(tmp_path: Path) -> None:
    issuer = BaselineCapabilityIssuer(
        baseline_ledger_dir=tmp_path / "private" / "baseline-ledger",
        secret_store=PrivateRuntimeSecretStore(tmp_path / "private" / "secrets"),
        controller_pod_uid="controller-uid",
    )
    summary = _summary()
    summary["measurementWindow"]["durationSeconds"] = 60

    with pytest.raises(RuntimeSecretError, match="600 seconds"):
        issuer.issue(
            trial_id="run-1-L1-a1",
            run_id="run-1",
            namespace="otel-demo",
            target_name="checkout-abc",
            target_uid="pod-uid",
            summary=summary,
        )
