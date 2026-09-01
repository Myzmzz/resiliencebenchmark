"""Native D0 Agent adapters with exact-prompt preservation."""

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from scripts.run_harness_trial import extract_json_objects, run_trial

from .common import CONFIRMATION_REPLY, utc_now


EventSink = Callable[[dict[str, Any]], None]


@dataclass(frozen=True)
class AdapterResult:
    status: str
    started_at: str
    finished_at: str
    process_status: str
    artifact_ref: str = ""
    error: str = ""
    agent_recovery_requested: bool = False
    tool_calls: int = 0
    confirmations: int = 0
    failure_code: str = ""
    needs_human: bool = False
    native_session_trace_captured: bool = False


class D0Adapter(Protocol):
    name: str

    def run(
        self,
        *,
        prompt: str,
        trial_id: str,
        artifact_dir: Path,
        event_sink: EventSink,
    ) -> AdapterResult: ...

    def cancel(self) -> bool: ...


def _tool_name(value: Mapping[str, Any]) -> str:
    nested = value.get("item")
    nested = nested if isinstance(nested, Mapping) else {}
    return str(
        value.get("tool")
        or value.get("name")
        or nested.get("tool")
        or nested.get("name")
        or ""
    )


def _is_recovery_tool(value: Mapping[str, Any]) -> bool:
    name = _tool_name(value).lower()
    return "destroy" in name or name.endswith("recover") or name.endswith("recover_experiment")


class HeadlessAdapter:
    def __init__(
        self,
        *,
        name: str,
        repo_root: Path,
        model_alias: str,
        parent_env: Mapping[str, str],
        artifact_root: Path,
        episode_file: Path,
        timeout_seconds: int,
    ):
        self.name = name
        self.repo_root = repo_root
        self.model_alias = model_alias
        self.parent_env = dict(parent_env)
        self.artifact_root = artifact_root
        self.episode_file = episode_file
        self.timeout_seconds = timeout_seconds
        self.cancel_event = threading.Event()

    def cancel(self) -> bool:
        self.cancel_event.set()
        return True

    def update_environment(self, values: Mapping[str, str]) -> None:
        self.parent_env.update({key: value for key, value in values.items() if value})

    def run(
        self,
        *,
        prompt: str,
        trial_id: str,
        artifact_dir: Path,
        event_sink: EventSink,
    ) -> AdapterResult:
        started_at = utc_now()
        self.cancel_event.clear()
        tool_calls = 0
        recovery = False

        def observe(item: Mapping[str, Any]) -> None:
            nonlocal tool_calls, recovery
            value = dict(item)
            marker = str(value.get("type") or value.get("event") or "")
            allowed = {
                "thread.started",
                "turn.started",
                "turn.completed",
                "response.created",
                "response.completed",
                "message",
                "assistant",
                "agent_message",
                "item.started",
                "item.completed",
                "mcp_tool_call",
                "tool_call",
                "tool_result",
                "function_call",
                "function_call_output",
            }
            if marker not in allowed:
                return
            tool = _tool_name(value)
            if tool:
                tool_calls += 1
            if _is_recovery_tool(value):
                recovery = True
            event_sink(
                {
                    "ts": utc_now(),
                    "actor": "agent",
                    "agent": self.name,
                    "kind": marker,
                    "tool": tool or None,
                    "payload": value,
                }
            )

        report = run_trial(
            repo_root=self.repo_root,
            harness_name=self.name,
            model_alias=self.model_alias,
            episode_file=self.episode_file,
            execute=True,
            artifact_root=artifact_dir.parent,
            timeout_seconds=self.timeout_seconds,
            parent_env=self.parent_env,
            event_observer=observe,
            trial_id=artifact_dir.name,
            prompt_text_override=prompt,
            require_structured_result=False,
            enforce_formal_runtime=False,
            cancel_requested=self.cancel_event.is_set,
        )
        diagnostic = "\n".join(
            path.read_text(encoding="utf-8", errors="replace")
            for path in (artifact_dir / "stdout.txt", artifact_dir / "stderr.txt")
            if path.is_file()
        ).lower()
        failure_code = ""
        if "model_not_found" in diagnostic or "no available channel for model" in diagnostic:
            failure_code = "MODEL_UNAVAILABLE"
        elif report.get("status") == "aborted_by_controller":
            failure_code = "CONTROLLER_CANCELLED"
        elif report.get("status") not in {"completed"}:
            failure_code = "ADAPTER_PROCESS_FAILED"
        needs_human = bool(
            re.search(
                r"(?:needs? (?:human|user)|permission required|waiting for (?:approval|confirmation)|please confirm)",
                diagnostic,
            )
        )
        return AdapterResult(
            status="finished" if report.get("status") == "completed" else "failed",
            started_at=started_at,
            finished_at=utc_now(),
            process_status=str(report.get("status") or "failed"),
            artifact_ref=str(report.get("artifactRef") or ""),
            error=str(report.get("error") or ""),
            agent_recovery_requested=recovery,
            tool_calls=tool_calls,
            failure_code=failure_code,
            needs_human=needs_human,
            native_session_trace_captured=bool(report.get("nativeSessionRefs")),
        )


