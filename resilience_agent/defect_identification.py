"""Our deterministic evidence collection capability for defect identification.

The matcher deliberately emits candidates, not confirmed runtime defects.  It
links conservative source/config signals to the repository's DefectSpec catalog
and preserves the exact evidence used for each judgment.
"""

from __future__ import annotations

import fnmatch
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import yaml

from .common import (
    load_document,
    redact_sensitive_text,
    sanitize_context,
    stable_id,
    unique_strings,
)


DEFAULT_IGNORES = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "__pycache__",
    ".pytest_cache",
    "node_modules",
    "vendor",
    "dist",
    "build",
    "target",
}


def _matches_glob(relative_path: str, pattern: str) -> bool:
    return fnmatch.fnmatch(relative_path, pattern) or (
        pattern.startswith("**/") and fnmatch.fnmatch(relative_path, pattern[3:])
    )


def _inventory(project_root: Path, max_file_bytes: int) -> list[tuple[str, str]]:
    files: list[tuple[str, str]] = []
    for path in sorted(project_root.rglob("*")):
        if not path.is_file() or any(part in DEFAULT_IGNORES for part in path.relative_to(project_root).parts):
            continue
        try:
            if path.stat().st_size > max_file_bytes:
                continue
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        files.append((path.relative_to(project_root).as_posix(), text))
    return files


def _flags(value: str) -> int:
    result = 0
    for flag in value.lower():
        result |= {"i": re.IGNORECASE, "m": re.MULTILINE, "s": re.DOTALL}.get(flag, 0)
    return result


def _first_match(signal: dict[str, Any], text: str) -> re.Match[str] | None:
    return re.search(signal["pattern"], text, _flags(str(signal.get("flags", "im"))))


def _evidence(relative_path: str, text: str, signal: dict[str, Any], match: re.Match[str]) -> dict[str, Any]:
    line_start = text.count("\n", 0, match.start()) + 1
    line = text.splitlines()[line_start - 1].strip() if text.splitlines() else ""
    suffix = Path(relative_path).suffix.lower()
    if relative_path == "$system-context":
        kind = "system"
    else:
        kind = "config" if suffix in {".yaml", ".yml", ".json", ".toml", ".properties"} else "source"
    return {
        "kind": kind,
        "path": relative_path,
        "line_start": line_start,
        "line_end": line_start,
        "signal_id": signal["id"],
        "summary": signal["message"],
        "excerpt": redact_sensitive_text(line)[:240],
    }


def _evaluate_text(
    rule: dict[str, Any],
    path_texts: list[tuple[str, str]],
) -> tuple[bool, list[dict[str, Any]], list[str]]:
    all_signals = rule.get("match", {}).get("all", [])
    any_signals = rule.get("match", {}).get("any", [])
    none_signals = rule.get("match", {}).get("none", [])
    evidence: list[dict[str, Any]] = []

    def locate(signal: dict[str, Any]) -> tuple[str, str, re.Match[str]] | None:
        for relative_path, text in path_texts:
            match = _first_match(signal, text)
            if match:
                return relative_path, text, match
        return None

    for signal in all_signals:
        found = locate(signal)
        if found is None:
            return False, [], []
        evidence.append(_evidence(found[0], found[1], signal, found[2]))

    any_found = []
    for signal in any_signals:
        found = locate(signal)
        if found:
            any_found.append((signal, found))
    if any_signals and not any_found:
        return False, [], []
    for signal, found in any_found:
        evidence.append(_evidence(found[0], found[1], signal, found[2]))

    for signal in none_signals:
        if locate(signal) is not None:
            return False, [], []

    missing_safeguards = [signal["message"] for signal in none_signals]
    return bool(evidence), evidence, missing_safeguards


def _component_from_path(relative_path: str) -> str:
    path = Path(relative_path)
    parent = path.parent.name
    if parent and parent not in {"src", "main", "config", "k8s", "kubernetes", "deployment"}:
        return parent
    return path.stem


def _candidate_from_match(
    rule: dict[str, Any],
    defect: dict[str, Any],
    evidence: list[dict[str, Any]],
    missing_safeguards: list[str],
) -> dict[str, Any]:
    component = str(rule.get("target_component") or _component_from_path(evidence[0]["path"]))
    score = float(rule["confidence_score"])
    candidate_id = stable_id(
        f"CAND-{defect['defect_id']}",
        [defect["defect_id"], component, rule["rule_id"], *(item["path"] for item in evidence)],
    )
    return {
        "candidate_id": candidate_id,
        "defect_ref": defect["defect_id"],
        "title": defect["title"],
        "family": defect["family"],
        "status": "candidate_unverified",
        "confidence": _confidence_label(score),
        "confidence_score": round(score, 2),
        "target": {
            "component": component,
            "artifacts": unique_strings(item["path"] for item in evidence),
        },
        "match_rule_ids": [rule["rule_id"]],
        "evidence": evidence,
        "reasoning": {
            "mechanism": defect["latent_defect"]["mechanism"],
            "matched_conditions": unique_strings(item["summary"] for item in evidence),
            "missing_safeguards": missing_safeguards,
            "alternative_explanations": list(rule.get("alternative_explanations", [])),
        },
        "validation_requirements": unique_strings(
            [
                "Confirm the matched call path is exercised by the intended workload.",
                "Use runtime evidence to verify the predicted fault effect and business impact.",
                *defect["failure_outcome"]["invalid_outcomes"],
            ]
        ),
        "planning_hints": {
            "trigger_class": defect["fault_trigger"]["trigger_class"],
            "actuator_candidates": defect["fault_trigger"]["actuator_candidates"],
            "parameters": defect["fault_trigger"]["parameters"],
            "guardrails": defect["fault_trigger"]["guardrails"],
            "expected_degradation": defect["failure_outcome"]["expected_degradation"],
            "slo_impact": defect["failure_outcome"]["slo_impact"],
            "observable_evidence": defect["observable_evidence"],
            "cleanup": defect["recovery"]["cleanup"],
            "recovery_verification": defect["recovery"]["verification"],
        },
    }


