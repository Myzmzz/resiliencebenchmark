#!/usr/bin/env python3
"""Dry-run-first SSH deploy driver for MCP host systemd units.

Execute mode never uses passwords and never reports host, local key paths,
known_hosts paths, remote repository paths, or token-like output. The remote
install script is fixed in this repository and is streamed over stdin.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import shlex
import subprocess
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional


HOST_ENV = "RESBENCH_MCP_HOST"
IDENTITY_ENV = "RESBENCH_SSH_BOOTSTRAP_IDENTITY"
KNOWN_HOSTS_ENV = "RESBENCH_SSH_KNOWN_HOSTS"
EXPECTED_HEAD_ENV = "RESBENCH_MCP_REPO_HEAD"

REMOTE_REPO = "/opt/resiliencebenchmark/repo"
REMOTE_BASE = "/opt/resiliencebenchmark"
REMOTE_RELEASES = f"{REMOTE_BASE}/releases"
REMOTE_ENV_DIR = "/etc/resiliencebenchmark/mcp"
REMOTE_UNIT_DIR = "/etc/systemd/system"
REMOTE_LEDGER_DIR = "/var/lib/resiliencebenchmark/chaos-control"
INSTALL_SCRIPT = Path("environment/mcp/host/install.sh")
SERVICE_NAMES = (
    "k8s_ro",
    "telemetry_ro",
    "source_ro",
    "chaos_control",
    "k8s_ro_sse",
    "telemetry_ro_sse",
    "source_ro_sse",
)
UNIT_NAMES = (
    "resbench-mcp-k8s-ro.service",
    "resbench-mcp-telemetry-ro.service",
    "resbench-mcp-source-ro.service",
    "resbench-mcp-chaos-control.service",
    "resbench-mcp-k8s-ro-sse.service",
    "resbench-mcp-telemetry-ro-sse.service",
    "resbench-mcp-source-ro-sse.service",
)
DEFAULT_TIMEOUT_SECONDS = 120
MIN_TIMEOUT_SECONDS = 5
MAX_TIMEOUT_SECONDS = 600

HOST_PATTERN = re.compile(r"^[A-Za-z0-9.-]+$")
HEAD_PATTERN = re.compile(r"^[0-9a-fA-F]{7,64}$")
TOKENISH_PATTERN = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{8,}")


@dataclass
class CommandResult:
    returncode: int
    stdout: bytes
    stderr: bytes


Runner = Callable[[list[str], Optional[bytes], int], CommandResult]


def subprocess_runner(argv: list[str], stdin: Optional[bytes], timeout_seconds: int) -> CommandResult:
    completed = subprocess.run(
        argv,
        input=stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout_seconds,
        check=False,
    )
    return CommandResult(
        returncode=int(completed.returncode),
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def dry_run_report(env: Mapping[str, str]) -> dict[str, Any]:
    return {
        "schemaVersion": "resiliencebenchmark.mcp_host_deploy/v1",
        "mode": "dry-run",
        "status": "not_executed",
        "services": list(SERVICE_NAMES),
        "requirements": [
            {"name": HOST_ENV, "status": "present" if env.get(HOST_ENV) else "required_for_execute"},
            {"name": IDENTITY_ENV, "status": "present" if env.get(IDENTITY_ENV) else "required_for_execute"},
            {"name": KNOWN_HOSTS_ENV, "status": "present" if env.get(KNOWN_HOSTS_ENV) else "required_for_execute"},
            {"name": EXPECTED_HEAD_ENV, "status": "present" if env.get(EXPECTED_HEAD_ENV) else "auto_from_local_git"},
            {"name": "ssh_batch_mode", "status": "enforced_for_execute"},
            {"name": "ssh_strict_host_key_checking", "status": "enforced_for_execute"},
            {"name": "password_auth", "status": "disabled"},
        ],
        "plannedSteps": [
            "ssh_preflight_tools",
            "materialize_pinned_release",
            "install_mcp_host_units_and_sources_from_stdin",
            "verify_mcp_unit_files",
        ],
    }


def validate_host(host: str) -> str | None:
    if not host:
        return "host is required"
    if host.strip() != host or any(ch.isspace() for ch in host):
        return "host must not contain whitespace"
    if "@" in host:
        return "host must not contain userinfo"
    if ":" in host:
        return "host must not contain a port"
    if host.startswith("-"):
        return "host must not start with '-'"
    if not HOST_PATTERN.fullmatch(host):
        return "host contains disallowed characters"
    return None


def validate_runtime_env(env: Mapping[str, str]) -> tuple[dict[str, str], list[dict[str, str]]]:
    issues: list[dict[str, str]] = []
    host = env.get(HOST_ENV, "")
    identity = env.get(IDENTITY_ENV, "")
    known_hosts = env.get(KNOWN_HOSTS_ENV, "")
    expected_head = env.get(EXPECTED_HEAD_ENV, "").strip()

    host_error = validate_host(host)
    if host_error:
        issues.append({"severity": "ERROR", "field": HOST_ENV, "message": host_error})
    for name, value in ((IDENTITY_ENV, identity), (KNOWN_HOSTS_ENV, known_hosts)):
        if not value:
            issues.append({"severity": "ERROR", "field": name, "message": "path is required"})
            continue
        path = Path(value)
        if not path.is_absolute():
            issues.append({"severity": "ERROR", "field": name, "message": "path must be absolute"})
        elif not path.is_file():
            issues.append({"severity": "ERROR", "field": name, "message": "path must reference an existing file"})
    if expected_head and not HEAD_PATTERN.fullmatch(expected_head):
        issues.append({"severity": "ERROR", "field": EXPECTED_HEAD_ENV, "message": "git head must be a hex revision"})

    return {
        HOST_ENV: host,
        IDENTITY_ENV: identity,
        KNOWN_HOSTS_ENV: known_hosts,
        EXPECTED_HEAD_ENV: expected_head,
    }, issues


def load_install_script(repo_root: Path) -> tuple[bytes | None, list[dict[str, str]]]:
    path = repo_root / INSTALL_SCRIPT
    if not path.is_file():
        return None, [{"severity": "ERROR", "message": "fixed MCP host install script is missing"}]
    content = path.read_bytes()
    text = content.decode("utf-8", errors="replace")
    required = ("uv --directory", "systemctl daemon-reload", "runtime env not created", "materialize_sources.py")
    missing = [item for item in required if item not in text]
    if missing:
        return None, [{"severity": "ERROR", "message": "fixed MCP host install script is missing required install guards"}]
    return content, []


def resolve_local_head(repo_root: Path, requested: str) -> tuple[str | None, list[dict[str, str]]]:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
            check=False,
        )
    except Exception as exc:  # noqa: BLE001 - local git failures should block execute.
        return None, [{"severity": "ERROR", "field": EXPECTED_HEAD_ENV, "message": type(exc).__name__}]
    if completed.returncode != 0:
        return None, [{"severity": "ERROR", "field": EXPECTED_HEAD_ENV, "message": "unable to resolve local git head"}]
    head = completed.stdout.decode("utf-8", errors="replace").strip()
    if not HEAD_PATTERN.fullmatch(head):
        return None, [{"severity": "ERROR", "field": EXPECTED_HEAD_ENV, "message": "local git head is invalid"}]
    if requested and requested.lower() != head.lower() and not head.lower().startswith(requested.lower()):
        return None, [{"severity": "ERROR", "field": EXPECTED_HEAD_ENV, "message": "requested head does not match local HEAD"}]
    return head, []


def build_release_archive(repo_root: Path, head: str) -> tuple[bytes | None, list[dict[str, str]]]:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo_root), "archive", "--format=tar", head],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
        )
    except Exception as exc:  # noqa: BLE001 - local git failures should block execute.
        return None, [{"severity": "ERROR", "message": f"git archive failed: {type(exc).__name__}"}]
    if completed.returncode != 0:
        return None, [{"severity": "ERROR", "message": "git archive failed for local HEAD"}]

    source = io.BytesIO(completed.stdout)
    output = io.BytesIO()
    with tarfile.open(fileobj=source, mode="r:*") as reader, tarfile.open(fileobj=output, mode="w") as writer:
        for member in reader.getmembers():
            fileobj = reader.extractfile(member) if member.isfile() else None
            writer.addfile(member, fileobj)
        data = f"{head}\n".encode("utf-8")
        info = tarfile.TarInfo(".resbench-head")
        info.size = len(data)
        info.mode = 0o644
        info.mtime = 0
        writer.addfile(info, io.BytesIO(data))
    return output.getvalue(), []


def ssh_base_argv(host: str, identity: str, known_hosts: str) -> list[str]:
    return [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"UserKnownHostsFile={known_hosts}",
        "-i",
        identity,
        f"root@{host}",
    ]


def redact_text(text: bytes | str, sensitive_values: Mapping[str, str]) -> str:
    if isinstance(text, bytes):
        decoded = text.decode("utf-8", errors="replace")
    else:
        decoded = text
    redacted = decoded
    for value in sensitive_values.values():
        if value:
            redacted = redacted.replace(value, "<redacted>")
    for value in (REMOTE_REPO, REMOTE_RELEASES, REMOTE_BASE, REMOTE_ENV_DIR, REMOTE_UNIT_DIR, REMOTE_LEDGER_DIR):
        redacted = redacted.replace(value, "<redacted-path>")
    redacted = TOKENISH_PATTERN.sub(r"\1<redacted-token>", redacted)
    return redacted.strip()


def step_result(name: str, result: CommandResult, sensitive: Mapping[str, str]) -> dict[str, Any]:
    status = "passed" if result.returncode == 0 else "failed"
    item: dict[str, Any] = {"name": name, "status": status, "returnCode": result.returncode}
    if result.returncode != 0:
        item["stderr"] = redact_text(result.stderr, sensitive)
        item["stdout"] = redact_text(result.stdout, sensitive)
    return item


def run_checked_step(
    runner: Runner,
    name: str,
    argv: list[str],
    stdin: Optional[bytes],
    timeout_seconds: int,
    sensitive: Mapping[str, str],
) -> dict[str, Any]:
    try:
        result = runner(argv, stdin, timeout_seconds)
    except subprocess.TimeoutExpired:
        return {"name": name, "status": "failed", "errorType": "TimeoutExpired"}
    except Exception as exc:  # noqa: BLE001 - injected runners may fail differently.
        return {
            "name": name,
            "status": "failed",
            "errorType": type(exc).__name__,
            "message": redact_text(str(exc), sensitive),
        }
    return step_result(name, result, sensitive)


def preflight_command() -> str:
    return (
        "set -eu; "
        "test \"$(id -u)\" = 0; "
        "command -v python3 >/dev/null; "
        "command -v uv >/dev/null; "
        "command -v systemctl >/dev/null; "
        "command -v tar >/dev/null; "
        "command -v readlink >/dev/null; "
        "command -v ln >/dev/null; "
        "command -v mv >/dev/null; "
        "command -v runuser >/dev/null; "
        "command -v git >/dev/null; "
        "command -v getent >/dev/null; "
        "command -v groupadd >/dev/null; "
        "command -v useradd >/dev/null"
    )


def materialize_release_command() -> str:
    return r"""set -eu
