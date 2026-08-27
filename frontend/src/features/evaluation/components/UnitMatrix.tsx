import { Button, Select, Space, Tag } from "antd";
import type { EvaluationUnitSummary, UnitOutcome } from "../types";
import { FilterOutlined, MenuOutlined, RightOutlined } from "@ant-design/icons";

function cellClass(unit?: EvaluationUnitSummary): string {
  if (!unit || unit.status === "PENDING") return "unit-cell-pending";
  if (unit.status === "RUNNING") return "unit-cell-running";
  return {
    PASS: "unit-cell-pass",
    FAIL: "unit-cell-fail",
    CASE_INVALID: "unit-cell-invalid",
    INCONCLUSIVE: "unit-cell-invalid",
    ABORTED: "unit-cell-fail",
    SKIPPED: "unit-cell-pending",
  }[unit.outcome as UnitOutcome] ?? "unit-cell-pending";
}

export default function UnitMatrix({
  units,
  systemId,
  harnessId,
  selectedUnitId,
  onSelect,
}: {
  units: EvaluationUnitSummary[];
  systemId?: string;
  harnessId?: string;
  selectedUnitId?: string;
  onSelect: (unit: EvaluationUnitSummary) => void;
}) {
  const scoped = units.filter((unit) => (!systemId || unit.systemId === systemId) && (!harnessId || unit.harnessId === harnessId));
  const models = [...new Map(scoped.map((unit) => [unit.modelId, unit.modelName])).entries()];
  const indexes = [...new Set(scoped.map((unit) => unit.questionIndex))].sort((a, b) => a - b);
  const selected = scoped.find((unit) => unit.unitId === selectedUnitId) ?? scoped.find((unit) => unit.status === "RUNNING");
  return (
    <section className="evaluation-panel unit-matrix-panel">
      <div className="evaluation-panel-header">
        <div><h3>评测单元矩阵</h3><div className="evaluation-muted">16/39个单元已完成</div></div>
        <Space size="small" wrap>
          {/* <Tag color="green">PASS</Tag><Tag color="red">FAIL</Tag><Tag color="gold">INVALID</Tag><Tag color="blue">运行中</Tag><Tag>待执行</Tag> */}
          <div className="unit-status unit-status-pass">PASS</div>
          <div className="unit-status unit-status-fail">FAIL</div>
          <div className="unit-status unit-status-invalid">INVALID</div>
          <div className="unit-status unit-status-running">运行中</div>
          <div className="unit-status unit-status-pending">待执行</div>
        </Space>
      </div>
      {models.length === 0 ? <div className="evaluation-muted">当前筛选没有评测单元</div> : (
        <div className="unit-matrix-scroll">
          <div className="unit-matrix-grid" style={{ gridTemplateColumns: `110px repeat(${indexes.length}, minmax(28px, 1fr))` }}>
            <div className="unit-matrix-label">模型</div>
            {indexes.map((index) => <div className="unit-matrix-index" key={index}>{String(index).padStart(2, "0")}</div>)}
            {models.flatMap(([modelId, modelName]) => [
              <div className="unit-matrix-label" key={`${modelId}-label`}>{modelName}</div>,
              ...indexes.map((index) => {
                const unit = scoped.find((candidate) => candidate.modelId === modelId && candidate.questionIndex === index);
                return (
                  <button
                    type="button"
                    title={unit ? `${unit.questionId} ${unit.questionTitle}` : `题目 ${index}`}
                    aria-label={unit ? `${modelName} ${unit.questionTitle} ${unit.outcome ?? unit.status}` : `${modelName} 题目 ${index}`}
                    className={`unit-matrix-cell ${cellClass(unit)} ${unit?.unitId === selected?.unitId ? "is-selected" : ""}`}
                    key={`${modelId}-${index}`}
                    disabled={!unit}
                    onClick={() => unit && onSelect(unit)}
                  >{unit?.status === "RUNNING" ? "●" : unit?.outcome === "PASS" ? "✓" : unit?.outcome === "FAIL" ? "×" : unit?.outcome === "CASE_INVALID" ? "!" : "–"}</button>
                );
              }),
            ])}
          </div>
        </div>
      )}
      {selected && (
        <div className="unit-selected-card">
          <div className="unit-selected-card-info"><strong>{selected.questionId} · {selected.questionTitle}</strong><span>{selected.harnessName} / {selected.modelName} <br /> Trial {selected.currentTrial ?? 0}/{selected.maxTrials}</span></div>
          <Button color="primary" variant="outlined"  onClick={() => onSelect(selected)}>
            进入题目详情<RightOutlined />
          </Button>
        </div>
      )}
      <div className="unit-matrix-footer">
        <Select prefix={<FilterOutlined />} style={{ width: 160 }} size="medium" defaultValue="all" options={[{ value: "all", label: "全部状态" }]} />
        <Button size="medium"><MenuOutlined />查看全部单元</Button>
      </div>
    </section>
  );
}
