<!-- 用途：本规则库覆盖的韧性机制清单与技术栈分层组件表。由第 1 步产生；组件表下半部分由 tools/gen_tables.py 依据 tools/sources.tsv 与 tools/fetch-log.json 重新生成。 -->

# stack.md — 韧性机制清单与技术栈

## 0. 两个定义（全文照此使用）

- **韧性缺陷**：正常条件下不影响运行，但在依赖变慢或不可用、丢包、实例被终止、CPU 或内存压力这类局部故障下，使某个韧性机制没有按它应有的方式工作的实现或配置。
- **缺陷类别**：1 规范明示型（文档写明该做或不该做）；2 需求相对型（文档允许，但相对系统自己的预算或 SLO 是错的）；3 组合型（各自合规，合起来放大故障）；4 实现错误型（机制在、参数对、行为错）。

## 1. 机制清单是怎么来的

先从第 10、11 层（准则、模式目录、检查规则集）里读它们自己的分类和命名，再与任务给的十组起点清单合并。归纳时实际用到的分类源：

| 来源 | doc_id | 它自己的机制分类方式 |
|---|---|---|
| Google SRE 书 ch.21/22 | `DOC-SRE-OVERLOAD` / `DOC-SRE-CASCADING` | 按小节切：Queue Management、Load Shedding and Graceful Degradation、Retries、Latency and Deadlines、Slow Startup and Cold Caching、Always Go Downward in the Stack；ch.21 另有 Per-Customer Limits、Client-Side Throttling、Criticality、Utilization Signals、Load from Connections |
| AWS Builders' Library | `DOC-AWS-TIMEOUTS` 等 5 篇 | 一篇一机制：超时/重试/抖动、健康检查、避免回退、负载削减、幂等 API |
| Azure 云设计模式 | `DOC-AZ-PATTERN-*` 12 篇 | 一模式一页：Retry、Circuit Breaker、Bulkhead、Health Endpoint Monitoring、Throttling、Queue-Based Load Leveling、Rate Limiting、Saga、Compensating Transaction、Leader Election、Cache-Aside |
| Azure Well-Architected 可靠性支柱 | `DOC-AZ-WAF-*` | 按"建议"切：自保护与自愈、瞬时故障处理、冗余设计 |
| microservices.io | `DOC-MSIO-*` | Circuit Breaker、Saga、Transactional outbox、Health Check API、Idempotent Consumer |
| Polaris | `DOC-POLARIS-RELIABILITY` | 按 Reliability / Efficiency 两类分组，每条一个 key |
| kube-score | `DOC-KUBESCORE-CHECKS` | 按被检查对象（Pod / Deployment / StatefulSet / PDB / HPA）分组 |
| kube-linter | `DOC-KUBELINTER-CHECKS` | 一 check 一 template，带 Description + Remediation |
| Istio `istioctl analyze` | `DOC-ISTIO-ANALYZERS` | 按消息码 IST0xxx；**绝大多数是 schema 校验与引用有效性，对韧性机制贡献很小**，只有 IST0130/IST0131（路由规则不可达/无效匹配）沾边 |

## 2. 机制清单（本任务采用）

十组沿用任务给的划分，未新增组。下表中 **[新增]** 是起点清单里没有、从上面来源归纳出来的；**[细化]** 是起点清单已有条目下按来源拆出的子项，不单列为机制。

### 组 1 限制等待（防：等太久拖垮自己）
| 机制 | 出处 |
|---|---|
| 超时与截止时间 | 起点；`DOC-GRPC-DEADLINES`、`DOC-AWS-TIMEOUTS`、`DOC-SRE-CASCADING`「Latency and Deadlines」 |
| 截止时间沿调用链传递 | 起点；`DOC-GRPC-DEADLINES`、`DOC-SRE-CASCADING` |
| 取消传播 | 起点；`DOC-GRPC-CANCELLATION`、`DOC-GO-CONTEXT` |
| 请求对冲（hedging） | 起点；`DOC-GRPC-HEDGING` |

