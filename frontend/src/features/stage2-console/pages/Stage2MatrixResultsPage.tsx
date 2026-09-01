import { useEffect, useMemo, useState } from "react";
import {
  Alert,
  Button,
  Drawer,
  Empty,
  Input,
  Select,
  Space,
  Spin,
  Table,
  Tabs,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import {
  Activity,
  CheckCircle2,
  Clock3,
  ExternalLink,
  FileCheck2,
  RefreshCw,
  ShieldAlert,
} from "lucide-react";
import { getMatrix, getMatrixTrial, listMatrices, matrixArtifactUrl } from "../matrixApi";
import type {
  MatrixInspection,
  MatrixListItem,
  MatrixTrialDetail,
  MatrixTrialSummary,
  MatrixVerdict,
} from "../matrixTypes";
import "../stage2-matrix-results.css";

const caseOrder = ["C0", "P1", "P2", "D1", "D2", "D3", "D4"] as const;
const verdictOptions: MatrixVerdict[] = ["PASS", "FAIL", "INCONCLUSIVE", "CASE_INVALID"];
const harnessNames: Record<string, string> = {
  bladeai: "BladeAI",
  "claude-code": "Claude Code",
  codex: "Codex",
  "deepseek-harness": "DeepSeek Harness",
};

export default function Stage2MatrixResultsPage() {
  const [matrices, setMatrices] = useState<MatrixListItem[]>([]);
  const [matrixId, setMatrixId] = useState("");
  const [matrix, setMatrix] = useState<MatrixInspection | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [harnessFilter, setHarnessFilter] = useState<string>();
  const [modelFilter, setModelFilter] = useState<string>();
  const [verdictFilter, setVerdictFilter] = useState<string>();
  const [search, setSearch] = useState("");
  const [trial, setTrial] = useState<MatrixTrialDetail | null>(null);
  const [trialLoading, setTrialLoading] = useState(false);

  async function refresh(preferredId?: string) {
    setLoading(true);
    setError("");
    try {
      const rows = await listMatrices();
      setMatrices(rows);
      const nextId = preferredId || matrixId || rows[0]?.matrix_id || "";
      setMatrixId(nextId);
      setMatrix(nextId ? await getMatrix(nextId) : null);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "无法读取 Stage2 矩阵证据");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void refresh();
  }, []);

  const filteredTrials = useMemo(() => {
    const term = search.trim().toLowerCase();
    return (matrix?.trials ?? []).filter((item) => {
      if (harnessFilter && item.harness !== harnessFilter) return false;
      if (modelFilter && item.model !== modelFilter) return false;
      if (verdictFilter && item.agent_verdict !== verdictFilter) return false;
      if (!term) return true;
      return [item.trial_id, item.campaign_id, item.target.name, item.target.uid]
        .filter(Boolean)
        .some((value) => String(value).toLowerCase().includes(term));
    });
  }, [harnessFilter, matrix, modelFilter, search, verdictFilter]);

  async function selectTrial(item: MatrixTrialSummary) {
    if (!matrix) return;
    setTrialLoading(true);
    setTrial(null);
    try {
      setTrial(await getMatrixTrial(matrix.matrix_id, item.trial_id));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "无法读取 Trial 证据");
    } finally {
      setTrialLoading(false);
    }
  }

  const columns: ColumnsType<MatrixTrialSummary> = [
    {
      title: "Harness / 模型",
      key: "unit",
      width: 230,
      render: (_, item) => (
        <div className="matrix-unit-cell">
          <strong>{harnessNames[item.harness] ?? item.harness}</strong>
          <span>{item.model}</span>
        </div>
      ),
    },
    { title: "用例", dataIndex: "case_id", width: 74 },
    {
      title: "Agent 判定",
      dataIndex: "agent_verdict",
      width: 132,
      render: (value: MatrixVerdict) => <VerdictTag verdict={value} />,
    },
    {
      title: "证据资格",
      key: "platform",
      width: 118,
      render: (_, item) => item.platform_valid
        ? <Tag color="green">VALID</Tag>
        : <Tag color="red">INVALID</Tag>,
    },
    {
      title: "注入 / 生效",
      key: "effect",
      width: 128,
      render: (_, item) => <BooleanPair left={item.fault_active} right={item.effect_verified} />,
    },
    {
      title: "Agent恢复 / 兜底",
      key: "recovery",
      width: 142,
      render: (_, item) => <BooleanPair left={item.agent_recovery_verified} right={item.controller_cleanup_verified} />,
    },
    {
      title: "耗时",
      dataIndex: "duration_seconds",
      width: 90,
      render: (value: number | null) => value == null ? "—" : duration(value),
    },
    {
      title: "证据",
      key: "action",
      width: 88,
      fixed: "right",
      render: (_, item) => <Button size="small" onClick={() => void selectTrial(item)}>检查</Button>,
    },
  ];

  if (loading && !matrix) {
    return <div className="matrix-loading"><Spin size="large" /><span>正在重算并加载密封证据…</span></div>;
  }

  return (
    <main className="stage2-matrix-page">
      <header className="matrix-hero">
        <div>
          <div className="matrix-eyebrow"><Activity size={15} /> 真实集群 · 密封证据 · 独立 Oracle</div>
          <Typography.Title level={2}>Stage2 实验矩阵审计</Typography.Title>
          <Typography.Paragraph>
            分开检查执行覆盖、故障生效、Agent 闭环能力和 Harness/平台有效性。56/56 表示执行完成，不表示 56 次通过。
          </Typography.Paragraph>
        </div>
        <Space wrap>
          <Select
            aria-label="选择实验矩阵"
            value={matrixId || undefined}
            style={{ width: 280 }}
            options={matrices.map((item) => ({
              label: `${item.matrix_id} · ${item.completed_trial_count}/${item.expected_trial_count}`,
              value: item.matrix_id,
            }))}
            onChange={(value) => void refresh(value)}
          />
          <Button icon={<RefreshCw size={15} />} onClick={() => void refresh(matrixId)}>刷新证据</Button>
          {matrix && (
            <Button
              icon={<ExternalLink size={15} />}
              href={matrixArtifactUrl(matrix.matrix_id, "report.md")}
              target="_blank"
            >
              原始报告
            </Button>
          )}
        </Space>
      </header>

      {error && <Alert className="matrix-alert" type="error" showIcon message={error} closable onClose={() => setError("")} />}
      {!matrix ? <Empty description="未发现已完成的 Stage2 矩阵" /> : (
        <>
          <OutcomeAlert matrix={matrix} />
          <MetricStrip matrix={matrix} />

          <section className="matrix-panel matrix-provenance">
            <div>
              <span>汇总矩阵</span>
              <strong>{matrix.matrix_id}</strong>
            </div>
            <div>
              <span>实际执行源矩阵</span>
              <strong>{matrix.source_matrices.join(" · ") || "未定位"}</strong>
            </div>
            <div>
              <span>证据完整性</span>
              <strong className={matrix.integrity.all_valid ? "tone-good" : "tone-bad"}>
                {matrix.integrity.verified_count}/{matrix.integrity.expected_count} manifests
              </strong>
            </div>
          </section>

          <section className="matrix-panel">
            <PanelHeading
              title="Harness × Model × Case 结果矩阵"
              description="点击任意格子查看 Agent 原始响应、Controller 动作、独立 Oracle 和恢复证据。"
            />
            <ResultHeatmap matrix={matrix} onSelect={selectTrial} />
          </section>

          <section className="matrix-panel">
            <PanelHeading
              title="评分资格与覆盖"
              description="只有平台有效、非诊断且判定为 PASS/FAIL 的 Trial 才进入分数；其余保持 N/A。"
            />
            <ScoreTable matrix={matrix} />
          </section>

          <section className="matrix-panel">
            <div className="matrix-table-head">
              <PanelHeading title="56 个 Trial 明细" description={`当前显示 ${filteredTrials.length} 条。`} />
              <Space wrap>
                <Select
                  allowClear
                  placeholder="Harness"
                  style={{ width: 170 }}
                  value={harnessFilter}
                  options={[...new Set(matrix.trials.map((item) => item.harness))].map((value) => ({ label: harnessNames[value] ?? value, value }))}
                  onChange={setHarnessFilter}
                />
                <Select
                  allowClear
                  placeholder="模型"
                  style={{ width: 180 }}
                  value={modelFilter}
                  options={[...new Set(matrix.trials.map((item) => item.model))].map((value) => ({ label: value, value }))}
                  onChange={setModelFilter}
                />
                <Select
                  allowClear
                  placeholder="判定"
                  style={{ width: 160 }}
                  value={verdictFilter}
                  options={verdictOptions.map((value) => ({ label: value, value }))}
                  onChange={setVerdictFilter}
                />
                <Input.Search placeholder="Trial / Pod / UID" allowClear style={{ width: 220 }} onSearch={setSearch} onChange={(event) => setSearch(event.target.value)} />
              </Space>
            </div>
            <Table
              rowKey="trial_id"
              size="small"
              columns={columns}
              dataSource={filteredTrials}
              pagination={{ pageSize: 14, showSizeChanger: false }}
              scroll={{ x: 1150 }}
            />
          </section>
        </>
      )}

      <Drawer
        title={trial ? `${harnessNames[trial.summary.harness] ?? trial.summary.harness} · ${trial.summary.model} · ${trial.summary.case_id}` : "Trial 证据"}
        width={960}
        open={trialLoading || Boolean(trial)}
        onClose={() => setTrial(null)}
        destroyOnHidden
      >
        {trialLoading ? <div className="matrix-loading"><Spin /><span>正在读取 Trial 全量证据…</span></div> : trial && <TrialEvidence detail={trial} />}
      </Drawer>
    </main>
  );
}

