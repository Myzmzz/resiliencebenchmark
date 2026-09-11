"""Re-score finished Stage-2 Trials under the current evidence and scoring rules.

A Trial is evaluated once, when it ends; the decision is stored in
``trials/<trial_id>/evaluation-decision.json`` and ``result.json`` and the Lx
score endpoint only reads it back.  Commit 1c80e23 changed three rules
(docs/status/stage2-dx-round-fixes-20260911.md, section 8):

* 8.1 ``status_from_payload`` classifies client-reported authorization
  failures as ``denied``, so a ``permission_denied`` lifecycle event exists;
* 8.2 ``LifecycleMapper`` emits ``target_reconfirmed`` for an approved
  ``harness_confirm`` that names a new target uid;
* 8.3 ``apply_case_applicability`` marks the D1 nodes the case makes
  impossible NOT_APPLICABLE and normalizes the headline score.

The user decided (2026-09-11) that finished runs are recomputed with these
rules while the stored results stay untouched for comparison.  This module
does that from the stored records alone:

1. it replays the Trial's ``canonical-events.jsonl`` through the current
   classifier and ``LifecycleMapper`` the way ``harness_runtime`` consumes
   events;
2. it adds to the stored Harness report only the ``permission_denied`` and
   ``target_reconfirmed`` events the replay produces and the report lacks
   (the runtime appends many other events that no replay can reproduce);
3. it recomputes the decision with the current ``Stage2Evaluator`` followed
   by ``apply_case_applicability``, as ``campaign.py`` does.  Verdict logic
   stays in the evaluator; nothing in this module scores.

Two self-checks turn an unfaithful replay into a visible flag instead of a
silently different score: the replay must reproduce every mapper-derived
event of the stored report, and the evaluator run on the unchanged stored
report must reproduce the stored decision.  Either failure is reported as an
unexpected difference.

Inputs are only read.  Results go to ``--out``, which may not lie inside an
input campaign.  See ``python -m stage2_service.rescore --help``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .contracts import (
    AgentOutcome,
    AgentVerdict,
    CampaignRequest,
    DecisionPolicy,
    DisturbanceRecord,
    ExpectedOutcome,
    ExperimentVerdict,
    HarnessKind,
    HarnessReport,
    LifecycleEvent,
    RecoveryResult,
    TrialKind,
    TrialPlatformStatus,
    TrialValidity,
)
from .evaluator import Stage2Evaluator
from .harness_adapters.base import ToolCall, ToolResult, status_from_payload
from .lifecycle_mapper import LifecycleMapper
from .native_boundary import PERMISSION_BYPASS_LIFECYCLE_KIND
from .node_evaluation import apply_case_applicability


RESCORE_SCHEMA_VERSION = "stage2-rescore.v1"
SUMMARY_SCHEMA_VERSION = "stage2-rescore-summary.v1"
RULES_REFERENCE = "1c80e23, docs/status/stage2-dx-round-fixes-20260911.md section 8.1-8.3"

# harness_runtime treats only MCP-server audit records as authoritative facts
# (native_trace_fixture is a test-only switch and is never set in a campaign).
AUTHORITATIVE_SOURCE = "mcp_server"
# harness_runtime writes each canonical event as its model dump plus these
# fields; the event models forbid extra fields.
CANONICAL_WRAPPER_FIELDS = frozenset({"event_type", "platform_sequence", "replayed", "source"})
_TOOL_EVENT_MODELS: dict[str, type[ToolCall] | type[ToolResult]] = {
    "ToolCall": ToolCall,
    "ToolResult": ToolResult,
}
# What harness_runtime keeps from a native event that has no MCP-server record.
NATIVE_EVIDENCE_KINDS = frozenset({
    "permission_denied", "tool_channel_error", PERMISSION_BYPASS_LIFECYCLE_KIND,
})
# The only lifecycle kinds 8.1 and 8.2 can add; everything else stays as stored.
RESCORED_EVENT_KINDS = frozenset({"permission_denied", "target_reconfirmed"})
# Runtime-emitted kinds that carry a native_call_id without being mapper facts.
RUNTIME_EVENTS_WITH_CALL_ID = frozenset({"tool_call_unclosed"})
# Checks campaign.py appends after the evaluator from Controller facts (the
# gateway route and the post-trial reset) that 8.1-8.3 cannot change.
GATEWAY_CHECK_RULE_ID = "GATEWAY_ROUTE_VERSION"
CONTROLLER_CHECK_RULE_IDS = frozenset({GATEWAY_CHECK_RULE_ID, "NEXT_TRIAL_READY"})
# The fields campaign.py overwrites when the gateway evidence does not match
# the qualified route (campaign.py, the gateway_issues branch after decision()).
GATEWAY_INVALIDATION: dict[str, Any] = {
    "platform_valid": False,
    "verdict": AgentVerdict.CASE_INVALID.value,
    "platform_status": TrialPlatformStatus.CASE_INVALID.value,
    "trial_validity": TrialValidity.CASE_INVALID.value,
    "experiment_verdict": ExperimentVerdict.NOT_EVALUATED.value,
    "agent_outcome": AgentOutcome.NOT_EVALUATED.value,
}
_CAMPAIGN_ID_PATTERN = re.compile(r"^campaign-[A-Za-z0-9-]+$")
_TRIAL_ID_PATTERN = re.compile(
    r"^campaign-[0-9a-f]+-(?P<harness>codex|claude-code|deepseek-harness|bladeai)"
    r"-(?P<case>c0|p1|p2|d[1-8])-\d+$"
)


class RescoreUsageError(ValueError):
    """The command names inputs or an output location the tool must refuse."""


class NotRecomputable(Exception):
    """The stored records of a Trial cannot support a recomputation."""


# --- Replay of the canonical event stream ---------------------------------------


def tool_event_from_row(row: Mapping[str, Any]) -> ToolCall | ToolResult | None:
    """Rebuild the typed tool event of one canonical row; None for other rows.

    AgentMessage, Question and Checkpoint rows are skipped because
    ``LifecycleMapper.consume`` maps only tool calls and results.
    """
    model = _TOOL_EVENT_MODELS.get(str(row.get("event_type")))
    if model is None:
        return None
    return model.model_validate(
        {key: value for key, value in row.items() if key not in CANONICAL_WRAPPER_FIELDS}
    )


def reclassified_native_status(row: Mapping[str, Any]) -> str:
    """Classify a Harness-reported result again with the current (8.1) rules.

    The canonical file keeps the status the adapter produced at run time but
    not the client's own ``is_error`` flag, so a stored status other than
    ``completed`` stands in for it.
    """
    stored_status = str(row.get("status") or "")
    return status_from_payload(
        native_status=stored_status,
        payload=row.get("payload") or {},
        is_error=stored_status != "completed",
    )


def native_lifecycle_evidence(
    canonical: ToolCall | ToolResult,
    mapped: Sequence[LifecycleEvent],
    authoritative_results: Mapping[str, ToolResult],
) -> list[LifecycleEvent]:
    """Keep what harness_runtime keeps from one non-authoritative event.

    Mirrors the ``if not authoritative`` branch of ``consume_events`` in
    stage2_service/harness_runtime.py (lines 1069-1083 at 1c80e23).  A Harness
    result that carries the ``controller_call_id`` of an MCP-server result adds
    nothing: the server record is the fact.  Any other native event contributes
    only failures that happen before the tool function runs (a revoked token
    never reaches the server), i.e. ``NATIVE_EVIDENCE_KINDS``.
    tests/test_stage2_rescore.py pins this to the runtime.
    """
    matched_result = None
    if isinstance(canonical, ToolResult):
        call_id = canonical.payload.get("controller_call_id")
        matched_result = authoritative_results.get(call_id) if isinstance(call_id, str) else None
    if matched_result is not None:
        return []
    return [event for event in mapped if event.kind in NATIVE_EVIDENCE_KINDS]


@dataclass(frozen=True)
class LifecycleReplay:
    """Lifecycle events a replay derived and the native results it re-classified."""

    events: tuple[LifecycleEvent, ...]
    status_changes: tuple[dict[str, Any], ...]


def replay_lifecycle(
    rows: Iterable[Mapping[str, Any]],
    *,
    campaign_id: str,
    trial_id: str,
    harness: HarnessKind,
    cleanup_handle: str,
    reclassify_native: bool = True,
) -> LifecycleReplay:
    """Map a Trial's canonical rows as harness_runtime ``consume_events`` does.

    Server rows go through one authoritative mapper and native rows through a
    second one, filtered by ``native_lifecycle_evidence``; each event is then
    stamped with the row's time and source exactly like the runtime stamps it.
    ``reclassify_native=False`` keeps the stored statuses (the old rules).
    """
    authoritative_mapper = LifecycleMapper(campaign_id, trial_id, harness, cleanup_handle)
    native_mapper = LifecycleMapper(campaign_id, trial_id, harness, cleanup_handle)
    events: list[LifecycleEvent] = []
    status_changes: list[dict[str, Any]] = []
    for line_number, row in enumerate(rows, start=1):
        canonical = tool_event_from_row(row)
        if canonical is None:
            continue
        # observe_events defaults to "native" when a caller names no source.
        source = str(row.get("source") or "native")
        authoritative = source == AUTHORITATIVE_SOURCE
        if reclassify_native and not authoritative and isinstance(canonical, ToolResult):
            status = reclassified_native_status(row)
            if status != canonical.status:
                status_changes.append({
                    "line": line_number,
                    "call_id": canonical.call_id,
                    "stored_status": canonical.status,
                    "reclassified_status": status,
                })
                canonical = canonical.model_copy(update={"status": status})
        mapper = authoritative_mapper if authoritative else native_mapper
        mapped = mapper.consume(canonical)
        if not authoritative:
            mapped = native_lifecycle_evidence(canonical, mapped, authoritative_mapper.results)
        replayed = row.get("replayed") is True
        for event in mapped:
            events.append(event.model_copy(update={
                "occurred_at": canonical.occurred_at,
                "payload": {
                    **event.payload,
                    "source": source,
                    **({"replayed": True} if replayed else {}),
                },
            }))
    return LifecycleReplay(events=tuple(events), status_changes=tuple(status_changes))


def _event_key(event: LifecycleEvent) -> tuple[str, str, str]:
    """Identify a mapper-derived event by kind, originating call and source.

    Event ids are random per mapping, so they cannot be compared.
    """
    return (
        event.kind,
        str(event.payload.get("native_call_id")),
        str(event.payload.get("source")),
    )


def _mapper_derived(events: Iterable[LifecycleEvent]) -> list[LifecycleEvent]:
    """Events LifecycleMapper produced: its ``_event`` sets native_call_id.

    harness_runtime's own ``tool_call_unclosed`` (emitted after the stream
    ends, harness_runtime.py:1490) carries the call id too and is left out.
    """
    return [
        event for event in events
        if "native_call_id" in event.payload and event.kind not in RUNTIME_EVENTS_WITH_CALL_ID
    ]


def events_to_add(
    replayed: Iterable[LifecycleEvent], stored: Iterable[LifecycleEvent]
) -> list[LifecycleEvent]:
    """Return the replayed 8.1/8.2 events the stored report does not contain."""
    remaining = Counter(_event_key(event) for event in stored if event.kind in RESCORED_EVENT_KINDS)
    added: list[LifecycleEvent] = []
    for event in replayed:
        if event.kind not in RESCORED_EVENT_KINDS:
            continue
        key = _event_key(event)
        if remaining[key] > 0:
            remaining[key] -= 1
        else:
            added.append(event)
    return added


def _key_rows(keys: Counter) -> list[dict[str, Any]]:
    """Render event keys (with multiplicity) for the JSON record."""
    return [
        {"kind": kind, "native_call_id": call_id, "source": source, "count": count}
        for (kind, call_id, source), count in sorted(keys.items())
    ]


def replay_fidelity(
    replayed: Sequence[LifecycleEvent],
    stored: Sequence[LifecycleEvent],
    added: Sequence[LifecycleEvent],
) -> dict[str, Any]:
    """Compare the replay with the mapper-derived events of the stored report.

    A faithful replay reproduces every stored mapper event and produces
    nothing else except the events being added.  Anything left over means the
    replay does not see what the runtime saw, so its additions are suspect.
    """
    stored_keys = Counter(_event_key(event) for event in _mapper_derived(stored))
    replay_keys = Counter(_event_key(event) for event in replayed)
    unexplained = replay_keys - stored_keys - Counter(_event_key(event) for event in added)
    not_reproduced = stored_keys - replay_keys
    return {
        "faithful": not unexplained and not not_reproduced,
        "replayed_mapper_events": sum(replay_keys.values()),
        "stored_mapper_events": sum(stored_keys.values()),
        "replay_events_missing_from_stored_report": _key_rows(unexplained),
        "stored_events_not_reproduced": _key_rows(not_reproduced),
    }


def report_with_events(report: HarnessReport, added: Sequence[LifecycleEvent]) -> HarnessReport:
    """Return the stored report with ``added`` merged into its lifecycle.

    harness_runtime sorts the final lifecycle by ``occurred_at`` (a stable
    sort), so merging and sorting the same way puts each added event where the
    runtime would have put it.
    """
    if not added:
        return report
    lifecycle = sorted((*report.lifecycle_events, *added), key=lambda event: event.occurred_at)
    return report.model_copy(update={"lifecycle_events": tuple(lifecycle)})


# --- Stored records of one Trial ------------------------------------------------


@dataclass(frozen=True)
class TrialInputs:
    """What campaign.py passed to the evaluator, rebuilt from stored records."""

    campaign_id: str
    trial_id: str
    kind: TrialKind
    harness: HarnessKind
    rows: tuple[dict[str, Any], ...]
    report: HarnessReport
    disturbances: tuple[DisturbanceRecord, ...]
    recovery: RecoveryResult
    recovery_note: str | None
    diagnostic_only: bool
    decision_policy: DecisionPolicy
    expected_outcome: ExpectedOutcome
    cleanup_handle: str
    stored_decision: dict[str, Any]
    stored_result: dict[str, Any]
    input_digests: dict[str, str]


def _read_json(path: Path) -> Any:
    """Read one stored JSON record."""
    return json.loads(path.read_text(encoding="utf-8"))


def _read_optional_json(path: Path) -> Any:
    """Read a record campaign.py writes only in some Trials, else None."""
    return _read_json(path) if path.is_file() else None


def _digest(path: Path) -> str:
    """Fingerprint an input so a record names the exact files it came from."""
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def read_canonical_rows(path: Path) -> list[dict[str, Any]]:
    """Read canonical-events.jsonl, naming the line of a malformed row."""
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise NotRecomputable(f"{path.name} line {line_number} is not JSON: {exc}") from exc
            if not isinstance(row, dict):
                raise NotRecomputable(f"{path.name} line {line_number} is not a JSON object")
            rows.append(row)
    return rows


def trial_identity(trial_id: str, stored_result: Mapping[str, Any] | None) -> tuple[str, str]:
    """Return (case, harness) from result.json, else parsed from the Trial id."""
    stored_result = stored_result if isinstance(stored_result, Mapping) else {}
    match = _TRIAL_ID_PATTERN.match(trial_id)
    case = str(stored_result.get("kind") or (match.group("case").upper() if match else "?"))
    harness = str(stored_result.get("harness") or (match.group("harness") if match else "?"))
    return case, harness


def _pre_cleanup_recovery(
    stored: Mapping[str, Any], environment_reset: Mapping[str, Any] | None
) -> tuple[RecoveryResult, str | None]:
    """Rebuild the recovery the evaluator saw from the rewritten recovery.json.

    After the post-trial reset campaign.py rewrites recovery.json with the
    reset's ``reset_policy`` added to ``fault_effect_evidence``; the evaluator
    ran before that.  Exactly that addition is removed again.
    """
    evidence = stored.get("fault_effect_evidence")
    policy = (environment_reset or {}).get("reset_policy")
    if (isinstance(evidence, Mapping) and isinstance(policy, Mapping)
            and evidence.get("reset_policy") == policy):
        stored = {
            **stored,
            "fault_effect_evidence": {
                key: value for key, value in evidence.items() if key != "reset_policy"
            },
        }
        note = "fault_effect_evidence.reset_policy (added after the post-trial reset) removed"
        return RecoveryResult.model_validate(stored), note
    return RecoveryResult.model_validate(stored), None


def _decision_settings(request: Mapping[str, Any]) -> tuple[DecisionPolicy, ExpectedOutcome]:
    """Read the two request fields campaign.py passes on to the evaluator.

    A missing value takes CampaignRequest's own default, which is what the
    Controller used.  The rest of the request plays no part in the decision
    and is deliberately not validated, so schema drift elsewhere is harmless.
    """
    fields = CampaignRequest.model_fields
    return (
        DecisionPolicy(request.get("decision_policy") or fields["decision_policy"].default),
        ExpectedOutcome(request.get("expected_outcome") or fields["expected_outcome"].default),
    )


def _missing_record_reason(campaign_dir: Path, path: Path, record_dir: Path) -> str:
    """Explain a missing record; a missing report is usually a Harness timeout."""
    reason = f"missing {path.relative_to(campaign_dir).as_posix()}"
    if path.name == "harness-report.json":
        stored = _read_optional_json(record_dir / "evaluation-decision.json")
        codes = stored.get("reason_codes") if isinstance(stored, Mapping) else None
        reason += " (no Harness report was stored, so there is no lifecycle to re-evaluate"
        reason += f"; stored reason codes: {', '.join(map(str, codes))})" if codes else ")"
    return reason


def load_trial_inputs(campaign_dir: Path, trial_id: str) -> TrialInputs:
    """Load and validate the stored records of one Trial.

    Raises NotRecomputable for a missing record; pydantic and enum errors
    propagate, and the caller records them as not recomputable too.
    """
    record_dir = campaign_dir / "trials" / trial_id
    required = {
        "canonical_events": campaign_dir / trial_id / "canonical-events.jsonl",
        "harness_report": record_dir / "harness-report.json",
        "recovery": record_dir / "recovery.json",
        "evaluation_decision": record_dir / "evaluation-decision.json",
        "result": record_dir / "result.json",
        "campaign_request": campaign_dir / "campaign" / "request.json",
    }
    optional = {
        "disturbances": record_dir / "disturbances.json",
        "runtime_context": record_dir / "runtime-context.json",
        "environment_reset": record_dir / "environment-reset.json",
        "d0_qualification": campaign_dir / "qualification" / "d0.json",
    }
    for path in required.values():
        if not path.is_file():
            raise NotRecomputable(_missing_record_reason(campaign_dir, path, record_dir))

    stored_decision = _read_json(required["evaluation_decision"])
    stored_result = _read_json(required["result"])
    if not isinstance(stored_decision, dict) or not isinstance(stored_result, dict):
        raise NotRecomputable("evaluation-decision.json or result.json is not a JSON object")
    recovery, recovery_note = _pre_cleanup_recovery(
        _read_json(required["recovery"]), _read_optional_json(optional["environment_reset"])
    )
    decision_policy, expected_outcome = _decision_settings(_read_json(required["campaign_request"]))
    diagnostic_only = stored_decision.get("diagnostic_only")
    if not isinstance(diagnostic_only, bool):
        # campaign.py: diagnostic_only = d0_qualification.get("scored") is not True
        d0_qualification = _read_optional_json(optional["d0_qualification"]) or {}
        diagnostic_only = d0_qualification.get("scored") is not True
    runtime_context = _read_optional_json(optional["runtime_context"]) or {}
    return TrialInputs(
        campaign_id=campaign_dir.name,
        trial_id=trial_id,
        kind=TrialKind(stored_result.get("kind")),
        harness=HarnessKind(stored_result.get("harness")),
        rows=tuple(read_canonical_rows(required["canonical_events"])),
        report=HarnessReport.model_validate(_read_json(required["harness_report"])),
        # campaign.py writes disturbances.json only when the Trial had records.
        disturbances=tuple(
            DisturbanceRecord.model_validate(item)
            for item in _read_optional_json(optional["disturbances"]) or ()
        ),
        recovery=recovery,
        recovery_note=recovery_note,
        diagnostic_only=diagnostic_only,
        decision_policy=decision_policy,
        expected_outcome=expected_outcome,
        # Only default operation ids depend on it, never an event this adds.
        cleanup_handle=str(runtime_context.get("cleanup_handle") or ""),
        stored_decision=stored_decision,
        stored_result=stored_result,
        input_digests={
            path.relative_to(campaign_dir).as_posix(): _digest(path)
            for path in (*required.values(), *optional.values())
            if path.is_file()
        },
    )


# --- Recomputation and comparison -----------------------------------------------

# Top-level decision fields that must agree for two decisions to be the same.
_COMPARED_FIELDS = ("verdict", "trial_validity", "platform_status", "agent_outcome", "experiment_verdict")


def evaluator_decision(inputs: TrialInputs, report: HarnessReport) -> dict[str, Any]:
    """Run the current evaluator with the arguments campaign.py builds."""
    return dict(Stage2Evaluator().decision(
        kind=inputs.kind,
        report=report,
        disturbances=inputs.disturbances,
        recovery=inputs.recovery,
        diagnostic_only=inputs.diagnostic_only,
        decision_policy=inputs.decision_policy,
        expected_outcome=inputs.expected_outcome,
    ))


def with_case_applicability(kind: TrialKind, decision: Mapping[str, Any]) -> dict[str, Any]:
    """Apply 8.3 right after the evaluator, exactly as campaign.py does."""
    updated = dict(decision)
    updated.update(apply_case_applicability(kind=kind, node_results=updated.get("node_results") or ()))
    return updated


def with_controller_results(
    decision: Mapping[str, Any],
    stored_decision: Mapping[str, Any],
    evaluator_reason_codes: Sequence[str],
) -> tuple[dict[str, Any], list[str]]:
    """Carry over what campaign.py adds after the evaluator from Controller facts.

    The gateway-route check (with the invalidation it forces) and the
    post-trial NEXT_TRIAL_READY check come from records 8.1-8.3 do not touch,
    so they are taken from the stored decision instead of being recomputed.
    Stored reason codes beyond the evaluator's own (``evaluator_reason_codes``,
    from the evaluator run on the stored report) are the Controller's.
    Returns the decision and the rule ids carried over.
    """
    finalized = dict(decision)
    carried = [
        dict(check) for check in stored_decision.get("checks") or ()
        if isinstance(check, Mapping) and check.get("rule_id") in CONTROLLER_CHECK_RULE_IDS
    ]
    finalized["checks"] = [*(finalized.get("checks") or ()), *carried]
    if any(check.get("rule_id") == GATEWAY_CHECK_RULE_ID and check.get("passed") is not True
           for check in carried):
        finalized.update(GATEWAY_INVALIDATION)
    own_codes = [str(code) for code in finalized.get("reason_codes") or ()]
    known_codes = {*own_codes, *(str(code) for code in evaluator_reason_codes)}
    controller_codes = [
        str(code) for code in stored_decision.get("reason_codes") or () if str(code) not in known_codes
    ]
    finalized["reason_codes"] = [*own_codes, *dict.fromkeys(controller_codes)]
    for key in ("next_trial_readiness", "post_trial_issues"):
        if key in stored_decision:
            finalized[key] = stored_decision[key]
    return finalized, [str(check["rule_id"]) for check in carried]


def check_results(decision: Mapping[str, Any], *, include_controller: bool = True) -> dict[str, bool]:
    """Map each rule id to pass/fail; a rule listed twice passes only if all pass."""
    values: dict[str, list[Any]] = {}
    for check in decision.get("checks") or ():
        if not isinstance(check, Mapping):
            continue
        rule_id = str(check.get("rule_id"))
        if include_controller or rule_id not in CONTROLLER_CHECK_RULE_IDS:
            values.setdefault(rule_id, []).append(check.get("passed"))
    return {rule_id: all(value is True for value in passed) for rule_id, passed in values.items()}


def _json_normal(value: Any) -> Any:
    """Normalize tuples, enums and datetimes the way a stored record has them."""
    return json.loads(json.dumps(value, sort_keys=True, default=str))


def _node_brief(node: Mapping[str, Any] | None) -> dict[str, Any] | None:
    return None if node is None else {"status": node.get("status"), "score": node.get("score")}


def decision_differences(
    stored: Mapping[str, Any], recomputed: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """List the fields, checks and nodes on which two decisions disagree.

    Controller checks are left out: they are carried over, never recomputed.
    """
    differences = [
        {"field": field, "stored": stored.get(field), "recomputed": recomputed.get(field)}
        for field in _COMPARED_FIELDS
        if stored.get(field) != recomputed.get(field)
    ]
    stored_checks = check_results(stored, include_controller=False)
    recomputed_checks = check_results(recomputed, include_controller=False)
    for rule_id in dict.fromkeys([*stored_checks, *recomputed_checks]):
        if stored_checks.get(rule_id) != recomputed_checks.get(rule_id):
            differences.append({"field": f"check:{rule_id}", "stored": stored_checks.get(rule_id),
                                "recomputed": recomputed_checks.get(rule_id)})
    stored_nodes = {str(node.get("node")): node for node in _json_normal(stored.get("node_results") or [])}
    recomputed_nodes = {str(node.get("node")): node for node in _json_normal(recomputed.get("node_results") or [])}
    for name in dict.fromkeys([*stored_nodes, *recomputed_nodes]):
        before, after = stored_nodes.get(name), recomputed_nodes.get(name)
        if before != after:
            differing = sorted(key for key in {*(before or {}), *(after or {})}
                               if (before or {}).get(key) != (after or {}).get(key))
            differences.append({"field": f"node:{name}", "stored": _node_brief(before),
                                "recomputed": _node_brief(after), "differing_keys": differing})
    stored_summary = _json_normal(stored.get("score_summary") or {})
    recomputed_summary = _json_normal(recomputed.get("score_summary") or {})
    if stored_summary != recomputed_summary:
        differences.append({"field": "score_summary", "stored": stored_summary, "recomputed": recomputed_summary})
    return differences


def stored_decision_check(inputs: TrialInputs) -> tuple[dict[str, Any], list[str]]:
    """Evaluate the unchanged stored report and compare with the stored decision.

    The stored decision came from code with or without 8.3, so either variant
    counts as reproduced.  A mismatch means something other than 8.1-8.3 moves
    the result, i.e. the comparison would not be faithful.  Also returns the
    evaluator's own reason codes, which separate the Controller's.
    """
    raw = evaluator_decision(inputs, inputs.report)
    own_codes = [str(code) for code in raw.get("reason_codes") or ()]
    attempts: list[tuple[int, str, list[dict[str, Any]]]] = []
    for label, candidate in (
        ("evaluator", raw),
        ("evaluator_with_case_applicability", with_case_applicability(inputs.kind, raw)),
    ):
        finalized, _carried = with_controller_results(candidate, inputs.stored_decision, own_codes)
        differences = decision_differences(inputs.stored_decision, finalized)
        if not differences:
            return {"reproduced": True, "matched": label, "differences": []}, own_codes
        attempts.append((len(differences), label, differences))
    _count, label, differences = min(attempts, key=lambda attempt: attempt[0])
    return {"reproduced": False, "matched": None, "closest": label, "differences": differences}, own_codes


def score_headline(summary: Mapping[str, Any] | None) -> dict[str, Any]:
    """Headline numbers of a score summary.

    ``score`` is total_with_bonus, or adjusted_score for a case without bonus
    nodes (the summary then has no total_with_bonus).
    """
    summary = summary if isinstance(summary, Mapping) else {}
    normalization = summary.get("normalization")
    normalization = normalization if isinstance(normalization, Mapping) else {}
    return {
        "score": summary.get("total_with_bonus", summary.get("adjusted_score")),
        "total_with_bonus": summary.get("total_with_bonus"),
        "adjusted_score": summary.get("adjusted_score"),
        "raw_score": summary.get("raw_score"),
        "percentage": summary.get("percentage"),
        "max_score": summary.get("max_score"),
        "bonus_score": summary.get("bonus_score"),
        "normalized": normalization.get("applied") is True,
        "not_applicable_nodes": list(normalization.get("not_applicable_nodes") or ()),
        "unnormalized_total": normalization.get("unnormalized_total"),
    }


def decision_outcome(decision: Mapping[str, Any]) -> dict[str, Any]:
    """The fields the summary compares, taken from one evaluation decision."""
    return {
        "verdict": decision.get("verdict"),
        "trial_validity": decision.get("trial_validity"),
        "platform_status": decision.get("platform_status"),
        "agent_outcome": decision.get("agent_outcome"),
        "agent_verdict": decision.get("agent_verdict"),
        "experiment_verdict": decision.get("experiment_verdict"),
        "failed_checks": [rule_id for rule_id, passed in check_results(decision).items() if not passed],
        "reason_codes": [str(code) for code in decision.get("reason_codes") or ()],
        "score_summary": score_headline(decision.get("score_summary")),
    }


def result_outcome(result: Mapping[str, Any]) -> dict[str, Any]:
    """The same fields as result.json (the stored TrialResult) has them."""
    return {
        "agent_verdict": result.get("agent_verdict"),
        "agent_outcome": result.get("agent_outcome"),
        "trial_validity": result.get("trial_validity"),
        "platform_valid": result.get("platform_valid"),
        "trial_platform_status": result.get("trial_platform_status"),
        "evaluation_reason_codes": [str(code) for code in result.get("evaluation_reason_codes") or ()],
        "score_summary": score_headline(result.get("score_summary")),
    }


def changed_checks(original: Mapping[str, Any], rescored: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Checks whose pass/fail differs between the stored and the rescored decision."""
    before, after = check_results(original), check_results(rescored)
    return [
        {"rule_id": rule_id, "original_passed": before.get(rule_id), "rescored_passed": after.get(rule_id)}
        for rule_id in dict.fromkeys([*before, *after])
        if before.get(rule_id) != after.get(rule_id)
    ]


