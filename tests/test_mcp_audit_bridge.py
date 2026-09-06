from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from mcp_servers.audit_bridge import (
    MAX_MESSAGE_BYTES,
    AuditBridgeClient,
    AuditBridgeConfig,
    AuditBridgeDenied,
    AuditBridgeError,
    AuditBridgeListener,
)
from stage2_service.harness_adapters.base import ToolCall, ToolResult
from stage2_service.platform_ledger import PlatformLedger
from stage2_service.tool_event_pump import RealtimeToolEventPump


@pytest.fixture
def config() -> AuditBridgeConfig:
    # macOS AF_UNIX has a short pathname limit; a dedicated 0700 /tmp directory
    # is still a real protected local-socket test, unlike an abstract mock.
    with tempfile.TemporaryDirectory(prefix="ab-", dir="/tmp") as directory:
        root = Path(directory)
        os.chmod(root, 0o700)
        yield AuditBridgeConfig(root / "audit.sock", "trial-a", "controller")


def _read_line(connection: socket.socket) -> dict:
    data = b""
    while not data.endswith(b"\n"):
        data += connection.recv(4096)
    return json.loads(data)


def _raw_request(path: Path, message: dict) -> dict:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.connect(str(path))
        connection.sendall(json.dumps(message).encode() + b"\n")
        return _read_line(connection)


def test_realtime_order_original_time_and_pump_ledger(tmp_path: Path, config: AuditBridgeConfig) -> None:
    received: list[tuple[object, str]] = []
    pump = RealtimeToolEventPump(
        "trial-a", PlatformLedger(tmp_path / "ledger"),
        lambda event, source: received.append((event, source)) or {"allowed": True},
    )
    originally_occurred = datetime(2026, 9, 5, 1, 2, 3, tzinfo=UTC)
    with AuditBridgeListener(config, pump):
        client = AuditBridgeClient(config)
        decision = client.before_call("telemetry_ro", "query", {"pod": "cart"}, occurred_at=originally_occurred)
        client.after_call(decision.call_id, {"ok": True}, "completed", occurred_at=originally_occurred)

    assert [type(event) for event, _source in received] == [ToolCall, ToolResult]
    assert [event.call_id for event, _source in received] == [decision.call_id, decision.call_id]
    assert [source for _event, source in received] == ["mcp_server", "mcp_server"]
    events = pump.ledger.query(trial_id="trial-a")
    assert [event.event_type for event in events] == ["ToolCall", "ToolResult"]
    assert events[0].occurred_at == originally_occurred.isoformat()
    assert events[0].payload["tool"] == "telemetry_ro.query"


def test_multimegabyte_bound_accepts_result_larger_than_legacy_64k(config: AuditBridgeConfig) -> None:
    received: list[ToolCall | ToolResult] = []
    with AuditBridgeListener(config, lambda event, _source: received.append(event) or {"allowed": True}):
        client = AuditBridgeClient(config)
        decision = client.before_call("telemetry_ro", "query", {"range": "fault-window"})
        payload = {"raw_trace_window": "x" * (64 * 1024 + 1)}
        client.after_call(decision.call_id, payload, "completed")
    assert isinstance(received[-1], ToolResult)
    assert received[-1].payload == payload


def test_parallel_calls_have_controller_generated_unique_ids(config: AuditBridgeConfig) -> None:
    calls: list[ToolCall] = []
    lock = threading.Lock()

    def callback(event: ToolCall | ToolResult, source: str) -> dict:
        if isinstance(event, ToolCall):
            with lock:
                calls.append(event)
        return {"allowed": True}

    with AuditBridgeListener(config, callback):
        def invoke(index: int) -> None:
            client = AuditBridgeClient(config)
            decision = client.before_call("k8s_ro", "get", {"index": index})
            client.after_call(decision.call_id, {"index": index}, "completed")

        workers = [threading.Thread(target=invoke, args=(index,)) for index in range(24)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=3)
            assert not worker.is_alive()

    assert len(calls) == 24
    assert len({call.call_id for call in calls}) == 24
    assert all(len(call.call_id) == 32 for call in calls)