### 组 2 处理瞬时失败（防：一次失败变永久，或重试放大故障）
| 机制 | 出处 |
|---|---|
| 重试与退避抖动 | 起点；`DOC-AWS-TIMEOUTS`、`DOC-AZ-PATTERN-RETRY`、`DOC-AZ-TRANSIENT` |
| 重试预算 | 起点；`DOC-SRE-CASCADING`「Retries」、`DOC-GRPC-RETRY` |
| 幂等键 | 起点；`DOC-AWS-RETRY-IDEMPOTENT`、`DOC-MSIO-IDEMPOTENT` |
| 死信队列 | 起点；`DOC-RABBITMQ-DLX`、`DOC-PULSAR-MESSAGING` |

### 组 3 阻断传播（防：一个慢依赖拖垮全部路径）
| 机制 | 出处 |
|---|---|
| 熔断 | 起点；`DOC-AZ-PATTERN-CB`、`DOC-MSIO-CIRCUITBREAKER`、`DOC-R4J-CB` |
| 舱壁隔离（线程池/连接池/信号量） | 起点；`DOC-AZ-PATTERN-BULKHEAD`、`DOC-R4J-BULKHEAD`、`DOC-ENVOY-CB` |
| 异常实例剔除（outlier detection、被动健康检查） | 起点；`DOC-ENVOY-OUTLIER`、`DOC-LINKERD-CB`、`DOC-NGINX-UPSTREAM` |
| 连接池上限与空闲超时 | 起点；`DOC-HIKARICP`、`DOC-ENVOY-CB`、`DOC-SRE-OVERLOAD`「Load from Connections」 |

### 组 4 卸载过载（防：请求多到全体变慢）
| 机制 | 出处 |
|---|---|
| 准入控制与限流（服务端与客户端） | 起点；`DOC-AZ-PATTERN-THROTTLE`、`DOC-AZ-PATTERN-RATELIMIT`、`DOC-SENTINEL-FLOW`；**[细化] 客户端节流** = `DOC-SRE-OVERLOAD`「Client-Side Throttling」；**[细化] 按调用方配额** = 同篇「Per-Customer Limits」 |
| 排队与背压 | 起点；**[细化] 有界队列与排队时长上限** = `DOC-SRE-CASCADING`「Queue Management」 |
| 负载削峰 | 起点；`DOC-AZ-PATTERN-QBLL` |
| 优先级与降级 | 起点；`DOC-SRE-CASCADING`「Load Shedding and Graceful Degradation」；**[细化] 请求分级 criticality** = `DOC-SRE-OVERLOAD`「Criticality」 |

### 组 5 保住容量（防：实例没了就没了）
| 机制 | 出处 |
|---|---|
| 副本数 | 起点；`DOC-KUBESCORE-CHECKS` deployment-replicas、`DOC-KUBELINTER-CHECKS` minimum-replicas、`DOC-POLARIS-RELIABILITY` deploymentMissingReplicas |
| 中断预算 | 起点；`DOC-K8S-DISRUPTIONS`、`DOC-KUBELINTER-CHECKS` pdb-*；**[细化] unhealthyPodEvictionPolicy** = kube-linter `pdb-unhealthy-pod-eviction-policy` |
| 拓扑分散与反亲和 | 起点；`DOC-K8S-TOPOSPREAD`、`DOC-KUBELINTER-CHECKS` anti-affinity、`DOC-POLARIS-RELIABILITY` topologySpreadConstraint |
| 自动扩缩 | 起点；`DOC-K8S-HPA`、`DOC-KUBELINTER-CHECKS` hpa-minimum-replicas |
| 优先级与抢占 | 起点；`DOC-K8S-PRIORITY`、`DOC-POLARIS-RELIABILITY` priorityClassNotSet |
| **[新增] 冷启动容量与预热** | `DOC-SRE-CASCADING`「Slow Startup and Cold Caching」——实例数够了不等于容量够了，刚起的实例缓存冷、连接未建，扩容或重启后可能立刻二次塌陷。起点清单没有这一项 |

