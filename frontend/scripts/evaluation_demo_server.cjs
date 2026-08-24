const http = require("node:http");
const { randomUUID } = require("node:crypto");

const PORT = Number(process.env.EVALUATION_DEMO_PORT || 8000);
const now = () => new Date().toISOString();
const systems = [
  { id: "train-ticket", name: "Train Ticket", version: "v0.3", namespace: "train-ticket", status: "READY", serviceCount: 51, sourceCommit: "5f7c21d", imageLocked: 51, imageTotal: 51, codeGraphStatus: "AVAILABLE", languages: ["Java"] },
  { id: "otel-demo", name: "OTel Demo", version: "v1.12.0", namespace: "otel-demo", status: "READY", serviceCount: 22, sourceCommit: "8a4f9c2", imageLocked: 22, imageTotal: 22, codeGraphStatus: "AVAILABLE", languages: ["Go", "Java", "Python", "TypeScript", "C#"] },
  { id: "sock-shop", name: "Sock Shop", version: "v1.0", namespace: "sock-shop", status: "READY", serviceCount: 10, sourceCommit: "b28e11a", imageLocked: 10, imageTotal: 10, codeGraphStatus: "AVAILABLE", languages: ["Go", "Java", "Node.js"] },
];
const models = ["gpt-5.6", "gpt-5.5", "gpt-5.4"].map((id) => ({ id, name: id.toUpperCase(), status: "AVAILABLE" }));
const harnesses = ["codex", "bladeai", "claude-code"].map((id) => ({ id, name: id === "claude-code" ? "Claude Code" : id === "bladeai" ? "BladeAI" : "Codex", status: "AVAILABLE", description: "演示 Harness", modelIds: models.map((item) => item.id), requiredMcpIds: ["k8s_ro", "telemetry_ro"] }));
const mcpServers = [
  { id: "k8s_ro", name: "k8s_ro", status: "CONNECTED", description: "Kubernetes 只读资源与事件" },
  { id: "telemetry_ro", name: "telemetry_ro", status: "CONNECTED", description: "指标、Trace 与日志" },
  { id: "source_ro", name: "source_ro", status: "CONNECTED", description: "锁定源码快照" },
  { id: "codegraph_ro", name: "codegraph_ro", status: "CONNECTED", description: "CodeGraph 查询" },
];
const questionNames = ["服务网络延迟", "下游服务异常", "CPU 资源压力", "超时传播", "故障恢复验证", "Pod 终止验证", "连接耗尽", "磁盘 IO 压力", "内存压力", "配置漂移", "指标数据缺口", "安全阈值压力", "清理延迟"];
const questions = questionNames.map((title, index) => ({ id: `EPI-DEMO-${String(index + 1).padStart(3, "0")}`, title, category: index < 3 ? "网络" : index < 7 ? "服务异常" : index < 10 ? "资源" : "恢复与安全", applicableSystemIds: systems.map((item) => item.id), targetService: index % 2 ? "payment" : "inventory", maxTrials: 5, status: "AVAILABLE" }));
const environments = [
  { id: "env-dev", name: "研发测试集群", status: "BUSY", currentTask: { taskId: "EVAL-DEMO-RUNNING", name: "云原生多系统韧性评测", progressPercent: 47, currentUnitLabel: "BladeAI × GPT-5.5 × CPU 资源压力", phase: "EXECUTING" }, queueSize: 1, lastCheckedAt: now() },
  { id: "env-free", name: "Train Ticket 测试环境", status: "IDLE", queueSize: 0, lastCheckedAt: now() },
  { id: "env-recovery", name: "Sock Shop 测试环境", status: "RECOVERING", queueSize: 0, lastCheckedAt: now() },
];

const options = { dataMode: "DEMO", environments, systems, harnesses, models, mcpServers, questions, questionSets: [{ id: "multi-system-core-v1", name: "多系统韧性核心题库", version: "v1", questionIds: questions.map((item) => item.id) }], scoringPolicies: [{ id: "episode-score-v1", name: "episode-score-v1" }], evaluators: [{ id: "independent-oracle-v1", name: "independent-oracle-v1" }], promptStrategies: [{ id: "full-lifecycle-v1", name: "full-lifecycle-v1" }] };

