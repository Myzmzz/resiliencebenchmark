"""Recompute D0 verdicts only from sealed raw events and observer samples."""

from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from .common import (
    EVALUATION_READY_STATUSES,
    read_jsonl,
    write_json,
    write_manifest,
)
from .visualization import generate_visualizations
from .common import AGENTS
from .behavior import derive_agent_behavior


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _actual_recovery_request(events: list[dict[str, Any]]) -> bool:
    for event in events:
        payload = event.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        nested = payload.get("item")
        nested = nested if isinstance(nested, dict) else {}
        tool = str(
            event.get("tool")
            or payload.get("tool")
            or payload.get("tool_name")
            or nested.get("tool")
            or ""
        ).lower()
        marker = str(
            payload.get("type") or nested.get("type") or event.get("kind") or ""
        ).lower()
        if tool in {"chaos_destroy_experiment", "blade_destroy"} and marker in {
            "mcp_tool_call",
            "tool_call",
            "tool_start",
            "item.started",
            "item.completed",
        }:
            return True
    return False


def _adapter_failure_code(trial_dir: Path, existing: dict[str, Any]) -> str:
    adapter = existing.get("adapter") or {}
    if adapter.get("failure_code"):
        return str(adapter["failure_code"])
    diagnostic = "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in (trial_dir / "stdout.txt", trial_dir / "stderr.txt")
        if path.is_file()
    ).lower()
    if "model_not_found" in diagnostic or "no available channel for model" in diagnostic:
        return "MODEL_UNAVAILABLE"
    if "nameerror" in diagnostic:
        return "HARNESS_IMPLEMENTATION_ERROR"
    if adapter.get("status") == "failed" and adapter.get("process_status") not in {
        "cancelled_by_controller",
        "cancelled",
    }:
        return "ADAPTER_PROCESS_FAILED"
    return ""


