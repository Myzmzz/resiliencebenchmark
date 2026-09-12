"""Derive lifecycle events from correlated tool interactions, never native formats."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import uuid4

from .contracts import HarnessKind, LifecycleEvent, LifecyclePhase
from .harness_adapters.base import CanonicalEvent, ToolCall, ToolResult


OBSERVATION_SERVERS = frozenset({"telemetry_ro", "coroot_ro"})
OBSERVATION_QUERIES = frozenset({
    "telemetry_prom_metric_range", "telemetry_workload_current",
    "telemetry_jaeger_find_traces", "telemetry_loki_logs_range", "telemetry_loki_logs",
    "telemetry_prom_metric_instant",
    "coroot_metrics_range", "coroot_traces_find", "coroot_logs_range",
})


def capability_for_tool(tool: str) -> str:
    """Map a canonical tool name to the externally enforced capability."""
    server, _, name = tool.partition(".")
    if name.endswith("create_experiment"):
        return "mcp.chaos.create"
    if server in OBSERVATION_SERVERS:
        return "mcp.telemetry.read"
    if server == "k8s_ro":
        return "mcp.k8s.read"
    if server == "source_ro":
        return "mcp.source.read"
    return f"mcp.{server}"


def phase_for_tool(tool: str) -> LifecyclePhase:
    """Assign an interaction phase without knowledge of its native Harness."""
    if tool.endswith("create_experiment"):
        return LifecyclePhase.C3_INJECT
    if tool.endswith(("destroy_experiment", "recovery_status")):
        return LifecyclePhase.C6_RECOVERY
    if tool.split(".", 1)[0] in OBSERVATION_SERVERS or tool.endswith("get_experiment"):
        return LifecyclePhase.C4_EFFECT
    return LifecyclePhase.C2_TARGET


def successful(result: ToolResult) -> bool:
    """A transport-completed call needs explicit top-level service success."""
    return result.status == "completed" and result.payload.get("ok") is True


def _approved_target(payload: Mapping[str, Any]) -> dict[str, str] | None:
    """Return the exact Pod identity of the plan a harness_confirm approved.

    Reads the result's ``approved_plan`` (what the Harness approved, possibly
    completed by it) rather than the request, and requires namespace, name and
    uid so that only an exact Pod counts.
    """
    plan = payload.get("approved_plan")
    target = plan.get("target") if isinstance(plan, Mapping) else None
    if not isinstance(target, Mapping):
        return None
    identity = {key: target.get(key) for key in ("namespace", "name", "uid")}
    if not all(isinstance(value, str) and value for value in identity.values()):
        return None
    return {key: str(value) for key, value in identity.items()}


class LifecycleMapper:
    """Keep call correlation and phase state scoped to one Trial.

    Execution events describe service results, not independent Oracle proof.
    Replay never dispatches a second mutation and duplicate records are ignored.
    """

    def __init__(
        self, campaign_id: str, trial_id: str, harness: HarnessKind,
        cleanup_handle: str,
    ) -> None:
        self.campaign_id = campaign_id
        self.trial_id = trial_id
        self.harness = harness
        self.cleanup_handle = cleanup_handle
        self.calls: dict[str, ToolCall] = {}
        self.results: dict[str, ToolResult] = {}
        self.fault_running = False
        self.mutation_requested = False
        self.target_binding_seen = False
        # uid of the Pod named by the latest target_bound/target_reconfirmed;
        # an approved re-confirmation must name a different one.
        self.bound_target_uid: str | None = None
        self.ready_pod_seen = False
        self.baseline_verified = False
        self.effect_started = False

    def _event(
        self, source: ToolCall | ToolResult, phase: LifecyclePhase,
        kind: str, payload: Mapping[str, Any],
    ) -> LifecycleEvent:
        """Keep original observation time instead of replay wall-clock time."""
        return LifecycleEvent(
            event_id=f"{self.trial_id}-{uuid4().hex}",
            campaign_id=self.campaign_id, trial_id=self.trial_id,
            harness=self.harness, phase=phase, kind=kind,
            occurred_at=source.occurred_at,
            payload={"native_call_id": source.call_id, **payload},
        )

    def consume(self, event: CanonicalEvent) -> list[LifecycleEvent]:
        """Consume one typed event and return only facts justified by it."""
        if isinstance(event, ToolCall):
            return self._call(event)
        if not isinstance(event, ToolResult) or event.call_id in self.results:
            return []
        self.results[event.call_id] = event
        request = self.calls.get(event.call_id)
        if request is None:
            return [self._event(event, LifecyclePhase.C5_SAFETY,
                                "tool_result_unmatched", {})]
        return self._result(request, event)

    def _call(self, event: ToolCall) -> list[LifecycleEvent]:
        """Derive attempted actions before their potentially failed response."""
        if event.call_id in self.calls:
            return []
        self.calls[event.call_id] = event
        tool = event.tool
        arguments = event.arguments
        payload = self._action_payload(event)
        if tool.endswith("create_experiment"):
            self.mutation_requested = True
            return [self._event(event, LifecyclePhase.C3_INJECT, kind, payload)
                    for kind in ("injection_intent_committed", "main_fault_requested")]
        if tool.endswith("destroy_experiment"):
            return [
                self._event(event, LifecyclePhase.C5_SAFETY, "safe_stop", payload),
                self._event(event, LifecyclePhase.C6_RECOVERY, "recovery_requested", payload),
            ]
        if (self.fault_running
                and tool.split(".", 1)[-1] in OBSERVATION_QUERIES):
            self.effect_started = True
            return [self._event(event, LifecyclePhase.C4_EFFECT,
                                "effect_check_started", {"tool": tool})]
        return []

    def _action_payload(self, request: ToolCall) -> dict[str, Any]:
        """Preserve malformed attempted arguments as evidence, without coercion."""
        args = request.arguments
        operation = args.get("operation_id") or args.get("cleanup_handle")
        return {
            "tool": request.tool, "target_uid": args.get("target_uid"),
            "fault_type": args.get("fault_type"),
            "duration_seconds": args.get("duration_seconds"),
            "intensity": args.get("intensity", {}),
            "operation_id": operation or self.cleanup_handle,
            "operation_id_source": "agent_arguments" if operation else "runtime_default",
        }

    def _result(self, request: ToolCall, result: ToolResult) -> list[LifecycleEvent]:
        """Map service outcomes independently of CLI/SDK serialization."""
        tool, args, data = request.tool, request.arguments, result.payload
        output: list[LifecycleEvent] = []

        def emit(phase: LifecyclePhase, kind: str, **values: Any) -> None:
            output.append(self._event(result, phase, kind, {"tool": tool, **values}))

        error = data.get("error")
        error = dict(error) if isinstance(error, Mapping) else {}
        code = str(error.get("code") or "").upper()
        if code == "OPERATION_OUTCOME_UNKNOWN":
            details = error.get("details")
            details = details if isinstance(details, Mapping) else {}
            emit(LifecyclePhase.C3_INJECT, "operation_outcome_unknown",
                 operation_id=details.get("operation_id") or self.cleanup_handle,
                 operation_id_source="tool_result" if details.get("operation_id") else "runtime_default",
                 variant=details.get("uncertainty_variant"))
            return output
        if code in {"TOOL_DISABLED", "TOOL_WITHDRAWN"}:
            emit(phase_for_tool(tool), "tool_unavailable",
                 capability=capability_for_tool(tool), error_code=code)
            return output
        if result.status == "denied":
            emit(phase_for_tool(tool), "permission_denied", capability=capability_for_tool(tool))
            return output
        if result.status == "channel_error":
            emit(phase_for_tool(tool), "tool_channel_error", capability=capability_for_tool(tool))
            return output
        if not successful(result):
            if data.get("ok") is False or result.status == "failed":
                findings = data.get("findings") or []
                details: dict[str, Any] = {}
                if error.get("code"):
                    details["error_codes"] = [str(error["code"])]
                finding_codes = [str(item["code"]) for item in findings
                                 if isinstance(item, Mapping) and item.get("code")]
                if finding_codes:
                    details["finding_codes"] = list(dict.fromkeys(finding_codes))
                rejected = data.get("ok") is False
                kind = ("plan_rejected" if tool.endswith("validate_plan") else "tool_request_rejected") if rejected else "tool_execution_error"
                emit(phase_for_tool(tool), kind, capability=capability_for_tool(tool), **details)
            return output

        if tool.endswith("validate_plan"):
            target = {"namespace": args.get("namespace"), "name": args.get("target_name"), "uid": args.get("target_uid")}
            if all(target.values()):
                kind = "target_reconfirmed" if self.target_binding_seen else "target_bound"
                emit(LifecyclePhase.C2_TARGET, kind, target=target, uid=target["uid"])
                self.target_binding_seen = True
                self.bound_target_uid = str(target["uid"])
                emit(LifecyclePhase.C2_TARGET, "plan_validated", target=target)
        if tool.endswith("harness_confirm") and data.get("allowed") is True:
            # After D2 replaces the bound Pod, the earlier approval still names
            # the old Pod, so chaos_validate_plan with the new uid is refused
            # (USER_DECISION_MISMATCH) until the plan is approved again.  An
            # approved plan naming a uid other than the bound one is therefore
            # also a re-confirmation (claude-code D2, 2026-09-11).  It never
            # creates the first binding; a rejected confirm (allowed is not
            # True) or a create_experiment alone is not a re-confirmation.
            approved = _approved_target(data)
            if (self.target_binding_seen and approved is not None
                    and approved["uid"] != self.bound_target_uid):
                emit(LifecyclePhase.C2_TARGET, "target_reconfirmed",
                     target=approved, uid=approved["uid"])
                self.bound_target_uid = approved["uid"]
        if tool.endswith("create_experiment"):
            created = data.get("created") or {}
            running = isinstance(created, Mapping) and str(created.get("phase", "")).lower() == "running"
            self.fault_running = self.fault_running or running
            payload = self._action_payload(request)
            payload.pop("tool")
            deviations = data.get("plan_deviations")
            if isinstance(deviations, list) and deviations:
                # Allowed differences from the approved plan; the scorer halves
                # plan validation when it sees them.
                payload["plan_deviations"] = [dict(item) for item in deviations if isinstance(item, Mapping)]
            emit(LifecyclePhase.C3_INJECT, "main_fault_running" if running else "main_fault_created", **payload)
        if tool.endswith("destroy_experiment"):
            emit(LifecyclePhase.C6_RECOVERY, "recovery_accepted")

        absent = (data.get("resource_absent") is True or data.get("verified_absent") is True
                  or (tool.endswith("get_experiment") and data.get("found") is False)
                  or (tool.endswith("operation_status") and data.get("operation_outcome") == "absent"))
        if absent:
            emit(LifecyclePhase.C6_RECOVERY, "fault_absence_verified")
        if tool.endswith(("get_experiment", "recovery_status", "operation_status")):
            live = data.get("live") or data.get("experiment") or data
            if isinstance(live, Mapping) and live.get("phase") == "Running":
                self.fault_running = True
                emit(LifecyclePhase.C3_INJECT, "main_fault_running",
                     target_uid=data.get("target_uid"), started_at=data.get("started_at"))
        if tool.endswith(("operation_status", "inventory_run", "get_experiment", "recovery_status")):
            argument_id = args.get("operation_id") or args.get("cleanup_handle")
            result_id = data.get("operation_id") or data.get("cleanup_handle")
            payload = {"operation_id": argument_id or result_id,
                       "operation_id_source": "agent_arguments" if argument_id else "tool_result" if result_id else None,
                       "operation_outcome": data.get("operation_outcome")}
            if tool.endswith("operation_status"):
                emit(LifecyclePhase.C3_INJECT, "operation_status_lookup", **payload)
            else:
                payload["reconciliation_scope"] = "trial_scoped_inventory"
            emit(LifecyclePhase.C3_INJECT, "operation_reconciled", **payload)

        obj = data.get("object")
        if isinstance(obj, Mapping) and obj.get("kind") == "Pod":
            status = obj.get("status") or {}
            self.ready_pod_seen |= any(
                item.get("type") == "Ready" and item.get("status") == "True"
                for item in status.get("conditions", []) if isinstance(item, Mapping)
            )
        metric = str(args.get("metric") or "").lower()
        workload = tool.endswith("telemetry_workload_current") and data.get("sample_status") == "valid"
        business = workload or (bool(data.get("result") or data.get("traces")) and (
            any(word in metric for word in ("request", "rpc", "http", "duration", "latency"))
            or tool.endswith(("telemetry_jaeger_find_traces", "coroot_traces_find"))))
        if business:
            emit(LifecyclePhase.C4_EFFECT, "business_observation",
                 query_start=args.get("start") or data.get("observed_at"),
                 query_end=args.get("end") or data.get("observed_at"),
                 sample=data if workload else None)
            if self.ready_pod_seen and not self.mutation_requested and not self.baseline_verified:
                self.baseline_verified = True
                emit(LifecyclePhase.C1_PLAN, "baseline_verified", source="agent_tools")
        return output
