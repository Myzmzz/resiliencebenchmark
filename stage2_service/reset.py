"""One-click OTel Demo Helm uninstall/reinstall inside the test cluster."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping, Protocol

from .condition_policy import CONDITION_POLICY
from .reset_policy import ResetPolicyDecision, ResetTier, classify_reset_policy


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
        recovery_timeout_seconds: int = 180,
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

    def reset(
        self,
        trial_id: str,
        episode,
        mutation_evidence: Mapping[str, Any] | ResetPolicyDecision | None = None,
    ) -> Mapping[str, Any]:
        if mutation_evidence is not None:
            return self.reset_with_policy(trial_id, episode, mutation_evidence)
        if self.verify_only:
            return self._verify_environment(trial_id, episode, {})
        return self._full_reinstall(trial_id, episode)

    def reset_with_policy(
        self,
        trial_id: str,
        episode,
        mutation_evidence: Mapping[str, Any] | ResetPolicyDecision,
    ) -> Mapping[str, Any]:
        source_evidence = (
            {}
            if isinstance(mutation_evidence, ResetPolicyDecision)
            else dict(mutation_evidence)
        )
        decision = (
            mutation_evidence
            if isinstance(mutation_evidence, ResetPolicyDecision)
            else classify_reset_policy(source_evidence)
        )
        if decision.tier is ResetTier.T3_FULL_REINSTALL:
            result = dict(self._full_reinstall(trial_id, episode))
            return self._attach_policy(
                result, decision, verified=result.get("verified") is True
            )

        result = dict(
            self._verify_environment(trial_id, episode, source_evidence)
        )
        verified = result.get("verified") is True
        return self._attach_policy(result, decision, verified=verified)

    def _verify_environment(
        self,
        trial_id: str,
        episode,
        prior_evidence: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        qualification = dict(self.environment_gate.qualify(episode))
        traffic = dict(self.traffic_evidence.current())
        if traffic.get("business_healthy") is not True:
            traffic = dict(
                self.traffic_evidence.wait_until_healthy(
                    timeout_seconds=self.recovery_timeout_seconds,
                    stability_samples=(
                        CONDITION_POLICY["recovery_sustain_seconds"] // 10 + 1
                    ),
                )
            )
        prior_recovery = (
            prior_evidence.get("fault_absent") is True
            and prior_evidence.get("business_recovery_verified") is True
        )
        traffic_verified = traffic.get("business_healthy") is True
        return {
            "trial_id": trial_id,
            "uninstalled": False,
            "reinstalled": False,
            "verify_only": True,
            "verified": (
                qualification.get("qualified") is True
                and traffic_verified
            ),
            "verification_source": (
                "bounded_current_traffic"
                if traffic_verified
                else "unverified"
            ),
            "prior_trial_recovery_verified": prior_recovery,
            "qualification": qualification,
            "traffic_recovery": traffic,
        }

    def _full_reinstall(self, trial_id: str, episode) -> Mapping[str, Any]:
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
                timeout_seconds=self.recovery_timeout_seconds,
                stability_samples=(
                    CONDITION_POLICY["recovery_sustain_seconds"] // 10 + 1
                ),
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

    @staticmethod
    def _attach_policy(
        result: dict[str, Any], decision: ResetPolicyDecision, *, verified: bool
    ) -> Mapping[str, Any]:
        policy = decision.to_dict()
        policy.update(
            {
                "verified": verified,
                "allows_next_trial": verified,
            }
        )
        if (
            not verified
            and "RESET_VERIFICATION_MISSING" not in policy["reason_codes"]
        ):
            policy["reason_codes"].append("RESET_VERIFICATION_MISSING")
        result["verified"] = verified
        result["reset_policy"] = policy
        return result
