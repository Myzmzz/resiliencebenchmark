from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

from resilience_agent.common import load_document, validate_document
from resilience_agent.pipeline import (
    CANDIDATE_SCHEMA,
    EPISODE_SCHEMA,
    PACKAGE_ROOT,
    REPO_ROOT,
    TEMPLATE_ROOT,
    run_pipeline,
)


EXAMPLE = PACKAGE_ROOT / "examples" / "minimal"
CATALOG = REPO_ROOT / "tasks" / "catalog" / "resilience-defect-classes.v0.1.yaml"
RULES = TEMPLATE_ROOT / "defect-matchers.v0.1.yaml"


def test_minimal_pipeline_identifies_candidate_and_designs_complete_episode() -> None:
    context = load_document(EXAMPLE / "system-context.yaml")
    candidates, designs = run_pipeline(EXAMPLE, system_context=context)

    validate_document(candidates, CANDIDATE_SCHEMA)
    validate_document(designs, EPISODE_SCHEMA)
    assert candidates["scan_summary"]["templates_total"] == 30
    assert [item["defect_ref"] for item in candidates["candidates"]] == ["RBD-001"]
    candidate = candidates["candidates"][0]
    assert candidate["status"] == "candidate_unverified"
    assert candidate["target"]["component"] == "app"
    assert candidate["evidence"][0]["path"] == "app/client.py"

    assert len(designs["episodes"]) == 1
    episode = designs["episodes"][0]
    assert episode["candidate_id"] == candidate["candidate_id"]
    assert episode["status"] == "draft_unqualified"
    assert episode["design_basis"]["truth_status"] == "hypothesis_not_independently_confirmed"
    assert episode["safety"]["mode"] == "design_only_not_executed"
    assert [item["role"] for item in episode["experiment_sequence"]] == [
        "baseline_control",
        "hypothesis_validation",
    ]
    assert episode["recovery"]["cleanup_actions"]
    assert episode["readiness"]["ready_for_execution"] is False
    assert episode["readiness"]["ready_for_lock"] is False
    assert {gate["gate_id"] for gate in episode["oracle"]["gates"]} == {
        "episode_validity",
        "safety",
        "fault_effect",
        "slo_violation",
        "causal_mechanism",
        "diagnosis",
        "recovery",
    }


def test_fully_bound_context_reaches_controller_review_not_episode_lock() -> None:
    context = copy.deepcopy(load_document(EXAMPLE / "system-context.yaml"))
    context.update(
        {
            "snapshot_id": "minimal-snapshot-v1",
            "release_ref": "demo-release-v1",
            "runtime_target": {
                "kind": "Pod",
                "name": "checkout-abc123",
                "uid": "uid-example-123",
            },
            "selected_actuator": "bounded dependency delay",
            "independent_observers_qualified": True,
            "cleanup_handle": "cleanup-example-001",
            "budget": {"max_experiments": 2, "max_duration_minutes": 20},
        }
    )
    context["workload"]["fixture_ref"] = "synthetic-checkout-v1"
    context["experiment_parameters"].update(
        {"delay duration": "200 ms", "traffic profile": "steady synthetic checkout"}
    )

    _, designs = run_pipeline(EXAMPLE, system_context=context)
    episode = designs["episodes"][0]
    assert episode["status"] == "draft_ready_for_controller_review"
    assert episode["readiness"]["ready_for_execution"] is True
    assert episode["readiness"]["execution_blockers"] == []
    assert episode["readiness"]["ready_for_lock"] is False


def test_explicit_timeout_suppresses_missing_timeout_candidate(tmp_path: Path) -> None:
    (tmp_path / "client.py").write_text(
        "import requests\nrequests.get('https://service.invalid', timeout=2)\n",
        encoding="utf-8",
    )
    candidates, designs = run_pipeline(tmp_path)
    assert candidates["candidates"] == []
    assert designs["episodes"] == []


def test_typescript_fetch_without_abort_signal_is_only_an_unverified_candidate(
    tmp_path: Path,
) -> None:
    (tmp_path / "Shipping.gateway.ts").write_text(
        "export async function quote(url: string) { return fetch(url, {method: 'POST'}); }\n",
        encoding="utf-8",
    )

    candidates, designs = run_pipeline(tmp_path)

    assert len(candidates["candidates"]) == 1
    candidate = candidates["candidates"][0]
    assert candidate["defect_ref"] == "RBD-001"
    assert candidate["status"] == "candidate_unverified"
    assert candidate["target"]["component"] == "frontend"
    assert designs["episodes"][0]["design_basis"]["truth_status"] == (
        "hypothesis_not_independently_confirmed"
    )


def test_typescript_fetch_with_abort_signal_does_not_match(tmp_path: Path) -> None:
    (tmp_path / "client.ts").write_text(
        "fetch(url, {signal: AbortSignal.timeout(1000)});\n",
        encoding="utf-8",
    )

    candidates, _ = run_pipeline(tmp_path)

    assert all(item["defect_ref"] != "RBD-001" for item in candidates["candidates"])


def test_system_context_can_supply_match_evidence(tmp_path: Path) -> None:
    context = {"application": "demo", "available_replicas": 1}
    candidates, _ = run_pipeline(tmp_path, system_context=context)
    item = next(
        candidate for candidate in candidates["candidates"] if candidate["defect_ref"] == "RBD-021"
    )
    assert item["evidence"][0]["kind"] == "system"
    assert item["evidence"][0]["path"] == "$system-context"


def test_compose_replicas_do_not_match_kubernetes_availability_rule(tmp_path: Path) -> None:
    (tmp_path / "docker-compose.yaml").write_text(
        "services:\n  checkout:\n    deploy:\n      replicas: 1\n",
        encoding="utf-8",
    )
    candidates, _ = run_pipeline(tmp_path)
    assert all(item["defect_ref"] != "RBD-021" for item in candidates["candidates"])


def test_internal_matcher_registry_references_existing_catalog_items() -> None:
    catalog_ids = {item["defect_id"] for item in load_document(CATALOG)["items"]}
    rules = load_document(RULES)["rules"]
    assert rules
    assert {rule["defect_id"] for rule in rules} <= catalog_ids


def test_cli_writes_candidate_and_episode_design_artifacts(tmp_path: Path) -> None:
    output = tmp_path / "out"
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "resilience_agent",
            "run",
            "--project",
            str(EXAMPLE),
            "--context",
            str(EXAMPLE / "system-context.yaml"),
            "--output-dir",
            str(output),
            "--reasoning-mode",
            "deterministic",
        ],
        cwd=REPO_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    summary = json.loads(proc.stdout)
    assert summary["candidate_count"] == 1
    assert summary["episode_count"] == 1
    validate_document(load_document(output / "candidate-defects.json"), CANDIDATE_SCHEMA)
    validate_document(load_document(output / "episode-designs.json"), EPISODE_SCHEMA)
    manifest = load_document(output / "agent-run.json")
    assert manifest["reasoning_mode"] == "deterministic"
    assert manifest["model"] is None
    assert not (output / "experiment-plans.json").exists()


def test_agent_has_no_external_host_skill_packages() -> None:
    assert not list(PACKAGE_ROOT.rglob("SKILL.md"))
    assert not (PACKAGE_ROOT / "skills").exists() or not any((PACKAGE_ROOT / "skills").rglob("*"))
