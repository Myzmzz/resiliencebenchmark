import { Tag } from "antd";
import type { GateStatus, TaskBusinessStatus, UnitOutcome } from "../types";
import { gateColor, outcomeColor, taskStatusLabel } from "../formatters";

export function TaskStatusTag({ status }: { status: TaskBusinessStatus }) {
  const color = status === "RUNNING" ? "blue" : status === "COMPLETED" ? "green" : "default";
  return <Tag color={color}>{taskStatusLabel(status)}</Tag>;
}

export function OutcomeTag({ outcome }: { outcome?: UnitOutcome }) {
  return <Tag color={outcomeColor(outcome)}>{outcome ?? "待执行"}</Tag>;
}

export function GateTag({ status }: { status: GateStatus }) {
  return <Tag color={gateColor(status)}>{status}</Tag>;
}
