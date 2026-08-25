"""Load and validate the twelve active semantic defect templates."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .contracts import DClass

ACTIVE_TEMPLATE_IDS = (
    "RD-01",
    "RD-02",
    "RD-05",
    "RD-06",
    "RD-07",
    "RD-08",
    "RD-09",
    "RD-10",
    "RD-11",
    "RD-12",
    "RD-13",
    "RD-14",
)


class RegistryModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DClassPolicy(RegistryModel):
    fixed: DClass | None
    allowed: list[DClass] = Field(min_length=1, max_length=6)

    @model_validator(mode="after")
    def validate_fixed(self) -> DClassPolicy:
        if self.fixed is not None and (
            len(self.allowed) != 1 or self.allowed[0] is not self.fixed
        ):
            raise ValueError("fixed D class must be the only allowed value")
        return self


class SemanticTemplate(RegistryModel):
    template_id: str = Field(pattern=r"^RD-(?:0[1-9]|1[0-4])$")
    defect_name: str
    domain: str
    prompt_file: str
    d_class: DClassPolicy
    mechanism: str
    graph_focus: list[str] = Field(min_length=1, max_length=20)
    kubernetes_kinds: list[str] = Field(default_factory=list, max_length=20)
    fault_types: list[str] = Field(min_length=1, max_length=12)
    fault_target_kinds: list[str] = Field(min_length=1, max_length=12)


class SemanticTemplateRegistry(RegistryModel):
    schema_version: str
    registry_version: str
    description: str
    templates: list[SemanticTemplate] = Field(min_length=12, max_length=12)

    @model_validator(mode="after")
    def validate_active_set(self) -> SemanticTemplateRegistry:
        observed = tuple(item.template_id for item in self.templates)
        if set(observed) != set(ACTIVE_TEMPLATE_IDS) or len(observed) != len(set(observed)):
            raise ValueError("template registry must contain exactly the twelve active IDs")
        return self

    def by_id(self, template_id: str) -> SemanticTemplate:
        return next(item for item in self.templates if item.template_id == template_id)

    def index_for_prompt(self) -> list[dict[str, Any]]:
        return [
            {
                "template_id": item.template_id,
                "defect_name": item.defect_name,
                "domain": item.domain,
                "mechanism": item.mechanism,
                "graph_focus": item.graph_focus,
                "kubernetes_kinds": item.kubernetes_kinds,
            }
            for item in self.templates
        ]


def load_template_registry(path: Path) -> SemanticTemplateRegistry:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError("semantic template registry must be a YAML object")
    return SemanticTemplateRegistry.model_validate(value)
