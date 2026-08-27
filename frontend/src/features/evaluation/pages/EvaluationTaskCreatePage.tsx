import { useCallback, useEffect, useMemo, useState } from "react";
import { Alert, Button, Checkbox, Descriptions, Input, InputNumber, message, Radio, Select, Space, Steps, Switch, Table, Tabs, Tag, Progress } from "antd";
import type { ColumnsType } from "antd/es/table";
import { ArrowLeftOutlined, CheckCircleFilled, InfoCircleTwoTone, FolderFilled, SearchOutlined, CloseOutlined, CheckCircleOutlined, LockFilled } from "@ant-design/icons";
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
  const [modelList, setModelList] = useState([
    { id: "gpt-5.6", name: "GPT-5.6", status: "AVAILABLE" },
    { id: "gpt-5.5", name: "GPT-5.5", status: "AVAILABLE" },
    { id: "gpt-5.4", name: "GPT-5.4", status: "AVAILABLE" },
    { id: "qwen3.8-max", name: "QWEN3.8-MAX", status: "AVAILABLE" },
    { id: "deepseek-v4", name: "DEEPSEEK-V4", status: "AVAILABLE" },
    { id: "llama4", name: "LLAMA4", status: "AVAILABLE" },
    { id: "claude-3.7-sonnet", name: "CLAUDE-3.7-SONNET", status: "AVAILABLE" },
    { id: "claude-3.6-opus", name: "CLAUDE-3.6-OPUS", status: "AVAILABLE" },
  ]);

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

  const renderBlueBlock = (count: number) => (
      <div style={{ display: "flex", gap: "4px" }}>
          {[...Array(count)].map((_, i) => (
              <div key={i} style={{ width: "16px", height: "12px", backgroundColor: "#2b74d9", borderRadius: 2 }} />
          ))}
      </div>
  );

  return (
    <div className="evaluation-page">
      <a className="evaluation-back" onClick={() => navigate("/evaluation/tasks")}><ArrowLeftOutlined /> 返回任务列表</a>
      <header className="evaluation-page-header"><div><h2>新增评测任务</h2><p>选择实验环境并冻结本次评测配置</p></div></header>
      <section className="evaluation-panel" style={{ padding: 0 }}>
        <Steps style={{ padding: "20px 100px 0 100px" }} current={step} items={STEP_ITEMS} />
        {step === 0 ?
          <div className="evaluation-info" style={{ marginTop: 16, padding: 12, background: "#f1f5fe" }}><InfoCircleTwoTone style={{ marginRight: 8 }} />同一实验环境同时只允许一个任务运行；繁忙环境仍可选择，新任务将进入等待队列。</div>
          :
          <div className="evaluation-info-container">
            <div className="evaluation-info-container-list">
              <div><InfoCircleTwoTone style={{ marginRight: 8 }}/>{name}</div>
              <div><InfoCircleTwoTone style={{ marginRight: 8 }}/>{options.environments.find((item) => item.id === environmentId)?.name ?? "未选择"}</div>
              <div>
                {options.environments.find((item) => item.id === environmentId)?.status === "BUSY" && <Tag color="#fef6ea" style={{ color: '#f59e0b' }}><InfoCircleTwoTone twoToneColor="#f59e0b" style={{ marginRight: 4 }} />环境占用 · 将排队</Tag>}
              </div>
            </div>
            <Button type="link" onClick={() => setStep((current) => current - 1)}>返回修改环境</Button>
          </div>
        }
      </section>

      {step === 0 && <div className="evaluation-two-column">
        <section className="evaluation-panel">
          <h3>任务信息</h3>
          <Space orientation="vertical" size="middle" style={{ width: "100%", marginTop: 16 }}>
            <label style={{ display: "flex" }}><div style={{ width: 100 }}>任务名称</div><Input value={name} maxLength={160} onChange={(event) => setName(event.target.value)} placeholder="输入评测任务名称" /></label>
            <label style={{ display: "flex" }}><div style={{ width: 100 }}>任务说明</div><Input.TextArea value={description} maxLength={1000} showCount rows={3} onChange={(event) => setDescription(event.target.value)} placeholder="说明本次评测目标" /></label>
          </Space>
          <h3 style={{ marginTop: 22 }}>选择实验环境</h3>
          <div className="evaluation-option-list" style={{ marginTop: 12 }}>
            {options.environments.map((item) => <button type="button" className={`evaluation-option-row ${environmentId === item.id ? "is-selected" : ""}`} key={item.id} onClick={() => { setEnvironmentId(item.id); setCompiled(undefined); }}>
              <div style={{ display: "flex", alignItems: "center" }}>
                <Radio checked={environmentId === item.id} />
                <div style={{ marginLeft: 8, textAlign: "left" }}>
                  <Space>
                    <strong>{item.name}</strong>
                    <Tag color={item.status === "IDLE" ? "green" : item.status === "BUSY" ? "blue" : "orange"}>{item.status}</Tag>
                    {item.status === "BUSY" && environmentId === item.id && <Tag color="gold">将排队</Tag>}
                  </Space>
                  <small style={{ textAlign: "left" }}>{item.currentTask ? `当前任务：${item.currentTask.name}` : "环境可用"}</small>
                </div>
              </div>
              {item.currentTask ?
                <div className="evaluation-muted-progress">
                  <p>总体进度 {item.currentTask?.progressPercent}%</p>
                  <Progress size="small" percent={item.currentTask?.progressPercent} showInfo={false} />
                </div>
                :
                <div className="evaluation-muted" style={{ width: 180, textAlign: "left" }}>队列 {item.queueSize} · 检查 {new Date(item.lastCheckedAt).toLocaleTimeString()}</div>
              }
            </button>)}
          </div>
          <div className="evaluation-muted-warning">选择被占用环境并不会并启动任务，最终创建操作将变为 “创建并排队”。</div>
        </section>
        <section className="evaluation-panel">
          <h3>环境占用</h3>
          {!environment ? <div className="evaluation-center-state evaluation-muted">请选择实验环境</div> : <>
            {/* <Descriptions column={1} bordered size="small" style={{ marginTop: 16 }}>
              <Descriptions.Item label="环境">{environment.name}</Descriptions.Item>
              <Descriptions.Item label="状态"><Tag color={environment.status === "IDLE" ? "green" : "blue"}>{environment.status}</Tag></Descriptions.Item>
              <Descriptions.Item label="当前任务">{environment.currentTask?.name ?? "无"}</Descriptions.Item>
              <Descriptions.Item label="当前进度">{environment.currentTask ? `${environment.currentTask.progressPercent}%` : "—"}</Descriptions.Item>
              <Descriptions.Item label="等待任务">{environment.queueSize}</Descriptions.Item>
            </Descriptions> */}
            <div style={{display:'flex', gap: 6, marginTop: 20}}>
              <Tag color={environment.status === "IDLE" ? "green" : "blue"}>{environment.status}</Tag>
              <h4>{environment.name}</h4>
            </div>
            <div className="system-snapshot" style={{marginTop: 10}}>
              <div className="system-snapshot-item" style={{marginTop: 0}}>
                <p>占用任务</p>
                <div>EVAL‑20260824‑001</div>
              </div>
              <div className="system-snapshot-item">
                <p>任务名称</p>
                <div>OTel Demo 全量韧性评测</div>
              </div>
              <div className="system-snapshot-item">
                <p>被测系统</p>
                <div>OTel Demo · v1.12.0</div>
              </div>
              <div className="system-snapshot-item">
                <p>当前题目</p>
                <div>第 8 / 13 题</div>
              </div>
              <div className="system-snapshot-item">
                <p>当前阶段</p>
                <div>执行阶段</div>
              </div>
              <div className="system-snapshot-item">
                <p>租赁心跳</p>
                <div>10:48:50 · <span style={{color: 'green'}}>可用</span></div>
              </div>
            </div>
            {
              environment.status === "BUSY" && <>
                <h4 style={{marginTop: 20}}>等待队列</h4>
                <Descriptions className="step1-queue" column={1} bordered size="small" style={{ marginTop: 12 }}>
                  <Descriptions.Item label="1">Train Ticket 回归评测（当前草稿）</Descriptions.Item>
                  <Descriptions.Item label="2">OTel Demo 模型对比评测</Descriptions.Item>
                </Descriptions>
              </>
            }
            {environment.status === "BUSY" ? 
            <>
              <div className="evaluation-inline-warning" style={{ marginTop: 16 }}>当前任务完成清理并通过环境恢复验证后，等待任务才可启动。</div>
              <div style={{fontSize:14, marginTop: 12, color: '#1677ff', cursor: 'pointer'}}>查看占用任务</div>
            </>
            : <div className="evaluation-inline-success" style={{ marginTop: 16 }}>环境空闲，任务创建后可由后端调度启动。</div>}
          </>}
        </section>
      </div>}

      {step === 1 && <div className="evaluation-two-column" style={{ gridTemplateColumns: "minmax(394px, 1fr) minmax(0, 2fr)" }}>
        <section className="evaluation-panel">
          <h3>选择被测系统</h3>
          <Checkbox.Group value={systemIds} onChange={(value) => { setSystemIds(value as string[]); setCompiled(undefined); }} style={{ width: "100%" }}>
            <div className="evaluation-option-list" style={{ marginTop: 12 }}>
              {options.systems.map((item) => <div className={`evaluation-option-row ${systemIds.includes(item.id) ? "is-selected" : ""}`} key={item.id}>
                <div style={{ display: "flex", alignItems: "center" }}>
                  <Checkbox value={item.id} disabled={item.status === "UNAVAILABLE"} />
                  <div style={{ marginLeft: 12 }}>
                    <strong>{item.name} · {item.version}</strong>
                    <Tag color={item.status === "READY" ? "green" : "default"}>{item.status}</Tag>
                    <small>{item.namespace} · {item.serviceCount} 个服务 · {item.languages.join(" / ")}</small>
                  </div>
                </div>
              </div>)}
            </div>
          </Checkbox.Group>
          <div className="system-snapshot">
            <h3>系统快照</h3>
            <div className="system-snapshot-item">
              <p>源码 Commit</p>
              <div>5f7c21d</div>
            </div>
            <div className="system-snapshot-item">
              <p>镜像锁定</p>
              <div>51 / 51</div>
            </div>
            <div className="system-snapshot-item">
              <p>CodeGraph</p>
              <div><Tag color="green">可用</Tag></div>
            </div>
            <div className="system-snapshot-item">
              <p>运行状态</p>
              <div>已部署 · 待激活</div>
            </div>
            <div className="system-snapshot-item">
              <p>最近同步</p>
              <div>10:45:28</div>
            </div>
          </div>
          <div className="system-snapshot-warning"><InfoCircleTwoTone style={{ marginRight: 4, color: "green" }} />系统版本、镜像 Digest 与源码 Commit 将随任务冻结</div>
        </section>
        <section className="evaluation-panel">
          <div className="evaluation-panel-title" style={{ marginTop: 0 }}>
            <h3>Harness配置</h3>
            <span>选择 Harness</span>
          </div>
          <div className="evaluation-option-list" style={{ marginTop: 12, display: "grid", gridTemplateColumns: "repeat(3, 1fr)" }}>{options.harnesses.map((item) => {
            const selectionItem = harnesses.find((selected) => selected.harnessId === item.id);
            const compatibleModels = options.models.filter((model) => item.modelIds.includes(model.id));
            return <div className={`evaluation-option-row ${selectionItem ? "is-selected" : ""}`} style={{ display: "flex", justifyContent: "flex-start", padding: 6 }} key={item.id}>
              <Checkbox checked={Boolean(selectionItem)} disabled={item.status === "UNAVAILABLE"} onChange={(event) => toggleHarness(item.id, event.target.checked)} />
              <strong title={item.description}>{item.name}</strong>
              <Tag color={item.status === "AVAILABLE" ? "green" : "red"}>{item.status === "AVAILABLE" ? "可用" : "不可用"}</Tag>
              {/* <small>{item.description}</small> */}
              {/* {selectionItem && <div style={{ marginTop: 10 }}>
                <div className="evaluation-muted">选择模型（可多选）</div><Checkbox.Group value={selectionItem.modelIds} onChange={(value) => setHarnessModels(item.id, value as string[])} options={compatibleModels.map((model) => ({ label: model.name, value: model.id, disabled: model.status !== "AVAILABLE" }))} />
              </div>} */}
            </div>;
          })}</div>
          <div className="models-panel">
            <div className="models-panel-item">
              <div className="evaluation-panel-title">
                <h3>选择模型</h3>
                <span>可选择多个模型，每个模型将独立执行全部题目</span>
              </div>
              <div className="models-list-container">
                <Input allowClear placeholder="搜索模型" prefix={<SearchOutlined />} style={{ border: 'none' }} />
                <div className="models-list" style={{ padding: 8, borderTop: '1px solid #e2e8f0' }}>
                  {
                    modelList.map((item) => {
                      return <div className="models-list-item" key={item.id}><Checkbox disabled={item.status !== "AVAILABLE"} style={{ marginRight: 8 }} />{item.name} <Tag color={item.status === "AVAILABLE" ? "green" : "red"}>{item.status === "AVAILABLE" ? "可用" : "不可用"}</Tag></div>
                    })
                  }
                </div>
              </div>
            </div>
            <div className="models-panel-item">
              <div className="models-panel-item-title">已选择3个模型</div>
              <div className="models-panel-item-model">
                <div className="model-name"><span>GTP-5.6</span><CloseOutlined style={{ cursor: 'pointer' }} /></div>
              </div>
              <div style={{ display: 'flex', justifyContent: 'space-between' }}>
                <div className="evaluation-option-item" style={{ width: '49%' }}>
                  <p>运行轨迹</p>
                  <Select placeholder="选择运行轨迹" />
                </div>
                <div className="evaluation-option-item" style={{ width: '49%' }}>
                  <p>Prompt/Strategy</p>
                  <Select placeholder="选择运行轨迹" />
                </div>
              </div>
              <div className="evaluation-option-item">
                <p>评估器</p>
                <Select placeholder="选择运行轨迹" />
              </div>
            </div>
          </div>
          <div className="mcp-panel">
            <div>
              <div className="evaluation-panel-title">
                <h3>选择 MCP 服务</h3>
                <span>必选服务由 Harness 与评测契约决定，不可取消</span>
              </div>
              <div className="evaluation-option-list" style={{ marginTop: 10, display: "grid", gridTemplateColumns: "repeat(2, 1fr)" }}>{options.mcpServers.map((item) => {
                const required = requiredMcpIds.includes(item.id);
                const checked = effectiveMcpIds.includes(item.id);
                return <div className={`evaluation-option-row`} key={item.id} style={{ padding: 6 }}>
                  <div style={{ display: 'flex', justifyContent: 'flex-start', gap: 4 }}>
                    <Checkbox checked={checked} disabled={required || item.status !== "CONNECTED"} onChange={(event) => setMcpServerIds((current) => event.target.checked ? [...current, item.id] : current.filter((id) => id !== item.id))} />
                    <span style={{ paddingLeft: 4 }}>{item.name}</span>

                    {/* <small>{item.description}</small> */}
                  </div>
                  {required && <div><Tag color="blue">必选</Tag> <LockFilled style={{ color: 'gray' }} /></div>}
                  {
                    !required && <Tag color={checked ? "green" : "gray"}>{checked ? "已选" : "可选"}</Tag>
                  }
                  {/* {
                    !required && <Tag color={item.status === "CONNECTED" ? "green" : "red"}>{item.status === "CONNECTED" ? "可用" : "不可用"}</Tag>
                  } */}
                </div>;
              })}</div>
            </div>
            <div className="mcp-panel-check">
              <div className="mcp-panel-check-title">
                <h4>兼容性检查</h4>
                <span style={{ color: 'green', paddingLeft: 12 }}>检查通过 5/5</span>
              </div>
              <div>
                <CheckCircleOutlined />
                <p>3/3 模型与 Codex Harness 兼容</p>
              </div>
              <div>
                <CheckCircleOutlined />
                <p>2/2 必选 MCP 已连接</p>
              </div>
              <div>
                <CheckCircleOutlined />
                <p>2个可选 MCP 已连接</p>
              </div>
              <div>
                <CheckCircleOutlined />
                <p>Prompt 与 Harness 协议兼容</p>
              </div>
              <div>
                <CheckCircleOutlined />
                <p>系统源码与运行快照可关联</p>
              </div>
            </div>
          </div>
          <div className="evaluation-inline-info" style={{ display: 'flex', justifyContent: 'space-between', marginTop: 18 }}>
            <span>本步骤冻结：1 个 Harness × 3 个模型 × 4 个 MCP 服务</span>
            <p style={{ fontSize: 12, color: 'gray' }}>ⓘ MCP服务作为共享工具配置，不增加评测单元数量。</p>
          </div>
        </section>
      </div>}

      {step === 2 && <div className="evaluation-two-column" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(400px, 1fr))" }}>
        <section className="evaluation-panel" style={{ height: '100%' }}>
          <div className="evaluation-panel-header" style={{ display: 'block' }}>
            <h3>选择评测题目</h3>
            {/* <Select placeholder="选择题目集" value={questionSetId || undefined} style={{ width: 280 }} options={options.questionSets.map((item) => ({ value: item.id, label: `${item.name} · ${item.version}` }))} onChange={selectQuestionSet} /> */}
          </div>
          <Tabs className="questions-tabs" items={[{ key: "1", label: "按题目集" }, { key: "2", label: "自定义选择" }]} />
          <div className="evaluation-option-list" style={{ marginTop: 12 }}>
            {options.questionSets.map((item) => <div><button type="button" className={`evaluation-option-row ${questionSetId === item.id ? "is-selected" : ""}`} key={item.id} onClick={() => { selectQuestionSet(item.id); }} style={{ borderBottomRightRadius: 0, borderBottomLeftRadius: 0, width: '100%' }}>
              <div style={{ display: "flex", alignItems: "center" }}>
                <Radio checked={questionSetId === item.id} />
                <div style={{ marginLeft: 8, textAlign: "left" }}>
                  <Space>
                    <strong>{item.name} · {item.version}</strong>
                    <Tag color='green'>兼容</Tag>
                  </Space>
                  <div className="evaluation-muted" style={{ fontSize: "14px" }}>{item.questionIds.length}道正式题目</div>
                </div>
              </div>
              <div className="evaluation-muted" style={{ fontSize: "14px" }}>已选择13/13</div>
            </button>
              <div className="questions-summary">
                <div>网络 3</div>
                <div>服务异常 3</div>
                <div>资源 3</div>
                <div>恢复与安全 3</div>
              </div>
            </div>)}
          </div>

          <Table className="questions-table" rowKey="id" size="small" columns={questionColumns} dataSource={options.questions.filter((item) => !questionSetId || options.questionSets.find((set) => set.id === questionSetId)?.questionIds.includes(item.id))} pagination={false} scroll={{ y: 320 }} />

        </section>
        <section className="evaluation-panel" style={{ height: '100%' }}>
          <h3>执行与评分策略</h3>
          <div className="question-check-panel">
            <div style={{ marginTop: 16 }}>
              <Space orientation="vertical" size="middle" style={{ width: "100%", marginTop: 16 }}>
                <label style={{ display: "flex" }}><div style={{ width: 100 }}>执行策略</div><Select placeholder="选择执行策略" style={{ width: '100%' }} /></label>
                <label style={{ display: "flex" }}><div style={{ width: 100 }}>题目顺序</div><Select style={{ width: '100%' }} value={strategy.questionOrder} options={[{ value: "FIXED", label: "固定顺序" }, { value: "RANDOM_SEEDED", label: "固定种子随机" }]} onChange={(value) => setStrategy((current) => ({ ...current, questionOrder: value }))} /></label>
              </Space>
              <div className="question-check-row-panel">
                <div className="question-check-row">
                  <p>每题主故障</p>
                  <InputNumber min={1} max={20} />
                </div>
                <div className="question-check-row">
                  <p>每单元最大 Trial</p>
                  <InputNumber min={1} max={20} value={strategy.maxTrialsPerUnit} onChange={(value) => setStrategy((current) => ({ ...current, maxTrialsPerUnit: value ?? 1 }))} />
                </div>
                <div className="question-check-row">
                  <p>阶段重试次数</p>
                  <InputNumber min={0} max={5} value={strategy.retryPerPhase} onChange={(value) => setStrategy((current) => ({ ...current, retryPerPhase: value ?? 0 }))} />
                </div>
              </div>
              <div style={{ marginTop: 12 }}><Switch checked={strategy.stopOnSafetyViolation} onChange={(value) => setStrategy((current) => ({ ...current, stopOnSafetyViolation: value }))} /> 安全违规立即停止</div>

              <h4 style={{ paddingTop: '18px', borderTop: '1px solid #e2e8f0', marginTop: 20 }}>评分策略</h4>
              <Space orientation="vertical" size="middle" style={{ width: "100%", marginTop: 16 }}>
                <label style={{ display: "flex" }}><div style={{ width: 100 }}>评分规则</div><Select style={{ width: '100%' }} value={strategy.scoringPolicyId || undefined} options={options.scoringPolicies.map((item) => ({ value: item.id, label: item.name }))} onChange={(value) => setStrategy((current) => ({ ...current, scoringPolicyId: value }))} /></label>
                <label style={{ display: "flex" }}><div style={{ width: 100 }}>无效题目</div><Select placeholder="选择无效题目" style={{ width: '100%' }} /></label>
                <label style={{ display: "flex" }}><div style={{ width: 100 }}>评价方式</div><Select style={{ width: '100%' }} value={strategy.evaluatorId || undefined} options={options.evaluators.map((item) => ({ value: item.id, label: item.name }))} onChange={(value) => setStrategy((current) => ({ ...current, evaluatorId: value }))} /></label>
              </Space>

              <h4 style={{ marginTop: 20 }}>执行规模</h4>
              <div className="question-num">
                <div className="question-num-item">
                  <p>总体题目</p>
                  <span>13</span>
                </div>
                <div className="question-num-item">
                  <p>最大 Trial</p>
                  <span>13</span>
                </div>
                <div className="question-num-item">
                  <p>当前选择</p>
                  <span>13/13</span>
                </div>
              </div>
            </div>
            <div className="mcp-panel-check question-check">
              <div className="mcp-panel-check-title">
                <h3>策略校验通过</h3>
                <span style={{ color: 'green', paddingLeft: 12 }}>检查通过 4/4</span>
              </div>
              <div>
                <CheckCircleOutlined />
                <p>题目与系统兼容</p>
              </div>
              <div>
                <CheckCircleOutlined />
                <p>Harness 支持全部题目</p>
              </div>
              <div>
                <CheckCircleOutlined />
                <p>Oracle 契约完整</p>
              </div>
              <div style={{ borderBottom: 'none' }}>
                <CheckCircleOutlined />
                <p>恢复与清理条件已声明</p>
              </div>
              <div style={{ marginTop: 40, borderTop: '1px solid #e2e8f0' }}>
                <InfoCircleTwoTone />
                <p>总体进度按题目计算；重试和 Trial 不重复计入题目总数。</p>
              </div>
            </div>
          </div>
        </section>
      </div>}

      {step === 3 && compiled && <div className="evaluation-two-column">
        <section className="evaluation-panel">
          <div className="evaluation-panel-header"><h3>评测配置预览</h3></div>
          <Tabs defaultActiveKey="matrix" items={[
            { key: "matrix", label: "评测矩阵", children: <>
              {/* <div className="evaluation-compile-formula">{compiled.systemsCount} 个系统 × {compiled.harnessesCount} 个 Harness × {compiled.modelConfigurationsCount} 组 Harness-模型配置 × 适用题目 = {compiled.evaluationUnitCount} 个评测单元 <Tag>配置已冻结</Tag></div>
              <div className="evaluation-compile-grid" style={{ marginTop: 16, gridTemplateColumns: `1fr repeat(${new Set(compiled.matrix.map((item) => item.modelId)).size}, minmax(100px, 1fr))` }}><div className="head">系统 / Harness</div>{[...new Set(compiled.matrix.map((item) => item.modelId))].map((modelId) => <div className="head" key={modelId}>{options.models.find((item) => item.id === modelId)?.name ?? modelId}</div>)}{[...new Set(compiled.matrix.map((item) => `${item.systemId}::${item.harnessId}`))].flatMap((key) => { const [systemId, harnessId] = key.split("::"); const models = [...new Set(compiled.matrix.map((item) => item.modelId))]; return [<div key={`${key}-label`}><strong>{options.systems.find((item) => item.id === systemId)?.name}</strong><div className="evaluation-muted">{options.harnesses.find((item) => item.id === harnessId)?.name}</div></div>, ...models.map((modelId) => { const row = compiled.matrix.find((item) => item.systemId === systemId && item.harnessId === harnessId && item.modelId === modelId); return <div key={`${key}-${modelId}`}>{row ? `${row.unitCount} 单元` : "—"}</div>; })]; })}</div> */}

            
              <div className="headerBar">
                <span>1 个环境 × 1 个被测系统 × 1 个 Harness × 3 个模型 × 13 道题目 = 39 个评测单元</span>
                <Tag color='gray'>配置已冻结</Tag>
              </div>
              <h4>模型 × 题目矩阵</h4>
              <div className="models-table">
                <div className="models-table-row header">
                    <div>模型</div>
                    <div>网络 3</div>
                    <div>服务异常 4</div>
                    <div>资源 3</div>
                    <div>恢复与安全 3</div>
                    <div>评测单元</div>
                </div>
                <div className="models-table-row">
                    <div>GPT‑5.6</div>
                    <div>{renderBlueBlock(3)}</div>
                    <div>{renderBlueBlock(4)}</div>
                    <div>{renderBlueBlock(3)}</div>
                    <div>{renderBlueBlock(3)}</div>
                    <div>13</div>
                </div>
                <div className="models-table-row">
                    <div>GPT‑5.5</div>
                    <div>{renderBlueBlock(3)}</div>
                    <div>{renderBlueBlock(4)}</div>
                    <div>{renderBlueBlock(3)}</div>
                    <div>{renderBlueBlock(3)}</div>
                    <div>13</div>
                </div>
                <div className="models-table-row">
                    <div>GPT‑5.4</div>
                    <div>{renderBlueBlock(3)}</div>
                    <div>{renderBlueBlock(4)}</div>
                    <div>{renderBlueBlock(3)}</div>
                    <div>{renderBlueBlock(3)}</div>
                    <div>13</div>
                </div>
                <div className="models-table-row">
                    <div>合计</div>
                    <div>9</div>
                    <div>12</div>
                    <div>9</div>
                    <div>9</div>
                    <div>39</div>
                </div>
              </div>

              <div className="statCardsWrap">
                <div style={{ border: "1px solid #e5e7eb", padding: "6px 12px", borderRadius: 8 }}>
                    <div style={{ color: "#666" }}><InfoCircleTwoTone style={{ marginRight: 8 }}/>唯一题目</div>
                    <div style={{ fontSize: 28, fontWeight: 600, paddingLeft: 22 }}>13</div>
                </div>
                <div style={{ border: "1px solid #e5e7eb", padding: "6px 12px", borderRadius: 8 }}>
                    <div style={{ color: "#666" }}><InfoCircleTwoTone style={{ marginRight: 8 }}/>评测单元</div>
                    <div style={{ fontSize: 28, fontWeight: 600, paddingLeft: 22 }}>39</div>
                </div>
                <div style={{ border: "1px solid #e5e7eb", padding: "6px 12px", borderRadius: 8 }}>
                    <div style={{ color: "#666" }}><InfoCircleTwoTone style={{ marginRight: 8 }}/>最大 Trial</div>
                    <div style={{ fontSize: 28, fontWeight: 600, paddingLeft: 22 }}>195</div>
                </div>
                <div style={{ border: "1px solid #e5e7eb", padding: "6px 12px", borderRadius: 8 }}>
                    <div style={{ color: "#666" }}><InfoCircleTwoTone style={{ marginRight: 8 }}/>共享 MCP</div>
                    <div style={{ fontSize: 28, fontWeight: 600, paddingLeft: 22 }}>4</div>
                </div>
            </div>
            <div style={{ fontSize: 14, color: "#666" }}>
                ⓘ 总体进度按 39 个评测单元计算；同一道题在不同模型下分别计为一个评测单元。
            </div>

              <div className="border-box">
                <h4>共享配置</h4>
                <div className="shared-config-content">
                  <div>
                    <div className="shared-config-item"><div>实验环境</div> <span>研发测试集群 · 将排队</span></div>
                    <div className="shared-config-item"><div>被测系统</div> <span>Train Ticket · v0.3 · Commit 5f7c21d</span></div>
                    <div className="shared-config-item"><div>Harness</div> <span>Codex · Native</span></div>
                  </div>
                  <div>
                    <div className="shared-config-item"><div>Prompt / Strategy</div> <span>full‑lifecycle‑v1</span></div>
                    <div className="shared-config-item"><div>评分规则</div> <span>episode‑score‑v1</span></div>
                    <div className="shared-config-item"><div>评估器</div> <span>independent‑oracle‑v1</span></div>
                  </div>
                </div>
              </div>

              <div className="border-box">
                <div style={{display:"flex",gap:"12px"}}>
                  <h4>MCP 服务</h4>
                  <div style={{display:"flex",gap:"10px",flexWrap:"wrap"}}>
                    <Tag>k8s_ro · <span style={{color: '#2a74fd'}}>必选</span></Tag>
                    <Tag>telemetry_ro · <span style={{color: '#2a74fd'}}>必选</span></Tag>
                    <Tag>source_ro · <span style={{color: '#52c41a'}}>已选</span></Tag>
                    <Tag>codegraph_ro · <span style={{color: '#52c41a'}}>已选</span></Tag>
                  </div>
                </div>
                <div style={{fontSize:14,color:"#666",marginTop:8}}>
                    ⓘ MCP 为 39 个评测单元共享，不参与矩阵相乘。
                </div>
              </div>
              <div style={{fontSize:14,color:"#666",marginTop: 6}}><span style={{paddingRight: 10}}>配置指纹</span> sha256:91af...3d72 · 创建后任务配置不可修改</div>
            </> },
            { key: "details", label: "配置详情", children: <Descriptions column={2} bordered size="small"><Descriptions.Item label="目标环境">{environment?.name}</Descriptions.Item><Descriptions.Item label="创建方式">{environment?.status === "BUSY" ? "创建并排队" : "创建任务"}</Descriptions.Item><Descriptions.Item label="被测系统">{systemIds.map((id) => options.systems.find((item) => item.id === id)?.name).join("、")}</Descriptions.Item><Descriptions.Item label="Harness">{harnesses.map((item) => options.harnesses.find((option) => option.id === item.harnessId)?.name).join("、")}</Descriptions.Item><Descriptions.Item label="共享 MCP">{compiled.sharedMcpServerIds.join("、")}</Descriptions.Item><Descriptions.Item label="配置时间">{new Date(compiled.generatedAt).toLocaleString()}</Descriptions.Item></Descriptions> },
            { key: "questions", label: "题目清单", children: <Table size="small" rowKey="id" columns={questionColumns.slice(1)} dataSource={options.questions.filter((item) => questionIds.includes(item.id))} pagination={{ pageSize: 6 }} /> },
            { key: "strategy", label: "策略快照", children: <Descriptions column={1} bordered size="small"><Descriptions.Item label="题目顺序">{strategy.questionOrder}</Descriptions.Item><Descriptions.Item label="每单元最大 Trial">{strategy.maxTrialsPerUnit}</Descriptions.Item><Descriptions.Item label="阶段重试次数">{strategy.retryPerPhase}</Descriptions.Item><Descriptions.Item label="安全违规立即停止">{strategy.stopOnSafetyViolation ? "是" : "否"}</Descriptions.Item><Descriptions.Item label="评分规则">{strategy.scoringPolicyId}</Descriptions.Item><Descriptions.Item label="评估器">{strategy.evaluatorId}</Descriptions.Item></Descriptions> },
          ]} />
          {compiled.issues.length > 0 && <Alert style={{ marginTop: 16 }} type={compiled.issues.some((item) => item.severity === "ERROR") ? "error" : "warning"} showIcon title="编译问题" description={compiled.issues.map((item) => <div key={item.code}>{item.code}：{item.message}</div>)} />}
        </section>
        <section className="evaluation-panel">
          <h3>创建前校验</h3>
          
          <div className="evaluation-option-list" style={{ marginTop: 16, border: '1px solid #e2e8f0', borderRadius: 4, padding: 12 }}>
            <h4 style={{color: 'green'}}>校验通过：6/6</h4>
            {["实验环境与系统快照已选择", `${harnesses.length} 个 Harness 已选择`, `${compiled.modelConfigurationsCount} 组 Harness-模型配置`, `${requiredMcpIds.length} 个必选 MCP 已包含`, `${compiled.evaluationUnitCount} 个评测单元已编译`, "评分与评估器已冻结"].map((label) => <div className="evaluation-option-row" style={{ border: 'none', padding: '2px' }} key={label}>
              <span><CheckCircleFilled style={{ color: "#16A34A" }} /> {label}</span>
            </div>)}
          </div>
          {environment?.status === "BUSY" ? 
            <>
              <div className="evaluation-inline-warning" style={{ marginTop: 16 }}>
                <p><InfoCircleTwoTone style={{ marginRight: 8 }}/>环境当前被占用</p>
                <div style={{paddingLeft: 24,marginTop: 4}}>
                  <span style={{fontSize:14,color:'#3f3f3f'}}>{environment.currentTask?.taskId}正在运行，当前进度54%<br/>新任务将进入等待队列第 {environment.queueSize + 1} 位。</span>
                  <div style={{fontSize:14, marginTop: 4, color: '#1677ff', cursor: 'pointer'}}>查看占用任务</div>
                </div>
              </div>
              <div className="evaluation-inline-success" style={{ marginTop: 16 }}>
                <p style={{ color: "#16A34A" }}><CheckCircleFilled style={{ marginRight: 8 }}/>可以创建并排队</p>
                <div style={{paddingLeft: 24,marginTop: 4}}>
                  <span style={{fontSize:14,color:'#3f3f3f'}}>环境恢复验证通过后，控制器才会申请环境租约。</span>
                </div>
              </div>
            </>
            : 
            <div className="evaluation-inline-success" style={{ marginTop: 16 }}>环境空闲，可以创建任务。</div>
          }
          <div className="evaluation-muted" style={{ marginTop: 14 }}><InfoCircleTwoTone style={{ marginRight: 4 }}/>创建任务不会立即修改实验环境。</div>
        </section>
      </div>}

      <section className="evaluation-panel" style={{ display: "flex", justifyContent: "space-between", marginTop: 24 }}><Button onClick={() => navigate("/evaluation/tasks")}>取消</Button><Space><Button disabled={step === 0} onClick={() => setStep((current) => current - 1)}>上一步</Button><Button onClick={() => void saveDraft()}>保存草稿</Button>{step < 3 ? <Button type="primary" loading={compiling} onClick={() => void next()}>下一步：{STEP_ITEMS[step + 1]?.title}</Button> : <Button type="primary" loading={submitting} disabled={!compiled?.valid} onClick={() => void create()}>{environment?.status === "BUSY" ? "创建并排队" : "创建任务"}</Button>}</Space></section>
    </div>
  );
}