def test_timeout_or_unavailable_bridge_never_executes_operation(config: AuditBridgeConfig) -> None:
    executed = False

    def operation() -> dict:
        nonlocal executed
        executed = True
        return {"ok": True}

    with pytest.raises(AuditBridgeError, match="not executed"):
        AuditBridgeClient(config).invoke("chaos_control", "create_experiment", {}, operation)
    assert executed is False

    slow_config = AuditBridgeConfig(config.socket_path, "trial-a", "controller", timeout_seconds=0.03)
    with AuditBridgeListener(slow_config, lambda _event, _source: time.sleep(0.15) or {"allowed": True}):
        with pytest.raises(AuditBridgeError, match="not executed"):
            AuditBridgeClient(slow_config).invoke("chaos_control", "create_experiment", {}, operation)
        time.sleep(0.18)
    assert executed is False


def test_after_failure_does_not_claim_operation_was_not_executed(config: AuditBridgeConfig) -> None:
    with pytest.raises(AuditBridgeError, match="may have executed and result audit is unacknowledged"):
        AuditBridgeClient(config).after_call("already-executed-call", {"ok": True}, "completed")


def test_denial_and_callback_exception_never_execute_operation(config: AuditBridgeConfig) -> None:
    executed = False

    def operation() -> dict:
        nonlocal executed
        executed = True
        return {"ok": True}

    with AuditBridgeListener(config, lambda _event, _source: {"allowed": False, "reason": "budget"}):
        with pytest.raises(AuditBridgeDenied, match="budget"):
            AuditBridgeClient(config).invoke("chaos_control", "create_experiment", {}, operation)
    assert executed is False
    with AuditBridgeListener(config, lambda _event, _source: (_ for _ in ()).throw(RuntimeError("boom"))):
        with pytest.raises(AuditBridgeError, match="callback failed"):
            AuditBridgeClient(config).invoke("chaos_control", "create_experiment", {}, operation)
    assert executed is False


def test_controller_denial_emits_authoritative_paired_result(config: AuditBridgeConfig) -> None:
    received: list[ToolCall | ToolResult] = []

    def callback(event: ToolCall | ToolResult, _source: str) -> dict:
        received.append(event)
        return {"allowed": False, "reason": "budget"} if isinstance(event, ToolCall) else {}

    with AuditBridgeListener(config, callback):
        decision = AuditBridgeClient(config).before_call("chaos_control", "chaos_create_experiment", {})

    assert decision.allowed is False
    assert [type(event) for event in received] == [ToolCall, ToolResult]
    assert received[1].status == "denied"
    assert received[1].call_id == decision.call_id
    assert received[1].payload["controller_call_id"] == decision.call_id
    assert received[1].payload["error"]["code"] == "CONTROLLER_CALL_DENIED"

def test_cross_trial_oversize_and_unknown_result_are_rejected(config: AuditBridgeConfig) -> None:
    with AuditBridgeListener(config, lambda _event, _source: {"allowed": True}):
        rejected = _raw_request(config.socket_path, {
            "kind": "before_call", "trial_id": "other", "authority": "controller",
            "server": "k8s_ro", "tool": "get", "arguments": {},
            "occurred_at": datetime.now(UTC).isoformat(),
        })
        assert rejected["ok"] is False
        assert "cross-trial" in rejected["reason"]

        unknown = _raw_request(config.socket_path, {
            "kind": "after_call", "trial_id": "trial-a", "authority": "controller",
            "call_id": "agent-supplied", "status": "completed", "payload": {},
            "occurred_at": datetime.now(UTC).isoformat(),
        })
        assert unknown["ok"] is False
        assert "unknown" in unknown["reason"]

        with pytest.raises(AuditBridgeError, match="size limit"):
            AuditBridgeClient(config).before_call("k8s_ro", "get", {"large": "x" * MAX_MESSAGE_BYTES})


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="SO_PEERCRED is Linux-only qualification")
def test_linux_rejects_unauthorized_peer_uid(config: AuditBridgeConfig) -> None:
    # This is deliberately a UID that cannot be the current test process.  The
    # connect succeeds through filesystem permissions, then the kernel peer
    # credential gate rejects it before any callback receives the message.
    expected_uid = os.getuid() + 1
    with AuditBridgeListener(config, lambda _event, _source: {"allowed": True}, expected_uid=expected_uid):
        result = _raw_request(config.socket_path, {
            "kind": "before_call", "trial_id": "trial-a", "authority": "controller",
            "server": "k8s_ro", "tool": "get", "arguments": {},
            "occurred_at": datetime.now(UTC).isoformat(),
        })
    assert result == {"ok": False, "reason": "unauthorized audit bridge peer"}