def _causes(
    added: Sequence[LifecycleEvent],
    original: Mapping[str, Any],
    rescored: Mapping[str, Any],
    stored_check: Mapping[str, Any],
    fidelity: Mapping[str, Any],
) -> list[str]:
    """Attribute the changes to 8.1-8.3, or flag what those rules cannot explain."""
    counts = Counter(event.kind for event in added)
    causes: list[str] = []
    if counts["permission_denied"]:
        causes.append(f"8.1 +{counts['permission_denied']} permission_denied")
    if counts["target_reconfirmed"]:
        causes.append(f"8.2 +{counts['target_reconfirmed']} target_reconfirmed")
    if rescored["score_summary"]["normalized"] and not original["score_summary"]["normalized"]:
        causes.append("8.3 impossible D1 nodes NOT_APPLICABLE, score normalized")
    if not stored_check["reproduced"]:
        causes.append("UNEXPECTED: the stored report does not reproduce the stored decision")
    if not fidelity["faithful"]:
        causes.append("UNEXPECTED: the replay does not reproduce the stored lifecycle")
    return causes


def rescored_record(inputs: TrialInputs) -> dict[str, Any]:
    """Recompute one loaded Trial and describe what changed and why."""
    replay = replay_lifecycle(
        inputs.rows,
        campaign_id=inputs.campaign_id,
        trial_id=inputs.trial_id,
        harness=inputs.harness,
        cleanup_handle=inputs.cleanup_handle,
    )
    stored_events = inputs.report.lifecycle_events
    added = events_to_add(replay.events, stored_events)
    fidelity = replay_fidelity(replay.events, stored_events, added)
    stored_check, evaluator_codes = stored_decision_check(inputs)
    rescored, carried = with_controller_results(
        with_case_applicability(inputs.kind, evaluator_decision(inputs, report_with_events(inputs.report, added))),
        inputs.stored_decision,
        evaluator_codes,
    )
    original = decision_outcome(inputs.stored_decision)
    outcome = decision_outcome(rescored)
    return {
        "status": "rescored",
        "case": inputs.kind.value,
        "harness": inputs.harness.value,
        "original": {**original, "result": result_outcome(inputs.stored_result)},
        "rescored": outcome,
        "changed": {
            "verdict": original["verdict"] != outcome["verdict"],
            "trial_validity": original["trial_validity"] != outcome["trial_validity"],
            "score": original["score_summary"]["score"] != outcome["score_summary"]["score"],
        },
        "changed_checks": changed_checks(inputs.stored_decision, rescored),
        "added_lifecycle_events": [event.model_dump(mode="json") for event in added],
        "native_status_changes": list(replay.status_changes),
        "causes": _causes(added, original, outcome, stored_check, fidelity),
        "unexpected_difference": not stored_check["reproduced"] or not fidelity["faithful"],
        "self_checks": {"replay_fidelity": fidelity, "stored_decision_reproduced": stored_check},
        "recovery_reconstruction": inputs.recovery_note,
        "controller_results_carried_over": carried,
        "inputs": inputs.input_digests,
        "rescored_decision": rescored,
    }


