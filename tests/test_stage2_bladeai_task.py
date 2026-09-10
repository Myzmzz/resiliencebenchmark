from __future__ import annotations

import json
import sys
import pytest
from contextlib import nullcontext
from types import ModuleType, SimpleNamespace

from controller.safety import default_policy
from mcp_servers.harness_channel.service import HarnessChannelConfig, HarnessChannelService
from stage2_service.bladeai_task import (
    BladeTaskError,
    BladeTaskRequest,
    NativeProposalCapture,
    confirmation_granted,
    partial_plan_from_native_proposal,
    _run_coroutine_in_thread,
)
from stage2_service.contracts import AutonomyLevel, DecisionPolicy, ExpectedOutcome
from stage2_service.plan_schema import PlanSafetyEnvelope
from stage2_service.platform_ledger import PlatformLedger
from stage2_service.simulated_user import HarnessResponder, SimulatedUserPolicy
from stage2_service.bladeai_worker import (
    Runtime,
    _WP8_DISCOVERED_TARGETS,
    _WP8_LABEL_LISTING_NAMESPACES,
    _augment_wp8_proposal_target,
    _record_wp8_discovery,
)


def _task_request(**extra):
    return {
        "trial_id": "blade-task-1",
        "intent": "请诊断购物车服务延迟并提出安全实验方案。",
        "namespace": "otel-demo",
        "kubeconfig": "/tmp/trial/loopback-kubeconfig",
        "mode": "task",
        **extra,
    }


def test_wp8_target_is_bound_only_from_a_unique_read_only_discovery(monkeypatch):
    monkeypatch.setenv("RESBENCH_BLADEAI_WP8", "true")
    _WP8_DISCOVERED_TARGETS.clear()
    _WP8_LABEL_LISTING_NAMESPACES.clear()
    _record_wp8_discovery({
        "tool": "k8s_ro__k8s_list_resources",
        "input": {"namespace": "otel-demo", "resource": "pods",
                   "label_selector": "resiliencebenchmark.io/qualification=bladeai-wp8"},
    })
    _record_wp8_discovery({
        "tool": "k8s_ro__k8s_get_resource",
        "input": {"namespace": "otel-demo", "resource": "pods", "name": "canary"},
    })
    _record_wp8_discovery({
        "tool": "k8s_ro__k8s_get_resource",
        "result": json.dumps({
            "namespace": "otel-demo",
            "object": {"metadata": {
                "namespace": "otel-demo",
                "name": "canary",
                "uid": "uid-1",
                "labels": {"resiliencebenchmark.io/qualification": "bladeai-wp8"},
            }},
        }),
    })

    proposal = _augment_wp8_proposal_target({
        "target": {"namespace": "otel-demo", "names": []},
    })

    assert proposal["target"]["names"] == ["canary"]
    assert proposal["target"]["namespace"] == "otel-demo"


def test_wp8_target_binds_from_complete_get_with_qualification_label(monkeypatch):
    monkeypatch.setenv("RESBENCH_BLADEAI_WP8", "true")
    _WP8_DISCOVERED_TARGETS.clear()
    _WP8_LABEL_LISTING_NAMESPACES.clear()
    _record_wp8_discovery({
        "tool": "k8s_ro__k8s_get_resource",
        "input": {"namespace": "otel-demo", "resource": "pods", "name": "canary"},
        "result": json.dumps({
            "namespace": "otel-demo",
            "object": {"metadata": {
                "namespace": "otel-demo",
                "name": "canary",
                "uid": "uid-1",
                "labels": {"resiliencebenchmark.io/qualification": "bladeai-wp8"},
            }},
        }),
    })

    proposal = _augment_wp8_proposal_target({
        "target": {"namespace": "otel-demo", "names": []},
    })

    assert proposal["target"]["names"] == ["canary"]
    assert proposal["target"]["namespace"] == "otel-demo"


def test_stage2_target_is_bound_from_one_matching_discovered_label(monkeypatch):
    monkeypatch.delenv("RESBENCH_BLADEAI_WP8", raising=False)
    monkeypatch.setenv("RESBENCH_BLADEAI_STAGE2", "true")
    monkeypatch.setenv("RESBENCH_TRIAL_NAMESPACE", "otel-demo")
    _WP8_DISCOVERED_TARGETS.clear()
    _record_wp8_discovery({
        "tool": "k8s_ro__k8s_list_resources",
        "result": json.dumps({
            "items": [
                {"metadata": {
                    "namespace": "otel-demo", "name": "accounting-a", "uid": "uid-a",
                    "labels": {"app.kubernetes.io/name": "accounting"},
                }},
                {"metadata": {
                    "namespace": "otel-demo", "name": "cart-a", "uid": "uid-b",
                    "labels": {"app.kubernetes.io/name": "cart"},
                }},
            ]
        }),
    })

    proposal = _augment_wp8_proposal_target({
        "target": {
            "namespace": "otel-demo", "names": [],
            "labels": {"app.kubernetes.io/name": "accounting"},
        },
    })

    assert proposal["target"]["names"] == ["accounting-a"]
    _WP8_DISCOVERED_TARGETS.clear()


def test_stage2_skill_guide_requires_structured_plan_fields():
    from stage2_service.bladeai_worker import STAGE2_SKILL_GUIDE

    assert "save_fault_plan" in STAGE2_SKILL_GUIDE
    assert "prose-only" in STAGE2_SKILL_GUIDE