function unitFor(index, status, outcome) {
  const system = systems[Math.floor(index / 117) % systems.length];
  const within = index % 117;
  const harness = harnesses[Math.floor(within / 39)];
  const model = models[Math.floor((within % 39) / 13)];
  const question = questions[within % 13];
  return { unitId: `UNIT-${String(index + 1).padStart(3, "0")}`, systemId: system.id, systemName: system.name, harnessId: harness.id, harnessName: harness.name, modelId: model.id, modelName: model.name, questionId: question.id, questionTitle: question.title, questionIndex: (within % 13) + 1, status, outcome, phase: status === "RUNNING" ? "EXECUTING" : undefined, currentTrial: status === "RUNNING" ? 2 : undefined, maxTrials: 5, targetService: question.targetService };
}
const runningUnits = Array.from({ length: 351 }, (_, index) => index < 165 ? unitFor(index, "COMPLETED", index % 17 === 0 ? "CASE_INVALID" : index % 5 === 0 ? "FAIL" : "PASS") : index === 165 ? unitFor(index, "RUNNING") : unitFor(index, "PENDING"));
const currentUnit = runningUnits[165];

function harnessProgress() {
  return harnesses.map((harness, index) => ({ harnessId: harness.id, harnessName: harness.name, status: index === 0 ? "COMPLETED" : index === 1 ? "RUNNING" : "PENDING", completedUnits: index === 0 ? 117 : index === 1 ? 48 : 0, totalUnits: 117, durationSeconds: index === 0 ? 5100 : undefined, modelProgress: models.map((model, modelIndex) => ({ modelId: model.id, modelName: model.name, completedUnits: index === 0 ? 39 : index === 1 && modelIndex === 0 ? 39 : index === 1 && modelIndex === 1 ? 9 : 0, totalUnits: 39, current: index === 1 && modelIndex === 1 })) }));
}
function summary(taskId, name, businessStatus, phase, completed, terminalStatus) {
  return { taskId, name, description: "契约演示数据，用于前端交互审阅", environmentId: "env-dev", environmentName: "研发测试集群", systems: systems.map(({ id, name, version }) => ({ id, name, version })), harnessNames: harnesses.map((item) => item.name), modelCount: models.length, uniqueQuestionCount: questions.length, evaluationUnitCount: 351, completedUnitCount: completed, businessStatus, phase, terminalStatus, createdAt: "2026-08-25T01:00:00Z", startedAt: businessStatus !== "PENDING" ? "2026-08-25T01:30:00Z" : undefined, finishedAt: businessStatus === "COMPLETED" ? "2026-08-25T08:30:00Z" : undefined };
}
function taskDetail() {
  return { dataMode: "DEMO", ...summary("EVAL-DEMO-RUNNING", "云原生多系统韧性评测", "RUNNING", "EXECUTING", 165), specFingerprint: "sha256:demo-evaluation", harnessProgress: harnessProgress(), systemProgress: systems.map((item, index) => ({ systemId: item.id, systemName: item.name, completedUnits: index === 0 ? 117 : index === 1 ? 48 : 0, totalUnits: 117, status: index === 0 ? "COMPLETED" : index === 1 ? "RUNNING" : "PENDING" })), currentUnit, units: runningUnits, recentEvents: [{ id: "evt-184", sequence: 184, taskId: "EVAL-DEMO-RUNNING", unitId: currentUnit.unitId, type: "DISTURBANCE_APPLIED", phase: "EXECUTING", occurredAt: now(), message: "动态扰动 telemetry_instability 已触发" }], lease: { status: "HELD", heartbeatAt: now(), holderTaskId: "EVAL-DEMO-RUNNING" } };
}

