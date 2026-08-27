import { useEffect, useState } from "react";
import { Alert, Button, Descriptions, Drawer, Input, message, Select, Space, Spin, Tag } from "antd";
import { InfoCircleOutlined, CheckCircleFilled,CloseCircleFilled } from "@ant-design/icons";
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
      className="reuse-drawer"
      size="large"
      title={<div>复用评测任务<div className="evaluation-muted">来源任务 {taskId}</div></div>}
      open={open}
      onClose={onClose}
      footer={<div className="evaluation-actions" style={{ margin: 0, border: 0, padding: 0 }}><Button onClick={onClose}>取消</Button><Space><Button onClick={() => navigate(`/evaluation/tasks/new?reuseFrom=${encodeURIComponent(taskId)}`)}>打开向导调整</Button><Button type="primary" loading={submitting} disabled={!validation?.canReuseDirectly || !environmentId || !name.trim()} onClick={() => void create()}>{environment?.status === "BUSY" ? "创建并排队" : "创建复用任务"}</Button></Space></div>}
    >
      <Alert style={{padding: '8px 12px'}} type="info" showIcon title="复用会创建新的任务 ID；原任务、结果和证据保持不变。" description="" />
      {loading ? <div className="evaluation-center-state"><Spin /></div> : error ? 
      <Alert style={{ marginTop: 16 }} type="error" showIcon title="复用配置加载失败" description={error.message} /> 
      : validation && <>
        <section style={{ margin: '16px 0' }}>

          <h3>新任务</h3>
          <Space orientation="vertical" size="middle" style={{ width: "100%", marginTop: 12 }}>
            <label style={{display: "flex"}}>任务名称<div style={{ display: "inline-flex", marginLeft: "8px",  gap: 8, flex: 1 }}><Input value={name} onChange={(event) => setName(event.target.value)} /></div></label>
            <label style={{display: "flex"}}>目标环境
              <div style={{ display: "inline-flex", alignItems: "center", marginLeft: "8px", gap: 8, flex: 1 }}>
                <Select value={environmentId} style={{ flex: 1 }} options={environments.map((item) => ({ value: item.id, label: `${item.name} · ${item.status}` }))} onChange={setEnvironmentId} />
                {environment?.status === "BUSY" &&<Tag color='orange'>环境占用 · 将排队</Tag>}
              </div>
            </label>
          </Space>
          {/* {environment?.status === "BUSY" && <div className="evaluation-inline-warning" style={{ marginTop: 12 }}>环境由 {environment.currentTask?.taskId} 占用，预计进入等待队列第 {environment.queueSize + 1} 位。</div>} */}
        </section>

        <div className="evaluation-compile-formula" style={{padding: '8px 12px', background: '#f8f8fb', borderColor: '#e8ebee', color: '#384664', fontWeight: 400}}>
          {validation.systems.length} 个系统 × {validation.harnesses.length} 个 Harness × {validation.models.length} 组模型引用 × 适用题目 = {validation.evaluationUnitCount} 个评测单元<Tag color='blue'>严格复用配置</Tag>
        </div>

        <section style={{ marginTop: 16, borderTop: '1px solid #cbcdcf', paddingTop: 12 }}><h3>复用配置</h3><Descriptions className="reuse-descriptions" column={1} size="small" style={{ marginTop: 12 }}>
          <Descriptions.Item label="被测系统"><Space wrap>{validation.systems.map((item) => <Tag color={item.available ? "default" : "red"} key={item.id}>{item.label}</Tag>)}</Space></Descriptions.Item>
          <Descriptions.Item label="Harness"><Space wrap>{validation.harnesses.map((item) => <Tag color={item.available ? "default" : "red"} key={item.id}>{item.label}</Tag>)}</Space></Descriptions.Item>
          <Descriptions.Item label="模型"><Space wrap>{validation.models.map((item) => <Tag color={item.available ? "default" : "red"} key={item.id}>{item.label}</Tag>)}</Space></Descriptions.Item>
          <Descriptions.Item label="MCP"><Space wrap>{validation.mcpServers.map((item) => <Tag color={!item.available ? "red" : item.required ? "blue" : "default"} key={item.id}>{item.label}{item.required ? " · 必选" : ""}</Tag>)}</Space></Descriptions.Item>
          <Descriptions.Item label="题目策略">{validation.questionStrategyLabel}</Descriptions.Item><Descriptions.Item label="评分策略">{validation.scoringPolicyLabel}</Descriptions.Item><Descriptions.Item label="评估器">{validation.evaluatorLabel}</Descriptions.Item>
        </Descriptions></section>

        <section style={{ margin: '16px 0', borderTop: '1px solid #cbcdcf', borderBottom: '1px solid #cbcdcf', padding: '12px 0' }}>
          <div className="evaluation-panel-header"><h3>可用性检查</h3></div>
          <div className="evaluation-option-list">
            <h4 style={{color: 'green'}}>检查通过：{validation.checks.filter((item) => item.passed).length}/{validation.checks.length}</h4>
            <div style={{display: 'grid', gridTemplateColumns: 'repeat(2, 1fr)', gap: 8}}>
              {validation.checks.map((item) => <div className="evaluation-option-row" style={{ border: 'none', padding: '2px' }} key={item.id}>
                <span>{!item.passed ? <CloseCircleFilled style={{ color: 'red' }} /> : <CheckCircleFilled style={{ color: '#16A34A' }} />} {item.label}{item.message && <small>{item.message}</small>}</span>
              </div>)}
            </div>
          </div>
          
        </section>

        {environment?.status === "BUSY" && 
          <section style={{ margin: '16px 0' }}>
            <h3 style={{marginBottom: 12}}>环境与队列</h3>
            <Alert style={{padding: '8px 12px'}} type="warning" showIcon title={`${environment.name}当前被 ${environment.currentTask?.taskId} 占用`} description={
              <div style={{display: 'flex', justifyContent: 'space-between'}}>
                <div>
                  <p>当前进度 {environment.currentTask?.progressPercent}%・预计进入等待队列第 {environment.queueSize + 1} 位</p>
                  <p>环境恢复验证通过后，控制器才会为新任务申请租约。</p>
                </div>
                <Button type="link">查看占用任务</Button>
              </div>
            } />
          </section>
        }
        <div className="evaluation-muted"><InfoCircleOutlined style={{ marginRight: 4 }} />复用只复制配置引用，不复制 PASS/FAIL、Trial、Oracle 结论或运行证据。</div>
      </>}
    </Drawer>
  );
}
