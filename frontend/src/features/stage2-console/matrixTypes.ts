import type { CaseId, HarnessId } from "./types";

export type MatrixVerdict = "PASS" | "FAIL" | "INCONCLUSIVE" | "CASE_INVALID" | "NOT_RUN";

export interface MatrixListItem {
  matrix_id: string;
  system: string;
  generated_at: string;
  expected_trial_count: number;
  completed_trial_count: number;
  campaign_count: number;
  manifest_valid: boolean;
}

export interface MatrixScoreRow {
  harness: HarnessId;
  model: string;
  score: number | null;
  pass: number;
  fail: number;
  inconclusive: number;
  case_invalid: number;
  valid_trials: number;
  completed_trials: number;
  expected_trials: number;
  agent_recovery_verified: number;
  controller_fallbacks: number;
}

export interface MatrixTrialSummary {
  campaign_id: string;
  request_id: string;
  source_matrix_id: string | null;
  trial_id: string;
  harness: HarnessId;
  model: string;
  case_id: CaseId;
  agent_verdict: MatrixVerdict;
  platform_valid: boolean;
  diagnostic_only: boolean;
  fault_active: boolean;
  effect_verified: boolean;
  agent_recovery_verified: boolean;
  controller_cleanup_verified: boolean;
  business_recovery_verified: boolean;
  target: {
    namespace?: string;
    component?: string;
    kind?: string;
    name?: string;
    uid?: string;
  };
  validation_error: string | null;
  started_at: string | null;
  finished_at: string | null;
  duration_seconds: number | null;
  artifact_count: number;
}

export interface MatrixInspection {
  schema_version: "stage2-matrix-inspection.v1";
  matrix_id: string;
  report: {
    system: string;
    prompt: string;
    generated_at: string;
    score_definition: string;
    score_table: MatrixScoreRow[];
    key_findings: Record<string, string[]>;
  };
  request: Record<string, unknown>;
  summary: {
    expected_trials: number;
    completed_trials: number;
    platform_valid: number;
    platform_invalid: number;
    diagnostic_only: number;
    fault_active: number;
    effect_verified: number;
    agent_recovery_verified: number;
    controller_cleanup_verified: number;
    business_recovery_verified: number;
    verdict_counts: Partial<Record<MatrixVerdict, number>>;
  };
  integrity: {
    all_valid: boolean;
    verified_count: number;
    expected_count: number;
    matrix: { valid: boolean; checked_files: number; errors: string[] };
    campaigns: Array<{ campaign_id: string; valid: boolean; checked_files: number; errors: string[] }>;
  };
  source_matrices: string[];
  trials: MatrixTrialSummary[];
}

export interface TextEvidence {
  available: boolean;
  text: string;
  truncated: boolean;
  size_bytes?: number;
}

export interface MatrixTrialDetail {
  schema_version: "stage2-trial-inspection.v1";
  matrix_id: string;
  summary: MatrixTrialSummary;
  result: Record<string, unknown>;
  agent: {
    harness_report: Record<string, unknown>;
    stdout: TextEvidence;
    stderr: TextEvidence;
    lifecycle_events: Array<Record<string, unknown>>;
  };
  controller: {
    events: Array<Record<string, unknown>>;
    disturbances: Array<Record<string, unknown>>;
    permission_restore: Record<string, unknown>;
    environment_reset: Record<string, unknown>;
  };
  oracle: {
    recovery: Record<string, unknown>;
    fault_effect_evidence: Record<string, unknown> | null;
  };
  runtime: {
    context: Record<string, unknown>;
    capability: Record<string, unknown>;
  };
  files: Array<{ path: string; size_bytes: number; download_url: string }>;
}
