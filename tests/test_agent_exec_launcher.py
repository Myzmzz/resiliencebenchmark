"""Trusted initializer transport tests, not Linux isolation qualification."""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
import sys

import pytest

from harness.agent_exec import launcher
from harness.agent_exec import server as server_module
from harness.agent_exec.protocol import StartRequest


def _config() -> dict:
    return {
        "argv": ["agent", "--input"], "env": {"TOKEN": "trial-only"},
        "sandbox": False, "sandbox_tmp": None, "cgroup": "/dedicated/run",
        "agent_uid": 10001, "agent_gid": 10004,
        "sandbox_uid": 10003, "sandbox_gid": 10003,
    }


def _config_fd(tmp_path: Path, content: bytes) -> int:
    path = tmp_path / "initializer-input"
    path.write_bytes(content)
    return os.open(path, os.O_RDONLY)


def test_initializer_closes_config_before_identity_change_and_exec(monkeypatch, tmp_path):
    config = _config()
    descriptor = _config_fd(tmp_path, json.dumps(config).encode())
    stages = []
    monkeypatch.setattr(launcher.os, "geteuid", lambda: 0)
    monkeypatch.setattr(launcher.sys, "path", list(sys.path))

    def initialize(identity, sandbox, temporary, cgroup):
        with pytest.raises(OSError):
            os.fstat(descriptor)
        assert identity.agent_uid == 10001
        assert sandbox is False and temporary is None
        assert cgroup == Path("/dedicated/run")
        stages.append("isolated")

    class Executed(BaseException):
        pass

    def execute(executable, argv, env):
        assert stages == ["isolated"]
        assert (executable, argv, env) == ("agent", config["argv"], config["env"])
        raise Executed

    monkeypatch.setattr(server_module, "_initialize_child", initialize)
    monkeypatch.setattr(launcher.os, "execvpe", execute)
    with pytest.raises(Executed):
        launcher.main([str(descriptor)])


@pytest.mark.parametrize("failure", ["untrusted_uid", "bad_json", "oversized", "root_target", "isolation_failed"])
def test_initializer_fails_closed_without_exec(failure, monkeypatch, tmp_path, capsys):
    config = _config()
    if failure == "root_target":
        config["agent_uid"] = 0
    raw = json.dumps(config).encode()
    if failure == "bad_json":
        raw = b"not JSON: private trial credential"
    if failure == "oversized":
        monkeypatch.setattr(launcher, "MAX_CONFIG_BYTES", 16)
    descriptor = _config_fd(tmp_path, raw)
    monkeypatch.setattr(launcher.os, "geteuid", lambda: 10001 if failure == "untrusted_uid" else 0)
    monkeypatch.setattr(launcher.sys, "path", list(sys.path))

    def initialize(*_args):
        raise OSError("sensitive controller path and trial credential")

    def forbidden_exec(*_args):
        pytest.fail("Agent must not execute after an initializer failure")

    monkeypatch.setattr(server_module, "_initialize_child", initialize)
    monkeypatch.setattr(launcher.os, "execvpe", forbidden_exec)
    assert launcher.main([str(descriptor)]) == 126
    with pytest.raises(OSError):
        os.fstat(descriptor)
    error = capsys.readouterr().err
    assert "initialization failed" in error
    assert "credential" not in error and "sensitive" not in error


def test_daemon_passes_only_initializer_fd_and_feeds_input_off_supervisor(monkeypatch, tmp_path):
    server = server_module.AgentExecServer(server_module.AgentExecServerConfig(
        socket_path=tmp_path / "unused.sock", trial_root=tmp_path,
        controller_uid=10000, agent_uid=10001, agent_gid=10004,
        allowed_env={"TOKEN"}, cgroup_path=tmp_path / "cgroups",
    ))
    request = StartRequest("run", ("agent", "--input"), {"TOKEN": "trial-only"}, b"prompt", ".", 1)
    received = {}
    cleared = []

    class InputBuffer(io.BytesIO):
        def close(self):
            received["stdin"] = self.getvalue()
            super().close()

    class Process:
        stdin = InputBuffer()

        def poll(self):
            return 0

    def start(argv, **kwargs):
        assert argv[:2] == [sys.executable, "-I"]
        assert Path(argv[2]).name == "launcher.py"
        assert kwargs["env"] == {}
        assert kwargs["close_fds"] and kwargs["start_new_session"]
        assert "preexec_fn" not in kwargs
        assert kwargs["pass_fds"] == (int(argv[3]),)
        received["fd"] = os.dup(int(argv[3]))
        return Process()

    def stream(*_args, **_kwargs):
        with os.fdopen(received["fd"], "rb") as descriptor:
            received["config"] = json.load(descriptor)

    monkeypatch.setattr(server_module.subprocess, "Popen", start)
    monkeypatch.setattr(server_module, "_create_child_cgroup", lambda *_args: tmp_path / "child")
    monkeypatch.setattr(server_module, "_kill_and_remove_cgroup", cleared.append)
    monkeypatch.setattr(server, "_stream_until_exit", stream)
    server._run_request(None, request)
    assert received["config"]["env"] == request.env
    assert received["config"]["argv"] == list(request.argv)
    assert received["stdin"] == b"prompt"
    assert cleared == [tmp_path / "child"]
    assert server.active_process_count == 0


def test_spawn_failure_closes_both_pipe_fds_and_cgroup(monkeypatch, tmp_path):
    server = server_module.AgentExecServer(server_module.AgentExecServerConfig(
        socket_path=tmp_path / "unused.sock", trial_root=tmp_path,
        controller_uid=10000, agent_uid=10001, allowed_env=set(),
        cgroup_path=tmp_path / "cgroups",
    ))
    request = StartRequest("run", ("agent",), {}, b"", ".", 1)
    descriptors = os.pipe()
    cleared = []
    monkeypatch.setattr(server_module.os, "pipe", lambda: descriptors)
    monkeypatch.setattr(server_module, "_create_child_cgroup", lambda *_args: tmp_path / "child")
    monkeypatch.setattr(server_module, "_kill_and_remove_cgroup", cleared.append)

    def fail(*_args, **_kwargs):
        raise OSError("cannot spawn")

    monkeypatch.setattr(server_module.subprocess, "Popen", fail)
    with pytest.raises(OSError, match="cannot spawn"):
        server._run_request(None, request)
    for descriptor in descriptors:
        with pytest.raises(OSError):
            os.fstat(descriptor)
    assert cleared == [tmp_path / "child"]