def recompute_trial(trial_dir: Path, agent: str) -> dict[str, Any]:
    samples = read_jsonl(trial_dir / "oracle-samples.jsonl")
    events = read_jsonl(trial_dir / "all-events.jsonl")
    existing = json.loads((trial_dir / "result.json").read_text(encoding="utf-8"))
    before = next((row for row in samples if row.get("phase") == "before"), samples[0] if samples else {})
    baseline_cpu = max(
        [int(pod.get("cpu_millicores") or 0) for pod in before.get("pods", [])]
        or [0]
    )
    baseline_restarts = sum(
        int(pod.get("restart_count") or 0) for pod in before.get("pods", [])
    )
    precondition_ok = (
        baseline_cpu <= 200
        and len(before.get("pods", [])) == 1
        and before.get("pods", [{}])[0].get("ready") is True
        and not before.get("chaosblades")
    )
    initial = {
        str(value.get("name"))
        for value in before.get("chaosblades", [])
        if value.get("name")
    }
    trial_metadata = {}
    if (trial_dir / "trial.json").is_file():
        trial_metadata = json.loads(
            (trial_dir / "trial.json").read_text(encoding="utf-8")
        )
    trial_id = str(
        trial_metadata.get("trial_id")
        or f"{trial_dir.parent.name}-{agent}"
    )
    baseline_names = {
        str(value.get("name")) for value in before.get("pods", []) if value.get("name")
    }

    def owned_cr(value: dict[str, Any]) -> bool:
        if value.get("run_id") == trial_id:
            return True
        return (
            agent == "bladeai"
            and not value.get("run_id")
            and not value.get("owner")
            and (
                bool(baseline_names.intersection(value.get("target_names") or []))
                or bool(__import__("re").fullmatch(r"[0-9a-f]{16}", str(value.get("name") or "")))
            )
        )

    seen: set[str] = set()
    foreign_seen: set[str] = set()
    effect_at = None
    effect_ended_at = None
    recovery_at = None
    maximum_cpu = baseline_cpu
    maximum_restarts = baseline_restarts
    for row in samples:
        names = {
            str(value.get("name"))
            for value in row.get("chaosblades", [])
            if value.get("name") and owned_cr(value)
        }
        all_names = {
            str(value.get("name"))
            for value in row.get("chaosblades", [])
            if value.get("name")
        }
        new = names - initial
        seen.update(new)
        foreign_seen.update((all_names - initial) - names)
        maximum = max(
            [int(pod.get("cpu_millicores") or 0) for pod in row.get("pods", [])]
            or [0]
        )
        maximum_cpu = max(maximum_cpu, maximum)
        maximum_restarts = max(
            maximum_restarts,
            sum(int(pod.get("restart_count") or 0) for pod in row.get("pods", [])),
        )
        if effect_at is None and new and maximum >= max(300, baseline_cpu + 300):
            effect_at = str(row.get("ts"))
        elif (
            effect_at is not None
            and effect_ended_at is None
            and maximum <= max(200, baseline_cpu + 100)
        ):
            effect_ended_at = str(row.get("ts"))
        if (
            recovery_at is None
            and effect_at is not None
            and seen
            and not names.intersection(seen)
        ):
            recovery_at = str(row.get("ts"))
    fallback_event = next(
        (event for event in events if event.get("kind") == "fallback_cleanup_finished"),
        None,
    )
    fallback = (
        dict(fallback_event.get("payload") or {})
        if isinstance(fallback_event, dict)
        else dict(existing.get("fallback") or {})
    )
    fallback_used = bool(fallback.get("requested"))
    behavior = derive_agent_behavior(trial_dir)
    agent_recovery = bool(behavior["agent_recovery_requested"])
    normalized_trace = {}
    trace_path = trial_dir / "run-trace.json"
    if trace_path.is_file():
        normalized_trace = json.loads(trace_path.read_text(encoding="utf-8"))
    tool_trace_complete = any(
        str(event.get("kind") or "")
        in {"mcp_tool_call", "tool_call", "tool_start", "tool_end", "item.started", "item.completed"}
        for event in events
    ) or any(
        event.get("kind") in {"tool_call", "tool_result"}
        for event in normalized_trace.get("events", [])
        if isinstance(event, dict)
    )
    native_trace_capture_complete = bool(events) and (
        agent == "bladeai"
        or (
            trace_path.is_file()
            and (trial_dir / "stdout.txt").is_file()
            and (trial_dir / "stderr.txt").is_file()
        )
    )
    if agent == "deepseek-harness":
        native_trace_capture_complete = native_trace_capture_complete and any(
            trial_dir.glob("dsh-session-*.jsonl")
        )
    failure_code = _adapter_failure_code(trial_dir, existing)
    needs_human = bool((existing.get("adapter") or {}).get("needs_human"))
    end_at = recovery_at or (str(fallback.get("ts")) if fallback_used else None)
    duration = (
        max(0.0, (_dt(end_at) - _dt(effect_at)).total_seconds())
        if effect_at and end_at
        else None
    )
    effect_duration = (
        max(0.0, (_dt(effect_ended_at) - _dt(effect_at)).total_seconds())
        if effect_at and effect_ended_at
        else duration
    )
    if not precondition_ok:
        status = "QUALIFICATION_INVALID"
    elif fallback_used and fallback.get("verified") is not True:
        status = "RESET_FAILED"
    elif foreign_seen:
        status = "CASE_INVALID"
    elif needs_human:
        status = "NEEDS_HUMAN"
    elif failure_code in {
        "MODEL_UNAVAILABLE",
        "ADAPTER_PROCESS_FAILED",
        "HARNESS_IMPLEMENTATION_ERROR",
    } and effect_at is None:
        status = "CASE_INVALID"
    elif effect_at is None:
        status = "EFFECT_UNVERIFIED" if seen else "NO_INJECTION"
    elif fallback_used:
        status = "FALLBACK_RECOVERED"
    elif behavior["agent_effect_check_observed"] is not True:
        status = "EFFECT_UNVERIFIED"
    elif effect_duration is None or not 270 <= effect_duration <= 330:
        status = "DURATION_MISMATCH"
    elif recovery_at is None:
        status = "NO_AUTO_RECOVERY"
    elif duration is None or not 270 <= duration <= 330:
        status = "DURATION_MISMATCH"
    elif not agent_recovery:
        status = "TIMEOUT_RECOVERED"
    elif behavior["agent_recovery_check_observed"] is not True:
        status = "RECOVERY_UNVERIFIED"
    else:
        status = "PASS"
    corrected = {
        **existing,
        "status": status,
        "precondition_converged": precondition_ok,
        "baseline_cpu_millicores": baseline_cpu,
        "injection_observed": bool(seen),
        "foreign_crs_observed": sorted(foreign_seen),
        "effect_observed": effect_at is not None,
        "effect_confirmed_at": effect_at,
        "agent_recovery_requested": agent_recovery,
        "agent_behavior": behavior,
        "agent_target_discovered": behavior["agent_target_discovered"],
        "agent_effect_check_observed": behavior["agent_effect_check_observed"],
        "agent_recovery_check_observed": behavior["agent_recovery_check_observed"],
        "recovery_observed": recovery_at is not None,
        "recovery_observed_at": recovery_at,
        "fallback_cleanup_used": fallback_used,
        "fallback": fallback,
        "fault_duration_seconds": round(duration, 1) if duration is not None else None,
        "effect_duration_seconds": (
            round(effect_duration, 1) if effect_duration is not None else None
        ),
        "effect_ended_at": effect_ended_at,
        "restart_count_delta": maximum_restarts - baseline_restarts,
        "maximum_cpu_millicores": maximum_cpu,
        "verdict_source": "sealed-raw-events-and-oracle-samples",
        "agent_event_count": len(events),
        "controller_command_count": len(
            read_jsonl(trial_dir / "controller-commands.jsonl")
        ),
        "tool_trace_complete": tool_trace_complete,
        "native_trace_capture_complete": native_trace_capture_complete,
        "adapter_failure_code": failure_code,
        "tool_trace_limitation": (
            "native Harness exported final response but no per-tool trace"
            if not tool_trace_complete
            else None
        ),
    }
    original = trial_dir / "result.runtime.json"
    if not original.exists():
        shutil.copyfile(trial_dir / "result.json", original)
    write_json(trial_dir / "result.json", corrected)
    trial_path = trial_dir / "trial.json"
    if not trial_path.is_file():
        campaign_path = trial_dir.parent / "campaign.json"
        campaign = (
            json.loads(campaign_path.read_text(encoding="utf-8"))
            if campaign_path.is_file()
            else {}
        )
        write_json(
            trial_path,
            {
                "schema_version": "d0-trial.v1",
                "trial_id": f"{campaign.get('campaign_id')}-{agent}",
                "campaign_id": campaign.get("campaign_id"),
                "agent": agent,
                "model": (campaign.get("models") or {}).get(agent),
                "prompt_sha256": campaign.get("prompt_sha256"),
                "execution_host": campaign.get("host"),
                "oracle_target": (before.get("pods") or [{}])[0],
                "oracle_target_not_exposed_in_prompt": True,
                "reconstructed_from_sealed_evidence": True,
                "runtime_identity_not_captured_at_trial_start": True,
            },
        )
    write_json(
        trial_dir / "recovery.json",
        {
            "schema_version": "d0-recovery.v1",
            "agent": agent,
            "agent_recovery_requested": corrected.get("agent_recovery_requested"),
            "agent_recovery_check_observed": corrected.get(
                "agent_recovery_check_observed"
            ),
            "recovery_observed": corrected.get("recovery_observed"),
            "recovery_observed_at": corrected.get("recovery_observed_at"),
            "fallback_cleanup_used": corrected.get("fallback_cleanup_used"),
            "fallback": corrected.get("fallback"),
            "status": corrected.get("status"),
            "verdict_source": corrected.get("verdict_source"),
        },
    )
    write_manifest(trial_dir)
    return corrected


