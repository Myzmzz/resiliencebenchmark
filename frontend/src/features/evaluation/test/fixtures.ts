import type {
  EvaluationOptions,
  EvaluationResultDetail,
  EvaluationResultListResponse,
  EvaluationTaskDetail,
  EvaluationTaskListResponse,
  EvaluationUnitDetail,
  MonitoringOverviewResponse,
  ReuseValidation,
} from "../types";

export const environment = {
  id: "env-dev",
  name: "研发测试集群",
  status: "BUSY" as const,
  currentTask: { taskId: "EVAL-001", name: "多系统韧性评测", progressPercent: 47, phase: "EXECUTING" as const },
  queueSize: 1,
  lastCheckedAt: "2026-08-25T10:00:00Z",
};

export const evaluationOptions: EvaluationOptions = {
  environments: [environment, { ...environment, id: "env-idle", name: "空闲环境", status: "IDLE", currentTask: undefined, queueSize: 0 }],
  systems: [
    { id: "train-ticket", name: "Train Ticket", version: "v0.3", namespace: "train-ticket", status: "READY", serviceCount: 51, sourceCommit: "5f7c21d", imageLocked: 51, imageTotal: 51, codeGraphStatus: "AVAILABLE", languages: ["Java"] },
    { id: "otel-demo", name: "OTel Demo", version: "v1.12.0", namespace: "otel-demo", status: "READY", serviceCount: 22, sourceCommit: "8a4f9c2", imageLocked: 22, imageTotal: 22, codeGraphStatus: "AVAILABLE", languages: ["Go", "Java", "Python", "TypeScript", "C#"] },
  ],
  harnesses: [
    { id: "codex", name: "Codex", status: "AVAILABLE", description: "原生 Harness", modelIds: ["gpt-5.6", "gpt-5.5"], requiredMcpIds: ["k8s_ro", "telemetry_ro"] },
    { id: "bladeai", name: "BladeAI", status: "AVAILABLE", modelIds: ["gpt-5.6"], requiredMcpIds: ["k8s_ro", "telemetry_ro"] },
  ],
  models: [
    { id: "gpt-5.6", name: "GPT-5.6", status: "AVAILABLE" },
    { id: "gpt-5.5", name: "GPT-5.5", status: "AVAILABLE" },
  ],
  mcpServers: [
    { id: "k8s_ro", name: "k8s_ro", status: "CONNECTED" },
    { id: "telemetry_ro", name: "telemetry_ro", status: "CONNECTED" },
    { id: "source_ro", name: "source_ro", status: "CONNECTED" },
  ],
  questions: [
    { id: "EPI-RES-003", title: "CPU 资源压力", category: "资源", applicableSystemIds: ["train-ticket", "otel-demo"], targetService: "inventory", maxTrials: 5, status: "AVAILABLE" },
    { id: "EPI-NET-001", title: "服务网络延迟", category: "网络", applicableSystemIds: ["train-ticket", "otel-demo"], targetService: "gateway", maxTrials: 5, status: "AVAILABLE" },
  ],
  questionSets: [{ id: "core", name: "韧性核心题库", version: "v1", questionIds: ["EPI-RES-003", "EPI-NET-001"] }],
  scoringPolicies: [{ id: "episode-score-v1", name: "episode-score-v1" }],
  evaluators: [{ id: "independent-oracle-v1", name: "independent-oracle-v1" }],
  promptStrategies: [{ id: "full-lifecycle-v1", name: "full-lifecycle-v1" }],
};

const summary = {
  taskId: "EVAL-001",
  name: "多系统韧性评测",
  environmentId: environment.id,
  environmentName: environment.name,
  systems: evaluationOptions.systems.map(({ id, name, version }) => ({ id, name, version })),
  harnessNames: ["Codex", "BladeAI"],
  modelCount: 2,
  uniqueQuestionCount: 2,
  evaluationUnitCount: 12,
  completedUnitCount: 5,
  businessStatus: "RUNNING" as const,
  phase: "EXECUTING" as const,
  createdAt: "2026-08-25T08:00:00Z",
  startedAt: "2026-08-25T08:30:00Z",
};

