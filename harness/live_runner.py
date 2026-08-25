"""Harness-specific live runner used by the multi-level orchestrator."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from controller.runtime_secrets import PrivateRuntimeSecretStore
from controller.trial_preparation import TrialRuntimeContextStore
from progression.controller import TrialTicket
from progression.orchestrator import EventEmitter
from scripts.run_harness_trial import run_trial

from .streaming import StreamingLifecycleBridge

TrialExecutor = Callable[..., dict[str, Any]]


class LiveHarnessTrialRunner:
    """Execute one level with real-time lifecycle and MCP tool events.

    The public Episode file is immutable and identical across levels. Runtime
    disturbances remain Controller-owned and are deliberately absent from the
    Agent prompt.
    """

    def __init__(
        self,
        *,
        repo_root: Path,
        public_episode_file: Path,
        harness_name: str,
        model_alias: str,
        artifact_root: Path,
        timeout_seconds: int = 600,
        parent_env: Mapping[str, str] | None = None,
        trial_context_store: TrialRuntimeContextStore | None = None,
        secret_store: PrivateRuntimeSecretStore | None = None,
        controller_token_ref: str | None = None,
        controller_pod_uid: str | None = None,
        main_fault: Mapping[str, Any] | None = None,
        trial_executor: TrialExecutor = run_trial,
    ):
        self.repo_root = repo_root.resolve()
        self.public_episode_file = public_episode_file.resolve()
        self.harness_name = harness_name
        self.model_alias = model_alias
        self.artifact_root = artifact_root
        self.timeout_seconds = timeout_seconds
        self.parent_env = dict(parent_env) if parent_env is not None else None
        self.trial_context_store = trial_context_store
        self.secret_store = secret_store
        self.controller_token_ref = controller_token_ref
        self.controller_pod_uid = controller_pod_uid
        self.main_fault = dict(main_fault) if main_fault is not None else None
        self.trial_executor = trial_executor

    def __call__(
        self,
        ticket: TrialTicket,
        level: Mapping[str, Any],
        emit_event: EventEmitter,
    ) -> Mapping[str, Any]:
        if str(level.get("level_id")) != ticket.level_id:
            raise ValueError("active level does not match TrialTicket")
        lifecycle_events: list[dict[str, Any]] = []

        def emit_and_capture(event):
            lifecycle_events.append(event.as_dict())
            return emit_event(event)

        bridge = StreamingLifecycleBridge(ticket.run_id, ticket.level_id, emit_and_capture)
        bridge.start()
        runtime_env = dict(self.parent_env or {})
        dynamic_parts = (
            self.trial_context_store,
            self.secret_store,
            self.controller_token_ref,
            self.controller_pod_uid,
            self.main_fault,
        )
        if any(item is not None for item in dynamic_parts):
            if any(item is None for item in dynamic_parts):
                raise ValueError("live fault capability configuration is incomplete")
            assert self.trial_context_store is not None
            assert self.secret_store is not None
            assert self.controller_token_ref is not None
            assert self.controller_pod_uid is not None
            assert self.main_fault is not None
            context = self.trial_context_store.load(ticket.trial_id)
            runtime_env.update(
                {
                    "RESBENCH_BASELINE_GATE_TOKEN": self.secret_store.get(
                        str(context["baseline_gate_token_ref"])
                    ),
                    "RESBENCH_CLEANUP_HANDLE": str(context["cleanup_handle"]),
                    "RESBENCH_CHAOS_CONTROLLER_TOKEN_REF": self.controller_token_ref,
                    "RESBENCH_CHAOS_CONTROLLER_POD_UID": self.controller_pod_uid,
                    "RESBENCH_AUTHORIZED_TARGET_JSON": json.dumps(
                        context["target"], separators=(",", ":"), sort_keys=True
                    ),
                    "RESBENCH_MAIN_FAULT_JSON": json.dumps(
                        self.main_fault, separators=(",", ":"), sort_keys=True
                    ),
                    "RESBENCH_AUTHORIZED_RUN_ID": ticket.run_id,
                }
            )
        report: dict[str, Any]
        try:
            report = self.trial_executor(
                repo_root=self.repo_root,
                harness_name=self.harness_name,
                model_alias=self.model_alias,
                episode_file=self.public_episode_file,
                execute=True,
                artifact_root=self.artifact_root,
                timeout_seconds=self.timeout_seconds,
                parent_env=runtime_env,
                event_observer=bridge.handle,
                trial_id=ticket.trial_id,
            )
        except Exception:
            bridge.finish("runner_failed")
            raise
        bridge.finish(str(report.get("status", "unknown")))
        report["lifecycleEvents"] = lifecycle_events
        report["mainFaultAppliedObserved"] = bridge.main_fault_applied
        return report
