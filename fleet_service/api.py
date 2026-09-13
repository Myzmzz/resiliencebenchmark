"""HTTP API of the Fleet service.

Every endpoint is curl-able on its own, and everything that touches a cluster
takes ``dry_run``, defaulting to true for provisioning and batches.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Response, status
from pydantic import ValidationError

from .contracts import BatchRequest, FleetConfig, ItemState, SlotRequest, StopRequest
from .controller_client import ControllerClient, ControllerError
from .guard import FleetGuardError, replica_namespace, slot_id
from .kube import KubeError
from .manifests import owned_object_summary, slot_manifests
from .provisioner import ProvisionError, Provisioner
from .scheduler import BatchDispatcher, plan_batch
from .store import FleetStore


def create_app(
    *,
    store: FleetStore,
    provisioner: Provisioner,
    dispatcher: BatchDispatcher,
    client_factory=None,
) -> FastAPI:
    app = FastAPI(title="Resilience Benchmark Fleet Service", docs_url="/api/docs")
    make_client = client_factory or (lambda url: ControllerClient(url))

    def current_config() -> FleetConfig:
        document = store.read_config()
        if document is None:
            raise HTTPException(status_code=409, detail="fleet configuration is not set; PUT /api/v1/fleet/config first")
        try:
            return FleetConfig.model_validate(document)
        except ValidationError as exc:
            raise HTTPException(status_code=500, detail=f"stored fleet configuration is invalid: {exc}") from exc

    def require_slot(slot: str) -> dict[str, Any]:
        record = store.slot(slot)
        if record is None:
            raise HTTPException(status_code=404, detail=f"unknown slot: {slot}")
        return record

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    # -- configuration and provisioning ------------------------------------
    @app.put("/api/v1/fleet/config")
    def put_config(config: FleetConfig) -> dict[str, Any]:
        store.write_config(config.model_dump(mode="json"))
        store.audit_event(action="put_config", namespace=None, dry_run=False, actor="api",
                          detail={"replicas": config.replicas, "prefix": config.namespace_prefix})
        return {"schema_version": "fleet-config-response.v1", "config": config.model_dump(mode="json"),
                "planned_namespaces": [replica_namespace(config.namespace_prefix, index)
                                       for index in range(1, config.replicas + 1)]}

    @app.get("/api/v1/fleet/config")
    def get_config() -> dict[str, Any]:
        document = store.read_config()
        if document is None:
            raise HTTPException(status_code=404, detail="fleet configuration is not set")
        return {"schema_version": "fleet-config-response.v1", "config": document}

    @app.post("/api/v1/fleet/provision")
    def provision(dry_run: bool = Query(default=True), wait: bool = Query(default=True)) -> dict[str, Any]:
        config = current_config()
        results = []
        try:
            for index in range(1, config.replicas + 1):
                results.append(provisioner.provision_slot(config, index, dry_run=dry_run))
            if not dry_run and wait:
                for index in range(1, config.replicas + 1):
                    results[index - 1]["readiness"] = provisioner.wait_ready(config, index)
        except FleetGuardError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except (ProvisionError, KubeError) as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return {"schema_version": "fleet-provision.v1", "dry_run": dry_run, "slots": results}

    @app.post("/api/v1/fleet/slots", status_code=status.HTTP_201_CREATED)
    def add_slot(request: SlotRequest, dry_run: bool = Query(default=True)) -> dict[str, Any]:
        config = current_config()
        try:
            record = provisioner.provision_slot(config, request.index, dry_run=dry_run)
            if not dry_run:
                record["readiness"] = provisioner.wait_ready(config, request.index)
        except FleetGuardError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except (ProvisionError, KubeError) as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return record

    @app.get("/api/v1/fleet/status")
    def fleet_status(refresh: bool = Query(default=False)) -> dict[str, Any]:
        config = current_config()
        rows = []
        for slot in store.slots():
            record = provisioner.refresh_slot(config, slot) if refresh else slot
            rows.append({
                "slot_id": record["slot_id"],
                "namespace": record["namespace"],
                "controller_url": record["controller_url"],
                "phase": record["phase"],
                "updated_at": record["updated_at"],
                "detail": record.get("detail") or {},
            })
        return {
            "schema_version": "fleet-status.v1",
            "config": store.read_config(),
            "controller_image": config.controller_image,
            "agent_image": config.agent_image,
            "slots": rows,
        }

    @app.post("/api/v1/fleet/slots/{slot}/reset")
    def reset_slot(slot: str) -> dict[str, Any]:
        config = current_config()
        record = require_slot(slot)
        try:
            return provisioner.reset_slot(config, record)
        except ProvisionError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.post("/api/v1/fleet/slots/{slot}/drain")
    def drain_slot(slot: str) -> dict[str, Any]:
        require_slot(slot)
        return provisioner.drain_slot(slot)

    @app.delete("/api/v1/fleet/slots/{slot}")
    def delete_slot(slot: str, confirm: str | None = Query(default=None),
                    dry_run: bool = Query(default=True)) -> dict[str, Any]:
        config = current_config()
        record = require_slot(slot)
        try:
            return provisioner.delete_slot(config, record, confirm=confirm, dry_run=dry_run)
        except FleetGuardError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except KubeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.delete("/api/v1/fleet")
    def delete_fleet(confirm: str | None = Query(default=None),
                     dry_run: bool = Query(default=True)) -> dict[str, Any]:
        config = current_config()
        removed = []
        for record in store.slots():
            try:
                removed.append(
                    provisioner.delete_slot(config, record, confirm=record["namespace"]
                                            if confirm == "all-replicas" else confirm,
                                            dry_run=dry_run)
                )
            except FleetGuardError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"schema_version": "fleet-delete.v1", "dry_run": dry_run, "slots": removed}

    # -- things left for a human to test -----------------------------------
    @app.get("/api/v1/fleet/preflight")
    def preflight() -> dict[str, Any]:
        config = current_config()
        rows = []
        for slot in store.slots():
            client = make_client(str(slot["controller_url"]))
            row: dict[str, Any] = {"slot_id": slot["slot_id"], "namespace": slot["namespace"]}
            try:
                options = client.options()
                probe = options.get("gateway_probe") or {}
                row.update({
                    "reachable": True,
                    "gateway_probe": probe.get("status"),
                    "available_models": probe.get("available_models") or [],
                    "applications": options.get("applications"),
                    "capability_loss": options.get("capability_loss"),
                })
            except ControllerError as exc:
                row.update({"reachable": False, "error": str(exc)})
            row["environment"] = provisioner.kube.namespace_deployments_ready(str(slot["namespace"]))
            rows.append(row)
        return {"schema_version": "fleet-preflight.v1", "slots": rows}

    @app.get("/api/v1/fleet/slots/{slot}/prompt")
    def slot_prompt(slot: str, level: str = Query(default="L0"),
                    case: str = Query(default="C0")) -> dict[str, Any]:
        config = current_config()
        record = require_slot(slot)
        client = make_client(str(record["controller_url"]))
        try:
            variants = client.prompt_variants(str(record["namespace"]), {
                "target": "cart", "fault_type": "cpu_load",
                "fault_params": {"cpu_percent": 80}, "duration_seconds": 300,
            })
            authoritative = client.autonomy_cases()
        except ControllerError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        variant = next((item for item in variants.get("variants", []) if item.get("level") == level), None)
        return {
            "schema_version": "fleet-slot-prompt.v1",
            "slot_id": slot, "namespace": record["namespace"], "level": level, "case": case,
            "variant_set_id": variants.get("variant_set_id"),
            "prompt": (variant or {}).get("prompt"),
            "lint": (variant or {}).get("lint"),
            "autonomy_cases": [
                {"level": item.get("level"), "copy_ready_prompt": item.get("copy_ready_prompt")}
                for item in authoritative.get("levels", [])
            ],
        }

    @app.post("/api/v1/fleet/slots/{slot}/trial", status_code=status.HTTP_202_ACCEPTED)
    def slot_trial(slot: str, body: dict[str, Any]) -> dict[str, Any]:
        """Submit one trial straight to one Controller; for debugging by hand."""
        record = require_slot(slot)
        client = make_client(str(record["controller_url"]))
        try:
            return client.create_run({**body, "application": record["namespace"]})
        except ControllerError as exc:
            raise HTTPException(status_code=exc.status or 502,
                                detail={"error": str(exc), "controller_response": exc.payload}) from exc

    # -- batches -----------------------------------------------------------
    @app.post("/api/v1/fleet/batches", status_code=status.HTTP_202_ACCEPTED)
    def submit_batch(request: BatchRequest, dry_run: bool = Query(default=True)) -> dict[str, Any]:
        config = current_config()
        existing = store.batch(request.batch_id)
        if existing is not None:
            # Idempotent: the same batch id returns the schedule that exists.
            return {"schema_version": "fleet-batch-response.v1", "batch_id": request.batch_id,
                    "idempotent_replay": True, "dry_run": False,
                    "items": [_item_row(item) for item in store.items(request.batch_id)]}
        slots = store.slots()
        try:
            plan = plan_batch(request, slots, max_concurrency=request.max_concurrency or config.max_concurrency)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        assignment_by_item = {item.item_id: item for item in plan}
        resolved_items = []
        for item in request.items:
            resolved = request.resolved(item)
            assignment = assignment_by_item[item.item_id]
            resolved["namespace"] = resolved["namespace"] or assignment.namespace
            resolved["planned_slot_id"] = assignment.slot_id
            resolved["planned_wave"] = assignment.wave
            resolved_items.append(resolved)
        if dry_run:
            return {
                "schema_version": "fleet-batch-response.v1",
                "batch_id": request.batch_id, "dry_run": True,
                "max_concurrency": request.max_concurrency or config.max_concurrency,
                "schedule": [
                    {"item_id": item["item_id"], "slot_id": item["planned_slot_id"],
                     "namespace": item["namespace"], "wave": item["planned_wave"],
                     "harness": item["harness"], "case": item["case"],
                     "autonomy_level": item["autonomy_level"], "model": item["model"],
                     "prompt_source": item["prompt_source"]}
                    for item in resolved_items
                ],
            }
        document = request.model_dump(mode="json")
        document["max_concurrency"] = request.max_concurrency or config.max_concurrency
        store.create_batch(request.batch_id, document, resolved_items)
        store.audit_event(action="submit_batch", namespace=None, dry_run=False, actor="api",
                          detail={"batch_id": request.batch_id, "items": len(resolved_items)})
        dispatcher.dispatch_queued()
        return {"schema_version": "fleet-batch-response.v1", "batch_id": request.batch_id,
                "dry_run": False, "items": [_item_row(item) for item in store.items(request.batch_id)]}

    @app.get("/api/v1/fleet/batches")
    def list_batches() -> dict[str, Any]:
        return {"schema_version": "fleet-batch-list.v1", "batches": store.batches()}

    @app.get("/api/v1/fleet/batches/{batch_id}")
    def get_batch(batch_id: str) -> dict[str, Any]:
        batch = store.batch(batch_id)
        if batch is None:
            raise HTTPException(status_code=404, detail=f"unknown batch: {batch_id}")
        items = [_item_row(item) for item in store.items(batch_id)]
        counts: dict[str, int] = {}
        for item in items:
            counts[item["state"]] = counts.get(item["state"], 0) + 1
        failures = {"platform": 0, "agent": 0}
        for item in items:
            owner = (item.get("failure") or {}).get("owner")
            if owner in failures:
                failures[owner] += 1
        return {"schema_version": "fleet-batch.v1", "batch_id": batch_id, "state": batch["state"],
                "counts": counts, "failure_owners": failures, "items": items}

    @app.get("/api/v1/fleet/batches/{batch_id}/results")
    def batch_results(batch_id: str, format: str = Query(default="json")):
        batch = store.batch(batch_id)
        if batch is None:
            raise HTTPException(status_code=404, detail=f"unknown batch: {batch_id}")
        rows = [_result_row(item) for item in store.items(batch_id)]
        if format == "csv":
            buffer = io.StringIO()
            writer = csv.DictWriter(buffer, fieldnames=list(_RESULT_FIELDS))
            writer.writeheader()
            writer.writerows(rows)
            return Response(content=buffer.getvalue(), media_type="text/csv")
        if format != "json":
            raise HTTPException(status_code=422, detail="format must be json or csv")
        return {"schema_version": "fleet-batch-results.v1", "batch_id": batch_id, "rows": rows}

    @app.post("/api/v1/fleet/batches/{batch_id}/stop", status_code=status.HTTP_202_ACCEPTED)
    def stop_batch(batch_id: str, request: StopRequest | None = None) -> dict[str, Any]:
        batch = store.batch(batch_id)
        if batch is None:
            raise HTTPException(status_code=404, detail=f"unknown batch: {batch_id}")
        dispatcher.request_stop(batch_id)
        stopped = []
        for item in store.items(batch_id):
            if item["state"] == ItemState.QUEUED.value:
                store.update_item(batch_id, item["item_id"], state=ItemState.INVALID.value,
                                  failure={"code": "FLEET_BATCH_STOPPED", "owner": "platform"})
                stopped.append({"item_id": item["item_id"], "action": "dequeued"})
            elif item["state"] in {ItemState.ASSIGNED.value, ItemState.RUNNING.value} and item.get("run_id"):
                slot = store.slot(str(item["slot_id"])) if item.get("slot_id") else None
                if slot is None:
                    continue
                try:
                    make_client(str(slot["controller_url"])).stop_run(str(item["run_id"]))
                    # Marked so the poller files the terminal run as stopped
                    # rather than as a platform or agent failure.
                    store.update_item(
                        batch_id, item["item_id"],
                        failure={"code": "FLEET_BATCH_STOPPED", "owner": "operator",
                                 "reason": request.reason if request else "stopped by request",
                                 "stop_requested": True},
                    )
                    stopped.append({"item_id": item["item_id"], "action": "stop_requested"})
                except ControllerError as exc:
                    stopped.append({"item_id": item["item_id"], "action": "stop_failed", "error": str(exc)})
        store.set_batch_state(batch_id, "Stopped")
        store.audit_event(action="stop_batch", namespace=None, dry_run=False, actor="api",
                          detail={"batch_id": batch_id, "items": len(stopped)})
        return {"schema_version": "fleet-batch-stop.v1", "batch_id": batch_id, "stopped": stopped}

    @app.get("/api/v1/fleet/batches/{batch_id}/items/{item_id}/artifacts")
    def item_artifacts(batch_id: str, item_id: str) -> dict[str, Any]:
        item = store.item(batch_id, item_id)
        if item is None:
            raise HTTPException(status_code=404, detail=f"unknown item: {batch_id}/{item_id}")
        slot = store.slot(str(item["slot_id"])) if item.get("slot_id") else None
        base = str(slot["controller_url"]) if slot else None
        run_id = item.get("run_id")
        links = {}
        if base and run_id:
            links = {
                "summary": f"{base}/api/v1/stage2/lx/runs/{run_id}",
                "interactions": f"{base}/api/v1/stage2/lx/runs/{run_id}/interactions",
                "usage": f"{base}/api/v1/stage2/lx/runs/{run_id}/usage",
                "score": f"{base}/api/v1/stage2/lx/runs/{run_id}/score",
            }
        return {"schema_version": "fleet-item-artifacts.v1", "batch_id": batch_id, "item_id": item_id,
                "slot_id": item.get("slot_id"), "namespace": item.get("namespace"),
                "run_id": run_id, "task_id": item.get("task_id"), "links": links}

    @app.get("/api/v1/fleet/audit")
    def audit(limit: int = Query(default=200, ge=1, le=2000)) -> dict[str, Any]:
        return {"schema_version": "fleet-audit.v1", "events": store.audit_log(limit)}

    return app


_RESULT_FIELDS = (
    "batch_id", "item_id", "namespace", "slot_id", "test_kind", "autonomy_level", "case",
    "tool_substitution_variant", "harness", "model", "llm_tag", "repetition", "prompt_source",
    "state", "run_id", "verdict", "trial_validity", "recovery_status", "adjusted_score",
    "finding_code", "failure_owner",
)


def _item_row(item: Mapping[str, Any]) -> dict[str, Any]:
    resolved = item.get("resolved") or {}
    return {
        "item_id": item["item_id"],
        "state": item["state"],
        "slot_id": item.get("slot_id"),
        "namespace": item.get("namespace"),
        "run_id": item.get("run_id"),
        "task_id": item.get("task_id"),
        "attempts": item.get("attempts"),
        "platform_retries": item.get("platform_retries"),
        "started_at": item.get("started_at"),
        "finished_at": item.get("finished_at"),
        "harness": resolved.get("harness"),
        "case": resolved.get("case"),
        "autonomy_level": resolved.get("autonomy_level"),
        "model": resolved.get("model"),
        "prompt_source": resolved.get("prompt_source"),
        "failure": item.get("failure"),
        "score": item.get("score"),
    }


def _result_row(item: Mapping[str, Any]) -> dict[str, Any]:
    resolved = item.get("resolved") or {}
    failure = item.get("failure") or {}
    score = item.get("score") or {}
    return {
        "batch_id": item.get("batch_id"),
        "item_id": item.get("item_id"),
        "namespace": item.get("namespace"),
        "slot_id": item.get("slot_id"),
        "test_kind": resolved.get("test_kind"),
        "autonomy_level": resolved.get("autonomy_level"),
        "case": resolved.get("case"),
        "tool_substitution_variant": resolved.get("tool_substitution_variant") or "",
        "harness": resolved.get("harness"),
        "model": resolved.get("model"),
        "llm_tag": resolved.get("llm_tag"),
        "repetition": resolved.get("repetition"),
        "prompt_source": resolved.get("prompt_source"),
        "state": item.get("state"),
        "run_id": item.get("run_id") or "",
        "verdict": score.get("verdict") or "",
        "trial_validity": score.get("trial_validity") or "",
        "recovery_status": score.get("recovery_status") or "",
        "adjusted_score": (score.get("score_summary") or {}).get("adjusted_score", ""),
        # A node-level finding on a finished trial is a result, not a failure;
        # only a row whose failure_owner is set is one the round should exclude.
        "finding_code": failure.get("code") or "",
        "failure_owner": failure.get("owner") or "",
    }
