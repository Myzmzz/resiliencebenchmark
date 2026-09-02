#!/usr/bin/env python3
"""Generate a table-only PDF for the sealed Stage2 matrix."""

from __future__ import annotations

import argparse
import html
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Mapping

from reportlab.lib import colors
from reportlab.lib.colors import HexColor
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    LongTable,
    PageBreak,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from generate_stage2_matrix_pdf import (
    CASE_ORDER,
    CASE_TITLES,
    FONT_BOLD,
    FONT_REGULAR,
    HARNESS_LABELS,
    HARNESS_ORDER,
    MODEL_ORDER,
    duration_between,
    fmt_duration,
    fmt_utc,
    load_dataset,
    pair_summary,
    register_fonts,
    short,
)
from stage2_service.matrix_evidence import MatrixEvidenceStore


PAGE_W, PAGE_H = landscape(A4)
NAVY = HexColor("#15243A")
BLUE = HexColor("#2F5F9F")
INK = HexColor("#1D2939")
MUTED = HexColor("#66758A")
LINE_C = HexColor("#CDD7E5")
ROW_ALT = HexColor("#F3F6FA")
GREEN = HexColor("#287D58")
RED = HexColor("#B94A45")
GOLD = HexColor("#A36C16")


def esc(value: Any) -> str:
    return html.escape(str(value), quote=False)


def styles() -> dict[str, ParagraphStyle]:
    return {
        "Title": ParagraphStyle(
            "Title",
            fontName=FONT_BOLD,
            fontSize=17,
            leading=22,
            textColor=NAVY,
            spaceAfter=5 * mm,
        ),
        "Heading": ParagraphStyle(
            "Heading",
            fontName=FONT_BOLD,
            fontSize=11.5,
            leading=15,
            textColor=BLUE,
            spaceBefore=2 * mm,
            spaceAfter=2.5 * mm,
            keepWithNext=True,
        ),
        "Head": ParagraphStyle(
            "Head",
            fontName=FONT_BOLD,
            fontSize=6.2,
            leading=8,
            textColor=colors.white,
            alignment=TA_CENTER,
            wordWrap="CJK",
        ),
        "Cell": ParagraphStyle(
            "Cell",
            fontName=FONT_REGULAR,
            fontSize=5.8,
            leading=7.8,
            textColor=INK,
            alignment=TA_LEFT,
            wordWrap="CJK",
        ),
        "Center": ParagraphStyle(
            "Center",
            fontName=FONT_REGULAR,
            fontSize=5.8,
            leading=7.8,
            textColor=INK,
            alignment=TA_CENTER,
            wordWrap="CJK",
        ),
        "Meta": ParagraphStyle(
            "Meta",
            fontName=FONT_REGULAR,
            fontSize=7,
            leading=10,
            textColor=INK,
            wordWrap="CJK",
        ),
    }


def para(value: Any, style: ParagraphStyle) -> Paragraph:
    return Paragraph(esc(value).replace("\n", "<br/>"), style)


def table(
    headers: list[str],
    rows: list[list[Any]],
    widths_mm: list[float],
    s: Mapping[str, ParagraphStyle],
    *,
    center_columns: set[int] | None = None,
) -> LongTable:
    center_columns = center_columns or set()
    data: list[list[Any]] = [[para(value, s["Head"]) for value in headers]]
    for row in rows:
        cells = []
        for index, value in enumerate(row):
            cells.append(value if isinstance(value, Table) else para(value, s["Center"] if index in center_columns else s["Cell"]))
        data.append(cells)
    result = LongTable(
        data,
        colWidths=[value * mm for value in widths_mm],
        repeatRows=1,
        hAlign="LEFT",
    )
    commands = [
        ("BACKGROUND", (0, 0), (-1, 0), NAVY),
        ("GRID", (0, 0), (-1, -1), 0.35, LINE_C),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 2.5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 2.5),
        ("TOPPADDING", (0, 0), (-1, -1), 2.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
    ]
    commands.extend(
        ("BACKGROUND", (0, index), (-1, index), ROW_ALT if index % 2 == 0 else colors.white)
        for index in range(1, len(data))
    )
    result.setStyle(TableStyle(commands))
    return result


