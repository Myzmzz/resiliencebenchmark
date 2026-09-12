"""Black-box HTTP/SSE driver for a BladeAI 0.7.0 server.

The platform already drives codex, claude-code and deepseek-harness as opaque
processes whose stdout is read line by line.  BladeAI publishes the same shape
of evidence over HTTP instead of a pipe, so this module is the transport peer
of :mod:`harness.agent_exec.client`: it turns one already-decided turn into a
server-sent event stream and forwards every event to the same
``stdout_line_observer`` callback the subprocess path uses.  Downstream --
adapter, LifecycleMapper, scoring -- cannot tell the two transports apart.

Nothing here imports BladeAI or reflects on its internals.  Only the published
endpoints and event fields in :mod:`harness.bladeai_http.protocol` are used.

Operational facts that shaped this module, all from the 2026-09-11 live run:

* ``POST /api/v1/sessions/{sid}/cancel`` cancels **every task on the server**,
  not just the addressed session (handoff s2.2).  Each Trial therefore gets its
  own server instance, and :meth:`BladeAIHttpClient.cancel` says so loudly.
* A turn stream can fall silent for many minutes while the pipeline is alive
  and healthy, so stall detection must read the *raw event arrival time* rather
  than any request's return value (findings W2, D8-A).  :attr:`last_event_at`
  exposes exactly that.
* An ``error`` event is not by itself evidence that the agent failed: in the
  live run L1/L3/L4/P2 all carried ``Turn cancelled``, which the evaluator had
  sent (finding F14).  Every cancellation this driver issues is written to the
  event log as a ``driver`` record so the error can be attributed later.
"""

from __future__ import annotations

import json
import queue
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from .protocol import (
    APPROVAL_WORDS,
    CONFIRM_ACTIONS,
    DEFAULT_API_PREFIX,
    BladeAIProtocolError,
    SSEFrame,
    cancel_path,
    confirm_path,
    event_type,
    frame_to_event,
    interrupt_path,
    is_terminal,
    iter_sse_frames,
    sessions_path,
    state_path,
    turn_path,
)


class BladeAIHttpError(RuntimeError):
    """The BladeAI server rejected a request or broke the published contract."""


LineObserver = Callable[[bytes], object]
CancelRequested = Callable[[], bool]

# One turn's evidence budget, matching the native-output ceiling the subprocess
# transport enforces.  Imported lazily-by-value to keep this package free of
# stage2_service imports.
MAX_TURN_OUTPUT_BYTES = 16 * 1024 * 1024

# How often the consumer loop wakes to re-check the deadline and the caller's
# cancellation flag while the stream is silent.
POLL_INTERVAL_SECONDS = 0.2

# ``POST /api/v1/confirm/{task_id}`` blocks until the pipeline actually resumes:
# 13.8 s and 28.7 s were typical in the live run and 172 s was the worst case
# (finding F8).  A short client timeout here reads as a failure and loses the
# gate answer, so the default is deliberately far above the worst observation.
DEFAULT_CONFIRM_TIMEOUT_SECONDS = 300.0

# Ordinary control-plane calls (create session, cancel, state) return promptly.
DEFAULT_REQUEST_TIMEOUT_SECONDS = 30.0

# After we ask the server to cancel, keep draining briefly so the resulting
# ``error`` event is captured and can be attributed to us rather than to the
# agent.
CANCEL_DRAIN_SECONDS = 10.0


@dataclass(frozen=True)
class TurnExecution:
    """Terminal state of one turn, shaped like ``SessionCommandResult``."""

    returncode: int
    stdout: bytes
    stderr: bytes
    timed_out: bool = False
    cancelled: bool = False
    output_truncated: bool = False
    event_count: int = 0
    terminal_event: str | None = None


