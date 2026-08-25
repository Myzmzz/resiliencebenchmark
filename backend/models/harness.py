"""Harness configuration data models."""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class HarnessEntrypoint(BaseModel):
    """Harness entrypoint configuration."""

    mode: str
    command: str
    prompt_transport: str = Field(alias="prompt_transport")
    args: List[str] = Field(default_factory=list)


class HarnessMCP(BaseModel):
    """Harness MCP configuration."""

    template: Optional[str] = None
    qualification: Optional[str] = None
    env_example: Optional[str] = Field(None, alias="env_example")
    enabled_flag: Optional[str] = Field(None, alias="enabled_flag")
    config_path_flag: Optional[str] = Field(None, alias="config_path_flag")
    config_path_status: Optional[str] = Field(None, alias="config_path_status")
    transport_status: Optional[str] = Field(None, alias="transport_status")
    host_native_sse_listeners: Optional[Dict[str, str]] = Field(
        None, alias="host_native_sse_listeners"
    )
    read_only_servers: Optional[List[str]] = Field(None, alias="read_only_servers")
    attach_to: Optional[Dict[str, List[str]]] = Field(None, alias="attach_to")
    chaos_control: Optional[str] = Field(None, alias="chaos_control")


class HarnessModels(BaseModel):
    """Harness models configuration."""

    source: str
    default_alias: Optional[str] = Field(None, alias="default_alias")
    candidate_aliases_requiring_probe: Optional[List[str]] = Field(
        None, alias="candidate_aliases_requiring_probe"
    )


class HarnessSafety(BaseModel):
    """Harness safety configuration."""

    require_controller_budget_token: Optional[bool] = Field(
        None, alias="require_controller_budget_token"
    )
    require_fresh_config_home_per_trial: Optional[bool] = Field(
        None, alias="require_fresh_config_home_per_trial"
    )
    require_fresh_codex_home_per_trial: Optional[bool] = Field(
        None, alias="require_fresh_codex_home_per_trial"
    )
    deny_direct_oracle_access: Optional[bool] = Field(
        None, alias="deny_direct_oracle_access"
    )
    deny_unscoped_shell: Optional[bool] = Field(None, alias="deny_unscoped_shell")


class HarnessVersionPin(BaseModel):
    """Harness version pin information."""

    upstream: Optional[str] = None
    distribution: Optional[str] = None
    channel: Optional[str] = None
    package_version: Optional[str] = Field(None, alias="package_version")
    npm_dist_tag_policy: Optional[str] = Field(None, alias="npm_dist_tag_policy")
    npm_integrity: Optional[str] = Field(None, alias="npm_integrity")
    runtime_lock: Optional[str] = Field(None, alias="runtime_lock")
    runtime_lock_sha256: Optional[str] = Field(None, alias="runtime_lock_sha256")
    git_tag: Optional[str] = Field(None, alias="git_tag")
    commit: Optional[str] = None
    verification_status: Optional[str] = Field(None, alias="verification_status")
    note: Optional[str] = None


class HarnessConfig(BaseModel):
    """Harness configuration."""

    id: str
    kind: str
    status: str
    qualification_status: Optional[str] = Field(None, alias="qualification_status")
    entrypoint: HarnessEntrypoint
    mcp: Optional[HarnessMCP] = None
    environment: Optional[Dict[str, str]] = None
    isolation: Optional[Dict[str, Any]] = None
    trace: Optional[Dict[str, str]] = None
    models: HarnessModels
    safety: HarnessSafety
    version_pin: Optional[HarnessVersionPin] = Field(None, alias="version_pin")


class HarnessesRegistry(BaseModel):
    """Harnesses registry configuration."""

    version: str
    description: str
    shared: Dict[str, Any]
    harnesses: List[HarnessConfig]

    class Config:
        populate_by_name = True
