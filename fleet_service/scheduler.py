"""Plan a batch onto slots, dispatch it, and collect what came back.

One slot runs one trial at a time; that is the Controller's own single-flight
lock, and the Fleet does not build a second one. It only decides which queued
item goes to which idle slot, keeps at most ``max_concurrency`` in flight, and
separates platform failures (retried) from agent failures (recorded).
"""

from __future__ import annotations

import random
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from threading import Event, Thread
from typing import Any, Callable

from .contracts import BatchRequest, FleetConfig, ItemState
from .controller_client import ControllerClient, ControllerError
from .store import FleetStore, utc_now


# Failures the platform owns: the trial never got a fair run, so it is voided
# and rescheduled rather than scored against the agent.
PLATFORM_REASON_CODES = frozenset(
    {
        "STAGE2_PLATFORM_FAILED",
        "STAGE2_PLATFORM_BLOCKED",
        "STAGE2_PLATFORM_RESET_FAILED",
        "PREPARATION_FAILED",
        "POST_TRIAL_ENVIRONMENT_NOT_READY",
        "GATEWAY_SNAPSHOT_MISSING",
        "GATEWAY_EVIDENCE_MISSING",
        "GATEWAY_ROUTE_VERSION",
        "BLADEAI_MODEL_QUOTA_EXHAUSTED",
        "RESET_FAILED",
    }
)
PLATFORM_REASON_FRAGMENTS = (
    "quota exhausted",
    "rate limited",
    "capacity temporarily unavailable",
    "gateway_probe_in_progress",
    "temporarily unavailable",
    "authentication or permission rejected",
)


def classify_failure(failure: Mapping[str, Any] | None, *, http_status: int = 0) -> str:
    """``platform`` or ``agent``. Mixing the two makes a parallel round unreadable."""
    if http_status in {408, 425, 429, 500, 502, 503, 504} or http_status == 0 and failure is None:
        return "platform"
    if not failure:
        return "agent"
    code = str(failure.get("code") or "")
    if code in PLATFORM_REASON_CODES:
        return "platform"
    reason = str(failure.get("reason") or "").lower()
    if any(fragment in reason for fragment in PLATFORM_REASON_FRAGMENTS):
        return "platform"
    return "agent"


@dataclass(frozen=True)
class Assignment:
    item_id: str
    slot_id: str
    namespace: str
    wave: int


def plan_batch(
    request: BatchRequest,
    slots: Sequence[Mapping[str, Any]],
    *,
    max_concurrency: int | None = None,
) -> list[Assignment]:
    """Which item runs on which replica, in which wave.

    Dispatch is randomised with the batch id as the seed: the same batch always
    plans the same way, but one harness does not always land on the same
    replica or node, which would turn a node difference into a confound.
    """
    ready = [slot for slot in slots if slot.get("phase") in {"Ready", "Busy"}]
    by_namespace = {slot["namespace"]: slot for slot in ready}
    concurrency = max(1, min(max_concurrency or request.max_concurrency or len(ready) or 1, max(len(ready), 1)))
    rng = random.Random(f"{request.batch_id}:{len(request.items)}")
    free = [slot for slot in ready]
    rng.shuffle(free)

    pinned: list[tuple[str, Mapping[str, Any]]] = []
    floating: list[str] = []
    for item in request.items:
        resolved = request.resolved(item)
        namespace = resolved["namespace"]
        if namespace:
            slot = by_namespace.get(namespace)
            if slot is None:
                raise ValueError(f"item {item.item_id} pins namespace {namespace}, which is not a Ready slot")
            pinned.append((item.item_id, slot))
        else:
            floating.append(item.item_id)
    rng.shuffle(floating)

    assignments: list[Assignment] = []
    load: dict[str, int] = {slot["slot_id"]: 0 for slot in ready}
    for item_id, slot in pinned:
        wave = load[slot["slot_id"]]
        load[slot["slot_id"]] = wave + 1
        assignments.append(Assignment(item_id, slot["slot_id"], slot["namespace"], wave))
    usable = free[:concurrency] or free
    for offset, item_id in enumerate(floating):
        if not usable:
            raise ValueError("no Ready slot is available for an unpinned item")
        slot = min(usable, key=lambda candidate: (load[candidate["slot_id"]], candidate["slot_index"]))
        wave = load[slot["slot_id"]]
        load[slot["slot_id"]] = wave + 1
        assignments.append(Assignment(item_id, slot["slot_id"], slot["namespace"], wave))
    order = {item.item_id: index for index, item in enumerate(request.items)}
    return sorted(assignments, key=lambda item: (item.wave, order[item.item_id]))


