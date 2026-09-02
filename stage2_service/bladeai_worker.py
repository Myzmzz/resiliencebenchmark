"""Isolated BladeAI L4 subprocess entrypoint used by the Stage-2 service."""

from __future__ import annotations

import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace


def emit(kind: str, payload: dict) -> None:
    print(
        json.dumps(
            {"type": "stage2_bladeai_event", "kind": kind, "payload": payload},
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        flush=True,
    )


class Step:
    def __init__(self, name: str, attrs: dict):
        self.name = name
        self.attrs = dict(attrs)


class Runtime:
    def __init__(self):
        self.trajectory = SimpleNamespace(
            thought_trace=[], state_transitions=[], agent_specific={}
        )
        self.tool = SimpleNamespace(execute=lambda *_args, **_kwargs: None)

    @contextmanager
    def step(self, name: str, attrs: dict | None = None):
        step = Step(name, attrs or {})
        emit("step_start", {"name": name, "attrs": step.attrs})
        try:
            yield step
        finally:
            emit("step_end", {"name": name, "attrs": step.attrs})

    def emit_event(self, kind: str, payload: dict):
        emit(kind, payload)

    def require_approval(self, risk_level: str):
        emit("approval", {"risk_level": risk_level, "decision": "approved"})
        return "approved"

    def finish(self, status: str):
        emit("finish", {"status": status})

    def heal(self, *_args, **_kwargs):
        return None


def main(argv: list[str] | None = None) -> int:
    values = list(sys.argv[1:] if argv is None else argv)
    if len(values) != 1:
        print("usage: python -m stage2_service.bladeai_worker <request.json>", file=sys.stderr)
        return 2
    request = json.loads(Path(values[0]).read_text(encoding="utf-8"))
    try:
        from chaos_agent.l4.agent import L4ResilienceAgent
        from chaos_agent.l4.schemas import L4TestTask
    except ImportError as exc:
        emit("fatal", {"error": f"BladeAI import failed: {type(exc).__name__}"})
        return 2
    payload = {
        "namespace": request["target"]["namespace"],
        "target_names": [request["target"]["name"]],
        "kubeconfig": request["kubeconfig"],
        "direct": False,
        "auto_recover": True,
    }
    managed_fault = request.get("managed_fault")
    if isinstance(managed_fault, dict):
        payload.update(managed_fault)
    task = L4TestTask(
        task_id=request["trial_id"],
        intent=request["intent"],
        target=request["target"]["name"],
        test_type="resilience-fault-injection",
        payload=payload,
    )
    runtime = Runtime()
    agent = L4ResilienceAgent()
    agent.prepare(runtime, task)
    result = agent.execute(runtime, task)
    agent.cleanup(runtime, task)
    print(
        json.dumps(
            {
                "type": "stage2_bladeai_result",
                "status": result.status,
                "task_id": result.task_id,
                "trajectory_id": result.trajectory_id,
                "summary": result.summary,
                "error": (
                    None
                    if result.error is None
                    else {
                        "code": result.error.code,
                        "message": result.error.message,
                        "recoverable": result.error.recoverable,
                    }
                ),
                "extras": result.extras,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        flush=True,
    )
    return 0 if result.status in {"passed", "degraded"} else 1


if __name__ == "__main__":
    exit_code = main()
    sys.stdout.flush()
    sys.stderr.flush()
    # BladeAI may leave exporter/checkpoint threads alive after agent.cleanup.
    # This is an isolated worker process and all evidence is flushed above.
    os._exit(exit_code)
