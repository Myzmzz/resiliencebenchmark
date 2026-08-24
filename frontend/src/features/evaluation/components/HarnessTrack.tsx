import { CheckCircleFilled, ClockCircleFilled, PlayCircleFilled } from "@ant-design/icons";
import { Progress } from "antd";
import { formatDuration, percent } from "../formatters";
import type { HarnessProgress } from "../types";

export default function HarnessTrack({ items, selectedId, onSelect }: {
  items: HarnessProgress[];
  selectedId?: string;
  onSelect?: (id: string) => void;
}) {
  return (
    <section className="evaluation-panel">
      <div className="evaluation-panel-header">
        <h3>Harness 执行轨道</h3>
        <span className="evaluation-muted">
          已完成 {items.filter((item) => item.status === "COMPLETED").length} · 运行中 {items.filter((item) => item.status === "RUNNING").length} · 待执行 {items.filter((item) => item.status === "PENDING").length}
        </span>
      </div>
      <div className="harness-track">
        {items.map((item, index) => {
          const active = selectedId === item.harnessId || (!selectedId && item.status === "RUNNING");
          const Icon = item.status === "COMPLETED" ? CheckCircleFilled : item.status === "RUNNING" ? PlayCircleFilled : ClockCircleFilled;
          return (
            <div className="harness-track-wrap" key={item.harnessId}>
              <button
                type="button"
                className={`harness-track-card harness-track-${item.status.toLowerCase()} ${active ? "is-selected" : ""}`}
                onClick={() => onSelect?.(item.harnessId)}
              >
                <div className="harness-track-title"><Icon /> {item.harnessName}<span>{item.status === "COMPLETED" ? "已完成" : item.status === "RUNNING" ? "正在运行" : "等待执行"}</span></div>
                <strong>{item.completedUnits} / {item.totalUnits}</strong>
                <Progress percent={percent(item.completedUnits, item.totalUnits)} showInfo={false} strokeColor={item.status === "COMPLETED" ? "#16A34A" : "#1D4ED8"} />
                <small>{item.modelProgress.length} 个模型{item.durationSeconds ? ` · ${formatDuration(item.durationSeconds)}` : ""}</small>
              </button>
              {index < items.length - 1 && <span className="harness-track-arrow">→</span>}
            </div>
          );
        })}
      </div>
    </section>
  );
}