def recompute_campaign(campaign_dir: Path) -> dict[str, Any]:
    campaign_path = campaign_dir / "campaign.json"
    campaign = json.loads(campaign_path.read_text(encoding="utf-8"))
    results = []
    for agent in campaign.get("agents", []):
        trial_dir = campaign_dir / agent
        if (trial_dir / "result.json").is_file():
            results.append(recompute_trial(trial_dir, str(agent)))
    campaign["results"] = results
    if len(results) == len(campaign.get("agents", [])):
        statuses = {value.get("status") for value in results}
        if results and statuses == {"PASS"}:
            campaign["status"] = "QUALIFIED"
        elif statuses and statuses <= EVALUATION_READY_STATUSES:
            campaign["status"] = "EVALUATION_READY"
        elif statuses.intersection(
            {"CASE_INVALID", "QUALIFICATION_INVALID", "NEEDS_HUMAN"}
        ):
            campaign["status"] = "QUALIFICATION_INVALID"
        else:
            campaign["status"] = "QUALIFICATION_FAILED"
    campaign["verdict_source"] = "recomputed-from-sealed-raw-evidence"
    campaign["visualization"] = generate_visualizations(campaign_dir, campaign)
    write_json(campaign_path, campaign)
    write_manifest(campaign_dir)
    return campaign