### 组 6 检测与恢复（防：坏了不知道、知道了不会好）
| 机制 | 出处 |
|---|---|
| 存活/就绪/启动探针 | 起点；`DOC-K8S-PROBES-TASK`、`DOC-K8S-POD-LIFECYCLE`、三个规则集都查 |
| 健康端点及其依赖范围 | 起点；`DOC-AWS-HEALTHCHECKS`、`DOC-AZ-PATTERN-HEM`、`DOC-MSIO-HEALTHCHECK`、`DOC-BOOT-ACTUATOR` |
| 故障后自愈（依赖恢复后能否回来） | 起点；`DOC-AZ-WAF-SELFPRES`、`DOC-R4J-CB`（half-open） |
| 服务发现的健康与会话 | 起点；`DOC-CONSUL-CHECKS`、`DOC-NACOS-*`、`DOC-ZK-PROGRAMMERS` |
| 负载均衡的健康剔除 | 起点；`DOC-ENVOY-HEALTHCHECK`、`DOC-NGINX-UPSTREAM` |
| **[新增] 连接保活与死连接探测（keepalive）** | `DOC-GRPC-KEEPALIVE` 是 gRPC 官方独立文档页；`DOC-SRE-OVERLOAD`「Load from Connections」。防的是"连接半开、对端已死但调用方不知道，请求挂到超时才失败"，起点清单的"连接池上限与空闲超时"不覆盖探测这一面 |
| **[新增] 容器重启策略** | `DOC-KUBELINTER-CHECKS` 的 `no-restart-policy` 检查明确把 restartPolicy 归为容错项；`DOC-K8S-POD-LIFECYCLE` |

### 组 7 资源边界（防：互相挤占、被 OOM 杀）
| 机制 | 出处 |
|---|---|
| 资源请求与限额 | 起点；`DOC-K8S-RESOURCES`、`DOC-K8S-QOS`、三个规则集都查 |
| 运行时内存设置与容器限额的关系 | 起点；`DOC-JAVA-LAUNCHER`（UseContainerSupport / MaxRAMPercentage） |
| 文件描述符与连接数上限 | 起点；`DOC-NGINX-*`、`DOC-SRE-OVERLOAD` |

### 组 8 状态与一致性（防：实例重建即丢数据、半途失败留脏数据）
| 机制 | 出处 |
|---|---|
| 持久卷 | 起点；`DOC-K8S-STORAGE-PV`、`DOC-K8S-STATEFULSET` |
| 补偿与 Saga | 起点；`DOC-AZ-PATTERN-SAGA`、`DOC-AZ-PATTERN-COMPENSATE`、`DOC-MSIO-SAGA` |
| 事务发件箱 | 起点；`DOC-MSIO-OUTBOX` |
| 幂等消费 | 起点；`DOC-MSIO-IDEMPOTENT`、`DOC-KAFKA-DOC`（enable.idempotence）、`DOC-ROCKETMQ-*` |

### 组 9 有状态组件的容错（防：主挂了没人接）
| 机制 | 出处 |
|---|---|
| 主备切换 | 起点；`DOC-RABBITMQ-QUORUM` |
| 法定人数 | 起点；`DOC-ETCD-FAQ`、`DOC-KAFKA-DOC`（min.insync.replicas） |
| 领导者选举的会话与租约 | 起点；`DOC-AZ-PATTERN-LEADER`、`DOC-ZK-PROGRAMMERS`（session/ephemeral）、`DOC-ETCD-API`（lease） |

### 组 10 降级（防：非关键依赖失败拖垮关键功能）
| 机制 | 出处 |
|---|---|
| 回退路径 | 起点；`DOC-AZ-PATTERN-CB`（fallback）、**与 `DOC-AWS-FALLBACK` 直接冲突**，见 `CHANGES.md` 待拍板项 |
| 缓存兜底 | 起点；`DOC-AZ-PATTERN-CACHEASIDE` |
| 功能开关 | 起点；`DOC-AZ-WAF-SELFPRES` |
| 错误裁剪 | 起点；`DOC-SRE-CASCADING`「Load Shedding and Graceful Degradation」 |

