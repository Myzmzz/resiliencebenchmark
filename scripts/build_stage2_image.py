#!/usr/bin/env python3
"""Build and push the single linux/amd64 Stage-2 service image."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = REPO_ROOT.parent
BLADEAI_REPO = WORKSPACE_ROOT / "bladeai-source/blade-ai"
OTEL_SOURCE = WORKSPACE_ROOT / "benchmark-sources/materialized/otel-demo-2.2.0"
BLADEAI_TAG = "blade-ai-v0.6.1"
OTEL_CHART_VERSION = "0.40.5"
OTEL_CHART_SHA256 = "92dd3c2e0d5d7a0db76cb3aefb21ae90adccf915c37803c82d454f5c74fc1d7a"
DEFAULT_REPOSITORY = "1.94.151.57:85/observe/resbench-stage2"
DEFAULT_PROXY = "http://host.docker.internal:7890"
DEFAULT_HOST_PROXY = "http://127.0.0.1:7890"
DEFAULT_NO_PROXY = "127.0.0.1,localhost,.svc,.cluster.local,kubernetes.default.svc"


def source_digest() -> str:
    digest = hashlib.sha256()
    digest.update(f"otel-chart:{OTEL_CHART_VERSION}:{OTEL_CHART_SHA256}\0".encode())
    roots = [
        REPO_ROOT / "stage2_service",
        REPO_ROOT / "mcp_servers",
        REPO_ROOT / "harness",
        REPO_ROOT / "controller",
        REPO_ROOT / "disturbances",
        REPO_ROOT / "evaluator",
        REPO_ROOT / "tasks/episodes/otel-demo/EPI-OTEL-CART-DEADLINE-001",
        REPO_ROOT / "deploy/stage2",
    ]
    files = sorted(path for root in roots for path in root.rglob("*") if path.is_file())
    for path in files:
        digest.update(path.relative_to(REPO_ROOT).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def run(argv: list[str], *, cwd: Path | None = None) -> str:
    completed = subprocess.run(
        argv,
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        timeout=3600,
    )
    if completed.returncode:
        raise RuntimeError(f"command failed: {argv[0]}")
    return completed.stdout


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", default=DEFAULT_REPOSITORY)
    parser.add_argument("--proxy", default=DEFAULT_PROXY)
    parser.add_argument("--host-proxy", default=DEFAULT_HOST_PROXY)
    parser.add_argument("--metadata", type=Path, default=REPO_ROOT / "artifacts/stage2/image.json")
    args = parser.parse_args(argv)
    if not BLADEAI_REPO.is_dir() or not OTEL_SOURCE.is_dir():
        raise RuntimeError("BladeAI or OTel source checkout is missing")
    content_sha = source_digest()
    image = f"{args.repository}:stage2-{content_sha[:12]}"
    with tempfile.TemporaryDirectory(prefix="resbench-stage2-build-") as raw:
        temporary = Path(raw)
        bladeai = temporary / "bladeai"
        bladeai.mkdir()
        chart_context = temporary / "otel-chart"
        chart_context.mkdir()
        archive = subprocess.Popen(
            ["git", "archive", BLADEAI_TAG],
            cwd=BLADEAI_REPO,
            stdout=subprocess.PIPE,
        )
        assert archive.stdout is not None
        extract = subprocess.run(
            ["tar", "-x", "-C", str(bladeai)],
            stdin=archive.stdout,
            check=False,
            timeout=120,
        )
        archive.stdout.close()
        if archive.wait(timeout=30) or extract.returncode:
            raise RuntimeError("could not materialize pinned BladeAI source")
        chart = subprocess.run(
            [
                "helm",
                "pull",
                "opentelemetry-demo",
                "--repo",
                "https://open-telemetry.github.io/opentelemetry-helm-charts",
                "--version",
                OTEL_CHART_VERSION,
                "--destination",
                str(chart_context),
            ],
            check=False,
            timeout=300,
            env={
                **os.environ,
                "HTTP_PROXY": args.host_proxy,
                "HTTPS_PROXY": args.host_proxy,
                "NO_PROXY": DEFAULT_NO_PROXY,
            },
        )
        chart_path = chart_context / f"opentelemetry-demo-{OTEL_CHART_VERSION}.tgz"
        if chart.returncode or not chart_path.is_file():
            raise RuntimeError("could not download pinned OTel Demo chart")
        if hashlib.sha256(chart_path.read_bytes()).hexdigest() != OTEL_CHART_SHA256:
            raise RuntimeError("OTel Demo chart digest does not match the pinned checksum")
        metadata_file = temporary / "build-metadata.json"
        build_argv = [
                "docker",
                "buildx",
                "build",
                "--progress=plain",
                "--platform",
                "linux/amd64",
                "--build-arg",
                f"HTTP_PROXY={args.proxy}",
                "--build-arg",
                f"HTTPS_PROXY={args.proxy}",
                "--build-arg",
                f"NO_PROXY={DEFAULT_NO_PROXY}",
                "--build-context",
                f"bladeai-src={bladeai}",
                "--build-context",
                f"otel-src={OTEL_SOURCE}",
                "--build-context",
                f"otel-chart={chart_context}",
                "--file",
                str(REPO_ROOT / "deploy/stage2/Dockerfile"),
                "--tag",
                image,
                "--metadata-file",
                str(metadata_file),
                "--push",
                str(REPO_ROOT),
            ]
        completed = subprocess.run(
            build_argv,
            check=False,
            timeout=3600,
        )
        if completed.returncode:
            raise RuntimeError("Stage-2 buildx build/push failed")
        metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
    digest = str(metadata.get("containerimage.digest") or "")
    if not digest.startswith("sha256:"):
        raise RuntimeError("buildx did not report a pushed manifest digest")
    output = {
        "schema_version": "stage2-image.v1",
        "image": image,
        "digest": digest,
        "immutable_ref": f"{args.repository}@{digest}",
        "source_sha256": content_sha,
        "bladeai_ref": BLADEAI_TAG,
        "otel_chart": f"opentelemetry-demo-{OTEL_CHART_VERSION}@sha256:{OTEL_CHART_SHA256}",
        "platform": "linux/amd64",
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
