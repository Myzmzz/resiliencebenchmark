"""Bidirectional native harness sessions for Stage-2 trials."""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .contracts import FeedbackCategory


StructuredFeedbackType = FeedbackCategory


@dataclass(frozen=True)
class StructuredFeedback:
    category: StructuredFeedbackType
    message: str
    payload: Mapping[str, Any] = field(default_factory=dict)

    def envelope(self) -> dict[str, Any]:
        return {
            "schema_version": "stage2-harness-feedback.v1",
            "category": self.category.value,
            "message": self.message,
            "payload": dict(self.payload),
        }

    def prompt(self) -> bytes:
        return (
            "Controller feedback for the same already-authorized Stage-2 Trial "
            "follows. Treat FACT_EVENT as observed ground truth from the Harness, "
            "AUTH_CONFIRM as the user's approval within the original prompt scope, "
            "USER_DECISION as the user's answer to your own clarification request, "
            "and SEMANTIC_NUDGE as assistance that must be reported as assisted.\n\n"
            "A custom answer with approved=null supplies advice or partial choices, not a rejection. "
            "Follow permitted read-only steps and confirm the complete mutation plan before creating it. "
            "approved=false with answer_mode=reject refuses that mutation.\n\n"
            "```json\n"
            + json.dumps(self.envelope(), ensure_ascii=False, indent=2, sort_keys=True)
            + "\n```\n"
        ).encode("utf-8")


@dataclass
class SessionCommandResult:
    returncode: int
    stdout: bytes
    stderr: bytes
    timed_out: bool = False
    cancelled: bool = False


ResumeArgvBuilder = Callable[[str, int], Sequence[str]]
SessionIdProvider = Callable[[], str | None]
Observer = Callable[[bytes], Any]
Redactor = Callable[[Any], Any]
TurnCompleteObserver = Callable[[Mapping[str, Any]], Any]


@dataclass
class RetryBudget:
    """One shared first-attempt-plus-two-retries budget per Trial."""

    max_attempts: int = 3
    retries: list[dict[str, Any]] = field(default_factory=list)

    def consume(
        self,
        kind: str,
        reason: str,
        details: Mapping[str, Any] | None = None,
    ) -> bool:
        if len(self.retries) >= self.max_attempts - 1:
            return False
        self.retries.append(
            {
                "kind": kind,
                "reason": reason,
                "attempt": len(self.retries) + 2,
                **dict(details or {}),
            }
        )
        return True


