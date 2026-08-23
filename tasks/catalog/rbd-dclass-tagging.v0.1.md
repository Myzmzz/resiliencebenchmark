# 韧性缺陷模板 D1–D6 分类与静态可匹配性标注（v0.1）

## 判定口径

本文只依据 `resilience-defect-classes.v0.1.yaml` 中各模板的 `latent_defect`、`observable_evidence`、`fault_trigger` 和 `failure_outcome` 字段打标，模板名保留 YAML 原文。主 D 类按“保护机制在哪一层失效”判定：完全不存在为 D1，已有机制存在覆盖缺口为 D2，对异常作出错误反应为 D3，机制本身的实现或参数不当为 D4，多层机制相互放大为 D5，保护链依赖不可靠组件为 D6。次 D 类只表示同一模板确实同时描述了另一种失效语义，不参与分布统计。

“静态可匹配”只表示：在给定完整仓库和部署快照时，可以从 K8s/Helm/服务网格清单、应用配置、拓扑和客户端代码构造确定的风险谓词。它不表示 SLO 违反已经发生；故障是否生效、是否造成业务影响以及是否恢复，仍由运行期 Oracle 判定。“静态+行为验证”表示静态扫描可以产生候选位置，但缺少表中写明的运行证据时不能把候选直接作为参考答案阳性。“行为验证为主”表示静态模式只能提供弱提示，无法可靠确认缺陷成立。

## 主表

