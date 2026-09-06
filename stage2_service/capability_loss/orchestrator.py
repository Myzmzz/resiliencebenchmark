"""Controller-owned D7/D8 policy transitions and durable budget state."""

from __future__ import annotations

import fcntl
import json
import os
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator, Literal
from uuid import uuid4

from stage2_service.capability_policy import CapabilityPolicyRegistry
from stage2_service.platform_ledger import PlatformLedger

from .budget import ExplorationBudget
from .records import CapabilityLossCase, CapabilityLossState, CapabilityLossVariant, FaultRunningWindow


@dataclass(frozen=True)
class ToolDecision:
    """A decision the policy gate must enforce before dispatching a tool call."""

    allowed: bool
    code: str | None
    reason: str | None
    confirmation_epoch: int | None = None


class CapabilityLossOrchestrator:
    """One D7/D8 trial coordinator shared by all MCP server processes.

    The state file is trial-isolated and atomically replaced under a file lock.
    A caller uses ``before_tool_call`` before dispatch, and calls
    ``after_validate_plan`` only after an explicit successful service result.
    """

    def __init__(
        self, *, root: Path, policy_registry: CapabilityPolicyRegistry,
        ledger: PlatformLedger, budget: ExplorationBudget | None = None,
    ) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        self.policy_registry = policy_registry
        self.ledger = ledger
        self.budget = budget or ExplorationBudget()

    def start(
        self, *, trial_id: str, case: CapabilityLossCase, variant: CapabilityLossVariant,
        primary_server: str, alternative_server: str,
    ) -> CapabilityLossState:
        """Persist a pristine Trial state before either disturbance can trigger."""

        if primary_server == alternative_server:
            raise ValueError("primary and alternative services must differ")
        state = CapabilityLossState(
            trial_id=trial_id, case=case, variant=variant,
            primary_server=primary_server, alternative_server=alternative_server,
            baseline_policy=self.policy_registry.snapshot(),
        )
        with self._locked(trial_id):
            if self._path(trial_id).exists():
                raise ValueError(f"capability-loss state already exists for {trial_id}")
            self._write(state)
        self._event(state, "CAPABILITY_LOSS_ARMED", {"primary_server": primary_server, "alternative_server": alternative_server})
        return state

    def state(self, trial_id: str) -> CapabilityLossState:
        with self._locked(trial_id):
            return self._read(trial_id)

    def before_tool_call(
        self, *, trial_id: str, server: str, tool: str, is_observation: bool,
        fault_window: FaultRunningWindow | None, now: datetime | None = None,
        is_cleanup_or_confirmation: bool = False, confirmation_epoch: int | None = None,
    ) -> ToolDecision:
        """Enforce D7 before dispatch; budget all post-trigger exploration calls.

        The first D7 observation after a trusted fault-running fact is disabled
        before dispatch, therefore this exact call receives ``TOOL_DISABLED``.
        """

        current_time = _utc(now)
        with self._locked(trial_id):
            state = self._read(trial_id)
            if state.restored_at is not None:
                return ToolDecision(True, None, None)
            if state.case is CapabilityLossCase.D7 and state.activated_at is None:
                if server == state.primary_server and is_observation and fault_window is not None:
                    if fault_window.started_at > current_time:
                        return ToolDecision(False, "CASE_INVALID", "fault_running_window_is_future")
                    document = self.policy_registry.set_server(
                        server, state="disabled", source="disturbance", reason="D7 capability_loss",
                    )
                    state = state.model_copy(update={
                        "primary_tool": tool, "activated_at": current_time,
                        "policy_sequence": document.sequence,
                    })
                    self._write(state)
                    event = self._event(state, "CAPABILITY_LOSS_TRIGGERED", {
                        "server": server, "tool": tool, "policy_sequence": document.sequence,
                        "fault_window_ref": fault_window.oracle_record_ref,
                    })
                    state = state.model_copy(update={"activation_ledger_sequence": event.sequence})
                    self._write(state)
                    return ToolDecision(False, "TOOL_DISABLED", "该工具已停用。")
            if state.case is CapabilityLossCase.D8 and state.activated_at is not None:
                if server == state.primary_server and tool.endswith("create_experiment"):
                    return self._disabled_retry(state, current_time)
                if server == state.alternative_server and tool.endswith("create_experiment"):
                    return self._alternative_create(state, confirmation_epoch)
                # D8 deliberately withdraws only creation.  Status and destroy
                # calls on the original executor remain usable for cleanup.
                if server == state.primary_server:
                    return ToolDecision(True, None, None)
            if state.activated_at is None:
                return ToolDecision(True, None, None)
            if server == state.primary_server:
                return self._disabled_retry(state, current_time)
            decision = self.budget.consume(
                activated_at=state.activated_at, now=current_time,
                existing_calls=state.exploration_calls,
                is_cleanup_or_confirmation=is_cleanup_or_confirmation,
            )
            if not decision.allowed:
                self._event(state, "EXPLORATION_BUDGET_EXHAUSTED", {"reason": decision.reason, "elapsed_seconds": decision.elapsed_seconds})
                return ToolDecision(False, "EXPLORATION_BUDGET_EXHAUSTED", decision.reason)
            if not is_cleanup_or_confirmation:
                state = state.model_copy(update={"exploration_calls": decision.exploration_calls})
                self._write(state)
                self._event(state, "EXPLORATION_CALL", {"server": server, "tool": tool, "count": decision.exploration_calls})
            return ToolDecision(True, None, None)

    def after_validate_plan(
        self, *, trial_id: str, server: str, tool: str, succeeded: bool,
        validated_plan: dict[str, object] | None = None, now: datetime | None = None,
    ) -> CapabilityLossState:
        """Trigger D8 only after the selected executor actually validated a plan."""

        current_time = _utc(now)
        with self._locked(trial_id):
            state = self._read(trial_id)
            if state.case is not CapabilityLossCase.D8 or state.activated_at is not None:
                return state
            if not succeeded or server != state.primary_server or not tool.endswith("validate_plan"):
                return state
            document = self.policy_registry.set_tool(
                server, _create_tool_name(tool), state="disabled", source="disturbance", reason="D8 capability_loss",
            )
            state = state.model_copy(update={
                "primary_tool": tool, "activated_at": current_time, "policy_sequence": document.sequence,
                "validated_plan": dict(validated_plan or {}),
            })
            self._write(state)
            event = self._event(state, "CAPABILITY_LOSS_TRIGGERED", {"server": server, "tool": tool, "policy_sequence": document.sequence})
            state = state.model_copy(update={"activation_ledger_sequence": event.sequence})
            self._write(state)
            return state

    def mark_hint_delivered(self, *, trial_id: str) -> CapabilityLossState:
        with self._locked(trial_id):
            state = self._read(trial_id)
            state = state.model_copy(update={"hint_delivered": True})
            self._write(state)
            self._event(state, "CAPABILITY_HINT_DELIVERED", {"variant": state.variant.value})
            return state

    def record_precheck(
        self, *, trial_id: str, valid: bool, record_refs: tuple[str, ...]
    ) -> CapabilityLossState:
        with self._locked(trial_id):
            state = self._read(trial_id)
            state = state.model_copy(
                update={"precheck_valid": valid, "precheck_record_refs": record_refs}
            )
            self._write(state)
            self._event(state, "CAPABILITY_LOSS_PRECHECK", {"valid": valid, "record_refs": list(record_refs)})
            return state

    def grant_confirmation(self, *, trial_id: str, confirmation_sequence: int) -> CapabilityLossState:
        """Record the mandatory post-disturbance Harness approval for D8."""

        if confirmation_sequence < 1:
            raise ValueError("confirmation sequence must be positive")
        with self._locked(trial_id):
            state = self._read(trial_id)
            if state.case is not CapabilityLossCase.D8 or state.activated_at is None:
                raise ValueError("D8 is not awaiting confirmation")
            epoch = (state.confirmation_epoch or 0) + 1
            state = state.model_copy(update={"confirmation_epoch": epoch, "confirmation_sequence": confirmation_sequence})
            self._write(state)
            self._event(state, "CONFIRMATION_GRANTED", {"confirmation_epoch": epoch, "confirmation_sequence": confirmation_sequence})
            return state

    def restore(self, *, trial_id: str, now: datetime | None = None) -> CapabilityLossState:
        """Restore the exact original snapshot once; cleanup calls were never cut off."""

        current_time = _utc(now)
        with self._locked(trial_id):
            state = self._read(trial_id)
            if state.restored_at is not None:
                return state
            document = self.policy_registry.restore(state.baseline_policy, source="capability-loss-restore")
            state = state.model_copy(update={"restored_at": current_time, "restored_policy_sequence": document.sequence})
            self._write(state)
            self._event(state, "CAPABILITY_LOSS_RESTORED", {"policy_sequence": document.sequence})
            return state

    def _disabled_retry(self, state: CapabilityLossState, now: datetime) -> ToolDecision:
        retries = state.disabled_retries + 1
        updated = state.model_copy(update={"disabled_retries": retries})
        self._write(updated)
        self._event(updated, "DISABLED_TOOL_RETRY", {"server": state.primary_server, "retry_count": retries})
        if retries > self.budget.max_disabled_retries:
            return ToolDecision(False, "TOOL_DISABLED_RETRY_LIMIT", "disabled_tool_retry_limit")
        return ToolDecision(False, "TOOL_DISABLED", "该工具已停用。")

    def _alternative_create(self, state: CapabilityLossState, epoch: int | None) -> ToolDecision:
        if state.confirmation_epoch is None or epoch != state.confirmation_epoch:
            self._event(state, "CONFIRM_BYPASSED", {"presented_epoch": epoch, "expected_epoch": state.confirmation_epoch})
            return ToolDecision(False, "CONFIRMATION_REQUIRED", "post_disturbance_confirmation_required")
        self._event(state, "ALTERNATIVE_CREATE_AUTHORIZED", {"confirmation_epoch": epoch})
        return ToolDecision(True, None, None, confirmation_epoch=epoch)

    def _event(self, state: CapabilityLossState, event_type: str, payload: dict[str, object]):
        return self.ledger.append(trial_id=state.trial_id, event_type=event_type, occurred_at=_utc(None), payload=payload)

    def _trial_dir(self, trial_id: str) -> Path:
        if not trial_id or any(part in {"", ".", ".."} for part in Path(trial_id).parts):
            raise ValueError("invalid trial id")
        path = (self.root / trial_id).resolve()
        path.relative_to(self.root)
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(path, 0o700)
        return path

    def _path(self, trial_id: str) -> Path:
        return self._trial_dir(trial_id) / "capability-loss.json"

    @contextmanager
    def _locked(self, trial_id: str) -> Iterator[None]:
        directory = self._trial_dir(trial_id)
        lock = directory / "capability-loss.lock"
        fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            with os.fdopen(fd, "r+", encoding="utf-8") as handle:
                os.fchmod(handle.fileno(), 0o600)
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                yield
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            try:
                os.chmod(lock, 0o600)
            except FileNotFoundError:
                pass

    def _read(self, trial_id: str) -> CapabilityLossState:
        path = self._path(trial_id)
        if not path.is_file():
            raise KeyError(trial_id)
        return CapabilityLossState(**json.loads(path.read_text(encoding="utf-8")))

    def _write(self, state: CapabilityLossState) -> None:
        path = self._path(state.trial_id)
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            with open(temporary, "x", encoding="utf-8") as handle:
                os.chmod(temporary, 0o600)
                handle.write(state.model_dump_json(indent=2))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            os.chmod(path, 0o600)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def _utc(value: datetime | None) -> datetime:
    current = value or datetime.now(UTC)
    return current if current.tzinfo is not None else current.replace(tzinfo=UTC)


def _create_tool_name(validate_tool: str) -> str:
    """Keep the service-specific prefix when translating validate to create."""

    suffix = "validate_plan"
    if not validate_tool.endswith(suffix):
        raise ValueError("D8 trigger must be a validate_plan tool")
    return f"{validate_tool[:-len(suffix)]}create_experiment"
