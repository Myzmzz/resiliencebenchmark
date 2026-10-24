import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import Phase1DefectsPage from "./Phase1DefectsPage";
import type {
  CoreFeatureCatalog,
  CoreTestPreview,
  CoreTestRun,
} from "../types/phase1";

const catalog: CoreFeatureCatalog = {
  schemaVersion: "phase1-core-feature-catalog.v1",
  features: [
    {
      id: "git-clone",
      group: "source-codegraph",
      title: "GitHub 拉取源码",
      description: "使用用户指定地址和目标目录拉取代码",
      evidenceOutput: "clone commands + observed Git HEAD/remote/files",
      mutatesLocalState: true,
      mayExceedFiveMinutes: true,
      inputFields: [
        { name: "repository_url", label: "GitHub 地址", valueType: "string", required: true, description: "HTTPS URL", allowedValues: [] },
        { name: "destination_path", label: "目标目录", valueType: "string", required: true, description: "拉取位置", allowedValues: [] },
      ],
      exampleInputs: { repository_url: "https://github.com/example/project.git", revision: "main", destination_path: "/workspace/source", auth_mode: "public_https", github_account: null, clone_depth: 1, existing_policy: "update_existing" },
    },
    {
      id: "codegraph-index",
      group: "source-codegraph",
      title: "CodeGraph 建图/同步",
      description: "建立用户源码索引",
      evidenceOutput: "CodeGraphManifest",
      mutatesLocalState: true,
      mayExceedFiveMinutes: true,
      inputFields: [
        { name: "source_path", label: "源码目录", valueType: "string", required: true, description: "源码路径", allowedValues: [] },
      ],
      exampleInputs: { source_path: "/workspace/source", source_identity: "fixture-v1", force_reindex: false },
    },
    {
      id: "contract-replay",
      group: "evidence-contracts",
      title: "当前合同回放",
      description: "用当前 Schema 验证报告",
      evidenceOutput: "valid + validation_errors",
      mutatesLocalState: false,
      mayExceedFiveMinutes: false,
      inputFields: [
        { name: "report_path", label: "扫描报告", valueType: "string", required: true, description: "报告路径", allowedValues: [] },
      ],
      exampleInputs: { report_path: "/workspace/report.json" },
    },
    {
      id: "static-candidates",
      group: "templates-defects",
      title: "OTel Demo 六个静态候选",
      description: "查看已经确定的六个源码级候选",
      evidenceOutput: "selected candidate descriptions + test notes",
      mutatesLocalState: false,
      mayExceedFiveMinutes: false,
      inputFields: [
        { name: "candidate_ids", label: "候选 ID", valueType: "string_array", required: true, description: "静态候选", allowedValues: ["OTEL-C01", "OTEL-C02", "OTEL-C03", "OTEL-C04", "OTEL-C05", "OTEL-C06"] },
      ],
      exampleInputs: { candidate_set_path: "/workspace/otel-demo-static-candidates.v1.yaml", candidate_ids: ["OTEL-C01", "OTEL-C02", "OTEL-C03", "OTEL-C04", "OTEL-C05", "OTEL-C06"] },
    },
    {
      id: "harness-prompt",
      group: "templates-defects",
      title: "生成 Harness 自然语言 Prompt",
      description: "返回可以直接发送给任意Harness的中文Prompt",
      evidenceOutput: "copy-ready natural-language Harness prompt",
      mutatesLocalState: false,
      mayExceedFiveMinutes: false,
      inputFields: [
        { name: "candidate_id", label: "静态候选", valueType: "string", required: true, description: "六个候选之一", allowedValues: ["OTEL-C01", "OTEL-C02", "OTEL-C03", "OTEL-C04", "OTEL-C05", "OTEL-C06"] },
        { name: "additional_instruction", label: "补充要求", valueType: "string", required: false, description: "追加要求", allowedValues: [] },
      ],
      exampleInputs: { prompt_catalog_path: "/workspace/otel-demo-harness-prompts.v1.yaml", candidate_id: "OTEL-C04", additional_instruction: null },
    },
  ],
};

