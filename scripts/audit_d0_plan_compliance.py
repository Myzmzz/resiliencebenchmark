#!/usr/bin/env python3
"""Audit a D0 Campaign against the 12 acceptance requirements in its plan."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any


EXPECTED_AGENTS = ("bladeai", "codex", "claude-code", "deepseek-harness")
EXPECTED_PROMPT = "请针对otel-demo下的accounting服务的一个 pod 注入高 cpu 故障，持续 5 分钟，5 分钟后需要自动恢复"


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    if not path.is_file():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def check(identifier: str, requirement: str, passed: bool, evidence: str) -> dict:
    return {
        "id": identifier,
        "requirement": requirement,
        "status": "PASS" if passed else "FAIL",
        "evidence": evidence,
    }


def audit(root: Path, repo_root: Path, remote_manifest_sha256: str | None) -> dict:
    campaign = json.loads((root / "campaign.json").read_text(encoding="utf-8"))
    results = {value.get("agent"): value for value in campaign.get("results", [])}
    agents = tuple(campaign.get("agents", []))
    checks = []

    entrypoint = repo_root / "scripts/run_otel_accounting_cpu_matrix.py"
    entrypoint_text = entrypoint.read_text(encoding="utf-8") if entrypoint.is_file() else ""
    checks.append(
        check(
            "D0-AC-01",
            "one dedicated command serially starts the four-Agent matrix",
            entrypoint.is_file()
            and "--execute" in entrypoint_text
            and agents == EXPECTED_AGENTS,
            "scripts/run_otel_accounting_cpu_matrix.py + campaign.agents",
        )
    )
    prompt = (root / "prompt.txt").read_text(encoding="utf-8").rstrip("\n")
    checks.append(
        check(
            "D0-AC-02",
            "all Agents receive the exact same versioned prompt",
            prompt == EXPECTED_PROMPT
            and (root / "prompt.sha256").read_text().strip()
            == hashlib.sha256(EXPECTED_PROMPT.encode()).hexdigest(),
            "prompt.txt + prompt.sha256",
        )
    )
    execution_mode = campaign.get("execution_mode") or {}
    needs_human = []
    for agent in agents:
        needs_human.extend(
            value
            for value in read_jsonl(root / agent / "all-events.jsonl")
            if str(value.get("kind") or "").lower()
            in {"needs_human", "unhandled", "operator_input"}
        )
    checks.append(
        check(
            "D0-AC-03",
            "the matrix is unattended and no native interaction needs a human",
            execution_mode.get("unattended") is True
            and execution_mode.get("operator_input_allowed") is False
            and not needs_human,
            "campaign.execution_mode + all-events.jsonl",
        )
    )
    inventory = campaign.get("execution_inventory") or {}
    runtime_agents = inventory.get("agents") or {}
    checks.append(
        check(
            "D0-AC-04",
            "each Agent retains a recorded native runtime and protocol",
            all(
                runtime_agents.get(agent, {}).get("available") is True
                and runtime_agents.get(agent, {}).get("agent_transport")
                for agent in EXPECTED_AGENTS
            ),
            "campaign.execution_inventory.agents",
        )
    )
    controller_injection = []
    for agent in agents:
        for value in read_jsonl(root / agent / "controller-commands.jsonl"):
            argv = " ".join(str(item) for item in value.get("argv", []))
            if " create " in f" {argv} " and "chaosblade" in argv.lower():
                controller_injection.append((agent, argv))
    agent_injection_evidence = all(
        results.get(agent, {}).get("agent_behavior", {}).get(
            "agent_injection_requested"
        )
        is True
        for agent in agents
        if results.get(agent, {}).get("effect_observed") is True
    )
    checks.append(
        check(
            "D0-AC-05",
            "normal injection and verification are Agent actions, not Controller substitutes",
            not controller_injection and agent_injection_evidence,
            "result.agent_behavior + controller-commands.jsonl",
        )
    )
    command_fields_ok = True
    command_count = 0
    for agent in agents:
        for value in read_jsonl(root / agent / "controller-commands.jsonl"):
            command_count += 1
            command_fields_ok &= all(
                value.get(key) not in (None, "")
                for key in (
                    "started_at",
                    "finished_at",
                    "execution_host_id",
                    "hostname",
                    "working_directory",
                )
            ) and "returncode" in value
    checks.append(
        check(
            "D0-AC-06",
            "every Controller command has host, start/end time and result",
            command_count > 0 and command_fields_ok,
            f"{command_count} rows in controller-commands.jsonl",
        )
    )
    separated = all(
        all((root / agent / name).is_file() for name in (
            "all-events.jsonl",
            "controller-events.jsonl",
            "controller-commands.jsonl",
            "oracle-samples.jsonl",
            "result.json",
        ))
        for agent in agents
    )
    checks.append(
        check(
            "D0-AC-07",
            "Agent events, Controller actions, Oracle facts and fallback are separated",
            separated,
            "per-Agent JSONL/result files",
        )
    )
    residue_ok = True
    cr_metadata_ok = True
    legacy_manual_cleanup = False
    for agent in agents:
        samples = read_jsonl(root / agent / "oracle-samples.jsonl")
        for sample in samples:
            for cr in sample.get("chaosblades", []):
                cr_metadata_ok &= all(
                    key in cr
                    for key in (
                        "name",
                        "uid",
                        "created_at",
                        "phase",
                        "run_id",
                        "target_uid",
                    )
                )
        recovery_samples = [
            value for value in samples if value.get("phase") == "post_recovery"
        ]
        residue_ok &= bool(recovery_samples) and any(
            value.get("pressure_process_check", {}).get("verified") is True
            and value.get("pressure_process_check", {}).get("residue") is False
            and not value.get("chaosblades")
            for value in recovery_samples
        )
        fallback = results.get(agent, {}).get("fallback") or {}
        legacy_manual_cleanup |= fallback.get("finalizer_removed") is True
        residue_ok &= not bool(results.get(agent, {}).get("foreign_crs_observed"))
    checks.append(
        check(
            "D0-AC-08",
            "each round proves no CR or pressure-process residue; cleanup is unattended",
            residue_ok and cr_metadata_ok and not legacy_manual_cleanup,
            "oracle-samples.jsonl + result.fallback",
        )
    )
    visual = root / "visualization"
    required_visual = {
        "index.html",
        "comparison.svg",
        "summary.csv",
        "summary.json",
        "command-tool-audit.csv",
        "command-tool-audit.json",
        "audit-report.md",
        *{f"{agent}-timeline.svg" for agent in agents},
        *{f"{agent}-cpu.svg" for agent in agents},
        *{f"{agent}-command-tool-audit.html" for agent in agents},
    }
    checks.append(
        check(
            "D0-AC-09",
            "the four rounds automatically produce complete traceable visualization",
            all((visual / name).is_file() for name in required_visual),
            "visualization/",
        )
    )
    truthful = campaign.get("status") != "QUALIFIED" and all(
        results.get(agent, {}).get("status") != "PASS"
        for agent in agents
        if results.get(agent, {}).get("fallback_cleanup_used") is True
    )
    checks.append(
        check(
            "D0-AC-10",
            "failures, missing evidence and fallback are not converted to PASS",
            truthful,
            "campaign.status + results[].status/fallback_cleanup_used",
        )
    )
    host = campaign.get("host") or {}
    kube = inventory.get("kubernetes") or {}
    checks.append(
        check(
            "D0-AC-11",
            "Agents, Controller and Kubernetes operations are tied to 1.94.151.57",
            host.get("verified") is True
            and host.get("declared_host_id") == "1.94.151.57"
            and bool(kube.get("context"))
            and bool(kube.get("api_server_sha256")),
            "campaign.host + execution_inventory.kubernetes",
        )
    )
    local_manifest = hashlib.sha256((root / "manifest.sha256").read_bytes()).hexdigest()
    checks.append(
        check(
            "D0-AC-12",
            "remote sealed artifact and local inspection copy have the same Manifest",
            bool(remote_manifest_sha256)
            and local_manifest == remote_manifest_sha256,
            f"local={local_manifest}; remote={remote_manifest_sha256 or 'not-supplied'}",
        )
    )
    passed = all(value["status"] == "PASS" for value in checks)
    return {
        "schema_version": "d0-plan-compliance-audit.v1",
        "campaign_id": campaign.get("campaign_id"),
        "status": "COMPLIANT" if passed else "NON_COMPLIANT",
        "passed": sum(value["status"] == "PASS" for value in checks),
        "failed": sum(value["status"] == "FAIL" for value in checks),
        "checks": checks,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("campaign_dir", type=Path)
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--remote-manifest-sha256")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = audit(
        args.campaign_dir.expanduser().resolve(),
        args.repo_root.expanduser().resolve(),
        args.remote_manifest_sha256,
    )
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        output = args.output.expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if report["status"] == "COMPLIANT" else 2


if __name__ == "__main__":
    sys.exit(main())
