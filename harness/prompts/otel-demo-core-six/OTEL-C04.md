请在 OTel Demo 2.2.0 中验证 Checkout Pod 被终止时，正在处理的结算请求能否被安全排空。

请只使用当前 Harness 授权的 `k8s_ro`、`telemetry_ro`、`source_ro` 和 `chaos_control`。开始前确认 `otel-demo` 命名空间、Checkout 当前唯一 Ready Pod 的名称和 UID、结算路径基线、全局故障清单，以及外部工作负载能否证明一个带唯一关联标识的请求正处于 Checkout 处理阶段。如果只有普通短请求、无法定位活动请求、没有 Controller 短期授权或无法对账支付、物流、购物车和 Kafka 副作用，请停止并说明实验无效。

前置条件满足后，在该请求仍处于 Checkout 处理阶段时，向 `chaos_control` 请求删除 Controller 授权的单个 Checkout Pod。记录旧 Pod UID、删除时刻、Endpoint 摘除、客户端结果、Trace、Checkout 日志以及各项业务副作用。替代 Pod Ready 只说明容量恢复，不能证明旧请求被安全完成；必须判断旧请求是完整完成、明确失败、重复执行还是留下部分状态。

删除后重新绑定替代 Pod，清理本次 Trial 的故障记录，并用真实结算请求验证恢复。最后用自然语言给出活动请求、Pod 删除、Endpoint 变化、请求结果、副作用对账和恢复的完整时间线；如果删除时没有活动长请求或副作用无法独立核对，结论必须是“证据不足”。
