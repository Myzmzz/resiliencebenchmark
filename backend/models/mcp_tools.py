"""MCP tools configuration data models."""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class ToolScope(BaseModel):
    """Tool scope configuration."""

    namespaces_from_episode: Optional[bool] = Field(None, alias="namespaces_from_episode")
    cluster_scoped_reads: Optional[str] = Field(None, alias="cluster_scoped_reads")
    kubeconfig_from_server_runtime_only: Optional[bool] = Field(
        None, alias="kubeconfig_from_server_runtime_only"
    )
    query_time_window_from_controller: Optional[bool] = Field(
        None, alias="query_time_window_from_controller"
    )
    one_namespace_per_episode: Optional[bool] = Field(
        None, alias="one_namespace_per_episode"
    )
    service_allowlist_from_episode: Optional[bool] = Field(
        None, alias="service_allowlist_from_episode"
    )
    upstream_label_scope_required_for_shared_cluster: Optional[bool] = Field(
        None, alias="upstream_label_scope_required_for_shared_cluster"
    )
    source_root_from_server_runtime_only: Optional[bool] = Field(
        None, alias="source_root_from_server_runtime_only"
    )
    application_allowlist_from_episode: Optional[bool] = Field(
        None, alias="application_allowlist_from_episode"
    )


class ToolGates(BaseModel):
    """Tool gates configuration."""

    require_episode_fault_allowlist: Optional[bool] = Field(
        None, alias="require_episode_fault_allowlist"
    )
    require_controller_budget_token: Optional[bool] = Field(
        None, alias="require_controller_budget_token"
    )
    require_preflight_baseline_passed: Optional[bool] = Field(
        None, alias="require_preflight_baseline_passed"
    )
    require_cleanup_handle: Optional[bool] = Field(None, alias="require_cleanup_handle")
    require_live_target_pod_uid: Optional[bool] = Field(
        None, alias="require_live_target_pod_uid"
    )
    require_global_chaosblade_inventory_clear: Optional[bool] = Field(
        None, alias="require_global_chaosblade_inventory_clear"
    )
    baseline_capability_one_time_use: Optional[bool] = Field(
        None, alias="baseline_capability_one_time_use"
    )
    require_durable_deadline_watchdog: Optional[bool] = Field(
        None, alias="require_durable_deadline_watchdog"
    )


class MCPTool(BaseModel):
    """MCP tool configuration."""

    id: str
    mode: str
    purpose: str
    allowed_operations: List[str] = Field(alias="allowed_operations")
    denied_operations: List[str] = Field(alias="denied_operations")
    scope: Optional[ToolScope] = None
    gates: Optional[ToolGates] = None


class NotExposedToAgent(BaseModel):
    """Configuration for tools not exposed to agent."""

    reason: str
    includes: List[str]


class MCPToolsRegistry(BaseModel):
    """MCP tools registry configuration."""

    version: str
    description: str
    runtime_refs: Dict[str, str] = Field(alias="runtime_refs")
    tools: List[MCPTool]
    not_exposed_to_agent: Optional[NotExposedToAgent] = Field(
        None, alias="not_exposed_to_agent"
    )

    class Config:
        populate_by_name = True
