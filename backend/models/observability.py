"""Observability stack data models."""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class ClusterAccess(BaseModel):
    """Cluster access configuration."""

    kubeconfig_ref: str = Field(alias="kubeconfigRef")
    namespace_scope: str = Field(alias="namespaceScope")
    rbac_profile: str = Field(alias="rbacProfile")
    secret_policy: str = Field(alias="secretPolicy")


class PrometheusConfig(BaseModel):
    """Prometheus configuration."""

    endpoint: str
    access_mode: str = Field(alias="accessMode")
    allowed_apis: List[str] = Field(alias="allowedApis")
    required_labels: List[str] = Field(alias="requiredLabels")
    forbidden_labels: List[str] = Field(alias="forbiddenLabels")


class JaegerConfig(BaseModel):
    """Jaeger configuration."""

    endpoint: str
    access_mode: str = Field(alias="accessMode")
    required_capabilities: List[str] = Field(alias="requiredCapabilities")
    notes: Optional[List[str]] = None


class LokiConfig(BaseModel):
    """Loki configuration."""

    endpoint: str
    access_mode: str = Field(alias="accessMode")
    allowed_apis: List[str] = Field(alias="allowedApis")
    required_labels: List[str] = Field(alias="requiredLabels")


class OtelCollectorConfig(BaseModel):
    """OpenTelemetry Collector configuration."""

    grpc_endpoint: str = Field(alias="grpcEndpoint")
    http_endpoint: str = Field(alias="httpEndpoint")
    access_mode: str = Field(alias="accessMode")
    exporter_baseline: Dict[str, str] = Field(alias="exporterBaseline")


class MCPServer(BaseModel):
    """MCP server configuration."""

    name: str
    scope: str
    allowed_operations: List[str] = Field(alias="allowedOperations")


class AgentTooling(BaseModel):
    """Agent tooling configuration."""

    mcp_servers: List[MCPServer] = Field(alias="mcpServers")


class EvidenceRetention(BaseModel):
    """Evidence retention configuration."""

    raw_window: str = Field(alias="rawWindow")
    normalized_window: str = Field(alias="normalizedWindow")
    export_format: List[str] = Field(alias="exportFormat")


class ObservabilitySpec(BaseModel):
    """Observability stack specification."""

    cluster_access: ClusterAccess = Field(alias="clusterAccess")
    prometheus: PrometheusConfig
    jaeger: JaegerConfig
    loki: LokiConfig
    otel_collector: OtelCollectorConfig = Field(alias="otelCollector")
    agent_tooling: AgentTooling = Field(alias="agentTooling")
    evidence_retention: EvidenceRetention = Field(alias="evidenceRetention")
    readiness_checks: List[str] = Field(alias="readinessChecks")


class ObservabilityMetadata(BaseModel):
    """Observability metadata."""

    name: str
    visibility: str


class ObservabilityStack(BaseModel):
    """Observability stack configuration."""

    api_version: str = Field(alias="apiVersion")
    kind: str
    metadata: ObservabilityMetadata
    spec: ObservabilitySpec

    class Config:
        populate_by_name = True
