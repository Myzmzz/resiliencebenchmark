"""M2 里程碑端到端集成测试。"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.main import app


@pytest.fixture
def full_environment_setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """创建完整的测试环境,包含所有 4 种资源类型。"""
    repo_path = tmp_path / "resiliencebenchmark"
    repo_path.mkdir()

    env_status = repo_path / "ENVIRONMENT_STATUS.md"
    env_status.write_text("""
# Environment Status

## Kubernetes Clusters
| Name | Endpoint | Status | Nodes | Last Qualified |
|------|----------|--------|-------|----------------|
| tcse-v100 | /path/to/kubeconfig | qualified | 3 | 2025-01-15T10:30:00+08:00 |
| dev-cluster | https://dev.k8s.local | partial | 1 | 2025-01-10T08:00:00+08:00 |

## SSH Hosts
| Name | Endpoint | Status | Acceptance | Last Qualified |
|------|----------|--------|------------|----------------|
| MCP Host | env:RESBENCH_HARNESS_HOST | partial | 18/20 | 2025-01-14T12:00:00+08:00 |
| Test Host | ssh://test.example.com | pending | 0/15 | |

## Image Registries
| Name | Endpoint | Status | Projects | Last Qualified |
|------|----------|--------|----------|----------------|
| Harbor Main | harbor.example.com | qualified | 12 | 2025-01-13T09:00:00+08:00 |

## Model Gateways
| Name | Endpoint | Status | Models | Last Qualified |
|------|----------|--------|--------|----------------|
| OpenAI Gateway | https://api.openai.com | qualified | 5 | 2025-01-12T14:30:00+08:00 |
| Local LLM | http://localhost:8080 | error | 0 | |
""")

    monkeypatch.setenv("BENCHMARK_REPO_PATH", str(repo_path))

    from backend.config import get_settings
    get_settings.cache_clear()

    yield repo_path


def test_full_pipeline_all_resource_types(full_environment_setup: Path):
    """测试完整流程:文件 → 解析 → API → JSON 响应。"""
    client = TestClient(app)
    response = client.get("/api/v1/infrastructure/resources")

    assert response.status_code == 200
    data = response.json()

    # 验证资源总数
    assert len(data) == 7, "应该有 7 个资源(2 K8s + 2 SSH + 1 Registry + 2 Gateway)"

    # 验证每种资源类型
    types = [r["type"] for r in data]
    assert types.count("kubernetes") == 2
    assert types.count("ssh_host") == 2
    assert types.count("registry") == 1
    assert types.count("model_gateway") == 2

    # 验证 K8s 集群
    k8s = [r for r in data if r["type"] == "kubernetes"]
    tcse = next(r for r in k8s if r["name"] == "tcse-v100")
    assert tcse["status"] == "qualified"
    assert tcse["metrics"]["nodes"] == 3
    assert tcse["last_qualified"] == "2025-01-15T10:30:00+08:00"

    # 验证 SSH 主机
    ssh = [r for r in data if r["type"] == "ssh_host"]
    mcp = next(r for r in ssh if r["name"] == "MCP Host")
    assert mcp["status"] == "partial"
    assert mcp["metrics"]["acceptance_pass"] == 18
    assert mcp["metrics"]["acceptance_total"] == 20

    # 验证镜像仓库
    registries = [r for r in data if r["type"] == "registry"]
    harbor = registries[0]
    assert harbor["name"] == "Harbor Main"
    assert harbor["status"] == "qualified"
    assert harbor["metrics"]["projects"] == 12

    # 验证模型网关
    gateways = [r for r in data if r["type"] == "model_gateway"]
    openai = next(r for r in gateways if r["name"] == "OpenAI Gateway")
    assert openai["status"] == "qualified"
    assert openai["metrics"]["models"] == 5

    local_llm = next(r for r in gateways if r["name"] == "Local LLM")
    assert local_llm["status"] == "error"
    assert local_llm["last_qualified"] is None


def test_schema_validation(full_environment_setup: Path):
    """验证 API 响应符合 InfrastructureResource 模型规范。"""
    client = TestClient(app)
    response = client.get("/api/v1/infrastructure/resources")

    assert response.status_code == 200
    data = response.json()

    # 验证每个资源的必需字段
    for resource in data:
        assert "type" in resource
        assert "name" in resource
        assert "status" in resource
        assert "endpoint" in resource
        assert "metrics" in resource
        assert "last_qualified" in resource
        assert "details" in resource

        # 验证类型约束
        assert resource["type"] in ["kubernetes", "ssh_host", "registry", "model_gateway"]
        assert resource["status"] in ["qualified", "partial", "pending", "error"]
        assert isinstance(resource["metrics"], dict)
        assert isinstance(resource["name"], str)
        assert isinstance(resource["endpoint"], str)