## 3. 明确不收的（含从规则集里遇到但排除的）

| 不收的东西 | 理由 |
|---|---|
| 优雅终止与连接排空 | 任务指定排除（"依赖恢复后组件回不来"仍留在组 6"故障后自愈"里） |
| 纯性能优化 | 任务指定排除 |
| 安全类机制（NetworkPolicy、securityContext、seccomp、privileged、只读根文件系统） | 任务指定排除；kube-score/kube-linter 里这类检查占比不小，一律不收 |
| 可观测性配置本身 | 任务指定排除（信号只作为规则 `checks.runtime.signals` 出现，不单独成规则） |
| 跨区域灾备与备份恢复 | 任务指定排除 |
| 滚动更新策略、maxSurge/maxUnavailable（kube-score `deployment-strategy`） | 只在发布过程中起作用，属发布过程而非故障模型内的韧性 |
| 镜像 tag、imagePullPolicy（Polaris `tagNotSpecified`/`pullPolicyNotAlways`、kube-score 同名检查） | 发布与供应链，不是局部故障下的行为 |
| 命名空间/标签规范、apiVersion 弃用、Service 类型（kube-score `label-values`/`stable-version`/`service-type`） | 配置卫生，与故障下行为无关 |
| Istio 分析器 IST0001–IST0149 的绝大多数 | schema 校验与引用有效性，不涉及故障下机制行为 |

## 4. 技术栈分层组件表

层次沿用任务给的 11 层，未增删层。下表由 `tools/gen_tables.py` 生成。"规则数"是 `rules.yaml` 里 `instantiations[].component` 命中该组件的规则条数。

