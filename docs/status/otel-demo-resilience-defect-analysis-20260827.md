# OpenTelemetry Demo 候选韧性缺陷系统分析

> 分析日期：2026-08-27
> 源码快照：`open-telemetry/opentelemetry-demo` `2.2.0`，commit `b74a7bc7bbe66099c61951f42b24dab8b6f02d18`
> Benchmark 部署：Helm chart `opentelemetry-demo` `0.40.5`，appVersion `2.2.0`
> 证据性质：离线源码、配置和 Helm 渲染分析；未在本次分析中连接集群、注入故障或执行运行时验证

## 1. 核心判断

当前仓库中的 OTel Demo 不是“因为单副本、没有 HPA 或没有 retry”就天然存在韧性缺陷。真正值得优先关注的是几条可以从代码和配置中还原出完整因果链的机制：

1. **checkout 的优雅停止逻辑在正常服务期间不可达**，Pod 终止可能在扣款、发货、清空购物车和返回结果之间任意打断请求。
2. **结算流程跨多个服务执行不可逆副作用，但没有幂等键、补偿或事务边界**；依赖超时、网络中断或 Pod 重启会把一次故障转化为“结果不确定”和重复扣款/重复发货风险。
3. **Kafka 链路同时存在生产端不等待 broker ack、消费端自动提交 offset 且吞掉数据库错误的问题**；checkout 可以成功，但 accounting/fraud 状态永久缺失。
4. **Valkey、Kafka、PostgreSQL 在 Benchmark Helm 部署中都是无业务 PVC 的单实例 Deployment**；Pod 或节点重建不只是短时不可用，还会丢失购物车、消息/offset 和订单历史。
5. **Product Reviews 的可选 AI 请求、普通评论查询和健康检查共享 10 个 worker**，而 LLM、PostgreSQL 和 product-catalog 调用没有在业务代码中设置调用级预算；慢的可选依赖可能耗尽整个服务。
6. **Kubernetes 无法识别大多数业务组件的逻辑失效**：离线 Helm 渲染显示，除 otel-collector 外，其余 22 个工作负载均无 readiness/liveness/startup probe，部分健康接口还无条件返回 `SERVING`。
7. **Benchmark values 中的数据库密码占位符被同时当成固定字符串 `otel` 的替代物**，进入数据库用户名、镜像仓库、collector 服务名和可执行文件名。只要密码轮换为其他值，部署拓扑与数据库凭据就会一起失效。

本报告共保留 **18 个候选**：9 个 high、7 个 medium、2 个 low。`high` 表示缺陷载体和传播机制有直接静态证据，不表示已经通过真实扰动证明了 SLO 违反；所有运行时结果仍是待验证的。

## 2. 分析边界与证据分级

### 2.1 纳入范围

- 上游源码：`benchmark-sources/otel-demo/`。
- 上游部署材料：`docker-compose.yml`、`docker-compose.minimal.yml`、`kubernetes/opentelemetry-demo.yaml`。
- Benchmark 部署材料：`resiliencebenchmark/environment/kubernetes/otel-demo/`、`environment/applications/otel-demo.yaml`、`scripts/deploy_application.py`。
- 既有离线语义扫描和 Episode 文件仅用于发现线索、识别未决假设；结论重新回到源码和部署清单核对。
- 历史校准产物显示 2026-08-22 的两个 600 秒健康运行窗口均为 0 failure、p95 约 37–38 ms。这只说明候选在正常负载下可以潜伏，不说明故障条件下不存在问题。

### 2.2 未纳入的确认性证据

- 本次没有读取当前集群对象、Pod 放置、事件或日志。
- 本次没有执行 Pod 删除、网络延迟、资源压力或依赖中断。
- 仓库中的三个 OTel Episode 明确仍是未验证候选；Episode 文件存在不等于缺陷已经成立。
- 运行库或网格可能提供的隐式 timeout、连接池上限、重试或取消传播，如果不能由仓库材料确认，均作为剩余不确定性保留。

### 2.3 confidence 含义

| confidence | 本报告中的含义 |
| --- | --- |
| high | 代码或部署配置直接给出缺陷载体和主要传播边；仍需运行时实验确认影响大小与 SLO 违反。 |
| medium | 主要因果链有证据，但库默认行为、并发量、Pod 放置或外部保护仍可能改变结果。 |
| low | 存在明确的不一致或风险线索，但尚不能稳定映射为业务韧性损害。 |

## 3. 源码、部署方式与适用范围

| 方式 | 入口 | 实际特征 | 本报告使用边界 |
| --- | --- | --- | --- |
| Docker Compose | `Makefile:184-208`、`docker-compose.yml` | `make start` 以 `--force-recreate --remove-orphans --detach` 启动；大部分服务使用 `restart: unless-stopped`；仅 Kafka/OpenSearch 有显式 healthcheck；Kafka、PostgreSQL、Valkey 没有数据卷。 | 用于判断本地部署的启动顺序、重启与数据持久性。 |
| 上游单文件 Kubernetes | `kubernetes/opentelemetry-demo.yaml` | 由 `make generate-kubernetes-manifests` 通过 Helm template 生成，但当前文件中的业务镜像标签仍是 `2.1.3`。 | 只代表该静态文件，不作为 Benchmark 2.2.0 运行清单的权威来源。 |
| Benchmark Helm | `environment/kubernetes/otel-demo/deployment.yaml`、`values.yaml` | chart `0.40.5` / app `2.2.0`；23 个 Deployment；Grafana、Jaeger、OpenSearch、Prometheus 禁用并接入共享观测栈；frontend-proxy 为入口。 | 本报告关于 Benchmark Kubernetes 行为的主要配置依据。 |
| Benchmark 生命周期脚本 | `scripts/deploy_application.py` | 读取 runtime-only 值，替换 `${...}`，再执行 `helm upgrade --install`；支持 activate/standby/delete 等应用生命周期。 | 用于判断占位符替换和部署恢复行为；本次未执行该脚本。 |

离线执行 `helm template`（使用非敏感占位值）得到的 Benchmark 工作负载摘要如下：

- 23 个 Deployment，0 个 StatefulSet。
- 23 个工作负载均为 1 副本。
- 仅 otel-collector 有 readiness/liveness probe；其余 22 个均无 readiness、liveness、startup probe。
- 23 个工作负载的 Helm YAML 均未显式声明 resources.requests，也没有 topologySpreadConstraints、业务 PDB 或 preStop。live API 可能依据 limit、LimitRange 或准入策略默认化部分 request，因此不能仅凭模板断言有效 request 全部为零。
- Kafka、Valkey、PostgreSQL 没有业务数据 PVC；唯一补充 PVC `otel-demo-workload-results` 只保存 Benchmark 工作负载结果。
- otel-collector 为单副本，sending queue 未配置持久化 storage。

