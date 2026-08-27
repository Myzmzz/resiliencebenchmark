import { CheckCircleFilled, ClockCircleFilled, PlayCircleFilled,FieldTimeOutlined } from "@ant-design/icons";
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
          <span style={{color: '#16A34A'}}>已完成 {items.filter((item) => item.status === "COMPLETED").length}</span> · <span style={{color: '#1D4ED8'}}>运行中 {items.filter((item) => item.status === "RUNNING").length}</span> · <span style={{color: 'gray'}}>待执行 {items.filter((item) => item.status === "PENDING").length}</span>
        </span>
      </div>
      <div className="harness-track">
        {items.map((item, index) => {
          const active = selectedId === item.harnessId || (!selectedId && item.status === "RUNNING");
          const Icon = item.status === "COMPLETED" ? CheckCircleFilled : item.status === "RUNNING" ? PlayCircleFilled : ClockCircleFilled;
          const currentModel = item.modelProgress.filter((mItem) => mItem.current==true)
          return (
            <div className="harness-track-wrap" key={item.harnessId}>
              <button
                type="button"
                className={`harness-track-card harness-track-${item.status.toLowerCase()} ${active ? "is-selected" : ""}`}
                onClick={() => onSelect?.(item.harnessId)}
              >
                <div className="harness-track-title">
                  <div><Icon style={{fontSize: 20, marginRight: 8, color: item.status === "COMPLETED" ? "#16A34A" : item.status === "RUNNING" ? "#1D4ED8" : "gray"}} /> {item.harnessName}</div>
                  <span style={{color: item.status === "COMPLETED" ? "#16A34A" : item.status === "RUNNING" ? "#1D4ED8" : "gray"}}>{item.status === "COMPLETED" ? "已完成" : item.status === "RUNNING" ? "正在运行" : "等待执行"}</span>
                </div>
                <strong>{item.completedUnits} / {item.totalUnits}</strong>
                {
                  item.status === "RUNNING" && <>
                    <div className="harness-track-info">当前 {currentModel[0].modelName} · 单月 {currentModel[0].completedUnits}</div>
                    <Progress percent={percent(item.completedUnits, item.totalUnits)} showInfo={false} strokeColor="#1D4ED8" />
                  </>
                }
                
                {
                  item.status === "COMPLETED" && <>
                  <div className="harness-track-info">{item.modelProgress.length} 个模型完成</div>
                  <div className="harness-track-info"><FieldTimeOutlined /> {formatDuration(item.durationSeconds)}</div>
                  </>
                }
                {
                  item.status === "PENDING" && <>
                    <div className="harness-track-info">预计在 {items.filter((citem) => citem.status === "RUNNING")[0].harnessName} 完成后启动</div>
                  </>
                }
              </button>
              {index < items.length - 1 && <span className="harness-track-arrow">→</span>}
            </div>
          );
        })}
      </div>
    </section>
  );
}
