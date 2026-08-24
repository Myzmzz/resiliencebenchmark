/**
 * Application environment types
 */

export interface NamespaceConfig {
  template: string;
  liveReference?: string;
  lifecycle: string;
}

export interface ReadinessGap {
  observedAt?: string;
  severity: string;
  item: string;
}

export interface ReadinessInfo {
  currentStatus: string;
  knownGaps: ReadinessGap[];
  resolvedIssues?: Array<{ resolvedAt: string; item: string }>;
  nextChecks?: string[];
}

export interface SLO {
  id: string;
  queryRef: string;
  objective: string;
  window: string;
}

export interface ApplicationDetails {
  sourceSnapshot?: Record<string, any>;
  imageLock?: Record<string, any>;
  workloads?: Record<string, any>;
  slos?: SLO[];
  observability?: Record<string, any>;
  resetContract?: Record<string, any>;
  qualifyContract?: Record<string, any>;
  readiness?: ReadinessInfo;
}

export interface Application {
  name: string;
  displayName: string;
  benchmarkRole: string;
  visibility: string;
  namespace: NamespaceConfig;
  imageCount: number;
  imagePolicy: string;
  criticalPathsCount: number;
  sloCount: number;
  status: "qualified" | "partial" | "pending" | "inactive" | "error";
  readinessStatus: string;
  knownGaps: ReadinessGap[];
  details?: ApplicationDetails;
}
