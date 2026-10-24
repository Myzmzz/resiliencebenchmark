"""Summarise every saved Dx-round run into one table for the final analysis.

Usage:
    python3 aggregate_dx.py                 # markdown table on stdout
    python3 aggregate_dx.py --json out.json # also write the rows as JSON

Each run directory under runs/ is written by run_dx.py and holds request.json,
summary.json, score.json, interactions.json and usage.json.  A run that is still
going only has request.json and is listed as "running".  The platform
environment of a run (image, with or without Coroot) is taken from the note
that run_dx.py put into the request, so part-1 runs (5746ecf, no Coroot),
part-2 runs (5746ecf+coroot) and part-3 runs (35c9e2c+coroot) stay apart.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

HERE = Path(__file__).resolve().parent
CASE_ORDER = ["C0", "D1", "D2", "D3", "D4", "D5", "D6", "D7-A", "D7-B", "D8-A", "D8-B"]
HARNESS_ORDER = ["codex", "claude-code", "deepseek-harness"]
RUN_ID = re.compile(r"lxr-[0-9a-f]{16}")


def load(path: Path) -> Dict[str, Any]:
    """Parse one JSON file, or return {} when it is missing or unreadable."""
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def environment_label(note: str) -> str:
    """Turn the run note ("... image 5746ecf+coroot") into a short environment label."""
    match = re.search(r"image (\S+)", note or "")
    return match.group(1) if match else "?"


def node_problems(score: Dict[str, Any]) -> List[str]:
    """Nodes that did not earn full credit, as 'NODE:STATUS score/max'.

    A run the platform blocked outright (every node BLOCKED_BY_PLATFORM, e.g.
    a harness timeout) collapses to one entry so the table stays readable.
    """
    nodes = score.get("node_results") or []
    if isinstance(nodes, dict):
        nodes = [dict(value, node=key) for key, value in nodes.items()]
    nodes = [node for node in nodes if isinstance(node, dict)]
    if nodes and all(str(node.get("status")) == "BLOCKED_BY_PLATFORM" for node in nodes):
        return ["all nodes BLOCKED_BY_PLATFORM"]
    problems = []
    for node in nodes:
        name = node.get("node") or node.get("node_id") or node.get("id") or node.get("name") or "?"
        status = str(node.get("status"))
        got, most = node.get("score"), node.get("max_score") or node.get("weight")
        if status != "VERIFIED" or (got is not None and most is not None and got < most):
            problems.append(f"{name}:{status} {got}/{most}")
    return problems


def row_for(run_dir: Path) -> Optional[Dict[str, Any]]:
    """One table row per run directory, or None when it is not a run directory."""
    request = load(run_dir / "request.json")
    if not request:
        return None
    summary = load(run_dir / "summary.json")
    score = load(run_dir / "score.json")
    usage = load(run_dir / "usage.json")
    counters = summary.get("counters") or {}
    score_summary = score.get("score_summary") or {}
    usage_summary = usage.get("summary") or {}
    variant = request.get("tool_substitution_variant")
    label = f"{request.get('case')}-{variant}" if variant else str(request.get("case"))
    failure = summary.get("failure") or {}
    found = RUN_ID.search(run_dir.name)
    return {
        "case": label,
        "harness": request.get("harness"),
        "run_id": summary.get("run_id") or (found.group(0) if found else run_dir.name),
        "environment": environment_label(request.get("note", "")),
        "status": summary.get("status") or "running",
        "verdict": score.get("verdict"),
        "validity": score.get("trial_validity"),
        "score": score_summary.get("total_with_bonus"),
        "raw_score": score_summary.get("raw_score"),
        "bonus": score_summary.get("bonus_score"),
        "reason_codes": score.get("reason_codes") or [],
        "failure_code": failure.get("code"),
        "failure_reason": failure.get("reason"),
        "agent_verdict": score.get("agent_verdict"),
        "agent_outcome": score.get("agent_outcome"),
        "experiment_gate": (score.get("experiment_gate") or {}).get("status"),
        "recovery_status": score.get("recovery_status"),
        "capability_loss_score": score.get("capability_loss_score"),
        "node_problems": node_problems(score),
        "interactions": counters.get("interactions"),
        "agent_questions": counters.get("questions_asked_by_agent"),
        "elapsed_seconds": counters.get("elapsed_seconds"),
        "llm_calls": usage_summary.get("total_calls"),
        "total_tokens": usage_summary.get("total_tokens"),
        "saved": str(run_dir),
    }


def sort_key(row: Dict[str, Any]) -> tuple:
    """Order rows by case ladder, then harness, then run id."""
    case = row["case"]
    harness = row["harness"]
    return (
        CASE_ORDER.index(case) if case in CASE_ORDER else len(CASE_ORDER),
        HARNESS_ORDER.index(harness) if harness in HARNESS_ORDER else len(HARNESS_ORDER),
        str(row["run_id"]),
    )


def markdown(rows: List[Dict[str, Any]]) -> str:
    """Render the rows as a compact markdown table."""
    header = ("| 用例 | 智能体 | 环境 | 状态 | 判定 | 有效性 | 分数 | 原因码 | 未满分节点 | 交互 | 调用 | 用时(s) | run_id |\n"
              "|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    lines = [header]
    for row in rows:
        values = {key: ("" if value is None else value) for key, value in row.items()
                  if key not in ("reason_codes", "node_problems")}
        lines.append("| {case} | {harness} | {environment} | {status} | {verdict} | {validity} | {score} | {codes} | "
                     "{nodes} | {interactions} | {llm_calls} | {elapsed_seconds} | {run_id} |".format(
                         codes=",".join(row["reason_codes"]) or (row["failure_code"] or ""),
                         nodes="; ".join(row["node_problems"]) or "—",
                         **values,
                     ))
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--runs", type=Path, default=HERE / "runs")
    parser.add_argument("--json", type=Path, default=None)
    args = parser.parse_args()
    rows = [row for row in (row_for(path) for path in sorted(args.runs.iterdir()) if path.is_dir()) if row]
    rows.sort(key=sort_key)
    print(markdown(rows))
    if args.json:
        args.json.write_text(json.dumps(rows, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
