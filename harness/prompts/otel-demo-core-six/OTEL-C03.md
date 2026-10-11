请在 OTel Demo 2.2.0 中验证 Kubernetes 显示 Pod 健康时，真实业务路径是否可能已经不可用。

请只使用当前 Harness 授权的 `k8s_ro`、`telemetry_ro`、`source_ro` 和 `chaos_control`。开始前确认 `otel-demo` 命名空间、Product Catalog 当前 Pod 名称和 UID、Service 与 Endpoint 状态、调用 Product Catalog 的持续业务流量、无故障基线和全局故障清单。如果无法同时观察 Pod Condition、Ready Endpoint 和独立客户端结果，或者没有 Controller 短期授权，请停止并说明当前不能验证健康状态与业务能力是否一致。

前置条件满足后，向 `chaos_control` 提交有界的网络延迟计划，只允许作用于 Controller 授权的单个 Product Catalog Pod。持续访问至少一条真实依赖 Product Catalog 的路径，例如商品浏览、推荐、评论或结算，同时记录平台健康状态、Endpoint 路由、客户端成功率和延迟、Trace 以及上下游日志。健康 RPC 返回成功、Pod Ready 或故障命令成功都不能单独作为结论，必须证明同一时间窗口内平台仍在路由而业务已经持续失败，或者证明二者始终一致。

观察结束后只清理由本次 Trial 创建的故障句柄，并同时确认延迟效果消失、Endpoint 状态正常和真实业务恢复。最后用自然语言给出统一时间线，将结论明确分为“平台健康与业务能力矛盾”“二者保持一致”或“证据不足”。
