"""Classify model-provider failures and break the circuit on the fatal ones.

On 2026-09-11 the Bailian account fell into arrears mid-round. The provider
answered ``HTTP 400`` with ``Arrearage`` in the body, the agent exited part-way
through, and the platform recorded "execution failed / output is not a
structured result" — a verdict about the agent for something that was never the
agent's doing, repeated for every Trial submitted afterwards (O03).

Two things are needed to avoid repeating that: a failure taxonomy that keeps a
billing failure distinct from a bad request, and a per-route breaker that stops
feeding Trials into a provider that cannot answer. Nothing here retries: the
platform's job is to stop and attribute, not to paper over the provider.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from enum import Enum
from threading import Lock
from typing import Any, Mapping


class ProviderFailureClass(str, Enum):
    """Why a model call failed, in the terms an operator needs to act on."""

    NONE = "NONE"
    ARREARAGE = "ARREARAGE"
    AUTHENTICATION = "AUTHENTICATION"
    RATE_LIMIT = "RATE_LIMIT"
    UPSTREAM_ERROR = "UPSTREAM_ERROR"
    TIMEOUT = "TIMEOUT"
    NETWORK = "NETWORK"
    BAD_REQUEST = "BAD_REQUEST"
    UNKNOWN = "UNKNOWN"

    @property
    def is_provider_fault(self) -> bool:
        """Whether the platform, not the agent, owns this outcome."""
        return self in _PROVIDER_FAULT_CLASSES


_PROVIDER_FAULT_CLASSES = frozenset(
    {
        ProviderFailureClass.ARREARAGE,
        ProviderFailureClass.AUTHENTICATION,
        ProviderFailureClass.RATE_LIMIT,
        ProviderFailureClass.UPSTREAM_ERROR,
        ProviderFailureClass.TIMEOUT,
        ProviderFailureClass.NETWORK,
    }
)

# A provider that is out of money says so in the body, not in the status line:
# Bailian returns HTTP 400 "Arrearage", OpenAI-compatible gateways return
# "insufficient_quota", and others word it as a balance or billing problem.
_ARREARAGE_MARKERS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\barrearage\b",
        r"\binsufficient[_ ]quota\b",
        r"\binsufficient[_ ]balance\b",
        r"\bquota[_ ]exceeded\b",
        r"\bexceeded your current quota\b",
        r"\baccount (?:is )?(?:in arrears|suspended for billing)\b",
        r"\bbilling[_ ](?:hard[_ ])?limit\b",
        # Bailian/DashScope says neither "arrearage" nor "quota": it returns a
        # 400 whose body asks you to keep the account "in good standing" and
        # links to an #overdue-payment help anchor. Built from the issue text
        # alone, this classifier read the real one as BAD_REQUEST -- the exact
        # mistake O03 exists to stop. Corpus evidence: the single real upstream
        # error in the BladeAI run set (D2-incomplete-20260911-2234).
        r"\boverdue[-_ ]payment\b",
        r"\baccount is in good standing\b",
        # Anthropic's wording for the same condition.
        r"\bcredit balance is too low\b",
        r"欠费",
        r"余额不足",
        r"账户余额",
    )
)
_AUTHENTICATION_MARKERS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\binvalid[_ ]api[_ ]key\b",
        r"\binvalid[_ ]token\b",
        r"\bauthentication[_ ](?:error|required|failed)\b",
        r"\bunauthorized\b",
        r"\bincorrect api key\b",
    )
)
_RATE_LIMIT_MARKERS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\brate[_ ]limit\b",
        r"\btoo many requests\b",
        r"\brequests per minute\b",
        r"\bthrottl",
    )
)

MAX_CLASSIFIED_BODY_BYTES = 4096


@dataclass(frozen=True)
class ProviderFailure:
    """One classified provider outcome, safe to put in a summary."""

    route_key: str
    failure_class: ProviderFailureClass
    status_code: int | None = None
    detail: str = ""
    model_alias: str = ""
    provider: str = ""
    trial_id: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "route_key": self.route_key,
            "failure_class": self.failure_class.value,
            "status_code": self.status_code,
            "detail": self.detail,
            "model_alias": self.model_alias,
            "provider": self.provider,
            "trial_id": self.trial_id,
            "provider_fault": self.failure_class.is_provider_fault,
        }


def route_key(provider: str, model_alias: str) -> str:
    """Identify one upstream route; the breaker never spans two providers."""
    return f"{(provider or 'unknown').strip().lower()}:{(model_alias or 'unknown').strip()}"


def classify_provider_failure(
    *,
    status_code: int | None = None,
    body: bytes | str | None = None,
    error_type: str | None = None,
) -> ProviderFailureClass:
    """Map a provider response, or a transport error, onto one failure class."""

    if error_type:
        lowered = error_type.lower()
        if "timeout" in lowered:
            return ProviderFailureClass.TIMEOUT
        return ProviderFailureClass.NETWORK

    text = _body_text(body)
    if status_code is None:
        return ProviderFailureClass.UNKNOWN
    if 200 <= status_code < 300:
        return ProviderFailureClass.NONE

    # The body decides before the status does: a billing failure arrives as a
    # 400 on one provider and a 403 on another, and reading it as "bad request"
    # is what turned a payment problem into an agent verdict.
    if _matches(text, _ARREARAGE_MARKERS):
        return ProviderFailureClass.ARREARAGE
    if status_code == 429 or _matches(text, _RATE_LIMIT_MARKERS):
        return ProviderFailureClass.RATE_LIMIT
    if status_code in {401, 403} or _matches(text, _AUTHENTICATION_MARKERS):
        return ProviderFailureClass.AUTHENTICATION
    if 500 <= status_code < 600:
        return ProviderFailureClass.UPSTREAM_ERROR
    if 400 <= status_code < 500:
        return ProviderFailureClass.BAD_REQUEST
    return ProviderFailureClass.UNKNOWN


def _body_text(body: bytes | str | None) -> str:
    if body is None:
        return ""
    if isinstance(body, bytes):
        return body[:MAX_CLASSIFIED_BODY_BYTES].decode("utf-8", errors="replace")
    return body[:MAX_CLASSIFIED_BODY_BYTES]


def _matches(text: str, patterns: tuple[re.Pattern[str], ...]) -> bool:
    return any(pattern.search(text) for pattern in patterns)


_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"sk-[A-Za-z0-9_-]{8,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE),
    re.compile(r"(?i)(api[_-]?key|token|password|secret)\s*[:=]\s*[\"']?[^\"'\s,}]+"),
)
MAX_DETAIL_CHARS = 240


def failure_detail(body: bytes | str | None) -> str:
    """A short, scrubbed excerpt of a provider error, safe for a summary."""
    text = _body_text(body).strip()
    if not text:
        return ""
    message = _error_message(text) or text
    for pattern in _SECRET_PATTERNS:
        message = pattern.sub("[redacted]", message)
    message = " ".join(message.split())
    return message[:MAX_DETAIL_CHARS]


def _error_message(text: str) -> str:
    import json

    try:
        document = json.loads(text)
    except (ValueError, UnicodeError):
        return ""
    if not isinstance(document, dict):
        return ""
    error = document.get("error")
    if isinstance(error, dict):
        for key in ("message", "code", "type"):
            value = error.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    for key in ("message", "error_description", "msg", "detail"):
        value = document.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass(frozen=True)
class CircuitPolicy:
    """How tolerant to be per class before a route stops accepting Trials."""

    failure_threshold: int = 3
    open_seconds: float = 300.0
    # A provider that is out of money or rejecting the credential will not
    # recover by being asked again, so one observation is enough.
    immediate_classes: frozenset[ProviderFailureClass] = field(
        default_factory=lambda: frozenset(
            {ProviderFailureClass.ARREARAGE, ProviderFailureClass.AUTHENTICATION}
        )
    )


@dataclass
class _RouteState:
    consecutive_failures: int = 0
    state: CircuitState = CircuitState.CLOSED
    opened_at: float | None = None
    failure_class: ProviderFailureClass = ProviderFailureClass.NONE
    last_detail: str = ""
    last_status_code: int | None = None
    trips: int = 0


class ProviderCircuitBreaker:
    """Per-route breaker: one provider's outage never blocks another's Trials."""

    def __init__(
        self,
        policy: CircuitPolicy | None = None,
        *,
        clock: Any = time.monotonic,
    ) -> None:
        self.policy = policy or CircuitPolicy()
        self._clock = clock
        self._lock = Lock()
        self._routes: dict[str, _RouteState] = {}

    def record(self, failure: ProviderFailure) -> CircuitState:
        """Record one classified outcome and return the resulting route state."""
        if failure.failure_class is ProviderFailureClass.NONE:
            return self.record_success(failure.route_key)
        if not failure.failure_class.is_provider_fault:
            # A malformed request is the caller's problem, not the route's.
            return self.state(failure.route_key)
        with self._lock:
            route = self._routes.setdefault(failure.route_key, _RouteState())
            self._expire_locked(route)
            route.consecutive_failures += 1
            route.failure_class = failure.failure_class
            route.last_detail = failure.detail
            route.last_status_code = failure.status_code
            immediate = failure.failure_class in self.policy.immediate_classes
            if immediate or route.consecutive_failures >= self.policy.failure_threshold:
                if route.state is not CircuitState.OPEN:
                    route.trips += 1
                route.state = CircuitState.OPEN
                route.opened_at = self._clock()
            return route.state

    def record_success(self, key: str) -> CircuitState:
        with self._lock:
            route = self._routes.get(key)
            if route is None:
                return CircuitState.CLOSED
            # A success closes the route outright: half-open exists so one probe
            # can prove recovery, not so a recovered provider stays penalised.
            route.consecutive_failures = 0
            route.state = CircuitState.CLOSED
            route.opened_at = None
            route.failure_class = ProviderFailureClass.NONE
            route.last_detail = ""
            route.last_status_code = None
            return route.state

    def state(self, key: str) -> CircuitState:
        with self._lock:
            route = self._routes.get(key)
            if route is None:
                return CircuitState.CLOSED
            self._expire_locked(route)
            return route.state

    def allows(self, key: str) -> bool:
        """Whether a new Trial may be submitted on this route."""
        return self.state(key) is not CircuitState.OPEN

    def rejection_reason(self, key: str) -> str | None:
        """A submission rejection an operator can act on without reading logs."""
        with self._lock:
            route = self._routes.get(key)
            if route is None:
                return None
            self._expire_locked(route)
            if route.state is not CircuitState.OPEN:
                return None
            status = f" (HTTP {route.last_status_code})" if route.last_status_code else ""
            detail = f": {route.last_detail}" if route.last_detail else ""
            remaining = self._remaining_locked(route)
            return (
                f"model provider circuit is open for route {key}; "
                f"classified as {route.failure_class.value}{status}{detail}. "
                f"New Trials on this route are refused for another {remaining:.0f}s, "
                "and are admitted again after one successful call."
            )

    def snapshot(self, key: str) -> dict[str, Any]:
        with self._lock:
            route = self._routes.get(key)
            if route is None:
                return {
                    "route_key": key,
                    "state": CircuitState.CLOSED.value,
                    "failure_class": ProviderFailureClass.NONE.value,
                    "consecutive_failures": 0,
                    "trips": 0,
                    "opens_for_seconds": 0.0,
                }
            self._expire_locked(route)
            return {
                "route_key": key,
                "state": route.state.value,
                "failure_class": route.failure_class.value,
                "consecutive_failures": route.consecutive_failures,
                "trips": route.trips,
                "opens_for_seconds": self._remaining_locked(route),
                "last_status_code": route.last_status_code,
                "last_detail": route.last_detail,
            }

    def snapshot_all(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            keys = list(self._routes)
        return {key: self.snapshot(key) for key in keys}

    def reset(self) -> None:
        with self._lock:
            self._routes.clear()

    def _expire_locked(self, route: _RouteState) -> None:
        if route.state is not CircuitState.OPEN or route.opened_at is None:
            return
        if self._clock() - route.opened_at >= self.policy.open_seconds:
            # Half-open lets exactly one Trial through to find out whether the
            # provider is back; it does not by itself declare recovery.
            route.state = CircuitState.HALF_OPEN
            route.consecutive_failures = 0

    def _remaining_locked(self, route: _RouteState) -> float:
        if route.opened_at is None:
            return 0.0
        return max(0.0, self.policy.open_seconds - (self._clock() - route.opened_at))


_DEFAULT_BREAKER = ProviderCircuitBreaker()


def default_breaker() -> ProviderCircuitBreaker:
    """The Controller-wide breaker shared by the relay and the submission gate."""
    return _DEFAULT_BREAKER


def failure_attribution(failures: Mapping[str, Any] | list[ProviderFailure]) -> dict[str, Any]:
    """Summarize provider faults so a Trial summary can name the real cause."""
    rows = list(failures) if isinstance(failures, list) else []
    provider_faults = [row for row in rows if row.failure_class.is_provider_fault]
    if not provider_faults:
        return {"provider_fault": False, "failure_classes": [], "failures": []}
    ordered: list[str] = []
    for row in provider_faults:
        if row.failure_class.value not in ordered:
            ordered.append(row.failure_class.value)
    return {
        "provider_fault": True,
        "failure_classes": ordered,
        # The first provider fault is the one that ended the Trial; later rows
        # are usually the same outage observed again.
        "primary_failure_class": provider_faults[0].failure_class.value,
        "failures": [row.as_dict() for row in provider_faults],
    }
