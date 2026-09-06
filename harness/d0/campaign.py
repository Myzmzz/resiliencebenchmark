"""Sequential remote D0 campaign for BladeAI, Codex, Claude Code and DSH."""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import threading
import time
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .adapters import (
    AdapterResult,
    D0Adapter,
    NativeD0Adapter,
    adapter_models,
)
from .behavior import derive_agent_behavior
from .common import (
    AGENTS,
    EVALUATION_READY_STATUSES,
    EXPECTED_EXECUTION_HOST_ID,
    FIXED_PROMPT,
    append_jsonl,
    host_evidence,
    sha256_text,
    utc_now,
    write_json,
    write_manifest,
)
from .observer import KubectlD0Observer
from .inventory import collect_execution_inventory
from .visualization import generate_visualizations


SAFE_CAMPAIGN = re.compile(r"^[a-z0-9][a-z0-9-]{4,80}$")


@dataclass(frozen=True)
class D0CampaignConfig:
    repo_root: Path
    artifact_root: Path
    kubeconfig: Path
    expected_host_id: str = EXPECTED_EXECUTION_HOST_ID
    sample_seconds: int = 10
    agent_timeout_seconds: int = 720
    effect_wait_seconds: int = 300
    recovery_deadline_seconds: int = 330
    agents: tuple[str, ...] = AGENTS


