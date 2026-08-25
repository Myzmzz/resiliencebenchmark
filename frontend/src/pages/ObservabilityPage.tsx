import { useState, useEffect } from "react";
import { Button, Alert, Spin, Descriptions, List, Typography, Card, Space, Tag } from "antd";
import { ReloadOutlined, DatabaseOutlined, CheckCircleOutlined } from "@ant-design/icons";
import { fetchObservability } from "../services/api";
import type { ObservabilityStack } from "../types/observability";

const { Title } = Typography;

export default function ObservabilityPage() {
  const [stack, setStack] = useState<ObservabilityStack | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const loadObservability = async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await fetchObservability();
      setStack(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to fetch observability");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadObservability();
  }, []);

  if (error) {
    return (
      <div style={{ padding: 24 }}>
        <Alert message="加载失败" description={error} type="error" showIcon />
        <Button icon={<ReloadOutlined />} onClick={loadObservability} style={{ marginTop: 16 }}>
          重试
        </Button>
      </div>
    );
  }

  if (loading) {
    return (
      <div style={{ padding: 24, textAlign: "center", paddingTop: 48 }}>
        <Spin size="large" />
      </div>
    );
  }

  if (!stack) {
    return (
      <div style={{ padding: 24 }}>
        <Alert
          message="未找到可观测性配置"
          description="未找到 environment/shared/observability.yaml 文件"
          type="info"
          showIcon
        />
      </div>
    );
  }

  return (
    <div style={{ padding: 24 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 16 }}>
        <Title level={3} style={{ margin: 0 }}>可观测性栈</Title>
        <Button icon={<ReloadOutlined />} onClick={loadObservability} loading={loading}>
          刷新
        </Button>
      </div>

      <Card size="small" style={{ marginBottom: 16 }}>
        <Descriptions column={2} size="small">
          <Descriptions.Item label="名称">{stack.metadata.name}</Descriptions.Item>
          <Descriptions.Item label="可见性">{stack.metadata.visibility}</Descriptions.Item>
          <Descriptions.Item label="API 版本">{stack.apiVersion}</Descriptions.Item>
          <Descriptions.Item label="类型">{stack.kind}</Descriptions.Item>
        </Descriptions>
      </Card>

      <Space direction="vertical" size="large" style={{ width: "100%" }}>
        {/* Cluster Access */}
        <Card title={<><DatabaseOutlined /> 集群访问</>} size="small">
          <Descriptions column={2} size="small" bordered>
            <Descriptions.Item label="Kubeconfig 引用">{stack.spec.clusterAccess.kubeconfigRef}</Descriptions.Item>
            <Descriptions.Item label="命名空间范围">{stack.spec.clusterAccess.namespaceScope}</Descriptions.Item>
            <Descriptions.Item label="RBAC 配置">{stack.spec.clusterAccess.rbacProfile}</Descriptions.Item>
            <Descriptions.Item label="密钥策略">{stack.spec.clusterAccess.secretPolicy}</Descriptions.Item>
          </Descriptions>
        </Card>

        {/* Prometheus */}
        <Card title="Prometheus" size="small">
          <Descriptions column={1} size="small" bordered>
            <Descriptions.Item label="端点"><code>{stack.spec.prometheus.endpoint}</code></Descriptions.Item>
            <Descriptions.Item label="访问模式">
              <Tag color="blue">{stack.spec.prometheus.accessMode}</Tag>
            </Descriptions.Item>
          </Descriptions>
          <Title level={5} style={{ marginTop: 16 }}>允许的 API</Title>
          <Space wrap>
            {stack.spec.prometheus.allowedApis.map((api) => (
              <Tag key={api} color="green"><code>{api}</code></Tag>
            ))}
          </Space>
          <Title level={5} style={{ marginTop: 16 }}>必需标签</Title>
          <Space wrap>
            {stack.spec.prometheus.requiredLabels.map((label) => (
              <Tag key={label} color="blue">{label}</Tag>
            ))}
          </Space>
          <Title level={5} style={{ marginTop: 16 }}>禁止标签</Title>
          <Space wrap>
            {stack.spec.prometheus.forbiddenLabels.map((label) => (
              <Tag key={label} color="red">{label}</Tag>
            ))}
          </Space>
        </Card>

        {/* Jaeger */}
        <Card title="Jaeger" size="small">
          <Descriptions column={1} size="small" bordered>
            <Descriptions.Item label="端点"><code>{stack.spec.jaeger.endpoint}</code></Descriptions.Item>
            <Descriptions.Item label="访问模式">
              <Tag color="blue">{stack.spec.jaeger.accessMode}</Tag>
            </Descriptions.Item>
          </Descriptions>
          <Title level={5} style={{ marginTop: 16 }}>必需能力</Title>
          <Space wrap>
            {stack.spec.jaeger.requiredCapabilities.map((cap) => (
              <Tag key={cap} color="cyan">{cap}</Tag>
            ))}
          </Space>
          {stack.spec.jaeger.notes && stack.spec.jaeger.notes.length > 0 && (
            <>
              <Title level={5} style={{ marginTop: 16 }}>备注</Title>
              <List
                size="small"
                bordered
                dataSource={stack.spec.jaeger.notes}
                renderItem={(note) => <List.Item>{note}</List.Item>}
              />
            </>
          )}
        </Card>

        {/* Loki */}
        <Card title="Loki" size="small">
          <Descriptions column={1} size="small" bordered>
            <Descriptions.Item label="端点"><code>{stack.spec.loki.endpoint}</code></Descriptions.Item>
            <Descriptions.Item label="访问模式">
              <Tag color="blue">{stack.spec.loki.accessMode}</Tag>
            </Descriptions.Item>
          </Descriptions>
          <Title level={5} style={{ marginTop: 16 }}>允许的 API</Title>
          <Space wrap>
            {stack.spec.loki.allowedApis.map((api) => (
              <Tag key={api} color="green"><code>{api}</code></Tag>
            ))}
          </Space>
          <Title level={5} style={{ marginTop: 16 }}>必需标签</Title>
          <Space wrap>
            {stack.spec.loki.requiredLabels.map((label) => (
              <Tag key={label} color="blue">{label}</Tag>
            ))}
          </Space>
        </Card>

        {/* OpenTelemetry Collector */}
        <Card title="OpenTelemetry Collector" size="small">
          <Descriptions column={2} size="small" bordered>
            <Descriptions.Item label="gRPC 端点"><code>{stack.spec.otelCollector.grpcEndpoint}</code></Descriptions.Item>
            <Descriptions.Item label="HTTP 端点"><code>{stack.spec.otelCollector.httpEndpoint}</code></Descriptions.Item>
            <Descriptions.Item label="访问模式" span={2}>
              <Tag color="orange">{stack.spec.otelCollector.accessMode}</Tag>
            </Descriptions.Item>
          </Descriptions>
          <Title level={5} style={{ marginTop: 16 }}>导出器基线</Title>
          <Descriptions column={3} size="small" bordered>
            {Object.entries(stack.spec.otelCollector.exporterBaseline).map(([key, value]) => (
              <Descriptions.Item key={key} label={key}>{value}</Descriptions.Item>
            ))}
          </Descriptions>
        </Card>

        {/* Agent Tooling */}
        <Card title="智能体工具" size="small">
          <Title level={5} style={{ marginBottom: 16 }}>MCP 服务器</Title>
          <Space direction="vertical" size="middle" style={{ width: "100%" }}>
            {stack.spec.agentTooling.mcpServers.map((server) => (
              <Card key={server.name} size="small" style={{ backgroundColor: "#fafafa" }}>
                <Descriptions column={1} size="small">
                  <Descriptions.Item label="名称">
                    <Tag color="purple">{server.name}</Tag>
                  </Descriptions.Item>
                  <Descriptions.Item label="作用域">{server.scope}</Descriptions.Item>
                  <Descriptions.Item label="允许的操作">
                    <Space wrap>
                      {server.allowedOperations.map((op) => (
                        <Tag key={op} color="green">{op}</Tag>
                      ))}
                    </Space>
                  </Descriptions.Item>
                </Descriptions>
              </Card>
            ))}
          </Space>
        </Card>

        {/* Evidence Retention */}
        <Card title="证据保留" size="small">
          <Descriptions column={2} size="small" bordered>
            <Descriptions.Item label="原始窗口">{stack.spec.evidenceRetention.rawWindow}</Descriptions.Item>
            <Descriptions.Item label="标准化窗口">{stack.spec.evidenceRetention.normalizedWindow}</Descriptions.Item>
            <Descriptions.Item label="导出格式" span={2}>
              <Space wrap>
                {stack.spec.evidenceRetention.exportFormat.map((format) => (
                  <Tag key={format} color="geekblue">{format}</Tag>
                ))}
              </Space>
            </Descriptions.Item>
          </Descriptions>
        </Card>

        {/* Readiness Checks */}
        <Card title={<><CheckCircleOutlined /> 就绪检查</>} size="small">
          <List
            size="small"
            bordered
            dataSource={stack.spec.readinessChecks}
            renderItem={(check) => (
              <List.Item>
                <CheckCircleOutlined style={{ color: "#52c41a", marginRight: 8 }} />
                {check}
              </List.Item>
            )}
          />
        </Card>
      </Space>
    </div>
  );
}
