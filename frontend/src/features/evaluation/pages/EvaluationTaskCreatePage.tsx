import { useCallback, useEffect, useMemo, useState } from "react";
import { Alert, Button, Checkbox, Descriptions, Input, InputNumber, message, Radio, Select, Space, Steps, Switch, Table, Tabs, Tag } from "antd";
import type { ColumnsType } from "antd/es/table";
import { ArrowLeftOutlined, CheckCircleFilled } from "@ant-design/icons";
import { useNavigate, useSearchParams } from "react-router-dom";
import { compileEvaluation, createEvaluationTask, getEvaluationOptions, getEvaluationTask, saveEvaluationDraft } from "../api";
import { useAsyncResource } from "../hooks/useAsyncResource";
import type {
  CompiledEvaluation,
  EvaluationSelection,
  ExecutionStrategy,
  HarnessSelection,
  QuestionOption,
} from "../types";
import { PageError, PageLoading } from "../components/PageState";

const STEP_ITEMS = ["基础与环境", "系统与 Harness", "题目与策略", "配置预览"].map((title) => ({ title }));

const DEFAULT_STRATEGY: ExecutionStrategy = {
  questionOrder: "FIXED",
  maxTrialsPerUnit: 5,
  retryPerPhase: 1,
  stopOnSafetyViolation: true,
  scoringPolicyId: "",
  evaluatorId: "",
  promptStrategyId: "",
};

