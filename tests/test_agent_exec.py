"""Contract tests for the isolated Unix execution proxy.

The production security gate requires Linux peer credentials and distinct
controller/daemon/agent UIDs.  The streaming round-trip is consequently a
Linux-only qualification test; macOS development must not silently emulate it.
"""

from __future__ import annotations

import os
import socket
import sys
from pathlib import Path

import pytest

from harness.agent_exec.client import AgentExecClient, AgentExecClientError
from harness.agent_exec.protocol import MAX_FRAME_BYTES, ProtocolError, decode_frame, encode_frame, recv_frame
from harness.agent_exec.server import AgentExecServer, AgentExecServerConfig


def test_protocol_rejects_oversized_and_non_object_frames() -> None:
    with pytest.raises(ProtocolError, match="frame exceeds"):
        decode_frame((1_048_577).to_bytes(4, "big") + b"x")
    with pytest.raises(ProtocolError, match="JSON object"):
        decode_frame(encode_frame(["not", "an", "object"]))


def test_protocol_preserves_a_delayed_partial_frame_across_socket_timeout() -> None:
    """Polling callers retain the body bytes received before a timeout."""
    reader, writer = socket.socketpair()
    try:
        frame = encode_frame({"type": "exit", "returncode": 0})
        reader.settimeout(0.01)
        writer.sendall(frame[:7])  # full header plus a partial JSON body
        pending = bytearray()
        with pytest.raises(TimeoutError):
            recv_frame(reader, buffer=pending)
        assert pending == bytearray(frame[:7])
        writer.sendall(frame[7:])
        assert recv_frame(reader, buffer=pending) == {"type": "exit", "returncode": 0}
        assert pending == bytearray()
    finally:
        reader.close()
        writer.close()


def test_protocol_rejects_oversized_declared_frame_before_accumulating_body() -> None:
    reader, writer = socket.socketpair()
    try:
        writer.sendall((MAX_FRAME_BYTES + 1).to_bytes(4, "big"))
        with pytest.raises(ProtocolError, match="frame exceeds"):
            recv_frame(reader, buffer=bytearray())
    finally:
        reader.close()
        writer.close()


def test_client_closes_after_fixed_cancel_grace_without_terminal_event(monkeypatch) -> None:
    """A silent daemon is disconnected so its server-side cleanup can run."""
    import harness.agent_exec.client as client_module

    class SilentSocket:
        def __init__(self):
            self.sent: list[bytes] = []
            self.closed = False

        def connect(self, _path):
            return None

        def settimeout(self, _seconds):
            return None

        def sendall(self, data):
            self.sent.append(bytes(data))

        def recv(self, _count):
            raise TimeoutError()

        def close(self):
            self.closed = True

    silent = SilentSocket()
    moments = iter((0.0, 1.0, 7.0))
    monkeypatch.setattr(client_module.socket, "socket", lambda *_args, **_kwargs: silent)
    monkeypatch.setattr(client_module, "_assert_linux_peer", lambda *_args: None)
    monkeypatch.setattr(client_module.time, "monotonic", lambda: next(moments))

    with pytest.raises(AgentExecClientError, match="terminal event after cancellation.*cleanup is unconfirmed"):
        AgentExecClient("/tmp/agent.sock", expected_server_uid=0).run(
            ["agent"], b"", {}, 1, cancel_requested=lambda: True,
        )

    assert [decode_frame(frame)["type"] for frame in silent.sent] == ["start", "cancel"]
    assert silent.closed is True


def test_protocol_rejects_bad_request_before_execution() -> None:
    from harness.agent_exec.protocol import parse_start_request

    with pytest.raises(ProtocolError, match="argv"):
        parse_start_request({"type": "start", "request_id": "test", "argv": "not-a-list", "env": {}, "stdin": "", "cwd": ".", "timeout_seconds": 1})
    with pytest.raises(ProtocolError, match="environment key"):
        parse_start_request(
            {"type": "start", "request_id": "test", "argv": ["x"], "env": {"BAD-KEY": "x"}, "stdin": "", "cwd": ".", "timeout_seconds": 1}
        )


