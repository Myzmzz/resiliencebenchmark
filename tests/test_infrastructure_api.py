"""测试基础设施资源 API 端点。"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.main import app


@pytest.fixture
def mock_environment_status(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """创建模拟的 ENVIRONMENT_STATUS.md 文件。"""
    repo_path = tmp_path / "resiliencebenchmark"
    repo_path.mkdir()

    env_status = repo_path / "ENVIRONMENT_STATUS.md"
    env_status.write_text("""
## Kubernetes Clusters
| Name | Endpoint | Status | Nodes |
|------|----------|--------|-------|
| tcse-v100 | /path/to/kubeconfig | qualified | 3 |

## SSH Hosts
| Name | Endpoint | Status | Acceptance |
|------|----------|--------|------------|
| MCP Host | env:RESBENCH_HARNESS_HOST | partial | 18/20 |
""")

    monkeypatch.setenv("BENCHMARK_REPO_PATH", str(repo_path))

    # Clear settings cache to pick up new env var
    from backend.config import get_settings
    get_settings.cache_clear()

    yield repo_path


def test_get_infrastructure_resources(mock_environment_status: Path):
    """测试获取基础设施资源列表。"""
    client = TestClient(app)
    response = client.get("/api/v1/infrastructure/resources")

    assert response.status_code == 200
    data = response.json()

    assert len(data) == 2
    assert data[0]["type"] == "kubernetes"
    assert data[0]["name"] == "tcse-v100"
    assert data[0]["status"] == "qualified"
    assert data[0]["metrics"]["nodes"] == 3

    assert data[1]["type"] == "ssh_host"
    assert data[1]["name"] == "MCP Host"
    assert data[1]["metrics"]["acceptance_pass"] == 18


def test_get_infrastructure_resources_missing_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """测试 ENVIRONMENT_STATUS.md 不存在时返回空列表。"""
    repo_path = tmp_path / "resiliencebenchmark"
    repo_path.mkdir()

    monkeypatch.setenv("BENCHMARK_REPO_PATH", str(repo_path))

    from backend.config import get_settings
    get_settings.cache_clear()

    client = TestClient(app)
    response = client.get("/api/v1/infrastructure/resources")

    assert response.status_code == 200
    assert response.json() == []
