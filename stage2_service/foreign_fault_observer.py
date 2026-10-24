"""Keep reading the Trial inventory while an Agent's own fault is live.

The platform's fault ledger is written only by ``chaos_control``.  An Agent
that injects with its own ChaosBlade client -- the black-box BladeAI server
does exactly this, through its own ServiceAccount -- writes nothing there, and
the CR it created is usually gone before finalization first reads the
inventory: BladeAI's experiments carry ``--timeout`` and the operator reaps
them.  On 2026-09-15 round six, ``437dada9fc368f28`` lived 17:50:14-18:00:19
while its Trial ran until 18:09:39, so a finalize-time look found nothing and
the Trial scored "no fault ever ran".

The attribution decision itself belongs to ``DirectChaosCleanup``, which owns
the ledger and the sticky per-Trial record.  This thread exists so that
somebody calls ``inventory_trial`` **while the fault still exists**; without a
caller in that window there is nothing for the Controller to remember.  It is
armed only when ``STAGE2_FOREIGN_FAULT_ATTRIBUTION`` is on.

It removes one thing, and only when it is overdue.  BladeAI 0.7.0 raises any
duration below 600 s to 600 s (``ensure_min_duration``) and says it will
recover early, but never does: all twelve experiments measured in rounds six
to eight lived 605-612 s against an approved 300 s.  The user's rule of
2026-09-17 is that the platform recovers such a fault itself once it outlives
the approved duration, with the same grace the ledger path gives
(``OVERTIME_GRACE_SECONDS``).  The removal goes through
``DirectChaosCleanup.cleanup_overdue_foreign``, which deletes only the one
experiment this Trial was credited with; the Agent session is left running.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from threading import Event, Lock, Thread
from typing import Any

from .condition_monitor import OVERTIME_GRACE_SECONDS

# Removal attempts for one overdue experiment before it is left to finalization.
OVERDUE_CLEANUP_ATTEMPTS = 3
# How long ``finish`` waits for a removal that is already under way, so that
# finalization does not race the observer for the same experiment.
CLEANUP_JOIN_SECONDS = 60.0
OVERTIME_CLEANUP_REASON = "approved_duration_exceeded"


def _now() -> str:
    """Wall-clock stamp for an observation, in the transcript's own format."""
    return datetime.now(UTC).isoformat()


def approved_duration_seconds(
    approved_plan: Mapping[str, Any] | None, main_fault: Mapping[str, Any] | None
) -> int | None:
    """The fault duration the user approved, for the foreign-fault overtime rule.

    ``safety_ttl_seconds`` is what the reviewer approved; the runtime's
    ``duration_seconds`` is only a fallback for a plan that lacks it.  ``None``
    means no usable duration, and nothing is removed on its account.  Shared by
    the live observer (armed in ``campaign``) and by finalization, which applies
    the same rule when the Agent session ends before the observer's deadline.
    """
    for value in ((approved_plan or {}).get("safety_ttl_seconds"), (main_fault or {}).get("duration_seconds")):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if value > 0:
            return int(value)
    return None


