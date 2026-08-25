"""Applications API endpoints."""

from typing import List

from fastapi import APIRouter, Depends

from backend.config import Settings, get_settings
from backend.models.application import Application
from backend.parsers.applications import parse_applications

router = APIRouter(prefix="/applications", tags=["applications"])


@router.get("", response_model=List[Application])
def get_applications(settings: Settings = Depends(get_settings)) -> List[Application]:
    """
    Get all application environments.

    Returns:
        List of applications from environment/applications/*.yaml
    """
    return parse_applications(settings.repo_path)
