"""Common validation for Controller-owned observation scopes.

The caller never chooses a namespace, application identity, or time-window
ceiling.  Observation services receive those values at process start from the
Controller and only accept query refinements that are safe inside that scope.
"""

from __future__ import annotations

from dataclasses import dataclass
import re


MAX_OBSERVATION_WINDOW_SECONDS = 6 * 60 * 60
_NAMESPACE_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")


class ScopeError(ValueError):
    """A structured, safe-to-return scope validation failure."""

    def __init__(self, code: str, message: str, action: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.action = action

    def to_dict(self) -> dict[str, str]:
        """Return a stable MCP error payload.

        It may name the time-window ceiling, which the caller needs in order
        to retry, but never the namespace or application identity.
        """

        return {"code": self.code, "message": self.message, "action": self.action}


def _describe_seconds(seconds: int) -> str:
    """Name a duration in its largest whole unit, e.g. 21600 -> "6 hours"."""

    for unit_seconds, unit in ((3600, "hour"), (60, "minute")):
        if seconds >= unit_seconds and seconds % unit_seconds == 0:
            count = seconds // unit_seconds
            return f"{count} {unit}" + ("" if count == 1 else "s")
    return f"{seconds} seconds"


@dataclass(frozen=True)
class ObservationScope:
    """One Controller-selected application and bounded observation interval."""

    namespace: str
    application: str
    max_window_seconds: int = MAX_OBSERVATION_WINDOW_SECONDS

    def __post_init__(self) -> None:
        if not _NAMESPACE_RE.fullmatch(self.namespace):
            raise ScopeError(
                "invalid_namespace_scope",
                "The Controller-provided namespace scope is invalid.",
                "Configure exactly one valid benchmark namespace.",
            )
        if not self.application.strip() or len(self.application) > 512:
            raise ScopeError(
                "invalid_application_scope",
                "The Controller-provided application scope is invalid.",
                "Configure one bounded Coroot application identifier.",
            )
        if not 1 <= self.max_window_seconds <= MAX_OBSERVATION_WINDOW_SECONDS:
            raise ScopeError(
                "invalid_time_scope",
                "The Controller-provided maximum observation window is invalid.",
                "Use a positive window no longer than six hours.",
            )

    def validate_window(self, *, start: int, end: int) -> None:
        """Reject invalid, future-unbounded, or overlong caller time ranges."""

        if start <= 0 or end <= 0 or start >= end:
            raise ScopeError(
                "invalid_time_window",
                "start and end must be positive Unix timestamps with start before end.",
                "Request one bounded historical observation interval.",
            )
        if end - start > self.max_window_seconds:
            # State the ceiling: told only that its 24-hour query was too long,
            # an Agent gave up instead of retrying shorter (2026-09-10 codex).
            limit = _describe_seconds(self.max_window_seconds)
            raise ScopeError(
                "time_window_out_of_scope",
                f"The requested observation interval is {end - start} seconds, which exceeds "
                f"the Controller limit of {self.max_window_seconds} seconds ({limit}).",
                f"Use an interval of at most {limit} within the current experiment window.",
            )
