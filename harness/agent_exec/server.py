"""Linux-only privileged boundary for launching evaluated Agent CLIs.

The daemon belongs in the ``agent-runtime`` container, while its caller belongs
in the control-plane container.  The design intentionally refuses development
platforms and same-UID deployments: a Unix socket alone is not a security
boundary when the evaluated process can impersonate the daemon.
"""

from __future__ import annotations

import ctypes
import argparse
import errno
import fcntl
import json
import os
import re
import queue
import selectors
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .protocol import (
    MAX_NATIVE_OUTPUT_BYTES,
    ProtocolError,
    StartRequest,
    parse_start_request,
    recv_frame,
    send_frame,
    stream_frame,
)
from .shared_trial import normalize_shared_trial_tree
from .environment import AGENT_ENV_ALLOWLIST


@dataclass(frozen=True)
class AgentExecServerConfig:
    """Immutable deployment-time policy; no request can weaken these fields."""

    socket_path: Path
    trial_root: Path
    controller_uid: int
    agent_uid: int
    allowed_env: set[str]
    sandbox_trial_root: Path | None = None
    shared_trial_gid: int | None = None
    agent_gid: int | None = None
    cgroup_path: Path | None = None
    socket_gid: int | None = None
    cgroup_limits: Mapping[str, str] | None = None
    sandbox_uid: int | None = None
    sandbox_gid: int | None = None
    sandbox_output_limit_bytes: int = 65_536
    native_output_limit_bytes: int = MAX_NATIVE_OUTPUT_BYTES
    socket_mode: int = 0o660