## 4. 服务依赖关系

### 4.1 业务主链

```text
Client / Load Generator
  -> frontend-proxy (Envoy)
    -> frontend (Next.js)
      -> product-catalog -> PostgreSQL
      -> cart -> Valkey
      -> checkout
           -> cart -> Valkey
           -> product-catalog -> PostgreSQL
           -> currency
           -> payment
           -> shipping -> quote
           -> email
           -> Kafka -> accounting -> PostgreSQL
                    -> fraud-detection
      -> recommendation -> product-catalog -> PostgreSQL
      -> product-reviews -> PostgreSQL
                         -> product-catalog -> PostgreSQL
                         -> llm
      -> ad
```

### 4.2 横切依赖

- 多数业务服务连接 flagd 获取 feature flag。
- 多数业务服务向 otel-collector 导出 telemetry。
- Benchmark collector 再向共享 Coroot、Prometheus 和 Jaeger 导出。
- image-provider 只提供静态图片；load-generator 只产生流量，不是业务状态源。

### 4.3 故障传播中心

| 中心组件 | 入边/调用者 | 传播范围 |
| --- | --- | --- |
| PostgreSQL | product-catalog、product-reviews、accounting | 商品浏览、评论、推荐、购物车详情、结算准备和异步订单记录。 |
| product-catalog | frontend、checkout、recommendation、product-reviews | 几乎所有浏览与结算路径，是同步 fan-in 中心。 |
| checkout | frontend | 将多个同步依赖、不可逆副作用和异步 Kafka 发布串在一次请求内。 |
| Valkey | cart，进一步被 frontend/checkout 使用 | 购物车状态和结算输入。 |
| Kafka | checkout、accounting、fraud-detection | checkout 后处理；业务响应与异步状态可能分叉。 |
| otel-collector | 几乎所有服务 | 故障期证据完整性，不一定直接阻断业务。 |

## 5. 候选韧性缺陷汇总

| ID | 候选名称 | 主要载体 | 典型激活扰动 | 主要后果 | confidence |
| --- | --- | --- | --- | --- | --- |
| RD-C01 | checkout 优雅停止逻辑不可达 | checkout server 启动顺序 | Pod 删除、滚动升级、节点驱逐 | 在途结算被硬中断、结果不确定 | high |
| RD-C02 | 结算副作用无幂等与补偿边界 | checkout/payment/shipping/cart | 中途网络失败、超时、进程重启 | 重复扣款/发货、购物车状态分叉 | high |
| RD-C03 | 请求生命周期预算未贯穿入口到依赖 | frontend/checkout 调用链 | 依赖慢、半开连接、网络延迟 | 请求堆积、长尾、OOM/错误放大 | medium |
| RD-C04 | Kafka 生产端不等待 broker ack 且失败不影响订单结果 | checkout Kafka producer | broker 重启、分区、队列压力 | checkout 成功但订单事件丢失 | high |
| RD-C05 | Accounting 自动提交 offset 并吞掉数据库失败 | accounting consumer | PostgreSQL 故障、Pod 终止 | 订单 offset 前移但数据库缺行 | high |
| RD-C06 | 单节点无持久化 Kafka 丢失日志与 offset | Kafka Deployment | Pod/Node 重建、磁盘丢失 | accounting/fraud 永久缺失事件 | high |
| RD-C07 | Valkey 状态无持久化且 20Mi 上限无内存淘汰边界 | valkey-cart/cart | Pod 重启、节点故障、高基数购物车负载 | 购物车丢失，压力下 OOM 后清空 | high |
| RD-C08 | Cart 重连在全局锁内同步执行高次数退避 | ValkeyCartStore | Valkey 不可达或抖动 | cart 请求串行阻塞、checkout 受拖累 | medium |
| RD-C09 | PostgreSQL 无数据卷导致共享状态重建 | PostgreSQL Deployment | Pod/Node 重建、重新部署 | accounting 历史丢失，catalog/reviews 重置 | high |
| RD-C10 | 可选 AI 调用耗尽 Product Reviews 共享 worker | product-reviews | LLM/DB/catalog 变慢并伴随并发 AI 请求 | 普通评论与健康检查一起饥饿 | medium |
| RD-C11 | Recommendation 对 catalog 无显式 deadline/fallback | recommendation | product-catalog 慢或不可达 | 推荐 worker 耗尽，推荐能力失效 | medium |
| RD-C12 | Product Catalog 数据库连接并发缺少显式上界 | product-catalog/PostgreSQL | DB 慢、入口并发升高 | 连接/内存压力扩散到核心路径 | medium |
| RD-C13 | 业务 Pod 逻辑失效不会被 Kubernetes 隔离或重启 | Helm probes + health handlers | 线程池耗尽、依赖挂起、逻辑假活 | Pod 显示 Ready 但持续接收失败流量 | high |
| RD-C14 | 关键同步链无替代容量且无拓扑中断保护 | 23 个单副本 Deployment | Pod 删除、节点故障、节点维护 | 替代 Pod Ready 前关键路径零容量 | medium |
| RD-C15 | 显式 CPU requests 缺失与无拓扑分散可能放大节点压力 | Helm resources/scheduling | 邻居噪声、节点 CPU/内存压力 | 多个单副本组件相关失效 | low |
| RD-C16 | Collector 单副本内存队列造成故障期遥测缺口 | otel-collector | 后端不可达、collector 重启 | 队列丢失/拒绝，恢复判断失真 | medium |
| RD-C17 | 数据库密码占位符与固定 `otel` 标识耦合 | Benchmark values/render_text | 密码轮换、全新环境恢复 | 镜像/collector/DB 凭据同时失效 | high |
| RD-C18 | 上游静态 Kubernetes 清单与源码版本错位 | generated manifest | 直接部署静态 YAML | 运行 2.1.3，却按 2.2.0 拓扑与代码诊断 | low |

## 6. 候选逐项展开

### RD-C01 checkout 优雅停止逻辑不可达

