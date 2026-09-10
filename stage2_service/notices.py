"""Shared in-band Controller notices and receipt evidence for every Harness."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from .platform_ledger import PlatformLedger


def claim_notices(ledger: PlatformLedger, trial_id: str, *, path: str, limit: int = 10) -> list[dict[str, Any]]:
    """Lease undelivered notices; offering a response is not a receipt."""
    output = []
    for _ in range(limit):
        delivery = ledger.claim_notice(trial_id=trial_id, claimed_by=path, lease_seconds=60)
        if delivery is None:
            break
        output.append({
            "delivery_id": delivery.delivery_id, "attempt": delivery.attempt,
            "notice": {"notice_id": delivery.notice.notice_id,
                       "notice_type": delivery.notice.notice_type,
                       "enqueued_at": delivery.notice.enqueued_at,
                       "payload": delivery.notice.payload},
        })
    return output


def attach_notices(result: Any, ledger: PlatformLedger, trial_id: str) -> Any:
    """Append notices only to an explicitly successful tool response."""
    if not isinstance(result, Mapping) or result.get("ok") is not True:
        return result
    notices = claim_notices(ledger, trial_id, path="in_band")
    return {**result, "controller_notices": notices} if notices else result


def acknowledge_received_notices(
    payload: Mapping[str, Any], ledger: PlatformLedger, trial_id: str,
    *, received_at: datetime,
) -> None:
    """Mark notices seen in native tool results, with the original receipt time."""
    for field, path in (("controller_notices", "in_band"), ("notices", "poll")):
        items = payload.get(field)
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, Mapping) or not isinstance(item.get("delivery_id"), str):
                continue
            try:
                ledger.deliver_notice(
                    delivery_id=item["delivery_id"], trial_id=trial_id,
                    delivered_at=received_at, delivery_path=path,
                )
            except KeyError:
                # An unrecognized/cross-Trial ID is never accepted as evidence.
                continue


def all_trial_events(ledger: PlatformLedger, trial_id: str) -> list[dict[str, Any]]:
    """Read every page, never infer completion from an arbitrary first page."""
    events: list[dict[str, Any]] = []
    cursor = 0
    while page := ledger.query(trial_id=trial_id, after_sequence=cursor, limit=500):
        events.extend(event.as_dict() for event in page)
        cursor = page[-1].sequence
    return events
