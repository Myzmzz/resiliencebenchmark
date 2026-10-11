"""测试实验环境 API 端点。"""

import os
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from backend.main import app


@pytest.fixture
def client():
    """测试客户端。"""
    return TestClient(app)


def test_get_experiment_no_kubeconfig(client):
    """未显式配置 Benchmark kubeconfig 时返回未配置，不伪造集群。"""
    with patch.dict(
        os.environ,
        {
            "KUBECONFIG": "/tmp/unrelated-cluster-config",
            "BENCHMARK_KUBECONFIG": "",
        },
        clear=False,
    ):
        response = client.get("/api/v1/experiments/environment")
        assert response.status_code == 200
        data = response.json()

        assert data["api_server"] == ""
        assert data["k8s_version"] == ""
        assert data["connection_status"] == "未配置"
        assert data["summary"] == {
            "node_count": 0,
            "namespace_count": 0,
            "pod_count": 0,
            "abnormal_pod_count": 0,
        }
