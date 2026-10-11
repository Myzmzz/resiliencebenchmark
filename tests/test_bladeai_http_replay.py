"""WP-A acceptance: a real BladeAI L0 session lands complete, with arrival times.

Replays the recorded 2026-09-11 L0 session (``sess_8a686003edf5``) through the
black-box HTTP/SSE driver.  This is the transport claim only -- every event the
server sent is forwarded once, in order, and lands on disk with the time it
arrived.  Semantic mapping to CanonicalEvent is WP-B and is not asserted here.

The fixture is a whole session, not one turn: L0 took five turns, and one
``POST /turn`` returns one turn's stream, terminated by that turn's ``done``.
The fake server below hands out the next turn on each request, which is what
the real server does.
"""

from __future__ import annotations

import collections
import json
from pathlib import Path

import httpx
import pytest

from harness.bladeai_http import BladeAIHttpClient, EventLog, bladeai_http_turn_executor
from harness.bladeai_http.protocol import frame_to_event, iter_sse_frames
from stage2_service.session import HarnessSession

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "harness_streams" / "golden"
L0_SSE = FIXTURE_DIR / "bladeai_L0.sse"
L0_RECV = FIXTURE_DIR / "bladeai_L0.recv.jsonl"

SESSION_EVENTS = 582
# Per-turn event counts, read straight off the fixture.  Turn 1 ends on ``done``
# with no confirm event at all: that is finding F10 -- the agent's clarifying
# question was plain text, so a platform waiting for a protocol interrupt would
# wait forever.
TURN_EVENT_COUNTS = [100, 16, 35, 17, 414]
SESSION_TYPES = {
    "tool_start": 99, "tool_end": 99, "context_size": 66, "node_start": 65,
    "node_end": 61, "usage": 49, "llm_start": 48, "thinking": 47,
    "node_message": 22, "token": 16, "done": 5, "confirm": 4, "result": 1,
}
# Coprime with the frame lengths, so frames straddle chunk boundaries the way
# they do on a real socket.
WIRE_CHUNK_BYTES = 997


def split_turns(wire: bytes) -> list[bytes]:
    """Split a session capture into per-turn wire streams after each ``done``."""
    turns: list[bytes] = []
    current = bytearray()
    for frame in iter_sse_frames([wire]):
        current.extend(b"data: " + frame.data.encode("utf-8") + b"\n\n")
        if frame_to_event(frame).get("type") == "done":
            turns.append(bytes(current))
            current = bytearray()
    if current:
        turns.append(bytes(current))
    return turns


class ReplayServer:
    """Serves the recorded turns in order, one per ``POST /turn``."""

    def __init__(self, wire: bytes, *, chunk: int = WIRE_CHUNK_BYTES):
        self.turns = split_turns(wire)
        self.chunk = chunk
        self.served = 0
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/turn"):
            if self.served >= len(self.turns):
                return httpx.Response(409, json={"error": "no further recorded turns"})
            body = self.turns[self.served]
            self.served += 1
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=iter([
                    body[i:i + self.chunk] for i in range(0, len(body), self.chunk)
                ]),
            )
        if path == "/api/v1/sessions":
            return httpx.Response(200, json={"session_id": "sess_8a686003edf5"})
        return httpx.Response(200, json={})

    def client(self, event_log: EventLog | None = None) -> BladeAIHttpClient:
        http = httpx.Client(transport=self.transport, base_url="http://blade.test")
        return BladeAIHttpClient("http://blade.test", http_client=http, event_log=event_log)


@pytest.fixture(scope="module")
def l0_wire() -> bytes:
    return L0_SSE.read_bytes()


def test_fixture_matches_its_recorded_provenance(l0_wire: bytes) -> None:
    entry = next(
        row for row in json.loads((FIXTURE_DIR / "provenance.json").read_text())["fixtures"]
        if row["name"] == "bladeai_L0.sse"
    )
    assert entry["bytes"] == len(l0_wire)
    assert entry["fixture_rows"] == SESSION_EVENTS
    assert entry["new_trial_executed"] is False


def test_recorded_session_splits_into_five_turns(l0_wire: bytes) -> None:
    turns = split_turns(l0_wire)
    assert [len(list(iter_sse_frames([turn]))) for turn in turns] == TURN_EVENT_COUNTS
    # Every turn is terminated by its own ``done``; none is truncated.
    for turn in turns:
        events = [frame_to_event(f) for f in iter_sse_frames([turn])]
        assert events[-1]["type"] == "done"
        assert sum(1 for e in events if e["type"] == "done") == 1


def test_one_turn_stops_at_its_own_terminal_event(l0_wire: bytes) -> None:
    server = ReplayServer(l0_wire)
    observed: list[bytes] = []
    with server.client() as client:
        result = client.run_turn(
            "sess_8a686003edf5", "L0 replay", timeout_seconds=60, observe=observed.append
        )

    assert result.event_count == TURN_EVENT_COUNTS[0]
    assert len(observed) == TURN_EVENT_COUNTS[0]
    assert result.terminal_event == "done"
    assert not (result.timed_out or result.cancelled or result.output_truncated)
    # F10 in the recorded evidence: the turn ends with no gate event at all.
    assert not any(json.loads(line)["type"] == "confirm" for line in observed)


