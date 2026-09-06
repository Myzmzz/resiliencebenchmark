"""Restart lifecycle checks with local sockets and a modeled iptables table."""

from __future__ import annotations

import os
from pathlib import Path
import socket
import subprocess
from types import SimpleNamespace

import pytest

from harness.agent_exec import __main__ as entrypoint
from harness.agent_exec import server as daemon


def test_uid_policy_restart_replaces_only_owned_chain_without_duplicate_jumps(monkeypatch):
    monkeypatch.setattr(entrypoint.sys, "platform", "linux")
    monkeypatch.setattr(entrypoint.os, "geteuid", lambda: 0)
    jumps = set()
    restores = []
    commands = []

    def execute(argv, payload):
        commands.append(tuple(argv))
        if argv[0] == "iptables-restore":
            assert argv[1:] == ("--noflush", "--wait", "5")
            text = payload.decode()
            assert text.startswith("*filter\n:RESBENCH_AGENT_UID_EGRESS - [0:0]\n")
            assert text.endswith("-j REJECT\nCOMMIT\n")
            assert ":OUTPUT" not in text and ":INPUT" not in text
            restores.append(text)
            return
        key = (argv[0], *argv[4:]) if argv[1] == "-I" else (argv[0], *argv[3:])
        if argv[1] == "-C" and key not in jumps:
            raise subprocess.CalledProcessError(1, argv)
        if argv[1] == "-I":
            assert key not in jumps
            jumps.add(key)

    for _ in range(2):
        entrypoint.configure_agent_egress(agent_uid=10002, runner=execute)
    assert len(restores) == 2 and restores[0] == restores[1]
    assert len(jumps) == 2
    assert sum(command[1] == "-I" for command in commands) == 2


def test_network_operational_failure_is_not_treated_as_missing_rule(monkeypatch):
    monkeypatch.setattr(entrypoint.sys, "platform", "linux")
    monkeypatch.setattr(entrypoint.os, "geteuid", lambda: 0)
    commands = []

    def execute(argv, _payload):
        commands.append(tuple(argv))
        if "-C" in argv:
            raise subprocess.CalledProcessError(4, argv)

    with pytest.raises(entrypoint.AgentExecNetworkError):
        entrypoint.configure_agent_egress(agent_uid=10002, runner=execute)
    assert not any("-I" in argv for argv in commands)


def test_iptables_uses_writable_lock_mount_in_readonly_runtime_image(monkeypatch):
    calls = []
    monkeypatch.setattr(entrypoint.subprocess, "run", lambda argv, **kwargs: calls.append((argv, kwargs)))
    entrypoint._run_iptables(("iptables-restore", "--noflush"), b"*filter\nCOMMIT\n")
    entrypoint._run_iptables(("iptables", "-C", "OUTPUT"))
    assert all(kwargs["env"]["XTABLES_LOCKFILE"] == "/run/resbench/xtables.lock" for _, kwargs in calls)
    assert calls[0][0][0] == "/usr/sbin/iptables-restore"
    assert calls[1][0][0] == "/usr/sbin/iptables"
    assert calls[0][1]["input"] == b"*filter\nCOMMIT\n"
    assert "stdin" not in calls[0][1]
    assert calls[1][1]["stdin"] == subprocess.DEVNULL


def test_privileged_firewall_runner_rejects_unrecognized_binary(monkeypatch):
    monkeypatch.setattr(entrypoint.subprocess, "run", lambda *_args, **_kwargs: pytest.fail("must not execute"))
    with pytest.raises(entrypoint.AgentExecNetworkError, match="unsupported"):
        entrypoint._run_iptables(("/tmp/iptables", "-L"))


def _root_socket_metadata(monkeypatch, target):
    original = Path.lstat

    def metadata(path):
        value = original(path)
        if path != target:
            return value
        return SimpleNamespace(st_mode=value.st_mode, st_uid=0, st_dev=value.st_dev, st_ino=value.st_ino)

    monkeypatch.setattr(Path, "lstat", metadata)


def test_stale_socket_is_reclaimed_but_live_listener_is_preserved(monkeypatch, tmp_path):
    # Short path also works on macOS's smaller Unix socket path limit.
    import tempfile
    with tempfile.TemporaryDirectory(prefix="agent-restart-") as directory:
        path = Path(directory) / "agent.sock"
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(path))
        listener.listen(1)
        _root_socket_metadata(monkeypatch, path)
        try:
            with pytest.raises(RuntimeError, match="live listener"):
                daemon._reclaim_stale_root_socket(path)
            assert path.exists()
        finally:
            listener.close()
        daemon._reclaim_stale_root_socket(path)
        assert not path.exists()


def test_stale_socket_recovery_never_deletes_regular_file_or_symlink(tmp_path):
    path = tmp_path / "agent.sock"
    path.write_text("unrelated data")
    with pytest.raises(RuntimeError, match="root-owned Unix socket"):
        daemon._reclaim_stale_root_socket(path)
    assert path.read_text() == "unrelated data"
    alias = tmp_path / "alias.sock"
    alias.symlink_to(path)
    with pytest.raises(RuntimeError, match="root-owned Unix socket"):
        daemon._reclaim_stale_root_socket(alias)
    assert alias.is_symlink()


def test_singleton_lock_survives_reuse_and_rejects_concurrent_daemon(monkeypatch, tmp_path):
    original = os.fstat

    def root_file(descriptor):
        value = original(descriptor)
        return SimpleNamespace(st_mode=value.st_mode, st_uid=0, st_nlink=value.st_nlink)

    monkeypatch.setattr(daemon.os, "fstat", root_file)
    path = tmp_path / "agent.sock"
    descriptor = daemon._acquire_socket_lock(path)
    try:
        with pytest.raises(BlockingIOError):
            daemon._acquire_socket_lock(path)
    finally:
        os.close(descriptor)
    restarted = daemon._acquire_socket_lock(path)
    os.close(restarted)
    assert path.with_name("agent.sock.lock").is_file()
