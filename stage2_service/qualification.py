"""Verify D0 qualification evidence before a formal Stage-2 Campaign."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .contracts import CampaignRequest


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
            agents[harness.value] = self._verify(harness.value, ref)
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
                    }
                )
        return {
            "schema_version": "stage2-d0-qualification-inventory.v1",
            "artifact_root_configured": self.artifact_root is not None,
            "campaigns": campaigns,
        }

    def _verify(self, agent: str, ref) -> dict[str, Any]:
        if self.artifact_root is None:
            return {"verified": False, "reason": "D0 artifact root is not configured"}
        campaign_dir = (self.artifact_root / ref.campaign_id).resolve()
        try:
            campaign_dir.relative_to(self.artifact_root)
        except ValueError:
            return {"verified": False, "reason": "D0 campaign path escaped artifact root"}
        campaign_path = campaign_dir / "campaign.json"
        manifest_path = campaign_dir / "manifest.sha256"
        if not campaign_path.is_file() or not manifest_path.is_file():
            return {"verified": False, "reason": "D0 campaign or Manifest is missing"}
        manifest_sha256 = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        if manifest_sha256 != ref.manifest_sha256:
            return {"verified": False, "reason": "D0 Manifest digest mismatch"}
        campaign = json.loads(campaign_path.read_text(encoding="utf-8"))
        result = next(
            (
                value
                for value in campaign.get("results", [])
                if value.get("agent") == agent
            ),
            None,
        )
        status = str((result or {}).get("status") or "MISSING")
        verified = (
            campaign.get("host", {}).get("verified") is True
            and status == "PASS"
            and ref.agent_status == status
        )
        return {
            "verified": verified,
            "campaign_id": ref.campaign_id,
            "campaign_status": campaign.get("status"),
            "agent_status": status,
            "host_verified": campaign.get("host", {}).get("verified") is True,
            "manifest_sha256": manifest_sha256,
            "reason": "qualified" if verified else "D0 Agent result is not PASS",
        }
