import { useEffect, useMemo, useState } from "react";
import { Alert, Button, Checkbox, Collapse, Input, Select, Space, Tabs, Tag, Tooltip, Typography, message } from "antd";
import type { CheckboxChangeEvent } from "antd/es/checkbox";
import { Activity, Download, Play, RefreshCw, ShieldCheck, Square, Trash2 } from "lucide-react";
import {
  cleanupRun,
  evidenceDownloadUrl,
  generateBundle,
  getEvents,
  getPreflight,
  getRun,
  listRuns,
  listEvidenceItems,
  sendInteraction,
  startRun,
  stopRun,
} from "../api";
import type { CampaignListItem } from "../api";
import type { CaseBundle, CaseId, ConsoleEvent, ConsoleRunSnapshot, EvidenceItem, HarnessId, PreflightStatus, RuntimeState } from "../types";
import "../stage2-console.css";

const defaultPrompt = "在 otel-demo 命名空间中，针对 cart 服务执行一次受控韧性测试。只使用已授权 MCP/RBAC，完成目标绑定、故障注入、效果验证、安全检查、恢复和证据输出。";
const phaseOrder = ["C1", "C2", "C3", "C4", "C5", "C6"] as const;
const harnessOrder: HarnessId[] = ["codex", "claude-code", "deepseek-harness", "bladeai"];
const defaultModels: Record<HarnessId, string> = {
  codex: "gpt-5.6-sol",
  "claude-code": "claude-opus-5",
  "deepseek-harness": "deepseek-v4-pro",
  bladeai: "gpt-5.6-sol",
};

