import { useState, useEffect } from "react";
import { Table, Button, Drawer, Tag, Alert, Spin, Descriptions, List, Typography, Card, Space } from "antd";
import type { ColumnsType } from "antd/es/table";
import { ReloadOutlined, CheckCircleOutlined, ClockCircleOutlined } from "@ant-design/icons";
import { fetchHarnesses } from "../services/api";
import type { HarnessesRegistry, HarnessConfig } from "../types/harness";

const { Title, Paragraph, Text } = Typography;

export default function HarnessesPage() {
  const [registry, setRegistry] = useState<HarnessesRegistry | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [selectedHarness, setSelectedHarness] = useState<HarnessConfig | null>(null);

  const loadHarnesses = async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await fetchHarnesses();
      setRegistry(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to fetch harnesses");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadHarnesses();
  }, []);

  const handleRowClick = (harness: HarnessConfig) => {
    setSelectedHarness(harness);
    setDrawerOpen(true);
  };

  const getStatusBadge = (status: string) => {
    if (status.includes("qualified")) {
      return <Tag icon={<CheckCircleOutlined />} color="success">{status}</Tag>;
    } else if (status.includes("pending") || status.includes("timeout")) {
      return <Tag icon={<ClockCircleOutlined />} color="warning">{status}</Tag>;
    } else {
      return <Tag color="default">{status}</Tag>;
    }
  };

  const columns: ColumnsType<HarnessConfig> = [
    {
      title: "Harness ID",
      dataIndex: "id",
      key: "id",
      width: 180,
      render: (text: string, record: HarnessConfig) => (
        <a onClick={() => handleRowClick(record)}>{text}</a>
      ),
    },
    {
      title: "类型",
      dataIndex: "kind",
      key: "kind",
      width: 200,
    },
    {
      title: "状态",
      dataIndex: "status",
      key: "status",
      width: 350,
      render: (status: string) => getStatusBadge(status),
    },
    {
      title: "入口模式",
      key: "mode",
      width: 150,
      render: (_: any, record: HarnessConfig) => record.entrypoint.mode,
    },
    {
      title: "命令",
      key: "command",
      width: 150,
      render: (_: any, record: HarnessConfig) => (
        <code>{record.entrypoint.command}</code>
      ),
    },
    {
      title: "默认模型",
      key: "default_model",
      width: 150,
      render: (_: any, record: HarnessConfig) =>
        record.models.default_alias ? (
          <Tag color="blue">{record.models.default_alias}</Tag>
        ) : (
          <Text type="secondary">多模型</Text>
        ),
    },
  ];

  if (error) {
    return (
      <div style={{ padding: 24 }}>
        <Alert message="加载失败" description={error} type="error" showIcon />
        <Button icon={<ReloadOutlined />} onClick={loadHarnesses} style={{ marginTop: 16 }}>
          重试
        </Button>
      </div>
    );
  }

  return (
    <div style={{ padding: 24 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 16 }}>
        <Title level={3} style={{ margin: 0 }}>Harness 管理</Title>
        <Button icon={<ReloadOutlined />} onClick={loadHarnesses} loading={loading}>
          刷新
        </Button>
      </div>

      {loading ? (
        <div style={{ textAlign: "center", padding: 48 }}>
          <Spin size="large" />
        </div>
      ) : !registry ? (
        <Alert
          message="未找到 Harness 配置"
          description="未找到 harness/harnesses.yaml 文件"
          type="info"
          showIcon
        />
      ) : (
        <>
          <Card size="small" style={{ marginBottom: 16 }}>
            <Descriptions column={2} size="small">
              <Descriptions.Item label="配置版本">{registry.version}</Descriptions.Item>
              <Descriptions.Item label="Harness 总数">{registry.harnesses.length} 个</Descriptions.Item>
            </Descriptions>
            {registry.description && (
              <Paragraph style={{ marginTop: 8, marginBottom: 0, fontSize: 13, color: "#666" }}>
                {registry.description}
              </Paragraph>
            )}
          </Card>

          <Table
            columns={columns}
            dataSource={registry.harnesses}
            rowKey="id"
            pagination={false}
          />
        </>
      )}

      <Drawer
        title={selectedHarness?.id}
        placement="right"
        width={720}
        onClose={() => setDrawerOpen(false)}
        open={drawerOpen}
      >
        {selectedHarness && <HarnessDetails harness={selectedHarness} />}
      </Drawer>
    </div>
  );
}