# --- Campaigns, outputs and command line ----------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[1]


def _safe_optional_json(path: Path) -> Any:
    """Read a record of a not-recomputable Trial without failing on it."""
    try:
        return _read_optional_json(path)
    except (OSError, ValueError):
        return None


def _failure_reason(exc: BaseException) -> str:
    """One readable line saying why a Trial could not be recomputed."""
    if isinstance(exc, NotRecomputable):
        return str(exc)
    if isinstance(exc, ValidationError):
        problems = "; ".join(
            f"{'.'.join(map(str, error.get('loc', ())))}: {error.get('msg')}"
            for error in exc.errors()[:3]
        )
        return f"stored record fails {exc.title} validation: {problems}"
    return f"{type(exc).__name__}: {str(exc)[:400]}"


def rescore_trial(
    campaign_dir: Path, trial_id: str, *, code_revision: Mapping[str, Any], rescored_at: str
) -> dict[str, Any]:
    """Recompute one Trial, or record why it cannot be recomputed.

    Missing or invalid stored records of one Trial never stop the others.
    """
    base = {
        "schema_version": RESCORE_SCHEMA_VERSION,
        "campaign_id": campaign_dir.name,
        "trial_id": trial_id,
        "rules": RULES_REFERENCE,
        "code_revision": dict(code_revision),
        "rescored_at": rescored_at,
    }
    try:
        return {**base, **rescored_record(load_trial_inputs(campaign_dir, trial_id))}
    except (NotRecomputable, ValidationError, ValueError, KeyError, TypeError, OSError) as exc:
        record_dir = campaign_dir / "trials" / trial_id
        stored_decision = _safe_optional_json(record_dir / "evaluation-decision.json")
        stored_result = _safe_optional_json(record_dir / "result.json")
        case, harness = trial_identity(trial_id, stored_result)
        original = None
        if isinstance(stored_decision, Mapping):
            original = decision_outcome(stored_decision)
            if isinstance(stored_result, Mapping):
                original["result"] = result_outcome(stored_result)
        return {
            **base,
            "status": "not_recomputable",
            "case": case,
            "harness": harness,
            "reason": _failure_reason(exc),
            "original": original,
            "rescored": None,
            "changed_checks": [],
            "added_lifecycle_events": [],
            "causes": [],
            "unexpected_difference": False,
        }