export const taskList: EvaluationTaskListResponse = {
  items: [summary, { ...summary, taskId: "EVAL-002", name: "等待任务", businessStatus: "PENDING", phase: "QUEUED", completedUnitCount: 0, queuePosition: 1, waitingForTaskId: "EVAL-001", startedAt: undefined }],
  total: 2,
  summary: { pending: 1, running: 1, completed: 0, occupiedEnvironments: 1, environments: 2 },
  occupancies: [{ environment, queue: [{ taskId: "EVAL-002", name: "等待任务", position: 1 }] }],
};

const units = [
  { unitId: "UNIT-001", systemId: "train-ticket", systemName: "Train Ticket", harnessId: "codex", harnessName: "Codex", modelId: "gpt-5.6", modelName: "GPT-5.6", questionId: "EPI-NET-001", questionTitle: "服务网络延迟", questionIndex: 1, status: "COMPLETED" as const, outcome: "PASS" as const, maxTrials: 5 },
  { unitId: "UNIT-002", systemId: "train-ticket", systemName: "Train Ticket", harnessId: "bladeai", harnessName: "BladeAI", modelId: "gpt-5.6", modelName: "GPT-5.6", questionId: "EPI-RES-003", questionTitle: "CPU 资源压力", questionIndex: 2, status: "RUNNING" as const, phase: "EXECUTING" as const, currentTrial: 2, maxTrials: 5, targetService: "inventory" },
];

export const taskDetail: EvaluationTaskDetail = {
  ...summary,
  description: "真实任务",
  specFingerprint: "sha256:abc",
  harnessProgress: [
    { harnessId: "codex", harnessName: "Codex", status: "COMPLETED", completedUnits: 6, totalUnits: 6, durationSeconds: 1200, modelProgress: [{ modelId: "gpt-5.6", modelName: "GPT-5.6", completedUnits: 6, totalUnits: 6, current: false }] },
    { harnessId: "bladeai", harnessName: "BladeAI", status: "RUNNING", completedUnits: 1, totalUnits: 6, modelProgress: [{ modelId: "gpt-5.6", modelName: "GPT-5.6", completedUnits: 1, totalUnits: 6, current: true }] },
  ],
  systemProgress: evaluationOptions.systems.map((system) => ({ systemId: system.id, systemName: system.name, completedUnits: 1, totalUnits: 6, status: "RUNNING" })),
  currentUnit: units[1],
  units,
  recentEvents: [{ id: "evt-1", sequence: 1, taskId: "EVAL-001", unitId: "UNIT-002", type: "FAULT_APPLIED", phase: "EXECUTING", occurredAt: "2026-08-25T09:00:00Z", message: "主故障已应用" }],
  lease: { status: "HELD", heartbeatAt: "2026-08-25T10:00:00Z", holderTaskId: "EVAL-001" },
};

export const monitoringOverview: MonitoringOverviewResponse = { environments: [{ environment, activeTask: summary, queueSize: 1 }] };

export const unitDetail: EvaluationUnitDetail = {
  ...units[1],
  taskId: "EVAL-001",
  environmentName: environment.name,
  systemVersion: "v0.3",
  target: { pod: "inventory-abc", uid: "uid-123", node: "worker-01", container: "inventory", confirmed: true },
  phaseStartedAt: "2026-08-25T09:55:00Z",
  unitStartedAt: "2026-08-25T09:45:00Z",
  disturbanceBudget: { used: 1, total: 2 },
  mainFault: { type: "CPU 资源压力", executor: "ChaosBlade", experimentId: "exp-1", target: "inventory-abc", parameters: { percent: 80 }, status: "EFFECT_OBSERVED", startedAt: "2026-08-25T09:55:00Z" },
  disturbances: [{ disturbanceId: "d-1", type: "telemetry_instability", trigger: "主故障效果确认后", parameters: { delay: "8s" }, status: "RUNNING", evidenceRef: "telemetry-event-1" }],
  liveMetrics: [{ id: "success", label: "成功率", value: "82%", baseline: "99.8%", status: "CRITICAL" }],
  gates: [{ id: "fault", label: "主故障效果", status: "PASS" }, { id: "recovery", label: "恢复验证", status: "PENDING" }],
  trials: [{ trialId: "t-1", attempt: 1, status: "COMPLETED", outcome: "INCONCLUSIVE", durationSeconds: 220, cleaned: true }, { trialId: "t-2", attempt: 2, status: "RUNNING", durationSeconds: 120, cleaned: false }],
  events: taskDetail.recentEvents,
  artifactRefs: [{ label: "运行上下文", href: "/api/v1/evaluation/artifacts/context.json" }],
};

