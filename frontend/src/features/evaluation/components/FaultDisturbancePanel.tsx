import { Descriptions, Empty, Space, Tag } from "antd";
import type { DisturbanceRecord, MainFaultRecord } from "../types";
import { RightOutlined } from "@ant-design/icons";

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
        <div className="evaluation-panel-header"><h3>主故障</h3></div>
        {!fault ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="主故障尚未应用" /> : (
          <Descriptions size="small" column={3}>
            <Descriptions.Item label="类型">{fault.type}</Descriptions.Item>
            <Descriptions.Item label="执行器">{fault.executor}</Descriptions.Item>
            <Descriptions.Item label="实验 ID">{fault.experimentId}</Descriptions.Item>
            <Descriptions.Item label="目标">{fault.target}</Descriptions.Item>
            <Descriptions.Item label="参数" span={2}>{parameters(fault.parameters)}</Descriptions.Item>
            <Descriptions.Item label="状态"><span style={{color: "green"}}>已应用 · 效果已观测</span></Descriptions.Item>
          </Descriptions>
        )}
      </section>
      <section className="disturbance-card">
        <div className="evaluation-panel-header"><h3>动态扰动</h3><span style={{color: "#4f4f4f"}}>扰动预算 {budget.used} / {budget.total}</span></div>
        {disturbances.length === 0 ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="尚未触发动态扰动" /> : disturbances.map((item) => (
          <div className="disturbance-row-new" key={item.disturbanceId}>
            <div>{item.type}<p><Tag color="orange" >已触发</Tag></p></div>
            <div>触发条件<p>{item.trigger}</p></div>
            <div>参数<p>{parameters(item.parameters)}</p></div>
            <div>状态<p><Tag color={item.status === "FAILED" ? "red" : item.status === "RUNNING" ? "blue" : "green"}>{item.status}</Tag></p></div>
            <div>证据<p>{item.evidenceRef ?? "无证据引用"}</p></div>
            <div style={{color:'#3d5cf5', cursor:'pointer'}}>查看扰动详情 <RightOutlined /></div>
            {/* <div><Space><strong>{item.type}</strong><Tag color={item.status === "FAILED" ? "red" : item.status === "RUNNING" ? "blue" : "green"}>{item.status}</Tag></Space><small>触发：{item.trigger}</small></div>
            <div><span>{parameters(item.parameters)}</span><small>{item.evidenceRef ?? "无证据引用"}</small></div> */}
          </div>
        ))}
        <div style={{color: "#4f4f4f", fontSize: "12px", display: "flex", justifyContent: "space-between", borderTop: "1px solid #fed7aa", paddingTop: "12px"}}>
          <span>剩余扰动1 · 按策略触发</span>
          <span>待触发</span>
        </div>
      </section>
    </div>
  );
}
