"""Import reviewed historical traces without executing any Agent or tool."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

import zstandard

from harness.d0.common import redact_sensitive_text
from stage2_service.harness_adapters.deepseek import iter_zstd_jsonl_lines


SOURCES = {
    "claude_L0.stream-json": "stage2-task-496842e3ddb04aac",
    "claude_L1.stream-json": "stage2-task-42f56cf4e46f4da3",
    "claude_L2.stream-json": "stage2-task-2b104dc48bda4dfd",
    "claude_L3.stream-json": "stage2-task-b5229e40fce84aa1",
    "claude_L4.stream-json": "stage2-task-ef6ce446732e48ef",
    "deepseek_L3.session.zstd": "stage2-task-b4b49138d1d84554",
    "codex_L3.jsonl": "stage2-task-e0561967a2bd46e3",
}
SECRET_KEY = re.compile(
    r"password|passwd|api[_-]?key|access[_-]?token|auth[_-]?token|authorization|"
    r"secret|cleanup[_-]?handle|baseline[_-]?(?:gate[_-]?)?token|mcp[_-]?token|controller[_-]?token[_-]?ref",
    re.IGNORECASE,
)
PRIVATE_FIELDS = {"thinking", "reasoning", "chain_of_thought", "analysis"}


def sanitize_value(value: Any) -> Any:
    """Redact nested protocol/JSON-string credentials without losing tool IDs."""
    if isinstance(value, dict):
        return {
            key: "<redacted>" if SECRET_KEY.search(key) and isinstance(item, str)
            else sanitize_value(item)
            for key, item in value.items() if key not in PRIVATE_FIELDS
        }
    if isinstance(value, list):
        return [sanitize_value(item) for item in value
                if not (isinstance(item, dict) and item.get("type") in PRIVATE_FIELDS)]
    if isinstance(value, str):
        try:
            nested = json.loads(value)
        except (ValueError, TypeError):
            return redact_sensitive_text(value)
        if isinstance(nested, (dict, list)):
            return json.dumps(sanitize_value(nested), ensure_ascii=False, separators=(",", ":"))
        return redact_sensitive_text(value)
    return value


def import_fixtures(source_dir: Path, output_dir: Path) -> dict[str, Any]:
    """Create new fixture files only; never overwrite a captured reference."""
    source_dir, output_dir = source_dir.resolve(), output_dir.resolve()
    if source_dir == output_dir:
        raise ValueError("private originals and sanitized fixtures must be separate")
    if any((output_dir / name).exists() for name in SOURCES):
        raise FileExistsError("golden fixtures already exist; review rather than overwrite")
    output_dir.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {"source_kind": "historical_native_trace", "fixtures": []}
    for name, task_id in SOURCES.items():
        source = source_dir / name
        if source.is_symlink() or not source.is_file():
            raise ValueError(f"missing regular source file: {name}")
        lines = list(iter_zstd_jsonl_lines(source)) if name.endswith(".zstd") else source.read_text(encoding="utf-8").splitlines()
        sanitized = []
        for line in lines:
            try:
                value = json.loads(line)
            except ValueError:
                sanitized.append(redact_sensitive_text(line))
            else:
                sanitized.append(json.dumps(sanitize_value(value), ensure_ascii=False, separators=(",", ":")))
        encoded = ("\n".join(sanitized) + "\n").encode("utf-8")
        if name.endswith(".zstd"):
            encoded = zstandard.ZstdCompressor().compress(encoded)
        with (output_dir / name).open("xb") as destination:
            destination.write(encoded)
        report["fixtures"].append({
            "name": name, "task_id": task_id, "native_rows": len(lines),
            "fixture_rows": len(sanitized), "bytes": len(encoded),
            "redacted": True, "recompressed": name.endswith(".zstd"),
            "new_trial_executed": False,
        })
    with (output_dir / "provenance.json").open("x", encoding="utf-8") as destination:
        json.dump(report, destination, ensure_ascii=False, indent=2)
        destination.write("\n")
    return report


def main() -> None:
    """Run the offline, source-preserving import from an explicitly chosen folder."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(import_fixtures(args.source_dir, args.output_dir), ensure_ascii=False))


if __name__ == "__main__":
    main()
