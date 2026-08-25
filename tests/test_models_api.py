"""Integration tests for models API endpoint."""

import pytest
from pathlib import Path
from fastapi.testclient import TestClient

from backend.main import app


@pytest.fixture
def client():
    """Create test client."""
    return TestClient(app)


def test_get_models_success(client):
    """Test successful retrieval of models registry."""
    response = client.get("/api/v1/models")

    # If file doesn't exist, expect 404
    if response.status_code == 404:
        pytest.skip("Models configuration file not available in test environment")

    assert response.status_code == 200
    data = response.json()

    # Verify structure
    assert "version" in data
    assert "description" in data
    assert "credential_refs" in data
    assert "defaults" in data
    assert "models" in data

    # Verify models list
    assert isinstance(data["models"], list)

    if len(data["models"]) > 0:
        model = data["models"][0]
        assert "id" in model
        assert "upstream_model" in model
        assert "display_name" in model
        assert "protocol_candidates" in model
        assert isinstance(model["protocol_candidates"], list)


def test_get_models_with_real_data(client):
    """Test with actual resiliencebenchmark repository if available."""
    response = client.get("/api/v1/models")

    # If file doesn't exist, skip the test
    if response.status_code == 404:
        pytest.skip("Models configuration file not available in test environment")

    assert response.status_code == 200
    data = response.json()

    # Check for expected models
    model_ids = [model["id"] for model in data["models"]]

    # Should have common models
    if len(model_ids) >= 5:
        # Check for some expected models
        expected_models = ["gpt-5.5", "gpt-5.6", "deepseek-v4-pro", "claude-opus-5"]
        found_models = [m for m in expected_models if m in model_ids]
        assert len(found_models) > 0, f"Expected to find some models from {expected_models}"

        # Find a specific model and verify its structure
        gpt_model = next((m for m in data["models"] if m["id"].startswith("gpt")), None)

        if gpt_model:
            assert "display_name" in gpt_model
            assert "upstream_model" in gpt_model
            assert "protocol_candidates" in gpt_model
            assert len(gpt_model["protocol_candidates"]) > 0

            # Verify protocol candidates are strings
            for protocol in gpt_model["protocol_candidates"]:
                assert isinstance(protocol, str)

    # Verify credential refs
    assert "credential_refs" in data
    assert isinstance(data["credential_refs"], dict)

    if len(data["credential_refs"]) > 0:
        # Check structure of first credential ref
        first_ref = next(iter(data["credential_refs"].values()))
        assert "base_url" in first_ref
        assert "api_key" in first_ref
        assert "auth_scheme" in first_ref

    # Verify defaults
    assert "defaults" in data
    assert isinstance(data["defaults"], dict)


def test_models_parser_handles_missing_file(monkeypatch):
    """Test parser handles missing models.yaml gracefully."""
    from backend.parsers.models import parse_models

    # Use a non-existent path
    fake_path = Path("/nonexistent/path")

    result = parse_models(fake_path)

    # Should return None, not crash
    assert result is None


def test_models_parser_adds_default_credential_ref():
    """Test parser adds default credential ref to models."""
    from backend.parsers.models import parse_models
    from pathlib import Path

    # This should use the real models.yaml if available
    repo_path = Path(__file__).parent.parent.parent / "resiliencebenchmark"

    if not repo_path.exists():
        pytest.skip("Test repository not available")

    registry = parse_models(repo_path)

    if registry and len(registry.models) > 0:
        # At least some models should have a credential_ref
        models_with_creds = [m for m in registry.models if m.credential_ref]
        assert len(models_with_creds) > 0