function OutcomeAlert({ matrix }: { matrix: MatrixInspection }) {
  const verdicts = matrix.summary.verdict_counts;
  return (
    <Alert
      className="matrix-alert"
      type="warning"
      showIcon
      icon={<ShieldAlert size={20} />}
      message="实验执行完整，但目前没有可计分的 PASS/FAIL"
      description={`实际结果为 INCONCLUSIVE ${verdicts.INCONCLUSIVE ?? 0}、CASE_INVALID ${verdicts.CASE_INVALID ?? 0}。独立 Oracle 证明 ${matrix.summary.effect_verified} 次故障生效；Agent 独立恢复验证为 ${matrix.summary.agent_recovery_verified}，Controller 兜底为 ${matrix.summary.controller_cleanup_verified}。`}
    />
  );
}

function MetricStrip({ matrix }: { matrix: MatrixInspection }) {
  const items = [
    ["执行覆盖", `${matrix.summary.completed_trials}/${matrix.summary.expected_trials}`, "56 个均已结束"],
    ["证据完整", `${matrix.integrity.verified_count}/${matrix.integrity.expected_count}`, matrix.integrity.all_valid ? "全部摘要一致" : "存在校验失败"],
    ["平台有效", `${matrix.summary.platform_valid}`, `${matrix.summary.platform_invalid} 个 CASE_INVALID`],
    ["故障生效", `${matrix.summary.effect_verified}`, `${matrix.summary.fault_active} 次真实激活`],
    ["Agent 恢复", `${matrix.summary.agent_recovery_verified}`, "独立证明恢复"],
    ["Controller 兜底", `${matrix.summary.controller_cleanup_verified}`, `${matrix.summary.business_recovery_verified} 次业务恢复`],
  ];
  return (
    <section className="matrix-metrics">
      {items.map(([label, value, note]) => (
        <div className="matrix-metric" key={label}>
          <span>{label}</span><strong>{value}</strong><small>{note}</small>
        </div>
      ))}
    </section>
  );
}

