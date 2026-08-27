import { useState, useEffect } from "react";
import { Table, Badge, Button, Drawer, Tag, Alert, Spin, Descriptions, List, Typography, Space, Steps, Input, Select, Radio, Progress, Tabs } from "antd";
import type { ColumnsType } from "antd/es/table";
import { ReloadOutlined, PlusOutlined, ArrowLeftOutlined, InfoCircleTwoTone, CheckCircleOutlined, InfoCircleOutlined, DownloadOutlined, SearchOutlined } from "@ant-design/icons";
import { fetchApplications } from "../services/api";
import type { Application, ReadinessGap } from "../types/application";
import type { RadioChangeEvent } from 'antd';
import { useNavigate } from "react-router-dom";


export default function ApplicationsCreatePage() {
  const navigate = useNavigate();
  const [step, setStep] = useState(0);
  const [formData, setFormData] = useState({
    name: "",
    gitUrl: "",
    authType: null,
    username: "",
    token: "",
    branch: "",
    credentialName: "",
    deployEntry: 'helm',
    cluster: null,
    nsMode: 'new',
    namespace: '',
    releaseName: '',
    storageClass: null,
    ingressClass: null,
    imageSecret: null,
  });

  const [activeTab, setActiveTab] = useState('list');
  const [resourceType, setResourceType] = useState('all');
  const [searchText, setSearchText] = useState('');

  const STEP_ITEMS = ["仓库连接", "部署识别", "目标环境", "配置预览与部署"].map((title) => ({ title }));
  // 部署入口数据
  interface DeployEntryItem {
    key: string;
    label: string;
    path: string;
    isRecommend?: boolean;
  }

  const entryList: DeployEntryItem[] = [
    {
      key: 'helm',
      label: 'Helm Chart',
      path: 'deploy/chart',
      isRecommend: true,
    },
    {
      key: 'kustomize',
      label: 'Kustomize',
      path: 'deploy/overlays/test',
    },
    {
      key: 'yaml',
      label: 'Kubernetes YAML',
      path: 'k8s/',
    },
  ];
  const resourceList = [
    { label: 'Workload', count: 6 },
    { label: 'Service', count: 6 },
    { label: 'ConfigMap', count: 3 },
    { label: 'Secret 引用', count: 2 },
    { label: 'PVC', count: 1 },
    { label: '其他', count: 6 },
  ];
  // 容量校验数据
  const capacityData = [
    { label: 'CPU', used: 4.5, total: 42.8, unit: 'Core' },
    { label: '内存', used: 8, total: 118, unit: 'GiB' },
    { label: '存储', used: 20, total: 420, unit: 'GiB' },
  ];

  // 预检项列表
  const preCheckList = [
    '集群 API 可访问',
    'Namespace 名称可用',
    'StorageClass 可用',
    'IngressClass 可用',
    '镜像仓库可访问',
    '资源容量满足部署需求',
  ];

  // 服务镜像表格数据
  interface ServiceRowItem {
    key: string;
    serviceName: string;
    workload: string;
    image: string;
    configSource: string;
  }
  const serviceData: ServiceRowItem[] = [
    {
      key: '1',
      serviceName: 'frontend',
      workload: 'Deployment',
      image: 'registry.example.com/order/frontend:1.4.0',
      configSource: 'values.yaml',
    },
    {
      key: '2',
      serviceName: 'checkout',
      workload: 'Deployment',
      image: 'registry.example.com/order/checkout:1.4.0',
      configSource: 'values.yaml',
    },
    {
      key: '3',
      serviceName: 'payment',
      workload: 'Deployment',
      image: 'registry.example.com/order/payment:1.4.0',
      configSource: 'values.yaml',
    },
    {
      key: '4',
      serviceName: 'inventory',
      workload: 'StatefulSet',
      image: 'registry.example.com/order/inventory:1.4.0',
      configSource: 'templates/inventory.yaml',
    },
  ];

  // 表格列配置
  const tableColumns = [
    {
      title: '服务名称',
      dataIndex: 'serviceName',
      key: 'serviceName',
    },
    {
      title: 'Workload',
      dataIndex: 'workload',
      key: 'workload',
    },
    {
      title: '镜像',
      dataIndex: 'image',
      key: 'image',
    },
    {
      title: '配置来源',
      dataIndex: 'configSource',
      key: 'configSource',
    },
  ];
  const tableData = [
    {
      key: '1',
      resourceType: 'Namespace',
      resourceName: 'order-service',
      namespace: '—',
      config: '新建 Namespace',
      change: '新增'
    },
    {
      key: '2',
      resourceType: 'Deployment',
      resourceName: 'frontend',
      namespace: 'order-service',
      config: '2 副本 · frontend:1.4.0',
      change: '新增'
    },
    {
      key: '3',
      resourceType: 'Service',
      resourceName: 'frontend',
      namespace: 'order-service',
      config: 'ClusterIP · 8080',
      change: '新增'
    },
    {
      key: '4',
      resourceType: 'StatefulSet',
      resourceName: 'inventory',
      namespace: 'order-service',
      config: '1 副本 · inventory:1.4.0',
      change: '新增'
    },
    {
      key: '5',
      resourceType: 'PersistentVolumeClaim',
      resourceName: 'inventory-data',
      namespace: 'order-service',
      config: 'standard‑rwo · 20 GiB',
      change: '新增'
    },
    {
      key: '6',
      resourceType: 'Ingress',
      resourceName: 'order-service',
      namespace: 'order-service',
      config: 'nginx · /orders',
      change: '新增'
    }
  ];
  const columns = [
    {
      title: '资源类型',
      dataIndex: 'resourceType',
      key: 'resourceType',
      width: 160
    },
    {
      title: '资源名称',
      dataIndex: 'resourceName',
      key: 'resourceName',
      width: 140
    },
    {
      title: 'Namespace',
      dataIndex: 'namespace',
      key: 'namespace',
      width: 120
    },
    {
      title: '关键配置',
      dataIndex: 'config',
      key: 'config'
    },
    {
      title: '变更',
      dataIndex: 'change',
      key: 'change',
      width: 80,
      render: (text: string) => <Tag color="blue">{text}</Tag>
    },
    {
      title: '操作',
      key: 'action',
      width: 100,
      render: () => (
        <a style={{ color: '#2563eb' }}>查看 YAML</a>
      )
    }
  ];

  const onChange = (e: RadioChangeEvent) => {
    setFormData({ ...formData, deployEntry: e.target.value });
  };

  return (
    <div className="evaluation-page">
      <a className="evaluation-back" onClick={() => navigate("/environments/applications")}><ArrowLeftOutlined /> 返回系统列表</a>
      <header className="evaluation-page-header"><div><h2>新增被测系统</h2><p>连接源码仓库，识别部署入口并部署到实验环境</p></div></header>

      <section className="evaluation-panel">
        <Steps style={{ padding: "0 100px" }} current={step} items={STEP_ITEMS} />
      </section>

      {
        step > 0 && <section className="evaluation-panel" style={{ display: "flex", alignItems: "center", justifyContent: "space-between", }}>
          <div className="app-header-info">
            <div>Order Service</div>
            <div>main</div>
            <div>Commit 3f82a1c <Tag color="green" style={{ marginLeft: 8 }}> 仓库已锁定</Tag></div>
          </div>

          <Button type="link" onClick={() => setStep((current) => current - 1)}>返回{STEP_ITEMS[step - 1].title}</Button>
        </section>
      }

      {
        step === 0 && (
          <div className="evaluation-two-column" style={{ gridTemplateColumns: "1fr 1fr" }}>
            <section className="evaluation-panel">
              <h3>仓库连接</h3>
              <Space orientation="vertical" size="middle" wrap style={{ width: "100%", marginTop: 16 }}>
                <label style={{ display: "flex", alignItems: "center" }}>
                  <div style={{ width: 90, flexShrink: 0 }}>系统名称</div><Input value={formData.name} onChange={(event) => setFormData({ ...formData, name: event.target.value })} placeholder="输入系统名称" />
                </label>
                <label style={{ display: "flex", alignItems: "center" }}>
                  <div style={{ width: 90, flexShrink: 0 }}>Git地址</div><Input value={formData.gitUrl} onChange={(event) => setFormData({ ...formData, gitUrl: event.target.value })} placeholder="输入Git地址" />
                </label>
                <label style={{ display: "flex", alignItems: "center" }}>
                  <div style={{ width: 90, flexShrink: 0 }}>认证方式</div><Select style={{ width: '90%' }} options={[
                    { value: 1, label: "HTTPS用户名+ Token" },
                  ]} value={formData.authType} onChange={(event) => setFormData({ ...formData, authType: event })} placeholder="选择认证方式" />
                </label>
                <label style={{ display: "flex", alignItems: "center" }}>
                  <div style={{ width: 90, flexShrink: 0 }}>用户名</div><Input value={formData.username} onChange={(event) => setFormData({ ...formData, username: event.target.value })} placeholder="输入用户名" />
                </label>
                <label style={{ display: "flex", alignItems: "center" }}>
                  <div style={{ width: 90, flexShrink: 0 }}>访问凭证</div><Input type="password" value={formData.token} onChange={(event) => setFormData({ ...formData, token: event.target.value })} placeholder="输入访问凭证" />
                </label>
                <label style={{ display: "flex", alignItems: "center" }}>
                  <div style={{ width: 90, flexShrink: 0 }}>分支/Tag</div><Input value={formData.branch} onChange={(event) => setFormData({ ...formData, branch: event.target.value })} placeholder="输入分支/Tag" />
                </label>
                <div>
                  <label style={{ display: "flex", alignItems: "center" }}>
                    <div style={{ width: 90, flexShrink: 0 }}>凭据名称</div><Input value={formData.credentialName} onChange={(event) => setFormData({ ...formData, credentialName: event.target.value })} placeholder="输入凭据名称" />
                  </label>
                  <p style={{ color: "gray", fontSize: 12, paddingLeft: 90, marginTop: 4 }}>注意：凭据仅提交给后端凭据服务，保存后不再回显。</p>
                </div>
                <Space style={{ marginTop: 12 }}>
                  <Button>连接验证</Button>
                </Space>
              </Space>
            </section>

            <section className="evaluation-panel">
              <div className="block-section">
                <h3 style={{ marginBottom: 12 }}>连接验证</h3>
                <div className="success-tip">
                  <span className="success-text"><CheckCircleOutlined style={{ marginRight: 8 }} />仓库连接成功</span>
                </div>
                <div className="info-grid">
                  <div className="info-row">
                    <div className="info-label">默认分支</div>
                    <span className="info-value">main</span>
                  </div>
                  <div className="info-row">
                    <div className="info-label">锁定 Commit</div>
                    <span className="info-value">3f82a1c</span>
                  </div>
                  <div className="info-row">
                    <div className="info-label">提交时间</div>
                    <span className="info-value">2026-08-24 10:48:32</span>
                  </div>
                  <div className="info-row">
                    <div className="info-label">仓库大小</div>
                    <span className="info-value">18.6 MB</span>
                  </div>
                  <div className="info-row">
                    <div className="info-label">文件数量</div>
                    <span className="info-value">426</span>
                  </div>
                </div>
              </div>

              <div className="block-section">
                <h3 style={{ marginBottom: 12 }}>检测到的部署入口</h3>
                <Radio.Group
                  onChange={onChange}
                  value={formData.deployEntry}
                  className="entry-radio-group"
                >
                  <div className="radio-list">
                    {entryList.map((item) => (
                      <div
                        key={item.key}
                        className={`radio-item ${formData.deployEntry === item.key ? 'radio-item--active' : ''}`}
                      >
                        <div>
                          <Radio value={item.key} />
                          <p className="entry-name"><div>{item.label}</div> <span className="entry-path">{item.path}</span></p>
                        </div>
                        {item.isRecommend && (
                          <Tag color="blue">推荐</Tag>
                        )}
                      </div>
                    ))}
                  </div>
                </Radio.Group>
              </div>

              <div className="bottom-note">ⓘ 已锁定源码版本，下一步将解析所选入口并生成资源清单。</div>
            </section>

          </div>
        )
      }

      {
        step === 1 && (
          <div className="evaluation-two-column" style={{ gridTemplateColumns: "1fr 2fr" }}>
            <section className="evaluation-panel">
              <div className="block-section">
                <h3 style={{ marginBottom: 12 }}>部署入口</h3>
                <Radio.Group
                  onChange={onChange}
                  value={formData.deployEntry}
                  className="entry-radio-group"
                >
                  <div className="radio-list">
                    {entryList.map((item) => (
                      <div
                        key={item.key}
                        className={`radio-item ${formData.deployEntry === item.key ? 'radio-item--active' : ''}`}
                      >
                        <div>
                          <Radio value={item.key} />
                          <p className="entry-name"><div>{item.label}</div> <span className="entry-path">{item.path}</span></p>
                        </div>
                        {item.isRecommend && (
                          <Tag color="blue">推荐</Tag>
                        )}
                      </div>
                    ))}
                  </div>
                </Radio.Group>
              </div>

              <div className="block-section">
                <div className="info-grid">
                  <p>helm信息</p>
                  <div className="info-row">
                    <div className="info-label">Chart 名称</div>
                    <span className="info-value">order-service</span>
                  </div>
                  <div className="info-row">
                    <div className="info-label">Chart 版本</div>
                    <span className="info-value">0.6.2</span>
                  </div>
                  <div className="info-row">
                    <div className="info-label">应用版本</div>
                    <span className="info-value">0.4.1</span>
                  </div>
                  <div className="info-row">
                    <div className="info-label">模板数量</div>
                    <span className="info-value">10</span>
                  </div>
                  <div className="info-row">
                    <div className="info-label">默认 Values</div>
                    <span className="info-value">values.yaml</span>
                  </div>
                </div>
              </div>

              <Button>重新解析</Button>
            </section>

            <section className="evaluation-panel">
              <h3>解析结果</h3>

              {/* 头部解析通过提示 */}
              <div className="header-status-bar">
                <div className="success-line">
                  <CheckCircleOutlined className="success-icon" />
                  <span className="success-text">解析通过</span>
                </div>
                <span className="desc-text">已生成 24 个 Kubernetes 资源</span>
              </div>

              {/* 资源统计行（网格卡片） */}
              <div className="resource-stat-card">
                {resourceList.map((item, idx) => (
                  <div className="stat-item" key={idx}>
                    <div className="stat-label">{item.label}</div>
                    <div className="stat-num">{item.count}</div>
                  </div>
                ))}
              </div>

              {/* 检测到的服务与镜像 */}
              <h3 style={{ marginBottom: 12 }}>检测到的服务与镜像</h3>
              <Table
                dataSource={serviceData}
                columns={tableColumns}
                pagination={false}
                bordered
                className="service-table"
              />

              {/* 底部待环境确认提示框 */}
              <div className="warn-notice">
                <div className="warn-header">
                  <InfoCircleOutlined className="warn-icon" />
                  <span className="warn-title">待环境确认</span>
                  <span className="warn-badge">2</span>
                </div>
                <div className="warn-list">
                  <div className="warn-row">StorageClass 未指定，将在下一步选择</div>
                  <div className="warn-row">IngressClass 未指定，将在下一步选择</div>
                </div>
              </div>
            </section>
          </div>
        )
      }

      {
        step === 2 && (
          <div className="evaluation-two-column" style={{ gridTemplateColumns: "1fr 1fr" }}>
            <section className="evaluation-panel">
              <h3 style={{ marginBottom: 12 }}>部署目标</h3>

              <div className="field-item" style={{ display: 'flex', alignItems: 'center' }}>
                <div className="field-label" style={{ width: 100 }}>实验环境</div>
                <Select
                  value={formData.cluster}
                  onChange={(val) => setFormData({ ...formData, cluster: val })}
                  options={[{ label: '研发测试集群', value: '研发测试集群' }]}
                  style={{ width: '100%' }}
                  placeholder="请选择实验环境"
                />
              </div>

              {/* API Server 信息条 */}
              <div className="api-server-bar">
                <div><span>API Server</span> https://10.0.0.12:6443</div>
                <div><span>Kubernetes</span> v1.29.3</div>
                <Tag color="green" style={{ marginLeft: 8 }}>连接正常</Tag>
              </div>

              <div className="field-item" style={{ display: 'flex' }}>
                <div className="field-label" style={{ width: 100 }}>Namespace</div>
                <Radio.Group
                  value={formData.nsMode}
                  onChange={(e) => setFormData({ ...formData, nsMode: e.target.value })}
                >
                  <Radio value="new">新建 Namespace</Radio>
                  <Radio value="exist">使用已有 Namespace</Radio>
                </Radio.Group>
              </div>

              <div className="field-item">
                <div className="field-label">Namespace 名称</div>
                <Input
                  value={formData.namespace}
                  onChange={(e) => setFormData({ ...formData, namespace: e.target.value })}
                  placeholder="请输入 Namespace 名称"
                // suffix={<span className="tag-ok">名称可用</span>}
                />
              </div>

              <div className="two-input-row">
                <div className="field-item">
                  <div className="field-label">Release 名称</div>
                  <Input
                    value={formData.releaseName}
                    placeholder="请输入 Release 名称"
                    onChange={(e) => setFormData({ ...formData, releaseName: e.target.value })}
                  />
                </div>
                <div className="field-item">
                  <div className="field-label">StorageClass</div>
                  <Select
                    value={formData.storageClass}
                    placeholder="请选择 StorageClass"
                    onChange={(val) => setFormData({ ...formData, storageClass: val })}
                    options={[{ label: 'standard-rwo', value: 'standard-rwo' }]}
                    style={{ width: '100%' }}
                  />
                </div>
              </div>

              <div className="two-input-row">
                <div className="field-item">
                  <div className="field-label">IngressClass</div>
                  <Select
                    value={formData.ingressClass}
                    placeholder="请选择 IngressClass"
                    onChange={(val) => setFormData({ ...formData, ingressClass: val })}
                    options={[{ label: 'nginx', value: 'nginx' }]}
                    style={{ width: '100%' }}
                  />
                </div>
                <div>
                  <div className="field-item">
                    <div className="field-label">镜像拉取凭据</div>
                    <Select
                      value={formData.imageSecret}
                      placeholder="请选择镜像拉取凭据"
                      onChange={(val) => setFormData({ ...formData, imageSecret: val })}
                      options={[{ label: 'harbor-readonly', value: 'harbor-readonly' }]}
                      style={{ width: '100%' }}
                    />
                  </div>
                  <div className="secret-tip">仅保存凭据引用，不读取或展示 Secret 内容。</div>
                </div>

              </div>

              {/* 预计资源请求 */}
              <div className="resource-section">
                <h3 className="sub-title">预计资源请求</h3>
                <div className="resource-stat-row">
                  <div className="stat-item">
                    <div className="stat-label">CPU</div>
                    <div className="stat-val">4.5 Core</div>
                  </div>
                  <div className="stat-item">
                    <div className="stat-label">内存</div>
                    <div className="stat-val">8 GiB</div>
                  </div>
                  <div className="stat-item">
                    <div className="stat-label">PVC</div>
                    <div className="stat-val">20 GiB</div>
                  </div>
                  <div className="stat-item">
                    <div className="stat-label">Pod</div>
                    <div className="stat-val">9</div>
                  </div>
                </div>
              </div>

            </section>

            <section className="evaluation-panel">
              <h3 style={{ marginBottom: 16 }}>环境预检</h3>

              <div className="success-tip" style={{ marginBottom: 6, paddingLeft: 12 }}>
                <span className="success-text">预检通过 6 / 6</span>
              </div>

              <div className="check-list">
                {preCheckList.map((item, idx) => (
                  <div className="check-item" key={idx}>
                    <CheckCircleOutlined className="ok-icon" />
                    <span>{item}</span>
                  </div>
                ))}
              </div>

              {/* 容量校验 */}
              <div className="capacity-card">
                <h3 className="sub-title">容量校验</h3>
                {capacityData.map((cap, i) => {
                  const percent = (cap.used / cap.total) * 100;
                  return (
                    <div className="cap-row" key={i}>
                      <span className="cap-label">{cap.label}</span>
                      <Progress
                        percent={percent}
                        showInfo={false}
                        strokeColor="#27ae60"
                        trailColor="#e8e8e8"
                        className="cap-progress"
                      />
                      <span className="cap-num">{cap.used} / {cap.total} {cap.unit}</span>
                    </div>
                  );
                })}
              </div>

              <div className="pre-time-tip">
                预检时间 2026-08-24 10:52:46 · 数据来源: Kubernetes API
              </div>

            </section>
          </div>
        )
      }

      {
        step === 3 && (
          <div className="evaluation-two-column" style={{ gridTemplateColumns: "2fr 1fr" }}>
            <section className="evaluation-panel">
              <h3 style={{ marginBottom: 12 }}>资源预览</h3>

              <Tabs
                activeKey={activeTab}
                onChange={setActiveTab}
                items={[
                  { key: 'list', label: '资源清单' },
                  { key: 'helm', label: 'Helm 参数' },
                  { key: 'yaml', label: '渲染 YAML' }
                ]}
              />

              <div className="filter-bar">
                <div className="filter-left">
                  <Select
                    value={resourceType}
                    onChange={setResourceType}
                    options={[{ label: '全部类型', value: 'all' }]}
                    style={{ width: 140 }}
                  />
                  <Input
                    value={searchText}
                    onChange={(e) => setSearchText(e.target.value)}
                    placeholder="搜索资源名称"
                    prefix={<SearchOutlined style={{ color: '#999' }} />}
                    style={{ width: 280 }}
                  />
                </div>
                <Button type="link" icon={<DownloadOutlined />}>
                  下载清单
                </Button>
              </div>

              <div className="success-tip">
                <span className="success-text"><CheckCircleOutlined style={{ marginRight: 8 }} />24 / 24 个资源校验通过</span>
              </div>

              {/* antd Table 组件 */}
              <Table
                columns={columns}
                dataSource={tableData}
                pagination={false}
                size="middle"
              />

              <div className="table-footer">
                <InfoCircleOutlined className="footer-icon" />
                <span>配置来源：Commit 3f82a1c 渲染结果；Secret 仅展示引用，不展示内容。</span>
              </div>

            </section>

            <section className="evaluation-panel">
              <h3 style={{ marginBottom: 12 }}>部署摘要</h3>

              <div className="summary-list">
                <div className="summary-row">
                  <span className="summary-label">目标集群</span>
                  <span className="summary-value">研发测试集群</span>
                </div>
                <div className="summary-row">
                  <span className="summary-label">Namespace</span>
                  <span className="summary-value">order‑service</span>
                </div>
                <div className="summary-row">
                  <span className="summary-label">Release</span>
                  <span className="summary-value">order‑service</span>
                </div>
                <div className="summary-row">
                  <span className="summary-label">资源数量</span>
                  <span className="summary-value">24</span>
                </div>
                <div className="summary-row">
                  <span className="summary-label">服务数量</span>
                  <span className="summary-value">6</span>
                </div>
                <div className="summary-row">
                  <span className="summary-label">镜像数量</span>
                  <span className="summary-value">6</span>
                </div>
              </div>

              <div className="post-check-section">
                <h3 className="sub-title">部署后验证</h3>
                <div className="check-item">
                  <CheckCircleOutlined className="check-icon" />
                  <span>等待 Workload Ready</span>
                </div>
                <div className="check-item">
                  <CheckCircleOutlined className="check-icon" />
                  <span>检查 Service Endpoint</span>
                </div>
                <div className="check-item">
                  <CheckCircleOutlined className="check-icon" />
                  <span>执行业务健康检查</span>
                </div>
              </div>

              <div className="deploy-ready-card">
                <div className="ready-header">
                  <CheckCircleOutlined className="ready-icon" />
                  <span className="ready-text">可以部署</span>
                </div>
                <div className="ready-desc">资源、环境与镜像预检均已通过</div>
              </div>

              <div className="bottom-note">ⓘ 开始后将创建 24 个 Kubernetes 资源，并进入部署任务页面。</div>
            </section>
          </div >
        )
      }


      <section className="evaluation-panel" style={{ display: "flex", justifyContent: "space-between" }}>
        <Button onClick={() => navigate("/environments/applications")}>取消</Button>
        <Space>
          <Button disabled={step === 0} onClick={() => setStep((current) => current - 1)}>上一步</Button>
          <Button>保存草稿</Button>
          {step < 3 ?
            <Button type="primary" onClick={() => setStep((current) => current + 1)}>下一步：{STEP_ITEMS[step + 1]?.title}</Button>
            :
            <Button type="primary">创建系统</Button>}
        </Space>
      </section>
    </div >
  );
}