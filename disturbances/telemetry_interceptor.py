"""In-process deterministic rules for a Telemetry MCP service or proxy."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import threading
from typing import Any, Mapping


class TelemetryInjectedFailure(RuntimeError):
    """A deliberate tool failure that an HTTP proxy may map to 503/timeout."""

    def __init__(self, mode: str, rule_id: str) -> None:
        super().__init__(f"controller-injected telemetry {mode} ({rule_id})")
        self.mode = mode
        self.rule_id = rule_id
        self.http_status = 503 if mode == "http_503" else None


@dataclass
class _ActiveRule:
    rule_id: str
    run_id: str
    level_id: str
    disturbance_id: str
    rule: dict[str, Any]
    call_count: int = 0


class TelemetryDisturbanceRuleEngine:
    """Thread-safe rule registry implementing failure and data-gap behavior."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._rules: dict[str, _ActiveRule] = {}
        self._events: list[dict[str, Any]] = []

    def register_rule(
        self,
        *,
        run_id: str,
        level_id: str,
        disturbance_id: str,
        rule: Mapping[str, Any],
    ) -> str:
        normalized = _validate_rule(rule)
        raw = f"{run_id}\x1f{level_id}\x1f{disturbance_id}".encode("utf-8")
        rule_id = "telemetry-rule-" + hashlib.sha256(raw).hexdigest()[:16]
        active = _ActiveRule(
            rule_id=rule_id,
            run_id=run_id,
            level_id=level_id,
            disturbance_id=disturbance_id,
            rule=normalized,
        )
        with self._lock:
            if rule_id in self._rules:
                raise ValueError(f"telemetry rule already exists: {rule_id}")
            self._rules[rule_id] = active
            self._events.append(_event(active, "registered", {"rule": normalized}))
        return rule_id

    def remove_rule(self, rule_id: str) -> bool:
        with self._lock:
            active = self._rules.pop(rule_id, None)
            if active is None:
                return False
            self._events.append(_event(active, "removed", {}))
            return True

    async def before_tool(self, tool: str) -> None:
        actions: list[tuple[_ActiveRule, str]] = []
        with self._lock:
            for active in self._rules.values():
                if active.rule.get("tool") != tool:
                    continue
                if active.rule["kind"] != "telemetry_failure_schedule":
                    continue
                active.call_count += 1
                slot = active.call_count
                if slot not in active.rule["slots"]:
                    continue
                modes = active.rule["response_modes"]
                mode = modes[(active.rule["slots"].index(slot)) % len(modes)]
                self._events.append(
                    _event(active, "matched", {"tool": tool, "call_slot": slot, "mode": mode})
                )
                actions.append((active, mode))
        if not actions:
            return
        active, mode = actions[0]
        if mode == "timeout":
            await asyncio.sleep(active.rule["timeout_milliseconds"] / 1000.0)
        raise TelemetryInjectedFailure(mode, active.rule_id)

    def after_tool(self, tool: str, response: Mapping[str, Any]) -> dict[str, Any]:
        result = deepcopy(dict(response))
        with self._lock:
            rules = [
                active
                for active in self._rules.values()
                if active.rule.get("tool") == tool and active.rule["kind"] == "metric_data_gap"
            ]
            for active in rules:
                removed = _remove_metric_slots(result, active.rule["missing_slots"])
                self._events.append(
                    _event(
                        active,
                        "response_transformed",
                        {"tool": tool, "removed_points": removed},
                    )
                )
        return result

    def events(self) -> list[dict[str, Any]]:
        with self._lock:
            return deepcopy(self._events)


def _validate_rule(value: Mapping[str, Any]) -> dict[str, Any]:
    kind = str(value.get("kind") or "")
    tool = str(value.get("tool") or "telemetry_prom_metric_range")
    if kind == "telemetry_failure_schedule":
        slots = _positive_unique_ints(value.get("slots"), "slots")
        modes = value.get("response_modes")
        if not isinstance(modes, list) or not modes or any(item not in {"http_503", "timeout"} for item in modes):
            raise ValueError("failure response_modes must contain http_503 and/or timeout")
        timeout = int(value.get("timeout_milliseconds", 1500))
        if timeout < 1 or timeout > 5000:
            raise ValueError("timeout_milliseconds must be between 1 and 5000")
        return {
            "kind": kind,
            "tool": tool,
            "slots": slots,
            "response_modes": list(modes),
            "timeout_milliseconds": timeout,
        }
    if kind == "metric_data_gap":
        return {
            "kind": kind,
            "tool": tool,
            "missing_slots": _positive_unique_ints(value.get("missing_slots"), "missing_slots"),
        }
    raise ValueError("unsupported telemetry disturbance rule kind")


def _positive_unique_ints(value: Any, name: str) -> list[int]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{name} must be a non-empty list")
    result = sorted({int(item) for item in value})
    if result[0] < 1:
        raise ValueError(f"{name} values must be positive")
    return result


def _remove_metric_slots(response: dict[str, Any], missing_slots: list[int]) -> int:
    removed = 0
    for series in response.get("result", []):
        if not isinstance(series, dict) or not isinstance(series.get("values"), list):
            continue
        values = series["values"]
        kept = [value for index, value in enumerate(values, start=1) if index not in missing_slots]
        removed += len(values) - len(kept)
        series["values"] = kept
    return removed


def _event(active: _ActiveRule, status: str, detail: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "rule_id": active.rule_id,
        "run_id": active.run_id,
        "level_id": active.level_id,
        "disturbance_id": active.disturbance_id,
        "status": status,
        "detail": dict(detail),
    }