<!-- BEGIN:components -->
| 层 | 层名 | 组件 | 文档数(成功/总) | 已有规则数 |
|---|---|---|---|---|
| 1 | 容器编排 | Kubernetes | 13/13 | 20 |
| 2 | 网关、代理与服务网格 | APISIX | 3/3 | 3 |
| 2 | 网关、代理与服务网格 | Envoy | 7/7 | 31 |
| 2 | 网关、代理与服务网格 | HAProxy | 1/1 | 1 |
| 2 | 网关、代理与服务网格 | Istio | 4/4 | 13 |
| 2 | 网关、代理与服务网格 | Kong | 1/1 | 2 |
| 2 | 网关、代理与服务网格 | Linkerd | 2/2 | 2 |
| 2 | 网关、代理与服务网格 | NGINX | 2/2 | 5 |
| 2 | 网关、代理与服务网格 | ingress-nginx | 1/1 | 1 |
| 3 | RPC 与 HTTP 框架 | .NET HttpClient | 2/2 | 2 |
| 3 | RPC 与 HTTP 框架 | Dubbo | 5/5 | 8 |
| 3 | RPC 与 HTTP 框架 | Go net/http | 1/1 | 3 |
| 3 | RPC 与 HTTP 框架 | Node.js http | 1/1 | 1 |
| 3 | RPC 与 HTTP 框架 | Python httpx | 1/1 | 1 |
| 3 | RPC 与 HTTP 框架 | Python requests | 1/1 | 1 |
| 3 | RPC 与 HTTP 框架 | Spring Boot | 1/1 | 8 |
| 3 | RPC 与 HTTP 框架 | gRPC | 7/7 | 18 |
| 4 | 容错库 | Failsafe | 3/3 | 4 |
| 4 | 容错库 | Hystrix | 1/1 | 1 |
| 4 | 容错库 | Polly | 4/4 | 8 |
| 4 | 容错库 | Resilience4j | 5/5 | 14 |
| 4 | 容错库 | Sentinel | 4/4 | 11 |
| 4 | 容错库 | Spring Cloud Alibaba | 1/1 | 1 |
| 4 | 容错库 | Spring Cloud CircuitBreaker | 1/1 | 1 |
| 4 | 容错库 | Spring Retry | 1/1 | 1 |
| 4 | 容错库 | gobreaker | 1/1 | 1 |
| 5 | 服务发现与配置中心 | Consul | 2/2 | 2 |
| 5 | 服务发现与配置中心 | Eureka | 1/1 | 2 |
| 5 | 服务发现与配置中心 | Nacos | 4/4 | 3 |
| 5 | 服务发现与配置中心 | ZooKeeper | 2/2 | 3 |
| 5 | 服务发现与配置中心 | etcd | 2/2 | 4 |
| 6 | 消息与流 | Kafka | 3/3 | 8 |
| 6 | 消息与流 | NATS | 1/1 | 1 |
| 6 | 消息与流 | Pulsar | 1/1 | 3 |
| 6 | 消息与流 | RabbitMQ | 4/4 | 8 |
| 6 | 消息与流 | RocketMQ | 2/2 | 7 |
| 7 | 数据存储客户端与连接池 | Go database/sql | 1/1 | 1 |
| 7 | 数据存储客户端与连接池 | HikariCP | 1/1 | 6 |
| 7 | 数据存储客户端与连接池 | Jedis | 1/1 | 1 |
| 7 | 数据存储客户端与连接池 | MongoDB driver | 1/1 | 1 |
| 7 | 数据存储客户端与连接池 | MySQL Connector/J | 1/1 | 2 |
| 7 | 数据存储客户端与连接池 | PostgreSQL JDBC | 1/1 | 1 |
| 7 | 数据存储客户端与连接池 | go-redis | 1/1 | 3 |
| 7 | 数据存储客户端与连接池 | ioredis | 1/1 | 1 |
| 7 | 数据存储客户端与连接池 | pgx | 1/1 | 1 |
| 8 | 语言运行时 | .NET | 1/1 | 1 |
| 8 | 语言运行时 | Go context | 1/1 | 5 |
| 8 | 语言运行时 | JVM | 1/1 | 3 |
| 8 | 语言运行时 | Java concurrency | 2/2 | 5 |
| 8 | 语言运行时 | Node.js | 2/2 | 2 |
| 8 | 语言运行时 | Python asyncio | 1/1 | 1 |
| 9 | 健康端点与应用框架 | Spring Boot Actuator | 2/2 | 7 |
| 10 | 跨层准则与模式目录 | AWS Builders Library | 5/5 | n/a（准则层，不作为 instantiations 的组件） |
| 10 | 跨层准则与模式目录 | AWS Well-Architected | 1/1 | n/a（准则层，不作为 instantiations 的组件） |
| 10 | 跨层准则与模式目录 | Azure Architecture Center | 13/13 | n/a（准则层，不作为 instantiations 的组件） |
| 10 | 跨层准则与模式目录 | Azure Well-Architected | 4/4 | n/a（准则层，不作为 instantiations 的组件） |
| 10 | 跨层准则与模式目录 | ChaosBlade | 1/1 | n/a（准则层，不作为 instantiations 的组件） |
| 10 | 跨层准则与模式目录 | Google Cloud Architecture Framework | 1/1 | n/a（准则层，不作为 instantiations 的组件） |
| 10 | 跨层准则与模式目录 | Google SRE | 4/4 | n/a（准则层，不作为 instantiations 的组件） |
| 10 | 跨层准则与模式目录 | microservices.io | 6/6 | 1 |
| 11 | 已有的检查规则集 | Istio | 1/1 | 13 |
| 11 | 已有的检查规则集 | Kubernetes | 1/1 | 20 |
| 11 | 已有的检查规则集 | Polaris | 2/2 | 7 |
| 11 | 已有的检查规则集 | kube-linter | 1/1 | 9 |
| 11 | 已有的检查规则集 | kube-score | 1/1 | 7 |
<!-- END:components -->