function unitDetail(unitId) {
  const unit = runningUnits.find((item) => item.unitId === unitId) || currentUnit;
  return { dataMode: "DEMO", ...unit, taskId: "EVAL-DEMO-RUNNING", environmentName: "研发测试集群", systemVersion: systems.find((item) => item.id === unit.systemId)?.version || "v1", target: { pod: `${unit.targetService}-5cf7db968-l8r4n`, uid: "7d8a-demo-b42f", node: "worker-01", container: unit.targetService, confirmed: true }, phaseStartedAt: new Date(Date.now() - 129000).toISOString(), unitStartedAt: new Date(Date.now() - 378000).toISOString(), disturbanceBudget: { used: 1, total: 2 }, mainFault: { type: "CPU 资源压力", executor: "ChaosBlade", experimentId: "exp-cpu-demo", target: `${unit.targetService}-5cf7db968-l8r4n`, parameters: { "cpu-percent": 80, duration: "120s" }, status: "EFFECT_OBSERVED", startedAt: now(), evidenceRef: "fault-evidence-demo" }, disturbances: [{ disturbanceId: "dist-demo-1", type: "telemetry_instability", trigger: "主故障效果确认后", parameters: { delay: "8s", duration: "30s" }, status: "RUNNING", startedAt: now(), evidenceRef: "telemetry-event-184" }], liveMetrics: [{ id: "success-rate", label: "成功率", value: "82%", baseline: "99.8%", status: "CRITICAL" }, { id: "p95", label: "P95 延迟", value: "12.4s", baseline: "34ms", status: "CRITICAL" }, { id: "cpu", label: "Pod CPU", value: "94%", baseline: "18%", status: "WARNING" }, { id: "error", label: "错误率", value: "18%", baseline: "0.2%", status: "CRITICAL" }], gates: [{ id: "lease", label: "环境租约", status: "PASS" }, { id: "target", label: "目标身份", status: "PASS" }, { id: "fault", label: "主故障效果", status: "PASS" }, { id: "recovery", label: "恢复验证", status: "PENDING" }], trials: [{ trialId: "trial-1", attempt: 1, status: "COMPLETED", outcome: "INCONCLUSIVE", durationSeconds: 221, cleaned: true }, { trialId: "trial-2", attempt: 2, status: "RUNNING", durationSeconds: 129, cleaned: false }], events: taskDetail().recentEvents, artifactRefs: [{ label: "运行上下文.json", href: "/api/v1/evaluation/demo-artifacts/context.json" }] };
}

const resultUnits = runningUnits.map((item, index) => ({ ...item, status: "COMPLETED", outcome: index % 16 === 0 ? "CASE_INVALID" : index % 4 === 0 ? "FAIL" : "PASS" }));
function completedResult() {
  const scoreMatrix = [];
  for (const system of systems) for (const harness of harnesses) {
    const base = 80 - systems.indexOf(system) * 5 - harnesses.indexOf(harness) * 4;
    scoreMatrix.push({ systemId: system.id, harnessId: harness.id, score: base, validUnits: 37, totalUnits: 39 });
    for (const model of models) scoreMatrix.push({ systemId: system.id, harnessId: harness.id, modelId: model.id, score: base - models.indexOf(model) * 2, validUnits: 12, totalUnits: 13 });
  }
  return { dataMode: "DEMO", ...summary("EVAL-DEMO-COMPLETED", "云原生多系统韧性评测", "COMPLETED", "CLEANING_UP", 351, "COMPLETED"), systems: systems.map((item) => item.name), harnessCount: 3, modelCount: 3, totalUnits: 351, validUnits: 329, pass: 236, fail: 93, caseInvalid: 22, score: 71.7, environmentName: "研发测试集群", durationSeconds: 56538, systemResults: systems.map((item, index) => ({ systemId: item.id, systemName: item.name, version: item.version, languages: item.languages, validUnits: 109 + index, totalUnits: 117, score: 74 - index * 3, bestHarnessName: "Codex" })), harnesses: harnesses.map(({ id, name }) => ({ id, name })), scoreMatrix, modelScores: models.map((item, index) => ({ modelId: item.id, modelName: item.name, score: 76 - index * 4, validUnits: 110 - index })), unitResults: resultUnits, recovery: [{ id: "terminal", label: "任务终态", status: "PASS", value: "COMPLETED" }, { id: "cleanup", label: "环境清理", status: "PASS", value: "通过" }, { id: "lease", label: "环境租约", status: "PASS", value: "已释放" }], oracleSummary: [{ id: "validity", label: "有效性门禁", passed: 329, failed: 0, invalid: 22 }, { id: "recovery", label: "恢复门禁", passed: 351, failed: 0 }], artifacts: [{ label: "评测报告.md", href: "/api/v1/evaluation/demo-artifacts/report.md" }, { label: "结果明细.json", href: "/api/v1/evaluation/demo-artifacts/results.json" }] };
}

const completed = completedResult();
const queued = { ...summary("EVAL-DEMO-QUEUED", "等待中的多系统评测", "PENDING", "QUEUED", 0), queuePosition: 1, waitingForTaskId: "EVAL-DEMO-RUNNING" };
const createdTasks = [];
const compileTokens = new Map();
let eventSequence = 184;