```yaml
name: "checkout 优雅停止逻辑不可达"
D:
  location: "benchmark-sources/otel-demo/src/checkout/main.go:242-270"
  defect: "主 goroutine 先同步调用 srv.Serve(lis)；只有该阻塞调用返回后才注册 SIGTERM/SIGINT、再次 Serve 并调用 GracefulStop。因此正常服务期间的终止信号不会进入设计的优雅停止路径。"
P:
  system_conditions:
    - "checkout 正由第一次 srv.Serve(lis) 提供服务。"
    - "存在正在执行的 PlaceOrder 请求，尤其已经越过扣款或发货步骤。"
A:
  disturbance:
    - "Kubernetes Pod 删除、滚动升级、节点驱逐或节点关机发送 SIGTERM。"
R:
  state_evolution:
    - "SIGTERM handler 尚未安装，进程按默认信号行为退出。"
    - "在途 gRPC 连接被中断，defer 中的 telemetry shutdown 和 srv.GracefulStop 没有机会完成。"
    - "结算可能停在扣款、发货、清空购物车、发邮件或 Kafka 发布之间。"
    - "Deployment 启动替代 Pod，但新实例无法知道旧实例已完成了哪些副作用。"
S:
  symptom:
    - "客户端看到连接重置、5xx 或超时。"
    - "一次 checkout 可能已经扣款/发货，却没有成功响应和完整 trace。"
V:
  violated_property:
    - "Availability"
    - "Consistency"
    - "Recovery"
E:
  evidence:
    - "main.go:255 的阻塞 Serve 位于 signal.NotifyContext(main.go:258) 之前。"
    - "第二次 Serve 和 GracefulStop 仅位于 main.go:261-269。"
    - "Benchmark Helm 渲染中 checkout 没有 preStop，默认 termination grace 为 30 秒。"
confidence: "high"
```

状态：静态机制直接成立；终止时是否实际产生重复/丢失副作用仍需长请求和独立业务 Oracle 验证。

### RD-C02 结算副作用无幂等与补偿边界

```yaml
name: "结算副作用无幂等与补偿边界"
D:
  location: "checkout PlaceOrder、payment charge、shipping ship_order、cart EmptyCart"
  defect: "PlaceOrder 每次生成新 order UUID，依次扣款、发货、清空购物车、发邮件和发 Kafka 消息；请求中没有幂等键，也没有补偿事务或持久化状态机。清空购物车的错误还被显式忽略。"
P:
  system_conditions:
    - "请求已经完成至少一个不可逆副作用，但客户端尚未收到成功响应。"
    - "客户端、用户或上游在结果不确定后重试相同业务操作。"
A:
  disturbance:
    - "扣款成功后 shipping 失败或变慢。"
    - "发货成功后 email、Kafka、frontend 或网络路径失败。"
    - "checkout 在流程中途被重启或驱逐。"
R:
  state_evolution:
    - "第一次调用生成 transactionId/trackingId，但响应在后续步骤丢失。"
    - "重试生成新的 orderId、transactionId 和 trackingId，无法识别前一次已执行。"
    - "若 EmptyCart 失败，API 仍继续返回成功；购物车可能保留已购买项目。"
    - "支付、物流、购物车和异步订单记录形成彼此不一致的状态。"
S:
  symptom:
    - "重复扣款或重复发货。"
    - "用户看到 checkout 失败，但后台已有成功交易。"
    - "订单成功后购物车未清空，或重试时商品被再次购买。"
V:
  violated_property:
    - "Consistency"
    - "Recovery"
E:
  evidence:
    - "checkout/main.go:309 每次请求创建新 UUID。"
    - "checkout/main.go:329-388 显示 charge -> ship -> ignored EmptyCart -> email -> Kafka 的顺序。"
    - "src/payment/charge.js:49 为每次调用创建新 transactionId。"
    - "src/shipping/src/shipping_service.rs:47-55 为每次调用创建新 tracking ID。"
    - "PlaceOrderRequest/调用路径未出现 idempotency key、saga log、compensation 或 transaction record。"
confidence: "high"
```

状态：静态高置信候选。Demo 的 payment/shipping 是模拟实现，但该缺陷对 Demo 自身的一致性语义仍成立；若只把它们视为无状态演示桩，业务严重性会下降。

### RD-C03 请求生命周期预算未贯穿入口到依赖

```yaml
name: "请求生命周期预算未贯穿入口到依赖"
D:
  location: "frontend RPC gateways、frontend Request.ts、checkout 下游调用"
  defect: "frontend 的 gRPC 调用没有传入 deadline/call options，统一 fetch 包装没有 AbortSignal；checkout 的 gRPC 和 otelhttp.Post 调用复用入站 context，但自身不创建总预算或分阶段预算。"
P:
  system_conditions:
    - "入站请求没有由上游设置可传播的 gRPC deadline。"
    - "下游连接建立或请求已发出，但依赖长时间不返回。"
    - "持续流量使多个等待请求同时存在。"
A:
  disturbance:
    - "cart、product-catalog、shipping、email、payment 等依赖的网络延迟、半开连接或服务变慢。"
R:
  state_evolution:
    - "frontend handler 与 checkout goroutine 等待下游。"
    - "入口代理即使先向客户端返回 timeout，后端取消能否传入各 gRPC/HTTP 调用仍未由代码证明。"
    - "等待请求积累，checkout 仅有 20Mi 容器上限和 16MiB GOMEMLIMIT，内存与连接压力上升。"
S:
  symptom:
    - "p95/p99 延迟上升、请求超时或 5xx。"
    - "高并发时可能出现 checkout OOMKill、连接耗尽或吞吐塌缩。"
V:
  violated_property:
    - "Performance"
    - "Availability"
    - "Isolation"
E:
  evidence:
    - "frontend/gateways/rpc/*.gateway.ts 的 client 调用均未传 deadline。"
    - "frontend/utils/Request.ts:12-32 的 fetch 未传 AbortSignal。"
    - "checkout/main.go:442-608 未创建 context.WithTimeout/WithDeadline。"
    - "frontend-proxy/envoy.tmpl.yaml 的 catch-all frontend route 未显式声明 route timeout；Envoy/平台默认值仍需核实。"
    - "仓库已有 EPI-OTEL-CART-DEADLINE-001，但文件本身明确标记为低置信、待实验区分。"
confidence: "medium"
```

待验证：Envoy 默认 route timeout、Next.js server 默认、gRPC service config/interceptor 和取消传播可能限制实际等待时间。

### RD-C04 Kafka 生产端不等待 broker ack 且失败不影响订单结果