class AgentExecServer:
    """Accept authenticated control-plane requests and supervise process trees."""

    def __init__(self, config: AgentExecServerConfig):
        self.config = config
        self._listener: socket.socket | None = None
        self._accept_thread: threading.Thread | None = None
        self._stopping = threading.Event()
        self._active: set[subprocess.Popen[bytes]] = set()
        self._active_lock = threading.Lock()
        self._socket_lock_fd: int | None = None
        self._socket_identity: tuple[int, int] | None = None

    @property
    def active_process_count(self) -> int:
        with self._active_lock:
            return sum(process.poll() is None for process in self._active)

    def start(self) -> None:
        """Bind a protected socket, failing closed if Linux isolation is absent."""
        if self.config.agent_uid == os.geteuid():
            raise RuntimeError("agent UID must be distinct from daemon UID")
        if sys.platform != "linux":
            raise RuntimeError("agent_exec requires Linux SO_PEERCRED and cgroup v2")
        if os.geteuid() != 0:
            raise RuntimeError("agent_exec daemon must start as root before dropping to Agent UID")
        if self.config.agent_uid == 0 or self.config.controller_uid == self.config.agent_uid:
            raise RuntimeError("controller and Agent identities must be distinct non-root identities")
        if self.config.cgroup_path is None:
            raise RuntimeError("agent_exec requires a dedicated cgroup root")
        if self.config.socket_gid is None:
            raise RuntimeError("agent_exec requires the control-plane socket group")
        if not self.config.cgroup_limits:
            raise RuntimeError("agent_exec requires explicit cgroup limits")
        invalid_limits = set(self.config.cgroup_limits).difference({"memory.max", "pids.max", "cpu.max"})
        if invalid_limits:
            raise RuntimeError("agent_exec has unsupported cgroup limit keys")
        if self.config.sandbox_uid is not None and self.config.sandbox_uid in {
            0, os.geteuid(), self.config.controller_uid, self.config.agent_uid,
        }:
            raise RuntimeError("sandbox UID must be distinct from daemon, controller, and Agent UID")
        if self.config.shared_trial_gid is not None:
            if self.config.shared_trial_gid <= 0 or self.config.agent_gid != self.config.shared_trial_gid:
                raise RuntimeError("agent gid must equal the configured shared Trial group")
        if self.config.sandbox_output_limit_bytes < 1:
            raise RuntimeError("sandbox output limit must be positive")
        if not 1 <= self.config.native_output_limit_bytes <= MAX_NATIVE_OUTPUT_BYTES:
            raise RuntimeError("native output limit is invalid")
        _validate_cgroup_root(self.config.cgroup_path)
        self.config.trial_root.mkdir(parents=True, exist_ok=True)
        _ensure_no_symlink(self.config.trial_root)
        if self.config.sandbox_uid is not None:
            if self.config.sandbox_trial_root is None:
                raise RuntimeError("sandbox mode requires a separate sandbox Trial root")
            self.config.sandbox_trial_root.mkdir(parents=True, exist_ok=True)
            _ensure_no_symlink(self.config.sandbox_trial_root)
        parent = self.config.socket_path.parent
        parent.mkdir(parents=True, exist_ok=True)
        _ensure_no_symlink(parent)
        if stat.S_IMODE(parent.stat().st_mode) & 0o002:
            raise RuntimeError("agent_exec socket directory must not be world writable")
        self._socket_lock_fd = _acquire_socket_lock(self.config.socket_path)
        listener = None
        try:
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            _reclaim_stale_root_socket(self.config.socket_path)
            listener.bind(str(self.config.socket_path))
            metadata = self.config.socket_path.lstat()
            self._socket_identity = (metadata.st_dev, metadata.st_ino)
            os.chown(self.config.socket_path, 0, self.config.socket_gid)
            os.chmod(self.config.socket_path, self.config.socket_mode)
            listener.listen(32)
        except BaseException:
            if listener is not None:
                listener.close()
            self._release_socket()
            raise
        self._listener = listener
        self._accept_thread = threading.Thread(target=self._accept_loop, name="agent-exec-accept", daemon=True)
        self._accept_thread.start()

    def stop(self) -> None:
        self._stopping.set()
        if self._listener is not None:
            self._listener.close()
        with self._active_lock:
            processes = list(self._active)
        for process in processes:
            _terminate_tree(process)
        if self._accept_thread is not None:
            self._accept_thread.join(timeout=2)
        self._release_socket()

    def _release_socket(self) -> None:
        """Unlink only this instance's socket while its singleton lock is held."""
        try:
            if self._socket_identity is not None:
                try:
                    metadata = self.config.socket_path.lstat()
                except FileNotFoundError:
                    pass
                else:
                    if (metadata.st_dev, metadata.st_ino) == self._socket_identity:
                        self.config.socket_path.unlink()
        finally:
            self._socket_identity = None
            if self._socket_lock_fd is not None:
                os.close(self._socket_lock_fd)
                self._socket_lock_fd = None

    def _accept_loop(self) -> None:
        assert self._listener is not None
        while not self._stopping.is_set():
            try:
                connection, _ = self._listener.accept()
            except OSError:
                return
            threading.Thread(target=self._serve_connection, args=(connection,), daemon=True).start()

    def _serve_connection(self, connection: socket.socket) -> None:
        with connection:
            try:
                _assert_peer_uid(connection, self.config.controller_uid)
                first = recv_frame(connection)
                if first is None:
                    return
                request = parse_start_request(first)
                self._validate_request(request)
            except (ProtocolError, OSError, RuntimeError) as exc:
                _best_effort_send(connection, {"type": "rejected", "reason": str(exc)})
                return
            try:
                self._run_request(connection, request)
            except Exception as exc:  # do not leak daemon paths or environment.
                _best_effort_send(connection, {"type": "rejected", "reason": f"execution failed: {type(exc).__name__}"})

    def _validate_request(self, request: StartRequest) -> None:
        unknown = set(request.env).difference(self.config.allowed_env)
        if unknown:
            raise RuntimeError("request contains environment variables outside daemon allowlist")
        root = (
            self.config.sandbox_trial_root
            if request.mode == "sandbox"
            else self.config.trial_root
        )
        assert root is not None
        _safe_trial_cwd(root, request.cwd)
        if request.mode == "sandbox":
            if self.config.sandbox_uid is None:
                raise RuntimeError("sandbox mode is not configured")
            if not request.argv or Path(request.argv[0]).name not in {"python", "python3"}:
                raise RuntimeError("sandbox mode only permits the configured Python interpreter")

    def _run_request(self, connection: socket.socket, request: StartRequest) -> None:
        root = (
            self.config.sandbox_trial_root
            if request.mode == "sandbox"
            else self.config.trial_root
        )
        assert root is not None
        cwd = _safe_trial_cwd(root, request.cwd)
        sandbox_tmp = _sandbox_tmp_dir(cwd) if request.mode == "sandbox" else None
        child_env = dict(request.env)
        if sandbox_tmp is not None:
            child_env.update({
                "TMPDIR": str(sandbox_tmp), "TMP": str(sandbox_tmp), "TEMP": str(sandbox_tmp),
                "PYTHONDONTWRITEBYTECODE": "1",
            })
        child_cgroup = _create_child_cgroup(self.config.cgroup_path, request.request_id, self.config.cgroup_limits or {})
        config_read, config_write = os.pipe()
        child_config = {
            "argv": list(request.argv), "env": child_env,
            "sandbox": request.mode == "sandbox", "sandbox_tmp": str(sandbox_tmp) if sandbox_tmp else None,
            "cgroup": str(child_cgroup), "agent_uid": self.config.agent_uid,
            "agent_gid": self.config.agent_gid, "sandbox_uid": self.config.sandbox_uid,
            "sandbox_gid": self.config.sandbox_gid,
        }
        try:
            # No Python preexec_fn in this multi-threaded daemon. Only the
            # trusted, single-threaded initializer runs before UID isolation.
            process = subprocess.Popen(
                [sys.executable, "-I", str(Path(__file__).with_name("launcher.py")), str(config_read)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                cwd=str(cwd), env={}, close_fds=True, start_new_session=True,
                pass_fds=(config_read,),
            )
        except BaseException:
            os.close(config_write)
            _kill_and_remove_cgroup(child_cgroup)
            raise
        finally:
            os.close(config_read)
        with self._active_lock:
            self._active.add(process)
        def feed_input() -> None:
            # A CLI that never reads stdin must not stall the supervising
            # thread before timeout/cancellation handling has started.
            try:
                with os.fdopen(config_write, "wb") as config_pipe:
                    config_pipe.write(json.dumps(child_config).encode("utf-8"))
                assert process.stdin is not None
                process.stdin.write(request.stdin)
                process.stdin.close()
            except (OSError, ValueError):
                if process.stdin is not None:
                    try:
                        process.stdin.close()
                    except OSError:
                        pass
        input_thread = threading.Thread(target=feed_input, daemon=True, name="agent-exec-input")
        input_thread.start()
        try:
            self._stream_until_exit(
                connection, process, request.timeout_seconds,
                max_output_bytes=min(
                    request.output_limit_bytes,
                    self.config.sandbox_output_limit_bytes
                    if request.mode == "sandbox"
                    else self.config.native_output_limit_bytes,
                ),
                child_cgroup=child_cgroup,
                before_terminal=(
                    lambda: normalize_shared_trial_tree(cwd, self.config.shared_trial_gid)
                    if request.mode == "agent" and self.config.shared_trial_gid is not None
                    else None
                ),
            )
        finally:
            _terminate_tree(process)
            input_thread.join(timeout=2)
            with self._active_lock:
                self._active.discard(process)
            _kill_and_remove_cgroup(child_cgroup)

    def _stream_until_exit(
        self, connection: socket.socket, process: subprocess.Popen[bytes], timeout_seconds: int,
        *,
        max_output_bytes: int,
        child_cgroup: Path,
        before_terminal: Callable[[], None] | None = None,
    ) -> None:
        assert process.stdout is not None and process.stderr is not None
        messages: queue.Queue[dict[str, Any] | None] = queue.Queue()
        disconnected = threading.Event()

        def receive_control() -> None:
            try:
                while True:
                    message = recv_frame(connection)
                    if message is None:
                        disconnected.set()
                        messages.put(None)
                        return
                    messages.put(message)
            except (OSError, ProtocolError):
                disconnected.set()
                messages.put(None)

        threading.Thread(target=receive_control, daemon=True).start()
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        deadline = time.monotonic() + timeout_seconds
        cancelled = False
        timed_out = False
        output_bytes = 0
        output_truncated = False
        cgroup_cleared = False

        def clear_cgroup() -> None:
            nonlocal cgroup_cleared
            if cgroup_cleared:
                return
            _kill_and_remove_cgroup(child_cgroup)
            cgroup_cleared = True

        try:
            while selector.get_map() or process.poll() is None:
                if disconnected.is_set() and not cgroup_cleared:
                    cancelled = True
                    _terminate_tree(process)
                    clear_cgroup()
                if time.monotonic() >= deadline and not cgroup_cleared:
                    timed_out = True
                    _terminate_tree(process)
                    clear_cgroup()
                while True:
                    try:
                        control = messages.get_nowait()
                    except queue.Empty:
                        break
                    if control is None:
                        continue
                    if control.get("type") == "cancel":
                        cancelled = True
                        _terminate_tree(process)
                        clear_cgroup()
                    else:
                        raise ProtocolError("only cancel is permitted after start")
                for key, _ in selector.select(timeout=0.1):
                    pipe = key.fileobj
                    data = os.read(pipe.fileno(), 16_384)
                    if not data:
                        selector.unregister(pipe)
                    elif not disconnected.is_set():
                        allowed = max(0, max_output_bytes - output_bytes)
                        if allowed < len(data):
                            output_truncated = True
                        if allowed:
                            send_frame(connection, stream_frame(str(key.data), data[:allowed]))
                            output_bytes += allowed
                        if output_bytes >= max_output_bytes and not cgroup_cleared:
                            # Reaching the contract maximum is an incomplete
                            # native result, not a successful truncated stream.
                            output_truncated = True
                            _terminate_tree(process)
                            clear_cgroup()
                # A verified empty cgroup means no descendant can retain a
                # pipe.  Do not let a pathological inherited descriptor hold
                # the controller loop after timeout/cancel/disconnect.
                if cgroup_cleared and selector.get_map():
                    for key in tuple(selector.get_map().values()):
                        selector.unregister(key.fileobj)
                        key.fileobj.close()
            returncode = process.wait(timeout=2)
            clear_cgroup()
            if before_terminal is not None:
                before_terminal()
            if not disconnected.is_set():
                send_frame(connection, {
                    "type": "exit", "returncode": returncode, "cancelled": cancelled,
                    "timed_out": timed_out, "output_truncated": output_truncated,
                })
        finally:
            selector.close()


def _acquire_socket_lock(path: Path) -> int:
    """Keep a root-owned inode locked across bind, service, and cleanup."""
    lock_path = path.with_name(path.name + ".lock")
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    try:
        metadata = os.fstat(descriptor)
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != 0
                or metadata.st_nlink != 1 or stat.S_IMODE(metadata.st_mode) != 0o600):
            raise RuntimeError("agent_exec singleton lock is not a private root-owned file")
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _reclaim_stale_root_socket(path: Path) -> None:
    """Recover a crashed daemon's socket; never remove active or foreign data."""
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(metadata.st_mode) or metadata.st_uid != 0:
        raise RuntimeError("agent_exec existing path is not a root-owned Unix socket")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.2)
        try:
            probe.connect(str(path))
        except OSError as exc:
            if exc.errno != errno.ECONNREFUSED:
                raise RuntimeError("agent_exec existing socket cannot be safely reclaimed") from exc
        else:
            raise RuntimeError("agent_exec socket already has a live listener")
    current = path.lstat()
    if (current.st_dev, current.st_ino) != (metadata.st_dev, metadata.st_ino):
        raise RuntimeError("agent_exec socket changed during stale-socket check")
    path.unlink()


