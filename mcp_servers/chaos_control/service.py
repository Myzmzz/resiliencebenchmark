"""Compatibility facade for the ChaosBlade controlled executor.

All safety, ledger, D6 and cleanup behaviour lives in :mod:`mcp_servers.chaos_core`.
This module deliberately keeps the established public imports used by existing
Harnesses and tests while avoiding a second implementation.
"""

from mcp_servers.chaos_core.backends.chaosblade import InMemoryChaosBackend, KubectlChaosBackend
from mcp_servers.chaos_core.contracts import (
    FAULT_TYPE_LABEL,
    LOGICAL_NAMESPACE_LABEL,
    OWNER_LABEL,
    OWNER_VALUE,
    RUN_ID_LABEL,
    TARGET_UID_LABEL,
    ChaosControlError,
    ChaosBackend,
    ExperimentRecord,
    RuntimeConfig,
)
from mcp_servers.chaos_core.service import (
    ControlledExecutionService,
    new_cleanup_handle,
)


class ChaosControlService(ControlledExecutionService):
    """ChaosBlade specialization of the shared controlled execution core."""

    def __init__(self, config: RuntimeConfig, backend: ChaosBackend | None = None) -> None:
        super().__init__(config, backend=backend, executor_id="chaosblade")


__all__ = [
    "ChaosBackend",
    "ChaosControlError",
    "ChaosControlService",
    "ControlledExecutionService",
    "ExperimentRecord",
    "FAULT_TYPE_LABEL",
    "InMemoryChaosBackend",
    "KubectlChaosBackend",
    "LOGICAL_NAMESPACE_LABEL",
    "OWNER_LABEL",
    "OWNER_VALUE",
    "RuntimeConfig",
    "RUN_ID_LABEL",
    "TARGET_UID_LABEL",
    "new_cleanup_handle",
]