def test_client_marks_exact_native_output_limit_and_sends_cancellation(monkeypatch) -> None:
    import harness.agent_exec.client as client_module
    from harness.agent_exec.protocol import parse_start_request

    class FloodSocket:
        def __init__(self):
            self.sent = []
            self.frames = [
                encode_frame({"type": "stream", "stream": "stdout", "data": "MTIzNDU2Nzg="}),
                encode_frame({"type": "exit", "returncode": 0, "timed_out": False, "cancelled": False, "output_truncated": True}),
            ]

        def connect(self, _path): pass
        def settimeout(self, _value): pass
        def sendall(self, value): self.sent.append(bytes(value))
        def recv(self, count):
            if not self.frames:
                return b""
            current = self.frames[0]
            result, self.frames[0] = current[:count], current[count:]
            if not self.frames[0]: self.frames.pop(0)
            return result
        def close(self): pass

    sock = FloodSocket()
    monkeypatch.setattr(client_module.socket, "socket", lambda *_args, **_kwargs: sock)
    monkeypatch.setattr(client_module, "_assert_linux_peer", lambda *_args: None)
    monkeypatch.setattr(client_module, "MAX_NATIVE_OUTPUT_BYTES", 8)
    monkeypatch.setattr(client_module.time, "monotonic", lambda: 0.0)

    result = client_module.AgentExecClient("/agent.sock", expected_server_uid=0).run(
        ["agent"], b"", {}, 1, output_limit_bytes=8,
    )

    assert result.stdout == b"12345678"
    assert result.output_truncated is True
    assert any(b"native_output_limit" in frame for frame in sock.sent)
    with pytest.raises(ProtocolError, match="relative"):
        parse_start_request(
            {"type": "start", "request_id": "test", "argv": ["x"], "env": {}, "stdin": "", "cwd": "/tmp", "timeout_seconds": 1}
        )
    with pytest.raises(ProtocolError, match="NUL"):
        parse_start_request(
            {"type": "start", "request_id": "test", "argv": ["bad\x00argv"], "env": {}, "stdin": "", "cwd": ".", "timeout_seconds": 1}
        )


def test_trial_cwd_rejects_symlink_escape(tmp_path: Path) -> None:
    from harness.agent_exec.server import _safe_trial_cwd

    root = tmp_path / "trial"
    root.mkdir()
    (root / "escape").symlink_to(tmp_path)
    with pytest.raises(RuntimeError, match="symlink"):
        _safe_trial_cwd(root, "escape")


def test_agent_output_normalization_makes_only_regular_shared_files_controller_readable(tmp_path: Path) -> None:
    from harness.agent_exec.shared_trial import normalize_shared_trial_tree

    root = tmp_path / "trial"
    nested = root / "native-session"
    nested.mkdir(parents=True)
    report = nested / "result.json"
    report.write_text("{}", encoding="utf-8")
    report.chmod(0o600)
    executable = nested / "helper"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o700)

    normalize_shared_trial_tree(root, os.getgid())

    assert (root.stat().st_mode & 0o7777) == 0o2770
    assert (nested.stat().st_mode & 0o7777) == 0o2770
    assert report.stat().st_mode & 0o777 == 0o660
    assert executable.stat().st_mode & 0o777 == 0o760


def test_agent_output_normalization_rejects_symlink_before_controller_can_read_it(tmp_path: Path) -> None:
    from harness.agent_exec.shared_trial import normalize_shared_trial_tree

    root = tmp_path / "trial"
    root.mkdir()
    (root / "indirect").symlink_to(tmp_path)

    with pytest.raises(RuntimeError, match="symlinks"):
        normalize_shared_trial_tree(root, os.getgid())


def test_controller_accepts_already_normalized_agent_tree_without_metadata_mutation(tmp_path: Path, monkeypatch) -> None:
    from harness.agent_exec.shared_trial import normalize_shared_trial_tree

    root = tmp_path / "trial"
    root.mkdir()
    report = root / "result.json"
    report.write_text("{}", encoding="utf-8")
    normalize_shared_trial_tree(root, os.getgid())
    monkeypatch.setattr("harness.agent_exec.shared_trial.os.chown", lambda *_args: pytest.fail("chown must not run"))
    monkeypatch.setattr("harness.agent_exec.shared_trial.os.chmod", lambda *_args: pytest.fail("chmod must not run"))

    normalize_shared_trial_tree(root, os.getgid())


