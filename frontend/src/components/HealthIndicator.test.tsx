import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import HealthIndicator from "./HealthIndicator";

const okBody = {
  service: "ok",
  version: "0.1.0",
  repo: { path: "/x", exists: true, factory_config_found: true },
};

afterEach(() => vi.restoreAllMocks());

describe("HealthIndicator", () => {
  it("服务与仓库均正常时显示已连接", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(
      new Response(JSON.stringify(okBody), { status: 200 }),
    ));
    render(<HealthIndicator />);
    await waitFor(() => expect(screen.getByText("服务已连接")).toBeInTheDocument());
  });

  it("仓库无效时提示检查 BENCHMARK_REPO_PATH", async () => {
    const bad = { ...okBody, repo: { ...okBody.repo, factory_config_found: false } };
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(
      new Response(JSON.stringify(bad), { status: 200 }),
    ));
    render(<HealthIndicator />);
    await waitFor(() => expect(screen.getByText("仓库路径无效")).toBeInTheDocument());
  });

  it("服务不可达时显示未连接", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("network")));
    render(<HealthIndicator />);
    await waitFor(() => expect(screen.getByText("服务未连接")).toBeInTheDocument());
  });
});
