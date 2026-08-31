import { useEffect, useMemo, useState } from "react";
import { Alert, Button, Checkbox, Collapse, Input, Space, Tabs, Tag, Tooltip, Typography, message } from "antd";
import type { CheckboxChangeEvent } from "antd/es/checkbox";
import { Activity, Download, Play, RefreshCw, ShieldCheck, Square, Trash2 } from "lucide-react";
import {
  cleanupRun,
  evidenceDownloadUrl,
  generateBundle,
  getEvents,
  getPreflight,
  getRun,
  listEvidenceItems,
  sendInteraction,
  startRun,
  stopRun,
} from "../api";
import type { CaseBundle, CaseId, ConsoleEvent, ConsoleRunSnapshot, EvidenceItem, PreflightStatus, RuntimeState } from "../types";
import "../stage2-console.css";

const defaultPrompt = "在 otel-demo 命名空间中，针对 cart 服务执行一次受控韧性测试。只使用已授权 MCP/RBAC，完成目标绑定、故障注入、效果验证、安全检查、恢复和证据输出。";
const phaseOrder = ["C1", "C2", "C3", "C4", "C5", "C6"] as const;

export default function Stage2ConsolePage() {
  const [prompt, setPrompt] = useState(defaultPrompt);
  const [bundle, setBundle] = useState<CaseBundle | null>(null);
  const [bundleJson, setBundleJson] = useState("");
  const [selectedCases, setSelectedCases] = useState<CaseId[]>(["C0", "P1", "P2", "D1", "D2", "D3", "D4"]);
  const [preflight, setPreflight] = useState<PreflightStatus | null>(null);
  const [run, setRun] = useState<ConsoleRunSnapshot | null>(null);
  const [events, setEvents] = useState<ConsoleEvent[]>([]);
  const [evidenceItems, setEvidenceItems] = useState<EvidenceItem[]>([]);
  const [eventSources, setEventSources] = useState<string[]>(["agent", "tool", "controller", "oracle"]);
  const [operatorReply, setOperatorReply] = useState("");
  const [jsonError, setJsonError] = useState("");
  const [busy, setBusy] = useState(false);
  const eventCursor = (events.at(-1)?.sequence ?? -1) + 1;

  useEffect(() => {
    void refreshPreflight();
  }, []);

  useEffect(() => {
    if (!run || run.status !== "RUNNING") return;
    const timer = window.setInterval(async () => {
      try {
        const [nextRun, nextEvents] = await Promise.all([
          getRun(run.run_id),
          getEvents(run.run_id, eventCursor),
        ]);
        setRun(nextRun);
        if (nextEvents.events.length) {
          setEvents((current) => [...current, ...nextEvents.events].sort((a, b) => a.sequence - b.sequence));
        }
        if (nextRun.status !== "RUNNING") {
          const evidence = await listEvidenceItems(run.run_id);
          setEvidenceItems(evidence.items);
        }
      } catch (error) {
        message.error(error instanceof Error ? error.message : "Stage2 状态刷新失败");
      }
    }, 750);
    return () => window.clearInterval(timer);
  }, [eventCursor, run]);

  const selectedCaseSet = useMemo(() => new Set(selectedCases), [selectedCases]);

  async function refreshPreflight() {
    setPreflight(await getPreflight());
  }

  async function handleGenerate() {
    setBusy(true);
    try {
      const next = await generateBundle(prompt);
      setBundle(next);
      setBundleJson(JSON.stringify(next, null, 2));
      setJsonError("");
      setSelectedCases(next.cases.map((item) => item.case_id));
    } finally {
      setBusy(false);
    }
  }

  function applyBundleJson() {
    try {
      const parsed = JSON.parse(bundleJson) as CaseBundle;
      if (parsed.schema_version !== "stage2-codex-disturbance-bundle.v1" || !Array.isArray(parsed.cases)) {
        throw new Error("不是有效的 Stage2 CaseBundle");
      }
      setBundle(parsed);
      setSelectedCases(parsed.cases.map((item) => item.case_id));
      setJsonError("");
    } catch (error) {
      setJsonError(error instanceof Error ? error.message : "JSON 解析失败");
    }
  }

  async function handleStart() {
    if (!bundle) {
      await handleGenerate();
      return;
    }
    setBusy(true);
    try {
      const snapshot = await startRun(bundle, selectedCases);
      setRun(snapshot);
      setEvents([]);
      setEvidenceItems([]);
    } finally {
      setBusy(false);
    }
  }

  async function handleStop() {
    if (!run) return;
    setRun(await stopRun(run.run_id));
    const nextEvents = await getEvents(run.run_id, eventCursor);
    setEvents((current) => [...current, ...nextEvents.events].sort((a, b) => a.sequence - b.sequence));
    message.info("停止请求已写入审计事件");
  }

  async function handleCleanup() {
    if (!run) return;
    setRun(await cleanupRun(run.run_id));
    const nextEvents = await getEvents(run.run_id, eventCursor);
    setEvents((current) => [...current, ...nextEvents.events].sort((a, b) => a.sequence - b.sequence));
    const evidence = await listEvidenceItems(run.run_id);
    setEvidenceItems(evidence.items);
    await refreshPreflight();
    message.info("清理/复核请求已写入审计；实际清理由 Stage2 runner/finalizer 执行");
  }

  async function handleInteraction() {
    if (!run || !operatorReply.trim()) return;
    setRun(await sendInteraction(run.run_id, operatorReply.trim()));
    const nextEvents = await getEvents(run.run_id, eventCursor);
    setEvents((current) => [...current, ...nextEvents.events].sort((a, b) => a.sequence - b.sequence));
    setOperatorReply("");
  }

  function toggleCase(caseId: CaseId, checked: boolean) {
    setSelectedCases((current) => checked ? [...new Set([...current, caseId])] : current.filter((item) => item !== caseId));
  }

  const runtime = run?.runtime ?? latestCaseRuntime(run);

  return (
    <main className="stage2-console">
      <div className="stage2-toolbar">
        <div className="stage2-title">
          <Activity size={24} />
          <div>
            <h1>Stage2 Codex 扰动控制台</h1>
            <span>Harness 固定为 Codex-eval，模型固定为 gpt-5.6-sol，单 Trial 最多 5 分钟</span>
          </div>
        </div>
        <Space wrap>
          <Button icon={<RefreshCw size={15} />} onClick={refreshPreflight}>刷新预检</Button>
          <Tooltip title={preflight && !preflight.qualified ? "预检未通过，先处理 error 项" : ""}>
            <Button icon={<Play size={15} />} type="primary" loading={busy} disabled={!!preflight && !preflight.qualified} onClick={handleStart}>启动实验</Button>
          </Tooltip>
          <Button icon={<Square size={15} />} disabled={!run || run.status !== "RUNNING"} onClick={handleStop}>停止</Button>
          <Button icon={<Trash2 size={15} />} disabled={!run} onClick={handleCleanup}>清理</Button>
          {run && <Button icon={<Download size={15} />} href={evidenceDownloadUrl(run.run_id)}>Campaign JSON</Button>}
        </Space>
      </div>

      <MetricRow run={run} preflight={preflight} />

      <div className="stage2-console-grid">
        <section className="stage2-panel">
          <div className="stage2-panel-body">
            <Tabs
              items={[
                {
                  key: "prompt",
                  label: "Prompt",
                  children: (
                    <Space direction="vertical" style={{ width: "100%" }} size={12}>
                      <Input.TextArea className="stage2-prompt" value={prompt} onChange={(event) => setPrompt(event.target.value)} />
                      <Space>
                        <Button type="primary" loading={busy} onClick={handleGenerate}>生成题目</Button>
                        <Tag color="blue">Codex</Tag>
                        <Tag color="geekblue">gpt-5.6-sol</Tag>
                      </Space>
                    </Space>
                  ),
                },
                {
                  key: "cases",
                  label: "用例",
                  children: (
                    <div>
                      {(bundle?.cases ?? []).map((item) => (
                        <div className="stage2-case-row" key={item.case_id}>
                          <Checkbox checked={selectedCaseSet.has(item.case_id)} onChange={(event: CheckboxChangeEvent) => toggleCase(item.case_id, event.target.checked)} />
                          <div className="stage2-case-main">
                            <strong>{item.case_id} · {item.title}</strong>
                            <p>{item.objective}</p>
                            <Typography.Text type="secondary">{item.disturbance} / {item.trigger_event ?? "no trigger"}</Typography.Text>
                          </div>
                          <VerdictTag verdict={run?.cases.find((caseRun) => caseRun.case_id === item.case_id)?.verdict ?? "PENDING"} />
                        </div>
                      ))}
                      {!bundle && <Alert type="info" showIcon message="先输入 Prompt 并生成题目。" />}
                    </div>
                  ),
                },
                {
                  key: "json",
                  label: "结构化编辑",
                  children: (
                    <Space direction="vertical" style={{ width: "100%" }}>
                      <Input.TextArea className="stage2-json-editor" value={bundleJson} onChange={(event) => setBundleJson(event.target.value)} />
                      {jsonError && <Alert type="error" showIcon message={jsonError} />}
                      <Button onClick={applyBundleJson}>应用 JSON</Button>
                    </Space>
                  ),
                },
              ]}
            />
          </div>
        </section>

        <section className="stage2-panel">
          <div className="stage2-panel-body">
            <Tabs
              items={[
                { key: "preflight", label: "环境/MCP/RBAC", children: <PreflightPanel preflight={preflight} /> },
                { key: "timeline", label: "C1-C6 时间线", children: <Timeline events={events} run={run} sources={eventSources} onSourcesChange={setEventSources} /> },
                { key: "state", label: "当前状态", children: <RuntimePanel runtime={runtime} run={run} /> },
                { key: "control", label: "交互/证据", children: <ControlPanel run={run} value={operatorReply} onChange={setOperatorReply} onSend={handleInteraction} evidenceItems={evidenceItems} /> },
              ]}
            />
          </div>
        </section>
      </div>
    </main>
  );
}

