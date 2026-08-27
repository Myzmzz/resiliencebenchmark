import { useCallback, useMemo, useState } from "react";
import { Alert, Button, Select, Space, Table, Tabs, Tag } from "antd";
import type { ColumnsType } from "antd/es/table";
import { ExportOutlined, InfoCircleOutlined, FileFilled } from "@ant-design/icons";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { getEvaluationResult } from "../api";
import { formatDuration, formatTime } from "../formatters";
import { useAsyncResource } from "../hooks/useAsyncResource";
import MetricCard from "../components/MetricCard";
import { OutcomeTag } from "../components/SemanticTag";
import { PageError, PageLoading } from "../components/PageState";
import ReuseTaskDrawer from "../components/ReuseTaskDrawer";
import type { EvaluationUnitSummary } from "../types";

export default function EvaluationResultDetailPage() {
  const { taskId } = useParams();
  const navigate = useNavigate();
  const [search, setSearch] = useSearchParams();
  const loader = useCallback((signal: AbortSignal) => getEvaluationResult(taskId ?? "", signal), [taskId]);
  const resource = useAsyncResource(loader);
  const [modelFilter, setModelFilter] = useState<string>();
  const [systemFilter, setSystemFilter] = useState<string>();
  const [harnessFilter, setHarnessFilter] = useState<string>();
  const [outcomeFilter, setOutcomeFilter] = useState<string>();
  const reuseOpen = search.get("reuse") === "1";
  const closeReuse = () => { const next = new URLSearchParams(search); next.delete("reuse"); setSearch(next, { replace: true }); };

  const result = resource.data;
  const harnesses = result?.harnesses ?? [];
  const scoreMatrix = useMemo(() => result?.scoreMatrix.filter((cell) => (!systemFilter || cell.systemId === systemFilter) && (modelFilter ? cell.modelId === modelFilter : !cell.modelId)) ?? [], [modelFilter, result, systemFilter]);
  const unitResults = useMemo(() => result?.unitResults.filter((unit) => (!systemFilter || unit.systemId === systemFilter) && (!harnessFilter || unit.harnessId === harnessFilter) && (!modelFilter || unit.modelId === modelFilter) && (!outcomeFilter || unit.outcome === outcomeFilter)) ?? [], [harnessFilter, modelFilter, outcomeFilter, result, systemFilter]);

  if (resource.loading && !result) return <div className="evaluation-page"><PageLoading /></div>;
  if (resource.error || !result) return <div className="evaluation-page"><PageError error={resource.error ?? new Error("结果不存在")} onRetry={() => void resource.reload()} /></div>;

  const resultColumns: ColumnsType<EvaluationUnitSummary> = [
    { title: "系统", dataIndex: "systemName" },
    { title: "题目", render: (_, item) => <div>{item.questionId}<div className="evaluation-muted">{item.questionTitle}</div></div> },
    { title: "Harness / Model", render: (_, item) => `${item.harnessName} / ${item.modelName}` },
    { title: "结果", render: (_, item) => <OutcomeTag outcome={item.outcome} /> },
    { title: "操作", render: (_, item) => <Button type="link" onClick={() => navigate(`/evaluation/monitoring/${result.taskId}/units/${item.unitId}`)}>查看详情</Button> },
  ];

  const overview = <div className="evaluation-two-column" style={{ gridTemplateColumns: "minmax(0, 2fr) minmax(340px, 1fr)" }}>
    <div>
      <section className="evaluation-panel"><div className="evaluation-panel-header"><h3>被测系统 × Harness 得分矩阵</h3><Select style={{ width: '160px' }} allowClear placeholder="全部模型" value={modelFilter} options={result.modelScores.map((item) => ({ value: item.modelId, label: item.modelName }))} onChange={setModelFilter} /></div>
        <div className="result-score-matrix" style={{ gridTemplateColumns: `190px repeat(${harnesses.length}, minmax(130px, 1fr))` }}>
          <div className="result-score-cell"><strong style={{fontSize: "14px"}}>被测系统 \ Harness</strong></div>{harnesses.map((item) => <div className="result-score-cell" key={item.id}><strong>{item.name}</strong></div>)}
          {result.systemResults.flatMap((system) => [
            <div className="result-score-cell" key={`${system.systemId}-label`}><strong>{system.systemName}</strong>
            <div style={{display: "flex", flexWrap: "wrap", gap: 4}}>
              {system.languages.map((language) => <Tag color="blue" key={language}>{language}</Tag>)}
            </div>
            </div>,
            ...harnesses.map((harness) => { const cell = scoreMatrix.find((item) => item.systemId === system.systemId && item.harnessId === harness.id); const score = cell?.score ?? 0; return <button type="button" className={`result-score-cell ${score >= 75 ? "result-score-high" : score >= 68 ? "result-score-mid" : "result-score-low"}`} key={`${system.systemId}-${harness.id}`} onClick={() => { setSystemFilter(system.systemId); setHarnessFilter(harness.id); }}><strong>{cell ? cell.score.toFixed(1) : "—"}</strong><span>{cell ? `有效 ${cell.validUnits}/${cell.totalUnits}` : "无评测单元"}</span></button>; }),
          ])}
        </div>
        <div className="evaluation-muted" style={{marginTop: 8}}><InfoCircleOutlined style={{ marginRight: 4 }} />点击单元格可查看该系统 × Harness下的模型与题目结果。</div>
      </section>
      <section className="evaluation-panel">
        <div className="evaluation-panel-header"><h3>系统结果</h3></div>
        <Table rowKey="systemId" bordered pagination={false} dataSource={result.systemResults} columns={[
          { title: "被测系统", dataIndex: "systemName" },
          { title: "实现语言覆盖", render: (_, item) => <Space wrap>{item.languages.map((language) => <Tag key={language}>{language}</Tag>)}</Space> },
          { title: "有效单元", render: (_, item) => `${item.validUnits}/${item.totalUnits}` },
          { title: "综合得分", dataIndex: "score" }, { title: "最佳 Harness", dataIndex: "bestHarnessName" },
          { title: "操作", render: (_, item) => <Button type="link" onClick={() => setSystemFilter(item.systemId)}>筛选结果</Button> }]} />
      </section>
    </div>
    <div>
      <section className="evaluation-panel">
        <div className="evaluation-panel-header">
          <h3>模型得分</h3>
          <Select style={{ width: '160px' }} allowClear placeholder="全部系统" value={systemFilter} options={result.systemResults.map((item) => ({ value: item.systemId, label: item.systemName }))} onChange={setSystemFilter} />
        </div>
        {result.modelScores.map((model) => <div className="model-score-row" style={{gridTemplateColumns: "130px 1fr auto"}} key={model.modelId}>
          <p style={{fontSize: 14}}>{model.modelName}</p>
          <div className="model-score-bar"><span style={{ width: `${model.score}%` }} /></div>
          <span>{model.score.toFixed(1)} · {model.validUnits} 有效</span>
        </div>)}
        <div className="evaluation-muted"><InfoCircleOutlined style={{ marginRight: 4 }} />模型是评分维度；语言仅用于实现覆盖说明，不参与得分计算。</div>
      </section>
      
      <section className="evaluation-panel" style={{display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12}}>
        <div style={{borderRight: '1px solid #edeef0', paddingRight: 24}}>
          <h3 style={{marginBottom: 12}}>终态与恢复</h3>
          <div className="oracle-gates">
            {result.recovery.map((item) => <div style={{borderTop: '1px solid #f1f5f9', borderBottom: 'none'}} className={`oracle-gate oracle-gate-${item.status.toLowerCase()}`} key={item.id}><span style={{color: '#030303'}}>{item.label}</span><p>{item.value ?? item.status}</p></div>)}
          </div>
        </div>
        
        <div style={{ marginTop: 12, paddingTop: 34 }}>
          {result.artifacts.map((artifact) => <div style={{height: 'fit-content', marginBottom: 12}} key={artifact.href}>
            <a style={{display: 'flex', justifyContent: 'space-between'}} href={artifact.href}><span style={{color: 'gray'}}><FileFilled /> {artifact.label}</span> <ExportOutlined /></a> 
          </div>)}
        </div>
      </section>

      <Alert style={{padding: '8px 12px'}} type="warning" showIcon title="展示 Oracle 门禁结论与证据引用，不公开Evaluator‑only Ground Truth 原文。" description="" />
    </div>
  </div>;

  const unitsTab = <section className="evaluation-panel">
    <div className="evaluation-panel-header">
      <h3>评测单元结果</h3>
      <Space wrap><Select style={{width: 160}} allowClear placeholder="全部系统" options={result.systemResults.map((item) => ({ value: item.systemId, label: item.systemName }))} onChange={setSystemFilter} /><Select style={{width: 160}} allowClear placeholder="全部 Harness" options={harnesses.map((item) => ({ value: item.id, label: item.name }))} onChange={setHarnessFilter} /><Select style={{width: 160}} allowClear placeholder="全部模型" options={result.modelScores.map((item) => ({ value: item.modelId, label: item.modelName }))} onChange={setModelFilter} /><Select style={{width: 160}} allowClear placeholder="全部结果" options={["PASS", "FAIL", "CASE_INVALID", "INCONCLUSIVE", "ABORTED"].map((value) => ({ value, label: value }))} onChange={setOutcomeFilter} /></Space>
    </div>
    <Table rowKey="unitId" bordered columns={resultColumns} dataSource={unitResults} pagination={{ pageSize: 12 }} />
  </section>;

  const systemsTab = <section className="evaluation-panel"><div className="evaluation-panel-header"><h3>被测系统对比</h3></div><Table bordered rowKey="systemId" pagination={false} dataSource={result.systemResults} columns={[{ title: "系统", dataIndex: "systemName" }, { title: "版本", dataIndex: "version" }, { title: "实现语言覆盖", render: (_, item) => <Space wrap>{item.languages.map((language) => <Tag key={language}>{language}</Tag>)}</Space> }, { title: "有效单元", render: (_, item) => `${item.validUnits}/${item.totalUnits}` }, { title: "得分", dataIndex: "score" }, { title: "最佳 Harness", dataIndex: "bestHarnessName" }, { title: "操作", render: (_, item) => <Button type="link" onClick={() => { setSystemFilter(item.systemId); }}>筛选题目结果</Button> }]} /></section>;
  const harnessRows = harnesses.map((harness) => {
    const cells = result.scoreMatrix.filter((cell) => cell.harnessId === harness.id && !cell.modelId);
    const score = cells.length ? cells.reduce((total, cell) => total + cell.score, 0) / cells.length : 0;
    return { ...harness, score, validUnits: cells.reduce((total, cell) => total + cell.validUnits, 0), totalUnits: cells.reduce((total, cell) => total + cell.totalUnits, 0) };
  });
  const harnessTab = <section className="evaluation-panel"><div className="evaluation-panel-header"><h3>Harness 对比</h3></div><Table bordered rowKey="id" pagination={false} dataSource={harnessRows} columns={[{ title: "Harness", dataIndex: "name" }, { title: "综合得分", render: (_, item) => item.score.toFixed(1) }, { title: "有效单元", render: (_, item) => `${item.validUnits}/${item.totalUnits}` }, { title: "覆盖系统", render: () => result.systemResults.length }, { title: "操作", render: (_, item) => <Button type="link" onClick={() => { setHarnessFilter(item.id); }}>查看单元结果</Button> }]} /></section>;

  const modelsTab = <section className="evaluation-panel"><div className="evaluation-panel-header"><h3>模型对比</h3><Select allowClear style={{width: 200}} placeholder="全部系统" options={result.systemResults.map((item) => ({ value: item.systemId, label: item.systemName }))} onChange={setSystemFilter} /></div>{result.modelScores.map((model) => <div className="model-score-row" style={{gridTemplateColumns: "130px 1fr auto"}} key={model.modelId}><div>{model.modelName}</div><div className="model-score-bar"><span style={{ width: `${model.score}%` }} /></div><span>{model.score.toFixed(1)} · {model.validUnits} 有效单元</span></div>)}<div className="evaluation-muted">语言只作为实现覆盖元数据，不参与模型得分。</div></section>;

  return <div className="evaluation-page">
    <a className="evaluation-back" onClick={() => navigate("/evaluation/results")}>← 返回结果分析</a>
    <header className="evaluation-page-header"><div><Space><h2>评测结果详情</h2><Tag color="green">{result.terminalStatus}</Tag></Space><p>{result.name} · {result.taskId}</p></div><Space>{result.artifacts[0] && <Button href={result.artifacts[0].href} icon={<ExportOutlined />}>导出报告</Button>}<Button type="primary" ghost onClick={() => setSearch({ reuse: "1" })}>复用任务</Button></Space></header>
    
    <section className="evaluation-panel evaluation-wizard-summary">
        <div>{result.environmentName}</div>
        <div>{result.systems.length} 个被测系统</div>
        <div>{result.harnesses.length} 个 Harness</div>
        <div>{result.modelScores.length} 个模型</div>
        <div>{result.totalUnits} 个评测单元</div>
        <div>总耗时 {formatDuration(result.durationSeconds)}</div>
      </section>
    <div className="evaluation-summary-grid six" style={{marginBottom: 8}}><MetricCard label="综合得分" value={result.score.toFixed(1)} /><MetricCard label="有效单元" value={`${result.validUnits}/${result.totalUnits}`} /><MetricCard label="PASS" value={result.pass} tone="success" /><MetricCard label="FAIL" value={result.fail} tone="danger" /><MetricCard label="CASE_INVALID" value={result.caseInvalid} tone="warning" /><MetricCard label="完成时间" value={formatTime(result.finishedAt)} /></div>

    <div className="evaluation-muted"><InfoCircleOutlined style={{ marginRight: 4 }} />有效得分不包含 CASE_INVALID；语言只作为系统与目标服务的实现覆盖信息，不参与得分计算。</div>

    <Tabs className="question-tabs" style={{ marginTop: 12 }} defaultActiveKey="overview" items={[
      { key: "overview", label: "总览", children: overview }, { key: "systems", label: "系统对比", children: systemsTab }, 
      { key: "harness", label: "Harness 对比", children: harnessTab }, 
      { key: "models", label: "模型对比", children: modelsTab }, 
      { key: "units", label: "题目结果", children: unitsTab }, 
      { key: "oracle", label: "Oracle 与证据", children: 
        <div className="evaluation-two-column">
          <section className="evaluation-panel">
            <h3 style={{marginBottom: 12}}>Oracle 汇总</h3>
            <div className="evaluation-option-list">{result.oracleSummary.map((item) => <div className="evaluation-option-row" key={item.id}><p>{item.label}</p><span>{item.passed} 通过 · {item.failed} 失败{item.invalid ? ` · ${item.invalid} 无效` : ""}</span></div>)}</div>
          </section>

          <section className="evaluation-panel"><Alert style={{padding: '8px 12px'}} type="warning" showIcon title="不公开 Evaluator-only Ground Truth 原文" description="页面只展示门禁结论和证据引用。" /><div className="evaluation-option-list" style={{ marginTop: 12 }}>{result.artifacts.map((artifact) => <div className="evaluation-option-row" key={artifact.href}><a href={artifact.href}>{artifact.label}</a></div>)}</div></section>
        </div> 
      }]} />

    <ReuseTaskDrawer taskId={result.taskId} sourceName={result.name} open={reuseOpen} onClose={closeReuse} />
  </div>;
}