def test_whole_session_replays_event_for_event(l0_wire: bytes) -> None:
    server = ReplayServer(l0_wire)
    observed: list[bytes] = []
    with server.client() as client:
        for _ in TURN_EVENT_COUNTS:
            client.run_turn("sess_8a686003edf5", "next", timeout_seconds=60, observe=observed.append)

    assert len(observed) == SESSION_EVENTS
    events = [json.loads(line) for line in observed]
    assert collections.Counter(e["type"] for e in events) == SESSION_TYPES
    # BladeAI sends no ``event:`` name, so every ``type`` came from the JSON
    # body itself; the frame-name fallback never fires on a real stream.
    assert all("type" in e for e in events)


def test_replay_is_independent_of_network_chunk_boundaries(l0_wire: bytes) -> None:
    """Frame reassembly must not depend on where the socket split the bytes."""
    payloads = set()
    for chunk in (1, 13, 997, 65536, len(l0_wire)):
        server = ReplayServer(l0_wire, chunk=chunk)
        observed: list[bytes] = []
        with server.client() as client:
            for _ in TURN_EVENT_COUNTS:
                client.run_turn("s", "go", timeout_seconds=120, observe=observed.append)
        assert len(observed) == SESSION_EVENTS
        payloads.add(b"".join(observed))

    assert len(payloads) == 1, "chunking changed the forwarded byte stream"


def test_every_forwarded_event_lands_with_an_arrival_timestamp(
    l0_wire: bytes, tmp_path: Path
) -> None:
    log_path = tmp_path / "bladeai-raw-events.jsonl"
    server = ReplayServer(l0_wire)
    with server.client(EventLog(log_path)) as client:
        for _ in TURN_EVENT_COUNTS:
            client.run_turn("sess_8a686003edf5", "next", timeout_seconds=60)

    records = [json.loads(line) for line in log_path.read_text().splitlines()]
    events = [row for row in records if row["kind"] == "event"]
    assert len(events) == SESSION_EVENTS
    assert collections.Counter(r["event"]["type"] for r in events) == SESSION_TYPES
    # Arrival time is present, parseable and non-decreasing: the only sound
    # input to the stall judgement WP-C.4 has to make.
    stamps = [r["received_at"] for r in events]
    assert all(stamps)
    assert stamps == sorted(stamps)
    # Turn numbers advance and each turn is bookended on the same timeline.
    assert [r["turn"] for r in events] == sorted(r["turn"] for r in events)
    assert max(r["turn"] for r in events) == len(TURN_EVENT_COUNTS)
    actions = [r.get("action") for r in records if r["kind"] == "driver"]
    assert actions[0] == "turn_started" and actions[-1] == "turn_finished"
    assert actions.count("turn_started") == len(TURN_EVENT_COUNTS)


def test_both_confirmation_gates_survive_the_transport(l0_wire: bytes) -> None:
    """The gates WP-C must answer arrive intact, distinguished by ``node``.

    Recorded fact that the handoff states differently: no event in this session
    -- nor in any of the other sixteen -- carries an ``interrupt_id`` key.  Both
    gates are ``type=confirm`` and are told apart by ``node``; the id to answer
    with is the event's own ``task_id``.
    """
    server = ReplayServer(l0_wire)
    observed: list[bytes] = []
    with server.client() as client:
        for _ in TURN_EVENT_COUNTS:
            client.run_turn("s", "go", timeout_seconds=60, observe=observed.append)

    events = [json.loads(line) for line in observed]
    confirms = [e for e in events if e["type"] == "confirm"]
    assert [e["node"] for e in confirms] == [
        "intent_confirm", "intent_confirm", "intent_confirm", "confirmation_gate"
    ]
    assert all(e["task_id"].startswith("turn-") for e in confirms)
    assert not any("interrupt_id" in e for e in events)


def test_recv_sidecar_agrees_with_the_replayed_event_order(l0_wire: bytes) -> None:
    sidecar = [json.loads(line) for line in L0_RECV.read_text().splitlines()]
    server = ReplayServer(l0_wire)
    observed: list[bytes] = []
    with server.client() as client:
        for _ in TURN_EVENT_COUNTS:
            client.run_turn("s", "go", timeout_seconds=60, observe=observed.append)

    assert [row["type"] for row in sidecar] == [json.loads(l)["type"] for l in observed]
    # The evaluator's own arrival times are non-decreasing, which is what makes
    # them usable as a stall reference for this session.
    assert [row["recv_ts"] for row in sidecar] == sorted(row["recv_ts"] for row in sidecar)


def test_harness_session_drives_the_real_stream_through_the_turn_executor(
    l0_wire: bytes,
) -> None:
    """The driver plugs into the existing session state machine unchanged.

    With no feedback queued, ``HarnessSession`` runs exactly one turn -- the
    same contract the subprocess transport has.
    """
    server = ReplayServer(l0_wire)
    observed: list[bytes] = []
    with server.client() as client:
        session = HarnessSession(
            argv=["blade-ai", "server"],
            stdin=b"L0 replay",
            env={},
            timeout_seconds=120,
            stdout_line_observer=lambda line: observed.append(line) and None,
            turn_executor=bladeai_http_turn_executor(client, "sess_8a686003edf5"),
        )
        result = session.start().wait()

    assert result.returncode == 0
    assert not (result.timed_out or result.cancelled or result.output_truncated)
    assert len(observed) == TURN_EVENT_COUNTS[0]
    # HarnessSession aggregates the same bytes it would from a subprocess stdout.
    assert result.stdout == b"".join(observed)