const preview: CoreTestPreview = {
  previewId: "preview-1",
  featureId: "git-clone",
  createdAt: "2026-08-27T00:00:00Z",
  expiresAt: "2026-08-27T00:15:00Z",
  normalizedInputs: catalog.features[0].exampleInputs,
  operations: ["使用公开 HTTPS 方式访问 GitHub", "执行 git clone", "执行 git checkout"],
  warnings: ["将在目标位置创建源码目录"],
  confirmationText: "我确认使用输入摘要 abcdef123456 执行「GitHub 拉取源码」",
  inputDigest: "abcdef123456".padEnd(64, "0"),
};

const finishedRun: CoreTestRun = {
  runId: "phase1-run-1",
  featureId: "git-clone",
  status: "passed",
  createdAt: "2026-08-27T00:00:01Z",
  startedAt: "2026-08-27T00:00:01Z",
  finishedAt: "2026-08-27T00:00:02Z",
  durationSeconds: 1,
  inputs: preview.normalizedInputs,
  logs: [
    { sequence: 1, occurredAt: "2026-08-27T00:00:01Z", level: "info", message: "测试开始", details: {} },
    { sequence: 2, occurredAt: "2026-08-27T00:00:02Z", level: "info", message: "源码拉取完成", details: { head: "abc" } },
  ],
  result: { test_passed: true, observed_head: "abc" },
};

const queuedRun: CoreTestRun = {
  runId: "phase1-queued",
  featureId: "git-clone",
  status: "queued",
  createdAt: "2026-08-27T00:00:01Z",
  startedAt: null,
  finishedAt: null,
  durationSeconds: null,
  inputs: preview.normalizedInputs,
  logs: [],
  result: null,
  error: null,
};

const candidatePreview: CoreTestPreview = {
  ...preview,
  previewId: "preview-candidates",
  featureId: "static-candidates",
  normalizedInputs: catalog.features[3].exampleInputs,
  operations: ["读取已确定的静态候选集", "选择候选：OTEL-C01, OTEL-C02, OTEL-C03, OTEL-C04, OTEL-C05, OTEL-C06"],
  warnings: [],
};

const candidateRun: CoreTestRun = {
  ...finishedRun,
  runId: "phase1-candidate-run",
  featureId: "static-candidates",
  inputs: candidatePreview.normalizedInputs,
  result: {
    test_passed: true,
    candidate_set_id: "otel-demo-2.2.0-core-six",
    runtime_confirmed: false,
    candidates: [
      {
        candidate_id: "OTEL-C01",
        title: "结算结果不确定时，重复提交可能再次扣款或发货",
        description: "第一次调用可能已经完成了部分操作，但响应丢失后再次提交会被当成一笔新订单。",
        test_note: "制造网络丢包后重试同一笔结算，并对所有业务副作用做唯一性对账。",
      },
      {
        candidate_id: "OTEL-C02",
        title: "Valkey内存空间很小时，购物车可能随着进程重启一起丢失",
        description: "购物车持续增长时，Valkey可能触碰20Mi容器内存上限并重启。",
        test_note: "先写入可核对的购物车，再施加内存压力并检查恢复后的数据完整性。",
      },
      {
        candidate_id: "OTEL-C03",
        title: "Pod看起来健康时，真实业务请求可能已经失败",
        description: "健康处理器不检查数据库或下游服务，Pod可能仍然显示Ready。",
        test_note: "让真实依赖失效，同时对比Pod、Endpoint和业务请求结果。",
      },
      {
        candidate_id: "OTEL-C04",
        title: "Checkout被终止时，正在进行的结算不会被安全排空",
        description: "Checkout正常服务时无法进入后面的GracefulStop控制流。",
        test_note: "在长结算请求期间终止Pod并对账请求和所有副作用。",
      },
      {
        candidate_id: "OTEL-C05",
        title: "结算中途失败后，各服务可能留下彼此不一致的状态",
        description: "扣款、发货、购物车和Kafka发布之间没有统一事务或补偿流程。",
        test_note: "在不同步骤间制造中断并进行跨服务完整性对账。",
      },
      {
        candidate_id: "OTEL-C06",
        title: "Accounting写库失败后，Kafka消息仍可能被当作已经消费",
        description: "数据库写入和Kafka自动提交Offset不在同一个确认边界。",
        test_note: "让写库失败并比较Offset、订单记录以及恢复后的重放情况。",
      },
    ],
  },
};

