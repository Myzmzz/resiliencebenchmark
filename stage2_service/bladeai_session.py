"""Trial-local BladeAI server and Session/Turn/SSE client."""

from __future__ import annotations

import json
import socket
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping


class BladeAISessionError(RuntimeError):
    pass


EventObserver = Callable[[dict[str, Any]], None]


@dataclass(frozen=True)
class BladeAISessionResult:
    session_id: str
    completed: bool
    assistant_text: str
    events: tuple[dict[str, Any], ...]
    state: dict[str, Any]
    error: str = ""


class BladeAIServerProcess:
    """Own one isolated BladeAI HTTP server process for a single Trial."""

    def __init__(
        self,
        *,
        python: Path,
        environment: Mapping[str, str],
        log_path: Path,
        startup_timeout_seconds: int = 30,
    ) -> None:
        self.python = python.resolve()
        self.environment = dict(environment)
        self.log_path = log_path.resolve()
        self.startup_timeout_seconds = startup_timeout_seconds
        self.process: subprocess.Popen[bytes] | None = None
        self._log = None
        self.port = 0

    @property
    def base_url(self) -> str:
        if not self.port:
            raise BladeAISessionError("BladeAI server has not started")
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> BladeAIServerProcess:
        if not self.python.is_file():
            raise BladeAISessionError("configured BladeAI Python runtime is unavailable")
        self.log_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._log = self.log_path.open("ab")
        self.log_path.chmod(0o600)
        self.port = _reserve_loopback_port()
        try:
            self.process = subprocess.Popen(
                [
                    str(self.python),
                    "-m",
                    "chaos_agent.server.app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(self.port),
                ],
                stdin=subprocess.DEVNULL,
                stdout=self._log,
                stderr=subprocess.STDOUT,
                env=self.environment,
            )
            _wait_for_health(
                self.process,
                f"{self.base_url}/api/v1/health",
                self.startup_timeout_seconds,
            )
        except Exception:
            self.stop()
            raise
        return self

    def stop(self) -> None:
        process = self.process
        self.process = None
        if process is not None and process.poll() is None:
            try:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            except OSError:
                try:
                    process.kill()
                    process.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    pass
        if self._log is not None:
            try:
                self._log.close()
            except OSError:
                pass
            self._log = None

    def __enter__(self) -> BladeAIServerProcess:
        return self.start()

    def __exit__(self, *_args: object) -> None:
        self.stop()


