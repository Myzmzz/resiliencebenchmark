"""Episodes API endpoints."""

from typing import List

from fastapi import APIRouter, Depends

from backend.config import Settings, get_settings
from backend.models.episode import Episode
from backend.parsers.episodes import parse_episodes

router = APIRouter(prefix="/episodes", tags=["episodes"])


@router.get("", response_model=List[Episode])
def get_episodes(settings: Settings = Depends(get_settings)) -> List[Episode]:
    """
    Get all episode (evaluation unit) configurations.

    Returns:
        List of episodes from tasks/examples/
    """
    return parse_episodes(settings.repo_path)