class ForeignFaultObserver:
    """Poll one Trial's fault inventory on a timer until the Trial ends."""

    def __init__(
        self,
        cleanup_backend: Any,
        *,
        poll_seconds: float = 5.0,
        grace_seconds: float = OVERTIME_GRACE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.cleanup_backend = cleanup_backend
        self.poll_seconds = poll_seconds
        self.grace_seconds = grace_seconds
        self._clock = clock
        self._stop = Event()
        self._lock = Lock()
        self._thread: Thread | None = None
        # Monotonic time of the first poll that saw the attributed experiment.
        self._first_seen: float | None = None
        self._cleanup_in_flight = False
        self._result: dict[str, Any] = {"armed": False, "polls": 0, "errors": 0}

    def arm(
        self,
        *,
        trial_id: str,
        runtime: Any,
        emit: Any | None = None,
        approved_duration_seconds: int | None = None,
    ) -> None:
        """Start polling for this Trial; arming twice is a no-op.

        Arming happens when the plan is approved, because that is the earliest
        moment the Agent is allowed to inject and the latest moment that is
        still before the fault exists.  Without an approved duration the
        observer only watches and never removes anything.
        """
        with self._lock:
            if self._thread is not None:
                return
            self._result.update(
                {
                    "armed": True,
                    "trial_id": trial_id,
                    "armed_at": _now(),
                    "approved_duration_seconds": approved_duration_seconds,
                    "grace_seconds": self.grace_seconds,
                }
            )
            self._thread = Thread(
                target=self._run,
                args=(runtime, emit, approved_duration_seconds),
                daemon=True,
                name=f"foreign-fault-{trial_id[-16:]}",
            )
            self._thread.start()

    def finish(self) -> dict[str, Any]:
        """Stop polling and return what was seen, for the Trial transcript."""
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=max(self.poll_seconds * 2, 1.0))
            if thread.is_alive() and self._cleanup_in_flight:
                thread.join(timeout=CLEANUP_JOIN_SECONDS)
        with self._lock:
            self._result["finished_at"] = _now()
            return dict(self._result)

    def _run(self, runtime: Any, emit: Any | None, approved_duration_seconds: int | None) -> None:
        while not self._stop.is_set():
            try:
                inventory = self.cleanup_backend.inventory_trial(runtime)
            except Exception as exc:  # noqa: BLE001 - a failed read is not evidence.
                with self._lock:
                    self._result["errors"] += 1
                    self._result["last_error"] = type(exc).__name__
            else:
                self._record(inventory, emit)
                if self._is_overdue(inventory, approved_duration_seconds):
                    self._clean_up_overdue(runtime, emit)
            self._stop.wait(self.poll_seconds)

    def _record(self, inventory: Any, emit: Any | None) -> None:
        """Note one poll, and the first poll that saw an attributed fault."""
        trial = inventory.get("trial") if isinstance(inventory, Mapping) else None
        with self._lock:
            self._result["polls"] += 1
            if not isinstance(trial, Mapping):
                return
            if trial.get("fault_attribution") != "observed_foreign":
                return
            first_observation = self._result.get("observed_at")
            if self._first_seen is None:
                self._first_seen = self._clock()
            self._result.update(
                {
                    "observed": True,
                    "observed_at": first_observation or _now(),
                    "experiment_name": trial.get("experiment_name"),
                    "fault_type": trial.get("fault_type"),
                    "target_name": trial.get("target_name"),
                }
            )
            cleanup = self._result.get("controller_cleanup")
            if (
                self._result.get("controller_fallback_used") is True
                and isinstance(cleanup, Mapping)
                and cleanup.get("verified_absent") is not True
                and trial.get("resource_absent") is True
            ):
                # The delete went out but its own re-read still saw the CR
                # being destroyed; a later poll is the proof that it is gone.
                self._result["controller_cleanup"] = {
                    **dict(cleanup),
                    "verified_absent": True,
                    "verified_absent_by": "later_poll",
                }
            should_emit = emit is not None and not first_observation
            payload = dict(self._result)
        if should_emit:
            # Outside the lock: the emitter writes to the Trial's event stream.
            emit("foreign_fault_observed", payload)

    def _is_overdue(self, inventory: Any, approved_duration_seconds: int | None) -> bool:
        """Whether the credited experiment is still running past duration plus grace."""
        if not approved_duration_seconds or approved_duration_seconds <= 0:
            return False
        trial = inventory.get("trial") if isinstance(inventory, Mapping) else None
        if not isinstance(trial, Mapping):
            return False
        if trial.get("fault_attribution") != "observed_foreign" or trial.get("resource_absent") is True:
            return False
        with self._lock:
            if self._first_seen is None or self._stop.is_set():
                return False
            if int(self._result.get("cleanup_attempts") or 0) >= OVERDUE_CLEANUP_ATTEMPTS:
                return False
            return self._clock() - self._first_seen >= approved_duration_seconds + self.grace_seconds

    def _clean_up_overdue(self, runtime: Any, emit: Any | None) -> None:
        """Ask the Controller to delete the overdue experiment and record the outcome."""
        with self._lock:
            if self._stop.is_set():
                return
            self._cleanup_in_flight = True
            self._result["cleanup_attempts"] = int(self._result.get("cleanup_attempts") or 0) + 1
        try:
            outcome = dict(self.cleanup_backend.cleanup_overdue_foreign(runtime))
        except Exception as exc:  # noqa: BLE001 - recorded; the next poll may retry.
            outcome = {"verified_absent": False, "reason": "cleanup_error", "error": type(exc).__name__}
        with self._lock:
            self._cleanup_in_flight = False
            self._result["last_cleanup_outcome"] = outcome
            if outcome.get("deleted_foreign_experiment"):
                # Only an issued delete counts as the platform stepping in; a
                # fault that was already gone when the Controller looked was
                # ended by its own timer or by the Agent.  A later refused
                # attempt must not overwrite it, hence the separate key.
                self._result.update(
                    {
                        "controller_cleanup": outcome,
                        "controller_fallback_used": True,
                        "controller_fallback_at": self._result.get("controller_fallback_at") or _now(),
                        "controller_fallback_reason": OVERTIME_CLEANUP_REASON,
                    }
                )
            payload = dict(self._result)
        if emit is not None:
            emit("foreign_fault_overtime_cleanup", payload)
