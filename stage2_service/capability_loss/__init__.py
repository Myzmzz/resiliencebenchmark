"""D7/D8 capability-loss disturbance policy, evidence and scoring.

This package deliberately has no dependency on the campaign or evaluator.  The
main runtime adapts its canonical records into :class:`CapabilityLossFacts` and
uses the returned decisions at the MCP policy gate.
"""

from .budget import ExplorationBudget, ExplorationDecision
from .orchestrator import CapabilityLossOrchestrator, ToolDecision
from .precheck import CapabilityLossPrecheck, D7HistoricalSample, D8CanaryEvidence
from .records import (
    CapabilityLossCase,
    CapabilityLossFacts,
    CapabilityLossState,
    CapabilityLossVariant,
    D7Evidence,
    D8Evidence,
    AuthorizationState,
    HonestyState,
)
from .scoring import CapabilityLossScore, score_capability_loss
from .runtime import CapabilityLossFinish, CapabilityLossRuntime, RuntimeToolDecision
from .factory import CapabilityLossRuntimeFactory

__all__ = [
    "CapabilityLossCase", "CapabilityLossFacts", "CapabilityLossOrchestrator",
    "CapabilityLossPrecheck", "CapabilityLossScore", "CapabilityLossState",
    "CapabilityLossVariant", "D7Evidence", "D7HistoricalSample", "D8CanaryEvidence",
    "D8Evidence", "ExplorationBudget", "ExplorationDecision", "ToolDecision",
    "AuthorizationState", "HonestyState", "score_capability_loss",
    "CapabilityLossFinish", "CapabilityLossRuntime", "RuntimeToolDecision",
    "CapabilityLossRuntimeFactory",
]
