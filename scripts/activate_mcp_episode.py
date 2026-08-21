#!/usr/bin/env python3
"""Dry-run-first local activation for per-Episode MCP runtime env files."""

from __future__ import annotations

import argparse
import json
import os
import pwd
import grp
import re
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlparse

import yaml
from jsonschema import Draft202012Validator


DEFAULT_ENV_DIR = Path("/etc/resiliencebenchmark/mcp")
DEFAULT_KUBECONFIG_ROOT = Path("/etc/resiliencebenchmark/kubeconfigs")
DEFAULT_SOURCE_ROOT = "/opt/resiliencebenchmark/sources"
DEFAULT_BASELINE_LEDGER_DIR = "/var/lib/resiliencebenchmark/chaos-control/baseline"
DEFAULT_ACTIVE_LEDGER_DIR = "/var/lib/resiliencebenchmark/chaos-control/active"
DEFAULT_SCHEMA = Path("tasks/schemas/episode-public.schema.json")
UNIT_NAMES = (
    "resbench-mcp-k8s-ro.service",
    "resbench-mcp-telemetry-ro.service",
    "resbench-mcp-source-ro.service",
    "resbench-mcp-chaos-control.service",
    "resbench-mcp-k8s-ro-sse.service",
    "resbench-mcp-telemetry-ro-sse.service",
    "resbench-mcp-source-ro-sse.service",
)
APPLICATIONS = {"train-ticket", "sock-shop", "otel-demo"}
SERVICE_GROUPS = {
    "k8s_ro.env": "resbench-k8s-ro",
    "telemetry_ro.env": "resbench-telemetry-ro",
    "source_ro.env": "resbench-source-ro",
    "chaos_control.env": "resbench-chaos-control",
}
KUBECONFIG_GROUPS = {
    "k8s_kubeconfig": "resbench-k8s-ro",
    "chaos_kubeconfig": "resbench-chaos-control",
}
NAMESPACE_RE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
TOKEN_RE = re.compile(r"^\S{32,}$")
SAFE_SCOPE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._/\s,-]{0,255}$")


@dataclass(frozen=True)
class RuntimeField:
    key: str
    env_name: str
    file_arg: str
    required: bool = True
    path_value: bool = False


RUNTIME_FIELDS = (
    RuntimeField("token", "RESBENCH_MCP_TOKEN", "mcp_token_file"),
    RuntimeField("issuer_url", "RESBENCH_MCP_ISSUER_URL", "mcp_issuer_url_file"),
    RuntimeField("resource_url", "RESBENCH_MCP_RESOURCE_URL", "mcp_resource_url_file"),
    RuntimeField("scope", "RESBENCH_MCP_SCOPE", "mcp_scope_file"),
    RuntimeField("k8s_kubeconfig", "RESBENCH_K8S_RO_KUBECONFIG", "k8s_kubeconfig", required=True, path_value=True),
    RuntimeField("chaos_kubeconfig", "RESBENCH_CHAOS_KUBECONFIG", "chaos_kubeconfig", required=True, path_value=True),
    RuntimeField("prometheus_url", "RESBENCH_PROMETHEUS_URL", "prometheus_url_file"),
    RuntimeField("jaeger_url", "RESBENCH_JAEGER_URL", "jaeger_url_file"),
    RuntimeField("loki_url", "RESBENCH_LOKI_URL", "loki_url_file"),
    RuntimeField("jaeger_allowlist", "RESBENCH_JAEGER_ALLOWED_SERVICES", "jaeger_allowlist_file"),
    RuntimeField("controller_token_ref", "RESBENCH_CHAOS_CONTROLLER_TOKEN_REF", "controller_token_ref_file"),
    RuntimeField("controller_pod_uid", "RESBENCH_CHAOS_CONTROLLER_POD_UID", "controller_pod_uid_file"),
    RuntimeField("controller_pod_namespace", "RESBENCH_CHAOS_CONTROLLER_POD_NAMESPACE", "controller_pod_namespace_file"),
    RuntimeField("controller_pod_name", "RESBENCH_CHAOS_CONTROLLER_POD_NAME", "controller_pod_name_file"),
)


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: bytes
    stderr: bytes


Runner = Callable[[list[str], int], CommandResult]


