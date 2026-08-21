#!/usr/bin/env python3
"""Dry-run-first recovery qualification for the BenchmarkFactory host and worker."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Callable, Mapping


HOST_ENV = "RESBENCH_MCP_HOST"
IDENTITY_ENV = "RESBENCH_SSH_BOOTSTRAP_IDENTITY"
KNOWN_HOSTS_ENV = "RESBENCH_SSH_KNOWN_HOSTS"
EXPECTED_HEAD_ENV = "RESBENCH_MCP_REPO_HEAD"
HOST_RE = re.compile(r"^[A-Za-z0-9.-]+$")
NODE_RE = re.compile(r"^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$")
HEAD_RE = re.compile(r"^[0-9a-f]{40}$")
MAX_LEASE_AGE_SECONDS = 120
MIN_AVAILABLE_MEMORY_KIB = 4 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 30


class QualificationError(RuntimeError):
    """Expected fail-closed recovery qualification error."""


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: bytes
    stderr: bytes


Runner = Callable[[list[str], bytes | None, int], CommandResult]


def subprocess_runner(argv: list[str], stdin: bytes | None, timeout_seconds: int) -> CommandResult:
    completed = subprocess.run(
        argv,
        input=stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout_seconds,
        check=False,
    )
    return CommandResult(completed.returncode, completed.stdout, completed.stderr)


def dry_run_report(env: Mapping[str, str], kubeconfig: Path | None, node: str | None) -> dict[str, Any]:
    return {
        "schemaVersion": "resiliencebenchmark.remote_preparation_qualification/v1",
        "mode": "dry-run",
        "status": "not_executed",
        "requirements": {
            HOST_ENV: bool(env.get(HOST_ENV)),
            IDENTITY_ENV: bool(env.get(IDENTITY_ENV)),
            KNOWN_HOSTS_ENV: bool(env.get(KNOWN_HOSTS_ENV)),
            EXPECTED_HEAD_ENV: bool(env.get(EXPECTED_HEAD_ENV)),
            "kubeconfig": bool(kubeconfig),
            "node": bool(node),
        },
        "plannedChecks": [
            "node-ready-and-current-lease",
            "strict-ssh-key-authentication",
            "pinned-remote-repository-head",
            "source-manifest-eleven-locks",
            "deepseek-root-and-resbench-version",
            "seven-mcp-units-and-listeners",
            "chaos-execution-disabled",
            "minimum-host-memory-available",
        ],
    }


def validate_inputs(
    env: Mapping[str, str], kubeconfig: Path, node: str
) -> tuple[dict[str, str], Path]:
    host = env.get(HOST_ENV, "")
    identity = env.get(IDENTITY_ENV, "")
    known_hosts = env.get(KNOWN_HOSTS_ENV, "")
    expected_head = env.get(EXPECTED_HEAD_ENV, "")
    if not HOST_RE.fullmatch(host) or "@" in host or ":" in host:
        raise QualificationError("SSH host is missing or unsafe")
    for value in (identity, known_hosts):
        path = Path(value)
        if not path.is_absolute() or not path.is_file():
            raise QualificationError("SSH identity and known_hosts must be explicit existing absolute files")
    if not HEAD_RE.fullmatch(expected_head):
        raise QualificationError("expected repository head must be a full lowercase SHA-1")
    resolved_kubeconfig = kubeconfig.expanduser().resolve()
    if not resolved_kubeconfig.is_file():
        raise QualificationError("kubeconfig must be an explicit existing file")
    if not NODE_RE.fullmatch(node):
        raise QualificationError("node name is unsafe")
    return {
        HOST_ENV: host,
        IDENTITY_ENV: identity,
        KNOWN_HOSTS_ENV: known_hosts,
        EXPECTED_HEAD_ENV: expected_head,
    }, resolved_kubeconfig


def ssh_base(runtime: Mapping[str, str]) -> list[str]:
    return [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"UserKnownHostsFile={runtime[KNOWN_HOSTS_ENV]}",
        "-o",
        "ConnectTimeout=8",
        "-i",
        runtime[IDENTITY_ENV],
        f"root@{runtime[HOST_ENV]}",
    ]


REMOTE_CHECK_SCRIPT = r'''set -eu
repo_head="$(tr -d '\n' </opt/resiliencebenchmark/repo/.resbench-head)"
root_dsh="$(/opt/resiliencebenchmark/deepseek-harness/bin/dsh --version)"
resbench_dsh="$(runuser -u resbench -- /opt/resiliencebenchmark/deepseek-harness/bin/dsh --version)"
source_summary="$(/opt/resiliencebenchmark/repo/.venv/bin/python - <<'PY'
import json
from pathlib import Path
p = json.loads(Path('/var/lib/resiliencebenchmark/source/source-materialization.json').read_text())
spec = p.get('spec', {})
sources = spec.get('sources', [])
valid = (
    spec.get('mode') in {'materialize', 'verify-existing'}
    and len(sources) == 11
    and all(item.get('id') and item.get('commit') and item.get('archiveSha256') for item in sources)
)
print(f"{'ok' if valid else 'invalid'}:{len(sources)}")
PY
)"
active_units=0
for unit in \
  resbench-mcp-k8s-ro.service \
  resbench-mcp-telemetry-ro.service \
  resbench-mcp-source-ro.service \
  resbench-mcp-chaos-control.service \
  resbench-mcp-k8s-ro-sse.service \
  resbench-mcp-telemetry-ro-sse.service \
  resbench-mcp-source-ro-sse.service; do
  [ "$(systemctl is-active "$unit" 2>/dev/null || true)" = active ] && active_units=$((active_units + 1))
done
listeners="$(ss -ltnH | awk '$4 ~ /127.0.0.1:18(08[1-4]|18[1-3])$/ {n++} END{print n+0}')"
chaos_execute="$(awk -F= '$1=="RESBENCH_CHAOS_EXECUTE_ENABLED" {gsub(/"/, "", $2); print $2}' /etc/resiliencebenchmark/mcp/chaos_control.env)"
mem_available="$(awk '/^MemAvailable:/ {print $2; exit}' /proc/meminfo)"
printf 'repo_head=%s\n' "$repo_head"
printf 'root_dsh=%s\n' "$root_dsh"
printf 'resbench_dsh=%s\n' "$resbench_dsh"
printf 'source_summary=%s\n' "$source_summary"
printf 'active_units=%s\n' "$active_units"
printf 'listeners=%s\n' "$listeners"
printf 'chaos_execute=%s\n' "$chaos_execute"
printf 'mem_available_kib=%s\n' "$mem_available"
'''


def parse_lines(payload: bytes) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in payload.decode("utf-8", errors="replace").splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        if re.fullmatch(r"[a-z_]+", key):
            values[key] = value.strip()
    return values


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def run_qualification(
    env: Mapping[str, str],
    *,
    execute: bool,
    kubeconfig: Path | None,
    node: str | None,
    runner: Runner = subprocess_runner,
    now: datetime | None = None,
) -> dict[str, Any]:
    if not execute:
        return dry_run_report(env, kubeconfig, node)
    if kubeconfig is None or node is None:
        raise QualificationError("execute requires kubeconfig and node")
    runtime, resolved_kubeconfig = validate_inputs(env, kubeconfig, node)
    checks: list[dict[str, Any]] = []

    node_result = runner(
        ["kubectl", "--kubeconfig", str(resolved_kubeconfig), "get", "node", node, "-o", "json"],
        None,
        DEFAULT_TIMEOUT_SECONDS,
    )
    lease_result = runner(
        [
            "kubectl",
            "--kubeconfig",
            str(resolved_kubeconfig),
            "-n",
            "kube-node-lease",
            "get",
            "lease",
            node,
            "-o",
            "json",
        ],
        None,
        DEFAULT_TIMEOUT_SECONDS,
    )
    if node_result.returncode != 0 or lease_result.returncode != 0:
        raise QualificationError("Kubernetes node or Lease read failed")
    node_payload = json.loads(node_result.stdout)
    lease_payload = json.loads(lease_result.stdout)
    ready = next(
        (item.get("status") for item in node_payload.get("status", {}).get("conditions", []) if item.get("type") == "Ready"),
        None,
    )
    renew_time = str(lease_payload.get("spec", {}).get("renewTime") or "")
    current = now or datetime.now(timezone.utc)
    lease_age = (current - parse_time(renew_time)).total_seconds() if renew_time else float("inf")
    checks.append({"name": "node_ready", "passed": ready == "True"})
    checks.append({"name": "node_lease_fresh", "passed": 0 <= lease_age <= MAX_LEASE_AGE_SECONDS})

    ssh_result = runner(
        [*ssh_base(runtime), "/bin/bash", "-s"],
        REMOTE_CHECK_SCRIPT.encode("utf-8"),
        DEFAULT_TIMEOUT_SECONDS,
    )
    if ssh_result.returncode != 0:
        checks.append({"name": "ssh_and_remote_checks", "passed": False})
    else:
        remote = parse_lines(ssh_result.stdout)
        checks.extend(
            [
                {"name": "ssh_and_remote_checks", "passed": True},
                {"name": "repository_head", "passed": remote.get("repo_head") == runtime[EXPECTED_HEAD_ENV]},
                {"name": "deepseek_root_version", "passed": remote.get("root_dsh") == "0.1.0-rc.7"},
                {"name": "deepseek_resbench_version", "passed": remote.get("resbench_dsh") == "0.1.0-rc.7"},
                {"name": "source_manifest", "passed": remote.get("source_summary") == "ok:11"},
                {"name": "mcp_units", "passed": remote.get("active_units") == "7"},
                {"name": "mcp_listeners", "passed": remote.get("listeners") == "7"},
                {"name": "chaos_execution_disabled", "passed": remote.get("chaos_execute") == "false"},
                {
                    "name": "host_memory_available",
                    "passed": int(remote.get("mem_available_kib", "0") or 0) >= MIN_AVAILABLE_MEMORY_KIB,
                },
            ]
        )
    passed = all(item["passed"] for item in checks)
    return {
        "schemaVersion": "resiliencebenchmark.remote_preparation_qualification/v1",
        "mode": "execute",
        "status": "qualified" if passed else "failed",
        "checks": checks,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--kubeconfig", type=Path)
    parser.add_argument("--node")
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = run_qualification(
            os.environ,
            execute=args.execute,
            kubeconfig=args.kubeconfig,
            node=args.node,
        )
    except Exception as exc:  # noqa: BLE001 - CLI output must stay redacted.
        report = {
            "schemaVersion": "resiliencebenchmark.remote_preparation_qualification/v1",
            "mode": "execute" if args.execute else "dry-run",
            "status": "failed",
            "errorType": type(exc).__name__,
            "message": "remote preparation qualification failed",
        }
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        output = args.output.expanduser().resolve()
        if output == Path(output.anchor) or output == Path.home().resolve():
            print("unsafe output path", file=sys.stderr)
            return 2
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
    else:
        sys.stdout.write(rendered)
    return 0 if report["status"] in {"not_executed", "qualified"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
