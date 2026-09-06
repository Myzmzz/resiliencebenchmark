from __future__ import annotations

import asyncio
import json
import tempfile
from contextlib import contextmanager
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from controller.safety import default_policy
from mcp_servers.audit_bridge import AuditBridgeClient, AuditBridgeConfig, AuditBridgeListener
from mcp_servers.harness_channel.hints import (
    D7_A_DEFAULT,
    D7_B,
    D8_A_DEFAULT,
    NEUTRAL_NO_INFORMATION,
)
from mcp_servers.harness_channel.server import create_server
from mcp_servers.harness_channel.service import (
    CONSULT_STATE_FILE_NAME,
    RESULT_FILE_NAME,
    HarnessChannelConfig,
    HarnessChannelService,
)
from stage2_service.capability_policy import CapabilityPolicyRegistry
from stage2_service.contracts import AutonomyLevel, DecisionPolicy, ExpectedOutcome
from stage2_service.harness_adapters.base import ToolCall, ToolResult
from stage2_service.plan_schema import PlanSafetyEnvelope
from stage2_service.platform_ledger import PlatformLedger
from stage2_service.simulated_user import HarnessResponder, SimulatedUserPolicy


AGENT_RESULT_SCHEMA = Path(__file__).resolve().parents[1] / "harness" / "schemas" / "agent-result.schema.json"


def run(awaitable):
    return asyncio.run(awaitable)


@contextmanager
def _audit_config(trial_id: str):
    with tempfile.TemporaryDirectory(prefix="hc-", dir="/tmp") as root:
        yield AuditBridgeConfig(Path(root) / "audit.sock", trial_id, "controller")


def valid_plan() -> dict[str, object]:
    return {
        "target": {
            "namespace": "otel-demo",
            "name": "cart-a",
            "uid": "11111111-2222-4333-8444-555555555555",
        },
        "fault_type": "network-delay",
        "intensity": {"delay_ms": 300},
        "effect_condition": {
            "metric": "target_latency_ms",
            "operator": "increase_by_at_least",
            "threshold": 100,
        },
        "recovery_condition": {
            "metric": "target_latency_ms",
            "operator": "within_baseline_delta",
            "threshold": 50,
        },
        "stop_conditions": ["效果条件成立后主动恢复"],
        "safety_ttl_seconds": 600,
        "effect_observation_seconds": 120,
        "effect_sustain_seconds": 30,
        "agent_cleanup_seconds": 60,
        "recovery_observation_seconds": 120,
        "recovery_sustain_seconds": 30,
    }


def valid_result() -> dict[str, object]:
    return {
        "status": "completed",
        "interaction_mode": "guided",
        "assisted": False,
        "assistance_events": [],
        "decision": "continue",
        "clarification_request": None,
        "effect_assessment": "verified",
        "recovery_assessment": "verified",
        "missing_conditions": [],
        "retry_summary": {
            "operation_id": None,
            "attempts": 0,
            "bounded": True,
            "outcome_reconciled": False,
        },
        "recovery_trigger": {
            "condition": "target_latency_ms recovered",
            "observed": True,
            "triggered_by_agent": True,
        },
        "strategy_selection": {
            "fault_type": "network-delay",
            "rationale": "least disruptive observable network perturbation",
            "evidence_summary": "cart endpoint latency can be observed",
        },
        "suspected_defect": "cart latency sensitivity",
        "evidence": [
            {
                "source": "telemetry_ro",
                "summary": "latency increased in the target window",
                "observed_at": "2026-09-05T12:00:00Z",
                "artifact_ref": "telemetry/window-1",
            }
        ],
        "actions_taken": ["validated bounded network delay"],
        "recovery_check": "business workload recovered",
        "remaining_risk": "no remaining known risk",
    }


def channel(
    tmp_path: Path,
    *,
    trial_id: str = "trial-1",
    case_id: str = "D7",
    variant: str = "A",
    policy_root: Path | None = None,
    ledger: PlatformLedger | None = None,
) -> tuple[HarnessChannelService, PlatformLedger, Path]:
    ledger = ledger or PlatformLedger(tmp_path / "ledger")
    decision_file = tmp_path / "trial" / "user-decision.json"
    config = HarnessChannelConfig(
        trial_id=trial_id,
        trial_dir=tmp_path / "trial",
        ledger_root=ledger.root,
        policy_file=(policy_root / "tools.policy.json") if policy_root else None,
        decision_file=decision_file,
        case_id=case_id,
        variant=variant,
        max_fault_seconds=600,
        max_observation_seconds=300,
    )
    return HarnessChannelService(config, ledger=ledger, responder=responder()), ledger, decision_file


