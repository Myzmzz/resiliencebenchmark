"""WP-C.3: a turn that ends with an unanswered question gets another turn.

Finding F10, reproduced three ways: in the corpus (11 of 17 cases have a turn
that ends on ``done`` carrying no gate event at all), in the L0 fixture (turn 1
of 5), and live on 2026-09-12.  BladeAI asks its clarifying questions as
ordinary text and then ends the turn, so a platform that only listens for
protocol-level interrupts waits forever -- the turn looks finished while the
task has not moved.

The platform already extracts such questions (``simulated_user.interpret``) and
already answers them (``simulated_user.reply``).  What was missing for BladeAI
is the ability to *deliver* the answer: ``HarnessSession`` drops queued
feedback unless the Harness is resumable, and BladeAI had no resume shims.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from harness.bladeai_http import (
    BladeAIHttpClient,
    bladeai_http_resume_argv_builder,
    bladeai_http_session_id_provider,
    bladeai_http_turn_executor,
)
from harness.bladeai_http.protocol import frame_to_event, iter_sse_frames
from stage2_service.contracts import FeedbackCategory
from stage2_service.session import HarnessSession

GOLDEN = Path(__file__).parent / "fixtures" / "harness_streams" / "golden"


def sse(*events: dict) -> bytes:
    return b"".join(
        f"data: {json.dumps(e)}\n\n".encode("utf-8") for e in events
    )


# A turn shaped like L0's first: it asks in plain text, then ends.  No confirm
# event, no interrupt -- only ``done``.
ASKING_TURN = sse(
    {"type": "node_start", "node": "intent_clarification", "task_id": "turn-1"},
    {"type": "token", "content": "请问要打 cart 的哪一个 Pod？A 单副本，B 全部。"},
    {"type": "done", "task_id": "turn-1"},
)
ACTING_TURN = sse(
    {"type": "node_start", "node": "execute_loop", "task_id": "turn-2"},
    {"type": "tool_start", "tool_name": "kubectl_read", "call_id": "c-1", "task_id": "turn-2"},
    {"type": "tool_end", "call_id": "c-1", "content": "ok", "task_id": "turn-2"},
    {"type": "token", "content": "收到，按 A 执行。"},
    {"type": "done", "task_id": "turn-2"},
)


class ReplayServer:
    def __init__(self, *turns: bytes):
        self.turns = list(turns)
        self.served = 0
        self.turn_bodies: list[dict] = []
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/turn"):
            self.turn_bodies.append(json.loads(request.content))
            body = self.turns[min(self.served, len(self.turns) - 1)]
            self.served += 1
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, content=iter([body])
            )
        if request.url.path == "/api/v1/sessions":
            return httpx.Response(200, json={"session_id": "sess_followup"})
        return httpx.Response(200, json={})

    def client(self) -> BladeAIHttpClient:
        return BladeAIHttpClient(
            "http://blade.test",
            http_client=httpx.Client(transport=self.transport, base_url="http://blade.test"),
        )


def session_for(server: ReplayServer, observer, *, resumable: bool = True) -> HarnessSession:
    client = server.client()
    session_id = client.create_session()
    return HarnessSession(
        argv=["blade-ai", "server"], stdin="初始题面".encode("utf-8"), env={},
        timeout_seconds=60, stdout_line_observer=observer,
        resume_argv_builder=bladeai_http_resume_argv_builder() if resumable else None,
        session_id_provider=bladeai_http_session_id_provider(session_id) if resumable else None,
        turn_executor=bladeai_http_turn_executor(client, session_id),
    )


def answer_plain_text_question(line: bytes):
    """Stand-in for interpret() + reply(): a plain-text question gets an answer."""
    event = json.loads(line)
    if event.get("type") == "token" and "请问" in str(event.get("content") or ""):
        return {
            "category": FeedbackCategory.USER_DECISION.value,
            "message": "选 A：只打 cart 的单个 Pod。",
            "payload": {"topic": "target_choice"},
        }
    return None


def test_a_plain_text_question_gets_answered_in_a_new_turn() -> None:
    server = ReplayServer(ASKING_TURN, ACTING_TURN)
    observed: list[bytes] = []

    def observe(line: bytes):
        observed.append(line)
        return answer_plain_text_question(line)

    result = session_for(server, observe).start().wait()

    assert result.returncode == 0
    # Two turns were posted: the original prompt, then the answer.
    assert len(server.turn_bodies) == 2
    assert server.turn_bodies[0]["input"] == "初始题面"
    assert "选 A" in server.turn_bodies[1]["input"]
    # And the Agent got on with the task in that second turn.
    assert any(json.loads(l).get("tool_name") == "kubectl_read" for l in observed)


def test_without_resume_the_answer_is_silently_dropped() -> None:
    """The exact failure this work package fixes.

    Same stream, same answer -- but with no resume shims ``HarnessSession``
    cannot deliver it, so the task never moves and the turn still looks like a
    clean success.
    """
    server = ReplayServer(ASKING_TURN, ACTING_TURN)
    result = session_for(server, answer_plain_text_question, resumable=False).start().wait()

    assert result.returncode == 0          # looks fine
    assert len(server.turn_bodies) == 1    # but the question was never answered


def test_the_followup_turn_reuses_the_same_session_id() -> None:
    server = ReplayServer(ASKING_TURN, ACTING_TURN)
    records: list[dict] = []
    client = server.client()
    session_id = client.create_session()
    session = HarnessSession(
        argv=["blade-ai", "server"], stdin="初始题面".encode("utf-8"), env={},
        timeout_seconds=60, stdout_line_observer=answer_plain_text_question,
        resume_argv_builder=bladeai_http_resume_argv_builder(),
        session_id_provider=bladeai_http_session_id_provider(session_id),
        turn_executor=bladeai_http_turn_executor(client, session_id),
        record_observer=records.append,
    )
    session.start().wait()

    captured = next(r for r in records if r["event"] == "SESSION_ID_CAPTURED")
    assert captured["payload"]["session_id"] == "sess_followup"
    delivered = [r for r in records if r["event"] == "FEEDBACK_DELIVERED"]
    assert len(delivered) == 1
    assert delivered[0]["payload"]["category"] == FeedbackCategory.USER_DECISION.value
    assert delivered[0]["payload"]["status"] == "delivered"


def test_l0_turn_one_really_does_end_without_a_gate() -> None:
    """The fixture backs the premise: F10 is not hypothetical."""
    wire = (GOLDEN / "bladeai_L0.sse").read_bytes()
    events = [frame_to_event(f) for f in iter_sse_frames([wire])]
    first_turn: list[dict] = []
    for event in events:
        first_turn.append(event)
        if event.get("type") == "done":
            break
    assert first_turn[-1]["type"] == "done"
    assert not any(e.get("type") == "confirm" for e in first_turn)
    # It did talk -- it just talked instead of raising a gate.
    assert any(e.get("type") == "token" for e in first_turn)


def test_a_turn_that_raises_a_gate_needs_no_followup_text_turn() -> None:
    """Only plain-text questions need this path; gates have their own channel."""
    gated = sse(
        {"type": "node_start", "node": "intent_clarification", "task_id": "turn-9"},
        {"type": "confirm", "node": "intent_confirm", "task_id": "turn-9",
         "content": "Fault type: pod-cpu-load"},
        {"type": "done", "task_id": "turn-9"},
    )
    server = ReplayServer(gated, ACTING_TURN)
    session_for(server, answer_plain_text_question).start().wait()
    assert len(server.turn_bodies) == 1
