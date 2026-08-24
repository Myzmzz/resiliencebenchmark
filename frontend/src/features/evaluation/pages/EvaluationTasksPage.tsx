import { useCallback, useMemo, useState } from "react";
import { Button, Input, message, Pagination, Progress, Select, Space, Table, Tabs, Tag } from "antd";
import type { ColumnsType } from "antd/es/table";
import { PlusOutlined, ReloadOutlined } from "@ant-design/icons";
import { useNavigate } from "react-router-dom";
import { cancelQueuedTask, listEvaluationTasks } from "../api";
import { formatTime, percent, phaseLabel, terminalLabel } from "../formatters";
import { useAsyncResource } from "../hooks/useAsyncResource";
import type { EvaluationTaskSummary, TaskBusinessStatus } from "../types";
import MetricCard from "../components/MetricCard";
import { PageEmpty, PageError, PageLoading } from "../components/PageState";
import { TaskStatusTag } from "../components/SemanticTag";

const PAGE_SIZE = 10;

export default function EvaluationTasksPage() {
  const navigate = useNavigate();
  const [status, setStatus] = useState<string>("");
  const [environmentId, setEnvironmentId] = useState<string>();
  const [q, setQ] = useState("");
  const [page, setPage] = useState(1);
  const loader = useCallback(
    (signal: AbortSignal) => listEvaluationTasks({ status, environmentId, q, page, pageSize: PAGE_SIZE }, signal),
    [environmentId, page, q, status],
  );
  const resource = useAsyncResource(loader);
  const environments = useMemo(
    () => resource.data?.occupancies.map((item) => ({ value: item.environment.id, label: item.environment.name })) ?? [],
    [resource.data],
  );

  const openTask = (task: EvaluationTaskSummary) => {
    if (task.businessStatus === "RUNNING") navigate(`/evaluation/monitoring/${task.taskId}`);
    else if (task.businessStatus === "COMPLETED") navigate(`/evaluation/results/${task.taskId}`);
  };

  const cancelQueue = async (taskId: string) => {
    try {
      await cancelQueuedTask(taskId);
      message.success("已取消排队");
      await resource.reload();
    } catch (reason) {
      message.error(reason instanceof Error ? reason.message : "取消排队失败");
    }
  };

  const columns: ColumnsType<EvaluationTaskSummary> = [
    {
      title: "任务",
      key: "task",
      width: 230,
      render: (_, item) => <div><a onClick={() => openTask(item)}>{item.name}</a><div className="evaluation-muted">{item.taskId}</div></div>,
    },
    { title: "实验环境", dataIndex: "environmentName", width: 145 },
    {
      title: "被测系统",
      key: "systems",
      width: 210,
      render: (_, item) => <Space size={[4, 4]} wrap>{item.systems.map((system) => <Tag key={system.id}>{system.name} · {system.version}</Tag>)}</Space>,
    },
    {
      title: "Harness / 模型",
      key: "matrix",
      width: 170,
      render: (_, item) => <div>{item.harnessNames.join("、")}<div className="evaluation-muted">{item.harnessNames.length} Harness · {item.modelCount} 模型</div></div>,
    },
    {
      title: "题目 / 单元",
      key: "questions",
      width: 120,
      render: (_, item) => <div>{item.uniqueQuestionCount} 道题<div className="evaluation-muted">{item.evaluationUnitCount} 单元</div></div>,
    },
    {
      title: "状态",
      key: "status",
      width: 150,
      render: (_, item) => <div><TaskStatusTag status={item.businessStatus} /><div className="evaluation-muted">{item.terminalStatus ? terminalLabel(item.terminalStatus) : phaseLabel(item.phase)}{item.queuePosition ? ` · 队列第 ${item.queuePosition} 位` : ""}</div></div>,
    },
    {
      title: "总体进度",
      key: "progress",
      width: 150,
      render: (_, item) => <div>{item.completedUnitCount} / {item.evaluationUnitCount}<Progress size="small" percent={percent(item.completedUnitCount, item.evaluationUnitCount)} showInfo={false} /></div>,
    },
    {
      title: "时间",
      key: "time",
      width: 130,
      render: (_, item) => <div>{item.finishedAt ? "完成" : item.startedAt ? "开始" : "创建"}<div className="evaluation-muted">{formatTime(item.finishedAt ?? item.startedAt ?? item.createdAt)}</div></div>,
    },
    {
      title: "操作",
      key: "actions",
      width: 200,
      render: (_, item) => <Space size="small" wrap>
        {item.businessStatus === "RUNNING" && <Button type="link" onClick={() => navigate(`/evaluation/monitoring/${item.taskId}`)}>运行监控</Button>}
        {item.businessStatus === "PENDING" && item.waitingForTaskId && <Button type="link" onClick={() => navigate(`/evaluation/monitoring/${item.waitingForTaskId}`)}>查看占用</Button>}
        {item.businessStatus === "PENDING" && item.phase === "QUEUED" && <Button type="link" danger onClick={() => void cancelQueue(item.taskId)}>取消排队</Button>}
        {item.businessStatus === "COMPLETED" && <Button type="link" onClick={() => navigate(`/evaluation/results/${item.taskId}`)}>查看结果</Button>}
        {item.businessStatus === "COMPLETED" && <Button type="link" onClick={() => navigate(`/evaluation/results/${item.taskId}?reuse=1`)}>复用</Button>}
      </Space>,
    },
  ];

  return (
    <div className="evaluation-page">
      <header className="evaluation-page-header">
        <div><h2>评测任务</h2><p>创建、排队并跟踪真实韧性评测任务</p></div>
        <Space><Button icon={<ReloadOutlined />} onClick={() => void resource.reload()}>刷新数据</Button><Button type="primary" icon={<PlusOutlined />} onClick={() => navigate("/evaluation/tasks/new")}>新增评测任务</Button></Space>
      </header>
      {resource.loading && !resource.data ? <PageLoading /> : resource.error ? <PageError error={resource.error} onRetry={() => void resource.reload()} /> : resource.data && <>
        <div className="evaluation-summary-grid">
          <MetricCard label="未评测" value={resource.data.summary.pending} />
          <MetricCard label="评测中" value={resource.data.summary.running} tone="primary" />
          <MetricCard label="已完成" value={resource.data.summary.completed} tone="success" />
          <MetricCard label="占用环境" value={`${resource.data.summary.occupiedEnvironments} / ${resource.data.summary.environments}`} />
        </div>
        <section className="evaluation-panel">
          <div className="evaluation-panel-header"><h3>环境占用</h3><Button type="link" onClick={() => navigate("/evaluation/monitoring")}>查看运行监控</Button></div>
          <div className="evaluation-occupancies">{resource.data.occupancies.slice(0, 3).map(({ environment }) => <div className="evaluation-occupancy" key={environment.id}><strong>{environment.name} <Tag color={environment.status === "IDLE" ? "green" : environment.status === "BUSY" ? "blue" : "orange"}>{environment.status === "IDLE" ? "空闲" : environment.status === "BUSY" ? "评测中" : "恢复确认"}</Tag></strong><span>{environment.currentTask ? `${environment.currentTask.name} · ${environment.currentTask.progressPercent}%` : "可启动新任务"}</span></div>)}</div>
        </section>
        <section className="evaluation-panel">
          <div className="evaluation-panel-header"><h3>任务列表</h3><Space wrap><Input.Search allowClear placeholder="搜索任务名称或 ID" onSearch={(value) => { setPage(1); setQ(value); }} /><Select allowClear placeholder="全部环境" style={{ width: 170 }} options={environments} onChange={(value) => { setPage(1); setEnvironmentId(value); }} /></Space></div>
          <Tabs activeKey={status || "ALL"} onChange={(key) => { setPage(1); setStatus(key === "ALL" ? "" : key); }} items={[{ key: "ALL", label: "全部" }, ...(["PENDING", "RUNNING", "COMPLETED"] as TaskBusinessStatus[]).map((key) => ({ key, label: { PENDING: "未评测", RUNNING: "评测中", COMPLETED: "已完成" }[key] }))]} />
          {resource.data.items.length === 0 ? <PageEmpty label="没有符合条件的评测任务" /> : <Table rowKey="taskId" columns={columns} dataSource={resource.data.items} pagination={false} scroll={{ x: 1500 }} />}
          <div className="evaluation-actions"><span className="evaluation-muted">共 {resource.data.total} 个任务</span><Pagination current={page} pageSize={PAGE_SIZE} total={resource.data.total} showSizeChanger={false} onChange={setPage} /></div>
        </section>
        <div className="evaluation-inline-info">同一实验环境同时只允许一个任务运行；等待任务仅在前序任务完成恢复验证后启动。</div>
      </>}
    </div>
  );
}
