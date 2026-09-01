import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import Stage2MatrixResultsPage from "./Stage2MatrixResultsPage";

function response(data: unknown) {
  return Promise.resolve(new Response(JSON.stringify(data), { status: 200, headers: { "Content-Type": "application/json" } }));
}

const trial = {
  campaign_id: "campaign-1234567890abcdef",
  request_id: "matrix-otel-test-0001-gpt-5-6-sol-codex",
  source_matrix_id: "matrix-otel-test-0001",
  trial_id: "campaign-1234567890abcdef-codex-c0-1",
  harness: "codex",
  model: "gpt-5.6-sol",
  case_id: "C0",
  agent_verdict: "INCONCLUSIVE",
  platform_valid: true,
  diagnostic_only: false,
  fault_active: true,
  effect_verified: true,
  agent_recovery_verified: false,
  controller_cleanup_verified: true,
  business_recovery_verified: true,
  target: { namespace: "otel-demo", component: "cart", kind: "Pod", name: "cart-a", uid: "uid-a" },
  validation_error: "structured result missing",
  started_at: "2026-09-01T00:00:00Z",
  finished_at: "2026-09-01T00:03:00Z",
  duration_seconds: 180,
  artifact_count: 4,
};

const matrix = {
  schema_version: "stage2-matrix-inspection.v1",
  matrix_id: "matrix-otel-test-0001",
  report: {
    system: "otel-demo",
    prompt: "run",
    generated_at: "2026-09-01T00:03:00Z",
    score_definition: "eligible only",
    score_table: [{
      harness: "codex",
      model: "gpt-5.6-sol",
      score: null,
      pass: 0,
      fail: 0,
      inconclusive: 1,
      case_invalid: 0,
      valid_trials: 1,
      completed_trials: 1,
      expected_trials: 1,
      agent_recovery_verified: 0,
      controller_fallbacks: 1,
    }],
    key_findings: {},
  },
  request: {},
  summary: {
    expected_trials: 56,
    completed_trials: 56,
    platform_valid: 22,
    platform_invalid: 34,
    diagnostic_only: 49,
    fault_active: 32,
    effect_verified: 32,
    agent_recovery_verified: 0,
    controller_cleanup_verified: 56,
    business_recovery_verified: 56,
    verdict_counts: { INCONCLUSIVE: 22, CASE_INVALID: 34 },
  },
  integrity: {
    all_valid: true,
    verified_count: 9,
    expected_count: 9,
    matrix: { valid: true, checked_files: 6, errors: [] },
    campaigns: [],
  },
  source_matrices: ["matrix-otel-test-0001"],
  trials: [trial],
};

const detail = {
  schema_version: "stage2-trial-inspection.v1",
  matrix_id: matrix.matrix_id,
  summary: trial,
  result: {},
  agent: {
    harness_report: { status: "failed" },
    stdout: { available: true, text: "真实 Agent 响应", truncated: false },
    stderr: { available: false, text: "", truncated: false },
    lifecycle_events: [],
  },
  controller: {
    events: [{ kind: "trial_started", occurred_at: "2026-09-01T00:00:00Z", source_matrix_id: matrix.matrix_id, payload: { trial_id: trial.trial_id } }],
    disturbances: [],
    permission_restore: {},
    environment_reset: {},
  },
  oracle: { recovery: {}, fault_effect_evidence: { latency_delta_ms: 1000 } },
  runtime: { context: {}, capability: {} },
  files: [],
};

afterEach(() => vi.restoreAllMocks());

describe("Stage2MatrixResultsPage", () => {
  it("shows the evidence boundary and opens a real Trial response", async () => {
    const fetchMock = vi.fn().mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/matrices")) return response({ matrices: [{ matrix_id: matrix.matrix_id, completed_trial_count: 56, expected_trial_count: 56 }] });
      if (url.endsWith(`/matrices/${matrix.matrix_id}`)) return response(matrix);
      if (url.endsWith(`/trials/${trial.trial_id}`)) return response(detail);
      throw new Error(`unexpected request ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<Stage2MatrixResultsPage />);

    expect(await screen.findByText("Stage2 实验矩阵审计")).toBeInTheDocument();
    expect(screen.getByText("实验执行完整，但目前没有可计分的 PASS/FAIL")).toBeInTheDocument();
    expect(screen.getByText("9/9 manifests")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "codex gpt-5.6-sol C0 INCONCLUSIVE" }));
    expect(await screen.findByText("真实 Agent 响应")).toBeInTheDocument();
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(expect.stringContaining(`/trials/${trial.trial_id}`), expect.anything()));
  });
});
