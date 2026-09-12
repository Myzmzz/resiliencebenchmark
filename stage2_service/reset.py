"""One-click OTel Demo Helm uninstall/reinstall inside the test cluster."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping, Protocol

from .condition_policy import CONDITION_POLICY
from .reset_policy import ResetPolicyDecision, ResetTier, classify_reset_policy


# ``deploy_application.py --server-dry-run`` reports this result only when
# every write of the reinstall passed the API server's dry run.
PREFLIGHT_PASSED_RESULT = "server-dry-run-passed"
_EXCERPT_CHARS = 1500
_MESSAGE_DETAIL_CHARS = 400
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)(password|token|secret|api[_-]?key)\s*[:=]\s*\S+"
)
_SECRET_WORD_RE = re.compile(r"(?i)password|token|secret|api[_-]?key")


class ResetError(RuntimeError):
    """A reset step failed; ``evidence`` records which step failed and why."""

    def __init__(
        self, message: str, *, evidence: Mapping[str, Any] | None = None
    ):
        super().__init__(message)
        self.evidence: dict[str, Any] = dict(evidence or {})


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
            if not decision.reinstall_authorized:
                # Recovery is unverified, so the platform does not know what it
                # would be reinstalling over. It stops here with the system
                # under test untouched, rather than uninstalling it (O04).
                return self._recovery_unverified(trial_id, decision)
            result = dict(self._full_reinstall(trial_id, episode))
            return self._attach_policy(
                result, decision, verified=result.get("verified") is True
            )

        result = dict(
            self._verify_environment(trial_id, episode, source_evidence)
        )
        verified = result.get("verified") is True
        return self._attach_policy(result, decision, verified=verified)

    def _recovery_unverified(
        self, trial_id: str, decision: ResetPolicyDecision
    ) -> Mapping[str, Any]:
        """Report an unknown environment without touching it."""
        result = {
            "trial_id": trial_id,
            "uninstalled": False,
            "reinstalled": False,
            "verify_only": False,
            "verified": False,
            "recovery_state": decision.recovery_state.value,
            "reinstall_withheld": True,
            "reason": decision.reinstall_block_reason,
        }
        return self._attach_policy(result, decision, verified=False)

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
        inventory_safe = prior_evidence.get("chaos_inventory_clear") is not False
        traffic_verified = traffic.get("business_healthy") is True
        return {
            "trial_id": trial_id,
            "uninstalled": False,
            "reinstalled": False,
            "verify_only": True,
            "verified": (
                qualification.get("qualified") is True
                and traffic_verified
                and inventory_safe
            ),
            "verification_source": (
                "bounded_current_traffic"
                if traffic_verified
                else "unverified"
            ),
            "prior_trial_recovery_verified": prior_recovery,
            "fault_inventory_safe": inventory_safe,
            "qualification": qualification,
            "traffic_recovery": traffic,
        }

    def _full_reinstall(self, trial_id: str, episode) -> Mapping[str, Any]:
        env = {
            **os.environ,
            "KUBECONFIG": str(self.kubeconfig),
            "OTEL_DEMO_CHART_FILE": str(self.chart_file),
        }
        with tempfile.TemporaryDirectory(
            prefix=f"{trial_id}-reset-", dir=self.kubeconfig.parent
        ) as raw_private:
            private_runtime = Path(raw_private) / "otel-demo.env"
            shutil.copyfile(self.runtime_env_file, private_runtime)
            private_runtime.chmod(0o600)
            # The uninstall below deletes the system under test, so it runs
            # only after a server-side dry run of the exact reinstall passed.
            # A failed preflight raises here and leaves OTel Demo installed.
            preflight = self._reinstall_preflight(private_runtime, env)
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
                raise ResetError(
                    "OTel Demo Helm uninstall failed",
                    evidence={
                        "stage": "uninstall",
                        "reinstall_preflight": preflight,
                        "uninstall_exit_code": uninstall.returncode,
                        "uninstall_stderr_excerpt": _excerpt(uninstall.stderr),
                    },
                )
            deploy = self.runner.run(
                self._deploy_argv(private_runtime, server_dry_run=False),
                env=env,
                timeout=self.timeout_seconds + 180,
            )
        if deploy.returncode:
            detail = (deploy.stderr or deploy.stdout).strip().replace("\n", " ")[-600:]
            raise ResetError(
                f"OTel Demo reinstallation failed: {detail}",
                evidence={
                    "stage": "reinstall",
                    "uninstalled": True,
                    "reinstall_preflight": preflight,
                    "reinstall_exit_code": deploy.returncode,
                },
            )
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
            "reinstall_preflight": preflight,
            "qualification": qualification,
            "traffic_recovery": traffic,
        }

    def _deploy_argv(self, private_runtime: Path, *, server_dry_run: bool) -> list[str]:
        """Build the ``deploy_application.py`` reinstall command.

        The preflight and the real reinstall share this argv and differ only
        in ``--server-dry-run`` versus ``--execute``, so the preflight covers
        the exact chart, values, namespace and Helm flags of the reinstall.
        """
        return [
            sys.executable,
            str(self.repo_root / "scripts/deploy_application.py"),
            "--application",
            "otel-demo",
            "--mode",
            "apply",
            "--server-dry-run" if server_dry_run else "--execute",
            "--kubeconfig",
            str(self.kubeconfig),
            "--runtime-env-file",
            str(private_runtime),
            "--timeout",
            str(self.timeout_seconds),
        ]

    def _reinstall_preflight(
        self, private_runtime: Path, env: Mapping[str, str]
    ) -> dict[str, Any]:
        """Server-side dry run of the reinstall, before anything is uninstalled.

        Returns the preflight evidence when ``deploy_application.py`` reports
        that every write of the reinstall passed the API server's dry run.
        Any other outcome (non-zero exit, timeout, launch failure or a report
        that is not a passing one) raises ResetError carrying the evidence.
        """
        argv = self._deploy_argv(private_runtime, server_dry_run=True)
        evidence: dict[str, Any] = {
            "passed": False,
            "command": _redacted_command(argv),
            "exit_code": None,
            "timed_out": False,
        }
        try:
            completed = self.runner.run(
                argv, env=env, timeout=self.timeout_seconds + 180
            )
        except subprocess.TimeoutExpired as exc:
            evidence["timed_out"] = True
            evidence["stderr_excerpt"] = _excerpt(_deploy_diagnostic(exc.stderr))
            raise _preflight_error("timed out", evidence) from exc
        except Exception as exc:  # noqa: BLE001 - any failure must block the uninstall.
            evidence["error_type"] = type(exc).__name__
            evidence["stderr_excerpt"] = _excerpt(str(exc))
            raise _preflight_error(f"could not run ({type(exc).__name__})", evidence) from exc
        evidence["exit_code"] = completed.returncode
        evidence["stderr_excerpt"] = _excerpt(_deploy_diagnostic(completed.stderr))
        if completed.returncode != 0:
            raise _preflight_error(f"exit code {completed.returncode}", evidence)
        report = _json_object(completed.stdout)
        if report.get("result") != PREFLIGHT_PASSED_RESULT:
            raise _preflight_error(
                "output is not a passing deploy_application.py server dry-run report",
                evidence,
            )
        dry_run = report.get("serverDryRun")
        dry_run = dry_run if isinstance(dry_run, Mapping) else {}
        evidence["passed"] = True
        evidence["checks"] = [str(item) for item in dry_run.get("checks") or []]
        evidence["not_simulated"] = [
            str(item) for item in dry_run.get("notSimulated") or []
        ]
        return evidence

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


def _preflight_error(reason: str, preflight: Mapping[str, Any]) -> ResetError:
    """ResetError for a failed preflight; nothing has been uninstalled yet."""
    detail = str(preflight.get("stderr_excerpt") or "no diagnostic output")
    return ResetError(
        "reinstall preflight failed; OTel Demo was left installed: "
        f"{reason}: {detail[-_MESSAGE_DETAIL_CHARS:]}",
        evidence={
            "stage": "reinstall_preflight",
            "uninstall_attempted": False,
            "uninstalled": False,
            "reinstalled": False,
            "reinstall_preflight": dict(preflight),
        },
    )


def _deploy_diagnostic(stderr: str | bytes | None) -> str:
    """The ``error`` field of deploy_application.py's failure JSON, else raw stderr."""
    text = stderr.decode("utf-8", errors="replace") if isinstance(stderr, bytes) else stderr or ""
    error = _json_object(text).get("error")
    return error if isinstance(error, str) and error else text


def _excerpt(value: str | bytes | None, limit: int = _EXCERPT_CHARS) -> str:
    """Whitespace-collapsed, secret-redacted tail of diagnostic output."""
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    text = " ".join(str(value or "").split())
    return _SECRET_ASSIGNMENT_RE.sub(r"\1=<redacted>", text)[-limit:]


def _json_object(text: str | None) -> dict[str, Any]:
    """Parse ``text`` as one JSON object; anything else yields an empty dict."""
    try:
        payload = json.loads(text or "")
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _redacted_command(argv: list[str]) -> str:
    """Shell-quoted command line with secret-looking arguments redacted."""
    return shlex.join(
        "<redacted-argument>" if _SECRET_WORD_RE.search(item) else item
        for item in argv
    )
