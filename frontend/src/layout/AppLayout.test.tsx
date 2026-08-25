import { render, screen } from "@testing-library/react";
import { RouterProvider, createMemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { routeChildren } from "../routes";
import AppLayout from "./AppLayout";
import { menuSelectionForPath } from "./menuSelection";

function renderAt(path: string) {
  const router = createMemoryRouter(
    [{ path: "/", element: <AppLayout />, children: routeChildren }],
    { initialEntries: [path] },
  );
  render(<RouterProvider router={router} />);
}

describe("AppLayout", () => {
  it("侧边栏包含四个一级模块", () => {
    renderAt("/environments/infrastructure");
    for (const label of ["环境与资源", "智能体", "题库", "评测"]) {
      expect(screen.getByText(label)).toBeInTheDocument();
    }
  });

  it("未实现页面渲染占位组件与里程碑标签", () => {
    renderAt("/episodes/defects");
    expect(screen.getByRole("heading", { name: /缺陷库/ })).toBeInTheDocument();
    expect(screen.getByText("M4")).toBeInTheDocument();
  });

  it("评测深层路由保持对应菜单选中", () => {
    expect(menuSelectionForPath("/evaluation/tasks/new")).toBe("/evaluation/tasks");
    expect(menuSelectionForPath("/evaluation/monitoring/EVAL-1/units/UNIT-1")).toBe("/evaluation/monitoring");
    expect(menuSelectionForPath("/evaluation/results/EVAL-1")).toBe("/evaluation/results");
  });
});
