import { Descriptions, Empty, Space, Tag } from "antd";
import type { DisturbanceRecord, MainFaultRecord } from "../types";

function parameters(value: Record<string, string | number | boolean>): string {
  return Object.entries(value).map(([key, item]) => `${key}=${String(item)}`).join(" · ") || "—";
}

export default function FaultDisturbancePanel({ fault, disturbances, budget }: {
  fault?: MainFaultRecord;
  disturbances: DisturbanceRecord[];
  budget: { used: number; total: number };
}) {
  return (
    <div className="fault-disturbance-stack">
      <section className="fault-card">
        <div className="evaluation-panel-header"><h3>主故障</h3>{fault && <Tag color="green">{fault.status}</Tag>}</div>
        {!fault ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="主故障尚未应用" /> : (
          <Descriptions size="small" column={3}>
            <Descriptions.Item label="类型">{fault.type}</Descriptions.Item>
            <Descriptions.Item label="执行器">{fault.executor}</Descriptions.Item>
            <Descriptions.Item label="实验 ID">{fault.experimentId}</Descriptions.Item>
            <Descriptions.Item label="目标">{fault.target}</Descriptions.Item>
            <Descriptions.Item label="参数" span={2}>{parameters(fault.parameters)}</Descriptions.Item>
          </Descriptions>
        )}
      </section>
      <section className="disturbance-card">
        <div className="evaluation-panel-header"><h3>动态扰动</h3><span>扰动预算 {budget.used}/{budget.total}</span></div>
        {disturbances.length === 0 ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="尚未触发动态扰动" /> : disturbances.map((item) => (
          <div className="disturbance-row" key={item.disturbanceId}>
            <div><Space><strong>{item.type}</strong><Tag color={item.status === "FAILED" ? "red" : item.status === "RUNNING" ? "blue" : "green"}>{item.status}</Tag></Space><small>触发：{item.trigger}</small></div>
            <div><span>{parameters(item.parameters)}</span><small>{item.evidenceRef ?? "无证据引用"}</small></div>
          </div>
        ))}
      </section>
    </div>
  );
}
