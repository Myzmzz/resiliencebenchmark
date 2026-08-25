"""Integration tests for applications API endpoint."""

import pytest
from pathlib import Path
from fastapi.testclient import TestClient

from backend.main import app


@pytest.fixture
def client():
    """Create test client."""
    return TestClient(app)


def test_get_applications_success(client, monkeypatch):
    """Test successful retrieval of applications."""
    # Mock repo path to point to test data
    test_repo_path = Path(__file__).parent.parent.parent / "resiliencebenchmark"

    from backend import config

    class MockSettings:
        repo_path = test_repo_path

    def mock_get_settings():
        return MockSettings()

    monkeypatch.setattr(config, "get_settings", mock_get_settings)

    response = client.get("/api/v1/applications")

    assert response.status_code == 200
    data = response.json()

    # Should return a list
    assert isinstance(data, list)

    # If test repo exists and has applications, verify structure
    if len(data) > 0:
        app = data[0]
        assert "name" in app
        assert "displayName" in app
        assert "benchmarkRole" in app
        assert "status" in app
        assert "imageCount" in app
        assert "criticalPathsCount" in app
        assert "sloCount" in app


def test_get_applications_with_real_data(client):
    """Test with actual resiliencebenchmark repository if available."""
    response = client.get("/api/v1/applications")

    assert response.status_code == 200
    data = response.json()

    assert isinstance(data, list)

    # Check for expected applications
    app_names = [app["name"] for app in data]

    # Should have at least sock-shop, otel-demo, train-ticket
    if len(data) >= 3:
        assert "sock-shop" in app_names or "otel-demo" in app_names

        # Find sock-shop if it exists
        sock_shop = next((a for a in data if a["name"] == "sock-shop"), None)

        if sock_shop:
            assert sock_shop["displayName"] == "Sock Shop"
            assert sock_shop["benchmarkRole"] == "ecommerce-reference-microservice"
            assert sock_shop["visibility"] == "agent-readable"
            assert sock_shop["imageCount"] >= 6  # Has multiple services
            assert sock_shop["criticalPathsCount"] >= 3  # Has browse, cart, checkout
            assert sock_shop["sloCount"] >= 4  # Has multiple SLOs
            assert sock_shop["status"] in ["qualified", "partial", "pending", "inactive"]

            # Check namespace config
            assert "namespace" in sock_shop
            assert "template" in sock_shop["namespace"]
            assert "lifecycle" in sock_shop["namespace"]

            # Check readiness gaps
            assert "knownGaps" in sock_shop
            assert isinstance(sock_shop["knownGaps"], list)


def test_applications_parser_handles_missing_directory(monkeypatch):
    """Test parser handles missing applications directory gracefully."""
    from backend.parsers.applications import parse_applications

    # Use a non-existent path
    fake_path = Path("/nonexistent/path")

    result = parse_applications(fake_path)

    # Should return empty list, not crash
    assert result == []


def test_applications_status_determination():
    """Test status determination logic."""
    from backend.parsers.applications import _determine_status
    from backend.models.application import ReadinessGap

    # Inactive status
    assert _determine_status("inactive-standby-otel-demo-selected", []) == "inactive"

    # Blocking gaps
    blocking_gap = ReadinessGap(severity="blocking", item="Test gap")
    assert _determine_status("active", [blocking_gap]) == "pending"

    # Non-blocking gaps
    info_gap = ReadinessGap(severity="informational", item="Info")
    assert _determine_status("active", [info_gap]) == "partial"

    # Qualified status
    assert _determine_status("qualified-ready", []) == "qualified"

    # Default pending
    assert _determine_status("unknown", []) == "pending"