def test_controller_rejects_non_owner_unormalized_agent_tree_instead_of_ignoring_it(tmp_path: Path, monkeypatch) -> None:
    from harness.agent_exec.shared_trial import normalize_shared_trial_tree

    root = tmp_path / "trial"
    root.mkdir()
    (root / "result.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr("harness.agent_exec.shared_trial.os.chown", lambda *_args: (_ for _ in ()).throw(PermissionError()))

    with pytest.raises(RuntimeError, match="not controller-normalized"):
        normalize_shared_trial_tree(root, os.getgid() + 1)


def test_initializer_joins_child_cgroup_before_uid_drop(monkeypatch, tmp_path: Path) -> None:
    import harness.agent_exec.server as server_module

    events = []
    config = AgentExecServerConfig(
        socket_path=tmp_path / "agent.sock",
        trial_root=tmp_path / "trials",
        controller_uid=10001,
        agent_uid=10002,
        agent_gid=10004,
        allowed_env=set(),
    )
    monkeypatch.setattr(server_module, "_add_to_cgroup", lambda _path, _pid: events.append("cgroup"))
    monkeypatch.setattr(server_module, "_set_no_new_privileges_and_drop_bounding_caps", lambda: events.append("caps"))
    monkeypatch.setattr(server_module.os, "setgroups", lambda _groups: events.append("groups"))
    monkeypatch.setattr(server_module.os, "setgid", lambda _gid: events.append("gid"))
    monkeypatch.setattr(server_module.os, "setuid", lambda _uid: events.append("uid"))
    monkeypatch.setattr(server_module.os, "umask", lambda _mask: events.append("umask"))

    server_module._initialize_child(config, False, None, tmp_path / "child-cgroup")

    assert events == ["cgroup", "caps", "groups", "gid", "uid", "umask"]


def test_parent_exit_with_open_pipe_clears_cgroup_before_terminal_event(monkeypatch, tmp_path: Path) -> None:
    import harness.agent_exec.server as server_module

    order = []

    class Pipe:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True
            order.append("pipe_closed")

    class Key:
        def __init__(self, fileobj):
            self.fileobj = fileobj
            self.data = "stdout"

    class Selector:
        def __init__(self):
            self.values = {}

        def register(self, fileobj, _events, data):
            key = Key(fileobj)
            key.data = data
            self.values[fileobj] = key

        def get_map(self):
            return self.values

        def select(self, timeout=0):
            return []

        def unregister(self, fileobj):
            self.values.pop(fileobj)

        def close(self):
            order.append("selector_closed")

    class Process:
        def __init__(self):
            self.stdout = Pipe()
            self.stderr = Pipe()
            self.pid = 123

        def poll(self):
            return 0  # Parent has exited, but its descendant kept pipes open.

        def wait(self, timeout=None):
            order.append("wait")
            return 0

    class ImmediateThread:
        def __init__(self, *, target, daemon):
            self.target = target

        def start(self):
            # Keep the control channel open: this reproduces a parent that
            # exited while a descendant still owns stdout/stderr.
            return None

    monkeypatch.setattr(server_module.selectors, "DefaultSelector", Selector)
    monkeypatch.setattr(server_module.threading, "Thread", ImmediateThread)
    moments = iter((0.0, 11.0))
    monkeypatch.setattr(server_module.time, "monotonic", lambda: next(moments))
    monkeypatch.setattr(server_module, "_kill_and_remove_cgroup", lambda _path: order.append("cgroup_cleared"))
    monkeypatch.setattr(server_module, "send_frame", lambda _connection, message: order.append(message["type"]))

    AgentExecServer._stream_until_exit(
        object(),
        object(),
        Process(),
        10,
        max_output_bytes=16 * 1024 * 1024,
        child_cgroup=tmp_path / "child-cgroup",
        before_terminal=lambda: order.append("normalized"),
    )

    assert order.index("cgroup_cleared") < order.index("normalized") < order.index("exit")
    assert order.count("cgroup_cleared") == 1


def test_server_rejects_same_uid_agent_configuration(tmp_path: Path) -> None:
    config = AgentExecServerConfig(
        socket_path=tmp_path / "agent.sock",
        trial_root=tmp_path / "trials",
        controller_uid=os.getuid(),
        agent_uid=os.getuid(),
        allowed_env={"SAFE"},
    )
    with pytest.raises(RuntimeError, match="distinct"):
        AgentExecServer(config).start()


@pytest.mark.skipif(sys.platform != "linux" or os.geteuid() != 0, reason="requires Linux root to prove distinct UID/cgroup containment")
def test_linux_socket_streams_child_and_rejects_escape(tmp_path: Path) -> None:
    """Real Linux socket/process qualification; never downgraded on developer hosts."""
    # The nobody account is available in the benchmark image; choose its UID
    # rather than accepting same-UID execution in test code.
    import pwd

    agent_uid = pwd.getpwnam("nobody").pw_uid
    agent_gid = pwd.getpwnam("nobody").pw_gid
    root = tmp_path / "trials"
    root.mkdir()
    socket_path = tmp_path / "runtime" / "agent.sock"
    cgroup = Path("/sys/fs/cgroup/resbench-agent-exec") / f"test-{os.getpid()}"
    server = AgentExecServer(
        AgentExecServerConfig(
            socket_path=socket_path,
            trial_root=root,
            controller_uid=os.getuid(),
            agent_uid=agent_uid,
            agent_gid=agent_gid,
            allowed_env={"SAFE"},
            cgroup_path=cgroup,
            socket_gid=os.getgid(),
            cgroup_limits={"memory.max": "536870912", "pids.max": "64", "cpu.max": "100000 100000"},
        )
    )
    server.start()
    try:
        client = AgentExecClient(socket_path, expected_server_uid=os.getuid())
        result = client.run(
            [sys.executable, "-c", "import sys;print('out', flush=True);print('err', file=sys.stderr, flush=True)"],
            b"",
            {"SAFE": "yes"},
            10,
            cwd=".",
        )
        assert result.returncode == 0
        assert result.stdout == b"out\n"
        assert result.stderr == b"err\n"
        with pytest.raises(AgentExecClientError, match="rejected"):
            client.run([sys.executable, "-c", "pass"], b"", {"SAFE": "yes"}, 5, cwd="../")
    finally:
        server.stop()


@pytest.mark.skipif(sys.platform != "linux" or os.geteuid() != 0, reason="requires Linux root to prove process-tree cleanup")
def test_linux_disconnect_kills_process_group(tmp_path: Path) -> None:
    """Disconnect is a cancellation signal, including for the child's descendants."""
    import pwd
    import time

    account = pwd.getpwnam("nobody")
    root = tmp_path / "trials"
    root.mkdir()
    server = AgentExecServer(
        AgentExecServerConfig(
            socket_path=tmp_path / "runtime" / "agent.sock",
            trial_root=root,
            controller_uid=os.getuid(),
            agent_uid=account.pw_uid,
            agent_gid=account.pw_gid,
            allowed_env={"SAFE"},
            cgroup_path=Path("/sys/fs/cgroup/resbench-agent-exec") / f"kill-test-{os.getpid()}",
            socket_gid=os.getgid(),
            cgroup_limits={"memory.max": "536870912", "pids.max": "64", "cpu.max": "100000 100000"},
        )
    )
    server.start()
    try:
        raw = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        raw.connect(str(server.config.socket_path))
        raw.sendall(encode_frame({
            "type": "start", "request_id": "disconnect-test", "argv": [sys.executable, "-c", "import time; time.sleep(30)"],
            "env": {"SAFE": "yes"}, "stdin": "", "cwd": ".", "timeout_seconds": 20,
        }))
        raw.close()
        time.sleep(0.5)
        assert server.active_process_count == 0
    finally:
        server.stop()