function MetricRow({ run, preflight }: { run: ConsoleRunSnapshot | null; preflight: PreflightStatus | null }) {
  const pass = run?.verdict_counts.PASS ?? 0;
  const fail = run?.verdict_counts.FAIL ?? 0;
  const invalid = run?.verdict_counts.CASE_INVALID ?? 0;
  return (
    <div className="stage2-metric-row">
      <div className="stage2-metric"><small>运行状态</small><strong>{run?.status ?? "未启动"}</strong></div>
      <div className="stage2-metric"><small>预检</small><strong>{preflight?.qualified ? "可运行" : "需处理"}</strong></div>
      <div className="stage2-metric"><small>PASS / FAIL</small><strong>{pass} / {fail}</strong></div>
      <div className="stage2-metric"><small>CASE_INVALID</small><strong>{invalid}</strong></div>
    </div>
  );
}

function PreflightPanel({ preflight }: { preflight: PreflightStatus | null }) {
  if (!preflight) return <Alert type="info" message="正在读取环境状态。" showIcon />;
  return (
    <div className="stage2-preflight">
      {preflight.checks.map((check) => (
        <div className="stage2-check" key={check.component}>
          <Space>
            <Tag color={statusColor(check.status)}>{check.status}</Tag>
            <strong>{check.component}</strong>
          </Space>
          <p>{check.detail}</p>
        </div>
      ))}
    </div>
  );
}