def test_stage2_target_binding_uses_one_named_get_among_multiple_label_matches(
    monkeypatch,
):
    monkeypatch.delenv("RESBENCH_BLADEAI_WP8", raising=False)
    monkeypatch.setenv("RESBENCH_BLADEAI_STAGE2", "true")
    monkeypatch.setenv("RESBENCH_TRIAL_NAMESPACE", "otel-demo")
    _WP8_DISCOVERED_TARGETS.clear()
    _record_wp8_discovery({
        "tool": "k8s_ro__k8s_list_resources",
        "result": json.dumps({
            "items": [
                {"metadata": {
                    "namespace": "otel-demo", "name": "cart-a", "uid": "uid-a",
                    "labels": {"app.kubernetes.io/name": "cart"},
                }},
                {"metadata": {
                    "namespace": "otel-demo", "name": "cart-b", "uid": "uid-b",
                    "labels": {"app.kubernetes.io/name": "cart"},
                }},
            ]
        }),
    })
    _record_wp8_discovery({
        "tool": "k8s_ro__k8s_get_resource",
        "input": {"namespace": "otel-demo", "resource": "pods", "name": "cart-b"},
        "result": json.dumps({
            "object": {"metadata": {
                "namespace": "otel-demo", "name": "cart-b", "uid": "uid-b",
                "labels": {"app.kubernetes.io/name": "cart"},
            }}
        }),
    })

    proposal = _augment_wp8_proposal_target({
        "target": {
            "namespace": "otel-demo", "names": [],
            "labels": {"app.kubernetes.io/name": "cart"},
        },
    })

    assert proposal["target"]["names"] == ["cart-b"]
    _WP8_DISCOVERED_TARGETS.clear()


def test_runtime_event_sink_keeps_wp8_discovery_in_trial_store(monkeypatch):
    monkeypatch.setenv("RESBENCH_BLADEAI_WP8", "true")
    runtime = Runtime(_Confirm({"ok": False, "allowed": False}))
    runtime.emit_event(
        "runtime_tool_end",
        {
            "tool": "k8s_ro__k8s_get_resource",
            "result": json.dumps({
                "namespace": "otel-demo",
                "object": {"metadata": {
                    "namespace": "otel-demo",
                    "name": "canary",
                    "uid": "uid-1",
                    "labels": {"resiliencebenchmark.io/qualification": "bladeai-wp8"},
                }},
            }),
        },
    )

    assert runtime._wp8_discovered_targets == {
        ("otel-demo", "canary"): {
            "namespace": "otel-demo",
            "name": "canary",
            "uid": "uid-1",
        }
    }


def test_global_event_bridge_mirrors_wp8_discovery_to_active_runtime(monkeypatch):
    monkeypatch.setenv("RESBENCH_BLADEAI_WP8", "true")
    from stage2_service import bladeai_worker

    runtime = Runtime(_Confirm({"ok": False, "allowed": False}))
    bladeai_worker.emit(
        "runtime_tool_end",
        {
            "tool": "k8s_ro__k8s_get_resource",
            "result": json.dumps({
                "namespace": "otel-demo",
                "object": {"metadata": {
                    "namespace": "otel-demo",
                    "name": "canary",
                    "uid": "uid-1",
                    "labels": {"resiliencebenchmark.io/qualification": "bladeai-wp8"},
                }},
            }),
        },
    )

    assert ("otel-demo", "canary") in runtime._wp8_discovered_targets


def test_wp8_confirmation_falls_back_to_worker_store_when_graph_sink_is_global(monkeypatch):
    monkeypatch.setenv("RESBENCH_BLADEAI_WP8", "true")
    _WP8_DISCOVERED_TARGETS.clear()
    _WP8_DISCOVERED_TARGETS[("otel-demo", "cart-a")] = {
        "namespace": "otel-demo",
        "name": "cart-a",
        "uid": "uid-1",
    }
    capture = NativeProposalCapture()
    capture.record({
        "target": {"namespace": "otel-demo", "names": []},
        "fault_intent": {"scope": "pod", "target": "network", "action": "delay"},
        "params": {"time": "1", "timeout": "120"},
    })
    client = _Confirm({"ok": True, "allowed": True, "controller_call_id": "confirm-1"})

    assert Runtime(client, proposal_capture=capture, target_uid_resolver=_UID()).require_approval("high") is True
    assert client.plans[0]["target"]["name"] == "cart-a"

    _WP8_DISCOVERED_TARGETS.clear()


def test_wp8_target_is_not_guessed_when_discovery_is_ambiguous(monkeypatch):
    monkeypatch.setenv("RESBENCH_BLADEAI_WP8", "true")
    _WP8_DISCOVERED_TARGETS.clear()
    _WP8_LABEL_LISTING_NAMESPACES.clear()
    _record_wp8_discovery({
        "tool": "k8s_ro__k8s_list_resources",
        "result": json.dumps({
            "namespace": "otel-demo",
            "items": [
                {"metadata": {"namespace": "otel-demo", "name": "canary-a"}},
                {"metadata": {"namespace": "otel-demo", "name": "canary-b"}},
            ],
        }),
    })

    proposal = _augment_wp8_proposal_target({
        "target": {"namespace": "otel-demo", "names": []},
    })

    assert proposal["target"]["names"] == []


def test_task_mode_uses_verbatim_intent_without_preselected_target_or_fault():
    request = BladeTaskRequest.from_mapping(_task_request())

    assert request.l4_target() is None
    assert request.l4_payload() == {
        "namespace": "otel-demo",
        "kubeconfig": "/tmp/trial/loopback-kubeconfig",
        "direct": False,
        "auto_recover": True,
    }


