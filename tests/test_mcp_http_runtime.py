from __future__ import annotations

import asyncio
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from mcp_servers.audit_bridge import AuditBridgeClient, AuditBridgeConfig, AuditBridgeListener
from mcp_servers.http_runtime import (
    CHANNEL_UNAVAILABLE_RESPONSE,
    PLATFORM_POLICY_ERROR_RESPONSE,
    TOOL_DISABLED_RESPONSE,
    PolicyGate,
)
from stage2_service.capability_policy import (
    MCP_POLICY_FILE_ENV,
    PLATFORM_LEDGER_ROOT_ENV,
    write_policy_file,
)
from stage2_service.contracts import ServerPolicy, ToolPolicy
from stage2_service.platform_ledger import PlatformLedger
from stage2_service.harness_adapters.base import ToolCall, ToolResult


@contextmanager
def _audit_config(trial_id: str):
    # AF_UNIX path limits are much shorter than pytest's tmp_path hierarchy.
    with tempfile.TemporaryDirectory(prefix="pg-", dir="/tmp") as root:
        yield AuditBridgeConfig(Path(root) / "audit.sock", trial_id, "controller")


class CountingService:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    async def get_resource(self, *, namespace: str, resource: str, name: str):
        self.calls.append(
            {"namespace": namespace, "resource": resource, "name": name}
        )
        return {"ok": True, "name": name}


def _policy_path(tmp_path: Path) -> Path:
    return tmp_path / "trial" / "tools.policy.json"


def test_exact_error_shapes_do_not_include_hints() -> None:
    assert TOOL_DISABLED_RESPONSE == {
        "ok": False,
        "error": {
            "code": "TOOL_DISABLED",
            "message": "该工具已停用。",
        },
    }
    assert CHANNEL_UNAVAILABLE_RESPONSE["error"]["code"] == "CHANNEL_UNAVAILABLE"
    assert "hint" not in TOOL_DISABLED_RESPONSE
    assert "next_step" not in TOOL_DISABLED_RESPONSE
    assert "hint" not in CHANNEL_UNAVAILABLE_RESPONSE
    assert "next_step" not in CHANNEL_UNAVAILABLE_RESPONSE


def test_unconfigured_policy_gate_preserves_existing_behavior() -> None:
    calls = []
    gate = PolicyGate(server_name="telemetry_ro", policy_file=None)

    @gate.guard("telemetry_workload_current")
    def tool() -> dict[str, object]:
        calls.append("called")
        return {"ok": True}

    assert tool() == {"ok": True}
    assert calls == ["called"]


def test_disabled_tool_is_blocked_without_calling_backend_and_records_ledger(tmp_path: Path) -> None:
    path = _policy_path(tmp_path)
    write_policy_file(
        path,
        trial_id="trial-disabled",
        servers={
            "telemetry_ro": ServerPolicy(
                server_name="telemetry_ro",
                tools={
                    "telemetry_prom_metric_range": ToolPolicy(
                        state="disabled",
                        reason="D7 primary observation tool withdrawn",
                    )
                },
            )
        },
    )
    calls = []
    gate = PolicyGate(
        server_name="telemetry_ro",
        policy_file=path,
        ledger_root=tmp_path / "shared-ledger",
    )

    @gate.guard("telemetry_prom_metric_range")
    async def tool() -> dict[str, object]:
        calls.append("called")
        return {"ok": True}

    assert asyncio.run(tool()) == TOOL_DISABLED_RESPONSE
    assert calls == []
    event = PlatformLedger(tmp_path / "shared-ledger").query()[0]
    assert event.trial_id == "trial-disabled"
    assert event.event_type == "TOOL_CALL_DENIED_DISABLED"
    assert event.payload["tool"] == "telemetry_prom_metric_range"
    assert event.payload["state"] == "disabled"
    assert event.payload["reason"] == "D7 primary observation tool withdrawn"


def test_server_disabled_blocks_unlisted_tool_by_inheritance(tmp_path: Path) -> None:
    path = _policy_path(tmp_path)
    write_policy_file(
        path,
        trial_id="trial-server-disabled",
        servers={
            "source_ro": ServerPolicy(
                server_name="source_ro",
                state="disabled",
                reason="server revoked",
            )
        },
    )
    called = False
    gate = PolicyGate(
        server_name="source_ro",
        policy_file=path,
        ledger_root=tmp_path / "ledger",
    )

    @gate.guard("source_unlisted_future_tool")
    def tool() -> dict[str, object]:
        nonlocal called
        called = True
        return {"ok": True}

    assert tool() == TOOL_DISABLED_RESPONSE
    assert called is False
    event = PlatformLedger(tmp_path / "ledger").query()[0]
    assert event.event_type == "TOOL_CALL_DENIED_DISABLED"
    assert event.payload["reason"] == "server revoked"


