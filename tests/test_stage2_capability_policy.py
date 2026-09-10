from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from stage2_service.capability_policy import (
    CapabilityPolicyError,
    CapabilityPolicyDocument,
    CapabilityPolicyRegistry,
    MCP_POLICY_FILE_ENV,
    PLATFORM_LEDGER_ROOT_ENV,
    effective_tool_state,
    is_channel_unavailable,
    platform_ledger_root_from_env,
    policy_file_from_env,
    read_policy_file,
    write_policy_file,
)
from stage2_service.contracts import (
    BladeAINativePermissions,
    OperationUncertaintyVariant,
    PermissionProfile,
    ServerPolicy,
    ToolPolicy,
)
from stage2_service.platform_ledger import PlatformLedger


_REGISTRY_SET_TOOL = """
from pathlib import Path
import sys
from stage2_service.capability_policy import CapabilityPolicyRegistry

root = Path(sys.argv[1])
tool = sys.argv[2]
registry = CapabilityPolicyRegistry(root)
registry.set_tool("telemetry_ro", tool, state="disabled", source="worker")
"""


def _profile(*servers: str) -> PermissionProfile:
    return PermissionProfile(
        profile_id="p0-full-authorized",
        mcp_servers=servers,
        bladeai_native=BladeAINativePermissions(
            kubernetes_read=True,
            kubernetes_metrics=True,
            chaosblade_execute=True,
        ),
    )


def test_permission_profile_models_static_mcp_and_bladeai_native_permissions() -> None:
    profile = _profile("k8s_ro", "telemetry_ro", "source_ro", "chaos_control")

    assert profile.schema_version == "stage2-permission-profile.v1"
    assert profile.bladeai_native.chaosblade_execute is True
    assert profile.mcp_servers == (
        "k8s_ro",
        "telemetry_ro",
        "source_ro",
        "chaos_control",
    )


def test_registry_initializes_policy_file_and_records_policy_applied(tmp_path: Path) -> None:
    ledger = PlatformLedger(tmp_path / "ledger")
    registry = CapabilityPolicyRegistry(tmp_path / "policy", ledger=ledger)

    document = registry.initialize(
        "trial-1",
        _profile("k8s_ro", "telemetry_ro"),
        source="unit-test",
    )

    assert document.sequence == 1
    assert document.source == "unit-test"
    assert set(document.servers) == {"k8s_ro", "telemetry_ro"}
    assert oct(registry.root.stat().st_mode & 0o777) == "0o700"
    assert oct(registry.policy_path.stat().st_mode & 0o777) == "0o600"
    events = ledger.query()
    assert [event.event_type for event in events] == ["POLICY_APPLIED"]
    assert events[0].payload["sequence"] == 1


def test_registry_set_tool_set_server_and_restore_are_monotonic(tmp_path: Path) -> None:
    registry = CapabilityPolicyRegistry(tmp_path / "policy")
    initial = registry.initialize("trial-1", _profile("telemetry_ro"))
    changed_tool = registry.set_tool(
        "telemetry_ro",
        "telemetry_prom_metric_range",
        state="disabled",
        reason="withdrawn",
        source="d7",
    )
    changed_server = registry.set_server(
        "telemetry_ro",
        state="disabled",
        source="d3",
    )
    restored = registry.restore(initial, source="restore")

    assert [initial.sequence, changed_tool.sequence, changed_server.sequence, restored.sequence] == [1, 2, 3, 4]
    assert changed_tool.source == "d7"
    assert changed_tool.since >= initial.since
    assert (
        changed_tool.tool_policy("telemetry_ro", "telemetry_prom_metric_range").state
        == "disabled"
    )
    assert changed_server.server_policy("telemetry_ro").state == "disabled"
    assert restored.server_policy("telemetry_ro").state == "enabled"
    assert registry.snapshot().sequence == 4


def test_server_state_is_inherited_by_unlisted_or_inheriting_tool() -> None:
    server = ServerPolicy(
        server_name="telemetry_ro",
        state="disabled",
        tools={
            "explicit_inherit": ToolPolicy(state=None),
            "explicit_enabled": ToolPolicy(state="enabled"),
        },
    )

    assert effective_tool_state(server, "unlisted_tool") == "disabled"
    assert effective_tool_state(server, "explicit_inherit") == "disabled"
    assert effective_tool_state(server, "explicit_enabled") == "enabled"
    assert effective_tool_state(None, "anything") == "disabled"


def test_harness_channel_cannot_be_mutated(tmp_path: Path) -> None:
    registry = CapabilityPolicyRegistry(tmp_path / "policy")

    with pytest.raises(CapabilityPolicyError, match="harness_channel"):
        registry.initialize("trial-1", _profile("harness_channel"))

    registry.initialize("trial-1", _profile("telemetry_ro"))
    with pytest.raises(CapabilityPolicyError, match="harness_channel"):
        registry.set_server("harness_channel", state="disabled")
    with pytest.raises(CapabilityPolicyError, match="harness_channel"):
        registry.set_tool("harness_channel", "ask_harness", state="disabled")


