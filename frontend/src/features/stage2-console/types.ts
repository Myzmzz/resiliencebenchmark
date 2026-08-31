export type CaseId = "C0" | "P1" | "P2" | "D1" | "D2" | "D3" | "D4";
export type ConsolePhase = "C1" | "C2" | "C3" | "C4" | "C5" | "C6";
export type ConsoleStatus = "IDLE" | "RUNNING" | "COMPLETED" | "FAILED" | "CASE_INVALID" | "ABORTED";
export type CaseVerdict = "PENDING" | "PASS" | "FAIL" | "CASE_INVALID" | "SKIPPED";

export interface CaseDefinition {
  case_id: CaseId;
  title: string;
  trial_kind?: string;
  prompt_exposure?: string;
  objective: string;
  prompt_delta: string;
  disturbance: string;
  trigger_phase: ConsolePhase | null;
  trigger_event: string | null;
  expected_behavior: string;
  expected_agent_signal?: string;
  stop_after_expected_signal?: boolean;
  failure_condition: string;
  max_seconds: number;
}

export interface CaseBundle {
  schema_version: "stage2-codex-disturbance-bundle.v1";
  prompt: string;
  generated_at: string;
  harness: "codex";
  model: "gpt-5.6-sol";
  cases: CaseDefinition[];
}

export interface EnvironmentCheck {
  component: string;
  status: "ok" | "warning" | "error" | "unknown";
  detail: string;
  evidence: Record<string, unknown>;
}

export interface PreflightStatus {
  schema_version: "stage2-console-preflight.v1";
  checked_at: string;
  qualified: boolean;
  checks: EnvironmentCheck[];
}

export interface RuntimeState {
  permissions: Record<string, boolean>;
  pod_name: string | null;
  pod_uid: string | null;
  fault_status: "none" | "planned" | "running" | "recovered" | "unknown";
  observability_status: "available" | "revoked" | "unknown";
}

export interface CaseRunSnapshot {
  case_id: CaseId;
  status: ConsoleStatus;
  verdict: CaseVerdict;
  current_phase: ConsolePhase | null;
  started_at: string | null;
  finished_at: string | null;
  runtime: RuntimeState;
  evidence_refs: string[];
  summary: string;
}

export interface ConsoleRunSnapshot {
  schema_version: "stage2-console-run.v1";
  run_id: string;
  campaign_id?: string;
  status: ConsoleStatus;
  harness: "codex";
  model: "gpt-5.6-sol";
  started_at: string;
  finished_at: string | null;
  selected_cases: CaseId[];
  cases: CaseRunSnapshot[];
  runtime: RuntimeState;
  verdict_counts: Record<string, number>;
  event_count: number;
}

export interface ConsoleEvent {
  sequence: number;
  run_id: string;
  case_id: CaseId | null;
  phase: ConsolePhase | null;
  event_type: string;
  occurred_at: string;
  message: string;
  payload: Record<string, unknown>;
}

export interface EvidenceItem {
  path: string;
  kind: string;
  size_bytes: number;
  created_at: string;
  summary: string;
  download_url?: string;
}
