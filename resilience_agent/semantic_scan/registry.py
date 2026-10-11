"""Load and validate the ten active ChaosBlade-anchored defect templates."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

from .contracts import DClass

ACTIVE_TEMPLATE_IDS = (
    "RD-01",
    "RD-02",
    "RD-03",
    "RD-04",
    "RD-05",
    "RD-06",
    "RD-07",
    "RD-08",
    "RD-09",
    "RD-10",
)

CHAOSBLADE_ATOMIC_FAULTS = {
    "network-delay": ("pod", "network", "delay", "k8s pod-network delay", "persistent"),
    "network-loss": ("pod", "network", "loss", "k8s pod-network loss", "persistent"),
    "cpu-load": ("pod", "cpu", "fullload", "k8s pod-cpu fullload", "persistent"),
    "memory-load": ("pod", "mem", "load", "k8s pod-mem load", "persistent"),
    "pod-delete": ("pod", "pod", "delete", "k8s pod-pod delete", "one_shot"),
    "pod-fail": ("pod", "pod", "fail", "k8s pod-pod fail", "persistent"),
}


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


class ChaosBladeAtomicFault(RegistryModel):
    fault_type: Literal[
        "network-delay",
        "network-loss",
        "cpu-load",
        "memory-load",
        "pod-delete",
        "pod-fail",
    ]
    actuator: Literal["ChaosBlade"] = "ChaosBlade"
    scope: Literal["pod"]
    target: Literal["network", "cpu", "mem", "pod"]
    action: Literal["delay", "loss", "fullload", "load", "delete", "fail"]
    command_kind: str = Field(min_length=8, max_length=120)
    fault_semantics: Literal["persistent", "one_shot"]
    required_parameters: list[str] = Field(min_length=1, max_length=12)

    @model_validator(mode="after")
    def validate_atomic_command(self) -> ChaosBladeAtomicFault:
        expected = CHAOSBLADE_ATOMIC_FAULTS[self.fault_type]
        observed = (
            self.scope,
            self.target,
            self.action,
            self.command_kind,
            self.fault_semantics,
        )
        if observed != expected:
            raise ValueError(
                f"{self.fault_type} must use qualified ChaosBlade atomic command {expected}"
            )
        return self


class SemanticTemplate(RegistryModel):
    template_id: str = Field(pattern=r"^RD-(?:0[1-9]|10)$")
    risk_name: str = Field(min_length=3, max_length=120)
    consequence_name: str = Field(min_length=3, max_length=160)
    plain_summary: str = Field(min_length=8, max_length=500)
    domain: str
    prompt_file: str
    d_class: DClassPolicy
    applicability: str = Field(min_length=8, max_length=1200)
    latent_condition: str = Field(min_length=8, max_length=1600)
    activation: str = Field(min_length=8, max_length=1200)
    mechanism: str
    unacceptable_outcome: str = Field(min_length=8, max_length=1200)
    graph_focus: list[str] = Field(min_length=1, max_length=20)
    kubernetes_kinds: list[str] = Field(default_factory=list, max_length=20)
    atomic_faults: list[ChaosBladeAtomicFault] = Field(min_length=1, max_length=2)
    fault_target_kinds: list[str] = Field(min_length=1, max_length=12)
    required_static_evidence: list[str] = Field(min_length=1, max_length=12)
    counter_evidence: list[str] = Field(min_length=1, max_length=12)
    runtime_oracles: list[str] = Field(min_length=1, max_length=12)
    recovery_criteria: list[str] = Field(min_length=1, max_length=12)
    boundary_exclusions: list[str] = Field(min_length=1, max_length=10)

    @computed_field
    @property
    def defect_name(self) -> str:
        return f"风险：{self.risk_name}；后果：{self.consequence_name}"

    @property
    def fault_types(self) -> list[str]:
        return [item.fault_type for item in self.atomic_faults]

    @model_validator(mode="after")
    def validate_unique_atomic_faults(self) -> SemanticTemplate:
        if len(self.fault_types) != len(set(self.fault_types)):
            raise ValueError("template atomic faults must be unique")
        return self


class SemanticTemplateRegistry(RegistryModel):
    schema_version: Literal["resilience-defect-registry.v6"]
    registry_version: str
    chaosblade_capability_registry: Literal["chaosblade-1.8.0-k8s-profiles-v1"]
    description: str
    templates: list[SemanticTemplate] = Field(min_length=10, max_length=10)

    @model_validator(mode="after")
    def validate_active_set(self) -> SemanticTemplateRegistry:
        observed = tuple(item.template_id for item in self.templates)
        if set(observed) != set(ACTIVE_TEMPLATE_IDS) or len(observed) != len(set(observed)):
            raise ValueError("template registry must contain exactly the ten active IDs")
        observed_faults = {
            fault.fault_type
            for template in self.templates
            for fault in template.atomic_faults
        }
        if observed_faults != set(CHAOSBLADE_ATOMIC_FAULTS):
            raise ValueError(
                "ten-template registry must cover every qualified ChaosBlade atomic fault"
            )
        prompts = [item.prompt_file for item in self.templates]
        domains = [item.domain for item in self.templates]
        if len(prompts) != len(set(prompts)):
            raise ValueError("active templates must use unique prompt files")
        if len(domains) != len(set(domains)):
            raise ValueError("active templates must use unique domains")
        return self

    def by_id(self, template_id: str) -> SemanticTemplate:
        return next(item for item in self.templates if item.template_id == template_id)

    def index_for_prompt(self) -> list[dict[str, Any]]:
        return [
            {
                "template_id": item.template_id,
                "defect_name": item.defect_name,
                "plain_summary": item.plain_summary,
                "domain": item.domain,
                "applicability": item.applicability,
                "latent_condition": item.latent_condition,
                "activation": item.activation,
                "mechanism": item.mechanism,
                "unacceptable_outcome": item.unacceptable_outcome,
                "graph_focus": item.graph_focus,
                "kubernetes_kinds": item.kubernetes_kinds,
                "atomic_faults": [
                    fault.model_dump(mode="json") for fault in item.atomic_faults
                ],
                "boundary_exclusions": item.boundary_exclusions,
            }
            for item in self.templates
        ]


def load_template_registry(path: Path) -> SemanticTemplateRegistry:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError("semantic template registry must be a YAML object")
    return SemanticTemplateRegistry.model_validate(value)
