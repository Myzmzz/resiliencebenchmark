"""Read-only inspection of sealed Stage-2 matrix and Trial evidence."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping


SAFE_MATRIX_ID = re.compile(r"^matrix-[a-z0-9][a-z0-9-]{7,100}$")
SAFE_TRIAL_ID = re.compile(r"^campaign-[a-f0-9]{16}-[a-z0-9-]+-[a-z0-9]+-[0-9]+$")
MAX_TEXT_CHARS = 1_000_000


class MatrixEvidenceNotFound(LookupError):
    """The requested sealed matrix or Trial does not exist."""


class MatrixEvidenceStore:
    """Build inspection views without changing the sealed evidence tree."""

    def __init__(self, artifact_root: Path):
        self.artifact_root = artifact_root.resolve()

    def list_matrices(self) -> list[dict[str, Any]]:
        rows = []
        if not self.artifact_root.is_dir():
            return rows
        for root in sorted(self.artifact_root.glob("matrix-*/report.json"), reverse=True):
            matrix_root = root.parent
            if not SAFE_MATRIX_ID.fullmatch(matrix_root.name):
                continue
            try:
                report = _load_json(root)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            rows.append(
                {
                    "matrix_id": matrix_root.name,
                    "system": report.get("system"),
                    "generated_at": report.get("generated_at"),
                    "expected_trial_count": report.get("expected_trial_count", 0),
                    "completed_trial_count": report.get("completed_trial_count", 0),
                    "campaign_count": len(report.get("campaigns") or ()),
                    "manifest_valid": _verify_manifest(matrix_root)["valid"],
                }
            )
        return rows

    def overview(self, matrix_id: str) -> dict[str, Any]:
        matrix_root = self._matrix_root(matrix_id)
        report = _load_json_required(matrix_root / "report.json", matrix_id)
        request = _load_json_required(matrix_root / "request.json", matrix_id)
        campaign_results = self._campaign_results(report)
        source_events = self._source_events(
            {item["campaign_id"] for item in campaign_results}
        )
        trials = []
        for campaign in campaign_results:
            model = _campaign_model(campaign)
            source_matrix = _source_matrix_for_campaign(
                campaign["campaign_id"], source_events
            )
            for trial in campaign.get("trials") or ():
                trials.append(
                    self._trial_summary(
                        campaign,
                        trial,
                        model=model,
                        source_matrix=source_matrix,
                        events=source_events,
                    )
                )

        verdicts = Counter(item["agent_verdict"] for item in trials)
        campaign_integrity = []
        for campaign in campaign_results:
            campaign_id = campaign["campaign_id"]
            campaign_integrity.append(
                {
                    "campaign_id": campaign_id,
                    **_verify_manifest(self.artifact_root / campaign_id),
                }
            )
        summary = {
            "expected_trials": int(report.get("expected_trial_count") or 0),
            "completed_trials": len(trials),
            "platform_valid": sum(item["platform_valid"] for item in trials),
            "platform_invalid": sum(not item["platform_valid"] for item in trials),
            "diagnostic_only": sum(item["diagnostic_only"] for item in trials),
            "fault_active": sum(item["fault_active"] for item in trials),
            "effect_verified": sum(item["effect_verified"] for item in trials),
            "agent_recovery_verified": sum(
                item["agent_recovery_verified"] for item in trials
            ),
            "controller_cleanup_verified": sum(
                item["controller_cleanup_verified"] for item in trials
            ),
            "business_recovery_verified": sum(
                item["business_recovery_verified"] for item in trials
            ),
            "verdict_counts": dict(verdicts),
        }
        matrix_integrity = _verify_manifest(matrix_root)
        return {
            "schema_version": "stage2-matrix-inspection.v1",
            "matrix_id": matrix_id,
            "report": report,
            "request": request,
            "summary": summary,
            "integrity": {
                "matrix": {"matrix_id": matrix_id, **matrix_integrity},
                "campaigns": campaign_integrity,
                "all_valid": matrix_integrity["valid"]
                and all(item["valid"] for item in campaign_integrity),
                "verified_count": int(matrix_integrity["valid"])
                + sum(item["valid"] for item in campaign_integrity),
                "expected_count": 1 + len(campaign_integrity),
            },
            "source_matrices": sorted(
                {item["source_matrix_id"] for item in trials if item["source_matrix_id"]}
            ),
            "trials": trials,
        }

    def trial_detail(self, matrix_id: str, trial_id: str) -> dict[str, Any]:
        if not SAFE_TRIAL_ID.fullmatch(trial_id):
            raise MatrixEvidenceNotFound(trial_id)
        overview = self.overview(matrix_id)
        summary = next(
            (item for item in overview["trials"] if item["trial_id"] == trial_id),
            None,
        )
        if summary is None:
            raise MatrixEvidenceNotFound(trial_id)
        campaign_root = self._campaign_root(summary["campaign_id"])
        trial_root = campaign_root / "trials" / trial_id
        output_root = campaign_root / trial_id
        result = _load_json_required(trial_root / "result.json", trial_id)
        harness_report = _load_json_optional(trial_root / "harness-report.json")
        recovery = _load_json_optional(trial_root / "recovery.json")
        disturbances = _load_json_optional(trial_root / "disturbances.json", default=[])
        permission_restore = _load_json_optional(trial_root / "permission-restore.json")
        environment_reset = _load_json_optional(trial_root / "environment-reset.json")
        runtime_context = _load_json_optional(trial_root / "runtime-context.json")
        capability = _load_json_optional(trial_root / "capability.json")
        events = self._source_events({summary["campaign_id"]})
        trial_events = [
            event
            for event in events
            if _event_trial_id(event) == trial_id
            or (
                event.get("campaign_id") == summary["campaign_id"]
                and event.get("kind") in {"campaign_started", "campaign_finished"}
            )
        ]
        files = []
        for base in (trial_root, output_root):
            if not base.is_dir():
                continue
            for path in sorted(item for item in base.rglob("*") if item.is_file()):
                relative = path.relative_to(campaign_root).as_posix()
                files.append(
                    {
                        "path": relative,
                        "size_bytes": path.stat().st_size,
                        "download_url": (
                            f"/api/v1/artifacts/{summary['campaign_id']}/{relative}"
                        ),
                    }
                )
        return {
            "schema_version": "stage2-trial-inspection.v1",
            "matrix_id": matrix_id,
            "summary": summary,
            "result": result,
            "agent": {
                "harness_report": harness_report,
                "stdout": _read_text(output_root / "stdout.txt"),
                "stderr": _read_text(output_root / "stderr.txt"),
                "lifecycle_events": harness_report.get("lifecycle_events", [])
                if isinstance(harness_report, Mapping)
                else [],
            },
            "controller": {
                "events": trial_events,
                "disturbances": disturbances,
                "permission_restore": permission_restore,
                "environment_reset": environment_reset,
            },
            "oracle": {
                "recovery": recovery,
                "fault_effect_evidence": recovery.get("fault_effect_evidence")
                if isinstance(recovery, Mapping)
                else None,
            },
            "runtime": {
                "context": runtime_context,
                "capability": capability,
            },
            "files": files,
        }

    def matrix_artifact(self, matrix_id: str, artifact_path: str) -> Path:
        root = self._matrix_root(matrix_id)
        path = (root / artifact_path).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise MatrixEvidenceNotFound(artifact_path) from exc
        if not path.is_file():
            raise MatrixEvidenceNotFound(artifact_path)
        return path

    def _matrix_root(self, matrix_id: str) -> Path:
        if not SAFE_MATRIX_ID.fullmatch(matrix_id):
            raise MatrixEvidenceNotFound(matrix_id)
        root = (self.artifact_root / matrix_id).resolve()
        try:
            root.relative_to(self.artifact_root)
        except ValueError as exc:
            raise MatrixEvidenceNotFound(matrix_id) from exc
        if not root.is_dir():
            raise MatrixEvidenceNotFound(matrix_id)
        return root

    def _campaign_root(self, campaign_id: str) -> Path:
        if not re.fullmatch(r"campaign-[a-f0-9]{16}", campaign_id):
            raise MatrixEvidenceNotFound(campaign_id)
        root = (self.artifact_root / campaign_id).resolve()
        try:
            root.relative_to(self.artifact_root)
        except ValueError as exc:
            raise MatrixEvidenceNotFound(campaign_id) from exc
        if not root.is_dir():
            raise MatrixEvidenceNotFound(campaign_id)
        return root

    def _campaign_results(self, report: Mapping[str, Any]) -> list[dict[str, Any]]:
        results = []
        for item in report.get("campaigns") or ():
            campaign_id = str(item.get("campaign_id") or "")
            root = self._campaign_root(campaign_id)
            results.append(
                _load_json_required(root / "campaign" / "result.json", campaign_id)
            )
        return results

    def _source_events(self, campaign_ids: set[str]) -> list[dict[str, Any]]:
        events = []
        if not campaign_ids:
            return events
        for path in sorted(self.artifact_root.glob("matrix-*/events.jsonl")):
            source_matrix = path.parent.name
            if not SAFE_MATRIX_ID.fullmatch(source_matrix):
                continue
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for line in lines:
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("campaign_id") not in campaign_ids:
                    continue
                events.append({"source_matrix_id": source_matrix, **event})
        return events

    def _trial_summary(
        self,
        campaign: Mapping[str, Any],
        trial: Mapping[str, Any],
        *,
        model: str,
        source_matrix: str | None,
        events: Iterable[Mapping[str, Any]],
    ) -> dict[str, Any]:
        trial_id = str(trial.get("trial_id") or "")
        campaign_id = str(campaign.get("campaign_id") or "")
        trial_events = [item for item in events if _event_trial_id(item) == trial_id]
        started_at = _event_time(trial_events, "trial_started")
        finished_at = _event_time(trial_events, "trial_finished")
        recovery = trial.get("recovery") or {}
        harness_report = _load_json_optional(
            self.artifact_root
            / campaign_id
            / "trials"
            / trial_id
            / "harness-report.json"
        )
        validation_error = None
        if isinstance(harness_report, Mapping):
            final_output = harness_report.get("final_output")
            if isinstance(final_output, Mapping):
                validation_error = final_output.get("validation_error")
            validation_error = validation_error or harness_report.get("error")
        return {
            "campaign_id": campaign_id,
            "request_id": campaign.get("request_id"),
            "source_matrix_id": source_matrix,
            "trial_id": trial_id,
            "harness": trial.get("harness"),
            "model": model,
            "case_id": trial.get("kind"),
            "agent_verdict": trial.get("agent_verdict"),
            "platform_valid": trial.get("platform_valid") is True,
            "diagnostic_only": trial.get("diagnostic_only") is True,
            "fault_active": recovery.get("main_fault_ever_active") is True,
            "effect_verified": recovery.get("fault_effect_verified") is True,
            "agent_recovery_verified": recovery.get("agent_recovery_verified") is True,
            "controller_cleanup_verified": recovery.get("controller_cleanup_verified")
            is True,
            "business_recovery_verified": recovery.get("business_recovery_verified")
            is True,
            "target": trial.get("runtime_target") or {},
            "validation_error": validation_error,
            "started_at": started_at,
            "finished_at": finished_at,
            "duration_seconds": _duration_seconds(started_at, finished_at),
            "artifact_count": len(trial.get("artifact_refs") or ()),
        }


def _verify_manifest(root: Path) -> dict[str, Any]:
    manifest = root / "manifest.sha256"
    if not manifest.is_file():
        return {"valid": False, "checked_files": 0, "errors": ["manifest missing"]}
    checked = 0
    errors = []
    try:
        rows = manifest.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        return {"valid": False, "checked_files": 0, "errors": [type(exc).__name__]}
    for row in rows:
        if not row.strip():
            continue
        try:
            digest, relative = row.split("  ", 1)
        except ValueError:
            errors.append("malformed manifest row")
            continue
        if not re.fullmatch(r"[a-f0-9]{64}", digest):
            errors.append(f"invalid digest: {relative}")
            continue
        path = (root / relative).resolve()
        try:
            path.relative_to(root.resolve())
        except ValueError:
            errors.append(f"escaped path: {relative}")
            continue
        if not path.is_file():
            errors.append(f"missing: {relative}")
            continue
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != digest:
            errors.append(f"digest mismatch: {relative}")
            continue
        checked += 1
    return {"valid": not errors, "checked_files": checked, "errors": errors}


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _load_json_required(path: Path, identity: str) -> dict[str, Any]:
    if not path.is_file():
        raise MatrixEvidenceNotFound(identity)
    try:
        return _load_json(path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise MatrixEvidenceNotFound(identity) from exc


def _load_json_optional(path: Path, default: Any = None) -> Any:
    if not path.is_file():
        return {} if default is None else default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {} if default is None else default


def _read_text(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"available": False, "text": "", "truncated": False}
    text = path.read_text(encoding="utf-8", errors="replace")
    truncated = len(text) > MAX_TEXT_CHARS
    return {
        "available": True,
        "text": text[:MAX_TEXT_CHARS],
        "truncated": truncated,
        "size_bytes": path.stat().st_size,
    }


def _campaign_model(campaign: Mapping[str, Any]) -> str:
    values = list((campaign.get("model_by_harness") or {}).values())
    return str(values[0]) if values else "unknown"


def _event_trial_id(event: Mapping[str, Any]) -> str | None:
    direct = event.get("trial_id")
    if isinstance(direct, str):
        return direct
    payload = event.get("payload")
    if isinstance(payload, Mapping) and isinstance(payload.get("trial_id"), str):
        return payload["trial_id"]
    return None


def _source_matrix_for_campaign(
    campaign_id: str, events: Iterable[Mapping[str, Any]]
) -> str | None:
    return next(
        (
            str(item.get("source_matrix_id"))
            for item in events
            if item.get("campaign_id") == campaign_id and item.get("source_matrix_id")
        ),
        None,
    )


def _event_time(events: Iterable[Mapping[str, Any]], kind: str) -> str | None:
    return next(
        (
            str(item.get("occurred_at") or item.get("observed_at"))
            for item in events
            if item.get("kind") == kind
            and (item.get("occurred_at") or item.get("observed_at"))
        ),
        None,
    )


def _duration_seconds(started_at: str | None, finished_at: str | None) -> float | None:
    if not started_at or not finished_at:
        return None
    try:
        start = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        finish = datetime.fromisoformat(finished_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    return round(max((finish - start).total_seconds(), 0.0), 3)