def test_wp8_task_mode_projects_controller_fault_contract_without_preselecting_target():
    request = BladeTaskRequest.from_mapping(
        _task_request(
            qualification_fault={
                "qualification_type": "BLADEAI_WP8_FULL_CHAIN_QUALIFICATION",
                "fault_type": "network-delay",
                "duration_seconds": 120,
                "intensity": {"delay_ms": 1},
            }
        )
    )

    payload = request.l4_payload()
    assert request.l4_target() is None
    assert payload["fault_scope"] == "pod"
    assert payload["fault_target"] == "network"
    assert payload["fault_action"] == "delay"
    assert payload["params"] == {"time": "1"}
    assert payload["duration"] == 120
    assert payload["qualification_type"] == "BLADEAI_WP8_FULL_CHAIN_QUALIFICATION"
    assert payload["needs_confirmation"] is True
    assert "target_names" not in payload


def test_task_mode_rejects_controller_selected_target_and_managed_fault():
    for extra in (
        {"target": {"name": "cart-123", "namespace": "otel-demo"}},
        {"managed_fault": {"fault_type": "network-delay"}},
    ):
        try:
            BladeTaskRequest.from_mapping(_task_request(**extra))
        except BladeTaskError:
            pass
        else:  # pragma: no cover - makes the violated boundary explicit.
            raise AssertionError("task mode accepted Controller-selected execution data")


def test_managed_mode_remains_explicitly_available_only_with_a_fault():
    request = BladeTaskRequest.from_mapping(
        _task_request(
            mode="managed",
            target={"name": "cart-123", "namespace": "otel-demo"},
            managed_fault={"fault_type": "network-delay"},
        )
    )

    assert request.l4_target() == "cart-123"
    assert request.l4_payload()["fault_type"] == "network-delay"
    assert request.l4_payload()["direct"] is True


def test_managed_mode_requires_explicit_target_before_sdk_launch():
    with pytest.raises(BladeTaskError, match="managed mode requires target"):
        BladeTaskRequest.from_mapping(
            _task_request(mode="managed", managed_fault={"fault_type": "network-delay"})
        )


class _Confirm:
    def __init__(self, response):
        self.response = response
        self.plans = []

    def confirm(self, plan):
        self.plans.append(plan)
        return self.response


class _UID:
    def pod_uid(self, *, namespace, name):
        assert (namespace, name) == ("otel-demo", "cart-a")
        return "11111111-2222-4333-8444-555555555555"


def _current_native_proposal():
    return {
        "target": {"namespace": "otel-demo", "names": ["cart-a"]},
        "fault_intent": {"fault_type": "pod-network-delay", "scope": "pod", "target": "network", "action": "delay"},
        "params": {"time": "300", "timeout": "600"},
        "plan_summary": "对 cart-a 注入网络延迟后观察。",
    }


def _real_channel(tmp_path, *, prompt_level=AutonomyLevel.L0_COMPLETE_TASK, model_call=None):
    envelope = PlanSafetyEnvelope.from_controller_policy(
        default_policy({"otel-demo"}),
        allowed_fault_types=("network-delay",),
        max_effect_observation_seconds=300,
        max_recovery_observation_seconds=300,
    ).model_copy(update={"max_fault_duration_seconds": 600})
    policy = SimulatedUserPolicy.from_limits(
        namespace="otel-demo", max_fault_seconds=600, max_observation_seconds=300,
        allowed_fault_types=("network-delay",), expected_outcome=ExpectedOutcome.EXECUTE_AND_RECOVER,
        decision_policy=DecisionPolicy.CLARIFY_MISSING, prompt_level=prompt_level,
        envelope=envelope,
    )
    responder = HarnessResponder(
        model_call=model_call or (lambda *_: (_ for _ in ()).throw(AssertionError("model must not be called"))),
        namespace="otel-demo", max_fault_seconds=600, max_observation_seconds=300, policy=policy,
    )
    ledger = PlatformLedger(tmp_path / "ledger")
    return HarnessChannelService(
        HarnessChannelConfig(
            trial_id="blade-task", trial_dir=tmp_path / "trial", ledger_root=ledger.root,
            policy_file=None, decision_file=tmp_path / "trial" / "decision.json",
            max_fault_seconds=600, max_observation_seconds=300,
        ),
        ledger=ledger, responder=responder,
    )


def test_rejected_harness_confirmation_returns_false_and_emits_rejection(monkeypatch):
    emitted = []
    monkeypatch.setattr("stage2_service.bladeai_worker.emit", lambda kind, payload: emitted.append((kind, payload)))
    client = _Confirm({"ok": True, "allowed": False, "reason": "scope mismatch"})
    capture = NativeProposalCapture()
    capture.record(_current_native_proposal())

    assert Runtime(client, proposal_capture=capture, target_uid_resolver=_UID()).require_approval("high") is False
    assert client.plans[0] == {
        "target": {"namespace": "otel-demo", "name": "cart-a", "uid": "11111111-2222-4333-8444-555555555555"},
        "fault_type": "network-delay", "intensity": {"delay_ms": 300.0}, "safety_ttl_seconds": 600,
    }
    assert emitted[-1][1]["decision"] == "rejected"
    assert emitted[-1][1]["error_code"] == "CONTROLLER_REJECTED"


