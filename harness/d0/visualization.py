"""Dependency-free D0 HTML, SVG, Markdown, CSV and JSON visualization output."""

from __future__ import annotations

import csv
import html
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .common import read_jsonl, redact_sensitive_text, write_json, write_summary_csv
from .behavior import collect_agent_tool_records


COLORS = {
    "bladeai": "#2563eb",
    "codex": "#0891b2",
    "claude-code": "#d97706",
    "deepseek-harness": "#7c3aed",
}


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _svg_cpu(
    agent: str, rows: list[dict[str, Any]], result: dict[str, Any]
) -> str:
    points: list[tuple[datetime, float]] = []
    for row in rows:
        for pod in row.get("pods", []):
            value = pod.get("cpu_millicores")
            if value is not None and row.get("ts"):
                points.append((_dt(str(row["ts"])), float(value)))
    width, height = 1040, 300
    left, top, bottom = 64, 28, 42
    if not points:
        return f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}"><text x="30" y="60">No CPU samples</text></svg>'
    start, end = points[0][0], points[-1][0]
    span = max(1.0, (end - start).total_seconds())
    ceiling = max(100.0, max(value for _, value in points) * 1.1)
    coords = []
    for stamp, value in points:
        x = left + ((stamp - start).total_seconds() / span) * (width - left - 24)
        y = top + (1 - value / ceiling) * (height - top - bottom)
        coords.append(f"{x:.1f},{y:.1f}")
    color = COLORS.get(agent, "#334155")
    markers: list[tuple[datetime, str, str]] = []
    behavior = result.get("agent_behavior") or {}
    raw_markers = (
        (result.get("effect_confirmed_at"), "T0 effect", "#b91c1c"),
        (
            (_dt(str(result["effect_confirmed_at"])) + timedelta(seconds=300)).isoformat()
            if result.get("effect_confirmed_at")
            else None,
            "T0+300",
            "#d97706",
        ),
        (behavior.get("agent_recovery_requested_at"), "Agent recovery", "#15803d"),
        ((result.get("fallback") or {}).get("ts"), "Fallback", "#7c3aed"),
        (result.get("recovery_observed_at"), "Oracle recovery", "#0891b2"),
    )
    for stamp, label, marker_color in raw_markers:
        if stamp:
            markers.append((_dt(str(stamp)), label, marker_color))
    marker_svg = []
    for stamp, label, marker_color in markers:
        seconds = (stamp - start).total_seconds()
        if not 0 <= seconds <= span:
            continue
        x = left + (seconds / span) * (width - left - 24)
        marker_svg.append(
            f'<line x1="{x:.1f}" y1="{top}" x2="{x:.1f}" y2="{height-bottom}" stroke="{marker_color}" stroke-width="1.5" stroke-dasharray="5,4"/>'
            f'<text x="{x+4:.1f}" y="{top+14}" fill="{marker_color}" font-family="sans-serif" font-size="10" transform="rotate(90 {x+4:.1f} {top+14})">{html.escape(label)}</text>'
        )
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">
<rect width="100%" height="100%" fill="#ffffff"/><line x1="{left}" y1="{top}" x2="{left}" y2="{height-bottom}" stroke="#94a3b8"/><line x1="{left}" y1="{height-bottom}" x2="{width-24}" y2="{height-bottom}" stroke="#94a3b8"/>
<text x="{left}" y="18" fill="#334155" font-family="sans-serif" font-size="13">CPU millicores · {html.escape(agent)}</text>
<text x="8" y="{top+8}" fill="#64748b" font-family="sans-serif" font-size="11">{ceiling:.0f}m</text><text x="28" y="{height-bottom+4}" fill="#64748b" font-family="sans-serif" font-size="11">0m</text>
<polyline points="{' '.join(coords)}" fill="none" stroke="{color}" stroke-width="3"/>{''.join(marker_svg)}
<text x="{left}" y="{height-12}" fill="#64748b" font-family="sans-serif" font-size="11">0s</text><text x="{width-90}" y="{height-12}" fill="#64748b" font-family="sans-serif" font-size="11">{span:.0f}s</text>
</svg>'''


def _svg_timeline(agent: str, rows: list[dict[str, Any]]) -> str:
    width = 1200
    lanes = ("agent", "harness", "controller", "oracle")
    height = 85 + len(lanes) * 80
    timestamps = [_dt(str(row["ts"])) for row in rows if row.get("ts")]
    if not timestamps:
        return f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="180"><text x="30" y="60">No timeline events</text></svg>'
    start, end = min(timestamps), max(timestamps)
    span = max(1.0, (end - start).total_seconds())
    lane_y = {lane: 75 + index * 80 for index, lane in enumerate(lanes)}
    elements = [f'<rect width="100%" height="100%" fill="#ffffff"/>', f'<text x="20" y="28" font-family="sans-serif" font-size="15" fill="#0f172a">{html.escape(agent)} · Agent / Harness / Controller / Oracle timeline</text>']
    for lane in lanes:
        y = lane_y[lane]
        elements.append(f'<text x="18" y="{y+5}" font-family="sans-serif" font-size="12" fill="#475569">{lane}</text>')
        elements.append(f'<line x1="105" y1="{y}" x2="1170" y2="{y}" stroke="#cbd5e1"/>')
    color = COLORS.get(agent, "#334155")
    for row in rows:
        if not row.get("ts"):
            continue
        actor = str(row.get("actor") or "agent")
        lane = actor if actor in lane_y else "agent"
        x = 105 + ((_dt(str(row["ts"])) - start).total_seconds() / span) * 1065
        y = lane_y[lane]
        label = str(row.get("kind") or row.get("event") or "event")[:28]
        relative = (_dt(str(row["ts"])) - start).total_seconds()
        elements.append(f'<circle cx="{x:.1f}" cy="{y}" r="5" fill="{color}"/><title>{html.escape(label)} · {html.escape(str(row.get("ts")))} · T+{relative:.1f}s</title>')
    elements.append(f'<text x="105" y="{height-18}" font-family="sans-serif" font-size="10" fill="#64748b">{html.escape(start.isoformat())} · T+0s</text><text x="900" y="{height-18}" font-family="sans-serif" font-size="10" fill="#64748b">{html.escape(end.isoformat())} · T+{span:.0f}s</text>')
    return f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">{"".join(elements)}</svg>'


def _svg_comparison(results: list[dict[str, Any]]) -> str:
    columns = (
        ("agent_target_discovered", "Target discovery"),
        ("agent_injection_requested", "Injection"),
        ("agent_effect_check_observed", "Effect verification"),
        ("duration_ok", "5-minute hold"),
        ("agent_recovery_requested", "Active recovery"),
        ("agent_recovery_check_observed", "Recovery verification"),
    )
    width = 1320
    row_h = 64
    height = 92 + row_h * len(results)
    cell_w = 155
    elements = [f'<rect width="100%" height="100%" fill="#ffffff"/>', '<text x="24" y="30" font-family="sans-serif" font-size="18" fill="#0f172a">D0 qualification stage comparison</text>', '<text x="24" y="70" font-family="sans-serif" font-size="12" fill="#475569">Agent</text>']
    for index, (_, label) in enumerate(columns):
        elements.append(f'<text x="{240 + index * cell_w}" y="70" font-family="sans-serif" font-size="12" fill="#475569">{html.escape(label)}</text>')
    elements.append('<text x="1175" y="70" font-family="sans-serif" font-size="12" fill="#475569">Verdict</text>')
    for row, result in enumerate(results):
        y = 92 + row * row_h
        agent = str(result.get("agent", ""))
        elements.append(f'<rect x="16" y="{y-20}" width="1288" height="52" fill="{"#f8fafc" if row % 2 == 0 else "#ffffff"}"/>')
        elements.append(f'<text x="24" y="{y+10}" font-family="sans-serif" font-size="14" font-weight="600" fill="{COLORS.get(agent, "#334155")}">{html.escape(agent)}</text>')
        values = dict(result)
        values.update(result.get("agent_behavior") or {})
        duration = result.get("fault_duration_seconds")
        values["duration_ok"] = isinstance(duration, int | float) and 270 <= float(duration) <= 330
        for index, (key, _) in enumerate(columns):
            raw = values.get(key)
            if raw is True:
                glyph, color = "PASS", "#15803d"
            elif raw is False:
                prior_injection = bool(values.get("agent_injection_requested"))
                if key not in {"agent_target_discovered", "agent_injection_requested"} and not prior_injection:
                    glyph, color = "NOT_REACHED", "#64748b"
                else:
                    glyph, color = "FAIL", "#b91c1c"
            else:
                glyph, color = "UNKNOWN", "#64748b"
            elements.append(f'<text x="{240 + index * cell_w}" y="{y+10}" font-family="sans-serif" font-size="13" font-weight="600" fill="{color}">{glyph}</text>')
        elements.append(f'<text x="1175" y="{y+10}" font-family="sans-serif" font-size="12" font-weight="600" fill="#0f172a">{html.escape(str(result.get("status", "")))}</text>')
    return f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">{"".join(elements)}</svg>'


def _audit_rows(agent: str, trial_dir: Path) -> list[dict[str, Any]]:
    rows = []
    for value in collect_agent_tool_records(trial_dir):
        rows.append(
            {
                "agent": agent,
                "ts": value.get("ts"),
                "actor": "agent",
                "action": value.get("tool"),
                "target": "",
                "started_at": value.get("ts"),
                "finished_at": "",
                "duration_ms": "",
                "returncode": "",
                "result_summary": "native tool event; inspect linked evidence",
                "evidence": value.get("payload_ref") or "all-events.jsonl",
            }
        )
    for index, value in enumerate(
        read_jsonl(trial_dir / "controller-commands.jsonl"), start=1
    ):
        argv = [str(item) for item in value.get("argv", [])]
        action = redact_sensitive_text(" ".join(argv))[:1000]
        target = ""
        for marker in ("pod", "pods", "chaosblades.chaosblade.io"):
            if marker in argv:
                position = argv.index(marker)
                if position + 1 < len(argv) and not argv[position + 1].startswith("-"):
                    target = argv[position + 1]
                break
        summary = redact_sensitive_text(
            str(value.get("stdout") or value.get("stderr") or "")
        ).replace("\n", " ")[:240]
        rows.append(
            {
                "agent": agent,
                "ts": value.get("ts"),
                "actor": "controller",
                "action": action,
                "target": target,
                "started_at": value.get("started_at") or value.get("ts"),
                "finished_at": value.get("finished_at") or value.get("ts"),
                "duration_ms": value.get("duration_ms", ""),
                "returncode": value.get("returncode", ""),
                "result_summary": summary,
                "evidence": f"controller-commands.jsonl#event-{index}",
            }
        )
    rows.sort(key=lambda value: (str(value.get("ts") or ""), value["actor"]))
    return rows


def _audit_html(agent: str, rows: list[dict[str, Any]]) -> str:
    table_rows = []
    for value in rows:
        evidence = str(value.get("evidence") or "")
        href = (
            f"../{html.escape(agent)}/{html.escape(evidence.split('#', 1)[0])}"
        )
        table_rows.append(
            "<tr>"
            + "".join(
                f"<td>{html.escape(str(value.get(key, '')))}</td>"
                for key in (
                    "ts",
                    "actor",
                    "action",
                    "target",
                    "started_at",
                    "finished_at",
                    "duration_ms",
                    "returncode",
                    "result_summary",
                )
            )
            + f'<td><a href="{href}">{html.escape(evidence)}</a></td></tr>'
        )
    return f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>{html.escape(agent)} command/tool audit</title><style>body{{font-family:Inter,Arial,sans-serif;margin:24px;color:#0f172a}}table{{border-collapse:collapse;width:100%;font-size:12px}}th,td{{border:1px solid #cbd5e1;padding:6px;vertical-align:top;word-break:break-word}}th{{background:#e2e8f0;position:sticky;top:0}}</style></head><body><h1>{html.escape(agent)} · Agent 工具与 Controller 命令审计</h1><p>按时间排列；每行均链接到原始证据文件。结构化 Kubernetes 原文不在新 Campaign 中落盘，只保留 Oracle 解析事实、摘要和 SHA-256。</p><table><thead><tr><th>Time</th><th>Actor</th><th>Command/tool</th><th>Target</th><th>Started</th><th>Finished</th><th>Duration ms</th><th>Return code</th><th>Result summary</th><th>Evidence</th></tr></thead><tbody>{''.join(table_rows)}</tbody></table></body></html>'''


