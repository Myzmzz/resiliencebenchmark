"""O08: MCP permission restoration must survive a Controller restart.

Before this change the pre-revocation token existed only in the Controller
process (``McpTokenStateRegistry._original``), so a restart between revocation
and restoration turned an ordinary D1/D3/D4 recovery into
``MCP permission restoration state is missing``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from stage2_service.runtime_adapters import McpTokenStateRegistry, RuntimeAdapterError


TRIAL_ID = "campaign-1234567890abcdef-codex-t1"
ORIGINAL = "o" * 48


def _registry(root: Path) -> McpTokenStateRegistry:
    """A fresh instance on the same root is what a Controller restart looks like."""
    return McpTokenStateRegistry(root)


def _last_event(registry: McpTokenStateRegistry, event_type: str) -> dict:
    events = registry.platform_ledger.query_dicts(trial_id=TRIAL_ID, limit=100)
    return [event for event in events if event["event_type"] == event_type][-1]


def test_restore_succeeds_after_the_controller_process_is_replaced(tmp_path: Path):
    before = _registry(tmp_path / "state")
    paths = before.initialize(TRIAL_ID, {"chaos_control": ORIGINAL})
    before.revoke(TRIAL_ID, "mcp.chaos.create")
    token_file = Path(paths["chaos_control"])
    assert token_file.read_text(encoding="utf-8") != ORIGINAL

    restarted = _registry(tmp_path / "state")
    assert restarted._original == {}

    restored = restarted.restore(TRIAL_ID, "mcp.chaos.create")

    assert restored["verified"] is True
    assert token_file.read_text(encoding="utf-8") == ORIGINAL
    assert _last_event(restarted, "MCP_PERMISSION_RESTORED")["payload"][
        "snapshot_source"
    ] == "ledger_snapshot"


def test_restore_is_idempotent_across_repeated_calls(tmp_path: Path):
    registry = _registry(tmp_path / "state")
    paths = registry.initialize(TRIAL_ID, {"chaos_control": ORIGINAL})
    registry.revoke(TRIAL_ID, "mcp.chaos.create")

    first = registry.restore(TRIAL_ID, "mcp.chaos.create")
    second = registry.restore(TRIAL_ID, "mcp.chaos.create")

    assert first == second == {
        "server": "chaos_control",
        "capability": "mcp.chaos.create",
        "verified": True,
    }
    restorations = [
        event
        for event in registry.platform_ledger.query_dicts(trial_id=TRIAL_ID, limit=100)
        if event["event_type"] == "MCP_PERMISSION_RESTORED"
    ]
    assert [event["payload"]["already_restored"] for event in restorations] == [False, True]
    assert Path(paths["chaos_control"]).read_text(encoding="utf-8") == ORIGINAL


def test_missing_snapshot_reports_the_server_and_the_expected_path(tmp_path: Path):
    registry = _registry(tmp_path / "state")
    registry.initialize(TRIAL_ID, {"chaos_control": ORIGINAL})
    registry.revoke(TRIAL_ID, "mcp.chaos.create")

    restarted = _registry(tmp_path / "state")
    snapshot = restarted._restore_path(TRIAL_ID, "chaos_control")
    snapshot.unlink()

    with pytest.raises(RuntimeAdapterError) as error:
        restarted.restore(TRIAL_ID, "mcp.chaos.create")

    message = str(error.value)
    assert "chaos_control" in message
    assert snapshot.as_posix() in message


def test_unmapped_capability_is_rejected_before_any_snapshot_lookup(tmp_path: Path):
    registry = _registry(tmp_path / "state")
    registry.initialize(TRIAL_ID, {"chaos_control": ORIGINAL})

    with pytest.raises(RuntimeAdapterError, match="no MCP server mapping"):
        registry.restore(TRIAL_ID, "mcp.nonexistent.capability")


def test_restoration_evidence_is_recorded_without_the_token_value(tmp_path: Path):
    registry = _registry(tmp_path / "state")
    registry.initialize(TRIAL_ID, {"chaos_control": ORIGINAL})
    registry.revoke(TRIAL_ID, "mcp.chaos.create")
    registry.restore(TRIAL_ID, "mcp.chaos.create")

    events = registry.platform_ledger.query_dicts(trial_id=TRIAL_ID, limit=100)
    types = [event["event_type"] for event in events]
    assert "MCP_PERMISSION_SNAPSHOT" in types
    assert "MCP_PERMISSION_REVOKED" in types
    assert "MCP_PERMISSION_RESTORED" in types

    serialized = json.dumps(events)
    assert ORIGINAL not in serialized
    restored = next(
        event for event in events if event["event_type"] == "MCP_PERMISSION_RESTORED"
    )
    assert len(restored["payload"]["token_sha256"]) == 64


def test_snapshot_files_stay_private_to_the_controller(tmp_path: Path):
    registry = _registry(tmp_path / "state")
    registry.initialize(TRIAL_ID, {"chaos_control": ORIGINAL})

    snapshot = registry._restore_path(TRIAL_ID, "chaos_control")

    assert snapshot.is_file()
    assert snapshot.stat().st_mode & 0o077 == 0
