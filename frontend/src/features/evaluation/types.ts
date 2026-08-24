export type EnvironmentRuntimeStatus = "IDLE" | "BUSY" | "RECOVERING" | "RESET_FAILED";
export type TaskBusinessStatus = "PENDING" | "RUNNING" | "COMPLETED";
export type TaskPhase =
  | "DRAFT"
  | "QUEUED"
  | "PREPARING"
  | "QUALIFYING"
  | "BASELINING"
  | "EXECUTING"
  | "RECOVERING"
  | "EVALUATING"
  | "SCORING"
  | "CLEANING_UP";
export type TaskTerminalStatus =
  | "COMPLETED"
  | "FAILED"
  | "BLOCKED"
  | "RESET_FAILED"
  | "ABORTED"
  | "CASE_INVALID"
  | "NO_EXECUTABLE_EPISODE";
export type UnitStatus = "PENDING" | "RUNNING" | "COMPLETED" | "SKIPPED";
export type UnitOutcome =
  | "PASS"
  | "FAIL"
  | "CASE_INVALID"
  | "INCONCLUSIVE"
  | "ABORTED"
  | "SKIPPED";
export type HarnessRunStatus = "PENDING" | "RUNNING" | "COMPLETED";
export type GateStatus = "PASS" | "FAIL" | "PENDING" | "CASE_INVALID";
export type ConnectionState = "CONNECTING" | "OPEN" | "RECONNECTING" | "CLOSED" | "ERROR";

export interface CurrentTaskBrief {
  taskId: string;
  name: string;
  progressPercent: number;
  currentUnitLabel?: string;
  phase?: TaskPhase;
}

export interface EnvironmentOption {
  id: string;
  name: string;
  status: EnvironmentRuntimeStatus;
  currentTask?: CurrentTaskBrief;
  queueSize: number;
  lastCheckedAt: string;
}

export interface SystemOption {
  id: string;
  name: string;
  version: string;
  namespace: string;
  status: "READY" | "INACTIVE" | "UNAVAILABLE";
  serviceCount: number;
  sourceCommit: string;
  imageLocked: number;
  imageTotal: number;
  codeGraphStatus: "AVAILABLE" | "STALE" | "MISSING";
  languages: string[];
}

export interface ModelOption {
  id: string;
  name: string;
  status: "AVAILABLE" | "UNAVAILABLE";
}

export interface HarnessOption {
  id: string;
  name: string;
  status: "AVAILABLE" | "UNAVAILABLE";
  description?: string;
  modelIds: string[];
  requiredMcpIds: string[];
}

export interface McpOption {
  id: string;
  name: string;
  status: "CONNECTED" | "DISCONNECTED";
  description?: string;
}

export interface QuestionOption {
  id: string;
  title: string;
  category: string;
  applicableSystemIds: string[];
  targetService?: string;
  maxTrials: number;
  status: "AVAILABLE" | "INCOMPATIBLE";
}

export interface QuestionSetOption {
  id: string;
  name: string;
  version: string;
  questionIds: string[];
}

export interface NamedPolicy {
  id: string;
  name: string;
}

export interface EvaluationOptions {
  dataMode?: "LIVE" | "DEMO";
  environments: EnvironmentOption[];
  systems: SystemOption[];
  harnesses: HarnessOption[];
  models: ModelOption[];
  mcpServers: McpOption[];
  questions: QuestionOption[];
  questionSets: QuestionSetOption[];
  scoringPolicies: NamedPolicy[];
  evaluators: NamedPolicy[];
  promptStrategies: NamedPolicy[];
}

export interface HarnessSelection {
  harnessId: string;
  modelIds: string[];
}

export interface ExecutionStrategy {
  questionOrder: "FIXED" | "RANDOM_SEEDED";
  maxTrialsPerUnit: number;
  retryPerPhase: number;
  stopOnSafetyViolation: boolean;
  scoringPolicyId: string;
  evaluatorId: string;
  promptStrategyId: string;
}

export interface EvaluationSelection {
  environmentId: string;
  systemIds: string[];
  harnesses: HarnessSelection[];
  mcpServerIds: string[];
  questionSetId: string;
  questionIds: string[];
  strategy: ExecutionStrategy;
}

export interface CompileEvaluationRequest extends EvaluationSelection {}

