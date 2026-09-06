"""Synchronous Controller receiver for realtime MCP audit-bridge events."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Mapping
from typing import Any

from .harness_adapters.base import ToolCall, ToolResult
from .platform_ledger import PlatformLedger


CanonicalToolEvent = ToolCall | ToolResult
RealtimeObserver = Callable[[CanonicalToolEvent, str], Mapping[str, Any] | None]


class RealtimeToolEventPump:
    """Append a canonical MCP boundary event, then synchronously notify root.

    This intentionally has no Harness/model branch and no independent ledger.
    A callback exception propagates to the bridge, so callers fail closed before
    a controlled operation is permitted.
    """

    def __init__(self, trial_id: str, ledger: PlatformLedger, observer: RealtimeObserver) -> None:
        if not isinstance(trial_id, str) or not trial_id.strip():
            raise ValueError("trial_id must be non-empty")
        self.trial_id = trial_id.strip()
        self.ledger = ledger
        self.observer = observer
        self._result_fingerprints: dict[str, str] = {}
        self._delivered_results: set[str] = set()
        self._results_lock = threading.Lock()

    def __call__(self, event: CanonicalToolEvent, source: str) -> Mapping[str, Any]:
        if source != "mcp_server":
            raise ValueError("realtime MCP event source must be mcp_server")
        if isinstance(event, ToolResult):
            fingerprint = json.dumps(
                {"status": event.status, "payload": event.payload},
                ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            )
            with self._results_lock:
                previous = self._result_fingerprints.get(event.call_id)
                if previous is not None and previous != fingerprint:
                    raise ValueError("conflicting realtime result for call_id")
                if event.call_id in self._delivered_results:
                    return {}
                if previous is None:
                    self._result_fingerprints[event.call_id] = fingerprint
                    self.ledger.append(
                        trial_id=self.trial_id, event_type="ToolResult",
                        occurred_at=event.occurred_at,
                        payload={"source": source, **event.model_dump(mode="json")},
                    )
        else:
            self.ledger.append(
                trial_id=self.trial_id, event_type="ToolCall",
                occurred_at=event.occurred_at,
                payload={"source": source, **event.model_dump(mode="json")},
            )
        decision = dict(self.observer(event, source) or {})
        # Only a before-call decision controls execution.  Result callbacks may
        # add evidence but may not retroactively pretend the invocation failed.
        if isinstance(event, ToolResult):
            decision.pop("allowed", None)
            with self._results_lock:
                self._delivered_results.add(event.call_id)
        return decision
