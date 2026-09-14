"""SSE framing rules for the BladeAI black-box transport.

These cover the cases a ``split(b"\\n\\n")`` parser gets wrong.  Frame loss here
would be silent evidence loss in a Trial, so the boundaries are pinned.
"""

from __future__ import annotations

import pytest

from harness.bladeai_http.protocol import (
    APPROVAL_WORDS,
    BladeAIProtocolError,
    SSEFrame,
    confirm_path,
    event_type,
    frame_to_event,
    interrupt_path,
    is_terminal,
    iter_sse_frames,
    turn_path,
)


def frames(*chunks: bytes) -> list[SSEFrame]:
    return list(iter_sse_frames(chunks))


def test_single_frame_dispatches_on_blank_line() -> None:
    assert frames(b'data: {"type":"token"}\n\n') == [
        SSEFrame(data='{"type":"token"}', event=None)
    ]


def test_event_name_and_id_are_captured() -> None:
    result = frames(b'id: 7\nevent: tool_start\ndata: {"call_id":"c1"}\n\n')
    assert result == [SSEFrame(data='{"call_id":"c1"}', event="tool_start", event_id="7")]


def test_repeated_data_fields_join_with_newline() -> None:
    result = frames(b"data: line one\ndata: line two\n\n")
    assert result[0].data == "line one\nline two"


def test_frame_split_across_chunk_boundaries_is_reassembled() -> None:
    # The network decides where chunks end; a frame routinely straddles two.
    result = frames(b'event: tok', b'en\ndata: {"te', b'xt":"hi"}\n', b"\n")
    assert result == [SSEFrame(data='{"text":"hi"}', event="token")]


def test_crlf_terminators_are_accepted_even_when_split_mid_pair() -> None:
    # A chunk may end between the CR and the LF of one CRLF terminator.
    result = frames(b'data: {"type":"done"}\r', b"\n\r\n")
    assert result == [SSEFrame(data='{"type":"done"}')]


def test_bare_cr_terminates_a_line() -> None:
    assert frames(b'data: {"type":"done"}\r\r')[0].data == '{"type":"done"}'


def test_comment_lines_are_keepalives_and_dispatch_nothing() -> None:
    result = frames(b": keep-alive\n\n", b'data: {"type":"usage"}\n\n')
    assert [frame.data for frame in result] == ['{"type":"usage"}']


def test_only_one_leading_space_after_colon_is_stripped() -> None:
    assert frames(b"data:  padded\n\n")[0].data == " padded"


def test_field_without_colon_is_a_name_with_empty_value() -> None:
    # "data" alone is a data field whose value is the empty string.
    assert frames(b"data\n\n")[0].data == ""


def test_unterminated_tail_is_dropped_rather_than_half_dispatched() -> None:
    # A connection cut mid-frame must not fabricate an event.
    assert frames(b'data: {"type":"tok') == []


def test_multiple_frames_stream_in_order() -> None:
    result = frames(
        b'data: {"type":"node_start"}\n\n'
        b'data: {"type":"token"}\n\n'
        b'data: {"type":"done"}\n\n'
    )
    assert [frame.data for frame in result] == [
        '{"type":"node_start"}',
        '{"type":"token"}',
        '{"type":"done"}',
    ]


def test_frame_to_event_fills_type_from_sse_name_when_body_omits_it() -> None:
    event = frame_to_event(SSEFrame(data='{"call_id":"c1"}', event="tool_start"))
    assert event == {"call_id": "c1", "type": "tool_start"}


def test_frame_to_event_never_overwrites_a_type_the_body_declared() -> None:
    event = frame_to_event(SSEFrame(data='{"type":"confirm"}', event="message"))
    assert event["type"] == "confirm"


def test_frame_to_event_rejects_non_json_and_non_object_payloads() -> None:
    with pytest.raises(BladeAIProtocolError):
        frame_to_event(SSEFrame(data="not json"))
    with pytest.raises(BladeAIProtocolError):
        frame_to_event(SSEFrame(data="[1, 2]"))
    with pytest.raises(BladeAIProtocolError):
        frame_to_event(SSEFrame(data="   "))


def test_terminal_events_are_done_and_error_only() -> None:
    assert is_terminal({"type": "done"})
    assert is_terminal({"type": "error"})
    # ``result`` carries the final report but the turn continues until ``done``.
    assert not is_terminal({"type": "result"})
    assert not is_terminal({"type": "tool_end"})


def test_event_type_tolerates_a_missing_or_non_string_type() -> None:
    assert event_type({"type": "token"}) == "token"
    assert event_type({}) is None
    assert event_type({"type": 3}) is None


def test_approval_vocabulary_matches_the_server_whitelist() -> None:
    # Finding F1: anything outside this set, including an approval carrying an
    # explanation, is normalised to a rejection by the server.
    assert APPROVAL_WORDS == ("approved", "yes", "y", "ok")
    assert "approved, 80% cpu" not in APPROVAL_WORDS


def test_endpoint_paths_match_the_published_contract() -> None:
    assert turn_path("s1") == "/api/v1/sessions/s1/turn"
    assert interrupt_path("s1") == "/api/v1/sessions/s1/interrupt"
    # The execution gate is keyed by task, not by session.
    assert confirm_path("t9") == "/api/v1/confirm/t9"


def test_multibyte_character_split_across_chunks_is_not_corrupted() -> None:
    """Regression: decoding must carry state across chunk boundaries.

    Caught by replaying the real L0 capture at several chunk sizes: the stream
    carries Chinese prompt and tool text, and a per-chunk ``bytes.decode`` turns
    any character straddling the split into U+FFFD on both sides.  The event
    count stays right, so only a byte-level comparison exposes it.
    """
    payload = '{"type":"token","content":"请针对 otel-demo 的 cart 服务注入负载"}'
    wire = f"data: {payload}\n\n".encode("utf-8")
    whole = frames(wire)
    assert whole[0].data == payload
    assert "�" not in whole[0].data

    # Every possible split point, including mid-character ones.
    for cut in range(1, len(wire)):
        split = frames(wire[:cut], wire[cut:])
        assert split == whole, f"chunk boundary at byte {cut} changed the frame"

    # And one byte at a time, the worst case a socket can produce.
    assert frames(*(wire[i : i + 1] for i in range(len(wire)))) == whole
