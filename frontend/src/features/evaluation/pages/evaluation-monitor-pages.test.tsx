import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import EvaluationTaskMonitorPage from "./EvaluationTaskMonitorPage";
import EvaluationUnitDetailPage from "./EvaluationUnitDetailPage";
import { taskDetail, unitDetail } from "../test/fixtures";

class EventSourceStub {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSED = 2;
  readonly CONNECTING = 0;
  readonly OPEN = 1;
  readonly CLOSED = 2;
  url: string;
  readyState = 1;
  withCredentials = false;
  onopen: ((event: Event) => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;
  constructor(url: string | URL) { this.url = String(url); queueMicrotask(() => this.onopen?.(new Event("open"))); }
  addEventListener() {}
  removeEventListener() {}
  close() { this.readyState = 2; }
  dispatchEvent() { return true; }
}

afterEach(() => vi.restoreAllMocks());

describe("evaluation monitoring pages", () => {
  it("任务详情展示 Harness 轨道、矩阵和环境租约", async () => {
    vi.stubGlobal("EventSource", EventSourceStub);
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify(taskDetail), { status: 200 })));
    render(<MemoryRouter initialEntries={["/evaluation/monitoring/EVAL-001"]}><Routes><Route path="/evaluation/monitoring/:taskId" element={<EvaluationTaskMonitorPage />} /><Route path="/evaluation/monitoring/:taskId/units/:unitId" element={<div>题目详情目标</div>} /></Routes></MemoryRouter>);
    expect(await screen.findByText("Harness 执行轨道")).toBeInTheDocument();
    expect(screen.getByText("评测单元矩阵")).toBeInTheDocument();
    expect(screen.getByText("环境租约")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /GPT-5.6 CPU 资源压力 RUNNING/ }));
    expect(await screen.findByText("题目详情目标")).toBeInTheDocument();
  });

  it("题目详情展示主故障、动态扰动、Trial 和 Ground Truth 边界", async () => {
    vi.stubGlobal("EventSource", EventSourceStub);
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify(unitDetail), { status: 200 })));
    render(<MemoryRouter initialEntries={["/evaluation/monitoring/EVAL-001/units/UNIT-002"]}><Routes><Route path="/evaluation/monitoring/:taskId/units/:unitId" element={<EvaluationUnitDetailPage />} /></Routes></MemoryRouter>);
    expect(await screen.findByText("主故障")).toBeInTheDocument();
    expect(screen.getByText("动态扰动")).toBeInTheDocument();
    expect(screen.getByText("telemetry_instability")).toBeInTheDocument();
    expect(screen.getByText("题目执行期间不展示 Ground Truth")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: "Trial 历史" }));
    expect(await screen.findByText("Trial 1")).toBeInTheDocument();
  });
});
