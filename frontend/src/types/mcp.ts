/**
 * MCP tools configuration types
 */

export interface ToolScope {
  namespaces_from_episode?: boolean;
  cluster_scoped_reads?: string;
  kubeconfig_from_server_runtime_only?: boolean;
  query_time_window_from_controller?: boolean;
  one_namespace_per_episode?: boolean;
  service_allowlist_from_episode?: boolean;
  upstream_label_scope_required_for_shared_cluster?: boolean;
  source_root_from_server_runtime_only?: boolean;
  application_allowlist_from_episode?: boolean;
}

export interface ToolGates {
  require_episode_fault_allowlist?: boolean;
  require_controller_budget_token?: boolean;
  require_preflight_baseline_passed?: boolean;
  require_cleanup_handle?: boolean;
  require_live_target_pod_uid?: boolean;
  require_global_chaosblade_inventory_clear?: boolean;
  baseline_capability_one_time_use?: boolean;
  require_durable_deadline_watchdog?: boolean;
}

export interface MCPTool {
  id: string;
  mode: string;
  purpose: string;
  allowed_operations: string[];
  denied_operations: string[];
  scope?: ToolScope;
  gates?: ToolGates;
}

export interface NotExposedToAgent {
  reason: string;
  includes: string[];
}

export interface MCPToolsRegistry {
  version: string;
  description: string;
  runtime_refs: Record<string, string>;
  tools: MCPTool[];
  not_exposed_to_agent?: NotExposedToAgent;
}
