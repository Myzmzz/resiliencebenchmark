"""Models API endpoints."""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from backend.config import Settings, get_settings
from backend.models.model_config import ModelsRegistry
from backend.parsers.models import parse_models

router = APIRouter(prefix="/models", tags=["models"])


@router.get("", response_model=ModelsRegistry)
def get_models(settings: Settings = Depends(get_settings)) -> ModelsRegistry:
    """
    Get models registry configuration.

    Returns:
        ModelsRegistry from harness/models.yaml
    """
    registry = parse_models(settings.repo_path)

    if registry is None:
        raise HTTPException(
            status_code=404,
            detail="Models configuration file not found at harness/models.yaml"
        )

    return registry
