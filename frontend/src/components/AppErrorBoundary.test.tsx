import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import AppErrorBoundary from "./AppErrorBoundary";

function BrokenPage(): never {
  throw new Error("fixture render failure");
}

afterEach(() => vi.restoreAllMocks());

describe("AppErrorBoundary", () => {
  it("把页面异常转换为可恢复界面", () => {
    vi.spyOn(console, "error").mockImplementation(() => undefined);
    render(<AppErrorBoundary><BrokenPage /></AppErrorBoundary>);

    expect(screen.getByText("页面渲染失败")).toBeInTheDocument();
    expect(screen.getByText("fixture render failure")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "刷新页面" })).toBeInTheDocument();
  });
});