def test_configured_document_missing_server_fails_closed(tmp_path: Path) -> None:
    path = _policy_path(tmp_path)
    write_policy_file(
        path,
        trial_id="trial-missing-server",
        servers={"k8s_ro": ServerPolicy(server_name="k8s_ro")},
    )
    calls = []
    gate = PolicyGate(
        server_name="telemetry_ro",
        policy_file=path,
        ledger_root=tmp_path / "ledger",
    )

    @gate.guard("telemetry_workload_current")
    def tool() -> dict[str, object]:
        calls.append("called")
        return {"ok": True}

    assert tool() == TOOL_DISABLED_RESPONSE
    assert calls == []
    event = PlatformLedger(tmp_path / "ledger").query()[0]
    assert event.event_type == "TOOL_CALL_DENIED_DISABLED"
    assert event.payload["state"] == "disabled"


def test_decoy_tool_is_blocked_and_records_decoy_invoked(tmp_path: Path) -> None:
    path = _policy_path(tmp_path)
    write_policy_file(
        path,
        trial_id="trial-decoy",
        servers={
            "chaos_control": ServerPolicy(
                server_name="chaos_control",
                tools={"chaos_create_experiment": ToolPolicy(state="decoy")},
            )
        },
    )
    called = False
    gate = PolicyGate(
        server_name="chaos_control",
        policy_file=path,
        ledger_root=tmp_path / "ledger",
    )

    @gate.guard("chaos_create_experiment")
    def create() -> dict[str, object]:
        nonlocal called
        called = True
        return {"ok": True}

    assert create() == TOOL_DISABLED_RESPONSE
    assert called is False
    event = PlatformLedger(tmp_path / "ledger").query()[0]
    assert event.event_type == "DECOY_INVOKED"
    assert event.payload["state"] == "decoy"


def test_policy_gate_reads_same_policy_file_every_call_without_mtime_cache(tmp_path: Path) -> None:
    path = _policy_path(tmp_path)
    write_policy_file(
        path,
        trial_id="trial-reload",
        servers={
            "source_ro": ServerPolicy(
                server_name="source_ro",
                tools={"source_read_file": ToolPolicy(state="disabled")},
            )
        },
    )
    calls = []
    gate = PolicyGate(
        server_name="source_ro",
        policy_file=path,
        ledger_root=tmp_path / "ledger",
    )

    @gate.guard("source_read_file")
    def read_file() -> dict[str, object]:
        calls.append("called")
        return {"ok": True}

    assert read_file() == TOOL_DISABLED_RESPONSE
    write_policy_file(
        path,
        trial_id="trial-reload",
        servers={
            "source_ro": ServerPolicy(
                server_name="source_ro",
                tools={"source_read_file": ToolPolicy(state="enabled")},
            )
        },
    )
    assert read_file() == {"ok": True}
    assert calls == ["called"]


def test_channel_unavailable_until_uses_d5_error_then_recovers_after_expiry(tmp_path: Path) -> None:
    path = _policy_path(tmp_path)
    future = datetime.now(timezone.utc) + timedelta(seconds=60)
    write_policy_file(
        path,
        trial_id="trial-channel",
        servers={
            "k8s_ro": ServerPolicy(
                server_name="k8s_ro",
                channel_unavailable_until=future,
            )
        },
    )
    calls = []
    gate = PolicyGate(
        server_name="k8s_ro",
        policy_file=path,
        ledger_root=tmp_path / "ledger",
    )

    @gate.guard("k8s_list_events")
    def list_events() -> dict[str, object]:
        calls.append("called")
        return {"ok": True}

    assert list_events() == CHANNEL_UNAVAILABLE_RESPONSE
    assert calls == []
    event = PlatformLedger(tmp_path / "ledger").query()[0]
    assert event.event_type == "CHANNEL_UNAVAILABLE_RETURNED"

    past = datetime.now(timezone.utc) - timedelta(seconds=1)
    write_policy_file(
        path,
        trial_id="trial-channel",
        servers={
            "k8s_ro": ServerPolicy(
                server_name="k8s_ro",
                channel_unavailable_until=past,
            )
        },
    )
    assert list_events() == {"ok": True}
    assert calls == ["called"]


def test_configured_missing_or_corrupt_policy_fails_closed_without_backend_call(tmp_path: Path) -> None:
    missing = tmp_path / "missing" / "tools.policy.json"
    calls = []
    gate = PolicyGate(
        server_name="telemetry_ro",
        policy_file=missing,
        ledger_root=tmp_path / "ledger",
    )

    @gate.guard("telemetry_workload_current")
    def tool() -> dict[str, object]:
        calls.append("called")
        return {"ok": True}

    assert tool() == PLATFORM_POLICY_ERROR_RESPONSE
    assert calls == []

    corrupt = _policy_path(tmp_path)
    corrupt.parent.mkdir(mode=0o700, parents=True)
    corrupt.write_text("{bad-json", encoding="utf-8")
    corrupt.chmod(0o600)
    gate = PolicyGate(
        server_name="telemetry_ro",
        policy_file=corrupt,
        ledger_root=tmp_path / "ledger",
    )
    assert gate.guard("telemetry_workload_current")(tool)() == PLATFORM_POLICY_ERROR_RESPONSE
    assert calls == []


