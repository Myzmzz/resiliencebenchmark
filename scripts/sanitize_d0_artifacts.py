#!/usr/bin/env python3
"""Remove credential-shaped values from a D0 artifact tree and reseal it."""

from __future__ import annotations

import argparse
import io
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

try:
    import zstandard
except ModuleNotFoundError:
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
utc_now = COMMON.utc_now
write_json = COMMON.write_json
write_manifest = COMMON.write_manifest


TEXT_SUFFIXES = {
    ".json",
    ".jsonl",
    ".txt",
    ".log",
    ".md",
    ".csv",
    ".html",
    ".svg",
    ".yaml",
    ".yml",
    ".toml",
}


def decompress_zstd(path: Path) -> bytes:
    if zstandard is not None:
        with zstandard.ZstdDecompressor().stream_reader(
            io.BytesIO(path.read_bytes())
        ) as reader:
            return reader.read()
    completed = subprocess.run(
        ["zstd", "-dc", str(path)],
        check=False,
        capture_output=True,
        timeout=30,
    )
    if completed.returncode:
        raise RuntimeError("zstd decompression failed")
    return completed.stdout


def compress_zstd(value: bytes) -> bytes:
    if zstandard is not None:
        return zstandard.ZstdCompressor(level=10).compress(value)
    completed = subprocess.run(
        ["zstd", "-q", "-10", "-c"],
        input=value,
        check=False,
        capture_output=True,
        timeout=30,
    )
    if completed.returncode:
        raise RuntimeError("zstd compression failed")
    return completed.stdout


def sanitize(root: Path) -> dict:
    report_path = root / "redaction-report.json"
    previous: dict = {}
    if report_path.is_file():
        try:
            previous = json.loads(report_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            previous = {}
    changed = []
    scanned = 0
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix()
        if path.name in {"manifest.sha256", "redaction-report.json"}:
            continue
        if path.suffix == ".zstd":
            raw = decompress_zstd(path)
            text = raw.decode("utf-8", errors="strict")
            sanitized = redact_sensitive_text(text)
            scanned += 1
            if sanitized != text:
                path.write_bytes(compress_zstd(sanitized.encode()))
                changed.append(relative)
            continue
        if path.suffix not in TEXT_SUFFIXES:
            continue
        text = path.read_text(encoding="utf-8")
        sanitized = redact_sensitive_text(text)
        scanned += 1
        if sanitized != text:
            path.write_text(sanitized, encoding="utf-8")
            changed.append(relative)
    removed_private_directories = []
    removed_private_file_count = 0
    for private in sorted(root.rglob(".controller-private")):
        if not private.is_dir():
            continue
        removed_private_file_count += sum(
            1 for item in private.rglob("*") if item.is_file()
        )
        removed_private_directories.append(private.relative_to(root).as_posix())
        shutil.rmtree(private)
    sanitized_at = utc_now()
    cumulative = sorted(set(previous.get("changed_files", [])) | set(changed))
    runs = list(previous.get("runs", []))
    if not runs and previous.get("sanitized_at"):
        runs.append(
            {
                "sanitized_at": previous.get("sanitized_at"),
                "changed_files": previous.get("changed_files", []),
            }
        )
    runs.append({"sanitized_at": sanitized_at, "changed_files": changed})
    report = {
        "schema_version": "d0-artifact-redaction.v1",
        "sanitized_at": sanitized_at,
        "root": root.name,
        "files_scanned": scanned,
        "changed_file_count": len(cumulative),
        "changed_files": cumulative,
        "last_run_changed_file_count": len(changed),
        "runs": runs,
        "removed_private_directories": sorted(
            set(previous.get("removed_private_directories", []))
            | set(removed_private_directories)
        ),
        "removed_private_file_count": int(
            previous.get("removed_private_file_count", 0)
        )
        + removed_private_file_count,
        "policy": "credential-shaped values replaced; experiment facts retained",
    }
    write_json(root / "redaction-report.json", report)
    write_manifest(root)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("campaign_dir", type=Path)
    args = parser.parse_args()
    root = args.campaign_dir.expanduser().resolve()
    if not (root / "campaign.json").is_file():
        raise SystemExit("campaign.json is required")
    report = sanitize(root)
    print(
        f"sanitized={report['changed_file_count']} scanned={report['files_scanned']} "
        f"campaign={report['root']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
