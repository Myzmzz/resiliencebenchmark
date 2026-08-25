#!/usr/bin/env python3
"""Deploy the semantic scan host service to the test-cluster worker.

Dry-run is the default. ``--execute`` is required before any SSH copy, systemd
change, image pull verification, or Kubernetes apply happens. Images are built
on the local Mac and pushed to the registry; this command never builds images
on the remote Ubuntu worker.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import tarfile
import tempfile
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REMOTE_ROOT = Path("/data/mj/resbench-system")
DEFAULT_RUN_ROOT = Path("/data/mj/resbench-runs")
DEFAULT_SERVICE_NAME = "resbench-semantic-scan.service"
DEFAULT_IMAGE = (
    "1.94.151.57:85/observe/resbench-semantic-scan:otel-2.2.0-semantic-v8@"
    "sha256:34927e2d1f92efa4623fff565265fd64d30e0aaa6d90d7616a8e634f13b4ecd3"
)
REMOTE_HOST_ENV = "RESBENCH_SEMANTIC_HOST"
REMOTE_USER_ENV = "RESBENCH_SEMANTIC_USER"
REMOTE_PASSWORD_ENV = "RESBENCH_SEMANTIC_PASSWORD"
REMOTE_IDENTITY_ENV = "RESBENCH_SEMANTIC_IDENTITY"
REMOTE_KNOWN_HOSTS_ENV = "RESBENCH_SEMANTIC_KNOWN_HOSTS"


class DeployError(RuntimeError):
    """Expected deployment failure."""


SAFE_SERVICE = re.compile(r"^[a-z0-9][a-z0-9_.@-]{1,80}\.service$")
SAFE_NODE = re.compile(r"^[A-Za-z0-9](?:[-A-Za-z0-9.]{0,251}[A-Za-z0-9])?$")
SAFE_IMAGE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:@-]{2,500}$")


def _safe_remote_path(path: Path) -> Path:
    resolved = Path(str(path))
    if not resolved.is_absolute() or resolved == Path("/"):
        raise DeployError(f"remote path must be a non-root absolute path: {path}")
    if not str(resolved).startswith("/data/mj/"):
        raise DeployError(f"remote path must stay under /data/mj: {path}")
    return resolved


def _validate_args(args: argparse.Namespace) -> None:
    for value in (args.remote_root, args.run_root):
        _safe_remote_path(value)
    if not SAFE_SERVICE.fullmatch(args.service_name):
        raise DeployError("invalid systemd service name")
    if not SAFE_IMAGE.fullmatch(args.image):
        raise DeployError("invalid image reference")
    if args.node_name and not SAFE_NODE.fullmatch(args.node_name):
        raise DeployError("invalid Kubernetes node name")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--host", default=os.environ.get(REMOTE_HOST_ENV) or os.environ.get("node1ip"))
    parser.add_argument("--user", default=os.environ.get(REMOTE_USER_ENV) or os.environ.get("node1user") or "root")
    parser.add_argument("--remote-root", type=Path, default=DEFAULT_REMOTE_ROOT)
    parser.add_argument("--run-root", type=Path, default=DEFAULT_RUN_ROOT)
    parser.add_argument("--service-name", default=DEFAULT_SERVICE_NAME)
    parser.add_argument("--port", type=int, default=18085)
    parser.add_argument("--image", default=DEFAULT_IMAGE)
    parser.add_argument("--apply-job", action="store_true")
    parser.add_argument("--node-name", help="Kubernetes node name for the Job nodeSelector")
    parser.add_argument("--kubeconfig", type=Path, help="remote kubeconfig path used by kubectl apply")
    return parser


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _redacted(value: str | None) -> str:
    return "<set>" if value else "<unset>"


def _ssh_command(args: argparse.Namespace, remote_command: str) -> tuple[list[str], dict[str, str]]:
    if not args.host:
        raise DeployError("remote host is required")
    env = os.environ.copy()
    argv: list[str] = []
    password = os.environ.get(REMOTE_PASSWORD_ENV) or os.environ.get("node1pwd")
    identity = os.environ.get(REMOTE_IDENTITY_ENV)
    known_hosts = os.environ.get(REMOTE_KNOWN_HOSTS_ENV)
    if password:
        if shutil.which("sshpass") is None:
            raise DeployError("password auth requires sshpass in PATH")
        argv.extend(["sshpass", "-e"])
        env["SSHPASS"] = password
    argv.append("ssh")
    argv.extend(["-o", "ConnectTimeout=8"])
    if password:
        argv.extend(
            [
                "-o",
                "PreferredAuthentications=password",
                "-o",
                "PubkeyAuthentication=no",
                "-o",
                "NumberOfPasswordPrompts=1",
            ]
        )
    if identity:
        argv.extend(["-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-i", identity])
    if known_hosts:
        argv.extend(["-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={known_hosts}"])
    else:
        argv.extend(["-o", "StrictHostKeyChecking=accept-new"])
    argv.append(f"{args.user}@{args.host}")
    argv.append(remote_command)
    return argv, env


def _scp_command(args: argparse.Namespace, source: Path, target: str) -> tuple[list[str], dict[str, str]]:
    if not args.host:
        raise DeployError("remote host is required")
    env = os.environ.copy()
    argv: list[str] = []
    password = os.environ.get(REMOTE_PASSWORD_ENV) or os.environ.get("node1pwd")
    identity = os.environ.get(REMOTE_IDENTITY_ENV)
    known_hosts = os.environ.get(REMOTE_KNOWN_HOSTS_ENV)
    if password:
        if shutil.which("sshpass") is None:
            raise DeployError("password auth requires sshpass in PATH")
        argv.extend(["sshpass", "-e"])
        env["SSHPASS"] = password
    argv.append("scp")
    if password:
        argv.extend(
            [
                "-o",
                "PreferredAuthentications=password",
                "-o",
                "PubkeyAuthentication=no",
                "-o",
                "NumberOfPasswordPrompts=1",
            ]
        )
    if identity:
        argv.extend(["-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-i", identity])
    if known_hosts:
        argv.extend(["-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={known_hosts}"])
    else:
        argv.extend(["-o", "StrictHostKeyChecking=accept-new"])
    argv.extend([str(source), f"{args.user}@{args.host}:{target}"])
    return argv, env


def _run(argv: Sequence[str], env: Mapping[str, str], *, timeout: int = 600) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        list(argv),
        env=dict(env),
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if completed.returncode != 0:
        raise DeployError(f"{Path(argv[0]).name} failed with exit {completed.returncode}: {completed.stderr[:1200]}")
    return completed


def _archive_repo(output: Path) -> None:
    exclude_dirs = {
        ".claude",
        ".git",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "artifacts",
        "benchmark-wheels",
        "build",
        "dist",
        "node_modules",
        "runs",
        "__pycache__",
    }
    with tarfile.open(output, "w:gz") as archive:
        for path in REPO_ROOT.rglob("*"):
            relative = path.relative_to(REPO_ROOT)
            if path.name == ".DS_Store" or any(
                part in exclude_dirs for part in relative.parts
            ):
                continue
            archive.add(path, arcname=f"repo/{relative}")


def _remote_bootstrap_script(args: argparse.Namespace, archive_name: str) -> str:
    remote_root = shlex.quote(str(_safe_remote_path(args.remote_root)))
    run_root = shlex.quote(str(_safe_remote_path(args.run_root)))
    service_name = shlex.quote(args.service_name)
    archive = shlex.quote(archive_name)
    return f"""set -eu
