"""Verify D0 qualification evidence before a formal Stage-2 Campaign."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from harness.d0.common import evaluation_ready_result

from .contracts import CampaignRequest, D0QualificationRef
from .gateway_evidence import read_gateway_artifact


class D0QualificationGate:
    def __init__(self, artifact_root: Path | None):
        self.artifact_root = artifact_root.resolve() if artifact_root else None

    def qualify(self, request: CampaignRequest) -> dict[str, Any]:
        agents: dict[str, Any] = {}
        for harness in request.harnesses:
            ref = request.qualification_refs.get(harness)
            if ref is None:
                agents[harness.value] = {
                    "verified": False,
                    "reason": "qualification reference is missing",
                }
                continue
            agents[harness.value] = self._verify(
                harness.value,
                request.model_by_harness[harness],
                ref,
            )
        formal_eligible = bool(agents) and all(
            value.get("verified") is True for value in agents.values()
        )
        diagnostic = request.qualification_mode == "diagnostic"
        return {
            "schema_version": "stage2-d0-qualification-gate.v1",
            "mode": request.qualification_mode,
            "execution_allowed": diagnostic or formal_eligible,
            "formal_eligible": formal_eligible,
            "scored": formal_eligible and not diagnostic,
            "agents": agents,
        }

    def inventory(self) -> dict[str, Any]:
        campaigns = []
        if self.artifact_root is not None and self.artifact_root.is_dir():
            for path in sorted(self.artifact_root.glob("d0-*/campaign.json")):
                try:
                    value = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                manifest = path.parent / "manifest.sha256"
                campaigns.append(
                    {
                        "campaign_id": value.get("campaign_id"),
                        "status": value.get("status"),
                        "finished_at": value.get("finished_at"),
                        "host": value.get("host"),
                        "manifest_sha256": (
                            hashlib.sha256(manifest.read_bytes()).hexdigest()
                            if manifest.is_file()
                            else None
                        ),
                        "agents": {
                            result.get("agent"): result.get("status")
                            for result in value.get("results", [])
                            if result.get("agent")
                        },
                        "models": value.get("models", {}),
                    }
                )
        return {
            "schema_version": "stage2-d0-qualification-inventory.v1",
            "artifact_root_configured": self.artifact_root is not None,
            "campaigns": campaigns,
        }

    def _verify(
        self, agent: str, requested_model: str, ref: D0QualificationRef
    ) -> dict[str, Any]:
        if self.artifact_root is None:
            return {"verified": False, "reason": "D0 artifact root is not configured"}
        return verify_d0_ref(
            self.artifact_root,
            agent=agent,
            requested_model=requested_model,
            ref=ref,
        )


def verify_d0_ref(
    artifact_root: Path,
    *,
    agent: str,
    requested_model: str,
    ref: D0QualificationRef,
) -> dict[str, Any]:
    """Verify a formal D0 qualification ref against sealed campaign evidence."""
    root = Path(artifact_root).resolve()
    campaign_dir = root / ref.campaign_id
    if _path_contains_symlink(root, campaign_dir):
        return _failure(ref, "D0 campaign path escaped artifact root")
    try:
        campaign_dir = campaign_dir.resolve(strict=True)
        campaign_dir.relative_to(root)
    except (OSError, ValueError):
        return _failure(ref, "D0 campaign path escaped artifact root")
    if not campaign_dir.is_dir():
        return _failure(ref, "D0 campaign or Manifest is missing")

    manifest_path = campaign_dir / "manifest.sha256"
    campaign_path = campaign_dir / "campaign.json"
    if (
        _path_contains_symlink(campaign_dir, manifest_path)
        or _path_contains_symlink(campaign_dir, campaign_path)
    ):
        return _failure(ref, "D0 Manifest listed path contains a symbolic link")
    if not campaign_path.is_file() or not manifest_path.is_file():
        return _failure(ref, "D0 campaign or Manifest is missing")

    try:
        manifest_sha256 = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    except OSError:
        return _failure(ref, "D0 campaign or Manifest is missing")
    if manifest_sha256 != ref.manifest_sha256:
        return _failure(ref, "D0 Manifest digest mismatch")

    manifest = _verify_manifest(campaign_dir, manifest_path)
    if manifest.get("error"):
        return _failure(ref, str(manifest["error"]), manifest_sha256=manifest_sha256)
    entries = manifest["entries"]
    if "campaign.json" not in entries:
        return _failure(
            ref,
            "D0 Manifest does not cover campaign.json",
            manifest_sha256=manifest_sha256,
        )

    receipt_rel = _agent_receipt_rel(agent, ref.gateway_evidence_ref)
    if receipt_rel is None:
        return _failure(
            ref,
            "D0 gateway receipt path escaped campaign artifact root",
            manifest_sha256=manifest_sha256,
        )
    if receipt_rel not in entries:
        return _failure(
            ref,
            "D0 Manifest does not cover gateway receipts",
            manifest_sha256=manifest_sha256,
        )

    try:
        campaign = json.loads(campaign_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return _failure(
            ref, "D0 campaign.json is not readable", manifest_sha256=manifest_sha256
        )
    if not isinstance(campaign, dict):
        return _failure(
            ref, "D0 campaign.json is not an object", manifest_sha256=manifest_sha256
        )
    if campaign.get("campaign_id") != ref.campaign_id:
        return _failure(
            ref, "D0 campaign id does not match the qualification reference"
        )

    results = campaign.get("results")
    if not isinstance(results, list):
        return _failure(
            ref, "D0 campaign results are missing", manifest_sha256=manifest_sha256
        )
    agent_results = [
        value
        for value in results
        if isinstance(value, dict) and value.get("agent") == agent
    ]
    if len(agent_results) != 1:
        return _failure(
            ref,
            "D0 campaign must contain exactly one result for the requested agent",
            manifest_sha256=manifest_sha256,
        )
    result = agent_results[0]

    status = str(result.get("status") or "MISSING")
    qualified_model = str((campaign.get("models") or {}).get(agent) or "")
    if ref.agent_status != status:
        return _failure(
            ref, "D0 agent status does not match the qualification reference"
        )
    if (
        qualified_model != requested_model
        or result.get("model_alias") != requested_model
        or ref.model_alias != requested_model
    ):
        return _failure(ref, "D0 model identity does not match the formal Trial")
    if result.get("gateway_route") != ref.gateway_route:
        return _failure(
            ref, "D0 gateway route does not match the qualification reference"
        )
    if result.get("gateway_config_sha256") != ref.gateway_config_sha256:
        return _failure(
            ref,
            "D0 gateway config does not match the qualification reference",
        )
    if (
        result.get("gateway_evidence_verified") is not True
        or ref.gateway_evidence_verified is not True
    ):
        return _failure(ref, "D0 gateway evidence was not verified")
    if tuple(result.get("gateway_request_ids") or ()) != tuple(ref.gateway_request_ids):
        return _failure(
            ref,
            "D0 gateway request ids do not match the qualification reference",
        )
    if result.get("gateway_evidence_ref") != ref.gateway_evidence_ref:
        return _failure(
            ref,
            "D0 gateway evidence ref does not match the qualification reference",
        )
    if result.get("gateway_trial_id") != ref.gateway_trial_id:
        return _failure(
            ref,
            "D0 gateway trial id does not match the qualification reference",
        )
    if not evaluation_ready_result(dict(result)):
        return _failure(ref, "D0 result is not evaluation-ready")

    receipt_path = campaign_dir / receipt_rel
    gateway_rows = read_gateway_artifact(
        receipt_path,
        trial_id=ref.gateway_trial_id,
        harness=agent,
        model_alias=requested_model,
        config_sha256=ref.gateway_config_sha256,
        request_ids=set(ref.gateway_request_ids),
    )
    if gateway_rows is None:
        return _failure(ref, "D0 gateway receipt artifact did not revalidate")

    host_verified = (campaign.get("host") or {}).get("verified") is True
    verified = host_verified and campaign.get("status") == "QUALIFIED"
    return {
        "verified": verified,
        "campaign_id": ref.campaign_id,
        "campaign_status": campaign.get("status"),
        "agent_status": status,
        "qualified_model": qualified_model,
        "requested_model": requested_model,
        "host_verified": host_verified,
        "manifest_sha256": manifest_sha256,
        "gateway_route": ref.gateway_route,
        "gateway_config_sha256": ref.gateway_config_sha256,
        "gateway_evidence_verified": True,
        "gateway_request_ids": ref.gateway_request_ids,
        "gateway_evidence_ref": ref.gateway_evidence_ref,
        "gateway_trial_id": ref.gateway_trial_id,
        "gateway_receipt_count": len(gateway_rows),
        "reason": "qualified" if verified else "D0 campaign host or status is not qualified",
    }


def _failure(
    ref: D0QualificationRef, reason: str, *, manifest_sha256: str | None = None
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "verified": False,
        "campaign_id": ref.campaign_id,
        "reason": reason,
    }
    if manifest_sha256 is not None:
        result["manifest_sha256"] = manifest_sha256
    return result


def _verify_manifest(campaign_dir: Path, manifest_path: Path) -> dict[str, Any]:
    try:
        lines = manifest_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return {"error": "D0 Manifest is not readable"}
    entries: dict[str, str] = {}
    for line in lines:
        if not line:
            continue
        try:
            digest, relative = line.split("  ", 1)
        except ValueError:
            return {"error": "D0 Manifest entry is malformed"}
        if (
            len(digest) != 64
            or any(char not in "0123456789abcdef" for char in digest)
            or relative in entries
        ):
            return {"error": "D0 Manifest entry is malformed"}
        rel_path = Path(relative)
        if (
            rel_path.is_absolute()
            or ".." in rel_path.parts
            or not rel_path.parts
            or rel_path.name == "manifest.sha256"
        ):
            return {"error": "D0 Manifest entry escaped campaign artifact root"}
        path = campaign_dir / rel_path
        if _path_contains_symlink(campaign_dir, path):
            return {"error": "D0 Manifest listed path contains a symbolic link"}
        if not path.is_file():
            return {"error": "D0 Manifest listed file is missing"}
        try:
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            return {"error": "D0 Manifest listed file is missing"}
        if actual != digest:
            return {"error": "D0 Manifest entry digest mismatch"}
        entries[relative] = digest
    return {"entries": entries}


def _agent_receipt_rel(agent: str, evidence_ref: str) -> str | None:
    ref_path = Path(evidence_ref)
    if (
        ref_path.is_absolute()
        or ".." in ref_path.parts
        or not ref_path.parts
        or ref_path.parts[0] != "native"
        or ref_path.name != "gateway-requests.json"
    ):
        return None
    return (Path(agent) / ref_path).as_posix()


def _path_contains_symlink(root: Path, path: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return True
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return True
    return False
