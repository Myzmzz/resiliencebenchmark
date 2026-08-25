/**
 * Harness configuration types
 */

export interface HarnessEntrypoint {
  mode: string;
  command: string;
  prompt_transport: string;
  args: string[];
}

export interface HarnessMCP {
  template?: string;
  qualification?: string;
  env_example?: string;
  enabled_flag?: string;
  config_path_flag?: string;
  config_path_status?: string;
  transport_status?: string;
  host_native_sse_listeners?: Record<string, string>;
  read_only_servers?: string[];
  attach_to?: Record<string, string[]>;
  chaos_control?: string;
}

export interface HarnessModels {
  source: string;
  default_alias?: string;
  candidate_aliases_requiring_probe?: string[];
}

export interface HarnessSafety {
  require_controller_budget_token?: boolean;
  require_fresh_config_home_per_trial?: boolean;
  require_fresh_codex_home_per_trial?: boolean;
  deny_direct_oracle_access?: boolean;
  deny_unscoped_shell?: boolean;
}

export interface HarnessVersionPin {
  upstream?: string;
  distribution?: string;
  channel?: string;
  package_version?: string;
  npm_dist_tag_policy?: string;
  npm_integrity?: string;
  runtime_lock?: string;
  runtime_lock_sha256?: string;
  git_tag?: string;
  commit?: string;
  verification_status?: string;
  note?: string;
}

export interface HarnessConfig {
  id: string;
  kind: string;
  status: string;
  qualification_status?: string;
  entrypoint: HarnessEntrypoint;
  mcp?: HarnessMCP;
  environment?: Record<string, string>;
  isolation?: Record<string, any>;
  trace?: Record<string, string>;
  models: HarnessModels;
  safety: HarnessSafety;
  version_pin?: HarnessVersionPin;
}

export interface HarnessesRegistry {
  version: string;
  description: string;
  shared: Record<string, any>;
  harnesses: HarnessConfig[];
}
