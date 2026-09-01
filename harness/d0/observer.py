"""Remote kubectl side observer and bounded D0 fallback cleanup."""

from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .common import append_jsonl, redact_sensitive_text, utc_now


CommandRunner = Callable[[list[str], int], subprocess.CompletedProcess[str]]


def _default_runner(argv: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _cpu_millicores(value: str) -> int:
    text = value.strip()
    if text.endswith("m"):
        return int(text[:-1])
    if text.endswith("n"):
        return int(int(text[:-1]) / 1_000_000)
    return int(float(text) * 1000)


@dataclass
class ObserverState:
    baseline_cpu: dict[str, int] = field(default_factory=dict)
    initial_cr_names: set[str] = field(default_factory=set)
    new_cr_names: set[str] = field(default_factory=set)
    effect_confirmed_at: str | None = None
    effect_monotonic: float | None = None
    recovery_observed_at: str | None = None
    maximum_cpu_millicores: int = 0
    samples: int = 0
    errors: list[str] = field(default_factory=list)
    foreign_cr_names: set[str] = field(default_factory=set)


class KubectlD0Observer:
    def __init__(
        self,
        *,
        kubeconfig: Path,
        artifact_dir: Path,
        trial_id: str,
        sample_seconds: int = 10,
        ownership_mode: str = "strict-run-id",
        runner: CommandRunner = _default_runner,
    ):
        self.kubeconfig = kubeconfig.expanduser().resolve()
        self.artifact_dir = artifact_dir
        self.trial_id = trial_id
        self.sample_seconds = max(1, int(sample_seconds))
        self.runner = runner
        self.ownership_mode = ownership_mode
        self.state = ObserverState()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.commands_path = artifact_dir / "controller-commands.jsonl"
        self.samples_path = artifact_dir / "oracle-samples.jsonl"
        self.command_sequence = 0
        self.execution_identity = {
            "execution_host_id": os.environ.get("RESBENCH_D0_EXECUTION_HOST_ID", ""),
            "hostname": socket.gethostname(),
            "platform": os.uname().sysname,
            "pid": os.getpid(),
            "working_directory": str(Path.cwd()),
        }

    def _run(self, args: list[str], *, timeout: int = 60) -> subprocess.CompletedProcess[str]:
        argv = ["kubectl", "--kubeconfig", str(self.kubeconfig), *args]
        self.command_sequence += 1
        command_id = f"{self.trial_id}-controller-{self.command_sequence:05d}"
        started_at = utc_now()
        started = time.monotonic()
        result = self.runner(argv, timeout)
        finished_at = utc_now()
        stdout = redact_sensitive_text(result.stdout[:4000])
        if "-o" in args and "json" in args:
            stdout = (
                "<kubectl JSON omitted; parsed facts retained in oracle-samples.jsonl; "
                f"sha256={hashlib.sha256(result.stdout.encode()).hexdigest()}; "
                f"bytes={len(result.stdout.encode())}>"
            )
        append_jsonl(
            self.commands_path,
            {
                "ts": finished_at,
                "actor": "controller",
                "command_id": command_id,
                "started_at": started_at,
                "finished_at": finished_at,
                **self.execution_identity,
                "argv": ["kubectl", "--kubeconfig", "<kubeconfig>", *args],
                "duration_ms": round((time.monotonic() - started) * 1000, 1),
                "returncode": result.returncode,
                "stdout": stdout,
                "stderr": redact_sensitive_text(result.stderr[:2000]),
            },
        )
        return result

    def _json(self, args: list[str]) -> dict[str, Any]:
        result = self._run([*args, "-o", "json"])
        if result.returncode:
            raise RuntimeError("kubectl observation failed")
        value = json.loads(result.stdout)
        if not isinstance(value, dict):
            raise TypeError("kubectl returned non-object JSON")
        return value

    @staticmethod
    def _is_accounting(item: dict[str, Any]) -> bool:
        labels = item.get("metadata", {}).get("labels", {})
        candidates = {
            labels.get("opentelemetry.io/name"),
            labels.get("app.kubernetes.io/component"),
            labels.get("app.kubernetes.io/name"),
            labels.get("app"),
        }
        return "accounting" in candidates

    def snapshot(self) -> dict[str, Any]:
        pods = self._json(["-n", "otel-demo", "get", "pods"])
        accounting = [item for item in pods.get("items", []) if self._is_accounting(item)]
        top = self._run(["top", "pod", "-n", "otel-demo", "--containers", "--no-headers"])
        metrics_observed_at = utc_now() if top.returncode == 0 else None
        cpu_by_pod: dict[str, int] = {}
        if top.returncode == 0:
            for line in top.stdout.splitlines():
                fields = line.split()
                if len(fields) >= 3:
                    try:
                        cpu_by_pod[fields[0]] = max(
                            cpu_by_pod.get(fields[0], 0), _cpu_millicores(fields[-2])
                        )
                    except (ValueError, IndexError):
                        continue
        chaos = self._json(["get", "chaosblades.chaosblade.io"])
        crs = []
        for item in chaos.get("items", []):
            metadata = item.get("metadata", {})
            labels = metadata.get("labels", {})
            experiments = item.get("spec", {}).get("experiments", [])
            target_names: list[str] = []
            targets: list[str] = []
            actions: list[str] = []
            for experiment in experiments:
                targets.append(str(experiment.get("target") or ""))
                actions.append(str(experiment.get("action") or ""))
                for matcher in experiment.get("matchers", []):
                    if matcher.get("name") == "names":
                        target_names.extend(
                            str(value) for value in matcher.get("value", [])
                        )
            crs.append(
                {
                    "name": metadata.get("name"),
                    "uid": metadata.get("uid"),
                    "created_at": metadata.get("creationTimestamp"),
                    "deletion_started_at": metadata.get("deletionTimestamp"),
                    "finalizers": list(metadata.get("finalizers") or []),
                    "phase": item.get("status", {}).get("phase"),
                    "owner": labels.get("benchmark.owner"),
                    "logical_namespace": labels.get("benchmark.namespace"),
                    "run_id": labels.get("benchmark.run_id"),
                    "target_uid": labels.get("benchmark.target_uid"),
                    "targets": sorted(set(targets)),
                    "actions": sorted(set(actions)),
                    "target_names": sorted(set(target_names)),
                }
            )
        pod_rows = []
        for item in accounting:
            metadata = item.get("metadata", {})
            status = item.get("status", {})
            ready = any(
                condition.get("type") == "Ready" and condition.get("status") == "True"
                for condition in status.get("conditions", [])
            )
            restarts = sum(
                int(value.get("restartCount", 0))
                for value in status.get("containerStatuses", [])
            )
            name = str(metadata.get("name") or "")
            pod_rows.append(
                {
                    "name": name,
                    "uid": metadata.get("uid"),
                    "ready": ready,
                    "restart_count": restarts,
                    "cpu_millicores": cpu_by_pod.get(name),
                }
            )
        return {
            "ts": utc_now(),
            "metrics_observed_at": metrics_observed_at,
            "metrics_source": "kubernetes-metrics-api-via-kubectl-top",
            "pods": pod_rows,
            "chaosblades": crs,
        }

    def prepare(self, *, convergence_timeout_seconds: int = 90) -> dict[str, Any]:
        deadline = time.monotonic() + convergence_timeout_seconds
        sample: dict[str, Any] = {}
        while time.monotonic() < deadline:
            sample = self.snapshot()
            pods = sample.get("pods", [])
            converged = (
                len(pods) == 1
                and pods[0].get("ready") is True
                and int(pods[0].get("cpu_millicores") or 0) <= 200
                and not sample.get("chaosblades")
            )
            append_jsonl(
                self.samples_path,
                {**sample, "phase": "before" if converged else "precondition_wait"},
            )
            if converged:
                break
            time.sleep(5)
        else:
            raise RuntimeError(
                "accounting did not converge to one Ready, low-CPU, residue-free Pod"
            )
        self.state.baseline_cpu = {
            str(item["name"]): int(item.get("cpu_millicores") or 0)
            for item in sample["pods"]
        }
        self.state.initial_cr_names = {
            str(item["name"]) for item in sample["chaosblades"] if item.get("name")
        }
        return sample

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("observer already started")
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.sample_seconds + 5)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                sample = self.snapshot()
                self._apply(sample)
                append_jsonl(self.samples_path, {**sample, "phase": "watch"})
            except Exception as exc:  # noqa: BLE001 - retain evidence and continue bounded watch.
                self.state.errors.append(type(exc).__name__)
                append_jsonl(
                    self.samples_path,
                    {"ts": utc_now(), "phase": "watch", "error": type(exc).__name__},
                )
            self._stop.wait(self.sample_seconds)

    def _apply(self, sample: dict[str, Any]) -> None:
        self.state.samples += 1
        baseline_names = set(self.state.baseline_cpu)

        def owned(item: dict[str, Any]) -> bool:
            if item.get("run_id") == self.trial_id:
                return True
            return (
                self.ownership_mode == "native-bladeai"
                and not item.get("run_id")
                and not item.get("owner")
                and bool(baseline_names.intersection(item.get("target_names") or []))
                and "cpu" in (item.get("targets") or [])
            )

        owned_names = {
            str(item["name"])
            for item in sample["chaosblades"]
            if item.get("name") and owned(item)
        }
        all_names = {
            str(item["name"])
            for item in sample["chaosblades"]
            if item.get("name")
        }
        new = owned_names - self.state.initial_cr_names
        foreign = (all_names - self.state.initial_cr_names) - owned_names
        self.state.new_cr_names.update(new)
        self.state.foreign_cr_names.update(foreign)
        maximum = max(
            [int(item.get("cpu_millicores") or 0) for item in sample["pods"]] or [0]
        )
        self.state.maximum_cpu_millicores = max(
            self.state.maximum_cpu_millicores, maximum
        )
        baseline = max(self.state.baseline_cpu.values() or [0])
        active_phase = any(
            item.get("name") in self.state.new_cr_names
            for item in sample["chaosblades"]
        )
        if (
            self.state.effect_monotonic is None
            and self.state.new_cr_names
            and active_phase
            and maximum >= max(300, baseline + 300)
        ):
            self.state.effect_monotonic = time.monotonic()
            self.state.effect_confirmed_at = sample["ts"]
        if self.state.effect_monotonic is not None and not (
            owned_names & self.state.new_cr_names
        ):
            self.state.recovery_observed_at = sample["ts"]

    @staticmethod
    def _container_id_missing(value: dict[str, Any]) -> bool:
        text = json.dumps(value.get("status") or {}, ensure_ascii=False).lower()
        return "container-id" in text and (
            "can not find container" in text or "cannot find container" in text
        )

    def _force_finalize_stuck_owned_cr(self, name: str) -> dict[str, Any]:
        """Remove only a known finalizer from this Trial's inert, stuck CR."""
        try:
            cr = self._json(["get", "chaosblades.chaosblade.io", name])
            metadata = cr.get("metadata", {})
            labels = metadata.get("labels", {})
            finalizers = list(metadata.get("finalizers") or [])
            sample = self.snapshot()
            pods = sample.get("pods", [])
            safe_target = (
                len(pods) == 1
                and pods[0].get("ready") is True
                and int(pods[0].get("cpu_millicores") or 0) <= 200
                and str(pods[0].get("uid") or "")
                == str(labels.get("benchmark.target_uid") or "")
            )
            owned = (
                labels.get("benchmark.owner") == "chaos_control"
                and labels.get("benchmark.run_id") == self.trial_id
                and name in self.state.new_cr_names
            )
            known_finalizer = finalizers == ["finalizer.chaosblade.io"]
            eligible = (
                owned
                and safe_target
                and metadata.get("deletionTimestamp")
                and cr.get("status", {}).get("phase") == "Destroying"
                and known_finalizer
                and self._container_id_missing(cr)
            )
            if not eligible:
                return {
                    "attempted": False,
                    "verified_safe": False,
                    "reason": "stuck CR did not satisfy exact ownership/inertness gates",
                }
            payload = '{"metadata":{"finalizers":null}}'
            result = self._run(
                [
                    "patch",
                    "chaosblades.chaosblade.io",
                    name,
                    "--type=merge",
                    "-p",
                    payload,
                ],
                timeout=30,
            )
            return {
                "attempted": True,
                "verified_safe": True,
                "returncode": result.returncode,
                "removed": result.returncode == 0,
            }
        except Exception as exc:  # noqa: BLE001 - return bounded cleanup evidence.
            return {
                "attempted": False,
                "verified_safe": False,
                "reason": type(exc).__name__,
            }

    def fallback_cleanup(self) -> dict[str, Any]:
        current = self.snapshot()
        current_names = {
            str(item["name"]) for item in current["chaosblades"] if item.get("name")
        }
        targets = sorted(current_names & self.state.new_cr_names)
        deleted = []
        errors = []
        warnings = []
        force_finalizer = {}
        for name in targets:
            try:
                result = self._run(
                    [
                        "delete",
                        "chaosblades.chaosblade.io",
                        name,
                        "--ignore-not-found=true",
                        "--wait=false",
                    ]
                )
                if result.returncode == 0:
                    deleted.append(name)
                else:
                    errors.append(f"{name}:returncode-{result.returncode}")
            except subprocess.TimeoutExpired:
                errors.append(f"{name}:delete-timeout")
        remaining = list(targets)
        deadline = time.monotonic() + 30
        while remaining and time.monotonic() < deadline:
            time.sleep(1)
            after = self.snapshot()
            names = {
                str(item["name"])
                for item in after["chaosblades"]
                if item.get("name")
            }
            remaining = sorted(names.intersection(targets))
        if remaining:
            for name in list(remaining):
                outcome = self._force_finalize_stuck_owned_cr(name)
                force_finalizer[name] = outcome
                if outcome.get("removed") is True:
                    warnings.append(f"{name}:known-stuck-finalizer-removed")
                elif outcome.get("attempted"):
                    errors.append(f"{name}:finalizer-patch-failed")
            deadline = time.monotonic() + 15
            while remaining and time.monotonic() < deadline:
                time.sleep(1)
                after = self.snapshot()
                names = {
                    str(item["name"])
                    for item in after["chaosblades"]
                    if item.get("name")
                }
                remaining = sorted(names.intersection(targets))
        return {
            "ts": utc_now(),
            "requested": bool(targets),
            "targets": targets,
            "deleted": deleted,
            "errors": errors,
            "warnings": warnings,
            "force_finalizer": force_finalizer,
            "remaining": remaining,
            "verified": not errors and not remaining,
        }

    def wait_recovery_convergence(self, *, timeout_seconds: int = 60) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_seconds
        last: dict[str, Any] = {}
        while time.monotonic() < deadline:
            last = self.snapshot()
            pods = last.get("pods", [])
            candidate = (
                len(pods) == 1
                and pods[0].get("ready") is True
                and int(pods[0].get("cpu_millicores") or 0) <= 200
                and not last.get("chaosblades")
            )
            pressure = (
                self.check_pressure_process(str(pods[0]["name"]))
                if candidate
                else {"verified": False, "residue": None, "reason": "state-not-converged"}
            )
            enriched = {
                **last,
                "phase": "post_recovery",
                "pressure_process_check": pressure,
            }
            append_jsonl(self.samples_path, enriched)
            if candidate and pressure.get("verified") is True and pressure.get("residue") is False:
                return {"verified": True, "sample": enriched, "pressure_process_check": pressure}
            time.sleep(5)
        return {"verified": False, "sample": last}

    def check_pressure_process(self, pod_name: str) -> dict[str, Any]:
        command = (
            "for f in /proc/[0-9]*/comm; do cat \"$f\" 2>/dev/null; done"
        )
        result = self._run(
            [
                "-n",
                "otel-demo",
                "exec",
                pod_name,
                "-c",
                "accounting",
                "--",
                "sh",
                "-c",
                command,
            ],
            timeout=30,
        )
        if result.returncode:
            return {
                "verified": False,
                "residue": None,
                "reason": "process-list-command-failed",
                "returncode": result.returncode,
            }
        markers = ("chaos", "blade", "stress", "burncpu", "fullload")
        matches = sorted(
            {
                line.strip()
                for line in result.stdout.splitlines()
                if line.strip()
                and any(marker in line.strip().lower() for marker in markers)
            }
        )
        return {
            "verified": True,
            "residue": bool(matches),
            "matched_process_names": matches,
            "returncode": 0,
        }

    def fault_duration_seconds(self) -> float | None:
        if not self.state.effect_confirmed_at or not self.state.recovery_observed_at:
            return None
        start = datetime.fromisoformat(self.state.effect_confirmed_at.replace("Z", "+00:00"))
        end = datetime.fromisoformat(self.state.recovery_observed_at.replace("Z", "+00:00"))
        return max(0.0, (end - start).total_seconds())