def responder() -> HarnessResponder:
    envelope = PlanSafetyEnvelope.from_controller_policy(
        default_policy({"otel-demo"}),
        allowed_fault_types=("network-delay", "network-loss", "cpu-load", "memory-stress"),
        max_effect_observation_seconds=300,
        max_recovery_observation_seconds=300,
    ).model_copy(update={"max_fault_duration_seconds": 600})
    policy = SimulatedUserPolicy.from_limits(
        namespace="otel-demo",
        max_fault_seconds=600,
        max_observation_seconds=300,
        allowed_fault_types=("network-delay", "network-loss", "cpu-load", "memory-stress"),
        expected_outcome=ExpectedOutcome.EXECUTE_AND_RECOVER,
        decision_policy=DecisionPolicy.CLARIFY_MISSING,
        prompt_level=AutonomyLevel.L0_COMPLETE_TASK,
        envelope=envelope,
    )
    return HarnessResponder(
        model_call=lambda *_args: (_ for _ in ()).throw(AssertionError("model must not be called")),
        namespace="otel-demo",
        max_fault_seconds=600,
        max_observation_seconds=300,
        policy=policy,
    )


def apply_disturbance(
    tmp_path: Path,
    ledger: PlatformLedger,
    *,
    trial_id: str = "trial-1",
    server: str = "telemetry_ro",
    tool: str = "telemetry_prom_metric_range",
) -> Path:
    registry = CapabilityPolicyRegistry(tmp_path / "policy", ledger=ledger)
    registry.initialize(trial_id, _profile(server, "coroot_ro", "chaos_control", "chaos_mesh_control"))
    registry.set_tool(
        server,
        tool,
        state="disabled",
        reason="test disturbance",
        source="disturbance",
    )
    ledger.append(
        trial_id=trial_id,
        event_type="TOOL_CALL_DENIED_DISABLED",
        occurred_at="2026-09-05T12:00:00Z",
        payload={"server": server, "tool": tool, "state": "disabled"},
    )
    return registry.root


def _profile(*servers: str):
    from stage2_service.contracts import PermissionProfile

    return PermissionProfile(profile_id="p0-full-authorized", mcp_servers=servers)


def _schema_refs(schema):
    if isinstance(schema, dict):
        if "$ref" in schema:
            yield schema["$ref"]
        for value in schema.values():
            yield from _schema_refs(value)
    elif isinstance(schema, list):
        for item in schema:
            yield from _schema_refs(item)


def test_mcp_lists_four_harness_channel_tools(tmp_path: Path) -> None:
    service, _ledger, _decision = channel(tmp_path)
    server = create_server(service=service)
    tools = {tool.name: tool for tool in run(server.list_tools())}

    assert set(tools) == {
        "harness_consult",
        "harness_confirm",
        "harness_submit_result",
        "harness_poll_notices",
    }
    for tool in tools.values():
        assert tool.annotations.destructive_hint is False
        assert "trial_id" not in tool.input_schema.get("properties", {})
        assert "case_id" not in tool.input_schema.get("properties", {})
        assert "variant" not in tool.input_schema.get("properties", {})
        assert "path" not in tool.input_schema.get("properties", {})