export default function EvaluationTaskCreatePage() {
  const navigate = useNavigate();
  const [search] = useSearchParams();
  const reuseFrom = search.get("reuseFrom") ?? undefined;
  const loader = useCallback((signal: AbortSignal) => getEvaluationOptions(signal), []);
  const optionsResource = useAsyncResource(loader);
  const [step, setStep] = useState(0);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [environmentId, setEnvironmentId] = useState("");
  const [systemIds, setSystemIds] = useState<string[]>([]);
  const [harnesses, setHarnesses] = useState<HarnessSelection[]>([]);
  const [mcpServerIds, setMcpServerIds] = useState<string[]>([]);
  const [questionSetId, setQuestionSetId] = useState("");
  const [questionIds, setQuestionIds] = useState<string[]>([]);
  const [strategy, setStrategy] = useState<ExecutionStrategy>(DEFAULT_STRATEGY);
  const [compiled, setCompiled] = useState<CompiledEvaluation>();
  const [compiling, setCompiling] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [draftId, setDraftId] = useState<string>();

  useEffect(() => {
    const options = optionsResource.data;
    if (!options) return;
    queueMicrotask(() => setStrategy((current) => ({
        ...current,
        scoringPolicyId: current.scoringPolicyId || options.scoringPolicies[0]?.id || "",
        evaluatorId: current.evaluatorId || options.evaluators[0]?.id || "",
        promptStrategyId: current.promptStrategyId || options.promptStrategies[0]?.id || "",
      })));
  }, [optionsResource.data]);

  useEffect(() => {
    if (!reuseFrom || !optionsResource.data) return;
    const controller = new AbortController();
    getEvaluationTask(reuseFrom, controller.signal).then((task) => {
      setName(`${task.name}-复用`);
      setDescription(task.description ?? "");
      if (task.selection) {
        setEnvironmentId(task.selection.environmentId);
        setSystemIds(task.selection.systemIds);
        setHarnesses(task.selection.harnesses);
        setMcpServerIds(task.selection.mcpServerIds);
        setQuestionSetId(task.selection.questionSetId);
        setQuestionIds(task.selection.questionIds);
        setStrategy(task.selection.strategy);
      }
    }).catch((reason) => message.error(reason instanceof Error ? reason.message : "无法加载复用配置"));
    return () => controller.abort();
  }, [optionsResource.data, reuseFrom]);

  const options = optionsResource.data;
  const environment = options?.environments.find((item) => item.id === environmentId);
  const requiredMcpIds = useMemo(() => {
    if (!options) return [];
    return [...new Set(harnesses.flatMap((selection) => options.harnesses.find((item) => item.id === selection.harnessId)?.requiredMcpIds ?? []))];
  }, [harnesses, options]);
  const effectiveMcpIds = useMemo(() => [...new Set([...requiredMcpIds, ...mcpServerIds])], [mcpServerIds, requiredMcpIds]);
  const selection: EvaluationSelection = useMemo(() => ({
    environmentId,
    systemIds,
    harnesses,
    mcpServerIds: effectiveMcpIds,
    questionSetId,
    questionIds,
    strategy,
  }), [effectiveMcpIds, environmentId, harnesses, questionIds, questionSetId, strategy, systemIds]);

  const selectQuestionSet = (id: string) => {
    setQuestionSetId(id);
    setQuestionIds(options?.questionSets.find((item) => item.id === id)?.questionIds ?? []);
    setCompiled(undefined);
  };

  const toggleHarness = (id: string, checked: boolean) => {
    if (!options) return;
    if (checked) {
      const harness = options.harnesses.find((item) => item.id === id);
      const firstModel = harness?.modelIds.find((modelId) => options.models.find((model) => model.id === modelId)?.status === "AVAILABLE");
      setHarnesses((current) => [...current, { harnessId: id, modelIds: firstModel ? [firstModel] : [] }]);
    } else {
      setHarnesses((current) => current.filter((item) => item.harnessId !== id));
    }
    setCompiled(undefined);
  };

  const setHarnessModels = (harnessId: string, modelIds: string[]) => {
    setHarnesses((current) => current.map((item) => item.harnessId === harnessId ? { ...item, modelIds } : item));
    setCompiled(undefined);
  };

  const validateStep = (): string | undefined => {
    if (step === 0 && (!name.trim() || !environmentId)) return "请填写任务名称并选择实验环境";
    if (step === 1 && systemIds.length === 0) return "请至少选择一个被测系统";
    if (step === 1 && (harnesses.length === 0 || harnesses.some((item) => item.modelIds.length === 0))) return "请至少选择一个 Harness，并为每个 Harness 选择模型";
    if (step === 1 && requiredMcpIds.some((id) => options?.mcpServers.find((item) => item.id === id)?.status !== "CONNECTED")) return "必选 MCP 未全部连接";
    if (step === 2 && (!questionSetId || questionIds.length === 0)) return "请选择题目集和至少一道题目";
    if (step === 2 && (!strategy.scoringPolicyId || !strategy.evaluatorId || !strategy.promptStrategyId)) return "执行与评分策略不完整";
    return undefined;
  };

  const next = async () => {
    const problem = validateStep();
    if (problem) return void message.warning(problem);
    if (step < 2) {
      setStep((current) => current + 1);
      return;
    }
    if (step === 2) {
      setCompiling(true);
      try {
        const result = await compileEvaluation(selection);
        setCompiled(result);
        if (!result.valid) {
          message.error("题目编译存在阻断错误，请根据问题列表调整配置");
          return;
        }
        setStep(3);
      } catch (reason) {
        message.error(reason instanceof Error ? reason.message : "题目编译失败");
      } finally {
        setCompiling(false);
      }
    }
  };

  const saveDraft = async () => {
    if (!name.trim()) return void message.warning("保存草稿前请填写任务名称");
    try {
      const draft = await saveEvaluationDraft(draftId, { name: name.trim(), description, selection });
      setDraftId(draft.taskId);
      message.success("草稿已保存");
    } catch (reason) {
      message.error(reason instanceof Error ? reason.message : "保存草稿失败");
    }
  };

  const create = async () => {
    if (!compiled?.valid) return void message.warning("请先完成配置编译");
    setSubmitting(true);
    try {
      const created = await createEvaluationTask({ name: name.trim(), description, compileToken: compiled.compileToken, selection, enqueueIfBusy: environment?.status === "BUSY" });
      message.success(environment?.status === "BUSY" ? "任务已创建并进入等待队列" : "任务已创建");
      navigate(created.businessStatus === "RUNNING" ? `/evaluation/monitoring/${created.taskId}` : "/evaluation/tasks");
    } catch (reason) {
      message.error(reason instanceof Error ? reason.message : "创建任务失败");
    } finally {
      setSubmitting(false);
    }
  };

  if (optionsResource.loading && !options) return <div className="evaluation-page"><PageLoading label="正在加载评测配置选项" /></div>;
  if (optionsResource.error || !options) return <div className="evaluation-page"><PageError error={optionsResource.error ?? new Error("评测配置选项为空")} onRetry={() => void optionsResource.reload()} /></div>;

  const questionColumns: ColumnsType<QuestionOption> = [
    { title: "选择", width: 64, render: (_, item) => <Checkbox checked={questionIds.includes(item.id)} disabled={item.status !== "AVAILABLE"} onChange={(event) => setQuestionIds((current) => event.target.checked ? [...current, item.id] : current.filter((id) => id !== item.id))} /> },
    { title: "题目", render: (_, item) => <div><strong>{item.id}</strong><div className="evaluation-muted">{item.title}</div></div> },
    { title: "分类", dataIndex: "category", width: 120 },
    { title: "目标服务", dataIndex: "targetService", width: 120 },
    { title: "Trial 上限", dataIndex: "maxTrials", width: 100 },
    { title: "状态", width: 100, render: (_, item) => <Tag color={item.status === "AVAILABLE" ? "green" : "red"}>{item.status === "AVAILABLE" ? "可用" : "不兼容"}</Tag> },
  ];

  return (
    <div className="evaluation-page">
      <a className="evaluation-back" onClick={() => navigate("/evaluation/tasks")}><ArrowLeftOutlined /> 返回任务列表</a>
      <header className="evaluation-page-header"><div><h2>新增评测任务</h2><p>选择实验环境并冻结本次评测配置</p></div></header>
      <section className="evaluation-panel"><Steps current={step} items={STEP_ITEMS} /><div className="evaluation-inline-info" style={{ marginTop: 16 }}>同一实验环境同时只允许一个任务运行；繁忙环境仍可选择，新任务将进入等待队列。</div></section>

      {step === 0 && <div className="evaluation-two-column">
        <section className="evaluation-panel">
          <h3>任务信息</h3>
          <Space orientation="vertical" size="middle" style={{ width: "100%", marginTop: 16 }}>
            <label>任务名称<Input value={name} maxLength={160} onChange={(event) => setName(event.target.value)} placeholder="输入评测任务名称" /></label>
            <label>任务说明<Input.TextArea value={description} maxLength={1000} showCount rows={3} onChange={(event) => setDescription(event.target.value)} placeholder="说明本次评测目标" /></label>
          </Space>
          <h3 style={{ marginTop: 22 }}>选择实验环境</h3>
          <div className="evaluation-option-list" style={{ marginTop: 12 }}>{options.environments.map((item) => <button type="button" className={`evaluation-option-row ${environmentId === item.id ? "is-selected" : ""}`} key={item.id} onClick={() => { setEnvironmentId(item.id); setCompiled(undefined); }}><div><Radio checked={environmentId === item.id} /> <strong>{item.name}</strong> <Tag color={item.status === "IDLE" ? "green" : item.status === "BUSY" ? "blue" : "orange"}>{item.status}</Tag>{item.status === "BUSY" && environmentId === item.id && <Tag color="gold">将排队</Tag>}<small>{item.currentTask ? `当前任务：${item.currentTask.name}` : "环境可用"}</small></div><div className="evaluation-muted">队列 {item.queueSize} · 检查 {new Date(item.lastCheckedAt).toLocaleTimeString()}</div></button>)}</div>
        </section>
        <section className="evaluation-panel">
          <h3>环境占用</h3>
          {!environment ? <div className="evaluation-center-state evaluation-muted">请选择实验环境</div> : <>
            <Descriptions column={1} bordered size="small" style={{ marginTop: 16 }}>
              <Descriptions.Item label="环境">{environment.name}</Descriptions.Item>
              <Descriptions.Item label="状态"><Tag color={environment.status === "IDLE" ? "green" : "blue"}>{environment.status}</Tag></Descriptions.Item>
              <Descriptions.Item label="当前任务">{environment.currentTask?.name ?? "无"}</Descriptions.Item>
              <Descriptions.Item label="当前进度">{environment.currentTask ? `${environment.currentTask.progressPercent}%` : "—"}</Descriptions.Item>
              <Descriptions.Item label="等待任务">{environment.queueSize}</Descriptions.Item>
            </Descriptions>
            {environment.status === "BUSY" ? <div className="evaluation-inline-warning" style={{ marginTop: 16 }}>创建后进入等待队列；当前任务完成清理并通过环境恢复验证后才会启动。</div> : <div className="evaluation-inline-success" style={{ marginTop: 16 }}>环境空闲，任务创建后可由后端调度启动。</div>}
          </>}
        </section>
      </div>}

      {step === 1 && <div className="evaluation-two-column">
        <section className="evaluation-panel">
          <h3>选择被测系统</h3>
          <Checkbox.Group value={systemIds} onChange={(value) => { setSystemIds(value as string[]); setCompiled(undefined); }} style={{ width: "100%" }}>
            <div className="evaluation-option-list" style={{ marginTop: 12 }}>{options.systems.map((item) => <div className={`evaluation-option-row ${systemIds.includes(item.id) ? "is-selected" : ""}`} key={item.id}><div><Checkbox value={item.id} disabled={item.status === "UNAVAILABLE"} /><strong>{item.name} · {item.version}</strong><small>{item.namespace} · {item.serviceCount} 个服务 · {item.languages.join(" / ")}</small></div><Tag color={item.status === "READY" ? "green" : "default"}>{item.status}</Tag></div>)}</div>
          </Checkbox.Group>
        </section>
        <section className="evaluation-panel">
          <h3>Harness、模型与 MCP</h3>
          <div className="evaluation-option-list" style={{ marginTop: 12 }}>{options.harnesses.map((item) => {
            const selectionItem = harnesses.find((selected) => selected.harnessId === item.id);
            const compatibleModels = options.models.filter((model) => item.modelIds.includes(model.id));
            return <div className={`evaluation-option-row ${selectionItem ? "is-selected" : ""}`} style={{ display: "block" }} key={item.id}><div><Checkbox checked={Boolean(selectionItem)} disabled={item.status === "UNAVAILABLE"} onChange={(event) => toggleHarness(item.id, event.target.checked)} /><strong>{item.name}</strong> <Tag color={item.status === "AVAILABLE" ? "green" : "red"}>{item.status}</Tag><small>{item.description}</small></div>{selectionItem && <div style={{ marginTop: 10 }}><div className="evaluation-muted">选择模型（可多选）</div><Checkbox.Group value={selectionItem.modelIds} onChange={(value) => setHarnessModels(item.id, value as string[])} options={compatibleModels.map((model) => ({ label: model.name, value: model.id, disabled: model.status !== "AVAILABLE" }))} /></div>}</div>;
          })}</div>
          <h3 style={{ marginTop: 20 }}>选择 MCP 服务</h3>
          <div className="evaluation-option-list" style={{ marginTop: 10 }}>{options.mcpServers.map((item) => {
            const required = requiredMcpIds.includes(item.id);
            const checked = effectiveMcpIds.includes(item.id);
            return <div className={`evaluation-option-row ${checked ? "is-selected" : ""}`} key={item.id}><div><Checkbox checked={checked} disabled={required || item.status !== "CONNECTED"} onChange={(event) => setMcpServerIds((current) => event.target.checked ? [...current, item.id] : current.filter((id) => id !== item.id))} /><strong>{item.name}</strong> {required && <Tag color="blue">必选</Tag>}<small>{item.description}</small></div><Tag color={item.status === "CONNECTED" ? "green" : "red"}>{item.status}</Tag></div>;
          })}</div>
        </section>
      </div>}

      {step === 2 && <div className="evaluation-two-column">
        <section className="evaluation-panel">
          <div className="evaluation-panel-header"><h3>选择评测题目</h3><Select placeholder="选择题目集" value={questionSetId || undefined} style={{ width: 280 }} options={options.questionSets.map((item) => ({ value: item.id, label: `${item.name} · ${item.version}` }))} onChange={selectQuestionSet} /></div>
          <Table rowKey="id" size="small" columns={questionColumns} dataSource={options.questions.filter((item) => !questionSetId || options.questionSets.find((set) => set.id === questionSetId)?.questionIds.includes(item.id))} pagination={{ pageSize: 8 }} />
        </section>
        <section className="evaluation-panel">
          <h3>执行与评分策略</h3>
          <div className="evaluation-form-grid" style={{ marginTop: 16 }}>
            <label>题目顺序<Select value={strategy.questionOrder} options={[{ value: "FIXED", label: "固定顺序" }, { value: "RANDOM_SEEDED", label: "固定种子随机" }]} onChange={(value) => setStrategy((current) => ({ ...current, questionOrder: value }))} /></label>
            <label>每单元最大 Trial<InputNumber min={1} max={20} value={strategy.maxTrialsPerUnit} onChange={(value) => setStrategy((current) => ({ ...current, maxTrialsPerUnit: value ?? 1 }))} /></label>
            <label>阶段重试次数<InputNumber min={0} max={5} value={strategy.retryPerPhase} onChange={(value) => setStrategy((current) => ({ ...current, retryPerPhase: value ?? 0 }))} /></label>
            <label>Prompt / Strategy<Select value={strategy.promptStrategyId || undefined} options={options.promptStrategies.map((item) => ({ value: item.id, label: item.name }))} onChange={(value) => setStrategy((current) => ({ ...current, promptStrategyId: value }))} /></label>
            <label>评分规则<Select value={strategy.scoringPolicyId || undefined} options={options.scoringPolicies.map((item) => ({ value: item.id, label: item.name }))} onChange={(value) => setStrategy((current) => ({ ...current, scoringPolicyId: value }))} /></label>
            <label>评估器<Select value={strategy.evaluatorId || undefined} options={options.evaluators.map((item) => ({ value: item.id, label: item.name }))} onChange={(value) => setStrategy((current) => ({ ...current, evaluatorId: value }))} /></label>
          </div>
          <div style={{ marginTop: 18 }}><Switch checked={strategy.stopOnSafetyViolation} onChange={(value) => setStrategy((current) => ({ ...current, stopOnSafetyViolation: value }))} /> 安全违规立即停止</div>
          <div className="evaluation-inline-info" style={{ marginTop: 18 }}>总体进度以编译后的评测单元计算；Trial 与重试不会重复计入题目总数。</div>
        </section>
      </div>}

      {step === 3 && compiled && <div className="evaluation-two-column">
        <section className="evaluation-panel">
          <div className="evaluation-panel-header"><h3>评测配置预览</h3><Tag color="blue">配置已编译</Tag></div>
          <Tabs defaultActiveKey="matrix" items={[
            { key: "matrix", label: "评测矩阵", children: <><div className="evaluation-compile-formula">{compiled.systemsCount} 个系统 × {compiled.harnessesCount} 个 Harness × {compiled.modelConfigurationsCount} 组 Harness-模型配置 × 适用题目 = {compiled.evaluationUnitCount} 个评测单元 <Tag>最大 Trial {compiled.maxTrialCount}</Tag></div><div className="evaluation-compile-grid" style={{ marginTop: 16, gridTemplateColumns: `1fr repeat(${new Set(compiled.matrix.map((item) => item.modelId)).size}, minmax(100px, 1fr))` }}><div className="head">系统 / Harness</div>{[...new Set(compiled.matrix.map((item) => item.modelId))].map((modelId) => <div className="head" key={modelId}>{options.models.find((item) => item.id === modelId)?.name ?? modelId}</div>)}{[...new Set(compiled.matrix.map((item) => `${item.systemId}::${item.harnessId}`))].flatMap((key) => { const [systemId, harnessId] = key.split("::"); const models = [...new Set(compiled.matrix.map((item) => item.modelId))]; return [<div key={`${key}-label`}><strong>{options.systems.find((item) => item.id === systemId)?.name}</strong><div className="evaluation-muted">{options.harnesses.find((item) => item.id === harnessId)?.name}</div></div>, ...models.map((modelId) => { const row = compiled.matrix.find((item) => item.systemId === systemId && item.harnessId === harnessId && item.modelId === modelId); return <div key={`${key}-${modelId}`}>{row ? `${row.unitCount} 单元` : "—"}</div>; })]; })}</div></> },
            { key: "details", label: "配置详情", children: <Descriptions column={2} bordered size="small"><Descriptions.Item label="目标环境">{environment?.name}</Descriptions.Item><Descriptions.Item label="创建方式">{environment?.status === "BUSY" ? "创建并排队" : "创建任务"}</Descriptions.Item><Descriptions.Item label="被测系统">{systemIds.map((id) => options.systems.find((item) => item.id === id)?.name).join("、")}</Descriptions.Item><Descriptions.Item label="Harness">{harnesses.map((item) => options.harnesses.find((option) => option.id === item.harnessId)?.name).join("、")}</Descriptions.Item><Descriptions.Item label="共享 MCP">{compiled.sharedMcpServerIds.join("、")}</Descriptions.Item><Descriptions.Item label="配置时间">{new Date(compiled.generatedAt).toLocaleString()}</Descriptions.Item></Descriptions> },
            { key: "questions", label: "题目清单", children: <Table size="small" rowKey="id" columns={questionColumns.slice(1)} dataSource={options.questions.filter((item) => questionIds.includes(item.id))} pagination={{ pageSize: 6 }} /> },
            { key: "strategy", label: "策略快照", children: <Descriptions column={1} bordered size="small"><Descriptions.Item label="题目顺序">{strategy.questionOrder}</Descriptions.Item><Descriptions.Item label="每单元最大 Trial">{strategy.maxTrialsPerUnit}</Descriptions.Item><Descriptions.Item label="阶段重试次数">{strategy.retryPerPhase}</Descriptions.Item><Descriptions.Item label="安全违规立即停止">{strategy.stopOnSafetyViolation ? "是" : "否"}</Descriptions.Item><Descriptions.Item label="评分规则">{strategy.scoringPolicyId}</Descriptions.Item><Descriptions.Item label="评估器">{strategy.evaluatorId}</Descriptions.Item></Descriptions> },
          ]} />
          {compiled.issues.length > 0 && <Alert style={{ marginTop: 16 }} type={compiled.issues.some((item) => item.severity === "ERROR") ? "error" : "warning"} showIcon title="编译问题" description={compiled.issues.map((item) => <div key={item.code}>{item.code}：{item.message}</div>)} />}
        </section>
        <section className="evaluation-panel">
          <h3>创建前校验</h3>
          <div className="evaluation-option-list" style={{ marginTop: 16 }}>{["实验环境与系统快照已选择", `${harnesses.length} 个 Harness 已选择`, `${compiled.modelConfigurationsCount} 组 Harness-模型配置`, `${requiredMcpIds.length} 个必选 MCP 已包含`, `${compiled.evaluationUnitCount} 个评测单元已编译`, "评分与评估器已冻结"].map((label) => <div className="evaluation-option-row" key={label}><span><CheckCircleFilled style={{ color: "#16A34A" }} /> {label}</span></div>)}</div>
          {environment?.status === "BUSY" ? <div className="evaluation-inline-warning" style={{ marginTop: 16 }}>环境被 {environment.currentTask?.taskId} 占用，新任务将进入等待队列第 {environment.queueSize + 1} 位。</div> : <div className="evaluation-inline-success" style={{ marginTop: 16 }}>环境空闲，可以创建任务。</div>}
          <div className="evaluation-muted" style={{ marginTop: 14 }}>创建任务不会立即在浏览器中修改实验环境；运行与租约状态由真实 API 返回。</div>
        </section>
      </div>}

      <div className="evaluation-actions"><Button onClick={() => navigate("/evaluation/tasks")}>取消</Button><Space><Button disabled={step === 0} onClick={() => setStep((current) => current - 1)}>上一步</Button><Button onClick={() => void saveDraft()}>保存草稿</Button>{step < 3 ? <Button type="primary" loading={compiling} onClick={() => void next()}>下一步：{STEP_ITEMS[step + 1]?.title}</Button> : <Button type="primary" loading={submitting} disabled={!compiled?.valid} onClick={() => void create()}>{environment?.status === "BUSY" ? "创建并排队" : "创建任务"}</Button>}</Space></div>
    </div>
  );
}