def _request_json(method: str, url: str, payload: dict[str, Any] | None, timeout: float) -> Any:
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


def _post_interrupt(base_url: str, session_id: str, interrupt_id: str, answer: str) -> dict[str, Any]:
    url = f"{base_url}/api/v1/sessions/{urllib.parse.quote(session_id)}/interrupt"
    response: Any = None
    for attempt in range(6):
        response = _request_json(
            "POST", url, {"interrupt_id": interrupt_id, "answer": answer}, 30
        )
        if not isinstance(response, dict) or response.get("delivered") is not False:
            break
        time.sleep(0.1 * (attempt + 1))
    return response if isinstance(response, dict) else {"response": response}


class BladeAISessionAdapter:
    name = "bladeai"

    def __init__(self, *, base_url: str, model_alias: str, timeout_seconds: int = 720):
        self.base_url = base_url.rstrip("/")
        self.model_alias = model_alias
        self.timeout_seconds = timeout_seconds
        self.cancel_event = threading.Event()
        self.session_id = ""

    def cancel(self) -> bool:
        self.cancel_event.set()
        session_id = self.session_id
        if not session_id:
            return True
        try:
            _request_json(
                "DELETE",
                f"{self.base_url}/api/v1/sessions/{urllib.parse.quote(session_id)}",
                None,
                15,
            )
            return True
        except Exception:
            return False

    @staticmethod
    def _event_payload(lines: list[str]) -> Any:
        raw = "\n".join(lines)
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw

    @staticmethod
    def _interrupt_node(payload: Any) -> str:
        if not isinstance(payload, dict):
            return ""
        return str(
            payload.get("node")
            or payload.get("type")
            or (payload.get("payload") or {}).get("type")
            or ""
        )

    @staticmethod
    def _interrupt_id(payload: Any) -> str:
        if not isinstance(payload, dict):
            return ""
        for mapping in (payload, payload.get("payload") or {}):
            if not isinstance(mapping, dict):
                continue
            for key in ("interrupt_id", "interruptId", "task_id", "taskId", "id"):
                if mapping.get(key):
                    return str(mapping[key])
        return ""

    def run(
        self,
        *,
        prompt: str,
        trial_id: str,
        artifact_dir: Path,
        event_sink: EventSink,
    ) -> AdapterResult:
        started_at = utc_now()
        self.cancel_event.clear()
        session = _request_json(
            "POST",
            f"{self.base_url}/api/v1/sessions",
            {"namespace": "otel-demo", "model_name": self.model_alias},
            30,
        )
        if not isinstance(session, dict):
            raise RuntimeError("BladeAI session endpoint returned non-object")
        session_id = str(
            session.get("session_id")
            or session.get("id")
            or (session.get("data") or {}).get("session_id")
            or ""
        )
        if not session_id:
            raise RuntimeError("BladeAI session endpoint returned no session id")
        self.session_id = session_id
        confirmations = 0
        tool_calls = 0
        recovery = False
        error = ""
        terminal = ""

        try:
            for turn_index, turn_input in enumerate((prompt, CONFIRMATION_REPLY), start=1):
                url = f"{self.base_url}/api/v1/sessions/{urllib.parse.quote(session_id)}/turn"
                request = urllib.request.Request(
                    url,
                    data=json.dumps(
                        {
                            "input": turn_input,
                            "permission_mode": "confirm",
                            "display_mode": "working",
                            "dry_run": False,
                        },
                        ensure_ascii=False,
                    ).encode(),
                    method="POST",
                    headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
                )
                event_name = "message"
                data_lines: list[str] = []

                def flush() -> None:
                    nonlocal event_name, data_lines, confirmations, tool_calls, recovery, error, terminal
                    if not data_lines:
                        event_name = "message"
                        return
                    payload = self._event_payload(data_lines)
                    effective = event_name
                    if effective == "message" and isinstance(payload, dict):
                        effective = str(payload.get("type") or "message")
                    record = {
                        "ts": utc_now(),
                        "actor": "agent",
                        "agent": self.name,
                        "turn": turn_index,
                        "kind": effective,
                        "payload": payload,
                    }
                    event_sink(record)
                    text = json.dumps(payload, ensure_ascii=False).lower()
                    if effective in {"tool_start", "tool_call"}:
                        tool_calls += 1
                    if any(marker in text for marker in ("blade_destroy", "recover_graph", "recovery_requested")):
                        recovery = True
                    if effective in {"confirm", "confirmation", "interrupt"}:
                        node = self._interrupt_node(payload)
                        interrupt_id = self._interrupt_id(payload) or trial_id
                        answer = (
                            "rejected"
                            if node in {"tool_screener", "plan_change_confirm"}
                            else "approved"
                        )
                        response = _post_interrupt(
                            self.base_url, session_id, interrupt_id, answer
                        )
                        confirmations += 1
                        event_sink(
                            {
                                "ts": utc_now(),
                                "actor": "harness",
                                "agent": self.name,
                                "kind": "approval_answered",
                                "node": node,
                                "answer": answer,
                                "interrupt_id": interrupt_id,
                                "response": response,
                            }
                        )
                    if effective in {"result", "done"}:
                        terminal = effective
                    if effective == "error":
                        error = text[:1000]
                    event_name = "message"
                    data_lines = []

                deadline = time.monotonic() + self.timeout_seconds
                with urllib.request.urlopen(request, timeout=60) as response:
                    while True:
                        if self.cancel_event.is_set():
                            raise TimeoutError("BladeAI turn cancelled by D0 Controller")
                        if time.monotonic() > deadline:
                            raise TimeoutError("BladeAI turn exceeded timeout")
                        line = response.readline()
                        if not line:
                            flush()
                            break
                        decoded = line.decode("utf-8", errors="replace").rstrip("\r\n")
                        if decoded == "":
                            flush()
                        elif decoded.startswith("event:"):
                            event_name = decoded[6:].strip() or "message"
                        elif decoded.startswith("data:"):
                            data_lines.append(decoded[5:].lstrip())
                if error:
                    break
        except (urllib.error.URLError, TimeoutError) as exc:
            error = f"{type(exc).__name__}: {exc}"
        finally:
            try:
                _request_json(
                    "DELETE",
                    f"{self.base_url}/api/v1/sessions/{urllib.parse.quote(session_id)}",
                    None,
                    15,
                )
            except Exception:
                pass
            self.session_id = ""
        return AdapterResult(
            status="finished" if not error else "failed",
            started_at=started_at,
            finished_at=utc_now(),
            process_status=terminal or ("failed" if error else "eof"),
            artifact_ref=artifact_dir.name,
            error=error,
            agent_recovery_requested=recovery,
            tool_calls=tool_calls,
            confirmations=confirmations,
        )


def adapter_models(env: Mapping[str, str]) -> dict[str, str]:
    return {
        "bladeai": env.get("RESBENCH_D0_BLADEAI_MODEL", "gpt-5.6-sol"),
        "codex": env.get("RESBENCH_D0_CODEX_MODEL", "gpt-5.6-sol"),
        "claude-code": env.get("RESBENCH_D0_CLAUDE_MODEL", "claude-opus-5"),
        "deepseek-harness": env.get("RESBENCH_D0_DSH_MODEL", "gpt-5.6-sol"),
    }