function PanelHeading({ title, description }: { title: string; description: string }) {
  return <div className="matrix-panel-heading"><Typography.Title level={4}>{title}</Typography.Title><p>{description}</p></div>;
}

function ResultHeatmap({ matrix, onSelect }: { matrix: MatrixInspection; onSelect: (trial: MatrixTrialSummary) => void }) {
  return (
    <div className="matrix-heatmap-wrap">
      <div className="matrix-heatmap">
        <div className="matrix-heatmap-corner">评测单元</div>
        {caseOrder.map((item) => <div className="matrix-case-head" key={item}>{item}</div>)}
        {matrix.report.score_table.map((row) => (
          <div className="matrix-heatmap-row" key={`${row.harness}-${row.model}`}>
            <div className="matrix-heatmap-label"><strong>{harnessNames[row.harness] ?? row.harness}</strong><span>{row.model}</span></div>
            {caseOrder.map((caseId) => {
              const trial = matrix.trials.find((item) => item.harness === row.harness && item.model === row.model && item.case_id === caseId);
              if (!trial) return <div className="matrix-cell matrix-cell-missing" key={caseId}>—</div>;
              return (
                <Tooltip
                  key={caseId}
                  title={`${trial.agent_verdict} · ${trial.platform_valid ? "平台有效" : "平台无效"} · 故障${trial.effect_verified ? "已验证" : "未验证"}`}
                >
                  <button
                    type="button"
                    className={`matrix-cell matrix-cell-${trial.agent_verdict.toLowerCase().replace("_", "-")} ${trial.platform_valid ? "" : "matrix-cell-invalid"}`}
                    aria-label={`${row.harness} ${row.model} ${caseId} ${trial.agent_verdict}`}
                    onClick={() => void onSelect(trial)}
                  >
                    <strong>{verdictGlyph(trial.agent_verdict)}</strong>
                    <span className={trial.effect_verified ? "signal-on" : "signal-off"} />
                  </button>
                </Tooltip>
              );
            })}
          </div>
        ))}
      </div>
      <div className="matrix-legend">
        <span><i className="legend-swatch verdict-inconclusive" />INCONCLUSIVE</span>
        <span><i className="legend-swatch verdict-invalid" />CASE_INVALID</span>
        <span><i className="legend-dot signal-on" />独立 Oracle 已验证生效</span>
        <span><i className="legend-dot signal-off" />效果未验证</span>
      </div>
    </div>
  );
}

