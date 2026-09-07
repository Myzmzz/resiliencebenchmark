"""HarnessSession tests using a fake sidecar turn transport.

These prove the session state machine, not Linux isolation.  Real Unix socket
and cgroup qualification remains Linux-only in ``test_agent_exec.py``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

import pytest

from harness.agent_exec.client import AgentExecClient, agent_exec_streaming_runner
from stage2_service.contracts import FeedbackCategory
from stage2_service.session import HarnessSession, RetryBudget, SessionCommandResult


class FakeTransport:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.cancel_on_first = False

    def __call__(
        self,
        argv: Sequence[str],
        stdin: bytes,
        env: Mapping[str, str],
        timeout_seconds: int,
        observe: Callable[[bytes], object],
        cancelled: Callable[[], bool],
        **_kwargs,
    ) -> SessionCommandResult:
        self.calls.append({
            "argv": list(argv), "stdin": stdin, "env": dict(env),
            "timeout": timeout_seconds,
        })
        if len(self.calls) == 1:
            observe(b'{"event":"agent_needs_confirmation"}\n')
            if self.cancel_on_first:
                return SessionCommandResult(-1, b"", b"", cancelled=cancelled())
            return SessionCommandResult(0, b"first\n", b"")
        observe(b'{"event":"tool_call","tool":"harness_channel.harness_confirm"}\n')
        return SessionCommandResult(0, b"resumed\n", b"")


def test_sidecar_transport_preserves_initial_feedback_resume_and_live_records() -> None:
    transport = FakeTransport()
    records: list[dict[str, object]] = []
    turns: list[dict[str, object]] = []

    def observe(line: bytes):
        if b"needs_confirmation" in line:
            return {
                "category": FeedbackCategory.AUTH_CONFIRM.value,
                "message": "plan approved",
                "payload": {"source": "test"},
            }
        return None

    session = HarnessSession(
        argv=["agent", "initial"], stdin=b"initial prompt", env={"SAFE": "yes"},
        timeout_seconds=10, stdout_line_observer=observe,
        resume_argv_builder=lambda session_id, turn: ["agent", "resume", session_id, str(turn)],
        session_id_provider=lambda: "native-session-1",
        turn_complete_observer=lambda summary: turns.append(dict(summary)) or None,
        record_observer=records.append,
        turn_executor=transport,
    )

    result = session.start().wait()

    assert result.returncode == 0
    assert result.stdout == b"first\nresumed\n"
    assert len(transport.calls) == 2
    assert transport.calls[0]["argv"] == ["agent", "initial"]
    assert transport.calls[1]["argv"] == ["agent", "resume", "native-session-1", "1"]
    assert b"plan approved" in transport.calls[1]["stdin"]
    assert [row["event"] for row in records if row["event"] in {
        "TURN_STARTED", "FEEDBACK_QUEUED", "SESSION_ID_CAPTURED", "FEEDBACK_DISPATCHED", "FEEDBACK_DELIVERED", "TURN_FINISHED",
    }] == [
        "TURN_STARTED", "FEEDBACK_QUEUED", "TURN_FINISHED", "SESSION_ID_CAPTURED",
        "FEEDBACK_DISPATCHED", "TURN_STARTED", "TURN_FINISHED", "FEEDBACK_DELIVERED",
    ]
    assert [turn["turn"] for turn in turns] == ["initial", "AUTH_CONFIRM"]


def test_cancel_signal_reaches_transport_and_prevents_resume() -> None:
    transport = FakeTransport()
    transport.cancel_on_first = True
    holder: dict[str, HarnessSession] = {}

    def observe(_line: bytes):
        holder["session"].cancel()
        return {
            "category": FeedbackCategory.AUTH_CONFIRM.value,
            "message": "will not resume",
        }

    records: list[dict[str, object]] = []
    session = HarnessSession(
        argv=["agent"], stdin=b"", env={}, timeout_seconds=10,
        stdout_line_observer=observe,
        resume_argv_builder=lambda _id, _turn: ["agent", "resume"],
        session_id_provider=lambda: "stable-session",
        record_observer=records.append,
        turn_executor=transport,
    )
    holder["session"] = session

    result = session.start().wait()

    assert result.cancelled is True
    assert len(transport.calls) == 1
    assert any(row["event"] == "SESSION_CANCEL_REQUESTED" for row in records)
    assert any(row["event"] == "TURN_FINISHED" and row["payload"]["cancelled"] for row in records)
    assert any(row["event"] == "FEEDBACK_FAILED" for row in records)


def test_transport_error_is_recorded_and_not_silently_fallen_back() -> None:
    records: list[dict[str, object]] = []

    def failing(*_args: object, **_kwargs: object) -> SessionCommandResult:
        raise RuntimeError("sidecar unavailable")

    session = HarnessSession(
        argv=["agent"], stdin=b"", env={}, timeout_seconds=10,
        stdout_line_observer=lambda _line: None, record_observer=records.append,
        turn_executor=failing,
    )
    with pytest.raises(RuntimeError, match="sidecar unavailable"):
        session.start().wait()
    assert [row["event"] for row in records][-1] == "TURN_FAILED"


def test_single_turn_remote_helper_rejects_resume_kwargs_instead_of_dropping_them() -> None:
    client = AgentExecClient("/never-connect", expected_server_uid=0)
    with pytest.raises(TypeError, match="resume_argv_builder"):
        agent_exec_streaming_runner(
            client, ["agent"], b"", {}, 1, lambda _line: None,
            resume_argv_builder=lambda _id, _turn: ["agent"],
        )


def test_sidecar_transport_preserves_shared_retry_budget_and_recording() -> None:
    calls = 0
    records: list[dict[str, object]] = []

    def transient_then_success(
        _argv: Sequence[str], _stdin: bytes, _env: Mapping[str, str], _timeout: int,
        _observe: Callable[[bytes], object], _cancel: Callable[[], bool], **_kwargs,
    ) -> SessionCommandResult:
        nonlocal calls
        calls += 1
        if calls == 1:
            return SessionCommandResult(1, b"", b"service unavailable")
        return SessionCommandResult(0, b"finished\n", b"")

    budget = RetryBudget(max_attempts=2)
    result = HarnessSession(
        argv=["agent"], stdin=b"", env={}, timeout_seconds=10,
        stdout_line_observer=lambda _line: None,
        activity_provider=lambda: False,
        retry_budget=budget,
        record_observer=records.append,
        turn_executor=transient_then_success,
    ).start().wait()

    assert result.returncode == 0
    assert calls == 2
    assert budget.retries == [{"kind": "native_startup", "reason": "transient native startup failure before Agent activity", "attempt": 2}]
    assert any(row["event"] == "NATIVE_RETRY" for row in records)


def test_classifier_can_retry_a_transient_terminal_result_after_native_output() -> None:
    calls = 0
    records: list[dict[str, object]] = []

    def transport(
        _argv: Sequence[str], _stdin: bytes, _env: Mapping[str, str], _timeout: int,
        _observe: Callable[[bytes], object], _cancel: Callable[[], bool], **_kwargs,
    ) -> SessionCommandResult:
        nonlocal calls
        calls += 1
        if calls == 1:
            return SessionCommandResult(
                0,
                b'{"type":"stage2_bladeai_event","kind":"llm_thought","payload":{}}\n'
                b'{"type":"stage2_bladeai_result","status":"failed","error":{"code":"UNKNOWN","message":"Too many pending requests, please retry later"}}\n',
                b"",
            )
        return SessionCommandResult(0, b"finished\n", b"")

    def classifier(result: SessionCommandResult):
        if b"Too many pending requests" in result.stdout:
            return True, "transient BladeAI provider failure before mutation", {
                "error_code": "UNKNOWN",
            }
        return False, "", {}

    budget = RetryBudget(max_attempts=2)
    result = HarnessSession(
        argv=["agent"], stdin=b"", env={}, timeout_seconds=30,
        stdout_line_observer=lambda _line: None,
        retry_budget=budget,
        retry_classifier=classifier,
        record_observer=records.append,
        turn_executor=transport,
    ).start().wait()

    assert result.returncode == 0
    assert calls == 2
    assert budget.retries[0]["kind"] == "native_startup"
    assert budget.retries[0]["reason"] == "transient BladeAI provider failure before mutation"
    assert budget.retries[0]["attempt"] == 2
    assert budget.retries[0]["error_code"] == "UNKNOWN"
    assert any(row["event"] == "NATIVE_RETRY_BACKOFF" for row in records)


def test_native_output_budget_is_shared_across_resume_turns(monkeypatch) -> None:
    import stage2_service.session as session_module

    monkeypatch.setattr(session_module, "MAX_NATIVE_OUTPUT_BYTES", 8)
    requested_limits = []
    calls = 0

    def transport(_argv, _stdin, _env, _timeout, observe, _cancel, *, output_limit_bytes):
        nonlocal calls
        calls += 1
        requested_limits.append(output_limit_bytes)
        if calls == 1:
            observe(b"need\n")
            return SessionCommandResult(0, b"12345", b"")
        return SessionCommandResult(0, b"67890", b"")

    result = HarnessSession(
        argv=["agent"], stdin=b"", env={}, timeout_seconds=10,
        stdout_line_observer=lambda line: {
            "category": FeedbackCategory.FACT_EVENT.value,
            "message": "continue",
        } if line == b"need\n" else None,
        resume_argv_builder=lambda _session, _turn: ["agent", "resume"],
        session_id_provider=lambda: "session-1",
        turn_executor=transport,
    ).start().wait()

    assert requested_limits == [8, 3]
    assert result.stdout == b"12345678"
    assert result.output_truncated is True
