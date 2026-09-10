from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from mcp_servers.harness_channel.hints import D7_A_DEFAULT
from mcp_servers.http_runtime import TOOL_DISABLED_RESPONSE
from stage2_service.capability_policy import read_policy_file
from stage2_service.capability_preflight import harness_capabilities_from_qualification
from stage2_service.capability_qualification import publish_capabilities
from stage2_service.channel_qualification import (
    ALL_CHANNEL_HARNESSES,
    BASE_CHANNEL_QUALIFICATION_MODE,
    CHANNEL_QUALIFICATION_MODE,
    EXPECTED_HINT_BODY,
    MUTATION_TOOLS,
    QUALIFICATION_NOTICE_TYPE,
    ChannelQualificationRecord,
    ChannelQualificationRunner,
    QualificationHarnessChannelSupervisor,
    collective_equality_check,
    evaluate_channel_qualification,
    evaluate_base_channel_qualification,
    qualification_runtime_context,
    write_collective_check,
    write_record,
)
from stage2_service.contracts import (
    AgentVerdict,
    CapabilityProfile,
    ExpectedOutcome,
    HarnessKind,
    HarnessReport,
    PromptMode,
    Stage2CaseId,
)
from stage2_service.episode import load_fixed_episode
from stage2_service.gateway_config import GatewayConfigSnapshot
from stage2_service.mcp_supervisor import McpSupervisorError
from stage2_service.matrix import fixed_otel_episode_ref
from stage2_service.permissions import Stage2PermissionManager
from stage2_service.platform_ledger import PlatformEvent, PlatformLedger
from stage2_service.runtime_adapters import McpTokenStateRegistry, RuntimeAdapterError
from stage2_service.runtime_lock import RuntimeLock, RuntimeLockBusy


TRIAL_ID = "trial-channel-qualification"
NOW = "2026-09-05T12:00:00+00:00"
A2_NOTICE_ACK_FIXTURE = (
    Path(__file__).resolve().parent
    / "fixtures/channel_qualification/base-codex-a2-notice-ack-events.json"
)


def _episode_fixture(episode_id: str = "EPI-TEST-CHANNEL-0001") -> SimpleNamespace:
    return SimpleNamespace(ref=SimpleNamespace(episode_id=episode_id))


def _fake_gateway_fields(model: str = "gpt-5.5") -> dict:
    """Explicit offline fixture, never an on-cluster qualification record."""
    return {
        "gateway_route": {"model_alias": model, "provider": "openai", "upstream_model": model},
        "gateway_config_sha256": "a" * 64,
        "gateway_sidecar_evidence": {
            "verified": True, "request_ids": ["offline-request-1"],
            "artifact_ref": "gateway-requests.json",
        },
    }


def _fake_gateway_output(model: str = "gpt-5.5") -> dict:
    fields = _fake_gateway_fields(model)
    proof = fields.pop("gateway_sidecar_evidence")
    return {
        **fields, "model_alias": model, "gateway_evidence_verified": proof["verified"],
        "gateway_request_ids": proof["request_ids"], "gateway_evidence_ref": proof["artifact_ref"],
    }


