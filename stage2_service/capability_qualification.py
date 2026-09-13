"""Publish Harness capabilities from verified native channel evidence.

A base record grants the basic in-band channel.  A verified WP11 substitution
record for the same non-BladeAI Harness additionally grants
``code_execution=platform_sandbox``: the Harness ran Agent code through the
platform ``code_sandbox`` MCP, which D7/D8 require.  Nothing here grants D0
fault qualification.  BladeAI always keeps ``code_execution=none``; its basic
channel evidence is retained but never grants it a sandboxed code path.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
import json
import os
from pathlib import Path
import stat
import tempfile
from typing import Any
import jsonschema
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
# Equals channel_qualification.CHANNEL_QUALIFICATION_MODE (a test pins this).  It
# is literal so that publishing base records alone does not import that module.
SUBSTITUTION_QUALIFICATION_TYPE = "CHANNEL_QUALIFICATION"
MAX_EVIDENCE_BYTES = 16 * 1024 * 1024
# Harnesses whose execution path a base channel run cannot establish, and which
# therefore need a separate full-chain proof before they count as qualified.
#
# BladeAI used to be the only member.  The reason was specific and is now gone:
# the platform drove it by replacing private functions inside its process, so a
# base channel run exercised our shim rather than the Agent's real execution
# path, and granting stream mode on that evidence would have overstated what we
# had verified.
#
# Driven as a black box (2026-09-13) there is no shim.  BladeAI answers the same
# published HTTP/SSE interface codex answers over stdout, and every BASE_CHECK
# below is platform-side evidence that does not depend on anything the Agent
# writes about itself:
#   * mcp_read / confirmation / consult / notice / result checks come from the
#     MCP gateway's own record of the calls it served;
#   * gateway_evidence comes from the model gateway's request log;
#   * tool_evidence is recomputed from canonical-events, which the driver lands
#     itself (harness/bladeai_http/client.py).
# So the base record now establishes BladeAI's real execution path, and the
# exclusion no longer has a basis.
#
# The set is kept rather than deleted: "this Harness needs more than base
# evidence" is a judgement worth being able to state in one place, instead of
# rediscovering it as a scattered ``if harness is ...`` later.
HARNESSES_NEEDING_FULL_CHAIN_PROOF: frozenset[HarnessKind] = frozenset()

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


@dataclass(frozen=True)
class _CanonicalToolEvidence:
    """Closed tool pairs from one Trial archive, bound to the record's exchanges."""

    server_results: dict[str, dict[str, str]]
    server_payloads: dict[str, Any]
    native_tools: frozenset[str]
    native_modes: frozenset[bool]


def _canonical_tool_evidence(path: Path, record: dict[str, Any], servers: frozenset[str],
                             label: str) -> _CanonicalToolEvidence:
    """Pair canonical native/MCP tool rows and bind the MCP rows to the record.

    Only non-mutating catalog operations of ``servers`` count as evidence.  A
    mutation attempt, an unknown MCP tool, an unclosed call or a recorded
    exchange that differs from the archive rejects the record.
    """
    catalog = yaml.safe_load((Path(__file__).resolve().parents[1] / "harness/mcp-tools.yaml").read_text())
    permitted = {f"{server}.{tool}" for server in servers
                 for tool in catalog["tools"][server]["allowed_operations"]} - MUTATIONS
    calls: dict[tuple[str, str], dict[str, Any]] = {}
    completed: set[tuple[str, str]] = set()
    server_results: dict[str, dict[str, str]] = {}
    server_payloads: dict[str, Any] = {}
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
                raise ValueError(f"{label} channel evidence contains a mutation attempt")
            calls[key] = {**row, "tool": tool}
        else:
            call = calls.get(key)
            if call is None or key in completed or call["replayed"] != row["replayed"]:
                raise ValueError("native tool result does not match one prior call")
            completed.add(key)
            tool = call["tool"]
            if row["source"] == "mcp_server":
                if tool not in permitted:
                    raise ValueError(f"{label} channel evidence contains an unknown or extension MCP tool")
                server_results[call_id] = {"tool": tool, "status": row.get("status", "")}
                server_payloads[call_id] = row.get("payload")
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
    return _CanonicalToolEvidence(server_results=server_results, server_payloads=server_payloads,
                                  native_tools=frozenset(native_tools), native_modes=frozenset(modes))


def _native_tool_modes(path: Path, record: dict[str, Any]) -> tuple[bool, bool]:
    """Verify basic MCP coverage and derive native delivery mode from those tools."""
    evidence = _canonical_tool_evidence(path, record, BASE_SERVERS, "base")
    server_tools = {value["tool"] for value in evidence.server_results.values() if value["status"] == "completed"}
    for observed in (server_tools, evidence.native_tools):
        if (not REQUIRED_TOOLS <= observed or not any(tool.startswith("k8s_ro.") for tool in observed)
                or not any(tool.startswith("telemetry_ro.") for tool in observed)):
            raise ValueError("basic tool coverage is missing from actual native or MCP evidence")
    if not evidence.native_modes:
        raise ValueError("native tool result evidence is missing")
    return False in evidence.native_modes, True in evidence.native_modes