class BatchDispatcher:
    """Background loop that keeps slots busy until a batch is finished."""

    def __init__(
        self,
        store: FleetStore,
        *,
        client_factory: Callable[[str], ControllerClient],
        poll_seconds: float = 20.0,
        trial_timeout_seconds: float = 45 * 60,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.store = store
        self.client_factory = client_factory
        self.poll_seconds = poll_seconds
        self.trial_timeout_seconds = trial_timeout_seconds
        self.clock = clock
        self._stop = Event()
        self._thread: Thread | None = None
        self._stopped_batches: set[str] = set()

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = Thread(target=self._loop, name="fleet-dispatcher", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
            self._thread = None

    def request_stop(self, batch_id: str) -> None:
        self._stopped_batches.add(batch_id)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - a dispatch bug must not kill the loop
                pass
            self._stop.wait(self.poll_seconds)

    # -- one pass ----------------------------------------------------------
    def tick(self) -> dict[str, Any]:
        polled = self.poll_running()
        dispatched = self.dispatch_queued()
        return {"polled": polled, "dispatched": dispatched}

    def poll_running(self) -> int:
        polled = 0
        for batch in self.store.batches():
            batch_id = batch["batch_id"]
            for item in self.store.items(batch_id):
                if item["state"] not in {ItemState.ASSIGNED.value, ItemState.RUNNING.value}:
                    continue
                polled += 1
                self._poll_item(batch_id, item)
            self._refresh_batch_state(batch_id)
        return polled

    def _poll_item(self, batch_id: str, item: Mapping[str, Any]) -> None:
        run_id = item.get("run_id")
        if not run_id:
            return
        slot = self.store.slot(str(item["slot_id"])) if item.get("slot_id") else None
        if slot is None:
            return
        client = self.client_factory(str(slot["controller_url"]))
        try:
            summary = client.run(str(run_id))
        except ControllerError as exc:
            if not exc.retryable:
                self._finish_failed(batch_id, item, {"code": "FLEET_RUN_UNREADABLE", "reason": str(exc)}, "platform")
            return
        status = str(summary.get("status") or "")
        if not summary.get("terminal"):
            if item["state"] != ItemState.RUNNING.value:
                self.store.update_item(batch_id, item["item_id"], state=ItemState.RUNNING.value)
            started = item.get("started_at")
            if started and self._elapsed(started) > self.trial_timeout_seconds:
                try:
                    client.stop_run(str(run_id))
                except ControllerError:
                    pass
            return
        failure = summary.get("failure") if isinstance(summary.get("failure"), Mapping) else None
        platform_status = str(summary.get("platform_status") or "")
        if failure is None and platform_status not in {"", "COMPLETED", "SUCCEEDED"}:
            # A campaign that never ran reports COMPLETED at task level while
            # its own verdict is BLOCKED or RESET_FAILED. Scoring that as an
            # agent result would credit a trial that did not happen.
            failure = {
                "code": f"STAGE2_PLATFORM_{platform_status}",
                "reason": f"platform status {platform_status}",
            }
        score: Any = None
        try:
            score = client.score(str(run_id))
        except ControllerError:
            score = None
        if status in {"COMPLETED", "DONE"} and not failure:
            self.store.update_item(
                batch_id, item["item_id"], state=ItemState.DONE.value,
                finished_at=utc_now(), failure=None, score=_score_summary(score),
            )
            return
        self._finish_failed(batch_id, item, failure or {"code": status or "STAGE2_TASK_FAILED"},
                            classify_failure(failure), score=_score_summary(score))

    def _finish_failed(
        self, batch_id: str, item: Mapping[str, Any], failure: Mapping[str, Any],
        owner: str, *, score: Any = None,
    ) -> None:
        batch = self.store.batch(batch_id) or {}
        retry_limit = int((batch.get("request") or {}).get("platform_retry_limit", 2))
        record = {**dict(failure), "owner": owner}
        if owner == "platform" and int(item.get("platform_retries") or 0) < retry_limit:
            # Voided, not scored: the agent never had a fair run.
            self.store.update_item(
                batch_id, item["item_id"], state=ItemState.QUEUED.value, slot_id=None,
                run_id=None, task_id=None, started_at=None,
                platform_retries=int(item.get("platform_retries") or 0) + 1,
                failure={**record, "voided_and_requeued": True},
            )
            return
        self.store.update_item(
            batch_id, item["item_id"], state=ItemState.FAILED.value,
            finished_at=utc_now(), failure=record, score=score,
        )

    def dispatch_queued(self) -> int:
        dispatched = 0
        for batch in self.store.batches():
            batch_id = batch["batch_id"]
            if batch["state"] in {"Stopped", "Completed"} or batch_id in self._stopped_batches:
                continue
            request = batch.get("request") or {}
            concurrency = int(request.get("max_concurrency") or 0)
            slots = {slot["slot_id"]: slot for slot in self.store.slots()}
            for item in self.store.items(batch_id):
                if item["state"] != ItemState.QUEUED.value:
                    continue
                if concurrency and self.store.running_count() >= concurrency:
                    break
                slot = self._pick_slot(item, slots)
                if slot is None:
                    continue
                if not self.store.claim_slot_for_item(batch_id, item["item_id"], slot["slot_id"], slot["namespace"]):
                    continue
                if self._submit(batch_id, item, slot):
                    dispatched += 1
            self._refresh_batch_state(batch_id)
        return dispatched

    def _pick_slot(self, item: Mapping[str, Any], slots: Mapping[str, Mapping[str, Any]]) -> Mapping[str, Any] | None:
        busy = self.store.busy_slot_ids()
        pinned = (item.get("resolved") or {}).get("namespace")
        candidates = [
            slot for slot in slots.values()
            if slot["phase"] == "Ready" and slot["slot_id"] not in busy
            and (not pinned or slot["namespace"] == pinned)
        ]
        if not candidates:
            return None
        rng = random.Random(f"{item.get('batch_id')}:{item.get('item_id')}")
        return rng.choice(sorted(candidates, key=lambda slot: slot["slot_index"]))

    def _submit(self, batch_id: str, item: Mapping[str, Any], slot: Mapping[str, Any]) -> bool:
        resolved = dict(item["resolved"])
        client = self.client_factory(str(slot["controller_url"]))
        try:
            body = build_run_request(client, slot["namespace"], resolved)
            created = client.create_run(body, idempotency_key=f"{batch_id}-{item['item_id']}")
        except ControllerError as exc:
            owner = "platform" if exc.retryable else "agent"
            detail = {"code": "FLEET_SUBMIT_REJECTED", "reason": str(exc), "controller_response": exc.payload}
            if owner == "agent":
                self.store.update_item(
                    batch_id, item["item_id"], state=ItemState.INVALID.value,
                    finished_at=utc_now(), failure={**detail, "owner": "agent"},
                )
                return False
            self._finish_failed(batch_id, item, detail, "platform")
            return False
        self.store.update_item(
            batch_id, item["item_id"], state=ItemState.RUNNING.value,
            run_id=created.get("run_id"), task_id=created.get("task_id"),
            started_at=utc_now(), attempts=int(item.get("attempts") or 0) + 1, failure=None,
        )
        return True

    def _refresh_batch_state(self, batch_id: str) -> None:
        items = self.store.items(batch_id)
        if not items:
            return
        open_states = {ItemState.QUEUED.value, ItemState.ASSIGNED.value, ItemState.RUNNING.value}
        state = "Running" if any(item["state"] in open_states for item in items) else "Completed"
        if batch_id in self._stopped_batches and state != "Completed":
            state = "Stopped"
        current = (self.store.batch(batch_id) or {}).get("state")
        if current != state:
            self.store.set_batch_state(batch_id, state)

    @staticmethod
    def _elapsed(started_at: str) -> float:
        from datetime import datetime

        try:
            started = datetime.fromisoformat(started_at)
        except ValueError:
            return 0.0
        return (datetime.now(started.tzinfo) - started).total_seconds()


def build_run_request(
    client: ControllerClient, namespace: str, resolved: Mapping[str, Any]
) -> dict[str, Any]:
    """One item as an ``LxRunRequest`` for one replica's Controller.

    A canonical item takes the prompt this replica renders for it, so the text
    always names the replica it runs on; a manual prompt is sent verbatim with
    the batch's explicit fault contract and marked as such by the Controller.
    """
    slots = dict(resolved["slots"])
    slots["duration_seconds"] = int(resolved["duration_seconds"])
    body: dict[str, Any] = {
        "autonomy_level": resolved["autonomy_level"],
        "application": namespace,
        "harness": resolved["harness"],
        "model": resolved["model"],
        "llm_tag": resolved["llm_tag"],
        "duration_seconds": int(resolved["duration_seconds"]),
        "case": resolved["case"],
    }
    if resolved.get("note"):
        body["note"] = resolved["note"]
    if resolved.get("tool_substitution_variant"):
        body["tool_substitution_variant"] = resolved["tool_substitution_variant"]
    if resolved.get("prompt_source") == "manual":
        body["prompt"] = resolved["prompt"]
        body["slots"] = slots
        return body
    variants = client.prompt_variants(namespace, slots)
    prompt = next(
        (item["prompt"] for item in variants.get("variants", [])
         if item.get("level") == resolved["autonomy_level"]),
        None,
    )
    if prompt is None:
        raise ControllerError(
            f"controller returned no {resolved['autonomy_level']} prompt variant", status=500
        )
    body["prompt"] = prompt
    body["variant_set_id"] = variants.get("variant_set_id")
    return body


def _score_summary(score: Any) -> dict[str, Any] | None:
    if not isinstance(score, Mapping):
        return None
    keep = ("run_id", "total_score", "score", "verdict", "validity", "nodes", "node_results", "summary")
    summary = {key: score[key] for key in keep if key in score}
    return summary or dict(list(score.items())[:12])
