"""Settings 解析 BENCHMARK_REPO_PATH 环境变量的行为。"""

from pathlib import Path

import pytest

from backend.config import Settings, get_settings


@pytest.fixture(autouse=True)
def clear_settings_cache() -> None:
    """每个测试前清空 get_settings 的 lru_cache。"""
    get_settings.cache_clear()


def test_settings_reads_env_var(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """显式设置 BENCHMARK_REPO_PATH 时以其为准。"""
    monkeypatch.setenv("BENCHMARK_REPO_PATH", str(tmp_path))
    settings = get_settings()
    assert settings.repo_path == tmp_path


def test_settings_default_is_current_repository(monkeypatch: pytest.MonkeyPatch) -> None:
    """未设置环境变量时默认取 backend 所在仓库，且兼容 Git worktree 名称。"""
    monkeypatch.delenv("BENCHMARK_REPO_PATH", raising=False)
    settings = get_settings()
    assert settings.repo_path == Path(__file__).resolve().parents[1]
    assert settings.factory_config_path.is_file()


def test_factory_config_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """factory_config_path 指向 repo 下的 benchmarkfactory.yaml。"""
    monkeypatch.setenv("BENCHMARK_REPO_PATH", str(tmp_path))
    settings = Settings(repo_path=tmp_path)
    assert settings.factory_config_path == tmp_path / "benchmarkfactory.yaml"