class FilesystemEnvWriter:
    def write_env_files(
        self,
        files: Mapping[str, str],
        *,
        env_dir: Path,
        mode: int,
        group_by_file: Mapping[str, str],
    ) -> None:
        if not env_dir.is_dir() or env_dir.is_symlink():
            raise ValueError("MCP env directory must be a pre-provisioned regular directory")
        directory_metadata = env_dir.stat()
        directory_mode = stat.S_IMODE(directory_metadata.st_mode)
        if directory_metadata.st_uid != 0 or directory_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise ValueError("MCP env directory must be root-owned and not group/world-writable")
        for name, content in files.items():
            gid = grp.getgrnam(group_by_file[name]).gr_gid
            target = env_dir / name
            fd, tmp_name = tempfile.mkstemp(prefix=f".{name}.", dir=str(env_dir), text=True)
            tmp_path = Path(tmp_name)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.chown(tmp_path, 0, gid)
                os.chmod(tmp_path, mode)
                os.replace(tmp_path, target)
            except Exception:
                try:
                    tmp_path.unlink()
                except FileNotFoundError:
                    pass
                raise


def subprocess_runner(argv: list[str], timeout_seconds: int) -> CommandResult:
    completed = subprocess.run(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout_seconds,
        check=False,
    )
    return CommandResult(int(completed.returncode), completed.stdout, completed.stderr)


def read_yaml_or_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        if path.suffix.lower() == ".json":
            return json.load(handle)
        return yaml.safe_load(handle)


def load_episode(episode_path: Path, schema_path: Path) -> tuple[dict[str, str] | None, list[dict[str, str]]]:
    issues: list[dict[str, str]] = []
    try:
        episode = read_yaml_or_json(episode_path)
        schema = read_yaml_or_json(schema_path)
        Draft202012Validator(schema).validate(episode)
    except Exception as exc:  # noqa: BLE001 - validator raises several structured exceptions.
        return None, [{"severity": "ERROR", "field": "episode", "message": f"episode schema validation failed: {type(exc).__name__}"}]
    application = episode.get("application", {}) if isinstance(episode, dict) else {}
    name = str(application.get("name", ""))
    namespace = str(application.get("namespace", ""))
    if name not in APPLICATIONS:
        issues.append({"severity": "ERROR", "field": "application.name", "message": "unsupported application"})
    if not NAMESPACE_RE.fullmatch(namespace):
        issues.append({"severity": "ERROR", "field": "application.namespace", "message": "invalid namespace"})
    if "," in name or "," in namespace:
        issues.append({"severity": "ERROR", "field": "application", "message": "application and namespace must be single values"})
    if issues:
        return None, issues
    return {"application": name, "namespace": namespace}, []


def read_safe_file(path: Path) -> str:
    validate_safe_file_path(path)
    value = path.read_text(encoding="utf-8")
    if value.endswith("\n"):
        value = value[:-1]
    return value


def validate_safe_file_path(path: Path) -> None:
    if not path.is_absolute():
        raise ValueError("path must be absolute")
    if not path.is_file():
        raise ValueError("path must reference an existing file")
    if path.is_symlink():
        raise ValueError("path must not be a symlink")
    mode = path.stat().st_mode
    if mode & stat.S_IWOTH:
        raise ValueError("path must not be world-writable")


def resolve_runtime_values(
    env: Mapping[str, str],
    file_overrides: Mapping[str, Path | None],
    *,
    kubeconfig_root: Path = DEFAULT_KUBECONFIG_ROOT,
    require_values: bool = True,
) -> tuple[dict[str, str], list[dict[str, str]]]:
    values: dict[str, str] = {}
    issues: list[dict[str, str]] = []
    for field in RUNTIME_FIELDS:
        override = file_overrides.get(field.file_arg)
        try:
            if override is not None and field.path_value:
                validate_safe_file_path(override)
                value = str(override)
            elif override is not None:
                value = read_safe_file(override)
            else:
                value = env.get(field.env_name, "")
        except ValueError as exc:
            issues.append({"severity": "ERROR", "field": field.key, "message": str(exc)})
            continue
        if field.required and not value and require_values:
            issues.append({"severity": "ERROR", "field": field.key, "message": "required runtime value is missing"})
            continue
        values[field.key] = value
    issues.extend(
        validate_runtime_values(
            values,
            kubeconfig_root=kubeconfig_root,
        )
    )
    return values, issues


