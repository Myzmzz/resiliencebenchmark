import { useCallback, useEffect, useState } from "react";
import { Alert, Button, Descriptions, message, Modal, Progress, Select, Space, Tag, Tabs } from "antd";
import { ReloadOutlined, StopOutlined } from "@ant-design/icons";
import { useNavigate, useParams } from "react-router-dom";
import { abortEvaluationTask, getEvaluationTask } from "../api";
import { formatDuration, formatTime, percent, phaseLabel } from "../formatters";
import { useAsyncResource } from "../hooks/useAsyncResource";
import { useEvaluationEventStream } from "../hooks/useEvaluationEventStream";
import HarnessTrack from "../components/HarnessTrack";
import MetricCard from "../components/MetricCard";
import { PageError, PageLoading } from "../components/PageState";
import UnitMatrix from "../components/UnitMatrix";
import type { EvaluationUnitSummary, TaskPhase } from "../types";

const PHASES: TaskPhase[] = ["PREPARING", "QUALIFYING", "BASELINING", "EXECUTING", "RECOVERING", "EVALUATING", "CLEANING_UP"];

function StageTrack({ current }: { current?: TaskPhase }) {
  const currentIndex = Math.max(0, PHASES.indexOf(current ?? "PREPARING"));
  return <div className="evaluation-stage-track">{PHASES.map((phase, index) => <div className={`evaluation-stage ${index < currentIndex ? "is-complete" : ""} ${index === currentIndex ? "is-current" : ""}`} key={phase}>{phaseLabel(phase)}</div>)}</div>;
}

