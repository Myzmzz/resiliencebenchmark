import { CheckCircleFilled, CloseCircleFilled, ClockCircleOutlined, WarningFilled } from "@ant-design/icons";
import type { OracleGate } from "../types";

export default function OracleGateList({ gates }: { gates: OracleGate[] }) {
  return <div className="oracle-gates">{gates.map((gate) => {
    const Icon = gate.status === "PASS" ? CheckCircleFilled : gate.status === "FAIL" ? CloseCircleFilled : gate.status === "CASE_INVALID" ? WarningFilled : ClockCircleOutlined;
    return <div className={`oracle-gate oracle-gate-${gate.status.toLowerCase()}`} key={gate.id}><span style={{color: '#3b3b3b'}}>{gate.label}</span><p><Icon style={{ marginRight: 4 }} />{gate.status}</p></div>;
  })}</div>;
}