def _assert_peer_uid(connection: socket.socket, expected_uid: int) -> None:
    import struct

    raw = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    _pid, uid, _gid = struct.unpack("3i", raw)
    if uid != expected_uid:
        raise RuntimeError("Unix peer is not the configured control-plane identity")


def _safe_trial_cwd(root: Path, relative: str) -> Path:
    _ensure_no_symlink(root)
    candidate = root if relative == "." else root.joinpath(*relative.split("/"))
    current = root
    for part in (() if relative == "." else relative.split("/")):
        current = current / part
        if current.is_symlink():
            raise RuntimeError("Trial cwd may not traverse a symlink")
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root.resolve(strict=True))
    except (FileNotFoundError, ValueError) as exc:
        raise RuntimeError("Trial cwd is not an existing directory below its shared root") from exc
    if not resolved.is_dir():
        raise RuntimeError("Trial cwd is not a directory")
    return resolved


def _ensure_no_symlink(path: Path) -> None:
    if path.is_symlink():
        raise RuntimeError("configured path may not be a symlink")


def _validate_cgroup_root(root: Path) -> None:
    prefix = Path("/sys/fs/cgroup/resbench-agent-exec")
    if not root.is_absolute() or root.parent != prefix or not root.name:
        raise RuntimeError("cgroup root must be one delegated per-Pod leaf below /sys/fs/cgroup/resbench-agent-exec")
    if not (Path("/sys/fs/cgroup/cgroup.controllers").is_file()):
        raise RuntimeError("agent_exec requires cgroup v2")
    prefix.mkdir(exist_ok=True)
    root.mkdir(exist_ok=True)
    required = {"cpu", "memory", "pids"}
    for target in (prefix, root):
        available = set((target.parent / "cgroup.controllers").read_text(encoding="ascii").split())
        if not required.issubset(available):
            raise RuntimeError("delegated cgroup root lacks cpu, memory, or pids controller")
        subtree = target / "cgroup.subtree_control"
        enabled = set(subtree.read_text(encoding="ascii").split())
        missing = required.difference(enabled)
        if missing:
            subtree.write_text(" ".join(f"+{item}" for item in sorted(missing)), encoding="ascii")


