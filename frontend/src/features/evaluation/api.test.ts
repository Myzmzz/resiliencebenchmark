import { afterEach, describe, expect, it, vi } from "vitest";
import { compileEvaluation, EvaluationApiError, evaluationEventUrl, listEvaluationTasks } from "./api";
import type { CompileEvaluationRequest } from "./types";
import { taskList } from "./test/fixtures";

afterEach(() => vi.restoreAllMocks());

describe("evaluation api", () => {
  it("构造任务列表查询并解析真实响应", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify(taskList), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    const result = await listEvaluationTasks({ status: "RUNNING", environmentId: "env-dev", q: "多系统", page: 2, pageSize: 10 });
    expect(result.total).toBe(2);
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/evaluation/tasks?status=RUNNING&environment_id=env-dev&q=%E5%A4%9A%E7%B3%BB%E7%BB%9F&page=2&page_size=10",
      expect.objectContaining({ signal: undefined }),
    );
  });

  it("题目编译使用 POST JSON", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ valid: true }), { status: 200 })));
    const request = { environmentId: "env", systemIds: ["system"], harnesses: [{ harnessId: "codex", modelIds: ["gpt"] }], mcpServerIds: ["k8s_ro"], questionSetId: "core", questionIds: ["EPI-1"], strategy: { questionOrder: "FIXED", maxTrialsPerUnit: 5, retryPerPhase: 1, stopOnSafetyViolation: true, scoringPolicyId: "score", evaluatorId: "oracle", promptStrategyId: "prompt" } } satisfies CompileEvaluationRequest;
    await compileEvaluation(request);
    expect(fetch).toHaveBeenCalledWith("/api/v1/evaluation/compile", expect.objectContaining({ method: "POST", body: JSON.stringify(request) }));
  });

  it("错误响应转换为 EvaluationApiError", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ title: "Conflict", status: 409, detail: "环境忙碌" }), { status: 409, headers: { "Content-Type": "application/problem+json" } })));
    await expect(listEvaluationTasks({})).rejects.toMatchObject({ status: 409, message: "环境忙碌" } satisfies Partial<EvaluationApiError>);
  });

  it("SSE URL 不携带 after_sequence", () => {
    expect(evaluationEventUrl("EVAL/1")).toBe("/api/v1/evaluation/tasks/EVAL%2F1/events");
    expect(evaluationEventUrl("EVAL/1")).not.toContain("after");
  });
});
