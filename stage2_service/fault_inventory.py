"""Executor-neutral, evidence-bearing inventory for benchmark fault resources.

Inventory is intentionally independent from an Agent-visible MCP session.  It
lists the real executor CRs, preserves foreign resources, and regards a fault
as owned only when its exact Controller labels match the current Trial.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from mcp_servers.chaos_core.service import ExperimentRecord, OWNER_VALUE

from .target_residue import TargetResidueVerdict


ACTIVE_TERMINAL_PHASES = {
    "absence", "absent", "destroyed", "deleted", "finished", "completed",
    "succeeded", "success",
}


@dataclass(frozen=True)
class FaultResource:
    executor_id: str
    resource_kind: str
    name: str
    namespace: str
    run_id: str
    target_name: str
    target_uid: str
    fault_type: str
    phase: str
    owner: str | None
    principal: str | None
    labels: Mapping[str, str]
    ledger_matched: bool

    @property
    def active(self) -> bool:
        return self.phase.strip().lower() not in ACTIVE_TERMINAL_PHASES

    def owned_by_trial(self, trial_id: str) -> bool:
        return (
            self.owner == OWNER_VALUE
            and self.run_id == trial_id
            and self.ledger_matched
            and bool(self.target_uid)
        )

    def to_dict(self, *, trial_id: str) -> dict[str, Any]:
        return {
            "executor_id": self.executor_id,
            "resource_kind": self.resource_kind,
            "name": self.name,
            "namespace": self.namespace,
            "run_id": self.run_id,
            "target_name": self.target_name,
            "target_uid": self.target_uid,
            "fault_type": self.fault_type,
            "phase": self.phase,
            "active": self.active,
            "owner": self.owner,
            "principal": self.principal,
            "ledger_matched": self.ledger_matched,
            "owned_by_trial": self.owned_by_trial(trial_id),
        }


def resource_from_experiment(
    executor_id: str,
    record: ExperimentRecord,
    *,
    ledger_matched: bool,
) -> FaultResource:
    raw = dict(record.raw or {})
    kind = str(raw.get("kind") or "ChaosBlade")
    labels = dict(record.labels or {})
    return FaultResource(
        executor_id=executor_id,
        resource_kind=kind,
        name=record.name,
        namespace=record.namespace,
        run_id=record.run_id,
        target_name=record.target_name,
        target_uid=record.target_uid,
        fault_type=record.fault_type,
        phase=record.phase,
        owner=record.owner,
        principal=(
            str(labels.get("benchmark.principal"))
            if labels.get("benchmark.principal") else None
        ),
        labels=labels,
        ledger_matched=ledger_matched,
    )


def snapshot_for_trial(
    *,
    trial_id: str,
    resources: Iterable[FaultResource],
    qualified_executors: Iterable[str],
    unavailable_executors: Iterable[str] = (),
    target_residue: TargetResidueVerdict | None = None,
) -> dict[str, Any]:
    """Build the one inventory contract consumed by finalization and reset.

    An unavailable executor is not an empty executor.  ``qualified`` is false
    until all expected CRD listings have succeeded, so callers cannot convert a
    read failure into a clean environment.

    ``inventory_clear`` keeps its original meaning -- the **cluster face** is
    clean -- because every existing consumer reads it that way.  What the
    cluster face cannot see is a fault acting on the target with no record
    behind it: D8-B ran a shell burner at 811m with no CR anywhere, and F5
    showed a deleted CR leaves the process running.  ``residue_clear`` is
    therefore the authoritative answer, and it requires **both** faces.

    With no ``target_residue`` supplied the two agree, so existing callers are
    unaffected; supply one and a probe that failed makes ``residue_clear``
    false rather than silently clean.
    """
    rows = tuple(resources)
    unavailable = tuple(sorted(set(unavailable_executors)))
    qualified = not unavailable and bool(tuple(qualified_executors))
    owned_present = [item for item in rows if item.owned_by_trial(trial_id)]
    owned_active = [item for item in owned_present if item.active]
    foreign_present = [item for item in rows if not item.owned_by_trial(trial_id)]
    foreign_active = [item for item in foreign_present if item.active]
    inventory_clear = qualified and not owned_present and not foreign_active
    return {
        "schema_version": "stage2-fault-inventory.v1",
        "trial_id": trial_id,
        "mode": "qualified" if qualified else "incomplete",
        "qualified": qualified,
        "qualified_executors": sorted(set(qualified_executors)),
        "unavailable_executors": list(unavailable),
        "resources": [item.to_dict(trial_id=trial_id) for item in rows],
        "owned_present_count": len(owned_present),
        "owned_active_count": len(owned_active),
        "foreign_present_count": len(foreign_present),
        "foreign_active_count": len(foreign_active),
        "global_resources_absent": qualified and not rows,
        "owned_resources_absent": qualified and not owned_present,
        "inventory_clear": inventory_clear,
        "target_residue": target_residue.to_dict() if target_residue is not None else None,
        "target_residue_state": target_residue.state if target_residue is not None else "not_probed",
        "residue_clear": inventory_clear and (
            target_residue is None or target_residue.clear
        ),
    }


class DualExecutorFaultInventory:
    """List actual ChaosBlade and Chaos Mesh CRs through Controller backends.

    ``ledger_matcher`` is Controller-owned and must compare a resource against
    its private Trial ledger.  It must not be implemented by an Agent MCP
    service or inferred solely from resource labels.
    """

    def __init__(
        self,
        *,
        services: Mapping[str, Any],
        kubeconfig: str,
        ledger_matcher: Callable[[str, ExperimentRecord], bool],
        trial_facts: Callable[[str, tuple[FaultResource, ...]], Mapping[str, Any]],
    ) -> None:
        self.services = dict(services)
        self.kubeconfig = kubeconfig
        self.ledger_matcher = ledger_matcher
        self.trial_facts = trial_facts

    async def inventory_trial(self, trial_id: str, namespace: str) -> dict[str, Any]:
        resources: list[FaultResource] = []
        qualified: list[str] = []
        unavailable: list[str] = []
        for executor_id, service in self.services.items():
            try:
                records = await service.backend.list_experiments(
                    self.kubeconfig, namespace
                )
            except Exception:  # A missing CRD/RBAC error is evidence, never zero.
                unavailable.append(executor_id)
                continue
            qualified.append(executor_id)
            resources.extend(
                resource_from_experiment(
                    executor_id,
                    record,
                    ledger_matched=self.ledger_matcher(executor_id, record),
                )
                for record in records
            )
        snapshot = snapshot_for_trial(
            trial_id=trial_id,
            resources=resources,
            qualified_executors=qualified,
            unavailable_executors=unavailable,
        )
        facts = self.trial_facts(trial_id, tuple(resources))
        snapshot["trial"] = dict(facts) if isinstance(facts, Mapping) else {}
        return snapshot
