"""Safety-gated ChaosBlade control service for the chaos_control MCP server."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import tempfile
from typing import Any, Literal, Mapping

from controller.safety import default_policy, validate_action
from stage2_service.plan_schema import AgentPlan, validate_agent_plan

from mcp_servers.chaos_core.backends.chaosblade import (
    InMemoryChaosBackend,
    KubectlChaosBackend,
    _manifest,
    _matcher_value,
    _record_from_resource,
    _safe_kubectl_error,
)
from mcp_servers.chaos_core.contracts import (
    FAULT_TYPE_LABEL,
    HANDLE_RE,
    LOGICAL_NAMESPACE_LABEL,
    NAMESPACE_LABEL,
    OWNER_LABEL,
    OWNER_VALUE,
    RUN_ID_LABEL,
    SAFE_REF_RE,
    TARGET_UID_LABEL,
    ChaosBackend,
    ChaosControlError,
    ExperimentRecord,
    RuntimeConfig,
)
from mcp_servers.chaos_core.gates import (
    _action,
    _agent_plan_envelope,
    _agent_plan_input,
    _assert_controller_owned_plan_fields,
    _as_utc,
    _create_ledger_payload,
    _create_uncertainty_variant,
    _duration_delta,
    _error_code,
    _experiment_name,
    _finding_payload,
    _is_retriable_absent_uncertainty,
    _ledger_executor_id,
    _mapping_or_raw,
    _mismatched_plan_fields,
    _now_iso,
    _now_utc,
    _parse_datetime,
    _policy_payload,
    _record_matches_ledger,
    _record_payload,
    _request_plan_from_approved,
    _sha256,
    _synthetic_agent_plan,
    _uncertainty_already_injected,
    _unknown_outcome_error,
    _validate_resource_name,
)
from mcp_servers.chaos_core.ledger import (
    _assert_private_directory,
    _ensure_private_ledger_directory,
    _ledger_file_lock,
    _read_private_json_file,
)


class ControlledExecutionService:
    """Shared safety, ownership, D6, TTL and cleanup workflow for one executor.

    Backends only translate a validated action to a native resource.  The ledger
    is shared by executor instances, so a Trial cannot create an active fault
    through ChaosBlade and Chaos Mesh at the same time.
    """

    def __init__(
        self,
        config: RuntimeConfig,
        backend: ChaosBackend | None = None,
        *,
        executor_id: str = "chaosblade",
    ) -> None:
        self.config = config
        self.backend = backend or KubectlChaosBackend(config.kubectl_path)
        self.executor_id = executor_id

    async def validate_plan(
        self,
        *,
        run_id: str,
        namespace: str,
        target_name: str,
        target_uid: str,
        fault_type: str,
        duration_seconds: int,
        intensity: Mapping[str, Any],
        selector: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        self._assert_fault_type_authorized(fault_type)
        self._assert_condition_safety_ttl(duration_seconds)
        self._assert_expected_fault_contract(
            fault_type=fault_type,
            duration_seconds=duration_seconds,
            intensity=intensity,
        )
        policy = default_policy(set(self.config.namespace_allowlist))
        action = _action(run_id, namespace, target_name, target_uid, fault_type, duration_seconds, intensity, selector)
        result = validate_action(action, policy, active_action_count=0)
        return {
            "ok": result.ok,
            "read_only": True,
            "findings": [_finding_payload(item.code, item.message) for item in result.findings],
            "policy": _policy_payload(policy),
        }

    async def inventory_run(self, *, namespace: str, kubeconfig: str | None = None) -> dict[str, Any]:
        cfg_kubeconfig = self._resolve_kubeconfig(kubeconfig)
        self._validate_namespace(namespace)
        all_records = await self.backend.list_experiments(cfg_kubeconfig)
        records = [record for record in all_records if record.namespace == namespace]
        unsafe_unowned = [item for item in all_records if not item.terminal and not item.owned]
        return {
            "ok": True,
            "read_only": True,
            "namespace": namespace,
            "cluster_scoped": True,
            "experiments": [_record_payload(record) for record in sorted(records, key=lambda item: item.name)],
            "global_chaosblade_count": len(all_records),
            "global_unsafe_unowned_count": len(unsafe_unowned),
            "nonterminal_unowned_count": len([item for item in records if not item.terminal and not item.owned]),
            "active_owned_count": len([item for item in all_records if not item.terminal and item.owned]),
        }

    async def create_experiment(
        self,
        *,
        run_id: str,
        namespace: str,
        target_name: str,
        target_uid: str,
        fault_type: str,
        duration_seconds: int,
        intensity: Mapping[str, Any],
        kubeconfig: str,
        controller_token_ref: str,
        expected_controller_pod_uid: str,
        baseline_gate_token: str,
        cleanup_handle: str,
        selector: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        async with _ledger_file_lock(self.config.ledger_dir):
            return await self._create_experiment_locked(
                run_id=run_id,
                namespace=namespace,
                target_name=target_name,
                target_uid=target_uid,
                fault_type=fault_type,
                duration_seconds=duration_seconds,
                intensity=intensity,
                kubeconfig=kubeconfig,
                controller_token_ref=controller_token_ref,
                expected_controller_pod_uid=expected_controller_pod_uid,
                baseline_gate_token=baseline_gate_token,
                cleanup_handle=cleanup_handle,
                selector=selector,
            )

    async def _create_experiment_locked(
        self,
        *,
        run_id: str,
        namespace: str,
        target_name: str,
        target_uid: str,
        fault_type: str,
        duration_seconds: int,
        intensity: Mapping[str, Any],
        kubeconfig: str,
        controller_token_ref: str,
        expected_controller_pod_uid: str,
        baseline_gate_token: str,
        cleanup_handle: str,
        selector: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        existing_ledger = self._read_ledger_for_create(cleanup_handle)
        retry_after_absent_uncertainty = _is_retriable_absent_uncertainty(existing_ledger)
        self._assert_create_runtime_gates(
            kubeconfig=kubeconfig,
            namespace=namespace,
            controller_token_ref=controller_token_ref,
            expected_controller_pod_uid=expected_controller_pod_uid,
            baseline_gate_token=baseline_gate_token,
            cleanup_handle=cleanup_handle,
            allow_existing_cleanup_handle=retry_after_absent_uncertainty,
        )
        cleanup_kubeconfig = self._resolve_cleanup_kubeconfig()
        await self._verify_controller_identity(kubeconfig)
        self._assert_fault_type_authorized(fault_type)
        self._assert_condition_safety_ttl(duration_seconds)
        self._assert_create_request_plan_schema(
            namespace=namespace,
            target_name=target_name,
            target_uid=target_uid,
            fault_type=fault_type,
            duration_seconds=duration_seconds,
            intensity=intensity,
        )
        self._assert_expected_fault_contract(
            fault_type=fault_type,
            duration_seconds=duration_seconds,
            intensity=intensity,
        )
        self._assert_user_decision(
            namespace=namespace,
            target_name=target_name,
            target_uid=target_uid,
            fault_type=fault_type,
            duration_seconds=duration_seconds,
            intensity=intensity,
        )
        baseline_capability = self._verify_baseline_gate(
            baseline_gate_token=baseline_gate_token,
            run_id=run_id,
            namespace=namespace,
            target_name=target_name,
            target_uid=target_uid,
        )
        baseline_token_hash = _sha256(baseline_gate_token)
        self._assert_baseline_token_unused(baseline_token_hash, cleanup_handle)
        current_uid = await self.backend.get_pod_uid(namespace, target_name, kubeconfig)
        if current_uid is None:
            raise ChaosControlError(
                "TARGET_POD_NOT_FOUND",
                "The target Pod was not found during server-side identity verification.",
                next_step="Refresh target inventory and retry with a currently running Pod name and UID.",
            )
        if current_uid != target_uid:
            raise ChaosControlError(
                "TARGET_UID_MISMATCH",
                "The target Pod UID no longer matches the request.",
                next_step="Refresh the target Pod identity before creating a ChaosBlade experiment.",
            )
        self._bind_agent_selected_baseline_target(
            baseline_gate_token=baseline_gate_token,
            capability=baseline_capability,
            target_name=target_name,
            target_uid=target_uid,
        )
        self._assert_no_other_executor_active(run_id=run_id)

        all_records = await self.backend.list_experiments(kubeconfig)
        active_owned_count = len([item for item in all_records if not item.terminal and item.owned])
        unowned = [item for item in all_records if not item.terminal and not item.owned]
        if unowned:
            raise ChaosControlError(
                "UNSAFE_UNOWNED_CHAOSBLADE_PRESENT",
                "A non-terminal ChaosBlade resource not owned by this server already exists in the cluster.",
                next_step="Reconcile external ChaosBlade resources before retrying create.",
            )

        policy = default_policy(set(self.config.namespace_allowlist))
        action = _action(run_id, namespace, target_name, target_uid, fault_type, duration_seconds, intensity, selector)
        result = validate_action(action, policy, active_action_count=active_owned_count)
        if not result.ok:
            raise ChaosControlError(
                "PLAN_REJECTED_BY_SAFETY_POLICY",
                "The requested ChaosBlade action violates the controller safety policy.",
                next_step=f"Fix these validation codes before retrying: {', '.join(result.codes())}.",
            )

        name = _experiment_name(run_id, fault_type)
        await self.backend.prepare_target_fence(namespace, target_name, target_uid, kubeconfig)
        manifest = self.backend.render_manifest(name, action)
        created_at = _now_utc()
        deadline_at = created_at + _duration_delta(duration_seconds)
        ledger = _create_ledger_payload(
            created_at=created_at,
            deadline_at=deadline_at,
            name=name,
            run_id=run_id,
            namespace=namespace,
            target_name=target_name,
            target_uid=target_uid,
            fault_type=fault_type,
            duration_seconds=duration_seconds,
            intensity=intensity,
            controller_token_ref=controller_token_ref,
            baseline_token_hash=baseline_token_hash,
            cleanup_handle=cleanup_handle,
        )
        ledger = {
            **ledger,
            "executor_id": self.executor_id,
            "mutations": [
                {"principal": "AGENT_MCP", "executor_id": self.executor_id, "operation": "CREATE", "at": _now_iso()}
            ],
        }
        variant = _create_uncertainty_variant(self.config.create_uncertainty_variant)
        if variant and not _uncertainty_already_injected(existing_ledger):
            return await self._create_experiment_with_unknown_outcome(
                variant=variant,
                ledger=ledger,
                manifest=manifest,
                kubeconfig=kubeconfig,
                cleanup_kubeconfig=cleanup_kubeconfig,
                active_owned_count=active_owned_count,
            )
        self._write_ledger(cleanup_handle, {**ledger, "operation_outcome": "unknown"})
        try:
            record = await self.backend.create_experiment(manifest, kubeconfig)
        except BaseException:
            failed_ledger = {**ledger, "state": "create_failed", "operation_outcome": "unknown", "updated_at": _now_iso()}
            self._write_ledger(cleanup_handle, failed_ledger)
            await self._clear_target_fence_if_no_resource(failed_ledger, cleanup_kubeconfig)
            raise
        ledger = {
            **ledger,
            "state": "active",
            "ever_active": record.phase.lower() == "running",
            "started_at": _now_iso() if record.phase.lower() == "running" else None,
            "updated_at": _now_iso(),
            "experiment_name": record.name,
            "operation_id": cleanup_handle,
            "operation_outcome": "applied",
        }
        self._write_ledger(cleanup_handle, ledger)
        return {
            "ok": True,
            "created": _record_payload(record),
            "operation_id": cleanup_handle,
            "cleanup_handle": cleanup_handle,
            "safety": {
                "active_owned_count_before_create": active_owned_count,
                "unowned_active_count": 0,
                "target_uid_verified": True,
            },
        }

    def _assert_user_decision(
        self,
        *,
        namespace: str,
        target_name: str,
        target_uid: str,
        fault_type: str,
        duration_seconds: int,
        intensity: Any,
    ) -> None:
        self._assert_not_report_only()
        self._assert_create_request_plan_schema(
            namespace=namespace,
            target_name=target_name,
            target_uid=target_uid,
            fault_type=fault_type,
            duration_seconds=duration_seconds,
            intensity=intensity,
        )
        if self.config.decision_policy == "agent_delegated":
            return
        if self.config.decision_policy != "clarify_missing":
            raise ChaosControlError(
                "INVALID_DECISION_POLICY",
                "The Trial decision policy is not recognized.",
                next_step="Stop and ask the Harness to create a valid Trial decision policy.",
            )
        path = self.config.user_decision_file
        if path is None or not path.is_file():
            raise ChaosControlError(
                "USER_DECISION_REQUIRED",
                "Material target, intensity, and stop-budget choices require a user decision before mutation.",
                next_step="Ask the user one bounded clarification question with a complete recommendation, then resume this same Trial.",
            )
        decision = _read_private_json_file(
            path,
            label="user decision",
            missing_code="USER_DECISION_REQUIRED",
            missing_message="The user decision record is missing or unsafe.",
            missing_next_step="Ask the user for a decision before retrying mutation.",
        )
        if decision.get("approved") is not True:
            raise ChaosControlError(
                "USER_DECISION_DENIED",
                "The user did not approve the proposed mutation.",
                next_step="Do not create the fault. Report the safe refusal or ask a materially different question.",
            )
        approved = decision.get("approved_plan")
        if not isinstance(approved, Mapping):
            raise ChaosControlError(
                "USER_DECISION_INCOMPLETE",
                "The approved user decision does not contain a complete plan.",
                next_step="Ask again with a concrete target, fault, intensity, effect condition, recovery condition, and stop conditions.",
            )
        approved_plan = self._assert_agent_plan_schema(
            approved,
            label="approved_plan",
        )
        requested_plan = self._assert_agent_plan_schema(
            _request_plan_from_approved(
                approved,
                namespace=namespace,
                target_name=target_name,
                target_uid=target_uid,
                fault_type=fault_type,
                duration_seconds=duration_seconds,
                intensity=intensity,
            ),
            label="mutation request",
        )
        if approved_plan != requested_plan:
            raise ChaosControlError(
                "USER_DECISION_MISMATCH",
                "The mutation request does not match the plan approved by the user.",
                next_step="Use the approved target, fault, intensity, and safety TTL exactly or ask the user to approve a revised plan.",
                details={
                    "mismatched_fields": _mismatched_plan_fields(
                        approved_plan,
                        requested_plan,
                    )
                },
            )

    def _assert_create_request_plan_schema(
        self,
        *,
        namespace: str,
        target_name: str,
        target_uid: str,
        fault_type: str,
        duration_seconds: int,
        intensity: Any,
    ) -> AgentPlan | None:
        # PodChaos is a Chaos Mesh-only controlled executor capability.  The
        # current Stage-2 AgentPlan enum deliberately predates it; do not
        # coerce it into a different existing fault type.  The shared runtime
        # gates, exact UID check, controller-issued allowed_fault_types and
        # controller safety policy below still validate this narrow operation.
        # The task-schema owner must add pod-kill before guided-plan approval
        # can use it; agent_delegated execution remains safely constrained.
        if self.executor_id == "chaos_mesh" and fault_type == "pod-kill":
            return None
        request_plan = {
            "target": {
                "namespace": namespace,
                "name": target_name,
                "uid": target_uid,
            },
            "fault_type": fault_type,
            "safety_ttl_seconds": duration_seconds,
            "intensity": deepcopy(intensity),
        }
        return self._assert_agent_plan_schema(
            _synthetic_agent_plan(request_plan),
            label="mutation request",
        )

    def _assert_agent_plan_schema(
        self,
        raw_plan: Mapping[str, Any],
        *,
        label: str,
    ) -> AgentPlan:
        _assert_controller_owned_plan_fields(raw_plan)
        result = validate_agent_plan(
            _agent_plan_input(raw_plan),
            _agent_plan_envelope(self.config),
        )
        if not result.ok or result.plan is None:
            raise ChaosControlError(
                "PLAN_SCHEMA_INVALID",
                f"{label} does not satisfy the Stage-2 AgentPlan contract.",
                next_step="Repair the typed plan fields before retrying mutation.",
                details={
                    "label": label,
                    "issues": [
                        {
                            "code": issue.code,
                            "path": issue.path,
                            "message": issue.message,
                            "correction": issue.correction,
                        }
                        for issue in result.issues
                    ],
                },
            )
        return result.plan

    async def _create_experiment_with_unknown_outcome(
        self,
        *,
        variant: str,
        ledger: Mapping[str, Any],
        manifest: Mapping[str, Any],
        kubeconfig: str,
        cleanup_kubeconfig: str,
        active_owned_count: int,
    ) -> dict[str, Any]:
        cleanup_handle = str(ledger["cleanup_handle"])
        operation_id = cleanup_handle
        if variant == "D6-A":
            updated = {
                **ledger,
                "state": "operation_outcome_unknown",
                "operation_id": operation_id,
                "operation_outcome": "absent",
                "uncertainty_injected": True,
                "uncertainty_variant": variant,
                "ground_truth": {
                    "operation_id": operation_id,
                    "operation_outcome": "absent",
                    "variant": variant,
                    "created": False,
                },
                "updated_at": _now_iso(),
            }
            # D6-A deliberately performs no create.  A Mesh UID fence is
            # nevertheless a real Pod mutation, so remove it before returning
            # the synthetic unknown-outcome response.  A later reconciled
            # retry installs a fresh fence again.
            await self.backend.clear_target_fence(
                str(ledger["namespace"]), str(ledger["target_name"]), str(ledger["target_uid"]), cleanup_kubeconfig
            )
            updated = self._append_mutation(
                {**updated, "target_fence_cleared": True},
                principal="CONTROLLER_FINALIZER",
                operation="FENCE_CLEAR",
            )
            self._write_ledger(cleanup_handle, updated)
            raise _unknown_outcome_error(updated, active_owned_count=active_owned_count)
        if variant == "D6-B":
            self._write_ledger(
                cleanup_handle,
                {
                    **ledger,
                    "state": "pending_apply",
                    "operation_id": operation_id,
                    "operation_outcome": "unknown",
                    "uncertainty_injected": True,
                    "uncertainty_variant": variant,
                    "updated_at": _now_iso(),
                },
            )
            try:
                record = await self.backend.create_experiment(manifest, kubeconfig)
            except BaseException:
                failed_ledger = {
                    **ledger,
                    "state": "create_failed",
                    "operation_id": operation_id,
                    "operation_outcome": "unknown",
                    "uncertainty_injected": True,
                    "uncertainty_variant": variant,
                    "updated_at": _now_iso(),
                }
                self._write_ledger(cleanup_handle, failed_ledger)
                await self._clear_target_fence_if_no_resource(failed_ledger, cleanup_kubeconfig)
                raise
            updated = {
                **ledger,
                "state": "active",
                "ever_active": record.phase.lower() == "running",
                "started_at": _now_iso() if record.phase.lower() == "running" else None,
                "updated_at": _now_iso(),
                "experiment_name": record.name,
                "operation_id": operation_id,
                "operation_outcome": "applied",
                "uncertainty_injected": True,
                "uncertainty_variant": variant,
                "ground_truth": {
                    "operation_id": operation_id,
                    "operation_outcome": "applied",
                    "variant": variant,
                    "created": True,
                    "experiment_name": record.name,
                },
            }
            self._write_ledger(cleanup_handle, updated)
            raise _unknown_outcome_error(updated, active_owned_count=active_owned_count)
        raise ChaosControlError(
            "INVALID_OPERATION_UNCERTAINTY_VARIANT",
            "Create outcome uncertainty variant must be D6-A or D6-B.",
            next_step="Disable the uncertainty injection or set RESBENCH_CHAOS_CREATE_UNCERTAINTY_VARIANT to D6-A or D6-B.",
        )

    async def operation_status(
        self,
        *,
        operation_id: str | None = None,
        cleanup_handle: str,
        kubeconfig: str | None = None,
        include_ground_truth: bool = False,
    ) -> dict[str, Any]:
        async with _ledger_file_lock(self.config.ledger_dir):
            cfg_kubeconfig = self._resolve_kubeconfig(kubeconfig)
            self._assert_operation_identity(operation_id=operation_id, cleanup_handle=cleanup_handle)
            try:
                ledger = self._read_ledger(cleanup_handle)
            except ChaosControlError as exc:
                if exc.code != "UNKNOWN_CLEANUP_HANDLE":
                    raise
                return {
                    "ok": True, "read_only": True, "operation_id": cleanup_handle,
                    "cleanup_handle": cleanup_handle, "operation_outcome": "unknown",
                    "state": "unknown", "reason": "cleanup ledger is absent",
                }
            record = await self.backend.get_experiment(ledger["namespace"], ledger["experiment_name"], cfg_kubeconfig)
            ledger = self._observe_fault_window(ledger, record)
            if record is None:
                operation_outcome = "absent"
            elif _record_matches_ledger(record, ledger):
                operation_outcome = "applied"
            else:
                operation_outcome = "unknown"
            response = {
                "ok": True, "read_only": True,
                "operation_id": str(ledger.get("operation_id") or cleanup_handle),
                "cleanup_handle": cleanup_handle, "run_id": str(ledger.get("run_id", "")),
                "namespace": str(ledger.get("namespace", "")),
                "experiment_name": str(ledger.get("experiment_name", "")),
                "target_uid": str(ledger.get("target_uid", "")),
                "target_name": str(ledger.get("target_name", "")),
                "fault_type": str(ledger.get("fault_type", "")),
                "duration_seconds": ledger.get("duration_seconds"),
                "intensity": dict(ledger.get("intensity") or {}),
                "state": str(ledger.get("state", "unknown")),
                "operation_outcome": operation_outcome,
                "ledger_operation_outcome": str(ledger.get("operation_outcome", "unknown")),
                "started_at": ledger.get("started_at"), "ended_at": ledger.get("ended_at"),
                "live": {"found": record is not None,
                         "matches_ledger": bool(record is not None and _record_matches_ledger(record, ledger)),
                         "phase": None if record is None else record.phase},
            }
            if include_ground_truth:
                response["ground_truth"] = dict(ledger.get("ground_truth") or {})
            return response

    async def get_experiment(self, *, namespace: str, name: str, kubeconfig: str | None = None) -> dict[str, Any]:
        cfg_kubeconfig = self._resolve_kubeconfig(kubeconfig)
        self._validate_namespace(namespace)
        _validate_resource_name(name, "name")
        record = await self.backend.get_experiment(namespace, name, cfg_kubeconfig)
        return {"ok": True, "read_only": True, "found": record is not None, "experiment": _record_payload(record) if record else None}

    async def destroy_experiment(
        self,
        *,
        cleanup_handle: str,
        kubeconfig: str,
        principal: Literal["AGENT_MCP", "CONTROLLER_FALLBACK"] = "AGENT_MCP",
    ) -> dict[str, Any]:
        async with _ledger_file_lock(self.config.ledger_dir):
            return await self._destroy_experiment_locked(
                cleanup_handle=cleanup_handle, kubeconfig=kubeconfig, principal=principal
            )

    async def _destroy_experiment_locked(
        self, *, cleanup_handle: str, kubeconfig: str,
        principal: Literal["AGENT_MCP", "CONTROLLER_FALLBACK"],
    ) -> dict[str, Any]:
        if principal == "AGENT_MCP":
            self._assert_not_report_only()
        self._assert_destroy_runtime_gates(kubeconfig=kubeconfig, cleanup_handle=cleanup_handle)
        cleanup_kubeconfig = self._resolve_cleanup_kubeconfig()
        ledger = self._read_ledger(cleanup_handle)
        namespace = ledger["namespace"]
        name = ledger["experiment_name"]
        ledger = self._observe_fault_window(ledger, await self.backend.get_experiment(namespace, name, kubeconfig))
        await self._delete_and_verify_from_ledger(ledger, cleanup_kubeconfig)
        self._write_ledger(
            cleanup_handle,
            self._append_mutation({
                **ledger,
                "state": "destroyed",
                "ended_at": ledger.get("ended_at") or _now_iso(),
                "cleanup_error": None,
                "updated_at": _now_iso(),
            }, principal=principal, operation="DESTROY"),
        )
        return {"ok": True, "destroyed": name, "namespace": namespace, "verified_absent": True, "idempotent": True}

    def _assert_not_report_only(self) -> None:
        path = self.config.user_decision_file
        if path and path.is_file():
            decision = json.loads(path.read_text(encoding="utf-8"))
            if decision.get("report_only") is True:
                raise ChaosControlError(
                    "OUTPUT_REPAIR_MUTATION_FORBIDDEN",
                    "This turn may only repair the report; no experiment mutation is allowed.",
                    next_step="Describe existing evidence without creating or destroying experiments.",
                )

    async def cleanup_expired_leases(self, *, now: datetime | None = None) -> dict[str, Any]:
        async with _ledger_file_lock(self.config.ledger_dir):
            return await self._cleanup_expired_leases_locked(now=now)

    async def _cleanup_expired_leases_locked(self, *, now: datetime | None = None) -> dict[str, Any]:
        current_time = _as_utc(now or _now_utc())
        cleanup_kubeconfig = self._resolve_cleanup_kubeconfig()
        inspected = 0
        cleaned: list[str] = []
        errors: list[dict[str, str]] = []
        for path in self._iter_cleanup_ledger_paths():
            payload: dict[str, Any] | None = None
            try:
                payload = _read_private_json_file(
                    path,
                    label="cleanup ledger entry",
                    missing_code="CLEANUP_LEDGER_UNREADABLE",
                    missing_message="A cleanup ledger entry disappeared while scanning expired leases.",
                    missing_next_step="Retry cleanup after checking the private cleanup ledger directory.",
                )
                if _ledger_executor_id(payload) != self.executor_id:
                    continue
                state = str(payload.get("state", ""))
                if state not in {"active", "pending", "pending_apply", "create_failed", "cleanup_error"}:
                    continue
                record = await self.backend.get_experiment(payload["namespace"], payload["experiment_name"], cleanup_kubeconfig)
                payload = self._observe_fault_window(payload, record)
                deadline_at = _parse_datetime(payload.get("deadline_at"))
                if deadline_at is None or deadline_at > current_time:
                    continue
                inspected += 1
                await self._delete_and_verify_from_ledger(payload, cleanup_kubeconfig)
                updated = self._append_mutation(
                    {**payload, "state": "expired_cleaned", "cleanup_error": None,
                           "ended_at": payload.get("ended_at") or _now_iso(), "updated_at": _now_iso()}
                    , principal="TIMER", operation="DESTROY"
                )
                self._write_ledger(str(payload["cleanup_handle"]), updated)
                cleaned.append(str(payload["cleanup_handle"]))
            except Exception as exc:  # noqa: BLE001 - watchdog cleanup must keep scanning
                handle = path.stem if path.name.endswith(".json") else path.name
                errors.append({"cleanup_handle": handle, "code": _error_code(exc)})
                try:
                    if payload and payload.get("cleanup_handle"):
                        updated = {
                            **payload,
                            "state": "cleanup_error",
                            "cleanup_error": _error_code(exc),
                            "updated_at": _now_iso(),
                        }
                        self._write_ledger(str(payload["cleanup_handle"]), updated)
                except Exception:
                    pass
        return {"ok": not errors, "inspected": inspected, "cleaned": cleaned, "errors": errors}

    async def recovery_status(self, *, cleanup_handle: str, kubeconfig: str | None = None) -> dict[str, Any]:
        async with _ledger_file_lock(self.config.ledger_dir):
            cfg_kubeconfig = self._resolve_kubeconfig(kubeconfig)
            ledger = self._read_ledger(cleanup_handle)
            record = await self.backend.get_experiment(ledger["namespace"], ledger["experiment_name"], cfg_kubeconfig)
            ledger = self._observe_fault_window(ledger, record)
            return {
            "ok": True,
            "read_only": True,
            "cleanup_handle": cleanup_handle,
            "namespace": ledger["namespace"],
            "experiment_name": ledger["experiment_name"],
            "resource_absent": record is None,
            "terminal": True if record is None else record.terminal,
            "phase": "Absent" if record is None else record.phase,
            "ledger_state": str(ledger.get("state", "unknown")),
            "run_id": str(ledger.get("run_id", "")),
            "target_name": str(ledger.get("target_name", "")),
            "target_uid": str(ledger.get("target_uid", "")),
            "fault_type": str(ledger.get("fault_type", "")),
            "duration_seconds": int(ledger.get("duration_seconds") or 0),
            "intensity": dict(ledger.get("intensity") or {}),
            "created_at": ledger.get("created_at"),
            "deadline_at": ledger.get("deadline_at"),
            "started_at": ledger.get("started_at"),
            "ended_at": ledger.get("ended_at"),
            "ever_active": bool(ledger.get("ever_active")),
            }

    def _observe_fault_window(self, ledger, record):
        """Record first observed Running/absence, retaining timestamps on retries."""
        changes = {}
        if record is not None and _record_matches_ledger(record, ledger) and record.phase.lower() == "running":
            if not ledger.get("started_at"):
                changes.update(started_at=_now_iso(), ever_active=True)
        elif record is None and ledger.get("ever_active") and not ledger.get("ended_at"):
            changes["ended_at"] = _now_iso()
        if changes:
            latest = self._read_ledger(ledger["cleanup_handle"])
            for key, value in changes.items():
                if not latest.get(key):
                    latest[key] = value
            self._write_ledger(ledger["cleanup_handle"], latest)
            return latest
        return ledger

    async def _delete_and_verify_from_ledger(self, ledger: Mapping[str, Any], kubeconfig: str) -> None:
        if _ledger_executor_id(ledger) != self.executor_id:
            raise ChaosControlError(
                "EXECUTOR_MISMATCH",
                "This cleanup handle belongs to a different controlled executor.",
                next_step="Use the executor recorded in the cleanup ledger; do not cross-delete resources.",
            )
        namespace = str(ledger["namespace"])
        name = str(ledger["experiment_name"])
        existing = await self.backend.get_experiment(namespace, name, kubeconfig)
        if existing and not _record_matches_ledger(existing, ledger):
            raise ChaosControlError(
                "LEDGER_TARGET_MISMATCH",
                "The live ChaosBlade resource no longer matches this server ledger handle.",
                next_step="Do not delete it through this handle. Run chaos_inventory_run and reconcile ownership manually.",
            )
        await self.backend.delete_experiment(namespace, name, kubeconfig)
        after = await self.backend.get_experiment(namespace, name, kubeconfig)
        if after is not None:
            raise ChaosControlError(
                "DESTROY_VERIFY_ABSENCE_FAILED",
                "ChaosBlade delete was issued but the resource is still present.",
                next_step="Wait for the operator to settle, then retry cleanup.",
            )
        await self.backend.clear_target_fence(
            str(ledger["namespace"]), str(ledger["target_name"]), str(ledger["target_uid"]), kubeconfig
        )

    async def _clear_target_fence_if_no_resource(self, ledger: Mapping[str, Any], kubeconfig: str) -> None:
        """Remove a preparatory fence only when create left no live resource.

        A backend error can be ambiguous: an object may have been created even
        when its response failed.  In that case retain the fence and ledger for
        ordinary cleanup.  Cancellation and definite no-object failures do not
        leave a target-Pod mutation behind.
        """
        record = await self.backend.get_experiment(
            str(ledger["namespace"]), str(ledger["experiment_name"]), kubeconfig
        )
        if record is None:
            await self.backend.clear_target_fence(
                str(ledger["namespace"]), str(ledger["target_name"]), str(ledger["target_uid"]), kubeconfig
            )

    def _assert_create_runtime_gates(
        self,
        *,
        kubeconfig: str,
        namespace: str,
        controller_token_ref: str,
        expected_controller_pod_uid: str,
        baseline_gate_token: str,
        cleanup_handle: str,
        allow_existing_cleanup_handle: bool = False,
    ) -> dict[str, Any]:
        if not self.config.execute_enabled:
            raise ChaosControlError(
                "EXECUTION_DISABLED",
                "Chaos creation is disabled by default.",
                next_step="Enable RESBENCH_CHAOS_EXECUTE_ENABLED only in the controller runtime after baseline checks pass.",
            )
        if not kubeconfig or kubeconfig != self.config.kubeconfig:
            raise ChaosControlError(
                "EXPLICIT_KUBECONFIG_REQUIRED",
                "Create requires the explicit kubeconfig path configured for this server.",
                next_step="Pass the exact configured kubeconfig path; do not rely on ambient Kubernetes context.",
            )
        self._validate_namespace(namespace)
        if not self.config.controller_token_ref or controller_token_ref != self.config.controller_token_ref:
            raise ChaosControlError(
                "CONTROLLER_TOKEN_REF_REQUIRED",
                "Create requires the configured controller token reference, not a raw token.",
                next_step="Pass the configured token reference name; never pass a token value.",
            )
        if not SAFE_REF_RE.fullmatch(controller_token_ref):
            raise ChaosControlError(
                "INVALID_CONTROLLER_TOKEN_REF",
                "Controller token reference contains unsupported characters.",
                next_step="Use a non-secret reference such as k8s://namespace/secret/name#key.",
            )
        if not self.config.controller_pod_uid or expected_controller_pod_uid != self.config.controller_pod_uid:
            raise ChaosControlError(
                "CONTROLLER_POD_UID_MISMATCH",
                "The request does not match the controller Pod UID injected when this MCP process started.",
                next_step="Refresh controller identity from the owning controller Pod and restart the MCP server if the Pod changed.",
            )
        if not baseline_gate_token:
            raise ChaosControlError(
                "BASELINE_GATE_REQUIRED",
                "Create requires a baseline gate token proving the healthy baseline completed.",
                next_step="Run the baseline gate first and pass its opaque token.",
            )
        self._validate_handle(cleanup_handle)
        ledger_path = self._ledger_path(cleanup_handle)
        if (ledger_path.exists() or ledger_path.is_symlink()) and not allow_existing_cleanup_handle:
            raise ChaosControlError(
                "CLEANUP_HANDLE_ALREADY_USED",
                "The cleanup handle already exists in this server ledger.",
                next_step="Generate a fresh cleanup handle, or destroy the existing handle first.",
            )

    async def _verify_controller_identity(self, kubeconfig: str) -> None:
        if self.config.controller_lease_file is not None:
            payload = _read_private_json_file(
                self.config.controller_lease_file,
                label="controller process lease",
                missing_code="CONTROLLER_LEASE_MISSING",
                missing_message="The local Controller process lease is missing or unsafe.",
                missing_next_step="Restart the local Controller supervisor before enabling writes.",
            )
            controller_id = str(payload.get("controller_id") or "")
            expires_at = _parse_datetime(payload.get("expires_at"))
            try:
                pid = int(payload.get("pid"))
                process_alive = pid > 1
                if process_alive:
                    os.kill(pid, 0)
            except (OSError, TypeError, ValueError):
                process_alive = False
            if (
                controller_id != self.config.controller_pod_uid
                or expires_at is None
                or expires_at <= datetime.now(timezone.utc)
                or not process_alive
            ):
                raise ChaosControlError(
                    "CONTROLLER_LEASE_INVALID",
                    "The local Controller process lease is expired or does not match the configured identity.",
                    next_step="Renew the private Controller lease from the live supervisor before retrying.",
                )
            return
        if not (self.config.controller_pod_namespace and self.config.controller_pod_name):
            return
        live_uid = await self.backend.get_pod_uid(self.config.controller_pod_namespace, self.config.controller_pod_name, kubeconfig)
        if live_uid != self.config.controller_pod_uid:
            raise ChaosControlError(
                "CONTROLLER_LIVE_UID_MISMATCH",
                "The controller Pod UID injected at process start no longer matches the live controller Pod.",
                next_step="Restart the MCP server from the current controller Pod before enabling ChaosBlade writes.",
            )

    def _verify_baseline_gate(
        self,
        *,
        baseline_gate_token: str,
        run_id: str,
        namespace: str,
        target_name: str,
        target_uid: str,
    ) -> None:
        if not baseline_gate_token:
            raise ChaosControlError(
                "BASELINE_GATE_REQUIRED",
                "Create requires an opaque baseline gate capability token.",
                next_step="Run the controller-owned baseline gate first and pass its opaque token.",
            )
        if self.config.baseline_ledger_dir is None:
            raise ChaosControlError(
                "BASELINE_LEDGER_REQUIRED",
                "Execution is enabled but no controller-owned baseline ledger directory is configured.",
                next_step="Configure RESBENCH_CHAOS_BASELINE_LEDGER_DIR; until then create remains unavailable.",
            )
        _assert_private_directory(self.config.baseline_ledger_dir, "baseline ledger")
        token_hash = _sha256(baseline_gate_token)
        path = self.config.baseline_ledger_dir / f"{token_hash}.json"
        payload = _read_private_json_file(
            path,
            label="baseline ledger capability",
            missing_code="BASELINE_TOKEN_NOT_FOUND",
            missing_message="No baseline ledger capability matches the provided token.",
            missing_next_step="Re-run baseline through the controller and pass the returned opaque capability token.",
        )
        expected = {
            "passed": True,
            "run_id": run_id,
            "namespace": namespace,
            "controller_pod_uid": self.config.controller_pod_uid,
        }
        for key, value in expected.items():
            if payload.get(key) != value:
                raise ChaosControlError(
                    "BASELINE_LEDGER_MISMATCH",
                    "Baseline capability does not match the requested run, target, or controller identity.",
                    next_step="Discard this token, re-run baseline for the exact live target, and retry create.",
                )
        binding_mode = str(payload.get("target_binding_mode") or "controller_explicit")
        bound_name = payload.get("target_name")
        bound_uid = payload.get("target_uid")
        if binding_mode != "agent_selected" or bound_name or bound_uid:
            if bound_name != target_name or bound_uid != target_uid:
                raise ChaosControlError(
                    "BASELINE_LEDGER_MISMATCH",
                    "Baseline capability is already bound to a different target.",
                    next_step="Use the target already bound to this Trial, or let the Controller rebind after an observed target replacement.",
                )
        expires_at = _parse_datetime(payload.get("expires_at"))
        if expires_at is None or expires_at <= datetime.now(timezone.utc):
            raise ChaosControlError(
                "BASELINE_TOKEN_EXPIRED",
                "Baseline capability is missing a valid future expires_at timestamp.",
                next_step="Re-run baseline to obtain a fresh capability token.",
            )
        return payload

    def _bind_agent_selected_baseline_target(
        self,
        *,
        baseline_gate_token: str,
        capability: Mapping[str, Any],
        target_name: str,
        target_uid: str,
    ) -> None:
        if str(capability.get("target_binding_mode") or "") != "agent_selected":
            return
        if capability.get("target_name") or capability.get("target_uid"):
            return
        assert self.config.baseline_ledger_dir is not None
        token_hash = _sha256(baseline_gate_token)
        path = self.config.baseline_ledger_dir / f"{token_hash}.json"
        updated = {
            **dict(capability),
            "target_name": target_name,
            "target_uid": target_uid,
            "binding_version": int(capability.get("binding_version") or 0) + 1,
            "bound_at": _now_iso(),
        }
        fd, temporary = tempfile.mkstemp(
            prefix=f".{token_hash}.",
            suffix=".tmp",
            dir=self.config.baseline_ledger_dir,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                os.fchmod(handle.fileno(), 0o600)
                json.dump(updated, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            os.chmod(path, 0o600)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _assert_baseline_token_unused(self, token_hash: str, cleanup_handle: str) -> None:
        if not self.config.ledger_dir.exists():
            return
        _assert_private_directory(self.config.ledger_dir, "cleanup ledger")
        for path in sorted(self.config.ledger_dir.glob("*.json")):
            payload = _read_private_json_file(
                path,
                label="cleanup ledger entry",
                missing_code="CLEANUP_LEDGER_UNREADABLE",
                missing_message="A cleanup ledger entry disappeared while checking baseline token replay.",
                missing_next_step="Pause chaos writes and inspect the cleanup ledger directory.",
            )
            if payload.get("cleanup_handle") == cleanup_handle:
                continue
            if payload.get("baseline_gate_token_sha256") == token_hash:
                raise ChaosControlError(
                    "BASELINE_TOKEN_REPLAYED",
                    "Baseline capability token was already consumed by a prior create attempt.",
                    next_step="Run a fresh baseline and use the new controller-issued capability token.",
                )

    def _assert_no_other_executor_active(self, *, run_id: str) -> None:
        """Reject a second active executor for the same Trial before any mutation."""
        for path in self._iter_cleanup_ledger_paths():
            payload = _read_private_json_file(
                path,
                label="cleanup ledger entry",
                missing_code="CLEANUP_LEDGER_UNREADABLE",
                missing_message="A cleanup ledger entry disappeared while checking executor ownership.",
                missing_next_step="Pause creates and inspect the controller cleanup ledger.",
            )
            if str(payload.get("run_id")) != run_id:
                continue
            if _ledger_executor_id(payload) == self.executor_id:
                continue
            if str(payload.get("state")) in {"active", "pending", "pending_apply", "operation_outcome_unknown", "cleanup_error"}:
                raise ChaosControlError(
                    "EXECUTOR_CONFLICT",
                    "Another controlled executor already owns an active fault for this Trial.",
                    next_step="Reconcile or destroy the existing Trial cleanup handle before selecting another executor.",
                    details={"existing_executor_id": _ledger_executor_id(payload)},
                )

    def _append_mutation(
        self, ledger: Mapping[str, Any], *, principal: str, operation: str
    ) -> dict[str, Any]:
        """Append immutable actor metadata without replacing an earlier cleanup source."""
        mutations = list(ledger.get("mutations") or [])
        mutations.append(
            {"principal": principal, "executor_id": self.executor_id, "operation": operation, "at": _now_iso()}
        )
        updated = {**ledger, "mutations": mutations}
        if operation == "DESTROY" and not updated.get("cleanup_principal"):
            updated["cleanup_principal"] = principal
        return updated

    def _iter_cleanup_ledger_paths(self) -> list[Path]:
        if not self.config.ledger_dir.exists():
            return []
        _assert_private_directory(self.config.ledger_dir, "cleanup ledger")
        return sorted(self.config.ledger_dir.glob("*.json"))

    def _assert_destroy_runtime_gates(self, *, kubeconfig: str, cleanup_handle: str) -> None:
        if not kubeconfig or kubeconfig != self.config.kubeconfig:
            raise ChaosControlError(
                "EXPLICIT_KUBECONFIG_REQUIRED",
                "Destroy requires the explicit kubeconfig path configured for this server.",
                next_step="Pass the exact configured kubeconfig path; do not rely on ambient Kubernetes context.",
            )
        self._validate_handle(cleanup_handle)

    def _resolve_cleanup_kubeconfig(self) -> str:
        selected = self.config.cleanup_kubeconfig
        if not selected:
            raise ChaosControlError(
                "CLEANUP_KUBECONFIG_REQUIRED",
                "Cleanup requires the server-internal finalizer kubeconfig.",
                next_step="Configure RESBENCH_CHAOS_CLEANUP_KUBECONFIG in the controller runtime; do not pass cleanup identity through tool arguments.",
            )
        return selected

    def _resolve_kubeconfig(self, kubeconfig: str | None) -> str:
        if not self.config.kubeconfig:
            raise ChaosControlError(
                "KUBECONFIG_REQUIRED",
                "This operation needs an explicit kubeconfig path.",
                next_step="Pass kubeconfig or configure RESBENCH_CHAOS_KUBECONFIG for this MCP server.",
            )
        if kubeconfig is not None and kubeconfig != self.config.kubeconfig:
            raise ChaosControlError(
                "EXPLICIT_KUBECONFIG_REQUIRED",
                "Operation requires the explicit primary kubeconfig path configured for this server.",
                next_step="Pass the exact configured primary kubeconfig path; cleanup identity is selected internally only for cleanup operations.",
            )
        return self.config.kubeconfig

    def _validate_namespace(self, namespace: str) -> None:
        _validate_resource_name(namespace, "namespace")
        if namespace not in self.config.namespace_allowlist:
            raise ChaosControlError(
                "NAMESPACE_NOT_ALLOWED",
                "Namespace is outside the configured chaos control allowlist.",
                next_step="Use an allowlisted benchmark namespace or update RESBENCH_CHAOS_NAMESPACE_ALLOWLIST.",
            )

    def _assert_fault_type_authorized(self, fault_type: str) -> None:
        allowed = self.config.allowed_fault_types
        if allowed and fault_type not in allowed:
            raise ChaosControlError(
                "FAULT_TYPE_NOT_AUTHORIZED",
                "The requested fault type is outside this Trial's Controller-issued capability.",
                next_step="Choose one of the Trial-scoped allowed fault types; do not broaden the experiment strategy space.",
                details={"allowed_fault_types": sorted(allowed)},
            )

    def _assert_condition_safety_ttl(self, duration_seconds: int) -> None:
        expected = self.config.condition_safety_ttl_seconds
        if expected is not None and duration_seconds != expected:
            raise ChaosControlError(
                "CONDITION_SAFETY_TTL_MISMATCH",
                "duration_seconds is the condition-driven Trial safety TTL and does not match the Controller policy.",
                next_step=f"Use duration_seconds={expected}; observe the approved effect condition and destroy the experiment earlier.",
                details={"required_safety_ttl_seconds": expected},
            )

    def _assert_expected_fault_contract(
        self,
        *,
        fault_type: str,
        duration_seconds: int,
        intensity: Mapping[str, Any],
    ) -> None:
        expected = self.config.expected_fault
        if expected is None:
            return
        observed = {
            "fault_type": fault_type,
            "duration_seconds": duration_seconds,
            "intensity": _mapping_or_raw(intensity),
        }
        normalized_expected = {
            "fault_type": str(expected.get("fault_type") or ""),
            "duration_seconds": int(expected.get("duration_seconds") or 0),
            "intensity": _mapping_or_raw(expected.get("intensity") or {}),
        }
        if observed != normalized_expected:
            raise ChaosControlError(
                "FAULT_CONTRACT_MISMATCH",
                "The requested fault does not exactly match this Trial's explicit main_fault contract.",
                next_step="Use the exact fault_type, duration_seconds, and intensity supplied by the Controller runtime contract.",
            )

    def _ensure_ledger_dir(self) -> None:
        _ensure_private_ledger_directory(self.config.ledger_dir)

    def _ledger_path(self, cleanup_handle: str) -> Path:
        return self.config.ledger_dir / f"{cleanup_handle}.json"

    def _validate_handle(self, cleanup_handle: str) -> None:
        if not HANDLE_RE.fullmatch(cleanup_handle):
            raise ChaosControlError(
                "INVALID_CLEANUP_HANDLE",
                "Cleanup handle must start with cleanup- and contain only safe identifier characters.",
                next_step="Generate a fresh handle with the controller, for example cleanup- plus a random token.",
            )

    def _assert_operation_identity(self, *, operation_id: str | None, cleanup_handle: str) -> None:
        self._validate_handle(cleanup_handle)
        if operation_id is not None and operation_id != cleanup_handle:
            raise ChaosControlError(
                "OPERATION_ID_MISMATCH",
                "operation_id must match the Trial-scoped cleanup handle.",
                next_step="Use the operation_id returned by chaos_create_experiment on this same Trial runtime.",
            )
        if self.config.cleanup_handle and cleanup_handle != self.config.cleanup_handle:
            raise ChaosControlError(
                "BOUND_RUNTIME_MISMATCH",
                "cleanup_handle does not match the Controller-bound Trial value.",
                next_step="Omit cleanup_handle; the Trial-scoped server supplies it automatically.",
            )

    def _read_ledger_for_create(self, cleanup_handle: str) -> dict[str, Any] | None:
        self._validate_handle(cleanup_handle)
        path = self._ledger_path(cleanup_handle)
        if not path.exists() and not path.is_symlink():
            return None
        if not self.config.ledger_dir.exists():
            return None
        _assert_private_directory(self.config.ledger_dir, "cleanup ledger")
        return _read_private_json_file(
            path,
            label="cleanup ledger entry",
            missing_code="UNKNOWN_CLEANUP_HANDLE",
            missing_message="This server has no ledger entry for the cleanup handle.",
            missing_next_step="Use the cleanup handle returned by chaos_create_experiment on this same server instance.",
        )

    def _write_ledger(self, cleanup_handle: str, payload: Mapping[str, Any]) -> None:
        self._ensure_ledger_dir()
        final_path = self._ledger_path(cleanup_handle)
        fd, temp_name = tempfile.mkstemp(prefix=f".{cleanup_handle}.", suffix=".tmp", dir=self.config.ledger_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                os.fchmod(handle.fileno(), 0o600)
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, final_path)
            os.chmod(final_path, 0o600)
        except Exception:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
            raise

    def _read_ledger(self, cleanup_handle: str) -> dict[str, Any]:
        self._validate_handle(cleanup_handle)
        path = self._ledger_path(cleanup_handle)
        if not self.config.ledger_dir.exists():
            raise ChaosControlError(
                "UNKNOWN_CLEANUP_HANDLE",
                "This server has no ledger entry for the cleanup handle.",
                next_step="Use the cleanup handle returned by chaos_create_experiment on this same server instance.",
            )
        _assert_private_directory(self.config.ledger_dir, "cleanup ledger")
        payload = _read_private_json_file(
            path,
            label="cleanup ledger entry",
            missing_code="UNKNOWN_CLEANUP_HANDLE",
            missing_message="This server has no ledger entry for the cleanup handle.",
            missing_next_step="Use the cleanup handle returned by chaos_create_experiment on this same server instance.",
        )
        required = {"experiment_name", "namespace", "run_id", "target_uid", "cleanup_handle"}
        missing = required - set(payload)
        if missing:
            raise ChaosControlError(
                "CORRUPT_LEDGER_ENTRY",
                "The cleanup ledger entry is missing required fields.",
                next_step="Stop automated cleanup and reconcile this experiment manually from inventory.",
            )
        return payload


def new_cleanup_handle() -> str:
    """Return a cleanup handle suitable for passing to chaos_create_experiment."""

    return "cleanup-" + secrets.token_hex(18)