head="$1"
base="/opt/resiliencebenchmark"
releases="$base/releases"
release="$releases/$head"
repo="$base/repo"
tmp="$releases/.tmp-$head-$$"
link_tmp="$base/.repo-link-$head-$$"
cleanup() {
  if [ -n "${tmp:-}" ] && [ -d "$tmp" ]; then rm -rf -- "$tmp"; fi
  if [ -n "${link_tmp:-}" ] && [ -L "$link_tmp" ]; then rm -f -- "$link_tmp"; fi
}
trap cleanup EXIT
mkdir -p "$releases"
if [ -e "$repo" ] && [ ! -L "$repo" ]; then
  exit 42
fi
if [ -L "$repo" ]; then
  target="$(readlink "$repo")"
  case "$target" in
    "$releases"/*) ;;
    *) exit 43 ;;
  esac
fi
rm -rf -- "$tmp"
mkdir "$tmp"
tar -xf - -C "$tmp"
test -f "$tmp/.resbench-head"
test "$(tr -d '\n' < "$tmp/.resbench-head")" = "$head"
if [ -e "$release" ]; then
  test -d "$release"
  test -f "$release/.resbench-head"
  test "$(tr -d '\n' < "$release/.resbench-head")" = "$head"
  rm -rf -- "$tmp"
else
  mv "$tmp" "$release"
fi
ln -s "$release" "$link_tmp"
mv -Tf "$link_tmp" "$repo"
trap - EXIT
cleanup"""


def materialize_release_ssh_argv(ssh_base: list[str], head: str) -> list[str]:
    """Keep the release script in remote argv while reserving stdin for the tar archive."""

    if not HEAD_PATTERN.fullmatch(head):
        raise ValueError("git head must be a hex revision")
    remote_command = (
        f"/bin/sh -c {shlex.quote(materialize_release_command())} "
        f"resbench-materialize {shlex.quote(head)}"
    )
    return [*ssh_base, remote_command]


def verify_units_command() -> str:
    units = " ".join(UNIT_NAMES)
    return f"set -eu; for unit in {units}; do systemctl cat \"$unit\" >/dev/null; done"


def run_deploy(
    env: Mapping[str, str],
    execute: bool = False,
    runner: Runner | None = None,
    repo_root: Path | None = None,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    materialize_sources: bool = True,
) -> dict[str, Any]:
    if not execute:
        return dry_run_report(env)

    repo = repo_root or Path(".")
    report: dict[str, Any] = {
        "schemaVersion": "resiliencebenchmark.mcp_host_deploy/v1",
        "mode": "execute",
        "status": "pending",
        "services": list(SERVICE_NAMES),
        "sourceMaterialization": {
            "requested": materialize_sources,
            "status": "will_materialize" if materialize_sources else "not_ready_skipped",
        },
        "steps": [],
        "issues": [],
    }
    if not MIN_TIMEOUT_SECONDS <= timeout_seconds <= MAX_TIMEOUT_SECONDS:
        report["status"] = "blocked"
        report["issues"] = [
            {
                "severity": "ERROR",
                "field": "timeout",
                "message": f"timeout must be between {MIN_TIMEOUT_SECONDS} and {MAX_TIMEOUT_SECONDS} seconds",
            }
        ]
        return report

    runtime, issues = validate_runtime_env(env)
    script, script_issues = load_install_script(repo)
    head, head_issues = resolve_local_head(repo, runtime[EXPECTED_HEAD_ENV])
    archive: bytes | None = None
    archive_issues: list[dict[str, str]] = []
    if head is not None:
        archive, archive_issues = build_release_archive(repo, head)
    issues.extend(script_issues)
    issues.extend(head_issues)
    issues.extend(archive_issues)
    if issues:
        report["status"] = "blocked"
        report["issues"] = issues
        return report
    assert script is not None
    assert head is not None
    assert archive is not None

    active_runner = runner or subprocess_runner
    ssh_base = ssh_base_argv(runtime[HOST_ENV], runtime[IDENTITY_ENV], runtime[KNOWN_HOSTS_ENV])
    sensitive = {**runtime, "remote_repo": REMOTE_REPO, "remote_releases": REMOTE_RELEASES, "remote_base": REMOTE_BASE}
    install_argv = [*ssh_base, "/bin/bash", "-s", "--", "--repo", REMOTE_REPO, "--head", head]
    if materialize_sources:
        install_argv.append("--materialize-sources")
    commands = [
        ("ssh_preflight_tools", [*ssh_base, "/bin/sh", "-c", preflight_command()], None),
        ("materialize_pinned_release", materialize_release_ssh_argv(ssh_base, head), archive),
        ("install_mcp_host_units_and_sources_from_stdin", install_argv, script),
        ("verify_mcp_unit_files", [*ssh_base, "/bin/sh", "-c", verify_units_command()], None),
    ]
    for name, argv, stdin in commands:
        result = run_checked_step(active_runner, name, argv, stdin, timeout_seconds, sensitive)
        report["steps"].append(result)
        if result["status"] != "passed":
            report["status"] = "failed"
            return report

    if materialize_sources:
        report["sourceMaterialization"]["status"] = "ready_or_verify_existing"
        report["status"] = "installed"
    else:
        report["status"] = "installed_source_not_ready"
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Deploy MCP host systemd units through a strict SSH bootstrap path")
    parser.add_argument("--execute", action="store_true", help="perform the remote install; default is dry-run")
    parser.add_argument(
        "--skip-source-materialization",
        action="store_true",
        help="install units without preparing /opt/resiliencebenchmark/sources; report Source MCP as not ready",
    )
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS, help="per SSH command timeout in seconds")
    return parser


def main(argv: list[str] | None = None, runner: Runner | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    report = run_deploy(
        os.environ,
        execute=args.execute,
        runner=runner,
        timeout_seconds=args.timeout,
        materialize_sources=not args.skip_source_materialization,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report["status"] in {"blocked", "failed"}:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
