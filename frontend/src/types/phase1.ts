export type CoreFeatureGroup =
  | "source-codegraph"
  | "templates-defects"
  | "kubernetes"
  | "evidence-contracts";

export type CoreRunStatus = "queued" | "running" | "passed" | "failed";

export interface FeatureInputField {
  name: string;
  label: string;
  valueType: "string" | "integer" | "boolean" | "string_array" | "object" | "object_array";
  required: boolean;
  description: string;
  allowedValues: string[];
}

export interface CoreFeature {
  id: string;
  group: CoreFeatureGroup;
  title: string;
  description: string;
  evidenceOutput: string;
  mutatesLocalState: boolean;
  mayExceedFiveMinutes: boolean;
  inputFields: FeatureInputField[];
  exampleInputs: Record<string, unknown>;
}

export interface CoreFeatureCatalog {
  schemaVersion: "phase1-core-feature-catalog.v1";
  features: CoreFeature[];
}

export interface CoreTestPreview {
  previewId: string;
  featureId: string;
  createdAt: string;
  expiresAt: string;
  normalizedInputs: Record<string, unknown>;
  operations: string[];
  warnings: string[];
  confirmationText: string;
  inputDigest: string;
}

export interface CoreTestLog {
  sequence: number;
  occurredAt: string;
  level: "info" | "warning" | "error";
  message: string;
  details: Record<string, unknown>;
}

export interface CoreTestRun {
  runId: string;
  featureId: string;
  status: CoreRunStatus;
  createdAt: string;
  startedAt?: string | null;
  finishedAt?: string | null;
  durationSeconds?: number | null;
  inputs: Record<string, unknown>;
  logs: CoreTestLog[];
  result?: unknown | null;
  error?: string | null;
}