def test_result_callback_failure_is_not_acknowledged(config: AuditBridgeConfig) -> None:
    calls = 0
    failed_once = False

    def callback(event: ToolCall | ToolResult, _source: str) -> dict:
        nonlocal calls, failed_once
        calls += 1
        if isinstance(event, ToolResult) and not failed_once:
            failed_once = True
            raise RuntimeError("ledger unavailable")
        return {"allowed": True}

    with AuditBridgeListener(config, callback):
        client = AuditBridgeClient(config)
        decision = client.before_call("telemetry_ro", "query", {})
        with pytest.raises(AuditBridgeError, match="callback failed.*may have executed"):
            client.after_call(decision.call_id, {}, "completed")
        # Exact replay is allowed because the failed result remains pending.
        client.after_call(decision.call_id, {}, "completed")
        client.after_call(decision.call_id, {}, "completed")
    assert calls == 3


def test_existing_socket_is_never_unlinked_and_close_is_concurrent_safe(config: AuditBridgeConfig) -> None:
    incumbent = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    incumbent.bind(str(config.socket_path))
    try:
        with pytest.raises(AuditBridgeError, match="existing audit bridge socket"):
            AuditBridgeListener(config, lambda _event, _source: {"allowed": True}).start()
        assert config.socket_path.is_socket()
    finally:
        incumbent.close()
        config.socket_path.unlink()

    listener = AuditBridgeListener(config, lambda _event, _source: {"allowed": True}).start()
    closers = [threading.Thread(target=listener.close) for _ in range(2)]
    for closer in closers:
        closer.start()
    for closer in closers:
        closer.join(timeout=2)
        assert not closer.is_alive()
    assert not config.socket_path.exists()
    with pytest.raises(AuditBridgeError, match="not executed"):
        AuditBridgeClient(config).before_call("k8s_ro", "get", {})


def test_repeated_result_is_one_ledger_record_and_client_verifies_peer(tmp_path: Path, config: AuditBridgeConfig) -> None:
    received: list[ToolCall | ToolResult] = []
    pump = RealtimeToolEventPump(
        "trial-a", PlatformLedger(tmp_path / "ledger"),
        lambda event, _source: received.append(event) or {"allowed": True},
    )
    with AuditBridgeListener(config, pump):
        client = AuditBridgeClient(config)
        decision = client.before_call("telemetry_ro", "query", {})
        client.after_call(decision.call_id, {"ok": True}, "completed")
        client.after_call(decision.call_id, {"ok": True}, "completed")
        if sys.platform.startswith("linux"):
            assert client.platform_identity_verified is True
        else:
            assert client.platform_identity_verified is False
    assert [event.event_type for event in pump.ledger.query(trial_id="trial-a")] == ["ToolCall", "ToolResult"]
    assert len(received) == 2


def test_pump_retries_failed_result_observer_without_second_ledger_record(tmp_path: Path) -> None:
    attempts = 0

    def observer(event: ToolCall | ToolResult, _source: str) -> dict:
        nonlocal attempts
        if isinstance(event, ToolResult):
            attempts += 1
            if attempts == 1:
                raise RuntimeError("root observer transient failure")
        return {"allowed": True}

    pump = RealtimeToolEventPump("trial-a", PlatformLedger(tmp_path / "ledger"), observer)
    result = ToolResult(call_id="controller-call", status="completed", payload={"ok": True})
    with pytest.raises(RuntimeError, match="transient"):
        pump(result, "mcp_server")
    assert [event.event_type for event in pump.ledger.query(trial_id="trial-a")] == ["ToolResult"]
    assert pump(result, "mcp_server") == {}
    assert attempts == 2
    assert [event.event_type for event in pump.ledger.query(trial_id="trial-a")] == ["ToolResult"]