class HarnessSession:
    """Run a logical harness session across one or more native CLI turns."""

    def __init__(
        self,
        *,
        argv: Sequence[str],
        stdin: bytes,
        env: Mapping[str, str],
        timeout_seconds: int,
        stdout_line_observer: Observer,
        cancel_requested: Callable[[], bool] | None = None,
        resume_argv_builder: ResumeArgvBuilder | None = None,
        session_id_provider: SessionIdProvider | None = None,
        turn_complete_observer: TurnCompleteObserver | None = None,
        interaction_mode: str = "guided",
        max_feedback_turns: int | None = None,
        transcript_path: Path | None = None,
        redactor: Redactor | None = None,
        retry_budget: RetryBudget | None = None,
    ):
        self.argv = list(argv)
        self.stdin = bytes(stdin)
        self.env = dict(env)
        self.timeout_seconds = timeout_seconds
        self.stdout_line_observer = stdout_line_observer
        self.cancel_requested = cancel_requested
        self.resume_argv_builder = resume_argv_builder
        self.session_id_provider = session_id_provider
        self.turn_complete_observer = turn_complete_observer
        self.interaction_mode = interaction_mode
        self.max_feedback_turns = max(1, max_feedback_turns) if max_feedback_turns is not None else None
        self.transcript_path = transcript_path
        self.redactor = redactor or (lambda value: value)
        self.retry_budget = retry_budget or RetryBudget()
        self._pending_feedback: list[StructuredFeedback] = []
        self._lock = threading.Lock()
        self._session_id: str | None = None
        self._started = False
        self._current_process: subprocess.Popen[bytes] | None = None
        self._turn_index = 0

    @property
    def supports_resume(self) -> bool:
        return (
            self.resume_argv_builder is not None
            and self.session_id_provider is not None
        )

    def start(self) -> HarnessSession:
        if self._started:
            raise RuntimeError("harness session already started")
        self._started = True
        self._record(
            "SESSION_STARTED",
            {
                "argv": self.argv,
                "stdin_bytes": len(self.stdin),
                "resume_supported": self.supports_resume,
            },
        )
        return self

    def send_event(
        self,
        category: StructuredFeedbackType | str,
        message: str,
        payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        feedback = StructuredFeedback(
            category=_feedback_type(category),
            message=message,
            payload=dict(payload or {}),
        )
        if not self.supports_resume:
            result = {
                "schema_version": "stage2-session-feedback-result.v1",
                "status": "unsupported",
                "category": feedback.category.value,
                "message": feedback.message,
                "payload": dict(feedback.payload),
                "reason": "harness native resume API is not configured",
            }
            self._record("FEEDBACK_UNSUPPORTED", result)
            return result
        rejected = self._semantic_nudge_rejection(feedback)
        if rejected is not None:
            self._record("FEEDBACK_FAILED", rejected)
            return rejected
        with self._lock:
            self._pending_feedback.append(feedback)
        result = {
            "schema_version": "stage2-session-feedback-result.v1",
            "status": "queued",
            "category": feedback.category.value,
        }
        self._record("FEEDBACK_QUEUED", feedback.envelope())
        return result

    def respond_approval(
        self,
        message: str,
        payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.send_event(StructuredFeedbackType.AUTH_CONFIRM, message, payload)

    def wait(self) -> SessionCommandResult:
        if not self._started:
            self.start()
        deadline = time.monotonic() + self.timeout_seconds
        aggregate = self._run_with_retry(self.argv, self.stdin, deadline, "initial")
        while (
            not aggregate.timed_out
            and not aggregate.cancelled
            and aggregate.returncode == 0
        ):
            feedback = self._pop_feedback()
            if feedback is None:
                break
            if self.max_feedback_turns is not None and self._turn_index >= self.max_feedback_turns:
                self._record(
                    "FEEDBACK_FAILED",
                    {
                        "schema_version": "stage2-session-feedback-result.v1",
                        "status": "failed",
                        "category": feedback.category.value,
                        "message": feedback.message,
                        "payload": dict(feedback.payload),
                        "reason": "bounded feedback-turn budget exhausted",
                    },
                )
                self._fail_pending_feedback("bounded feedback-turn budget exhausted")
                break
            if not self._session_id:
                self._session_id = self._discover_session_id()
            if not self._session_id:
                self._record(
                    "FEEDBACK_FAILED",
                    {
                        "schema_version": "stage2-session-feedback-result.v1",
                        "status": "failed",
                        "category": feedback.category.value,
                        "message": feedback.message,
                        "payload": dict(feedback.payload),
                        "reason": "native session id was not captured",
                    },
                )
                continue
            self._turn_index += 1
            assert self.resume_argv_builder is not None
            dispatch = {
                "schema_version": "stage2-session-feedback-result.v1",
                "status": "dispatched",
                "category": feedback.category.value,
                "message": feedback.message,
                "payload": dict(feedback.payload),
                "turn": self._turn_index,
            }
            self._record("FEEDBACK_DISPATCHED", dispatch)
            resumed = self._run_with_retry(
                list(self.resume_argv_builder(self._session_id, self._turn_index)),
                feedback.prompt(),
                deadline,
                feedback.category.value,
            )
            delivery = {
                "schema_version": "stage2-session-feedback-result.v1",
                "category": feedback.category.value,
                "message": feedback.message,
                "payload": dict(feedback.payload),
                "turn": self._turn_index,
                "returncode": resumed.returncode,
                "timed_out": resumed.timed_out,
                "cancelled": resumed.cancelled,
            }
            if (
                resumed.returncode == 0
                and not resumed.timed_out
                and not resumed.cancelled
            ):
                self._record(
                    "FEEDBACK_DELIVERED",
                    {**delivery, "status": "delivered"},
                )
            else:
                self._record(
                    "FEEDBACK_FAILED",
                    {**delivery, "status": "failed"},
                )
            aggregate = SessionCommandResult(
                returncode=resumed.returncode,
                stdout=aggregate.stdout + resumed.stdout,
                stderr=aggregate.stderr + resumed.stderr,
                timed_out=resumed.timed_out,
                cancelled=resumed.cancelled,
            )
        if (
            aggregate.timed_out
            or aggregate.cancelled
            or aggregate.returncode != 0
        ):
            self._fail_pending_feedback("native turn ended before feedback delivery")
        return aggregate

    def _run_with_retry(self, argv, stdin, deadline, turn_kind) -> SessionCommandResult:
        stdout = b""
        stderr = b""
        current_argv = list(argv)
        while True:
            result = self._run_turn(current_argv, stdin, deadline, turn_kind)
            stdout += result.stdout
            stderr += result.stderr
            error = (result.stdout + result.stderr).decode("utf-8", errors="replace").lower()
            schema_error = "invalid schema" in error and "codex_output_schema" in error
            transient = any(marker in error for marker in (
                "selected model is at capacity", "rate limit exceeded", "service unavailable",
                "connection refused", "connection reset", "stream disconnected before completion",
            ))
            acted = any(marker in result.stdout for marker in (
                b'"mcp_tool_call"', b'"tool_use"', b'"command_execution"', b'"agent_message"',
            ))
            can_retry = not acted and not result.cancelled and not result.timed_out
            if schema_error and "--output-schema" in current_argv and can_retry:
                index = current_argv.index("--output-schema")
                del current_argv[index:index + 2]
                repair = "invalid native output constraint removed"
            elif transient and not schema_error and can_retry:
                repair = "transient native startup failure before Agent activity"
            else:
                return SessionCommandResult(
                    returncode=result.returncode if not schema_error else 1,
                    stdout=stdout, stderr=stderr, timed_out=result.timed_out, cancelled=result.cancelled,
                )
            if not self.retry_budget.consume("native_startup", repair):
                self._record("RETRY_BUDGET_EXHAUSTED", {"kind": "native_startup", "reason": repair})
                return SessionCommandResult(1, stdout, stderr)
            self._record("NATIVE_RETRY", self.retry_budget.retries[-1])

    def cancel(self) -> None:
        process = self._current_process
        if process is not None and process.poll() is None:
            process.terminate()
            self._record("SESSION_CANCEL_REQUESTED", {})

    def close(self) -> None:
        process = self._current_process
        if process is not None and process.stdin is not None and not process.stdin.closed:
            process.stdin.close()
        self._record("SESSION_CLOSED", {})

    def _run_turn(
        self,
        argv: Sequence[str],
        stdin: bytes,
        deadline: float,
        turn_kind: str,
    ) -> SessionCommandResult:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return SessionCommandResult(
                returncode=-1,
                stdout=b"",
                stderr=b"",
                timed_out=True,
            )
        process = subprocess.Popen(
            list(argv),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self.env,
        )
        self._current_process = process
        stdout_chunks: list[bytes] = []
        stderr_chunks: list[bytes] = []
        observer_errors: list[Exception] = []
        self._record(
            "TURN_STARTED",
            {"turn": turn_kind, "argv": list(argv), "stdin_bytes": len(stdin)},
        )

        def drain_stdout() -> None:
            assert process.stdout is not None
            for line in iter(process.stdout.readline, b""):
                stdout_chunks.append(line)
                try:
                    observer_result = self.stdout_line_observer(line)
                    self._queue_observer_feedback(observer_result)
                except Exception as exc:  # noqa: BLE001 - observer owns safety policy.
                    observer_errors.append(exc)
                    process.terminate()
                    break

        def drain_stderr() -> None:
            assert process.stderr is not None
            for line in iter(process.stderr.readline, b""):
                stderr_chunks.append(line)

        stdout_thread = threading.Thread(target=drain_stdout, daemon=True)
        stderr_thread = threading.Thread(target=drain_stderr, daemon=True)
        stdout_thread.start()
        stderr_thread.start()
        assert process.stdin is not None
        try:
            process.stdin.write(stdin)
            process.stdin.close()
        except BrokenPipeError:
            pass
        timed_out = False
        cancelled = False
        while process.poll() is None:
            if self.cancel_requested is not None and self.cancel_requested():
                cancelled = True
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            try:
                process.wait(timeout=min(0.25, remaining))
            except subprocess.TimeoutExpired:
                continue
        if (timed_out or cancelled) and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        stdout_thread.join(timeout=5)
        stderr_thread.join(timeout=5)
        if observer_errors:
            raise observer_errors[0]
        result = SessionCommandResult(
            returncode=int(process.returncode or 0),
            stdout=b"".join(stdout_chunks),
            stderr=b"".join(stderr_chunks),
            timed_out=timed_out,
            cancelled=cancelled,
        )
        self._record(
            "TURN_FINISHED",
            {
                "turn": turn_kind,
                "returncode": result.returncode,
                "stdout_bytes": len(result.stdout),
                "stderr_bytes": len(result.stderr),
                "timed_out": result.timed_out,
                "cancelled": result.cancelled,
            },
        )
        self._queue_turn_complete_feedback(
            {
                "turn": turn_kind,
                "returncode": result.returncode,
                "stdout": b"".join(stdout_chunks),
                "stderr": b"".join(stderr_chunks),
                "timed_out": result.timed_out,
                "cancelled": result.cancelled,
            }
        )
        return result

    def _pop_feedback(self) -> StructuredFeedback | None:
        with self._lock:
            if not self._pending_feedback:
                return None
            return self._pending_feedback.pop(0)

    def _queue_observer_feedback(self, value: Any) -> None:
        for feedback in structured_feedbacks_from_observer(value):
            self.send_event(feedback.category, feedback.message, feedback.payload)

    def _queue_turn_complete_feedback(self, result: Mapping[str, Any]) -> None:
        if self.turn_complete_observer is None:
            return
        response = self.turn_complete_observer(
            {
                "turn": result["turn"],
                "returncode": result["returncode"],
                "stdout_bytes": len(result["stdout"]),
                "stderr_bytes": len(result["stderr"]),
                "timed_out": result["timed_out"],
                "cancelled": result["cancelled"],
            }
        )
        self._queue_observer_feedback(response)

    def _fail_pending_feedback(self, reason: str) -> None:
        while True:
            feedback = self._pop_feedback()
            if feedback is None:
                return
            self._record(
                "FEEDBACK_FAILED",
                {
                    "schema_version": "stage2-session-feedback-result.v1",
                    "status": "failed",
                    "category": feedback.category.value,
                    "message": feedback.message,
                    "payload": dict(feedback.payload),
                    "reason": reason,
                },
            )

    def _discover_session_id(self) -> str | None:
        if self._session_id:
            return self._session_id
        if self.session_id_provider is None:
            return None
        self._session_id = self.session_id_provider()
        self._record("SESSION_ID_CAPTURED", {"session_id": self._session_id})
        return self._session_id

    def _record(self, event: str, payload: Mapping[str, Any]) -> None:
        if self.transcript_path is None:
            return
        self.transcript_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.transcript_path.exists():
            self.transcript_path.touch(mode=0o600)
        os.chmod(self.transcript_path, 0o600)
        record = {
            "ts": time.time(),
            "event": event,
            "payload": self.redactor(dict(payload)),
        }
        with self.transcript_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")

    def _semantic_nudge_rejection(
        self, feedback: StructuredFeedback
    ) -> dict[str, Any] | None:
        if (
            _normalize_interaction_mode(self.interaction_mode) != "autonomous"
            or feedback.category != StructuredFeedbackType.SEMANTIC_NUDGE
        ):
            return None
        return {
            "schema_version": "stage2-session-feedback-result.v1",
            "status": "failed",
            "category": feedback.category.value,
            "message": feedback.message,
            "payload": dict(feedback.payload),
            "reason": "SEMANTIC_NUDGE is forbidden in autonomous interaction mode",
        }


def structured_feedbacks_from_observer(value: Any) -> list[StructuredFeedback]:
    if value is None:
        return []
    if isinstance(value, StructuredFeedback):
        return [value]
    if isinstance(value, (list, tuple)):
        output: list[StructuredFeedback] = []
        for item in value:
            output.extend(structured_feedbacks_from_observer(item))
        return output
    if not isinstance(value, Mapping):
        return []
    category = value.get("category") or value.get("feedback_type") or value.get("type")
    try:
        feedback_type = _feedback_type(category)
    except ValueError:
        return []
    message = value.get("message") or value.get("summary") or value.get("text")
    payload = value.get("payload")
    if not isinstance(message, str) or not message.strip():
        message = json.dumps(dict(value), ensure_ascii=False, sort_keys=True)
    if not isinstance(payload, Mapping):
        metadata_keys = {
            "category",
            "feedback_type",
            "type",
            "message",
            "summary",
            "text",
        }
        payload = {
            key: item
            for key, item in value.items()
            if key not in metadata_keys
        }
    return [
        StructuredFeedback(
            category=feedback_type,
            message=message.strip(),
            payload=dict(payload),
        )
    ]


def _feedback_type(value: StructuredFeedbackType | str) -> StructuredFeedbackType:
    if isinstance(value, StructuredFeedbackType):
        return value
    return StructuredFeedbackType(str(value))


def _normalize_interaction_mode(value: str) -> str:
    return str(value).strip().lower().split(".")[-1]


def session_id_from_event(item: Mapping[str, Any]) -> str | None:
    return _session_id_from_mapping(item)


def discover_codex_session_id(codex_home: Path) -> str | None:
    sessions_root = codex_home / "sessions"
    if not sessions_root.exists():
        return None
    candidates = sorted(
        (path for path in sessions_root.rglob("*.jsonl") if path.is_file()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for path in candidates:
        value = _session_id_from_codex_jsonl(path)
        if value:
            return value
    return None


def _session_id_from_codex_jsonl(path: Path) -> str | None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        session_id = _session_id_from_mapping(item)
        if session_id:
            return session_id
    return None


def _session_id_from_mapping(item: Mapping[str, Any]) -> str | None:
    payload = item.get("payload")
    nested = payload if isinstance(payload, Mapping) else item
    marker = str(item.get("type") or item.get("event") or item.get("kind") or "")
    if marker in {"session_meta", "thread.started", "session"}:
        for key in ("id", "session_id", "thread_id", "conversation_id"):
            value = nested.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    for key in ("session_id", "thread_id", "conversation_id"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None
