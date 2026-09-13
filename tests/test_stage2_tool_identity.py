"""O19: every spelling of a tool name resolves, and unknown names are recorded.

During the Dx round codex called MCP tools by their bare name. Its own client
answered "tool does not exist" before anything left the Agent runtime, so the
platform saw neither the call nor the error; the agent read the silence as a
platform disturbance and cleaned up the fault early.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from scripts import run_harness_trial as trial
from stage2_service.harness_adapters.base import ToolCall, normalize_tool_name
from stage2_service.mcp_tool_catalog import (
    MCP_TOOL_CATALOG,
    SERVERS_BY_BARE_TOOL,
    resolve_tool_identity,
)


CANONICAL = "chaos_control.chaos_create_experiment"


@pytest.mark.parametrize(
    ("raw", "hint", "resolution"),
    [
        ("chaos_control.chaos_create_experiment", None, "qualified"),
        ("mcp__chaos_control__chaos_create_experiment", None, "client_prefixed"),
        ("chaos_create_experiment", "chaos_control", "server_hint"),
        ("chaos_create_experiment", None, "inferred_unique"),
        ("  chaos_create_experiment  ", None, "inferred_unique"),
    ],
)
def test_every_accepted_spelling_resolves_to_one_canonical_name(raw, hint, resolution):
    identity = resolve_tool_identity(raw, hint)

    assert identity.canonical == CANONICAL
    assert (identity.server, identity.tool) == ("chaos_control", "chaos_create_experiment")
    assert identity.resolution == resolution
    assert identity.known is True
    assert normalize_tool_name(raw, hint) == CANONICAL


@pytest.mark.parametrize(
    "raw",
    ["Bash", "Read", "chaos_control.not_a_tool", "mcp__other_server__whatever"],
)
def test_unknown_names_stay_unknown_and_keep_what_the_agent_wrote(raw):
    identity = resolve_tool_identity(raw)

    assert identity.known is False
    assert identity.resolution == "unknown"
    assert identity.raw == raw


def test_empty_names_are_not_invented():
    for raw in (None, "", "   ", 17):
        identity = resolve_tool_identity(raw)
        assert identity.canonical is None
        assert identity.known is False
        assert normalize_tool_name(raw) is None


def test_no_bare_tool_name_is_owned_by_two_servers():
    """The unique-owner inference is only safe while this holds."""
    ambiguous = {tool: servers for tool, servers in SERVERS_BY_BARE_TOOL.items() if len(servers) > 1}

    assert ambiguous == {}


def test_the_agent_runtime_and_the_catalogue_agree_on_the_tool_surface():
    assert trial.ALLOWED_MCP_TOOLS == {
        server: set(tools) for server, tools in MCP_TOOL_CATALOG.items()
    }


def _call(tool: str, *, raw_tool: str | None = None, resolution: str | None = None) -> ToolCall:
    return ToolCall(
        call_id="call-1",
        tool=tool,
        raw_tool=raw_tool,
        tool_resolution=resolution,
        arguments={},
        occurred_at=datetime(2026, 9, 12, tzinfo=timezone.utc),
    )


def test_a_bare_name_is_no_longer_treated_as_a_non_mcp_tool():
    call = _call(CANONICAL, raw_tool="chaos_create_experiment", resolution="inferred_unique")

    assert trial.allowed_mcp_tool_call(call) is True
    assert trial.forbidden_tool_call(call) is False
    assert trial.unknown_tool_events([call]) == []


def test_an_unknown_name_is_recorded_with_both_spellings():
    call = _call("Bash", raw_tool="Bash", resolution="unknown")

    assert trial.forbidden_tool_call(call) is True
    rows = trial.unknown_tool_events([call])

    assert len(rows) == 1
    row = rows[0]
    assert row["kind"] == "error"
    assert row["summary"] == "UNKNOWN_TOOL_NAME"
    assert row["call_id"] == "call-1"
    assert row["tool_identity"]["raw"] == "Bash"
    assert row["tool_identity"]["known"] is False
    assert row["tool_identity"]["recorded_tool"] == "Bash"


def test_unknown_tool_rows_satisfy_the_run_trace_schema():
    import json

    import jsonschema

    repo_root = Path(__file__).resolve().parents[1]
    schema = json.loads(
        (repo_root / trial.DEFAULT_RUN_TRACE_SCHEMA).read_text(encoding="utf-8")
    )
    rows = trial.unknown_tool_events([_call("Bash", raw_tool="Bash", resolution="unknown")])

    jsonschema.validate(rows, schema["properties"]["events"])
