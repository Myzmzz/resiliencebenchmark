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
the ledger and the sticky per-Trial record.  This thread exists only so that
somebody calls ``inventory_trial`` **while the fault still exists**; without a
caller in that window there is nothing for the Controller to remember.  It is
armed only when ``STAGE2_FOREIGN_FAULT_ATTRIBUTION`` is on, and it reads --
it never creates, deletes or attributes anything by itself.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from threading import Event, Lock, Thread
from typing import Any


def _now() -> str:
    """Wall-clock stamp for an observation, in the transcript's own format."""
    return datetime.now(UTC).isoformat()


class ForeignFaultObserver:
    """Poll one Trial's fault inventory on a timer until the Trial ends."""

    def __init__(self, cleanup_backend: Any, *, poll_seconds: float = 5.0) -> None:
        self.cleanup_backend = cleanup_backend
        self.poll_seconds = poll_seconds
        self._stop = Event()
        self._lock = Lock()
        self._thread: Thread | None = None
        self._result: dict[str, Any] = {"armed": False, "polls": 0, "errors": 0}

    def arm(
        self,
        *,
        trial_id: str,
        runtime: Any,
        emit: Any | None = None,
    ) -> None:
        """Start polling for this Trial; arming twice is a no-op.

        Arming happens when the plan is approved, because that is the earliest
        moment the Agent is allowed to inject and the latest moment that is
        still before the fault exists.
        """
        with self._lock:
            if self._thread is not None:
                return
            self._result.update(
                {"armed": True, "trial_id": trial_id, "armed_at": _now()}
            )
            self._thread = Thread(
                target=self._run,
                args=(runtime, emit),
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
        with self._lock:
            self._result["finished_at"] = _now()
            return dict(self._result)

    def _run(self, runtime: Any, emit: Any | None) -> None:
        while not self._stop.is_set():
            try:
                inventory = self.cleanup_backend.inventory_trial(runtime)
            except Exception as exc:  # noqa: BLE001 - a failed read is not evidence.
                with self._lock:
                    self._result["errors"] += 1
                    self._result["last_error"] = type(exc).__name__
            else:
                self._record(inventory, emit)
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
            self._result.update(
                {
                    "observed": True,
                    "observed_at": first_observation or _now(),
                    "experiment_name": trial.get("experiment_name"),
                    "fault_type": trial.get("fault_type"),
                    "target_name": trial.get("target_name"),
                }
            )
            should_emit = emit is not None and not first_observation
            payload = dict(self._result)
        if should_emit:
            # Outside the lock: the emitter writes to the Trial's event stream.
            emit("foreign_fault_observed", payload)