def _create_child_cgroup(root: Path | None, request_id: str, limits: Mapping[str, str]) -> Path:
    assert root is not None
    safe = "".join(char if char.isalnum() or char in "_.-" else "_" for char in request_id)
    path = root / f"run-{safe}-{os.getpid()}-{time.monotonic_ns()}"
    path.mkdir()
    try:
        for name, value in limits.items():
            if not value or "\n" in value:
                raise RuntimeError("invalid cgroup limit value")
            (path / name).write_text(value, encoding="ascii")
    except Exception:
        path.rmdir()
        raise
    return path


def _add_to_cgroup(path: Path, pid: int) -> None:
    (path / "cgroup.procs").write_text(f"{pid}\n", encoding="ascii")


def _kill_and_remove_cgroup(path: Path) -> None:
    """Eliminate escaped ``setsid`` descendants before accepting next work.

    Process-group signalling alone cannot prove cleanup because a child may
    create a new session.  Every sandbox child inherits its dedicated cgroup;
    cgroup v2's ``cgroup.kill`` is the final containment cleanup primitive.
    """
    if not path.exists():
        return
    kill_file = path / "cgroup.kill"
    if not kill_file.is_file():
        raise RuntimeError("dedicated cgroup lacks cgroup.kill")
    kill_file.write_text("1\n", encoding="ascii")
    deadline = time.monotonic() + 2
    procs_file = path / "cgroup.procs"
    while time.monotonic() < deadline:
        if not procs_file.read_text(encoding="ascii").strip():
            path.rmdir()
            return
        time.sleep(0.02)
    raise RuntimeError("cgroup descendants survived cleanup")


