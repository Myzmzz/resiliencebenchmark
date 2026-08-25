import { Card, Tag, Descriptions } from 'antd';
import type { InfrastructureResource, ResourceStatus } from '../types/infrastructure';

const STATUS_COLORS: Record<ResourceStatus, string> = {
  qualified: 'success',
  partial: 'warning',
  pending: 'default',
  error: 'error',
};

const STATUS_LABELS: Record<ResourceStatus, string> = {
  qualified: '已认证',
  partial: '部分可用',
  pending: '待检测',
  error: '错误',
};

interface ResourceCardProps {
  resource: InfrastructureResource;
}

export function ResourceCard({ resource }: ResourceCardProps) {
  return (
    <Card
      title={resource.name}
      extra={<Tag color={STATUS_COLORS[resource.status]}>{STATUS_LABELS[resource.status]}</Tag>}
      size="small"
    >
      <Descriptions column={1} size="small">
        <Descriptions.Item label="Endpoint">{resource.endpoint}</Descriptions.Item>
        {Object.entries(resource.metrics).map(([key, value]) => (
          <Descriptions.Item key={key} label={key}>
            {value}
          </Descriptions.Item>
        ))}
        {resource.last_qualified && (
          <Descriptions.Item label="Last Qualified">
            {new Date(resource.last_qualified).toLocaleString()}
          </Descriptions.Item>
        )}
      </Descriptions>
    </Card>
  );
}
