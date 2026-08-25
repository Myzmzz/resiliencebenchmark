"""Observability API endpoints."""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from backend.config import Settings, get_settings
from backend.models.observability import ObservabilityStack
from backend.parsers.observability import parse_observability

router = APIRouter(prefix="/observability", tags=["observability"])


@router.get("", response_model=ObservabilityStack)
def get_observability(settings: Settings = Depends(get_settings)) -> ObservabilityStack:
    """
    Get observability stack configuration.

    Returns:
        ObservabilityStack from environment/shared/observability.yaml
    """
    stack = parse_observability(settings.repo_path)

    if stack is None:
        raise HTTPException(
            status_code=404,
            detail="Observability configuration file not found at environment/shared/observability.yaml"
        )

    return stack