def _initialize_child(
    config: AgentExecServerConfig,
    sandbox: bool,
    sandbox_tmp: Path | None,
    child_cgroup: Path,
):
    uid = config.sandbox_uid if sandbox else config.agent_uid
    gid = config.sandbox_gid if sandbox else config.agent_gid
    assert uid is not None
    effective_gid = gid if gid is not None else uid

    # Called only in the trusted single-threaded launcher, before Agent exec.
    _add_to_cgroup(child_cgroup, os.getpid())
    if sandbox:
        assert sandbox_tmp is not None
        _isolate_sandbox_namespaces(sandbox_tmp)
    _set_no_new_privileges_and_drop_bounding_caps()
    os.setgroups([])
    os.setgid(effective_gid)
    os.setuid(uid)
    os.umask(0o077)


def _isolate_sandbox_namespaces(sandbox_tmp: Path) -> None:
    """Cut all network paths and make the runtime image read-only for a guest.

    This runs while still root in the sidecar child.  If the pod security
    context does not grant mount/network namespace creation, failure aborts the
    launch; executing on the host namespace is never a fallback.
    """
    libc = ctypes.CDLL(None, use_errno=True)
    unshare = libc.unshare
    unshare.argtypes = [ctypes.c_int]
    unshare.restype = ctypes.c_int
    clone_newns = 0x00020000
    clone_newnet = 0x40000000
    if unshare(clone_newns | clone_newnet) != 0:
        raise OSError(ctypes.get_errno(), "sandbox requires mount and network namespaces")
    mount = libc.mount
    mount.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_ulong, ctypes.c_char_p]
    mount.restype = ctypes.c_int
    ms_rec, ms_private, ms_remount, ms_rdonly = 16384, 1 << 18, 32, 1
    if mount(None, b"/", None, ms_rec | ms_private, None) != 0:
        raise OSError(ctypes.get_errno(), "sandbox cannot privatize mount propagation")
    if mount(None, b"/", None, ms_remount | ms_rdonly, None) != 0:
        raise OSError(ctypes.get_errno(), "sandbox cannot remount runtime filesystem read-only")
    encoded_tmp = os.fsencode(sandbox_tmp)
    ms_bind = 4096
    if mount(encoded_tmp, encoded_tmp, None, ms_bind, None) != 0:
        raise OSError(ctypes.get_errno(), "sandbox cannot bind its temporary directory")
    if mount(None, encoded_tmp, None, ms_bind | ms_remount, None) != 0:
        raise OSError(ctypes.get_errno(), "sandbox cannot make its temporary directory writable")


