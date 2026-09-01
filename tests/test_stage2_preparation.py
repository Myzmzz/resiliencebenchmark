from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import json

import pytest

from stage2_service.preparation import (
    ApplicationTrafficCapabilityIssuer,
    KubernetesTrialPreparer,
    PreparationError,
)


class Traffic:
    def __init__(self, observed=True):
        self.observed = observed

    def current(self):
        return {
            "application_owned": True,
            "load_generator_ready": True,
            "traffic_observed": self.observed,
        }

    def record_baseline(self, trial_id, evidence):
        self.trial_id = trial_id
        self.baseline = dict(evidence)


def pod(name="cart-abc", uid="uid-current"):
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name, uid=uid),
        status=SimpleNamespace(
            conditions=[SimpleNamespace(type="Ready", status="True")]
        ),
    )


class Core:
    def list_namespaced_pod(self, namespace, label_selector):
        del namespace
        return SimpleNamespace(
            items=[pod()] if label_selector.startswith("app.kubernetes.io") else []
        )


class Binding:
    component = "cart"


class MainFault:
    def model_dump(self, mode):
        del mode
        return {
            "fault_type": "network-delay",
            "target": {},
            "parameters": {"delay_ms": 1200, "interface": "eth0"},
            "duration_seconds": 600,
            "effect_verification": [{"criterion_id": "latency_delta"}],
        }


class Identity:
    episode_id = "EPI-OTEL-CART-DEADLINE-001"


class Internal:
    runtime_binding = Binding()
    main_fault = MainFault()
    identity = Identity()


class Episode:
    internal = Internal()


def test_preparer_rebinds_current_pod_and_issues_application_traffic_capability(tmp_path: Path):
    issuer = ApplicationTrafficCapabilityIssuer(
        ledger_dir=tmp_path / "ledger",
        controller_pod_uid="controller-uid",
        traffic_evidence=Traffic(),
    )
    context = KubernetesTrialPreparer(Core(), issuer).prepare(
        "campaign-1234567890abcdef-codex-t1", Episode()
    )

    assert context.target.name == "cart-abc"
    assert context.target.uid == "uid-current"
    assert context.main_fault["target"]["pod_uid"] == "uid-current"
    assert context.main_fault["duration_seconds"] == 180
    assert context.main_fault["intensity"] == {"delay_ms": 1000}
    assert context.main_fault["request_contract"]["omit_selector"] is True
    assert "interface" not in context.main_fault["intensity"]
    assert len(context.baseline_capability) >= 32
    assert list((tmp_path / "ledger").glob("*.json"))

    rebound = issuer.rebind(
        context.trial_id,
        namespace="otel-demo",
        target_name="cart-new",
        target_uid="uid-new",
    )
    payload = json.loads(next((tmp_path / "ledger").glob("*.json")).read_text())
    assert rebound["baseline_capability_rebound"] is True
    assert payload["target_name"] == "cart-new"
    assert payload["target_uid"] == "uid-new"


def test_application_traffic_capability_fails_when_builtin_traffic_is_not_observed(tmp_path: Path):
    issuer = ApplicationTrafficCapabilityIssuer(
        ledger_dir=tmp_path / "ledger",
        controller_pod_uid="controller-uid",
        traffic_evidence=Traffic(observed=False),
    )
    with pytest.raises(PreparationError, match="traffic evidence"):
        KubernetesTrialPreparer(Core(), issuer).prepare(
            "campaign-1234567890abcdef-codex-t1", Episode()
        )