class EventLog:
    """Append-only raw evidence file for one Trial.

    Two record kinds share the file and are told apart by ``kind``: ``event``
    is the server's own payload, forwarded verbatim under ``event``; ``driver``
    is something this platform did (turn start, cancellation, stream close).
    Keeping both on one timeline is what lets a later stage say whether an
    ``error`` event was the agent failing or us pulling the plug (finding F14).
    """

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._seq = 0

    def _write(self, record: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._seq += 1
            record["seq"] = self._seq
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
                # Evidence must survive an abrupt exit, so never buffer it.
                handle.flush()
        return record

    def record_event(
        self,
        event: Mapping[str, Any],
        *,
        received_at: datetime,
        elapsed_ms: int,
        sse_event: str | None,
        session_id: str | None,
        turn: int,
    ) -> dict[str, Any]:
        return self._write({
            "kind": "event",
            "received_at": received_at.isoformat(),
            "elapsed_ms": elapsed_ms,
            "session_id": session_id,
            "turn": turn,
            "sse_event": sse_event,
            "event": dict(event),
        })

    def record_driver(self, action: str, detail: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return self._write({
            "kind": "driver",
            "received_at": datetime.now(timezone.utc).isoformat(),
            "action": action,
            "detail": dict(detail or {}),
        })


class BladeAIHttpClient:
    """Drive one BladeAI server over its published HTTP/SSE interface."""

    def __init__(
        self,
        base_url: str,
        *,
        http_client: httpx.Client | None = None,
        api_prefix: str = DEFAULT_API_PREFIX,
        event_log: EventLog | None = None,
        request_timeout: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
        confirm_timeout: float = DEFAULT_CONFIRM_TIMEOUT_SECONDS,
        headers: Mapping[str, str] | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_prefix = api_prefix
        self.event_log = event_log
        self.request_timeout = request_timeout
        self.confirm_timeout = confirm_timeout
        self._owns_client = http_client is None
        self._http = http_client or httpx.Client(
            base_url=self.base_url,
            timeout=httpx.Timeout(request_timeout),
            headers=dict(headers or {}),
        )
        self._state_lock = threading.Lock()
        self._last_event_at: datetime | None = None
        self._last_event_monotonic: float | None = None
        self._turn = 0

    # ---- observability -------------------------------------------------

    @property
    def last_event_at(self) -> datetime | None:
        """Wall-clock arrival of the most recent raw event, or ``None``.

        This is the only sound input to a stall judgement.  A confirm call that
        returned successfully, or a tool log that looks busy, both lie about
        whether the pipeline is still producing (findings F8, W2, D8-A).
        """
        with self._state_lock:
            return self._last_event_at

    def seconds_since_last_event(self) -> float | None:
        with self._state_lock:
            if self._last_event_monotonic is None:
                return None
            return time.monotonic() - self._last_event_monotonic

    def _mark_event(self) -> datetime:
        now = datetime.now(timezone.utc)
        with self._state_lock:
            self._last_event_at = now
            self._last_event_monotonic = time.monotonic()
        return now

    # ---- session lifecycle ---------------------------------------------

    def create_session(self, **payload: Any) -> str:
        response = self._request("POST", sessions_path(self.api_prefix), json=payload or {})
        body = _json_body(response, "session creation")
        for key in ("session_id", "sessionId", "id", "sid"):
            value = body.get(key)
            if isinstance(value, str) and value:
                if self.event_log is not None:
                    self.event_log.record_driver("session_created", {"session_id": value})
                return value
        raise BladeAIHttpError(
            "session creation response carried no session id; keys="
            f"{sorted(body)}"
        )

    def state(self, session_id: str) -> dict[str, Any]:
        response = self._request("GET", state_path(session_id, self.api_prefix))
        return _json_body(response, "session state")

    def cancel(self, session_id: str, *, reason: str = "platform_cancel") -> dict[str, Any]:
        """Cancel work on the server.

        This is server-wide, not session-scoped: BladeAI 0.7.0 cancels every
        running task when this is called (handoff s2.2).  It is safe only
        because each Trial owns a dedicated server instance; never point two
        Trials at one server.
        """
        if self.event_log is not None:
            self.event_log.record_driver(
                "cancel_requested", {"session_id": session_id, "reason": reason}
            )
        response = self._request("POST", cancel_path(session_id, self.api_prefix), json={})
        return _json_body(response, "cancel", allow_empty=True)

    # ---- confirmation channels -----------------------------------------
    #
    # WP-A exposes both gates as plain transport.  Which gate to answer, what
    # to answer, and the delivered=False fallback are WP-C's decisions.

    def answer_interrupt(self, session_id: str, interrupt_id: str, answer: str) -> dict[str, Any]:
        """Answer the intent gate.

        ``answer`` reaches the server's ``normalise_answer`` verbatim, which
        recognises only :data:`APPROVAL_WORDS` as approval and treats every
        other string -- an approval with a reason attached included -- as a
        rejection (finding F1).  The caller owns that choice; this method
        refuses to silently reshape it.
        """
        payload = {"interrupt_id": interrupt_id, "answer": answer}
        if self.event_log is not None:
            self.event_log.record_driver("interrupt_answer_sent", {
                "session_id": session_id,
                "interrupt_id": interrupt_id,
                "answer": answer,
                "is_approval_word": answer in APPROVAL_WORDS,
            })
        response = self._request(
            "POST", interrupt_path(session_id, self.api_prefix), json=payload
        )
        body = _json_body(response, "interrupt answer", allow_empty=True)
        if self.event_log is not None:
            self.event_log.record_driver("interrupt_answer_result", {
                "interrupt_id": interrupt_id,
                "delivered": body.get("delivered"),
            })
        return body

    def confirm_task(
        self,
        task_id: str,
        action: str,
        *,
        reason: str = "",
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Answer the execution gate, which is keyed by ``task_id``.

        The call blocks until the pipeline resumes -- tens of seconds is normal
        and 172 s was observed -- and the event stream stays silent throughout
        (finding F8), so the timeout here is generous by design.
        """
        if action not in CONFIRM_ACTIONS:
            raise BladeAIHttpError(
                f"confirm action must be one of {CONFIRM_ACTIONS}, got {action!r}"
            )
        if self.event_log is not None:
            self.event_log.record_driver("confirm_sent", {"task_id": task_id, "action": action})
        started = time.monotonic()
        response = self._request(
            "POST",
            confirm_path(task_id, self.api_prefix),
            json={"action": action, "reason": reason},
            timeout=self.confirm_timeout if timeout is None else timeout,
        )
        body = _json_body(response, "confirm", allow_empty=True)
        if self.event_log is not None:
            self.event_log.record_driver("confirm_result", {
                "task_id": task_id,
                "action": action,
                "blocked_seconds": round(time.monotonic() - started, 3),
            })
        return body

    # ---- the turn stream -----------------------------------------------

    def run_turn(
        self,
        session_id: str,
        prompt: str,
        *,
        timeout_seconds: float,
        observe: LineObserver | None = None,
        cancel_requested: CancelRequested | None = None,
        output_limit_bytes: int = MAX_TURN_OUTPUT_BYTES,
        permission_mode: str = "confirm",
        display_mode: str | None = None,
        dry_run: bool | None = None,
        planning_mode: str | None = None,
    ) -> TurnExecution:
        """Run one turn and forward every event as a JSON line.

        Each SSE frame becomes one newline-terminated JSON object handed to
        ``observe``, which is the identical contract
        ``subprocess_streaming_runner`` gives the adapter for a stdout line.
        """
        if not 1 <= output_limit_bytes <= MAX_TURN_OUTPUT_BYTES:
            raise BladeAIHttpError("turn output limit is outside allowed bounds")
        self._turn += 1
        turn = self._turn
        body: dict[str, Any] = {"input": prompt, "permission_mode": permission_mode}
        # Optional turn fields are omitted rather than sent as null so the
        # server applies its own defaults.
        for key, value in (
            ("display_mode", display_mode),
            ("dry_run", dry_run),
            ("planning_mode", planning_mode),
        ):
            if value is not None:
                body[key] = value
        if self.event_log is not None:
            self.event_log.record_driver("turn_started", {
                "session_id": session_id,
                "turn": turn,
                "timeout_seconds": timeout_seconds,
                "permission_mode": permission_mode,
                "prompt_bytes": len(prompt.encode("utf-8")),
            })

        collected = bytearray()
        frames: queue.Queue[SSEFrame | BaseException | None] = queue.Queue()
        stream_error: list[BaseException] = []
        started = time.monotonic()
        deadline = started + timeout_seconds
        reader_stop = threading.Event()

        def read_stream() -> None:
            try:
                with self._http.stream(
                    "POST",
                    turn_path(session_id, self.api_prefix),
                    json=body,
                    headers={"Accept": "text/event-stream"},
                    # Silence is expected between events; the consumer loop
                    # owns the deadline, so no read timeout is imposed here.
                    timeout=httpx.Timeout(self.request_timeout, read=None),
                ) as response:
                    if response.status_code >= 400:
                        response.read()
                        raise BladeAIHttpError(
                            f"turn request failed: HTTP {response.status_code} "
                            f"{response.text[:500]}"
                        )
                    for frame in iter_sse_frames(_iter_until(response, reader_stop)):
                        frames.put(frame)
            except BaseException as exc:  # surfaced on the consumer thread
                stream_error.append(exc)
                frames.put(exc)
            finally:
                frames.put(None)

        reader = threading.Thread(
            target=read_stream, name=f"bladeai-sse-{session_id}-{turn}", daemon=True
        )
        reader.start()

        timed_out = False
        cancelled = False
        truncated = False
        terminal: str | None = None
        event_count = 0
        cancel_deadline: float | None = None

        def request_cancel(reason: str) -> None:
            nonlocal cancelled, cancel_deadline
            if cancel_deadline is not None:
                return
            cancelled = True
            cancel_deadline = time.monotonic() + CANCEL_DRAIN_SECONDS
            try:
                self.cancel(session_id, reason=reason)
            except BladeAIHttpError as exc:
                if self.event_log is not None:
                    self.event_log.record_driver(
                        "cancel_failed", {"reason": reason, "error": str(exc)}
                    )

        while True:
            now = time.monotonic()
            if cancel_deadline is None:
                if now > deadline:
                    timed_out = True
                    request_cancel("turn_deadline")
                elif cancel_requested is not None and cancel_requested():
                    request_cancel("controller_cancelled")
            elif now > cancel_deadline:
                # The server owes us a terminal event after a cancel but must
                # not be allowed to hold the Trial open if it never sends one.
                if self.event_log is not None:
                    self.event_log.record_driver("cancel_drain_expired", {"turn": turn})
                break
            try:
                item = frames.get(timeout=POLL_INTERVAL_SECONDS)
            except queue.Empty:
                continue
            if item is None:
                break
            if isinstance(item, BaseException):
                continue
            received_at = self._mark_event()
            try:
                event = frame_to_event(item)
            except BladeAIProtocolError as exc:
                # A malformed frame is evidence of an upstream problem, not a
                # reason to abandon the stream.  Record and keep reading.
                if self.event_log is not None:
                    self.event_log.record_driver("malformed_event", {
                        "turn": turn, "error": str(exc), "raw": item.data[:2000],
                    })
                continue
            event_count += 1
            if self.event_log is not None:
                self.event_log.record_event(
                    event,
                    received_at=received_at,
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                    sse_event=item.event,
                    session_id=session_id,
                    turn=turn,
                )
            line = (json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
            remaining = max(0, output_limit_bytes - len(collected))
            accepted = line[:remaining]
            collected.extend(accepted)
            if len(accepted) < len(line):
                truncated = True
            if observe is not None and accepted:
                observe(bytes(accepted))
            if truncated:
                request_cancel("turn_output_limit")
                continue
            if is_terminal(event):
                terminal = event_type(event)
                break

        reader_stop.set()
        reader.join(timeout=CANCEL_DRAIN_SECONDS)
        failure = stream_error[0] if stream_error else None
        if failure is not None and self.event_log is not None:
            self.event_log.record_driver("stream_failed", {
                "turn": turn,
                "error_type": type(failure).__name__,
                "error": str(failure)[:1000],
            })
        if self.event_log is not None:
            self.event_log.record_driver("turn_finished", {
                "turn": turn,
                "events": event_count,
                "terminal_event": terminal,
                "timed_out": timed_out,
                "cancelled": cancelled,
                "output_truncated": truncated,
                "duration_seconds": round(time.monotonic() - started, 3),
            })
        if failure is not None and not cancelled and not timed_out:
            raise BladeAIHttpError(f"turn stream failed: {failure}") from failure
        returncode = 0 if terminal == "done" and not cancelled and not timed_out else 1
        return TurnExecution(
            returncode=returncode,
            stdout=bytes(collected),
            stderr=b"",
            timed_out=timed_out,
            cancelled=cancelled,
            output_truncated=truncated,
            event_count=event_count,
            terminal_event=terminal,
        )

    # ---- plumbing -------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        timeout: float | None = None,
    ) -> httpx.Response:
        try:
            response = self._http.request(
                method,
                path,
                json=json,
                timeout=httpx.Timeout(self.request_timeout if timeout is None else timeout),
            )
        except httpx.HTTPError as exc:
            raise BladeAIHttpError(f"{method} {path} failed: {exc}") from exc
        if response.status_code >= 400:
            raise BladeAIHttpError(
                f"{method} {path} failed: HTTP {response.status_code} {response.text[:500]}"
            )
        return response

    def close(self) -> None:
        if self._owns_client:
            self._http.close()

    def __enter__(self) -> BladeAIHttpClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def _iter_until(response: httpx.Response, stop: threading.Event):
    for chunk in response.iter_bytes():
        if stop.is_set():
            return
        yield chunk


def _json_body(response: httpx.Response, label: str, *, allow_empty: bool = False) -> dict[str, Any]:
    text = response.text.strip()
    if not text:
        if allow_empty:
            return {}
        raise BladeAIHttpError(f"{label} response body was empty")
    try:
        body = response.json()
    except ValueError as exc:
        raise BladeAIHttpError(f"{label} response was not JSON: {exc}") from exc
    if not isinstance(body, dict):
        raise BladeAIHttpError(f"{label} response was not a JSON object")
    return body


def bladeai_http_streaming_runner(
    client: BladeAIHttpClient,
    session_id: str,
    prompt: str,
    timeout_seconds: float,
    stdout_line_observer: LineObserver,
    cancel_requested: CancelRequested | None = None,
    **unsupported: object,
) -> TurnExecution:
    """Run exactly one turn and reject session-level kwargs that cannot apply.

    Mirrors :func:`harness.agent_exec.client.agent_exec_streaming_runner`.
    Accepting resume or feedback kwargs here would silently run only the first
    turn and corrupt the evidence, so they are refused.
    """
    if unsupported:
        raise TypeError(
            "bladeai_http_streaming_runner is a single-turn transport; "
            "inject bladeai_http_turn_executor into HarnessSession for: "
            + ", ".join(sorted(unsupported))
        )
    return client.run_turn(
        session_id,
        prompt,
        timeout_seconds=timeout_seconds,
        observe=stdout_line_observer,
        cancel_requested=cancel_requested,
    )


def bladeai_http_turn_executor(
    client: BladeAIHttpClient,
    session_id: str,
    *,
    permission_mode: str = "confirm",
    display_mode: str | None = None,
    dry_run: bool | None = None,
    planning_mode: str | None = None,
) -> Callable[..., TurnExecution]:
    """Return a per-turn transport for ``HarnessSession.turn_executor``.

    ``HarnessSession`` keeps owning resume, feedback, retry budget, transcript
    records and session-id lifetime; this closure only moves one turn onto the
    HTTP transport.  ``argv`` is unused -- a served harness has no command line
    -- and the turn's prompt arrives as ``stdin``, exactly as it does for the
    subprocess transport.
    """

    def execute(
        argv: Sequence[str],
        stdin: bytes,
        env: Mapping[str, str],
        timeout_seconds: int,
        observe: LineObserver,
        cancel: CancelRequested,
        *,
        output_limit_bytes: int = MAX_TURN_OUTPUT_BYTES,
    ) -> TurnExecution:
        return client.run_turn(
            session_id,
            stdin.decode("utf-8", errors="replace"),
            timeout_seconds=timeout_seconds,
            observe=observe,
            cancel_requested=cancel,
            output_limit_bytes=output_limit_bytes,
            permission_mode=permission_mode,
            display_mode=display_mode,
            dry_run=dry_run,
            planning_mode=planning_mode,
        )

    return execute
