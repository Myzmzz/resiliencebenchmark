"""Integration tests for harnesses API endpoint."""

import pytest
from pathlib import Path
from fastapi.testclient import TestClient

from backend.main import app


@pytest.fixture
def client():
    """Create test client."""
    return TestClient(app)


def test_get_harnesses_success(client):
    """Test successful retrieval of harnesses registry."""
    response = client.get("/api/v1/harnesses")

    # If file doesn't exist, expect 404
    if response.status_code == 404:
        pytest.skip("Harnesses configuration file not available in test environment")

    assert response.status_code == 200
    data = response.json()

    # Verify structure
    assert "version" in data
    assert "description" in data
    assert "shared" in data
    assert "harnesses" in data

    # Verify harnesses list
    assert isinstance(data["harnesses"], list)

    if len(data["harnesses"]) > 0:
        harness = data["harnesses"][0]
        assert "id" in harness
        assert "kind" in harness
        assert "status" in harness
        assert "entrypoint" in harness
        assert "models" in harness
        assert "safety" in harness


def test_get_harnesses_with_real_data(client):
    """Test with actual resiliencebenchmark repository if available."""
    response = client.get("/api/v1/harnesses")

    # If file doesn't exist, skip the test
    if response.status_code == 404:
        pytest.skip("Harnesses configuration file not available in test environment")

    assert response.status_code == 200
    data = response.json()

    # Check for expected harnesses
    harness_ids = [h["id"] for h in data["harnesses"]]

    # Should have common harnesses
    if len(harness_ids) >= 3:
        expected_harnesses = ["bladeai", "claude-code", "codex", "deepseek-harness"]
        found_harnesses = [h for h in expected_harnesses if h in harness_ids]
        assert len(found_harnesses) > 0, f"Expected to find some harnesses from {expected_harnesses}"

        # Find a specific harness and verify its structure
        claude_harness = next((h for h in data["harnesses"] if h["id"] == "claude-code"), None)

        if claude_harness:
            assert claude_harness["kind"] == "claude_code"
            assert "status" in claude_harness
            assert "entrypoint" in claude_harness
            assert "mode" in claude_harness["entrypoint"]
            assert "command" in claude_harness["entrypoint"]
            assert claude_harness["entrypoint"]["command"] == "claude"

            # Verify models config
            assert "models" in claude_harness
            assert "source" in claude_harness["models"]
            assert claude_harness["models"]["source"] == "models.yaml"

            # Verify safety config
            assert "safety" in claude_harness
            assert isinstance(claude_harness["safety"], dict)


def test_harnesses_parser_handles_missing_file(monkeypatch):
    """Test parser handles missing harnesses.yaml gracefully."""
    from backend.parsers.harnesses import parse_harnesses

    # Use a non-existent path
    fake_path = Path("/nonexistent/path")

    result = parse_harnesses(fake_path)

    # Should return None, not crash
    assert result is None


def test_harnesses_api_model_choices_match_stage2_gateway(client):
    """The management API must not advertise stale or unrouted model choices."""
    from stage2_service.contracts import (
        HarnessKind,
        STAGE2_DEFAULT_MODEL,
        STAGE2_SUPPORTED_MODELS,
    )

    response = client.get("/api/v1/harnesses")
    assert response.status_code == 200
    harnesses = response.json()["harnesses"]
    assert {harness["id"] for harness in harnesses} == {
        harness.value for harness in HarnessKind
    }
    for harness in harnesses:
        models = harness["models"]
        assert set(models["candidate_aliases_requiring_probe"]) == set(
            STAGE2_SUPPORTED_MODELS
        )
        expected_default = (
            "claude-opus-5"
            if harness["id"] == HarnessKind.CLAUDE_CODE.value
            else STAGE2_DEFAULT_MODEL
        )
        assert models["default_alias"] == expected_default
