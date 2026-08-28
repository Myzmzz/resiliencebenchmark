import { useState, useEffect } from "react";
import { Table, Badge, Button, Drawer, Tag, Alert, Spin, Descriptions, List, Typography, Space, Select, Input, Tabs } from "antd";
import type { ColumnsType } from "antd/es/table";
import { ReloadOutlined, PlusOutlined, SearchOutlined, SyncOutlined, InfoCircleOutlined, ExportOutlined } from "@ant-design/icons";
import { fetchApplications } from "../services/api";
import type { Application, ReadinessGap } from "../types/application";
import { useNavigate } from "react-router-dom";
import MetricCard from "../features/evaluation/components/MetricCard";


export default function ApplicationsDetailPage() {
  const navigate = useNavigate();
  const [systemValue, setSystemValue] = useState('OTel Demo');
  const [searchService, setSearchService] = useState('');
  const [statusFilter, setStatusFilter] = useState('all');
  const [nodeFilter, setNodeFilter] = useState('all');
  const [activeTab, setActiveTab] = useState('instance');

  // 服务列表表格数据
  const serviceTableData = [
    {
      key: '1',
      serviceName: 'frontend',
      workload: 'Deployment/frontend',
      readyReplica: '2 / 2',
      podNode: 'frontend-7c9d6 · worker-01',
      image: 'ghcr.io/otel-demo/frontend:v1.12.0',
      status: 'ok'
    },
    {
      key: '2',
      serviceName: 'checkout',
      workload: 'Deployment/checkout',
      readyReplica: '2 / 2',
      podNode: 'checkout-7d9c8 · worker-01',
      image: 'ghcr.io/otel-demo/checkout:v1.12.0',
      status: 'ok'
    },
    {
      key: '3',
      serviceName: 'cart',
      workload: 'Deployment/cart',
      readyReplica: '2 / 2',
      podNode: 'cart-6b8df4 · worker-02',
      image: 'ghcr.io/otel-demo/cart:v1.12.0',
      status: 'ok'
    },
    {
      key: '4',
      serviceName: 'payment',
      workload: 'Deployment/payment',
      readyReplica: '2 / 2',
      podNode: 'payment-6b8df4 · worker-01',
      image: 'ghcr.io/otel-demo/payment:v1.12.0',
      status: 'error'
    },
    {
      key: '5',
      serviceName: 'inventory',
      workload: 'Deployment/inventory',
      readyReplica: '2 / 2',
      podNode: 'inventory-5cf7db · worker-02',
      image: 'ghcr.io/otel-demo/inventory:v1.12.0',
      status: 'ok'
    },
    {
      key: '6',
      serviceName: 'recommendation',
      workload: 'Deployment/recommendation',
      readyReplica: '1 / 1',
      podNode: 'recommendation-9d6f7 · worker-01',
      image: 'ghcr.io/otel-demo/recommendation:v1.12.0',
      status: 'notice'
    }
  ];

  // 右侧Pod实例表格
  const podTableData = [
    {
      key: 'p1',
      podNode: 'frontend-7c9d6f8b-x2k9p worker-01',
      podIp: '10.244.1.12',
      ready: true,
      restartCount: 0,
      createTime: '2024-05-20 10:12:34'
    },
    {
      key: 'p2',
      podNode: 'frontend-7c9d6f8b-p7m2q worker-02',
      podIp: '10.244.2.18',
      ready: true,
      restartCount: 1,
      createTime: '2024-05-20 10:13:01'
    }
  ];

  // 服务列表列
  const serviceColumns = [
    { title: '服务名称', dataIndex: 'serviceName', width: 120 },
    { title: 'Workload', dataIndex: 'workload', width: 180 },
    { title: '就绪副本', dataIndex: 'readyReplica', width: 180 },
    { title: 'Pod / 节点', dataIndex: 'podNode', width: 180 },
    { title: '镜像', dataIndex: 'image' },
    {
      title: '状态',
      dataIndex: 'status',
      width: 80,
      render: (val: string) => {
        return <Tag color={val === 'ok' ? 'success' : val === 'error' ? 'error' : 'warning'}>{val === 'ok' ? '正常' : val === 'error' ? '异常' : '关注'}</Tag>
      }
    }
  ];

  // Pod实例表格列
  const podColumns = [
    { title: 'Pod 名称 / 节点', dataIndex: 'podNode' },
    { title: 'Pod IP', dataIndex: 'podIp', width: 120 },
    {
      title: '容器就绪',
      dataIndex: 'ready',
      width: 100,
      render: (val: boolean) => val ? <span style={{ color: '#00b42a' }}>● Ready</span> : ''
    },
    { title: '重启次数', dataIndex: 'restartCount', width: 90 },
    { title: '创建时间', dataIndex: 'createTime', width: 170 }
  ]

  return (
    <div className="evaluation-page">
      <header className="evaluation-page-header">
        <div><h2>新增被测系统</h2><p>连接源码仓库，识别部署入口并部署到实验环境</p></div>
        <Space>
          <Button icon={<PlusOutlined />} onClick={() => navigate("/environments/applications/new")}>新增系统</Button>
          <Button icon={<ReloadOutlined />}>刷新数据</Button>
        </Space>
      </header>

      <section className="evaluation-panel">
        <h3>系统信息 <Tag color="success" style={{ marginLeft: 8 }}>运行中</Tag></h3>
        <div className="app-detail-info">
          <div className="app-detail-info-row">
            <div>
              <p className="label">系统名称</p>
              <span className="value">OTel Demo</span>
            </div>
            <div>
              <p className="label">业务版本</p>
              <span className="value">v1.12.0</span>
            </div>
            <div>
              <p className="label">实验环境</p>
              <span className="value">研发测试集群</span>
            </div>
            <div>
              <p className="label">Namespace</p>
              <span className="value">otel-demo</span>
            </div>
          </div>
          <div className="app-detail-info-row">
            <div>
              <p className="label">源码仓库</p>
              <span className="value">github.com/open-telemetry/opentelemetry-demo</span>
            </div>
            <div>
              <p className="label">分支</p>
              <span className="value">main</span>
            </div>
            <div>
              <p className="label">Commit</p>
              <span className="value">8a4f9c2</span>
            </div>
            <div>
              <p className="label">最近同步</p>
              <span className="value">10:36:42</span>
            </div>
          </div>
        </div>
      </section>

      <div className="evaluation-summary-grid">
        <MetricCard label="服务" value={22} />
        <MetricCard label="正常服务" value={21} tone="primary" />
        <MetricCard label="异常服务" value={1} tone="danger" />
        <MetricCard label="CodeGraph" value={'可用'} tone="success" />
      </div>

      <div className="evaluation-two-column" style={{ gridTemplateColumns: "2fr 1fr" }}>
        <section className="evaluation-panel">
          <header className="evaluation-page-header">
            <div><h3>服务列表</h3></div>
            <Space>
              <Input
                placeholder="搜索服务名称"
                prefix={<SearchOutlined style={{ color: '#999' }} />}
                value={searchService}
                onChange={e => setSearchService(e.target.value)}
                style={{ width: 240 }}
              />
              <Select value={statusFilter} onChange={setStatusFilter} options={[{ label: '全部状态', value: 'all' }]} style={{ width: 130 }} />
              <Select value={nodeFilter} onChange={setNodeFilter} options={[{ label: '全部节点', value: 'all' }]} style={{ width: 130 }} />
            </Space>
          </header>

          <Table
            columns={serviceColumns}
            dataSource={serviceTableData}
            pagination={false}
            size="middle"
            rowClassName={(record) => record.serviceName === 'frontend' ? 'active-row' : ''}
            scroll={{ x: true }}
          />
        </section>

        <section className="evaluation-panel">
          <h3>frontend服务详情</h3>

          <Tabs
            activeKey={activeTab}
            onChange={setActiveTab}
            items={[
              { key: 'instance', label: '运行实例' },
              { key: 'image', label: '镜像信息' },
              { key: 'code', label: '代码关联' }
            ]}
          />
          <Table
            columns={podColumns}
            dataSource={podTableData}
            pagination={false}
            size="small"
            scroll={{ x: true }}
          />
          <div className="link-tip" style={{marginTop: 12}}>
            <a>在实验环境中查看 <ExportOutlined /></a>
          </div>
        </section>
      </div>

      <section className="evaluation-panel">
        <h3>CodeGraph <Tag color="success" style={{ marginLeft: 8 }}>可用</Tag></h3>

        <div className="cg-content">
          <div className="cg-content-left">
            <div className="info-row">
              <div className="info-label">绑定 Commit</div>
              <span className="info-value">8a4f9c2</span>
            </div>
            <div className="info-row">
              <div className="info-label">生成时间</div>
              <span className="info-value">2026-08-24 10:28:15</span>
            </div>
            <div className="info-row">
              <div className="info-label">代码目录</div>
              <span className="info-value">/workspace/opentelemetry-demo</span>
            </div>
            <div className="info-row">
              <div className="info-label">覆盖文件</div>
              <span className="info-value">1,284</span>
            </div>
          </div>
          <div className="cg-center-box">
            <div>
              <div className="cg-center">
                <div className="cg-stat-item">
                  <div className="cg-stat-title">节点</div>
                  <div className="cg-stat-num">18,462</div>
                </div>
                <div className="cg-stat-item">
                  <div className="cg-stat-title">关系</div>
                  <div className="cg-stat-num">31,905</div>
                </div>
                <div className="cg-stat-item">
                  <div className="cg-stat-title">服务覆盖</div>
                  <div className="cg-stat-num">22 / 22</div>
                </div>
              </div>
              <div className="cg-footer">
                <span>Commit 与运行版本一致 · 数据来源: 源码分析任务</span>
              </div>
            </div>
            <Space vertical>
              <Button style={{width:160, marginBottom:12}}>打开图谱</Button>
              <Button style={{width:160}} icon={<SyncOutlined />}>重新生成</Button>
            </Space>
          </div>
        </div>
        
      </section>

    </div>
  );
}