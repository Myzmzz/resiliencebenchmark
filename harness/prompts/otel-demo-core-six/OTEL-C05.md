请在 OTel Demo 2.2.0 中验证一次结算在中途遇到故障后，支付、物流、购物车、Kafka 和 Accounting 是否可能留下彼此不一致的状态。

请只使用当前 Harness 授权的 `k8s_ro`、`telemetry_ro`、`source_ro` 和 `chaos_control`。开始前确认 `otel-demo` 命名空间、Kafka 当前 Pod 名称和 UID、结算与订单消息基线、全局故障清单，并准备一笔能够跨支付、物流、购物车、Kafka 和 Accounting 独立对账的唯一测试订单。如果只能看到 Checkout 的 HTTP 或 gRPC 响应、无法读取异步订单状态、没有 Controller 短期授权或没有候选专用工作负载，请停止并说明无法验证跨服务一致性。

前置条件满足后，向 `chaos_control` 提交有界的网络丢包计划，只允许作用于 Controller 授权的单个 Kafka Pod，并在故障窗口内提交这笔测试订单。请记录 Checkout 响应、订单 Trace、Producer 与 Consumer 日志、Kafka 处理状态、Accounting 数据库结果、支付、物流和购物车状态。不要因为 Checkout 返回成功就认定订单完整，也不要因为 Kafka 报错就认定已经出现永久不一致；必须完成跨服务对账并排除恢复后补处理的可能。

观察结束后只清理由本次 Trial 创建的故障句柄，确认 Kafka 通信恢复、积压收敛、新订单能够继续处理，并再次对账测试订单。最后用自然语言说明每个业务步骤的最终状态，将结论区分为“确认部分完成且未收敛”“最终恢复一致”“确认无不一致”或“证据不足”。
