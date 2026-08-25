"""Integration tests for episodes API endpoint."""

import pytest
from pathlib import Path
from fastapi.testclient import TestClient

from backend.main import app


@pytest.fixture
def client():
    """Create test client."""
    return TestClient(app)


def test_get_episodes_success(client):
    """Test successful retrieval of episodes."""
    response = client.get("/api/v1/episodes")

    assert response.status_code == 200
    data = response.json()

    # Should return a list
    assert isinstance(data, list)

    # If episodes exist, verify structure
    if len(data) > 0:
        episode = data[0]
        assert "episode_id" in episode
        assert "title" in episode
        assert "status" in episode
        assert "application" in episode
        assert "agent_goal" in episode
        assert "budget" in episode


def test_get_episodes_with_real_data(client):
    """Test with actual resiliencebenchmark repository if available."""
    response = client.get("/api/v1/episodes")

    assert response.status_code == 200
    data = response.json()

    assert isinstance(data, list)

    # If episodes exist, check structure
    if len(data) > 0:
        episode = data[0]

        # Verify basic fields
        assert "episode_id" in episode
        assert "title" in episode
        assert "status" in episode

        # Verify application config
        assert "application" in episode
        app_config = episode["application"]
        assert "name" in app_config
        assert "namespace" in app_config
        assert "candidate_services" in app_config
        assert isinstance(app_config["candidate_services"], list)

        # Verify budget
        assert "budget" in episode
        budget = episode["budget"]
        assert "max_experiments" in budget
        assert "max_duration_minutes" in budget
        assert "max_concurrent_faults" in budget

        # Verify observability
        assert "observability" in episode
        obs = episode["observability"]
        assert "metrics" in obs
        assert "traces" in obs
        assert "logs" in obs
        assert "kubernetes" in obs

        # Verify source access
        assert "source_access" in episode
        src = episode["source_access"]
        assert "mode" in src
        assert "allowed_paths" in src
        assert "forbidden_paths" in src

        # Verify action space
        assert "action_space" in episode
        action = episode["action_space"]
        assert "allowed_trigger_classes" in action
        assert "allowed_target_scope" in action
        assert "forbidden_actions" in action


def test_episodes_parser_handles_missing_directory(monkeypatch):
    """Test parser handles missing episodes directory gracefully."""
    from backend.parsers.episodes import parse_episodes

    # Use a non-existent path
    fake_path = Path("/nonexistent/path")

    result = parse_episodes(fake_path)

    # Should return empty list, not crash
    assert result == []


def test_episodes_empty_directory():
    """Test parser handles empty directory."""
    from backend.parsers.episodes import parse_episodes
    import tempfile

    # Create a temporary directory
    with tempfile.TemporaryDirectory() as tmpdir:
        result = parse_episodes(Path(tmpdir))
        assert result == []
