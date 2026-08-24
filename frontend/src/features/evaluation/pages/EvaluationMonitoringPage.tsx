import { useCallback } from "react";
import { Button, Progress, Space, Tag } from "antd";
import { ReloadOutlined } from "@ant-design/icons";
import { useNavigate } from "react-router-dom";
import { getMonitoringOverview } from "../api";
import { percent, phaseLabel } from "../formatters";
import { useAsyncResource } from "../hooks/useAsyncResource";
import { PageEmpty, PageError, PageLoading } from "../components/PageState";

export default function EvaluationMonitoringPage() {
  const navigate = useNavigate();
  const loader = useCallback((signal: AbortSignal) => getMonitoringOverview(signal), []);
  const resource = useAsyncResource(loader);
  return (
    <div className="evaluation-page">
      <header className="evaluation-page-header"><div><h2>运行监控</h2><p>按实验环境查看唯一活动评测任务和等待队列</p></div><Button icon={<ReloadOutlined />} onClick={() => void resource.reload()}>刷新数据</Button></header>
      {resource.loading && !resource.data ? <PageLoading /> : resource.error ? <PageError error={resource.error} onRetry={() => void resource.reload()} /> : !resource.data?.environments.length ? <PageEmpty label="暂无实验环境监控数据" /> : <div className="evaluation-three-column">{resource.data.environments.map(({ environment, activeTask, queueSize }) => <section className="evaluation-panel" key={environment.id}>
        <div className="evaluation-panel-header"><h3>{environment.name}</h3><Tag color={environment.status === "IDLE" ? "green" : environment.status === "BUSY" ? "blue" : "orange"}>{environment.status}</Tag></div>
        {!activeTask ? <div className="evaluation-center-state evaluation-muted">无运行任务</div> : <>
          <strong>{activeTask.name}</strong><div className="evaluation-muted">{activeTask.taskId}</div>
          <Progress percent={percent(activeTask.completedUnitCount, activeTask.evaluationUnitCount)} />
          <div className="evaluation-muted">{activeTask.completedUnitCount}/{activeTask.evaluationUnitCount} 单元 · {phaseLabel(activeTask.phase)}</div>
          <Button type="link" style={{ paddingInline: 0 }} onClick={() => navigate(`/evaluation/monitoring/${activeTask.taskId}`)}>进入任务监控</Button>
        </>}
        <div className={queueSize ? "evaluation-inline-warning" : "evaluation-inline-info"}><Space>等待任务 <strong>{queueSize}</strong></Space></div>
      </section>)}</div>}
    </div>
  );
}
