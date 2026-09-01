#!/usr/bin/env python3
"""Audit D0 artifact completeness without changing Agent verdicts."""

from __future__ import annotations

import argparse
import hashlib
import io
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

try:
    import zstandard
except ModuleNotFoundError:  # Keep the standalone system-Python audit path.
    zstandard = None

REPO_ROOT = Path(__file__).resolve().parents[1]
COMMON_SPEC = importlib.util.spec_from_file_location(
    "_d0_common_standalone", REPO_ROOT / "harness/d0/common.py"
)
if COMMON_SPEC is None or COMMON_SPEC.loader is None:
    raise RuntimeError("cannot load D0 common helpers")
COMMON = importlib.util.module_from_spec(COMMON_SPEC)
COMMON_SPEC.loader.exec_module(COMMON)
redact_sensitive_text = COMMON.redact_sensitive_text

EXPECTED_AGENTS = ("bladeai", "codex", "claude-code", "deepseek-harness")
EXPECTED_PROMPT = "请针对otel-demo下的accounting服务的一个 pod 注入高 cpu 故障，持续 5 分钟，5 分钟后需要自动恢复"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_zstd_text(path: Path) -> str:
    if zstandard is not None:
        with zstandard.ZstdDecompressor().stream_reader(
            io.BytesIO(path.read_bytes())
        ) as reader:
            return reader.read().decode("utf-8")
    completed = subprocess.run(
        ["zstd", "-dc", str(path)],
        check=False,
        capture_output=True,
        timeout=30,
    )
    if completed.returncode:
        raise RuntimeError("zstd decompression failed")
    return completed.stdout.decode("utf-8")


def audit(root: Path) -> dict:
    issues = []
    private_directories = [
        path.relative_to(root).as_posix()
        for path in root.rglob(".controller-private")
        if path.is_dir()
    ]
    if private_directories:
        issues.append(
            "controller-private capability directories remain in evidence: "
            + ", ".join(private_directories)
        )
    campaign = json.loads((root / "campaign.json").read_text(encoding="utf-8"))
    if tuple(campaign.get("agents", [])) != EXPECTED_AGENTS:
        issues.append("campaign does not contain the four required Agents in order")
    if campaign.get("host", {}).get("verified") is not True or campaign.get("host", {}).get("declared_host_id") != "1.94.151.57":
        issues.append("remote execution-host evidence is missing")
    prompt = (root / "prompt.txt").read_text(encoding="utf-8").rstrip("\n")
    if prompt != EXPECTED_PROMPT:
        issues.append("fixed prompt changed")
    if (root / "prompt.sha256").read_text().strip() != hashlib.sha256(EXPECTED_PROMPT.encode()).hexdigest():
        issues.append("prompt digest mismatch")
    required_trial = (
        "trial.json",
        "result.json",
        "recovery.json",
        "all-events.jsonl",
        "controller-commands.jsonl",
        "controller-events.jsonl",
        "oracle-samples.jsonl",
    )
    results = {value.get("agent"): value for value in campaign.get("results", [])}
    for agent in EXPECTED_AGENTS:
        for name in required_trial:
            path = root / agent / name
            if not path.is_file() or path.stat().st_size == 0:
                issues.append(f"{agent}/{name} is missing or empty")
        result = results.get(agent)
        if not isinstance(result, dict) or not result.get("verdict_source"):
            issues.append(f"{agent} has no evidence-derived verdict")
        response_candidates = (
            root / agent / "agent-responses.jsonl",
            root / agent / "stdout.txt",
        )
        if not any(path.is_file() and path.stat().st_size > 0 for path in response_candidates):
            issues.append(f"{agent} has no retained Agent response")
        if result and result.get("native_trace_capture_complete") is not True:
            issues.append(f"{agent} has no complete native trace capture")
    required_visual = (
        "index.html",
        "comparison.svg",
        "summary.csv",
        "summary.json",
        "command-tool-audit.csv",
        "command-tool-audit.json",
        "audit-report.md",
    )
    for name in required_visual:
        if not (root / "visualization" / name).is_file():
            issues.append(f"visualization/{name} is missing")
    sensitive_files = []
    text_suffixes = {".json", ".jsonl", ".txt", ".log", ".md", ".csv", ".html", ".svg", ".yaml", ".yml", ".toml"}
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        if path.name == "manifest.sha256":
            continue
        try:
            if path.suffix == ".zstd":
                text = read_zstd_text(path)
            elif path.suffix in text_suffixes:
                text = path.read_text(encoding="utf-8")
            else:
                continue
        except (OSError, RuntimeError, UnicodeDecodeError):
            issues.append(f"cannot inspect artifact for sensitive values: {path.relative_to(root)}")
            continue
        if redact_sensitive_text(text) != text:
            sensitive_files.append(path.relative_to(root).as_posix())
    if sensitive_files:
        issues.append(
            "credential-shaped values remain in: " + ", ".join(sensitive_files)
        )
    manifest = root / "manifest.sha256"
    checked = 0
    for line in manifest.read_text(encoding="utf-8").splitlines():
        expected, relative = line.split("  ", 1)
        path = root / relative
        checked += 1
        if not path.is_file() or sha256(path) != expected:
            issues.append(f"manifest mismatch: {relative}")
    return {
        "schema_version": "d0-artifact-audit.v1",
        "campaign_id": campaign.get("campaign_id"),
        "campaign_status": campaign.get("status"),
        "complete": not issues,
        "manifest_entries_checked": checked,
        "issues": issues,
        "agent_statuses": {agent: results.get(agent, {}).get("status") for agent in EXPECTED_AGENTS},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("campaign_dir", type=Path)
    args = parser.parse_args()
    report = audit(args.campaign_dir.expanduser().resolve())
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["complete"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
