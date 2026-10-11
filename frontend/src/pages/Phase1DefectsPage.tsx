import {
  Alert,
  Badge,
  Button,
  Card,
  Checkbox,
  Col,
  Descriptions,
  Empty,
  Input,
  Row,
  Space,
  Spin,
  Table,
  Tabs,
  Tag,
  Typography,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import {
  CheckCircleOutlined,
  ClockCircleOutlined,
  CodeOutlined,
  DatabaseOutlined,
  ExperimentOutlined,
  FileSearchOutlined,
  PlayCircleOutlined,
  ReloadOutlined,
  SafetyCertificateOutlined,
} from "@ant-design/icons";
import { useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import {
  fetchCoreFeatures,
  fetchCoreRun,
  previewCoreFeature,
  startCoreRun,
} from "../services/api";
import type {
  CoreFeature,
  CoreFeatureGroup,
  CoreRunStatus,
  CoreTestPreview,
  CoreTestRun,
  FeatureInputField,
} from "../types/phase1";
import CodeGraphConsole from "../components/CodeGraphConsole";
import CodeGraphVisualization from "../components/CodeGraphVisualization";
import "./phase1-defects.css";

const { Paragraph, Text, Title } = Typography;
const { TextArea } = Input;

const groups: Array<{
  id: CoreFeatureGroup;
  label: string;
  description: string;
  icon: ReactNode;
}> = [
  {
    id: "source-codegraph",
    label: "Git / CodeGraph",
    description: "源码身份、物化、建图与交互查询",
    icon: <CodeOutlined />,
  },
  {
    id: "templates-defects",
    label: "候选 / 测试",
    description: "OTel Demo 静态候选与后续验证入口",
    icon: <ExperimentOutlined />,
  },
  {
    id: "kubernetes",
    label: "Kubernetes",
    description: "Manifest 与 API Server 数据源扫描",
    icon: <DatabaseOutlined />,
  },
  {
    id: "evidence-contracts",
    label: "证据 / 合同",
    description: "Evidence 转换与当前 Schema 回放",
    icon: <FileSearchOutlined />,
  },
];

const terminalStatuses = new Set<CoreRunStatus>(["passed", "failed"]);

function formatted(value: unknown): string {
  return JSON.stringify(value, null, 2);
}

function statusTag(status?: CoreRunStatus) {
  if (status === "passed") return <Tag color="success" icon={<CheckCircleOutlined />}>通过</Tag>;
  if (status === "failed") return <Tag color="error">失败</Tag>;
  if (status === "running") return <Tag color="processing">运行中</Tag>;
  if (status === "queued") return <Tag color="default" icon={<ClockCircleOutlined />}>等待执行</Tag>;
  return <Tag>尚未执行</Tag>;
}

function record(value: unknown): Record<string, unknown> | undefined {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : undefined;
}

function optionalString(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() ? value : undefined;
}

interface ServiceInventoryRow {
  key: string;
  name: string;
  category?: string;
  otelServiceName?: string;
  sourceDir?: string;
  kubernetesResourceCount: number;
}

interface StaticCandidateRow {
  key: string;
  candidateId: string;
  title: string;
  description: string;
  testNote: string;
}

function serviceRows(raw: Record<string, unknown> | undefined): ServiceInventoryRow[] {
  if (!Array.isArray(raw?.services)) return [];
  return raw.services.flatMap((value, index) => {
    const item = record(value);
    const name = optionalString(item?.name);
    if (!item || !name) return [];
    return [{
      key: name || `service-${index}`,
      name,
      category: optionalString(item.category),
      otelServiceName: optionalString(item.otel_service_name),
      sourceDir: optionalString(item.source_dir),
      kubernetesResourceCount: Array.isArray(item.kubernetes_resources)
        ? item.kubernetes_resources.length
        : 0,
    }];
  });
}

function CodeGraphAnswer({ result }: { result: unknown }) {
  const resultObject = record(result);
  const raw = record(resultObject?.raw_result);
  const queryStatus = optionalString(resultObject?.query_status);
  const resolvedOperation = optionalString(resultObject?.resolved_operation);
  const answer = optionalString(raw?.answer) ?? optionalString(raw?.summary);
  const rows = serviceRows(raw);
  if (!answer && !queryStatus && rows.length === 0) return null;

  const columns: ColumnsType<ServiceInventoryRow> = [
    { title: "服务", dataIndex: "name", width: 190, render: (value: string) => <Text strong>{value}</Text> },
    { title: "类别", dataIndex: "category", width: 130, render: (value?: string) => <Tag>{value ?? "unknown"}</Tag> },
    { title: "OTEL_SERVICE_NAME", dataIndex: "otelServiceName", width: 180, render: (value?: string) => value ? <Text code>{value}</Text> : "—" },
    { title: "源码目录", dataIndex: "sourceDir", render: (value?: string) => value ? <Text code>{value}</Text> : "—" },
    { title: "K8s 资源", dataIndex: "kubernetesResourceCount", width: 100 },
  ];

  return (
    <section className="codegraph-answer" aria-label="CodeGraph 查询回答">
      <div className="codegraph-answer-header">
        <div>
          <Text strong>查询回答</Text>
          <div><Text type="secondary">这是对本次输入的语义化结果，不只是底层命令是否执行成功。</Text></div>
        </div>
        <Space>
          {resolvedOperation && <Tag color="geekblue">{resolvedOperation}</Tag>}
          {queryStatus && <Tag color={queryStatus === "matched" ? "success" : "warning"}>{queryStatus}</Tag>}
        </Space>
      </div>
      {answer && <Paragraph className="codegraph-answer-text">{answer}</Paragraph>}
      {rows.length > 0 && (
        <Table
          size="small"
          columns={columns}
          dataSource={rows}
          pagination={rows.length > 12 ? { pageSize: 12 } : false}
        />
      )}
    </section>
  );
}

function staticCandidateRows(result: unknown): StaticCandidateRow[] {
  const resultObject = record(result);
  if (!Array.isArray(resultObject?.candidates)) return [];
  return resultObject.candidates.flatMap((value) => {
    const item = record(value);
    const candidateId = optionalString(item?.candidate_id);
    const title = optionalString(item?.title);
    const description = optionalString(item?.description);
    const testNote = optionalString(item?.test_note);
    if (!item || !candidateId || !title || !description || !testNote) return [];
    return [{
      key: candidateId,
      candidateId,
      title,
      description,
      testNote,
    }];
  });
}

function StaticCandidatesSummary({ result }: { result: unknown }) {
  const resultObject = record(result);
  const rows = staticCandidateRows(result);
  if (rows.length === 0) return null;
  const candidateSetId = optionalString(resultObject?.candidate_set_id);
  const runtimeConfirmed = resultObject?.runtime_confirmed === true;
  return (
    <section className="static-candidates-summary" aria-label="OTel Demo 静态候选">
      <div className="codegraph-answer-header">
        <div>
          <Text strong>OTel Demo 后续测试候选</Text>
          <div><Text type="secondary">当前固定 {rows.length} 个源码级候选，只保留自然语言说明；运行时后果仍待验证。</Text></div>
        </div>
        <Space>
          <Tag color={runtimeConfirmed ? "success" : "warning"}>{runtimeConfirmed ? "已运行时确认" : "静态候选"}</Tag>
          {candidateSetId && <Tag color="geekblue">{candidateSetId}</Tag>}
        </Space>
      </div>
      <Space orientation="vertical" size={12} style={{ width: "100%" }}>
        {rows.map((candidate) => (
          <Card
            key={candidate.key}
            size="small"
            title={<Space><Tag color="blue">{candidate.candidateId}</Tag><Text strong>{candidate.title}</Text></Space>}
          >
            <Paragraph>{candidate.description}</Paragraph>
            <Text strong>后续测试关注</Text>
            <Paragraph className="candidate-test-note">{candidate.testNote}</Paragraph>
          </Card>
        ))}
      </Space>
    </section>
  );
}

function HarnessPromptSummary({ result }: { result: unknown }) {
  const resultObject = record(result);
  const prompt = optionalString(resultObject?.prompt);
  if (!prompt) return null;
  const candidateId = optionalString(resultObject?.candidate_id);
  const title = optionalString(resultObject?.candidate_title);
  const promptFile = optionalString(resultObject?.prompt_file);
  return (
    <section className="harness-prompt-summary" aria-label="Harness 自然语言 Prompt">
      <div className="codegraph-answer-header">
        <div>
          <Text strong>{title ?? "Harness 自然语言 Prompt"}</Text>
          <div><Text type="secondary">下面内容可以直接复制发送给 Codex、Claude Code、BladeAI 或 DeepSeek Harness。</Text></div>
        </div>
        <Space wrap>
          {candidateId && <Tag color="blue">{candidateId}</Tag>}
          <Tag color="success">可直接复制</Tag>
        </Space>
      </div>
      <pre className="phase1-result harness-prompt-output">{prompt}</pre>
      {promptFile && <Text type="secondary">来源：<Text code>{promptFile}</Text></Text>}
      <Alert
        className="harness-prompt-boundary"
        type="info"
        showIcon
        title="这里只生成 Prompt"
        description="页面没有调用 Harness、访问 Kubernetes 或注入故障；真正执行由接收该 Prompt 的 Harness 和 Controller 决定。"
      />
    </section>
  );
}

export default function Phase1DefectsPage() {
  const [features, setFeatures] = useState<CoreFeature[]>([]);
  const [activeGroup, setActiveGroup] = useState<CoreFeatureGroup>("source-codegraph");
  const [selectedId, setSelectedId] = useState<string>();
  const [inputText, setInputText] = useState("{}");
  const [preview, setPreview] = useState<CoreTestPreview>();
  const [confirmed, setConfirmed] = useState(false);
  const [run, setRun] = useState<CoreTestRun>();
  const [loading, setLoading] = useState(true);
  const [previewing, setPreviewing] = useState(false);
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState<string>();

  const selected = useMemo(
    () => features.find((feature) => feature.id === selectedId),
    [features, selectedId],
  );
  const groupedFeatures = useMemo(
    () => features.filter((feature) => feature.group === activeGroup),
    [activeGroup, features],
  );

  useEffect(() => {
    let cancelled = false;
    fetchCoreFeatures()
      .then((catalog) => {
        if (cancelled) return;
        setFeatures(catalog.features);
        const first = catalog.features.find((item) => item.group === "source-codegraph");
        if (first) {
          setSelectedId(first.id);
          setInputText(formatted(first.exampleInputs));
        }
      })
      .catch((value) => {
        if (!cancelled) setError(value instanceof Error ? value.message : "核心功能目录加载失败");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (!run || terminalStatuses.has(run.status)) return undefined;
    let cancelled = false;
    const poll = window.setTimeout(() => {
      fetchCoreRun(run.runId)
        .then((next) => {
          if (!cancelled) setRun(next);
        })
        .catch((value) => {
          if (!cancelled) setError(value instanceof Error ? value.message : "运行状态读取失败");
        });
    }, 800);
    return () => {
      cancelled = true;
      window.clearTimeout(poll);
    };
  }, [run]);

  const selectFeature = (feature: CoreFeature) => {
    setSelectedId(feature.id);
    setInputText(formatted(feature.exampleInputs));
    setPreview(undefined);
    setRun(undefined);
    setConfirmed(false);
    setError(undefined);
  };

  const changeGroup = (group: string) => {
    const nextGroup = group as CoreFeatureGroup;
    setActiveGroup(nextGroup);
    const first = features.find((feature) => feature.group === nextGroup);
    if (first) selectFeature(first);
  };

  const handlePreview = async () => {
    if (!selected) return;
    setError(undefined);
    setPreviewing(true);
    setPreview(undefined);
    setRun(undefined);
    setConfirmed(false);
    try {
      const inputs = JSON.parse(inputText) as Record<string, unknown>;
      if (!inputs || Array.isArray(inputs) || typeof inputs !== "object") {
        throw new Error("输入必须是 JSON 对象");
      }
      setPreview(await previewCoreFeature(selected.id, inputs));
    } catch (value) {
      setError(value instanceof Error ? value.message : "输入解析失败");
    } finally {
      setPreviewing(false);
    }
  };

  const handleStart = async () => {
    if (!preview || !confirmed) return;
    setStarting(true);
    setError(undefined);
    try {
      setRun(await startCoreRun(preview));
    } catch (value) {
      setError(value instanceof Error ? value.message : "测试启动失败");
    } finally {
      setStarting(false);
    }
  };

  if (loading) return <div className="phase1-centered"><Spin size="large" /></div>;
  if (!features.length) {
    return (
      <div className="phase1-page">
        <Alert type="error" showIcon title="无法加载核心功能" description={error ?? "后端未返回功能目录"} />
      </div>
    );
  }

  const fieldColumns: ColumnsType<FeatureInputField> = [
    { title: "输入", dataIndex: "label", width: 150, render: (value: string, item) => <><Text strong>{value}</Text><div><Text code>{item.name}</Text></div></> },
    { title: "类型", dataIndex: "valueType", width: 120, render: (value: string) => <Tag>{value}</Tag> },
    { title: "必填", dataIndex: "required", width: 72, render: (value: boolean) => value ? <Tag color="blue">是</Tag> : <Tag>否</Tag> },
    { title: "说明", dataIndex: "description", render: (value: string, item) => <><div>{value}</div>{item.allowedValues.length > 0 && <Text type="secondary">允许值：{item.allowedValues.join(" / ")}</Text>}</> },
  ];

  return (
    <div className="phase1-page phase1-workbench">
      <div className="phase1-hero">
        <div>
          <Space size={10}><Tag color="blue">PHASE 1</Tag><Text className="phase1-eyebrow">CORE FUNCTION WORKBENCH</Text></Space>
          <Title level={2}>第一阶段 · 核心功能测试台</Title>
          <Paragraph>每次只测试一个功能。你定义全部输入，预览确认后才执行；日志和最终结果按本次运行独立保留。</Paragraph>
        </div>
        <Space><Badge status="success" text="后端已连接" /><Button icon={<ReloadOutlined />} onClick={() => window.location.reload()}>刷新</Button></Space>
      </div>

      <Alert
        className="phase1-boundary"
        type="info"
        showIcon
        icon={<SafetyCertificateOutlined />}
        title="不会自动执行或串联测试"
        description="编辑输入和生成预览都不会运行功能。只有确认输入摘要后，才启动这一项测试；结束后也不会自动进入下一个功能或端到端流程。"
      />

      {error && <Alert className="phase1-error" type="error" showIcon title="操作失败" description={error} closable onClose={() => setError(undefined)} />}

      <Card className="phase1-section phase1-tabs-card">
        <Tabs
          activeKey={activeGroup}
          onChange={changeGroup}
          items={groups.map((group) => ({
            key: group.id,
            label: <Space>{group.icon}{group.label}</Space>,
            children: <Text type="secondary">{group.description}</Text>,
          }))}
        />
      </Card>

      <Row gutter={[16, 16]} className="phase1-section">
        <Col xs={24} lg={7} xl={6}>
          <Card title="选择功能" className="phase1-feature-panel">
            <div className="phase1-feature-list">
              {groupedFeatures.map((feature) => (
                <button
                  type="button"
                  className={`phase1-feature-item ${feature.id === selectedId ? "selected" : ""}`}
                  key={feature.id}
                  onClick={() => selectFeature(feature)}
                >
                  <span className="phase1-feature-title">{feature.title}</span>
                  <span>{feature.description}</span>
                  <span className="phase1-feature-tags">
                    {feature.mutatesLocalState ? <Tag color="warning">写本地状态</Tag> : <Tag color="green">只读</Tag>}
                    {feature.mayExceedFiveMinutes && <Tag color="volcano">可能超过 5 分钟</Tag>}
                  </span>
                </button>
              ))}
            </div>
          </Card>
        </Col>

        <Col xs={24} lg={17} xl={18}>
          {selected ? (
            <div className="phase1-main-stack">
              <Card
                title={<Space><CodeOutlined />{selected.title}</Space>}
                extra={<Tag color="blue">输出：{selected.evidenceOutput}</Tag>}
              >
                <Paragraph>{selected.description}</Paragraph>
                <Table
                  size="small"
                  columns={fieldColumns}
                  dataSource={selected.inputFields}
                  rowKey="name"
                  pagination={false}
                />

                <div className="phase1-editor-header">
                  <div><Text strong>本次测试输入</Text><div><Text type="secondary">所有字段均由你编辑；修改后必须重新生成预览。</Text></div></div>
                  <Button onClick={() => setInputText(formatted(selected.exampleInputs))}>恢复示例</Button>
                </div>
                <TextArea
                  aria-label="核心功能测试输入"
                  className="phase1-json-editor"
                  value={inputText}
                  onChange={(event) => {
                    setInputText(event.target.value);
                    setPreview(undefined);
                    setRun(undefined);
                    setConfirmed(false);
                  }}
                  autoSize={{ minRows: 12, maxRows: 24 }}
                  spellCheck={false}
                />
                <div className="phase1-action-row">
                  <Button type="primary" icon={<FileSearchOutlined />} loading={previewing} onClick={() => void handlePreview()}>
                    生成执行预览
                  </Button>
                  <Text type="secondary">此操作只校验输入，不执行测试。</Text>
                </div>
              </Card>

              {preview && (
                <Card title="执行预览与确认" className="phase1-preview-card">
                  <Descriptions bordered size="small" column={1}>
                    <Descriptions.Item label="功能">{selected.title}</Descriptions.Item>
                    <Descriptions.Item label="输入摘要"><Text code copyable>{preview.inputDigest}</Text></Descriptions.Item>
                    <Descriptions.Item label="预览有效期">{new Date(preview.expiresAt).toLocaleString()}</Descriptions.Item>
                  </Descriptions>

                  <Title level={5}>将执行的操作</Title>
                  <div className="phase1-operation-list">
                    {preview.operations.map((operation, index) => <div key={`${index}-${operation}`}><span>{index + 1}</span>{operation}</div>)}
                  </div>

                  {preview.warnings.length > 0 && (
                    <Alert type="warning" showIcon title="执行前必须确认的影响" description={<div>{preview.warnings.map((warning) => <div key={warning}>• {warning}</div>)}</div>} />
                  )}

                  <details className="phase1-normalized-input">
                    <summary>查看后端规范化输入</summary>
                    <pre>{formatted(preview.normalizedInputs)}</pre>
                  </details>

                  <Checkbox checked={confirmed} onChange={(event) => setConfirmed(event.target.checked)}>
                    {preview.confirmationText}
                  </Checkbox>
                  <div className="phase1-action-row">
                    <Button
                      danger={selected.mutatesLocalState}
                      type="primary"
                      icon={<PlayCircleOutlined />}
                      disabled={!confirmed}
                      loading={starting}
                      onClick={() => void handleStart()}
                    >
                      确认并开始测试
                    </Button>
                    {!confirmed && <Text type="secondary">勾选确认后才可执行。</Text>}
                  </div>
                </Card>
              )}

              {run && <RunPanel run={run} onRun={setRun} />}
            </div>
          ) : <Empty description="请选择功能" />}
        </Col>
      </Row>
    </div>
  );
}

function RunPanel({ run, onRun }: { run: CoreTestRun; onRun: (run: CoreTestRun) => void }) {
  const logLines = run.logs.map((entry) => {
    const time = new Date(entry.occurredAt).toLocaleTimeString();
    const details = Object.keys(entry.details).length ? ` ${JSON.stringify(entry.details)}` : "";
    return `${String(entry.sequence).padStart(3, "0")} ${time} ${entry.level.toUpperCase()} ${entry.message}${details}`;
  });
  return (
    <Card
      title={<Space><PlayCircleOutlined />执行日志与结果</Space>}
      extra={<Space>{statusTag(run.status)}<Text code>{run.runId}</Text></Space>}
      className="phase1-run-card"
    >
      <Descriptions bordered size="small" column={{ xs: 1, sm: 2 }}>
        <Descriptions.Item label="状态">{statusTag(run.status)}</Descriptions.Item>
        <Descriptions.Item label="耗时">{run.durationSeconds == null ? "—" : `${run.durationSeconds.toFixed(3)} 秒`}</Descriptions.Item>
        <Descriptions.Item label="开始时间">{run.startedAt ? new Date(run.startedAt).toLocaleString() : "等待中"}</Descriptions.Item>
        <Descriptions.Item label="结束时间">{run.finishedAt ? new Date(run.finishedAt).toLocaleString() : "—"}</Descriptions.Item>
      </Descriptions>

      <Title level={5}>执行日志</Title>
      <pre className="phase1-output" aria-label="执行日志">{logLines.join("\n") || "等待执行器输出日志…"}</pre>

      {run.error && <Alert type="error" showIcon title="执行异常" description={run.error} />}
      {run.result !== undefined && (
        <>
          {run.featureId === "static-candidates" && <StaticCandidatesSummary result={run.result} />}
          {run.featureId === "harness-prompt" && <HarnessPromptSummary result={run.result} />}
          {run.featureId === "codegraph-query" && <CodeGraphAnswer result={run.result} />}
          {run.featureId === "codegraph-query" && <CodeGraphConsole baseInputs={run.inputs} latestRun={run} onRun={onRun} />}
          {run.featureId === "codegraph-query" && <CodeGraphVisualization result={run.result} />}
          <Title level={5}>最终结构化结果</Title>
          <pre className="phase1-result" aria-label="最终结构化结果">{formatted(run.result)}</pre>
        </>
      )}
    </Card>
  );
}
