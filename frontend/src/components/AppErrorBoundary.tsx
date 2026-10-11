import { Button, Result, Typography } from "antd";
import { Component } from "react";
import type { ErrorInfo, ReactNode } from "react";

interface Props {
  children: ReactNode;
}

interface State {
  error?: Error;
}

/** Prevent one page rendering bug from falling through to React Router's raw error screen. */
export default class AppErrorBoundary extends Component<Props, State> {
  state: State = {};

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("page rendering failed", error, info.componentStack);
  }

  render() {
    if (!this.state.error) return this.props.children;
    return (
      <Result
        status="error"
        title="页面渲染失败"
        subTitle="该错误已被隔离，不会影响后端正在运行的测试。刷新页面后可以重新读取状态。"
        extra={[
          <Button type="primary" key="reload" onClick={() => window.location.reload()}>
            刷新页面
          </Button>,
        ]}
      >
        <Typography.Paragraph code copyable>
          {this.state.error.message}
        </Typography.Paragraph>
      </Result>
    );
  }
}