umask 077
id resbench-scan >/dev/null 2>&1 || useradd --system --user-group --home-dir /data/mj/resbench-system --shell /usr/sbin/nologin resbench-scan
release={remote_root}/releases/{archive_name.removesuffix('.tar.gz')}
mkdir -p {remote_root}/archives {remote_root}/releases "$release" {remote_root}/state {remote_root}/logs {run_root}
tar -xzf {remote_root}/archives/{archive} -C "$release"
test -f "$release/repo/pyproject.toml"
if [ -e {remote_root}/repo ] && [ ! -L {remote_root}/repo ]; then
  echo "refusing to replace non-symlink remote repo path" >&2
  exit 3
fi
ln -sfn "$release/repo" {remote_root}/repo
cd {remote_root}/repo
if command -v uv >/dev/null 2>&1; then
  install -d -o resbench-scan -g resbench-scan -m 0755 /data/mj/resbench-tools/python
  UV_PYTHON_INSTALL_DIR=/data/mj/resbench-tools/python uv python install 3.12
  managed_python=$(UV_PYTHON_INSTALL_DIR=/data/mj/resbench-tools/python uv python find --managed-python --no-project 3.12)
  UV_PYTHON_INSTALL_DIR=/data/mj/resbench-tools/python uv sync --python "$managed_python" --extra runtime --extra test
