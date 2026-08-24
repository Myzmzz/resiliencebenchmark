import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import FaultDisturbancePanel from "./FaultDisturbancePanel";
import HarnessTrack from "./HarnessTrack";
import UnitMatrix from "./UnitMatrix";
import { taskDetail, unitDetail } from "../test/fixtures";

describe("evaluation components", () => {
  it("Harness 轨道显示完成、运行和等待状态", () => {
    const items = [...taskDetail.harnessProgress, { harnessId: "claude", harnessName: "Claude Code", status: "PENDING" as const, completedUnits: 0, totalUnits: 6, modelProgress: [] }];
    render(<HarnessTrack items={items} />);
    expect(screen.getByText("Codex")).toBeInTheDocument();
    expect(screen.getByText("BladeAI")).toBeInTheDocument();
    expect(screen.getByText("Claude Code")).toBeInTheDocument();
  });

  it("评测矩阵单元可点击下钻", () => {
    const onSelect = vi.fn();
    render(<UnitMatrix units={taskDetail.units} systemId="train-ticket" onSelect={onSelect} />);
    fireEvent.click(screen.getByRole("button", { name: /GPT-5.6 服务网络延迟 PASS/ }));
    expect(onSelect).toHaveBeenCalledWith(expect.objectContaining({ unitId: "UNIT-001" }));
  });

  it("故障与扰动分别展示且包含预算", () => {
    render(<FaultDisturbancePanel fault={unitDetail.mainFault} disturbances={unitDetail.disturbances} budget={unitDetail.disturbanceBudget} />);
    expect(screen.getByText("主故障")).toBeInTheDocument();
    expect(screen.getByText("动态扰动")).toBeInTheDocument();
    expect(screen.getByText("telemetry_instability")).toBeInTheDocument();
    expect(screen.getByText(/扰动预算 1\/2/)).toBeInTheDocument();
  });
});
