"""WP-C.4: stall detection and error attribution from the raw event log.

The two upstream-failure fixtures are real captures made on 2026-09-12 for
exactly this purpose: the corpus had no genuine "upstream model error" sample,
only the evaluator's own cancellations.
"""

from __future__ import annotations

import json
from pathlib import Path

from stage2_service.harness_adapters.bladeai_diagnosis import (
    assess_stall,
    diagnose_turn,
    unanswered_questions,
)

GOLDEN = Path(__file__).parent / "fixtures" / "harness_streams" / "golden"


def load(name: str) -> list[dict]:
    return [json.loads(line) for line in (GOLDEN / name).read_text().splitlines()]


def event(kind: str, **fields) -> dict:
    return {"kind": "event", "received_at": "2026-09-12T00:00:00+00:00",
            "event": {"type": kind, **fields}}


def driver(action: str, **detail) -> dict:
    return {"kind": "driver", "received_at": "2026-09-12T00:00:00+00:00",
            "action": action, "detail": detail}


# ---- upstream failure, from the real captures ---------------------------


def test_upstream_auth_failure_is_invalid_not_a_zero_score() -> None:
    diagnosis = diagnose_turn(load("bladeai_upstream_auth_failure.events.jsonl"))
    assert diagnosis.outcome == "upstream_failure"
    assert diagnosis.valid_for_scoring is False
    # The captured turn really did end on ``done`` with no error event at all.
    assert diagnosis.detail["terminal_event"] == "done"
    assert diagnosis.detail["productive_events"] == 0
    assert diagnosis.detail["llm_start_count"] >= 2


def test_upstream_connection_failure_is_classified_the_same_way() -> None:
    diagnosis = diagnose_turn(load("bladeai_upstream_conn_failure.events.jsonl"))
    assert diagnosis.outcome == "upstream_failure"
    assert diagnosis.valid_for_scoring is False


def test_both_captures_would_look_like_success_on_the_terminal_event_alone() -> None:
    """Why the terminal event cannot be the judgement: it says ``done``."""
    for name in ("bladeai_upstream_auth_failure.events.jsonl",
                 "bladeai_upstream_conn_failure.events.jsonl"):
        records = load(name)
        events = [r["event"] for r in records if r["kind"] == "event"]
        assert events[-1]["type"] == "done"
        assert not any(e["type"] == "error" for e in events)
        # Seven events, and none of them productive.
        assert len(events) == 7


# ---- attribution --------------------------------------------------------


def test_our_own_cancel_is_attributed_to_the_platform() -> None:
    """All 10 error events in the corpus were the evaluator's own /cancel."""
    diagnosis = diagnose_turn([
        driver("turn_started"),
        event("node_start", node="agent_loop"),
        event("thinking", content="..."),
        driver("cancel_requested", reason="controller_cancelled"),
        event("error", content="Turn cancelled"),
        event("done"),
    ])
    assert diagnosis.outcome == "platform_cancelled"
    assert diagnosis.valid_for_scoring is False
    assert diagnosis.detail["cancel_reasons"] == ["controller_cancelled"]


def test_an_error_we_did_not_cause_is_attributed_to_the_agent() -> None:
    diagnosis = diagnose_turn([
        driver("turn_started"),
        event("node_start"), event("llm_start"), event("token", content="工作中"),
        event("error", content="internal planner failure"),
    ])
    assert diagnosis.outcome == "agent_error"
    assert diagnosis.valid_for_scoring is True
    assert diagnosis.detail["message"] == "internal planner failure"


def test_a_normal_turn_is_completed_and_scorable() -> None:
    diagnosis = diagnose_turn([
        driver("turn_started"),
        event("node_start"), event("llm_start"),
        event("token", content="做完了"), event("tool_start", tool_name="kubectl_read"),
        event("tool_end", call_id="c"), event("done"),
    ])
    assert diagnosis.outcome == "completed"
    assert diagnosis.valid_for_scoring is True


def test_a_cut_stream_is_truncated_not_completed() -> None:
    """D6-B's shape: the stream stopped with no terminal event."""
    diagnosis = diagnose_turn([
        driver("turn_started"), event("node_start"),
        event("tool_start", tool_name="blade_create", call_id="c"),
    ])
    assert diagnosis.outcome == "truncated"
    assert diagnosis.valid_for_scoring is False


def test_retries_are_not_mistaken_for_productivity() -> None:
    """``llm_start`` fires per attempt, so a retry storm looks busy."""
    records = [driver("turn_started"), event("node_start")]
    records += [event("llm_start") for _ in range(3)]
    records += [event("node_end"), event("done")]
    assert diagnose_turn(records).outcome == "upstream_failure"


def test_one_llm_start_with_real_output_is_a_normal_turn() -> None:
    diagnosis = diagnose_turn([
        driver("turn_started"), event("node_start"), event("llm_start"),
        event("thinking", content="思考"), event("node_end"), event("done"),
    ])
    assert diagnosis.outcome == "completed"


# ---- stall detection ----------------------------------------------------


def test_stall_is_judged_on_raw_event_arrival() -> None:
    """P2 went silent for 18 minutes with the connection alive and no error."""
    assert assess_stall(1080.0, limit_seconds=600).stalled is True
    assert assess_stall(30.0, limit_seconds=600).stalled is False


def test_no_event_yet_is_not_a_stall() -> None:
    verdict = assess_stall(None, limit_seconds=600)
    assert verdict.stalled is False and verdict.silent_seconds == 0.0


def test_the_limit_widens_while_a_gate_answer_is_in_flight() -> None:
    """/confirm blocks for tens of seconds -- 172 s in the worst observed case."""
    assert assess_stall(120.0, limit_seconds=60).stalled is True
    assert assess_stall(120.0, limit_seconds=60, awaiting_gate=True).stalled is False
    # Even the widened grace ends somewhere.
    assert assess_stall(400.0, limit_seconds=60, awaiting_gate=True).stalled is True


def test_stall_verdict_explains_itself() -> None:
    verdict = assess_stall(700.0, limit_seconds=600)
    assert "700s" in verdict.reason and "600s" in verdict.reason


# ---- unanswered gates ---------------------------------------------------


def test_an_unanswered_gate_is_reported() -> None:
    """Missing one leaves the Agent waiting silently for six hours."""
    records = [
        event("confirm", node="intent_confirm", task_id="turn-1"),
        event("confirm", node="confirmation_gate", task_id="turn-1"),
    ]
    pending = unanswered_questions(records, answered_ids=[])
    assert {p["node"] for p in pending} == {"intent_confirm", "confirmation_gate"}
    assert unanswered_questions(records, answered_ids=["turn-1"]) == []
