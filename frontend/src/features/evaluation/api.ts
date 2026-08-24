import { queryString } from "./formatters";
import type {
  CompileEvaluationRequest,
  CompiledEvaluation,
  CreateEvaluationTaskRequest,
  EvaluationOptions,
  EvaluationResultDetail,
  EvaluationResultListResponse,
  EvaluationTaskDetail,
  EvaluationTaskListResponse,
  EvaluationUnitDetail,
  MonitoringOverviewResponse,
  ProblemDetails,
  ReuseTaskRequest,
  ReuseValidation,
  SaveDraftRequest,
} from "./types";

const ROOT = "/api/v1/evaluation";

export class EvaluationApiError extends Error {
  readonly status: number;
  readonly problem?: ProblemDetails;

  constructor(message: string, status: number, problem?: ProblemDetails) {
    super(message);
    this.name = "EvaluationApiError";
    this.status = status;
    this.problem = problem;
  }
}

async function parseError(response: Response): Promise<EvaluationApiError> {
  let problem: ProblemDetails | undefined;
  try {
    problem = (await response.json()) as ProblemDetails;
  } catch {
    problem = undefined;
  }
  return new EvaluationApiError(
    problem?.detail || problem?.title || `评测 API 请求失败：${response.status}`,
    response.status,
    problem,
  );
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${ROOT}${path}`, {
    ...init,
    headers: {
      Accept: "application/json",
      ...(init?.body ? { "Content-Type": "application/json" } : {}),
      ...init?.headers,
    },
  });
  if (!response.ok) throw await parseError(response);
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export function getEvaluationOptions(signal?: AbortSignal): Promise<EvaluationOptions> {
  return request("/options", { signal });
}

export function listEvaluationTasks(
  filters: { status?: string; environmentId?: string; q?: string; page?: number; pageSize?: number },
  signal?: AbortSignal,
): Promise<EvaluationTaskListResponse> {
  return request(
    `/tasks${queryString({
      status: filters.status,
      environment_id: filters.environmentId,
      q: filters.q,
      page: filters.page,
      page_size: filters.pageSize,
    })}`,
    { signal },
  );
}

export function getEvaluationTask(taskId: string, signal?: AbortSignal): Promise<EvaluationTaskDetail> {
  return request(`/tasks/${encodeURIComponent(taskId)}`, { signal });
}

export function compileEvaluation(
  body: CompileEvaluationRequest,
  signal?: AbortSignal,
): Promise<CompiledEvaluation> {
  return request("/compile", { method: "POST", body: JSON.stringify(body), signal });
}

export function createEvaluationTask(body: CreateEvaluationTaskRequest): Promise<EvaluationTaskDetail> {
  return request("/tasks", { method: "POST", body: JSON.stringify(body) });
}

export function saveEvaluationDraft(taskId: string | undefined, body: SaveDraftRequest): Promise<EvaluationTaskDetail> {
  return request(taskId ? `/tasks/${encodeURIComponent(taskId)}/draft` : "/tasks/drafts", {
    method: taskId ? "PUT" : "POST",
    body: JSON.stringify(body),
  });
}

export function cancelQueuedTask(taskId: string): Promise<EvaluationTaskDetail> {
  return request(`/tasks/${encodeURIComponent(taskId)}/cancel`, { method: "POST" });
}

export function abortEvaluationTask(taskId: string, reason: string): Promise<EvaluationTaskDetail> {
  return request(`/tasks/${encodeURIComponent(taskId)}/abort`, {
    method: "POST",
    body: JSON.stringify({ reason }),
  });
}

export function getMonitoringOverview(signal?: AbortSignal): Promise<MonitoringOverviewResponse> {
  return request("/monitoring", { signal });
}

export function getEvaluationUnit(
  taskId: string,
  unitId: string,
  signal?: AbortSignal,
): Promise<EvaluationUnitDetail> {
  return request(`/tasks/${encodeURIComponent(taskId)}/units/${encodeURIComponent(unitId)}`, { signal });
}

export function listEvaluationResults(
  filters: { q?: string; terminalStatus?: string; page?: number; pageSize?: number },
  signal?: AbortSignal,
): Promise<EvaluationResultListResponse> {
  return request(
    `/results${queryString({
      q: filters.q,
      terminal_status: filters.terminalStatus,
      page: filters.page,
      page_size: filters.pageSize,
    })}`,
    { signal },
  );
}

export function getEvaluationResult(taskId: string, signal?: AbortSignal): Promise<EvaluationResultDetail> {
  return request(`/results/${encodeURIComponent(taskId)}`, { signal });
}

export function validateReuse(taskId: string, signal?: AbortSignal): Promise<ReuseValidation> {
  return request(`/tasks/${encodeURIComponent(taskId)}/reuse/validation`, { signal });
}

export function reuseEvaluationTask(
  taskId: string,
  body: ReuseTaskRequest,
): Promise<EvaluationTaskDetail> {
  return request(`/tasks/${encodeURIComponent(taskId)}/reuse`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function evaluationEventUrl(taskId: string): string {
  return `${ROOT}/tasks/${encodeURIComponent(taskId)}/events`;
}