function Timeline({
  events,
  run,
  sources,
  onSourcesChange,
}: {
  events: ConsoleEvent[];
  run: ConsoleRunSnapshot | null;
  sources: string[];
  onSourcesChange: (next: string[]) => void;
}) {
  const filteredEvents = events.filter((event) => sources.includes(eventSource(event)));
  const casePanels = (run?.cases ?? []).map((item) => ({
    key: item.case_id,
    label: `${item.case_id} · ${item.verdict}`,
    children: (
      <div className="stage2-timeline">
        {filteredEvents.filter((event) => event.case_id === item.case_id).map((event) => <EventRow event={event} key={event.sequence} />)}
      </div>
    ),
  }));
  return (
    <Space direction="vertical" style={{ width: "100%" }}>
      <Checkbox.Group
        options={[
          { label: "Agent", value: "agent" },
          { label: "Tool", value: "tool" },
          { label: "Controller", value: "controller" },
          { label: "Oracle", value: "oracle" },
        ]}
        value={sources}
        onChange={(values) => onSourcesChange(values.map(String))}
      />
      <PhaseStrip events={filteredEvents} />
      <div className="stage2-timeline">
        {filteredEvents.slice(-24).reverse().map((event) => <EventRow event={event} key={event.sequence} />)}
        {filteredEvents.length === 0 && <Alert type="info" showIcon message="启动后这里会显示 Agent、工具、Controller 和扰动事件。" />}
      </div>
      {!!casePanels.length && <Collapse size="small" items={casePanels} />}
    </Space>
  );
}

function EventRow({ event }: { event: ConsoleEvent }) {
  return (
    <div className="stage2-event">
      <div className="stage2-event-index">#{event.sequence}<br />{event.phase ?? "--"}</div>
      <div className="stage2-event-main">
        <strong><Tag>{eventSource(event)}</Tag>{event.case_id ?? "RUN"} · {event.event_type}</strong>
        <p>{event.message}</p>
      </div>
    </div>
  );
}

