import { useState, useEffect } from "react";
import { Table, Badge, Button, Drawer, Tag, Alert, Spin, Descriptions, List, Typography, Space, Input, Select,Progress } from "antd";
import type { ColumnsType } from "antd/es/table";
import { ReloadOutlined, PlusOutlined, InfoCircleTwoTone } from "@ant-design/icons";
import { fetchApplications } from "../services/api";
import type { Application, ReadinessGap } from "../types/application";
import { useNavigate } from "react-router-dom";
import MetricCard from "../features/evaluation/components/MetricCard";

const { Title, Text } = Typography;

export default function ApplicationsPage() {
  const navigate = useNavigate();
  const [applications, setApplications] = useState<Application[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [selectedApp, setSelectedApp] = useState<Application | null>(null);

  const loadApplications = async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await fetchApplications();
      setApplications(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to fetch applications");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadApplications();
  }, []);

  const handleRowClick = (app: Application) => {
    setSelectedApp(app);
    setDrawerOpen(true);
  };

  const getStatusBadge = (status: Application["status"]) => {
    const statusMap = {
      qualified: { status: "success" as const, text: "合格" },
      partial: { status: "warning" as const, text: "部分就绪" },
      pending: { status: "default" as const, text: "待处理" },
      inactive: { status: "default" as const, text: "未激活" },
      error: { status: "error" as const, text: "错误" },
    };
    const config = statusMap[status] || statusMap.pending;
    return <Badge status={config.status} text={config.text} />;
  };

  const columns: ColumnsType<Application> = [
    {
      title: "应用名称",
      dataIndex: "displayName",
      key: "displayName",
      width: 140,
      render: (text: string, record: Application) => (
        <span>{text}</span>
        // <a onClick={() => handleRowClick(record)}>{text}</a>
      ),
    },
    {
      title: "源码仓库",
      key: "repository",
      width: 260,
      render: (text: string, record: Application) => (
        <span>github.com/pen-telemetry/opentelemetry-demo</span>
      ),
    },
    {
      title: "环境/Namespace",
      key: "namespace",
      width: 200,
      render: (text: string, record: Application) => (
        <div>
          <span>研发测试集群</span>
          <span>{record.namespace.liveReference}</span>
        </div>
      ),
    },
    {
      title: "版本/Commit",
      key: "namespace",
      width: 100,
      render: (text: string, record: Application) => (
        <div>
          <p>v1.0.0</p>
          <span style={{fontSize: 12, color: 'gray'}}>8a4f9c2</span>
        </div>
      ),
    },
    {
      title: "服务",
      key: "namespace",
      width: 100,
      render: (text: string, record: Application) => (
        <div>
          <span>22</span>
        </div>
      ),
    },
    {
      title: "部署状态",
      key: "namespace",
      width: 200,
      render: (_, item) => <div>
        <Tag color="green">部署中</Tag>
        <Progress size="small" percent={20} showInfo={false} />
        <p style={{fontSize: 12, color: 'gray'}}>等待 Workload 就绪</p>
      </div>,
    },
    {
      title: "最近操作",
      key: "namespace",
      width: 100,
      render: (_, item) => <div>5分钟前</div>,
    },
    {
      title: "操作",
      key: "actions",
      width: 100,
      render: (_, item) => <Space size="small" wrap>
        <Button type="link" onClick={() => navigate(`/environments/applications/${item.id}`)}>查看详情</Button>
      </Space>,
    },
  ];

  if (error) {
    return (
      <div style={{ padding: 24 }}>
        <Alert message="加载失败" description={error} type="error" showIcon />
        <Button icon={<ReloadOutlined />} onClick={loadApplications} style={{ marginTop: 16 }}>
          重试
        </Button>
      </div>
    );
  }

  return (
    <div style={{ padding: 24 }}>
      <header className="evaluation-page-header">
        <div><h2>被测系统</h2><p>管理源代码接入、部署状态与运行系统</p></div>
        <Space>
          <Button type="primary" icon={<PlusOutlined />} onClick={() => navigate("/environments/applications/new")}>新增系统</Button>
          <Button icon={<ReloadOutlined />} onClick={loadApplications} loading={loading}>刷新</Button>
        </Space>
      </header>

      <div className="evaluation-summary-grid">
        <MetricCard label="全部系统" value={applications.length} />
        <MetricCard label="运行中" value={1} tone="primary" />
        <MetricCard label="部署中" value={2} tone="success" />
        <MetricCard label="异常" value={3} tone="danger" />
      </div>

      <section className="evaluation-panel"  style={{ padding: '14px 0px 0 0' }}>
        <div className="evaluation-panel-header" style={{padding: '0 12px'}}>
          <h3>系统列表</h3>
          <Space wrap>
            <Input.Search allowClear placeholder="搜索系统名称或仓库" />
            <Select allowClear placeholder="全部状态" style={{ width: 170 }} options={[]} />
            <Select allowClear placeholder="全部环境" style={{ width: 170 }} options={[]} />
            <span>最近同步 10:42:35</span>
          </Space>
        </div>

        {loading ? (
          <div style={{ textAlign: "center", padding: 48 }}>
            <Spin size="large" />
          </div>
        ) : applications.length === 0 ? (
          <Alert
            message="暂无被测系统"
            description="未找到任何应用配置文件"
            type="info"
            showIcon
          />
        ) : (
          <Table
            columns={columns}
            dataSource={applications}
            rowKey="name"
            pagination={false}
            scroll={{ x: 1500 }}
          />
        )}
      </section>

      <div className="evaluation-info" style={{ background: '#fff' }}><InfoCircleTwoTone style={{ marginRight: 8 }}/>部署成功后进入系统详情页；失败记录会保留，可查看节点、日志并重试。</div>


      <Drawer
        title={selectedApp?.displayName}
        placement="right"
        width={720}
        onClose={() => setDrawerOpen(false)}
        open={drawerOpen}
      >
        {selectedApp && <ApplicationDetails app={selectedApp} />}
      </Drawer>
    </div>
  );
}

