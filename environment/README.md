# Environment

本目录用于定义被测系统、可重复工作负载、可观测数据接入、快照和环境重置。

每个环境实现都应提供可证明的初始状态和幂等清理能力，不能只依赖 Agent 报告“已恢复”。

## Layout

- `applications/`: 每个被测系统一个环境适配器。适配器只记录可公开给 Agent 的环境契约、源码快照、镜像锁定、工作负载、SLO、观测端点、reset 与 qualify 规则。
- `shared/observability.yaml`: Prometheus、Jaeger、Loki、OpenTelemetry Collector 等观测平面的共享接入约束。
- `shared/source-snapshots.yaml`: 源码快照的不可变映射规则，保证 Agent 能读取代码，但不能从路径、diff 或隐藏真值中直接看到答案。

## Current Preparation Boundary

这些文件是环境准备配置，不是 Benchmark 结果。完整实时摘要见根目录 `ENVIRONMENT_STATUS.md`，各应用细节见 `readiness`：

- `train-ticket`: 应用 Deployment 当前 Ready；可重复负载镜像尚未构建并固定 digest，订单服务连接堆积复发风险和正式业务基线仍待验证。
- `sock-shop`: 固定清单已创建，但节点不能访问 Docker Hub；14 个 Deployment 暂时缩容为 0，等待 Harbor digest 镜像同步。
- `otel-demo`: load-generator 已启用，Prometheus/Jaeger/Loki 三信号已接通；kubeletstats 仍受节点证书过期影响。

## Secret Boundary

本目录不得提交真实 kubeconfig、Harbor 密码、API key、SSH 凭证、公网 IP、私有绝对路径或隐藏 Ground Truth。所有敏感值必须通过运行时 secret provider 注入，并在配置中使用占位符。