def test_chaos_uncertainty_variant_is_read_from_server_policy(tmp_path: Path) -> None:
    path = _policy_path(tmp_path)
    write_policy_file(
        path,
        trial_id="trial-d6",
        servers={
            "chaos_control": ServerPolicy(
                server_name="chaos_control",
                chaos_create_uncertainty_variant="D6-A",
            )
        },
    )
    gate = PolicyGate(server_name="chaos_control", policy_file=path)

    assert gate.chaos_create_uncertainty_variant() == "D6-A"


def test_from_env_prefers_shared_platform_ledger_root(monkeypatch, tmp_path: Path) -> None:
    path = _policy_path(tmp_path)
    ledger_root = tmp_path / "shared-ledger"
    write_policy_file(
        path,
        trial_id="trial-env",
        servers={
            "telemetry_ro": ServerPolicy(
                server_name="telemetry_ro",
                tools={"telemetry_workload_current": ToolPolicy(state="disabled")},
            )
        },
    )
    monkeypatch.setenv(MCP_POLICY_FILE_ENV, str(path))
    monkeypatch.setenv(PLATFORM_LEDGER_ROOT_ENV, str(ledger_root))
    gate = PolicyGate.from_env("telemetry_ro")

    @gate.guard("telemetry_workload_current")
    def tool() -> dict[str, object]:
        return {"ok": True}

    assert tool() == TOOL_DISABLED_RESPONSE
    assert PlatformLedger(ledger_root).query()[0].trial_id == "trial-env"
    assert not (path.parent / "platform-ledger").exists()


def test_configured_audit_without_trial_identity_refuses_server_start(monkeypatch) -> None:
    monkeypatch.setenv("RESBENCH_MCP_AUDIT_SOCKET", "/tmp/controlled.sock")
    monkeypatch.delenv("RESBENCH_AUTHORIZED_RUN_ID", raising=False)
    monkeypatch.delenv("RESBENCH_MCP_AUDIT_AUTHORITY", raising=False)
    with pytest.raises(RuntimeError, match="requires RESBENCH_AUTHORIZED_RUN_ID"):
        PolicyGate.from_env("telemetry_ro")


def test_k8s_server_guard_uses_env_policy_before_service_call(monkeypatch, tmp_path: Path) -> None:
    path = _policy_path(tmp_path)
    write_policy_file(
        path,
        trial_id="trial-k8s",
        servers={
            "k8s_ro": ServerPolicy(
                server_name="k8s_ro",
                tools={"k8s_get_resource": ToolPolicy(state="disabled")},
            )
        },
    )
    monkeypatch.setenv(MCP_POLICY_FILE_ENV, str(path))

    from mcp_servers.k8s_ro.server import create_server

    service = CountingService()
    server = create_server(service=service)
    result = asyncio.run(
        server.call_tool(
            "k8s_get_resource",
            {"namespace": "otel-demo", "resource": "pods", "name": "cart-1"},
        )
    )

    assert result.structured_content == TOOL_DISABLED_RESPONSE
    assert service.calls == []


def test_realtime_audit_precedes_policy_and_returns_controller_receipt(tmp_path: Path) -> None:
    """A Controller policy mutation on before-call applies to that same call."""
    path = _policy_path(tmp_path)
    write_policy_file(
        path,
        trial_id="trial-realtime",
        servers={"telemetry_ro": ServerPolicy(server_name="telemetry_ro")},
    )
    seen: list[ToolCall | ToolResult] = []

    def controller(event: ToolCall | ToolResult, _source: str) -> dict[str, object]:
        seen.append(event)
        if isinstance(event, ToolCall):
            write_policy_file(
                path,
                trial_id="trial-realtime",
                servers={
                    "telemetry_ro": ServerPolicy(
                        server_name="telemetry_ro",
                        tools={"telemetry_workload_current": ToolPolicy(state="disabled")},
                    )
                },
            )
        return {"allowed": True}

    backend_calls: list[str] = []
    with _audit_config("trial-realtime") as config, AuditBridgeListener(config, controller):
        gate = PolicyGate(
            server_name="telemetry_ro",
            policy_file=path,
            ledger_root=tmp_path / "ledger",
            audit_client=AuditBridgeClient(config),
        )

        @gate.guard("telemetry_workload_current")
        async def query(*, namespace: str) -> dict[str, object]:
            backend_calls.append(namespace)
            return {"ok": True}

        result = asyncio.run(query(namespace="otel-demo"))

    assert backend_calls == []
    assert result["error"]["code"] == "TOOL_DISABLED"
    assert isinstance(result["controller_call_id"], str)
    assert [type(event) for event in seen] == [ToolCall, ToolResult]
    assert seen[0].arguments == {"namespace": "otel-demo"}
    assert seen[1].payload["controller_call_id"] == result["controller_call_id"]
    assert seen[1].payload["error"]["code"] == "TOOL_DISABLED"
