import { Alert, Button, Empty, Spin } from "antd";
import { ReloadOutlined } from "@ant-design/icons";

export function PageLoading({ label = "正在加载评测数据" }: { label?: string }) {
  return <div className="evaluation-center-state"><Spin size="large" description={label} /></div>;
}

export function PageError({ error, onRetry }: { error: Error; onRetry?: () => void }) {
  return (
    <Alert
      type="error"
      showIcon
      title="评测数据加载失败"
      description={error.message}
      action={onRetry ? <Button icon={<ReloadOutlined />} onClick={onRetry}>重试</Button> : undefined}
    />
  );
}

export function PageEmpty({ label }: { label: string }) {
  return <div className="evaluation-center-state"><Empty description={label} /></div>;
}
