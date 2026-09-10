"""Bounded D7/D8 exploration accounting independent of cleanup and confirmation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class ExplorationDecision:
    allowed: bool
    reason: str | None
    exploration_calls: int
    elapsed_seconds: float


@dataclass(frozen=True)
class ExplorationBudget:
    """The frozen qualification budget from the D7/D8 contract."""

    max_seconds: float = 180.0
    max_calls: int = 12
    max_disabled_retries: int = 3

    def consume(
        self,
        *,
        activated_at: datetime,
        now: datetime,
        existing_calls: int,
        is_cleanup_or_confirmation: bool,
    ) -> ExplorationDecision:
        elapsed = max(0.0, (now - activated_at).total_seconds())
        if is_cleanup_or_confirmation:
            return ExplorationDecision(True, None, existing_calls, elapsed)
        if elapsed >= self.max_seconds:
            return ExplorationDecision(False, "exploration_time_exhausted", existing_calls, elapsed)
        if existing_calls >= self.max_calls:
            return ExplorationDecision(False, "exploration_call_budget_exhausted", existing_calls, elapsed)
        return ExplorationDecision(True, None, existing_calls + 1, elapsed)
