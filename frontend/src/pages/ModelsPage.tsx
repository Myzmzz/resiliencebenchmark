import { useState, useEffect } from "react";
import { Table, Button, Drawer, Tag, Alert, Spin, Descriptions, List, Typography, Card } from "antd";
import type { ColumnsType } from "antd/es/table";
import { ReloadOutlined } from "@ant-design/icons";
import { fetchModels } from "../services/api";
import type { ModelsRegistry, ModelConfig } from "../types/model";

const { Title, Paragraph, Text } = Typography;

export default function ModelsPage() {
  const [registry, setRegistry] = useState<ModelsRegistry | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [selectedModel, setSelectedModel] = useState<ModelConfig | null>(null);

  const loadModels = async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await fetchModels();
      setRegistry(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to fetch models");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadModels();
  }, []);

  const handleRowClick = (model: ModelConfig) => {
    setSelectedModel(model);
    setDrawerOpen(true);
  };

  const columns: ColumnsType<ModelConfig> = [
    {
      title: "模型 ID",
      dataIndex: "id",
      key: "id",
      width: 180,
      render: (text: string, record: ModelConfig) => (
        <a onClick={() => handleRowClick(record)}>{text}</a>
      ),
    },
    {
      title: "显示名称",
      dataIndex: "display_name",
      key: "display_name",
      width: 200,
    },
    {
      title: "上游模型",
      dataIndex: "upstream_model",
      key: "upstream_model",
      width: 200,
    },
    {
      title: "协议候选",
      dataIndex: "protocol_candidates",
      key: "protocol_candidates",
      width: 300,
      render: (protocols: string[]) => (
        <>
          {protocols.map((protocol) => (
            <Tag key={protocol} color="blue" style={{ marginBottom: 4 }}>
              {protocol}
            </Tag>
          ))}
        </>
      ),
    },
    {
      title: "认证模式",
      dataIndex: "authentication_modes",
      key: "authentication_modes",
      width: 200,
      render: (modes?: string[]) =>
        modes ? (
          <>
            {modes.map((mode) => (
              <Tag key={mode} color="green">
                {mode}
              </Tag>
            ))}
          </>
        ) : (
          <Text type="secondary">默认</Text>
        ),
    },
    {
      title: "能力探测",
      key: "capability_probe",
      width: 120,
      render: (_: any, record: ModelConfig) => {
        const probe = record.capability_probe;
        if (!probe || Object.keys(probe).length === 0) {
          return <Tag color="default">默认</Tag>;
        }
        if (probe.enabled === false) {
          return <Tag color="red">已禁用</Tag>;
        }
        return <Tag color="orange">自定义</Tag>;
      },
    },
  ];

  if (error) {
    return (
      <div style={{ padding: 24 }}>
        <Alert message="加载失败" description={error} type="error" showIcon />
        <Button icon={<ReloadOutlined />} onClick={loadModels} style={{ marginTop: 16 }}>
          重试
        </Button>
      </div>
    );
  }

  return (
    <div style={{ padding: 24 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 16 }}>
        <Title level={3} style={{ margin: 0 }}>模型管理</Title>
        <Button icon={<ReloadOutlined />} onClick={loadModels} loading={loading}>
          刷新
        </Button>
      </div>

      {loading ? (
        <div style={{ textAlign: "center", padding: 48 }}>
          <Spin size="large" />
        </div>
      ) : !registry ? (
        <Alert
          message="未找到模型配置"
          description="未找到 harness/models.yaml 文件"
          type="info"
          showIcon
        />
      ) : (
        <>
          <Card size="small" style={{ marginBottom: 16 }}>
            <Descriptions column={2} size="small">
              <Descriptions.Item label="配置版本">{registry.version}</Descriptions.Item>
              <Descriptions.Item label="模型总数">{registry.models.length} 个</Descriptions.Item>
              <Descriptions.Item label="凭证引用" span={2}>
                {Object.keys(registry.credential_refs).join(", ")}
              </Descriptions.Item>
            </Descriptions>
            {registry.description && (
              <Paragraph style={{ marginTop: 8, marginBottom: 0, fontSize: 13, color: "#666" }}>
                {registry.description}
              </Paragraph>
            )}
          </Card>

          <Table
            columns={columns}
            dataSource={registry.models}
            rowKey="id"
            pagination={false}
          />
        </>
      )}

      <Drawer
        title={selectedModel?.display_name}
        placement="right"
        width={720}
        onClose={() => setDrawerOpen(false)}
        open={drawerOpen}
      >
        {selectedModel && <ModelDetails model={selectedModel} />}
      </Drawer>
    </div>
  );
}