const harnessPromptPreview: CoreTestPreview = {
  ...preview,
  previewId: "preview-harness-prompt",
  featureId: "harness-prompt",
  normalizedInputs: catalog.features[4].exampleInputs,
  operations: [
    "读取候选Prompt：OTEL-C04",
    "校验Prompt文件位于受控Harness Prompt目录",
    "返回可直接复制给Harness的完整自然语言Prompt",
  ],
  warnings: ["这里只生成Prompt，不会调用Harness、访问集群或注入故障。"],
};

const harnessPromptRun: CoreTestRun = {
  ...finishedRun,
  runId: "phase1-harness-prompt-run",
  featureId: "harness-prompt",
  inputs: harnessPromptPreview.normalizedInputs,
  result: {
    test_passed: true,
    candidate_id: "OTEL-C04",
    candidate_title: "Checkout被终止时，正在进行的结算不会被安全排空",
    prompt: "请在 OTel Demo 2.2.0 中验证 Checkout Pod 被终止时，正在处理的结算请求能否被安全排空。\n\n请只使用当前 Harness 授权的 k8s_ro、telemetry_ro、source_ro 和 chaos_control。开始前确认目标和基线。\n\n在活动请求期间请求删除授权的 Checkout Pod，并记录请求、副作用和恢复证据。\n\n最后用自然语言给出完整时间线和明确结论。",
    prompt_file: "harness/prompts/otel-demo-core-six/OTEL-C04.md",
    interaction_mode: "direct_natural_language",
    ready_to_copy: true,
    runtime_execution_status: "not_executed",
  },
};

function response(data: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(data), { status, headers: { "Content-Type": "application/json" } }));
}

afterEach(() => vi.restoreAllMocks());

