"""Integration tests for observability API endpoint."""

import pytest
from pathlib import Path
from fastapi.testclient import TestClient

from backend.main import app


@pytest.fixture
def client():
    """Create test client."""
    return TestClient(app)


def test_get_observability_success(client):
    """Test successful retrieval of observability stack."""
    response = client.get("/api/v1/observability")

    # If file doesn't exist, expect 404
    if response.status_code == 404:
        pytest.skip("Observability configuration file not available in test environment")

    assert response.status_code == 200
    data = response.json()

    # Verify structure
    assert "apiVersion" in data
    assert "kind" in data
    assert "metadata" in data
    assert "spec" in data

    # Verify metadata
    metadata = data["metadata"]
    assert "name" in metadata
    assert "visibility" in metadata

    # Verify spec
    spec = data["spec"]
    assert "clusterAccess" in spec
    assert "prometheus" in spec
    assert "jaeger" in spec
    assert "loki" in spec
    assert "otelCollector" in spec
    assert "agentTooling" in spec
    assert "evidenceRetention" in spec
    assert "readinessChecks" in spec


def test_get_observability_with_real_data(client):
    """Test with actual resiliencebenchmark repository if available."""
    response = client.get("/api/v1/observability")

    # If file doesn't exist, skip the test
    if response.status_code == 404:
        pytest.skip("Observability configuration file not available in test environment")

    assert response.status_code == 200
    data = response.json()

    # Verify Prometheus config
    prometheus = data["spec"]["prometheus"]
    assert "endpoint" in prometheus
    assert "accessMode" in prometheus
    assert "allowedApis" in prometheus
    assert isinstance(prometheus["allowedApis"], list)
    assert "requiredLabels" in prometheus
    assert isinstance(prometheus["requiredLabels"], list)
    assert "forbiddenLabels" in prometheus

    # Verify Jaeger config
    jaeger = data["spec"]["jaeger"]
    assert "endpoint" in jaeger
    assert "accessMode" in jaeger
    assert "requiredCapabilities" in jaeger
    assert isinstance(jaeger["requiredCapabilities"], list)

    # Verify Loki config
    loki = data["spec"]["loki"]
    assert "endpoint" in loki
    assert "accessMode" in loki
    assert "allowedApis" in loki
    assert "requiredLabels" in loki

    # Verify OTel Collector config
    otel = data["spec"]["otelCollector"]
    assert "grpcEndpoint" in otel
    assert "httpEndpoint" in otel
    assert "accessMode" in otel
    assert "exporterBaseline" in otel

    # Verify agent tooling
    agent_tooling = data["spec"]["agentTooling"]
    assert "mcpServers" in agent_tooling
    assert isinstance(agent_tooling["mcpServers"], list)

    if len(agent_tooling["mcpServers"]) > 0:
        server = agent_tooling["mcpServers"][0]
        assert "name" in server
        assert "scope" in server
        assert "allowedOperations" in server

    # Verify evidence retention
    evidence = data["spec"]["evidenceRetention"]
    assert "rawWindow" in evidence
    assert "normalizedWindow" in evidence
    assert "exportFormat" in evidence

    # Verify readiness checks
    assert isinstance(data["spec"]["readinessChecks"], list)


def test_observability_parser_handles_missing_file(monkeypatch):
    """Test parser handles missing observability.yaml gracefully."""
    from backend.parsers.observability import parse_observability

    # Use a non-existent path
    fake_path = Path("/nonexistent/path")

    result = parse_observability(fake_path)

    # Should return None, not crash
    assert result is None
