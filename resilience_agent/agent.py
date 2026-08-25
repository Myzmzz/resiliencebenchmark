"""Complete model-driven resilience analysis Agent orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .agent_loop import AgentLoopError
from .analysis_tools import ProjectAnalysisTools
from .common import redact_sensitive_text, stable_id, validate_document, write_json
from .defect_identification import identify_defects
from .episode_design import design_episodes
from .model_client import ReasoningModel
from .model_reasoning import analyze_defects_with_model, review_episode_designs_with_model
from .pipeline import (
    CANDIDATE_SCHEMA,
    EPISODE_SCHEMA,
    REPO_ROOT,
    TEMPLATE_ROOT,
)


PACKAGE_ROOT = Path(__file__).resolve().parent
MODEL_DEFECT_SCHEMA = PACKAGE_ROOT / "schemas" / "model-defect-assessment.schema.json"
MODEL_EPISODE_SCHEMA = PACKAGE_ROOT / "schemas" / "model-episode-review.schema.json"
RUN_SCHEMA = PACKAGE_ROOT / "schemas" / "agent-run.schema.json"


@dataclass
class AgentRunResult:
    candidates: dict[str, Any]
    episode_designs: dict[str, Any]
    run_manifest: dict[str, Any]
    model_defect_assessment: dict[str, Any]
    model_episode_review: dict[str, Any]


class ResilienceAnalysisAgent:
    """Our Agent: deterministic evidence + model reasoning + deterministic gates."""

    def __init__(self, model: ReasoningModel):
        self.model = model

    def run(
        self,
        project_root: Path,
        *,
        system_context: dict[str, Any] | None = None,
        evidence_roots: dict[str, Path] | None = None,
        output_dir: Path | None = None,
        catalog_path: Path | None = None,
        rules_path: Path | None = None,
        episode_templates_path: Path | None = None,
        min_confidence: float = 0.65,
    ) -> AgentRunResult:
        started = datetime.now(timezone.utc)
        project_root = project_root.resolve()
        public_evidence_roots = {
            alias: path.resolve().as_posix()
            for alias, path in (evidence_roots or {}).items()
        }
        run_id = stable_id("RARUN", [project_root.as_posix(), started.isoformat()])
        catalog = catalog_path or REPO_ROOT / "tasks/catalog/resilience-defect-classes.v0.1.yaml"
        rules = rules_path or TEMPLATE_ROOT / "defect-matchers.v0.1.yaml"
        episode_templates = (
            episode_templates_path or TEMPLATE_ROOT / "episode-design-templates.v0.1.yaml"
        )
        tools = ProjectAnalysisTools(
            project_root,
            catalog,
            system_context,
            evidence_roots=evidence_roots,
        )

        static_candidates = identify_defects(
            project_root,
            catalog,
            rules,
            system_context,
        )
        validate_document(static_candidates, CANDIDATE_SCHEMA)
        stages: list[dict[str, Any]] = [
            {
                "stage": "deterministic_evidence_collection",
                "status": "completed",
                "summary": static_candidates["scan_summary"],
            }
        ]
        try:
            candidates, defect_trace, defect_assessment = analyze_defects_with_model(
                seed_document=static_candidates,
                model=self.model,
                tools=tools,
                assessment_schema_path=MODEL_DEFECT_SCHEMA,
            )
        except Exception as exc:
            self._write_failure_manifest(
                run_id=run_id,
                started=started,
                project_root=project_root,
                output_dir=output_dir,
                stages=stages,
                error=exc,
                evidence_roots=public_evidence_roots,
            )
            raise
        stages.append(defect_trace)
        validate_document(candidates, CANDIDATE_SCHEMA)

        base_designs = design_episodes(
            candidates,
            episode_templates,
            min_confidence=min_confidence,
        )
        validate_document(base_designs, EPISODE_SCHEMA)
        stages.append(
            {
                "stage": "deterministic_episode_scaffold",
                "status": "completed",
                "summary": base_designs["summary"],
            }
        )
        try:
            episode_designs, episode_trace, episode_review = review_episode_designs_with_model(
                candidate_document=candidates,
                base_designs=base_designs,
                model=self.model,
                tools=tools,
                review_schema_path=MODEL_EPISODE_SCHEMA,
            )
        except Exception as exc:
            self._write_failure_manifest(
                run_id=run_id,
                started=started,
                project_root=project_root,
                output_dir=output_dir,
                stages=stages,
                error=exc,
                evidence_roots=public_evidence_roots,
                partial_candidates=candidates,
                partial_defect_assessment=defect_assessment,
            )
            raise
        stages.append(episode_trace)
        validate_document(episode_designs, EPISODE_SCHEMA)

        completed = datetime.now(timezone.utc)
        limitations = unique_limitations(candidates, episode_designs)
        manifest = {
            "schema_version": "resilience-agent-run.v0.1",
            "run_id": run_id,
            "status": "completed",
            "started_at": started.isoformat(),
            "completed_at": completed.isoformat(),
            "reasoning_mode": "model",
            "model": self.model.config.public_dict(),
            "project_root": project_root.as_posix(),
            "evidence_roots": public_evidence_roots,
            "stages": stages,
            "artifacts": {
                "candidate_defects": "candidate-defects.json",
                "episode_designs": "episode-designs.json",
                "agent_run": "agent-run.json",
                "model_defect_assessment": "model-defect-assessment.json",
                "model_episode_review": "model-episode-review.json",
            },
            "limitations": limitations,
        }
        validate_document(manifest, RUN_SCHEMA)
        if output_dir is not None:
            write_json(output_dir / "candidate-defects.json", candidates)
            write_json(output_dir / "episode-designs.json", episode_designs)
            write_json(output_dir / "model-defect-assessment.json", defect_assessment)
            write_json(output_dir / "model-episode-review.json", episode_review)
            write_json(output_dir / "agent-run.json", manifest)
        return AgentRunResult(
            candidates=candidates,
            episode_designs=episode_designs,
            run_manifest=manifest,
            model_defect_assessment=defect_assessment,
            model_episode_review=episode_review,
        )

    def _write_failure_manifest(
        self,
        *,
        run_id: str,
        started: datetime,
        project_root: Path,
        output_dir: Path | None,
        stages: list[dict[str, Any]],
        error: Exception,
        evidence_roots: dict[str, str],
        partial_candidates: dict[str, Any] | None = None,
        partial_defect_assessment: dict[str, Any] | None = None,
    ) -> None:
        trace = error.trace if isinstance(error, AgentLoopError) else None
        failed_stages = [*stages]
        if trace:
            failed_stages.append(trace)
        else:
            failed_stages.append(
                {
                    "stage": "model_contract_validation",
                    "status": "failed",
                    "error_type": type(error).__name__,
                }
            )
        manifest = {
            "schema_version": "resilience-agent-run.v0.1",
            "run_id": run_id,
            "status": "failed",
            "started_at": started.isoformat(),
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "reasoning_mode": "model",
            "model": self.model.config.public_dict(),
            "project_root": project_root.as_posix(),
            "evidence_roots": evidence_roots,
            "stages": failed_stages,
            "artifacts": {
                "candidate_defects": (
                    "candidate-defects.json" if partial_candidates is not None else None
                ),
                "episode_designs": None,
                "agent_run": "agent-run.json",
                "model_defect_assessment": (
                    "model-defect-assessment.json"
                    if partial_defect_assessment is not None
                    else None
                ),
                "model_episode_review": None,
            },
            "limitations": [redact_sensitive_text(str(error))[:2000]],
        }
        validate_document(manifest, RUN_SCHEMA)
        if output_dir is not None:
            if partial_candidates is not None:
                write_json(output_dir / "candidate-defects.json", partial_candidates)
            if partial_defect_assessment is not None:
                write_json(
                    output_dir / "model-defect-assessment.json",
                    partial_defect_assessment,
                )
            write_json(output_dir / "agent-run.json", manifest)


def unique_limitations(
    candidates: dict[str, Any],
    episode_designs: dict[str, Any],
) -> list[str]:
    limitations = list(candidates["model_review"]["coverage_notes"])
    limitations.extend(
        f"Dropped invalid model finding {item['defect_ref']} for {item['target_component']}: {item['reason']}"
        for item in candidates["model_review"]["invalid_findings"]
    )
    if not candidates["candidates"]:
        limitations.append("No evidence-backed candidate passed model and deterministic validation.")
    for episode in episode_designs["episodes"]:
        limitations.extend(episode["readiness"]["execution_blockers"])
    seen: set[str] = set()
    result = []
    for item in limitations:
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result