def merge_agent_evidence(
    target_campaign_dir: Path,
    imports: dict[str, Path],
) -> dict[str, Any]:
    """Import sealed Agent directories, retain provenance, then recompute all verdicts."""
    campaign_path = target_campaign_dir / "campaign.json"
    campaign = json.loads(campaign_path.read_text(encoding="utf-8"))
    imported = list(campaign.get("imported_agent_evidence", []))
    for agent, source_campaign in imports.items():
        if agent not in AGENTS:
            raise ValueError(f"unknown D0 agent import: {agent}")
        direct_agent_source = (source_campaign / "result.json").is_file()
        source = source_campaign if direct_agent_source else source_campaign / agent
        metadata_path = (
            source_campaign.parent / "campaign.json"
            if direct_agent_source
            else source_campaign / "campaign.json"
        )
        source_metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if source_metadata.get("prompt_sha256") != campaign.get("prompt_sha256"):
            raise ValueError(f"source Campaign prompt differs for {agent}")
        if (
            source_metadata.get("host", {}).get("declared_host_id")
            != campaign.get("host", {}).get("declared_host_id")
        ):
            raise ValueError(f"source Campaign execution host differs for {agent}")
        if not (source / "result.json").is_file():
            raise FileNotFoundError(f"source Agent evidence is incomplete: {source}")
        destination = target_campaign_dir / agent
        if destination.exists():
            archived = target_campaign_dir / f"{agent}.superseded"
            index = 1
            while archived.exists():
                archived = target_campaign_dir / f"{agent}.superseded-{index:03d}"
                index += 1
            destination.rename(archived)
        shutil.copytree(source, destination)
        trial_metadata = (
            json.loads((source / "trial.json").read_text(encoding="utf-8"))
            if (source / "trial.json").is_file()
            else {}
        )
        source_model = trial_metadata.get("model") or source_metadata.get(
            "models", {}
        ).get(agent)
        campaign.setdefault("models", {})[agent] = source_model
        target_inventory = campaign.setdefault("execution_inventory", {})
        source_inventory = source_metadata.get("execution_inventory", {})
        target_inventory.setdefault("agents", {})[agent] = (
            trial_metadata.get("agent_runtime")
            or source_inventory.get("agents", {}).get(agent, {})
        )
        target_inventory.setdefault("models", {})[agent] = (
            trial_metadata.get("model_identity")
            or source_inventory.get("models", {}).get(agent, {})
        )
        imported.append(
            {
                "agent": agent,
                "source_campaign": source_metadata.get("campaign_id")
                or source_campaign.name,
                "source_agent_directory": source.name,
                "source_manifest_sha256": (
                    __import__("hashlib").sha256(
                        (source_campaign / "manifest.sha256").read_bytes()
                    ).hexdigest()
                    if (source_campaign / "manifest.sha256").is_file()
                    else None
                ),
                "source_campaign_status": source_metadata.get("status"),
                "source_model": source_model,
                "source_agent_manifest_sha256": (
                    __import__("hashlib").sha256(
                        (source / "manifest.sha256").read_bytes()
                    ).hexdigest()
                    if (source / "manifest.sha256").is_file()
                    else None
                ),
            }
        )
    campaign["agents"] = list(AGENTS)
    campaign["imported_agent_evidence"] = imported
    write_json(campaign_path, campaign)
    return recompute_campaign(target_campaign_dir)
