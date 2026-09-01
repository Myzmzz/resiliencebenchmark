"""One-click OTel Demo Helm uninstall/reinstall inside the test cluster."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping, Protocol


class ResetError(RuntimeError):
    pass


class ResetRunner(Protocol):
    def run(
        self,
        argv: list[str],
        *,
        env: Mapping[str, str],
        timeout: int,
    ) -> subprocess.CompletedProcess[str]: ...


class SubprocessResetRunner:
    def run(self, argv, *, env, timeout):
        return subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            shell=False,
            env=dict(env),
            timeout=timeout,
        )


class OtelDemoResetter:
    def __init__(
        self,
        *,
        repo_root: Path,
        kubeconfig: Path,
        runtime_env_file: Path,
        chart_file: Path,
        environment_gate,
        traffic_evidence,
        runner: ResetRunner | None = None,
        timeout_seconds: int = 900,
        recovery_timeout_seconds: int = 300,
        verify_only: bool = False,
    ):
        self.repo_root = repo_root.resolve()
        self.kubeconfig = kubeconfig.resolve()
        self.runtime_env_file = runtime_env_file.resolve()
        self.chart_file = chart_file.resolve()
        if not self.chart_file.is_file() or self.chart_file.is_symlink():
            raise ResetError("pinned OTel Demo chart is missing or unsafe")
        self.environment_gate = environment_gate
        self.traffic_evidence = traffic_evidence
        self.runner = runner or SubprocessResetRunner()
        self.timeout_seconds = timeout_seconds
        self.recovery_timeout_seconds = recovery_timeout_seconds
        self.verify_only = verify_only

    def reset(self, trial_id: str, episode) -> Mapping[str, Any]:
        if self.verify_only:
            qualification = dict(self.environment_gate.qualify(episode))
            # Stage2Finalizer already performs the bounded traffic recovery loop.
            # Repeating it here both resets the Oracle evidence a second time and
            # consumes the remaining Trial budget.  This final gate is deliberately
            # a fresh one-shot qualification after permission restoration.
            traffic = dict(self.traffic_evidence.current())
            return {
                "trial_id": trial_id,
                "uninstalled": False,
                "reinstalled": False,
                "verify_only": True,
                "verified": (
                    qualification.get("qualified") is True
                    and traffic.get("business_healthy") is True
                ),
                "qualification": qualification,
                "traffic_recovery": traffic,
            }
        env = {
            **os.environ,
            "KUBECONFIG": str(self.kubeconfig),
            "OTEL_DEMO_CHART_FILE": str(self.chart_file),
        }
        uninstall = self.runner.run(
            [
                "helm",
                "uninstall",
                "otel-demo",
                "--namespace",
                "otel-demo",
                "--wait",
                "--timeout",
                f"{self.timeout_seconds}s",
            ],
            env=env,
            timeout=self.timeout_seconds + 60,
        )
        if uninstall.returncode and "release: not found" not in uninstall.stderr.lower():
            raise ResetError("OTel Demo Helm uninstall failed")
        with tempfile.TemporaryDirectory(
            prefix=f"{trial_id}-reset-", dir=self.kubeconfig.parent
        ) as raw_private:
            private_runtime = Path(raw_private) / "otel-demo.env"
            shutil.copyfile(self.runtime_env_file, private_runtime)
            private_runtime.chmod(0o600)
            deploy = self.runner.run(
                [
                    sys.executable,
                    str(self.repo_root / "scripts/deploy_application.py"),
                    "--application",
                    "otel-demo",
                    "--mode",
                    "apply",
                    "--execute",
                    "--kubeconfig",
                    str(self.kubeconfig),
                    "--runtime-env-file",
                    str(private_runtime),
                    "--timeout",
                    str(self.timeout_seconds),
                ],
                env=env,
                timeout=self.timeout_seconds + 180,
            )
        if deploy.returncode:
            detail = (deploy.stderr or deploy.stdout).strip().replace("\n", " ")[-600:]
            raise ResetError(f"OTel Demo reinstallation failed: {detail}")
        qualification = dict(self.environment_gate.qualify(episode))
        traffic = dict(
            self.traffic_evidence.reset_and_wait_healthy(
                timeout_seconds=self.recovery_timeout_seconds
            )
        )
        return {
            "trial_id": trial_id,
            "uninstalled": True,
            "reinstalled": True,
            "verified": (
                qualification.get("qualified") is True
                and traffic.get("business_healthy") is True
            ),
            "qualification": qualification,
            "traffic_recovery": traffic,
        }