export const resultDetail: EvaluationResultDetail = {
  taskId: "EVAL-RESULT-1",
  name: "云原生多系统韧性评测",
  finishedAt: "2026-08-25T11:00:00Z",
  systems: ["Train Ticket", "OTel Demo"],
  harnessCount: 2,
  modelCount: 2,
  totalUnits: 12,
  validUnits: 11,
  pass: 8,
  fail: 3,
  caseInvalid: 1,
  score: 72.7,
  terminalStatus: "COMPLETED",
  environmentName: environment.name,
  durationSeconds: 5400,
  systemResults: evaluationOptions.systems.map((system, index) => ({ systemId: system.id, systemName: system.name, version: system.version, languages: system.languages, validUnits: index ? 5 : 6, totalUnits: 6, score: index ? 70 : 75, bestHarnessName: "Codex" })),
  harnesses: [{ id: "codex", name: "Codex" }, { id: "bladeai", name: "BladeAI" }],
  scoreMatrix: [
    { systemId: "train-ticket", harnessId: "codex", score: 76, validUnits: 3, totalUnits: 3 },
    { systemId: "train-ticket", harnessId: "bladeai", score: 72, validUnits: 3, totalUnits: 3 },
    { systemId: "otel-demo", harnessId: "codex", score: 74, validUnits: 3, totalUnits: 3 },
    { systemId: "otel-demo", harnessId: "bladeai", score: 68, validUnits: 2, totalUnits: 3 },
    { systemId: "train-ticket", harnessId: "codex", modelId: "gpt-5.6", score: 78, validUnits: 2, totalUnits: 2 },
  ],
  modelScores: [{ modelId: "gpt-5.6", modelName: "GPT-5.6", score: 76, validUnits: 6 }, { modelId: "gpt-5.5", modelName: "GPT-5.5", score: 69, validUnits: 5 }],
  unitResults: units,
  recovery: [{ id: "cleanup", label: "环境清理", status: "PASS", value: "通过" }, { id: "lease", label: "环境租约", status: "PASS", value: "已释放" }],
  oracleSummary: [{ id: "validity", label: "有效性门禁", passed: 11, failed: 0, invalid: 1 }],
  artifacts: [{ label: "评测报告.md", href: "/api/v1/evaluation/artifacts/report.md" }],
};

export const resultList: EvaluationResultListResponse = { items: [{ taskId: resultDetail.taskId, name: resultDetail.name, finishedAt: resultDetail.finishedAt, systems: resultDetail.systems, harnessCount: 2, modelCount: 2, totalUnits: 12, validUnits: 11, pass: 8, fail: 3, caseInvalid: 1, score: 72.7, terminalStatus: "COMPLETED" }], total: 1 };

export const reuseValidation: ReuseValidation = {
  sourceTaskId: resultDetail.taskId,
  specFingerprint: "sha256:reuse",
  systems: evaluationOptions.systems.map((item) => ({ id: item.id, label: `${item.name} ${item.version}`, available: true })),
  harnesses: evaluationOptions.harnesses.map((item) => ({ id: item.id, label: item.name, available: true })),
  models: evaluationOptions.models.map((item) => ({ id: item.id, label: item.name, available: true })),
  mcpServers: evaluationOptions.mcpServers.map((item) => ({ id: item.id, label: item.name, required: item.id !== "source_ro", available: true })),
  questionStrategyLabel: "core-v1",
  scoringPolicyLabel: "episode-score-v1",
  evaluatorLabel: "independent-oracle-v1",
  evaluationUnitCount: 12,
  checks: [{ id: "systems", label: "系统快照可用", passed: true }, { id: "mcp", label: "必选 MCP 已连接", passed: true }],
  canReuseDirectly: true,
};