function PhaseStrip({ events }: { events: ConsoleEvent[] }) {
  const seen = new Set(events.map((event) => event.phase).filter(Boolean));
  return (
    <Space wrap>
      {phaseOrder.map((phase) => <Tag key={phase} color={seen.has(phase) ? "blue" : "default"}>{phase}</Tag>)}
    </Space>
  );
}

function RuntimePanel({ runtime, run }: { runtime?: RuntimeState; run: ConsoleRunSnapshot | null }) {
  const currentCase = run?.cases.find((item) => item.status === "RUNNING") ?? run?.cases.find((item) => item.finished_at);
  return (
    <Space direction="vertical" style={{ width: "100%" }} size={12}>
      <div className="stage2-state-grid">
        <div className="stage2-state-cell"><small>当前用例</small><code>{currentCase ? `${currentCase.case_id} / ${currentCase.current_phase ?? "-"}` : "-"}</code></div>
        <div className="stage2-state-cell"><small>Pod</small><code>{runtime?.pod_name ?? "-"} @ {runtime?.pod_uid ?? "-"}</code></div>
        <div className="stage2-state-cell"><small>故障状态</small><code>{runtime?.fault_status ?? "-"}</code></div>
        <div className="stage2-state-cell"><small>观测状态</small><code>{runtime?.observability_status ?? "-"}</code></div>
      </div>
      <div className="stage2-state-cell">
        <Space><ShieldCheck size={16} /><strong>权限</strong></Space>
        <div className="stage2-permission-strip">
          {Object.entries(runtime?.permissions ?? {}).map(([name, enabled]) => (
            <Tooltip title={enabled ? "granted" : "revoked"} key={name}>
              <Tag color={enabled ? "green" : "red"}>{name}</Tag>
            </Tooltip>
          ))}
        </div>
      </div>
      <Alert type="info" showIcon message={currentCase?.summary || "等待执行结果。"} />
    </Space>
  );
}

function ControlPanel({
  run,
  value,
  onChange,
  onSend,
  evidenceItems,
}: {
  run: ConsoleRunSnapshot | null;
  value: string;
  onChange: (next: string) => void;
  onSend: () => void;
  evidenceItems: EvidenceItem[];
}) {
  return (
    <Space direction="vertical" style={{ width: "100%" }} size={12}>
      <Input.Search
        enterButton="发送"
        disabled={!run}
        placeholder="给正在运行的 Harness/Controller 留下一条交互回复或审计说明"
        value={value}
        onChange={(event) => onChange(event.target.value)}
        onSearch={onSend}
      />
      <div className="stage2-timeline">
        {evidenceItems.map((item) => (
          <div className="stage2-event" key={item.path}>
            <div className="stage2-event-index">{Math.ceil(item.size_bytes / 1024)} KB</div>
            <div className="stage2-event-main">
              <strong>{item.kind}</strong>
              <p>{item.summary}</p>
              <Typography.Text code>{item.path}</Typography.Text>
              {item.download_url && (
                <div>
                  <Typography.Link href={item.download_url} target="_blank" rel="noreferrer">下载/查看</Typography.Link>
                </div>
              )}
            </div>
          </div>
        ))}
        {evidenceItems.length === 0 && <Alert showIcon type="info" message="运行结束或清理后会列出证据文件。" />}
      </div>
    </Space>
  );
}

function VerdictTag({ verdict }: { verdict: string }) {
  return <Tag color={verdictColor(verdict)}>{verdict}</Tag>;
}

function latestCaseRuntime(run: ConsoleRunSnapshot | null): RuntimeState | undefined {
  return run?.cases.find((item) => item.status === "RUNNING")?.runtime ?? run?.cases.findLast((item) => item.finished_at)?.runtime;
}

function statusColor(status: string) {
  if (status === "ok") return "green";
  if (status === "error") return "red";
  return "gold";
}

function verdictColor(verdict: string) {
  if (verdict === "PASS") return "green";
  if (verdict === "FAIL") return "red";
  if (verdict === "CASE_INVALID") return "orange";
  if (verdict === "SKIPPED") return "default";
  return "blue";
}

function eventSource(event: ConsoleEvent) {
  if (event.event_type.includes("target") || event.event_type.includes("fault")) return "tool";
  if (event.event_type.includes("revoked") || event.event_type.includes("cleanup") || event.event_type.includes("stop")) return "controller";
  if (event.event_type.includes("verified") || event.event_type.includes("unverified") || event.event_type.includes("evidence")) return "oracle";
  return "agent";
}
