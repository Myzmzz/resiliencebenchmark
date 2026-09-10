"""Bind authenticated MCP function calls to the live Controller event bridge."""

from __future__ import annotations

import asyncio
import inspect
import json
import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from .audit_bridge import AuditBridgeClient, AuditBridgeConfig
from stage2_service.harness_adapters.base import status_from_payload


def audit_client_from_env(env: Mapping[str, str] | None = None) -> AuditBridgeClient | None:
    """Standalone MCP servers may omit auditing; Trial servers configure it."""
    values = os.environ if env is None else env
    path = values.get("RESBENCH_MCP_AUDIT_SOCKET")
    if not path:
        return None
    trial_id = values.get("RESBENCH_AUTHORIZED_RUN_ID", "").strip()
    authority = values.get("RESBENCH_MCP_AUDIT_AUTHORITY", "").strip()
    if not trial_id or not authority:
        raise RuntimeError(
            "RESBENCH_MCP_AUDIT_SOCKET requires RESBENCH_AUTHORIZED_RUN_ID "
            "and RESBENCH_MCP_AUDIT_AUTHORITY"
        )
    return AuditBridgeClient(AuditBridgeConfig(
        socket_path=Path(path), trial_id=trial_id,
        authority=authority,
        timeout_seconds=float(values.get("RESBENCH_MCP_AUDIT_TIMEOUT_SECONDS", "130")),
    ))


def bound_arguments(function: Callable[..., Any], args: tuple, kwargs: dict) -> dict[str, Any]:
    """Use the actual validated function signature, not native Harness JSON."""
    return dict(inspect.signature(function).bind(*args, **kwargs).arguments)


def audit_payload(value: Any) -> dict[str, Any]:
    """Keep structured MCP results, or normalize the controlled K8s proxy."""
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, tuple) and len(value) == 3 and isinstance(value[0], int):
        status, body, _headers = value
        try:
            parsed = json.loads(body)
        except (TypeError, ValueError):
            return {"ok": False, "error": {"code": "UNSTRUCTURED_PROXY_RESPONSE"}}
        key = "items" if isinstance(parsed, dict) and "items" in parsed else "object"
        return {"ok": 200 <= status < 300, key: parsed.get("items") if key == "items" else parsed}
    return {"ok": False, "error": {"code": "UNSTRUCTURED_TOOL_RESULT"}}


def _with_controller_call_id(value: Any, call_id: str) -> Any:
    """Return a receipt-bearing result without inventing one offline.

    MCP mappings expose the receipt as a top-level field.  The BladeAI
    Kubernetes proxy has to preserve Kubernetes response bodies, so its receipt
    is instead a response header.  In both shapes the identical identifier is
    also persisted in the audited result payload.
    """
    if isinstance(value, Mapping):
        result = dict(value)
        result["controller_call_id"] = call_id
        return result
    if isinstance(value, tuple) and len(value) == 3 and isinstance(value[0], int):
        status, body, headers = value
        copied_headers = dict(headers)
        copied_headers["X-Resbench-Controller-Call-Id"] = call_id
        return status, body, copied_headers
    return value


async def audited_async_call(
    client: AuditBridgeClient | None, server: str, tool: str, arguments: dict[str, Any],
    operation: Callable[[], Any],
) -> Any:
    """Await the Controller before execution and record every resulting denial."""
    if client is None:
        result = operation()
        return await result if inspect.isawaitable(result) else result
    decision = await asyncio.to_thread(client.before_call, server, tool, arguments)
    if not decision.allowed:
        return decision.payload or {"ok": False, "error": {
            "code": "CONTROLLER_CALL_DENIED", "message": decision.reason or "调用被拒绝。",
        }}
    try:
        result = operation()
        if inspect.isawaitable(result):
            result = await result
    except Exception as exc:
        await asyncio.to_thread(client.after_call, decision.call_id,
                                {"ok": False, "error": {"code": type(exc).__name__}}, "failed")
        raise
    result = _with_controller_call_id(result, decision.call_id)
    payload = audit_payload(result)
    payload["controller_call_id"] = decision.call_id
    status = status_from_payload(native_status="completed", payload=payload)
    await asyncio.to_thread(client.after_call, decision.call_id, payload, status)
    return result


def audited_sync_call(
    client: AuditBridgeClient | None, server: str, tool: str, arguments: dict[str, Any],
    operation: Callable[[], Any],
) -> Any:
    """Synchronous counterpart for explicitly synchronous SDK tool functions."""
    if client is None:
        return operation()
    decision = client.before_call(server, tool, arguments)
    if not decision.allowed:
        return decision.payload or {"ok": False, "error": {"code": "CONTROLLER_CALL_DENIED"}}
    try:
        result = operation()
    except Exception as exc:
        client.after_call(decision.call_id, {"ok": False, "error": {"code": type(exc).__name__}}, "failed")
        raise
    result = _with_controller_call_id(result, decision.call_id)
    payload = audit_payload(result)
    payload["controller_call_id"] = decision.call_id
    client.after_call(decision.call_id, payload, status_from_payload(native_status="completed", payload=payload))
    return result
