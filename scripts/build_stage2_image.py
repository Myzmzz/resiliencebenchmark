#!/usr/bin/env python3
"""Build/push paired controller+agent images and render old-cluster manifests."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPOSITORY = "1.94.151.57:85/observe/resbench-stage2"
DEFAULT_RUNTIME_BASE = (
    "1.94.151.57:85/observe/resbench-stage2@"
    "sha256:416b7a66756e69438c8a50e5aba407c0951eb98339fd765f654e1f7d8cb2b7cf"
)
TEMPLATES = ("stage2.yaml", "execution-identities.yaml", "stage2-integration.yaml", "stage2-matrix-job.yaml")


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
    parser.add_argument("--bladeai-context", type=Path, required=True)
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
    bladeai_context = args.bladeai_context.resolve()
    if not bladeai_context.is_dir() or not (bladeai_context / "pyproject.toml").is_file():
        raise RuntimeError("--bladeai-context must be the real BladeAI SDK source directory")
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
    with tempfile.TemporaryDirectory(prefix="resbench-stage2-overlay-") as raw:
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
