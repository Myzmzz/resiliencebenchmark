"""Explicit recovery states, so a failed recovery cannot become a reinstall.

Rollback, capability-loss rollback and environment reset used to collapse into
one question — "did the evidence look clean?" — and any unclean answer landed on
``T3_FULL_REINSTALL``. On 2026-09-11 a permission-restore error took that path:
the platform uninstalled the system under test and the reinstall then failed
(O04).

A failed or unobserved recovery is not evidence that a reinstall is safe; it is
evidence that the platform does not know what state the environment is in. That
state gets its own name here, ``RECOVERY_UNVERIFIED``, and it withholds the
reinstall instead of authorizing it. Only an environment that is dirty for a
reason the platform did observe may escalate, and the reinstall preflight in
``reset.py`` still gates that.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any


class RecoveryState(str, Enum):
    NOT_REQUIRED = "RECOVERY_NOT_REQUIRED"
    VERIFIED = "RECOVERY_VERIFIED"
    UNVERIFIED = "RECOVERY_UNVERIFIED"


# Each entry is one of the four paths that must never reach a reinstall on its
# own, mapped to the evidence keys the Controller writes for it.
UNVERIFIED_SIGNALS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "RECOVERY_RAISED",
        (
            "recovery_exception",
            "recovery_failed",
            "rollback_error",
            "restore_failed",
            "permission_restore_failed",
        ),
    ),
    (
        "CONTROLLER_RESTARTED",
        (
            "controller_restarted",
            "restoration_state_missing",
            "recovery_state_missing",
        ),
    ),
    (
        "CLEANUP_HANDLE_MISSING",
        (
            "cleanup_handle_missing",
            "cleanup_handle_unresolved",
        ),
    ),
    (
        "CAPABILITY_LOSS_ROLLBACK_FAILED",
        (
            "capability_loss_rollback_failed",
            "substitute_rollback_failed",
        ),
    ),
)

VERIFIED_KEYS: tuple[str, ...] = (
    "permission_restore_verified",
    "permissions_restored",
    "capability_rebound_verified",
    "capability_restored",
    "rollback_verified",
    "rolled_back",
)

ATTEMPTED_KEYS: tuple[str, ...] = (
    "rollback_attempted",
    "cleanup_attempted",
    "recovery_attempted",
)


@dataclass(frozen=True)
class RecoveryAssessment:
    state: RecoveryState
    reason_codes: tuple[str, ...]
    reinstall_authorized: bool
    block_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "reason_codes": list(self.reason_codes),
            "reinstall_authorized": self.reinstall_authorized,
            "block_reason": self.block_reason,
        }


def classify_recovery(evidence: Mapping[str, Any] | None) -> RecoveryAssessment:
    """Decide whether recovery is verified, and whether a reinstall may follow."""

    data = dict(evidence or {})
    signals = tuple(
        code for code, keys in UNVERIFIED_SIGNALS if _truthy(data, keys)
    )
    attempted = _truthy(data, ATTEMPTED_KEYS)
    verified = _truthy(data, VERIFIED_KEYS)
    if attempted and _falsey(data, ("rolled_back", "rollback_verified", "cleanup_verified")):
        signals = signals + ("ROLLBACK_NOT_VERIFIED",)

    if signals:
        return RecoveryAssessment(
            state=RecoveryState.UNVERIFIED,
            reason_codes=signals,
            reinstall_authorized=False,
            block_reason=(
                "recovery is unverified ("
                + ", ".join(signals)
                + "); the environment state is unknown, so a full reinstall is "
                "withheld and the run stops for an operator to look at it"
            ),
        )
    if verified:
        return RecoveryAssessment(
            state=RecoveryState.VERIFIED,
            reason_codes=("RECOVERY_VERIFIED",),
            reinstall_authorized=True,
        )
    if attempted:
        return RecoveryAssessment(
            state=RecoveryState.VERIFIED,
            reason_codes=("RECOVERY_COMPLETED",),
            reinstall_authorized=True,
        )
    return RecoveryAssessment(
        state=RecoveryState.NOT_REQUIRED,
        reason_codes=("NO_RECOVERY_REQUIRED",),
        reinstall_authorized=True,
    )


def _truthy(data: Mapping[str, Any], keys: Iterable[str]) -> bool:
    return any(_value_at(data, key) is True for key in keys)


def _falsey(data: Mapping[str, Any], keys: Iterable[str]) -> bool:
    return any(_value_at(data, key) is False for key in keys)


def _value_at(data: Mapping[str, Any], dotted: str) -> Any:
    current: Any = data
    for part in dotted.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return None
        current = current[part]
    return current