| 模板 ID | 模板名 | 主 D 类 | 次 D 类（可空） | 一句话分类依据 | 匹配方式 | 静态证据来源 |
| --- | --- | --- | --- | --- | --- | --- |
| RBD-001 | Missing outbound request timeout | D1 机制缺失 |  | HTTP、gRPC、数据库或消息调用完全没有显式 deadline，慢依赖因此可长期占用线程和连接。 | 静态可匹配 | 客户端构造和调用点：HTTP/gRPC timeout/deadline 字段缺失、数据库 query timeout 缺失、消息 RPC 无截止时间、调用点未创建带 deadline 的 context。 |
| RBD-002 | Deadline not propagated across calls | D2 保护盲区 |  | 入口请求已有 deadline，但内部调用、异步任务或拦截器没有继承它，保护未覆盖子工作。 | 静态+行为验证 | 静态候选：handler 新建 root context、丢失 context 参数、异步任务无 cancellation token；仍缺子 Span 晚于父 Span 结束、上游取消后下游继续执行的运行证据。 |
| RBD-003 | Cancellation not propagated to work units | D2 保护盲区 | D1 机制缺失 | 请求取消边界存在，但 future、后台任务、循环或批处理没有观察取消；若系统根本没有取消接口，则退化为 D1。 | 行为验证为主 | 静态只能提示 future/task 无取消 hook、循环无 cancellation check；还必须用 client abort/timeout 证明任务在调用者放弃后继续，且 CPU 或队列在触发流量停止后仍高。 |
| RBD-004 | Timeout budget inversion | D4 自身缺陷 |  | 内层 timeout 比外层剩余预算更长，超时机制方向正确但预算参数倒置。 | 静态可匹配 | 同一调用路径上的 Ingress/Gateway timeout、服务端请求预算与下游客户端 timeout 常量或策略；沿拓扑比较后满足 `child_timeout > parent_remaining_budget`。 |
| RBD-005 | Layered retries without a global budget | D5 机制冲突 |  | Gateway、服务客户端、SDK 或数据库层分别重试，同一失败操作被多层机制乘法放大。 | 静态可匹配 | 服务网格 route retry、Gateway policy、客户端/SDK retry、数据库 driver retry 同时覆盖同一拓扑边；未发现共享 attempt/deadline budget 或单层所有权。 |
| RBD-006 | Retries without exponential backoff and jitter | D4 自身缺陷 |  | 重试机制存在且用于瞬时故障，但退避、抖动或共享节流参数缺失，形成同步重试波。 | 静态可匹配 | retry policy 中固定间隔或零 backoff multiplier、无 jitter/randomization、无 retry throttle；代码中固定 sleep 的 retry loop。 |
| RBD-007 | Permanent errors are retried | D3 反应失调 |  | 对验证、鉴权、确定性 4xx 等永久错误继续重试，异常分类后的反应方向错误。 | 静态可匹配 | retry predicate 捕获宽泛异常，HTTP status classifier 把 4xx 纳入 retryable，exception mapping 或 consumer policy 将 validation/auth 错误映射为重试。 |
| RBD-008 | Non-idempotent operation retried | D3 反应失调 | D2 保护盲区 | 对 create、charge、reserve 等非幂等副作用重试，且幂等键或去重保护没有覆盖该写路径。 | 静态+行为验证 | 静态候选：写操作被 retry wrapper/mesh policy 覆盖，未传 idempotency key，服务端无去重检查；仍缺“提交成功但响应丢失后再次执行并产生重复状态”的证据。 |
| RBD-009 | Missing or misconfigured circuit breaker | D1 机制缺失 | D4 自身缺陷 | 模板同时覆盖 breaker 完全缺失与阈值过松/异常分类错误两种情况，主标签按“缺失”分支归 D1。 | 静态+行为验证 | resilience policy、client wrapper、service mesh 中 breaker 缺失，或 threshold/exception classifier 可疑；仍需确认该路径确有降级契约，且达到阈值后请求仍持续打向失败依赖。 |
| RBD-010 | Missing bulkhead between request classes | D1 机制缺失 |  | 关键与非关键请求共享 executor、连接池、队列或 semaphore，没有建立容量隔离机制。 | 静态+行为验证 | 静态候选：关键/可选路径映射到同一 executor、HTTP/DB pool 或 consumer queue；仍缺可选路径变慢时共享池饱和且关键路径被拖慢、其直接依赖仍健康的证据。 |
| RBD-011 | Unbounded queue or concurrency | D4 自身缺陷 | D1 机制缺失 | 队列/并发机制存在但容量无界或高于 Pod 承载能力，同时通常缺少入场控制。 | 静态可匹配 | executor 使用无界 queue、每请求创建 goroutine/future、channel/buffer 无上限，或配置上限明显超过 `resources.limits.memory`/worker capacity；入口未配置 rejection bound。 |
| RBD-012 | Missing load shedding or rate limit | D1 机制缺失 |  | 过载时没有 admission control、rate limit 或 load shedding，新请求持续进入并挤占关键资源。 | 静态可匹配 | Gateway/Ingress/Envoy route 无 rate-limit/local-rate-limit/overload policy，应用中 limiter middleware 未注册或被禁用，queue producer 无入场上限。 |
| RBD-013 | Fanout all-or-nothing without degradation | D3 反应失调 | D1 机制缺失 | 聚合器把可选分支失败直接升级为整单失败，既是错误反应，也表现为 partial response/fallback 缺失。 | 静态+行为验证 | 静态候选：fanout join 对任一异常 fail-fast、没有 default/fallback；仍缺产品契约证明该分支可选，以及仅该分支故障时其他分支健康而父请求仍失败的证据。 |
| RBD-014 | Pool exhaustion or leaked connections | D4 自身缺陷 |  | pool 的方向正确，但容量、等待上限或异常路径释放实现有缺陷。 | 静态+行为验证 | 静态候选：pool max 低于声明并发、acquire wait 无界、异常分支缺少 close/finally；仍缺 active=max、waiter 增长或资源未归还并导致健康请求等待的运行证据。 |
| RBD-015 | Runtime heap exceeds container memory | D4 自身缺陷 |  | Runtime heap 与容器内存限制的参数关系错误，没有给 native memory 和运行时开销留出余量。 | 静态可匹配 | Deployment/StatefulSet 的 `resources.limits.memory` 与 `JAVA_TOOL_OPTIONS`/`JAVA_OPTS` 中 `-Xmx`、`MaxRAMPercentage`，或 `NODE_OPTIONS=--max-old-space-size`、Helm runtime flags 的比值。 |
| RBD-016 | CPU request and limit mismatch | D4 自身缺陷 |  | CPU request/limit 与线程或 worker 并发参数失配，使保护性资源配置反而造成 throttling/欠调度。 | 静态+行为验证 | 静态候选：`resources.requests.cpu`、`resources.limits.cpu` 与 thread-pool/worker-count/runtime concurrency 的组合；仍缺预期负载内 throttled seconds 上升并与服务端延迟同向的证据。 |
| RBD-017 | Misconfigured autoscaling policy | D4 自身缺陷 |  | HPA/KEDA 已存在，但 metric、target、min replicas 或稳定窗口参数不能在排队前补足容量。 | 静态+行为验证 | HPA/KEDA 的 `metrics`、`target`、`minReplicas`、`behavior.scaleUp.stabilizationWindowSeconds` 及 workload resource requests；仍缺流量 ramp 下 desired replicas 不变/滞后且集群有可用容量的证据。 |
| RBD-018 | Liveness probe depends on slow downstream | D6 保护依赖 | D3 反应失调 | liveness 这一保护机制依赖数据库/下游健康，依赖变慢会误杀本地仍健康的进程。 | 静态可匹配 | PodSpec `livenessProbe` 的 path/command 与 health endpoint 调用图；endpoint 内访问 DB/下游，结合 `timeoutSeconds`、`failureThreshold` 和 `periodSeconds` 可形成确定规则。 |
| RBD-019 | Readiness probe false positive | D2 保护盲区 | D4 自身缺陷 | readiness 已存在，却未覆盖必需的本地初始化、缓存预热或连接状态，因而过早放流。 | 静态+行为验证 | 静态候选：readiness handler 仅返回 process alive、async warmup/连接建立未进入条件，startupProbe 缺失；仍缺新 Pod 先进入 Endpoints、后完成初始化且首批请求失败的证据。 |
| RBD-020 | Missing graceful drain on termination | D1 机制缺失 | D4 自身缺陷 | termination drain/SIGTERM shutdown 完全缺失，或 grace period 短于在途请求上界。 | 静态可匹配 | PodSpec 无 `preStop`、`terminationGracePeriodSeconds` 缺失/短于声明请求时长，应用无 SIGTERM handler、server graceful shutdown 或 in-flight drain 调用。 |
| RBD-021 | Single replica without disruption protection | D1 机制缺失 |  | 关键路径只有一个副本，且没有 PDB 或跨拓扑冗余，单次受控中断即可清空服务容量。 | 静态可匹配 | Deployment/StatefulSet `replicas: 1`，目标无匹配的 PodDisruptionBudget，且无 `topologySpreadConstraints`/pod anti-affinity；服务拓扑显示其位于关键调用路径。 |
| RBD-022 | Asynchronous event ordering assumption | D1 机制缺失 |  | consumer 更新状态时没有 version、sequence 或 causal-order guard，延迟旧事件可覆盖新状态。 | 静态+行为验证 | 静态候选：consumer 无 version/sequence compare，状态写入采用 last-write-wins；仍缺 broker/partition 契约允许乱序，以及旧事件实际晚到并被接受的证据。 |
| RBD-023 | At-least-once consumer is not idempotent | D1 机制缺失 |  | 在 at-least-once/redelivery 语义下，consumer 没有幂等键或去重机制保护副作用。 | 静态+行为验证 | 静态候选：broker ack/retry 配置允许 redelivery，代码先提交副作用后 ack，且无 idempotency table/message-id check；仍缺同一 logical message 被重投并产生两次提交的证据。 |
| RBD-024 | Non-atomic database and message write | D1 机制缺失 |  | 数据库写入与消息发布分成两个非事务步骤，没有 outbox/原子提交机制封闭失败窗口。 | 静态可匹配 | 业务代码中 DB commit 与 broker publish 顺序分离，未使用同一事务、transactional outbox/CDC 或可证明的原子协议；配置中也无对应 outbox publisher。 |
| RBD-025 | Saga compensation missing or non-idempotent | D1 机制缺失 | D4 自身缺陷 | 模板同时描述补偿完全缺失与补偿已存在但不幂等，主标签按“缺失”分支归 D1。 | 静态+行为验证 | 静态候选：workflow 在后续失败分支无 compensating action，或补偿无幂等键/去重；仍缺 late step 已到达、早期状态已提交而补偿缺失/重复的状态证据。 |
| RBD-026 | Poison message has no dead-letter path | D1 机制缺失 |  | 对确定性坏消息没有 max-attempt、DLQ 或 quarantine，consumer 只能无限重试同一消息。 | 静态可匹配 | broker subscription/consumer policy 无 max delivery attempts、dead-letter exchange/queue/topic，代码 retry forever 或反序列化异常直接回到 redelivery。 |
| RBD-028 | Cache is an incorrect hard dependency | D6 保护依赖 | D3 反应失调 | 本应只是加速/降级手段的 cache 被变成请求成功的硬依赖，cache 错误又被错误地升级为整请求失败。 | 静态+行为验证 | 静态候选：cache exception 直接向上抛出、cache write failure 终止请求、无 source-of-truth fallback；仍缺业务契约证明 cache 非权威源，并确认 backing DB/服务健康时请求仍失败。 |

