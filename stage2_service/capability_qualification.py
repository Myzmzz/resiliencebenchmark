"""Publish basic Harness capabilities from verified native channel evidence.

This does not grant D0 fault qualification or D7/D8 substitution capabilities.
BladeAI basic channel evidence is retained but cannot replace its WP8 full chain.
"""
from __future__ import annotations

from collections.abc import Sequence
import json
import os
from pathlib import Path
import stat
import tempfile
from typing import Any
import yaml

from .capability_preflight import CAPABILITY_QUALIFICATION_SCHEMA
from .contracts import HarnessKind
from .gateway_evidence import read_gateway_artifact
from .gateway_config import GatewayConfigSnapshot
from .harness_adapters.base import HarnessCapability, normalize_tool_name

BASE_QUALIFICATION_TYPE = "BASE_CHANNEL_QUALIFICATION"
BASE_CHECKS = frozenset({
    "mcp_read_verified", "confirmation_roundtrip_verified", "consult_roundtrip_verified",
    "notice_ack_verified", "result_submission_verified", "gateway_evidence_verified",
    "tool_evidence_verified",
})
MAX_EVIDENCE_BYTES = 16 * 1024 * 1024
BASE_SERVERS = frozenset({"k8s_ro", "telemetry_ro", "source_ro", "chaos_control", "harness_channel"})
REQUIRED_TOOLS = frozenset({"harness_channel.harness_confirm", "harness_channel.harness_consult",
                            "harness_channel.harness_poll_notices", "harness_channel.harness_submit_result"})
MUTATIONS = frozenset({"chaos_control.chaos_create_experiment", "chaos_control.chaos_destroy_experiment",
                       "chaos_mesh_control.chaos_mesh_create_experiment", "chaos_mesh_control.chaos_mesh_destroy_experiment"})


def _no_links(path: Path) -> Path:
    """Reject linked evidence paths before resolving away that information."""
    candidate = path.absolute()
    if any(part.is_symlink() for part in (candidate, *candidate.parents)):
        raise ValueError("qualification paths must not contain symbolic links")
    return candidate


def _read(path: Path) -> str:
    path = _no_links(path)
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o022 or info.st_size > MAX_EVIDENCE_BYTES:
                raise ValueError("qualification evidence is not a bounded protected regular file")
            raw = handle.read(MAX_EVIDENCE_BYTES + 1)
        if len(raw) > MAX_EVIDENCE_BYTES:
            raise ValueError("qualification evidence exceeds its byte limit")
        return raw.decode("utf-8")
    except (OSError, UnicodeError) as error:
        raise ValueError("qualification evidence cannot be read") from error


def _artifact(record: dict[str, Any], root: Path, name: str) -> Path:
    refs = record.get("artifact_refs")
    if not isinstance(refs, list) or not all(isinstance(ref, str) for ref in refs):
        raise ValueError("native artifact references are missing")
    matches = [Path(ref) for ref in refs if Path(ref).name == name]
    if len(matches) != 1:
        raise ValueError(f"exactly one {name} artifact is required")
    ref = matches[0]
    if ref.is_absolute() or ".." in ref.parts:
        raise ValueError("native artifact reference escaped its root")
    path = _no_links(root / ref)
    path.resolve().relative_to(root.resolve())
    return path


