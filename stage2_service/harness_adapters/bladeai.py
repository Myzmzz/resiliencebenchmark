"""BladeAI 0.7.0 black-box SSE stream adapter.

The Harness is driven over its published HTTP/SSE interface
(:mod:`harness.bladeai_http`), so this module translates BladeAI's own event
vocabulary into CanonicalEvent.  It replaces the previous adapter, which parsed
``stage2_bladeai_event`` / ``stage2_bladeai_result`` envelopes minted by our own
in-process worker; those envelopes do not exist once the Harness is a service.

Nothing here imports BladeAI or reflects on its internals.  Every field read
below appears on the public event stream and is pinned by
``tests/fixtures/harness_streams/golden/bladeai_L0.sse``.

Mapping decisions that are not obvious, all grounded in the 2026-09-11/12
corpus (17 cases, 197,687 events) and re-confirmed against a live server:

* ``token`` is a **character-by-character** stream -- L0 alone carries 1,905 of
  them -- so emitting one AgentMessage per event would produce a structure
  nothing like codex's one-message-per-utterance.  Consecutive ``token`` events
  are therefore coalesced into a single AgentMessage; in L0 that yields 39
  messages, and each reassembles into exactly one complete utterance.
* ``thinking`` is the model's private reasoning channel (14,231 events in L0,
  interleaved English reasoning) and is **not** emitted, matching how the other
  three adapters treat native reasoning.  It also does not break a ``token``
  run: the two channels never interleave in the corpus, and not flushing on
  ``thinking`` keeps an utterance whole even if they ever do.
* ``confirm`` carries **all three** gates, told apart by ``node``; there is no
  ``interrupt_id`` field anywhere in the corpus, so the id to answer with is
  the event's own ``task_id``.
* A ``result`` event appears in only 6 of 17 cases: it is emitted when a turn
  finishes on its own, and a turn we cancelled never produces one.  The adapter
  therefore must not treat ``result`` as a precondition for anything.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from stage2_service.contracts import HarnessKind

from .bladeai_intensity import (
    BladeShimError,
    canonical_fault_type,
    canonical_native_intensity,
    native_intensity_source,
)
from .bladeai_legacy import LegacyBladeAIHarnessAdapter

from .base import (
    AgentMessage,
    CanonicalEvent,
    Checkpoint,
    HarnessCapability,
    Question,
    ToolCall,
    ToolResult,
    decode_json_line,
    extract_agent_message,
    extract_call_id,
    normalize_tool_name,
    parse_occurred_at,
    payload_from_result,
    stable_call_id,
    status_from_payload,
)

# The three confirmation gates, keyed by the event's ``node``.  ``request_kind``
# is what WP-C dispatches on; the transport channel differs per gate.
# Envelopes minted by the pre-black-box in-process worker; handled by the
# legacy base class. Removed together with the hook layer in WP-F.
LEGACY_ENVELOPE_TYPES: frozenset[str] = frozenset({
    "stage2_bladeai_event", "stage2_bladeai_result",
})

GATE_REQUEST_KINDS: dict[str, str] = {
    "intent_confirm": "intent",
    "confirmation_gate": "execution",
    "tool_screener": "target_change",
}

# Tools whose unresolved call means "a fault may be live and we cannot see its
# outcome".  D6-B proved this is not hypothetical: the SSE stream was cut right
# after ``blade_create``'s ``tool_start``, no ``tool_end`` ever arrived, and the
# independent observer measured the target going from 3m to ~783m CPU.  Such an
# orphan must never be read as "nothing was injected".
INJECTION_TOOL_MARKERS: tuple[str, ...] = (
    "blade_create", "blade_destroy", "chaos_os", "inject", "exec",
)

# Events that carry no CanonicalEvent of their own.  They still terminate a
# ``token`` run, which is what makes the coalescing boundary exact.
STRUCTURAL_EVENT_TYPES: frozenset[str] = frozenset({
    "node_start", "node_end", "llm_start", "context_size", "usage", "done",
})


class BladeAIHarnessAdapter(LegacyBladeAIHarnessAdapter):
    kind = HarnessKind.BLADEAI

    def __init__(self) -> None:
        super().__init__()
        # The SDK's terminal envelope, kept separate from the Agent's own
        # assessment: a provider failure can arrive with an empty summary, and
        # dropping the envelope makes the later qualification failure
        # undiagnosable.
        self.terminal_result: dict[str, Any] | None = None
        self._token_parts: list[str] = []
        self._token_node: str | None = None
        self._token_at: datetime | None = None
        self._token_task: str | None = None
        self._gate_versions: dict[str, int] = {}
        self._injection_calls: dict[str, ToolCall] = {}
        # What the run was observed to actually do.  Populated only from
        # post-hoc evidence -- never from a plan or an approval card.
        self._executed_spec: dict[str, Any] = {}

    def capability(self) -> HarnessCapability:
        return HarnessCapability(
            kind=self.kind,
            # Black-box BladeAI streams its own events over SSE exactly as
            # codex streams JSONL over stdout; it is no longer driven by us
            # replacing its private functions in-process.
            execution_model="stream",
            streams_tool_results=True,
            post_hoc_trace=False,
            # One session id accepts many turns -- L0 used five.
            supports_resume=True,
            # Two of the three gates accept an answer mid-turn.
            supports_mid_turn_feedback=True,
            feedback_channels=("resume",),
            code_execution="none",
        )

    # ---- stream ---------------------------------------------------------

    def on_stream_line(self, line: bytes) -> list[CanonicalEvent]:
        raw_ref = self._raw_ref()
        value = decode_json_line(line)
        if value is None:
            return []
        if isinstance(value, str):
            return [extract_agent_message(value, parse_occurred_at({}))]

        event_type = str(value.get("type") or "")
        if event_type in LEGACY_ENVELOPE_TYPES:
            # The in-process hook layer's own envelope.  It is still reachable
            # until WP-F removes that layer, and WP-E must be accepted first,
            # so the legacy parser stays available rather than being deleted
            # out from under a path that can still run.
            self._line_index -= 1  # the base parser takes its own raw_ref
            return super().on_stream_line(line)
        if event_type == "token":
            self._accumulate_token(value)
            return []
        if event_type == "thinking":
            # Private reasoning: not an utterance, and deliberately not a
            # flush boundary (see module docstring).
            return []

        events: list[CanonicalEvent] = []
        flushed = self._flush_tokens()
        if flushed is not None:
            events.append(flushed)

        if event_type == "tool_start":
            call = self._tool_call(value)
            if call is not None:
                recorded = self._record_call(call)
                if recorded is not None:
                    events.append(recorded)
        elif event_type == "tool_end":
            result = self._tool_result(value, raw_ref)
            if result is not None:
                self._injection_calls.pop(result.call_id, None)
                events.append(self._record_result(result))
        elif event_type == "node_message":
            message = self._message(value)
            if message is not None:
                events.append(message)
        elif event_type == "confirm":
            events.append(self._question(value))
        elif event_type == "result":
            events.append(self._result_checkpoint(value))
        elif event_type == "error":
            events.append(self._error_checkpoint(value))
        elif event_type not in STRUCTURAL_EVENT_TYPES:
            # An event kind upstream added after 0.7.0.  Record it rather than
            # drop it: losing evidence silently is worse than an extra row.
            events.append(self._unknown_checkpoint(value))
        return events

    def on_turn_end(self, artifact_dir: Path) -> list[CanonicalEvent]:
        flushed = self._flush_tokens()
        return [flushed] if flushed is not None else []

    def unresolved_injection_calls(self) -> list[ToolCall]:
        """Injection-capable calls that never received a ``tool_end``.

        The caller must treat these as **fault state unknown**, not as "no
        fault was injected", and resolve them by measurement (WP-E).
        """
        return [
            call for call_id, call in self._injection_calls.items()
            if call_id not in self._completed_call_ids
        ]

    # ---- token coalescing ----------------------------------------------

    def _accumulate_token(self, value: Mapping[str, Any]) -> None:
        content = value.get("content")
        if not isinstance(content, str):
            return
        if not self._token_parts:
            self._token_node = value.get("node") if isinstance(value.get("node"), str) else None
            self._token_at = parse_occurred_at(value)
            self._token_task = value.get("task_id") if isinstance(value.get("task_id"), str) else None
        self._token_parts.append(content)

    def _flush_tokens(self) -> AgentMessage | None:
        if not self._token_parts:
            return None
        text = "".join(self._token_parts)
        occurred_at = self._token_at or parse_occurred_at({})
        structured: dict[str, Any] = {}
        if self._token_node:
            structured["node"] = self._token_node
        if self._token_task:
            structured["task_id"] = self._token_task
        self._token_parts = []
        self._token_node = None
        self._token_at = None
        self._token_task = None
        if not text.strip():
            return None
        message = extract_agent_message(text, occurred_at)
        if structured:
            merged = dict(message.structured or {})
            merged.update(structured)
            return AgentMessage(text=message.text, structured=merged, occurred_at=occurred_at)
        return message

    # ---- per-event mapping ----------------------------------------------

    def _tool_call(self, value: Mapping[str, Any]) -> ToolCall | None:
        raw_name = value.get("tool_name") or value.get("tool") or value.get("name")
        if not isinstance(raw_name, str) or not raw_name:
            return None
        call_id = extract_call_id(value) or stable_call_id(
            "bladeai", raw_name, self._line_index
        )
        call = ToolCall(
            call_id=call_id,
            tool=self._canonical_tool_name(raw_name),
            # ``tool_start`` publishes no arguments.  Inventing them from other
            # fields would fabricate evidence; the real parameters are recovered
            # from the paired ``tool_end`` and from the platform's own ledger.
            arguments={},
            occurred_at=parse_occurred_at(value),
        )
        if any(marker in raw_name.lower() for marker in INJECTION_TOOL_MARKERS):
            self._injection_calls[call_id] = call
        return call

    def _tool_result(self, value: Mapping[str, Any], raw_ref: str) -> ToolResult | None:
        call_id = extract_call_id(value)
        if not call_id:
            return None
        payload = self._result_payload(value)
        return ToolResult(
            call_id=call_id,
            status=status_from_payload(
                native_status=value.get("status"),
                payload=payload,
                is_error=str(value.get("status") or "").lower() in {"failed", "error"},
            ),
            payload=payload,
            raw_ref=raw_ref,
            occurred_at=parse_occurred_at(value),
        )

    def _result_payload(self, value: Mapping[str, Any]) -> dict[str, Any]:
        """Translate a native tool result into the platform's result contract.

        BladeAI's built-in tools do not answer through the platform's MCP
        gateway, so they carry none of its ``{ok: ...}`` envelope.  Without
        translating here, every call would reach LifecycleMapper as a failure
        (``successful()`` requires ``ok is True``) and a perfectly good L0 run
        would score as 99 tool execution errors.

        Shapes measured across 1,133 ``tool_end`` events in the corpus:

        * 59 JSON bodies carrying ``{code, success, result}`` -- ``success`` is
          authoritative;
        * 103 plain-text bodies starting with ``Error:`` -- always a real
          failure (Forbidden / NotFound / non-zero exit);
        * 922 other plain-text bodies -- ordinary successful output;
        * 39 with ``content: null`` -- an empty result, **not** a failure.
        """
        content = value.get("content")
        if isinstance(content, Mapping):
            parsed = payload_from_result(content) or dict(content)
            return self._as_contract(parsed, None)
        if content is None:
            # Empty output from a read tool: it ran, it just returned nothing.
            return {"ok": True, "text": ""}
        if not isinstance(content, str):
            return {"ok": True, "value": content}
        parsed = _loads_object(content)
        if parsed is not None:
            return self._as_contract(parsed, content)
        return self._as_contract(None, content)

    def _as_contract(
        self, parsed: dict[str, Any] | None, text: str | None
    ) -> dict[str, Any]:
        if parsed is not None and "success" in parsed:
            ok = bool(parsed.get("success"))
            payload: dict[str, Any] = {"ok": ok, **parsed}
            if not ok:
                payload["error"] = _error_block(
                    parsed.get("error") or parsed.get("message") or text or "",
                    parsed.get("code"),
                )
            return payload
        if parsed is not None:
            # A JSON body that does not declare success: keep it verbatim and
            # let the absence of an error speak for itself.
            return {"ok": True, **parsed}
        body = text or ""
        if _is_error_text(body):
            return {"ok": False, "error": _error_block(body, None), "text": body}
        return {"ok": True, "text": body}

    def _message(self, value: Mapping[str, Any]) -> AgentMessage | None:
        content = value.get("content")
        if not isinstance(content, str) or not content.strip():
            return None
        message = extract_agent_message(content, parse_occurred_at(value))
        node = value.get("node")
        if isinstance(node, str) and node:
            merged = dict(message.structured or {})
            merged["node"] = node
            return AgentMessage(
                text=message.text, structured=merged, occurred_at=message.occurred_at
            )
        return message

    def _question(self, value: Mapping[str, Any]) -> Question:
        """One confirmation gate.

        ``question_id`` is the event's ``task_id`` -- the id the platform must
        send back, on the channel the gate's ``node`` selects.  ``version``
        counts re-emissions of the same gate, which the corpus shows do happen.
        """
        node = value.get("node") if isinstance(value.get("node"), str) else ""
        task_id = value.get("task_id") if isinstance(value.get("task_id"), str) else ""
        key = f"{task_id}:{node}"
        self._gate_versions[key] = self._gate_versions.get(key, 0) + 1
        payload = value.get("payload")
        recommendation: dict[str, Any] = dict(payload) if isinstance(payload, Mapping) else {}
        recommendation["gate_node"] = node
        if isinstance(value.get("content"), str):
            recommendation["card_text"] = value["content"]
        return Question(
            question_id=task_id or stable_call_id("bladeai-gate", node, self._line_index),
            version=self._gate_versions[key],
            # Unknown gate nodes stay identifiable instead of being coerced
            # into one of the three known kinds.
            request_kind=GATE_REQUEST_KINDS.get(node, f"unknown:{node}" if node else "unknown"),
            recommendation=recommendation,
            occurred_at=parse_occurred_at(value),
        )

    def _result_checkpoint(self, value: Mapping[str, Any]) -> Checkpoint:
        content = value.get("content")
        parsed = _loads_object(content) if isinstance(content, str) else (
            dict(content) if isinstance(content, Mapping) else None
        )
        values: dict[str, Any] = {"kind": "result"}
        if parsed is not None:
            self.terminal_result = parsed
            values["result"] = parsed
            executed = self._extract_executed_spec(parsed)
            if executed:
                self._executed_spec = executed
                values["executed_fault_spec"] = executed
        elif isinstance(content, str):
            values["text"] = content
        if isinstance(value.get("task_id"), str):
            values["task_id"] = value["task_id"]
        return Checkpoint(values=values, occurred_at=parse_occurred_at(value))

    def _error_checkpoint(self, value: Mapping[str, Any]) -> Checkpoint:
        """An ``error`` event.

        Deliberately **not** an AgentMessage: ``harness_runtime`` counts
        AgentMessage as evidence the Agent was active, and every ``error`` in
        the corpus was the platform's own ``/cancel``.  Attribution is WP-C.4's
        job and is decided from the driver's own records, not from this text.
        """
        text = value.get("content") or value.get("message")
        return Checkpoint(
            values={
                "kind": "error",
                "message": text if isinstance(text, str) else "",
                **({"task_id": value["task_id"]} if isinstance(value.get("task_id"), str) else {}),
            },
            occurred_at=parse_occurred_at(value),
        )

    def _unknown_checkpoint(self, value: Mapping[str, Any]) -> Checkpoint:
        return Checkpoint(
            values={"kind": "unknown_event", "event": dict(value)},
            occurred_at=parse_occurred_at(value),
        )

    def executed_fault_spec(self) -> dict[str, Any]:
        """What the run actually did, recovered from post-hoc evidence.

        BladeAI's ``tool_start`` publishes no arguments at all, so the platform
        cannot learn the injected parameters when the call is made.  They are
        recovered afterwards from the ``result`` envelope, which reports the
        spec the run executed.  The approval card is deliberately not used as a
        source: finding F11 showed the structured plan and the command actually
        issued can disagree, so only observed execution counts.
        """
        return dict(self._executed_spec)

    def _extract_executed_spec(self, parsed: Mapping[str, Any]) -> dict[str, Any]:
        data = parsed.get("data")
        if not isinstance(data, Mapping):
            return {}
        spec = data.get("fault_spec")
        spec = spec if isinstance(spec, Mapping) else {}
        fault_type = data.get("fault_type") or spec.get("fault_type")
        executed: dict[str, Any] = {"parameters_source": "observed_execution"}
        if isinstance(fault_type, str):
            executed["native_fault_type"] = fault_type
        try:
            executed["fault_type"] = canonical_fault_type(
                str(spec.get("scope") or ""),
                str(spec.get("fault_target") or ""),
                str(spec.get("fault_action") or ""),
            )[0]
        except BladeShimError:
            if isinstance(fault_type, str):
                executed["fault_type"] = fault_type
        for key in ("experiment_uid", "task_id", "injection_method", "task_state"):
            if isinstance(data.get(key), str):
                executed[key] = data[key]
        if isinstance(data.get("experiment_uid"), str):
            executed["operation_id"] = data["experiment_uid"]
        names = spec.get("names")
        if isinstance(names, list) and names:
            executed["target_names"] = [str(n) for n in names]
            executed["target_uid"] = str(names[0])
        for key in ("namespace", "scope", "fault_target", "fault_action"):
            if isinstance(spec.get(key), str):
                executed[key] = spec[key]
        duration = spec.get("duration_seconds")
        if isinstance(duration, int):
            executed["duration_seconds"] = duration
        intensity = self._normalised_intensity(fault_type, spec)
        if intensity:
            executed.update(intensity)
        return executed

    def _normalised_intensity(
        self, fault_type: Any, spec: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Normalise executed native params into the Controller's contract.

        Uses the mapping lifted out of the shim so the black-box path reports
        intensity exactly as the in-process path did, including whether the
        value was the Agent's own or a ChaosBlade default.
        """
        params = spec.get("params")
        if not isinstance(params, Mapping):
            return {}
        # BladeAI names the fault by its own scope/target/action triple
        # ("pod"/"cpu"/"load"); the intensity table is keyed by the Stage-2
        # name ("cpu-load"), so normalise through the shim's own mapping.
        try:
            fault_type, action = canonical_fault_type(
                str(spec.get("scope") or ""),
                str(spec.get("fault_target") or ""),
                str(spec.get("fault_action") or ""),
            )
        except BladeShimError:
            return {"intensity": {}, "intensity_source": "unmappable",
                    "native_params": dict(params)}
        flags = {f"--{str(key).replace('_', '-')}": value for key, value in params.items()
                 if str(key) not in {"timeout", "duration"}}
        try:
            intensity = canonical_native_intensity(fault_type, dict(flags), action=action)
            source = native_intensity_source(fault_type, dict(flags), action=action)
        except BladeShimError:
            # Not representable by Controller policy.  This is a real finding,
            # not a gap: ``--cpu-percent 80 --cpu-count 1`` means "80% of one
            # core", while the Controller's ``cpu_percent`` alone would read as
            # "80% of the Pod".  Collapsing the two would misstate the blast
            # radius -- the very ambiguity D1 flagged about "80%".  Report the
            # native parameters verbatim and let a human read them.
            return {"intensity": {}, "intensity_source": "unmappable",
                    "native_params": dict(params)}
        return {"intensity": intensity, "intensity_source": source}

    def _canonical_tool_name(self, name: str) -> str:
        """Normalise into ``server.tool`` so MCP allow-listing can read it."""
        tool = normalize_tool_name(name)
        if not tool:
            return f"bladeai.{name}"
        if "__" in tool and "." not in tool:
            server, _, tool_name = tool.partition("__")
            tool = normalize_tool_name(tool_name, server) or tool
        if "." not in tool:
            tool = f"bladeai.{tool}"
        return tool


