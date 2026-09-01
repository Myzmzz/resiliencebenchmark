from __future__ import annotations

import os
from pathlib import Path

import uvicorn

from .api import CampaignSupervisor, create_app
from .runtime_factory import Stage2RuntimeConfig, Stage2System


def main() -> None:
    config = Stage2RuntimeConfig.from_env()
    system = Stage2System(config)
    app = create_app(
        CampaignSupervisor(system),
        artifact_root=config.artifact_root,
        preflight_provider=system.preflight,
        qualification_inventory=system.d0_gate.inventory,
        frontend_root=Path(
            os.environ.get("STAGE2_FRONTEND_ROOT", "/app/frontend-dist")
        ),
    )
    uvicorn.run(app, host="0.0.0.0", port=8080, log_level="info")


if __name__ == "__main__":
    main()
