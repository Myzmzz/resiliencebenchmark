from datetime import UTC, datetime

from backend.models.infrastructure import InfrastructureResource, ResourceStatus, ResourceType


def test_infrastructure_resource_kubernetes():
    resource = InfrastructureResource(
        type="kubernetes",
        name="tcse-v100",
        status="qualified",
        endpoint="/path/to/kubeconfig",
        metrics={"nodes": 3, "containers": 101},
        last_qualified=datetime.now(UTC),
        details={"nodes": ["tcse-v100-01", "tcse-v100-02", "tcse-v100-03"]},
    )
    assert resource.type == "kubernetes"
    assert resource.status == "qualified"
    assert resource.metrics["nodes"] == 3


def test_infrastructure_resource_ssh_host():
    resource = InfrastructureResource(
        type="ssh_host",
        name="MCP Host",
        status="partial",
        endpoint="env:RESBENCH_HARNESS_HOST",
        metrics={"acceptance_pass": 18, "acceptance_total": 20},
    )
    assert resource.metrics["acceptance_pass"] == 18


def test_infrastructure_resource_registry():
    resource = InfrastructureResource(
        type="registry",
        name="Harbor",
        status="qualified",
        endpoint="https://harbor.example.com",
        metrics={"projects": 3, "containers": 101},
    )
    assert resource.type == "registry"


def test_infrastructure_resource_model_gateway():
    resource = InfrastructureResource(
        type="model_gateway",
        name="模型网关",
        status="pending",
        endpoint="default_openai_compatible",
        metrics={"models": 7},
    )
    assert resource.metrics["models"] == 7
