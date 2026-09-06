#!/usr/bin/env python3
"""Build/push paired controller+agent images and render old-cluster manifests."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPOSITORY = "1.94.151.57:85/observe/resbench-stage2"
DEFAULT_RUNTIME_BASE = (
    "1.94.151.57:85/observe/resbench-stage2@"
    "sha256:416b7a66756e69438c8a50e5aba407c0951eb98339fd765f654e1f7d8cb2b7cf"
)
TEMPLATES = ("stage2.yaml", "execution-identities.yaml", "stage2-integration.yaml", "stage2-matrix-job.yaml")
BLADEAI_RELEASE_TAG = "blade-ai-v0.6.2"
BLADEAI_RELEASE_COMMIT = "d8c5473ccda329a3841f114f83a43881a2205ab5"
BLADEAI_SUBTREE = "blade-ai"
BLADEAI_PACKAGE_NAME = "blade-ai"
BLADEAI_PACKAGE_VERSION = "0.3.0"
BLADEAI_MCP_DEPENDENCY = "mcp>=1.0,<2.0"
BLADEAI_MCP_PIN = "1.27.0"
BLADEAI_REQUIRED_PATHS = (
    "pyproject.toml",
    "hatch_build.py",
    "src/chaos_agent/__init__.py",
    "tui/package.json",
)


@dataclass(frozen=True)
class BladeAISourceMetadata:
    release_tag: str
    release_commit: str
    subtree: str
    package_name: str
    package_version: str
    mcp_dependency: str
    mcp_pin: str


def source_digest() -> str:
    digest = hashlib.sha256()
    roots = [
        REPO_ROOT / "controller",
        REPO_ROOT / "stage2_service",
        REPO_ROOT / "harness",
        REPO_ROOT / "mcp_servers",
        REPO_ROOT / "frontend",
        REPO_ROOT / "tasks/episodes",
    ]
    files = [
        path
        for root in roots
        for path in root.rglob("*")
        if path.is_file()
        and "__pycache__" not in path.parts
        and "node_modules" not in path.parts
        and "dist" not in path.parts
        and path.suffix not in {".pyc", ".pyo"}
    ]
    files.extend(
        [
            REPO_ROOT / "scripts/run_harness_trial.py",
            REPO_ROOT / "scripts/run_otel_accounting_cpu_matrix.py",
            REPO_ROOT / "scripts/run_stage2_matrix.py",
            REPO_ROOT / "scripts/build_stage2_qualification_matrix.py",
            REPO_ROOT / "scripts/probe_models.py",
            REPO_ROOT / "scripts/qualify_agent_channel.py",
            REPO_ROOT / "scripts/publish_harness_capabilities.py",
            REPO_ROOT / "scripts/qualify_execution_identities.py",
            REPO_ROOT / "deploy/stage2/Dockerfile.runtime-overlay",
            REPO_ROOT / "deploy/stage2/Dockerfile.agent",
            REPO_ROOT / "deploy/stage2/codex-eval",
        ]
    )
    for path in sorted(files):
        digest.update(path.relative_to(REPO_ROOT).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def git_head() -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "--short=7", "HEAD"],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if completed.returncode or not completed.stdout.strip():
        raise RuntimeError("could not resolve Stage-2 source HEAD")
    return completed.stdout.strip()


def prepare_bladeai_build_context(upstream_repo: Path, destination: Path) -> BladeAISourceMetadata:
    """Materialize the fixed upstream blade-ai subtree without reading dirty files."""

    repo = Path(upstream_repo).resolve()
    if not repo.is_dir():
        raise RuntimeError("--bladeai-repo must be an existing upstream chaosblade Git repository")
    if _git_text(repo, "rev-parse", "--is-inside-work-tree") != "true":
        raise RuntimeError("--bladeai-repo must be a non-bare Git worktree")
    release_commit = _git_text(repo, "rev-parse", f"{BLADEAI_RELEASE_TAG}^{{commit}}")
    if release_commit != BLADEAI_RELEASE_COMMIT:
        raise RuntimeError(
            f"{BLADEAI_RELEASE_TAG} resolved to {release_commit}, expected {BLADEAI_RELEASE_COMMIT}"
        )
    for relative in BLADEAI_REQUIRED_PATHS:
        _git_text(repo, "cat-file", "-e", f"{BLADEAI_RELEASE_TAG}:{BLADEAI_SUBTREE}/{relative}")
    _git_text(repo, "cat-file", "-e", f"{BLADEAI_RELEASE_TAG}:.github/workflows/release-blade-ai.yml")
    pyproject = _git_text(repo, "show", f"{BLADEAI_RELEASE_TAG}:{BLADEAI_SUBTREE}/pyproject.toml")
    package_name, package_version, dependencies = _bladeai_project_metadata(pyproject)
    if package_name != BLADEAI_PACKAGE_NAME:
        raise RuntimeError(f"BladeAI package name is {package_name!r}, expected {BLADEAI_PACKAGE_NAME!r}")
    if package_version != BLADEAI_PACKAGE_VERSION:
        raise RuntimeError(
            f"{BLADEAI_RELEASE_TAG} package version is {package_version}, expected {BLADEAI_PACKAGE_VERSION}"
        )
    if BLADEAI_MCP_DEPENDENCY not in dependencies:
        raise RuntimeError(f"{BLADEAI_RELEASE_TAG} must declare {BLADEAI_MCP_DEPENDENCY}")
    names = _git_text(
        repo, "ls-tree", "-r", "--name-only", BLADEAI_RELEASE_TAG, "--", BLADEAI_SUBTREE
    ).splitlines()
    if any(_forbidden_bladeai_archive_path(name) for name in names):
        raise RuntimeError("BladeAI source archive would include native ChaosBlade material")
    destination = Path(destination)
    if destination.exists() and any(destination.iterdir()):
        raise RuntimeError("BladeAI build context destination must be empty")
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    _extract_bladeai_subtree(
        _git_bytes(repo, "archive", "--format=tar", BLADEAI_RELEASE_TAG, BLADEAI_SUBTREE),
        destination,
    )
    return BladeAISourceMetadata(
        release_tag=BLADEAI_RELEASE_TAG,
        release_commit=release_commit,
        subtree=BLADEAI_SUBTREE,
        package_name=package_name,
        package_version=package_version,
        mcp_dependency=BLADEAI_MCP_DEPENDENCY,
        mcp_pin=BLADEAI_MCP_PIN,
    )


def _git_text(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
    )
    if completed.returncode:
        raise RuntimeError(f"git {' '.join(args)} failed for BladeAI source")
    return completed.stdout.strip()


def _git_bytes(repo: Path, *args: str) -> bytes:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=False,
        capture_output=True,
        timeout=60,
        env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
    )
    if completed.returncode:
        raise RuntimeError(f"git {' '.join(args)} failed for BladeAI source")
    return completed.stdout


def _bladeai_project_metadata(document: str) -> tuple[str, str, set[str]]:
    try:
        data = tomllib.loads(document)
    except tomllib.TOMLDecodeError as exc:
        raise RuntimeError("BladeAI pyproject.toml is invalid") from exc
    project = data.get("project")
    if not isinstance(project, dict):
        raise RuntimeError("BladeAI pyproject.toml is missing [project]")
    package_name = project.get("name")
    package_version = project.get("version")
    dependencies = project.get("dependencies")
    if not isinstance(package_name, str) or not package_name:
        raise RuntimeError("BladeAI pyproject.toml is missing project.name")
    if not isinstance(package_version, str) or not package_version:
        raise RuntimeError("BladeAI pyproject.toml is missing project.version")
    if not isinstance(dependencies, list) or not all(isinstance(item, str) for item in dependencies):
        raise RuntimeError("BladeAI pyproject.toml is missing project.dependencies")
    return package_name, package_version, set(dependencies)


def _forbidden_bladeai_archive_path(path: str) -> bool:
    value = PurePosixPath(path)
    return value.name == "blade" or path.startswith(f"{BLADEAI_SUBTREE}/vendor/chaosblade/")


def _extract_bladeai_subtree(archive: bytes, destination: Path) -> None:
    root = destination.resolve()
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as handle:
        for member in handle.getmembers():
            source = PurePosixPath(member.name)
            parts = source.parts
            if not parts or parts[0] != BLADEAI_SUBTREE:
                raise RuntimeError("BladeAI archive contains an unexpected path")
            relative_parts = parts[1:]
            if not relative_parts:
                continue
            if any(part in {"", ".", ".."} for part in relative_parts):
                raise RuntimeError("BladeAI archive contains an unsafe path")
            if member.issym() or member.islnk():
                raise RuntimeError("BladeAI archive contains links")
            target = (root / Path(*relative_parts)).resolve()
            target.relative_to(root)
            if member.isdir():
                target.mkdir(mode=0o755, parents=True, exist_ok=True)
                continue
            if not member.isfile():
                raise RuntimeError("BladeAI archive contains unsupported file types")
            target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            source_file = handle.extractfile(member)
            if source_file is None:
                raise RuntimeError("BladeAI archive member cannot be read")
            with source_file, open(target, "wb") as output:
                shutil.copyfileobj(source_file, output)
            os.chmod(target, member.mode & 0o777 or 0o644)


def render_manifests(*, controller: str, agent: str, source_head: str, destination: Path) -> list[Path]:
    """Render old-cluster Stage2 workloads and their identity RBAC; never apply."""
    destination.mkdir(parents=True, exist_ok=True)
    rendered: list[Path] = []
    for name in TEMPLATES:
        text = (REPO_ROOT / "deploy/stage2" / name).read_text(encoding="utf-8")
        text = text.replace("__STAGE2_IMAGE__", controller).replace("__STAGE2_AGENT_IMAGE__", agent).replace("__SOURCE_HEAD__", source_head)
        if "__STAGE2_" in text or "__SOURCE_HEAD__" in text:
            raise RuntimeError(f"unresolved image placeholder in {name}")
        output = destination / name
        output.write_text(text, encoding="utf-8")
        rendered.append(output)
    return rendered


def _metadata_digest(path: Path) -> str:
    metadata = json.loads(path.read_text(encoding="utf-8"))
    digest = str(metadata.get("containerimage.digest") or "")
    if not digest.startswith("sha256:"):
        raise RuntimeError("buildx did not report a pushed manifest digest")
    return digest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", default=DEFAULT_REPOSITORY)
    parser.add_argument("--runtime-base", default=DEFAULT_RUNTIME_BASE)
    parser.add_argument("--builder")
    parser.add_argument("--bladeai-repo", type=Path, required=True)
    parser.add_argument(
        "--metadata",
        type=Path,
        default=REPO_ROOT / "artifacts/stage2/image.json",
    )
    parser.add_argument("--render-dir", type=Path, default=REPO_ROOT / "artifacts/stage2/rendered-old-cluster")
    args = parser.parse_args(argv)
    head = git_head()
    content_sha = source_digest()
    image = f"{args.repository}:stage2-d0-{head}"
    agent_image = f"{args.repository}:stage2-agent-{head}"
    with tempfile.TemporaryDirectory(prefix="resbench-stage2-build-") as raw:
        build_root = Path(raw)
        bladeai_context = build_root / "blade-ai-context"
        bladeai_metadata = prepare_bladeai_build_context(args.bladeai_repo, bladeai_context)
        pnpm = shutil.which("pnpm")
        if not pnpm:
            raise RuntimeError("pnpm is required to build the Stage-2 frontend")
        frontend = subprocess.run(
            [pnpm, "build"],
            cwd=REPO_ROOT / "frontend",
            check=False,
            timeout=600,
        )
        if frontend.returncode:
            raise RuntimeError("Stage-2 frontend build failed")
        metadata_file = Path(raw) / "build-metadata.json"
        build_argv = [
            "docker",
            "buildx",
            "build",
        ]
        if args.builder:
            build_argv.extend(["--builder", args.builder])
        build_argv.extend(
            [
            "--progress=plain",
            "--pull=false",
            "--platform",
            "linux/amd64",
            "--build-arg",
            f"STAGE2_RUNTIME_BASE={args.runtime_base}",
            "--build-arg",
            f"SOURCE_HEAD={head}",
            "--file",
            str(REPO_ROOT / "deploy/stage2/Dockerfile.runtime-overlay"),
            "--tag",
            image,
            "--metadata-file",
            str(metadata_file),
            "--push",
            str(REPO_ROOT),
            ]
        )
        completed = subprocess.run(build_argv, check=False, timeout=3600)
        if completed.returncode:
            raise RuntimeError("Stage-2 overlay build/push failed")
        digest = _metadata_digest(metadata_file)
        agent_metadata = Path(raw) / "agent-build-metadata.json"
        agent_argv = ["docker", "buildx", "build"]
        if args.builder:
            agent_argv.extend(["--builder", args.builder])
        agent_argv.extend([
            "--progress=plain", "--pull=false", "--platform", "linux/amd64",
            "--build-context", f"bladeai-src={bladeai_context}",
            "--build-arg", f"SOURCE_HEAD={head}",
            "--build-arg", f"BLADEAI_RELEASE_TAG={bladeai_metadata.release_tag}",
            "--build-arg", f"BLADEAI_RELEASE_COMMIT={bladeai_metadata.release_commit}",
            "--build-arg", f"BLADEAI_PACKAGE_VERSION={bladeai_metadata.package_version}",
            "--build-arg", f"BLADEAI_MCP_VERSION={bladeai_metadata.mcp_pin}",
            "--file", str(REPO_ROOT / "deploy/stage2/Dockerfile.agent"),
            "--tag", agent_image, "--metadata-file", str(agent_metadata), "--push", str(REPO_ROOT),
        ])
        if subprocess.run(agent_argv, check=False, timeout=3600).returncode:
            raise RuntimeError("Stage-2 agent image build/push failed")
        agent_digest = _metadata_digest(agent_metadata)
    output = {
        "schema_version": "stage2-runtime-overlay-image.v1",
        "image": image,
        "digest": digest,
        "immutable_ref": f"{image}@{digest}",
        "agent_image": agent_image,
        "agent_digest": agent_digest,
        "agent_immutable_ref": f"{agent_image}@{agent_digest}",
        "runtime_base": args.runtime_base,
        "source_head": head,
        "source_sha256": content_sha,
        "platform": "linux/amd64",
        "frontend_included": True,
        "bladeai_source": asdict(bladeai_metadata),
    }
    destination = args.metadata.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    output["rendered_manifests"] = [str(path) for path in render_manifests(
        controller=output["immutable_ref"], agent=output["agent_immutable_ref"], source_head=head,
        destination=args.render_dir.resolve(),
    )]
    destination.write_text(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(output, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
