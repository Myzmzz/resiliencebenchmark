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

    conflict = _current_native_proposal()
    conflict["duration_seconds"] = 60
    conflict["params"] = {"time": "300", "timeout": "600"}
    try:
        partial_plan_from_native_proposal(conflict, target_uid_resolver=_UID())
    except BladeTaskError as exc:
        assert "disagree" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("conflicting source durations must not be silently resolved")


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
