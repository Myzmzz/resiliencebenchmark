"""Entry point: ``python -m fleet_service``.

Runs in the Controller image; only the start command differs.
"""

from __future__ import annotations

import os
from pathlib import Path

import uvicorn

from stage2_service.runtime_factory import write_incluster_kubeconfig

from .api import create_app
from .controller_client import ControllerClient
from .kube import KubeClient
from .provisioner import Provisioner
from .scheduler import BatchDispatcher
from .store import FleetStore


def main() -> None:
    state_path = Path(os.environ.get("FLEET_STATE_FILE", "/var/lib/resbench-fleet/fleet.sqlite3"))
    repo_root = Path(os.environ.get("STAGE2_REPO_ROOT", "/app"))
    # deploy_application.py refuses to mutate a cluster without an explicit
    # kubeconfig, so write one from this Pod's own projected credential.
    kubeconfig = os.environ.get("FLEET_KUBECONFIG") or str(state_path.parent / "fleet.kubeconfig")
    if not os.environ.get("FLEET_KUBECONFIG"):
        state_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        write_incluster_kubeconfig(Path(kubeconfig))
    runtime_env_file = os.environ.get("STAGE2_RUNTIME_ENV_FILE") or None
    timeout = float(os.environ.get("FLEET_CONTROLLER_TIMEOUT_SECONDS", "30"))

    store = FleetStore(state_path)
    kube = KubeClient(kubeconfig=kubeconfig)
    client_factory = lambda url: ControllerClient(url, timeout=timeout)  # noqa: E731
    provisioner = Provisioner(
        store, kube=kube, repo_root=repo_root, kubeconfig=kubeconfig,
        runtime_env_file=runtime_env_file, client_factory=client_factory,
    )
    dispatcher = BatchDispatcher(
        store, client_factory=client_factory,
        poll_seconds=float(os.environ.get("FLEET_POLL_SECONDS", "20")),
    )
    dispatcher.start()
    app = create_app(store=store, provisioner=provisioner, dispatcher=dispatcher,
                     client_factory=client_factory)
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("FLEET_PORT", "8090")), log_level="info")


if __name__ == "__main__":
    main()