def _read_record(path: Path) -> dict[str, Any]:
    try:
        record = json.loads(_read(path))
    except ValueError as error:
        raise ValueError("invalid channel qualification record") from error
    if not isinstance(record, dict):
        raise ValueError("channel qualification record must be an object")
    return record


@dataclass(frozen=True)
class _GatewayIdentity:
    """Model route of one qualification Trial, re-verified against its receipts."""

    model: str
    trial_id: str
    config_sha256: str
    request_ids: list[str]
    canonical_path: Path


def _verified_gateway_identity(record: dict[str, Any], harness: HarnessKind, artifact_root: Path,
                               gateway: GatewayConfigSnapshot) -> _GatewayIdentity:
    """Bind a record to the current gateway route and to one archive's receipts."""
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
    return _GatewayIdentity(model=model, trial_id=trial, config_sha256=version, request_ids=ids,
                            canonical_path=canonical_path)


def _entry(path: Path, record: dict[str, Any], artifact_root: Path,
           gateway: GatewayConfigSnapshot) -> tuple[str, dict[str, Any]]:
    """Verify one base channel record into its published entry."""
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
    identity = _verified_gateway_identity(record, harness, artifact_root, gateway)
    streamed, replayed = _native_tool_modes(identity.canonical_path, record)
    qualified = harness not in HARNESSES_NEEDING_FULL_CHAIN_PROOF
    descriptor = HarnessCapability(
        kind=harness,
        execution_model=("stream" if streamed else "post_hoc") if qualified else "controller_driven",
        streams_tool_results=streamed, post_hoc_trace=replayed,
        supports_resume=False, supports_mid_turn_feedback=True,
        # Base evidence never proves sandboxed code execution; only a verified
        # substitution record for the same Harness upgrades this field.
        feedback_channels=("in_band_mcp",), code_execution="none",
        qualification_passed=qualified,
        probe={"qualification_profile": BASE_QUALIFICATION_TYPE, "channel_trial_id": identity.trial_id,
               "model_alias": identity.model, "gateway_config_sha256": identity.config_sha256,
               "gateway_request_ids": identity.request_ids, "base_checks": checks},
    )
    return harness.value, {
        "qualification": {"status": "passed" if qualified else "platform_integration_incomplete",
                          "evidence_ref": str(path.resolve()), "qualification_type": BASE_QUALIFICATION_TYPE,
                          "reason": None if qualified else "bladeai_full_chain_qualification_required"},
        "capability": descriptor.model_dump(mode="json"),
    }


def _substitution_proof(path: Path, record: dict[str, Any], artifact_root: Path,
                        gateway: GatewayConfigSnapshot) -> tuple[str, dict[str, Any]]:
    """Verify one WP11 substitution record as proof of platform sandbox execution.

    The record must carry the evaluator's positive checks, not merely an empty
    failure list, so records from evaluators that predate those checks are
    refused.  Its archive must show the recorded ``code_sandbox.run_python``
    call succeeding at the Controller MCP boundary and in the Harness's own
    tool stream.  Any gap raises; nothing is inferred.
    """
    # Imported here: the evaluator module composes the Stage-2 runtime, which a
    # publication of base records alone does not need.
    from .channel_qualification import (
        CHANNEL_QUALIFICATION_MODE,
        EXPECTED_HINT_BODY,
        SUBSTITUTION_CHECKS,
        SUBSTITUTION_MCP_SERVERS,
        TELEMETRY_DENIAL_BODY,
    )

    if (record.get("schema_version") != "stage2-channel-qualification.v1"
            or record.get("qualification_type") != CHANNEL_QUALIFICATION_MODE
            or record.get("qualification_profile") != CHANNEL_QUALIFICATION_MODE):
        raise ValueError("a substitution channel qualification record is required")
    checks = record.get("substitution_checks")
    if (record.get("passed") is not True or record.get("status") != "passed"
            or record.get("harness_report_status") != "completed"
            or record.get("failure_reasons") != [] or record.get("cleanup_errors") != []
            or record.get("scored_as_d7") is not False
            or record.get("telemetry_denial_body") != TELEMETRY_DENIAL_BODY
            or record.get("hint_body") != EXPECTED_HINT_BODY
            or not isinstance(checks, dict)
            or any(checks.get(key) is not True for key in SUBSTITUTION_CHECKS)):
        raise ValueError("substitution channel qualification did not pass every required check")
    try:
        harness = HarnessKind(record.get("harness"))
    except ValueError as error:
        raise ValueError("unknown qualified Harness") from error
    if harness is HarnessKind.BLADEAI:
        # ``code_execution="none"`` is BladeAI's settled grade and survives the
        # black-box migration unchanged.  It runs its own built-in tooling
        # rather than code the platform sandboxes, so no substitution evidence
        # can establish a sandboxed code path for it.
        raise ValueError("BladeAI code execution cannot be qualified by channel substitution evidence")
    identity = _verified_gateway_identity(record, harness, artifact_root, gateway)
    evidence = _canonical_tool_evidence(identity.canonical_path, record, SUBSTITUTION_MCP_SERVERS,
                                        "substitution")
    return harness.value, {
        "qualification_profile": CHANNEL_QUALIFICATION_MODE,
        "evidence_ref": str(path.resolve()),
        "channel_trial_id": identity.trial_id,
        "model_alias": identity.model,
        "gateway_config_sha256": identity.config_sha256,
        "gateway_request_ids": identity.request_ids,
        "substitution_checks": checks,
        "sandbox_run": _verified_sandbox_run(record, evidence),
    }