class BladeAISessionClient:
    """Run one non-interactive BladeAI conversation and retain native SSE."""

    def __init__(self, base_url: str, *, timeout_seconds: int) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def run(
        self,
        *,
        prompt: str,
        namespace: str,
        model_name: str,
        event_observer: EventObserver,
    ) -> BladeAISessionResult:
        session = _request_json(
            "POST",
            f"{self.base_url}/api/v1/sessions",
            {"namespace": namespace, "model_name": model_name},
            timeout=30,
        )
        if not isinstance(session, dict):
            raise BladeAISessionError("BladeAI session endpoint returned non-object")
        session_id = str(
            session.get("sid")
            or session.get("session_id")
            or session.get("id")
            or (session.get("data") or {}).get("session_id")
            or ""
        )
        if not session_id:
            raise BladeAISessionError("BladeAI session endpoint returned no session id")

        events: list[dict[str, Any]] = []
        tokens: list[str] = []
        messages: list[str] = []
        state: dict[str, Any] = {}
        error = ""
        completed = False
        try:
            request = urllib.request.Request(
                f"{self.base_url}/api/v1/sessions/{urllib.parse.quote(session_id)}/turn",
                data=json.dumps(
                    {
                        "input": prompt,
                        "permission_mode": "auto",
                        "display_mode": "working",
                    },
                    ensure_ascii=False,
                ).encode("utf-8"),
                method="POST",
                headers={
                    "Content-Type": "application/json",
                    "Accept": "text/event-stream",
                },
            )
            deadline = time.monotonic() + self.timeout_seconds
            with urllib.request.urlopen(
                request, timeout=max(60, self.timeout_seconds)
            ) as response:
                for event in _read_sse_events(response, deadline):
                    events.append(event)
                    event_observer(event)
                    event_type = str(event.get("type") or "")
                    if event_type == "token":
                        tokens.append(str(event.get("content") or ""))
                    elif event_type in {"assistant", "assistant_message", "message", "result"}:
                        content = event.get("content") or event.get("message")
                        if isinstance(content, str) and content:
                            messages.append(content)
                    elif event_type == "error":
                        error = str(event.get("message") or event.get("content") or event)
                    elif event_type in {"confirm", "confirmation", "interrupt"}:
                        node = _interrupt_node(event)
                        interrupt_id = _interrupt_id(event) or session_id
                        answer = (
                            "approved"
                            if node in {"intent_confirm", "confirmation_gate"}
                            else "rejected"
                        )
                        response = _request_json(
                            "POST",
                            f"{self.base_url}/api/v1/sessions/{urllib.parse.quote(session_id)}/interrupt",
                            {"interrupt_id": interrupt_id, "answer": answer},
                            timeout=15,
                        )
                        approval = {
                            "type": "harness_approval",
                            "node": node,
                            "interrupt_id": interrupt_id,
                            "answer": answer,
                            "delivered": not (
                                isinstance(response, dict)
                                and response.get("delivered") is False
                            ),
                        }
                        events.append(approval)
                        event_observer(approval)
                    elif event_type == "done":
                        completed = True
            current = _request_json(
                "GET",
                f"{self.base_url}/api/v1/sessions/{urllib.parse.quote(session_id)}/state",
                None,
                timeout=15,
            )
            if isinstance(current, dict):
                state = current
        except (TimeoutError, urllib.error.URLError) as exc:
            error = f"{type(exc).__name__}: {exc}"
        finally:
            try:
                _request_json(
                    "DELETE",
                    f"{self.base_url}/api/v1/sessions/{urllib.parse.quote(session_id)}",
                    None,
                    timeout=15,
                )
            except Exception:
                pass

        return BladeAISessionResult(
            session_id=session_id,
            completed=completed and not error,
            assistant_text="".join(tokens) if tokens else (messages[-1] if messages else ""),
            events=tuple(events),
            state=state,
            error=error,
        )


def _read_sse_events(response: Any, deadline: float):
    data_lines: list[str] = []
    while True:
        if time.monotonic() > deadline:
            raise TimeoutError("BladeAI turn exceeded timeout")
        raw = response.readline()
        if not raw:
            if data_lines:
                yield _decode_event(data_lines)
            return
        line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        if not line:
            if data_lines:
                yield _decode_event(data_lines)
                data_lines = []
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())


def _decode_event(data_lines: list[str]) -> dict[str, Any]:
    raw = "\n".join(data_lines)
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {"type": "message", "content": raw}
    return value if isinstance(value, dict) else {"type": "message", "content": value}


def _interrupt_node(event: Mapping[str, Any]) -> str:
    payload = event.get("payload")
    nested = payload if isinstance(payload, Mapping) else {}
    return str(event.get("node") or nested.get("node") or nested.get("type") or "")


def _interrupt_id(event: Mapping[str, Any]) -> str:
    payload = event.get("payload")
    for value in (event, payload if isinstance(payload, Mapping) else {}):
        for key in ("interrupt_id", "interruptId", "task_id", "taskId", "id"):
            if value.get(key):
                return str(value[key])
    return ""


def _request_json(
    method: str,
    url: str,
    payload: dict[str, Any] | None,
    *,
    timeout: float,
) -> Any:
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode()
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read().decode("utf-8", errors="replace")
    return json.loads(raw) if raw else None


def _reserve_loopback_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_health(
    process: subprocess.Popen[bytes], url: str, timeout_seconds: int
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise BladeAISessionError("BladeAI server exited before becoming ready")
        try:
            value = _request_json("GET", url, None, timeout=2)
        except (OSError, urllib.error.URLError, json.JSONDecodeError):
            time.sleep(0.1)
            continue
        if isinstance(value, dict) and value.get("status") == "ok":
            return
        time.sleep(0.1)
    raise BladeAISessionError("BladeAI server did not become ready")
