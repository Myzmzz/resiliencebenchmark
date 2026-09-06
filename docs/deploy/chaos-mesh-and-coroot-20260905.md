# Chaos Mesh / Coroot 环境资格

记录延续到 UTC 2026-09-06。范围仅旧集群 `coroot-config`、context
`kubernetes-admin@kubernetes`；新集群没有部署或测试。

## 部署与权限

Chaos Mesh固定官方Chart2.7.3。Controller1/1、daemon3/3实际就绪；
Controller位于tcse-v100-03，daemon覆盖三个旧测试节点。运行镜像为
`observe/chaos-mesh:v2.7.3-stage2-20260905`和
`observe/chaos-daemon:v2.7.3-stage2-20260905`，不可变引用记录在
`deploy/chaos-mesh/values-old-cluster.yaml`。Dashboard/DNS/BPF组件关闭。

已核实以下对象，详细权限见`deploy/stage2/execution-identities.yaml`：

- `resiliencebenchmark-system`的Controller、executor、finalizer三个独立SA。
- `otel-demo`的`resbench-stage2-executor` Role/Binding负责创建故障与Pod UID围栏；
  `resbench-stage2-finalizer` Role/Binding负责读取、删除/清理，不授予create。
- Controller的cluster-control、runtime、otel-demo-control及liveness授权。
- Mesh启动补充`resbench-chaos-mesh-runtime-read` ClusterRole/Binding，
  `chaos-mesh`中的`resbench-chaos-mesh-controller-events` Role/Binding。

80项实际身份和授权检查通过。当前设计没有可用于注入的“Agent SA”：被测进程
没有SA挂载或kubeconfig。不能通过捏造一个不存在的SA并得到`can-i=no`来替代
真实边界；实际UID10002凭据/出网检查已单列通过。

## NetworkChaos金丝雀：真实注入和恢复

本项不是dry-run，不调用智能体模型，不等于D8或四家接入资格。
运行ID：`resbench-mesh-canary-20260906-a1`。
新建两个仅用于本次检查的临时Pod，均在otel-demo/tcse-v100-03，未挂SA令牌。
只对source Pod `9734b453-6db5-4200-a54d-d386ca204fae`注入1000ms出方向延迟，
最长90秒，效果确认后提前清理。使用生产后端的manifest渲染及UID围栏约束，
创建和删除分别以executor/finalizer身份执行；不是完整MCP创建链路。

| 阶段 | HTTP请求 | 延迟中位数 |
| --- | --- | --- |
| 注入前 | 5/5成功 | 1.022ms |
| 注入中 | 3/3成功 | 2001.251ms |
| 删除故障后 | 5/5成功 | 0.913ms |

配置为每次出方向1000ms，不要求完整HTTP事务只增加1000ms；连接与请求涉及
多个数据包，实际测得约2000ms。原始采样（包括每阶段首次请求较慢值）全部保留。

初始检查器因发现空的PodNetworkChaos对象将整项记为failed，原结果未覆盖。
核实该对象`spec={}`、`observedGeneration=metadata.generation=2`，属于Pod拥有的
已清空状态缓存，不是存活故障规则。清理临时Pod后该对象被正常回收；
04:28:05 UTC再次确认五类故障资源清单为空、两个临时Pod均不存在。
最终复核的引擎金丝雀结果为通过；清单、原始失败与复核依据分别保存。

证据根：`artifacts/remediation/20260905/chaos-mesh-canary-a1/`：
`state.json`、`fault-manifest.json`、`fault-latest.json`、`result.json`、
`internal-after-delete.json`、`post-cleanup.json`、`final-adjudication.json`。
临时Pod已删除，清单可重建；cart/accounting等业务Pod和OTel values未修改。

## 实跑暴露并修复的代码问题

Chaos Mesh返回`desiredPhase=Run`，同时以AllInjected条件和containerRecords
报告实际注入；旧解析器却把期望值直接作为phase，导致共享核心无法得到Running。
修复后依据实际条件、执行计数、目标记录与恢复状态归一化，期望Run本身不构成
运行事实。真实状态夹具及负例回归已入库，仍不能当作其他故障类型实跑。

## Coroot与未完成项

Coroot复用旧实例，目前仍为匿名Admin。改为独立Viewer及其运行配置待用户确认，
没有使用匿名Admin冒充只读资格。历史原生API格式检查不等于Viewer权限资格。
尚需Coroot三类查询资格、四家原生通道资格、执行器完整MCP金丝雀、八个D0与68格。
NetworkChaos本次结果不能替代StressChaos、PodChaos或正式Agent扰动结论。