def _trial_links(agent: str, trial_dir: Path) -> tuple[str, str]:
    candidates = (
        ("result.json", "result.json"),
        ("all-events.jsonl", "events"),
        (
            "stdout.txt"
            if (trial_dir / "stdout.txt").is_file()
            else "agent-responses.jsonl",
            "agent response",
        ),
        ("controller-commands.jsonl", "controller commands"),
        ("oracle-samples.jsonl", "oracle samples"),
        ("run-trace.json", "run trace"),
    )
    available = [
        (filename, label)
        for filename, label in candidates
        if (trial_dir / filename).is_file()
    ]
    html_links = " · ".join(
        f'<a href="../{html.escape(agent)}/{html.escape(filename)}">{html.escape(label)}</a>'
        for filename, label in available
    )
    markdown_links = " · ".join(
        f"[{label}](../{agent}/{filename})" for filename, label in available
    )
    return html_links, markdown_links


def generate_visualizations(campaign_dir: Path, campaign: dict[str, Any]) -> dict[str, str]:
    visual = campaign_dir / "visualization"
    visual.mkdir(parents=True, exist_ok=True)
    results = list(campaign.get("results", []))
    write_summary_csv(visual / "summary.csv", results)
    write_json(visual / "summary.json", {"campaign_id": campaign.get("campaign_id"), "results": results})
    (visual / "comparison.svg").write_text(
        _svg_comparison(results), encoding="utf-8"
    )
    sections = []
    markdown = [
        "# D0 多智能体故障注入资格检查报告",
        "",
        f"- Campaign: `{campaign.get('campaign_id', '')}`",
        f"- Status: `{campaign.get('status', '')}`",
        f"- Execution host: `{campaign.get('host', {}).get('declared_host_id', '')}` / `{campaign.get('host', {}).get('hostname', '')}`",
        f"- Started: `{campaign.get('started_at', '')}`",
        f"- Finished: `{campaign.get('finished_at', '')}`",
        f"- Verdict source: `{campaign.get('verdict_source', '')}`",
        "",
        "## 总览",
        "",
        "| Agent | Verdict | Baseline CPU | Max CPU | Effect seconds | CR lifetime seconds | Restart delta | Agent recovery | Fallback | Tool trace |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | --- | --- | --- |",
    ]
    rows_html = []
    all_audit_rows: list[dict[str, Any]] = []
    for result in results:
        agent = str(result["agent"])
        trial_dir = campaign_dir / agent
        samples = read_jsonl(trial_dir / "oracle-samples.jsonl")
        events = read_jsonl(trial_dir / "all-events.jsonl")
        if result.get("effect_confirmed_at"):
            events.append(
                {
                    "ts": result["effect_confirmed_at"],
                    "actor": "oracle",
                    "kind": "effect_observed",
                }
            )
        if result.get("recovery_observed_at"):
            events.append(
                {
                    "ts": result["recovery_observed_at"],
                    "actor": "oracle",
                    "kind": "recovery_observed",
                }
            )
        fallback = result.get("fallback") or {}
        if result.get("fallback_cleanup_used") and fallback.get("ts"):
            events.append(
                {
                    "ts": fallback["ts"],
                    "actor": "controller",
                    "kind": "fallback_cleanup",
                }
            )
        events.sort(key=lambda value: str(value.get("ts") or ""))
        cpu_name = f"{agent}-cpu.svg"
        timeline_name = f"{agent}-timeline.svg"
        (visual / cpu_name).write_text(
            _svg_cpu(agent, samples, result), encoding="utf-8"
        )
        (visual / timeline_name).write_text(_svg_timeline(agent, events), encoding="utf-8")
        audit_rows = _audit_rows(agent, trial_dir)
        all_audit_rows.extend(audit_rows)
        audit_name = f"{agent}-command-tool-audit.html"
        (visual / audit_name).write_text(
            _audit_html(agent, audit_rows), encoding="utf-8"
        )
        adapter = result.get("adapter") or {}
        html_evidence_links, markdown_evidence_links = _trial_links(
            agent, trial_dir
        )
        rows_html.append(
            "<tr>"
            + "".join(
                f"<td>{html.escape(str(result.get(key, '')))}</td>"
                for key in (
                    "agent", "status", "effect_observed", "fault_duration_seconds",
                    "agent_recovery_requested", "recovery_observed", "fallback_cleanup_used",
                )
            )
            + f"<td>{html.escape(str(result.get('total_duration_seconds', '')))}</td>"
            + f"<td>{html.escape(str(adapter.get('tool_calls', '')))}</td>"
            + f"<td>{html.escape(str(adapter.get('confirmations', '')))}</td>"
            + "</tr>"
        )
        markdown.append(
            "| "
            + " | ".join(
                [
                    agent,
                    str(result.get("status", "")),
                    str(result.get("baseline_cpu_millicores", "")),
                    str(result.get("maximum_cpu_millicores", "")),
                    str(result.get("effect_duration_seconds", "")),
                    str(result.get("fault_duration_seconds", "")),
                    str(result.get("restart_count_delta", "")),
                    str(bool(result.get("agent_recovery_requested"))),
                    str(bool(result.get("fallback_cleanup_used"))),
                    str(bool(result.get("tool_trace_complete"))),
                ]
            )
            + " |"
        )
        sections.append(
            f'<section><h2>{html.escape(agent)}</h2><p><strong>{html.escape(str(result.get("status", "")))}</strong> · baseline {html.escape(str(result.get("baseline_cpu_millicores", "")))}m · max {html.escape(str(result.get("maximum_cpu_millicores", "")))}m · effect {html.escape(str(result.get("effect_duration_seconds", "")))}s · CR lifetime {html.escape(str(result.get("fault_duration_seconds", "")))}s · restart Δ {html.escape(str(result.get("restart_count_delta", "")))} · tool trace {html.escape(str(result.get("tool_trace_complete", "")))}</p><p>{html_evidence_links} · <a href="{audit_name}">command/tool audit table</a></p><img src="{timeline_name}"/><img src="{cpu_name}"/></section>'
        )
    write_json(
        visual / "command-tool-audit.json",
        {"campaign_id": campaign.get("campaign_id"), "rows": all_audit_rows},
    )
    audit_fields = [
        "agent",
        "ts",
        "actor",
        "action",
        "target",
        "started_at",
        "finished_at",
        "duration_ms",
        "returncode",
        "result_summary",
        "evidence",
    ]
    with (visual / "command-tool-audit.csv").open(
        "w", encoding="utf-8", newline=""
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=audit_fields)
        writer.writeheader()
        writer.writerows(
            {key: value.get(key, "") for key in audit_fields}
            for value in all_audit_rows
        )
    markdown.extend(["", "## 逐轮证据索引", ""])
    for result in results:
        agent = str(result["agent"])
        markdown.extend(
            [
                f"### {agent}",
                "",
                f"- Verdict: `{result.get('status')}`",
                f"- Effect: `{result.get('effect_observed')}` from `{result.get('effect_confirmed_at')}` to `{result.get('effect_ended_at')}`",
                f"- Recovery observed: `{result.get('recovery_observed')}` at `{result.get('recovery_observed_at')}`",
                f"- Agent recovery requested: `{result.get('agent_recovery_requested')}`",
                f"- Controller fallback: `{result.get('fallback_cleanup_used')}`",
                f"- Tool trace complete: `{result.get('tool_trace_complete')}`",
                f"- Agent events: `{result.get('agent_event_count')}`; Controller commands: `{result.get('controller_command_count')}`; Oracle samples: `{result.get('oracle_samples')}`",
                f"- {markdown_evidence_links}",
                "",
            ]
        )
    markdown.extend(
        [
            "## 证据与边界",
            "",
            "- `FALLBACK_RECOVERED` 只证明 Controller 最终保护了环境，不是 Agent PASS。",
            "- `TIMEOUT_RECOVERED` 表示 Agent 设置了有界 timeout，故障到期后由独立 Oracle 验证恢复。",
            "- `RECOVERY_UNVERIFIED` 仅表示恢复证据不足，不等同于所有未显式 destroy 的情况。",
            "- DeepSeek Harness 的重跑导出了 Trial-local DSH session/tool trace；其 CPU 效果因容器重启提前结束，CR 随后因旧 container-id 不存在而卡在 finalizer，最终由 Controller 精确清理。",
            "- Campaign 结束后的 Deployment/Pod 替换发生在封存完成之后，见 `post-campaign-environment-observation.json`，不直接归因于某个 Agent。",
            "- 全部文件由 `manifest.sha256` 约束；派生结论使用 `sealed-raw-events-and-oracle-samples`。",
            "",
        ]
    )
    page = f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>D0 multi-agent qualification</title><style>body{{font-family:Inter,Arial,sans-serif;margin:28px;color:#0f172a}}table{{border-collapse:collapse;width:100%}}th,td{{border:1px solid #cbd5e1;padding:8px;text-align:left}}th{{background:#e2e8f0}}section{{margin-top:34px;border-top:2px solid #cbd5e1;padding-top:18px}}img{{max-width:100%;display:block;margin:14px 0;border:1px solid #e2e8f0}}</style></head><body><h1>D0 多智能体故障注入资格检查</h1><p>Campaign <code>{html.escape(str(campaign.get('campaign_id', '')))}</code> · execution host <code>{html.escape(str(campaign.get('host', {}).get('declared_host_id', '')))}</code></p><p><a href="command-tool-audit.csv">all command/tool audit CSV</a> · <a href="command-tool-audit.json">JSON</a></p><img src="comparison.svg"/><table><thead><tr><th>Agent</th><th>Status</th><th>Effect</th><th>Fault duration(s)</th><th>Agent recovery</th><th>Recovery observed</th><th>Fallback</th><th>Total seconds</th><th>Tool calls</th><th>Confirmations</th></tr></thead><tbody>{''.join(rows_html)}</tbody></table>{''.join(sections)}</body></html>'''
    (visual / "index.html").write_text(page, encoding="utf-8")
    (visual / "audit-report.md").write_text("\n".join(markdown) + "\n", encoding="utf-8")
    return {
        "html": "visualization/index.html",
        "markdown": "visualization/audit-report.md",
        "csv": "visualization/summary.csv",
        "json": "visualization/summary.json",
        "comparison": "visualization/comparison.svg",
        "command_tool_audit_csv": "visualization/command-tool-audit.csv",
        "command_tool_audit_json": "visualization/command-tool-audit.json",
    }
