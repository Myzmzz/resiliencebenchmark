from __future__ import annotations

import uvicorn

from .api import CampaignSupervisor, create_app
from .runtime_factory import Stage2RuntimeConfig, Stage2System


def main() -> None:
    system = Stage2System(Stage2RuntimeConfig.from_env())
    app = create_app(CampaignSupervisor(system))
    uvicorn.run(app, host="0.0.0.0", port=8080, log_level="info")


if __name__ == "__main__":
    main()
