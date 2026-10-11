"""第一阶段核心功能测试工作台 API。"""

from functools import lru_cache
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import ValidationError

from backend.config import Settings, get_settings
from backend.models.phase1 import (
    CoreFeatureCatalog,
    CoreTestPreview,
    CoreTestRun,
    PreviewRequest,
    StartRunRequest,
)
from backend.services.phase1 import Phase1CoreService

router = APIRouter(prefix="/phase1/core", tags=["phase1-core"])


@lru_cache
def _service_for_repo(repo_path: str) -> Phase1CoreService:
    return Phase1CoreService(Path(repo_path))


def get_phase1_core_service(
    settings: Settings = Depends(get_settings),
) -> Phase1CoreService:
    return _service_for_repo(str(settings.repo_path.resolve()))


@router.get("/features", response_model=CoreFeatureCatalog)
def get_core_features(
    service: Phase1CoreService = Depends(get_phase1_core_service),
) -> CoreFeatureCatalog:
    return service.catalog()


@router.post(
    "/features/{feature_id}/preview",
    response_model=CoreTestPreview,
)
def preview_core_feature(
    feature_id: str,
    request: PreviewRequest,
    service: Phase1CoreService = Depends(get_phase1_core_service),
) -> CoreTestPreview:
    try:
        return service.preview(feature_id, request.inputs)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"unknown core feature: {feature_id}") from exc
    except (ValidationError, ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post(
    "/previews/{preview_id}/start",
    response_model=CoreTestRun,
    status_code=status.HTTP_202_ACCEPTED,
)
def start_core_run(
    preview_id: str,
    request: StartRunRequest,
    service: Phase1CoreService = Depends(get_phase1_core_service),
) -> CoreTestRun:
    try:
        return service.start(
            preview_id,
            confirmed=request.confirmed,
            input_digest=request.input_digest,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="preview not found") from exc
    except PermissionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (TimeoutError, RuntimeError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/runs/{run_id}", response_model=CoreTestRun)
def get_core_run(
    run_id: str,
    service: Phase1CoreService = Depends(get_phase1_core_service),
) -> CoreTestRun:
    try:
        return service.get_run(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="run not found") from exc
