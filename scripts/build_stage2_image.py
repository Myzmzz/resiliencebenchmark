#!/usr/bin/env python3
"""Build and push the linux/amd64 Stage-2 service and review UI overlay."""

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


def source_digest() -> str:
    digest = hashlib.sha256()
    roots = [
        REPO_ROOT / "stage2_service",
        REPO_ROOT / "harness",
        REPO_ROOT / "mcp_servers",
        REPO_ROOT / "frontend",
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
            REPO_ROOT
            / "tasks/examples/public/episode.otel-accounting-cpu-d0.v1.yaml",
            REPO_ROOT / "deploy/stage2/Dockerfile.runtime-overlay",
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", default=DEFAULT_REPOSITORY)
    parser.add_argument("--runtime-base", default=DEFAULT_RUNTIME_BASE)
    parser.add_argument("--builder")
    parser.add_argument(
        "--metadata",
        type=Path,
        default=REPO_ROOT / "artifacts/stage2/image.json",
    )
    args = parser.parse_args(argv)
    head = git_head()
    content_sha = source_digest()
    image = f"{args.repository}:stage2-d0-{head}"
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
        metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
    digest = str(metadata.get("containerimage.digest") or "")
    if not digest.startswith("sha256:"):
        raise RuntimeError("buildx did not report a pushed manifest digest")
    output = {
        "schema_version": "stage2-runtime-overlay-image.v1",
        "image": image,
        "digest": digest,
        "immutable_ref": f"{image}@{digest}",
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
    print(json.dumps(output, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
