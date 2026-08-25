/**
 * Episode (evaluation unit) types
 */

export interface EpisodeApplication {
  name: string;
  namespace: string;
  candidate_services: string[];
  release_ref: string;
}

export interface EnvironmentSnapshot {
  snapshot_id: string;
  health_prerequisites?: string[];
  reset_contract?: string[];
}

export interface WorkloadConfig {
  profile: string;
  slo: string[];
}

export interface ObservabilityConfig {
  metrics: string[];
  traces: string[];
  logs: string[];
  kubernetes: string[];
}

export interface SourceAccess {
  mode: string;
  allowed_paths: string[];
  forbidden_paths: string[];
}

export interface ActionSpace {
  allowed_trigger_classes: string[];
  allowed_target_scope: string[];
  forbidden_actions: string[];
}

export interface BudgetConfig {
  max_experiments: number;
  max_duration_minutes: number;
  max_concurrent_faults: number;
}

export interface Episode {
  schema_version: string;
  episode_id: string;
  title: string;
  status: string;
  application: EpisodeApplication;
  agent_goal: string;
  environment_snapshot: EnvironmentSnapshot;
  workload: WorkloadConfig;
  observability: ObservabilityConfig;
  source_access: SourceAccess;
  action_space: ActionSpace;
  budget: BudgetConfig;
  safety_constraints: string[];
  expected_agent_output: string[];
  leakage_controls: string[];
}
