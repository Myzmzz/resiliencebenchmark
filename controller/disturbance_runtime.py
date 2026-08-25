"""Compose live disturbance adapters from the per-trial runtime context."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from disturbances.injector import (
    DisturbanceInjector,
    JsonlControllerRecordSink,
    KubernetesDisturbanceAdapter,
    TelemetryInterceptorAdapter,
)
from disturbances.types import DisturbanceSpec, DisturbanceType
from progression.controller import TrialTicket

from .disturbance import ControllerDisturbanceSafetyGate
from .safety import default_policy
from .trial_preparation import TrialRuntimeContextStore


class RuntimeDisturbanceInjectorFactory:
    def __init__(
        self,
        *,
        context_store: TrialRuntimeContextStore,
        kubernetes_client: Any,
        telemetry_rule_client: Any,
        controller_record_root: Path,
        namespace_allowlist: set[str] | frozenset[str],
        allowed_types: set[DisturbanceType] | frozenset[DisturbanceType],
    ):
        self.context_store = context_store
        self.kubernetes_client = kubernetes_client
        self.telemetry_rule_client = telemetry_rule_client
        self.controller_record_root = controller_record_root.resolve()
        self.namespace_allowlist = frozenset(namespace_allowlist)
        self.allowed_types = frozenset(allowed_types)

    def __call__(
        self,
        ticket: TrialTicket,
        level: Mapping[str, Any],
    ) -> DisturbanceInjector:
        context = self.context_store.load(ticket.trial_id)
        target = context.get("target")
        if not isinstance(target, Mapping):
            raise TypeError("trial context has no exact target")
        specs = [
            DisturbanceSpec.from_mapping(item)
            for item in level.get("disturbances", [])
            if isinstance(item, Mapping)
        ]
        requested_types = {spec.type for spec in specs}
        unsupported = requested_types - self.allowed_types
        if unsupported:
            raise RuntimeError(
                "level requested non-qualified disturbance type(s): "
                + ", ".join(sorted(item.value for item in unsupported))
            )
        record_path = self.controller_record_root / ticket.trial_id / "controller-record.jsonl"
        return DisturbanceInjector(
            run_id=ticket.run_id,
            level_id=ticket.level_id,
            trial_id=ticket.trial_id,
            attempt=ticket.attempt,
            specs=specs,
            target=target,
            safety_gate=ControllerDisturbanceSafetyGate(
                default_policy(set(self.namespace_allowlist)),
                allowed_types={item.value for item in self.allowed_types},
            ),
            adapters={
                "kubernetes": KubernetesDisturbanceAdapter(self.kubernetes_client),
                "telemetry_interceptor": TelemetryInterceptorAdapter(
                    self.telemetry_rule_client
                ),
            },
            record_sink=JsonlControllerRecordSink(record_path),
        )
