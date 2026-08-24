import type { ReactNode } from "react";

export default function MetricCard({ label, value, footer, tone = "default" }: {
  label: string;
  value: ReactNode;
  footer?: ReactNode;
  tone?: "default" | "primary" | "success" | "danger" | "warning";
}) {
  return (
    <div className={`evaluation-metric evaluation-metric-${tone}`}>
      <div className="evaluation-metric-label">{label}</div>
      <div className="evaluation-metric-value">{value}</div>
      {footer && <div className="evaluation-metric-footer">{footer}</div>}
    </div>
  );
}