export default function EvaluationTaskMonitorPage() {
  const { taskId } = useParams();
  const navigate = useNavigate();
  const loader = useCallback((signal: AbortSignal) => getEvaluationTask(taskId ?? "", signal), [taskId]);
  const resource = useAsyncResource(loader);
  const stream = useEvaluationEventStream(taskId);
  const [now, setNow] = useState(() => Date.now());
  const [selectedHarnessId, setSelectedHarnessId] = useState<string>();
  const [selectedSystemId, setSelectedSystemId] = useState<string>();

  const reload = resource.reload;
  useEffect(() => { if (stream.lastEventId) void reload(); }, [reload, stream.lastEventId]);
  useEffect(() => {
    const update = () => setNow(Date.now());
    const timer = window.setInterval(update, 1000);
    return () => window.clearInterval(timer);
  }, []);
  const abort = () => Modal.confirm({
    title: "安全中止评测任务？",
    content: "中止后任务将进入恢复和清理阶段，不会立即释放环境。",
    okText: "确认安全中止",
    okButtonProps: { danger: true },
    cancelText: "取消",
    onOk: async () => {
      if (!taskId) return;
      try { await abortEvaluationTask(taskId, "operator_requested"); message.success("中止请求已提交"); await resource.reload(); }
      catch (reason) { message.error(reason instanceof Error ? reason.message : "中止失败"); }
    },
  });

  if (resource.loading && !resource.data) return <div className="evaluation-page"><PageLoading /></div>;
  if (resource.error || !resource.data) return <div className="evaluation-page"><PageError error={resource.error ?? new Error("任务不存在")} onRetry={() => void resource.reload()} /></div>;
  const task = resource.data;
  const events = stream.events.length ? stream.events : task.recentEvents;
  const current = task.currentUnit;
  const effectiveHarnessId = selectedHarnessId ?? task.harnessProgress.find((item) => item.status === "RUNNING")?.harnessId ?? task.harnessProgress[0]?.harnessId;
  const effectiveSystemId = selectedSystemId ?? task.currentUnit?.systemId ?? task.systems[0]?.id;
  const selectedHarness = task.harnessProgress.find((item) => item.harnessId === effectiveHarnessId);
  const selectedUnit = task.units.find((item) => item.status === "RUNNING" && item.harnessId === effectiveHarnessId) ?? current;
  const duration = task.startedAt && now ? Math.floor((now - new Date(task.startedAt).getTime()) / 1000) : undefined;
  const connectionLabel = { CONNECTING: "SSE 连接中", OPEN: "SSE 已连接", RECONNECTING: "SSE 重连中", ERROR: "SSE 错误", CLOSED: "SSE 已关闭" }[stream.connection];
  const selectUnit = (unit: EvaluationUnitSummary) => navigate(`/evaluation/monitoring/${task.taskId}/units/${unit.unitId}`);
  const eventsContent = <><div className="evaluation-event-list">{events.slice(-12).map((event) => <div className="evaluation-event" key={`${event.sequence}-${event.id}`}><time>{new Date(event.occurredAt).toLocaleTimeString()}</time><span>{event.message}</span></div>)}</div><div className="evaluation-actions"><span className={`connection-${stream.connection.toLowerCase()}`}>{connectionLabel}</span><span className="evaluation-muted">事件 {events.length} · Last-Event-ID {stream.lastEventId ?? "—"}</span></div></>;
  const evidenceContent = <div><div className="evaluation-summary-grid"><MetricCard label="PASS" value={task.units.filter((unit) => unit.outcome === "PASS").length} tone="success" /><MetricCard label="FAIL" value={task.units.filter((unit) => unit.outcome === "FAIL").length} tone="danger" /><MetricCard label="CASE_INVALID" value={task.units.filter((unit) => unit.outcome === "CASE_INVALID").length} tone="warning" /><MetricCard label="待执行" value={task.units.filter((unit) => unit.status === "PENDING").length} /></div>{selectedUnit && <Button type="link" onClick={() => selectUnit(selectedUnit)}>查看当前单元的可观测证据</Button>}</div>;
  const environmentContent = <div><Descriptions size="small" column={2} bordered><Descriptions.Item label="环境">{task.environmentName}</Descriptions.Item><Descriptions.Item label="租约">{task.lease?.status ?? "未知"}</Descriptions.Item><Descriptions.Item label="最后心跳">{formatTime(task.lease?.heartbeatAt)}</Descriptions.Item><Descriptions.Item label="租约持有者">{task.lease?.holderTaskId ?? task.taskId}</Descriptions.Item></Descriptions><div className="evaluation-option-list" style={{ marginTop: 12 }}>{task.systemProgress.map((system) => <div className="evaluation-option-row" key={system.systemId}><span>{system.systemName}</span><span>{system.completedUnits}/{system.totalUnits} · {system.status}</span></div>)}</div></div>;

  return (
    <div className="evaluation-page">
      <a className="evaluation-back" onClick={() => navigate("/evaluation/monitoring")}>← 返回运行监控</a>
      <header className="evaluation-page-header">
        <div><Space><h2>{task.name}</h2><Tag color="blue">评测中</Tag><Tag color={task.lease?.status === "HELD" ? "green" : "orange"}>环境租约{task.lease?.status === "HELD" ? "正常" : task.lease?.status}</Tag></Space><p>{task.taskId}</p></div>
        <Space><Button icon={<ReloadOutlined />} onClick={() => void resource.reload()}>刷新</Button><Button danger icon={<StopOutlined />} onClick={abort}>安全中止</Button></Space>
      </header>
      <section className="evaluation-panel evaluation-wizard-summary"><span>{task.environmentName}</span><span>{task.systems.length} 个被测系统</span><span>{task.harnessProgress.length} 个 Harness</span><span>{task.modelCount} 个模型</span><span>{task.uniqueQuestionCount} 道题</span><span>{task.evaluationUnitCount} 个评测单元</span></section>
      <div className="evaluation-summary-grid five">
        <MetricCard label="总体进度" value={`${task.completedUnitCount} / ${task.evaluationUnitCount}`} footer={<Progress size="small" percent={percent(task.completedUnitCount, task.evaluationUnitCount)} showInfo={false} />} />
        <MetricCard label="当前单元" value={current ? current.questionIndex : "—"} tone="primary" footer={current?.unitId} />
        <MetricCard label="Harness 总数" value={task.harnessProgress.length} />
        <MetricCard label="已完成 Harness" value={task.harnessProgress.filter((item) => item.status === "COMPLETED").length} tone="success" />
        <MetricCard label="运行时长" value={formatDuration(duration)} />
      </div>
      <HarnessTrack items={task.harnessProgress} selectedId={effectiveHarnessId} onSelect={setSelectedHarnessId} />
      <div className="evaluation-two-column" style={{ gridTemplateColumns: "minmax(0, 1.55fr) minmax(450px, 1fr)" }}>
        <section className="evaluation-panel">
          <div className="evaluation-panel-header"><div><h3>当前评测单元</h3><span className="evaluation-muted">{selectedHarness?.harnessName}</span></div>{selectedUnit && <Tag color="blue">{phaseLabel(selectedUnit.phase)}</Tag>}</div>
          {!selectedUnit ? <Alert type="info" showIcon title="当前 Harness 暂无运行单元" /> : <>
            <h3>{selectedUnit.harnessName} × {selectedUnit.modelName} × {selectedUnit.questionId} · {selectedUnit.questionTitle}</h3>
            <div className="evaluation-wizard-summary" style={{ marginTop: 12 }}><span>系统 {selectedUnit.systemName}</span><span>目标 {selectedUnit.targetService ?? "—"}</span><span>Trial {selectedUnit.currentTrial ?? 0}/{selectedUnit.maxTrials}</span></div>
            <StageTrack current={selectedUnit.phase} />
            {stream.error && <Alert type="warning" showIcon title={stream.error.message} style={{ marginTop: 12 }} />}
            <Tabs items={[{ key: "events", label: "实时事件", children: eventsContent }, { key: "evidence", label: "可观测证据", children: evidenceContent }, { key: "environment", label: "环境状态", children: environmentContent }]} />
          </>}
        </section>
        <div>
          <div className="evaluation-panel" style={{ marginBottom: 12 }}><div className="evaluation-panel-header"><h3>矩阵筛选</h3></div><Space wrap><Select value={effectiveSystemId} style={{ minWidth: 180 }} options={task.systems.map((item) => ({ value: item.id, label: item.name }))} onChange={setSelectedSystemId} /><Tag>{selectedHarness?.harnessName}</Tag></Space></div>
          <UnitMatrix units={task.units} systemId={effectiveSystemId} harnessId={effectiveHarnessId} selectedUnitId={selectedUnit?.unitId} onSelect={selectUnit} />
          <section className="evaluation-panel"><div className="evaluation-panel-header"><h3>环境租约</h3><Tag color={task.lease?.status === "HELD" ? "green" : "orange"}>{task.lease?.status ?? "未知"}</Tag></div><div className="evaluation-muted">最后心跳 {formatTime(task.lease?.heartbeatAt)} · 持有者 {task.lease?.holderTaskId ?? task.taskId}</div></section>
        </div>
      </div>
    </div>
  );
}
