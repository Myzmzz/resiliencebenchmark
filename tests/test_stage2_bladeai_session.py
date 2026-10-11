from __future__ import annotations

from pathlib import Path

from stage2_service import bladeai_session
from stage2_service.bladeai_session import (
    BladeAIServerProcess,
    BladeAISessionClient,
)


class Response:
    def __init__(self, lines: list[bytes]):
        self.lines = iter(lines)

    def readline(self):
        return next(self.lines, b"")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None


def test_bladeai_session_client_captures_native_sse_and_cleans_session(monkeypatch):
    requests = []

    def fake_request(method, url, payload, *, timeout):
        requests.append((method, url, payload, timeout))
        if method == "POST" and url.endswith("/sessions"):
            return {"sid": "sess-static-profile"}
        if method == "GET" and url.endswith("/state"):
            return {"id": "sess-static-profile", "namespace": "otel-demo"}
        return {"ok": True}

    stream = Response(
        [
            b'data: {"type":"tool_start","tool_name":"k8s_ro__k8s_list_resources"}\n',
            b"\n",
            b'data: {"type":"token","content":"{\\"status\\":\\"completed\\"}"}\n',
            b"\n",
            b'data: {"type":"done"}\n',
            b"\n",
        ]
    )
    monkeypatch.setattr(bladeai_session, "_request_json", fake_request)
    monkeypatch.setattr(bladeai_session.urllib.request, "urlopen", lambda *_a, **_k: stream)
    observed = []

    result = BladeAISessionClient(
        "http://127.0.0.1:8091", timeout_seconds=30
    ).run(
        prompt="inspect the current state",
        namespace="otel-demo",
        model_name="gpt-5.6-sol",
        event_observer=observed.append,
    )

    assert result.completed is True
    assert result.session_id == "sess-static-profile"
    assert result.assistant_text == '{"status":"completed"}'
    assert result.state["namespace"] == "otel-demo"
    assert [item["type"] for item in observed] == ["tool_start", "token", "done"]
    assert any(method == "DELETE" for method, *_rest in requests)


def test_bladeai_session_client_answers_only_expected_native_approvals(monkeypatch):
    requests = []

    def fake_request(method, url, payload, *, timeout):
        requests.append((method, url, payload, timeout))
        if method == "POST" and url.endswith("/sessions"):
            return {"sid": "sess-approval"}
        if method == "GET" and url.endswith("/state"):
            return {"id": "sess-approval"}
        return {"delivered": True}

    stream = Response(
        [
            b'data: {"type":"interrupt","node":"confirmation_gate","interrupt_id":"task-1"}\n',
            b"\n",
            b'data: {"type":"done"}\n',
            b"\n",
        ]
    )
    monkeypatch.setattr(bladeai_session, "_request_json", fake_request)
    monkeypatch.setattr(bladeai_session.urllib.request, "urlopen", lambda *_a, **_k: stream)
    observed = []

    result = BladeAISessionClient(
        "http://127.0.0.1:8091", timeout_seconds=30
    ).run(
        prompt="run the authorized task",
        namespace="otel-demo",
        model_name="gpt-5.6-sol",
        event_observer=observed.append,
    )

    approval = next(item for item in observed if item["type"] == "harness_approval")
    assert approval["answer"] == "approved"
    assert any(
        method == "POST"
        and url.endswith("/interrupt")
        and payload == {"interrupt_id": "task-1", "answer": "approved"}
        for method, url, payload, _timeout in requests
    )
    assert result.completed is True


class Process:
    def __init__(self, argv, **kwargs):
        self.argv = argv
        self.kwargs = kwargs
        self.returncode = None
        self.terminated = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.returncode = -9


def test_bladeai_server_process_uses_explicit_runtime_and_stops(monkeypatch, tmp_path):
    python = tmp_path / "python"
    python.write_text("runtime", encoding="utf-8")
    process = None

    def fake_popen(argv, **kwargs):
        nonlocal process
        process = Process(argv, **kwargs)
        return process

    monkeypatch.setattr(bladeai_session, "_reserve_loopback_port", lambda: 19091)
    monkeypatch.setattr(bladeai_session.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(bladeai_session, "_wait_for_health", lambda *_a, **_k: None)
    log_path = tmp_path / "server.log"

    with BladeAIServerProcess(
        python=python,
        environment={"HOME": str(tmp_path)},
        log_path=log_path,
    ) as server:
        assert server.base_url == "http://127.0.0.1:19091"
        assert process is not None
        assert process.argv[:3] == [str(python.resolve()), "-m", "chaos_agent.server.app"]
        assert process.kwargs["env"]["HOME"] == str(tmp_path)

    assert process is not None and process.terminated is True
    assert log_path.stat().st_mode & 0o777 == 0o600
