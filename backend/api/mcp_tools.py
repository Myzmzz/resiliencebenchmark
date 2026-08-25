"""MCP tools API endpoints."""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from backend.config import Settings, get_settings
from backend.models.mcp_tools import MCPToolsRegistry
from backend.parsers.mcp_tools import parse_mcp_tools

router = APIRouter(prefix="/mcp-tools", tags=["mcp"])


@router.get("", response_model=MCPToolsRegistry)
def get_mcp_tools(settings: Settings = Depends(get_settings)) -> MCPToolsRegistry:
    """
    Get MCP tools registry configuration.

    Returns:
        MCPToolsRegistry from harness/mcp-tools.yaml
    """
    registry = parse_mcp_tools(settings.repo_path)

    if registry is None:
        raise HTTPException(
            status_code=404,
            detail="MCP tools configuration file not found at harness/mcp-tools.yaml"
        )

    return registry