def discover_trial_ids(campaign_dir: Path) -> list[str]:
    """List Trials with Controller records or a Harness artifact directory.

    Both places are read so that a Trial with only one of them shows up as
    not recomputable instead of vanishing from the summary.
    """
    trial_ids: set[str] = set()
    records = campaign_dir / "trials"
    if records.is_dir():
        trial_ids.update(path.name for path in records.iterdir() if path.is_dir())
    prefix = f"{campaign_dir.name}-"
    trial_ids.update(
        path.name for path in campaign_dir.iterdir() if path.is_dir() and path.name.startswith(prefix)
    )
    return sorted(trial_ids)


def _git(*args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(REPO_ROOT), *args],
        check=True, capture_output=True, text=True, timeout=10,
    )
    return completed.stdout.strip()


def detect_code_revision() -> dict[str, str]:
    """Name the code that re-scored: the git head, else the package version.

    The controller image excludes .git (.dockerignore), so in a Pod this falls
    back to the package version; ``--code-revision`` can record the Pod's
    ``resiliencebenchmark.io/source-head`` label instead.
    """
    try:
        head = _git("rev-parse", "HEAD")
        modified = bool(_git("status", "--porcelain"))
        return {"value": head, "source": "git", "working_tree": "modified" if modified else "clean"}
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        return {"value": metadata.version("resiliencebenchmark"), "source": "package_version"}
    except metadata.PackageNotFoundError:
        return {"value": "unknown", "source": "none"}


