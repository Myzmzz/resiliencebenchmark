from __future__ import annotations

from pathlib import Path

from controller.runtime_secrets import (
    BaselineCapabilityIssuer,
    PrivateRuntimeSecretStore,
)
from controller.trial_preparation import (
    EngineeringOtelBaselineMeasurer,
    PerTrialPreparer,
    TrialRuntimeContextStore,
)
from progression.controller import TrialTicket


def _ticket() -> TrialTicket:
    return TrialTicket(
        trial_id="run-1-L1-a1",
        run_id="run-1",
        episode_id="EPI-1",
        level_id="L1",
        attempt=1,
    )


def test_per_trial_preparation_orders_reset_rebind_formal_baseline_and_capability(
    tmp_path: Path,
) -> None:
    calls = []

    def reset(ticket, level):
        calls.append("reset")
        return {"verified": True, "evidence_refs": ["reset.json"]}

    def target(ticket, level):
        calls.append("target")
        return {
            "namespace": "otel-demo",
            "kind": "Pod",
            "name": "frontend-abc",
            "uid": "pod-uid",
            "component": "frontend",
        }

    def baseline(ticket, level, target):
        calls.append("baseline")
        return {
            "qualified": True,
            "summary": {
                "qualified": True,
                "measurementWindow": {
                    "durationSeconds": 600,
                    "measurementWindowSeconds": 300,
                    "calibrationWindowEligible": True,
                },
            },
            "evidence_refs": ["baseline.json"],
        }

    secret_store = PrivateRuntimeSecretStore(tmp_path / "private" / "secrets")
    preparer = PerTrialPreparer(
        reset_verifier=reset,
        target_resolver=target,
        baseline_measurer=baseline,
        capability_issuer=BaselineCapabilityIssuer(
            baseline_ledger_dir=tmp_path / "private" / "baseline-ledger",
            secret_store=secret_store,
            controller_pod_uid="controller-uid",
        ),
        context_store=TrialRuntimeContextStore(tmp_path / "private" / "contexts"),
    )

    context = preparer(_ticket(), {"level_id": "L1"})

    assert calls == ["reset", "target", "baseline"]
    assert context["status"] == "qualified"
    assert context["target"]["uid"] == "pod-uid"
    assert context["baseline_summary"]["measurementWindow"]["durationSeconds"] == 600
    assert context["cleanup_handle"].startswith("cleanup-")
    token = secret_store.get(context["baseline_gate_token_ref"])
    assert len(token) >= 32
    assert token not in str(context)


def test_engineering_baseline_uses_formal_reference_and_fresh_smoke(
    tmp_path: Path, monkeypatch
) -> None:
    formal_summary = {
        "qualified": True,
        "requests": 2280,
        "measurementWindow": {
            "calibrationWindowEligible": True,
            "durationSeconds": 600,
            "measurementWindowSeconds": 300,
        },
    }
    report = tmp_path / "formal-baseline.json"
    report.write_text(
        __import__("json").dumps({"qualified": True, "summary": formal_summary}),
        encoding="utf-8",
    )
    calls = []

    def smoke(*args, **kwargs):
        calls.append((args, kwargs))
        return {"summary": {"qualified": True, "requests": 474}}

    monkeypatch.setattr("controller.trial_preparation.wait_cleanup_workload", smoke)
    monkeypatch.setattr(
        "controller.trial_preparation.wait_application_ready", lambda *args: None
    )
    measurer = EngineeringOtelBaselineMeasurer(
        kubeconfig=tmp_path / "kubeconfig",
        workload_image="registry.example/otel@sha256:" + "a" * 64,
        formal_baseline_report=report,
    )

    result = measurer(_ticket(), {"level_id": "L1"}, {"uid": "pod-uid"})

    assert result["qualified"] is True
    assert result["formal_run_eligible"] is False
    assert result["summary"] == formal_summary
    assert result["fresh_smoke_summary"]["requests"] == 474
    assert calls[0][1]["duration_seconds"] == 60
