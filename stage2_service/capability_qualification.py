"""Publish Harness capabilities from verified native channel evidence.

A base record grants the basic in-band channel.  A verified WP11 substitution
record for the same non-BladeAI Harness additionally grants
``code_execution=platform_sandbox``: the Harness ran Agent code through the
platform ``code_sandbox`` MCP, which D7/D8 require.  Nothing here grants D0
fault qualification.  BladeAI always keeps ``code_execution=none``; its basic
channel evidence is retained but cannot replace its WP8 full chain.
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
    """Verify one base or BladeAI WP8 record into its published entry."""
    if record.get("qualification_type") == "BLADEAI_WP8_FULL_CHAIN_QUALIFICATION":
        return _wp8_entry(path, record, artifact_root, gateway)
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
    # A basic channel run cannot establish BladeAI's shim/approval execution path.
    # Keep that missing gate explicit rather than quietly granting stream mode.
    qualified = harness != HarnessKind.BLADEAI
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
    # publication of base or WP8 records alone does not need.
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
        # BladeAI is promoted only by its WP8 full chain; a channel run does not
        # prove its worker's code path, so it never grants BladeAI a sandbox.
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


def evaluate_wp8_artifacts(
    artifact_refs: Sequence[str], *, artifact_root: Path, gateway: GatewayConfigSnapshot
) -> dict[str, Any]:
    """Recompute WP8 from protected production artifacts, not a checks table.

    This reads an already-finished canary run. It never launches an Agent,
    creates a canary, or grants capabilities by itself.
    """
    from .bladeai_qualification import evaluate_bladeai_full_chain
    from .contracts import HarnessReport, RecoveryResult, RuntimeTarget, TrialRuntimeContext
    from .platform_ledger import PlatformEvent

    root = _no_links(artifact_root)
    refs = {"artifact_refs": list(artifact_refs)}

    def artifact_path(
        name: str,
        *,
        record: dict[str, Any] = refs,
        optional: bool = False,
    ) -> Path | None:
        matches = [Path(ref) for ref in record.get("artifact_refs", [])
                   if isinstance(ref, str) and Path(ref).name == name]
        if optional and name == "bladeai-shim-evidence.json" and not matches:
            # A model/provider failure can happen before the first write.
            # Missing shim evidence is then a measured "no mutation" outcome,
            # not an evaluator exception.  Ambiguous or malformed references
            # remain hard failures below.
            return None
        try:
            return _artifact(record, root, name)
        except ValueError:
            raise

    def document(name: str, *, optional: bool = False) -> Any:
        path = artifact_path(name, optional=optional)
        return [] if path is None else json.loads(_read(path))

    report = HarnessReport.model_validate(document("harness-report.json"))
    recovery = RecoveryResult.model_validate(document("recovery.json"))
    runtime = TrialRuntimeContext.model_validate(document("runtime-context.json"))
    canary = document("canary-evidence.json")
    if not isinstance(canary, dict) or canary.get("trial_id") != runtime.trial_id:
        raise ValueError("canary evidence does not belong to this Trial")
    pod = canary.get("pod")
    metadata = pod.get("metadata") if isinstance(pod, dict) else None
    if (not isinstance(metadata, dict) or pod.get("kind") != "Pod"
            or (metadata.get("labels") or {}).get("resiliencebenchmark.io/qualification") != "bladeai-wp8"):
        raise ValueError("an explicitly labelled BladeAI canary Pod record is required")
    if any(not isinstance(metadata.get(key), str) or not metadata[key] for key in ("namespace", "name", "uid")):
        raise ValueError("canary Pod identity is incomplete")
    expected = RuntimeTarget(namespace=metadata["namespace"], name=metadata["name"],
                             uid=metadata["uid"], component="bladeai-canary")
    if report.final_output.get("trial_id") != runtime.trial_id:
        raise ValueError("Harness report does not belong to the runtime Trial")
    if recovery.recovery_attribution.get("trial_id") != runtime.trial_id:
        raise ValueError("recovery evidence does not belong to the runtime Trial")
    for name in ("harness-report.json", "recovery.json"):
        if _artifact(refs, root, name).parent != _artifact(refs, root, "runtime-context.json").parent:
            raise ValueError("runtime and recovery evidence must belong to one Trial directory")
    if document("bladeai-launch.json") != report.final_output.get("bladeai_launch"):
        raise ValueError("BladeAI launch facts differ from the Controller artifact")
    shim_path = artifact_path("bladeai-shim-evidence.json", optional=True)
    shim_document = [] if shim_path is None else json.loads(_read(shim_path))
    reported_shim = report.final_output.get("bladeai_shim_evidence")
    if shim_path is None:
        if reported_shim not in (None, []):
            raise ValueError("BladeAI shim receipts are claimed but the artifact is missing")
    elif shim_document != reported_shim:
        raise ValueError("BladeAI shim receipts differ from their captured artifact")

    event_rows = report.final_output.get("platform_events")
    if not isinstance(event_rows, list) or not event_rows:
        raise ValueError("Controller platform events are missing")
    if any(not isinstance(row, dict) or not isinstance(row.get("payload"), dict)
           or not isinstance(row.get("event_type"), str) for row in event_rows):
        raise ValueError("Controller platform event structure is invalid")
    try:
        events = [PlatformEvent(**row) for row in event_rows]
    except TypeError as error:
        raise ValueError("Controller platform event fields are invalid") from error
    canonical_path = _artifact(refs, root, "canonical-events.jsonl")
    canonical = [json.loads(line) for line in _read(canonical_path).splitlines() if line.strip()]
    native_stream_verified = _verify_wp8_canonical_events(canonical, event_rows)
    native_parent = canonical_path.parent
    for name in ("gateway-requests.json", "bladeai-launch.json", "bladeai-shim-evidence.json",
                 "harness-report.json", "runtime-context.json", "recovery.json", "canary-evidence.json"):
        path = artifact_path(name, optional=name == "bladeai-shim-evidence.json")
        if path is not None and path.parent != native_parent:
            raise ValueError("all BladeAI WP8 evidence must belong to one Trial archive")
    reported_refs = {"artifact_refs": list(report.artifact_refs)}
    for name in ("canonical-events.jsonl", "gateway-requests.json", "bladeai-launch.json", "bladeai-shim-evidence.json"):
        actual = artifact_path(name, optional=name == "bladeai-shim-evidence.json")
        reported = artifact_path(
            name,
            record=reported_refs,
            optional=name == "bladeai-shim-evidence.json",
        )
        if (actual is None) != (reported is None) or (
            actual is not None and reported is not None and actual != reported
        ):
            raise ValueError("WP8 references differ from the actual Harness archive references")
    model = report.final_output.get("model_alias")
    route = report.final_output.get("gateway_route")
    version = report.final_output.get("gateway_config_sha256")
    ids = report.final_output.get("gateway_request_ids")
    if (not isinstance(model, str) or not model or version != gateway.config_sha256
            or route != gateway.route(model) or report.final_output.get("gateway_evidence_verified") is not True
            or not isinstance(ids, list) or not ids or not all(isinstance(item, str) and item for item in ids)
            or len(ids) != len(set(ids))):
        raise ValueError("WP8 model route evidence is missing or does not match the current gateway")
    receipt_path = _artifact(refs, root, "gateway-requests.json")
    if read_gateway_artifact(receipt_path, trial_id=runtime.trial_id, harness="bladeai",
                             model_alias=model, config_sha256=version, request_ids=set(ids)) is None:
        raise ValueError("WP8 gateway receipt does not verify this Trial")
    evaluated = evaluate_bladeai_full_chain(
        trial_id=runtime.trial_id, model=model, report=report, recovery=recovery,
        runtime_target=runtime.target, events=events, expected_canary=expected,
        expected_cleanup_handle=runtime.cleanup_handle,
    )
    # A provider/SDK failure can happen before BladeAI emits its first native
    # tool result.  That is a valid failed qualification outcome, not a
    # corrupted archive.  Preserve the strict live-stream requirement for any
    # record that otherwise claims a passing WP8 chain.
    if evaluated.get("passed") is True and not native_stream_verified:
        raise ValueError("WP8 terminal success requires a live native BladeAI ToolResult")
    evaluated["evidence"] = {
        **dict(evaluated.get("evidence") or {}),
        "canonical_stream": {
            "controller_ledger_verified": True,
            "live_native_tool_result": native_stream_verified,
        },
    }
    if evaluated.get("passed") is True:
        result = report.final_output.get("agent_result")
        schema_path = Path(__file__).resolve().parents[1] / "harness/schemas/agent-result.schema.json"
        try:
            jsonschema.Draft202012Validator(json.loads(schema_path.read_text())).validate(result)
        except jsonschema.ValidationError as error:
            raise ValueError("WP8 terminal Agent result does not satisfy the current contract") from error
        submit_id = evaluated["evidence"]["result_submit_call_id"]
        submitted = [event.payload.get("arguments", {}).get("result") for event in events
                     if event.event_type == "ToolCall" and event.payload.get("source") == "mcp_server"
                     and event.payload.get("call_id") == submit_id]
        if submitted != [result]:
            raise ValueError("WP8 terminal result differs from the actual submitted result")
    return {
        **evaluated, "artifact_refs": list(artifact_refs), "gateway_config_sha256": version,
        "gateway_route": route,
        "gateway_sidecar_evidence": {"verified": True, "request_ids": ids,
                                     "artifact_ref": "gateway-requests.json"},
    }


def _verify_wp8_canonical_events(rows: list[Any], platform: list[dict[str, Any]]) -> bool:
    """Verify canonical events against the Controller ledger.

    Return whether a live native ``ToolResult`` is present.  A missing native
    result is expected when a model/provider fails before the first tool call;
    callers decide whether that absence is acceptable for the outcome.  Any
    mismatch between events that do exist remains a hard evidence error.
    """
    fields = {
        "ToolCall": ("call_id", "tool", "arguments"),
        "ToolResult": ("call_id", "status", "payload"),
        "Checkpoint": ("values",),
    }

    def identity(kind: str, source: str, value: dict[str, Any]) -> str:
        return json.dumps([kind, source, {key: value.get(key) for key in fields[kind]}], sort_keys=True)

    recorded = Counter()
    streamed = False
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("invalid BladeAI canonical event")
        kind, source = row.get("event_type"), row.get("source")
        if not isinstance(kind, str) or not isinstance(source, str):
            raise ValueError("invalid BladeAI canonical event identity")
        if kind in fields and source in {"native", "mcp_server"}:
            if row.get("replayed") is not False:
                raise ValueError("WP8 requires the live BladeAI stream, not replayed events")
            recorded[identity(kind, source, row)] += 1
            streamed |= source == "native" and kind == "ToolResult"
    original = Counter(
        identity(row["event_type"], row["payload"]["source"], row["payload"])
        for row in platform if row.get("event_type") in fields
        and row.get("payload", {}).get("source") in {"native", "mcp_server"}
    )
    if recorded != original:
        raise ValueError("BladeAI canonical stream does not match the Controller ledger")
    return streamed


def _wp8_entry(path: Path, record: dict[str, Any], root: Path,
               gateway: GatewayConfigSnapshot) -> tuple[str, dict[str, Any]]:
    refs = record.get("artifact_refs")
    if not isinstance(refs, list) or not all(isinstance(ref, str) for ref in refs):
        raise ValueError("WP8 artifact references are required")
    derived = evaluate_wp8_artifacts(refs, artifact_root=root, gateway=gateway)
    if record != derived or derived.get("passed") is not True or derived.get("status") != "passed":
        raise ValueError("BladeAI full-chain qualification did not verify against its actual artifacts")
    descriptor = HarnessCapability(
        kind=HarnessKind.BLADEAI, execution_model="stream", streams_tool_results=True,
        post_hoc_trace=False, supports_resume=False, supports_mid_turn_feedback=True,
        feedback_channels=("in_band_mcp",), code_execution="none", qualification_passed=True,
        probe={"qualification_profile": derived["qualification_type"],
               "channel_trial_id": derived["trial_id"], "model_alias": derived["model"],
               "gateway_config_sha256": derived["gateway_config_sha256"],
               "gateway_request_ids": derived["gateway_sidecar_evidence"]["request_ids"],
               "full_chain_checks": derived["checks"]},
    )
    return "bladeai", {
        "qualification": {"status": "passed", "evidence_ref": str(path.resolve()),
                          "qualification_type": derived["qualification_type"], "reason": None},
        "capability": descriptor.model_dump(mode="json"),
    }


def publish_capabilities(record_files: Sequence[Path], *, artifact_root: Path, output: Path,
                         gateway: GatewayConfigSnapshot) -> Path:
    """Atomically publish exactly the supplied evidence set, never inferred entries.

    Each Harness needs one base (or BladeAI WP8) record.  An optional WP11
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
