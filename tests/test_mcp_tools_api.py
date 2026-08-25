"""Integration tests for MCP tools API endpoint."""

import pytest
from pathlib import Path
from fastapi.testclient import TestClient

from backend.main import app


@pytest.fixture
def client():
    """Create test client."""
    return TestClient(app)


def test_get_mcp_tools_success(client):
    """Test successful retrieval of MCP tools registry."""
    response = client.get("/api/v1/mcp-tools")

    # If file doesn't exist, expect 404
    if response.status_code == 404:
        pytest.skip("MCP tools configuration file not available in test environment")

    assert response.status_code == 200
    data = response.json()

    # Verify structure
    assert "version" in data
    assert "description" in data
    assert "runtime_refs" in data
    assert "tools" in data

    # Verify tools list
    assert isinstance(data["tools"], list)

    if len(data["tools"]) > 0:
        tool = data["tools"][0]
        assert "id" in tool
        assert "mode" in tool
        assert "purpose" in tool
        assert "allowed_operations" in tool
        assert "denied_operations" in tool
        assert isinstance(tool["allowed_operations"], list)
        assert isinstance(tool["denied_operations"], list)


def test_get_mcp_tools_with_real_data(client):
    """Test with actual resiliencebenchmark repository if available."""
    response = client.get("/api/v1/mcp-tools")

    # If file doesn't exist, skip the test
    if response.status_code == 404:
        pytest.skip("MCP tools configuration file not available in test environment")

    assert response.status_code == 200
    data = response.json()

    # Check for expected tools
    tool_ids = [tool["id"] for tool in data["tools"]]

    # Should have common MCP tools
    if len(tool_ids) >= 3:
        expected_tools = ["k8s_ro", "telemetry_ro", "source_ro", "chaos_control"]
        found_tools = [t for t in expected_tools if t in tool_ids]
        assert len(found_tools) > 0, f"Expected to find some tools from {expected_tools}"

        # Find a specific tool and verify its structure
        k8s_tool = next((t for t in data["tools"] if t["id"] == "k8s_ro"), None)

        if k8s_tool:
            assert k8s_tool["mode"] == "read_only"
            assert "Kubernetes" in k8s_tool["purpose"]
            assert len(k8s_tool["allowed_operations"]) > 0
            assert len(k8s_tool["denied_operations"]) > 0

            # Verify scope exists
            assert "scope" in k8s_tool
            assert isinstance(k8s_tool["scope"], dict)

        # Find chaos_control tool and verify gates
        chaos_tool = next((t for t in data["tools"] if t["id"] == "chaos_control"), None)

        if chaos_tool:
            assert chaos_tool["mode"] == "controlled_write"
            assert "gates" in chaos_tool
            assert isinstance(chaos_tool["gates"], dict)

    # Verify runtime refs
    assert "runtime_refs" in data
    assert isinstance(data["runtime_refs"], dict)


def test_mcp_tools_parser_handles_missing_file(monkeypatch):
    """Test parser handles missing mcp-tools.yaml gracefully."""
    from backend.parsers.mcp_tools import parse_mcp_tools

    # Use a non-existent path
    fake_path = Path("/nonexistent/path")

    result = parse_mcp_tools(fake_path)

    # Should return None, not crash
    assert result is None