```yaml
name: "Kafka 生产端不等待 broker ack 且失败不影响订单结果"
D:
  location: "src/checkout/kafka/producer.go 与 checkout.sendToPostProcessor"
  defect: "Sarama Producer.RequiredAcks 被设置为 NoResponse，源码注释明确说明这可能吞掉失败消息；sendToPostProcessor 只记录错误，不向 PlaceOrder 返回失败。"
P:
  system_conditions:
    - "checkout 已完成支付和发货，并准备向 orders topic 发布。"
    - "accounting/fraud 的正确状态依赖该 Kafka 事件。"
A:
  disturbance:
    - "Kafka broker 重启、网络分区、连接抖动或消息队列压力。"
R:
  state_evolution:
    - "producer 将写入视为无需 broker 响应，无法证明消息已被 broker 持久化。"
    - "错误只写日志或 span；PlaceOrder 继续构造成功响应。"
    - "accounting 和 fraud-detection 永远收不到该订单。"
S:
  symptom:
    - "用户看到 checkout 成功，但 accounting 数据库中没有订单，fraud 计数也缺失。"
    - "仅从入口成功率无法发现异步状态丢失。"
V:
  violated_property:
    - "Consistency"
    - "Durability"
E:
  evidence:
    - "checkout/kafka/producer.go:39-41 的注释与 RequiredAcks=NoResponse。"
    - "checkout/main.go:385-392 调用 sendToPostProcessor 后无错误返回路径。"
    - "checkout/main.go:639-660 仅记录 Kafka error/context cancellation。"
confidence: "high"
```

### RD-C05 Accounting 自动提交 offset 并吞掉数据库失败

```yaml
name: "Accounting 自动提交 offset 并吞掉数据库失败"
D:
  location: "src/accounting/Consumer.cs"
  defect: "consumer 启用 EnableAutoCommit，消息处理后没有显式 StoreOffset/Commit；数据库 SaveChanges 或解析异常被 catch 后只记录日志，循环继续。Kafka offset 与数据库事务没有原子关系。"
P:
  system_conditions:
    - "orders 消息已被 Consume 返回。"
    - "自动提交定时器可能推进 offset。"
    - "数据库写入失败、变慢或进程在写入窗口被终止。"
A:
  disturbance:
    - "PostgreSQL 不可达、连接耗尽、事务失败或 accounting Pod 被终止。"
R:
  state_evolution:
    - "offset 可能在数据库成功前被自动提交。"
    - "SaveChanges 异常被吞掉，consumer 继续处理下一条消息。"
    - "数据库恢复后，已提交的失败消息不再回放；相反的时序窗口也可能导致重复处理。"
S:
  symptom:
    - "accounting 表缺少部分已成功 checkout 的订单。"
    - "重启后可能出现主键冲突日志或重复处理，但没有可查询的失败队列。"
V:
  violated_property:
    - "Consistency"
    - "Recovery"
E:
  evidence:
    - "Consumer.cs:66-68 在 Consume 后立即 ProcessMessage。"
    - "Consumer.cs:132 执行 SaveChanges。"
    - "Consumer.cs:134-137 捕获所有异常且不重试、不停止、不进 DLQ。"
    - "Consumer.cs:142-149 配置 EnableAutoCommit=true。"
    - "Program.cs:21-24 在 host.Run 之前阻塞调用 StartListening，优雅停止路径也不清晰。"
confidence: "high"
```

### RD-C06 单节点无持久化 Kafka 丢失日志与 offset

```yaml
name: "单节点无持久化 Kafka 丢失日志与 offset"
D:
  location: "Kafka Dockerfile、Benchmark Helm values、渲染后的 Kafka Deployment"
  defect: "Kafka 同时承担 controller 和 broker，quorum 只有 node 1，内部 topic replication factor 为 1；Deployment 没有 Kafka data volume/PVC。"
P:
  system_conditions:
    - "orders topic 中存在尚未消费或需要恢复的记录、consumer offset 或 broker metadata。"
    - "Pod 被重新创建到新的容器文件系统。"
A:
  disturbance:
    - "Kafka Pod 删除、节点故障/维护、容器文件系统丢失或重新部署。"
R:
  state_evolution:
    - "唯一 broker/controller 停止，生产和消费同时不可用。"
    - "新 Pod 使用空文件系统启动，topic 数据、offset 和 KRaft metadata 可能重建为空状态。"
    - "checkout、accounting、fraud-detection 重新连接，但旧事件无法恢复。"
S:
  symptom:
    - "故障窗口 checkout 后处理失败；恢复后历史订单事件仍缺失。"
    - "consumer 可能从新 topic 的 earliest 开始，但原有数据已经不存在。"
V:
  violated_property:
    - "Availability"
    - "Consistency"
    - "Recovery"
E:
  evidence:
    - "src/kafka/Dockerfile:17-24 配置单节点 controller/broker 和 replication factor 1。"
    - "environment/kubernetes/otel-demo/values.yaml:417-447 配置 replicas=1、单 voter。"
    - "离线 Helm 渲染的 Kafka Deployment volumes 为空。"
    - "docker-compose.yml:719-752 同样没有 Kafka 数据卷。"
confidence: "high"
```

### RD-C07 Valkey 状态无持久化且 20Mi 上限无内存淘汰边界

```yaml
name: "Valkey 状态无持久化且 20Mi 上限无内存淘汰边界"
D:
  location: "valkey-cart Deployment 与 Cart ValkeyCartStore"
  defect: "Valkey 以单副本 Deployment 运行，没有数据 PVC；命令只启动 valkey-server，未设置与 20Mi cgroup limit 对齐的 maxmemory/eviction 边界。Cart 把每个用户购物车写入 Valkey，TTL 为 60 分钟。"
P:
  system_conditions:
    - "存在未结算购物车。"
    - "Pod 被重建，或 60 分钟窗口内高基数 userId 使 Valkey 内存接近 20Mi。"
A:
  disturbance:
    - "Valkey Pod/Node 故障、滚动重建。"
    - "购物车流量突增、唯一用户数增长或大购物车负载。"
R:
  state_evolution:
    - "Pod 重建后内存数据全部消失。"
    - "压力场景中 Valkey 可能先触及容器 limit，被 OOMKill；Deployment 重启后得到空实例。"
    - "cart 对原用户返回空购物车，checkout 失去原始输入。"
S:
  symptom:
    - "用户购物车突然清空。"
    - "结算得到空订单、失败或商品数量与用户预期不一致。"
V:
  violated_property:
    - "Consistency"
    - "Availability"
    - "Recovery"
E:
  evidence:
    - "values.yaml:725-753 配置 replicas=1、memory limit=20Mi，command 未包含 maxmemory。"
    - "离线 Helm 渲染的 valkey-cart volumes 为空。"
    - "ValkeyCartStore.cs:147-174 把 cart 写入 hash 并设置 60 分钟 TTL。"
    - "Benchmark 工作负载为不同迭代生成不同 userId，可在高负载变体下形成高基数状态。"
confidence: "high"
```

### RD-C08 Cart 重连在全局锁内同步执行高次数退避

