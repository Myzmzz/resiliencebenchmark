"""Single-process HTTP API and campaign supervisor."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from threading import Lock
from typing import Protocol

from fastapi import FastAPI, HTTPException, status

from .contracts import CampaignRequest, CampaignResult


class CampaignRunner(Protocol):
    def run(self, request: CampaignRequest) -> CampaignResult: ...


class CampaignSupervisor:
    def __init__(self, runner: CampaignRunner):
        self.runner = runner
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="stage2-campaign")
        self.lock = Lock()
        self.futures: dict[str, Future[CampaignResult]] = {}

    def submit(self, request: CampaignRequest) -> str:
        with self.lock:
            existing = self.futures.get(request.request_id)
            if existing is not None:
                return request.request_id
            if any(not future.done() for future in self.futures.values()):
                raise RuntimeError("one Stage-2 campaign is already active")
            self.futures[request.request_id] = self.pool.submit(self.runner.run, request)
            return request.request_id

    def get(self, request_id: str) -> dict:
        with self.lock:
            future = self.futures.get(request_id)
        if future is None:
            raise KeyError(request_id)
        if not future.done():
            return {"request_id": request_id, "status": "RUNNING"}
        result = future.result()
        return {
            "request_id": request_id,
            "status": result.platform_status.value,
            "result": result.model_dump(mode="json"),
        }


def create_app(supervisor: CampaignSupervisor) -> FastAPI:
    app = FastAPI(title="Resilience Benchmark Stage-2 Service", docs_url="/api/docs")

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/v1/campaigns", status_code=status.HTTP_202_ACCEPTED)
    def create_campaign(request: CampaignRequest) -> dict[str, str]:
        try:
            request_id = supervisor.submit(request)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"request_id": request_id, "status": "ACCEPTED"}

    @app.get("/api/v1/campaigns/{request_id}")
    def get_campaign(request_id: str) -> dict:
        try:
            return supervisor.get(request_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="campaign not found") from exc

    return app
