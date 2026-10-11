import { useMemo, useState } from "react";
import { Alert, Button, InputNumber, Select, Space, Tag, Typography } from "antd";
import { SendOutlined } from "@ant-design/icons";
import { previewCoreFeature, startCoreRun } from "../services/api";
import type { CoreRunStatus, CoreTestRun } from "../types/phase1";

const { Text } = Typography;

const terminalStatuses = new Set<CoreRunStatus>(["passed", "failed"]);

type CodeGraphOperation = "context" | "query" | "callers" | "callees" | "impact" | "services";

interface MessageRecord {
  runId: string;
  expression: string;
  operation: CodeGraphOperation;
  status: CoreRunStatus;
}

function stringInput(inputs: Record<string, unknown>, key: string): string | undefined {
  const value = inputs[key];
  return typeof value === "string" && value.trim() ? value : undefined;
}

function numberInput(inputs: Record<string, unknown>, key: string, fallback: number): number {
  const value = inputs[key];
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function statusColor(status: CoreRunStatus): string {
  if (status === "passed") return "success";
  if (status === "failed") return "error";
  if (status === "running") return "processing";
  return "default";
}

export default function CodeGraphConsole({
  baseInputs,
  latestRun,
  onRun,
}: {
  baseInputs: Record<string, unknown>;
  latestRun: CoreTestRun;
  onRun: (run: CoreTestRun) => void;
}) {
  const [expression, setExpression] = useState("");
  const [operation, setOperation] = useState<CodeGraphOperation>("context");
  const [limit, setLimit] = useState(() => numberInput(baseInputs, "limit", 50));
  const [maxNodes, setMaxNodes] = useState(() => numberInput(baseInputs, "max_nodes", 120));
  const [depth, setDepth] = useState(() => numberInput(baseInputs, "depth", 2));
  const [messages, setMessages] = useState<MessageRecord[]>([]);
  const [activeRunId, setActiveRunId] = useState<string>();
  const [error, setError] = useState<string>();

  const sourcePath = stringInput(baseInputs, "source_path");
  const sourceIdentity = stringInput(baseInputs, "source_identity");
  const command = stringInput(baseInputs, "command") ?? "codegraph";
  const busy = Boolean(
    activeRunId && !(latestRun.runId === activeRunId && terminalStatuses.has(latestRun.status)),
  );
  const canSend = Boolean(sourcePath && sourceIdentity && expression.trim() && !busy);
  const visibleMessages = useMemo(() => messages.map((item) => (
    item.runId === latestRun.runId ? { ...item, status: latestRun.status } : item
  )), [latestRun, messages]);

  const operationHelp = useMemo(() => {
    if (operation === "context") return "面向自然语言任务构造上下文，最适合看源码路径和局部关系。";
    if (operation === "query") return "按符号或关键词搜索节点，通常返回节点列表。";
    if (operation === "impact") return "按符号分析影响范围，返回 affected 节点集合。";
    if (operation === "services") return "从 compose/k8s/source 派生服务清单，适合回答“有哪些服务”。";
    return "按符号查调用关系，只有 CodeGraph 能解析到关系时才会返回边。";
  }, [operation]);

  const send = async () => {
    if (!canSend || !sourcePath || !sourceIdentity) return;
    setActiveRunId("starting");
    setError(undefined);
    const nextInputs = {
      source_path: sourcePath,
      source_identity: sourceIdentity,
      command,
      operation,
      expression: expression.trim(),
      kind: operation === "query" ? baseInputs.kind ?? null : null,
      limit,
      max_nodes: maxNodes,
      max_code_blocks: numberInput(baseInputs, "max_code_blocks", 8),
      depth,
    };
    try {
      const preview = await previewCoreFeature("codegraph-query", nextInputs);
      const run = await startCoreRun(preview);
      setMessages((items) => [
        {
          runId: run.runId,
          expression: nextInputs.expression,
          operation,
          status: run.status,
        },
        ...items,
      ]);
      setActiveRunId(run.runId);
      setExpression("");
      onRun(run);
    } catch (value) {
      setActiveRunId(undefined);
      setError(value instanceof Error ? value.message : "CodeGraph 查询失败");
    }
  };

  return (
    <section className="codegraph-console" aria-label="CodeGraph 交互">
      <div className="codegraph-console-header">
        <div>
          <Text strong>CodeGraph 交互</Text>
          <div><Text type="secondary">发送后执行一次只读查询，并刷新下方图谱、日志和结构化结果。</Text></div>
        </div>
        <Tag color="geekblue">{operation}</Tag>
      </div>
      {!sourcePath || !sourceIdentity ? (
        <Alert type="warning" showIcon message="当前运行缺少 source_path 或 source_identity，不能继续交互。" />
      ) : (
        <>
          <div className="codegraph-console-controls">
            <Select
              aria-label="CodeGraph 查询类型"
              value={operation}
              onChange={setOperation}
              options={[
                { label: "context", value: "context" },
                { label: "query", value: "query" },
                { label: "callers", value: "callers" },
                { label: "callees", value: "callees" },
                { label: "impact", value: "impact" },
                { label: "services", value: "services" },
              ]}
            />
            <InputNumber
              aria-label="CodeGraph 返回上限"
              min={1}
              max={100}
              value={limit}
              onChange={(value) => setLimit(value ?? 50)}
              addonBefore="limit"
            />
            <InputNumber
              aria-label="CodeGraph Context 节点上限"
              min={10}
              max={200}
              value={maxNodes}
              onChange={(value) => setMaxNodes(value ?? 120)}
              addonBefore="nodes"
            />
            <InputNumber
              aria-label="CodeGraph Impact 深度"
              min={1}
              max={5}
              value={depth}
              onChange={(value) => setDepth(value ?? 2)}
              addonBefore="depth"
            />
          </div>
          <Text type="secondary">{operationHelp}</Text>
          <Space.Compact className="codegraph-console-input">
            <input
              aria-label="CodeGraph 消息"
              value={expression}
              onChange={(event) => setExpression(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter" && !event.shiftKey) void send();
              }}
              placeholder="例如：有哪些服务呢 / trace checkout request entry to CartService dependency"
            />
            <Button type="primary" icon={<SendOutlined />} loading={busy} disabled={!canSend} onClick={() => void send()}>
              发送并执行
            </Button>
          </Space.Compact>
          {error && <Alert type="error" showIcon message={error} />}
          {visibleMessages.length > 0 && (
            <div className="codegraph-console-history">
              {visibleMessages.slice(0, 5).map((message) => (
                <div key={message.runId} className="codegraph-console-message">
                  <span>{message.expression}</span>
                  <Space size={6}>
                    <Tag>{message.operation}</Tag>
                    <Tag color={statusColor(message.status)}>{message.status}</Tag>
                  </Space>
                </div>
              ))}
            </div>
          )}
        </>
      )}
    </section>
  );
}
