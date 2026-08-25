"""Parser for mcp-tools.yaml configuration."""

from pathlib import Path
from typing import Optional

import yaml

from backend.models.mcp_tools import (
    MCPTool,
    MCPToolsRegistry,
    NotExposedToAgent,
    ToolGates,
    ToolScope,
)


def parse_mcp_tools(repo_path: Path) -> Optional[MCPToolsRegistry]:
    """
    Parse mcp-tools.yaml configuration.

    Args:
        repo_path: Path to the resiliencebenchmark repository

    Returns:
        MCPToolsRegistry object or None if file doesn't exist
    """
    mcp_tools_file = repo_path / "harness" / "mcp-tools.yaml"

    if not mcp_tools_file.exists():
        return None

    try:
        with open(mcp_tools_file, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        if not data:
            return None

        # Parse tools
        tools = []
        for tool_id, tool_data in data.get("tools", {}).items():
            tool_data_with_id = {"id": tool_id, **tool_data}

            # Parse nested scope
            if "scope" in tool_data:
                tool_data_with_id["scope"] = ToolScope(**tool_data["scope"])

            # Parse nested gates
            if "gates" in tool_data:
                tool_data_with_id["gates"] = ToolGates(**tool_data["gates"])

            tools.append(MCPTool(**tool_data_with_id))

        # Parse not_exposed_to_agent
        not_exposed = None
        if "not_exposed_to_agent" in data:
            not_exposed_data = data["not_exposed_to_agent"]
            # Handle oracle or other keys
            if isinstance(not_exposed_data, dict):
                for key, value in not_exposed_data.items():
                    if isinstance(value, dict):
                        not_exposed = NotExposedToAgent(**value)
                        break

        return MCPToolsRegistry(
            version=data.get("version", "unknown"),
            description=data.get("description", ""),
            runtime_refs=data.get("runtime_refs", {}),
            tools=tools,
            not_exposed_to_agent=not_exposed,
        )

    except Exception as e:
        print(f"Error parsing mcp-tools.yaml: {e}")
        return None
