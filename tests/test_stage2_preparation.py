from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import json

import pytest

from stage2_service.contracts import MainFaultSpec, TargetSpec

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
    def __init__(self):
        self.selectors = []

    def list_namespaced_pod(self, namespace, label_selector):
        del namespace
        self.selectors.append(label_selector)
        return SimpleNamespace(
            items=[pod()] if label_selector.startswith("app.kubernetes.io") else []
        )


class Binding:
    component = "historical-fixed-component"


class Identity:
    episode_id = "EPI-OTEL-CART-DEADLINE-001"


class Internal:
    runtime_binding = Binding()
    identity = Identity()


class Episode:
    internal = Internal()


def test_preparer_rebinds_current_pod_and_issues_application_traffic_capability(tmp_path: Path):
    issuer = ApplicationTrafficCapabilityIssuer(
        ledger_dir=tmp_path / "ledger",
        controller_pod_uid="controller-uid",
        traffic_evidence=Traffic(),
    )
    core = Core()
    context = KubernetesTrialPreparer(core, issuer).prepare(
        "campaign-1234567890abcdef-codex-t1",
        Episode(),
        namespace="otel-demo",
        target=TargetSpec(namespace="otel-demo", component="cart"),
        main_fault=MainFaultSpec(
            fault_type="network-delay",
            duration_seconds=180,
            intensity={"delay_ms": 1000},
        ),
    )

    assert context.target.name == "cart-abc"
    assert context.target.uid == "uid-current"
    assert core.selectors[0] == "app.kubernetes.io/component=cart"
    assert context.main_fault["target"]["pod_uid"] == "uid-current"
    assert context.main_fault["duration_seconds"] == 180
    assert context.main_fault["intensity"] == {"delay_ms": 1000}
    assert context.main_fault["request_contract"]["omit_selector"] is True
    assert context.main_fault["selection_mode"] == "explicit_api_contract"
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
            "campaign-1234567890abcdef-codex-t1",
            Episode(),
            namespace="otel-demo",
            target=TargetSpec(namespace="otel-demo", component="cart"),
            main_fault=MainFaultSpec(
                fault_type="cpu-load",
                duration_seconds=300,
                intensity={"cpu_percent": 80},
            ),
        )


def test_agent_selected_preparation_does_not_choose_target_or_fault(tmp_path: Path):
    issuer = ApplicationTrafficCapabilityIssuer(
        ledger_dir=tmp_path / "ledger",
        controller_pod_uid="controller-uid",
        traffic_evidence=Traffic(),
    )
    core = Core()

    context = KubernetesTrialPreparer(core, issuer).prepare(
        "campaign-1234567890abcdef-codex-agent",
        Episode(),
        namespace="otel-demo",
        target=None,
        main_fault=None,
    )
    ledger = json.loads(next((tmp_path / "ledger").glob("*.json")).read_text())

    assert core.selectors == []
    assert context.target.name == "unbound"
    assert context.main_fault["selection_mode"] == "agent_strategy"
    assert context.main_fault["fault_type"] is None
    assert ledger["target_binding_mode"] == "agent_selected"
    assert ledger["target_name"] is None
    assert ledger["target_uid"] is None
