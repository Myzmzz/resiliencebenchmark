import { useState, useEffect } from "react";
import { Table, Button, Tag, Alert, Spin, Typography, Card, Space, Descriptions } from "antd";
import type { ColumnsType } from "antd/es/table";
import { ReloadOutlined, LineChartOutlined } from "@ant-design/icons";
import { fetchApplications } from "../services/api";
import type { SLO } from "../types/application";

const { Title, Text } = Typography;

interface WorkloadRow {
  key: string;
  application: string;
  displayName: string;
  criticalPathsCount: number;
  sloCount: number;
  slos: SLO[];
  imagePolicy: string;
  status: string;
}

export default function WorkloadsPage() {
  const [workloads, setWorkloads] = useState<WorkloadRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const loadWorkloads = async () => {
    setLoading(true);
    setError(null);
    try {
      const apps = await fetchApplications();

      // Transform applications into workload rows
      const workloadData: WorkloadRow[] = apps.map((app) => ({
        key: app.name,
        application: app.name,
        displayName: app.displayName,
        criticalPathsCount: app.criticalPathsCount,
        sloCount: app.sloCount,
        slos: app.details?.slos || [],
        imagePolicy: app.imagePolicy,
        status: app.status,
      }));

      setWorkloads(workloadData);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to fetch workloads");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadWorkloads();
  }, []);

  const columns: ColumnsType<WorkloadRow> = [
    {
      title: "应用",
      dataIndex: "displayName",
      key: "displayName",
      width: 200,
    },
    {
      title: "关键路径",
      dataIndex: "criticalPathsCount",
      key: "criticalPathsCount",
      width: 120,
      render: (count: number) => (
        <Space>
          <LineChartOutlined />
          <Text>{count} 条</Text>
        </Space>
      ),
    },
    {
      title: "SLO 数量",
      dataIndex: "sloCount",
      key: "sloCount",
      width: 120,
      render: (count: number) => <Text strong>{count} 个</Text>,
    },
    {
      title: "SLO 详情",
      dataIndex: "slos",
      key: "slos",
      render: (slos: SLO[]) => {
        if (slos.length === 0) {
          return <Text type="secondary">无详细信息</Text>;
        }
        return (
          <Space direction="vertical" size="small" style={{ width: "100%" }}>
            {slos.slice(0, 2).map((slo) => (
              <div key={slo.id} style={{ fontSize: 12 }}>
                <Tag color="blue">{slo.id}</Tag>
                <Text type="secondary">{slo.objective} / {slo.window}</Text>
              </div>
            ))}
            {slos.length > 2 && (
              <Text type="secondary" style={{ fontSize: 12 }}>
                +{slos.length - 2} 更多...
              </Text>
            )}
          </Space>
        );
      },
    },
    {
      title: "镜像策略",
      dataIndex: "imagePolicy",
      key: "imagePolicy",
      width: 150,
      render: (policy: string) => (
        <Tag color={policy === "digest-required" ? "green" : "orange"}>
          {policy}
        </Tag>
      ),
    },
    {
      title: "状态",
      dataIndex: "status",
      key: "status",
      width: 120,
      render: (status: string) => {
        const statusMap: Record<string, { color: string; text: string }> = {
          qualified: { color: "success", text: "合格" },
          partial: { color: "warning", text: "部分就绪" },
          pending: { color: "default", text: "待处理" },
          inactive: { color: "default", text: "未激活" },
          error: { color: "error", text: "错误" },
        };
        const config = statusMap[status] || statusMap.pending;
        return <Tag color={config.color}>{config.text}</Tag>;
      },
    },
  ];

  const expandedRowRender = (record: WorkloadRow) => {
    if (record.slos.length === 0) {
      return (
        <Alert
          message="无 SLO 配置"
          description="该应用暂无详细的 SLO 配置信息"
          type="info"
          showIcon
          style={{ margin: "8px 0" }}
        />
      );
    }

    return (
      <div style={{ padding: "8px 0" }}>
        <Title level={5} style={{ marginBottom: 16 }}>SLO 配置详情</Title>
        <Space direction="vertical" size="middle" style={{ width: "100%" }}>
          {record.slos.map((slo) => (
            <Card key={slo.id} size="small" style={{ backgroundColor: "#fafafa" }}>
              <Descriptions column={2} size="small">
                <Descriptions.Item label="SLO ID" span={2}>
                  <Tag color="blue">{slo.id}</Tag>
                </Descriptions.Item>
                <Descriptions.Item label="目标">{slo.objective}</Descriptions.Item>
                <Descriptions.Item label="时间窗口">{slo.window}</Descriptions.Item>
                <Descriptions.Item label="查询引用" span={2}>
                  <code style={{ fontSize: 12 }}>{slo.queryRef}</code>
                </Descriptions.Item>
              </Descriptions>
            </Card>
          ))}
        </Space>
      </div>
    );
  };

  if (error) {
    return (
      <div style={{ padding: 24 }}>
        <Alert message="加载失败" description={error} type="error" showIcon />
        <Button icon={<ReloadOutlined />} onClick={loadWorkloads} style={{ marginTop: 16 }}>
          重试
        </Button>
      </div>
    );
  }

  // Calculate statistics
  const totalCriticalPaths = workloads.reduce((sum, w) => sum + w.criticalPathsCount, 0);
  const totalSLOs = workloads.reduce((sum, w) => sum + w.sloCount, 0);
  const qualifiedApps = workloads.filter((w) => w.status === "qualified").length;

  return (
    <div style={{ padding: 24 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 16 }}>
        <Title level={3} style={{ margin: 0 }}>负载与 SLO</Title>
        <Button icon={<ReloadOutlined />} onClick={loadWorkloads} loading={loading}>
          刷新
        </Button>
      </div>

      {loading ? (
        <div style={{ textAlign: "center", padding: 48 }}>
          <Spin size="large" />
        </div>
      ) : workloads.length === 0 ? (
        <Alert
          message="暂无负载配置"
          description="未找到任何应用的负载和 SLO 配置"
          type="info"
          showIcon
        />
      ) : (
        <>
          <Card size="small" style={{ marginBottom: 16 }}>
            <Descriptions column={4} size="small">
              <Descriptions.Item label="应用总数">{workloads.length} 个</Descriptions.Item>
              <Descriptions.Item label="关键路径总数">{totalCriticalPaths} 条</Descriptions.Item>
              <Descriptions.Item label="SLO 总数">{totalSLOs} 个</Descriptions.Item>
              <Descriptions.Item label="合格应用">{qualifiedApps} 个</Descriptions.Item>
            </Descriptions>
          </Card>

          <Table
            columns={columns}
            dataSource={workloads}
            expandable={{
              expandedRowRender,
              rowExpandable: (record) => record.slos.length > 0,
            }}
            pagination={{ pageSize: 10 }}
          />
        </>
      )}
    </div>
  );
}
