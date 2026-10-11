import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import CodeGraphConsole from "./CodeGraphConsole";
import { previewCoreFeature, startCoreRun } from "../services/api";
import type { CoreTestPreview, CoreTestRun } from "../types/phase1";

vi.mock("../services/api", () => ({
  previewCoreFeature: vi.fn(),
  startCoreRun: vi.fn(),
}));

const preview: CoreTestPreview = {
  previewId: "preview-codegraph",
  featureId: "codegraph-query",
  createdAt: "2026-08-27T00:00:00Z",
  expiresAt: "2026-08-27T00:15:00Z",
  normalizedInputs: {},
  operations: ["调用 codegraph context"],
  warnings: [],
  confirmationText: "confirmed",
  inputDigest: "a".repeat(64),
};

const run: CoreTestRun = {
  runId: "phase1-codegraph-1",
  featureId: "codegraph-query",
  status: "running",
  createdAt: "2026-08-27T00:00:01Z",
  startedAt: "2026-08-27T00:00:01Z",
  finishedAt: null,
  durationSeconds: null,
  inputs: {
    source_path: "/workspace/source",
    source_identity: "fixture-source",
    command: "codegraph",
    operation: "context",
    expression: "checkout",
  },
  logs: [],
  result: null,
  error: null,
};

describe("CodeGraphConsole", () => {
  it("发送消息时启动一次只读 CodeGraph 查询", async () => {
    vi.mocked(previewCoreFeature).mockResolvedValue(preview);
    vi.mocked(startCoreRun).mockResolvedValue(run);
    const onRun = vi.fn();

    render(<CodeGraphConsole baseInputs={run.inputs} latestRun={run} onRun={onRun} />);

    fireEvent.change(screen.getByLabelText("CodeGraph 消息"), {
      target: { value: "trace checkout to cart" },
    });
    fireEvent.click(screen.getByRole("button", { name: /发送并执行/ }));

    await waitFor(() => expect(onRun).toHaveBeenCalledWith(run));
    expect(previewCoreFeature).toHaveBeenCalledWith("codegraph-query", expect.objectContaining({
      source_path: "/workspace/source",
      source_identity: "fixture-source",
      operation: "context",
      expression: "trace checkout to cart",
      max_nodes: 120,
    }));
    expect(startCoreRun).toHaveBeenCalledWith(preview);
    expect(screen.getByText("trace checkout to cart")).toBeInTheDocument();
  });
});