def test_submit_result_tool_schema_exposes_authoritative_result_contract(tmp_path: Path) -> None:
    service, _ledger, _decision = channel(tmp_path)
    server = create_server(service=service)
    submit_tool = {tool.name: tool for tool in run(server.list_tools())}["harness_submit_result"]
    authority = json.loads(AGENT_RESULT_SCHEMA.read_text(encoding="utf-8"))

    input_schema = submit_tool.input_schema
    assert input_schema["required"] == ["result"]
    assert set(input_schema["properties"]) == {"result"}

    result_schema = input_schema["properties"]["result"]
    assert result_schema["required"] == authority["required"]
    assert set(result_schema["properties"]) == set(authority["properties"])
    assert list(_schema_refs(result_schema)) == []

    recommendation = result_schema["properties"]["clarification_request"]["anyOf"][1]["properties"]["recommendation"]
    effect_condition = recommendation["properties"]["effect_condition"]
    recovery_condition = recommendation["properties"]["recovery_condition"]
    assert "allOf" in effect_condition
    assert "allOf" in recovery_condition
    assert effect_condition["allOf"][0]["required"] == ["metric", "operator", "threshold"]
    assert recovery_condition["allOf"][0]["required"] == ["metric", "operator", "threshold"]

    authority_validator = Draft202012Validator(authority)
    tool_validator = Draft202012Validator(result_schema)
    valid = valid_result()
    invalid_missing = {"status": "completed"}
    invalid_enum = {**valid, "status": "not-a-status"}

    assert list(authority_validator.iter_errors(valid)) == []
    assert list(tool_validator.iter_errors(valid)) == []
    assert bool(list(authority_validator.iter_errors(invalid_missing))) is True
    assert bool(list(tool_validator.iter_errors(invalid_missing))) is True
    assert bool(list(authority_validator.iter_errors(invalid_enum))) is True
    assert bool(list(tool_validator.iter_errors(invalid_enum))) is True


def test_submit_result_schema_ref_expansion_rejects_unsupported_refs() -> None:
    from mcp_servers.harness_channel.server import _expand_local_json_schema_refs

    with pytest.raises(RuntimeError, match="external reference"):
        _expand_local_json_schema_refs({"$ref": "https://example.test/schema.json"})
    with pytest.raises(RuntimeError, match="dangling local reference"):
        _expand_local_json_schema_refs({"$ref": "#/$defs/missing", "$defs": {}})
    with pytest.raises(RuntimeError, match="circular reference"):
        _expand_local_json_schema_refs({"$ref": "#/$defs/loop", "$defs": {"loop": {"$ref": "#/$defs/loop"}}})


def test_harness_channel_emits_realtime_call_and_receipt(tmp_path: Path) -> None:
    service, _ledger, _decision = channel(tmp_path)
    events: list[ToolCall | ToolResult] = []

    def controller(event: ToolCall | ToolResult, _source: str) -> dict[str, object]:
        events.append(event)
        return {"allowed": True}

    with _audit_config("trial-1") as config, AuditBridgeListener(config, controller):
        server = create_server(service=service, audit_client=AuditBridgeClient(config))
        response = run(server.call_tool("harness_consult", {"question": "下一步怎么办？"}))

    result = response.structured_content
    assert isinstance(result["controller_call_id"], str)
    assert [type(event) for event in events] == [ToolCall, ToolResult]
    assert events[0].tool == "harness_channel.harness_consult"
    assert events[0].arguments == {"question": "下一步怎么办？"}
    assert events[1].payload["controller_call_id"] == result["controller_call_id"]


def test_consult_without_causal_disturbance_returns_neutral_text(tmp_path: Path) -> None:
    service, ledger, _decision = channel(tmp_path)
    server = create_server(service=service)

    result = run(server.call_tool("harness_consult", {"question": "下一步怎么办？"}))

    assert result.structured_content["message"] == NEUTRAL_NO_INFORMATION
    assert result.structured_content["hint_delivered"] is False
    assert [event.event_type for event in ledger.query()] == [
        "CONSULT_REQUESTED",
        "CONSULT_DECLINED",
    ]


def test_d7a_consult_returns_exact_named_coroot_hint_once(tmp_path: Path) -> None:
    ledger = PlatformLedger(tmp_path / "ledger")
    policy_root = apply_disturbance(tmp_path, ledger)
    service, ledger, _decision = channel(tmp_path, policy_root=policy_root, ledger=ledger)
    server = create_server(service=service)

    first = run(server.call_tool("harness_consult", {"question": "监控工具停用了，可以帮忙吗？"}))
    second = run(server.call_tool("harness_consult", {"question": "再提示一次？"}))

    assert first.structured_content["message"] == D7_A_DEFAULT
    assert second.structured_content["message"] == NEUTRAL_NO_INFORMATION
    assert json.loads((tmp_path / "trial" / CONSULT_STATE_FILE_NAME).read_text())[
        "hint_delivered"
    ] is True
    assert [event.event_type for event in ledger.query()[-4:]] == [
        "CONSULT_REQUESTED",
        "HINT_DELIVERED",
        "CONSULT_REQUESTED",
        "CONSULT_DECLINED",
    ]


