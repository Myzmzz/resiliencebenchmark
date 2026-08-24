import { CheckCircleFilled, CloseCircleFilled, ClockCircleOutlined, WarningFilled } from "@ant-design/icons";
import type { OracleGate } from "../types";

export default function OracleGateList({ gates }: { gates: OracleGate[] }) {
  return <div className="oracle-gates">{gates.map((gate) => {
    const Icon = gate.status === "PASS" ? CheckCircleFilled : gate.status === "FAIL" ? CloseCircleFilled : gate.status === "CASE_INVALID" ? WarningFilled : ClockCircleOutlined;
    return <div className={`oracle-gate oracle-gate-${gate.status.toLowerCase()}`} key={gate.id}><span><Icon /> {gate.label}</span><strong>{gate.status}</strong></div>;
  })}</div>;
}
