/**
 * Model configuration types
 */

export interface CredentialRef {
  base_url: string;
  api_key: string;
  auth_scheme: string;
}

export interface CapabilityProbe {
  enabled?: boolean;
  timeout_seconds?: number;
  isolation?: string;
  prompt_ref?: string;
  transport_checks_implemented?: string[];
  behavioral_checks_required_before_matrix_freeze?: string[];
  recorded_fields?: string[];
  transport_acceptance?: Record<string, any>;
  behavioral_acceptance_pending?: Record<string, any>;
  current_oauth_catalog?: string;
  transport_checks_required?: string[];
}

export interface ModelConfig {
  id: string;
  upstream_model: string;
  display_name: string;
  protocol_candidates: string[];
  credential_ref?: string;
  authentication_modes?: string[];
  capability_probe?: CapabilityProbe;
}

export interface ModelsRegistry {
  version: string;
  description: string;
  credential_refs: Record<string, CredentialRef>;
  defaults: Record<string, any>;
  models: ModelConfig[];
}
