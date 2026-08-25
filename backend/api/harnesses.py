"""Harnesses API endpoints."""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from backend.config import Settings, get_settings
from backend.models.harness import HarnessesRegistry
from backend.parsers.harnesses import parse_harnesses

router = APIRouter(prefix="/harnesses", tags=["harnesses"])


@router.get("", response_model=HarnessesRegistry)
def get_harnesses(settings: Settings = Depends(get_settings)) -> HarnessesRegistry:
    """
    Get harnesses registry configuration.

    Returns:
        HarnessesRegistry from harness/harnesses.yaml
    """
    registry = parse_harnesses(settings.repo_path)

    if registry is None:
        raise HTTPException(
            status_code=404,
            detail="Harnesses configuration file not found at harness/harnesses.yaml"
        )

    return registry
