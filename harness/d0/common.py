"""Shared D0 contracts and append-only artifact helpers."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import socket
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


FIXED_PROMPT = "请针对otel-demo下的accounting服务的一个 pod 注入高 cpu 故障，持续 5 分钟，5 分钟后需要自动恢复"
CONFIRMATION_REPLY = "确认执行上述故障意图，不修改目标、范围和参数。"
EXPECTED_EXECUTION_HOST_ID = "1.94.151.57"
AGENTS = ("bladeai", "codex", "claude-code", "deepseek-harness")

_SECRET_FIELD = re.compile(
    r'(?i)("[^"\\]*(?:password|passwd|pwd|api[_-]?key|access[_-]?token|auth[_-]?token|authorization|secret|cleanup[_-]?handle|baseline[_-]?(?:gate[_-]?)?token|mcp[_-]?token|controller[_-]?token[_-]?ref)[^"\\]*"\s*:\s*)"(?:\\.|[^"\\])*"'
)
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)(\b[A-Za-z0-9_]*(?:password|passwd|pwd|api[_-]?key|access[_-]?token|auth[_-]?token|authorization|secret|cleanup[_-]?handle|baseline[_-]?(?:gate[_-]?)?token|mcp[_-]?token)\b\s*[=:]\s*)([^;,\s\"']+)"
)
_BEARER = re.compile(r"(?i)(\bBearer\s+)[A-Za-z0-9._~+/=-]+")
_BASIC = re.compile(r"(?i)(\bBasic\s+)[A-Za-z0-9+/=]+")
_URI_CREDENTIAL = re.compile(r"(https?://[^\s/:@]+:)[^\s/@]+(@)")
_MODEL_KEY = re.compile(
    r"(?<![A-Za-z0-9])(?:sk-ant-|sk-)[A-Za-z0-9_-]{12,}"
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def redact_sensitive_text(value: str) -> str:
    """Redact credential-shaped values while preserving evidence structure."""
    text = _SECRET_FIELD.sub(r'\1"<redacted>"', value)
    text = _SECRET_ASSIGNMENT.sub(r"\1<redacted>", text)
    text = _BEARER.sub(r"\1<redacted>", text)
    text = _BASIC.sub(r"\1<redacted>", text)
    text = _URI_CREDENTIAL.sub(r"\1<redacted>\2", text)
    return _MODEL_KEY.sub("<redacted-model-key>", text)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def append_jsonl(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def host_evidence(expected_host_id: str = EXPECTED_EXECUTION_HOST_ID) -> dict[str, Any]:
    actual = os.environ.get("RESBENCH_D0_EXECUTION_HOST_ID", "")
    return {
        "expected_host_id": expected_host_id,
        "declared_host_id": actual,
        "hostname": socket.gethostname(),
        "platform": os.uname().sysname,
        "pid": os.getpid(),
        "verified": actual == expected_host_id and os.uname().sysname == "Linux",
        "observed_at": utc_now(),
    }


def write_manifest(root: Path) -> Path:
    rows = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        if path.name == "manifest.sha256":
            continue
        rows.append(f"{sha256_file(path)}  {path.relative_to(root).as_posix()}")
    output = root / "manifest.sha256"
    output.write_text("\n".join(rows) + ("\n" if rows else ""), encoding="utf-8")
    return output


def write_summary_csv(path: Path, results: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "agent",
        "status",
        "injection_observed",
        "effect_observed",
        "agent_recovery_requested",
        "recovery_observed",
        "fallback_cleanup_used",
        "fault_duration_seconds",
        "total_duration_seconds",
        "artifact_ref",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for value in results:
            writer.writerow({key: value.get(key, "") for key in fields})