function ScoreTable({ matrix }: { matrix: MatrixInspection }) {
  return (
    <div className="matrix-score-grid">
      {matrix.report.score_table.map((row) => (
        <div className="matrix-score-card" key={`${row.harness}-${row.model}`}>
          <div><strong>{harnessNames[row.harness] ?? row.harness}</strong><span>{row.model}</span></div>
          <b>{row.score == null ? "N/A" : row.score.toFixed(2)}</b>
          <small>有效 {row.valid_trials}/7 · I {row.inconclusive} · X {row.case_invalid}</small>
        </div>
      ))}
    </div>
  );
}

function TrialEvidence({ detail }: { detail: MatrixTrialDetail }) {
  const item = detail.summary;
  const effect = detail.oracle.fault_effect_evidence ?? {};
  return (
    <Space direction="vertical" size={16} style={{ width: "100%" }}>
      <div className="trial-identity">
        <div><span>判定</span><VerdictTag verdict={item.agent_verdict} /></div>
        <div><span>平台证据</span><Tag color={item.platform_valid ? "green" : "red"}>{item.platform_valid ? "VALID" : "INVALID"}</Tag></div>
        <div><span>源矩阵</span><code>{item.source_matrix_id ?? "—"}</code></div>
        <div><span>目标 Pod</span><code>{item.target.name ?? "—"}</code></div>
        <div><span>目标 UID</span><code>{item.target.uid ?? "—"}</code></div>
        <div><span>耗时</span><code>{item.duration_seconds == null ? "—" : duration(item.duration_seconds)}</code></div>
      </div>
      {item.validation_error && <Alert type="error" showIcon message="Harness 输出校验失败" description={item.validation_error} />}
      <div className="trial-gates">
        <Gate label="故障真实激活" value={item.fault_active} />
        <Gate label="独立效果验证" value={item.effect_verified} />
        <Gate label="Agent 恢复验证" value={item.agent_recovery_verified} />
        <Gate label="Controller 清理" value={item.controller_cleanup_verified} />
        <Gate label="业务恢复" value={item.business_recovery_verified} />
      </div>
      <Tabs items={[
        {
          key: "agent",
          label: "Agent 原始响应",
          children: (
            <Space direction="vertical" style={{ width: "100%" }}>
              <EvidenceText title="stdout" evidence={detail.agent.stdout} />
              {detail.agent.stderr.available && detail.agent.stderr.text && <EvidenceText title="stderr" evidence={detail.agent.stderr} tone="error" />}
              <JsonBlock title="Harness 报告" value={detail.agent.harness_report} />
            </Space>
          ),
        },
        {
          key: "controller",
          label: `Controller 动作 (${detail.controller.events.length})`,
          children: (
            <Space direction="vertical" style={{ width: "100%" }}>
              <EventList events={detail.controller.events} />
              <JsonBlock title="扰动计划与回滚" value={detail.controller.disturbances} />
              <JsonBlock title="权限恢复" value={detail.controller.permission_restore} />
              <JsonBlock title="环境重置" value={detail.controller.environment_reset} />
            </Space>
          ),
        },
        {
          key: "oracle",
          label: "独立 Oracle / 恢复",
          children: (
            <Space direction="vertical" style={{ width: "100%" }}>
              <div className="oracle-metrics">
                <OracleMetric label="基线延迟" value={formatMs(effect.baseline_cart_avg_response_ms)} />
                <OracleMetric label="故障窗口延迟" value={formatMs(effect.fault_window_cart_avg_response_ms)} />
                <OracleMetric label="延迟增量" value={formatMs(effect.latency_delta_ms)} />
                <OracleMetric label="Cart 请求增量" value={String(effect.cart_request_delta ?? "—")} />
              </div>
              <JsonBlock title="恢复与故障效果证据" value={detail.oracle.recovery} />
            </Space>
          ),
        },
        {
          key: "files",
          label: `原始证据文件 (${detail.files.length})`,
          children: <FileList files={detail.files} />,
        },
      ]} />
    </Space>
  );
}