describe("Phase1DefectsPage", () => {
  it("以四组核心功能和用户输入为主体", async () => {
    vi.stubGlobal("fetch", vi.fn().mockImplementation(() => response(catalog)));
    render(<Phase1DefectsPage />);

    expect(await screen.findByRole("heading", { name: "第一阶段 · 核心功能测试台" })).toBeInTheDocument();
    for (const label of ["Git / CodeGraph", "候选 / 测试", "Kubernetes", "证据 / 合同"]) {
      expect(screen.getByText(label)).toBeInTheDocument();
    }
    expect(screen.getAllByText("GitHub 拉取源码")).toHaveLength(2);
    expect((screen.getByLabelText("核心功能测试输入") as HTMLTextAreaElement).value).toContain("repository_url");
    expect(screen.getByText(/编辑输入和生成预览都不会运行功能/)).toBeInTheDocument();
  });

  it("生成预览不执行，确认前开始按钮保持禁用", async () => {
    const fetchMock = vi.fn().mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).endsWith("/features") && !init) return response(catalog);
      if (String(input).endsWith("/git-clone/preview") && init?.method === "POST") return response(preview);
      throw new Error(`unexpected request ${input}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<Phase1DefectsPage />);

    fireEvent.click((await screen.findByText("生成执行预览")).closest("button") as HTMLButtonElement);
    expect(await screen.findByText("使用公开 HTTPS 方式访问 GitHub")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /确认并开始测试/ })).toBeDisabled();
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("用户确认后启动，并展示执行日志与最终结果", async () => {
    const fetchMock = vi.fn().mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/features") && !init) return response(catalog);
      if (url.endsWith("/git-clone/preview") && init?.method === "POST") return response(preview);
      if (url.endsWith("/preview-1/start") && init?.method === "POST") return response(finishedRun, 202);
      throw new Error(`unexpected request ${input}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<Phase1DefectsPage />);

    fireEvent.click((await screen.findByText("生成执行预览")).closest("button") as HTMLButtonElement);
    fireEvent.click(await screen.findByRole("checkbox"));
    fireEvent.click(screen.getByRole("button", { name: /确认并开始测试/ }));

    expect(await screen.findByLabelText("执行日志")).toHaveTextContent("源码拉取完成");
    expect(screen.getByLabelText("最终结构化结果")).toHaveTextContent('"observed_head": "abc"');
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/phase1/core/previews/preview-1/start",
      expect.objectContaining({ method: "POST" }),
    ));
  });

  it("只用自然语言展示六个OTel Demo静态候选", async () => {
    const fetchMock = vi.fn().mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/features") && !init) return response(catalog);
      if (url.endsWith("/static-candidates/preview") && init?.method === "POST") return response(candidatePreview);
      if (url.endsWith("/preview-candidates/start") && init?.method === "POST") return response(candidateRun, 202);
      throw new Error(`unexpected request ${input}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<Phase1DefectsPage />);

    fireEvent.click(await screen.findByText("候选 / 测试"));
    fireEvent.click((await screen.findByText("生成执行预览")).closest("button") as HTMLButtonElement);
    fireEvent.click(await screen.findByRole("checkbox"));
    fireEvent.click(screen.getByRole("button", { name: /确认并开始测试/ }));

    const candidates = await screen.findByLabelText("OTel Demo 静态候选");
    expect(candidates).toHaveTextContent("当前固定 6 个源码级候选");
    expect(candidates).toHaveTextContent("重复提交可能再次扣款或发货");
    expect(candidates).toHaveTextContent("Valkey内存空间很小时");
    expect(candidates).toHaveTextContent("Pod看起来健康时");
    expect(candidates).toHaveTextContent("Checkout被终止时");
    expect(candidates).toHaveTextContent("各服务可能留下彼此不一致的状态");
    expect(candidates).toHaveTextContent("Accounting写库失败后");
    expect(candidates).not.toHaveTextContent("风险：");
    expect(candidates).not.toHaveTextContent("后果：");
  });

  it("生成可以直接发送给Harness的自然语言Prompt", async () => {
    const fetchMock = vi.fn().mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/features") && !init) return response(catalog);
      if (url.endsWith("/harness-prompt/preview") && init?.method === "POST") return response(harnessPromptPreview);
      if (url.endsWith("/preview-harness-prompt/start") && init?.method === "POST") return response(harnessPromptRun, 202);
      throw new Error(`unexpected request ${input}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<Phase1DefectsPage />);

    fireEvent.click(await screen.findByText("候选 / 测试"));
    fireEvent.click(await screen.findByText("生成 Harness 自然语言 Prompt"));
    fireEvent.click(screen.getByText("生成执行预览").closest("button") as HTMLButtonElement);

    expect(await screen.findByText(/这里只生成Prompt/)).toBeInTheDocument();
    fireEvent.click(await screen.findByRole("checkbox"));
    fireEvent.click(screen.getByRole("button", { name: /确认并开始测试/ }));

    const prompt = await screen.findByLabelText("Harness 自然语言 Prompt");
    expect(prompt).toHaveTextContent("请在 OTel Demo 2.2.0 中验证");
    expect(prompt).toHaveTextContent("k8s_ro、telemetry_ro、source_ro 和 chaos_control");
    expect(prompt).toHaveTextContent("最后用自然语言给出完整时间线");
    expect(prompt).toHaveTextContent("可直接复制");
    expect(prompt).not.toHaveTextContent("run_harness_trial.py");
    expect(prompt).not.toHaveTextContent("--execute");
  });

  it("后端返回 null 耗时时保持页面可用", async () => {
    const fetchMock = vi.fn().mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/features") && !init) return response(catalog);
      if (url.endsWith("/git-clone/preview") && init?.method === "POST") return response(preview);
      if (url.endsWith("/preview-1/start") && init?.method === "POST") return response(queuedRun, 202);
      return response(queuedRun);
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<Phase1DefectsPage />);

    fireEvent.click((await screen.findByText("生成执行预览")).closest("button") as HTMLButtonElement);
    fireEvent.click(await screen.findByRole("checkbox"));
    fireEvent.click(screen.getByRole("button", { name: /确认并开始测试/ }));

    expect(await screen.findAllByText("等待执行")).toHaveLength(2);
    expect(screen.getByRole("heading", { name: "第一阶段 · 核心功能测试台" })).toBeInTheDocument();
  });
});
