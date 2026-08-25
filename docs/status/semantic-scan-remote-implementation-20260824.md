# 语义扫描远程实现状态

> 更新：2026-08-25 CST
>
> 执行边界：`1.94.151.57` / `tcse-v100-03` 与测试 Kubernetes 集群

## 核心判断

语义缺陷扫描、实时 Kubernetes 证据、Agent 驱动 CodeGraph、模板匹配、ChaosBlade 能力门和按 Finding 生成 Episode 的工程链路已经在测试集群完成一次真实闭环。最终 v8 运行覆盖12个模板、16个Finding和全部Verifier，无`scan_failed`；15个Episode-eligible候选全部物化，`one_to_one_verified=true`。

## 已实现

- 首次可从 HTTPS Git 地址 clone 源码；后续优先使用带revision、commit和SHA256清单的节点缓存。缓存不完整、身份不符或摘要不符时直接失败，不静默接受。固定源码为`2.2.0@b74a7bc7bbe66099c61951f42b24dab8b6f02d18`。
- Controller只负责建索引；Coordinator、12个模板 Agent 和每个 Finding 的 Verifier 自主调用 CodeGraph 与 Kubernetes 只读工具。每个 Agent/Verifier 的工具上限为100次，不代表固定调用100次。
- Kubernetes证据直接来自 API Server，覆盖 workload、Pod、Service、EndpointSlice、ConfigMap正文、command/args、非敏感env、Probe、UID、resourceVersion、owner refs与imageID；Secret值不可由扫描 ServiceAccount 读取。
- `confidence_claim`和未解决替代解释均不再作为出题否决门。候选区分为 confirmed、plausible、unactionable，并保留残余假设。
- 仅实时验证为可用的 ChaosBlade 故障能够生成 Episode：`network-delay`、`network-loss`、`cpu-load`、`memory-load`、`pod-delete`、`pod-fail`。其他候选保留但不出题。
- Episode一一统计按“可行动且存在已验证执行器”的候选计算；运行时漂移 Episode 会物化，但标记 `execution_qualified=false`。
- 模板分析和 Verifier 支持最多3路受控并发；CodeGraph CLI串行访问索引，相同只读查询使用线程安全缓存。100次工具预算保持不变。
- 模型调用超时从120秒提高到600秒；provider结构化输出为空时自动切换 ToolStrategy；阶段事件实时输出到Pod日志。
- 用量审计发现旧运行单轮输入约770万至790万token；v6取消Coordinator工具调用，把单次工具结果严格限制为8000字符，并将12–20次设为正常调查范围，以减少工具历史反复重放。每Agent的100次硬上限保持不变。

## 真实运行证据

### 首轮完整顺序运行

Run：`semantic-20260824t14411787582480z-66205fd1`

- 源码身份：exact。
- Kubernetes：115个资源，EndpointSlice 21个且完整，无 unavailable 类别。
- CodeGraph：307个文件、8597个节点、12406条边、15种语言。
- ChaosBlade：6类故障均为实时 verified。
- 扫描：10个候选，9个 Episode-eligible。
- 修复运行时绑定和 entrypoints 上限后：9个 Episode 全部物化，`one_to_one_verified=true`；其中6个可执行、3个因运行镜像漂移待重新资格化。
- 未完成项：RD-06、RD-10、RD-12 在该轮因模型输出词表不合法被记为 scan_failed；后续 v4 已证明三类均可成功扫描。

### v4 三路并发运行

Run：`semantic-20260824t15511787586718z-d401492a`

- 前置资格化再次通过；Kubernetes读取116个资源。
- Coordinator约1分40秒；12模板分析约8分40秒，相比顺序运行约32分钟明显下降。
- RD-06、RD-10、RD-12 均成功形成候选；RD-01 的 provider结构化响应为空，已在 v5 修复自动fallback。
- 15个 Finding 进入 Verifier；前8个完成，后7个被中转站以 `insufficient_quota / API key 额度已用完`拒绝。
- 本轮只形成部分结果：8个match、5个Episode-eligible、5个Episode物化。该结果保留为失败证据，不作为最终12模板结论。
- v4 的 tool trace actor 使用 thread-local，在框架内部工具线程中丢失；v5/v6 已改为 ContextVar，仍需真实模型运行复验。

### v8 最终完整运行

Run：`semantic-20260824t23231787613823z-df911ece`

- 总墙钟时间约21分钟；源码缓存校验和解包约0.3秒。
- 源码身份exact；CodeGraph为307个文件、8597个节点、12406条边、15种语言。
- Kubernetes快照无unavailable类别；累积Event较多，但核心workload、Pod、Service、EndpointSlice和ConfigMap均完整。
- 12模板全部完成：11类形成风险候选，RD-02为`not_found`；无`scan_failed`。
- 16个Finding全部完成独立Verifier；15个question-eligible。
- 15个Episode全部物化：12个`generated`、3个`runtime_drift`；另1个RD-11候选因不可行动保留但不出题。
- 15个Episode的主故障全部属于已实时验证的ChaosBlade能力，持续时间不少于600秒；公开题面无缺陷依据、Pod UID或命令泄漏。
- 工具轨迹921条，Actor缺失0；单Actor最大62次，未超过100次硬上限。
- 本轮输入token为7,835,070，总token为7,944,814。8000字符边界和无工具Coordinator没有带来预期的总token降幅；这是后续性能优化项，不影响本次功能闭环结论。

## 当前部署

- 宿主服务：`resbench-semantic-scan.service`，仅监听`127.0.0.1:18085`，用户`resbench-scan`。
- Kubernetes：namespace `resbench-system`，ServiceAccount `resbench-semantic-scan`；可读`otel-demo`证据，不可读Secret、不可创建业务Deployment。
- 最终镜像由本地Mac以`linux/amd64`构建并推送，远端Ubuntu不执行Docker build：
  `1.94.151.57:85/observe/resbench-semantic-scan:otel-2.2.0-semantic-v8@sha256:34927e2d1f92efa4623fff565265fd64d30e0aaa6d90d7616a8e634f13b4ecd3`
- 远端测试：19项通过、1项按环境条件跳过；任务范围 Ruff 通过。

## 后续优化项

功能闭环已经完成。下一步应独立优化模型上下文累计策略，目标是在不降低入口、语言、Kubernetes字段和反证覆盖率的前提下降低工具历史重放token；该性能工作不应改写本次v8的真实结果。
