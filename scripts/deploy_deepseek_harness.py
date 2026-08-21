#!/usr/bin/env python3
"""Dry-run-first SSH deploy driver for DeepSeek Harness.

The driver never uses passwords. Execute mode requires a host, identity file,
and known_hosts file from runtime environment variables; output is intentionally
redacted so benchmark artifacts do not capture infrastructure details.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional


HOST_ENV = "RESBENCH_HARNESS_HOST"
IDENTITY_ENV = "RESBENCH_SSH_BOOTSTRAP_IDENTITY"
KNOWN_HOSTS_ENV = "RESBENCH_SSH_KNOWN_HOSTS"

PACKAGE_NAME = "@deepseek-ai/dsh"
PACKAGE_VERSION = "0.1.0-rc.7"
PACKAGE_SPEC = f"{PACKAGE_NAME}@{PACKAGE_VERSION}"
INSTALL_SCRIPT = Path("harness/deepseek-harness/install.sh")
INSTALL_ROOT = "/opt/resiliencebenchmark/deepseek-harness"
DSH_BINARY = f"{INSTALL_ROOT}/node_modules/.bin/dsh"
DEPENDENCY_TREE_FILE = "/var/lib/resiliencebenchmark/deepseek-harness-dependency-tree.json"
DEFAULT_TIMEOUT_SECONDS = 120
MIN_TIMEOUT_SECONDS = 5
MAX_TIMEOUT_SECONDS = 600

HOST_PATTERN = re.compile(r"^[A-Za-z0-9.-]+$")


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
        "schemaVersion": "resiliencebenchmark.deepseek_harness_deploy/v1",
        "mode": "dry-run",
        "status": "not_executed",
        "pinnedPackage": {
            "name": PACKAGE_NAME,
            "version": PACKAGE_VERSION,
            "spec": PACKAGE_SPEC,
        },
        "requirements": [
            {"name": HOST_ENV, "status": "present" if env.get(HOST_ENV) else "required_for_execute"},
            {"name": IDENTITY_ENV, "status": "present" if env.get(IDENTITY_ENV) else "required_for_execute"},
            {"name": KNOWN_HOSTS_ENV, "status": "present" if env.get(KNOWN_HOSTS_ENV) else "required_for_execute"},
            {"name": "ssh_batch_mode", "status": "enforced_for_execute"},
            {"name": "ssh_strict_host_key_checking", "status": "enforced_for_execute"},
            {"name": "password_auth", "status": "disabled"},
        ],
        "plannedSteps": [
            "ssh_preflight_true",
            "stdin_install_script_to_remote_bash",
            "verify_dsh_version",
            "verify_dependency_tree_recorded",
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

    return {HOST_ENV: host, IDENTITY_ENV: identity, KNOWN_HOSTS_ENV: known_hosts}, issues


def redact_text(text: bytes | str, sensitive_values: Mapping[str, str]) -> str:
    if isinstance(text, bytes):
        decoded = text.decode("utf-8", errors="replace")
    else:
        decoded = text
    redacted = decoded
    for value in sensitive_values.values():
        if value:
            redacted = redacted.replace(value, "<redacted>")
    redacted = redacted.replace(INSTALL_ROOT, "<install-root>")
    redacted = redacted.replace(DSH_BINARY, "<dsh-binary>")
    redacted = redacted.replace(DEPENDENCY_TREE_FILE, "<dependency-tree-file>")
    return redacted.strip()


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


def load_install_script(repo_root: Path) -> tuple[bytes | None, list[dict[str, str]]]:
    path = repo_root / INSTALL_SCRIPT
    issues: list[dict[str, str]] = []
    if not path.is_file():
        return None, [{"severity": "ERROR", "message": "fixed install script is missing"}]
    content = path.read_bytes()
    text = content.decode("utf-8", errors="replace")
    if PACKAGE_SPEC not in text:
        issues.append({"severity": "ERROR", "message": "fixed install script does not contain the pinned package spec"})
    return content, issues


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
) -> tuple[dict[str, Any], CommandResult | None]:
    try:
        result = runner(argv, stdin, timeout_seconds)
    except subprocess.TimeoutExpired:
        return {"name": name, "status": "failed", "errorType": "TimeoutExpired"}, None
    except Exception as exc:  # noqa: BLE001 - injected runners may fail differently.
        return {
            "name": name,
            "status": "failed",
            "errorType": type(exc).__name__,
            "message": redact_text(str(exc), sensitive),
        }, None
    return step_result(name, result, sensitive), result


def run_deploy(
    env: Mapping[str, str],
    execute: bool = False,
    runner: Runner | None = None,
    repo_root: Path | None = None,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    if not execute:
        return dry_run_report(env)

    repo = repo_root or Path(".")
    report: dict[str, Any] = {
        "schemaVersion": "resiliencebenchmark.deepseek_harness_deploy/v1",
        "mode": "execute",
        "status": "pending",
        "pinnedPackage": {"name": PACKAGE_NAME, "version": PACKAGE_VERSION, "spec": PACKAGE_SPEC},
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
    issues.extend(script_issues)
    if issues:
        report["status"] = "blocked"
        report["issues"] = issues
        return report
    assert script is not None

    active_runner = runner or subprocess_runner
    ssh_base = ssh_base_argv(runtime[HOST_ENV], runtime[IDENTITY_ENV], runtime[KNOWN_HOSTS_ENV])
    sensitive = runtime
    commands = [
        ("ssh_preflight_true", [*ssh_base, "true"], None),
        ("install_deepseek_harness", [*ssh_base, "/bin/bash", "-s"], script),
        ("verify_dsh_version", [*ssh_base, DSH_BINARY, "--version"], None),
        ("verify_dependency_tree_recorded", [*ssh_base, "test", "-s", DEPENDENCY_TREE_FILE], None),
    ]
    for name, argv, stdin in commands:
        result, command_result = run_checked_step(active_runner, name, argv, stdin, timeout_seconds, sensitive)
        if name == "verify_dsh_version" and result["status"] == "passed":
            version_output = b""
            if command_result is not None:
                version_output = command_result.stdout + command_result.stderr
            if PACKAGE_VERSION.encode("utf-8") not in version_output:
                result = {
                    "name": name,
                    "status": "failed",
                    "returnCode": 1,
                    "message": "dsh --version did not report the pinned version",
                }
        report["steps"].append(result)
        if result["status"] != "passed":
            report["status"] = "failed"
            return report

    report["status"] = "installed"
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Deploy DeepSeek Harness through a strict SSH bootstrap path")
    parser.add_argument("--execute", action="store_true", help="perform the remote install; default is dry-run")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS, help="per SSH command timeout in seconds")
    return parser


def main(argv: list[str] | None = None, runner: Runner | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    report = run_deploy(os.environ, execute=args.execute, runner=runner, timeout_seconds=args.timeout)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report["status"] in {"blocked", "failed"}:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