function HarnessDetails({ harness }: { harness: HarnessConfig }) {
  return (
    <div>
      <Descriptions column={1} bordered size="small">
        <Descriptions.Item label="Harness ID">{harness.id}</Descriptions.Item>
        <Descriptions.Item label="类型">{harness.kind}</Descriptions.Item>
        <Descriptions.Item label="状态">{harness.status}</Descriptions.Item>
        {harness.qualification_status && (
          <Descriptions.Item label="验证状态">{harness.qualification_status}</Descriptions.Item>
        )}
      </Descriptions>

      <Title level={5} style={{ marginTop: 24 }}>入口点配置</Title>
      <Descriptions column={1} bordered size="small">
        <Descriptions.Item label="模式">{harness.entrypoint.mode}</Descriptions.Item>
        <Descriptions.Item label="命令">
          <code>{harness.entrypoint.command}</code>
        </Descriptions.Item>
        <Descriptions.Item label="提示传输">{harness.entrypoint.prompt_transport}</Descriptions.Item>
        {harness.entrypoint.args.length > 0 && (
          <Descriptions.Item label="参数">
            <Space direction="vertical" size="small">
              {harness.entrypoint.args.map((arg, idx) => (
                <code key={idx}>{arg}</code>
              ))}
            </Space>
          </Descriptions.Item>
        )}
      </Descriptions>

      <Title level={5} style={{ marginTop: 24 }}>模型配置</Title>
      <Descriptions column={1} bordered size="small">
        <Descriptions.Item label="模型来源">{harness.models.source}</Descriptions.Item>
        {harness.models.default_alias && (
          <Descriptions.Item label="默认别名">
            <Tag color="blue">{harness.models.default_alias}</Tag>
          </Descriptions.Item>
        )}
      </Descriptions>
      {harness.models.candidate_aliases_requiring_probe && harness.models.candidate_aliases_requiring_probe.length > 0 && (
        <>
          <Title level={5} style={{ marginTop: 16 }}>需要探测的候选模型</Title>
          <div>
            {harness.models.candidate_aliases_requiring_probe.map((alias) => (
              <Tag key={alias} color="orange" style={{ marginBottom: 4 }}>
                {alias}
              </Tag>
            ))}
          </div>
        </>
      )}

      <Title level={5} style={{ marginTop: 24 }}>安全配置</Title>
      <List size="small" bordered>
        {harness.safety.require_controller_budget_token && (
          <List.Item>
            <CheckCircleOutlined style={{ color: "#52c41a", marginRight: 8 }} />
            需要控制器预算令牌
          </List.Item>
        )}
        {harness.safety.require_fresh_config_home_per_trial && (
          <List.Item>
            <CheckCircleOutlined style={{ color: "#52c41a", marginRight: 8 }} />
            每次试验需要新的配置主目录
          </List.Item>
        )}
        {harness.safety.require_fresh_codex_home_per_trial && (
          <List.Item>
            <CheckCircleOutlined style={{ color: "#52c41a", marginRight: 8 }} />
            每次试验需要新的 Codex 主目录
          </List.Item>
        )}
        {harness.safety.deny_direct_oracle_access && (
          <List.Item>
            <CheckCircleOutlined style={{ color: "#52c41a", marginRight: 8 }} />
            拒绝直接访问 Oracle
          </List.Item>
        )}
        {harness.safety.deny_unscoped_shell && (
          <List.Item>
            <CheckCircleOutlined style={{ color: "#52c41a", marginRight: 8 }} />
            拒绝未限定范围的 Shell
          </List.Item>
        )}
      </List>

      {harness.environment && Object.keys(harness.environment).length > 0 && (
        <>
          <Title level={5} style={{ marginTop: 24 }}>环境变量</Title>
          <Descriptions column={1} bordered size="small">
            {Object.entries(harness.environment).map(([key, value]) => (
              <Descriptions.Item key={key} label={key}>
                <code>{value}</code>
              </Descriptions.Item>
            ))}
          </Descriptions>
        </>
      )}

      {harness.mcp && (
        <>
          <Title level={5} style={{ marginTop: 24 }}>MCP 配置</Title>
          <Descriptions column={1} bordered size="small">
            {harness.mcp.template && (
              <Descriptions.Item label="模板">{harness.mcp.template}</Descriptions.Item>
            )}
            {harness.mcp.transport_status && (
              <Descriptions.Item label="传输状态">{harness.mcp.transport_status}</Descriptions.Item>
            )}
            {harness.mcp.chaos_control && (
              <Descriptions.Item label="混沌控制">{harness.mcp.chaos_control}</Descriptions.Item>
            )}
          </Descriptions>
          {harness.mcp.read_only_servers && harness.mcp.read_only_servers.length > 0 && (
            <>
              <Title level={5} style={{ marginTop: 16 }}>只读服务器</Title>
              <div>
                {harness.mcp.read_only_servers.map((server) => (
                  <Tag key={server} color="cyan" style={{ marginBottom: 4 }}>
                    {server}
                  </Tag>
                ))}
              </div>
            </>
          )}
        </>
      )}

      {harness.version_pin && (
        <>
          <Title level={5} style={{ marginTop: 24 }}>版本锁定</Title>
          <Descriptions column={1} bordered size="small">
            {harness.version_pin.upstream && (
              <Descriptions.Item label="上游">{harness.version_pin.upstream}</Descriptions.Item>
            )}
            {harness.version_pin.package_version && (
              <Descriptions.Item label="包版本">{harness.version_pin.package_version}</Descriptions.Item>
            )}
            {harness.version_pin.commit && (
              <Descriptions.Item label="提交">
                <code>{harness.version_pin.commit}</code>
              </Descriptions.Item>
            )}
            {harness.version_pin.verification_status && (
              <Descriptions.Item label="验证状态">
                <Tag color="green">{harness.version_pin.verification_status}</Tag>
              </Descriptions.Item>
            )}
          </Descriptions>
          {harness.version_pin.note && (
            <Alert
              message="备注"
              description={harness.version_pin.note}
              type="info"
              showIcon
              style={{ marginTop: 16 }}
            />
          )}
        </>
      )}

      {harness.isolation && Object.keys(harness.isolation).length > 0 && (
        <>
          <Title level={5} style={{ marginTop: 24 }}>隔离配置</Title>
          <Descriptions column={1} bordered size="small">
            {Object.entries(harness.isolation).map(([key, value]) => (
              <Descriptions.Item key={key} label={key}>
                {typeof value === "boolean" ? (value ? "是" : "否") : String(value)}
              </Descriptions.Item>
            ))}
          </Descriptions>
        </>
      )}

      {harness.trace && Object.keys(harness.trace).length > 0 && (
        <>
          <Title level={5} style={{ marginTop: 24 }}>追踪配置</Title>
          <Descriptions column={1} bordered size="small">
            {Object.entries(harness.trace).map(([key, value]) => (
              <Descriptions.Item key={key} label={key}>
                {value}
              </Descriptions.Item>
            ))}
          </Descriptions>
        </>
      )}
    </div>
  );
}