function ApplicationDetails({ app }: { app: Application }) {
  return (
    <div>
      <Descriptions column={1} bordered size="small">
        <Descriptions.Item label="应用名称">{app.displayName}</Descriptions.Item>
        <Descriptions.Item label="内部标识">{app.name}</Descriptions.Item>
        <Descriptions.Item label="角色">{app.benchmarkRole}</Descriptions.Item>
        <Descriptions.Item label="可见性">{app.visibility}</Descriptions.Item>
        <Descriptions.Item label="状态">{app.readinessStatus}</Descriptions.Item>
      </Descriptions>

      <Title level={5} style={{ marginTop: 24 }}>命名空间配置</Title>
      <Descriptions column={1} bordered size="small">
        <Descriptions.Item label="模板">{app.namespace.template}</Descriptions.Item>
        <Descriptions.Item label="生命周期">{app.namespace.lifecycle}</Descriptions.Item>
        {app.namespace.liveReference && (
          <Descriptions.Item label="实时引用">{app.namespace.liveReference}</Descriptions.Item>
        )}
      </Descriptions>

      <Title level={5} style={{ marginTop: 24 }}>资源统计</Title>
      <Descriptions column={2} bordered size="small">
        <Descriptions.Item label="镜像数量">{app.imageCount}</Descriptions.Item>
        <Descriptions.Item label="镜像策略">{app.imagePolicy}</Descriptions.Item>
        <Descriptions.Item label="关键路径">{app.criticalPathsCount} 条</Descriptions.Item>
        <Descriptions.Item label="SLO">{app.sloCount} 个</Descriptions.Item>
      </Descriptions>

      {app.knownGaps.length > 0 && (
        <>
          <Title level={5} style={{ marginTop: 24 }}>已知问题</Title>
          <List
            size="small"
            bordered
            dataSource={app.knownGaps}
            renderItem={(gap: ReadinessGap) => (
              <List.Item>
                <div style={{ width: "100%" }}>
                  <Tag color={gap.severity === "blocking" ? "red" : gap.severity === "informational" ? "blue" : "orange"}>
                    {gap.severity}
                  </Tag>
                  <Text>{gap.item}</Text>
                  {gap.observedAt && (
                    <div style={{ marginTop: 4, fontSize: 12, color: "#666" }}>
                      观察时间: {gap.observedAt}
                    </div>
                  )}
                </div>
              </List.Item>
            )}
          />
        </>
      )}

      {app.details?.readiness?.resolvedIssues && app.details.readiness.resolvedIssues.length > 0 && (
        <>
          <Title level={5} style={{ marginTop: 24 }}>已解决问题</Title>
          <List
            size="small"
            bordered
            dataSource={app.details.readiness.resolvedIssues}
            renderItem={(issue: { resolvedAt: string; item: string }) => (
              <List.Item>
                <div style={{ width: "100%" }}>
                  <Tag color="green">已解决</Tag>
                  <Text>{issue.item}</Text>
                  <div style={{ marginTop: 4, fontSize: 12, color: "#666" }}>
                    解决时间: {issue.resolvedAt}
                  </div>
                </div>
              </List.Item>
            )}
          />
        </>
      )}

      {app.details?.readiness?.nextChecks && app.details.readiness.nextChecks.length > 0 && (
        <>
          <Title level={5} style={{ marginTop: 24 }}>后续检查</Title>
          <List
            size="small"
            bordered
            dataSource={app.details.readiness.nextChecks}
            renderItem={(check: string) => <List.Item>{check}</List.Item>}
          />
        </>
      )}

      {app.details?.slos && app.details.slos.length > 0 && (
        <>
          <Title level={5} style={{ marginTop: 24 }}>SLO 配置</Title>
          <List
            size="small"
            bordered
            dataSource={app.details.slos}
            renderItem={(slo) => (
              <List.Item>
                <div style={{ width: "100%" }}>
                  <Text strong>{slo.id}</Text>
                  <div style={{ marginTop: 4 }}>
                    <Text>目标: {slo.objective}</Text>
                    <span style={{ margin: "0 8px" }}>|</span>
                    <Text>窗口: {slo.window}</Text>
                  </div>
                  <div style={{ marginTop: 4, fontSize: 12, color: "#666" }}>
                    查询: {slo.queryRef}
                  </div>
                </div>
              </List.Item>
            )}
          />
        </>
      )}
    </div>
  );
}