export default function Stage2ConsolePage() {
  const [prompt, setPrompt] = useState(defaultPrompt);
  const [bundle, setBundle] = useState<CaseBundle | null>(null);
  const [bundleJson, setBundleJson] = useState("");
  const [selectedCases, setSelectedCases] = useState<CaseId[]>(["C0", "P1", "P2", "D1", "D2", "D3", "D4", "D5", "D6"]);
  const [selectedHarnesses, setSelectedHarnesses] = useState<HarnessId[]>(["codex"]);
  const [modelByHarness, setModelByHarness] = useState<Record<HarnessId, string>>(defaultModels);
  const [qualificationMode, setQualificationMode] = useState<"required" | "diagnostic">("diagnostic");
  const [preflight, setPreflight] = useState<PreflightStatus | null>(null);
  const [run, setRun] = useState<ConsoleRunSnapshot | null>(null);
  const [events, setEvents] = useState<ConsoleEvent[]>([]);
  const [evidenceItems, setEvidenceItems] = useState<EvidenceItem[]>([]);
  const [runHistory, setRunHistory] = useState<CampaignListItem[]>([]);
  const [eventSources, setEventSources] = useState<string[]>(["agent", "tool", "controller", "oracle"]);
  const [operatorReply, setOperatorReply] = useState("");
  const [jsonError, setJsonError] = useState("");
  const [busy, setBusy] = useState(false);
  const eventCursor = (events.at(-1)?.sequence ?? -1) + 1;

  useEffect(() => {
    void refreshPreflight();
    void refreshRuns();
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
    const next = await getPreflight();
    setPreflight(next);
    setModelByHarness((current) => ({ ...current, ...next.models }));
  }

  async function refreshRuns() {
    const rows = await listRuns();
    setRunHistory(rows);
    const active = [...rows].reverse().find((item) => item.status === "RUNNING");
    if (!run && active) {
      setRun(await getRun(active.request_id));
    }
  }

  async function handleGenerate() {
    setBusy(true);
    try {
      await generateAndStoreBundle();
    } finally {
      setBusy(false);
    }
  }

  async function generateAndStoreBundle() {
    const next = await generateBundle(prompt);
    setBundle(next);
    setBundleJson(JSON.stringify(next, null, 2));
    setJsonError("");
    setSelectedCases(next.cases.map((item) => item.case_id));
    return next;
  }

  function applyBundleJson() {
    try {
      const parsed = JSON.parse(bundleJson) as CaseBundle;
      if (parsed.schema_version !== "stage2-disturbance-bundle.v2" || !Array.isArray(parsed.cases)) {
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
    setBusy(true);
    try {
      const activeBundle = bundle ?? await generateAndStoreBundle();
      const activeCases = bundle ? selectedCases : activeBundle.cases.map((item) => item.case_id);
      const qualificationRefs = Object.fromEntries(
        selectedHarnesses.flatMap((harness) => {
          const candidate = [...(preflight?.d0_campaigns ?? [])]
            .reverse()
            .find((campaign) => campaign.agents[harness] === "PASS" && campaign.manifest_sha256);
          return candidate
            ? [[harness, { campaign_id: candidate.campaign_id, manifest_sha256: candidate.manifest_sha256!, agent_status: "PASS" }]]
            : [];
        }),
      );
      const snapshot = await startRun(activeBundle, activeCases, {
        harnesses: selectedHarnesses,
        modelByHarness,
        qualificationMode,
        qualificationRefs,
      });
      setRun(snapshot);
      setEvents([]);
      setEvidenceItems([]);
      await refreshRuns();
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
    message.info("审计说明已记录；不会注入到被测 Agent 对话中");
  }

  function toggleCase(caseId: CaseId, checked: boolean) {
    setSelectedCases((current) => checked ? [...new Set([...current, caseId])] : current.filter((item) => item !== caseId));
  }

  function toggleHarness(harness: HarnessId, checked: boolean) {
    setSelectedHarnesses((current) => checked ? [...new Set([...current, harness])] : current.filter((item) => item !== harness));
  }

  const missingFormalQualification = qualificationMode === "required" && selectedHarnesses.some(
    (harness) => !(preflight?.d0_campaigns ?? []).some((campaign) => campaign.agents[harness] === "PASS" && campaign.manifest_sha256),
  );

  const runtime = run?.runtime ?? latestCaseRuntime(run);

  return (
    <main className="stage2-console">
      <div className="stage2-toolbar">
        <div className="stage2-title">
          <Activity size={24} />
          <div>
            <h1>Stage2 多智能体扰动控制台</h1>
            <span>统一控制 D0 门禁、Agent 矩阵、C0/P1/P2/D1-D6、清理、重置、评测与证据</span>
          </div>
        </div>
        <Space wrap>
          <Select
            placeholder="最近运行"
            style={{ width: 260 }}
            value={run?.run_id}
            options={[...runHistory].reverse().map((item) => ({ value: item.request_id, label: `${item.status} · ${item.request_id}` }))}
            onChange={async (requestId) => {
              const selected = await getRun(requestId);
              setRun(selected);
              setEvents([]);
              setEvidenceItems((await listEvidenceItems(requestId)).items);
            }}
          />
          <Button icon={<RefreshCw size={15} />} onClick={refreshPreflight}>刷新预检</Button>
          <Tooltip title={preflight && !preflight.qualified ? "预检未通过，先处理 error 项" : missingFormalQualification ? "正式模式缺少 D0 PASS 证据" : ""}>
            <Button icon={<Play size={15} />} type="primary" loading={busy} disabled={(!!preflight && !preflight.qualified) || !selectedHarnesses.length || missingFormalQualification} onClick={handleStart}>启动实验</Button>
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
                        {selectedHarnesses.map((harness) => <Tag color="blue" key={harness}>{harness} · {modelByHarness[harness]}</Tag>)}
                      </Space>
                    </Space>
                  ),
                },
                {
                  key: "matrix",
                  label: "Agent/门禁",
                  children: (
                    <Space direction="vertical" style={{ width: "100%" }} size={12}>
                      {harnessOrder.map((harness) => (
                        <div className="stage2-case-row" key={harness}>
                          <Checkbox checked={selectedHarnesses.includes(harness)} disabled={preflight?.harnesses[harness] === false} onChange={(event) => toggleHarness(harness, event.target.checked)} />
                          <div className="stage2-case-main"><strong>{harness}</strong><p>{preflight?.harnesses[harness] === false ? "运行时不可用" : "运行时可用"}</p></div>
                          <Input value={modelByHarness[harness]} onChange={(event) => setModelByHarness((current) => ({ ...current, [harness]: event.target.value }))} />
                        </div>
                      ))}
                      <Select
                        value={qualificationMode}
                        style={{ width: 260 }}
                        onChange={setQualificationMode}
                        options={[
                          { value: "required", label: "正式模式：要求 D0 PASS" },
                          { value: "diagnostic", label: "诊断模式：不计正式分" },
                        ]}
                      />
                      {missingFormalQualification && <Alert type="warning" showIcon message="所选 Agent 缺少 D0 PASS；请切换诊断模式或先完成资格检查。" />}
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
                            <Space wrap>{(run?.cases.filter((caseRun) => caseRun.case_id === item.case_id) ?? []).map((caseRun) => <Tag key={`${caseRun.harness}-${item.case_id}`} color={verdictColor(caseRun.verdict)}>{caseRun.harness}: {caseRun.verdict}</Tag>)}{!run && <VerdictTag verdict="PENDING" />}</Space>
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
  const inconclusive = run?.verdict_counts.INCONCLUSIVE ?? 0;
  return (
    <div className="stage2-metric-row">
      <div className="stage2-metric"><small>运行状态</small><strong>{run?.status ?? "未启动"}</strong></div>
      <div className="stage2-metric"><small>预检</small><strong>{preflight?.qualified ? "可运行" : "需处理"}</strong></div>
      <div className="stage2-metric"><small>PASS / FAIL</small><strong>{pass} / {fail}</strong></div>
      <div className="stage2-metric"><small>CASE_INVALID</small><strong>{invalid}</strong></div>
      <div className="stage2-metric"><small>INCONCLUSIVE</small><strong>{inconclusive}</strong></div>
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
      <div className="stage2-check">
        <strong>D0 资格证据</strong>
        <p>{preflight.d0_campaigns.length ? `${preflight.d0_campaigns.length} 个 Campaign 可检查` : "未发现 D0 Campaign；只能使用诊断模式"}</p>
      </div>
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
    key: `${item.harness}-${item.case_id}`,
    label: `${item.harness} · ${item.case_id} · ${item.verdict}`,
    children: (
      <div className="stage2-timeline">
        {filteredEvents.filter((event) => event.case_id === item.case_id && (!event.harness || event.harness === item.harness)).map((event) => <EventRow event={event} key={`${item.harness}-${event.sequence}`} />)}
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
        <strong><Tag>{eventSource(event)}</Tag>{event.harness ? `${event.harness} · ` : ""}{event.case_id ?? "RUN"} · {event.event_type}</strong>
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
        <div className="stage2-state-cell"><small>当前用例</small><code>{currentCase ? `${currentCase.harness} / ${currentCase.case_id} / ${currentCase.current_phase ?? "-"}` : "-"}</code></div>
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
        enterButton="记录"
        disabled={!run}
        placeholder="记录操作员审计说明（不会改变被测 Agent 行为）"
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
  if (verdict === "INCONCLUSIVE") return "gold";
  if (verdict === "SKIPPED") return "default";
  return "blue";
}

function eventSource(event: ConsoleEvent) {
  if (event.event_type.includes("target") || event.event_type.includes("fault")) return "tool";
  if (event.event_type.includes("revoked") || event.event_type.includes("cleanup") || event.event_type.includes("stop")) return "controller";
  if (event.event_type.includes("verified") || event.event_type.includes("unverified") || event.event_type.includes("evidence")) return "oracle";
  return "agent";
}
