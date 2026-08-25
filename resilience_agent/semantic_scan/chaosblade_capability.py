"""Versioned ChaosBlade capability manifest used by Episode generation."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class CapabilityModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ChaosBladeCapability(CapabilityModel):
    fault_type: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,63}$")
    actuator: Literal["ChaosBlade"] = "ChaosBlade"
    status: Literal["verified", "unsupported", "unverified"]
    command_kind: str = Field(min_length=3, max_length=160)
    blade_version: str = Field(min_length=1, max_length=120)
    verification_source: str = Field(min_length=3, max_length=300)
    verified_at: datetime | None = None
    reason: str = Field(default="", max_length=600)

    @model_validator(mode="after")
    def validate_verified_timestamp(self) -> ChaosBladeCapability:
        if self.status == "verified" and self.verified_at is None:
            raise ValueError("verified ChaosBlade capabilities require verified_at")
        return self


class ChaosBladeCapabilityManifest(CapabilityModel):
    schema_version: Literal["chaosblade-capability-manifest.v1"] = (
        "chaosblade-capability-manifest.v1"
    )
    registry_version: str = Field(min_length=3, max_length=120)
    generated_at: datetime
    namespace: str | None = Field(default=None, max_length=253)
    capabilities: list[ChaosBladeCapability] = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def validate_unique_fault_types(self) -> ChaosBladeCapabilityManifest:
        keys = [item.fault_type for item in self.capabilities]
        if len(keys) != len(set(keys)):
            raise ValueError("ChaosBlade capability manifest has duplicate fault types")
        return self

    def capability_for(self, fault_type: str) -> ChaosBladeCapability | None:
        for item in self.capabilities:
            if item.fault_type == fault_type:
                return item
        return None

    def verified_fault_types(self) -> set[str]:
        return {
            item.fault_type for item in self.capabilities if item.status == "verified"
        }


def load_chaosblade_capabilities(path: Path) -> ChaosBladeCapabilityManifest:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise TypeError("ChaosBlade capability manifest must be a YAML object")
    return ChaosBladeCapabilityManifest.model_validate(raw)
