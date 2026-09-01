#!/usr/bin/env python3
"""Run the real remote D0 accounting CPU qualification across four Agents."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from harness.d0 import D0Campaign, D0CampaignConfig
from harness.d0.recompute import merge_agent_evidence, recompute_campaign


DEFAULT_ARTIFACT_ROOT = Path("/var/lib/resiliencebenchmark/artifacts/d0")
DEFAULT_EPISODE = Path("tasks/examples/public/episode.otel-accounting-cpu-d0.v1.yaml")


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    value.add_argument("--execute", action="store_true", help="required: execute the real remote four-Agent matrix")
    value.add_argument("--artifact-root", type=Path, default=Path(os.environ.get("RESBENCH_D0_ARTIFACT_ROOT", DEFAULT_ARTIFACT_ROOT)))
    value.add_argument("--kubeconfig", type=Path, default=Path(os.environ.get("RESBENCH_CONTROLLER_KUBECONFIG", "")))
    value.add_argument("--campaign-id")
    value.add_argument("--recompute-campaign", type=Path)
    value.add_argument(
        "--import-agent",
        action="append",
        default=[],
        metavar="AGENT=CAMPAIGN_DIR",
        help="import one sealed Agent directory before recomputing a campaign",
    )
    value.add_argument("--sample-seconds", type=int, default=10)
    value.add_argument("--agent-timeout-seconds", type=int, default=720)
    value.add_argument(
        "--agents",
        default=",".join(("bladeai", "codex", "claude-code", "deepseek-harness")),
        help="comma-separated D0 adapters; default runs the full four-Agent matrix",
    )
    return value


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.recompute_campaign is not None:
        campaign_dir = args.recompute_campaign.expanduser().resolve()
        imports = {}
        for raw in args.import_agent:
            if "=" not in raw:
                raise SystemExit("--import-agent must use AGENT=CAMPAIGN_DIR")
            agent, source = raw.split("=", 1)
            imports[agent] = Path(source).expanduser().resolve()
        report = (
            merge_agent_evidence(campaign_dir, imports)
            if imports
            else recompute_campaign(campaign_dir)
        )
        print(json.dumps({"status": report["status"], "campaign_id": report["campaign_id"], "visualization": report.get("visualization", {})}, ensure_ascii=False, sort_keys=True))
        return 0
    if not args.execute:
        raise SystemExit("--execute is required; this command has no local or simulated mode")
    repo = args.repo_root.expanduser().resolve()
    kubeconfig = args.kubeconfig.expanduser().resolve()
    if not kubeconfig.is_file():
        raise SystemExit("RESBENCH_CONTROLLER_KUBECONFIG/--kubeconfig must identify the remote controller kubeconfig")
    agents = tuple(item.strip() for item in args.agents.split(",") if item.strip())
    allowed = {"bladeai", "codex", "claude-code", "deepseek-harness"}
    if not agents or len(set(agents)) != len(agents) or not set(agents) <= allowed:
        raise SystemExit("--agents must contain unique registered D0 adapter names")
    config = D0CampaignConfig(
        repo_root=repo,
        artifact_root=args.artifact_root.expanduser().resolve(),
        kubeconfig=kubeconfig,
        episode_file=repo / DEFAULT_EPISODE,
        sample_seconds=args.sample_seconds,
        agent_timeout_seconds=args.agent_timeout_seconds,
        agents=agents,
    )
    try:
        report = D0Campaign(config).run(args.campaign_id)
    except Exception as exc:  # noqa: BLE001 - emit bounded structured failure.
        print(
            json.dumps(
                {"status": "failed", "error_type": type(exc).__name__, "error": str(exc)[:1000]},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 2
    print(
        json.dumps(
            {
                "status": report["status"],
                "campaign_id": report["campaign_id"],
                "artifact_dir": report["artifact_dir"],
                "visualization": report["visualization"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if report["status"] == "QUALIFIED" else 1


if __name__ == "__main__":
    sys.exit(main())
