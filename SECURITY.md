# Security Policy

本仓库公开保存 BenchmarkFactory 的配置契约、公开样例和验证工具，不保存任何可直接访问测试环境的凭证，也不保存正式评测的隐藏 Ground Truth。

## Secret boundary

以下内容只能通过运行时秘密源注入，禁止写入 Git、Episode、Prompt、运行轨迹或测试夹具：

- SSH 私钥、口令和主机登录凭证；
- Kubernetes client certificate、token 和完整 kubeconfig；
- Harbor 用户口令、Robot Account token；
- 模型网关和模型厂商 API key；
- MCP bearer token、Grafana service account token；
- Evaluator/Oracle 专用身份和隐藏 Ground Truth 数据包。

配置文件只能引用环境变量名称或 credential reference。日志只能记录秘密是否存在、凭据引用名和校验结果，不能输出秘密值、Authorization header 或完整连接配置。

## Runtime identities

Benchmark 运行时至少分为三类身份：

1. Agent observer：只读访问 Kubernetes、指标、Trace、日志和冻结源码；
2. Controller executor：只能在 Episode 白名单内创建、查询和清理本轮故障；
3. Evaluator/Oracle：独立采集判分证据，不能向 Agent 暴露凭据、隐藏真值或中间判定。

任何一个身份都不应同时拥有集群管理员权限、Harbor 管理权限和 Oracle 数据访问权限。

## Public and private artifacts

可以公开：schema、缺陷分类、无答案样例、脱敏配置模板、验证脚本。

必须私有：正式 Episode 的隐藏因果真值、健康版与缺陷版的对照补丁、未解锁判分数据、真实环境凭证和原始运行日志中的敏感字段。

隐藏数据包应在运行时由 Evaluator 挂载到 Agent 不可见的路径，并在运行结束后按保留策略归档或销毁。

## Incident handling

一旦凭证出现在聊天、日志、命令行参数、Git 历史或公开制品中，应立即停止使用该凭证并完成轮换。删除工作树中的文件不能消除 Git 历史或外部日志中的泄露。
