const { chromium } = require("playwright");
const fs = require("node:fs");

const now = "2026-08-25T10:00:00Z";
const environment = { id: "env-dev", name: "研发测试集群", status: "BUSY", currentTask: { taskId: "EVAL-001", name: "多系统韧性评测", progressPercent: 47, phase: "EXECUTING" }, queueSize: 1, lastCheckedAt: now };
const systems = [
  { id: "train-ticket", name: "Train Ticket", version: "v0.3", namespace: "train-ticket", status: "READY", serviceCount: 51, sourceCommit: "5f7c21d", imageLocked: 51, imageTotal: 51, codeGraphStatus: "AVAILABLE", languages: ["Java"] },
  { id: "otel-demo", name: "OTel Demo", version: "v1.12.0", namespace: "otel-demo", status: "READY", serviceCount: 22, sourceCommit: "8a4f9c2", imageLocked: 22, imageTotal: 22, codeGraphStatus: "AVAILABLE", languages: ["Go", "Java", "Python"] },
];
const options = {
  environments: [environment, { ...environment, id: "env-free", name: "空闲环境", status: "IDLE", currentTask: undefined, queueSize: 0 }], systems,
  harnesses: [{ id: "codex", name: "Codex", status: "AVAILABLE", modelIds: ["gpt-5.6"], requiredMcpIds: ["k8s_ro", "telemetry_ro"] }, { id: "bladeai", name: "BladeAI", status: "AVAILABLE", modelIds: ["gpt-5.6"], requiredMcpIds: ["k8s_ro", "telemetry_ro"] }],
  models: [{ id: "gpt-5.6", name: "GPT-5.6", status: "AVAILABLE" }],
  mcpServers: [{ id: "k8s_ro", name: "k8s_ro", status: "CONNECTED" }, { id: "telemetry_ro", name: "telemetry_ro", status: "CONNECTED" }, { id: "source_ro", name: "source_ro", status: "CONNECTED" }],
  questions: [{ id: "EPI-RES-003", title: "CPU 资源压力", category: "资源", applicableSystemIds: systems.map((s) => s.id), targetService: "inventory", maxTrials: 5, status: "AVAILABLE" }],
  questionSets: [{ id: "core", name: "韧性核心题库", version: "v1", questionIds: ["EPI-RES-003"] }],
  scoringPolicies: [{ id: "episode-score-v1", name: "episode-score-v1" }], evaluators: [{ id: "independent-oracle-v1", name: "independent-oracle-v1" }], promptStrategies: [{ id: "full-lifecycle-v1", name: "full-lifecycle-v1" }],
};
const units = [{ unitId: "UNIT-001", systemId: "train-ticket", systemName: "Train Ticket", harnessId: "bladeai", harnessName: "BladeAI", modelId: "gpt-5.6", modelName: "GPT-5.6", questionId: "EPI-RES-003", questionTitle: "CPU 资源压力", questionIndex: 1, status: "RUNNING", phase: "EXECUTING", currentTrial: 2, maxTrials: 5, targetService: "inventory" }];
const task = { taskId: "EVAL-001", name: "多系统韧性评测", environmentId: "env-dev", environmentName: "研发测试集群", systems: systems.map(({ id, name, version }) => ({ id, name, version })), harnessNames: ["Codex", "BladeAI"], modelCount: 1, uniqueQuestionCount: 1, evaluationUnitCount: 4, completedUnitCount: 1, businessStatus: "RUNNING", phase: "EXECUTING", createdAt: now, startedAt: now, specFingerprint: "sha256:abc", harnessProgress: [{ harnessId: "codex", harnessName: "Codex", status: "COMPLETED", completedUnits: 2, totalUnits: 2, modelProgress: [{ modelId: "gpt-5.6", modelName: "GPT-5.6", completedUnits: 2, totalUnits: 2, current: false }] }, { harnessId: "bladeai", harnessName: "BladeAI", status: "RUNNING", completedUnits: 1, totalUnits: 2, modelProgress: [{ modelId: "gpt-5.6", modelName: "GPT-5.6", completedUnits: 1, totalUnits: 2, current: true }] }], systemProgress: systems.map((s) => ({ systemId: s.id, systemName: s.name, completedUnits: 1, totalUnits: 2, status: "RUNNING" })), currentUnit: units[0], units, recentEvents: [{ id: "evt-1", sequence: 1, taskId: "EVAL-001", unitId: "UNIT-001", type: "FAULT_APPLIED", phase: "EXECUTING", occurredAt: now, message: "主故障已应用" }], lease: { status: "HELD", heartbeatAt: now, holderTaskId: "EVAL-001" } };
const taskList = { items: [task], total: 1, summary: { pending: 0, running: 1, completed: 0, occupiedEnvironments: 1, environments: 2 }, occupancies: [{ environment, queue: [] }] };
const monitoring = { environments: [{ environment, activeTask: task, queueSize: 1 }] };
const unit = { ...units[0], taskId: "EVAL-001", environmentName: environment.name, systemVersion: "v0.3", target: { pod: "inventory-abc", uid: "uid-1", node: "worker-01", container: "inventory", confirmed: true }, phaseStartedAt: now, unitStartedAt: now, disturbanceBudget: { used: 1, total: 2 }, mainFault: { type: "CPU 资源压力", executor: "ChaosBlade", experimentId: "exp-1", target: "inventory-abc", parameters: { percent: 80 }, status: "EFFECT_OBSERVED", startedAt: now }, disturbances: [{ disturbanceId: "d-1", type: "telemetry_instability", trigger: "主故障效果确认后", parameters: { delay: "8s" }, status: "RUNNING", evidenceRef: "telemetry-1" }], liveMetrics: [{ id: "success", label: "成功率", value: "82%", baseline: "99.8%", status: "CRITICAL" }], gates: [{ id: "fault", label: "主故障效果", status: "PASS" }, { id: "recovery", label: "恢复验证", status: "PENDING" }], trials: [{ trialId: "t-1", attempt: 1, status: "COMPLETED", outcome: "INCONCLUSIVE", cleaned: true }, { trialId: "t-2", attempt: 2, status: "RUNNING", cleaned: false }], events: task.recentEvents, artifactRefs: [{ label: "运行上下文", href: "/artifact/context.json" }] };
const result = { taskId: "EVAL-RESULT", name: "云原生多系统韧性评测", finishedAt: now, systems: systems.map((s) => s.name), harnessCount: 2, modelCount: 1, totalUnits: 4, validUnits: 3, pass: 2, fail: 1, caseInvalid: 1, score: 66.7, terminalStatus: "COMPLETED", environmentName: environment.name, durationSeconds: 3600, systemResults: systems.map((s, i) => ({ systemId: s.id, systemName: s.name, version: s.version, languages: s.languages, validUnits: i ? 1 : 2, totalUnits: 2, score: i ? 62 : 71, bestHarnessName: "Codex" })), harnesses: [{ id: "codex", name: "Codex" }, { id: "bladeai", name: "BladeAI" }], scoreMatrix: [{ systemId: "train-ticket", harnessId: "codex", score: 72, validUnits: 1, totalUnits: 1 }, { systemId: "train-ticket", harnessId: "bladeai", score: 70, validUnits: 1, totalUnits: 1 }, { systemId: "otel-demo", harnessId: "codex", score: 65, validUnits: 1, totalUnits: 1 }, { systemId: "otel-demo", harnessId: "bladeai", score: 0, validUnits: 0, totalUnits: 1 }], modelScores: [{ modelId: "gpt-5.6", modelName: "GPT-5.6", score: 66.7, validUnits: 3 }], unitResults: units, recovery: [{ id: "cleanup", label: "环境清理", status: "PASS", value: "通过" }], oracleSummary: [{ id: "validity", label: "有效性门禁", passed: 3, failed: 0, invalid: 1 }], artifacts: [{ label: "评测报告.md", href: "/artifact/report.md" }] };
const results = { items: [result], total: 1 };
const reuse = { sourceTaskId: "EVAL-RESULT", specFingerprint: "sha256:reuse", systems: systems.map((s) => ({ id: s.id, label: `${s.name} ${s.version}`, available: true })), harnesses: options.harnesses.map((h) => ({ id: h.id, label: h.name, available: true })), models: options.models.map((m) => ({ id: m.id, label: m.name, available: true })), mcpServers: options.mcpServers.map((m) => ({ id: m.id, label: m.name, required: m.id !== "source_ro", available: true })), questionStrategyLabel: "core-v1", scoringPolicyLabel: "episode-score-v1", evaluatorLabel: "independent-oracle-v1", evaluationUnitCount: 4, checks: [{ id: "all", label: "全部引用可用", passed: true }], canReuseDirectly: true };