def validate_runtime_values(
    values: Mapping[str, str],
    *,
    kubeconfig_root: Path = DEFAULT_KUBECONFIG_ROOT,
) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    token = values.get("token", "")
    if token and not TOKEN_RE.fullmatch(token):
        issues.append({"severity": "ERROR", "field": "token", "message": "token must be at least 32 non-whitespace characters"})
    for field in ("issuer_url", "resource_url", "prometheus_url", "jaeger_url", "loki_url"):
        value = values.get(field, "")
        if value and not is_safe_url(value):
            issues.append({"severity": "ERROR", "field": field, "message": "URL must be http(s) without credentials, query, or fragment"})
    for field in ("k8s_kubeconfig", "chaos_kubeconfig"):
        path = Path(values.get(field, ""))
        if not values.get(field):
            continue
        if not path.is_absolute() or not path.is_file() or path.is_symlink():
            issues.append({"severity": "ERROR", "field": field, "message": "kubeconfig must be an absolute existing regular file, not a symlink"})
            continue
        try:
            path.resolve().relative_to(kubeconfig_root.resolve())
        except ValueError:
            issues.append(
                {
                    "severity": "ERROR",
                    "field": field,
                    "message": "kubeconfig must be under the dedicated service kubeconfig root",
                }
            )
    if values.get("scope") and not SAFE_SCOPE_RE.fullmatch(values.get("scope", "")):
        issues.append({"severity": "ERROR", "field": "scope", "message": "scope contains unsupported characters"})
    for field, value in values.items():
        if contains_control(value):
            issues.append({"severity": "ERROR", "field": field, "message": "value contains newline or control characters"})
    return issues


def is_safe_url(value: str) -> bool:
    parsed = urlparse(value)
    return (
        parsed.scheme in {"http", "https"}
        and bool(parsed.netloc)
        and not parsed.username
        and not parsed.password
        and not parsed.query
        and not parsed.fragment
    )


def contains_control(value: str) -> bool:
    return any(ord(char) < 32 or ord(char) == 127 for char in value)


def quote_env(value: str) -> str:
    if contains_control(value):
        raise ValueError("environment value contains control characters")
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("`", "\\`")
    return f'"{escaped}"'


def render_env_file(values: Mapping[str, str]) -> str:
    lines = [f"{key}={quote_env(values[key])}" for key in sorted(values)]
    return "\n".join(lines) + "\n"


def build_env_files(runtime: Mapping[str, str], episode: Mapping[str, str]) -> dict[str, str]:
    common = {
        "RESBENCH_MCP_TOKEN": runtime["token"],
        "RESBENCH_MCP_ISSUER_URL": runtime["issuer_url"],
        "RESBENCH_MCP_RESOURCE_URL": runtime["resource_url"],
        "RESBENCH_MCP_SCOPE": runtime["scope"],
    }
    namespace = episode["namespace"]
    application = episode["application"]
    files = {
        "k8s_ro.env": {
            **common,
            "RESBENCH_K8S_RO_KUBECONFIG": runtime["k8s_kubeconfig"],
            "RESBENCH_K8S_RO_NAMESPACE_ALLOWLIST": namespace,
        },
        "telemetry_ro.env": {
            **common,
            "RESBENCH_PROMETHEUS_URL": runtime["prometheus_url"],
            "RESBENCH_JAEGER_URL": runtime["jaeger_url"],
            "RESBENCH_LOKI_URL": runtime["loki_url"],
            "RESBENCH_TELEMETRY_ALLOWED_NAMESPACES": namespace,
            "RESBENCH_JAEGER_ALLOWED_SERVICES": runtime["jaeger_allowlist"],
            "RESBENCH_TELEMETRY_ALLOW_RAW_QUERIES": "false",
        },
        "source_ro.env": {
            **common,
            "RESBENCH_SOURCE_ROOT": DEFAULT_SOURCE_ROOT,
            "RESBENCH_SOURCE_ALLOWED_APPLICATIONS": application,
        },
        "chaos_control.env": {
            **common,
            "RESBENCH_CHAOS_EXECUTE_ENABLED": "false",
            "RESBENCH_CHAOS_KUBECONFIG": runtime["chaos_kubeconfig"],
            "RESBENCH_CHAOS_NAMESPACE_ALLOWLIST": namespace,
            "RESBENCH_CHAOS_CONTROLLER_TOKEN_REF": runtime["controller_token_ref"],
            "RESBENCH_CHAOS_CONTROLLER_POD_UID": runtime["controller_pod_uid"],
            "RESBENCH_CHAOS_CONTROLLER_POD_NAMESPACE": runtime["controller_pod_namespace"],
            "RESBENCH_CHAOS_CONTROLLER_POD_NAME": runtime["controller_pod_name"],
            "RESBENCH_CHAOS_BASELINE_LEDGER_DIR": DEFAULT_BASELINE_LEDGER_DIR,
            "RESBENCH_CHAOS_LEDGER_DIR": DEFAULT_ACTIVE_LEDGER_DIR,
        },
    }
    return {name: render_env_file(content) for name, content in files.items()}


