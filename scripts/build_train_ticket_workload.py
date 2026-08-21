#!/usr/bin/env python3
"""Build the Train-Ticket workload image.

Dry-run is the default. Real builds require ``--execute`` and use a fixed
``docker buildx build`` argument vector. Harbor pushes are only attempted when
``--push`` is explicitly supplied; credentials are expected to be handled by the
operator's existing Docker/Harbor login flow.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse


DEFAULT_CONTEXT = Path("environment/workloads/train-ticket/image")
DEFAULT_DOCKERFILE = DEFAULT_CONTEXT / "Dockerfile"
DEFAULT_TAG = "train-ticket-workload"
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class BuildError(Exception):
    """Raised when the workload image cannot be planned or built safely."""


@dataclass(frozen=True)
class BuildPlan:
    repository: str
    tag: str
    dockerfile: Path
    context: Path
    platform: str
    push: bool


class CommandRunner(Protocol):
    def run(self, argv: list[str]) -> str:
        """Run a command and return stdout."""


class SubprocessRunner:
    def run(self, argv: list[str]) -> str:
        completed = subprocess.run(
            argv,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if completed.returncode != 0:
            raise BuildError(f"command failed: {' '.join(argv)}: {completed.stderr.strip() or completed.stdout.strip()}")
        return completed.stdout.strip()


def normalize_registry(value: str) -> str:
    raw = value.strip()
    if not raw:
        return ""
    parsed = urlparse(raw if "://" in raw else f"//{raw}")
    if parsed.scheme and parsed.scheme not in {"http", "https"}:
        raise BuildError("HARBOR_REGISTRY must use http or https when a scheme is supplied")
    if not parsed.hostname or parsed.username or parsed.password:
        raise BuildError("HARBOR_REGISTRY must be a hostname with optional port and no userinfo")
    if parsed.path not in {"", "/"} or parsed.params or parsed.query or parsed.fragment:
        raise BuildError("HARBOR_REGISTRY must not contain a path, query, fragment, or userinfo")
    try:
        port = parsed.port
    except ValueError as exc:
        raise BuildError("HARBOR_REGISTRY port is invalid") from exc
    return f"{parsed.hostname}:{port}" if port is not None else parsed.hostname


def normalize_project(value: str) -> str:
    project = value.strip().strip("/")
    if project and not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,127}", project):
        raise BuildError("Harbor project must be one lowercase project name")
    return project


def default_repository() -> str:
    explicit = os.environ.get("TRAIN_TICKET_WORKLOAD_IMAGE_REPOSITORY", "").strip()
    if explicit:
        return explicit
    registry = normalize_registry(os.environ.get("HARBOR_REGISTRY", ""))
    project = normalize_project(os.environ.get("HARBOR_PROJECT_TRAIN_TICKET", ""))
    if registry and project:
        return f"{registry}/{project}/train-ticket-workload"
    return "localhost/resiliencebenchmark/train-ticket-workload"


def validate_repository(value: str) -> str:
    repo = value.strip().strip("/")
    if not repo or "@" in repo or "://" in repo or any(part in {"", ".", ".."} for part in repo.split("/")):
        raise BuildError("repository must be an image repository without scheme, tag, digest, or traversal")
    if not re.fullmatch(r"[A-Za-z0-9._:/-]+", repo):
        raise BuildError("repository contains unsupported characters")
    return repo


def validate_tag(value: str) -> str:
    tag = value.strip()
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", tag):
        raise BuildError("tag is not a valid container image tag")
    return tag


def build_ref(plan: BuildPlan) -> str:
    return f"{plan.repository}:{plan.tag}"


def build_argv(plan: BuildPlan, metadata_path: Path) -> list[str]:
    argv = [
        "docker",
        "buildx",
        "build",
        "--platform",
        plan.platform,
        "--file",
        str(plan.dockerfile),
        "--tag",
        build_ref(plan),
        "--metadata-file",
        str(metadata_path),
    ]
    argv.append("--push" if plan.push else "--load")
    argv.append(str(plan.context))
    return argv


def display_path(path: Path, root: Path | None = None) -> str:
    resolved = path.resolve()
    base = (root or Path.cwd()).resolve()
    try:
        return str(resolved.relative_to(base))
    except ValueError:
        return f"<path:{resolved.name}>"


def safe_command(argv: list[str], *, root: Path | None = None) -> list[str]:
    safe: list[str] = []
    skip_next = False
    for index, part in enumerate(argv):
        if skip_next:
            skip_next = False
            continue
        if part in {"--file", "--metadata-file"} and index + 1 < len(argv):
            safe.extend([part, display_path(Path(argv[index + 1]), root)])
            skip_next = True
        elif index == len(argv) - 1:
            safe.append(display_path(Path(part), root))
        else:
            safe.append(part)
    return safe


def inspect_base_image(dockerfile: Path) -> str:
    content = dockerfile.read_text(encoding="utf-8")
    for line in content.splitlines():
        if line.startswith("FROM "):
            image = line.split(None, 1)[1].strip()
            if "@sha256:" not in image:
                raise BuildError("Dockerfile base image must be pinned by sha256 digest")
            return image
    raise BuildError("Dockerfile missing FROM")


def plan_build(
    *,
    repository: str | None,
    tag: str,
    dockerfile: Path,
    context: Path,
    platform: str,
    push: bool,
) -> BuildPlan:
    dockerfile = dockerfile.resolve()
    context = context.resolve()
    if not dockerfile.is_file():
        raise BuildError(f"Dockerfile does not exist: {display_path(dockerfile)}")
    if not context.is_dir():
        raise BuildError(f"context directory does not exist: {display_path(context)}")
    if not str(dockerfile).startswith(str(context)):
        raise BuildError("Dockerfile must live inside the build context")
    inspect_base_image(dockerfile)
    if platform != "linux/amd64":
        raise BuildError("only linux/amd64 is currently supported")
    return BuildPlan(
        repository=validate_repository(repository or default_repository()),
        tag=validate_tag(tag),
        dockerfile=dockerfile,
        context=context,
        platform=platform,
        push=push,
    )


def parse_metadata(path: Path) -> tuple[str | None, str | None]:
    if not path.is_file():
        return None, None
    data = json.loads(path.read_text(encoding="utf-8") or "{}")
    digest = data.get("containerimage.digest")
    if digest == "":
        digest = None
    image_id = data.get("containerimage.config.digest") or data.get("containerimage.descriptor", {}).get("digest")
    if digest is not None and not DIGEST_RE.match(str(digest)):
        raise BuildError(f"docker metadata returned invalid image digest: {digest}")
    return str(digest) if digest else None, str(image_id) if image_id else None


def build_image(plan: BuildPlan, *, execute: bool, runner: CommandRunner | None = None) -> dict[str, object]:
    runner = runner or SubprocessRunner()
    if execute and shutil.which("docker") is None and isinstance(runner, SubprocessRunner):
        raise BuildError("docker is required for --execute but was not found in PATH")
    with tempfile.TemporaryDirectory(prefix="resbench-tt-image-") as tmpdir:
        metadata_path = Path(tmpdir) / "metadata.json"
        argv = build_argv(plan, metadata_path)
        result: dict[str, object] = {
            "repository": plan.repository,
            "tag": plan.tag,
            "ref": build_ref(plan),
            "platform": plan.platform,
            "push": plan.push,
            "baseImage": inspect_base_image(plan.dockerfile),
            "command": safe_command(argv),
            "digest": None,
            "pinnedImage": None,
        }
        if not execute:
            result["dryRun"] = True
            return result
        runner.run(argv)
        digest, image_id = parse_metadata(metadata_path)
        if plan.push and not digest:
            raise BuildError("push build did not return a manifest digest")
        result["dryRun"] = False
        result["digest"] = digest
        result["imageId"] = image_id
        if digest:
            result["pinnedImage"] = f"{plan.repository}@{digest}"
        return result


def write_json(data: dict[str, object]) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build the Train-Ticket workload image.")
    parser.add_argument("--repository", help="Target image repository. Defaults to env-derived Harbor repo or localhost.")
    parser.add_argument("--tag", default=DEFAULT_TAG)
    parser.add_argument("--context", type=Path, default=DEFAULT_CONTEXT)
    parser.add_argument("--dockerfile", type=Path, default=DEFAULT_DOCKERFILE)
    parser.add_argument("--platform", default="linux/amd64")
    parser.add_argument("--execute", action="store_true", help="Run docker buildx. Default is dry-run.")
    parser.add_argument("--push", action="store_true", help="Push the built image. Requires --execute and prior registry login.")
    return parser


def run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.push and not args.execute:
            raise BuildError("--push requires --execute")
        plan = plan_build(
            repository=args.repository,
            tag=args.tag,
            dockerfile=args.dockerfile,
            context=args.context,
            platform=args.platform,
            push=args.push,
        )
        sys.stdout.write(write_json(build_image(plan, execute=args.execute)))
    except BuildError as exc:
        print(f"build_train_ticket_workload: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