def _native_tool_modes(path: Path, record: dict[str, Any]) -> tuple[bool, bool]:
    """Verify basic MCP coverage and derive native delivery mode from those tools."""
    catalog = yaml.safe_load((Path(__file__).resolve().parents[1] / "harness/mcp-tools.yaml").read_text())
    permitted = {f"{server}.{tool}" for server in BASE_SERVERS
                 for tool in catalog["tools"][server]["allowed_operations"]} - MUTATIONS
    calls: dict[tuple[str, str], dict[str, Any]] = {}
    completed: set[tuple[str, str]] = set()
    server_results: dict[str, dict[str, str]] = {}
    native_tools: set[str] = set()
    modes: set[bool] = set()
    for line in _read(path).splitlines():
        try:
            row = json.loads(line)
        except ValueError as error:
            raise ValueError("canonical evidence is not valid JSONL") from error
        if not isinstance(row, dict) or row.get("source") not in {"native", "mcp_server"}:
            continue
        kind = row.get("event_type")
        if kind not in {"ToolCall", "ToolResult"}:
            continue
        call_id = row.get("call_id")
        if not isinstance(call_id, str) or not call_id or type(row.get("replayed")) is not bool:
            raise ValueError("native tool identity or delivery mode is missing")
        key = (row["source"], call_id)
        if kind == "ToolCall":
            tool = normalize_tool_name(row.get("tool"))
            if key in calls or tool is None:
                raise ValueError("native tool call identity is ambiguous")
            if tool in MUTATIONS:
                raise ValueError("base channel evidence contains a mutation attempt")
            calls[key] = {**row, "tool": tool}
        else:
            call = calls.get(key)
            if call is None or key in completed or call["replayed"] != row["replayed"]:
                raise ValueError("native tool result does not match one prior call")
            completed.add(key)
            tool = call["tool"]
            if row["source"] == "mcp_server":
                if tool not in permitted:
                    raise ValueError("base channel evidence contains an unknown or extension MCP tool")
                server_results[call_id] = {"tool": tool, "status": row.get("status", "")}
            elif tool in permitted and row.get("status") == "completed":
                modes.add(row["replayed"])
                native_tools.add(tool)
    if set(calls) != completed:
        raise ValueError("canonical tool evidence has unclosed calls")
    exchanges = record.get("ordered_exchanges")
    if not isinstance(exchanges, list) or len(exchanges) != len(server_results):
        raise ValueError("record exchanges do not match canonical MCP evidence")
    seen: set[str] = set()
    for exchange in exchanges:
        if not isinstance(exchange, dict):
            raise ValueError("invalid recorded exchange")
        call_id = exchange.get("call_id")
        if not isinstance(call_id, str) or call_id in seen or server_results.get(call_id) != {
                "tool": exchange.get("tool"), "status": exchange.get("status")}:
            raise ValueError("record exchange identity does not match canonical MCP evidence")
        seen.add(call_id)
    server_tools = {value["tool"] for value in server_results.values() if value["status"] == "completed"}
    for observed in (server_tools, native_tools):
        if (not REQUIRED_TOOLS <= observed or not any(tool.startswith("k8s_ro.") for tool in observed)
                or not any(tool.startswith("telemetry_ro.") for tool in observed)):
            raise ValueError("basic tool coverage is missing from actual native or MCP evidence")
    if not modes:
        raise ValueError("native tool result evidence is missing")
    return False in modes, True in modes


