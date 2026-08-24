import type {
  GateStatus,
  TaskBusinessStatus,
  TaskPhase,
  TaskTerminalStatus,
  UnitOutcome,
} from "./types";

export function percent(completed: number, total: number): number {
  if (total <= 0) return 0;
  return Math.min(100, Math.max(0, Math.round((completed / total) * 100)));
}

export function formatDuration(seconds?: number): string {
  if (seconds === undefined || Number.isNaN(seconds)) return "—";
  const safe = Math.max(0, Math.floor(seconds));
  const hours = Math.floor(safe / 3600);
  const minutes = Math.floor((safe % 3600) / 60);
  const remainder = safe % 60;
  return [hours, minutes, remainder].map((item) => String(item).padStart(2, "0")).join(":");
}

export function formatTime(value?: string): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(date);
}

export function taskStatusLabel(status: TaskBusinessStatus): string {
  return { PENDING: "未评测", RUNNING: "评测中", COMPLETED: "已完成" }[status];
}

export function phaseLabel(phase?: TaskPhase): string {
  if (!phase) return "—";
  return {
    DRAFT: "草稿",
    QUEUED: "等待环境",
    PREPARING: "准备",
    QUALIFYING: "资格检查",
    BASELINING: "基线",
    EXECUTING: "执行",
    RECOVERING: "恢复",
    EVALUATING: "评价",
    SCORING: "评分",
    CLEANING_UP: "清理",
  }[phase];
}

export function terminalLabel(status?: TaskTerminalStatus): string {
  if (!status) return "—";
  return {
    COMPLETED: "COMPLETED",
    FAILED: "FAILED",
    BLOCKED: "BLOCKED",
    RESET_FAILED: "RESET_FAILED",
    ABORTED: "ABORTED",
    CASE_INVALID: "CASE_INVALID",
    NO_EXECUTABLE_EPISODE: "NO_EXECUTABLE_EPISODE",
  }[status];
}

export function outcomeColor(outcome?: UnitOutcome): string {
  return {
    PASS: "green",
    FAIL: "red",
    CASE_INVALID: "gold",
    INCONCLUSIVE: "orange",
    ABORTED: "volcano",
    SKIPPED: "default",
  }[outcome ?? "SKIPPED"];
}

export function gateColor(status: GateStatus): string {
  return { PASS: "green", FAIL: "red", PENDING: "default", CASE_INVALID: "gold" }[status];
}

export function queryString(values: Record<string, string | number | undefined>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(values)) {
    if (value !== undefined && value !== "") search.set(key, String(value));
  }
  const encoded = search.toString();
  return encoded ? `?${encoded}` : "";
}
