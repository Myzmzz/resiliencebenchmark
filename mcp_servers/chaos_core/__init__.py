"""Shared safety-gated execution core used by ChaosBlade and Chaos Mesh MCPs."""

from .contracts import ChaosControlError, ExperimentRecord, RuntimeConfig

__all__ = ["ChaosControlError", "ControlledExecutionService", "ExperimentRecord", "RuntimeConfig"]


def __getattr__(name: str):
    if name == "ControlledExecutionService":
        from .service import ControlledExecutionService

        return ControlledExecutionService
    raise AttributeError(name)
