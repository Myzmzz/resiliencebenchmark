/**
 * Observability stack types
 */

export interface ClusterAccess {
  kubeconfigRef: string;
  namespaceScope: string;
  rbacProfile: string;
  secretPolicy: string;
}

export interface PrometheusConfig {
  endpoint: string;
  accessMode: string;
  allowedApis: string[];
  requiredLabels: string[];
  forbiddenLabels: string[];
}

export interface JaegerConfig {
  endpoint: string;
  accessMode: string;
  requiredCapabilities: string[];
  notes?: string[];
}

export interface LokiConfig {
  endpoint: string;
  accessMode: string;
  allowedApis: string[];
  requiredLabels: string[];
}

export interface OtelCollectorConfig {
  grpcEndpoint: string;
  httpEndpoint: string;
  accessMode: string;
  exporterBaseline: Record<string, string>;
}

export interface MCPServer {
  name: string;
  scope: string;
  allowedOperations: string[];
}

export interface AgentTooling {
  mcpServers: MCPServer[];
}

export interface EvidenceRetention {
  rawWindow: string;
  normalizedWindow: string;
  exportFormat: string[];
}

export interface ObservabilitySpec {
  clusterAccess: ClusterAccess;
  prometheus: PrometheusConfig;
  jaeger: JaegerConfig;
  loki: LokiConfig;
  otelCollector: OtelCollectorConfig;
  agentTooling: AgentTooling;
  evidenceRetention: EvidenceRetention;
  readinessChecks: string[];
}

export interface ObservabilityMetadata {
  name: string;
  visibility: string;
}

export interface ObservabilityStack {
  apiVersion: string;
  kind: string;
  metadata: ObservabilityMetadata;
  spec: ObservabilitySpec;
}
