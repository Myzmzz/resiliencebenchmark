"""Configuration loading for local semantic scans."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class ConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CodebaseConfig(ConfigModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{1,63}$")
    path: Path
    source_identity: str = Field(min_length=3, max_length=200)


class CodeGraphConfig(ConfigModel):
    command: str = "codegraph"
    force_reindex: bool = False
    timeout_seconds: int = Field(default=300, ge=30, le=1800)
    max_nodes: int = Field(default=60, ge=10, le=200)
    max_code_blocks: int = Field(default=12, ge=0, le=40)
    max_query_results: int = Field(default=30, ge=5, le=100)


class KubernetesSourceConfig(ConfigModel):
    alias: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,31}$")
    path: Path


class KubernetesConfig(ConfigModel):
    mode: Literal["live", "manifest"] = "live"
    sources: list[KubernetesSourceConfig] = Field(default_factory=list, max_length=20)
    namespace: str = Field(default="otel-demo", min_length=1, max_length=253)
    kubeconfig_path: Path | None = None
    include_configmap_data: bool = True
    discover_custom_resources: bool = True
    max_resources_per_agent: int = Field(default=80, ge=10, le=500)
    authoritative_for_namespace: bool = False


class ContextBudgetConfig(ConfigModel):
    coordinator_chars: int = Field(default=18_000, ge=4_000, le=60_000)
    subagent_chars: int = Field(default=32_000, ge=8_000, le=100_000)
    verifier_chars: int = Field(default=24_000, ge=8_000, le=80_000)
    max_tool_result_chars: int = Field(default=16_000, ge=2_000, le=50_000)


class AgentConfig(ConfigModel):
    model_config_path: Path
    max_concurrency: int = Field(default=1, ge=1, le=6)
    recursion_limit: int = Field(default=320, ge=10, le=500)
    agent_timeout_seconds: int = Field(default=1800, ge=60, le=7200)
    max_attempts_per_agent: int = Field(default=2, ge=1, le=3)
    tool_call_limit: int = Field(default=100, ge=1, le=500)
    model_call_limit: int = Field(default=120, ge=1, le=500)
    context_budget: ContextBudgetConfig = Field(default_factory=ContextBudgetConfig)
    structured_output_strategy: Literal["tool", "provider", "auto"] = "tool"


class SemanticScanConfig(ConfigModel):
    schema_version: Literal["semantic-scan-config.v1"] = "semantic-scan-config.v1"
    codebase: CodebaseConfig
    codegraph: CodeGraphConfig = Field(default_factory=CodeGraphConfig)
    kubernetes: KubernetesConfig
    templates_path: Path
    prompts_root: Path
    output_dir: Path
    agents: AgentConfig
    active_template_ids: list[str] = Field(
        default_factory=lambda: [
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
        ],
        min_length=1,
        max_length=12,
    )
    planning_mode: Literal["model", "deterministic"] = "model"

    @model_validator(mode="after")
    def validate_paths(self) -> SemanticScanConfig:
        if not self.templates_path.is_file():
            raise ValueError(f"template registry does not exist: {self.templates_path}")
        if not self.prompts_root.is_dir():
            raise ValueError(f"prompts root does not exist: {self.prompts_root}")
        if not self.agents.model_config_path.is_file():
            raise ValueError(
                f"model configuration does not exist: {self.agents.model_config_path}"
            )
        if self.kubernetes.mode == "manifest":
            if not self.kubernetes.sources:
                raise ValueError("manifest Kubernetes mode requires at least one source")
            for source in self.kubernetes.sources:
                if not source.path.exists():
                    raise ValueError(f"Kubernetes source does not exist: {source.path}")
        if self.kubernetes.kubeconfig_path is not None and not self.kubernetes.kubeconfig_path.exists():
            raise ValueError(
                f"Kubernetes kubeconfig does not exist: {self.kubernetes.kubeconfig_path}"
            )
        if len(self.active_template_ids) != len(set(self.active_template_ids)):
            raise ValueError("active_template_ids must be unique")
        return self


def _resolve_path(raw: str | Path, root: Path) -> Path:
    value = Path(raw).expanduser()
    return value.resolve() if value.is_absolute() else (root / value).resolve()


def load_semantic_scan_config(path: Path) -> SemanticScanConfig:
    resolved = path.expanduser().resolve()
    raw = yaml.safe_load(resolved.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise TypeError("semantic scan config must be a YAML object")
    root = resolved.parent
    raw["codebase"]["path"] = _resolve_path(raw["codebase"]["path"], root)
    raw["templates_path"] = _resolve_path(raw["templates_path"], root)
    raw["prompts_root"] = _resolve_path(raw["prompts_root"], root)
    raw["output_dir"] = _resolve_path(raw["output_dir"], root)
    raw["agents"]["model_config_path"] = _resolve_path(
        raw["agents"]["model_config_path"], root
    )
    for source in raw["kubernetes"].get("sources", []):
        source["path"] = _resolve_path(source["path"], root)
    if raw["kubernetes"].get("kubeconfig_path") is not None:
        raw["kubernetes"]["kubeconfig_path"] = _resolve_path(
            raw["kubernetes"]["kubeconfig_path"], root
        )
    return SemanticScanConfig.model_validate(raw)