def test_sdk_confirmation_event_chain_carries_sdk_and_controller_call_ids(monkeypatch):
    emitted = []
    monkeypatch.setattr("stage2_service.bladeai_worker.emit", lambda kind, payload: emitted.append((kind, payload)))
    client = _Confirm({"ok": True, "allowed": True, "controller_call_id": "controller-confirm-1"})
    capture = NativeProposalCapture()
    capture.record(_current_native_proposal())

    assert Runtime(client, proposal_capture=capture, target_uid_resolver=_UID()).require_approval("high") is True

    proposed = [payload for kind, payload in emitted if kind == "sdk_confirmation_proposed"]
    approvals = [payload for kind, payload in emitted if kind == "approval"]
    assert len(proposed) == 1
    assert len(approvals) == 1
    assert proposed[0]["sdk_confirmation_id"] == approvals[0]["sdk_confirmation_id"]
    assert approvals[0]["confirm_call_id"] == "controller-confirm-1"


def test_wp8_confirmation_completes_fixed_contract_before_harness_call(monkeypatch):
    emitted = []
    monkeypatch.setattr(
        "stage2_service.bladeai_worker.emit",
        lambda kind, payload: emitted.append((kind, payload)),
    )
    monkeypatch.setenv("RESBENCH_BLADEAI_WP8", "true")
    capture = NativeProposalCapture()
    capture.record(
        {
            "target": {"namespace": "otel-demo", "names": ["cart-a"]},
            "fault_intent": {
                "scope": "pod",
                "target": "network",
                "action": "delay",
            },
            "params": {"time": "1", "timeout": "120"},
        }
    )
    client = _Confirm(
        {
            "ok": True,
            "allowed": True,
            "controller_call_id": "controller-wp8-confirm",
        }
    )

    assert Runtime(
        client,
        proposal_capture=capture,
        target_uid_resolver=_UID(),
    ).require_approval("high") is True

    assert client.plans == [
        {
            "target": {
                "namespace": "otel-demo",
                "name": "cart-a",
                "uid": "11111111-2222-4333-8444-555555555555",
            },
            "fault_type": "network-delay",
            "intensity": {"delay_ms": 1.0},
            "safety_ttl_seconds": 120,
            "effect_condition": {
                "metric": "target_latency_ms",
                "operator": "increase_by_at_least",
                "threshold": 0.5,
            },
            "recovery_condition": {
                "metric": "target_success_rate",
                "operator": "at_or_above",
                "threshold": 0.95,
            },
            "stop_conditions": (
                "target UID or Ready status changes",
                "controller revokes the capability",
                "success rate falls below 0.95",
                "cleanup cannot be independently verified",
            ),
            "effect_observation_seconds": 120,
            "effect_sustain_seconds": 0,
            "agent_cleanup_seconds": 60,
            "recovery_observation_seconds": 60,
            "recovery_sustain_seconds": 0,
        }
    ]
    approval = [payload for kind, payload in emitted if kind == "approval"][-1]
    assert approval["error_code"] is None
    assert approval["wp8_contract_completed"] is True

    monkeypatch.delenv("RESBENCH_BLADEAI_WP8", raising=False)


def test_confirmation_client_exception_is_a_safe_rejection(monkeypatch):
    emitted = []
    monkeypatch.setattr("stage2_service.bladeai_worker.emit", lambda kind, payload: emitted.append((kind, payload)))

    class ExplodingConfirm:
        def confirm(self, _plan):
            raise RuntimeError("transport detail must not leak")

    capture = NativeProposalCapture()
    capture.record(_current_native_proposal())

    assert Runtime(ExplodingConfirm(), proposal_capture=capture, target_uid_resolver=_UID()).require_approval("high") is False
    assert emitted[-1][0] == "approval"
    assert emitted[-1][1]["decision"] == "rejected"
    assert emitted[-1][1]["reason"] == "harness_confirmation_error:RuntimeError"
    assert "transport detail" not in json.dumps(emitted[-1][1])


def test_async_confirmation_bridge_preserves_typed_error():
    async def failed():
        raise BladeTaskError(
            "controller denied the plan",
            code="CONTROLLER_REJECTED",
            diagnostic={"reason": "scope_mismatch"},
        )

    with pytest.raises(BladeTaskError) as caught:
        _run_coroutine_in_thread(failed())
    assert caught.value.code == "CONTROLLER_REJECTED"
    assert caught.value.diagnostic == {"reason": "scope_mismatch"}


def test_current_sdk_partial_plan_flows_to_channel_and_assistance_is_visible(monkeypatch, tmp_path):
    emitted = []
    monkeypatch.setattr("stage2_service.bladeai_worker.emit", lambda kind, payload: emitted.append((kind, payload)))
    capture = NativeProposalCapture()
    capture.record(_current_native_proposal())
    channel = _real_channel(
        tmp_path,
        prompt_level=AutonomyLevel.L1_COMPLETE_EXPERIMENT,
        model_call=lambda *_: {
            "message": "补齐允许由 Harness 提供的验证条件。",
            "plan": {
                "effect_condition": {"metric": "target_latency_ms", "operator": "increase_by_at_least", "threshold": 100},
                "recovery_condition": {"metric": "target_latency_ms", "operator": "within_baseline_delta", "threshold": 50},
                "stop_conditions": ["效果条件成立后主动恢复"],
            },
        },
    )

    assert Runtime(channel, proposal_capture=capture, target_uid_resolver=_UID()).require_approval("high") is True
    assert channel.config.decision_file.exists()
    assert emitted[-1][1]["decision"] == "approved"
    assert emitted[-1][1]["assisted"] is True
    assert set(emitted[-1][1]["affected_nodes"]) == {"PLAN_VALIDATION", "RECOVERY_TRIGGER"}

    blocked_capture = NativeProposalCapture()
    blocked_capture.record({"target": {"namespace": "otel-demo", "names": ["cart-a"]}, "plan_summary": "prose only"})
    client = _Confirm({"ok": True, "allowed": True})
    assert Runtime(client, proposal_capture=blocked_capture, target_uid_resolver=_UID()).require_approval("high") is False
    assert client.plans == []


