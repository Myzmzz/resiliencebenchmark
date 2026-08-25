import { useEffect, useState } from "react";
import { Alert, Button, Descriptions, Drawer, Input, message, Select, Space, Spin, Tag } from "antd";
import { useNavigate } from "react-router-dom";
import { getEvaluationOptions, reuseEvaluationTask, validateReuse } from "../api";
import type { EnvironmentOption, ReuseValidation } from "../types";

export default function ReuseTaskDrawer({ taskId, sourceName, open, onClose }: {
  taskId: string;
  sourceName: string;
  open: boolean;
  onClose: () => void;
}) {
  const navigate = useNavigate();
  const [validation, setValidation] = useState<ReuseValidation>();
  const [environments, setEnvironments] = useState<EnvironmentOption[]>([]);
  const [name, setName] = useState(`${sourceName}-复用`);
  const [environmentId, setEnvironmentId] = useState<string>();
  const [loading, setLoading] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<Error>();

  useEffect(() => {
    if (!open) return;
    const controller = new AbortController();
    queueMicrotask(() => {
      if (controller.signal.aborted) return;
      setLoading(true);
      setError(undefined);
      setName(`${sourceName}-复用`);
    });
    Promise.all([validateReuse(taskId, controller.signal), getEvaluationOptions(controller.signal)])
      .then(([reuse, options]) => {
        setValidation(reuse);
        setEnvironments(options.environments);
        setEnvironmentId(options.environments[0]?.id);
      })
      .catch((reason) => { if (!controller.signal.aborted) setError(reason instanceof Error ? reason : new Error(String(reason))); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [open, sourceName, taskId]);

  const environment = environments.find((item) => item.id === environmentId);
  const create = async () => {
    if (!environmentId || !name.trim() || !validation?.canReuseDirectly) return;
    setSubmitting(true);
    try {
      const task = await reuseEvaluationTask(taskId, { name: name.trim(), environmentId, enqueueIfBusy: environment?.status === "BUSY" });
      message.success(environment?.status === "BUSY" ? "复用任务已创建并排队" : "复用任务已创建");
      onClose();
      navigate(task.businessStatus === "RUNNING" ? `/evaluation/monitoring/${task.taskId}` : "/evaluation/tasks");
    } catch (reason) {
      message.error(reason instanceof Error ? reason.message : "复用任务创建失败");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Drawer
      size="large"
      title={<div>复用评测任务<div className="evaluation-muted">来源任务 {taskId}</div></div>}
      open={open}
      onClose={onClose}
      footer={<div className="evaluation-actions" style={{ margin: 0, border: 0, padding: 0 }}><Button onClick={onClose}>取消</Button><Space><Button onClick={() => navigate(`/evaluation/tasks/new?reuseFrom=${encodeURIComponent(taskId)}`)}>打开向导调整</Button><Button type="primary" loading={submitting} disabled={!validation?.canReuseDirectly || !environmentId || !name.trim()} onClick={() => void create()}>{environment?.status === "BUSY" ? "创建并排队" : "创建复用任务"}</Button></Space></div>}
    >
      <Alert type="info" showIcon title="复用会创建新的任务 ID" description="原任务、结果和证据保持不变。" />
      {loading ? <div className="evaluation-center-state"><Spin /></div> : error ? <Alert style={{ marginTop: 16 }} type="error" showIcon title="复用配置加载失败" description={error.message} /> : validation && <>
        <section className="evaluation-panel" style={{ marginTop: 16 }}><h3>新任务</h3><Space orientation="vertical" size="middle" style={{ width: "100%", marginTop: 12 }}><label>任务名称<Input value={name} onChange={(event) => setName(event.target.value)} /></label><label>目标环境<Select value={environmentId} style={{ width: "100%" }} options={environments.map((item) => ({ value: item.id, label: `${item.name} · ${item.status}` }))} onChange={setEnvironmentId} /></label></Space>{environment?.status === "BUSY" && <div className="evaluation-inline-warning" style={{ marginTop: 12 }}>环境由 {environment.currentTask?.taskId} 占用，预计进入等待队列第 {environment.queueSize + 1} 位。</div>}</section>
        <div className="evaluation-compile-formula">{validation.systems.length} 个系统 × {validation.harnesses.length} 个 Harness × {validation.models.length} 组模型引用 × 适用题目 = {validation.evaluationUnitCount} 个评测单元<Tag>严格复用配置</Tag></div>
        <section className="evaluation-panel" style={{ marginTop: 16 }}><h3>复用配置</h3><Descriptions column={1} size="small" style={{ marginTop: 12 }}>
          <Descriptions.Item label="被测系统"><Space wrap>{validation.systems.map((item) => <Tag color={item.available ? "default" : "red"} key={item.id}>{item.label}</Tag>)}</Space></Descriptions.Item>
          <Descriptions.Item label="Harness"><Space wrap>{validation.harnesses.map((item) => <Tag color={item.available ? "default" : "red"} key={item.id}>{item.label}</Tag>)}</Space></Descriptions.Item>
          <Descriptions.Item label="模型"><Space wrap>{validation.models.map((item) => <Tag color={item.available ? "default" : "red"} key={item.id}>{item.label}</Tag>)}</Space></Descriptions.Item>
          <Descriptions.Item label="MCP"><Space wrap>{validation.mcpServers.map((item) => <Tag color={!item.available ? "red" : item.required ? "blue" : "default"} key={item.id}>{item.label}{item.required ? " · 必选" : ""}</Tag>)}</Space></Descriptions.Item>
          <Descriptions.Item label="题目策略">{validation.questionStrategyLabel}</Descriptions.Item><Descriptions.Item label="评分策略">{validation.scoringPolicyLabel}</Descriptions.Item><Descriptions.Item label="评估器">{validation.evaluatorLabel}</Descriptions.Item>
        </Descriptions></section>
        <section className="evaluation-panel"><div className="evaluation-panel-header"><h3>可用性检查</h3><Tag color={validation.canReuseDirectly ? "green" : "red"}>{validation.checks.filter((item) => item.passed).length}/{validation.checks.length}</Tag></div><div className="evaluation-option-list">{validation.checks.map((check) => <div className="evaluation-option-row" key={check.id}><span>{check.passed ? "✓" : "×"} {check.label}</span>{check.message && <small>{check.message}</small>}</div>)}</div></section>
        <div className="evaluation-muted">复用只复制配置引用，不复制 PASS/FAIL、Trial、Oracle 结论或运行证据。</div>
      </>}
    </Drawer>
  );
}