## 待讨论的分类边界

以下模板仍给出一个主标签，以满足六选一约束；但在生成具体 Ground Truth instance 前应按实际分支收窄，否则同一个模板 ID 会对应不同 D 类。

| 模板 ID | 当前主类 | 候选 D 类 | 分歧点与收敛条件 |
| --- | --- | --- | --- |
| RBD-003 | D2 | D2 / D1 | 若入口已有 cancellation，只是没有传入 future/worker，则是 D2；若调用链从未建立 cancellation 机制，则应改为 D1。 |
| RBD-009 | D1 | D1 / D4 | breaker 不存在是 D1；breaker 存在但 threshold 太高或异常分类错误是 D4。建议把 locked instance 明确为其中一个分支。 |
| RBD-013 | D3 | D3 / D1 | 若强调“把可选错误升级为整单失败”的反应方向，则是 D3；若题目只证明 partial-response/fallback 完全不存在，则可归 D1。 |
| RBD-025 | D1 | D1 / D4 | compensation 不存在是 D1；已实现但非幂等、重复执行不安全是 D4。建议首版只保留单一分支。 |

## D 类分布统计

统计只使用主 D 类，合计 27 个模板：

| D 类 | 模板 | 数量 |
| --- | --- | ---: |
| D1 机制缺失 | RBD-001、RBD-009、RBD-010、RBD-012、RBD-020、RBD-021、RBD-022、RBD-023、RBD-024、RBD-025、RBD-026 | 11 |
| D2 保护盲区 | RBD-002、RBD-003、RBD-019 | 3 |
| D3 反应失调 | RBD-007、RBD-008、RBD-013 | 3 |
| D4 自身缺陷 | RBD-004、RBD-006、RBD-011、RBD-014、RBD-015、RBD-016、RBD-017 | 7 |
| D5 机制冲突 | RBD-005 | 1 |
| D6 保护依赖 | RBD-018、RBD-028 | 2 |
| **合计** | **27 个未裁撤模板** | **27** |