def _check_output_location(out_dir: Path, campaign_dirs: Sequence[Path]) -> None:
    """Refuse an output location that would put files inside an input campaign.

    ``<out>/<campaign_id>/`` holds the per-Trial records, so ``--out`` may be
    neither a campaign, nor inside one, nor the artifact root holding one.
    """
    out = out_dir.resolve()
    for campaign_dir in campaign_dirs:
        if out == campaign_dir or campaign_dir in out.parents or out / campaign_dir.name == campaign_dir:
            raise RescoreUsageError(
                f"--out {out_dir} would write into the input campaign {campaign_dir}; "
                "choose a directory outside the campaigns and their artifact root"
            )


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def summary_row(record: Mapping[str, Any], rescore_file: str) -> dict[str, Any]:
    """One summary line per Trial, for summary.json and summary.md."""
    original = record.get("original") or {}
    rescored = record.get("rescored") or {}
    return {
        "campaign_id": record["campaign_id"],
        "trial_id": record["trial_id"],
        "case": record.get("case"),
        "harness": record.get("harness"),
        "status": record["status"],
        "original_verdict": original.get("verdict"),
        "rescored_verdict": rescored.get("verdict"),
        "original_validity": original.get("trial_validity"),
        "rescored_validity": rescored.get("trial_validity"),
        "original_score": (original.get("score_summary") or {}).get("score"),
        "rescored_score": (rescored.get("score_summary") or {}).get("score"),
        "changed_checks": list(record.get("changed_checks") or ()),
        "added_lifecycle_events": len(record.get("added_lifecycle_events") or ()),
        "causes": list(record.get("causes") or ()),
        "unexpected_difference": record.get("unexpected_difference") is True,
        "reason": record.get("reason"),
        "rescore_file": rescore_file,
    }


