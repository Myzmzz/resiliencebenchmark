"""Single-process HTTP API and campaign supervisor."""

from __future__ import annotations

import asyncio
import inspect
import json
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from threading import Condition, Event, Lock
from typing import Protocol

from fastapi import FastAPI, Header, HTTPException, Query, status
from fastapi.responses import FileResponse, StreamingResponse

from .contracts import (
    STAGE2_MODEL_MATRIX,
    CampaignRequest,
    CampaignResult,
    CaseBundle,
    CaseBundleGenerationRequest,
    PlatformStatus,
    default_case_specs,
)
from .matrix_evidence import MatrixEvidenceNotFound, MatrixEvidenceStore
from .task_service import (
    AbortTaskRequest,
    EnvironmentResetRequest,
    PermissionRestoreRequest,
    Stage2TaskCreateRequest,
    Stage2TaskService,
    TaskDetailMode,
    TaskConflict,
    TaskNotFound,
    TaskValidationError,
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
        self.stop_events: dict[str, Event] = {}
        self.event_sinks: dict[str, object] = {}
        self.result_sinks: dict[str, object] = {}

    def submit(self, request: CampaignRequest, *, event_sink=None, result_sink=None) -> str:
        with self.lock:
            existing = self.futures.get(request.request_id)
            if existing is not None:
                return request.request_id
            if any(not future.done() for future in self.futures.values()):
                raise RuntimeError("one Stage-2 campaign is already active")
            self.events[request.request_id] = []
            self.interactions[request.request_id] = []
            self.stop_events[request.request_id] = Event()
            if event_sink is not None:
                self.event_sinks[request.request_id] = event_sink
            if result_sink is not None:
                self.result_sinks[request.request_id] = result_sink
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
            kwargs = {}
            if "event_observer" in parameters:
                kwargs["event_observer"] = observe
            if "stop_requested" in parameters:
                kwargs["stop_requested"] = self.stop_events[request.request_id].is_set
            if kwargs:
                result = self.runner.run(request, **kwargs)
            else:
                result = self.runner.run(request)
        except TypeError:
            result = self.runner.run(request)
        sink = self.result_sinks.get(request.request_id)
        if sink is not None:
            sink(result)
        return result

    def append_event(self, request_id: str, event: dict) -> None:
        with self.condition:
            event = {"sequence": len(self.events.setdefault(request_id, [])), **event}
            self.events[request_id].append(event)
            sink = self.event_sinks.get(request_id)
            self.condition.notify_all()
        if sink is not None:
            sink(dict(event))

    def has(self, request_id: str) -> bool:
        with self.lock:
            return request_id in self.futures

    def wait_result(self, request_id: str, timeout: float | None = None) -> CampaignResult:
        with self.lock:
            future = self.futures.get(request_id)
        if future is None:
            raise KeyError(request_id)
        return future.result(timeout=timeout)

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

    def list_runs(self) -> list[dict]:
        with self.lock:
            rows = []
            for request_id, future in self.futures.items():
                if future.done():
                    result = future.result()
                    status = result.platform_status.value
                    campaign_id = result.campaign_id
                else:
                    status = "RUNNING"
                    campaign_id = None
                rows.append(
                    {
                        "request_id": request_id,
                        "campaign_id": campaign_id,
                        "status": status,
                        "stop_requested": request_id in self.stop_requests,
                        "event_count": len(self.events.get(request_id, ())),
                    }
                )
        return rows

    def request_stop(self, request_id: str) -> dict:
        with self.condition:
            if request_id not in self.futures:
                raise KeyError(request_id)
            self.stop_requests.add(request_id)
            self.stop_events[request_id].set()
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

    def request_cleanup(self, request_id: str) -> dict:
        with self.condition:
            future = self.futures.get(request_id)
            if future is None:
                raise KeyError(request_id)
            if not future.done():
                self.stop_requests.add(request_id)
                self.stop_events[request_id].set()
                status = "STOP_AND_CLEANUP_REQUESTED"
            else:
                status = "ALREADY_FINALIZED"
            self.events.setdefault(request_id, []).append(
                {
                    "sequence": len(self.events.setdefault(request_id, [])),
                    "kind": "operator_cleanup_requested",
                    "request_id": request_id,
                    "payload": {"status": status},
                }
            )
            self.condition.notify_all()
        return {"request_id": request_id, "cleanup_status": status}

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
    preflight_provider=None,
    qualification_inventory=None,
    frontend_root: Path | None = None,
    task_service: Stage2TaskService | None = None,
) -> FastAPI:
    app = FastAPI(title="Resilience Benchmark Stage-2 Service", docs_url="/api/docs")
    matrix_evidence = MatrixEvidenceStore(artifact_root) if artifact_root is not None else None

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/v1/meta/health")
    def frontend_health() -> dict:
        repo_root = Path.cwd().resolve()
        return {
            "service": "ok",
            "version": "stage2-matrix-v1",
            "repo": {
                "path": str(repo_root),
                "exists": repo_root.is_dir(),
                "factory_config_found": (repo_root / "benchmarkfactory.yaml").is_file(),
            },
        }

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
        if preflight_provider is not None:
            return dict(preflight_provider())
        return {
            "status": "READY_TO_CHECK",
            "harnesses": {
                "codex": True,
                "claude-code": False,
                "deepseek-harness": False,
                "bladeai": False,
            },
            "models": list(STAGE2_MODEL_MATRIX),
            "model_matrix": {
                harness: {model: available for model in STAGE2_MODEL_MATRIX}
                for harness, available in {
                    "codex": True,
                    "claude-code": False,
                    "deepseek-harness": False,
                    "bladeai": False,
                }.items()
            },
            "cases": [item.model_dump(mode="json") for item in default_case_specs()],
            "mcp_servers": ["k8s_ro", "telemetry_ro", "source_ro", "chaos_control"],
            "rbac": {
                "trial_token_rotation": True,
                "observability_revoke": [
                    "mcp.k8s.read",
                    "mcp.telemetry.read",
                    "mcp.source.read",
                ],
                "chaos_revoke": ["mcp.chaos.create"],
            },
            "chaosblade": {"executor": "chaos_control", "execute_enabled_required": True},
            "d0": {"artifact_root_configured": False, "campaigns": []},
            "reset_mode": "unknown",
        }

    @app.get("/api/v1/qualifications")
    def qualifications() -> dict:
        if qualification_inventory is None:
            return {"artifact_root_configured": False, "campaigns": []}
        return dict(qualification_inventory())

    @app.get("/api/v1/stage2/options")
    def stage2_options() -> dict:
        if task_service is None:
            raise HTTPException(status_code=503, detail="Stage2 task service is unavailable")
        return task_service.options()

    @app.get("/api/v1/stage2/cases")
    def stage2_cases() -> dict:
        if task_service is None:
            raise HTTPException(status_code=503, detail="Stage2 task service is unavailable")
        return task_service.cases()

    @app.get("/api/v1/stage2/autonomy/cases")
    def stage2_autonomy_cases() -> dict:
        if task_service is None:
            raise HTTPException(status_code=503, detail="Stage2 task service is unavailable")
        return task_service.autonomy_cases()

    @app.post("/api/v1/stage2/tasks", status_code=status.HTTP_202_ACCEPTED)
    def create_stage2_task(
        request: Stage2TaskCreateRequest,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> dict:
        if task_service is None:
            raise HTTPException(status_code=503, detail="Stage2 task service is unavailable")
        try:
            return task_service.create(request, idempotency_key=idempotency_key)
        except TaskConflict as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "TASK_ALREADY_RUNNING",
                    "message": str(exc),
                    "active_task_id": exc.active_task_id,
                },
            ) from exc
        except TaskValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/api/v1/stage2/tasks")
    def list_stage2_tasks() -> dict:
        if task_service is None:
            raise HTTPException(status_code=503, detail="Stage2 task service is unavailable")
        return task_service.list()

    @app.get("/api/v1/stage2/tasks/{task_id}")
    def get_stage2_task(
        task_id: str,
        mode: TaskDetailMode = Query(
            default=TaskDetailMode.SUMMARY,
            description="summary, timeline, or debug",
        ),
        after_sequence: int = Query(
            default=-1,
            ge=-1,
            description="exclusive event cursor for timeline and debug modes",
        ),
        limit: int = Query(
            default=200,
            ge=1,
            le=1000,
            description="maximum event count for timeline and debug modes",
        ),
    ) -> dict:
        if task_service is None:
            raise HTTPException(status_code=503, detail="Stage2 task service is unavailable")
        try:
            return task_service.get(
                task_id,
                mode=mode,
                after_sequence=after_sequence,
                limit=limit,
            )
        except TaskNotFound as exc:
            raise HTTPException(status_code=404, detail="Stage2 task not found") from exc

    @app.post(
        "/api/v1/stage2/tasks/{task_id}/abort",
        status_code=status.HTTP_202_ACCEPTED,
    )
    def abort_stage2_task(task_id: str, request: AbortTaskRequest) -> dict:
        if task_service is None:
            raise HTTPException(status_code=503, detail="Stage2 task service is unavailable")
        try:
            return task_service.abort(task_id, request)
        except TaskNotFound as exc:
            raise HTTPException(status_code=404, detail="Stage2 task not found") from exc
        except TaskConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post(
        "/api/v1/stage2/tasks/{task_id}/environment/reset",
        status_code=status.HTTP_202_ACCEPTED,
    )
    def reset_stage2_environment(
        task_id: str, request: EnvironmentResetRequest
    ) -> dict:
        if task_service is None:
            raise HTTPException(status_code=503, detail="Stage2 task service is unavailable")
        try:
            return task_service.reset_environment(task_id, request)
        except TaskNotFound as exc:
            raise HTTPException(status_code=404, detail="Stage2 task not found") from exc
        except TaskConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post(
        "/api/v1/stage2/tasks/{task_id}/permissions/restore",
        status_code=status.HTTP_202_ACCEPTED,
    )
    def restore_stage2_permissions(
        task_id: str, request: PermissionRestoreRequest
    ) -> dict:
        if task_service is None:
            raise HTTPException(status_code=503, detail="Stage2 task service is unavailable")
        try:
            return task_service.restore_permissions(task_id, request)
        except TaskNotFound as exc:
            raise HTTPException(status_code=404, detail="Stage2 task not found") from exc
        except TaskConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/v1/matrices")
    def list_matrices() -> dict:
        if matrix_evidence is None:
            return {"matrices": []}
        return {"matrices": matrix_evidence.list_matrices()}

    @app.get("/api/v1/matrices/{matrix_id}")
    def get_matrix(matrix_id: str) -> dict:
        if matrix_evidence is None:
            raise HTTPException(status_code=404, detail="matrix artifacts are not configured")
        try:
            return matrix_evidence.overview(matrix_id)
        except MatrixEvidenceNotFound as exc:
            raise HTTPException(status_code=404, detail="matrix not found") from exc

    @app.get("/api/v1/matrices/{matrix_id}/trials/{trial_id}")
    def get_matrix_trial(matrix_id: str, trial_id: str) -> dict:
        if matrix_evidence is None:
            raise HTTPException(status_code=404, detail="matrix artifacts are not configured")
        try:
            return matrix_evidence.trial_detail(matrix_id, trial_id)
        except MatrixEvidenceNotFound as exc:
            raise HTTPException(status_code=404, detail="matrix Trial not found") from exc

    @app.get("/api/v1/matrices/{matrix_id}/artifacts/{artifact_path:path}")
    def download_matrix_artifact(matrix_id: str, artifact_path: str):
        if matrix_evidence is None:
            raise HTTPException(status_code=404, detail="matrix artifacts are not configured")
        try:
            return FileResponse(matrix_evidence.matrix_artifact(matrix_id, artifact_path))
        except MatrixEvidenceNotFound as exc:
            raise HTTPException(status_code=404, detail="matrix artifact not found") from exc

    @app.post("/api/v1/campaigns", status_code=status.HTTP_202_ACCEPTED)
    def create_campaign(request: CampaignRequest) -> dict[str, str]:
        try:
            request_id = supervisor.submit(request)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"request_id": request_id, "status": "ACCEPTED"}

    @app.get("/api/v1/campaigns")
    def list_campaigns() -> dict:
        return {"campaigns": supervisor.list_runs()}

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
                    events = await asyncio.to_thread(
                        supervisor.wait_events, request_id, cursor
                    )
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

    @app.post("/api/v1/campaigns/{request_id}/cleanup")
    def cleanup_campaign(request_id: str) -> dict:
        try:
            return supervisor.request_cleanup(request_id)
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

    if frontend_root is not None and frontend_root.is_dir():
        root = frontend_root.resolve()

        @app.get("/{frontend_path:path}", include_in_schema=False)
        def frontend(frontend_path: str):
            candidate = (root / frontend_path).resolve()
            try:
                candidate.relative_to(root)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="frontend path escaped root") from exc
            if candidate.is_file():
                return FileResponse(candidate)
            index = root / "index.html"
            if not index.is_file():
                raise HTTPException(status_code=404, detail="frontend is unavailable")
            return FileResponse(index)

    return app


def _sse(event: str, payload: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