function Gate({ label, value }: { label: string; value: boolean }) {
  return <div className={value ? "trial-gate gate-good" : "trial-gate gate-muted"}>{value ? <CheckCircle2 size={16} /> : <ShieldAlert size={16} />}<span>{label}</span><strong>{value ? "YES" : "NO"}</strong></div>;
}

function EvidenceText({ title, evidence, tone }: { title: string; evidence: { available: boolean; text: string; truncated: boolean }; tone?: "error" }) {
  if (!evidence.available) return <Alert type="info" showIcon message={`${title} 不存在`} />;
  return <div className={`evidence-text ${tone === "error" ? "evidence-error" : ""}`}><div><strong>{title}</strong>{evidence.truncated && <Tag color="gold">已截断</Tag>}</div><pre>{evidence.text || "（空）"}</pre></div>;
}

function EventList({ events }: { events: Array<Record<string, unknown>> }) {
  if (!events.length) return <Alert type="warning" showIcon message="源矩阵中没有匹配到该 Trial 的 Controller 时间线" />;
  return <div className="trial-event-list">{events.map((event, index) => {
    const payload = asRecord(event.payload);
    return (
      <div className="trial-event" key={`${String(event.kind)}-${index}`}>
        <div><Clock3 size={14} /><span>{formatTime(event.occurred_at ?? event.observed_at)}</span></div>
        <strong>{String(event.kind ?? "event")}</strong>
        <p>{eventSummary(payload)}</p>
        <code>{String(event.source_matrix_id ?? "")}</code>
      </div>
    );
  })}</div>;
}

function JsonBlock({ title, value }: { title: string; value: unknown }) {
  return <details className="json-block"><summary>{title}</summary><pre>{JSON.stringify(value, null, 2)}</pre></details>;
}

function FileList({ files }: { files: MatrixTrialDetail["files"] }) {
  return <div className="evidence-file-list">{files.map((file) => (
    <a href={file.download_url} target="_blank" rel="noreferrer" key={file.path}>
      <FileCheck2 size={16} /><span>{file.path}</span><small>{formatBytes(file.size_bytes)}</small><ExternalLink size={14} />
    </a>
  ))}</div>;
}

function OracleMetric({ label, value }: { label: string; value: string }) {
  return <div><span>{label}</span><strong>{value}</strong></div>;
}

function VerdictTag({ verdict }: { verdict: MatrixVerdict }) {
  return <Tag color={verdict === "PASS" ? "green" : verdict === "FAIL" ? "red" : verdict === "CASE_INVALID" ? "volcano" : "gold"}>{verdict}</Tag>;
}

function BooleanPair({ left, right }: { left: boolean; right: boolean }) {
  return <span className="boolean-pair"><i className={left ? "bool-on" : "bool-off"}>{left ? "✓" : "×"}</i><i className={right ? "bool-on" : "bool-off"}>{right ? "✓" : "×"}</i></span>;
}

function verdictGlyph(verdict: MatrixVerdict) {
  return verdict === "PASS" ? "P" : verdict === "FAIL" ? "F" : verdict === "CASE_INVALID" ? "X" : "I";
}

function duration(value: number) {
  const minutes = Math.floor(value / 60);
  const seconds = Math.round(value % 60);
  return minutes ? `${minutes}m ${seconds}s` : `${seconds}s`;
}

function formatMs(value: unknown) {
  return typeof value === "number" ? `${value.toFixed(1)} ms` : "—";
}

function formatTime(value: unknown) {
  if (typeof value !== "string") return "—";
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? value : date.toLocaleString("zh-CN", { hour12: false });
}

function formatBytes(value: number) {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / 1024 / 1024).toFixed(1)} MB`;
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

function eventSummary(payload: Record<string, unknown>) {
  if (payload.agent_verdict) return `Agent verdict: ${String(payload.agent_verdict)}; platform_valid=${String(payload.platform_valid)}`;
  if (payload.event_kind) return `${String(payload.event_kind)} · phase=${String(payload.phase ?? "—")}`;
  if (payload.cases) return `cases=${JSON.stringify(payload.cases)}`;
  if (payload.platform_status) return `platform_status=${String(payload.platform_status)}`;
  return Object.keys(payload).length ? JSON.stringify(payload) : "—";
}
