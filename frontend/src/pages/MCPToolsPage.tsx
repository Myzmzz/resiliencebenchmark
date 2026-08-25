import { useState, useEffect } from "react";
import { Table, Button, Drawer, Tag, Alert, Spin, Descriptions, List, Typography, Card, Space } from "antd";
import type { ColumnsType } from "antd/es/table";
import { ReloadOutlined, CheckCircleOutlined, StopOutlined, LockOutlined } from "@ant-design/icons";
import { fetchMCPTools } from "../services/api";
import type { MCPToolsRegistry, MCPTool } from "../types/mcp";

const { Title, Paragraph, Text } = Typography;

export default function MCPToolsPage() {
  const [registry, setRegistry] = useState<MCPToolsRegistry | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [selectedTool, setSelectedTool] = useState<MCPTool | null>(null);

  const loadMCPTools = async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await fetchMCPTools();
      setRegistry(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to fetch MCP tools");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadMCPTools();
  }, []);

  const handleRowClick = (tool: MCPTool) => {
    setSelectedTool(tool);
    setDrawerOpen(true);
  };

  const getModeTag = (mode: string) => {
    const modeMap: Record<string, { color: string; icon: any }> = {
      read_only: { color: "blue", icon: <CheckCircleOutlined /> },
      controlled_write: { color: "orange", icon: <LockOutlined /> },
      write: { color: "red", icon: <StopOutlined /> },
    };
    const config = modeMap[mode] || { color: "default", icon: null };
    return <Tag icon={config.icon} color={config.color}>{mode}</Tag>;
  };

  const columns: ColumnsType<MCPTool> = [
    {
      title: "工具 ID",
      dataIndex: "id",
      key: "id",
      width: 180,
      render: (text: string, record: MCPTool) => (
        <a onClick={() => handleRowClick(record)}>{text}</a>
      ),
    },
    {
      title: "模式",
      dataIndex: "mode",
      key: "mode",
      width: 180,
      render: (mode: string) => getModeTag(mode),
    },
    {
      title: "用途",
      dataIndex: "purpose",
      key: "purpose",
      ellipsis: true,
    },
    {
      title: "允许操作",
      key: "allowed_operations",
      width: 120,
      render: (_: any, record: MCPTool) => record.allowed_operations.length,
    },
    {
      title: "拒绝操作",
      key: "denied_operations",
      width: 120,
      render: (_: any, record: MCPTool) => record.denied_operations.length,
    },
  ];

  if (error) {
    return (
      <div style={{ padding: 24 }}>
        <Alert message="加载失败" description={error} type="error" showIcon />
        <Button icon={<ReloadOutlined />} onClick={loadMCPTools} style={{ marginTop: 16 }}>
          重试
        </Button>
      </div>
    );
  }

  return (
    <div style={{ padding: 24 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 16 }}>
        <Title level={3} style={{ margin: 0 }}>MCP 服务</Title>
        <Button icon={<ReloadOutlined />} onClick={loadMCPTools} loading={loading}>
          刷新
        </Button>
      </div>

      {loading ? (
        <div style={{ textAlign: "center", padding: 48 }}>
          <Spin size="large" />
        </div>
      ) : !registry ? (
        <Alert
          message="未找到 MCP 配置"
          description="未找到 harness/mcp-tools.yaml 文件"
          type="info"
          showIcon
        />
      ) : (
        <>
          <Card size="small" style={{ marginBottom: 16 }}>
            <Descriptions column={2} size="small">
              <Descriptions.Item label="配置版本">{registry.version}</Descriptions.Item>
              <Descriptions.Item label="工具总数">{registry.tools.length} 个</Descriptions.Item>
            </Descriptions>
            {registry.description && (
              <Paragraph style={{ marginTop: 8, marginBottom: 0, fontSize: 13, color: "#666" }}>
                {registry.description}
              </Paragraph>
            )}
            {Object.keys(registry.runtime_refs).length > 0 && (
              <>
                <Title level={5} style={{ marginTop: 16, marginBottom: 8 }}>运行时引用</Title>
                <Space wrap>
                  {Object.entries(registry.runtime_refs).map(([key, value]) => (
                    <Tag key={key} color="purple">
                      {key}: <code>{value}</code>
                    </Tag>
                  ))}
                </Space>
              </>
            )}
          </Card>

          <Table
            columns={columns}
            dataSource={registry.tools}
            rowKey="id"
            pagination={false}
          />

          {registry.not_exposed_to_agent && (
            <Alert
              message="不对智能体暴露"
              description={
                <div>
                  <Text strong>原因：</Text> {registry.not_exposed_to_agent.reason}
                  <br />
                  <Text strong>包含：</Text> {registry.not_exposed_to_agent.includes.join(", ")}
                </div>
              }
              type="warning"
              showIcon
              style={{ marginTop: 16 }}
            />
          )}
        </>
      )}

      <Drawer
        title={selectedTool?.id}
        placement="right"
        width={720}
        onClose={() => setDrawerOpen(false)}
        open={drawerOpen}
      >
        {selectedTool && <MCPToolDetails tool={selectedTool} />}
      </Drawer>
    </div>
  );
}

function MCPToolDetails({ tool }: { tool: MCPTool }) {
  return (
    <div>
      <Descriptions column={1} bordered size="small">
        <Descriptions.Item label="工具 ID">{tool.id}</Descriptions.Item>
        <Descriptions.Item label="模式">
          <Tag color={tool.mode === "read_only" ? "blue" : tool.mode === "controlled_write" ? "orange" : "red"}>
            {tool.mode}
          </Tag>
        </Descriptions.Item>
        <Descriptions.Item label="用途">{tool.purpose}</Descriptions.Item>
      </Descriptions>

      <Title level={5} style={{ marginTop: 24 }}>允许的操作</Title>
      <Space wrap>
        {tool.allowed_operations.map((op) => (
          <Tag key={op} color="green">{op}</Tag>
        ))}
      </Space>

      <Title level={5} style={{ marginTop: 24 }}>拒绝的操作</Title>
      <Space wrap>
        {tool.denied_operations.map((op) => (
          <Tag key={op} color="red">{op}</Tag>
        ))}
      </Space>

      {tool.scope && Object.keys(tool.scope).length > 0 && (
        <>
          <Title level={5} style={{ marginTop: 24 }}>作用域配置</Title>
          <List size="small" bordered>
            {Object.entries(tool.scope).map(([key, value]) => (
              <List.Item key={key}>
                <Space>
                  {value ? (
                    <CheckCircleOutlined style={{ color: "#52c41a" }} />
                  ) : (
                    <StopOutlined style={{ color: "#ff4d4f" }} />
                  )}
                  <Text strong>{key}:</Text>
                  <Text>{typeof value === "boolean" ? (value ? "是" : "否") : value}</Text>
                </Space>
              </List.Item>
            ))}
          </List>
        </>
      )}

      {tool.gates && Object.keys(tool.gates).length > 0 && (
        <>
          <Title level={5} style={{ marginTop: 24 }}>门控配置</Title>
          <List size="small" bordered>
            {Object.entries(tool.gates).map(([key, value]) => (
              <List.Item key={key}>
                <Space>
                  {value ? (
                    <CheckCircleOutlined style={{ color: "#52c41a" }} />
                  ) : (
                    <StopOutlined style={{ color: "#ff4d4f" }} />
                  )}
                  <Text>{key}</Text>
                </Space>
              </List.Item>
            ))}
          </List>
        </>
      )}
    </div>
  );
}