def on_page(canvas, doc) -> None:
    canvas.saveState()
    canvas.setStrokeColor(LINE_C)
    canvas.setLineWidth(0.5)
    canvas.line(12 * mm, PAGE_H - 11 * mm, PAGE_W - 12 * mm, PAGE_H - 11 * mm)
    canvas.setFont(FONT_BOLD, 7.2)
    canvas.setFillColor(NAVY)
    canvas.drawString(12 * mm, PAGE_H - 8.5 * mm, "Stage2 OTel Demo Matrix-006 - 实验表格数据")
    canvas.setFont(FONT_REGULAR, 6.8)
    canvas.setFillColor(MUTED)
    canvas.drawRightString(PAGE_W - 12 * mm, PAGE_H - 8.5 * mm, "56 Trials / sealed evidence")
    canvas.line(12 * mm, 10 * mm, PAGE_W - 12 * mm, 10 * mm)
    canvas.drawString(12 * mm, 6.7 * mm, "matrix-otel-20260901-006")
    canvas.drawRightString(PAGE_W - 12 * mm, 6.7 * mm, f"第 {doc.page} 页")
    canvas.restoreState()


def yn(value: Any) -> str:
    return "Y" if value is True else "N"


def compact_json(value: Any, limit: int = 180) -> str:
    return short(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")), limit)


def build_tables(data: Mapping[str, Any], audit: Mapping[str, Any], s: Mapping[str, ParagraphStyle]):
    report = data["report"]
    trials = data["trials"]
    campaigns = data["campaigns"]
    summary = audit["summary"]
    story = [para("Stage2 OTel Demo Matrix-006 实验表格数据", s["Title"])]

    host = "-"
    d0_campaigns = data["preflight"].get("d0", {}).get("campaigns", [])
    if d0_campaigns:
        host = d0_campaigns[0].get("host", {}).get("declared_host_id", "-")
    metadata_rows = [
        ["Matrix ID", data["matrix_id"]],
        ["System", report.get("system", "otel-demo")],
        ["Execution host", host],
        ["Namespace / component", "otel-demo / cart"],
        ["Prompt", report.get("prompt", "")],
        ["Models", ", ".join(report.get("models", []))],
        ["Harnesses", ", ".join(HARNESS_LABELS.get(x, x) for x in report.get("harnesses", []))],
        ["Cases", ", ".join(report.get("cases", []))],
        ["Source matrices", ", ".join(audit.get("source_matrices", []))],
        ["Completed / expected", f"{summary['completed_trials']} / {summary['expected_trials']}"],
        ["Manifest verification", f"{audit['integrity']['verified_count']} / {audit['integrity']['expected_count']}"],
        ["Report generated at", report.get("generated_at", "-")],
    ]
    story.extend(
        [
            para("表 1  实验元数据", s["Heading"]),
            table(["Field", "Value"], metadata_rows, [46, 225], s),
            Spacer(1, 3 * mm),
        ]
    )

    overall_rows = [
        ["Completed trials", summary["completed_trials"], "Trial 已生成终态"],
        ["Platform valid", summary["platform_valid"], "满足 Harness/扰动/清理平台条件"],
        ["Platform invalid", summary["platform_invalid"], "CASE_INVALID"],
        ["Diagnostic only", summary["diagnostic_only"], "不进入正式评分"],
        ["PASS", summary["verdict_counts"].get("PASS", 0), "正式 PASS"],
        ["FAIL", summary["verdict_counts"].get("FAIL", 0), "正式 FAIL"],
        ["INCONCLUSIVE", summary["verdict_counts"].get("INCONCLUSIVE", 0), "有效但未正式判定"],
        ["CASE_INVALID", summary["verdict_counts"].get("CASE_INVALID", 0), "平台/Harness/扰动无效"],
        ["Main fault active", summary["fault_active"], "主故障曾真实激活"],
        ["Fault effect verified", summary["effect_verified"], "独立 Oracle 验证效果"],
        ["Agent recovery verified", summary["agent_recovery_verified"], "可归因于 Agent 的恢复"],
        ["Controller cleanup verified", summary["controller_cleanup_verified"], "Controller/Harness 清理"],
        ["Business recovery verified", summary["business_recovery_verified"], "业务恢复窗口通过"],
    ]
    story.extend(
        [
            para("表 2  总体统计", s["Heading"]),
            table(["Metric", "Value", "Definition"], overall_rows, [72, 28, 171], s, center_columns={1}),
            PageBreak(),
        ]
    )

    campaign_rows = []
    for index, campaign in enumerate(sorted(campaigns, key=lambda c: c["started_at"] or ""), start=1):
        evaluation = campaign.get("evaluation") or {}
        campaign_rows.append(
            [
                index,
                campaign["source_matrix_id"],
                HARNESS_LABELS[campaign["harness"]],
                campaign["model"],
                campaign["campaign_id"],
                campaign.get("platform_status", "-"),
                evaluation.get("valid_trial_count", "-"),
                fmt_utc(campaign.get("started_at")),
                fmt_utc(campaign.get("finished_at")),
                fmt_duration(campaign.get("duration_seconds")),
                yn(campaign.get("formally_scored")),
            ]
        )
    story.extend(
        [
            para("表 3  Campaign 执行记录", s["Heading"]),
            table(
                ["No.", "Source", "Harness", "Model", "Campaign ID", "Status", "Valid", "Started UTC", "Finished UTC", "Duration", "Scored"],
                campaign_rows,
                [8, 22, 27, 29, 40, 18, 12, 33, 33, 18, 14],
                s,
                center_columns={0, 5, 6, 9, 10},
            ),
            Spacer(1, 4 * mm),
        ]
    )

    pair_rows = []
    index = 0
    for harness in HARNESS_ORDER:
        for model in MODEL_ORDER:
            index += 1
            rows = [x for x in trials if x["harness"] == harness and x["model"] == model]
            stats = pair_summary(rows)
            verdicts = stats["verdicts"]
            campaign = next(c for c in campaigns if c["harness"] == harness and c["model"] == model)
            pair_rows.append(
                [
                    index,
                    HARNESS_LABELS[harness],
                    model,
                    campaign["source_matrix_id"],
                    campaign["campaign_id"],
                    7,
                    stats["valid"],
                    stats["diagnostic"],
                    verdicts.get("PASS", 0),
                    verdicts.get("FAIL", 0),
                    verdicts.get("INCONCLUSIVE", 0),
                    verdicts.get("CASE_INVALID", 0),
                    stats["fault_active"],
                    stats["effect"],
                    stats["agent_recovery"],
                    stats["controller"],
                    stats["business"],
                    stats["validation_errors"],
                    stats["missing_disturbance"],
                    fmt_duration(stats["avg_duration"]),
                ]
            )
    story.extend(
        [
            para("表 4  Harness × Model 汇总", s["Heading"]),
            table(
                ["No.", "Harness", "Model", "Source", "Campaign", "N", "Valid", "Diag", "P", "F", "I", "X", "Fault", "Effect", "Agent Rec", "Ctrl", "Biz", "Schema Err", "Dist Miss", "Avg Time"],
                pair_rows,
                [7, 22, 25, 17, 35, 7, 9, 9, 7, 7, 7, 7, 9, 9, 11, 9, 9, 12, 12, 15],
                s,
                center_columns=set(range(5, 20)),
            ),
            PageBreak(),
        ]
    )

    case_rows = []
    for case in CASE_ORDER:
        rows = [x for x in trials if x["kind"] == case]
        counts = Counter(x["agent_verdict"] for x in rows)
        case_rows.append(
            [
                case,
                CASE_TITLES[case],
                len(rows),
                sum(x["platform_valid"] for x in rows),
                sum(x["diagnostic_only"] for x in rows),
                counts.get("PASS", 0),
                counts.get("FAIL", 0),
                counts.get("INCONCLUSIVE", 0),
                counts.get("CASE_INVALID", 0),
                sum(x["recovery"].get("main_fault_ever_active") is True for x in rows),
                sum(x["recovery"].get("fault_effect_verified") is True for x in rows),
                sum(x["recovery"].get("agent_recovery_verified") is True for x in rows),
                sum(x["recovery"].get("controller_cleanup_verified") is True for x in rows),
                sum(x["expected_disturbance_missing"] for x in rows),
            ]
        )
    story.extend(
        [
            para("表 5  Case 汇总", s["Heading"]),
            table(
                ["Case", "Title", "N", "Valid", "Diag", "P", "F", "I", "X", "Fault", "Effect", "Agent Rec", "Ctrl", "Dist Miss"],
                case_rows,
                [10, 70, 10, 13, 13, 10, 10, 10, 10, 15, 15, 18, 15, 18],
                s,
                center_columns=set(range(2, 14)),
            ),
            PageBreak(),
        ]
    )

    result_rows = []
    evidence_rows = []
    oracle_rows = []
    lifecycle_rows = []
    disturbance_rows = []
    for index, trial in enumerate(trials, start=1):
        recovery = trial["recovery"]
        disturbances = trial.get("disturbances") or []
        disturbance = disturbances[0] if len(disturbances) == 1 else {}
        result_rows.append(
            [
                index,
                HARNESS_LABELS[trial["harness"]],
                trial["model"],
                trial["kind"],
                trial["agent_verdict"],
                yn(trial["platform_valid"]),
                yn(trial["diagnostic_only"]),
                trial["harness_status"],
                yn(recovery.get("main_fault_ever_active")),
                yn(recovery.get("fault_effect_verified")),
                yn(recovery.get("agent_recovery_verified")),
                yn(recovery.get("controller_cleanup_verified")),
                yn(recovery.get("business_recovery_verified")),
                yn(trial["disturbance_expected"]),
                yn(bool(disturbances and disturbance.get("applied"))),
                yn(bool(disturbances and disturbance.get("rolled_back"))),
                fmt_duration(trial["duration_seconds"]),
                trial["trial_id"],
            ]
        )
        target = trial.get("runtime_target") or {}
        issue = trial["validation_error"] or ("expected disturbance missing" if trial["expected_disturbance_missing"] else "-")
        evidence_rows.append(
            [
                index,
                trial["trial_id"],
                trial["campaign_id"],
                trial["source_matrix_id"],
                target.get("name", "-"),
                target.get("uid", "-"),
                short(issue, 140),
                trial["artifact_count"],
            ]
        )
        effect = recovery.get("fault_effect_evidence") or {}
        oracle_rows.append(
            [
                index,
                HARNESS_LABELS[trial["harness"]],
                trial["model"],
                trial["kind"],
                number(effect.get("baseline_cart_avg_response_ms")),
                number(effect.get("fault_window_cart_avg_response_ms")),
                number(effect.get("latency_delta_ms")),
                effect.get("cart_request_delta", "-"),
                number(effect.get("fault_window_success_rate"), digits=4),
                yn(effect.get("verified")),
                yn(recovery.get("main_fault_ever_active")),
                yn(recovery.get("main_fault_target_verified")),
                yn(recovery.get("business_recovery_verified")),
                number(effect.get("timeout_wait_seconds")),
            ]
        )
        lifecycle_rows.append(
            [
                index,
                HARNESS_LABELS[trial["harness"]],
                trial["model"],
                trial["kind"],
                trial["harness_status"],
                ", ".join(trial["phases"]) or "-",
                ", ".join(trial["event_kinds"]) or "-",
                short(trial["validation_error"] or "-", 160),
            ]
        )
        if trial["kind"] in {"D1", "D2", "D3", "D4"}:
            plan = disturbance.get("plan") or {}
            disturbance_rows.append(
                [
                    index,
                    HARNESS_LABELS[trial["harness"]],
                    trial["model"],
                    trial["kind"],
                    plan.get("phase", "-"),
                    plan.get("type", "-"),
                    plan.get("backend", "-"),
                    yn(disturbance.get("applied")),
                    yn(disturbance.get("rolled_back")),
                    plan.get("disturbance_id", "-"),
                    compact_json(disturbance.get("application_evidence") or {}, 220),
                    compact_json(disturbance.get("rollback_evidence") or {}, 180),
                ]
            )

    story.extend(
        [
            para("表 6  56-Trial 判定与闭环状态", s["Heading"]),
            table(
                ["No.", "Harness", "Model", "Case", "Verdict", "V", "D", "H Status", "Fault", "Effect", "Agent Rec", "Ctrl", "Biz", "Dist Exp", "Dist App", "Rollback", "Time", "Trial ID"],
                result_rows,
                [7, 21, 24, 8, 18, 7, 7, 14, 8, 8, 10, 8, 8, 10, 10, 10, 14, 52],
                s,
                center_columns=set(range(3, 17)),
            ),
            PageBreak(),
            para("表 7  Trial、Campaign、Target 与验证问题", s["Heading"]),
            table(
                ["No.", "Trial ID", "Campaign ID", "Source", "Target Pod", "Target UID", "Validation / Disturbance Issue", "Artifacts"],
                evidence_rows,
                [8, 55, 38, 19, 31, 52, 59, 11],
                s,
                center_columns={0, 7},
            ),
            PageBreak(),
            para("表 8  56-Trial 独立 Oracle 指标", s["Heading"]),
            table(
                ["No.", "Harness", "Model", "Case", "Baseline ms", "Fault-window ms", "Delta ms", "Cart req delta", "Success rate", "Verified", "Active", "Target", "Biz rec", "Timeout wait s"],
                oracle_rows,
                [8, 23, 26, 9, 18, 22, 18, 19, 18, 14, 12, 12, 13, 20],
                s,
                center_columns=set(range(3, 14)),
            ),
            PageBreak(),
            para("表 9  D1-D4 扰动与回滚记录", s["Heading"]),
            table(
                ["Trial No.", "Harness", "Model", "Case", "Trigger phase", "Type", "Backend", "Applied", "Rolled back", "Disturbance ID", "Application evidence", "Rollback evidence"],
                disturbance_rows,
                [12, 22, 25, 8, 22, 24, 24, 12, 14, 37, 48, 42],
                s,
                center_columns={0, 3, 7, 8},
            ),
            PageBreak(),
            para("表 10  56-Trial 生命周期与 Harness 输出状态", s["Heading"]),
            table(
                ["No.", "Harness", "Model", "Case", "Harness status", "Lifecycle phases", "Lifecycle event kinds", "Validation error"],
                lifecycle_rows,
                [8, 23, 26, 9, 20, 48, 76, 63],
                s,
                center_columns={0, 3, 4},
            ),
            PageBreak(),
        ]
    )

    definitions = [
        ["V", "platform_valid"],
        ["D", "diagnostic_only"],
        ["Fault", "main_fault_ever_active"],
        ["Effect", "fault_effect_verified"],
        ["Agent Rec", "agent_recovery_verified"],
        ["Ctrl", "controller_cleanup_verified"],
        ["Biz", "business_recovery_verified"],
        ["Dist Exp", "该用例要求 D1-D4 运行时扰动"],
        ["Dist App", "扰动记录存在且 applied=true"],
        ["Rollback", "扰动记录 rolled_back=true"],
        ["P/F/I/X", "PASS / FAIL / INCONCLUSIVE / CASE_INVALID"],
        ["时间", "Campaign 与 Trial 时间均为 UTC；duration 为事件差值"],
    ]
    story.extend(
        [
            para("表 11  字段定义", s["Heading"]),
            table(["Field", "Definition"], definitions, [54, 217], s),
        ]
    )
    return story


def number(value: Any, digits: int = 2) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "-"
    return f"{value:.{digits}f}"


def generate(data: Mapping[str, Any], audit: Mapping[str, Any], output: Path) -> None:
    register_fonts()
    s = styles()
    output.parent.mkdir(parents=True, exist_ok=True)
    frame = Frame(
        12 * mm,
        13 * mm,
        PAGE_W - 24 * mm,
        PAGE_H - 26 * mm,
        leftPadding=0,
        rightPadding=0,
        topPadding=0,
        bottomPadding=0,
        id="table-frame",
    )
    doc = BaseDocTemplate(
        str(output),
        pagesize=(PAGE_W, PAGE_H),
        leftMargin=12 * mm,
        rightMargin=12 * mm,
        topMargin=13 * mm,
        bottomMargin=13 * mm,
        title="Stage2 OTel Demo Matrix-006 实验表格数据",
        author="Resilience Benchmark Stage2",
        subject="table-only sealed experiment data",
    )
    doc.addPageTemplates([PageTemplate(id="tables", frames=[frame], onPage=on_page)])
    doc.build(build_tables(data, audit, s))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--matrix-id", default="matrix-otel-20260901-006")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = args.artifact_root.resolve()
    data = load_dataset(root, args.matrix_id)
    audit = MatrixEvidenceStore(root).overview(args.matrix_id)
    generate(data, audit, args.output.resolve())
    print(json.dumps({"output": str(args.output.resolve()), "trials": len(data["trials"]), "tables": 11}, ensure_ascii=False))


if __name__ == "__main__":
    main()