function json(route, body, status = 200) { return route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) }); }

(async () => {
  let browser;
  try {
    browser = await chromium.launch({ headless: true });
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    const consoleErrors = [];
    page.on("console", (msg) => { if (msg.type() === "error") consoleErrors.push(msg.text()); });

    await page.goto("http://127.0.0.1:5173/evaluation/tasks", { waitUntil: "domcontentloaded" });
    await page.getByText("评测数据加载失败").waitFor();

    await page.route("**/api/v1/meta/health", (route) => json(route, { service: "ok", version: "1", repo: { path: "/repo", exists: true, factory_config_found: true } }));
    await page.route("**/api/v1/evaluation/**", (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    if (path.endsWith("/options")) return json(route, options);
    if (path.endsWith("/monitoring")) return json(route, monitoring);
    if (path.endsWith("/units/UNIT-001")) return json(route, unit);
    if (path.endsWith("/events")) return route.abort();
    if (path.endsWith("/reuse/validation")) return json(route, reuse);
    if (path.endsWith("/results/EVAL-RESULT")) return json(route, result);
    if (path.endsWith("/results")) return json(route, results);
    if (path.endsWith("/tasks/EVAL-001")) return json(route, task);
    if (path.endsWith("/tasks")) return json(route, taskList);
    return json(route, { title: "Not found", status: 404 }, 404);
    });

    const checks = [
    ["/evaluation/tasks", "评测任务"],
    ["/evaluation/tasks/new", "新增评测任务"],
    ["/evaluation/monitoring", "运行监控"],
    ["/evaluation/monitoring/EVAL-001", "Harness 执行轨道"],
    ["/evaluation/monitoring/EVAL-001/units/UNIT-001", "动态扰动"],
    ["/evaluation/results", "结果分析"],
    ["/evaluation/results/EVAL-RESULT", "被测系统 × Harness 得分矩阵"],
    ];
    for (const [path, text] of checks) {
      await page.goto(`http://127.0.0.1:5173${path}`, { waitUntil: "domcontentloaded" });
      await page.getByText(text, { exact: false }).first().waitFor();
    }
    await page.getByRole("button", { name: "复用任务" }).click();
    await page.getByText("复用评测任务").waitFor();
    await page.waitForTimeout(500);
    await page.screenshot({ path: "/tmp/evaluation-frontend-smoke.png", fullPage: true });

    const unexpected = consoleErrors.filter((item) => !item.includes("ERR_FAILED") && !item.includes("Failed to load resource"));
    if (unexpected.length) throw new Error(`Browser console errors:\n${unexpected.join("\n")}`);
    fs.writeFileSync("/tmp/evaluation-frontend-smoke.json", JSON.stringify({ pages: checks.length, noBackendErrorState: true, reuseDrawer: true, screenshot: "/tmp/evaluation-frontend-smoke.png" }, null, 2));
  } finally {
    await browser?.close();
  }
})().catch((error) => { console.error(error); process.exitCode = 1; });