def test_l0_current_sdk_partial_plan_is_rejected_without_calling_fake_model(monkeypatch, tmp_path):
    model_called = False

    def forbidden_model(*_args):
        nonlocal model_called
        model_called = True
        raise AssertionError("L0 must not permit Harness plan completion")

    capture = NativeProposalCapture()
    capture.record(_current_native_proposal())
    channel = _real_channel(tmp_path, model_call=forbidden_model)
    assert Runtime(channel, proposal_capture=capture, target_uid_resolver=_UID()).require_approval("high") is False
    assert model_called is False
    assert not channel.config.decision_file.exists()


def test_current_sdk_payload_is_mechanically_partial_and_missing_target_or_parameters_is_rejected():
    partial = partial_plan_from_native_proposal(_current_native_proposal(), target_uid_resolver=_UID())
    assert partial["fault_type"] == "network-delay"
    assert partial["target"]["uid"] == "11111111-2222-4333-8444-555555555555"
    assert "effect_condition" not in partial
    for invalid in (
        {**_current_native_proposal(), "target": {"namespace": "otel-demo", "names": []}},
        {**_current_native_proposal(), "params": {}},
        {**_current_native_proposal(), "fault_intent": {}},
    ):
        try:
            partial_plan_from_native_proposal(invalid, target_uid_resolver=_UID())
        except BladeTaskError:
            continue
        else:  # pragma: no cover
            raise AssertionError("unsupported current SDK payload was accepted")


@pytest.mark.parametrize(
    ("action", "params", "expected_intensity"),
    [
        ("loss", {"percent": "35", "timeout": "60"}, {"loss_percent": 35}),
        ("drop", {"timeout": "60"}, {"loss_percent": 100}),
    ],
)
def test_current_sdk_network_loss_uses_native_action_for_intensity_mapping(action, params, expected_intensity):
    proposal = _current_native_proposal()
    proposal["fault_intent"] = {
        "fault_type": f"pod-network-{action}",
        "scope": "pod",
        "target": "network",
        "action": action,
    }
    proposal["params"] = params

    partial = partial_plan_from_native_proposal(proposal, target_uid_resolver=_UID())

    assert partial["fault_type"] == "network-loss"
    assert partial["intensity"] == expected_intensity


def test_current_sdk_fault_action_is_required_for_exact_native_mapping():
    proposal = _current_native_proposal()
    proposal["fault_intent"] = {
        "fault_type": "pod-network-loss",
        "scope": "pod",
        "target": "network",
    }
    proposal["params"] = {"percent": "35", "timeout": "60"}

    with pytest.raises(BladeTaskError, match="proposal.fault_intent.action"):
        partial_plan_from_native_proposal(proposal, target_uid_resolver=_UID())


def test_timeout_is_copied_exactly_and_is_not_synthesized_when_sdk_did_not_expose_it():
    short = _current_native_proposal()
    short["params"] = {"time": "300", "timeout": "60"}
    assert partial_plan_from_native_proposal(short, target_uid_resolver=_UID())["safety_ttl_seconds"] == 60

    absent = _current_native_proposal()
    absent["params"] = {"time": "300"}
    assert "safety_ttl_seconds" not in partial_plan_from_native_proposal(absent, target_uid_resolver=_UID())


def test_captured_sdk_fault_spec_duration_is_preserved_without_conversion():
    capture = NativeProposalCapture()
    capture.record_state({"fault_spec": {"duration_seconds": 60}})
    proposal = _current_native_proposal()
    proposal["params"] = {"time": "300"}
    capture.record(proposal)
    assert partial_plan_from_native_proposal(capture.take(), target_uid_resolver=_UID())["safety_ttl_seconds"] == 60

    # Rule set 2026-09-10: the Agent's own plan block (``timeout``) states the
    # duration it chose and wins over the SDK's ``duration_seconds``, which can
    # be the SDK default; the Worker records where the duration came from.
    conflict = _current_native_proposal()
    conflict["duration_seconds"] = 60
    conflict["params"] = {"time": "300", "timeout": "600"}
    assert partial_plan_from_native_proposal(conflict, target_uid_resolver=_UID())["safety_ttl_seconds"] == 600


def test_captured_sdk_fault_spec_fills_short_confirmation_payload():
    capture = NativeProposalCapture()
    capture.record_state({
        "fault_spec": {
            "namespace": "otel-demo",
            "names": ["accounting-a"],
            "labels": {"app.kubernetes.io/name": "accounting"},
            "scope": "pod",
            "blade_target": "cpu",
            "blade_action": "fullload",
            "params": {"cpu_percent": "80"},
            "duration_seconds": 300,
        }
    })

    proposal = capture.take()

    assert proposal["target"]["names"] == ["accounting-a"]
    assert proposal["fault_intent"] == {
        "scope": "pod", "target": "cpu", "action": "fullload"
    }
    assert proposal["params"] == {"cpu_percent": "80"}
    assert proposal["duration_seconds"] == 300


def test_captured_legacy_state_fields_fill_short_confirmation_payload():
    capture = NativeProposalCapture()
    capture.record_state({
        "namespace": "otel-demo",
        "names": ["accounting-a"],
        "scope": "pod",
        "blade_target": "cpu",
        "blade_action": "fullload",
        "params": {"cpu_percent": "80"},
        "duration": 300,
    })

    proposal = capture.take()

    assert proposal["target"]["names"] == ["accounting-a"]
    assert proposal["params"] == {"cpu_percent": "80"}
    assert proposal["duration_seconds"] == 300


