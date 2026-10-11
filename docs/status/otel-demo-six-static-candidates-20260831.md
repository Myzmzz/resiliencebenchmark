# OTel Demo 后续测试使用的六个静态候选

当前测试范围已经固定为下面六项。它们都能在 OTel Demo 2.2.0 的源码或 Benchmark 部署配置中找到直接依据，因此可以进入针对性测试设计；但这些依据只确定了问题可能存在的代码和配置载体，还没有证明故障注入后一定会产生预期的业务损害。

原来的二十个通用模式继续作为研究档案保存，不再作为当前 OTel Demo 测试入口。后续界面、执行日志和结果归档主要围绕下面六项组织。

## 结算结果不确定时，重复提交可能再次扣款或发货

Checkout 的 PlaceOrder 请求没有携带可以在多次提交之间复用的操作标识。一次结算会依次扣款、发货、清空购物车并发送订单消息。如果第一次调用实际上已经完成了部分操作，但成功响应在后续步骤或返回途中丢失，用户再次提交时，系统会把它当成一笔新订单，可能再次扣款、再次发货或再次发送消息。源码依据是 `pb/demo.proto` 中的 PlaceOrderRequest，以及 `src/checkout/main.go` 中的 PlaceOrder 执行顺序。

测试时需要在第一次结算已经越过至少一个副作用、但客户端没有收到成功响应时制造网络丢包，随后重试同一笔结算，并对订单、交易、物流、购物车和 Kafka 消息做唯一性对账。

## Valkey内存空间很小时，购物车可能随着进程重启一起丢失

Cart 会不断把不同用户的购物车写入 Valkey，并让数据保留六十分钟。Benchmark 只给 Valkey 配置了 20Mi 内存上限，启动参数中没有看到 maxmemory 或淘汰策略。购物车继续增长时，Valkey 可能触碰容器内存上限，随后被杀死或重启；由于该部署也没有为购物车状态配置持久化存储，已有购物车可能同时丢失。源码和配置依据是 `src/cart/src/cartstore/ValkeyCartStore.cs` 与 `environment/kubernetes/otel-demo/values.yaml`。

测试时需要先写入可以独立核对的购物车数据，再对 Valkey 施加受控内存压力，持续观察内存、OOM、Pod 重启和 Cart 接口，并在恢复后逐项检查原有购物车是否完整。

## Pod看起来健康时，真实业务请求可能已经失败

Checkout、Product Catalog、Product Reviews 和 Recommendation 的健康处理器会直接返回 SERVING，并不检查各自依赖的数据库或下游服务。Benchmark 也没有为这些业务组件配置能够代表真实业务能力的 Kubernetes 探针。因此，下游服务变慢或不可用时，Pod 仍可能显示 Ready 并继续接收流量，但用户请求已经失败。

测试时需要让一个真实业务依赖变慢或失效，同时记录 Pod 状态、Service Endpoint 和业务请求结果。只有在 Pod 仍被路由流量而真实请求持续失败时，才能确认这个问题。

## Checkout被终止时，正在进行的结算不会被安全排空

Checkout 在注册 SIGTERM 处理逻辑之前，就同步进入了阻塞的 gRPC Serve 调用。正常提供服务期间，后面的 GracefulStop 代码实际上无法执行。Kubernetes 终止 Checkout Pod 时，正在进行的结算可能被直接切断，而系统无法保证已经接收的请求完成，也无法确认扣款、发货、购物车和订单消息分别进行到了哪一步。这一结论直接来自 `src/checkout/main.go` 中的服务启动和停止控制流。

测试时需要在一个可跟踪的长结算请求进行期间终止 Checkout Pod，并同时记录旧 Pod、Endpoint、客户端响应以及所有业务副作用，确认请求是否被安全排空、重复执行或部分完成。

## 结算中途失败后，各服务可能留下彼此不一致的状态

Checkout 按顺序执行扣款、发货、清空购物车、发送邮件和发布 Kafka 消息，但没有持久化的结算状态机、Outbox 或补偿流程。清空购物车失败会被忽略，邮件或 Kafka 失败也可能只留下日志。只要结算在这些步骤之间中断，就可能出现支付已经成功但没有订单消息、商品已经发货但购物车未清空等部分完成状态。源码依据是 `src/checkout/main.go` 和 `src/checkout/kafka/producer.go`。

测试时需要分别在扣款后、发货后、清空购物车后和 Kafka 发布期间制造中断，并对订单、支付、物流、购物车、Kafka 和 Accounting 记录进行跨服务完整性对账。

## Accounting写库失败后，Kafka消息仍可能被当作已经消费

Accounting 从 Kafka 取出订单后写入 PostgreSQL。数据库或解析异常会被捕获并只记录日志，消费循环不会因此失败；同时消费者启用了自动提交 Offset。数据库写入和 Kafka 消费位置不在同一个事务或显式确认边界内，所以写库失败后，消费位置仍可能向前推进，订单随后不会再次交给 Accounting 处理。这一结论直接来自 `src/accounting/Consumer.cs`。

测试时需要在 Accounting 处理订单时让 PostgreSQL 写入失败，或者在 SaveChanges 前后终止消费者，然后精确对比 Kafka consumer group Offset、PostgreSQL 订单记录以及恢复后的重放和重复情况。

## 后续执行边界

建议先验证 Checkout 终止、Accounting 消费一致性和 Checkout 部分完成问题，再验证重复提交、健康状态错位和 Valkey 内存问题。每次测试仍由用户定义输入并确认执行；系统需要完整保留实际执行命令、实时日志、业务对账结果、故障清理和恢复证据。命令成功、Pod Ready 或故障对象删除，都不能单独作为缺陷成立或恢复完成的依据。

六项现在都已经配有可以直接发送给 Harness 的中文自然语言 Prompt。第一阶段页面提供“生成 Harness 自然语言 Prompt”入口，用户选择候选后即可得到完整内容，也可以追加自己的补充要求。Prompt 对 Codex、Claude Code、BladeAI 和 DeepSeek Harness 保持中立，只描述验证目标、允许使用的 MCP、执行前置条件、建议扰动、证据、停止、清理和恢复要求，不包含 Shell 命令、模型参数、凭据或隐藏答案。
