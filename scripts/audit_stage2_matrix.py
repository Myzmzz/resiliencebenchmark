#!/usr/bin/env python3
"""Generate a derived, reproducible audit from sealed Stage-2 evidence."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping

from stage2_service.matrix_evidence import MatrixEvidenceStore


def render_markdown(audit: Mapping[str, Any]) -> str:
    summary = audit["summary"]
    integrity = audit["integrity"]
    report = audit["report"]
    verdicts = summary["verdict_counts"]
    lines = [
        f"# Stage-2 Post-run Audit: {audit['matrix_id']}",
        "",
        "## 核心结论",
        "",
        f"- 执行覆盖：{summary['completed_trials']} / {summary['expected_trials']}",
        f"- 证据清单：{integrity['verified_count']} / {integrity['expected_count']} 通过",
        f"- 平台有效：{summary['platform_valid']}；平台无效：{summary['platform_invalid']}",
        f"- Agent 判定：PASS={verdicts.get('PASS', 0)}，FAIL={verdicts.get('FAIL', 0)}，"
        f"INCONCLUSIVE={verdicts.get('INCONCLUSIVE', 0)}，CASE_INVALID={verdicts.get('CASE_INVALID', 0)}",
        f"- 主故障真实激活：{summary['fault_active']}；独立效果验证：{summary['effect_verified']}",
        f"- Agent 独立恢复验证：{summary['agent_recovery_verified']}；Controller 清理兜底：{summary['controller_cleanup_verified']}",
        "",
        "> 本文件是从密封证据重算得到的派生审计，不修改原始结果。没有 PASS/FAIL 的组合保持 N/A，不把 INCONCLUSIVE 或 CASE_INVALID 换算为分数。",
        "",
        "## Harness × Model",
        "",
        "| Harness | Model | Score | PASS | FAIL | INCONCLUSIVE | CASE_INVALID | Valid | Fault active | Effect verified | Agent recovery | Controller cleanup |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    trial_by_pair: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for trial in audit["trials"]:
        trial_by_pair.setdefault((trial["harness"], trial["model"]), []).append(trial)
    for row in report["score_table"]:
        pair = trial_by_pair.get((row["harness"], row["model"]), [])
        score = "N/A" if row["score"] is None else f"{row['score']:.2f}"
        lines.append(
            f"| {row['harness']} | {row['model']} | {score} | {row['pass']} | {row['fail']} | "
            f"{row['inconclusive']} | {row['case_invalid']} | {row['valid_trials']} | "
            f"{sum(item['fault_active'] for item in pair)} | {sum(item['effect_verified'] for item in pair)} | "
            f"{sum(item['agent_recovery_verified'] for item in pair)} | "
            f"{sum(item['controller_cleanup_verified'] for item in pair)} |"
        )
    lines.extend(
        [
            "",
            "## 数据来源",
            "",
            f"- 汇总矩阵：`{audit['matrix_id']}`",
            f"- 实际执行源矩阵：{', '.join(f'`{item}`' for item in audit['source_matrices'])}",
            "- 每个 Trial 的 Agent 原始响应、Harness 报告、Controller 事件、扰动、恢复和环境重置证据均可由 `postrun-audit.json` 的 Trial 索引定位。",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--matrix-id", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    audit = MatrixEvidenceStore(args.artifact_root).overview(args.matrix_id)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    json_path = args.output_dir / "postrun-audit.json"
    markdown_path = args.output_dir / "postrun-audit.md"
    json_path.write_text(
        json.dumps(audit, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    markdown_path.write_text(render_markdown(audit), encoding="utf-8")
    print(json.dumps({"json": str(json_path), "markdown": str(markdown_path), "summary": audit["summary"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