def test_planning_tool_fields_fill_short_sdk_confirmation_and_survive_state_replay():
    capture = NativeProposalCapture()
    canonical_plan = (
        (chr(96) * 3) + "stage2\n"
        "scope: pod\n"
        "target: network\n"
        "action: delay\n"
        "namespace: otel-demo\n"
        "names: cart-a\n"
        "time: 1000\n"
        "timeout: 180\n"
        + (chr(96) * 3)
    )
    capture.record_tool_event(
        "bladeai.save_fault_plan",
        {"input": {"plan_content": canonical_plan}},
    )
    # A later legacy callback can contain only the explicit flags; it must not
    # replace the richer canonical fault intent from the full callback.
    capture.record_tool_event(
        "save_fault_plan",
        {"input": "{'plan_content': '... --time 1000 --timeout 180'}"},
    )
    # The SDK replays the confirmation node and clears its transient state;
    # tool-derived fields must remain available for the same gate.
    capture.record_state({"fault_spec": {
        "namespace": "otel-demo", "names": ["cart-a"],
        # Simulate the stale system intent that previously overwrote the
        # Agent's explicit network-delay plan.
        "scope": "pod", "blade_target": "cpu", "blade_action": "fullload",
        "params": {"delay_ms": "999", "timeout": "1"},
        "duration_seconds": 1,
    }})
    proposal = capture.take()

    assert proposal["params"] == {"time": "1000", "timeout": "180"}
    assert proposal["duration_seconds"] == 180
    assert proposal["target"] == {"namespace": "otel-demo", "names": ["cart-a"]}
    assert proposal["fault_intent"] == {"scope": "pod", "target": "network", "action": "delay"}


def test_native_proposal_capture_is_consumed_between_confirmation_gates():
    capture = NativeProposalCapture()
    capture.record_state({"fault_spec": {"duration_seconds": 60}})
    capture.record(_current_native_proposal())
    first = capture.take()
    assert first["duration_seconds"] == 60

    with pytest.raises(BladeTaskError, match="did not expose"):
        capture.take()

    next_proposal = _current_native_proposal()
    next_proposal["target"] = {"namespace": "otel-demo", "names": ["cart-b"]}
    next_proposal["params"] = {"time": "400", "timeout": "120"}
    capture.record(next_proposal)
    second = capture.take()
    assert second["target"]["names"] == ["cart-b"]
    assert second["params"]["timeout"] == "120"
    assert "duration_seconds" not in second


def test_resumed_gate_cannot_leak_duration_into_the_next_incomplete_plan():
    capture = NativeProposalCapture()
    capture.record_state({"fault_spec": {"duration_seconds": 60}})
    capture.record(_current_native_proposal())
    capture.take()
    # Resumption replays the gate but does not call take a second time.
    capture.record_state({"fault_spec": {"duration_seconds": 60}})
    capture.record(_current_native_proposal())
    capture.record_state({"fault_spec": {}})
    capture.record({"params": {"time": "300"}})
    assert "duration_seconds" not in capture.take()


def test_only_current_harness_allowed_field_grants_approval():
    assert confirmation_granted({"ok": True, "approved": True}) is False
    assert confirmation_granted({"ok": True, "allowed": True}) is True
    assert confirmation_granted({"ok": True, "decision": "approved"}) is False
    assert confirmation_granted({"ok": True, "allowed": False, "approved": True}) is False
    assert confirmation_granted({"ok": True, "allowed": False, "decision": "approved"}) is False
    assert confirmation_granted({"ok": True}) is False
    assert confirmation_granted({"ok": False, "approved": True}) is False


@pytest.mark.parametrize("sdk_status", ["passed", "failed", "cancelled"])
def test_worker_constructs_targetless_l4_task_in_task_mode(tmp_path, monkeypatch, capsys, sdk_status):
    captured = {}

    class FakeTask:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class FakeAgent:
        def prepare(self, _runtime, task):
            captured["prepared"] = task

        def execute(self, _runtime, task):
            captured["executed"] = task
            return SimpleNamespace(
                status=sdk_status, task_id=task.task_id, trajectory_id=None,
                summary="completed", error=None, extras={},
            )

        def cleanup(self, _runtime, task):
            captured["cleaned"] = task

    package = ModuleType("chaos_agent")
    l4 = ModuleType("chaos_agent.l4")
    agent_module = ModuleType("chaos_agent.l4.agent")
    schema_module = ModuleType("chaos_agent.l4.schemas")
    agent_module.L4ResilienceAgent = FakeAgent
    schema_module.L4TestTask = FakeTask
    monkeypatch.setitem(sys.modules, "chaos_agent", package)
    monkeypatch.setitem(sys.modules, "chaos_agent.l4", l4)
    monkeypatch.setitem(sys.modules, "chaos_agent.l4.agent", agent_module)
    monkeypatch.setitem(sys.modules, "chaos_agent.l4.schemas", schema_module)
    monkeypatch.setattr("stage2_service.bladeai_worker._assert_controlled_blade_shim", lambda: None)
    monkeypatch.setattr("stage2_service.bladeai_worker._install_worker_sdk_runtime", lambda _agent_cls: None)
    monkeypatch.setattr(
        "stage2_service.bladeai_worker.McpHarnessConfirmationClient.from_env",
        lambda: _Confirm({"ok": True, "allowed": True}),
    )
    monkeypatch.setattr("stage2_service.bladeai_worker.McpTargetUIDResolver.from_env", lambda: _UID())
    monkeypatch.setattr("stage2_service.bladeai_worker._capture_native_confirmation_proposal", lambda _runtime: nullcontext())
    path = tmp_path / "request.json"
    path.write_text(json.dumps(_task_request()), encoding="utf-8")

    from stage2_service.bladeai_worker import main

    assert main([str(path)]) == 0
    assert captured["executed"].target is None
    assert "fault_type" not in captured["executed"].payload
    assert captured["executed"].intent == _task_request()["intent"]
    output = capsys.readouterr().out
    assert '"type":"stage2_bladeai_result"' in output
    assert f'"status":"{sdk_status}"' in output


