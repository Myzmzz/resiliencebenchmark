"""Controller-to-telemetry-MCP rule channel backed by a private runtime directory."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from uuid import uuid4

from .telemetry_interceptor import (
    TelemetryDisturbanceRuleEngine,
    telemetry_rule_id,
)


class FileTelemetryRuleClient:
    """Controller-side typed rule publisher; Agent processes cannot call it."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.rules_dir = self.root / "rules"
        self.events_dir = self.root / "events"
        self.rules_dir.mkdir(parents=True, exist_ok=True)
        self.events_dir.mkdir(parents=True, exist_ok=True)

    def register_rule(
        self,
        *,
        run_id: str,
        level_id: str,
        disturbance_id: str,
        rule: Mapping[str, Any],
    ) -> str:
        # Reuse the in-process validator before publishing cross-process state.
        validator = TelemetryDisturbanceRuleEngine()
        rule_id = validator.register_rule(
            run_id=run_id,
            level_id=level_id,
            disturbance_id=disturbance_id,
            rule=rule,
        )
        payload = {
            "schema_version": "telemetry-disturbance-rule.v1",
            "rule_id": rule_id,
            "run_id": run_id,
            "level_id": level_id,
            "disturbance_id": disturbance_id,
            "rule": dict(rule),
        }
        path = self.rules_dir / f"{rule_id}.json"
        content = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        if path.exists():
            if path.read_text(encoding="utf-8") != content:
                raise RuntimeError("telemetry rule identity collision with different content")
            return rule_id
        _atomic_write(path, content, 0o640)
        return rule_id

    def remove_rule(self, rule_id: str) -> bool:
        _safe_rule_id(rule_id)
        path = self.rules_dir / f"{rule_id}.json"
        if not path.exists():
            return False
        path.unlink()
        return True

    def events(self) -> list[dict[str, Any]]:
        output = []
        for path in sorted(self.events_dir.glob("*.jsonl")):
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict):
                    output.append(value)
        return output


class FileBackedTelemetryDisturbanceHook:
    """Telemetry-service hook that hot-loads Controller-published rules."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.rules_dir = self.root / "rules"
        self.events_dir = self.root / "events"
        self.rules_dir.mkdir(parents=True, exist_ok=True)
        self.events_dir.mkdir(parents=True, exist_ok=True)
        self.engine = TelemetryDisturbanceRuleEngine()
        self.loaded: dict[str, str] = {}
        self.event_offset = 0

    async def before_tool(self, tool: str) -> None:
        self._sync_rules()
        try:
            await self.engine.before_tool(tool)
        finally:
            self._flush_events()

    def after_tool(self, tool: str, response: Mapping[str, Any]) -> dict[str, Any]:
        self._sync_rules()
        try:
            return self.engine.after_tool(tool, response)
        finally:
            self._flush_events()

    def _sync_rules(self) -> None:
        present = set()
        for path in sorted(self.rules_dir.glob("telemetry-rule-*.json")):
            payload = json.loads(path.read_text(encoding="utf-8"))
            rule_id = str(payload.get("rule_id") or "")
            _safe_rule_id(rule_id)
            expected = telemetry_rule_id(
                str(payload["run_id"]),
                str(payload["level_id"]),
                str(payload["disturbance_id"]),
            )
            if expected != rule_id:
                raise RuntimeError("telemetry rule file identity mismatch")
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            present.add(rule_id)
            if rule_id in self.loaded:
                if self.loaded[rule_id] != digest:
                    raise RuntimeError("active telemetry rule changed after registration")
                continue
            registered = self.engine.register_rule(
                run_id=str(payload["run_id"]),
                level_id=str(payload["level_id"]),
                disturbance_id=str(payload["disturbance_id"]),
                rule=payload["rule"],
            )
            if registered != rule_id:
                raise RuntimeError("telemetry rule engine returned a mismatched identity")
            self.loaded[rule_id] = digest
        for removed in set(self.loaded) - present:
            self.engine.remove_rule(removed)
            del self.loaded[removed]

    def _flush_events(self) -> None:
        events = self.engine.events()
        for event in events[self.event_offset :]:
            rule_id = str(event["rule_id"])
            _safe_rule_id(rule_id)
            path = self.events_dir / f"{rule_id}.jsonl"
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
            os.chmod(path, 0o640)
        self.event_offset = len(events)


def _safe_rule_id(rule_id: str) -> None:
    if not rule_id.startswith("telemetry-rule-") or len(rule_id) != 31:
        raise ValueError("invalid telemetry rule id")


def _atomic_write(path: Path, payload: str, mode: int) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, mode)
    finally:
        if temporary.exists():
            temporary.unlink()