```yaml
name: "Cart 重连在全局锁内同步执行高次数退避"
D:
  location: "src/cart/src/cartstore/ValkeyCartStore.cs:19-129"
  defect: "Valkey 断连后，每个业务操作先进入 EnsureRedisConnected；重连在单个全局 lock 内同步调用 ConnectionMultiplexer.Connect，配置 30 次连接重试和指数退避，没有调用级取消或 circuit breaker。"
P:
  system_conditions:
    - "_isRedisConnectionOpened 已因 ConnectionFailed 变为 false。"
    - "多个 cart RPC 并发到达。"
A:
  disturbance:
    - "Valkey 网络不可达、连接抖动、Pod 启停或 DNS/Service 暂时无 Endpoint。"
R:
  state_evolution:
    - "第一个请求持有 _locker 并执行同步连接/重试。"
    - "其他请求在同一锁后排队；失败后后续请求可能再次创建连接并重复退避。"
    - "cart 的延迟扩散到 frontend 和 checkout。"
S:
  symptom:
    - "cart RPC 长时间等待或成批失败。"
    - "checkout 因无法读取/清空购物车而失败，恢复延迟超过 Valkey 本身恢复时间。"
V:
  violated_property:
    - "Performance"
    - "Isolation"
    - "Recovery"
E:
  evidence:
    - "ValkeyCartStore.cs:19 设置 RedisRetryNumber=30。"
    - "ValkeyCartStore.cs:56-60 设置 ConnectRetry 和 ExponentialRetry。"
    - "ValkeyCartStore.cs:74-102 在 lock 内同步 Connect。"
    - "AddItem/GetCart/EmptyCart 均在访问前调用 EnsureRedisConnected。"
confidence: "medium"
```

待验证：StackExchange.Redis 在 `abortConnect=false` 下的实际 Connect 等待时间和并发行为可能降低或改变放大程度。

### RD-C09 PostgreSQL 无数据卷导致共享状态重建

```yaml
name: "PostgreSQL 无数据卷导致共享状态重建"
D:
  location: "Benchmark postgresql Deployment、src/postgresql/init.sql"
  defect: "PostgreSQL 作为单副本 Deployment 运行，只有 init ConfigMap 和 /dev/shm emptyDir，没有挂载 PGDATA PVC。catalog、reviews 和 accounting 共享该数据库。"
P:
  system_conditions:
    - "accounting 已写入运行时订单历史，或数据库状态相对 init.sql 已发生变化。"
    - "PostgreSQL Pod 被新的容器文件系统替代。"
A:
  disturbance:
    - "Pod 删除、节点故障、镜像升级、重新部署或容器文件系统损坏。"
R:
  state_evolution:
    - "旧 PGDATA 随容器消失。"
    - "新实例执行 init.sql，catalog/reviews 恢复为种子数据，accounting 表恢复为空。"
    - "恢复期间 product-catalog/reviews/accounting 不可用；恢复后历史状态仍不可恢复。"
S:
  symptom:
    - "商品浏览、评论和结算准备短时失败。"
    - "先前成功订单从 accounting 中永久消失；评论/商品状态回到初始快照。"
V:
  violated_property:
    - "Consistency"
    - "Recovery"
    - "Availability"
E:
  evidence:
    - "values.yaml:527-573 只声明 /dev/shm emptyDir 和 init ConfigMap。"
    - "离线 Helm 渲染的 postgresql volumes 只有 postgresql-init ConfigMap 与 dshm emptyDir。"
    - "init.sql:6-60 创建 accounting/reviews 表，init.sql:125-156 创建并填充 catalog。"
    - "accounting/Consumer.cs:98-132 将订单运行时写入同一 PostgreSQL。"
confidence: "high"
```

### RD-C10 可选 AI 调用耗尽 Product Reviews 共享 worker

```yaml
name: "可选 AI 调用耗尽 Product Reviews 共享 worker"
D:
  location: "src/product-reviews/product_reviews_server.py、database.py"
  defect: "GetProductReviews、GetAverageProductReviewScore、AskProductAIAssistant 和 health RPC 共用 ThreadPoolExecutor(max_workers=10)。AI 路径同步调用 LLM 一至两次，并可能同步访问 catalog/DB；业务代码未传调用级 timeout。"
P:
  system_conditions:
    - "至少约 10 个 AI 请求同时占用 worker。"
    - "LLM、product-catalog 或 PostgreSQL 调用显著变慢。"
A:
  disturbance:
    - "LLM 延迟/限流/网络半开，PostgreSQL 或 product-catalog 延迟。"
    - "AI assistant 并发流量突增。"
R:
  state_evolution:
    - "AI handler 在同步调用中占住 worker。"
    - "新的普通评论、平均分和 health RPC 在同一 executor 后排队。"
    - "单副本服务没有第二个 worker pool 或实例接管。"
S:
  symptom:
    - "不仅 AI 功能变慢，普通产品评论和平均分也不可用。"
    - "health RPC 可能同样排队；Kubernetes 又没有配置该 probe。"
V:
  violated_property:
    - "Isolation"
    - "Performance"
    - "Availability"
E:
  evidence:
    - "product_reviews_server.py:90-115 定义三个业务方法和 health。"
    - "product_reviews_server.py:173-223、292-295 创建 LLM 调用，未传 timeout。"
    - "product_reviews_server.py:312-319 的 catalog gRPC 调用未传 timeout。"
    - "product_reviews_server.py:360-366 将业务与 health 注册到 max_workers=10 的同一 server。"
    - "database.py:33、60 每请求新建 psycopg2 connection，连接串未显示 connect_timeout。"
confidence: "medium"
```

待验证：OpenAI SDK、psycopg2 和 gRPC Python 的库级默认 timeout；实际 gRPC 接收队列大小；AI endpoint 的真实并发量。

### RD-C11 Recommendation 对 catalog 无显式 deadline/fallback

```yaml
name: "Recommendation 对 catalog 无显式 deadline/fallback"
D:
  location: "src/recommendation/recommendation_server.py"
  defect: "默认路径每次直接调用 product_catalog_stub.ListProducts；缓存分支只在故障演示 flag 开启时存在，且首次/未命中仍访问 catalog。调用无显式 timeout、try/except、circuit breaker 或稳定 fallback，并与 health 共用 10 个 worker。"
P:
  system_conditions:
    - "默认 feature flag 基线下处理推荐请求。"
    - "并发推荐请求达到 worker 容量。"
A:
  disturbance:
    - "product-catalog 变慢、不可达或其 PostgreSQL 变慢。"
R:
  state_evolution:
    - "ListRecommendations worker 阻塞在 catalog RPC。"
    - "10 个 worker 被占满后，推荐和 health 请求排队。"
    - "frontend 的推荐区域可能降级为空，但 recommendation 服务本身没有快速降级边界。"
S:
  symptom:
    - "推荐列表消失或长时间加载；recommendation RPC 可用性下降。"
    - "主商品页可能仍可用，因此系统级严重性低于 checkout/catalog 故障。"
V:
  violated_property:
    - "Performance"
    - "Availability"
    - "Isolation"
E:
  evidence:
    - "recommendation_server.py:43-56 的入口直接调用 get_product_list。"
    - "recommendation_server.py:78-96 显示默认/缓存未命中均调用 catalog。"
    - "recommendation_server.py:156-166 创建无 service config 的 channel，并将 health 注册到 max_workers=10 的 server。"
    - "frontend Ad.provider.tsx 和 Recommendations.tsx 对无数据结果显示空区域，是系统级降级反证。"
confidence: "medium"
```

