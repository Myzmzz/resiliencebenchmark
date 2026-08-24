import { useCallback, useState } from "react";
import { Button, Input, Pagination, Select, Space, Table, Tag } from "antd";
import type { ColumnsType } from "antd/es/table";
import { ReloadOutlined } from "@ant-design/icons";
import { useNavigate } from "react-router-dom";
import { listEvaluationResults } from "../api";
import { formatTime } from "../formatters";
import { useAsyncResource } from "../hooks/useAsyncResource";
import type { EvaluationResultSummary } from "../types";
import { PageEmpty, PageError, PageLoading } from "../components/PageState";

const PAGE_SIZE = 10;

export default function EvaluationResultsPage() {
  const navigate = useNavigate();
  const [q, setQ] = useState("");
  const [terminalStatus, setTerminalStatus] = useState<string>();
  const [page, setPage] = useState(1);
  const loader = useCallback((signal: AbortSignal) => listEvaluationResults({ q, terminalStatus, page, pageSize: PAGE_SIZE }, signal), [page, q, terminalStatus]);
  const resource = useAsyncResource(loader);
  const columns: ColumnsType<EvaluationResultSummary> = [
    { title: "任务", render: (_, item) => <div><a onClick={() => navigate(`/evaluation/results/${item.taskId}`)}>{item.name}</a><div className="evaluation-muted">{item.taskId}</div></div> },
    { title: "被测系统", render: (_, item) => <Space wrap>{item.systems.map((system) => <Tag key={system}>{system}</Tag>)}</Space> },
    { title: "矩阵", render: (_, item) => `${item.harnessCount} Harness · ${item.modelCount} 模型 · ${item.totalUnits} 单元` },
    { title: "有效覆盖", render: (_, item) => `${item.validUnits}/${item.totalUnits}` },
    { title: "结果", render: (_, item) => <Space><Tag color="green">PASS {item.pass}</Tag><Tag color="red">FAIL {item.fail}</Tag><Tag color="gold">INVALID {item.caseInvalid}</Tag></Space> },
    { title: "综合得分", dataIndex: "score", sorter: (a, b) => a.score - b.score },
    { title: "终态", render: (_, item) => <Tag color={item.terminalStatus === "COMPLETED" ? "green" : "red"}>{item.terminalStatus}</Tag> },
    { title: "完成时间", render: (_, item) => formatTime(item.finishedAt) },
    { title: "操作", render: (_, item) => <Space><Button type="link" onClick={() => navigate(`/evaluation/results/${item.taskId}`)}>查看详情</Button><Button type="link" onClick={() => navigate(`/evaluation/results/${item.taskId}?reuse=1`)}>复用</Button></Space> },
  ];
  return <div className="evaluation-page">
    <header className="evaluation-page-header"><div><h2>结果分析</h2><p>查看终态任务的系统、Harness、模型、Oracle 和恢复结果</p></div><Button icon={<ReloadOutlined />} onClick={() => void resource.reload()}>刷新数据</Button></header>
    <section className="evaluation-panel"><div className="evaluation-panel-header"><h3>已完成任务</h3><Space><Input.Search placeholder="搜索任务" onSearch={(value) => { setPage(1); setQ(value); }} /><Select allowClear placeholder="全部终态" style={{ width: 190 }} options={["COMPLETED", "FAILED", "BLOCKED", "RESET_FAILED", "ABORTED", "CASE_INVALID"].map((value) => ({ value, label: value }))} onChange={(value) => { setPage(1); setTerminalStatus(value); }} /></Space></div>
      {resource.loading && !resource.data ? <PageLoading /> : resource.error ? <PageError error={resource.error} onRetry={() => void resource.reload()} /> : !resource.data?.items.length ? <PageEmpty label="暂无终态评测结果" /> : <><Table rowKey="taskId" columns={columns} dataSource={resource.data.items} pagination={false} scroll={{ x: 1350 }} /><div className="evaluation-actions"><span className="evaluation-muted">共 {resource.data.total} 个结果</span><Pagination current={page} pageSize={PAGE_SIZE} total={resource.data.total} showSizeChanger={false} onChange={setPage} /></div></>}
    </section>
  </div>;
}