## 首版推荐子集

推荐首版纳入 10 个模板：**RBD-001、RBD-004、RBD-005、RBD-006、RBD-007、RBD-015、RBD-018、RBD-020、RBD-021、RBD-026**。

筛选使用三个同时满足的门槛：

1. **可静态匹配**：能在冻结的仓库与部署快照上写成确定规则，定位到具体 workload、调用边、route、probe、retry policy 或 consumer policy；不依赖先观察一次故障才能判断题目存在。
2. **注入动作安全可控**：YAML 给出的 trigger 可以约束为单服务/单依赖/单消息、有限比例和有限时长，并有明确清理路径。RBD-015 只允许 Pod cgroup 内的单目标内存压力；RBD-021 只允许 controller 管理的无状态工作负载做单 Pod 中断，禁止 node disruption。
3. **判分证据清晰**：每题都能预注册“故障生效—业务/SLO 影响—机制因果—恢复”证据。例如 RBD-005 用 attempt ratio 与重复 child Span，RBD-018 用 probe failure、下游延迟与 restart 时间对齐，RBD-026 用同一消息的 redelivery、lag 和 DLQ=0；不能以故障对象创建成功或 Pod `Running` 代替结果证据。

这 10 题覆盖 timeout、retry、capacity、health、availability、messaging 六个 family，并覆盖 D1、D3、D4、D5、D6 五类主标签。D2 暂未进入首版，因为当前三个 D2 模板都需要运行期确认“保护确实只漏掉了某类工作”，仅凭负向源码模式容易误报。

### 本轮排除模板

“排除”只表示不进入第一版，不表示模板无研究价值。