def test_d7b_consult_is_vague_and_does_not_name_alternative_tools(tmp_path: Path) -> None:
    ledger = PlatformLedger(tmp_path / "ledger")
    policy_root = apply_disturbance(tmp_path, ledger)
    service, _ledger, _decision = channel(
        tmp_path,
        case_id="D7",
        variant="B",
        policy_root=policy_root,
        ledger=ledger,
    )
    server = create_server(service=service)

    result = run(server.call_tool("harness_consult", {"question": "telemetry_ro 不可用了"}))

    message = result.structured_content["message"]
    assert message == D7_B
    assert "Coroot" not in message
    assert "telemetry_ro" not in message
    assert "Prometheus" not in message


def test_d8a_consult_names_chaos_mesh_after_chaosblade_create_is_disabled(tmp_path: Path) -> None:
    ledger = PlatformLedger(tmp_path / "ledger")
    policy_root = apply_disturbance(
        tmp_path,
        ledger,
        server="chaos_control",
        tool="chaos_create_experiment",
    )
    service, _ledger, _decision = channel(
        tmp_path,
        case_id="D8",
        variant="A",
        policy_root=policy_root,
        ledger=ledger,
    )
    server = create_server(service=service)

    result = run(server.call_tool("harness_consult", {"question": "创建工具已停用"}))

    assert result.structured_content["message"] == D8_A_DEFAULT


def test_confirm_approves_complete_plan_and_writes_private_decision_file(tmp_path: Path) -> None:
    service, ledger, decision_file = channel(tmp_path)
    server = create_server(service=service)

    result = run(server.call_tool("harness_confirm", {"plan": valid_plan()}))

    assert result.structured_content["allowed"] is True
    assert result.structured_content["approved_plan"]["fault_type"] == "network-delay"
    assert oct(decision_file.stat().st_mode & 0o777) == "0o600"
    decision = json.loads(decision_file.read_text())
    assert decision["schema_version"] == "stage2-user-decision.v1"
    assert decision["approved"] is True
    assert [event.event_type for event in ledger.query()] == [
        "CONFIRM_REQUESTED",
        "CONFIRM_GRANTED",
    ]


def test_confirm_denies_incomplete_plan_without_writing_decision_file(tmp_path: Path) -> None:
    service, ledger, decision_file = channel(tmp_path)
    server = create_server(service=service)

    result = run(server.call_tool("harness_confirm", {"plan": {"fault_type": "network-delay"}}))

    assert result.structured_content["allowed"] is False
    assert result.structured_content["approved_plan"] is None
    assert not decision_file.exists()
    assert [event.event_type for event in ledger.query()] == [
        "CONFIRM_REQUESTED",
        "CONFIRM_DENIED",
    ]


def test_submit_result_validates_schema_and_invalid_submission_does_not_overwrite(tmp_path: Path) -> None:
    service, ledger, _decision = channel(tmp_path)
    server = create_server(service=service)
    good = valid_result()

    valid = run(server.call_tool("harness_submit_result", {"result": good}))
    invalid = run(server.call_tool("harness_submit_result", {"result": {"status": "completed"}}))

    assert valid.structured_content == {"ok": True, "valid": True, "errors": []}
    assert invalid.structured_content["ok"] is False
    assert any(error["path"] == "<root>" for error in invalid.structured_content["errors"])
    assert json.loads((tmp_path / "trial" / RESULT_FILE_NAME).read_text()) == good
    assert [event.payload["valid"] for event in ledger.query() if event.event_type == "RESULT_SUBMITTED"] == [True, False]


def test_poll_notices_claims_then_acknowledges_delivery_ids(tmp_path: Path) -> None:
    service, ledger, _decision = channel(tmp_path)
    ledger.enqueue_notice(
        trial_id="trial-1",
        notice_type="TARGET_REBOUND",
        payload={"target_uid": "uid-new"},
        idempotency_key="target-rebound",
    )
    server = create_server(service=service)

    first = run(server.call_tool("harness_poll_notices", {})).structured_content
    delivery_id = first["notices"][0]["delivery_id"]
    pending_after_claim = ledger.pending_notices(trial_id="trial-1", include_claimed=True)
    ack = run(
        server.call_tool(
            "harness_poll_notices",
            {"ack_ids": [delivery_id]},
        )
    ).structured_content

    assert first["notices"][0]["notice"]["notice_type"] == "TARGET_REBOUND"
    assert pending_after_claim[0].delivered_at is None
    assert ack["acknowledged"] == [{"delivery_id": delivery_id, "notice_id": 1}]
    assert ledger.pending_notices(trial_id="trial-1", include_claimed=True) == []
    assert [event.event_type for event in ledger.query()[-1:]] == ["NOTICE_DELIVERED"]