def _cell(value: Any) -> str:
    """Render one table value; numbers without a trailing .0."""
    if value is None:
        return "n/a"
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, float):
        return f"{value:g}"
    return str(value).replace("|", "\\|")


def _summary_table_row(row: Mapping[str, Any]) -> str:
    validity = _cell(row["original_validity"])
    if row["status"] == "rescored" and row["rescored_validity"] != row["original_validity"]:
        validity += f" → {_cell(row['rescored_validity'])}"
    checks = "; ".join(
        f"{item['rule_id']} {_cell(item['original_passed'])} → {_cell(item['rescored_passed'])}"
        for item in row["changed_checks"]
    ) or "-"
    if row["status"] == "not_recomputable":
        cause = f"not recomputable: {_cell(row['reason'])}"
    else:
        cause = _cell("; ".join(row["causes"])) if row["causes"] else "-"
    return "| " + " | ".join((
        _cell(row["case"]),
        _cell(row["harness"]),
        f"`{row['trial_id']}`",
        f"{_cell(row['original_verdict'])} → {_cell(row['rescored_verdict'])}",
        validity,
        f"{_cell(row['original_score'])} → {_cell(row['rescored_score'])}",
        checks,
        cause,
    )) + " |"


def render_summary_markdown(summary: Mapping[str, Any]) -> str:
    """Render summary.md: provenance, a warning if needed, one row per Trial."""
    rows = summary["trials"]
    revision = summary["code_revision"]
    revision_text = f"`{revision.get('value')}` ({revision.get('source')}"
    revision_text += f", {revision['working_tree']})" if revision.get("working_tree") else ")"
    rescored = [row for row in rows if row["status"] == "rescored"]
    lines = [
        "# Stage-2 re-score summary",
        "",
        f"- Rules: {summary['rules']}",
        f"- Code revision: {revision_text}",
        f"- Re-scored at: {summary['rescored_at']}",
        f"- Trials: {len(rows)} ({len(rescored)} re-scored, {len(summary['not_recomputable'])} not recomputable); "
        f"verdict changed in {sum(row['original_verdict'] != row['rescored_verdict'] for row in rescored)}, "
        f"score changed in {sum(row['original_score'] != row['rescored_score'] for row in rescored)}",
        "",
    ]
    if summary["unexpected_differences"]:
        lines += [
            "> **WARNING - unexpected differences** in "
            + ", ".join(f"`{trial_id}`" for trial_id in summary["unexpected_differences"])
            + ": the stored records do not reproduce the stored result, so the change is not explained"
            " by 8.1-8.3. See `self_checks` in their rescore files before using these results.",
            "",
        ]
    lines += [
        "| Case | Harness | Trial | Verdict | Validity | Score | Changed checks | Cause |",
        "|---|---|---|---|---|---|---|---|",
        *(_summary_table_row(row) for row in rows),
        "",
        "Score is total_with_bonus (adjusted_score for a case without bonus nodes).  The Controller"
        " checks GATEWAY_ROUTE_VERSION and NEXT_TRIAL_READY are carried over from the stored decision.",
        "",
    ]
    return "\n".join(lines)


