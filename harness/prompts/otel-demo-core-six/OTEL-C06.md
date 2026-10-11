请在 OTel Demo 2.2.0 中验证 Accounting 写入 PostgreSQL 失败或处理中断后，Kafka 消费位置是否可能已经前移，导致订单消息丢失或重复处理。

请只使用当前 Harness 授权的 `k8s_ro`、`telemetry_ro`、`source_ro` 和 `chaos_control`。开始前确认 `otel-demo` 命名空间、Accounting 当前 Pod 名称和 UID、Kafka、Accounting 与 PostgreSQL 的无故障基线、全局故障清单，并产生一条能够由独立 Oracle 识别的测试订单消息。还必须能够读取或可靠推断该消息对应的 Kafka 消费位置和 PostgreSQL 订单记录；如果缺少任一证据、没有 Controller 短期授权或无法控制中断时机，请停止并说明实验条件不足。

前置条件满足后，在 Accounting 正在处理该测试消息时，向 `chaos_control` 请求删除 Controller 授权的单个 Accounting Pod。记录旧 Pod UID、删除时刻、Kafka Consumer 日志与消费位置、数据库写入结果、Trace 和替代 Pod 的处理行为。Pod 删除成功、替代 Pod Ready 或日志中出现错误都不能单独证明消息丢失或重复，必须对比恢复前后的消费位置与数据库记录。

删除后重新绑定替代 Accounting Pod，清理本次 Trial 的故障记录，确认消息处理继续推进并检查测试订单最终出现零次、一次还是多次。最后用自然语言给出消息产生、消费、中断、替代实例、Offset 与数据库记录的完整时间线，并将结论区分为“消息丢失”“重复处理”“正确重放”或“证据不足”。