def _gateway_snapshot(tmp_path: Path, model: str = "gpt-5.5") -> GatewayConfigSnapshot:
    path = tmp_path / "gateway.json"
    path.write_text(
        json.dumps(
            {
                "model_list": [
                    {
                        "model_name": model,
                        "litellm_params": {
                            "model": f"openai/{model}",
                            "api_base": "https://provider.example/v1",
                            "api_key": "os.environ/PROBE_KEY",
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return GatewayConfigSnapshot.from_file(path, required_aliases=(model,))


def test_channel_rejects_native_bypass_even_after_successful_mcp_sequence(tmp_path):
    ledger = PlatformLedger(tmp_path / "ledger")
    _append_success_events(ledger)
    ledger.append(trial_id=TRIAL_ID, event_type="PERMISSION_BYPASS_ATTEMPT", occurred_at=NOW,
                  payload={"source": "CLI_native", "tool": "Bash", "semantics": "attempt_only"})
    record = evaluate_channel_qualification(ledger.query(trial_id=TRIAL_ID),
                                            harness=HarnessKind.CODEX, model="gpt-5.5", trial_id=TRIAL_ID)
    assert not record.passed
    assert "native_boundary_violation_attempt" in record.failure_reasons


def _append_pair(
    ledger: PlatformLedger,
    call_id: str,
    tool: str,
    payload: dict[str, object],
    *,
    trial_id: str = TRIAL_ID,
    source: str = "mcp_server",
    status: str = "completed",
    arguments: dict[str, object] | None = None,
) -> None:
    ledger.append(
        trial_id=trial_id,
        event_type="ToolCall",
        occurred_at=NOW,
        payload={
            "source": source,
            "call_id": call_id,
            "tool": tool,
            "arguments": arguments or {},
        },
    )
    ledger.append(
        trial_id=trial_id,
        event_type="ToolResult",
        occurred_at=NOW,
        payload={
            "source": source,
            "call_id": call_id,
            "status": status,
            "payload": payload,
        },
    )


def _append_submit_exchange(
    ledger: PlatformLedger,
    call_id: str,
    *,
    valid: bool,
    stored: bool | None = None,
    include_result_event: bool = True,
    trial_id: str = TRIAL_ID,
    source: str = "mcp_server",
) -> None:
    ledger.append(
        trial_id=trial_id,
        event_type="ToolCall",
        occurred_at=NOW,
        payload={
            "source": source,
            "call_id": call_id,
            "tool": "harness_channel.harness_submit_result",
            "arguments": {"result": {"status": "completed"}},
        },
    )
    if include_result_event:
        ledger.append(
            trial_id=trial_id,
            event_type="RESULT_SUBMITTED",
            occurred_at=NOW,
            payload={
                "valid": valid,
                "stored": valid if stored is None else stored,
                "errors": [] if valid else ["schema violation"],
            },
        )
    ledger.append(
        trial_id=trial_id,
        event_type="ToolResult",
        occurred_at=NOW,
        payload={
            "source": source,
            "call_id": call_id,
            "status": "completed" if valid else "failed",
            "payload": {
                "ok": valid,
                "valid": valid,
                "errors": [] if valid else ["schema violation"],
            },
        },
    )


def _append_policy_and_denial(ledger: PlatformLedger, *, trial_id: str = TRIAL_ID) -> None:
    ledger.append(
        trial_id=trial_id,
        event_type="POLICY_APPLIED",
        occurred_at=NOW,
        payload={
            "source": "disturbance",
            "server": "telemetry_ro",
            "state": "disabled",
        },
    )
    ledger.append(
        trial_id=trial_id,
        event_type="TOOL_CALL_DENIED_DISABLED",
        occurred_at=NOW,
        payload={
            "server": "telemetry_ro",
            "tool": "telemetry_prom_metric_range",
            "state": "disabled",
        },
    )


def _append_success_events(
    ledger: PlatformLedger,
    *,
    trial_id: str = TRIAL_ID,
    source: str = "mcp_server",
    hint_message: str = D7_A_DEFAULT,
    include_submit: bool = True,
    sandbox_ok: bool = True,
    coroot_before_hint: bool = False,
    coroot_call_before_hint_result_after: bool = False,
    include_mutation: bool = False,
    sandbox_exit_code: int = 0,
    sandbox_truncated: bool = False,
    include_sandbox_run: bool = True,
    invalid_submit_before_valid: bool = False,
) -> None:
    _append_policy_and_denial(ledger, trial_id=trial_id)
    _append_pair(
        ledger,
        "telemetry",
        "telemetry_ro.telemetry_prom_metric_range",
        {**TOOL_DISABLED_RESPONSE, "controller_call_id": "telemetry"},
        trial_id=trial_id,
        source=source,
        status="failed",
    )
    if include_mutation:
        _append_pair(
            ledger,
            "mutation",
            next(iter(MUTATION_TOOLS)),
            {"ok": False, "error": {"code": "TOOL_DISABLED"}},
            trial_id=trial_id,
            source=source,
            status="failed",
        )
    if coroot_before_hint:
        _append_pair(
            ledger,
            "coroot",
            "coroot_ro.coroot_metrics_range",
            {"ok": True, "source": "coroot", "data": []},
            trial_id=trial_id,
            source=source,
        )
    if coroot_call_before_hint_result_after:
        ledger.append(
            trial_id=trial_id,
            event_type="ToolCall",
            occurred_at=NOW,
            payload={
                "source": source,
                "call_id": "coroot",
                "tool": "coroot_ro.coroot_metrics_range",
                "arguments": {},
            },
        )
    ledger.append(
        trial_id=trial_id,
        event_type="ToolCall",
        occurred_at=NOW,
        payload={
            "source": source,
            "call_id": "consult",
            "tool": "harness_channel.harness_consult",
            "arguments": {"question": "telemetry_ro returned TOOL_DISABLED; what should I use?"},
        },
    )
    ledger.append(
        trial_id=trial_id,
        event_type="HINT_DELIVERED",
        occurred_at=NOW,
        payload={
            "case_id": "D7",
            "variant": "A",
            "disabled_server": "telemetry_ro",
            "hint": hint_message,
            "help_counted": True,
        },
    )
    ledger.append(
        trial_id=trial_id,
        event_type="ToolResult",
        occurred_at=NOW,
        payload={
            "source": source,
            "call_id": "consult",
            "status": "completed",
            "payload": {
                "ok": True,
                "message": hint_message,
                "hint_delivered": True,
                "controller_call_id": "consult",
            },
        },
    )
    if not coroot_before_hint and not coroot_call_before_hint_result_after:
        _append_pair(
            ledger,
            "coroot",
            "coroot_ro.coroot_metrics_range",
            {"ok": True, "source": "coroot", "data": []},
            trial_id=trial_id,
            source=source,
        )
    if coroot_call_before_hint_result_after:
        ledger.append(
            trial_id=trial_id,
            event_type="ToolResult",
            occurred_at=NOW,
            payload={
                "source": source,
                "call_id": "coroot",
                "status": "completed",
                "payload": {"ok": True, "source": "coroot", "data": []},
            },
        )
    ledger.append(
        trial_id=trial_id,
        event_type="ToolCall",
        occurred_at=NOW,
        payload={
            "source": source,
            "call_id": "sandbox",
            "tool": "code_sandbox.run_python",
            "arguments": {"code": "print(2 + 2)", "timeout_seconds": 5},
        },
    )
    if include_sandbox_run:
        ledger.append(
            trial_id=trial_id,
            event_type="SANDBOX_RUN",
            occurred_at=NOW,
            payload={
                "status": "completed" if sandbox_ok else "execution_unavailable",
                "exit_code": sandbox_exit_code if sandbox_ok else None,
                "truncated": sandbox_truncated,
            },
        )
    ledger.append(
        trial_id=trial_id,
        event_type="ToolResult",
        occurred_at=NOW,
        payload={
            "source": source,
            "call_id": "sandbox",
            "status": "completed" if sandbox_ok else "failed",
            "payload": (
                {
                    "ok": True,
                    "stdout": "4\n",
                    "stderr": "",
                    "exit_code": sandbox_exit_code,
                    "truncated": sandbox_truncated,
                }
                if sandbox_ok
                else {"ok": False, "error": {"code": "CODE_SANDBOX_ERROR"}}
            ),
        },
    )
    _append_pair(
        ledger,
        "poll-1",
        "harness_channel.harness_poll_notices",
        {
            "ok": True,
            "notices": [
                {
                    "delivery_id": "delivery-1",
                    "notice": {
                        "notice_type": QUALIFICATION_NOTICE_TYPE,
                        "notice_id": 1,
                        "payload": {"fact": "No chaos fault is active."},
                    },
                }
            ],
            "acknowledged": [],
            "ack_errors": [],
        },
        trial_id=trial_id,
        source=source,
    )
    ledger.append(
        trial_id=trial_id,
        event_type="ToolCall",
        occurred_at=NOW,
        payload={
            "source": source,
            "call_id": "poll-2",
            "tool": "harness_channel.harness_poll_notices",
            "arguments": {"ack_ids": ["delivery-1"]},
        },
    )
    ledger.append(
        trial_id=trial_id,
        event_type="NOTICE_DELIVERED",
        occurred_at=NOW,
        payload={
            "delivery_id": "delivery-1",
            "notice_id": 1,
            "notice_type": QUALIFICATION_NOTICE_TYPE,
            "path": "poll",
        },
    )
    ledger.append(
        trial_id=trial_id,
        event_type="ToolResult",
        occurred_at=NOW,
        payload={
            "source": source,
            "call_id": "poll-2",
            "status": "completed",
            "payload": {
                "ok": True,
                "notices": [],
                "acknowledged": [{"delivery_id": "delivery-1", "notice_id": 1}],
                "ack_errors": [],
            },
        },
    )
    if include_submit:
        if invalid_submit_before_valid:
            _append_submit_exchange(
                ledger,
                "submit-invalid",
                valid=False,
                trial_id=trial_id,
                source=source,
            )
        _append_submit_exchange(
            ledger,
            "submit",
            valid=True,
            trial_id=trial_id,
            source=source,
        )


def _append_base_success_events(
    ledger: PlatformLedger,
    *,
    trial_id: str = TRIAL_ID,
    source: str = "mcp_server",
    include_confirm: bool = True,
    include_notice_ack: bool = True,
    include_submit: bool = True,
    invalid_submit_before_valid: bool = False,
) -> None:
    _append_pair(
        ledger,
        "k8s",
        "k8s_ro.k8s_list_resources",
        {"ok": True, "items": [{"kind": "Pod", "metadata": {"name": "cart"}}]},
        trial_id=trial_id,
        source=source,
    )
    _append_pair(
        ledger,
        "telemetry",
        "telemetry_ro.telemetry_prom_metric_range",
        {"ok": True, "window": {"seconds": 30}},
        trial_id=trial_id,
        source=source,
    )
    if include_confirm:
        ledger.append(
            trial_id=trial_id,
            event_type="ToolCall",
            occurred_at=NOW,
            payload={
                "source": source,
                "call_id": "confirm",
                "tool": "harness_channel.harness_confirm",
                "arguments": {"plan": {"action": "no_mutation_channel_check"}},
            },
        )
        ledger.append(
            trial_id=trial_id,
            event_type="CONFIRM_DENIED",
            occurred_at=NOW,
            payload={"allowed": False, "reason": "no mutation authorized"},
        )
        ledger.append(
            trial_id=trial_id,
            event_type="ToolResult",
            occurred_at=NOW,
            payload={
                "source": source,
                "call_id": "confirm",
                "status": "completed",
                "payload": {
                    "ok": True,
                    "allowed": False,
                    "reason": "no mutation authorized",
                    "message": "No mutation is authorized for base channel qualification.",
                },
            },
        )
    ledger.append(
        trial_id=trial_id,
        event_type="ToolCall",
        occurred_at=NOW,
        payload={
            "source": source,
            "call_id": "consult",
            "tool": "harness_channel.harness_consult",
            "arguments": {"question": "Any extra hint for a no-fault base channel qualification?"},
        },
    )
    ledger.append(
        trial_id=trial_id,
        event_type="CONSULT_DECLINED",
        occurred_at=NOW,
        payload={
            "reason": "no_disturbance_observed",
            "question": "Any extra hint for a no-fault base channel qualification?",
            "message": "No additional information is available.",
        },
    )
    ledger.append(
        trial_id=trial_id,
        event_type="ToolResult",
        occurred_at=NOW,
        payload={
            "source": source,
            "call_id": "consult",
            "status": "completed",
            "payload": {
                "ok": True,
                "message": "No additional information is available.",
                "hint_delivered": False,
            },
        },
    )
    if include_notice_ack:
        _append_pair(
            ledger,
            "poll-1",
            "harness_channel.harness_poll_notices",
            {
                "ok": True,
                "notices": [
                    {
                        "delivery_id": "delivery-1",
                        "notice": {
                            "notice_type": QUALIFICATION_NOTICE_TYPE,
                            "notice_id": 1,
                            "payload": {"fact": "Base channel qualification notice."},
                        },
                    }
                ],
                "acknowledged": [],
                "ack_errors": [],
            },
            trial_id=trial_id,
            source=source,
        )
        ledger.append(
            trial_id=trial_id,
            event_type="ToolCall",
            occurred_at=NOW,
            payload={
                "source": source,
                "call_id": "poll-2",
                "tool": "harness_channel.harness_poll_notices",
                "arguments": {"ack_ids": ["delivery-1"]},
            },
        )
        ledger.append(
            trial_id=trial_id,
            event_type="NOTICE_DELIVERED",
            occurred_at=NOW,
            payload={
                "delivery_id": "delivery-1",
                "notice_id": 1,
                "notice_type": QUALIFICATION_NOTICE_TYPE,
                "path": "poll",
            },
        )
        ledger.append(
            trial_id=trial_id,
            event_type="ToolResult",
            occurred_at=NOW,
            payload={
                "source": source,
                "call_id": "poll-2",
                "status": "completed",
                "payload": {
                    "ok": True,
                    "notices": [],
                    "acknowledged": [{"delivery_id": "delivery-1", "notice_id": 1}],
                    "ack_errors": [],
                },
            },
    )
    if include_submit:
        if invalid_submit_before_valid:
            _append_submit_exchange(
                ledger,
                "submit-invalid",
                valid=False,
                trial_id=trial_id,
                source=source,
            )
        _append_submit_exchange(
            ledger,
            "submit",
            valid=True,
            trial_id=trial_id,
            source=source,
        )


def _append_base_controller_notice_events(
    ledger: PlatformLedger,
    *,
    trial_id: str = TRIAL_ID,
    delivery_trial_id: str = TRIAL_ID,
    include_ack: bool = True,
    include_delivery: bool = True,
    ack_delivery_id: str = "delivery-1",
) -> None:
    _append_pair(
        ledger,
        "k8s",
        "k8s_ro.k8s_list_resources",
        {"ok": True, "items": [{"kind": "Pod", "metadata": {"name": "cart"}}]},
        trial_id=trial_id,
    )
    _append_pair(
        ledger,
        "telemetry",
        "telemetry_ro.telemetry_prom_metric_range",
        {"ok": True, "window": {"seconds": 30}},
        trial_id=trial_id,
    )
    ledger.append(
        trial_id=trial_id,
        event_type="ToolCall",
        occurred_at=NOW,
        payload={
            "source": "mcp_server",
            "call_id": "confirm",
            "tool": "harness_channel.harness_confirm",
            "arguments": {"plan": {"intent": "confirm-only", "hypothetical": True}},
        },
    )
    ledger.append(
        trial_id=trial_id,
        event_type="CONFIRM_DENIED",
        occurred_at=NOW,
        payload={"allowed": False, "reason": "safe_refusal_expected"},
    )
    ledger.append(
        trial_id=trial_id,
        event_type="ToolResult",
        occurred_at=NOW,
        payload={
            "source": "mcp_server",
            "call_id": "confirm",
            "status": "completed",
            "payload": {"ok": True, "allowed": False, "reason": "safe_refusal_expected"},
        },
    )
    ledger.append(
        trial_id=trial_id,
        event_type="ToolCall",
        occurred_at=NOW,
        payload={
            "source": "mcp_server",
            "call_id": "consult",
            "tool": "harness_channel.harness_consult",
            "arguments": {"question": "neutral decline only"},
        },
    )
    ledger.append(
        trial_id=trial_id,
        event_type="CONSULT_DECLINED",
        occurred_at=NOW,
        payload={"reason": "no_causal_disturbance_denial", "question": "neutral decline only"},
    )
    ledger.append(
        trial_id=trial_id,
        event_type="ToolResult",
        occurred_at=NOW,
        payload={
            "source": "mcp_server",
            "call_id": "consult",
            "status": "completed",
            "payload": {
                "ok": True,
                "message": "当前没有可提供的信息。",
                "hint_delivered": False,
                "controller_notices": [
                    {
                        "attempt": 1,
                        "delivery_id": "delivery-1",
                        "notice": {
                            "notice_id": 1,
                            "notice_type": QUALIFICATION_NOTICE_TYPE,
                            "payload": {"fact": "Base channel qualification notice."},
                        },
                    }
                ],
            },
        },
    )
    if include_delivery:
        ledger.append(
            trial_id=delivery_trial_id,
            event_type="NOTICE_DELIVERED",
            occurred_at=NOW,
            payload={
                "delivery_id": "delivery-1",
                "notice_id": 1,
                "notice_type": QUALIFICATION_NOTICE_TYPE,
                "path": "in_band",
            },
        )
    _append_pair(
        ledger,
        "poll-empty",
        "harness_channel.harness_poll_notices",
        {"ok": True, "notices": [], "acknowledged": [], "ack_errors": []},
        trial_id=trial_id,
        arguments={"ack_ids": None, "limit": 5},
    )
    if include_ack:
        _append_pair(
            ledger,
            "poll-ack",
            "harness_channel.harness_poll_notices",
            {
                "ok": True,
                "notices": [],
                "acknowledged": [{"delivery_id": ack_delivery_id, "notice_id": 1}],
                "ack_errors": [],
            },
            trial_id=trial_id,
            arguments={"ack_ids": [ack_delivery_id], "limit": 5},
        )
    _append_submit_exchange(ledger, "submit", valid=True, trial_id=trial_id)


def _evaluate(ledger: PlatformLedger) -> ChannelQualificationRecord:
    return evaluate_channel_qualification(
        ledger.query(trial_id=TRIAL_ID, limit=10_000),
        harness=HarnessKind.CODEX,
        model="gpt-5.5",
        trial_id=TRIAL_ID,
    )


def test_evaluator_accepts_exact_mcp_server_sequence(tmp_path: Path) -> None:
    ledger = PlatformLedger(tmp_path / "ledger")
    _append_success_events(ledger)

    record = _evaluate(ledger)

    assert record.passed is True
    assert record.status == "passed"
    assert record.telemetry_denial_body == TOOL_DISABLED_RESPONSE
    assert record.hint_body == EXPECTED_HINT_BODY
    assert [item["tool"] for item in record.ordered_exchanges] == [
        "telemetry_ro.telemetry_prom_metric_range",
        "harness_channel.harness_consult",
        "coroot_ro.coroot_metrics_range",
        "code_sandbox.run_python",
        "harness_channel.harness_poll_notices",
        "harness_channel.harness_poll_notices",
        "harness_channel.harness_submit_result",
    ]


def test_evaluator_accepts_invalid_result_then_last_valid_submission(tmp_path: Path) -> None:
    ledger = PlatformLedger(tmp_path / "ledger")
    _append_success_events(ledger, invalid_submit_before_valid=True)

    record = _evaluate(ledger)

    assert record.passed is True
    assert [item["call_id"] for item in record.ordered_exchanges if item["tool"] == "harness_channel.harness_submit_result"] == [
        "submit-invalid",
        "submit",
    ]


def test_evaluator_accepts_controller_notices_carrier_after_sandbox_before_poll_ack(tmp_path: Path) -> None:
    ledger = PlatformLedger(tmp_path / "ledger")
    _append_success_events(ledger)
    rows = [event.as_dict() for event in ledger.query(trial_id=TRIAL_ID, limit=10_000)]
    old_delivery = next(row for row in rows if row["event_type"] == "NOTICE_DELIVERED")
    notice_item = next(
        row["payload"]["payload"]["notices"][0]
        for row in rows
        if row["event_type"] == "ToolResult"
        and row["payload"].get("call_id") == "poll-1"
    )
    rows = [row for row in rows if row["event_type"] != "NOTICE_DELIVERED"]
    for row in rows:
        if row["event_type"] != "ToolResult":
            continue
        if row["payload"].get("call_id") == "sandbox":
            row["payload"]["payload"]["controller_notices"] = [notice_item]
        elif row["payload"].get("call_id") == "poll-1":
            row["payload"]["payload"]["notices"] = []
    sandbox_index = next(
        index
        for index, row in enumerate(rows)
        if row["event_type"] == "ToolResult"
        and row["payload"].get("call_id") == "sandbox"
    )
    rows.insert(sandbox_index + 1, old_delivery)
    events = [
        PlatformEvent(**{**row, "sequence": index + 1})
        for index, row in enumerate(rows)
    ]

    record = evaluate_channel_qualification(
        events,
        harness=HarnessKind.CODEX,
        model="gpt-5.5",
        trial_id=TRIAL_ID,
    )

    assert record.passed is True
    assert "missing_notice_ack" not in record.failure_reasons
    assert "required_order_violated" not in record.failure_reasons


def test_evaluator_rejects_valid_result_without_matching_result_submitted_event(tmp_path: Path) -> None:
    ledger = PlatformLedger(tmp_path / "ledger")
    _append_success_events(ledger, include_submit=False)
    _append_submit_exchange(ledger, "submit", valid=True, include_result_event=False)

    record = _evaluate(ledger)

    assert record.passed is False
    assert "missing_valid_result_submission" in record.failure_reasons


def test_base_evaluator_accepts_foundational_channel_sequence(tmp_path: Path) -> None:
    ledger = PlatformLedger(tmp_path / "ledger")
    _append_base_success_events(ledger)

    record = evaluate_base_channel_qualification(
        ledger.query(trial_id=TRIAL_ID, limit=10_000),
        harness=HarnessKind.CODEX,
        model="gpt-5.5",
        trial_id=TRIAL_ID,
        report=HarnessReport(
            status="completed",
            agent_verdict=AgentVerdict.INCONCLUSIVE,
            lifecycle_events=(),
            artifact_refs=("base-channel-qualification/trial/stdout.txt",),
            final_output=_fake_gateway_output(),
        ),
    )

    assert record.passed is True
    assert record.qualification_type == BASE_CHANNEL_QUALIFICATION_MODE
    assert record.qualification_profile == BASE_CHANNEL_QUALIFICATION_MODE
    assert record.scored_as_d7 is False
    assert record.base_checks == {
        "mcp_read_verified": True,
        "confirmation_roundtrip_verified": True,
        "consult_roundtrip_verified": True,
        "notice_ack_verified": True,
        "result_submission_verified": True,
        "gateway_evidence_verified": True,
        "tool_evidence_verified": True,
    }
    assert record.observed_capability_evidence["mcp_servers"] == [
        "harness_channel",
        "k8s_ro",
        "telemetry_ro",
    ]
    assert record.observed_capability_evidence["required_mcp_servers"] == [
        "chaos_control",
        "harness_channel",
        "k8s_ro",
        "source_ro",
        "telemetry_ro",
    ]
    assert "base-channel qualification; not a fault qualification" in record.limitations


def test_base_evaluator_accepts_a2_controller_notice_carrier_native_ack_replay() -> None:
    rows = json.loads(A2_NOTICE_ACK_FIXTURE.read_text(encoding="utf-8"))
    events = [PlatformEvent(**row) for row in rows]
    trial_id = next(row["trial_id"] for row in rows if row["event_type"] == "ToolCall")

    record = evaluate_base_channel_qualification(
        events,
        harness=HarnessKind.CODEX,
        model="gpt-5.5",
        trial_id=trial_id,
        report=HarnessReport(
            status="completed",
            agent_verdict=AgentVerdict.INCONCLUSIVE,
            lifecycle_events=(),
            final_output=_fake_gateway_output(),
        ),
    )

    assert record.passed is True
    assert record.base_checks["notice_ack_verified"] is True
    assert "missing_notice_ack" not in record.failure_reasons


@pytest.mark.parametrize("malformed_notice_id", ["2", True, None])
def test_base_evaluator_rejects_controller_notice_without_valid_notice_id(
    tmp_path: Path,
    malformed_notice_id: object,
) -> None:
    ledger = PlatformLedger(tmp_path / "ledger")
    _append_base_controller_notice_events(ledger)
    rows = [event.as_dict() for event in ledger.query(trial_id=TRIAL_ID, limit=10_000)]
    for row in rows:
        if row["event_type"] != "ToolResult":
            continue
        payload = row["payload"].get("payload", {})
        notices = payload.get("controller_notices")
        if not notices:
            continue
        notice = notices[0]["notice"]
        if malformed_notice_id is None:
            notice.pop("notice_id", None)
        else:
            notice["notice_id"] = malformed_notice_id
        break
    events = [PlatformEvent(**row) for row in rows]

    record = evaluate_base_channel_qualification(
        events,
        harness=HarnessKind.CODEX,
        model="gpt-5.5",
        trial_id=TRIAL_ID,
        report=HarnessReport(
            status="completed",
            agent_verdict=AgentVerdict.INCONCLUSIVE,
            lifecycle_events=(),
            final_output=_fake_gateway_output(),
        ),
    )

    assert record.passed is False
    assert record.base_checks["notice_ack_verified"] is False
    assert "missing_notice_ack" in record.failure_reasons


def test_base_evaluator_accepts_controller_notices_carrier_before_explicit_ack(tmp_path: Path) -> None:
    ledger = PlatformLedger(tmp_path / "ledger")
    _append_base_controller_notice_events(ledger)

    record = evaluate_base_channel_qualification(
        ledger.query(trial_id=TRIAL_ID, limit=10_000),
        harness=HarnessKind.CODEX,
        model="gpt-5.5",
        trial_id=TRIAL_ID,
        report=HarnessReport(
            status="completed",
            agent_verdict=AgentVerdict.INCONCLUSIVE,
            lifecycle_events=(),
            final_output=_fake_gateway_output(),
        ),
    )

    assert record.passed is True
    assert record.base_checks["notice_ack_verified"] is True


@pytest.mark.parametrize(
    "damage",
    ["wrong_ack_id", "cross_trial_delivery", "missing_ack", "missing_delivery"],
)
def test_base_evaluator_rejects_broken_notice_ack_evidence(tmp_path: Path, damage: str) -> None:
    ledger = PlatformLedger(tmp_path / "ledger")
    _append_base_controller_notice_events(
        ledger,
        ack_delivery_id="wrong-delivery" if damage == "wrong_ack_id" else "delivery-1",
        delivery_trial_id="other-trial" if damage == "cross_trial_delivery" else TRIAL_ID,
        include_ack=damage != "missing_ack",
        include_delivery=damage != "missing_delivery",
    )

    record = evaluate_base_channel_qualification(
        ledger.query(limit=10_000),
        harness=HarnessKind.CODEX,
        model="gpt-5.5",
        trial_id=TRIAL_ID,
        report=HarnessReport(
            status="completed",
            agent_verdict=AgentVerdict.INCONCLUSIVE,
            lifecycle_events=(),
            final_output=_fake_gateway_output(),
        ),
    )

    assert record.passed is False
    assert record.base_checks["notice_ack_verified"] is False
    assert "missing_notice_ack" in record.failure_reasons


def test_base_record_publishes_into_preflight_from_real_evaluator_output(tmp_path: Path) -> None:
    artifact_root = tmp_path / "artifacts"
    archive = artifact_root / "codex-base"
    archive.mkdir(parents=True)
    gateway = _gateway_snapshot(tmp_path)
    model = "gpt-5.5"
    trial_id = TRIAL_ID
    ledger = PlatformLedger(tmp_path / "ledger")
    _append_base_success_events(ledger, trial_id=trial_id)
    report = HarnessReport(
        status="completed",
        agent_verdict=AgentVerdict.INCONCLUSIVE,
        lifecycle_events=(),
        artifact_refs=(
            "codex-base/gateway-requests.json",
            "codex-base/canonical-events.jsonl",
        ),
        final_output={
            **_fake_gateway_output(model),
            "gateway_route": gateway.route(model),
            "gateway_config_sha256": gateway.config_sha256,
        },
    )
    record = evaluate_base_channel_qualification(
        ledger.query(trial_id=trial_id, limit=10_000),
        harness=HarnessKind.CODEX,
        model=model,
        trial_id=trial_id,
        report=report,
    )
    assert record.passed is True
    record_path = write_record(tmp_path / "records", record)
    record_payload = json.loads(record_path.read_text(encoding="utf-8"))
    exchanges = record_payload["ordered_exchanges"]

    gateway_receipts = [
        {
            "trial_id": trial_id,
            "harness": "codex",
            "model_alias": model,
            "gateway_config_sha256": gateway.config_sha256,
            "request_id": "offline-request-1",
            "outcome": "received",
        }
    ]
    (archive / "gateway-requests.json").write_text(
        json.dumps(gateway_receipts),
        encoding="utf-8",
    )
    native_tools = [
        "k8s_ro.k8s_list_resources",
        "telemetry_ro.telemetry_prom_metric_range",
        "harness_channel.harness_confirm",
        "harness_channel.harness_consult",
        "harness_channel.harness_poll_notices",
        "harness_channel.harness_submit_result",
    ]
    rows: list[dict[str, object]] = []
    for index, tool in enumerate(native_tools):
        call_id = f"native-{index}"
        rows.append(
            {
                "event_type": "ToolCall",
                "source": "native",
                "replayed": False,
                "call_id": call_id,
                "tool": tool,
            }
        )
        rows.append(
            {
                "event_type": "ToolResult",
                "source": "native",
                "replayed": False,
                "call_id": call_id,
                "status": "completed",
                "payload": {"ok": True},
            }
        )
    for exchange in exchanges:
        rows.append(
            {
                "event_type": "ToolCall",
                "source": "mcp_server",
                "replayed": False,
                "call_id": exchange["call_id"],
                "tool": exchange["tool"],
            }
        )
        rows.append(
            {
                "event_type": "ToolResult",
                "source": "mcp_server",
                "replayed": False,
                "call_id": exchange["call_id"],
                "status": exchange["status"],
                "payload": {"ok": True},
            }
        )
    (archive / "canonical-events.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )

    output = tmp_path / "private" / "capabilities.json"
    publish_capabilities(
        [record_path],
        artifact_root=artifact_root,
        output=output,
        gateway=gateway,
    )
    descriptors, source = harness_capabilities_from_qualification(output)

    assert source["status"] == "qualification_records_loaded"
    assert descriptors["codex"]["qualification_passed"] is True
    assert descriptors["codex"]["feedback_channels"] == ["in_band_mcp"]
    assert descriptors["codex"]["probe"]["qualification_profile"] == BASE_CHANNEL_QUALIFICATION_MODE


def test_base_evaluator_accepts_invalid_result_then_last_valid_submission(tmp_path: Path) -> None:
    ledger = PlatformLedger(tmp_path / "ledger")
    _append_base_success_events(ledger, invalid_submit_before_valid=True)

    record = evaluate_base_channel_qualification(
        ledger.query(trial_id=TRIAL_ID, limit=10_000),
        harness=HarnessKind.CODEX,
        model="gpt-5.5",
        trial_id=TRIAL_ID,
        report=HarnessReport(
            status="completed",
            agent_verdict=AgentVerdict.INCONCLUSIVE,
            lifecycle_events=(),
            final_output=_fake_gateway_output(),
        ),
    )

    assert record.passed is True
    assert record.base_checks["result_submission_verified"] is True
    assert [item["call_id"] for item in record.ordered_exchanges if item["tool"] == "harness_channel.harness_submit_result"] == [
        "submit-invalid",
        "submit",
    ]


def test_base_evaluator_rejects_valid_result_without_matching_result_submitted_event(tmp_path: Path) -> None:
    ledger = PlatformLedger(tmp_path / "ledger")
    _append_base_success_events(ledger, include_submit=False)
    _append_submit_exchange(ledger, "submit", valid=True, include_result_event=False)

    record = evaluate_base_channel_qualification(
        ledger.query(trial_id=TRIAL_ID, limit=10_000),
        harness=HarnessKind.CODEX,
        model="gpt-5.5",
        trial_id=TRIAL_ID,
        report=HarnessReport(
            status="completed",
            agent_verdict=AgentVerdict.INCONCLUSIVE,
            lifecycle_events=(),
            final_output=_fake_gateway_output(),
        ),
    )

    assert record.passed is False
    assert record.base_checks["result_submission_verified"] is False
    assert "missing_valid_result_submission" in record.failure_reasons


@pytest.mark.parametrize(
    "damage,reason,check",
    [
        ("native", "missing_base_mcp_read_exchange", "mcp_read_verified"),
        ("no_confirm", "missing_harness_confirm_roundtrip", "confirmation_roundtrip_verified"),
        ("fake_hint", "base_consult_must_decline_without_hint", "consult_roundtrip_verified"),
        ("no_ack", "missing_notice_ack", "notice_ack_verified"),
        ("no_submit", "missing_valid_result_submission", "result_submission_verified"),
        ("d7_tool", "base_disallowed_mcp_server_exchange", "tool_evidence_verified"),
    ],
)
def test_base_evaluator_rejects_missing_or_forged_platform_evidence(
    tmp_path: Path,
    damage: str,
    reason: str,
    check: str,
) -> None:
    ledger = PlatformLedger(tmp_path / "ledger")
    _append_base_success_events(
        ledger,
        source="native_stream" if damage == "native" else "mcp_server",
        include_confirm=damage != "no_confirm",
        include_notice_ack=damage != "no_ack",
        include_submit=damage != "no_submit",
    )
    if damage == "d7_tool":
        _append_pair(
            ledger,
            "sandbox",
            "code_sandbox.run_python",
            {"ok": True, "exit_code": 0, "truncated": False},
            trial_id=TRIAL_ID,
            source="mcp_server",
        )
    if damage == "fake_hint":
        events = ledger.query(trial_id=TRIAL_ID, limit=10_000)
        for event in events:
            if event.event_type == "ToolResult" and event.payload.get("call_id") == "consult":
                event.payload["payload"]["hint_delivered"] = True
        record = evaluate_base_channel_qualification(
            events,
            harness=HarnessKind.CODEX,
            model="gpt-5.5",
            trial_id=TRIAL_ID,
            report=HarnessReport(
                status="completed",
                agent_verdict=AgentVerdict.INCONCLUSIVE,
                lifecycle_events=(),
                final_output=_fake_gateway_output(),
            ),
        )
    else:
        record = evaluate_base_channel_qualification(
            ledger.query(trial_id=TRIAL_ID, limit=10_000),
            harness=HarnessKind.CODEX,
            model="gpt-5.5",
            trial_id=TRIAL_ID,
            report=HarnessReport(
                status="completed",
                agent_verdict=AgentVerdict.INCONCLUSIVE,
                lifecycle_events=(),
                final_output=_fake_gateway_output(),
            ),
        )

    assert record.passed is False
    assert reason in record.failure_reasons
    assert record.base_checks[check] is False


@pytest.mark.parametrize(
    "tool",
    [
        "chaos_control.chaos_destroy_experiment",
        "chaos_mesh_control.chaos_mesh_destroy_experiment",
    ],
)
def test_base_evaluator_rejects_destroy_attempts(tmp_path: Path, tool: str) -> None:
    ledger = PlatformLedger(tmp_path / "ledger")
    _append_base_success_events(ledger)
    _append_pair(
        ledger,
        "destroy",
        tool,
        {"ok": False, "error": {"code": "TOOL_DISABLED"}},
        source="mcp_server",
        status="failed",
    )

    record = evaluate_base_channel_qualification(
        ledger.query(trial_id=TRIAL_ID, limit=10_000),
        harness=HarnessKind.CODEX,
        model="gpt-5.5",
        trial_id=TRIAL_ID,
        report=HarnessReport(
            status="completed",
            agent_verdict=AgentVerdict.INCONCLUSIVE,
            lifecycle_events=(),
            final_output=_fake_gateway_output(),
        ),
    )

    assert record.passed is False
    assert "mutation_attempted" in record.failure_reasons
    assert record.base_checks["tool_evidence_verified"] is False


def test_evaluator_rejects_forged_native_stream_evidence(tmp_path: Path) -> None:
    ledger = PlatformLedger(tmp_path / "ledger")
    _append_success_events(ledger, source="native_stream")

    record = _evaluate(ledger)

    assert record.passed is False
    assert "missing_mcp_telemetry_denial_exchange" in record.failure_reasons
    assert "missing_harness_consult" in record.failure_reasons


def test_evaluator_rejects_wrong_hint_body(tmp_path: Path) -> None:
    ledger = PlatformLedger(tmp_path / "ledger")
    _append_success_events(ledger, hint_message="use some other tool")

    record = _evaluate(ledger)

    assert record.passed is False
    assert "wrong_hint_body" in record.failure_reasons
    assert "missing_exact_hint_event" in record.failure_reasons


def test_evaluator_rejects_extra_hint_fields_after_controller_metadata_strip(tmp_path: Path) -> None:
    ledger = PlatformLedger(tmp_path / "ledger")
    _append_success_events(ledger)
    events = ledger.query(trial_id=TRIAL_ID, limit=10_000)
    for event in events:
        if event.event_type == "ToolResult" and event.payload.get("call_id") == "consult":
            event.payload["payload"]["unexpected"] = "not controller metadata"

    record = evaluate_channel_qualification(
        events,
        harness=HarnessKind.CODEX,
        model="gpt-5.5",
        trial_id=TRIAL_ID,
    )

    assert record.passed is False
    assert "wrong_hint_body" in record.failure_reasons


def test_evaluator_rejects_out_of_order_alternative_use(tmp_path: Path) -> None:
    ledger = PlatformLedger(tmp_path / "ledger")
    _append_success_events(ledger, coroot_before_hint=True)

    record = _evaluate(ledger)

    assert record.passed is False
    assert "required_order_violated" in record.failure_reasons


def test_evaluator_rejects_coroot_call_started_before_hint_even_if_result_finishes_after(tmp_path: Path) -> None:
    ledger = PlatformLedger(tmp_path / "ledger")
    _append_success_events(ledger, coroot_call_before_hint_result_after=True)

    record = _evaluate(ledger)

    assert record.passed is False
    assert "required_order_violated" in record.failure_reasons


def test_evaluator_rejects_missing_result_submission(tmp_path: Path) -> None:
    ledger = PlatformLedger(tmp_path / "ledger")
    _append_success_events(ledger, include_submit=False)

    record = _evaluate(ledger)

    assert record.passed is False
    assert "missing_valid_result_submission" in record.failure_reasons


def test_evaluator_rejects_failed_sandbox(tmp_path: Path) -> None:
    ledger = PlatformLedger(tmp_path / "ledger")
    _append_success_events(ledger, sandbox_ok=False)

    record = _evaluate(ledger)

    assert record.passed is False
    assert "run_python_failed" in record.failure_reasons


def test_evaluator_rejects_ok_sandbox_with_nonzero_exit_or_truncation(tmp_path: Path) -> None:
    ledger = PlatformLedger(tmp_path / "ledger")
    _append_success_events(ledger, sandbox_exit_code=1)

    record = _evaluate(ledger)

    assert record.passed is False
    assert "run_python_failed" in record.failure_reasons
    assert "missing_completed_sandbox_run_evidence" in record.failure_reasons


def test_evaluator_requires_sandbox_run_ledger_evidence(tmp_path: Path) -> None:
    ledger = PlatformLedger(tmp_path / "ledger")
    _append_success_events(ledger, include_sandbox_run=False)

    record = _evaluate(ledger)

    assert record.passed is False
    assert "missing_completed_sandbox_run_evidence" in record.failure_reasons


def test_evaluator_rejects_mutation_attempt(tmp_path: Path) -> None:
    ledger = PlatformLedger(tmp_path / "ledger")
    _append_success_events(ledger, include_mutation=True)

    record = _evaluate(ledger)

    assert record.passed is False
    assert "mutation_attempted" in record.failure_reasons


@pytest.mark.parametrize(
    "tool",
    [
        "chaos_control.chaos_destroy_experiment",
        "chaos_mesh_control.chaos_mesh_destroy_experiment",
    ],
)
def test_evaluator_rejects_destroy_attempts(tmp_path: Path, tool: str) -> None:
    ledger = PlatformLedger(tmp_path / "ledger")
    _append_success_events(ledger)
    _append_pair(
        ledger,
        "destroy",
        tool,
        {"ok": False, "error": {"code": "TOOL_DISABLED"}},
        source="mcp_server",
        status="failed",
    )

    record = _evaluate(ledger)

    assert record.passed is False
    assert "mutation_attempted" in record.failure_reasons


def test_evaluator_rejects_unclosed_mutation_tool_call(tmp_path: Path) -> None:
    ledger = PlatformLedger(tmp_path / "ledger")
    _append_success_events(ledger)
    ledger.append(
        trial_id=TRIAL_ID,
        event_type="ToolCall",
        occurred_at=NOW,
        payload={
            "source": "mcp_server",
            "call_id": "unclosed-create",
            "tool": "chaos_control.chaos_create_experiment",
            "arguments": {},
        },
    )

    record = _evaluate(ledger)

    assert record.passed is False
    assert "mutation_attempted" in record.failure_reasons
    assert "unclosed_tool_call_id" in record.failure_reasons


def test_evaluator_rejects_duplicate_unmatched_cross_trial_and_unsorted_events(tmp_path: Path) -> None:
    ledger = PlatformLedger(tmp_path / "ledger")
    _append_success_events(ledger)
    ledger.append(
        trial_id=TRIAL_ID,
        event_type="ToolCall",
        occurred_at=NOW,
        payload={
            "source": "mcp_server",
            "call_id": "duplicate",
            "tool": "source_ro.source_list_files",
            "arguments": {},
        },
    )
    ledger.append(
        trial_id=TRIAL_ID,
        event_type="ToolCall",
        occurred_at=NOW,
        payload={
            "source": "mcp_server",
            "call_id": "duplicate",
            "tool": "source_ro.source_list_files",
            "arguments": {},
        },
    )
    ledger.append(
        trial_id=TRIAL_ID,
        event_type="ToolResult",
        occurred_at=NOW,
        payload={
            "source": "mcp_server",
            "call_id": "missing-call",
            "status": "completed",
            "payload": {"ok": True},
        },
    )
    cross = ledger.append(
        trial_id="other-trial",
        event_type="ToolCall",
        occurred_at=NOW,
        payload={
            "source": "mcp_server",
            "call_id": "cross",
            "tool": "source_ro.source_list_files",
            "arguments": {},
        },
    )
    events = [cross, *ledger.query(trial_id=TRIAL_ID, limit=10_000)]

    record = evaluate_channel_qualification(
        events,
        harness=HarnessKind.CODEX,
        model="gpt-5.5",
        trial_id=TRIAL_ID,
    )

    assert record.passed is False
    assert "cross_trial_event" in record.failure_reasons
    assert "event_sequence_not_monotonic" in record.failure_reasons
    assert "duplicate_tool_call_id" in record.failure_reasons
    assert "unmatched_tool_result_id" in record.failure_reasons
    assert "unclosed_tool_call_id" in record.failure_reasons


def test_evaluator_rejects_failed_harness_report_even_with_good_ledger(tmp_path: Path) -> None:
    ledger = PlatformLedger(tmp_path / "ledger")
    _append_success_events(ledger)
    report = HarnessReport(
        status="failed",
        agent_verdict=AgentVerdict.INCONCLUSIVE,
        lifecycle_events=(),
        final_output={
            "validation_error": "OUTPUT_UNSTRUCTURED",
            "harness_error_code": "NATIVE_FAILED",
        },
    )

    record = evaluate_channel_qualification(
        ledger.query(trial_id=TRIAL_ID, limit=10_000),
        harness=HarnessKind.CODEX,
        model="gpt-5.5",
        trial_id=TRIAL_ID,
        report=report,
    )

    assert record.passed is False
    assert "harness_report_not_completed" in record.failure_reasons
    assert "harness_validation_error" in record.failure_reasons
    assert "harness_runtime_error" in record.failure_reasons


def test_supervisor_wrapper_patches_private_context_before_start(tmp_path: Path) -> None:
    context_file = tmp_path / "context.json"
    context_file.write_text(json.dumps({"trial_id": TRIAL_ID, "case_id": "C0", "variant": "A"}), encoding="utf-8")
    context_file.chmod(0o600)
    captured: dict[str, object] = {}

    class Supervisor:
        base_environment = {"RESBENCH_CHAOS_EXECUTE_ENABLED": "false"}

        def start_trial(self, **kwargs):
            captured["context"] = json.loads(context_file.read_text(encoding="utf-8"))
            captured["kwargs"] = kwargs
            return {"RESBENCH_HARNESS_CHANNEL_MCP_URL": "http://127.0.0.1:18085/mcp"}

        def stop(self):
            captured["stopped"] = True

    wrapper = QualificationHarnessChannelSupervisor(Supervisor())
    result = wrapper.start_trial(
        trial_id=TRIAL_ID,
        harness=HarnessKind.CODEX,
        token="x" * 40,
        token_state_files={},
        runtime_environment={"RESBENCH_HARNESS_CHANNEL_CONTEXT_FILE": str(context_file)},
    )

    assert result["RESBENCH_HARNESS_CHANNEL_MCP_URL"].endswith("/mcp")
    assert captured["context"]["case_id"] == "D7"
    assert captured["context"]["variant"] == "A"
    assert captured["context"]["qualification_type"] == CHANNEL_QUALIFICATION_MODE
    assert captured["context"]["scored_as_d7"] is False


@pytest.mark.parametrize("gateway_state", ["verified", "missing", "wrong_model", "unverified"])
def test_runner_builds_runtime_disables_fault_creation_and_writes_record(tmp_path: Path, gateway_state: str) -> None:
    ledger = PlatformLedger(tmp_path / "ledger")
    policy_calls: list[tuple[str, str, str | None, str]] = []
    restored: list[str] = []

    class PolicyRegistry:
        def set_server(self, server_name, *, state=None, source="controller", reason=None, **_kwargs):
            policy_calls.append(("server", server_name, None, source))

        def set_tool(self, server_name, tool_name, *, state=None, source="controller", reason=None):
            policy_calls.append(("tool", server_name, tool_name, source))

    class TokenRegistry:
        platform_ledger = ledger

        def policy_registry(self, trial_id):
            assert trial_id.startswith("campaign-")
            assert trial_id.endswith("-codex-d0-1")
            return PolicyRegistry()

    class Permissions:
        token_registry = TokenRegistry()

        def provision(self, campaign_id, trial_id, harness, episode, runtime):
            assert campaign_id.startswith("campaign-")
            assert trial_id.startswith(campaign_id + "-")
            assert runtime.tool_substitution_variant == "A"
            return CapabilityProfile(
                harness=harness,
                mcp_servers=(
                    "k8s_ro",
                    "telemetry_ro",
                    "source_ro",
                    "chaos_control",
                    "harness_channel",
                    "coroot_ro",
                    "chaos_mesh_control",
                    "code_sandbox",
                ),
                mcp_tools=(),
                kubernetes_rules=(),
                direct_kubeconfig=False,
                allowed_fault_types=("network-delay",),
                expires_at="2026-09-05T13:00:00Z",
            )

        def restore(self, trial_id):
            restored.append(trial_id)

    class Supervisor:
        def __init__(self):
            self.base_environment = {"RESBENCH_CHAOS_EXECUTE_ENABLED": "true"}

        def stop(self):
            pass

    class HarnessRunner:
        def __init__(self):
            self.mcp_supervisor = None
            self.base_environment = {}

        def run(self, **kwargs):
            assert kwargs["prompt_mode"] is PromptMode.VERBATIM
            assert kwargs["expected_outcome"] is ExpectedOutcome.SAFE_REFUSAL
            assert kwargs["case"].case_id is Stage2CaseId.C0
            kwargs["event_observer"](
                {
                    "event_type": "TOOL_INTERACTION",
                    "native_type": "tool_result",
                    "tool": "code_sandbox.run_python",
                    "payload": {
                        "source": "mcp_server",
                        "result": {
                            "ok": True,
                            "exit_code": 0,
                            "truncated": False,
                        },
                    },
                }
            )
            pending = ledger.pending_notices(
                trial_id=kwargs["trial_id"],
                include_claimed=True,
            )
            assert pending[0].notice_type == QUALIFICATION_NOTICE_TYPE
            _append_success_events(ledger, trial_id=kwargs["trial_id"])
            gateway_output = _fake_gateway_output()
            if gateway_state == "missing":
                gateway_output = {}
            elif gateway_state == "wrong_model":
                gateway_output = _fake_gateway_output("other-model")
            elif gateway_state == "unverified":
                gateway_output["gateway_evidence_verified"] = False
            return HarnessReport(
                status="completed",
                agent_verdict=AgentVerdict.INCONCLUSIVE,
                lifecycle_events=(),
                artifact_refs=("channel-qualification/trial/stdout.txt",),
                final_output=gateway_output,
            )

    supervisor = Supervisor()
    harness_runner = HarnessRunner()
    components = SimpleNamespace(
        permissions=Permissions(),
        token_registry=Permissions.token_registry,
        supervisor=supervisor,
        harness_runner=harness_runner,
    )

    class System:
        def build_runtime(self, episode, request_model_by_harness, *, namespace):
            assert request_model_by_harness == {HarnessKind.CODEX: "gpt-5.5"}
            assert namespace == "otel-demo"
            return components

    record = ChannelQualificationRunner(System()).run_one(
        episode=_episode_fixture(),
        harness=HarnessKind.CODEX,
        model="gpt-5.5",
        output_dir=tmp_path / "out",
    )

    assert record.passed is (gateway_state == "verified")
    if gateway_state == "verified":
        assert record.gateway_route["model_alias"] == "gpt-5.5"
        assert record.gateway_sidecar_evidence["verified"] is True
    else:
        assert "gateway_route_evidence_missing" in record.failure_reasons
    assert supervisor.base_environment["RESBENCH_CHAOS_EXECUTE_ENABLED"] == "false"
    assert harness_runner.base_environment["RESBENCH_CHAOS_EXECUTE_ENABLED"] == "false"
    assert policy_calls == [
        ("server", "telemetry_ro", None, "disturbance"),
        ("tool", "chaos_control", "chaos_create_experiment", "channel-qualification-safety"),
        ("tool", "chaos_mesh_control", "chaos_mesh_create_experiment", "channel-qualification-safety"),
    ]
    assert len(restored) == 1
    assert (tmp_path / "out" / "channel-qualification-codex.json").is_file()


def test_runner_reports_safe_mcp_supervisor_error_message_to_stderr(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ledger = PlatformLedger(tmp_path / "ledger")

    class PolicyRegistry:
        def set_server(self, *_args, **_kwargs):
            pass

        def set_tool(self, *_args, **_kwargs):
            pass

    class TokenRegistry:
        platform_ledger = ledger

        def policy_registry(self, _trial_id):
            return PolicyRegistry()

    class Permissions:
        token_registry = TokenRegistry()

        def provision(self, campaign_id, trial_id, harness, episode, runtime):
            return CapabilityProfile(
                harness=harness,
                mcp_servers=(
                    "k8s_ro",
                    "telemetry_ro",
                    "source_ro",
                    "chaos_control",
                    "harness_channel",
                ),
                mcp_tools=(),
                kubernetes_rules=(),
                direct_kubeconfig=False,
                allowed_fault_types=("network-delay",),
                expires_at="2026-09-05T13:00:00Z",
            )

        def restore(self, _trial_id):
            pass

    class Supervisor:
        def __init__(self):
            self.base_environment = {"RESBENCH_CHAOS_EXECUTE_ENABLED": "true"}

        def stop(self):
            pass

    class HarnessRunner:
        def __init__(self):
            self.mcp_supervisor = None
            self.base_environment = {}

        def run(self, **_kwargs):
            raise McpSupervisorError("MCP port did not become ready: 18185")

    components = SimpleNamespace(
        permissions=Permissions(),
        token_registry=Permissions.token_registry,
        supervisor=Supervisor(),
        harness_runner=HarnessRunner(),
    )

    class System:
        def build_runtime(self, episode, request_model_by_harness, *, namespace):
            return components

    record = ChannelQualificationRunner(System()).run_one(
        episode=_episode_fixture(),
        harness=HarnessKind.BLADEAI,
        model="gpt-5.5",
        output_dir=tmp_path / "out",
        profile="base",
    )

    stderr = capsys.readouterr().err
    assert "channel qualification MCP startup failed:" in stderr
    assert "MCP port did not become ready: 18185" in stderr
    assert "runner_error:McpSupervisorError" in record.failure_reasons


def test_base_runner_uses_only_foundational_servers_and_separate_record_prefix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = PlatformLedger(tmp_path / "ledger")
    policy_calls: list[tuple[str, str, str | None, str]] = []
    restored: list[str] = []
    captured: dict[str, object] = {}
    monkeypatch.setenv("RESBENCH_COROOT_URL", "https://coroot.invalid")
    monkeypatch.setenv("RESBENCH_COROOT_PROJECT_ID", "project-that-base-must-not-read")

    class PolicyRegistry:
        def set_server(self, server_name, *, state=None, source="controller", reason=None, **_kwargs):
            policy_calls.append(("server", server_name, None, source))

        def set_tool(self, server_name, tool_name, *, state=None, source="controller", reason=None):
            policy_calls.append(("tool", server_name, tool_name, source))

    class TokenRegistry:
        platform_ledger = ledger

        def policy_registry(self, trial_id):
            assert trial_id.startswith("campaign-")
            assert trial_id.endswith("-codex-d0-1")
            return PolicyRegistry()

    class Permissions:
        token_registry = TokenRegistry()

        def provision(self, campaign_id, trial_id, harness, episode, runtime):
            assert runtime.tool_substitution_variant is None
            assert runtime.main_fault["qualification_type"] == BASE_CHANNEL_QUALIFICATION_MODE
            assert "RESBENCH_COROOT_URL" not in supervisor.base_environment
            assert "RESBENCH_COROOT_PROJECT_ID" not in supervisor.base_environment
            return CapabilityProfile(
                harness=harness,
                mcp_servers=(
                    "k8s_ro",
                    "telemetry_ro",
                    "source_ro",
                    "chaos_control",
                    "harness_channel",
                ),
                mcp_tools=(
                    "harness_consult",
                    "harness_confirm",
                    "harness_submit_result",
                    "harness_poll_notices",
                    "k8s_list_resources",
                    "telemetry_workload_current",
                    "source_list_files",
                    "chaos_validate_plan",
                    "chaos_create_experiment",
                ),
                kubernetes_rules=(),
                direct_kubeconfig=False,
                allowed_fault_types=("network-delay",),
                expires_at="2026-09-05T13:00:00Z",
            )

        def restore(self, trial_id):
            restored.append(trial_id)

    class Supervisor:
        def __init__(self):
            self.base_environment = {"RESBENCH_CHAOS_EXECUTE_ENABLED": "true"}

        def stop(self):
            pass

    class HarnessRunner:
        def __init__(self):
            self.mcp_supervisor = None
            self.base_environment = {}

        def run(self, **kwargs):
            captured["prompt"] = kwargs["base_prompt"]
            captured["capability_servers"] = kwargs["capability"].mcp_servers
            captured["capability_tools"] = kwargs["capability"].mcp_tools
            assert "coroot_ro" not in kwargs["capability"].mcp_servers
            assert "chaos_mesh_control" not in kwargs["capability"].mcp_servers
            assert "code_sandbox" not in kwargs["capability"].mcp_servers
            assert kwargs["prompt_level_label"] == BASE_CHANNEL_QUALIFICATION_MODE
            assert "Never call chaos_create_experiment" in kwargs["base_prompt"]
            assert "Coroot tools" in kwargs["base_prompt"]
            assert "chaos_validate_plan" not in kwargs["base_prompt"]
            assert "source_ro" not in kwargs["base_prompt"]
            kwargs["event_observer"](
                {
                    "event_type": "TOOL_INTERACTION",
                    "native_type": "tool_result",
                    "tool": "harness_channel.harness_confirm",
                    "payload": {
                        "source": "mcp_server",
                        "result": {
                            "ok": True,
                            "allowed": False,
                            "reason": "no mutation authorized",
                        },
                    },
                }
            )
            pending = ledger.pending_notices(
                trial_id=kwargs["trial_id"],
                include_claimed=True,
            )
            assert pending[0].notice_type == QUALIFICATION_NOTICE_TYPE
            _append_base_success_events(ledger, trial_id=kwargs["trial_id"])
            return HarnessReport(
                status="completed",
                agent_verdict=AgentVerdict.INCONCLUSIVE,
                lifecycle_events=(),
                artifact_refs=("base-channel-qualification/trial/stdout.txt",),
                final_output=_fake_gateway_output(),
            )

    supervisor = Supervisor()
    harness_runner = HarnessRunner()
    components = SimpleNamespace(
        permissions=Permissions(),
        token_registry=Permissions.token_registry,
        supervisor=supervisor,
        harness_runner=harness_runner,
    )

    class System:
        def build_runtime(self, episode, request_model_by_harness, *, namespace):
            assert request_model_by_harness == {HarnessKind.CODEX: "gpt-5.5"}
            assert namespace == "otel-demo"
            return components

    record = ChannelQualificationRunner(System()).run_one(
        episode=_episode_fixture(),
        harness=HarnessKind.CODEX,
        model="gpt-5.5",
        output_dir=tmp_path / "out",
        profile="base",
    )

    assert record.passed is True
    assert record.qualification_type == BASE_CHANNEL_QUALIFICATION_MODE
    assert record.base_checks["gateway_evidence_verified"] is True
    assert captured["capability_servers"] == (
        "k8s_ro",
        "telemetry_ro",
        "source_ro",
        "chaos_control",
        "harness_channel",
    )
    assert policy_calls == [
        ("tool", "chaos_control", "chaos_create_experiment", "channel-qualification-safety"),
    ]
    assert len(restored) == 1
    assert "RESBENCH_COROOT_URL" not in supervisor.base_environment
    assert "RESBENCH_COROOT_PROJECT_ID" not in supervisor.base_environment
    assert (tmp_path / "out" / "base-channel-qualification-codex.json").is_file()
    assert not (tmp_path / "out" / "channel-qualification-codex.json").exists()


def test_real_permission_provision_for_base_adds_coroot_but_no_other_substitution_server(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RESBENCH_COROOT_URL", "https://coroot.invalid")
    manager = Stage2PermissionManager(
        private_root=tmp_path / "private",
        token_registry=McpTokenStateRegistry(tmp_path / "tokens"),
    )
    trial_id = "campaign-1234567890abcdef-bladeai-d0-1"
    runtime = qualification_runtime_context(
        trial_id=trial_id,
        episode_id="episode-1",
        namespace="otel-demo",
        profile="base",
    )

    profile = manager.provision(
        "campaign-1234567890abcdef",
        trial_id,
        HarnessKind.BLADEAI,
        _episode_fixture(),
        runtime,
    )
    context = manager.runtime_context(trial_id)
    policy = read_policy_file(Path(context["mcp_policy_file"]))

    # Coroot is registered for every Trial as a backup observation source;
    # only the remaining substitution tools stay D7/D8-only.
    optional = {"chaos_mesh_control", "code_sandbox"}
    assert runtime.tool_substitution_variant is None
    assert set(profile.mcp_servers) == {
        "k8s_ro",
        "telemetry_ro",
        "source_ro",
        "chaos_control",
        "harness_channel",
        "coroot_ro",
    }
    assert optional.isdisjoint(set(context["mcp_token_files"]))
    assert optional.isdisjoint(set(policy.servers))


def test_runner_provisions_real_token_registry_with_campaign_trial_identity(tmp_path: Path) -> None:
    token_registry = McpTokenStateRegistry(tmp_path / "tokens")
    permissions = Stage2PermissionManager(
        private_root=tmp_path / "private",
        token_registry=token_registry,
    )

    class Supervisor:
        def __init__(self):
            self.base_environment = {}

        def stop(self):
            pass

    class HarnessRunner:
        def __init__(self):
            self.mcp_supervisor = None
            self.base_environment = {}

        def run(self, **kwargs):
            assert kwargs["campaign_id"].startswith("campaign-")
            assert kwargs["trial_id"].startswith(kwargs["campaign_id"] + "-")
            assert kwargs["trial_id"].endswith("-codex-d0-1")
            _append_success_events(
                token_registry.platform_ledger,
                trial_id=kwargs["trial_id"],
            )
            return HarnessReport(
                status="completed",
                agent_verdict=AgentVerdict.INCONCLUSIVE,
                lifecycle_events=(),
                final_output=_fake_gateway_output(),
            )

    components = SimpleNamespace(
        permissions=permissions,
        token_registry=token_registry,
        supervisor=Supervisor(),
        harness_runner=HarnessRunner(),
    )

    class System:
        def build_runtime(self, episode, request_model_by_harness, *, namespace):
            return components

    repo_root = Path(__file__).resolve().parents[1]
    episode = load_fixed_episode(fixed_otel_episode_ref(repo_root), root=repo_root)

    record = ChannelQualificationRunner(System()).run_one(
        episode=episode,
        harness=HarnessKind.CODEX,
        model="gpt-5.5",
    )

    assert record.passed is True
    assert record.status == "passed"
    assert record.trial_id.startswith("campaign-")
    assert record.trial_id.endswith("-codex-d0-1")
    assert not (tmp_path / "tokens" / record.trial_id).exists()


def test_token_registry_trial_identity_allows_nested_campaign_trial_path(tmp_path: Path) -> None:
    registry = McpTokenStateRegistry(tmp_path / "tokens")

    paths = registry.initialize(
        "campaign-x/trial-y",
        {"telemetry_ro": "x" * 32},
    )

    token_path = Path(paths["telemetry_ro"])
    assert token_path == tmp_path / "tokens" / "campaign-x" / "trial-y" / "telemetry_ro.token"
    assert token_path.is_file()


@pytest.mark.parametrize(
    "trial_id",
    [
        "/campaign-x/trial-y",
        "campaign-x/../trial-y",
        "campaign-x/./trial-y",
        "campaign-x//trial-y",
        "campaign-x\\trial-y",
        "campaign-x/trial_y",
        "campaign-x/trial-é",
        "trial-y",
    ],
)
def test_token_registry_trial_identity_rejects_unsafe_path_components(
    tmp_path: Path,
    trial_id: str,
) -> None:
    registry = McpTokenStateRegistry(tmp_path / "tokens")

    with pytest.raises(RuntimeAdapterError, match="invalid token-state identity"):
        registry.initialize(trial_id, {"telemetry_ro": "x" * 32})


def test_runner_records_cleanup_failure_without_dropping_result(tmp_path: Path) -> None:
    ledger = PlatformLedger(tmp_path / "ledger")

    class TokenRegistry:
        platform_ledger = ledger

        def policy_registry(self, trial_id):
            class PolicyRegistry:
                def set_server(self, *args, **kwargs):
                    pass

                def set_tool(self, *args, **kwargs):
                    pass

            return PolicyRegistry()

    class Permissions:
        token_registry = TokenRegistry()

        def provision(self, campaign_id, trial_id, harness, episode, runtime):
            return CapabilityProfile(
                harness=harness,
                mcp_servers=(
                    "k8s_ro",
                    "telemetry_ro",
                    "source_ro",
                    "chaos_control",
                    "harness_channel",
                    "coroot_ro",
                    "chaos_mesh_control",
                    "code_sandbox",
                ),
                mcp_tools=(),
                kubernetes_rules=(),
                direct_kubeconfig=False,
                allowed_fault_types=("network-delay",),
                expires_at="2026-09-05T13:00:00Z",
            )

        def restore(self, trial_id):
            raise RuntimeError("token")

    class HarnessRunner:
        def __init__(self):
            self.mcp_supervisor = None
            self.base_environment = {}

        def run(self, **kwargs):
            _append_success_events(ledger, trial_id=kwargs["trial_id"])
            return HarnessReport(
                status="completed",
                agent_verdict=AgentVerdict.INCONCLUSIVE,
                lifecycle_events=(),
            )

    class Supervisor:
        def __init__(self):
            self.base_environment = {}

        def stop(self):
            raise RuntimeError("stop")

    class Traffic:
        def close(self):
            raise RuntimeError("traffic")

    components = SimpleNamespace(
        permissions=Permissions(),
        token_registry=Permissions.token_registry,
        supervisor=Supervisor(),
        harness_runner=HarnessRunner(),
        traffic=Traffic(),
    )

    class System:
        def build_runtime(self, episode, request_model_by_harness, *, namespace):
            return components

    record = ChannelQualificationRunner(System()).run_one(
        episode=_episode_fixture(),
        harness=HarnessKind.CODEX,
        model="gpt-5.5",
        output_dir=tmp_path / "out",
    )

    assert record.passed is False
    assert "cleanup_failed" in record.failure_reasons
    assert set(record.cleanup_errors) == {
        "traffic.close:RuntimeError",
        "permissions.restore:RuntimeError",
        "supervisor.stop:RuntimeError",
    }
    assert (tmp_path / "out" / "channel-qualification-codex.json").is_file()


def test_collective_equality_requires_all_four_harnesses() -> None:
    records = [
        ChannelQualificationRecord(
            **_fake_gateway_fields(),
            harness=harness.value,
            model="gpt-5.5",
            trial_id=f"trial-{harness.value}",
            status="passed",
            passed=True,
            telemetry_denial_body=TOOL_DISABLED_RESPONSE,
            hint_body=EXPECTED_HINT_BODY,
        )
        for harness in ALL_CHANNEL_HARNESSES
    ]

    result = collective_equality_check(records)

    assert result["qualification_type"] == "CHANNEL_QUALIFICATION"
    assert result["qualification_profile"] == "CHANNEL_QUALIFICATION"
    assert result["complete_harness_set"] is True
    assert result["all_passed"] is True
    assert result["telemetry_denial_body_equal"] is True
    assert result["hint_body_equal"] is True
    assert result["scored_as_d7"] is False


def test_collective_rejects_duplicate_harnesses_and_mixed_models() -> None:
    records = [
        ChannelQualificationRecord(
            harness="codex",
            model="gpt-5.5",
            trial_id="trial-1",
            passed=True,
            telemetry_denial_body=TOOL_DISABLED_RESPONSE,
            hint_body=EXPECTED_HINT_BODY,
        ),
        ChannelQualificationRecord(
            harness="codex",
            model="claude-opus-5",
            trial_id="trial-2",
            passed=True,
            telemetry_denial_body=TOOL_DISABLED_RESPONSE,
            hint_body=EXPECTED_HINT_BODY,
        ),
    ]

    result = collective_equality_check(records)

    assert result["complete_harness_set"] is False
    assert result["all_passed"] is False
    assert result["duplicate_harnesses"] == ["codex"]
    assert result["mixed_models"] is True


@pytest.mark.parametrize("damage", ["mixed_profile", "missing_profile", "different_hint"])
def test_collective_cannot_mix_qualification_profiles_or_help(damage: str) -> None:
    records = [ChannelQualificationRecord(harness=h.value, model="gpt-5.5", passed=True,
                                         **_fake_gateway_fields()).as_dict()
               for h in ALL_CHANNEL_HARNESSES]
    if damage == "mixed_profile":
        records[0]["qualification_profile"] = BASE_CHANNEL_QUALIFICATION_MODE
    elif damage == "missing_profile":
        records[0].pop("qualification_profile")
    else:
        records[0]["hint_body"] = {"message": "different help"}
    assert collective_equality_check(records)["all_passed"] is False


@pytest.mark.parametrize("damage", ["missing_version", "mixed_version", "wrong_route", "missing_proof"])
def test_collective_rejects_unqualified_gateway_even_with_a_complete_harness_set(damage):
    records = [ChannelQualificationRecord(harness=h.value, model="gpt-5.5", passed=True,
                                         **_fake_gateway_fields()).as_dict()
               for h in ALL_CHANNEL_HARNESSES]
    if damage == "missing_version":
        records[0]["gateway_config_sha256"] = ""
    elif damage == "mixed_version":
        records[0]["gateway_config_sha256"] = "b" * 64
    elif damage == "wrong_route":
        records[0]["gateway_route"]["model_alias"] = "other-model"
    else:
        records[0]["gateway_sidecar_evidence"]["verified"] = False
    result = collective_equality_check(records)
    assert result["complete_harness_set"] is True
    assert result["all_passed"] is False


def test_write_record_fails_on_existing_file_and_symlink_parent(tmp_path: Path) -> None:
    record = ChannelQualificationRecord(harness="codex", model="gpt-5.5", trial_id="trial-1")
    output = tmp_path / "out"
    write_record(output, record)

    with pytest.raises(RuntimeError, match="already exists"):
        write_record(output, record)

    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(RuntimeError, match="symlinks"):
        write_record(link / "child", ChannelQualificationRecord(harness="bladeai"))


def test_base_and_substitution_records_use_separate_files(tmp_path: Path) -> None:
    output = tmp_path / "out"
    substitution = ChannelQualificationRecord(harness="codex", model="gpt-5.5", trial_id="trial-sub")
    base = ChannelQualificationRecord(
        harness="codex",
        model="gpt-5.5",
        trial_id="trial-base",
        qualification_type=BASE_CHANNEL_QUALIFICATION_MODE,
        qualification_profile=BASE_CHANNEL_QUALIFICATION_MODE,
    )

    substitution_path = write_record(output, substitution)
    base_path = write_record(output, base)

    assert substitution_path.name == "channel-qualification-codex.json"
    assert base_path.name == "base-channel-qualification-codex.json"
    assert substitution_path.read_text(encoding="utf-8") != base_path.read_text(encoding="utf-8")


def test_base_collective_uses_base_prefix_and_profile(tmp_path: Path) -> None:
    output = tmp_path / "out"
    records = [
        ChannelQualificationRecord(
            **_fake_gateway_fields(),
            harness=harness.value,
            model="gpt-5.5",
            trial_id=f"trial-{harness.value}",
            status="passed",
            passed=True,
            qualification_type=BASE_CHANNEL_QUALIFICATION_MODE,
            qualification_profile=BASE_CHANNEL_QUALIFICATION_MODE,
        )
        for harness in ALL_CHANNEL_HARNESSES
    ]
    for record in records:
        write_record(output, record)

    collective_path = write_collective_check(output, profile="base")

    assert collective_path == output / "base-channel-qualification-collective.json"
    collective = json.loads(collective_path.read_text(encoding="utf-8"))
    assert collective["qualification_type"] == BASE_CHANNEL_QUALIFICATION_MODE
    assert collective["qualification_profile"] == BASE_CHANNEL_QUALIFICATION_MODE
    assert collective["complete_harness_set"] is True
    assert collective["all_passed"] is True


def test_cli_runs_runner_and_writes_collective_record(tmp_path: Path, monkeypatch, capsys) -> None:
    from scripts import qualify_agent_channel as cli

    calls: list[tuple[str, ...]] = []
    monkeypatch.setenv("RESBENCH_AGENT_EXEC_SOCKET", str(tmp_path / "agent-exec" / "agent.sock"))

    class Config:
        repo_root = tmp_path
        private_root = tmp_path / "private"

    class FakeRunner:
        def __init__(self, system, *, namespace):
            self.system = system
            self.namespace = namespace

        def run_all(self, *, episode, model, harnesses, output_dir, profile):
            assert profile == "substitution"
            with pytest.raises(RuntimeLockBusy):
                RuntimeLock.from_environment().acquire(owner="nested-api")
            calls.append(tuple(harness.value for harness in harnesses))
            records = [
                ChannelQualificationRecord(
                    **_fake_gateway_fields(model),
                    harness=harness.value,
                    model=model,
                    trial_id=f"trial-{harness.value}",
                    status="passed",
                    passed=True,
                    telemetry_denial_body=TOOL_DISABLED_RESPONSE,
                    hint_body=EXPECTED_HINT_BODY,
                )
                for harness in harnesses
            ]
            for record in records:
                write_record(output_dir, record)
            return records

    monkeypatch.setattr(cli.Stage2RuntimeConfig, "from_env", lambda: Config())
    monkeypatch.setattr(cli, "fixed_otel_episode_ref", lambda repo_root: "episode-ref")
    monkeypatch.setattr(cli, "load_fixed_episode", lambda ref, root: _episode_fixture())
    monkeypatch.setattr(cli, "Stage2System", lambda config: SimpleNamespace(config=config))
    monkeypatch.setattr(cli, "ChannelQualificationRunner", FakeRunner)

    rc = cli.main(
        [
            "--model",
            "gpt-5.5",
            "--profile",
            "substitution",
            "--output-dir",
            str(tmp_path / "out"),
            "--protected-root",
            str(tmp_path / "private"),
        ]
    )

    assert rc == 0
    assert calls == [tuple(harness.value for harness in ALL_CHANNEL_HARNESSES)]
    collective = json.loads((tmp_path / "out" / "channel-qualification-collective.json").read_text(encoding="utf-8"))
    assert collective["complete_harness_set"] is True
    assert collective["all_passed"] is True
    printed = json.loads(capsys.readouterr().out)
    assert printed["collective"]["hint_body_equal"] is True
    with RuntimeLock.from_environment().acquire(owner="after-cli"):
        pass


def test_cli_rejects_mismatched_protected_root(tmp_path: Path, monkeypatch) -> None:
    from scripts import qualify_agent_channel as cli

    class Config:
        repo_root = tmp_path
        private_root = tmp_path / "private"

    monkeypatch.setattr(cli.Stage2RuntimeConfig, "from_env", lambda: Config())

    with pytest.raises(SystemExit):
        cli.main(
            [
                "--model",
                "gpt-5.5",
                "--profile",
                "substitution",
                "--output-dir",
                str(tmp_path / "out"),
                "--protected-root",
                str(tmp_path / "other-private"),
            ]
        )


def test_cli_rejects_non_otel_namespace() -> None:
    from scripts import qualify_agent_channel as cli

    with pytest.raises(SystemExit):
        cli.parse_args(
            [
                "--model",
                "gpt-5.5",
                "--profile",
                "base",
                "--output-dir",
                "/tmp/channel-qualification",
                "--namespace",
                "other",
            ]
        )