def _sandbox_tmp_dir(cwd: Path) -> Path:
    candidate = cwd / ".sandbox-tmp"
    if candidate.is_symlink() or not candidate.is_dir():
        raise RuntimeError("sandbox requires a Controller-created .sandbox-tmp directory")
    return candidate.resolve(strict=True)


def _set_no_new_privileges_and_drop_bounding_caps() -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    prctl = libc.prctl
    prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
    prctl.restype = ctypes.c_int
    # PR_CAPBSET_DROP before setuid prevents retaining capabilities through an
    # unexpected file capability.  Failure is fatal: this is a security gate.
    try:
        maximum_capability = int(Path("/proc/sys/kernel/cap_last_cap").read_text(encoding="ascii").strip())
    except (OSError, ValueError) as exc:
        raise OSError("cannot determine Linux capability ceiling") from exc
    for capability in range(0, maximum_capability + 1):
        result = prctl(24, capability, 0, 0, 0)  # PR_CAPBSET_DROP
        if result != 0:
            raise OSError(ctypes.get_errno(), "unable to drop capability bounding set")
    if prctl(38, 1, 0, 0, 0) != 0:  # PR_SET_NO_NEW_PRIVS
        raise OSError(ctypes.get_errno(), "unable to set no_new_privs")


def _terminate_tree(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=1)
            return
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return


def _best_effort_send(connection: socket.socket, message: Mapping[str, Any]) -> None:
    try:
        send_frame(connection, message)
    except OSError:
        pass


def main() -> int:
    """Run the sidecar daemon from an explicit, deployment-owned argv.

    Secrets and environment values are intentionally not accepted here.  The
    control plane supplies the permitted per-Trial values in each authenticated
    request, and ``--allow-env`` names are a static image/manifest policy.
    """
    parser = argparse.ArgumentParser(description="resbench agent execution sidecar")
    parser.add_argument("--socket", required=True, type=Path)
    parser.add_argument("--trial-root", required=True, type=Path)
    parser.add_argument("--sandbox-trial-root", type=Path)
    parser.add_argument("--cgroup-root", required=True, type=Path)
    parser.add_argument("--controller-uid", required=True, type=int)
    parser.add_argument("--agent-uid", required=True, type=int)
    parser.add_argument("--agent-gid", type=int)
    parser.add_argument("--shared-trial-gid", type=int)
    parser.add_argument("--sandbox-uid", type=int)
    parser.add_argument("--sandbox-gid", type=int)
    parser.add_argument("--socket-gid", required=True, type=int)
    parser.add_argument("--allow-env", action="append", default=[])
    parser.add_argument("--memory-max", required=True)
    parser.add_argument("--pids-max", required=True)
    parser.add_argument("--cpu-max", required=True)
    parser.add_argument("--job-completion-file", type=Path)
    args = parser.parse_args()
    pod_uid = os.environ.get("RESBENCH_AGENT_EXEC_POD_UID", "")
    if not re.fullmatch(r"[a-f0-9-]{16,64}", pod_uid):
        parser.error("RESBENCH_AGENT_EXEC_POD_UID must be a Kubernetes Pod UID")
    server = AgentExecServer(
        AgentExecServerConfig(
            socket_path=args.socket,
            trial_root=args.trial_root,
            sandbox_trial_root=args.sandbox_trial_root,
            controller_uid=args.controller_uid,
            agent_uid=args.agent_uid,
            agent_gid=args.agent_gid,
            shared_trial_gid=args.shared_trial_gid,
            socket_gid=args.socket_gid,
            allowed_env=set(args.allow_env) or set(AGENT_ENV_ALLOWLIST),
            cgroup_path=args.cgroup_root / pod_uid,
            cgroup_limits={"memory.max": args.memory_max, "pids.max": args.pids_max, "cpu.max": args.cpu_max},
            sandbox_uid=args.sandbox_uid,
            sandbox_gid=args.sandbox_gid,
        )
    )
    server.start()
    try:
        while not server._stopping.wait(1):
            if args.job_completion_file is not None and args.job_completion_file.is_file():
                # Matrix Job main owns this 0700 controller IPC directory.  The
                # sidecar only observes completion; it never treats it as an
                # Agent result or synthesizes successful task evidence.
                break
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
