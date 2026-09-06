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
        """Return a stable MCP error payload without configuration values."""

        return {"code": self.code, "message": self.message, "action": self.action}


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
            raise ScopeError(
                "time_window_out_of_scope",
                "The requested observation interval exceeds the Controller limit.",
                "Use a shorter interval within the current experiment window.",
            )