def test_write_policy_file_is_atomic_private_and_readable_without_cache(tmp_path: Path) -> None:
    path = tmp_path / "policy" / "tools.policy.json"
    first = write_policy_file(
        path,
        trial_id="trial-1",
        source="first",
        servers={
            "telemetry_ro": ServerPolicy(
                server_name="telemetry_ro",
                tools={
                    "telemetry_prom_metric_range": ToolPolicy(state="disabled"),
                },
            )
        },
    )
    second = write_policy_file(
        path,
        trial_id="trial-1",
        source="second",
        servers={
            "telemetry_ro": ServerPolicy(
                server_name="telemetry_ro",
                tools={
                    "telemetry_prom_metric_range": ToolPolicy(state="enabled"),
                },
            )
        },
    )

    assert first.tool_policy("telemetry_ro", "telemetry_prom_metric_range").state == "disabled"
    assert second.tool_policy("telemetry_ro", "telemetry_prom_metric_range").state == "enabled"
    assert oct(path.parent.stat().st_mode & 0o777) == "0o700"
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert read_policy_file(path).source == "second"


def test_read_policy_file_fails_closed_for_missing_bad_mode_invalid_json_or_symlink(tmp_path: Path) -> None:
    missing = tmp_path / "missing.json"
    with pytest.raises(CapabilityPolicyError, match="missing"):
        read_policy_file(missing)

    path = tmp_path / "policy.json"
    path.write_text("{}", encoding="utf-8")
    path.chmod(0o644)
    with pytest.raises(CapabilityPolicyError, match="0600"):
        read_policy_file(path)

    path.write_text("{not-json", encoding="utf-8")
    path.chmod(0o600)
    with pytest.raises(CapabilityPolicyError, match="invalid"):
        read_policy_file(path)

    target = tmp_path / "target.json"
    write_policy_file(target, trial_id="trial-1", servers={})
    link = tmp_path / "link.json"
    link.symlink_to(target)
    with pytest.raises(CapabilityPolicyError, match="symlinks"):
        read_policy_file(link)

    symlink_parent = tmp_path / "linked-dir"
    real_parent = tmp_path / "real-dir"
    real_parent.mkdir()
    symlink_parent.symlink_to(real_parent)
    with pytest.raises(CapabilityPolicyError, match="symlinks"):
        write_policy_file(
            symlink_parent / "policy.json",
            trial_id="trial-1",
            servers={},
        )


def test_policy_document_validates_server_key_channel_expiry_and_d6_service_field() -> None:
    now = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)
    policy = ServerPolicy(
        server_name="chaos_control",
        channel_unavailable_until=now + timedelta(seconds=5),
        chaos_create_uncertainty_variant=OperationUncertaintyVariant.NOT_APPLIED,
    )

    assert is_channel_unavailable(policy, now=now)
    assert not is_channel_unavailable(policy, now=now + timedelta(seconds=6))
    assert policy.chaos_create_uncertainty_variant is OperationUncertaintyVariant.NOT_APPLIED
    with pytest.raises(ValidationError, match="server policy key"):
        CapabilityPolicyDocument(
            trial_id="trial-1",
            sequence=1,
            source="unit",
            since=now,
            servers={"k8s_ro": ServerPolicy(server_name="telemetry_ro")},
        )


def test_policy_file_from_env_distinguishes_unconfigured_and_configured_missing(tmp_path: Path) -> None:
    assert policy_file_from_env({}) is None
    assert platform_ledger_root_from_env({}) is None
    with pytest.raises(CapabilityPolicyError, match=MCP_POLICY_FILE_ENV):
        policy_file_from_env({MCP_POLICY_FILE_ENV: "relative-policy.json"})
    with pytest.raises(CapabilityPolicyError, match=PLATFORM_LEDGER_ROOT_ENV):
        platform_ledger_root_from_env({PLATFORM_LEDGER_ROOT_ENV: "relative-ledger"})

    path = tmp_path / "tools.policy.json"
    ledger = tmp_path / "ledger"
    assert policy_file_from_env({MCP_POLICY_FILE_ENV: str(path)}) == path
    assert platform_ledger_root_from_env({PLATFORM_LEDGER_ROOT_ENV: str(ledger)}) == ledger


def test_policy_file_rejects_non_object_json(tmp_path: Path) -> None:
    path = tmp_path / "policy.json"
    path.write_text(json.dumps([]), encoding="utf-8")
    path.chmod(0o600)

    with pytest.raises(CapabilityPolicyError, match="invalid"):
        read_policy_file(path)


def test_cross_instance_concurrent_registry_updates_do_not_lose_tools(tmp_path: Path) -> None:
    root = tmp_path / "policy"
    registry = CapabilityPolicyRegistry(root)
    registry.initialize("trial-concurrent", _profile("telemetry_ro"))
    process_count = 8
    processes = [
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                _REGISTRY_SET_TOOL,
                str(root),
                f"tool_{index}",
            ],
            cwd=Path(__file__).resolve().parents[1],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for index in range(process_count)
    ]

    failures = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=20)
        if process.returncode != 0:
            failures.append(
                {
                    "returncode": process.returncode,
                    "stdout": stdout,
                    "stderr": stderr,
                }
            )

    assert failures == []
    document = registry.snapshot()
    assert document.sequence == process_count + 1
    assert set(document.server_policy("telemetry_ro").tools) == {
        f"tool_{index}" for index in range(process_count)
    }