def _entry(path: Path, artifact_root: Path, gateway: GatewayConfigSnapshot) -> tuple[str, dict[str, Any]]:
    try:
        record = json.loads(_read(path))
    except ValueError as error:
        raise ValueError("invalid channel qualification record") from error
    if not isinstance(record, dict):
        raise ValueError("channel qualification record must be an object")
    if (record.get("schema_version") != "stage2-channel-qualification.v1"
            or record.get("qualification_type") != BASE_QUALIFICATION_TYPE
            or record.get("qualification_profile") != BASE_QUALIFICATION_TYPE):
        raise ValueError("a base channel qualification record is required")
    checks = record.get("base_checks")
    if (record.get("passed") is not True or record.get("status") != "passed"
            or record.get("harness_report_status") != "completed"
            or record.get("failure_reasons") != [] or record.get("cleanup_errors") != []
            or not isinstance(checks, dict) or any(checks.get(key) is not True for key in BASE_CHECKS)):
        raise ValueError("basic channel qualification did not pass every required check")
    try:
        harness = HarnessKind(record.get("harness"))
    except ValueError as error:
        raise ValueError("unknown qualified Harness") from error
    model = record.get("model")
    trial = record.get("trial_id")
    version = record.get("gateway_config_sha256")
    route = record.get("gateway_route")
    proof = record.get("gateway_sidecar_evidence")
    if (not isinstance(model, str) or not model or not isinstance(trial, str) or not trial
            or not isinstance(version, str) or len(version) != 64
            or any(character not in "0123456789abcdef" for character in version)
            or not isinstance(route, dict) or route.get("model_alias") != model
            or not isinstance(proof, dict) or proof.get("verified") is not True):
        raise ValueError("gateway qualification identity is missing")
    ids = proof.get("request_ids")
    if (not isinstance(ids, list) or not ids or not all(isinstance(item, str) and item for item in ids)
            or len(ids) != len(set(ids)) or proof.get("artifact_ref") != "gateway-requests.json"):
        raise ValueError("gateway request identities are missing or ambiguous")
    if version != gateway.config_sha256 or route != gateway.route(model):
        raise ValueError("qualification uses a different gateway configuration or route")
    receipt_path = _artifact(record, artifact_root, "gateway-requests.json")
    canonical_path = _artifact(record, artifact_root, "canonical-events.jsonl")
    if receipt_path.parent != canonical_path.parent:
        raise ValueError("native and gateway evidence must belong to the same archive")
    receipts = read_gateway_artifact(
        receipt_path, trial_id=trial,
        harness=harness.value, model_alias=model, config_sha256=version, request_ids=set(ids),
    )
    if receipts is None:
        raise ValueError("gateway receipt artifact does not verify this qualification")
    streamed, replayed = _native_tool_modes(canonical_path, record)
    # A basic channel run cannot establish BladeAI's shim/approval execution path.
    # Keep that missing gate explicit rather than quietly granting stream mode.
    qualified = harness != HarnessKind.BLADEAI
    descriptor = HarnessCapability(
        kind=harness,
        execution_model=("stream" if streamed else "post_hoc") if qualified else "controller_driven",
        streams_tool_results=streamed, post_hoc_trace=replayed,
        supports_resume=False, supports_mid_turn_feedback=True,
        feedback_channels=("in_band_mcp",), code_execution="none",
        qualification_passed=qualified,
        probe={"qualification_profile": BASE_QUALIFICATION_TYPE, "channel_trial_id": trial,
               "model_alias": model, "gateway_config_sha256": version,
               "gateway_request_ids": ids, "base_checks": checks},
    )
    return harness.value, {
        "qualification": {"status": "passed" if qualified else "platform_integration_incomplete",
                          "evidence_ref": str(path.resolve()), "qualification_type": BASE_QUALIFICATION_TYPE,
                          "reason": None if qualified else "bladeai_full_chain_qualification_required"},
        "capability": descriptor.model_dump(mode="json"),
    }


def publish_capabilities(record_files: Sequence[Path], *, artifact_root: Path, output: Path,
                         gateway: GatewayConfigSnapshot) -> Path:
    """Atomically publish exactly the supplied evidence set, never inferred entries.

    To retain previously qualified Harnesses, explicitly include their records.
    Invalid input leaves any existing publication unchanged. No output is D0 proof.
    """
    root = _no_links(artifact_root)
    if not root.is_dir() or not record_files:
        raise ValueError("an existing artifact root and qualification records are required")
    entries: dict[str, dict[str, Any]] = {}
    for path in record_files:
        name, entry = _entry(Path(path), root, gateway)
        if name in entries:
            raise ValueError("duplicate Harness qualification records")
        entries[name] = entry
    payload = {"schema_version": CAPABILITY_QUALIFICATION_SCHEMA, "harnesses": entries}
    destination = _no_links(output)
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if destination.parent.stat().st_mode & 0o022:
        raise ValueError("capability publication directory must not be group/world writable")
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=destination.parent,
                                         prefix=".capabilities-", delete=False) as handle:
            temporary = Path(handle.name)
            os.fchmod(handle.fileno(), 0o600)
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    return destination