### RD-C12 Product Catalog 数据库连接并发缺少显式上界

```yaml
name: "Product Catalog 数据库连接并发缺少显式上界"
D:
  location: "src/product-catalog/main.go"
  defect: "product-catalog 使用 Go database/sql 连接 PostgreSQL，但代码未设置 SetMaxOpenConns、SetMaxIdleConns 或连接生命周期；查询预算完全依赖入站 context。该服务又是多条核心路径的 fan-in。"
P:
  system_conditions:
    - "frontend、checkout、recommendation、product-reviews 同时访问 catalog。"
    - "入站 context 没有短且可靠的 deadline。"
    - "PostgreSQL 查询或建连变慢。"
A:
  disturbance:
    - "数据库 CPU/IO 压力、连接延迟、网络抖动或入口并发突增。"
R:
  state_evolution:
    - "请求在 database/sql 中等待或扩张连接数。"
    - "PostgreSQL max_connections、内存或 product-catalog 100Mi 容器资源受到压力。"
    - "catalog 失败传播到浏览、推荐、评论、购物车详情和 checkout。"
S:
  symptom:
    - "核心 API 普遍变慢或返回 5xx。"
    - "数据库出现连接耗尽，checkout 和普通浏览同时受影响。"
V:
  violated_property:
    - "Performance"
    - "Availability"
    - "Isolation"
E:
  evidence:
    - "product-catalog/main.go:138-166 初始化 database/sql。"
    - "main.go:268-344 的查询均直接使用共享 db。"
    - "全文件不存在 SetMaxOpenConns/SetMaxIdleConns/SetConnMaxLifetime。"
    - "values.yaml:574-600 将 product-catalog 指向 PostgreSQL，memory limit 为 100Mi。"
confidence: "medium"
```

待验证：Go driver/database/sql 的实际连接行为、PostgreSQL max_connections、正常和压力流量下的连接数曲线。

### RD-C13 业务 Pod 逻辑失效不会被 Kubernetes 隔离或重启

```yaml
name: "业务 Pod 逻辑失效不会被 Kubernetes 隔离或重启"
D:
  location: "Benchmark Helm probes；checkout/product-catalog/recommendation/product-reviews health handlers"
  defect: "除 otel-collector 外，22 个 Deployment 无 readiness/liveness/startup probe。部分服务虽然注册 gRPC health，但 checkout/product-catalog/recommendation/product-reviews 的 Check 无条件返回 SERVING，不反映关键依赖。"
P:
  system_conditions:
    - "进程仍存活并保持容器 Running，但 worker、连接或依赖已处于不可服务状态。"
    - "Service 仍把该 Pod 视为可用 Endpoint。"
A:
  disturbance:
    - "线程池耗尽、依赖永久挂起、数据库连接耗尽、内部死锁或部分初始化失败。"
R:
  state_evolution:
    - "kubelet 没有 probe 失败信号，不会重启容器。"
    - "EndpointSlice 没有 readiness 变化，不会隔离逻辑失效 Pod。"
    - "单副本意味着全部流量继续进入该实例，恢复只依赖依赖自然恢复、进程崩溃或外部干预。"
S:
  symptom:
    - "Pod 显示 Running/Ready，但业务持续 timeout/5xx。"
    - "依赖恢复后服务可能仍被阻塞状态拖住，Kubernetes 不主动修复。"
V:
  violated_property:
    - "Availability"
    - "Recovery"
E:
  evidence:
    - "对 chart 0.40.5 + committed values 的离线 Helm 渲染：仅 collector 有 readiness/liveness。"
    - "checkout/main.go:281-283 与 product-catalog/main.go:409-411 无条件返回 SERVING。"
    - "recommendation_server.py:58-64、product_reviews_server.py:109-115 同样无条件返回 SERVING。"
    - "cart 实现了依赖 Valkey 的 readinessCheck，但 Helm 未把它接到 Kubernetes probe。"
confidence: "high"
```

### RD-C14 关键同步链无替代容量且无拓扑中断保护

```yaml
name: "关键同步链无替代容量且无拓扑中断保护"
D:
  location: "Benchmark Helm default.replicas/schedulingRules 与各 component values"
  defect: "frontend-proxy、frontend、product-catalog、PostgreSQL、cart、Valkey、checkout 及其同步依赖均为单副本，没有 topology spread、anti-affinity 或业务 PDB。关键链任一点丢失时没有等价 Ready Endpoint。"
P:
  system_conditions:
    - "持续流量经过被中断组件。"
    - "替代 Pod 尚未完成调度、拉镜像、初始化和应用启动。"
    - "若讨论节点级相关故障，还需多个关键 Pod 实际共置于同一节点。"
A:
  disturbance:
    - "单 Pod 删除/崩溃、节点故障、节点维护或多个共置 Pod 同时丢失。"
R:
  state_evolution:
    - "Service 的 Ready Endpoint 数从 1 变为 0。"
    - "Deployment 创建替代 Pod，但恢复窗口内调用快速失败或等待。"
    - "若状态组件同时重建，还会叠加 RD-C06/RD-C07/RD-C09 的数据丢失。"
S:
  symptom:
    - "关键路径在替代 Pod Ready 前出现完全不可用窗口。"
    - "节点级故障可能同时中断多个串联依赖，恢复时间由最慢组件决定。"
V:
  violated_property:
    - "Availability"
    - "Recovery"
E:
  evidence:
    - "values.yaml:755-777 将 default.replicas 设为 1，scheduling affinity 为空。"
    - "离线 Helm 渲染显示 23/23 Deployment replicas=1，均无 topology spread。"
    - "collector PDB disabled；业务组件没有 PDB 配置。"
    - "仓库已有 currency 单实例中断 Episode，但明确仍需实验确认业务影响窗口。"
confidence: "medium"
```

待验证：当前 Pod 放置、镜像缓存、调度余量、客户端/Service 的失败速度和实际 SLO 影响。

### RD-C15 显式 CPU requests 缺失与无拓扑分散可能放大节点压力