def _is_zero_exit(value: Any) -> bool:
    # ``False == 0`` in Python; a JSON boolean must not pass as an exit code.
    return type(value) is int and value == 0


def _verified_sandbox_run(record: dict[str, Any], evidence: _CanonicalToolEvidence) -> dict[str, Any]:
    """Bind the recorded ``SANDBOX_RUN`` ledger event to a successful run_python call.

    Only the evaluator saw the Controller ledger, so the record names the ledger
    event and the call it belongs to: the first run_python exchange, which is
    the one the evaluator checked.  The event must fall inside that call's
    window, and the record, the Controller MCP row and the Harness stream must
    all show a completed, untruncated, zero-exit execution.
    """
    observed = record.get("observed_capability_evidence")
    run = observed.get("sandbox_run") if isinstance(observed, dict) else None
    sandbox = next((item for item in record["ordered_exchanges"]
                    if item.get("tool") == "code_sandbox.run_python"), None)
    if not isinstance(run, dict) or sandbox is None or run.get("call_id") != sandbox.get("call_id"):
        raise ValueError("platform sandbox run evidence is missing or belongs to another call")
    window = (sandbox.get("call_sequence"), run.get("ledger_sequence"), sandbox.get("result_sequence"))
    if (not all(type(value) is int for value in window) or not window[0] < window[1] < window[2]
            or (run.get("call_sequence"), run.get("result_sequence")) != (window[0], window[2])):
        raise ValueError("platform sandbox run is not inside its run_python call")
    if (run.get("status") != "completed" or not _is_zero_exit(run.get("exit_code"))
            or run.get("truncated") is not False):
        raise ValueError("platform sandbox run did not complete successfully")
    if sandbox.get("status") != "completed":
        raise ValueError("run_python did not complete at the Controller MCP boundary")
    for payload in (sandbox.get("payload"), evidence.server_payloads.get(sandbox["call_id"])):
        if (not isinstance(payload, dict) or payload.get("ok") is not True
                or not _is_zero_exit(payload.get("exit_code")) or payload.get("truncated") is not False):
            raise ValueError("run_python result does not show a successful sandbox execution")
    if "code_sandbox.run_python" not in evidence.native_tools:
        raise ValueError("the Harness stream does not show its run_python call completing")
    return dict(run)


def _with_platform_sandbox(entry: dict[str, Any], proof: dict[str, Any]) -> dict[str, Any]:
    """Add ``platform_sandbox`` to the passed base entry of the same Harness."""
    capability = HarnessCapability.model_validate({
        **entry["capability"],
        "code_execution": "platform_sandbox",
        "probe": {**entry["capability"]["probe"], "substitution_qualification": proof},
    })
    return {
        "qualification": {**entry["qualification"], "substitution_evidence_ref": proof["evidence_ref"]},
        "capability": capability.model_dump(mode="json"),
    }


def publish_capabilities(record_files: Sequence[Path], *, artifact_root: Path, output: Path,
                         gateway: GatewayConfigSnapshot) -> Path:
    """Atomically publish exactly the supplied evidence set, never inferred entries.

    Each Harness needs one base channel record.  An optional WP11
    substitution record for the same non-BladeAI Harness adds
    ``code_execution=platform_sandbox``; every other Harness is published with
    ``none``.  To retain previously qualified Harnesses, explicitly include
    their records.  Invalid input leaves any existing publication unchanged.
    No output is D0 proof.
    """
    root = _no_links(artifact_root)
    if not root.is_dir() or not record_files:
        raise ValueError("an existing artifact root and qualification records are required")
    entries: dict[str, dict[str, Any]] = {}
    sandbox_proofs: dict[str, dict[str, Any]] = {}
    for path in record_files:
        record = _read_record(Path(path))
        if record.get("qualification_type") == SUBSTITUTION_QUALIFICATION_TYPE:
            name, proof = _substitution_proof(Path(path), record, root, gateway)
            if name in sandbox_proofs:
                raise ValueError("duplicate substitution qualification records")
            sandbox_proofs[name] = proof
            continue
        name, entry = _entry(Path(path), record, root, gateway)
        if name in entries:
            raise ValueError("duplicate Harness qualification records")
        entries[name] = entry
    for name, proof in sandbox_proofs.items():
        # The sandbox grant extends a verified base channel; it never stands alone.
        base = entries.get(name)
        if base is None or base["qualification"]["status"] != "passed":
            raise ValueError("a substitution record requires a passed base record for the same Harness")
        entries[name] = _with_platform_sandbox(base, proof)
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