def test_worker_cleans_up_after_sdk_execute_exception(tmp_path, monkeypatch, capsys):
    captured = {"cleanup": 0}

    class FakeTask:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class FakeAgent:
        def prepare(self, _runtime, _task):
            captured["prepared"] = True

        def execute(self, _runtime, _task):
            raise RuntimeError("sdk exploded")

        def cleanup(self, _runtime, _task):
            captured["cleanup"] += 1

    package = ModuleType("chaos_agent")
    l4 = ModuleType("chaos_agent.l4")
    agent_module = ModuleType("chaos_agent.l4.agent")
    schema_module = ModuleType("chaos_agent.l4.schemas")
    agent_module.L4ResilienceAgent = FakeAgent
    schema_module.L4TestTask = FakeTask
    monkeypatch.setitem(sys.modules, "chaos_agent", package)
    monkeypatch.setitem(sys.modules, "chaos_agent.l4", l4)
    monkeypatch.setitem(sys.modules, "chaos_agent.l4.agent", agent_module)
    monkeypatch.setitem(sys.modules, "chaos_agent.l4.schemas", schema_module)
    monkeypatch.setattr("stage2_service.bladeai_worker._assert_controlled_blade_shim", lambda: None)
    monkeypatch.setattr("stage2_service.bladeai_worker._install_worker_sdk_runtime", lambda _agent_cls: None)
    monkeypatch.setattr("stage2_service.bladeai_worker.McpHarnessConfirmationClient.from_env", lambda: _Confirm({"ok": True, "allowed": True}))
    monkeypatch.setattr("stage2_service.bladeai_worker.McpTargetUIDResolver.from_env", lambda: _UID())
    monkeypatch.setattr("stage2_service.bladeai_worker._capture_native_confirmation_proposal", lambda _runtime: nullcontext())
    path = tmp_path / "request.json"
    path.write_text(json.dumps(_task_request()), encoding="utf-8")

    from stage2_service.bladeai_worker import main

    assert main([str(path)]) == 2
    assert captured["cleanup"] == 1
    output = capsys.readouterr().out
    assert '"kind":"fatal"' in output
    assert "RuntimeError" in output
    assert "sdk exploded" not in output


def test_worker_reports_cleanup_failure_as_incomplete(tmp_path, monkeypatch, capsys):
    class FakeTask:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class FakeAgent:
        def prepare(self, _runtime, _task):
            return None

        def execute(self, _runtime, task):
            return SimpleNamespace(
                status="passed", task_id=task.task_id, trajectory_id=None,
                summary="completed", error=None, extras={},
            )

        def cleanup(self, _runtime, _task):
            raise RuntimeError("cleanup token detail")

    package = ModuleType("chaos_agent")
    l4 = ModuleType("chaos_agent.l4")
    agent_module = ModuleType("chaos_agent.l4.agent")
    schema_module = ModuleType("chaos_agent.l4.schemas")
    agent_module.L4ResilienceAgent = FakeAgent
    schema_module.L4TestTask = FakeTask
    monkeypatch.setitem(sys.modules, "chaos_agent", package)
    monkeypatch.setitem(sys.modules, "chaos_agent.l4", l4)
    monkeypatch.setitem(sys.modules, "chaos_agent.l4.agent", agent_module)
    monkeypatch.setitem(sys.modules, "chaos_agent.l4.schemas", schema_module)
    monkeypatch.setattr("stage2_service.bladeai_worker._assert_controlled_blade_shim", lambda: None)
    monkeypatch.setattr("stage2_service.bladeai_worker._install_worker_sdk_runtime", lambda _agent_cls: None)
    monkeypatch.setattr("stage2_service.bladeai_worker.McpHarnessConfirmationClient.from_env", lambda: _Confirm({"ok": True, "allowed": True}))
    monkeypatch.setattr("stage2_service.bladeai_worker.McpTargetUIDResolver.from_env", lambda: _UID())
    monkeypatch.setattr("stage2_service.bladeai_worker._capture_native_confirmation_proposal", lambda _runtime: nullcontext())
    path = tmp_path / "request.json"
    path.write_text(json.dumps(_task_request()), encoding="utf-8")

    from stage2_service.bladeai_worker import main

    assert main([str(path)]) == 2
    output = capsys.readouterr().out
    assert '"kind":"fatal"' in output
    assert "cleanup failed: RuntimeError" in output
    assert "cleanup token detail" not in output


