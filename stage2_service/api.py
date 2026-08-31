"""Single-process HTTP API and campaign supervisor."""

from __future__ import annotations

import asyncio
import inspect
import json
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from threading import Condition, Lock
from typing import Protocol

from fastapi import FastAPI, HTTPException, status
from fastapi.responses import FileResponse, StreamingResponse

from .contracts import (
    CampaignRequest,
    CampaignResult,
    CaseBundle,
    CaseBundleGenerationRequest,
    PlatformStatus,
    default_case_specs,
)


class CampaignRunner(Protocol):
    def run(self, request: CampaignRequest, event_observer=None) -> CampaignResult: ...


class CampaignSupervisor:
    def __init__(self, runner: CampaignRunner):
        self.runner = runner
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="stage2-campaign")
        self.lock = Lock()
        self.condition = Condition(self.lock)
        self.futures: dict[str, Future[CampaignResult]] = {}
        self.events: dict[str, list[dict]] = {}
        self.interactions: dict[str, list[dict]] = {}
        self.stop_requests: set[str] = set()

    def submit(self, request: CampaignRequest) -> str:
        with self.lock:
            existing = self.futures.get(request.request_id)
            if existing is not None:
                return request.request_id
            if any(not future.done() for future in self.futures.values()):
                raise RuntimeError("one Stage-2 campaign is already active")
            self.events[request.request_id] = []
            self.interactions[request.request_id] = []
            self.futures[request.request_id] = self.pool.submit(
                self._run_request, request
            )
            return request.request_id

    def _run_request(self, request: CampaignRequest) -> CampaignResult:
        def observe(event) -> None:
            payload = dict(event) if isinstance(event, dict) else {"event": str(event)}
            self.append_event(request.request_id, payload)

        try:
            parameters = inspect.signature(self.runner.run).parameters
            if "event_observer" in parameters:
                return self.runner.run(request, event_observer=observe)
            return self.runner.run(request)
        except TypeError:
            return self.runner.run(request)

    def append_event(self, request_id: str, event: dict) -> None:
        with self.condition:
            event = {"sequence": len(self.events.setdefault(request_id, [])), **event}
            self.events[request_id].append(event)
            self.condition.notify_all()

    def get(self, request_id: str) -> dict:
        with self.lock:
            future = self.futures.get(request_id)
            events = list(self.events.get(request_id, ()))
        if future is None:
            raise KeyError(request_id)
        if not future.done():
            return {
                "request_id": request_id,
                "status": "RUNNING",
                "events": events[-100:],
                "stop_requested": request_id in self.stop_requests,
            }
        result = future.result()
        return {
            "request_id": request_id,
            "status": result.platform_status.value,
            "result": result.model_dump(mode="json"),
            "events": events[-200:],
        }

    def request_stop(self, request_id: str) -> dict:
        with self.condition:
            if request_id not in self.futures:
                raise KeyError(request_id)
            self.stop_requests.add(request_id)
            event = {
                "sequence": len(self.events.setdefault(request_id, [])),
                "kind": "operator_stop_requested",
                "request_id": request_id,
                "payload": {"requested": True},
            }
            self.events[request_id].append(event)
            future = self.futures[request_id]
            cancelled = future.cancel()
            self.condition.notify_all()
        return {"request_id": request_id, "stop_requested": True, "cancelled": cancelled}

    def add_interaction(self, request_id: str, message: str) -> dict:
        with self.condition:
            if request_id not in self.futures:
                raise KeyError(request_id)
            item = {
                "kind": "operator_interaction",
                "request_id": request_id,
                "message": message,
            }
            self.interactions.setdefault(request_id, []).append(item)
            event = {
                "sequence": len(self.events.setdefault(request_id, [])),
                "kind": "operator_interaction_queued",
                "request_id": request_id,
                "payload": {"message": message},
            }
            self.events[request_id].append(event)
            self.condition.notify_all()
        return {"request_id": request_id, "queued": True}

    def wait_events(self, request_id: str, after: int) -> list[dict]:
        with self.condition:
            if request_id not in self.futures and request_id not in self.events:
                raise KeyError(request_id)
            current = list(self.events.get(request_id, ()))
            if len(current) <= after:
                self.condition.wait(timeout=2.0)
                current = list(self.events.get(request_id, ()))
            return current[after:]

    def terminal_status(self, request_id: str) -> str | None:
        with self.lock:
            future = self.futures.get(request_id)
        if future is None or not future.done():
            return None
        return future.result().platform_status.value


def create_app(
    supervisor: CampaignSupervisor,
    *,
    artifact_root: Path | None = None,
) -> FastAPI:
    app = FastAPI(title="Resilience Benchmark Stage-2 Service", docs_url="/api/docs")

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/v1/case-bundles")
    def generate_case_bundle(request: CaseBundleGenerationRequest) -> dict:
        bundle = CaseBundle(
            bundle_id=request.bundle_id,
            base_prompt=request.prompt,
            cases=default_case_specs(request.cases),
        )
        return bundle.model_dump(mode="json")

    @app.get("/api/v1/preflight")
    def preflight() -> dict:
        return {
            "status": "READY_TO_CHECK",
            "harnesses": ["codex"],
            "model": "gpt-5.6-sol",
            "cases": [item.model_dump(mode="json") for item in default_case_specs()],
            "mcp_servers": ["k8s_ro", "telemetry_ro", "source_ro", "chaos_control"],
            "rbac": {
                "trial_token_rotation": True,
                "observability_revoke": ["mcp.k8s.read", "mcp.telemetry.read"],
                "chaos_revoke": ["mcp.chaos.create"],
            },
            "chaosblade": {"executor": "chaos_control", "execute_enabled_required": True},
        }

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

    @app.get("/api/v1/campaigns/{request_id}/events")
    async def campaign_events(request_id: str, after: int = 0):
        async def stream():
            cursor = max(after, 0)
            while True:
                try:
                    events = supervisor.wait_events(request_id, cursor)
                except KeyError:
                    yield _sse("error", {"detail": "campaign not found"})
                    return
                for item in events:
                    cursor = int(item.get("sequence", cursor)) + 1
                    yield _sse("event", item)
                terminal = supervisor.terminal_status(request_id)
                if terminal is not None:
                    yield _sse("terminal", {"request_id": request_id, "status": terminal})
                    return
                await asyncio.sleep(0.25)

        return StreamingResponse(stream(), media_type="text/event-stream")

    @app.post("/api/v1/campaigns/{request_id}/stop")
    def stop_campaign(request_id: str) -> dict:
        try:
            return supervisor.request_stop(request_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="campaign not found") from exc

    @app.post("/api/v1/campaigns/{request_id}/interactions")
    def queue_interaction(request_id: str, payload: dict) -> dict:
        message = str(payload.get("message") or "").strip()
        if not message:
            raise HTTPException(status_code=422, detail="message is required")
        try:
            return supervisor.add_interaction(request_id, message)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="campaign not found") from exc

    @app.get("/api/v1/artifacts/{campaign_id}/{artifact_path:path}")
    def download_artifact(campaign_id: str, artifact_path: str):
        if artifact_root is None:
            raise HTTPException(status_code=404, detail="artifact root is not configured")
        if not campaign_id.startswith("campaign-"):
            raise HTTPException(status_code=400, detail="invalid campaign id")
        root = artifact_root.resolve() / campaign_id
        path = (root / artifact_path).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="artifact path escaped campaign root") from exc
        if not path.is_file():
            raise HTTPException(status_code=404, detail="artifact not found")
        return FileResponse(path)

    return app


def _sse(event: str, payload: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
