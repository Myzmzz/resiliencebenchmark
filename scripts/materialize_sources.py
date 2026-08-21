#!/usr/bin/env python3
"""Materialize locked source repositories for BenchmarkFactory agents.

The script reads ``environment/shared/source-locks.yaml`` and checks out each
selected git lock into an explicit destination root. It verifies the exact
commit and the ``git archive HEAD`` SHA-256 before writing a redacted manifest.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence
from urllib.parse import urlparse, urlunparse

import yaml


DEFAULT_LOCKFILE = Path("environment/shared/source-locks.yaml")
DEFAULT_OUTPUT = Path("artifacts/source-materialization-manifest.json")
DESTINATION_ROOT_ENV = "RESBENCH_SOURCE_ROOT"
HTTP_URL_RE = re.compile(r"https?://[^\s'\"<>]+", re.IGNORECASE)
SECRET_TEXT_RE = re.compile(
    r"(?i)(access[_-]?token|api[_-]?key|password|secret)(\s*[:=]\s*)([^\s,;]+)"
)


class SourceMaterializationError(RuntimeError):
    """Raised when a source lock cannot be materialized safely."""


@dataclass(frozen=True)
class SourceLock:
    id: str
    application: str
    remote: str
    commit: str
    archive_sha256: str
    component: str | None = None
    tag: str | None = None
    agent_mount_path: str | None = None
    runtime_mapping_status: str | None = None


@dataclass(frozen=True)
class MaterializedSource:
    lock: SourceLock
    destination_ref: str
    head: str
    archive_sha256: str
    mode: str


def run_git(
    args: Sequence[str],
    *,
    cwd: Path | None = None,
    capture_stdout: bool = True,
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(  # noqa: S603 - arguments are fixed lists.
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        check=False,
        stdout=subprocess.PIPE if capture_stdout else subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    if check and result.returncode != 0:
        stderr = redact_git_text(result.stderr.decode("utf-8", errors="replace").strip())
        command = "git " + " ".join(redact_git_arg(arg) for arg in args)
        raise SourceMaterializationError(f"{command} failed: {stderr}")
    return result


def redact_git_arg(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme in {"http", "https"}:
        return redacted_remote_ref(value)
    return value


def redact_git_text(value: str) -> str:
    def replace_url(match: re.Match[str]) -> str:
        return redacted_remote_ref(match.group(0).rstrip(".!,"))

    redacted = HTTP_URL_RE.sub(replace_url, value)
    return SECRET_TEXT_RE.sub(lambda match: f"{match.group(1)}{match.group(2)}<redacted>", redacted)


def load_lockfile(path: Path) -> list[SourceLock]:
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if not isinstance(data, dict):
        raise SourceMaterializationError("source lockfile top-level document must be a mapping")

    raw_locks = data.get("spec", {}).get("locks")
    if not isinstance(raw_locks, list) or not raw_locks:
        raise SourceMaterializationError("source lockfile must define spec.locks")

    locks: list[SourceLock] = []
    for index, raw in enumerate(raw_locks):
        if not isinstance(raw, dict):
            raise SourceMaterializationError(f"source lock #{index} must be a mapping")
        missing = [key for key in ("id", "application", "remote", "commit", "archiveSha256") if not raw.get(key)]
        if missing:
            raise SourceMaterializationError(f"source lock #{index} missing required field(s): {', '.join(missing)}")
        locks.append(
            SourceLock(
                id=str(raw["id"]),
                application=str(raw["application"]),
                component=str(raw["component"]) if raw.get("component") else None,
                remote=str(raw["remote"]),
                tag=str(raw["tag"]) if raw.get("tag") else None,
                commit=str(raw["commit"]),
                archive_sha256=str(raw["archiveSha256"]),
                agent_mount_path=str(raw["agentMountPath"]) if raw.get("agentMountPath") else None,
                runtime_mapping_status=str(raw["runtimeMappingStatus"])
                if raw.get("runtimeMappingStatus")
                else None,
            )
        )
    return locks


def select_locks(
    locks: Iterable[SourceLock],
    *,
    applications: set[str] | None,
    components: set[str] | None,
) -> list[SourceLock]:
    selected: list[SourceLock] = []
    for lock in locks:
        if applications and lock.application not in applications:
            continue
        if components and lock.component not in components:
            continue
        selected.append(lock)
    if not selected:
        raise SourceMaterializationError("no source locks matched the requested filters")
    return selected


def parse_csv_filter(values: list[str] | None) -> set[str] | None:
    if not values:
        return None
    parsed = {item.strip() for value in values for item in value.split(",") if item.strip()}
    return parsed or None


def ensure_safe_destination_root(destination: Path) -> Path:
    resolved = destination.expanduser().resolve()
    if resolved == Path("/"):
        raise SourceMaterializationError("refusing to use filesystem root as source destination")
    try:
        home = Path.home().resolve()
    except RuntimeError:
        home = None
    if home and resolved == home:
        raise SourceMaterializationError("refusing to use the user home directory as source destination")
    return resolved


def is_empty_dir(path: Path) -> bool:
    return path.is_dir() and next(path.iterdir(), None) is None


def ensure_target_available(target: Path) -> None:
    if not target.exists():
        return
    if is_empty_dir(target):
        return
    raise SourceMaterializationError(
        f"destination for {target.name} already exists and is not empty; "
        "use --verify-existing for offline verification"
    )


def verify_clean_worktree(repo: Path) -> None:
    result = run_git(["status", "--porcelain", "--untracked-files=normal"], cwd=repo)
    status = result.stdout.decode("utf-8", errors="replace").strip()
    if status:
        raise SourceMaterializationError(f"repository {repo.name} has uncommitted or untracked changes")


def verify_detached_head(repo: Path) -> None:
    result = run_git(["symbolic-ref", "-q", "HEAD"], cwd=repo, check=False)
    if result.returncode == 0:
        branch = result.stdout.decode("utf-8", errors="replace").strip()
        raise SourceMaterializationError(f"repository {repo.name} is not detached; HEAD points to {branch}")


def current_head(repo: Path) -> str:
    result = run_git(["rev-parse", "HEAD"], cwd=repo)
    return result.stdout.decode("utf-8", errors="replace").strip()


def archive_sha256(repo: Path) -> str:
    result = run_git(["archive", "HEAD"], cwd=repo)
    return hashlib.sha256(result.stdout).hexdigest()


def verify_locked_checkout(lock: SourceLock, target: Path) -> tuple[str, str]:
    if not (target / ".git").exists():
        raise SourceMaterializationError(f"destination for {lock.id} is not a git repository")
    verify_clean_worktree(target)
    verify_detached_head(target)

    head = current_head(target)
    if head != lock.commit:
        raise SourceMaterializationError(f"{lock.id} checked out {head}, expected {lock.commit}")

    digest = archive_sha256(target)
    if digest != lock.archive_sha256:
        raise SourceMaterializationError(
            f"{lock.id} archive SHA-256 mismatch: got {digest}, expected {lock.archive_sha256}"
        )
    return head, digest


def clone_and_checkout(lock: SourceLock, target: Path) -> None:
    ensure_target_available(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    run_git(["clone", "--no-checkout", lock.remote, str(target)], capture_stdout=False)
    run_git(["checkout", "--detach", lock.commit], cwd=target, capture_stdout=False)


def destination_ref(lock_id: str) -> str:
    return f"${{{DESTINATION_ROOT_ENV}}}/{lock_id}"


def redacted_remote_ref(remote: str) -> str:
    parsed = urlparse(remote)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        hostname = parsed.hostname or "<redacted-host>"
        if parsed.port is not None:
            hostname = f"{hostname}:{parsed.port}"
        return urlunparse((parsed.scheme, hostname, parsed.path, "", "", ""))
    return "<redacted-local-or-private-remote>"


def redacted_path_ref(path: Path) -> str:
    if not path.is_absolute():
        return path.as_posix()
    try:
        return path.resolve().relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return f"<redacted-absolute-path>/{path.name}"


def materialize_lock(lock: SourceLock, destination_root: Path, *, verify_existing: bool) -> MaterializedSource:
    target = destination_root / lock.id
    if verify_existing:
        if not target.exists():
            raise SourceMaterializationError(f"destination for {lock.id} does not exist")
        mode = "verify-existing"
    else:
        clone_and_checkout(lock, target)
        mode = "materialize"

    head, digest = verify_locked_checkout(lock, target)
    return MaterializedSource(
        lock=lock,
        destination_ref=destination_ref(lock.id),
        head=head,
        archive_sha256=digest,
        mode=mode,
    )


def build_manifest(
    *,
    lockfile: Path,
    selected: list[MaterializedSource],
    verify_existing: bool,
    applications: set[str] | None,
    components: set[str] | None,
) -> dict[str, Any]:
    return {
        "apiVersion": "resiliencebenchmark.io/v1alpha1",
        "kind": "SourceMaterializationManifest",
        "metadata": {
            "generatedAt": datetime.now(timezone.utc).isoformat(),
            "lockfile": redacted_path_ref(lockfile),
        },
        "spec": {
            "destinationRootEnv": DESTINATION_ROOT_ENV,
            "mode": "verify-existing" if verify_existing else "materialize",
            "selection": {
                "applications": sorted(applications) if applications else None,
                "components": sorted(components) if components else None,
            },
            "sources": [
                {
                    "id": item.lock.id,
                    "application": item.lock.application,
                    "component": item.lock.component,
                    "remoteRef": redacted_remote_ref(item.lock.remote),
                    "tag": item.lock.tag,
                    "commit": item.head,
                    "archiveSha256": item.archive_sha256,
                    "agentMountPath": item.lock.agent_mount_path,
                    "destinationRef": item.destination_ref,
                    "runtimeMappingStatus": item.lock.runtime_mapping_status,
                    "mode": item.mode,
                }
                for item in selected
            ],
        },
    }


def write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2, sort_keys=True)
        handle.write("\n")


def materialize_sources(
    *,
    lockfile: Path,
    destination: Path,
    output: Path,
    applications: set[str] | None = None,
    components: set[str] | None = None,
    verify_existing: bool = False,
) -> dict[str, Any]:
    destination_root = ensure_safe_destination_root(destination)
    locks = select_locks(load_lockfile(lockfile), applications=applications, components=components)
    selected = [
        materialize_lock(lock, destination_root, verify_existing=verify_existing)
        for lock in locks
    ]
    manifest = build_manifest(
        lockfile=lockfile,
        selected=selected,
        verify_existing=verify_existing,
        applications=applications,
        components=components,
    )
    write_manifest(output, manifest)
    return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Materialize locked benchmark source repositories")
    parser.add_argument("--lockfile", type=Path, default=DEFAULT_LOCKFILE, help="source lock YAML file")
    parser.add_argument(
        "--destination",
        type=Path,
        required=True,
        help=f"explicit source root; materialized paths are stored as ${{{DESTINATION_ROOT_ENV}}}/<lock-id>",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="redacted manifest output path")
    parser.add_argument("--application", action="append", help="application filter; may be repeated or comma-separated")
    parser.add_argument("--component", action="append", help="component filter; may be repeated or comma-separated")
    parser.add_argument(
        "--verify-existing",
        action="store_true",
        help="offline mode: verify existing checkouts without cloning or fetching",
    )
    return parser


def run(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    manifest = materialize_sources(
        lockfile=args.lockfile,
        destination=args.destination,
        output=args.output,
        applications=parse_csv_filter(args.application),
        components=parse_csv_filter(args.component),
        verify_existing=args.verify_existing,
    )
    print(json.dumps({"status": "ok", "sources": len(manifest["spec"]["sources"])}, sort_keys=True))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    try:
        return run(argv)
    except SourceMaterializationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
