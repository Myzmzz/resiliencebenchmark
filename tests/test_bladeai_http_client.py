"""Black-box HTTP/SSE driver against a simulated BladeAI 0.7.0 server.

The fake server reproduces the published contract and the behaviours the
2026-09-11 live run recorded: a turn that streams events and ends on ``done``,
an execution gate keyed by ``task_id`` that blocks before returning, an intent
gate that can answer ``delivered=False``, and a server-wide cancel whose only
visible trace on the stream is an ``error`` event.

No BladeAI installation, cluster or model gateway is involved.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from harness.bladeai_http import (
    BladeAIHttpClient,
    BladeAIHttpError,
    EventLog,
    bladeai_http_streaming_runner,
    bladeai_http_turn_executor,
)


def sse(*events: dict) -> list[bytes]:
    return [
        f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode("utf-8")
        for event in events
    ]


# One L0-shaped turn: target read, plan, both gates, injection, report, done.
L0_EVENTS: tuple[dict, ...] = (
    {"type": "node_start", "node": "intent_clarification"},
    {"type": "llm_start", "node": "intent_clarification"},
    {"type": "thinking", "text": "resolve the target pod"},
    {"type": "token", "text": "Plan: burn one core on otel-demo/cart."},
    {"type": "node_end", "node": "intent_clarification"},
    {"type": "confirm", "node": "confirmation_gate", "task_id": "task-l0"},
    {"type": "tool_start", "tool_name": "blade_create", "call_id": "call-1"},
    {"type": "tool_end", "call_id": "call-1", "status": "ok"},
    {"type": "context_size", "tokens": 8241},
    {"type": "usage", "input_tokens": 8241, "output_tokens": 512},
    {"type": "result", "fault_spec": {"names": [], "labels": {"app": "cart"}}},
    {"type": "done"},
)


class FakeBladeAIServer:
    """Minimal stand-in wired to an ``httpx.MockTransport``."""

    def __init__(
        self,
        events: tuple[dict, ...] = L0_EVENTS,
        *,
        chunk_delay: float = 0.0,
        confirm_block_seconds: float = 0.0,
        interrupt_delivered: bool = True,
        stall_forever_after: int | None = None,
    ):
        self.events = events
        self.chunk_delay = chunk_delay
        self.confirm_block_seconds = confirm_block_seconds
        self.interrupt_delivered = interrupt_delivered
        self.stall_forever_after = stall_forever_after
        self.requests: list[tuple[str, str]] = []
        self.bodies: list[dict] = []
        self.cancelled = threading.Event()
        self.transport = httpx.MockTransport(self._handle)

    def client(self, **kwargs) -> BladeAIHttpClient:
        http = httpx.Client(transport=self.transport, base_url="http://blade.test")
        return BladeAIHttpClient("http://blade.test", http_client=http, **kwargs)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.requests.append((request.method, path))
        if request.content:
            self.bodies.append(json.loads(request.content))
        if path == "/api/v1/sessions" and request.method == "POST":
            return httpx.Response(200, json={"session_id": "sess_l0"})
        if path.endswith("/turn"):
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=self._stream(),
            )
        if path.endswith("/cancel"):
            self.cancelled.set()
            return httpx.Response(200, json={"cancelled": True})
        if path.endswith("/interrupt"):
            return httpx.Response(200, json={"delivered": self.interrupt_delivered})
        if path.startswith("/api/v1/confirm/"):
            time.sleep(self.confirm_block_seconds)
            return httpx.Response(200, json={"status": "resumed"})
        if path.endswith("/state"):
            return httpx.Response(200, json={"status": "running"})
        return httpx.Response(404, json={"error": "no route"})

    def _stream(self) -> Iterator[bytes]:
        for index, chunk in enumerate(sse(*self.events)):
            if self.chunk_delay:
                time.sleep(self.chunk_delay)
            yield chunk
            if self.stall_forever_after is not None and index + 1 >= self.stall_forever_after:
                # Alive, connected, and silent -- the W2 / D8-A shape.  Ends
                # only once the platform calls the server-wide cancel.
                while not self.cancelled.wait(timeout=0.05):
                    continue
                yield from sse({"type": "error", "message": "Turn cancelled"})
                return


def test_full_turn_forwards_every_event_as_one_json_line() -> None:
    server = FakeBladeAIServer()
    observed: list[bytes] = []
    with server.client() as client:
        session_id = client.create_session()
        result = client.run_turn(
            session_id, "inject cpu load", timeout_seconds=30, observe=observed.append
        )

    assert session_id == "sess_l0"
    assert result.returncode == 0
    assert result.terminal_event == "done"
    assert not (result.timed_out or result.cancelled or result.output_truncated)
    assert result.event_count == len(L0_EVENTS)
    # Every line is one complete JSON object, in order, newline-terminated.
    assert all(line.endswith(b"\n") for line in observed)
    assert [json.loads(line)["type"] for line in observed] == [
        event["type"] for event in L0_EVENTS
    ]
    # stdout is the same stream, so the subprocess and HTTP transports hand the
    # adapter byte-identical input.
    assert result.stdout == b"".join(observed)


def test_event_log_lands_the_raw_stream_with_receive_timestamps(tmp_path: Path) -> None:
    log_path = tmp_path / "bladeai-raw-events.jsonl"
    server = FakeBladeAIServer()
    with server.client(event_log=EventLog(log_path)) as client:
        client.run_turn(client.create_session(), "go", timeout_seconds=30)

    records = [json.loads(line) for line in log_path.read_text().splitlines()]
    events = [row for row in records if row["kind"] == "event"]
    assert [row["event"]["type"] for row in events] == [e["type"] for e in L0_EVENTS]
    # Every event carries an arrival timestamp and a monotonic sequence number.
    assert all(row["received_at"] and row["elapsed_ms"] >= 0 for row in events)
    assert [row["seq"] for row in records] == sorted(row["seq"] for row in records)
    assert all(row["session_id"] == "sess_l0" and row["turn"] == 1 for row in events)
    # The SSE frame name is preserved next to the event, so the ``type``
    # normalisation in frame_to_event is always reversible.
    assert events[0]["sse_event"] == "node_start"


def test_driver_actions_share_the_event_timeline_for_error_attribution(
    tmp_path: Path,
) -> None:
    """Finding F14: an ``error`` event may be our cancel, not an agent failure."""
    log_path = tmp_path / "events.jsonl"
    server = FakeBladeAIServer(stall_forever_after=3)
    cancel_flag = threading.Event()
    with server.client(event_log=EventLog(log_path)) as client:
        def observe(line: bytes) -> None:
            if json.loads(line)["type"] == "thinking":
                cancel_flag.set()

        result = client.run_turn(
            client.create_session(),
            "go",
            timeout_seconds=30,
            observe=observe,
            cancel_requested=cancel_flag.is_set,
        )

    assert result.cancelled and not result.timed_out
    records = [json.loads(line) for line in log_path.read_text().splitlines()]
    ordered = [
        (row["kind"], row.get("action") or row["event"]["type"]) for row in records
    ]
    # Our cancellation is recorded before the error it produces, so a later
    # stage can attribute the error to the platform instead of to the agent.
    assert ("driver", "cancel_requested") in ordered
    assert ordered.index(("driver", "cancel_requested")) < ordered.index(
        ("event", "error")
    )
    cancel_record = next(r for r in records if r.get("action") == "cancel_requested")
    assert cancel_record["detail"]["reason"] == "controller_cancelled"


def test_turn_deadline_cancels_and_reports_timed_out(tmp_path: Path) -> None:
    server = FakeBladeAIServer(stall_forever_after=1)
    with server.client(event_log=EventLog(tmp_path / "e.jsonl")) as client:
        result = client.run_turn(client.create_session(), "go", timeout_seconds=1)

    assert result.timed_out and result.cancelled
    assert result.returncode == 1
    assert server.cancelled.is_set()


def test_last_event_time_is_the_stall_signal_not_the_request_return() -> None:
    server = FakeBladeAIServer()
    with server.client() as client:
        assert client.last_event_at is None
        assert client.seconds_since_last_event() is None
        client.run_turn(client.create_session(), "go", timeout_seconds=30)
        assert client.last_event_at is not None
        assert client.seconds_since_last_event() < 30


def test_error_terminal_event_ends_the_turn_with_a_failure_code() -> None:
    server = FakeBladeAIServer(events=(
        {"type": "node_start", "node": "agent_loop"},
        {"type": "error", "message": "upstream model returned 400"},
    ))
    with server.client() as client:
        result = client.run_turn(client.create_session(), "go", timeout_seconds=30)

    assert result.terminal_event == "error"
    assert result.returncode == 1
    assert not result.cancelled and not result.timed_out


def test_output_limit_truncates_and_cancels_rather_than_running_unbounded() -> None:
    server = FakeBladeAIServer(stall_forever_after=6)
    with server.client() as client:
        result = client.run_turn(
            client.create_session(), "go", timeout_seconds=30, output_limit_bytes=200
        )

    assert result.output_truncated
    assert len(result.stdout) <= 200
    assert server.cancelled.is_set()


def test_malformed_frame_is_recorded_without_abandoning_the_stream(
    tmp_path: Path,
) -> None:
    log_path = tmp_path / "e.jsonl"
    server = FakeBladeAIServer()
    server.transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=iter([
                b"data: not json at all\n\n",
                b'data: {"type":"token","text":"ok"}\n\n',
                b'data: {"type":"done"}\n\n',
            ]),
        )
        if request.url.path.endswith("/turn")
        else httpx.Response(200, json={"session_id": "s"})
    )
    with server.client(event_log=EventLog(log_path)) as client:
        result = client.run_turn("s", "go", timeout_seconds=30)

    assert result.terminal_event == "done"
    assert result.event_count == 2  # the malformed frame is not counted as evidence
    records = [json.loads(line) for line in log_path.read_text().splitlines()]
    assert any(row.get("action") == "malformed_event" for row in records)


def test_confirm_requires_a_published_action_and_records_how_long_it_blocked(
    tmp_path: Path,
) -> None:
    log_path = tmp_path / "e.jsonl"
    server = FakeBladeAIServer(confirm_block_seconds=0.25)
    with server.client(event_log=EventLog(log_path)) as client:
        with pytest.raises(BladeAIHttpError, match="confirm action"):
            client.confirm_task("task-l0", "approved")
        body = client.confirm_task("task-l0", "approve", reason="within scope")

    assert body == {"status": "resumed"}
    assert ("POST", "/api/v1/confirm/task-l0") in server.requests
    assert server.bodies[-1] == {"action": "approve", "reason": "within scope"}
    records = [json.loads(line) for line in log_path.read_text().splitlines()]
    blocked = next(r for r in records if r.get("action") == "confirm_result")
    assert blocked["detail"]["blocked_seconds"] >= 0.25


def test_interrupt_answer_is_sent_verbatim_and_delivery_is_recorded(
    tmp_path: Path,
) -> None:
    log_path = tmp_path / "e.jsonl"
    server = FakeBladeAIServer(interrupt_delivered=False)
    with server.client(event_log=EventLog(log_path)) as client:
        body = client.answer_interrupt("sess_l0", "int-1", "approved")

    # delivered=False is the documented fallback signal, not an exception.
    assert body == {"delivered": False}
    assert server.bodies[-1] == {"interrupt_id": "int-1", "answer": "approved"}
    records = [json.loads(line) for line in log_path.read_text().splitlines()]
    sent = next(r for r in records if r.get("action") == "interrupt_answer_sent")
    assert sent["detail"]["is_approval_word"] is True
    result = next(r for r in records if r.get("action") == "interrupt_answer_result")
    assert result["detail"]["delivered"] is False


def test_non_whitelist_answer_is_flagged_but_still_sent_unchanged(
    tmp_path: Path,
) -> None:
    """The driver never rewrites an answer; it records that F1 will bite."""
    log_path = tmp_path / "e.jsonl"
    server = FakeBladeAIServer()
    with server.client(event_log=EventLog(log_path)) as client:
        client.answer_interrupt("sess_l0", "int-1", "approved, use --cpu-count 1")

    assert server.bodies[-1]["answer"] == "approved, use --cpu-count 1"
    records = [json.loads(line) for line in log_path.read_text().splitlines()]
    sent = next(r for r in records if r.get("action") == "interrupt_answer_sent")
    assert sent["detail"]["is_approval_word"] is False


def test_http_errors_surface_as_driver_errors_not_silent_empty_streams() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(503, json={"error": "server starting"})
    )
    client = BladeAIHttpClient(
        "http://blade.test",
        http_client=httpx.Client(transport=transport, base_url="http://blade.test"),
    )
    with pytest.raises(BladeAIHttpError, match="HTTP 503"):
        client.create_session()


def test_session_creation_without_an_id_is_an_explicit_failure() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True}))
    client = BladeAIHttpClient(
        "http://blade.test",
        http_client=httpx.Client(transport=transport, base_url="http://blade.test"),
    )
    with pytest.raises(BladeAIHttpError, match="no session id"):
        client.create_session()


def test_streaming_runner_refuses_session_level_kwargs() -> None:
    server = FakeBladeAIServer()
    with server.client() as client:
        with pytest.raises(TypeError, match="single-turn transport"):
            bladeai_http_streaming_runner(
                client, "sess_l0", "go", 30, lambda line: None,
                resume_argv_builder=lambda *a: [],
            )


def test_turn_options_are_omitted_unless_set() -> None:
    server = FakeBladeAIServer()
    with server.client() as client:
        client.run_turn("sess_l0", "go", timeout_seconds=30)
        assert server.bodies[-1] == {"input": "go", "permission_mode": "confirm"}
        client.run_turn(
            "sess_l0", "go", timeout_seconds=30, dry_run=True, planning_mode="deep"
        )
        assert server.bodies[-1] == {
            "input": "go",
            "permission_mode": "confirm",
            "dry_run": True,
            "planning_mode": "deep",
        }


def test_turn_executor_passes_the_prompt_through_as_stdin() -> None:
    server = FakeBladeAIServer()
    observed: list[bytes] = []
    with server.client() as client:
        execute = bladeai_http_turn_executor(client, "sess_l0", planning_mode="deep")
        result = execute(
            ["unused", "argv"], b"inject cpu load", {}, 30, observed.append, lambda: False
        )

    assert result.returncode == 0
    assert server.bodies[-1]["input"] == "inject cpu load"
    assert server.bodies[-1]["planning_mode"] == "deep"
    assert len(observed) == len(L0_EVENTS)
