"""Typed, deterministic contracts for controller-owned disturbances."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import hashlib
from pathlib import Path
from typing import Any, Mapping

import yaml


class DisturbancePhase(str, Enum):
    EXECUTION = "execution"
    OBSERVATION = "observation"
    VERIFICATION = "verification"
    ABORT = "abort"


class DisturbanceType(str, Enum):
    TARGET_DRIFT = "target_drift"
    RESOURCE_QUOTA_REDUCTION = "resource_quota_reduction"
    TELEMETRY_INSTABILITY = "telemetry_instability"
    METRIC_DATA_GAP = "metric_data_gap"
    FAULT_EFFECT_DEVIATION = "fault_effect_deviation"
    BASELINE_DRIFT = "baseline_drift"
    SAFETY_THRESHOLD_PRESSURE = "safety_threshold_pressure"
    CLEANUP_DELAY = "cleanup_delay"


class TriggerMode(str, Enum):
    LIFECYCLE_EVENT = "lifecycle_event"
    TOOL_CALL_SEQUENCE = "tool_call_sequence"
    TIME_OFFSET = "time_offset"


@dataclass(frozen=True)
class TriggerSpec:
    mode: TriggerMode
    phase: DisturbancePhase
    event: str | None = None
    tool: str | None = None
    occurrence: int | None = None
    offset_seconds: float | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "TriggerSpec":
        trigger = cls(
            mode=TriggerMode(str(value["mode"])),
            phase=DisturbancePhase(str(value["phase"])),
            event=_optional_text(value.get("event")),
            tool=_optional_text(value.get("tool")),
            occurrence=_optional_int(value.get("occurrence")),
            offset_seconds=_optional_float(value.get("offset_seconds")),
        )
        trigger.validate()
        return trigger

    def validate(self) -> None:
        if self.mode is TriggerMode.LIFECYCLE_EVENT and not self.event:
            raise ValueError("lifecycle_event triggers require event")
        if self.mode is TriggerMode.TOOL_CALL_SEQUENCE:
            if not self.tool or self.occurrence is None or self.occurrence < 1:
                raise ValueError("tool_call_sequence triggers require tool and occurrence >= 1")
        if self.mode is TriggerMode.TIME_OFFSET:
            if self.offset_seconds is None or self.offset_seconds < 0:
                raise ValueError("time_offset triggers require offset_seconds >= 0")

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"mode": self.mode.value, "phase": self.phase.value}
        for name in ("event", "tool", "occurrence", "offset_seconds"):
            value = getattr(self, name)
            if value is not None:
                result[name] = value
        return result


@dataclass(frozen=True)
class DisturbanceDefinition:
    type: DisturbanceType
    phase: DisturbancePhase
    description: str
    backend: str
    trigger: TriggerSpec
    action: Mapping[str, Any]
    parameters: Mapping[str, Any]
    expected_behaviors: tuple[str, ...]
    verification: tuple[str, ...]
    evidence_source: str = "controller_record"
    reproducible: bool = True

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "DisturbanceDefinition":
        definition = cls(
            type=DisturbanceType(str(value["type"])),
            phase=DisturbancePhase(str(value["phase"])),
            description=str(value["description"]),
            backend=str(value["backend"]),
            trigger=TriggerSpec.from_mapping(_mapping(value["trigger"])),
            action=dict(_mapping(value["action"])),
            parameters=dict(_mapping(value.get("parameters", {}))),
            expected_behaviors=_strings(value.get("expected_behaviors", [])),
            verification=_strings(value.get("verification", [])),
            evidence_source=str(value.get("evidence_source", "controller_record")),
            reproducible=bool(value.get("reproducible", True)),
        )
        definition.validate()
        return definition

    def validate(self) -> None:
        if self.phase is not self.trigger.phase:
            raise ValueError(f"{self.type.value}: definition phase and trigger phase differ")
        if self.evidence_source != "controller_record":
            raise ValueError(f"{self.type.value}: evidence_source must be controller_record")
        if not self.reproducible:
            raise ValueError(f"{self.type.value}: disturbances must be reproducible")
        if not self.expected_behaviors or not self.verification:
            raise ValueError(f"{self.type.value}: expected_behaviors and verification are required")
        if not self.backend or not self.action:
            raise ValueError(f"{self.type.value}: backend and action are required")

    def instantiate(
        self,
        *,
        disturbance_id: str,
        replay_seed: int,
        parameters: Mapping[str, Any] | None = None,
        trigger: TriggerSpec | None = None,
    ) -> "DisturbanceSpec":
        merged = dict(self.parameters)
        merged.update(parameters or {})
        return DisturbanceSpec(
            disturbance_id=disturbance_id,
            type=self.type,
            phase=self.phase,
            backend=self.backend,
            trigger=trigger or self.trigger,
            action=dict(self.action),
            parameters=merged,
            expected_behaviors=self.expected_behaviors,
            verification=self.verification,
            replay_seed=replay_seed,
        )


@dataclass(frozen=True)
class DisturbanceSpec:
    disturbance_id: str
    type: DisturbanceType
    phase: DisturbancePhase
    backend: str
    trigger: TriggerSpec
    action: Mapping[str, Any]
    parameters: Mapping[str, Any]
    expected_behaviors: tuple[str, ...]
    verification: tuple[str, ...]
    replay_seed: int | None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "DisturbanceSpec":
        spec = cls(
            disturbance_id=str(value["disturbance_id"]),
            type=DisturbanceType(str(value["type"])),
            phase=DisturbancePhase(str(value["phase"])),
            backend=str(value["backend"]),
            trigger=TriggerSpec.from_mapping(_mapping(value["trigger"])),
            action=dict(_mapping(value["action"])),
            parameters=dict(_mapping(value.get("parameters", {}))),
            expected_behaviors=_strings(value.get("expected_behaviors", [])),
            verification=_strings(value.get("verification", [])),
            replay_seed=(int(value["replay_seed"]) if value.get("replay_seed") is not None else None),
        )
        if not spec.disturbance_id or spec.phase is not spec.trigger.phase:
            raise ValueError("disturbance spec requires an id and a trigger in the same phase")
        if spec.replay_seed is not None and spec.replay_seed < 0:
            raise ValueError("replay_seed must be non-negative")
        return spec

    def as_dict(self) -> dict[str, Any]:
        return {
            "disturbance_id": self.disturbance_id,
            "type": self.type.value,
            "phase": self.phase.value,
            "backend": self.backend,
            "trigger": self.trigger.as_dict(),
            "action": dict(self.action),
            "parameters": dict(self.parameters),
            "expected_behaviors": list(self.expected_behaviors),
            "verification": list(self.verification),
            "replay_seed": self.replay_seed,
        }


@dataclass(frozen=True)
class LifecycleEvent:
    run_id: str
    level_id: str
    phase: DisturbancePhase
    kind: str
    occurred_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    tool: str | None = None
    elapsed_seconds: float | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "run_id": self.run_id,
            "level_id": self.level_id,
            "phase": self.phase.value,
            "kind": self.kind,
            "occurred_at": _utc_iso(self.occurred_at),
            "payload": dict(self.payload),
        }
        if self.tool:
            result["tool"] = self.tool
        if self.elapsed_seconds is not None:
            result["elapsed_seconds"] = self.elapsed_seconds
        return result


def derive_replay_seed(*parts: str) -> int:
    """Return a stable 63-bit seed independent of Python hash randomization."""

    if not parts or any(not str(part) for part in parts):
        raise ValueError("derive_replay_seed requires non-empty parts")
    digest = hashlib.sha256("\x1f".join(str(part) for part in parts).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") & ((1 << 63) - 1)


def load_disturbance_library(path: Path | None = None) -> dict[DisturbanceType, DisturbanceDefinition]:
    library_path = path or Path(__file__).with_name("library.yaml")
    raw = yaml.safe_load(library_path.read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping) or raw.get("schema_version") != "disturbance-library.v1":
        raise ValueError("disturbance library must use schema_version disturbance-library.v1")
    entries = raw.get("disturbances")
    if not isinstance(entries, list):
        raise ValueError("disturbance library must contain a disturbances list")
    result: dict[DisturbanceType, DisturbanceDefinition] = {}
    for item in entries:
        definition = DisturbanceDefinition.from_mapping(_mapping(item))
        if definition.type in result:
            raise ValueError(f"duplicate disturbance type: {definition.type.value}")
        result[definition.type] = definition
    missing = set(DisturbanceType) - set(result)
    if missing:
        raise ValueError("disturbance library is incomplete: " + ", ".join(sorted(x.value for x in missing)))
    return result


def _mapping(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("expected a mapping")
    return value


def _strings(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise ValueError("expected a list of non-empty strings")
    return tuple(value)


def _optional_text(value: Any) -> str | None:
    return None if value is None else str(value)


def _optional_int(value: Any) -> int | None:
    return None if value is None else int(value)


def _optional_float(value: Any) -> float | None:
    return None if value is None else float(value)


def _utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
