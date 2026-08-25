import { useEffect, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Col,
  Input,
  Row,
  Select,
  Space,
  Spin,
  Statistic,
  Table,
  Tabs,
  Tag,
  Typography,
} from "antd";
import type { TableColumnsType } from "antd";
import { ReloadOutlined } from "@ant-design/icons";
import { fetchExperimentEnvironment } from "../api/experiments";
import type {
  ExperimentEnvironment,
  PodInfo,
  ComponentStatus,
  PodPhase,
  ComponentHealth,
} from "../types/experiment";

const { Title, Text } = Typography;

// 状态标签颜色映射
const phaseColorMap: Record<PodPhase, string> = {
  Running: "green",
  Pending: "orange",
  Succeeded: "blue",
  Failed: "red",
  Unknown: "default",
};

const healthColorMap: Record<ComponentHealth, string> = {
  运行正常: "green",
  需要关注: "orange",
  异常: "red",
};

export default function ExperimentPage() {
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [data, setData] = useState<ExperimentEnvironment | null>(null);

  // 筛选状态
  const [selectedNamespace, setSelectedNamespace] = useState<string>("全部 Namespace");
  const [selectedStatus, setSelectedStatus] = useState<string>("全部状态");
  const [searchText, setSearchText] = useState("");
  const [groupBy, setGroupBy] = useState<"node" | "namespace">("node");

  const loadData = async (isRefresh = false) => {
    try {
      if (isRefresh) {
        setRefreshing(true);
      } else {
        setLoading(true);
      }
      setError(null);
      const result = await fetchExperimentEnvironment();
      setData(result);
    } catch (err) {
      setError(err instanceof Error ? err.message : "加载失败");
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  };

  useEffect(() => {
    void loadData();
  }, []);

  if (loading) {
    return (
      <div style={{ padding: 24, textAlign: "center" }}>
        <Spin size="large" />
      </div>
    );
  }

  if (error) {
    return (
      <div style={{ padding: 24 }}>
        <Alert
          type="error"
          message="加载失败"
          description={error}
          showIcon
          action={
            <Button size="small" onClick={() => loadData()}>
              重试
            </Button>
          }
        />
      </div>
    );
  }

  if (!data) {
    return null;
  }

  // 提取所有 namespace 用于筛选
  const namespaces = Array.from(new Set(data.pods.map((p) => p.namespace)));
  const namespaceOptions = [
    { label: "全部 Namespace", value: "全部 Namespace" },
    ...namespaces.map((ns) => ({ label: ns, value: ns })),
  ];

  const statusOptions = [
    { label: "全部状态", value: "全部状态" },
    { label: "Running", value: "Running" },
    { label: "Pending", value: "Pending" },
    { label: "Failed", value: "Failed" },
  ];

  // 筛选 Pods
  const filteredPods = data.pods.filter((pod) => {
    if (selectedNamespace !== "全部 Namespace" && pod.namespace !== selectedNamespace) {
      return false;
    }
    if (selectedStatus !== "全部状态" && pod.phase !== selectedStatus) {
      return false;
    }
    if (searchText && !pod.name.toLowerCase().includes(searchText.toLowerCase())) {
      return false;
    }
    return true;
  });

  // Pod 表格列定义
  const podColumns: TableColumnsType<PodInfo> = [
    {
      title: "Pod 名称",
      dataIndex: "name",
      key: "name",
      width: 280,
      ellipsis: true,
    },
    {
      title: "Namespace",
      dataIndex: "namespace",
      key: "namespace",
      width: 140,
    },
    {
      title: "所在节点",
      dataIndex: "node",
      key: "node",
      width: 120,
    },
    {
      title: "状态",
      dataIndex: "phase",
      key: "phase",
      width: 100,
      render: (phase: PodPhase) => (
        <Tag color={phaseColorMap[phase]}>{phase}</Tag>
      ),
    },
    {
      title: "数据次数",
      dataIndex: "ready",
      key: "ready",
      width: 100,
    },
    {
      title: "重启次数",
      dataIndex: "restarts",
      key: "restarts",
      width: 100,
    },
    {
      title: "Pod IP",
      dataIndex: "ip",
      key: "ip",
      width: 140,
      render: (ip: string | null) => ip || "-",
    },
    {
      title: "创建时间",
      dataIndex: "created_at",
      key: "created_at",
      width: 180,
      render: (created: string) => new Date(created).toLocaleString("zh-CN"),
    },
  ];

  // 组件状态表格列定义
  const componentColumns: TableColumnsType<ComponentStatus> = [
    {
      title: "组件",
      dataIndex: "name",
      key: "name",
    },
    {
      title: "版本",
      dataIndex: "version",
      key: "version",
    },
    {
      title: "运行实例",
      dataIndex: "instances",
      key: "instances",
    },
    {
      title: "健康状态",
      dataIndex: "health",
      key: "health",
      render: (health: ComponentHealth) => (
        <Tag color={healthColorMap[health]}>{health}</Tag>
      ),
    },
  ];

  // 节点分组
  const nodeGroups: Record<string, PodInfo[]> = {};
  filteredPods.forEach((pod) => {
    if (!nodeGroups[pod.node]) {
      nodeGroups[pod.node] = [];
    }
    nodeGroups[pod.node].push(pod);
  });

  // Namespace 分组
  const namespaceGroups: Record<string, PodInfo[]> = {};
  filteredPods.forEach((pod) => {
    if (!namespaceGroups[pod.namespace]) {
      namespaceGroups[pod.namespace] = [];
    }
    namespaceGroups[pod.namespace].push(pod);
  });

  const groups = groupBy === "node" ? nodeGroups : namespaceGroups;

  return (
    <div style={{ padding: 24, background: "#0A0D12", minHeight: "100vh" }}>
      {/* 页面标题和操作栏 */}
      <div style={{
        display: "flex",
        justifyContent: "space-between",
        alignItems: "center",
        marginBottom: 24
      }}>
        <div style={{ display: "flex", alignItems: "center", gap: 16 }}>
          <Title level={3} style={{ margin: 0, color: "#F8FAFC" }}>
            实验环境
          </Title>
          <Tag color="blue">M3</Tag>
        </div>
        <Space>
          <Select
            value="研发测试集群"
            style={{ width: 160 }}
            options={[{ label: "研发测试集群", value: "研发测试集群" }]}
          />
          <Button icon={<ReloadOutlined spin={refreshing} />} onClick={() => loadData(true)}>
            刷新数据
          </Button>
        </Space>
      </div>

      {/* 顶部状态栏 */}
      <Card
        style={{ marginBottom: 16, background: "#0F131C", borderColor: "#1E2636" }}
        bodyStyle={{ padding: "12px 24px" }}
      >
        <Row gutter={24} align="middle">
          <Col>
            <Text style={{ color: "#94A3B8" }}>API Server</Text>{" "}
            <Text strong style={{ color: "#F8FAFC" }}>{data.api_server}</Text>
          </Col>
          <Col>
            <Text style={{ color: "#94A3B8" }}>Kubernetes</Text>{" "}
            <Text strong style={{ color: "#F8FAFC" }}>{data.k8s_version}</Text>
          </Col>
          <Col>
            <Text style={{ color: "#94A3B8" }}>最近同步</Text>{" "}
            <Text strong style={{ color: "#F8FAFC" }}>
              {new Date(data.last_sync).toLocaleTimeString("zh-CN")}
            </Text>
          </Col>
          <Col>
            <Tag color={data.connection_status === "连接正常" ? "green" : "red"}>
              {data.connection_status}
            </Tag>
          </Col>
        </Row>
      </Card>

      {/* 统计卡片 */}
      <Row gutter={16} style={{ marginBottom: 24 }}>
        <Col span={6}>
          <Card style={{ background: "#0F131C", borderColor: "#1E2636" }}>
            <Statistic
              title={<span style={{ color: "#94A3B8" }}>节点</span>}
              value={data.summary.node_count}
              valueStyle={{ color: "#F8FAFC" }}
            />
          </Card>
        </Col>
        <Col span={6}>
          <Card style={{ background: "#0F131C", borderColor: "#1E2636" }}>
            <Statistic
              title={<span style={{ color: "#94A3B8" }}>Namespace</span>}
              value={data.summary.namespace_count}
              valueStyle={{ color: "#F8FAFC" }}
            />
          </Card>
        </Col>
        <Col span={6}>
          <Card style={{ background: "#0F131C", borderColor: "#1E2636" }}>
            <Statistic
              title={<span style={{ color: "#94A3B8" }}>Pod</span>}
              value={data.summary.pod_count}
              valueStyle={{ color: "#F8FAFC" }}
            />
          </Card>
        </Col>
        <Col span={6}>
          <Card style={{ background: "#0F131C", borderColor: "#1E2636" }}>
            <Statistic
              title={<span style={{ color: "#94A3B8" }}>异常 Pod</span>}
              value={data.summary.abnormal_pod_count}
              valueStyle={{ color: data.summary.abnormal_pod_count > 0 ? "#F87171" : "#F8FAFC" }}
            />
          </Card>
        </Col>
      </Row>

      {/* 资源清单 */}
      <Card
        title={<span style={{ color: "#F8FAFC" }}>资源清单</span>}
        style={{ marginBottom: 24, background: "#0F131C", borderColor: "#1E2636" }}
        extra={
          <Space>
            <Select
              value={selectedNamespace}
              onChange={setSelectedNamespace}
              style={{ width: 200 }}
              options={namespaceOptions}
            />
            <Select
              value={selectedStatus}
              onChange={setSelectedStatus}
              style={{ width: 140 }}
              options={statusOptions}
            />
            <Input.Search
              placeholder="搜索 Pod 名称"
              style={{ width: 240 }}
              value={searchText}
              onChange={(e) => setSearchText(e.target.value)}
              allowClear
            />
          </Space>
        }
      >
        <Row gutter={16}>
          {/* 左侧：按节点/命名空间分组 */}
          <Col span={6}>
            <Tabs
              activeKey={groupBy}
              onChange={(key) => setGroupBy(key as "node" | "namespace")}
              items={[
                { key: "node", label: "按节点" },
                { key: "namespace", label: "按命名空间" },
              ]}
            />
            <div style={{ marginTop: 16 }}>
              {Object.entries(groups).map(([groupName, pods]) => (
                <Card
                  key={groupName}
                  size="small"
                  style={{
                    marginBottom: 8,
                    background: "#161D2B",
                    borderColor: "#1E2636"
                  }}
                >
                  <div style={{ display: "flex", justifyContent: "space-between" }}>
                    <Text strong style={{ color: "#F8FAFC" }}>{groupName}</Text>
                    <Tag color="blue">{pods.length} Pods</Tag>
                  </div>
                  {groupBy === "node" && data.nodes.find((n) => n.name === groupName) && (
                    <div style={{ marginTop: 8 }}>
                      <Tag color="green" style={{ fontSize: 12 }}>
                        {data.nodes.find((n) => n.name === groupName)?.status}
                      </Tag>
                    </div>
                  )}
                </Card>
              ))}
            </div>
          </Col>

          {/* 右侧：Pod 详情表格 */}
          <Col span={18}>
            <Table
              columns={podColumns}
              dataSource={filteredPods}
              rowKey="name"
              pagination={{ pageSize: 10, showSizeChanger: true }}
              scroll={{ x: 1200 }}
              size="small"
              style={{
                background: "#161D2B",
              }}
            />
          </Col>
        </Row>
      </Card>

      {/* 资源能力检查 */}
      <Card
        title={<span style={{ color: "#F8FAFC" }}>资源能力检查</span>}
        style={{ background: "#0F131C", borderColor: "#1E2636" }}
      >
        <Table
          columns={componentColumns}
          dataSource={data.components}
          rowKey="name"
          pagination={false}
          size="small"
          style={{ background: "#161D2B" }}
        />
        <div style={{ marginTop: 16, color: "#94A3B8", fontSize: 12 }}>
          数据来源：Kubernetes API · 采集时间{" "}
          {new Date(data.last_sync).toLocaleTimeString("zh-CN")}
        </div>
      </Card>
    </div>
  );
}
