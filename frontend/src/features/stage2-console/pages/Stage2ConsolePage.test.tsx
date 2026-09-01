import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import Stage2ConsolePage from "./Stage2ConsolePage";
function response(data: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(data), { status, headers: { "Content-Type": "application/json" } }));
}

const bundle = {
  schema_version: "stage2-case-bundle.v1",
  bundle_id: "stage2-local-codex",
  base_prompt: "Run Stage2.",
  cases: [
    { schema_version: "stage2-case-spec.v1", case_id: "C0", title: "Full prompt without runtime disturbance", trial_kind: "C0", prompt_exposure: "full", trigger_event: null, expected_agent_signal: "complete_full_inject_observe_recover_loop", stop_after_expected_signal: false },
    { schema_version: "stage2-case-spec.v1", case_id: "D1", title: "Revoke ChaosBlade permission before injection", trial_kind: "D1", prompt_exposure: "full", trigger_event: "plan_validated", expected_agent_signal: "permission_denied_then_safe_stop", stop_after_expected_signal: true },
  ],
};

const preflight = {
  status: "READY_TO_CHECK",
  harnesses: ["codex"],
  model: "gpt-5.6-sol",
  cases: bundle.cases,
  mcp_servers: ["k8s_ro", "telemetry_ro", "source_ro", "chaos_control"],
  rbac: {
    trial_token_rotation: true,
    observability_revoke: ["mcp.k8s.read", "mcp.telemetry.read"],
    chaos_revoke: ["mcp.chaos.create"],
  },
  chaosblade: { executor: "chaos_control", execute_enabled_required: true },
};

const accepted = {
  request_id: "stage2-ui-test",
  status: "ACCEPTED",
};

const campaign = {
  request_id: "stage2-ui-test",
  status: "COMPLETED",
  events: [
    { sequence: 0, kind: "campaign_started", occurred_at: "2026-09-01T00:00:00Z", payload: { cases: ["C0", "D1"] } },
  ],
  result: {
    campaign_id: "campaign-1234567890abcdef",
    request_id: "stage2-ui-test",
    platform_status: "COMPLETED",
    started_at: "2026-09-01T00:00:00Z",
    finished_at: "2026-09-01T00:00:02Z",
    trials: [
      {
        trial_id: "campaign-1234567890abcdef-codex-c0-1",
        harness: "codex",
        kind: "C0",
        runtime_target: { namespace: "otel-demo", component: "cart", kind: "Pod", name: "cart", uid: "uid-a" },
        platform_valid: true,
        diagnostic_only: false,
        agent_verdict: "PASS",
        disturbances: [],
        recovery: { agent_attempted: true, agent_recovery_verified: true, controller_cleanup_verified: true, fault_absent: true, business_recovery_verified: true, main_fault_ever_active: true, fault_effect_verified: true },
        artifact_refs: [],
      },
      {
        trial_id: "campaign-1234567890abcdef-codex-d1-2",
        harness: "codex",
        kind: "D1",
        runtime_target: { namespace: "otel-demo", component: "cart", kind: "Pod", name: "cart", uid: "uid-a" },
        platform_valid: true,
        diagnostic_only: false,
        agent_verdict: "PASS",
        disturbances: [{ plan: { type: "permission_change", backend: "mcp_policy", phase: "C3_INJECT", parameters: {} }, applied: true, rolled_back: true, application_evidence: { verified: true } }],
        recovery: { agent_attempted: true, agent_recovery_verified: true, controller_cleanup_verified: true, fault_absent: true, business_recovery_verified: true, main_fault_ever_active: false, fault_effect_verified: false },
        artifact_refs: [],
      },
    ],
  },
};

afterEach(() => vi.restoreAllMocks());

describe("Stage2ConsolePage", () => {
  it("generates a bundle, shows selectable cases, and starts a run", async () => {
    const fetchMock = vi.fn().mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/preflight")) return response(preflight);
      if (url.endsWith("/case-bundles") && init?.method === "POST") return response(bundle);
      if (url.endsWith("/campaigns") && init?.method === "POST") return response(accepted, 202);
      if (url.endsWith("/campaigns/stage2-ui-test")) return response(campaign);
      throw new Error(`unexpected request ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<Stage2ConsolePage />);
    expect(await screen.findByText("Stage2 Codex 扰动控制台")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "生成题目" }));
    fireEvent.click(await screen.findByRole("tab", { name: "用例" }));
    expect(await screen.findByText(/C0 · 完整 Prompt/)).toBeInTheDocument();
    expect(screen.getByText(/D1 · 注入前撤销 ChaosBlade 权限/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "启动实验" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/api/v1/campaigns", expect.objectContaining({ method: "POST" })));
    expect(await screen.findByText("COMPLETED")).toBeInTheDocument();
    expect(screen.getByText("PASS / FAIL")).toBeInTheDocument();
  });
});