export interface CompilationIssue {
  code: string;
  message: string;
  severity: "ERROR" | "WARNING";
  systemId?: string;
  harnessId?: string;
  modelId?: string;
  questionId?: string;
}

export interface CompilationMatrixRow {
  systemId: string;
  harnessId: string;
  modelId: string;
  applicableQuestionIds: string[];
  unitCount: number;
}

export interface CompiledEvaluation {
  compileToken: string;
  generatedAt: string;
  systemsCount: number;
  harnessesCount: number;
  modelConfigurationsCount: number;
  uniqueQuestionCount: number;
  evaluationUnitCount: number;
  maxTrialCount: number;
  sharedMcpServerIds: string[];
  matrix: CompilationMatrixRow[];
  issues: CompilationIssue[];
  valid: boolean;
}

export interface CreateEvaluationTaskRequest {
  name: string;
  description?: string;
  compileToken: string;
  selection: EvaluationSelection;
  enqueueIfBusy: boolean;
}

export interface SaveDraftRequest {
  name: string;
  description?: string;
  selection: Partial<EvaluationSelection>;
}

export interface EnvironmentOccupancy {
  environment: EnvironmentOption;
  queue: Array<{ taskId: string; name: string; position: number }>;
}

export interface EvaluationTaskSummary {
  taskId: string;
  name: string;
  description?: string;
  environmentId: string;
  environmentName: string;
  systems: Array<Pick<SystemOption, "id" | "name" | "version">>;
  harnessNames: string[];
  modelCount: number;
  uniqueQuestionCount: number;
  evaluationUnitCount: number;
  completedUnitCount: number;
  businessStatus: TaskBusinessStatus;
  phase: TaskPhase;
  terminalStatus?: TaskTerminalStatus;
  queuePosition?: number;
  waitingForTaskId?: string;
  createdAt: string;
  startedAt?: string;
  finishedAt?: string;
}

export interface EvaluationTaskListResponse {
  dataMode?: "LIVE" | "DEMO";
  items: EvaluationTaskSummary[];
  total: number;
  summary: { pending: number; running: number; completed: number; occupiedEnvironments: number; environments: number };
  occupancies: EnvironmentOccupancy[];
}

export interface ModelProgress {
  modelId: string;
  modelName: string;
  completedUnits: number;
  totalUnits: number;
  current: boolean;
}

export interface HarnessProgress {
  harnessId: string;
  harnessName: string;
  status: HarnessRunStatus;
  completedUnits: number;
  totalUnits: number;
  durationSeconds?: number;
  modelProgress: ModelProgress[];
}

export interface SystemProgress {
  systemId: string;
  systemName: string;
  completedUnits: number;
  totalUnits: number;
  status: HarnessRunStatus;
}

export interface EvaluationEvent {
  id: string;
  sequence: number;
  taskId: string;
  unitId?: string;
  trialId?: string;
  type: string;
  phase: TaskPhase;
  occurredAt: string;
  message: string;
  payload?: Record<string, unknown>;
}

export interface EvaluationUnitSummary {
  unitId: string;
  systemId: string;
  systemName: string;
  harnessId: string;
  harnessName: string;
  modelId: string;
  modelName: string;
  questionId: string;
  questionTitle: string;
  questionIndex: number;
  status: UnitStatus;
  outcome?: UnitOutcome;
  phase?: TaskPhase;
  currentTrial?: number;
  maxTrials: number;
  targetService?: string;
}

export interface EvaluationTaskDetail extends EvaluationTaskSummary {
  dataMode?: "LIVE" | "DEMO";
  description?: string;
  specFingerprint: string;
  selection?: EvaluationSelection;
  compiled?: CompiledEvaluation;
  harnessProgress: HarnessProgress[];
  systemProgress: SystemProgress[];
  currentUnit?: EvaluationUnitSummary;
  units: EvaluationUnitSummary[];
  recentEvents: EvaluationEvent[];
  lease?: { status: "HELD" | "WAITING" | "RELEASED" | "STALE"; heartbeatAt?: string; holderTaskId?: string };
}

export interface MonitoringEnvironment {
  environment: EnvironmentOption;
  activeTask?: EvaluationTaskSummary;
  queueSize: number;
}

export interface MonitoringOverviewResponse {
  dataMode?: "LIVE" | "DEMO";
  environments: MonitoringEnvironment[];
}