def _loads_object(text: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


# ``Error:`` is how every failing built-in tool opens its output -- 103 of the
# corpus's 1,133 results, all of them genuine failures.
_ERROR_PREFIXES: tuple[str, ...] = ("error:", "error ", "failed:", "failure:")


def _is_error_text(text: str) -> bool:
    return text.strip().lower().startswith(_ERROR_PREFIXES)


def _error_block(message: Any, code: Any) -> dict[str, Any]:
    """Give a native failure the error code the platform classifies on.

    ``status_from_payload`` reads ``error.code`` against PERMISSION_ERROR_CODES,
    so a kubectl ``Forbidden`` has to arrive as ``forbidden`` to be classified
    as denied rather than as a generic failure -- which is what the other three
    Harnesses get for free from the MCP gateway's structured errors.
    """
    text = str(message or "")
    lowered = text.lower()
    derived: str | None = None
    if "forbidden" in lowered or "cannot get resource" in lowered:
        derived = "forbidden"
    elif "unauthorized" in lowered or "401" in lowered:
        derived = "unauthorized"
    elif "notfound" in lowered or "not found" in lowered:
        derived = "not_found"
    block: dict[str, Any] = {"message": text}
    if code is not None:
        block["native_code"] = code
    if derived is not None:
        block["code"] = derived
    return block
