"""Tell a stalled turn from a finished one, and say who caused an ending.

Both judgements are made from the raw event log the driver writes
(:class:`harness.bladeai_http.EventLog`), never from a request's return value.
Two measurements force that:

* ``POST /confirm/{task_id}`` returns ``{"status":"success"}`` even for a
  ``task_id`` that does not exist, so a successful answer proves nothing about
  whether the pipeline moved.
* A turn can go silent for 18 minutes (P2) or 10 minutes (D8-A) with the
  connection alive and no error at all.

And the attribution judgement cannot key on the ``error`` event:

* All 10 ``error`` events in the corpus read ``Turn cancelled`` -- every one
  was the evaluator's own ``/cancel``, none was the Agent failing.
* An upstream model failure produces **no ``error`` event at all**.  Measured
  on 2026-09-12 with an invalid key and with an unreachable gateway: both
  ended on ``done`` with returncode 0 after seven events, in under three
  seconds.  Reading the terminal event alone would score that as the Agent
  doing nothing, when the run is simply invalid.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

TurnOutcome = Literal[
    "completed",            # the Agent finished the turn itself
    "platform_cancelled",   # we pulled the plug
    "upstream_failure",     # the model gateway failed; the Trial is invalid
    "agent_error",          # the Agent itself reported an error
    "truncated",            # the stream ended without a terminal event
]

# Events that prove the pipeline is producing.  ``llm_start`` is excluded on
# purpose: it is emitted per attempt, so a retry storm looks busy while
# producing nothing at all.
PRODUCTIVE_EVENT_TYPES: frozenset[str] = frozenset({
    "token", "thinking", "tool_start", "tool_end", "node_message",
    "confirm", "result",
})

# Retries exhausted inside one node with no output is the upstream-failure
# signature; ``resilient_llm`` tries three times.
UPSTREAM_RETRY_THRESHOLD = 2


@dataclass(frozen=True)
class TurnDiagnosis:
    outcome: TurnOutcome
    reason: str
    valid_for_scoring: bool
    detail: dict[str, Any]


def diagnose_turn(records: Sequence[Mapping[str, Any]]) -> TurnDiagnosis:
    """Classify one turn from the driver's own event log.

    ``records`` are the log's rows: ``kind="event"`` rows carry the server's
    payload under ``event``; ``kind="driver"`` rows are what this platform did.
    Keeping both on one timeline is what makes attribution possible.
    """
    events = [r["event"] for r in records
              if r.get("kind") == "event" and isinstance(r.get("event"), Mapping)]
    driver = [r for r in records if r.get("kind") == "driver"]
    types = [str(e.get("type") or "") for e in events]
    cancelled_by_us = any(r.get("action") == "cancel_requested" for r in driver)
    terminal = next((t for t in reversed(types) if t in {"done", "error"}), None)

    if cancelled_by_us:
        return TurnDiagnosis(
            outcome="platform_cancelled",
            reason="the platform issued /cancel before this turn ended",
            # Our own intervention is never the Agent's failure.
            valid_for_scoring=False,
            detail={"cancel_reasons": [r.get("detail", {}).get("reason") for r in driver
                                       if r.get("action") == "cancel_requested"],
                    "terminal_event": terminal},
        )

    productive = sum(1 for t in types if t in PRODUCTIVE_EVENT_TYPES)
    llm_starts = types.count("llm_start")
    if productive == 0 and llm_starts >= UPSTREAM_RETRY_THRESHOLD:
        return TurnDiagnosis(
            outcome="upstream_failure",
            reason=(f"{llm_starts} llm_start events produced no output: the model "
                    "gateway failed and the turn ended without an error event"),
            valid_for_scoring=False,
            detail={"llm_start_count": llm_starts, "productive_events": 0,
                    "terminal_event": terminal},
        )

    if terminal == "error":
        message = next((str(e.get("content") or e.get("message") or "")
                        for e in reversed(events) if e.get("type") == "error"), "")
        # Nothing in the corpus produced this, so it is reported as-is rather
        # than being folded into a category it has never been observed in.
        return TurnDiagnosis(
            outcome="agent_error", reason=message or "the Agent reported an error",
            valid_for_scoring=True,
            detail={"message": message, "terminal_event": "error"},
        )
    if terminal == "done":
        return TurnDiagnosis(
            outcome="completed", reason="the turn ended on its own",
            valid_for_scoring=True,
            detail={"productive_events": productive, "terminal_event": "done"},
        )
    return TurnDiagnosis(
        outcome="truncated",
        reason="the stream ended without a terminal event",
        valid_for_scoring=False,
        detail={"productive_events": productive, "terminal_event": None},
    )


@dataclass(frozen=True)
class StallVerdict:
    stalled: bool
    silent_seconds: float
    reason: str


def assess_stall(
    seconds_since_last_event: float | None,
    *,
    limit_seconds: float,
    awaiting_gate: bool = False,
    gate_grace_seconds: float = 300.0,
) -> StallVerdict:
    """Decide whether a live turn has stopped producing.

    ``seconds_since_last_event`` must come from the raw stream's arrival clock
    (``BladeAIHttpClient.seconds_since_last_event``).  A tool log looks busy
    while nothing is produced, and a confirm call returns success regardless.

    ``awaiting_gate`` widens the limit because answering the execution gate
    legitimately blocks the stream: 13.8 s and 28.7 s were typical in the live
    run and 172 s was the worst case.
    """
    if seconds_since_last_event is None:
        return StallVerdict(False, 0.0, "no event has arrived yet")
    effective = max(limit_seconds, gate_grace_seconds) if awaiting_gate else limit_seconds
    if seconds_since_last_event <= effective:
        return StallVerdict(False, seconds_since_last_event,
                            "the stream is still producing")
    return StallVerdict(
        True, seconds_since_last_event,
        f"no raw event for {seconds_since_last_event:.0f}s (limit {effective:.0f}s"
        + (", widened while a gate answer is in flight" if awaiting_gate else "") + ")",
    )


def unanswered_questions(
    records: Iterable[Mapping[str, Any]], answered_ids: Iterable[str]
) -> list[dict[str, Any]]:
    """Gates that arrived but were never answered.

    Missing one leaves the Agent waiting silently for six hours.
    """
    done = set(answered_ids)
    pending: dict[str, dict[str, Any]] = {}
    for row in records:
        if row.get("kind") != "event":
            continue
        event = row.get("event")
        if not isinstance(event, Mapping) or event.get("type") != "confirm":
            continue
        task_id = str(event.get("task_id") or "")
        if task_id and task_id not in done:
            pending[f"{task_id}:{event.get('node')}"] = {
                "task_id": task_id, "node": event.get("node"),
                "received_at": row.get("received_at"),
            }
    return list(pending.values())