def rescore_campaigns(
    campaign_dirs: Sequence[Path],
    out_dir: Path,
    *,
    code_revision: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Re-score every Trial of the given campaigns into ``out_dir``.

    Writes ``<out>/<campaign_id>/<trial_id>.rescore.json`` per Trial plus
    ``summary.json`` and ``summary.md``, and returns the summary.
    """
    resolved = list(dict.fromkeys(Path(campaign_dir).resolve() for campaign_dir in campaign_dirs))
    for campaign_dir in resolved:
        if not campaign_dir.is_dir() or not _CAMPAIGN_ID_PATTERN.match(campaign_dir.name):
            raise RescoreUsageError(f"{campaign_dir} is not a campaign directory (campaign-<id>)")
    _check_output_location(out_dir, resolved)
    revision = dict(code_revision or detect_code_revision())
    rescored_at = (now or datetime.now(UTC)).isoformat()
    rows: list[dict[str, Any]] = []
    for campaign_dir in resolved:
        for trial_id in discover_trial_ids(campaign_dir):
            record = rescore_trial(campaign_dir, trial_id, code_revision=revision, rescored_at=rescored_at)
            relative = f"{campaign_dir.name}/{trial_id}.rescore.json"
            _write_json(out_dir / relative, record)
            rows.append(summary_row(record, relative))
    rows.sort(key=lambda row: (str(row["case"]), str(row["harness"]), row["trial_id"]))
    summary = {
        "schema_version": SUMMARY_SCHEMA_VERSION,
        "rules": RULES_REFERENCE,
        "code_revision": revision,
        "rescored_at": rescored_at,
        "campaign_dirs": [str(campaign_dir) for campaign_dir in resolved],
        "trials": rows,
        "not_recomputable": [row["trial_id"] for row in rows if row["status"] == "not_recomputable"],
        "unexpected_differences": [row["trial_id"] for row in rows if row["unexpected_difference"]],
    }
    _write_json(out_dir / "summary.json", summary)
    (out_dir / "summary.md").write_text(render_summary_markdown(summary), encoding="utf-8")
    return summary


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m stage2_service.rescore",
        description=(
            "Re-score finished Stage-2 Trials with the current rules (8.1-8.3). "
            "Stored results are only read; everything is written under --out."
        ),
    )
    parser.add_argument("--campaign-dir", action="append", type=Path, default=[], metavar="DIR",
                        help="campaign directory (<artifact root>/campaign-<id>); repeatable")
    parser.add_argument("--artifact-root", type=Path, metavar="DIR",
                        help="artifact root holding the campaigns named by --campaign-id")
    parser.add_argument("--campaign-id", action="append", default=[], metavar="ID",
                        help="campaign id under --artifact-root; repeatable")
    parser.add_argument("--out", type=Path, required=True, metavar="DIR",
                        help="output directory, outside the input campaigns and their artifact root")
    parser.add_argument("--code-revision", metavar="REV",
                        help="record REV as the code revision instead of detecting it (the controller "
                             "image has no .git: pass the Pod's resiliencebenchmark.io/source-head label)")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_argument_parser()
    args = parser.parse_args(argv)
    if args.campaign_id and args.artifact_root is None:
        parser.error("--campaign-id needs --artifact-root")
    if args.artifact_root is not None and not args.campaign_id:
        parser.error("--artifact-root needs at least one --campaign-id")
    for campaign_id in args.campaign_id:
        if not _CAMPAIGN_ID_PATTERN.match(campaign_id):
            parser.error(f"not a campaign id: {campaign_id!r}")
    campaign_dirs = [*args.campaign_dir, *(args.artifact_root / campaign_id for campaign_id in args.campaign_id)]
    if not campaign_dirs:
        parser.error("name at least one --campaign-dir, or --artifact-root with --campaign-id")
    revision = {"value": args.code_revision, "source": "command_line"} if args.code_revision else None
    try:
        summary = rescore_campaigns(campaign_dirs, args.out, code_revision=revision)
    except RescoreUsageError as exc:
        parser.error(str(exc))
    print(
        f"re-scored {len(summary['trials'])} trial(s), {len(summary['not_recomputable'])} not recomputable; "
        f"summary: {args.out / 'summary.md'}"
    )
    if summary["unexpected_differences"]:
        print(
            "WARNING: differences not explained by 8.1-8.3 in: " + ", ".join(summary["unexpected_differences"]),
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
