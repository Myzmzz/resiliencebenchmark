"""Stage-2 disturbance console API."""

from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException, Query, Response, status

from backend.config import get_settings
from stage2_service.console_contracts import (
    EvidenceBundle,
    GenerateBundleRequest,
    InteractionRequest,
    PreflightStatus,
    StartRunRequest,
)
from stage2_service.console_runtime import ConsoleRuntimeError, Stage2ConsoleRuntime


router = APIRouter(prefix="/stage2-console", tags=["stage2-console"])

_runtime: Stage2ConsoleRuntime | None = None


def get_runtime() -> Stage2ConsoleRuntime:
    global _runtime
    if _runtime is None:
        repo_root = get_settings().repo_path
        _runtime = Stage2ConsoleRuntime(
            repo_root=repo_root,
            allow_deterministic=os.environ.get("STAGE2_CONSOLE_ALLOW_DETERMINISTIC") == "1",
        )
    return _runtime


@router.post("/bundles")
def generate_bundle(request: GenerateBundleRequest):
    return get_runtime().generate_bundle(request.prompt)


@router.get("/preflight")
def preflight() -> PreflightStatus:
    return get_runtime().preflight()


@router.post("/runs", status_code=status.HTTP_202_ACCEPTED)
def start_run(request: StartRunRequest):
    try:
        return get_runtime().start(request)
    except ConsoleRuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/runs/{run_id}")
def get_run(run_id: str):
    try:
        return get_runtime().get_run(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="run not found") from exc


@router.get("/runs/{run_id}/events")
def get_events(run_id: str, after: int = Query(default=0, ge=0)):
    try:
        return {"events": get_runtime().events(run_id, after=after)}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="run not found") from exc


@router.post("/runs/{run_id}/stop")
def stop_run(run_id: str, reason: str = "operator requested stop"):
    try:
        return get_runtime().stop(run_id, reason)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="run not found") from exc


@router.post("/runs/{run_id}/cleanup")
def cleanup_run(run_id: str):
    try:
        return get_runtime().cleanup(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="run not found") from exc


@router.post("/runs/{run_id}/interactions")
def interact(run_id: str, request: InteractionRequest):
    try:
        return get_runtime().interact(run_id, request.message)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="run not found") from exc


@router.get("/runs/{run_id}/evidence/items")
def evidence_items(run_id: str):
    try:
        return {"items": get_runtime().evidence_items(run_id)}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="run not found") from exc


@router.get("/runs/{run_id}/evidence")
def evidence(run_id: str) -> EvidenceBundle:
    try:
        return get_runtime().evidence(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="run not found") from exc


@router.get("/runs/{run_id}/evidence/download")
def download_evidence(run_id: str) -> Response:
    try:
        payload = get_runtime().evidence(run_id).model_dump_json(indent=2)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="run not found") from exc
    return Response(
        content=payload,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{run_id}-evidence.json"'},
    )