def _confidence_label(score: float) -> str:
    if score >= 0.85:
        return "high"
    if score >= 0.65:
        return "medium"
    return "low"


def _merge_candidates(candidates: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    for candidate in candidates:
        candidate_artifacts = set(candidate["target"]["artifacts"])
        current = next(
            (
                item
                for item in merged
                if item["defect_ref"] == candidate["defect_ref"]
                and (
                    item["target"]["component"] == candidate["target"]["component"]
                    or bool(set(item["target"]["artifacts"]) & candidate_artifacts)
                )
            ),
            None,
        )
        if current is None:
            merged.append(candidate)
            continue
        current["match_rule_ids"] = unique_strings(current["match_rule_ids"] + candidate["match_rule_ids"])
        current["target"]["artifacts"] = unique_strings(
            current["target"]["artifacts"] + candidate["target"]["artifacts"]
        )
        seen_evidence = {
            (item["path"], item["line_start"], item["signal_id"]) for item in current["evidence"]
        }
        current["evidence"].extend(
            item
            for item in candidate["evidence"]
            if (item["path"], item["line_start"], item["signal_id"]) not in seen_evidence
        )
        current["reasoning"]["matched_conditions"] = unique_strings(
            current["reasoning"]["matched_conditions"] + candidate["reasoning"]["matched_conditions"]
        )
        current["reasoning"]["missing_safeguards"] = unique_strings(
            current["reasoning"]["missing_safeguards"] + candidate["reasoning"]["missing_safeguards"]
        )
        current["reasoning"]["alternative_explanations"] = unique_strings(
            current["reasoning"]["alternative_explanations"]
            + candidate["reasoning"]["alternative_explanations"]
        )
        current["validation_requirements"] = unique_strings(
            current["validation_requirements"] + candidate["validation_requirements"]
        )
        if "model-semantic-analysis" in candidate["match_rule_ids"]:
            current["target"]["component"] = candidate["target"]["component"]
            current["reasoning"]["mechanism"] = candidate["reasoning"]["mechanism"]
        score = min(0.99, max(current["confidence_score"], candidate["confidence_score"]) + 0.05)
        current["confidence_score"] = round(score, 2)
        current["confidence"] = _confidence_label(score)
    return sorted(merged, key=lambda item: (-item["confidence_score"], item["defect_ref"]))


def identify_defects(
    project_root: Path,
    catalog_path: Path,
    rules_path: Path,
    system_context: dict[str, Any] | None = None,
    *,
    max_file_bytes: int = 1_000_000,
) -> dict[str, Any]:
    """Match project evidence against the operational rules of DefectSpecs."""
    project_root = project_root.resolve()
    if not project_root.is_dir():
        raise ValueError(f"project root is not a directory: {project_root}")
    catalog = load_document(catalog_path)
    registry = load_document(rules_path)
    system_context = sanitize_context(system_context or {})
    defects = {item["defect_id"]: item for item in catalog["items"]}
    inventory = _inventory(project_root, max_file_bytes)
    raw_candidates: list[dict[str, Any]] = []

    for rule in registry["rules"]:
        defect = defects.get(rule["defect_id"])
        if defect is None:
            raise ValueError(f"matcher {rule['rule_id']} references unknown defect {rule['defect_id']}")
        if rule.get("input", "files") == "context":
            selected = [("$system-context", yaml.safe_dump(system_context, allow_unicode=True))]
        else:
            selected = [
                item
                for item in inventory
                if any(_matches_glob(item[0], glob) for glob in rule["file_globs"])
            ]
        if rule.get("scope", "file") == "project":
            matched, evidence, missing = _evaluate_text(rule, selected)
            if matched:
                raw_candidates.append(_candidate_from_match(rule, defect, evidence, missing))
            continue
        for item in selected:
            matched, evidence, missing = _evaluate_text(rule, [item])
            if matched:
                raw_candidates.append(_candidate_from_match(rule, defect, evidence, missing))

    candidates = _merge_candidates(raw_candidates)
    analysis_id = stable_id(
        "ANALYSIS",
        [project_root.as_posix(), registry["registry_version"], *(item["candidate_id"] for item in candidates)],
    )
    covered = sorted({rule["defect_id"] for rule in registry["rules"]})
    return {
        "schema_version": "candidate-defects.v0.1",
        "analysis_id": analysis_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "analysis_mode": "static_candidate_identification",
        "model_provenance": None,
        "model_review": {
            "analysis_summary": "Deterministic matcher pass only; no model reasoning was used.",
            "rejected_seed_candidates": [],
            "invalid_findings": [],
            "coverage_notes": [
                "Templates without deterministic matchers require model-assisted semantic review."
            ],
        },
        "project": {
            "root": project_root.as_posix(),
            "application": str(system_context.get("application", "unknown")),
            "context": system_context,
        },
        "template_registry": {
            "catalog_version": catalog["catalog_version"],
            "matcher_registry_version": registry["registry_version"],
        },
        "scan_summary": {
            "files_scanned": len(inventory),
            "templates_total": len(defects),
            "templates_with_deterministic_matchers": len(covered),
            "rules_evaluated": len(registry["rules"]),
            "candidate_count": len(candidates),
            "coverage_note": (
                "Templates without deterministic matchers remain available for semantic Agent review; "
                "absence of a candidate is not proof that the project is defect-free."
            ),
        },
        "candidates": candidates,
    }
