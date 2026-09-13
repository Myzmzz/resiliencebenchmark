"""Single source of truth for MCP tool identity across the Stage-2 boundary.

Agents address the same tool in three different spellings: the MCP client form
``mcp__<server>__<tool>``, the canonical form ``<server>.<tool>``, and — often
enough to matter — the bare ``<tool>``.  Codex used the bare form during the Dx
round; its own client answered "tool does not exist" without the platform ever
seeing the call, so the agent concluded the platform was disturbing it and
cleaned up the fault early (O19).

Resolution here is deliberately conservative: a bare name is canonicalized only
when exactly one server owns it, and anything else is reported as ``unknown``
rather than guessed at, so an unknown call is recorded instead of disappearing.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping


MCP_TOOL_CATALOG: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "harness_channel": frozenset(
            {
                "harness_consult",
                "harness_confirm",
                "harness_submit_result",
                "harness_poll_notices",
            }
        ),
        "k8s_ro": frozenset(
            {
                "k8s_get_resource",
                "k8s_list_resources",
                "k8s_list_events",
                "k8s_pod_logs",
                "k8s_cluster_inventory",
            }
        ),
        "telemetry_ro": frozenset(
            {
                "telemetry_workload_current",
                "telemetry_prom_metric_instant",
                "telemetry_prom_metric_range",
                "telemetry_prom_metric_series",
                "telemetry_prom_list_labels",
                "telemetry_jaeger_list_services",
                "telemetry_jaeger_list_operations",
                "telemetry_jaeger_find_traces",
                "telemetry_loki_list_labels",
                "telemetry_loki_logs",
                "telemetry_loki_logs_range",
            }
        ),
        "source_ro": frozenset(
            {
                "source_list_repositories",
                "source_list_files",
                "source_search_text",
                "source_read_file",
                "source_show_commit",
            }
        ),
        "chaos_control": frozenset(
            {
                "chaos_validate_plan",
                "chaos_inventory_run",
                "chaos_create_experiment",
                "chaos_get_experiment",
                "chaos_operation_status",
                "chaos_destroy_experiment",
                "chaos_recovery_status",
            }
        ),
        "coroot_ro": frozenset(
            {"coroot_metrics_range", "coroot_traces_find", "coroot_logs_range"}
        ),
        "chaos_mesh_control": frozenset(
            {
                "chaos_mesh_validate_plan",
                "chaos_mesh_inventory_run",
                "chaos_mesh_create_experiment",
                "chaos_mesh_get_experiment",
                "chaos_mesh_operation_status",
                "chaos_mesh_destroy_experiment",
                "chaos_mesh_recovery_status",
            }
        ),
        "code_sandbox": frozenset({"run_python"}),
    }
)


def _servers_by_bare_tool() -> Mapping[str, tuple[str, ...]]:
    owners: dict[str, list[str]] = {}
    for server, tools in MCP_TOOL_CATALOG.items():
        for tool in tools:
            owners.setdefault(tool, []).append(server)
    return MappingProxyType(
        {tool: tuple(sorted(servers)) for tool, servers in owners.items()}
    )


SERVERS_BY_BARE_TOOL: Mapping[str, tuple[str, ...]] = _servers_by_bare_tool()

# Resolution outcomes, most to least certain.
QUALIFIED = "qualified"
CLIENT_PREFIXED = "client_prefixed"
SERVER_HINT = "server_hint"
INFERRED_UNIQUE = "inferred_unique"
AMBIGUOUS = "ambiguous"
UNKNOWN = "unknown"


@dataclass(frozen=True)
class ToolIdentity:
    """What the agent wrote, what the platform made of it, and how."""

    raw: str
    canonical: str | None
    server: str | None
    tool: str | None
    resolution: str

    @property
    def known(self) -> bool:
        return self.server is not None and self.tool is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "raw": self.raw,
            "canonical": self.canonical,
            "server": self.server,
            "tool": self.tool,
            "resolution": self.resolution,
            "known": self.known,
        }


def resolve_tool_identity(raw: Any, server_hint: Any = None) -> ToolIdentity:
    """Map any spelling of a tool name onto ``<server>.<tool>`` when it is certain."""

    if not isinstance(raw, str) or not raw.strip():
        return ToolIdentity(raw="", canonical=None, server=None, tool=None, resolution=UNKNOWN)
    value = raw.strip()
    hint = server_hint.strip() if isinstance(server_hint, str) and server_hint.strip() else None

    if value.startswith("mcp__"):
        parts = value.split("__")
        if len(parts) >= 3 and parts[1] and parts[2]:
            return _identify(parts[1], parts[2], raw=value, resolution=CLIENT_PREFIXED)
        return ToolIdentity(raw=value, canonical=value, server=None, tool=None, resolution=UNKNOWN)

    if "." in value:
        server, tool = value.split(".", 1)
        return _identify(server, tool, raw=value, resolution=QUALIFIED)

    if hint:
        return _identify(hint, value, raw=value, resolution=SERVER_HINT)

    owners = SERVERS_BY_BARE_TOOL.get(value, ())
    if len(owners) == 1:
        # The bare name the agent used belongs to exactly one server, so this is
        # a spelling fix rather than a guess about intent.
        return _identify(owners[0], value, raw=value, resolution=INFERRED_UNIQUE)
    if len(owners) > 1:
        return ToolIdentity(
            raw=value, canonical=value, server=None, tool=value, resolution=AMBIGUOUS
        )
    return ToolIdentity(raw=value, canonical=value, server=None, tool=None, resolution=UNKNOWN)


def _identify(server: str, tool: str, *, raw: str, resolution: str) -> ToolIdentity:
    canonical = f"{server}.{tool}"
    if tool in MCP_TOOL_CATALOG.get(server, frozenset()):
        return ToolIdentity(
            raw=raw, canonical=canonical, server=server, tool=tool, resolution=resolution
        )
    # A well-formed but unlisted name is still recorded under the name the agent
    # used; the platform must not silently accept an unknown tool as known.
    return ToolIdentity(
        raw=raw, canonical=canonical, server=None, tool=tool, resolution=UNKNOWN
    )


def is_catalogued_tool(raw: Any, server_hint: Any = None) -> bool:
    return resolve_tool_identity(raw, server_hint).known
