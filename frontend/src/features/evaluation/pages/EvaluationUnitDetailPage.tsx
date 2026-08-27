import { useCallback, useEffect, useState } from "react";
import { Alert, Button, Descriptions, message, Modal, Space, Tabs, Tag } from "antd";
import { ReloadOutlined, StopOutlined, CheckOutlined } from "@ant-design/icons";
import { useNavigate, useParams } from "react-router-dom";
import { abortEvaluationTask, getEvaluationUnit } from "../api";
import { formatDuration, formatTime, phaseLabel } from "../formatters";
import { useAsyncResource } from "../hooks/useAsyncResource";
import { useEvaluationEventStream } from "../hooks/useEvaluationEventStream";
import FaultDisturbancePanel from "../components/FaultDisturbancePanel";
import MetricCard from "../components/MetricCard";
import OracleGateList from "../components/OracleGateList";
import { PageError, PageLoading } from "../components/PageState";
import type { TaskPhase } from "../types";

const PHASES: TaskPhase[] = ["PREPARING", "QUALIFYING", "BASELINING", "EXECUTING", "RECOVERING", "EVALUATING", "CLEANING_UP"];

export default function EvaluationUnitDetailPage() {
  const { taskId, unitId } = useParams();
  const navigate = useNavigate();
  const loader = useCallback((signal: AbortSignal) => getEvaluationUnit(taskId ?? "", unitId ?? "", signal), [taskId, unitId]);
  const resource = useAsyncResource(loader);
  const stream = useEvaluationEventStream(taskId);
  const [now, setNow] = useState(() => Date.now());
  const reload = resource.reload;
  useEffect(() => { if (stream.lastEventId) void reload(); }, [reload, stream.lastEventId]);
  useEffect(() => {
    const update = () => setNow(Date.now());
    const timer = window.setInterval(update, 1000);
    return () => window.clearInterval(timer);
  }, []);

  const abort = () => Modal.confirm({
    title: "安全中止当前任务？",
    content: "中止作用于整个任务，并进入恢复和清理阶段。",
    okText: "安全中止",
    okButtonProps: { danger: true },
    onOk: async () => {
      if (!taskId) return;
      try { await abortEvaluationTask(taskId, "operator_requested_from_unit"); message.success("中止请求已提交"); }
      catch (reason) { message.error(reason instanceof Error ? reason.message : "中止失败"); }
    },
  });

  if (resource.loading && !resource.data) return <div className="evaluation-page"><PageLoading /></div>;
  if (resource.error || !resource.data) return <div className="evaluation-page"><PageError error={resource.error ?? new Error("评测单元不存在")} onRetry={() => void resource.reload()} /></div>;
  const unit = resource.data;
  const currentIndex = Math.max(0, PHASES.indexOf(unit.phase ?? "PREPARING"));
  const events = stream.events.filter((event) => event.unitId === unit.unitId).length ? stream.events.filter((event) => event.unitId === unit.unitId) : unit.events;
  const phaseDuration = unit.phaseStartedAt && now ? Math.floor((now - new Date(unit.phaseStartedAt).getTime()) / 1000) : undefined;
  const unitDuration = unit.unitStartedAt && now ? Math.floor((now - new Date(unit.unitStartedAt).getTime()) / 1000) : undefined;

  const overview = <div >
    <div>
      <FaultDisturbancePanel fault={unit.mainFault} disturbances={unit.disturbances} budget={unit.disturbanceBudget} />
      
      <div className="overview-box">
        <div className="evaluation-summary-grid" style={{ padding: "12px 0", borderBottom: "1px solid #e2e8f0", marginBottom: 0 }}>
          {unit.liveMetrics.map((metric) => 
          <div className="overview-box-metric">
            <p className="overview-box-metric-label">{metric.label}</p>
            <p className={`overview-box-metric-value ${metric.status === "CRITICAL" ? "danger" : metric.status === "WARNING" ? "warning" : "success"}`}>{metric.value}</p>
            <p className="overview-box-metric-baseline">{metric.baseline ? `基线 ${metric.baseline}` : undefined}</p>
            {/* <p>{metric.status === "CRITICAL" ? "danger" : metric.status === "WARNING" ? "warning" : "success"}</p> */}
          </div>
          // <MetricCard key={metric.id} label={metric.label} value={metric.value} footer={metric.baseline ? `基线 ${metric.baseline}` : undefined} tone={metric.status === "CRITICAL" ? "danger" : metric.status === "WARNING" ? "warning" : "success"} />
          )}
        </div>
        <div className="events-new">
          <div className="events-new-content">
            <h3>实时事件</h3>
            {stream.error && <Alert type="warning" showIcon title={stream.error.message} />}
            <div className="evaluation-event-list">{events.map((event) => <div className="evaluation-event" key={`${event.sequence}-${event.id}`}><time>{new Date(event.occurredAt).toLocaleTimeString()}</time><span>{event.message}</span></div>)}</div>
          </div>
          <div className="events-new-footer">
            事件序列137 · SSE已连接 · 最近更新2秒前
          </div>
        </div>
      </div>
    </div>
    
  </div>;

  return (
    <div className="evaluation-page">
      <a className="evaluation-back" onClick={() => navigate(`/evaluation/monitoring/${taskId}`)}>← 返回任务运行详情</a>
      <header className="evaluation-page-header"><div><Space><h2>{unit.questionId} · {unit.questionTitle}</h2><Tag color="blue">{phaseLabel(unit.phase)}</Tag><Tag>{unit.harnessName}</Tag><Tag>{unit.modelName}</Tag><Tag>Trial {unit.currentTrial ?? 0}/{unit.maxTrials}</Tag></Space><p>评测单元 {unit.unitId} · 任务 {unit.taskId}</p></div><Space><Button icon={<ReloadOutlined />} onClick={() => void resource.reload()}>刷新</Button><Button danger icon={<StopOutlined />} onClick={abort}>安全中止当前任务</Button></Space></header>

      <section className="evaluation-panel evaluation-wizard-summary-new" style={{}}>
        <div>环境 <p>{unit.environmentName}</p></div>
        <div>系统 <p>{unit.systemName} · {unit.systemVersion}</p></div>
        <div>Harness <p>{unit.harnessName}</p></div>
        <div>模型 <p>{unit.modelName}</p></div>
        <div>目标服务 <p>{unit.targetService ?? "—"}</p></div>
        <div style={{minWidth: 300}}>Pod UID<p>{unit.target?.pod}<Tag color={unit.target?.confirmed ? "green" : "gray"}>{unit.target?.confirmed ? "已确认" : "待确认"}</Tag></p></div>
      </section>

      <section className="evaluation-panel">
        <h3>执行阶段</h3>
        <div className="evaluation-stage-track">
          {
            PHASES.map((phase, index) => <div className={`StageTrack-item ${index < currentIndex ? "is-success" : ""}`} key={phase}>
              <div style={{ backgroundColor: index == currentIndex ? "#1d4ed8" : "" }}>{index < currentIndex ? <CheckOutlined /> : index + 1}</div>
              <span style={{ color: index == currentIndex ? "#1d4ed8" : "" }}>{phaseLabel(phase)}</span>
            </div>)
          }
        </div>
        <div className="question-summary">
          <div>当前 Trial<p>{`${unit.currentTrial ?? 0} / ${unit.maxTrials}`}</p></div>
          <div>阶段耗时<p>{formatDuration(phaseDuration)}</p></div>
          <div>题目耗时<p>{formatDuration(unitDuration)}</p></div>
          <div>剩余扰动预算<p>{Math.max(0, unit.disturbanceBudget.total - unit.disturbanceBudget.used)}</p></div>
        </div>
      </section>
      <div style={{display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12}}>
        <Tabs className="question-tabs" defaultActiveKey="overview" items={[
          { key: "overview", label: "故障与扰动", children: overview },
          { key: "evidence", label: "可观测证据", children: <section className="evaluation-panel"><div className="evaluation-summary-grid">{unit.liveMetrics.map((metric) => <MetricCard key={metric.id} label={metric.label} value={metric.value} footer={metric.baseline} />)}</div></section> },
          { key: "events", label: "工具与事件", children: <section className="evaluation-panel"><div className="evaluation-event-list">{events.map((event) => <div className="evaluation-event" key={event.id}><time>{formatTime(event.occurredAt)}</time><span>{event.type} · {event.message}</span></div>)}</div></section> },
          { key: "artifacts", label: "产物", children: <section className="evaluation-panel"><div className="evaluation-option-list">{unit.artifactRefs.map((artifact) => <div className="evaluation-option-row" key={artifact.href}><a href={artifact.href}>{artifact.label}</a></div>)}</div></section> },
        ]} />
        <div>
          <div style={{display: "grid", gridTemplateColumns: "2fr 3fr", gap: 12}}>
            <div>
              <section style={{height: "100%"}} className="evaluation-panel"><h3>状态与门禁</h3><OracleGateList gates={unit.gates} /></section>
            </div>
            <div>
              <section className="evaluation-panel">
                <h3 style={{marginBottom: 12}}>Trial历史</h3>
                <div className="evaluation-option-list">
                  {unit.trials.map((trial) => <div className="evaluation-option-row" key={trial.trialId}>
                    <div>
                      <strong style={{paddingRight: 6}}>Trial {trial.attempt}</strong><Tag color={trial.status === "RUNNING" ? "blue" : trial.outcome === "PASS" ? "green" : "orange"}>{trial.outcome ?? trial.status}</Tag>
                    </div>
                    <div>
                      <span style={{paddingRight: 6}}>{formatDuration(trial.durationSeconds)}</span><span>{trial.cleaned ? "已清理" : "未清理"}</span>
                    </div>
                  </div>)}
                </div>
              </section>

              <section className="evaluation-panel" style={{marginBottom: 0}}>
                <h3 style={{marginBottom: 12}}>当前目标</h3>
                <div className="target-list">
                  <div><p>Pod</p>{unit.target?.pod ?? "—"}</div>
                  <div><p>UID</p>{unit.target?.uid ?? "—"}</div>
                  <div><p>Node</p>{unit.target?.node ?? "—"}</div>
                  <div><p>Container</p>{unit.target?.container ?? "—"}</div>
                </div>
                {unit.target && <Button style={{marginTop: 12}} type="link" onClick={() => navigate(`/environments/infrastructure?pod=${encodeURIComponent(unit.target?.pod ?? "")}`)}>在实验环境中查看</Button>}
              </section>
            </div>
          </div>
          <div className="evaluation-inline-warning" style={{ marginTop: 16 }}>题目执行期间不展示 Ground Truth；最终结果只在恢复、评价和清理完成后由独立 Oracle 生成。</div>
        </div>
      </div>
    </div>
  );
}
