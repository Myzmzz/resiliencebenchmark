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
    """测试无 KUBECONFIG 时返回模拟数据。"""
    with patch.dict(os.environ, {"KUBECONFIG": ""}, clear=False):
        response = client.get("/api/v1/experiments/environment")
        assert response.status_code == 200
        data = response.json()

        # 应该返回模拟数据
        assert data["api_server"] == "https://10.0.0.12:6443"
        assert data["k8s_version"] == "v1.29.3"
        assert data["connection_status"] == "连接正常"
        assert data["summary"]["node_count"] == 6
        assert data["summary"]["namespace_count"] == 12
        assert data["summary"]["pod_count"] == 86
        assert data["summary"]["abnormal_pod_count"] == 3
