"""BladeAI worker-local target-guard classification for controlled MCP tools."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import yaml


class BladeAIMcpGuardError(RuntimeError):
    """Raised when the pinned BladeAI guard patch cannot be applied safely."""


_READONLY_SERVERS = frozenset({"k8s_ro", "telemetry_ro", "source_ro", "coroot_ro"})
_PHASES = ("clarification", "phase1", "phase2", "verifier")


def build_allowed_mcp_guard_tool_names(
    mcp_manager: Any,
    policy_path: Path,
) -> frozenset[str]:
    """Return connected, declared MCP tool names that may pass as READONLY."""

    policy = _load_mcp_tool_policy(policy_path)
    connected = _connected_server_names(mcp_manager)
    actual = _actual_tool_names(mcp_manager)

    try:
        from chaos_agent.mcp.adapter import _safe_tool_name
    except ImportError as exc:
        raise BladeAIMcpGuardError(
            "BladeAI MCP adapter _safe_tool_name is unavailable"
        ) from exc
    if not callable(_safe_tool_name):
        raise BladeAIMcpGuardError("BladeAI MCP adapter _safe_tool_name is invalid")

    allowed: set[str] = set()
    for server_name, spec in policy.items():
        if server_name not in connected:
            continue
        if not _server_may_use_readonly_sentinel(server_name, spec):
            continue
        operations = spec.get("allowed_operations")
        if not isinstance(operations, list):
            raise BladeAIMcpGuardError(
                f"MCP tool policy has invalid allowed_operations for {server_name}"
            )
        for operation in operations:
            if not isinstance(operation, str) or not operation:
                raise BladeAIMcpGuardError(
                    f"MCP tool policy has invalid operation for {server_name}"
                )
            candidate = _safe_tool_name(server_name, operation)
            if candidate in actual:
                allowed.add(candidate)
    return frozenset(allowed)


class BladeAIMcpGuardPatch:
    """Temporarily classify selected connected MCP tools as READONLY."""

    def __init__(self, allowed_tool_names: Iterable[str]) -> None:
        self._allowed_tool_names = frozenset(str(name) for name in allowed_tool_names)
        self._originals: list[tuple[Any, str, Any]] = []
        self._installed = False

    def install(self) -> None:
        if self._installed:
            raise BladeAIMcpGuardError("BladeAI MCP guard patch is already installed")

        try:
            import chaos_agent.agent.nodes.phase1_screener as phase1_screener
            import chaos_agent.agent.nodes.tool_screener as tool_screener
            import chaos_agent.agent.target_guard as target_guard
            import chaos_agent.agent.target_guard.classifier as classifier
            from chaos_agent.agent.target_guard.types import (
                ConfidenceLevel,
                EffectiveTarget,
            )
        except ImportError as exc:
            raise BladeAIMcpGuardError(
                "BladeAI target_guard modules are unavailable"
            ) from exc

        self._require_attrs(
            (classifier, "infer_effective_target"),
            (classifier, "SCOPE_READONLY"),
            (target_guard, "infer_effective_target"),
            (phase1_screener, "infer_effective_target"),
            (tool_screener, "infer_effective_target"),
        )
        original = classifier.infer_effective_target
        readonly_scope = classifier.SCOPE_READONLY
        allowed_tool_names = self._allowed_tool_names

        def infer_effective_target(
            tool_name: str,
            tool_args: dict[str, Any] | str | list[str] | None,
            *,
            skill_script_allowed: bool = False,
        ) -> Any:
            if tool_name in allowed_tool_names:
                return EffectiveTarget(
                    scope=readonly_scope,
                    namespace="",
                    confidence=ConfidenceLevel.HIGH,
                    raw_command=f"{tool_name}({tool_args!r})",
                )
            return original(
                tool_name,
                tool_args,
                skill_script_allowed=skill_script_allowed,
            )

        self._replace(classifier, "infer_effective_target", infer_effective_target)
        self._replace(target_guard, "infer_effective_target", infer_effective_target)
        self._replace(
            phase1_screener,
            "infer_effective_target",
            infer_effective_target,
        )
        self._replace(tool_screener, "infer_effective_target", infer_effective_target)
        self._installed = True

    def restore(self) -> None:
        for module, name, original in reversed(self._originals):
            setattr(module, name, original)
        self._originals = []
        self._installed = False

    def _replace(self, module: Any, name: str, value: Any) -> None:
        if not hasattr(module, name):
            raise BladeAIMcpGuardError(
                f"BladeAI guard symbol missing: {module.__name__}.{name}"
            )
        self._originals.append((module, name, getattr(module, name)))
        setattr(module, name, value)

    def _require_attrs(self, *targets: tuple[Any, str]) -> None:
        missing = [
            f"{module.__name__}.{name}"
            for module, name in targets
            if not hasattr(module, name)
        ]
        if missing:
            raise BladeAIMcpGuardError(
                "BladeAI guard symbols missing: " + ", ".join(missing)
            )


def _load_mcp_tool_policy(policy_path: Path) -> Mapping[str, Mapping[str, Any]]:
    document = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
    if not isinstance(document, Mapping):
        raise BladeAIMcpGuardError("MCP tool policy must be a mapping")
    tools = document.get("tools")
    if not isinstance(tools, Mapping):
        raise BladeAIMcpGuardError("MCP tool policy must define tools")
    invalid = [
        name
        for name, spec in tools.items()
        if not isinstance(name, str) or not isinstance(spec, Mapping)
    ]
    if invalid:
        raise BladeAIMcpGuardError("MCP tool policy contains invalid tool entries")
    return tools


def _connected_server_names(mcp_manager: Any) -> frozenset[str]:
    if not hasattr(mcp_manager, "_clients"):
        raise BladeAIMcpGuardError("BladeAI McpManager._clients is missing")
    clients = getattr(mcp_manager, "_clients")
    names = [
        name
        for client in clients
        if isinstance((name := getattr(client, "name", None)), str)
    ]
    return frozenset(names)


def _actual_tool_names(mcp_manager: Any) -> frozenset[str]:
    if not hasattr(mcp_manager, "tools_for_phase"):
        raise BladeAIMcpGuardError("BladeAI McpManager.tools_for_phase is missing")
    names: set[str] = set()
    for phase in _PHASES:
        for tool in mcp_manager.tools_for_phase(phase):
            name = getattr(tool, "name", None)
            if isinstance(name, str) and name:
                names.add(name)
    return frozenset(names)


def _server_may_use_readonly_sentinel(
    server_name: str,
    spec: Mapping[str, Any],
) -> bool:
    mode = spec.get("mode")
    if server_name in _READONLY_SERVERS:
        return mode == "read_only"
    if server_name == "harness_channel":
        return mode == "controlled_write"
    if server_name == "code_sandbox":
        return mode == "controlled_write"
    return False