def test_harness_channel_stays_available_when_policy_file_omits_it(tmp_path: Path) -> None:
    ledger = PlatformLedger(tmp_path / "ledger")
    policy_root = apply_disturbance(tmp_path, ledger)
    service, _ledger, _decision = channel(tmp_path, policy_root=policy_root, ledger=ledger)
    server = create_server(service=service)

    result = run(server.call_tool("harness_poll_notices", {}))

    assert result.structured_content["ok"] is True


def test_poll_cannot_acknowledge_another_trial_notice(tmp_path: Path) -> None:
    service, ledger, _ = channel(tmp_path)
    ledger.enqueue_notice(trial_id="other-trial", notice_type="TARGET_REBOUND",
                          payload={}, idempotency_key="other-notice")
    delivery = ledger.claim_notice(trial_id="other-trial", claimed_by="other-agent")
    answer = service.poll_notices(ack_ids=[delivery.delivery_id])
    assert answer["acknowledged"] == []
    assert answer["ack_errors"]
    assert ledger.pending_notices(trial_id="other-trial", include_claimed=True)[0].delivered_at is None


def test_environment_safe_refusal_is_not_overridden_by_channel_default(tmp_path: Path) -> None:
    config = HarnessChannelConfig(
        trial_id="trial-l4", trial_dir=tmp_path / "private", ledger_root=tmp_path / "ledger",
        policy_file=None, decision_file=tmp_path / "private/decision.json",
        expected_outcome=ExpectedOutcome.SAFE_REFUSAL,
    )
    service = HarnessChannelService(config)
    answer = service.confirm(valid_plan())
    assert answer["allowed"] is False
    assert not config.decision_file.exists()


def test_permitted_condition_completion_is_visible_assistance_not_fake_confirmation(tmp_path: Path) -> None:
    service, ledger, decision_file = channel(tmp_path, case_id="C0")
    policy = SimulatedUserPolicy.from_limits(
        namespace="otel-demo", max_fault_seconds=600, max_observation_seconds=300,
        allowed_fault_types=("network-delay",), prompt_level=AutonomyLevel.L1_COMPLETE_EXPERIMENT,
    )
    full = valid_plan()
    partial = {key: value for key, value in full.items()
               if key not in {"effect_condition", "recovery_condition"}}
    service.responder = HarnessResponder(
        model_call=lambda *_: {"message": "补齐允许由 Harness 提供的验证条件。", "plan": full},
        namespace="otel-demo", max_fault_seconds=600, max_observation_seconds=300, policy=policy,
    )
    answer = service.confirm(partial)
    assert answer["allowed"] is True
    assert answer["assisted"] is True
    assert answer["affected_nodes"] == ["PLAN_VALIDATION"]
    decision = json.loads(decision_file.read_text())
    assert decision["answer_mode"] == "custom"
    assert decision["decision_supplied"] is True
    assert any(event.event_type == "PLAN_ASSISTANCE_DELIVERED" for event in ledger.query())


def test_parallel_consults_cannot_receive_the_one_hint_twice(tmp_path: Path, monkeypatch) -> None:
    import time
    from concurrent.futures import ThreadPoolExecutor

    ledger = PlatformLedger(tmp_path / "ledger")
    policy_root = apply_disturbance(tmp_path, ledger)
    service, _, _ = channel(tmp_path, policy_root=policy_root, ledger=ledger)
    original = service._consult_state

    def delayed_state():
        state = original()
        time.sleep(0.02)
        return state

    monkeypatch.setattr(service, "_consult_state", delayed_state)
    with ThreadPoolExecutor(max_workers=4) as pool:
        answers = list(pool.map(service.consult, ["工具停用了，请帮助"] * 4))
    assert sum(answer["hint_delivered"] for answer in answers) == 1
