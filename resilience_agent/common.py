"""Shared, side-effect-light helpers for our resilience analysis capabilities."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable

import yaml
from jsonschema import Draft202012Validator


SENSITIVE_KEY = re.compile(r"(?i)(?:api[_-]?key|token|password|secret|credential|private[_-]?key)")
SECRET_TOKEN = re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b")
SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|token|password|secret)\b([ \t]*[:=][ \t]*)(['\"]?)[^'\"\s#]+\3"
)


def load_document(path: Path) -> Any:
    """Load JSON or YAML from an explicit path."""
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        return json.loads(text)
    return yaml.safe_load(text)


def write_json(path: Path, value: Any) -> None:
    """Write a stable UTF-8 JSON artifact."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )


def validate_document(value: Any, schema_path: Path) -> None:
    """Raise a readable error when an internal artifact violates its contract."""
    schema = load_document(schema_path)
    errors = sorted(Draft202012Validator(schema).iter_errors(value), key=lambda e: list(e.path))
    if not errors:
        return
    formatted = []
    for error in errors[:10]:
        location = ".".join(str(part) for part in error.absolute_path) or "<root>"
        formatted.append(f"{location}: {error.message}")
    raise ValueError(f"schema validation failed for {schema_path}: " + "; ".join(formatted))


def stable_id(prefix: str, parts: Iterable[str], length: int = 12) -> str:
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:length]
    return f"{prefix}-{digest}"


def unique_strings(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def sanitize_context(value: Any) -> Any:
    """Redact secret-like context fields before tools, prompts, or artifacts see them."""
    if isinstance(value, dict):
        return {
            str(key): "[REDACTED]" if SENSITIVE_KEY.search(str(key)) else sanitize_context(child)
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [sanitize_context(item) for item in value]
    return value


def redact_sensitive_text(text: str) -> str:
    text = SECRET_TOKEN.sub("[REDACTED_SECRET]", text)
    return SECRET_ASSIGNMENT.sub(
        lambda match: f"{match.group(1)}{match.group(2)}[REDACTED_SECRET]",
        text,
    )