else
  python3 -m venv .venv
  .venv/bin/python -m pip install --upgrade pip
  .venv/bin/python -m pip install -e '.[runtime,test]'
fi
chown resbench-scan:resbench-scan {remote_root}/releases
chmod 0750 {remote_root}/releases
chown -R resbench-scan:resbench-scan {remote_root}/state {remote_root}/logs {run_root} "$release"
test -r {remote_root}/kubeconfig
runuser -u resbench-scan -- kubectl --kubeconfig {remote_root}/kubeconfig auth can-i get pods -n otel-demo | grep -Fx yes
install -m 0644 deploy/semantic-scan/{service_name} /etc/systemd/system/{service_name}
systemctl daemon-reload
systemctl enable {service_name}
systemctl restart {service_name}
systemctl --no-pager --full status {service_name} >/dev/null
"""


def _remote_job_script(args: argparse.Namespace) -> str:
    remote_root = shlex.quote(str(_safe_remote_path(args.remote_root)))
    kubeconfig = (
        f"--kubeconfig {shlex.quote(str(args.kubeconfig))}" if args.kubeconfig else ""
    )
    node_name = shlex.quote(args.node_name or "")
    image = shlex.quote(args.image)
    return f"""set -eu
cd {remote_root}/repo
kubectl {kubeconfig} apply -f environment/kubernetes/resbench-system/semantic-scan-rbac.yaml
RESBENCH_NODE_NAME={node_name} RESBENCH_SEMANTIC_SCAN_IMAGE={image} sh deploy/semantic-scan/render-job.sh | kubectl {kubeconfig} apply -f -
"""


def dry_run_report(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "schema_version": "resbench-semantic-remote-deploy.v1",
        "mode": "dry-run",
        "status": "not_executed",
        "remote": {
            "host": _redacted(args.host),
            "user": _redacted(args.user),
            "password": _redacted(os.environ.get(REMOTE_PASSWORD_ENV) or os.environ.get("node1pwd")),
            "identity": _redacted(os.environ.get(REMOTE_IDENTITY_ENV)),
            "known_hosts": _redacted(os.environ.get(REMOTE_KNOWN_HOSTS_ENV)),
        },
        "planned_paths": {
            "remote_root": str(args.remote_root),
            "repo": str(args.remote_root / "repo"),
            "run_root": str(args.run_root),
        },
        "planned_actions": [
            "sync current repository snapshot to the worker",
            "install Python runtime dependencies under the remote repo",
            f"install and restart systemd unit {args.service_name}",
            "use the prebuilt linux/amd64 image reference supplied by --image",
            "optionally render and apply the resbench-system Kubernetes Job",
        ],
        "execute_required": "--execute",
    }


def execute(args: argparse.Namespace) -> dict[str, Any]:
    _validate_args(args)
    with tempfile.TemporaryDirectory(prefix="resbench-semantic-deploy-") as tmp:
        archive = Path(tmp) / "repo.tar.gz"
        _archive_repo(archive)
        remote_archive = f"{args.remote_root}/archives/repo-{datetime.now(timezone.utc):%Y%m%d%H%M%S}.tar.gz"
        archive_root = shlex.quote(str(_safe_remote_path(args.remote_root) / "archives"))
        mkdir_argv, mkdir_env = _ssh_command(args, f"mkdir -p {archive_root}")
        _run(mkdir_argv, mkdir_env)
        scp_argv, scp_env = _scp_command(args, archive, remote_archive)
        _run(scp_argv, scp_env, timeout=1800)
        bootstrap_argv, bootstrap_env = _ssh_command(args, _remote_bootstrap_script(args, Path(remote_archive).name))
        _run(bootstrap_argv, bootstrap_env, timeout=1800)
        actions = ["service"]
        if args.apply_job:
            if not args.node_name:
                raise DeployError("--apply-job requires --node-name")
            job_argv, job_env = _ssh_command(args, _remote_job_script(args))
            _run(job_argv, job_env, timeout=300)
            actions.append("job")
    return {
        "schema_version": "resbench-semantic-remote-deploy.v1",
        "mode": "execute",
        "status": "completed",
        "completed_at": _utc_now(),
        "remote_root": str(args.remote_root),
        "service": args.service_name,
        "actions": actions,
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        _validate_args(args)
        report = execute(args) if args.execute else dry_run_report(args)
    except Exception as exc:  # noqa: BLE001 - command boundary keeps secrets out.
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__, "error": str(exc)[:2000]}, ensure_ascii=False, indent=2, sort_keys=True))
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