function ModelDetails({ model }: { model: ModelConfig }) {
  return (
    <div>
      <Descriptions column={1} bordered size="small">
        <Descriptions.Item label="模型 ID">{model.id}</Descriptions.Item>
        <Descriptions.Item label="显示名称">{model.display_name}</Descriptions.Item>
        <Descriptions.Item label="上游模型">{model.upstream_model}</Descriptions.Item>
        <Descriptions.Item label="凭证引用">
          {model.credential_ref || <Text type="secondary">使用默认</Text>}
        </Descriptions.Item>
      </Descriptions>

      <Title level={5} style={{ marginTop: 24 }}>协议候选</Title>
      <div>
        {model.protocol_candidates.map((protocol) => (
          <Tag key={protocol} color="blue" style={{ marginBottom: 4 }}>
            {protocol}
          </Tag>
        ))}
      </div>

      {model.authentication_modes && model.authentication_modes.length > 0 && (
        <>
          <Title level={5} style={{ marginTop: 24 }}>认证模式</Title>
          <div>
            {model.authentication_modes.map((mode) => (
              <Tag key={mode} color="green" style={{ marginBottom: 4 }}>
                {mode}
              </Tag>
            ))}
          </div>
        </>
      )}

      {model.capability_probe && Object.keys(model.capability_probe).length > 0 && (
        <>
          <Title level={5} style={{ marginTop: 24 }}>能力探测配置</Title>
          <Descriptions column={1} bordered size="small">
            {model.capability_probe.enabled !== undefined && (
              <Descriptions.Item label="启用状态">
                {model.capability_probe.enabled ? "已启用" : "已禁用"}
              </Descriptions.Item>
            )}
            {model.capability_probe.timeout_seconds && (
              <Descriptions.Item label="超时时间">
                {model.capability_probe.timeout_seconds} 秒
              </Descriptions.Item>
            )}
            {model.capability_probe.isolation && (
              <Descriptions.Item label="隔离模式">{model.capability_probe.isolation}</Descriptions.Item>
            )}
            {model.capability_probe.prompt_ref && (
              <Descriptions.Item label="提示引用">{model.capability_probe.prompt_ref}</Descriptions.Item>
            )}
            {model.capability_probe.current_oauth_catalog && (
              <Descriptions.Item label="OAuth 目录">{model.capability_probe.current_oauth_catalog}</Descriptions.Item>
            )}
          </Descriptions>

          {model.capability_probe.transport_checks_implemented && (
            <>
              <Title level={5} style={{ marginTop: 16 }}>已实现的传输检查</Title>
              <List
                size="small"
                bordered
                dataSource={model.capability_probe.transport_checks_implemented}
                renderItem={(check) => <List.Item>{check}</List.Item>}
              />
            </>
          )}

          {model.capability_probe.transport_checks_required && (
            <>
              <Title level={5} style={{ marginTop: 16 }}>必需的传输检查</Title>
              <List
                size="small"
                bordered
                dataSource={model.capability_probe.transport_checks_required}
                renderItem={(check) => <List.Item>{check}</List.Item>}
              />
            </>
          )}

          {model.capability_probe.behavioral_checks_required_before_matrix_freeze && (
            <>
              <Title level={5} style={{ marginTop: 16 }}>矩阵冻结前必需的行为检查</Title>
              <List
                size="small"
                bordered
                dataSource={model.capability_probe.behavioral_checks_required_before_matrix_freeze}
                renderItem={(check) => <List.Item>{check}</List.Item>}
              />
            </>
          )}

          {model.capability_probe.recorded_fields && (
            <>
              <Title level={5} style={{ marginTop: 16 }}>记录字段</Title>
              <List
                size="small"
                bordered
                dataSource={model.capability_probe.recorded_fields}
                renderItem={(field) => <List.Item>{field}</List.Item>}
              />
            </>
          )}
        </>
      )}
    </div>
  );
}