function json(res, status, body, headers = {}) {
  res.writeHead(status, { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store", "X-Resilience-Data-Mode": "DEMO", ...headers });
  res.end(JSON.stringify(body));
}
function problem(res, status, detail) { json(res, status, { title: "Evaluation demo API error", status, detail }); }
async function body(req) { const chunks = []; for await (const chunk of req) chunks.push(chunk); return chunks.length ? JSON.parse(Buffer.concat(chunks).toString("utf8")) : {}; }
function compile(selection) {
  const matrix = [];
  for (const systemId of selection.systemIds || []) for (const harness of selection.harnesses || []) for (const modelId of harness.modelIds || []) {
    const applicableQuestionIds = (selection.questionIds || []).filter((id) => questions.find((item) => item.id === id)?.applicableSystemIds.includes(systemId));
    matrix.push({ systemId, harnessId: harness.harnessId, modelId, applicableQuestionIds, unitCount: applicableQuestionIds.length });
  }
  const token = `compile-${randomUUID()}`;
  const result = { compileToken: token, generatedAt: now(), systemsCount: new Set(matrix.map((item) => item.systemId)).size, harnessesCount: new Set(matrix.map((item) => item.harnessId)).size, modelConfigurationsCount: matrix.length, uniqueQuestionCount: new Set(matrix.flatMap((item) => item.applicableQuestionIds)).size, evaluationUnitCount: matrix.reduce((sum, item) => sum + item.unitCount, 0), maxTrialCount: matrix.reduce((sum, item) => sum + item.unitCount, 0) * (selection.strategy?.maxTrialsPerUnit || 5), sharedMcpServerIds: selection.mcpServerIds || [], matrix, issues: [], valid: matrix.length > 0 && matrix.every((item) => item.unitCount > 0) };
  compileTokens.set(token, result); return result;
}
function reuseValidation() { return { sourceTaskId: completed.taskId, specFingerprint: "sha256:demo-result", systems: systems.map((item) => ({ id: item.id, label: `${item.name} ${item.version}`, available: true })), harnesses: harnesses.map((item) => ({ id: item.id, label: item.name, available: true })), models: models.map((item) => ({ id: item.id, label: item.name, available: true })), mcpServers: mcpServers.map((item) => ({ id: item.id, label: item.name, required: item.id === "k8s_ro" || item.id === "telemetry_ro", available: true })), questionStrategyLabel: "multi-system-core-v1", scoringPolicyLabel: "episode-score-v1", evaluatorLabel: "independent-oracle-v1", evaluationUnitCount: 351, checks: [{ id: "systems", label: "3/3 系统快照可用", passed: true }, { id: "harnesses", label: "3/3 Harness 已连接", passed: true }, { id: "mcp", label: "必选 MCP 已连接", passed: true }], canReuseDirectly: true }; }
function taskList() { const items = [taskDetail(), queued, completed, ...createdTasks]; return { dataMode: "DEMO", items, total: items.length, summary: { pending: items.filter((item) => item.businessStatus === "PENDING").length, running: items.filter((item) => item.businessStatus === "RUNNING").length, completed: items.filter((item) => item.businessStatus === "COMPLETED").length, occupiedEnvironments: 1, environments: environments.length }, occupancies: environments.map((environment) => ({ environment, queue: environment.id === "env-dev" ? [{ taskId: queued.taskId, name: queued.name, position: 1 }] : [] })) }; }

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, `http://${req.headers.host}`);
  const path = url.pathname;
  if (path === "/api/v1/meta/health") return json(res, 200, { service: "ok", version: "evaluation-demo", repo: { path: "frontend-demo", exists: true, factory_config_found: true } });
  if (!path.startsWith("/api/v1/evaluation")) return problem(res, 404, "demo server only implements evaluation API");
  if (path === "/api/v1/evaluation/options" && req.method === "GET") return json(res, 200, options);
  if (path === "/api/v1/evaluation/compile" && req.method === "POST") return json(res, 200, compile(await body(req)));
  if (path === "/api/v1/evaluation/tasks" && req.method === "GET") return json(res, 200, taskList());
  if (path === "/api/v1/evaluation/tasks" && req.method === "POST") { const input = await body(req); if (!compileTokens.has(input.compileToken)) return problem(res, 409, "compileToken invalid or expired"); const task = { ...queued, taskId: `EVAL-DEMO-${Date.now()}`, name: input.name, description: input.description, queuePosition: environments[0].queueSize + createdTasks.length + 1 }; createdTasks.push(task); return json(res, 201, task); }
  if (path === "/api/v1/evaluation/tasks/drafts" && req.method === "POST") { const input = await body(req); return json(res, 201, { ...queued, taskId: `DRAFT-${Date.now()}`, name: input.name, phase: "DRAFT", queuePosition: undefined }); }
  if (path === "/api/v1/evaluation/monitoring" && req.method === "GET") return json(res, 200, { dataMode: "DEMO", environments: environments.map((environment) => ({ environment, activeTask: environment.id === "env-dev" ? taskDetail() : undefined, queueSize: environment.queueSize })) });
  if (path === "/api/v1/evaluation/results" && req.method === "GET") return json(res, 200, { dataMode: "DEMO", items: [completed], total: 1 });
  if (path === `/api/v1/evaluation/results/${completed.taskId}` && req.method === "GET") return json(res, 200, completed);
  const unitMatch = path.match(/^\/api\/v1\/evaluation\/tasks\/([^/]+)\/units\/([^/]+)$/);
  if (unitMatch && req.method === "GET") return json(res, 200, unitDetail(unitMatch[2]));
  const eventMatch = path.match(/^\/api\/v1\/evaluation\/tasks\/([^/]+)\/events$/);
  if (eventMatch && req.method === "GET") {
    const last = Number(req.headers["last-event-id"] || 0); res.writeHead(200, { "Content-Type": "text/event-stream", "Cache-Control": "no-cache", Connection: "keep-alive", "X-Accel-Buffering": "no", "X-Resilience-Data-Mode": "DEMO" });
    const send = () => { eventSequence += 1; if (eventSequence <= last) return; const data = { id: `evt-${eventSequence}`, sequence: eventSequence, taskId: eventMatch[1], unitId: currentUnit.unitId, type: "DEMO_HEARTBEAT", phase: "EXECUTING", occurredAt: now(), message: "演示事件流心跳" }; res.write(`id: ${eventSequence}\nevent: evaluation-event\ndata: ${JSON.stringify(data)}\n\n`); };
    send(); const timer = setInterval(send, 5000); req.on("close", () => clearInterval(timer)); return;
  }
  const reuseValidationMatch = path.match(/^\/api\/v1\/evaluation\/tasks\/([^/]+)\/reuse\/validation$/);
  if (reuseValidationMatch && req.method === "GET") return json(res, 200, reuseValidation());
  const reuseMatch = path.match(/^\/api\/v1\/evaluation\/tasks\/([^/]+)\/reuse$/);
  if (reuseMatch && req.method === "POST") { const input = await body(req); const task = { ...queued, taskId: `EVAL-REUSE-${Date.now()}`, name: input.name, environmentId: input.environmentId }; createdTasks.push(task); return json(res, 201, task); }
  const abortMatch = path.match(/^\/api\/v1\/evaluation\/tasks\/([^/]+)\/abort$/);
  if (abortMatch && req.method === "POST") return json(res, 202, { ...taskDetail(), phase: "RECOVERING" });
  const cancelMatch = path.match(/^\/api\/v1\/evaluation\/tasks\/([^/]+)\/cancel$/);
  if (cancelMatch && req.method === "POST") return json(res, 200, { ...queued, terminalStatus: "ABORTED", businessStatus: "COMPLETED", phase: "CLEANING_UP" });
  const taskMatch = path.match(/^\/api\/v1\/evaluation\/tasks\/([^/]+)$/);
  if (taskMatch && req.method === "GET") { if (taskMatch[1] === completed.taskId) return json(res, 200, completed); const created = createdTasks.find((item) => item.taskId === taskMatch[1]); return json(res, 200, created || taskDetail()); }
  return problem(res, 404, `no demo route for ${req.method} ${path}`);
});

server.listen(PORT, "127.0.0.1", () => console.log(`Evaluation demo API listening on http://127.0.0.1:${PORT}`));
process.on("SIGINT", () => server.close(() => process.exit(0)));
process.on("SIGTERM", () => server.close(() => process.exit(0)));
