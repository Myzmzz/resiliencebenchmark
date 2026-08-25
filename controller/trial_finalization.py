"""Controller fallback cleanup and target-side recovery verification per attempt."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any, Protocol

from progression.controller import TrialTicket
from scripts.reset_episode import (
    call_chaos_tool,
    validate_chaos_endpoint,
    validate_token,
)

from .trial_preparation import TrialRuntimeContextStore


class MainFaultControl(Protocol):
    def destroy(self, cleanup_handle: str) -> Mapping[str, Any]: ...

    def recovery_status(self, cleanup_handle: str) -> Mapping[str, Any]: ...

    def inventory(self, namespace: str) -> Mapping[str, Any]: ...


class BusinessRecoveryVerifier(Protocol):
    def __call__(
        self,
        ticket: TrialTicket,
        level: Mapping[str, Any],
        trial_report: Mapping[str, Any],
    ) -> Mapping[str, Any]: ...


class McpMainFaultControl:
    def __init__(self, *, url: str, token: str, timeout_seconds: float = 30):
        self.url = validate_chaos_endpoint(url)
        self.token = validate_token(token)
        self.timeout_seconds = timeout_seconds

    def destroy(self, cleanup_handle: str) -> Mapping[str, Any]:
        return asyncio.run(
            call_chaos_tool(
                self.url,
                self.token,
                "chaos_destroy_experiment",
                {"cleanup_handle": cleanup_handle},
                self.timeout_seconds,
            )
        )

    def recovery_status(self, cleanup_handle: str) -> Mapping[str, Any]:
        return asyncio.run(
            call_chaos_tool(
                self.url,
                self.token,
                "chaos_recovery_status",
                {"cleanup_handle": cleanup_handle},
                self.timeout_seconds,
            )
        )

    def inventory(self, namespace: str) -> Mapping[str, Any]:
        return asyncio.run(
            call_chaos_tool(
                self.url,
                self.token,
                "chaos_inventory_run",
                {"namespace": namespace},
                self.timeout_seconds,
            )
        )


class PerTrialFinalizer:
    def __init__(
        self,
        *,
        context_store: TrialRuntimeContextStore,
        main_fault_control: MainFaultControl,
        business_recovery_verifier: BusinessRecoveryVerifier,
    ):
        self.context_store = context_store
        self.main_fault_control = main_fault_control
        self.business_recovery_verifier = business_recovery_verifier

    def __call__(
        self,
        ticket: TrialTicket,
        level: Mapping[str, Any],
        trial_report: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        context = self.context_store.load(ticket.trial_id)
        handle = str(context["cleanup_handle"])
        namespace = str(context.get("target", {}).get("namespace") or "")
        errors: list[dict[str, str]] = []
        destroy: dict[str, Any] = {}
        status: dict[str, Any] = {}
        try:
            destroy = dict(self.main_fault_control.destroy(handle))
        except Exception as exc:  # noqa: BLE001 - still verify inventory and business recovery.
            errors.append({"stage": "destroy", "error_type": type(exc).__name__})
        try:
            status = dict(self.main_fault_control.recovery_status(handle))
        except Exception as exc:  # noqa: BLE001 - unknown handle can still be absent globally.
            errors.append({"stage": "recovery_status", "error_type": type(exc).__name__})
        inventory: dict[str, Any] = {}
        try:
            inventory = dict(self.main_fault_control.inventory(namespace))
        except Exception as exc:  # noqa: BLE001 - absence remains unverified.
            errors.append({"stage": "inventory", "error_type": type(exc).__name__})
        absent = (
            bool(destroy.get("verified_absent"))
            or status.get("resource_absent") is True
            or (
                inventory.get("global_chaosblade_count") == 0
                and inventory.get("active_owned_count") == 0
            )
        )
        business = dict(self.business_recovery_verifier(ticket, level, trial_report))
        verified = absent and business.get("verified") is True
        return {
            "status": "verified" if verified else "failed",
            "verified": verified,
            "cleanup_handle": handle,
            "fault_absent": absent,
            "destroy_state": destroy.get("state"),
            "recovery_state": status.get("state"),
            "inventory": {
                "global_chaosblade_count": inventory.get("global_chaosblade_count"),
                "active_owned_count": inventory.get("active_owned_count"),
                "global_unsafe_unowned_count": inventory.get(
                    "global_unsafe_unowned_count"
                ),
            },
            "control_errors": errors,
            "fault_provenance": {
                key: status.get(key)
                for key in (
                    "ledger_state",
                    "run_id",
                    "target_uid",
                    "fault_type",
                    "created_at",
                    "deadline_at",
                    "ever_active",
                )
            },
            "business_recovery": business,
            "evidence_refs": [
                f"chaos-control://{ticket.trial_id}/cleanup",
                *list(business.get("evidence_refs", [])),
            ],
        }