| 模板 | 匹配方式 | 排除原因 |
| --- | --- | --- |
| RBD-002 | 静态+行为验证 | context/dataflow 只能产生候选；需要 child 超过 parent deadline 的 trace 才能消除替代传播机制带来的误报。 |
| RBD-003 | 行为验证为主 | 任务可能很快自然结束或通过其他机制取消，必须证明 client abort 后仍持续占用资源。 |
| RBD-008 | 静态+行为验证 | 非幂等语义、提交时点和去重边界需运行确认；响应丢失测试还涉及合成状态清理。 |
| RBD-009 | 静态+行为验证 | 单模板混合 D1/D4 分支，且是否应 fallback 属于路径契约；应先拆分再入题。 |
| RBD-010 | 静态+行为验证 | 共享池不等于实际相互干扰，需混合负载证明关键路径被可选工作饿死。 |
| RBD-011 | 静态可匹配 | 虽可命中无界队列，但触发方式可能把 Pod 推向 OOM/长队列，停止阈值和恢复时间不如首版题清晰。 |
| RBD-012 | 静态可匹配 | 无 limiter 可静态确认，但“何时应限流”依赖容量基线；过载注入容易扩大到共享依赖，首版先不选。 |
| RBD-013 | 静态+行为验证 | 必须先证明分支在产品契约中可选，否则 all-or-nothing 可能是正确行为。 |
| RBD-014 | 静态+行为验证 | 模板混合 pool 太小、等待无界和资源泄漏，运行证据与注入方法不统一。 |
| RBD-016 | 静态+行为验证 | CPU/worker 比例没有跨语言通用静态阈值，需用期望负载下的 throttling—latency 相关性确认。 |
| RBD-017 | 静态+行为验证 | HPA 参数是否错误依赖 workload、指标时延、冷启动和可调度容量，不能仅看 manifest 定性。 |
| RBD-019 | 静态+行为验证 | 需证明 Pod 进入 Endpoints 的时间确实早于必需初始化完成，并造成首批请求失败。 |
| RBD-022 | 静态+行为验证 | 缺 version check 只有在消息可乱序且旧事件会覆盖新状态时才构成缺陷。 |
| RBD-023 | 静态+行为验证 | ack/commit 时序与副作用幂等性需用受控 redelivery 确认，正确性 Oracle 和数据清理成本较高。 |
| RBD-024 | 静态可匹配 | 非原子双写可静态确认，但在 commit/publish 窗口精确 kill、取得状态分歧证据并回收数据，首版执行复杂度偏高。 |
| RBD-025 | 静态+行为验证 | 模板混合“无补偿”和“补偿不幂等”，需要先拆分，且 partial state 的独立 Oracle 依赖业务实体语义。 |
| RBD-028 | 静态+行为验证 | 必须由产品/数据契约确认 cache 只是可选加速层；否则把 cache 当硬依赖可能是正确设计。 |

## 附录：已裁撤模板

以下三项按任务约束只记录裁撤状态，不打 D 类和匹配方式；原因文字是依据已给出的两类裁撤理由及 YAML 风险条件写出的占位，正式表述仍待项目组固化。

| 模板 ID | 模板名 | 状态 | 裁撤原因占位 |
| --- | --- | --- | --- |
| RBD-027 | Rolling upgrade contract drift | 已裁撤，不打标 | 偏 API/schema/迁移兼容性与发布契约问题，不按本版“保护机制失效”型韧性缺陷计；正式边界说明待补。 |
| RBD-029 | Cache stampede or cold-start amplification | 已裁撤，不打标 | cache flush/同步失效会把并发压力转移到 backing service，安全停止与数据面爆炸半径暂不能稳定约束；正式安全说明待补。 |
| RBD-030 | Stale service discovery or DNS dependency | 已裁撤，不打标 | DNS/discovery 故障可能越过单 namespace/单服务边界影响共享解析链，爆炸半径不可控；正式安全说明待补。 |

## 自检声明

- 已覆盖应打标的 27 个模板：RBD-001～RBD-026（连续 26 个）及 RBD-028（1 个）。
- RBD-027、RBD-029、RBD-030 只在附录记录为已裁撤，未参与主表和统计。
- 主表每行恰有一个主 D 类，次 D 类最多一个；D 类统计 `11 + 3 + 3 + 7 + 1 + 2 = 27`。
- 本文所用模板 ID 全部来自源 YAML，无编造的模板 ID，无遗漏。