```yaml
name: "显式 CPU requests 缺失与无拓扑分散可能放大节点压力"
D:
  location: "Benchmark Helm resources 与 schedulingRules"
  defect: "23 个工作负载的 Helm YAML 均未显式设置 requests；大多数只设置 memory limit，LLM 连 memory limit 也没有，且所有单副本都没有分散约束。CPU 需求可能未被调度器表达；memory request 是否由 limit/LimitRange 默认化需查看 live API。"
P:
  system_conditions:
    - "运行在多租户或资源利用率较高的节点。"
    - "多个关键 Pod 被调度到同一节点，实际工作集高于调度器感知。"
    - "集群没有通过 LimitRange/准入策略为 CPU 提供足够的默认 request。"
A:
  disturbance:
    - "邻居噪声、节点 CPU 饱和、节点内存压力、流量突增或 telemetry 量突增。"
R:
  state_evolution:
    - "CPU 竞争推高同步调用延迟，形成上游等待。"
    - "内存压力触发 Pod 驱逐/OOM；没有 requests 的 Pod 缺少资源预留。"
    - "共置的多个单副本组件相关失效，故障跨服务放大。"
S:
  symptom:
    - "多个服务同时出现长尾、重启或 Evicted。"
    - "单一依赖压力演化为入口吞吐和成功率下降。"
V:
  violated_property:
    - "Performance"
    - "Isolation"
    - "Availability"
E:
  evidence:
    - "离线 Helm 渲染显示 23/23 workload 的模板 requests 为空，但这不是 live API 有效 request 的证明。"
    - "values.yaml 只为多数 component 配置 resources.limits.memory。"
    - "values.yaml:774-777 的 affinity/nodeSelector/tolerations 均为空。"
confidence: "low"
```

该候选不是“模板没 requests 就一定有缺陷”；Kubernetes 可能把 memory limit 默认成 request，LimitRange/准入策略也可能补齐资源。只有 live API 仍缺少有效 CPU 预留，并且节点压力和不利共置同时存在时，传播链才成立。

### RD-C16 Collector 单副本内存队列造成故障期遥测缺口

```yaml
name: "Collector 单副本内存队列造成故障期遥测缺口"
D:
  location: "Benchmark otel-collector values"
  defect: "collector 单副本运行；Jaeger/Prometheus exporter 启用 sending_queue，但未配置 file_storage 或其他持久化 storage，collector 也没有 PVC。memory_limiter 在 80% limit 工作。"
P:
  system_conditions:
    - "应用持续产生 traces/metrics/logs。"
    - "共享后端不可达或吞吐低于入口，队列开始积压。"
A:
  disturbance:
    - "Coroot、Prometheus、Jaeger 网络故障/变慢，collector Pod 重启或节点故障。"
R:
  state_evolution:
    - "内存 sending queue 增长。"
    - "达到 limiter/queue 边界后数据被拒绝或丢弃；Pod 重启时内存队列全部丢失。"
    - "业务可能继续运行，但故障期证据出现不可恢复缺口。"
S:
  symptom:
    - "故障窗口 traces/metrics/logs 缺失或不连续。"
    - "恢复判断看似无错误，实际是 telemetry 丢失；根因分析和独立 Oracle 失真。"
V:
  violated_property:
    - "Observability"
    - "Recovery"
    - "Consistency"
E:
  evidence:
    - "values.yaml:905-918 的 Jaeger/Prometheus exporter sending_queue 未声明 storage。"
    - "values.yaml:966-969 配置 memory_limiter。"
    - "values.yaml:1090-1133 将三类信号导向共享后端。"
    - "values.yaml:1152-1159、1288-1295 配置单副本 Deployment；extraVolumes 为空。"
confidence: "medium"
```

待验证：collector 版本的默认 queue/retry 参数、应用 SDK 在 collector 不可用时的本地缓冲与丢弃策略、共享后端实际可用性。

### RD-C17 数据库密码占位符与固定 `otel` 标识耦合

```yaml
name: "数据库密码占位符与固定 otel 标识耦合"
D:
  location: "environment/kubernetes/otel-demo/values.yaml 与 scripts/deploy_application.py"
  defect: "OTEL_DEMO_POSTGRES_PASSWORD 不只进入 POSTGRES_PASSWORD，还替换数据库名/用户名的 otel 前缀、service.namespace、默认镜像仓库、OTEL_COLLECTOR_NAME、collector 可执行文件名、日志索引等固定标识。只有值恰好为 otel 时，许多引用才与源码/镜像约定一致。"
P:
  system_conditions:
    - "部署者为 OTEL_DEMO_POSTGRES_PASSWORD 提供非空且不等于固定标识 otel 的安全密码。"
    - "deploy_application.render_text 按字面替换所有占位符。"
A:
  disturbance:
    - "数据库密码轮换、灾难恢复到新环境、重新部署或安全加固。"
R:
  state_evolution:
    - "连接串变为 Username=<password>u、Password=<password>p，但 init.sql 固定创建 otelu/otelp。"
    - "默认业务镜像仓库变为 <password>-demo，collector command 变为 <password>col-contrib。"
    - "若镜像/命令不存在，Pod ImagePullBackOff/CrashLoop；即使容器启动，数据库认证也失败。"
    - "catalog、reviews、accounting 和 telemetry 同时失效，恢复流程被一个无关的 secret 轮换破坏。"
S:
  symptom:
    - "部署或恢复后大批 Pod 无法启动。"
    - "product-catalog 启动时 DB Ping 失败并退出；reviews/accounting 认证失败；collector 可能 command not found。"
V:
  violated_property:
    - "Availability"
    - "Recovery"
    - "Isolation"
E:
  evidence:
    - "values.yaml:9-10、588、623-625 用密码占位符拼接固定数据库用户/密码。"
    - "src/postgresql/init.sql:4 固定创建 otelu WITH PASSWORD otelp。"
    - "values.yaml:762-770 用同一占位符生成 collector 名和镜像仓库。"
    - "values.yaml:889-903 用同一占位符生成 collector command 与日志索引。"
    - "deploy_application.py:154-170 对 `${KEY}` 执行无语义区分的字面替换。"
    - "使用安全测试值 rotated 的离线 Helm render 得到 harbor.invalid/observe/rotated-demo:*、Username=rotatedu、Password=rotatedp、Deployment/rotated-collector 和 command=/rotatedcol-contrib，直接复现了耦合。"
confidence: "high"
```

这是配置层高置信缺陷，不需要真实密码才能确认耦合关系；但当前运行环境是否一直固定使用兼容值，本次未读取。

### RD-C18 上游静态 Kubernetes 清单与源码版本错位