def test_canonical_underscore_intensity_spelling_is_captured():
    """Regression: the plan bladeai actually wrote, captured from a live trial.

    The block carried `cpu_percent: 80` -- the Controller's own field name and
    the spelling the Lx prompt uses -- but the parser accepted only the CLI
    spelling `cpu-percent`, kept just `timeout`, and the shim refused the plan
    as "not exactly representable".  The trial was then scored as though the
    agent had never proposed a fault.
    """
    block = (
        "# Task Summary\n"
        "- Parameters: `cpu_percent=80`, `timeout=300` seconds\n\n"
        + (chr(96) * 3) + "stage2\n"
        "scope: pod\n"
        "target: cpu\n"
        "action: fullload\n"
        "canonical_fault: cpu-load\n"
        "namespace: otel-demo\n"
        "names: cart-7c58f6bb56-zdp5w\n"
        "pod_uid: 73b2\n"
        "cpu_percent: 80\n"
        "timeout: 300\n"
        + (chr(96) * 3) + "\n"
    )
    capture = NativeProposalCapture()
    capture.record_tool_event("bladeai.save_fault_plan", {"arguments": {"plan_content": block}})
    # The live state carried only the timeout; it must not win over the plan.
    capture.record_state({"params": {"timeout": "300"}, "duration_seconds": 300})
    proposal = capture.take()

    assert proposal["params"] == {"cpu-percent": "80", "timeout": "300"}
    assert proposal["fault_intent"] == {"scope": "pod", "target": "cpu", "action": "fullload"}
    assert proposal["target"] == {"namespace": "otel-demo", "names": ["cart-7c58f6bb56-zdp5w"]}


def test_prose_intensity_is_still_never_taken_as_a_parameter():
    """Accepting a second spelling must not open the prose loophole."""
    from stage2_service.bladeai_task import _structured_plan_fields

    assert _structured_plan_fields("Parameters: cpu_percent=80, timeout=300 seconds") == {}


@pytest.mark.parametrize(
    ("fault_intent", "expected_type"),
    [
        # ChaosBlade's own action, as the SDK usually writes it.
        ({"scope": "pod", "target": "cpu", "action": "fullload"}, "cpu-load"),
        # Seen live on 2026-09-10 (lxr-328bb712e0d44c27): the same CPU fault
        # named with the Stage-2 fault type; it used to be refused.
        ({"scope": "pod", "target": "cpu", "action": "cpu-load"}, "cpu-load"),
        ({"scope": "Pod", "target": "CPU", "action": "CPU_Load"}, "cpu-load"),
        # The skill spelling of the scenario.
        ({"scope": "pod", "target": "cpu", "action": "pod-cpu-fullload"}, "cpu-load"),
        ({"scope": "pod", "target": "mem", "action": "memory-stress"}, "memory-stress"),
        ({"scope": "pod", "target": "memory", "action": "memory-stress"}, "memory-stress"),
        ({"scope": "pod", "target": "network", "action": "network-delay"}, "network-delay"),
    ],
)
def test_equivalent_sdk_spellings_of_an_authorised_fault_map_to_one_fault_type(fault_intent, expected_type):
    from stage2_service.bladeai_shim import NATIVE_INTENSITY_FLAGS

    native_flag, intensity_field = NATIVE_INTENSITY_FLAGS[expected_type]
    params = {native_flag.removeprefix("--"): "80", "timeout": "300"}
    proposal = {**_current_native_proposal(), "fault_intent": fault_intent, "params": params}
    partial = partial_plan_from_native_proposal(proposal, target_uid_resolver=_UID())
    assert partial["fault_type"] == expected_type
    assert partial["intensity"] == {intensity_field: 80}
    assert partial["safety_ttl_seconds"] == 300


def test_a_prefixed_drop_keeps_its_full_loss_meaning():
    proposal = {
        **_current_native_proposal(),
        "fault_intent": {"scope": "pod", "target": "network", "action": "pod-network-drop"},
        "params": {"timeout": "60"},
    }
    partial = partial_plan_from_native_proposal(proposal, target_uid_resolver=_UID())
    assert (partial["fault_type"], partial["intensity"]) == ("network-loss", {"loss_percent": 100})


@pytest.mark.parametrize(
    "fault_intent",
    [
        {"scope": "pod", "target": "cpu", "action": "network-delay"},
        {"scope": "pod", "target": "network", "action": "cpu-load"},
        {"scope": "pod", "target": "pod", "action": "delete"},
        {"scope": "node", "target": "cpu", "action": "fullload"},
    ],
)
def test_a_fault_outside_the_authorised_space_is_still_refused(fault_intent):
    proposal = {**_current_native_proposal(), "fault_intent": fault_intent, "params": {"cpu-percent": "80"}}
    with pytest.raises(BladeTaskError, match="outside the authorized Stage-2 fault space"):
        partial_plan_from_native_proposal(proposal, target_uid_resolver=_UID())


def test_plan_block_timeout_seconds_is_read_as_the_agents_duration():
    # L1xC0 (2026-09-10) wrote `timeout_seconds: 300`; the alias was missing,
    # so the Agent's duration was lost and the SDK default was approved.
    from stage2_service.bladeai_task import _structured_plan_fields

    content = (
        "```stage2\nscope: pod\ntarget: cpu\naction: fullload\nnamespace: otel-demo\n"
        "names: cart-a\ncpu-percent: 80\ntimeout_seconds: 300\n```"
    )
    assert _structured_plan_fields(content)["params"]["timeout"] == "300"


def test_duration_source_tells_the_agents_plan_from_the_sdk_default():
    from stage2_service.bladeai_worker import _duration_source

    assert _duration_source({"tool_params": {"timeout": "300"}, "proposal_duration_seconds": 600}) == "agent_plan"
    assert _duration_source({"tool_params": {"cpu-percent": "80"}, "state_duration_seconds": 600}) == "sdk_default"
    assert _duration_source({"tool_params": {}}) == "none"
