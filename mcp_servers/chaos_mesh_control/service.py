"""Chaos Mesh specialization of the shared controlled execution core."""

from __future__ import annotations

from mcp_servers.chaos_core.backends.chaos_mesh import ChaosMeshBackend, InMemoryChaosMeshBackend
from mcp_servers.chaos_core.contracts import ChaosBackend, ChaosControlError, RuntimeConfig
from mcp_servers.chaos_core.service import ControlledExecutionService


class ChaosMeshControlService(ControlledExecutionService):
    """Serve the same safety contract through the Chaos Mesh executor."""

    def __init__(self, config: RuntimeConfig, backend: ChaosBackend | None = None) -> None:
        super().__init__(config, backend=backend or ChaosMeshBackend(config.kubectl_path), executor_id="chaos_mesh")


__all__ = ["ChaosControlError", "ChaosMeshBackend", "ChaosMeshControlService", "InMemoryChaosMeshBackend", "RuntimeConfig"]