def check_host_prerequisites() -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    if os.geteuid() != 0:
        issues.append({"severity": "ERROR", "field": "host", "message": "execute requires root"})
    for group_name in sorted(set(SERVICE_GROUPS.values())):
        try:
            pwd.getpwnam(group_name)
            grp.getgrnam(group_name)
        except KeyError:
            issues.append(
                {
                    "severity": "ERROR",
                    "field": "host",
                    "message": f"dedicated service identity is required: {group_name}",
                }
            )
    return issues


def check_kubeconfig_permissions(paths: Mapping[str, Path]) -> list[dict[str, str]]:
    """Require service-readable kubeconfigs without granting world access."""

    issues: list[dict[str, str]] = []
    for field, path in paths.items():
        group_name = KUBECONFIG_GROUPS[field]
        try:
            expected_gid = grp.getgrnam(group_name).gr_gid
        except KeyError:
            issues.append(
                {
                    "severity": "ERROR",
                    "field": field,
                    "message": "dedicated kubeconfig service group is required",
                }
            )
            continue
        try:
            metadata = path.stat()
        except OSError:
            issues.append({"severity": "ERROR", "field": field, "message": "kubeconfig metadata is unavailable"})
            continue
        mode = stat.S_IMODE(metadata.st_mode)
        if metadata.st_uid != 0 or metadata.st_gid != expected_gid:
            issues.append(
                {
                    "severity": "ERROR",
                    "field": field,
                    "message": f"kubeconfig must be owned by root:{group_name}",
                }
            )
        if not mode & stat.S_IRGRP or mode & (stat.S_IWGRP | stat.S_IRWXO):
            issues.append(
                {
                    "severity": "ERROR",
                    "field": field,
                    "message": "kubeconfig must be group-readable, not group-writable, and inaccessible to others",
                }
            )
    return issues


def redact_text(text: bytes | str, sensitive_values: Sequence[str]) -> str:
    decoded = text.decode("utf-8", errors="replace") if isinstance(text, bytes) else text
    redacted = decoded
    for value in sensitive_values:
        if value:
            redacted = redacted.replace(value, "<redacted>")
    return redacted.strip()


def restart_units(runner: Runner, timeout_seconds: int, sensitive_values: Sequence[str]) -> dict[str, Any]:
    result = runner(["systemctl", "restart", *UNIT_NAMES], timeout_seconds)
    item: dict[str, Any] = {"name": "restart_mcp_units", "status": "passed" if result.returncode == 0 else "failed", "returnCode": result.returncode}
    if result.returncode != 0:
        item["stdout"] = redact_text(result.stdout, sensitive_values)
        item["stderr"] = redact_text(result.stderr, sensitive_values)
    return item


