import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import EvaluationTasksPage from "./EvaluationTasksPage";
import EvaluationTaskCreatePage from "./EvaluationTaskCreatePage";
import EvaluationResultDetailPage from "./EvaluationResultDetailPage";
import { evaluationOptions, resultDetail, reuseValidation, taskDetail, taskList } from "../test/fixtures";

function response(data: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(data), { status, headers: { "Content-Type": "application/json" } }));
}

afterEach(() => vi.restoreAllMocks());

describe("evaluation pages", () => {
  it("任务列表展示多系统矩阵并可进入运行监控", async () => {
    vi.stubGlobal("fetch", vi.fn().mockImplementation(() => response(taskList)));
    render(<MemoryRouter initialEntries={["/evaluation/tasks"]}><Routes><Route path="/evaluation/tasks" element={<EvaluationTasksPage />} /><Route path="/evaluation/monitoring/:taskId" element={<div>监控目标页</div>} /></Routes></MemoryRouter>);
    expect(await screen.findByText("多系统韧性评测")).toBeInTheDocument();
    expect(screen.getAllByText(/2 Harness · 2 模型/)).toHaveLength(2);
    fireEvent.click(screen.getByRole("button", { name: "运行监控" }));
    expect(await screen.findByText("监控目标页")).toBeInTheDocument();
  });

  it("创建向导选择繁忙环境后进入多系统 Harness 步骤并锁定必选 MCP", async () => {
    vi.stubGlobal("fetch", vi.fn().mockImplementation((input: RequestInfo | URL) => {
      if (String(input).endsWith("/options")) return response(evaluationOptions);
      return response(taskDetail);
    }));
    render(<MemoryRouter initialEntries={["/evaluation/tasks/new"]}><Routes><Route path="/evaluation/tasks/new" element={<EvaluationTaskCreatePage />} /></Routes></MemoryRouter>);
    fireEvent.change(await screen.findByPlaceholderText("输入评测任务名称"), { target: { value: "完整前端评测" } });
    fireEvent.click(screen.getByText("研发测试集群"));
    fireEvent.click(screen.getByRole("button", { name: /下一步：系统与 Harness/ }));
    expect(await screen.findByText("选择被测系统")).toBeInTheDocument();
    const systemRow = screen.getByText("Train Ticket · v0.3").closest(".evaluation-option-row");
    expect(systemRow).not.toBeNull();
    fireEvent.click(within(systemRow as HTMLElement).getByRole("checkbox"));
    const harnessRow = screen.getByText("Codex").closest(".evaluation-option-row");
    expect(harnessRow).not.toBeNull();
    fireEvent.click(within(harnessRow as HTMLElement).getByRole("checkbox"));
    await waitFor(() => expect(screen.getAllByText("必选")).toHaveLength(2));
    const requiredCheckboxes = screen.getAllByText("必选").map((tag) => within(tag.closest(".evaluation-option-row") as HTMLElement).getByRole("checkbox"));
    expect(requiredCheckboxes.every((item) => item.hasAttribute("disabled"))).toBe(true);
  });

  it("结果详情只对系统 Harness 和模型评分，语言仅作覆盖", async () => {
    vi.stubGlobal("fetch", vi.fn().mockImplementation(() => response(resultDetail)));
    render(<MemoryRouter initialEntries={["/evaluation/results/EVAL-RESULT-1"]}><Routes><Route path="/evaluation/results/:taskId" element={<EvaluationResultDetailPage />} /></Routes></MemoryRouter>);
    expect(await screen.findByText("被测系统 × Harness 得分矩阵")).toBeInTheDocument();
    expect(screen.getByText("模型得分")).toBeInTheDocument();
    expect(screen.queryByText("语言得分")).not.toBeInTheDocument();
    expect(screen.getByText(/语言只作为系统与目标服务的实现覆盖信息/)).toBeInTheDocument();
  });

  it("复用抽屉验证配置后创建新的排队任务", async () => {
    const fetchMock = vi.fn().mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/results/EVAL-RESULT-1")) return response(resultDetail);
      if (url.endsWith("/reuse/validation")) return response(reuseValidation);
      if (url.endsWith("/options")) return response(evaluationOptions);
      if (url.endsWith("/reuse") && init?.method === "POST") return response({ ...taskDetail, taskId: "EVAL-REUSED", businessStatus: "PENDING", phase: "QUEUED" }, 201);
      throw new Error(`unexpected request ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<MemoryRouter initialEntries={["/evaluation/results/EVAL-RESULT-1?reuse=1"]}><Routes><Route path="/evaluation/results/:taskId" element={<EvaluationResultDetailPage />} /><Route path="/evaluation/tasks" element={<div>任务列表目标</div>} /></Routes></MemoryRouter>);
    expect(await screen.findByText("复用评测任务")).toBeInTheDocument();
    expect(screen.getByText(/复用只复制配置引用/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "创建并排队" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/api/v1/evaluation/tasks/EVAL-RESULT-1/reuse", expect.objectContaining({ method: "POST" })));
  });
});
