"""健康检查端点：服务存活 + 仓库路径有效性。"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.config import get_settings
from backend.main import create_app


@pytest.fixture(autouse=True)
def clear_settings_cache() -> None:
    """每个测试前清空 get_settings 的 lru_cache。"""
    get_settings.cache_clear()


def _client_with_repo(monkeypatch: pytest.MonkeyPatch, repo: Path) -> TestClient:
    monkeypatch.setenv("BENCHMARK_REPO_PATH", str(repo))
    return TestClient(create_app())


def test_health_with_valid_repo(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """仓库存在且含 benchmarkfactory.yaml 时全部为 true。"""
    (tmp_path / "benchmarkfactory.yaml").write_text("kind: BenchmarkFactoryConfig\n")
    response = _client_with_repo(monkeypatch, tmp_path).get("/api/v1/meta/health")
    assert response.status_code == 200
    body = response.json()
    assert body["service"] == "ok"
    assert body["repo"]["exists"] is True
    assert body["repo"]["factory_config_found"] is True


def test_health_with_missing_repo(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """仓库路径无效时仍返回 200，由标志位表达异常（前端据此引导）。"""
    response = _client_with_repo(monkeypatch, tmp_path / "nope").get("/api/v1/meta/health")
    assert response.status_code == 200
    body = response.json()
    assert body["repo"]["exists"] is False
    assert body["repo"]["factory_config_found"] is False
