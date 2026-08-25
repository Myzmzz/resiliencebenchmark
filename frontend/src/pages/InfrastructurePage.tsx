import { useEffect, useState } from 'react';
import { Typography, Row, Col, Spin, Alert } from 'antd';
import { fetchInfrastructureResources } from '../api/infrastructure';
import { ResourceCard } from '../components/ResourceCard';
import type { InfrastructureResource, ResourceType } from '../types/infrastructure';

const { Title } = Typography;

const TYPE_LABELS: Record<ResourceType, string> = {
  kubernetes: 'Kubernetes 集群',
  ssh_host: 'SSH 主机',
  registry: '镜像仓库',
  model_gateway: '模型网关',
};

export default function InfrastructurePage() {
  const [resources, setResources] = useState<InfrastructureResource[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetchInfrastructureResources()
      .then(setResources)
      .catch((err) => setError(err.message))
      .finally(() => setLoading(false));
  }, []);

  if (loading) {
    return <Spin size="large" style={{ display: 'block', margin: '100px auto' }} />;
  }

  if (error) {
    return <Alert title="加载失败" description={error} type="error" showIcon />;
  }

  const grouped = resources.reduce((acc, resource) => {
    if (!acc[resource.type]) acc[resource.type] = [];
    acc[resource.type].push(resource);
    return acc;
  }, {} as Record<ResourceType, InfrastructureResource[]>);

  return (
    <div style={{ padding: '24px' }}>
      <Title level={2}>基础设施资源</Title>
      {(['kubernetes', 'ssh_host', 'registry', 'model_gateway'] as ResourceType[]).map((type) => {
        const items = grouped[type] || [];
        if (items.length === 0) return null;

        return (
          <div key={type} style={{ marginBottom: '32px' }}>
            <Title level={4}>{TYPE_LABELS[type]}</Title>
            <Row gutter={[16, 16]}>
              {items.map((resource) => (
                <Col key={resource.name} xs={24} md={12} lg={8}>
                  <ResourceCard resource={resource} />
                </Col>
              ))}
            </Row>
          </div>
        );
      })}
      {resources.length === 0 && (
        <Alert title="暂无资源" description="未找到基础设施资源" type="info" showIcon />
      )}
    </div>
  );
}