export interface LiveMetric {
  id: string;
  label: string;
  value: string;
  baseline?: string;
  status: "NORMAL" | "WARNING" | "CRITICAL";
}

export interface MainFaultRecord {
  type: string;
  executor: string;
  experimentId: string;
  target: string;
  parameters: Record<string, string | number | boolean>;
  status: "PENDING" | "APPLIED" | "EFFECT_OBSERVED" | "DESTROYED";
  startedAt?: string;
  evidenceRef?: string;
}

export interface DisturbanceRecord {
  disturbanceId: string;
  type: string;
  trigger: string;
  parameters: Record<string, string | number | boolean>;
  status: "PENDING" | "RUNNING" | "COMPLETED" | "ROLLED_BACK" | "FAILED";
  startedAt?: string;
  evidenceRef?: string;
}

export interface OracleGate {
  id: string;
  label: string;
  status: GateStatus;
  evidenceRef?: string;
}

export interface TrialSummary {
  trialId: string;
  attempt: number;
  status: "PENDING" | "RUNNING" | "COMPLETED";
  outcome?: UnitOutcome;
  durationSeconds?: number;
  cleaned: boolean;
}

export interface EvaluationUnitDetail extends EvaluationUnitSummary {
  dataMode?: "LIVE" | "DEMO";
  taskId: string;
  environmentName: string;
  systemVersion: string;
  target?: { pod: string; uid: string; node?: string; container?: string; confirmed: boolean };
  phaseStartedAt?: string;
  unitStartedAt?: string;
  disturbanceBudget: { used: number; total: number };
  mainFault?: MainFaultRecord;
  disturbances: DisturbanceRecord[];
  liveMetrics: LiveMetric[];
  gates: OracleGate[];
  trials: TrialSummary[];
  events: EvaluationEvent[];
  artifactRefs: Array<{ label: string; href: string }>;
}

export interface ResultScoreCell {
  systemId: string;
  harnessId: string;
  modelId?: string;
  score: number;
  validUnits: number;
  totalUnits: number;
}

export interface ModelScore {
  modelId: string;
  modelName: string;
  score: number;
  validUnits: number;
}

export interface SystemResultSummary {
  systemId: string;
  systemName: string;
  version: string;
  languages: string[];
  validUnits: number;
  totalUnits: number;
  score: number;
  bestHarnessName?: string;
}

export interface EvaluationResultSummary {
  taskId: string;
  name: string;
  finishedAt: string;
  systems: string[];
  harnessCount: number;
  modelCount: number;
  totalUnits: number;
  validUnits: number;
  pass: number;
  fail: number;
  caseInvalid: number;
  score: number;
  terminalStatus: TaskTerminalStatus;
}

export interface EvaluationResultListResponse {
  dataMode?: "LIVE" | "DEMO";
  items: EvaluationResultSummary[];
  total: number;
}

export interface EvaluationResultDetail extends EvaluationResultSummary {
  dataMode?: "LIVE" | "DEMO";
  environmentName: string;
  durationSeconds: number;
  systemResults: SystemResultSummary[];
  harnesses: Array<{ id: string; name: string }>;
  scoreMatrix: ResultScoreCell[];
  modelScores: ModelScore[];
  unitResults: EvaluationUnitSummary[];
  recovery: Array<{ id: string; label: string; status: GateStatus; value?: string }>;
  oracleSummary: Array<{ id: string; label: string; passed: number; failed: number; invalid?: number }>;
  artifacts: Array<{ label: string; href: string }>;
}

export interface ReuseValidation {
  sourceTaskId: string;
  specFingerprint: string;
  systems: Array<{ id: string; label: string; available: boolean }>;
  harnesses: Array<{ id: string; label: string; available: boolean }>;
  models: Array<{ id: string; label: string; available: boolean }>;
  mcpServers: Array<{ id: string; label: string; required: boolean; available: boolean }>;
  questionStrategyLabel: string;
  scoringPolicyLabel: string;
  evaluatorLabel: string;
  evaluationUnitCount: number;
  checks: Array<{ id: string; label: string; passed: boolean; message?: string }>;
  canReuseDirectly: boolean;
}

export interface ReuseTaskRequest {
  name: string;
  environmentId: string;
  enqueueIfBusy: boolean;
}

export interface ProblemDetails {
  type?: string;
  title: string;
  status: number;
  detail?: string;
  instance?: string;
  errors?: Record<string, string[]>;
}
