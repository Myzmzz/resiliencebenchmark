"""解析 ENVIRONMENT_STATUS.md 文件。"""

import re
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from backend.models.infrastructure import InfrastructureResource, ResourceStatus, ResourceType


def parse_environment_status(md_path: Path) -> List[InfrastructureResource]:
    """
    解析环境状态 Markdown 文件。

    识别四类资源表格:
    - Kubernetes Clusters -> type="kubernetes"
    - SSH Hosts -> type="ssh_host"
    - Image Registries -> type="registry"
    - Model Gateways -> type="model_gateway"
    """
    if not md_path.exists():
        return []

    content = md_path.read_text()
    if not content.strip():
        return []

    resources: List[InfrastructureResource] = []

    # 按章节切分
    sections = re.split(r'^## ', content, flags=re.MULTILINE)

    for section in sections:
        if not section.strip():
            continue

        lines = section.split('\n')
        title = lines[0].strip()

        # 映射标题到资源类型
        resource_type = _map_title_to_type(title)
        if not resource_type:
            continue

        # 解析表格
        table_rows = [line for line in lines[1:] if line.strip().startswith('|')]
        if len(table_rows) < 2:  # 需要至少有表头和分隔行
            continue

        header = _parse_table_row(table_rows[0])
        # 跳过分隔行 (table_rows[1])

        for row_line in table_rows[2:]:
            row = _parse_table_row(row_line)
            if not row or len(row) < 3:  # 至少需要 Name, Endpoint, Status
                continue

            resource = _parse_resource_row(resource_type, header, row)
            if resource:
                resources.append(resource)

    return resources


def _map_title_to_type(title: str) -> Optional[ResourceType]:
    """将章节标题映射到资源类型。"""
    title_lower = title.lower()
    if "kubernetes" in title_lower or "k8s" in title_lower:
        return "kubernetes"
    elif "ssh" in title_lower or "host" in title_lower:
        return "ssh_host"
    elif "registry" in title_lower or "registries" in title_lower:
        return "registry"
    elif "model" in title_lower and "gateway" in title_lower:
        return "model_gateway"
    return None


def _parse_table_row(line: str) -> List[str]:
    """解析 Markdown 表格行。"""
    # 去掉首尾的 | 并分割
    parts = line.strip().strip('|').split('|')
    return [p.strip() for p in parts]


def _parse_resource_row(
    resource_type: ResourceType,
    header: List[str],
    row: List[str]
) -> Optional[InfrastructureResource]:
    """从表格行构造资源对象。"""
    if len(row) < len(header):
        return None

    # 构建字段字典
    fields = dict(zip(header, row))

    name = fields.get("Name", "")
    endpoint = fields.get("Endpoint", "")
    status_str = fields.get("Status", "").lower()

    if not name or not endpoint:
        return None

    # 状态映射
    status: ResourceStatus = "pending"
    if status_str in {"qualified", "partial", "pending", "error"}:
        status = status_str  # type: ignore

    # 解析 metrics
    metrics: dict[str, int] = {}

    # Nodes (for kubernetes)
    if "Nodes" in fields:
        try:
            metrics["nodes"] = int(fields["Nodes"])
        except ValueError:
            pass

    # Acceptance (for ssh_host, format "18/20")
    if "Acceptance" in fields:
        acceptance = fields["Acceptance"]
        if '/' in acceptance:
            parts = acceptance.split('/')
            try:
                metrics["acceptance_pass"] = int(parts[0])
                metrics["acceptance_total"] = int(parts[1])
            except (ValueError, IndexError):
                pass

    # Projects (for registry)
    if "Projects" in fields:
        try:
            metrics["projects"] = int(fields["Projects"])
        except ValueError:
            pass

    # Models (for model_gateway)
    if "Models" in fields:
        try:
            metrics["models"] = int(fields["Models"])
        except ValueError:
            pass

    # Last Qualified (optional datetime)
    last_qualified = None
    if "Last Qualified" in fields:
        try:
            last_qualified = datetime.fromisoformat(fields["Last Qualified"].replace('Z', '+00:00'))
        except (ValueError, AttributeError):
            pass

    return InfrastructureResource(
        type=resource_type,
        name=name,
        status=status,
        endpoint=endpoint,
        metrics=metrics,
        last_qualified=last_qualified,
    )