```yaml
name: "上游静态 Kubernetes 清单与源码版本错位"
D:
  location: "benchmark-sources/otel-demo/kubernetes/opentelemetry-demo.yaml"
  defect: "源码仓库 HEAD/tag 为 2.2.0，但 committed 静态 YAML 的业务镜像和标签大量为 2.1.3；其中 product-catalog 仍挂载 products ConfigMap，而 2.2.0 源码已经依赖 PostgreSQL。"
P:
  system_conditions:
    - "操作者直接 apply committed opentelemetry-demo.yaml。"
    - "分析、测试或修复依据使用 2.2.0 源码和 Benchmark 2.2.0 拓扑。"
A:
  disturbance:
    - "重新部署、故障恢复或测试环境重建时选择静态 YAML 路径。"
R:
  state_evolution:
    - "实际运行 2.1.3 镜像和旧依赖图。"
    - "针对 2.2.0 PostgreSQL/catalog 链路的故障模型、修复和 Oracle 与运行系统错位。"
    - "故障时可能注入或诊断错误目标，得到不可复现结论。"
S:
  symptom:
    - "运行行为与源码分析不一致。"
    - "韧性测试报告声称覆盖 2.2.0，实际验证的是 2.1.3。"
V:
  violated_property:
    - "Recovery"
    - "Assurance Consistency"
E:
  evidence:
    - "git describe 返回 2.2.0，HEAD 为 b74a7bc7...。"
    - "kubernetes/opentelemetry-demo.yaml:19383-19445 等资源使用 2.1.3 镜像/标签。"
    - "同文件 product-catalog 使用 ConfigMap products；2.2.0 main.go 使用 PostgreSQL。"
    - "Benchmark deployment.yaml 单独固定 chart 0.40.5/appVersion 2.2.0，因此该错位不自动适用于 Benchmark Helm 路径。"
confidence: "low"
```

该项更接近部署可追溯性/评测有效性缺陷。若没有人直接使用该静态 YAML，它不会激活为运行时韧性问题。

## 7. 主要因果簇与优先级

候选并非 18 个彼此独立的问题。最值得验证的是四个因果簇：

1. **结算结果不确定簇（RD-C01/C02/C03/C04）**：终止、慢依赖和 Kafka 故障都可能发生在支付完成之后。单看 HTTP 成功率会漏掉重复副作用和异步事件丢失。
2. **异步一致性簇（RD-C04/C05/C06/C09）**：生产 ack、consumer offset、数据库事务和存储持久性之间没有端到端一致性边界。
3. **依赖慢与容量耗尽簇（RD-C03/C08/C10/C11/C12/C13）**：缺少调用级预算或隔离，有限 worker/内存/连接逐步被占满，而 Kubernetes 看不到逻辑失效。
4. **故障恢复与证据丢失簇（RD-C07/C09/C14/C15/C16/C17）**：Pod/Node 恢复可能同时带来状态丢失、零容量窗口和 telemetry 缺口；仅检查 Pod Ready 不能证明业务恢复。

若后续安排运行时验证，优先顺序建议是：

1. checkout 长请求期间终止 Pod，并以 transactionId、trackingId、cart、Kafka event、accounting row 为独立 Oracle，联合验证 RD-C01/C02。
2. 在 Kafka broker 不可用和 PostgreSQL 不可用两种情况下逐订单对账，区分 RD-C04/C05/C06。
3. 分别重建 Valkey、Kafka、PostgreSQL Pod，检查状态恢复而不只检查 Pod Ready，验证 RD-C06/C07/C09。
4. 对 LLM、product-catalog、Valkey 施加有界延迟，测 worker、连接、goroutine、队列和入口 SLO，验证 RD-C03/C08/C10/C11/C12。
5. 用非默认的安全占位值做纯离线 Helm render/容器启动验证，确认 RD-C17 的具体失败点；不要把真实凭据写入仓库或日志。

## 8. 明确不直接判为韧性缺陷的发现

- **单副本本身**不是缺陷；RD-C06/C07/C09/C14 都额外要求关键状态、零替代容量、实际中断窗口或数据丢失机制。
- **没有 HPA 本身**不是缺陷；本报告没有把 HPA 缺失单独列项。
- **没有 retry 本身**不是缺陷；相反，RD-C08 关注的是高次数退避在全局锁内形成的放大机制。
- **没有 PDB 本身**不是缺陷；PDB 也不保护直接 Pod crash/delete。RD-C14 的核心是单副本关键链的零 Endpoint 窗口和可能的节点相关故障。
- **内置 feature flags**是 Demo 有意提供的故障演示机制，不作为自然产生的韧性缺陷计数。只有 flag 未开启时也存在的生命周期、状态和传播问题进入候选表。
- **上游 Grafana/OpenSearch 的配置问题**不自动适用于 Benchmark 运行态，因为 Benchmark values 明确禁用了本地 Grafana、Jaeger、OpenSearch 和 Prometheus。
- **健康基线通过**不等于没有韧性缺陷。两次校准只覆盖无扰动稳定窗口，恰好符合“正常条件潜伏、扰动下激活”的定义。

## 9. 可复现的离线检查摘要

本次使用的主要只读检查包括：

```bash
git -C benchmark-sources/otel-demo status --short --branch
git -C benchmark-sources/otel-demo rev-parse HEAD
git -C benchmark-sources/otel-demo describe --tags --always
docker compose -f benchmark-sources/otel-demo/docker-compose.yml config --format json
helm show chart open-telemetry/opentelemetry-demo --version 0.40.5
helm template otel-demo open-telemetry/opentelemetry-demo \
  --version 0.40.5 --namespace otel-demo --values <rendered-safe-values>
rg -n 'timeout|deadline|retry|backoff|fallback|ThreadPoolExecutor|GracefulStop' \
  benchmark-sources/otel-demo/src
```

其中 `<rendered-safe-values>` 只使用非敏感占位值在内存中渲染，没有读取、输出或持久化真实密码/API key。未执行 `kubectl`、Helm install/upgrade 或任何故障注入。

## 10. 最终证据边界

本报告已经可以确认“代码/配置中存在这些潜在不足和传播入口”，但不能把 18 项都写成“已在当前 Kubernetes 集群复现的缺陷”。更准确的结论是：

- RD-C01/C02/C04/C05/C06/C07/C09/C13/C17 的缺陷载体和关键因果边具有高静态置信度。
- RD-C03/C08/C10/C11/C12/C14/C16 仍需要排除库默认保护、实际并发/放置和外部基础设施保护。
- RD-C15 还需要读取 live API 的有效 requests 与 LimitRange/准入默认；RD-C18 是部署/评测一致性候选，两者都不应直接写成已确认业务故障。
- 任何最终“缺陷成立”结论都应同时保留：故障生效证据、业务症状、状态传播、清理、业务恢复和竞争解释；Pod Ready、命令成功或 CR 删除都不足以单独证明。
