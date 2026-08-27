import type { ReactNode } from "react";

export default function MetricCard({ label, value, footer, tone = "default", percent }: {
  label: string;
  value: ReactNode;
  footer?: ReactNode;
  tone?: "default" | "primary" | "success" | "danger" | "warning";
  percent?: string;
}) {
  return (
    <div className={`evaluation-metric evaluation-metric-${tone}`}>
      <div className="evaluation-metric-label">{label}</div>
      <div className="evaluation-metric-value">{value}{percent && <span style={{fontSize: 14, color: "#6f6f6f" }}> · {percent}</span>}</div>
      {footer && <div className="evaluation-metric-footer">{footer}</div>}
    </div>
  );
}
