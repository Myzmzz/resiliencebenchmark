"""Derive observable Agent lifecycle stages from native tool traces."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .common import read_jsonl


TARGET_TOOLS = {
    "k8s_get_resource",
    "k8s_list_resources",
    "kubectl_read",
    "blade_query_k8s",
}
INJECTION_TOOLS = {"chaos_create_experiment", "blade_create"}
EFFECT_CHECK_TOOLS = {
    "chaos_get_experiment",
    "chaos_inventory_run",
    "blade_status",
    "blade_query_k8s",
    "k8s_get_resource",
    "k8s_list_resources",
    "telemetry_query",
}
RECOVERY_TOOLS = {"chaos_destroy_experiment", "blade_destroy"}
RECOVERY_CHECK_TOOLS = {
    "chaos_recovery_status",
    "chaos_get_experiment",
    "chaos_inventory_run",
    "blade_status",
    "blade_query_k8s",
    "k8s_get_resource",
    "k8s_list_resources",
    "telemetry_query",
}


def _normalized(value: str) -> str:
    text = value.strip().lower()
    if "__" in text:
        text = text.rsplit("__", 1)[-1]
    if "." in text:
        text = text.rsplit(".", 1)[-1]
    return text


def _tool_names(value: Any) -> list[str]:
    names: list[str] = []

    def visit(item: Any) -> None:
        if isinstance(item, dict):
            marker = str(item.get("type") or item.get("kind") or "").lower()
            for key in ("tool", "tool_name"):
                if isinstance(item.get(key), str):
                    names.append(_normalized(str(item[key])))
            tool_marker = marker in {
                "tool_use",
                "tool/call",
                "tool-call-chunks",
                "mcp_tool_call",
                "tool_call",
                "tool_start",
                "item.started",
                "item.completed",
            } or ("tool" in marker and ("call" in marker or "use" in marker))
            if tool_marker:
                for source in (item, item.get("data"), item.get("payload")):
                    if isinstance(source, dict) and isinstance(source.get("name"), str):
                        names.append(_normalized(str(source["name"])))
            for nested in item.values():
                if isinstance(nested, (dict, list)):
                    visit(nested)
        elif isinstance(item, list):
            for nested in item:
                visit(nested)

    visit(value)
    return names


def _load_ref(trial_dir: Path, value: dict[str, Any]) -> Any:
    ref = value.get("payload_ref")
    if not isinstance(ref, str):
        return None
    path = trial_dir / ref
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


def derive_agent_behavior(trial_dir: Path) -> dict[str, Any]:
    tools = collect_agent_tool_records(trial_dir)
    injection = next(
        (value for value in tools if value["tool"] in INJECTION_TOOLS), None
    )
    recovery = next(
        (value for value in tools if value["tool"] in RECOVERY_TOOLS), None
    )
    target_checks = [
        value
        for value in tools
        if value["tool"] in TARGET_TOOLS
        and (not injection or value["sequence"] < injection["sequence"])
    ]
    effect_checks = [
        value
        for value in tools
        if injection
        and value["sequence"] > injection["sequence"]
        and value["tool"] in EFFECT_CHECK_TOOLS
        and (not recovery or value["sequence"] < recovery["sequence"])
    ]
    recovery_checks = [
        value
        for value in tools
        if recovery
        and value["sequence"] > recovery["sequence"]
        and value["tool"] in RECOVERY_CHECK_TOOLS
    ]
    return {
        "schema_version": "d0-agent-behavior.v1",
        "agent_target_discovered": bool(target_checks),
        "agent_target_discovered_at": target_checks[-1].get("ts")
        if target_checks
        else None,
        "agent_injection_requested": injection is not None,
        "agent_injection_requested_at": injection.get("ts") if injection else None,
        "agent_effect_check_observed": bool(effect_checks),
        "agent_effect_check_at": effect_checks[0].get("ts") if effect_checks else None,
        "agent_recovery_requested": recovery is not None,
        "agent_recovery_requested_at": recovery.get("ts") if recovery else None,
        "agent_recovery_check_observed": bool(recovery_checks),
        "agent_recovery_check_at": recovery_checks[0].get("ts")
        if recovery_checks
        else None,
        "recognized_tool_events": [
            value
            for value in tools
            if value["tool"]
            in TARGET_TOOLS
            | INJECTION_TOOLS
            | EFFECT_CHECK_TOOLS
            | RECOVERY_TOOLS
            | RECOVERY_CHECK_TOOLS
        ],
    }


def collect_agent_tool_records(trial_dir: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    records.extend(read_jsonl(trial_dir / "all-events.jsonl"))
    trace_path = trial_dir / "run-trace.json"
    if trace_path.is_file():
        try:
            trace = json.loads(trace_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            trace = {}
        records.extend(
            value for value in trace.get("events", []) if isinstance(value, dict)
        )

    tools: list[dict[str, Any]] = []
    seen = set()
    for index, record in enumerate(records):
        candidates = [record, record.get("payload"), _load_ref(trial_dir, record)]
        for candidate in candidates:
            for name in _tool_names(candidate):
                key = (index, name)
                if key in seen:
                    continue
                seen.add(key)
                tools.append(
                    {
                        "sequence": index,
                        "ts": record.get("ts"),
                        "tool": name,
                        "payload_ref": record.get("payload_ref"),
                    }
                )
    return tools
