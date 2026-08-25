"""Parser for observability.yaml configuration."""

from pathlib import Path
from typing import Optional

import yaml

from backend.models.observability import (
    AgentTooling,
    ClusterAccess,
    EvidenceRetention,
    JaegerConfig,
    LokiConfig,
    MCPServer,
    ObservabilityMetadata,
    ObservabilitySpec,
    ObservabilityStack,
    OtelCollectorConfig,
    PrometheusConfig,
)


def parse_observability(repo_path: Path) -> Optional[ObservabilityStack]:
    """
    Parse observability.yaml configuration.

    Args:
        repo_path: Path to the resiliencebenchmark repository

    Returns:
        ObservabilityStack object or None if file doesn't exist
    """
    observability_file = repo_path / "environment" / "shared" / "observability.yaml"

    if not observability_file.exists():
        return None

    try:
        with open(observability_file, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        if not data:
            return None

        # Parse metadata
        metadata = ObservabilityMetadata(**data.get("metadata", {}))

        # Parse spec
        spec_data = data.get("spec", {})

        # Parse cluster access
        cluster_access = ClusterAccess(**spec_data.get("clusterAccess", {}))

        # Parse Prometheus
        prometheus = PrometheusConfig(**spec_data.get("prometheus", {}))

        # Parse Jaeger
        jaeger = JaegerConfig(**spec_data.get("jaeger", {}))

        # Parse Loki
        loki = LokiConfig(**spec_data.get("loki", {}))

        # Parse OTel Collector
        otel_collector = OtelCollectorConfig(**spec_data.get("otelCollector", {}))

        # Parse agent tooling
        agent_tooling_data = spec_data.get("agentTooling", {})
        mcp_servers = [
            MCPServer(**server) for server in agent_tooling_data.get("mcpServers", [])
        ]
        agent_tooling = AgentTooling(mcpServers=mcp_servers)

        # Parse evidence retention
        evidence_retention = EvidenceRetention(
            **spec_data.get("evidenceRetention", {})
        )

        # Build spec
        spec = ObservabilitySpec(
            clusterAccess=cluster_access,
            prometheus=prometheus,
            jaeger=jaeger,
            loki=loki,
            otelCollector=otel_collector,
            agentTooling=agent_tooling,
            evidenceRetention=evidence_retention,
            readinessChecks=spec_data.get("readinessChecks", []),
        )

        return ObservabilityStack(
            apiVersion=data.get("apiVersion", "unknown"),
            kind=data.get("kind", "unknown"),
            metadata=metadata,
            spec=spec,
        )

    except Exception as e:
        print(f"Error parsing observability.yaml: {e}")
        return None