class D0Campaign:
    def __init__(
        self,
        config: D0CampaignConfig,
        *,
        environment: Mapping[str, str] | None = None,
        adapters: Mapping[str, D0Adapter] | None = None,
        observer_factory=KubectlD0Observer,
        host_evidence_provider=host_evidence,
        inventory_provider=collect_execution_inventory,
        runtime_builder=None,
    ):
        self.config = config
        self.environment = dict(environment or os.environ)
        self.observer_factory = observer_factory
        self.host_evidence_provider = host_evidence_provider
        self.inventory_provider = inventory_provider
        self.runtime_builder = runtime_builder or self._build_runtime
        self._runtime_system = None
        self.models = adapter_models(self.environment)
        self.adapters = dict(adapters or self._default_adapters())

    def _default_adapters(self) -> dict[str, D0Adapter]:
        return {
            name: NativeD0Adapter(
                name=name, repo_root=self.config.repo_root, model_alias=self.models[name],
                runtime_builder=self.runtime_builder, timeout_seconds=self.config.agent_timeout_seconds,
            )
            for name in self.config.agents
        }

    def _build_runtime(self, episode, models):
        """Reuse production composition without requiring D0 to qualify itself."""
        from stage2_service.runtime_factory import Stage2RuntimeConfig, Stage2System

        if self._runtime_system is None:
            runtime_config = replace(
                Stage2RuntimeConfig.from_env(self.environment),
                repo_root=self.config.repo_root,
                kubeconfig=self.config.kubeconfig,
                private_root=self.config.artifact_root / ".runtime-private",
                artifact_root=self.config.artifact_root / ".native-runtime",
            )
            self._runtime_system = Stage2System(runtime_config)
        return self._runtime_system.build_runtime(episode, models)

    @staticmethod
    def campaign_id() -> str:
        return "d0-otel-accounting-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")

    def run(self, campaign_id: str | None = None) -> dict[str, Any]:
        host = self.host_evidence_provider(self.config.expected_host_id)
        if host["verified"] is not True:
            raise RuntimeError("WRONG_EXECUTION_HOST: D0 execute is allowed only on the declared Linux test host")
        campaign = campaign_id or self.campaign_id()
        if not SAFE_CAMPAIGN.fullmatch(campaign):
            raise ValueError("invalid campaign id")
        campaign_dir = (self.config.artifact_root / campaign).resolve()
        campaign_dir.mkdir(parents=True, exist_ok=False)
        (campaign_dir / "prompt.txt").write_text(FIXED_PROMPT + "\n", encoding="utf-8")
        (campaign_dir / "prompt.sha256").write_text(sha256_text(FIXED_PROMPT) + "\n", encoding="utf-8")
        metadata = {
            "schema_version": "d0-campaign.v1",
            "campaign_id": campaign,
            "type": "D0_FAULT_EXECUTION_QUALIFICATION",
            "status": "RUNNING",
            "started_at": utc_now(),
            "host": host,
            "prompt_sha256": sha256_text(FIXED_PROMPT),
            "agents": list(self.config.agents),
            "models": self.models,
            "execution_mode": {
                "unattended": True,
                "operator_input_allowed": False,
                "approval_handling": "shared-harness-channel",
                "execution_boundary": "agent_exec_sidecar",
                "agent_processes_run_in_controller_container": False,
                "agent_execution_transport": "agent_exec_unix_socket",
            },
            "repo_revision": self._repo_revision(),
            "results": [],
        }
        self.execution_host = host
        self.execution_inventory = self.inventory_provider(
            repo_root=self.config.repo_root,
            kubeconfig=self.config.kubeconfig,
            artifact_root=self.config.artifact_root,
            campaign_dir=campaign_dir,
            host=host,
            models=self.models,
            environment=self.environment,
            runtime_descriptors=self._runtime_descriptors(),
        )
        missing_runtimes = [
            agent
            for agent in self.config.agents
            if self.execution_inventory.get("agents", {})
            .get(agent, {})
            .get("available")
            is not True
        ]
        kube = self.execution_inventory.get("kubernetes", {})
        if missing_runtimes:
            raise RuntimeError(
                "D0 runtime inventory failed: " + ", ".join(missing_runtimes)
            )
        if self.inventory_provider is collect_execution_inventory and (
            not kube.get("context") or not kube.get("api_server_sha256")
        ):
            raise RuntimeError("D0 Kubernetes execution identity is incomplete")
        metadata["execution_inventory"] = self.execution_inventory
        write_json(campaign_dir / "campaign.json", metadata)
        for agent in self.config.agents:
            result = self._run_agent(campaign, campaign_dir, agent)
            metadata["results"].append(result)
            write_json(campaign_dir / "campaign.json", metadata)
            if result["status"] == "RESET_FAILED":
                metadata["status"] = "STOPPED_RESET_FAILED"
                break
            if result.get("foreign_crs_observed"):
                metadata["status"] = "QUALIFICATION_INVALID"
                break
        if metadata["status"] == "RUNNING":
            statuses = {value["status"] for value in metadata["results"]}
            if statuses == {"PASS"}:
                metadata["status"] = "QUALIFIED"
            elif statuses and statuses <= EVALUATION_READY_STATUSES:
                metadata["status"] = "EVALUATION_READY"
            elif statuses.intersection({"CASE_INVALID", "QUALIFICATION_INVALID", "NEEDS_HUMAN"}):
                metadata["status"] = "QUALIFICATION_INVALID"
            else:
                metadata["status"] = "QUALIFICATION_FAILED"
        metadata["finished_at"] = utc_now()
        metadata["visualization"] = generate_visualizations(campaign_dir, metadata)
        write_json(campaign_dir / "campaign.json", metadata)
        write_manifest(campaign_dir)
        return metadata | {"artifact_dir": str(campaign_dir)}

    def _runtime_descriptors(self) -> dict[str, dict[str, Any]]:
        from stage2_service.capability_preflight import harness_capabilities_from_qualification

        configured = self.environment.get("STAGE2_HARNESS_CAPABILITIES_FILE")
        descriptors, source = harness_capabilities_from_qualification(
            Path(configured) if configured else None,
        )
        return {
            name: {
                "available": descriptors[name]["qualification_passed"],
                "runtime_source": "qualification_descriptor",
                "execution_boundary": "agent_exec_sidecar",
                "capability": descriptors[name],
                "qualification": source["harnesses"][name],
            }
            for name in self.config.agents
        }

    def _repo_revision(self) -> dict[str, Any]:
        command = ["git", "-C", str(self.config.repo_root), "rev-parse", "HEAD"]
        result = subprocess.run(command, check=False, capture_output=True, text=True)
        status = subprocess.run(
            ["git", "-C", str(self.config.repo_root), "status", "--short"],
            check=False,
            capture_output=True,
            text=True,
        )
        source_paths = [
            self.config.repo_root / "harness/d0",
            self.config.repo_root / "mcp_servers/chaos_core",
            self.config.repo_root / "mcp_servers/chaos_control",
            self.config.repo_root / "stage2_service",
            self.config.repo_root / "harness/agent_exec",
            self.config.repo_root / "scripts/run_otel_accounting_cpu_matrix.py",
            self.config.repo_root / "scripts/run_harness_trial.py",
            self.config.repo_root / "scripts/audit_d0_campaign.py",
            self.config.repo_root / "scripts/sanitize_d0_artifacts.py",
            self.config.repo_root / "harness/harnesses.yaml",
            self.config.repo_root / "harness/models.yaml",
        ]
        files: list[Path] = []
        for path in source_paths:
            if path.is_dir():
                files.extend(
                    item
                    for item in path.rglob("*")
                    if item.is_file() and "__pycache__" not in item.parts
                )
            elif path.is_file():
                files.append(path)
        digest = hashlib.sha256()
        for path in sorted(set(files)):
            relative = path.relative_to(self.config.repo_root).as_posix()
            digest.update(relative.encode())
            digest.update(b"\0")
            digest.update(hashlib.sha256(path.read_bytes()).digest())
        git_available = result.returncode == 0 and status.returncode == 0
        return {
            "git_available": git_available,
            "head": result.stdout.strip() if git_available else None,
            "dirty": bool(status.stdout.strip()) if git_available else None,
            "source_tree_sha256": digest.hexdigest(),
            "source_file_count": len(set(files)),
        }

    def _event_sink(self, trial_dir: Path):
        all_path = trial_dir / "all-events.jsonl"

        def sink(value: dict[str, Any]) -> None:
            append_jsonl(all_path, value)
            actor = str(value.get("actor") or "agent")
            if actor == "agent":
                append_jsonl(trial_dir / "agent-events.jsonl", value)
                if str(value.get("kind")) in {"token", "agent_message", "message"}:
                    append_jsonl(trial_dir / "agent-responses.jsonl", value)
                if value.get("tool") or "tool" in str(value.get("kind", "")):
                    append_jsonl(trial_dir / "tool-events.jsonl", value)
            else:
                append_jsonl(trial_dir / "controller-events.jsonl", value)

        return sink

    def _run_agent(self, campaign: str, campaign_dir: Path, agent: str) -> dict[str, Any]:
        trial_id = f"{campaign}-{agent}".replace("_", "-")
        trial_dir = campaign_dir / agent
        trial_dir.mkdir(parents=True, exist_ok=False)
        sink = self._event_sink(trial_dir)
        observer = self.observer_factory(
            kubeconfig=self.config.kubeconfig,
            cleanup_kubeconfig=lambda: getattr(self.adapters[agent], "cleanup_kubeconfig", None),
            artifact_dir=trial_dir,
            trial_id=trial_id,
            sample_seconds=self.config.sample_seconds,
            include_chaos_mesh=True,
        )
        started = time.monotonic()
        before = observer.prepare()
        sink({"ts": utc_now(), "actor": "oracle", "kind": "before_sample", "payload": before})
        trial_metadata = {
            "schema_version": "d0-trial.v1",
            "trial_id": trial_id,
            "campaign_id": campaign,
            "agent": agent,
            "model": self.models[agent],
            "prompt_sha256": sha256_text(FIXED_PROMPT),
            "started_at": utc_now(),
            "execution_host": self.execution_host,
            "working_directory": str(Path.cwd()),
            "agent_runtime": self.execution_inventory.get("agents", {}).get(agent, {}),
            "model_identity": self.execution_inventory.get("models", {}).get(agent, {}),
            "oracle_target": dict(before["pods"][0]),
            "oracle_target_not_exposed_in_prompt": True,
        }
        write_json(trial_dir / "trial.json", trial_metadata)
        observer.start()
        fallback = {"requested": False, "verified": True}
        convergence = {"verified": False}
        adapter_result = None
        deadline = {
            "effect_wait_exceeded": False,
            "recovery_deadline_exceeded": False,
            "agent_cancel_requested": False,
            "agent_cancel_acknowledged": None,
            "agent_thread_stopped": None,
            "foreign_interference_observed": False,
        }
        adapter_box: list[AdapterResult] = []

        def run_adapter() -> None:
            try:
                adapter_box.append(
                    self.adapters[agent].run(
                        prompt=FIXED_PROMPT,
                        trial_id=trial_id,
                        artifact_dir=trial_dir,
                        event_sink=sink,
                    )
                )
            except Exception as exc:  # noqa: BLE001 - preserve failure and recover.
                adapter_box.append(
                    AdapterResult(
                        status="failed",
                        started_at=utc_now(),
                        finished_at=utc_now(),
                        process_status="adapter_exception",
                        artifact_ref=agent,
                        error=f"{type(exc).__name__}: D0 isolated runtime failed",
                        failure_code="HARNESS_IMPLEMENTATION_ERROR",
                    )
                )
                sink(
                    {
                        "ts": utc_now(),
                        "actor": "harness",
                        "kind": "adapter_exception",
                        "error_type": type(exc).__name__,
                    }
                )

        adapter_thread = threading.Thread(
            target=run_adapter,
            name=f"d0-{agent}-adapter",
            daemon=True,
        )
        try:
            adapter_thread.start()
            effect_deadline = time.monotonic() + self.config.effect_wait_seconds
            while True:
                now = time.monotonic()
                effect_at = observer.state.effect_monotonic
                recovered = observer.state.recovery_observed_at is not None
                if observer.state.foreign_cr_names:
                    deadline["foreign_interference_observed"] = True
                    sink(
                        {
                            "ts": utc_now(),
                            "actor": "oracle",
                            "kind": "foreign_chaos_interference_observed",
                            "payload": {
                                "cr_names": sorted(observer.state.foreign_cr_names)
                            },
                        }
                    )
                    break
                if effect_at is None and now >= effect_deadline:
                    deadline["effect_wait_exceeded"] = True
                    sink(
                        {
                            "ts": utc_now(),
                            "actor": "controller",
                            "kind": "effect_wait_deadline_exceeded",
                        }
                    )
                    break
                if (
                    effect_at is not None
                    and not recovered
                    and now >= effect_at + self.config.recovery_deadline_seconds
                ):
                    deadline["recovery_deadline_exceeded"] = True
                    sink(
                        {
                            "ts": utc_now(),
                            "actor": "controller",
                            "kind": "recovery_deadline_exceeded",
                            "payload": {
                                "effect_confirmed_at": observer.state.effect_confirmed_at,
                                "deadline_seconds": self.config.recovery_deadline_seconds,
                            },
                        }
                    )
                    break
                if effect_at is not None and recovered:
                    break
                time.sleep(0.2)

            if adapter_thread.is_alive() and (
                deadline["effect_wait_exceeded"]
                or deadline["recovery_deadline_exceeded"]
                or deadline["foreign_interference_observed"]
            ):
                deadline["agent_cancel_requested"] = True
                cancel = getattr(self.adapters[agent], "cancel", None)
                deadline["agent_cancel_acknowledged"] = bool(cancel and cancel())
                sink(
                    {
                        "ts": utc_now(),
                        "actor": "controller",
                        "kind": "agent_cancel_requested",
                        "payload": {
                            "acknowledged": deadline[
                                "agent_cancel_acknowledged"
                            ]
                        },
                    }
                )
                adapter_thread.join(timeout=15)

            if observer.state.new_cr_names and observer.state.recovery_observed_at is None:
                sink({"ts": utc_now(), "actor": "controller", "kind": "fallback_cleanup_started"})
                fallback = observer.fallback_cleanup()
                sink({"ts": utc_now(), "actor": "controller", "kind": "fallback_cleanup_finished", "payload": fallback})
                time.sleep(min(5, self.config.sample_seconds))
            if adapter_thread.is_alive() and observer.state.recovery_observed_at is not None:
                adapter_thread.join(timeout=60)
                if adapter_thread.is_alive():
                    deadline["agent_cancel_requested"] = True
                    cancel = getattr(self.adapters[agent], "cancel", None)
                    deadline["agent_cancel_acknowledged"] = bool(cancel and cancel())
                    adapter_thread.join(timeout=15)
            deadline["agent_thread_stopped"] = not adapter_thread.is_alive()
            adapter_result = adapter_box[0] if adapter_box else None
            convergence = observer.wait_recovery_convergence(timeout_seconds=120)
            # A cancelled model stream may already have dispatched one final
            # chaos_create call.  Require a quiet window after apparent
            # convergence so a late CR cannot leak into the next Agent Trial.
            if self.observer_factory is KubectlD0Observer:
                time.sleep(10)
            late_sample = observer.snapshot()
            late_owned = {
                str(item.get("name") or "")
                for item in late_sample.get("chaosblades", [])
                if str(item.get("name") or "") in observer.state.new_cr_names
            }
            if late_owned:
                sink(
                    {
                        "ts": utc_now(),
                        "actor": "controller",
                        "kind": "late_owned_chaos_detected",
                        "payload": {"names": sorted(late_owned)},
                    }
                )
                fallback = observer.fallback_cleanup()
                sink(
                    {
                        "ts": utc_now(),
                        "actor": "controller",
                        "kind": "late_owned_chaos_cleanup_finished",
                        "payload": fallback,
                    }
                )
                convergence = observer.wait_recovery_convergence(
                    timeout_seconds=120
                )
            sink(
                {
                    "ts": utc_now(),
                    "actor": "oracle",
                    "kind": "post_recovery_convergence",
                    "payload": convergence,
                }
            )
        finally:
            observer.stop()
        result = self._result(
            agent,
            trial_dir,
            observer,
            adapter_result,
            fallback,
            convergence,
            started,
            derive_agent_behavior(trial_dir),
            deadline,
        )
        trial_metadata["finished_at"] = utc_now()
        trial_metadata["adapter"] = result.get("adapter", {})
        write_json(trial_dir / "trial.json", trial_metadata)
        write_json(
            trial_dir / "recovery.json",
            {
                "schema_version": "d0-recovery.v1",
                "trial_id": trial_id,
                "agent_recovery_requested": result.get("agent_recovery_requested"),
                "recovery_observed": result.get("recovery_observed"),
                "recovery_observed_at": result.get("recovery_observed_at"),
                "fallback_cleanup_used": result.get("fallback_cleanup_used"),
                "fallback": result.get("fallback"),
                "post_recovery_convergence": result.get("post_recovery_convergence"),
                "controller_deadline": result.get("controller_deadline"),
                "status": result.get("status"),
            },
        )
        write_json(trial_dir / "result.json", result)
        write_manifest(trial_dir)
        return result

    @staticmethod
    def _result(
        agent,
        trial_dir,
        observer,
        adapter_result,
        fallback,
        convergence,
        started,
        behavior,
        deadline,
    ):
        adapter = asdict(adapter_result) if adapter_result is not None else {}
        effect = observer.state.effect_monotonic is not None
        recovered = observer.state.recovery_observed_at is not None
        fallback_used = bool(fallback.get("requested"))
        duration = observer.fault_duration_seconds()
        if (
            duration is None
            and observer.state.effect_confirmed_at
            and fallback_used
            and fallback.get("ts")
        ):
            effect_time = datetime.fromisoformat(
                observer.state.effect_confirmed_at.replace("Z", "+00:00")
            )
            fallback_time = datetime.fromisoformat(
                str(fallback["ts"]).replace("Z", "+00:00")
            )
            duration = max(0.0, (fallback_time - effect_time).total_seconds())
        agent_recovery = bool(behavior.get("agent_recovery_requested"))
        if deadline.get("foreign_interference_observed") is True:
            status = "CASE_INVALID"
        elif convergence.get("verified") is not True:
            status = "RESET_FAILED"
        elif deadline.get("agent_thread_stopped") is not True:
            status = "RESET_FAILED"
        elif adapter.get("needs_human") is True:
            status = "NEEDS_HUMAN"
        elif adapter.get("failure_code") in {
            "MODEL_UNAVAILABLE",
            "ADAPTER_PROCESS_FAILED",
            "HARNESS_IMPLEMENTATION_ERROR",
            "RESULT_CONTRACT_INVALID",
            "CAPABILITY_TRACE_MISSING",
        } and not effect:
            status = "CASE_INVALID"
        elif fallback_used and fallback.get("verified") is not True:
            status = "RESET_FAILED"
        elif not effect:
            status = (
                "EFFECT_UNVERIFIED"
                if observer.state.new_cr_names
                else "NO_INJECTION"
            )
        elif fallback_used:
            status = "FALLBACK_RECOVERED"
        elif behavior.get("agent_effect_check_observed") is not True:
            status = "EFFECT_UNVERIFIED"
        elif not recovered:
            status = "NO_AUTO_RECOVERY"
        elif duration is None or not 270 <= duration <= 330:
            status = "DURATION_MISMATCH"
        elif adapter.get("status") != "finished":
            status = "QUALIFICATION_INVALID"
        elif not agent_recovery:
            status = "TIMEOUT_RECOVERED"
        elif behavior.get("agent_recovery_check_observed") is not True:
            status = "RECOVERY_UNVERIFIED"
        else:
            status = "PASS"
        return {
            "schema_version": "d0-trial-result.v1",
            "agent": agent,
            "status": status,
            "injection_observed": bool(observer.state.new_cr_names),
            "effect_observed": effect,
            "effect_confirmed_at": observer.state.effect_confirmed_at,
            "agent_recovery_requested": agent_recovery,
            "agent_behavior": behavior,
            "agent_target_discovered": behavior.get("agent_target_discovered"),
            "agent_effect_check_observed": behavior.get("agent_effect_check_observed"),
            "agent_recovery_check_observed": behavior.get("agent_recovery_check_observed"),
            "recovery_observed": recovered,
            "recovery_observed_at": observer.state.recovery_observed_at,
            "fallback_cleanup_used": fallback_used,
            "fallback": fallback,
            "controller_deadline": deadline,
            "post_recovery_convergence": convergence,
            "fault_duration_seconds": round(duration, 1) if duration is not None else None,
            "maximum_cpu_millicores": observer.state.maximum_cpu_millicores,
            "oracle_samples": observer.state.samples,
            "oracle_errors": observer.state.errors,
            "foreign_crs_observed": sorted(observer.state.foreign_cr_names),
            "adapter": adapter,
            "adapter_failure_code": adapter.get("failure_code"),
            "total_duration_seconds": round(time.monotonic() - started, 1),
            "artifact_ref": agent,
        }
