from __future__ import annotations

import os
from pathlib import Path

import uvicorn

from .api import CampaignSupervisor, create_app
from .runtime_lock import RuntimeLock
from .runtime_factory import Stage2RuntimeConfig, Stage2System
from .task_service import Stage2TaskService
from .lx import LxService


def main() -> None:
    config = Stage2RuntimeConfig.from_env()
    system = Stage2System(config)
    supervisor = CampaignSupervisor(system, runtime_lock=RuntimeLock.from_environment())
    task_service = Stage2TaskService(
        supervisor=supervisor,
        artifact_root=config.artifact_root,
        repo_root=config.repo_root,
        preflight_provider=system.preflight,
        control_backend=system,
    )
    lx_service = LxService(
        task_service=task_service,
        artifact_root=config.artifact_root,
        gateway_audit_root=Path(os.environ.get("RESBENCH_GATEWAY_AUDIT_DIR", "/var/lib/resbench-stage2/gateway-audit")),
    )
    frontend_value = os.environ.get("STAGE2_FRONTEND_ROOT", "").strip()
    frontend_root = Path(frontend_value).resolve() if frontend_value else None
    app = create_app(
        supervisor,
        artifact_root=config.artifact_root,
        preflight_provider=system.preflight,
        qualification_inventory=system.d0_gate.inventory,
        frontend_root=frontend_root,
        task_service=task_service,
        lx_service=lx_service,
    )
    uvicorn.run(app, host="0.0.0.0", port=8080, log_level="info")


if __name__ == "__main__":
    main()
