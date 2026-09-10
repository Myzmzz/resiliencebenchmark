"""Agent-runtime entry point: install UID-scoped egress rules, then run daemon.

The evaluated CLI runs as ``agent_uid`` rather than this root-owned daemon.
Rules therefore match process ownership and do not interrupt Controller or
LiteLLM traffic in the shared Pod network namespace.  This is a deployment
precondition, not evidence that a particular CNI or Linux kernel has qualified
the isolation boundary.
"""

from __future__ import annotations

import os
import ctypes
import subprocess
import sys
from collections.abc import Callable, Sequence

from .server import main as server_main


class AgentExecNetworkError(RuntimeError):
    """The agent-runtime cannot establish its mandatory UID egress boundary."""


# 18090 is an inference-only relay.  Agent UID traffic must never reach the
# LiteLLM sidecar's mixed inference/management port 4000 directly.
DEFAULT_ALLOWED_LOOPBACK_PORTS = (
    *range(18081, 18089),  # streamable HTTP MCP services
    *range(18181, 18189),  # BladeAI's SSE MCP services
    18090,  # inference-only relay
    18481,  # controlled Blade proxy
)
Runner = Callable[[Sequence[str], bytes | None], None]
HOST_CGROUP_NAMESPACE = "/run/resbench-host/cgroupns"
CLONE_NEWCGROUP = 0x02000000


def join_host_cgroup_namespace() -> None:
    """Enter only the host cgroup namespace before daemon startup."""
    if sys.platform != "linux" or os.geteuid() != 0:
        raise AgentExecNetworkError("host cgroup namespace join requires Linux root")
    fd = os.open(HOST_CGROUP_NAMESPACE, os.O_RDONLY | os.O_CLOEXEC)
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.setns(fd, CLONE_NEWCGROUP) != 0:
            raise AgentExecNetworkError("unable to join host cgroup namespace")
    finally:
        os.close(fd)


def configure_agent_egress(
    *,
    agent_uid: int,
    allowed_loopback_ports: Sequence[int] = DEFAULT_ALLOWED_LOOPBACK_PORTS,
    runner: Runner | None = None,
) -> None:
    """Fail closed unless IPv4/IPv6 owner rules are installed successfully."""
    if sys.platform != "linux":
        raise AgentExecNetworkError("agent egress policy requires Linux")
    if os.geteuid() != 0:
        raise AgentExecNetworkError("agent egress policy requires root daemon startup")
    if agent_uid <= 0:
        raise AgentExecNetworkError("agent UID must be a non-root positive integer")
    ports = tuple(sorted(set(allowed_loopback_ports)))
    if not ports or any(port < 1 or port > 65535 for port in ports):
        raise AgentExecNetworkError("allowed loopback ports are invalid")
    execute = runner or _run_iptables
    chain = "RESBENCH_AGENT_UID_EGRESS"
    try:
        # --noflush replaces ONLY our declared chain, retaining all other
        # chains. The committed table never exposes a partially empty chain
        # during container restart; a failed restore aborts daemon startup.
        rules = ["*filter", f":{chain} - [0:0]"]
        rules.extend(
            f"-A {chain} -d 127.0.0.1/32 -p tcp --dport {port} -j ACCEPT"
            for port in ports
        )
        rules.extend((f"-A {chain} -j REJECT", "COMMIT", ""))
        execute(("iptables-restore", "--noflush", "--wait", "5"), "\n".join(rules).encode("ascii"))
        owner = ("-m", "owner", "--uid-owner", str(agent_uid))
        _ensure_output_rule(execute, "iptables", (*owner, "-j", chain))
        # No Stage-2 service binds IPv6.  Reject it explicitly so the Agent
        # cannot bypass the IPv4 loopback port allowlist.
        _ensure_output_rule(execute, "ip6tables", (*owner, "-j", "REJECT"))
    except (OSError, subprocess.SubprocessError) as exc:
        raise AgentExecNetworkError("unable to install agent UID egress policy") from exc


def _ensure_output_rule(execute: Runner, binary: str, rule: Sequence[str]) -> None:
    """Insert an owner rule once; permission/lock failures are not absence."""
    try:
        execute((binary, "-C", "OUTPUT", *rule), None)
    except subprocess.CalledProcessError as exc:
        if exc.returncode != 1:
            raise
        execute((binary, "-I", "OUTPUT", "1", *rule), None)


def _run_iptables(argv: Sequence[str], payload: bytes | None = None) -> None:
    binaries = {
        "iptables-restore": "/usr/sbin/iptables-restore",
        "iptables": "/usr/sbin/iptables",
        "ip6tables": "/usr/sbin/ip6tables",
    }
    if not argv or argv[0] not in binaries:
        raise AgentExecNetworkError("unsupported privileged firewall binary")
    subprocess.run(
        [binaries[argv[0]], *argv[1:]], check=True, input=payload,
        # /run outside this shared mount belongs to a read-only image.
        env={**os.environ, "XTABLES_LOCKFILE": "/run/resbench/xtables.lock"},
        **({"stdin": subprocess.DEVNULL} if payload is None else {}),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
    )


def _ports_from_env() -> tuple[int, ...]:
    raw = os.environ.get("RESBENCH_AGENT_EXEC_ALLOWED_LOOPBACK_PORTS", "")
    if not raw:
        return DEFAULT_ALLOWED_LOOPBACK_PORTS
    try:
        return tuple(int(value) for value in raw.split(",") if value)
    except ValueError as exc:
        raise AgentExecNetworkError("allowed loopback ports are invalid") from exc


def main(argv: Sequence[str] | None = None) -> int:
    values = tuple(sys.argv[1:] if argv is None else argv)
    if "--help" in values or "-h" in values:
        return server_main()
    if os.environ.get("RESBENCH_AGENT_EXEC_EGRESS_POLICY") != "required":
        raise AgentExecNetworkError("agent UID egress policy must be required")
    try:
        agent_uid = int(os.environ["RESBENCH_AGENT_EXEC_AGENT_UID"])
    except (KeyError, ValueError) as exc:
        raise AgentExecNetworkError("RESBENCH_AGENT_EXEC_AGENT_UID is required") from exc
    join_host_cgroup_namespace()
    configure_agent_egress(agent_uid=agent_uid, allowed_loopback_ports=_ports_from_env())
    return server_main()


if __name__ == "__main__":
    raise SystemExit(main())