def run_activation(
    *,
    episode_path: Path,
    schema_path: Path = DEFAULT_SCHEMA,
    env: Mapping[str, str] | None = None,
    file_overrides: Mapping[str, Path | None] | None = None,
    execute: bool = False,
    restart: bool = False,
    env_dir: Path = DEFAULT_ENV_DIR,
    kubeconfig_root: Path = DEFAULT_KUBECONFIG_ROOT,
    writer: Any | None = None,
    runner: Runner | None = None,
    host_check: Callable[[], list[dict[str, str]]] | None = None,
    kubeconfig_access_check: Callable[[Mapping[str, Path]], list[dict[str, str]]] | None = None,
    timeout_seconds: int = 30,
) -> dict[str, Any]:
    runtime_env = os.environ if env is None else env
    overrides = file_overrides or {}
    episode, episode_issues = load_episode(episode_path, schema_path)
    values, runtime_issues = resolve_runtime_values(
        runtime_env,
        overrides,
        kubeconfig_root=kubeconfig_root,
        require_values=execute,
    )
    issues = [*episode_issues, *runtime_issues]
    report: dict[str, Any] = {
        "schemaVersion": "resiliencebenchmark.mcp_episode_activation/v1",
        "mode": "execute" if execute else "dry-run",
        "status": "pending",
        "episode": episode if episode is not None else None,
        "envFiles": ["k8s_ro.env", "telemetry_ro.env", "source_ro.env", "chaos_control.env"],
        "permissions": {
            name: {"mode": "0640", "owner": "root", "group": group}
            for name, group in SERVICE_GROUPS.items()
        },
        "restartRequested": restart,
        "restartUnits": list(UNIT_NAMES) if restart else [],
        "runtimeSources": runtime_source_report(runtime_env, overrides),
        "issues": issues,
        "steps": [],
    }
    if issues:
        report["status"] = "blocked"
        return report
    assert episode is not None
    files = build_env_files(values, episode)
    report["envKeys"] = {name: sorted(parse_env_keys(content)) for name, content in files.items()}
    if not execute:
        report["status"] = "not_executed"
        return report

    prereq_issues = (host_check or check_host_prerequisites)()
    if not prereq_issues:
        permission_check = kubeconfig_access_check or check_kubeconfig_permissions
        prereq_issues.extend(
            permission_check(
                {
                    "k8s_kubeconfig": Path(values["k8s_kubeconfig"]),
                    "chaos_kubeconfig": Path(values["chaos_kubeconfig"]),
                }
            )
        )
    if prereq_issues:
        report["status"] = "blocked"
        report["issues"].extend(prereq_issues)
        return report

    try:
        active_writer = writer or FilesystemEnvWriter()
        active_writer.write_env_files(
            files,
            env_dir=env_dir,
            mode=0o640,
            group_by_file=SERVICE_GROUPS,
        )
    except Exception as exc:  # noqa: BLE001 - filesystem errors should fail closed.
        report["status"] = "failed"
        report["steps"].append({"name": "write_env_files", "status": "failed", "message": type(exc).__name__})
        return report
    report["steps"].append({"name": "write_env_files", "status": "passed"})

    if restart:
        restart_step = restart_units(runner or subprocess_runner, timeout_seconds, list(values.values()))
        report["steps"].append(restart_step)
        if restart_step["status"] != "passed":
            report["status"] = "failed"
            return report
    report["status"] = "activated"
    return report


def runtime_source_report(env: Mapping[str, str], overrides: Mapping[str, Path | None]) -> dict[str, str]:
    report: dict[str, str] = {}
    for field in RUNTIME_FIELDS:
        if overrides.get(field.file_arg) is not None:
            report[field.key] = "file"
        elif env.get(field.env_name):
            report[field.key] = "env"
        else:
            report[field.key] = "missing"
    return report


def parse_env_keys(content: str) -> list[str]:
    return [line.split("=", 1)[0] for line in content.splitlines() if line]


def build_file_overrides(args: argparse.Namespace) -> dict[str, Path | None]:
    return {field.file_arg: getattr(args, field.file_arg) for field in RUNTIME_FIELDS}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Activate per-Episode MCP runtime env files on the local host")
    parser.add_argument("--episode", type=Path, required=True, help="public Episode YAML/JSON")
    parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA, help="EpisodePublic schema")
    parser.add_argument("--env-dir", type=Path, default=DEFAULT_ENV_DIR, help="target MCP env directory")
    parser.add_argument("--execute", action="store_true", help="write env files; default is dry-run")
    parser.add_argument("--restart", action="store_true", help="restart all MCP units after writing env files")
    parser.add_argument("--timeout", type=int, default=30, help="systemctl restart timeout")
    parser.add_argument("--mcp-token-file", type=Path)
    parser.add_argument("--mcp-issuer-url-file", type=Path)
    parser.add_argument("--mcp-resource-url-file", type=Path)
    parser.add_argument("--mcp-scope-file", type=Path)
    parser.add_argument("--k8s-kubeconfig", type=Path)
    parser.add_argument("--chaos-kubeconfig", type=Path)
    parser.add_argument("--prometheus-url-file", type=Path)
    parser.add_argument("--jaeger-url-file", type=Path)
    parser.add_argument("--loki-url-file", type=Path)
    parser.add_argument("--jaeger-allowlist-file", type=Path)
    parser.add_argument("--controller-token-ref-file", type=Path)
    parser.add_argument("--controller-pod-uid-file", type=Path)
    parser.add_argument("--controller-pod-namespace-file", type=Path)
    parser.add_argument("--controller-pod-name-file", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    report = run_activation(
        episode_path=args.episode,
        schema_path=args.schema,
        env_dir=args.env_dir,
        execute=args.execute,
        restart=args.restart,
        file_overrides=build_file_overrides(args),
        timeout_seconds=args.timeout,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] in {"not_executed", "activated"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
