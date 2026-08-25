"""测试 ENVIRONMENT_STATUS.md 解析器。"""

from pathlib import Path

from backend.parsers.environment_status import parse_environment_status


def test_parse_kubernetes_clusters(tmp_path: Path):
    """解析 Kubernetes 集群状态表。"""
    content = """
## Kubernetes Clusters
| Name | Endpoint | Status | Nodes | Last Qualified |
|------|----------|--------|-------|----------------|
| tcse-v100 | /path/to/kubeconfig | qualified | 3 | 2026-08-20T10:00:00Z |
"""
    md_file = tmp_path / "ENVIRONMENT_STATUS.md"
    md_file.write_text(content)

    resources = parse_environment_status(md_file)

    assert len(resources) == 1
    assert resources[0].type == "kubernetes"
    assert resources[0].name == "tcse-v100"
    assert resources[0].status == "qualified"
    assert resources[0].metrics["nodes"] == 3


def test_parse_ssh_hosts(tmp_path: Path):
    """解析 SSH 主机状态表。"""
    content = """
## SSH Hosts
| Name | Endpoint | Status | Acceptance |
|------|----------|--------|------------|
| MCP Host | env:RESBENCH_HARNESS_HOST | partial | 18/20 |
"""
    md_file = tmp_path / "ENVIRONMENT_STATUS.md"
    md_file.write_text(content)

    resources = parse_environment_status(md_file)

    assert len(resources) == 1
    assert resources[0].type == "ssh_host"
    assert resources[0].metrics["acceptance_pass"] == 18
    assert resources[0].metrics["acceptance_total"] == 20


def test_parse_all_resource_types(tmp_path: Path):
    """解析所有资源类型的综合测试。"""
    content = """
## Kubernetes Clusters
| Name | Endpoint | Status | Nodes |
|------|----------|--------|-------|
| tcse-v100 | /path/to/kubeconfig | qualified | 3 |

## SSH Hosts
| Name | Endpoint | Status | Acceptance |
|------|----------|--------|------------|
| MCP Host | env:RESBENCH_HARNESS_HOST | partial | 18/20 |

## Image Registries
| Name | Endpoint | Status | Projects |
|------|----------|--------|----------|
| Harbor | https://harbor.example.com | qualified | 3 |

## Model Gateways
| Name | Endpoint | Status | Models |
|------|----------|--------|--------|
| 模型网关 | default_openai_compatible | pending | 7 |
"""
    md_file = tmp_path / "ENVIRONMENT_STATUS.md"
    md_file.write_text(content)

    resources = parse_environment_status(md_file)

    assert len(resources) == 4
    types = {r.type for r in resources}
    assert types == {"kubernetes", "ssh_host", "registry", "model_gateway"}


def test_parse_empty_file(tmp_path: Path):
    """空文件返回空列表。"""
    md_file = tmp_path / "ENVIRONMENT_STATUS.md"
    md_file.write_text("")

    resources = parse_environment_status(md_file)
    assert resources == []
