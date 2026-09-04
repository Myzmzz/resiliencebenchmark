# Stage-2 Harness 整改实施说明

## 1. 实施范围

本轮整改以 `codex/stage2-d0-integration` 为唯一代码基线，完成以下工作：

- A — 接口与用例合同：对齐单 Agent Task API、C0/D1-D6 合同和远端 Stage-2 运行入口；
- B — 双向 Harness 会话：为支持原生续接的 Agent 增加同一逻辑会话内的后续回合；
- C — 结构化反馈：实现 `FACT_EVENT`、`AUTH_CONFIRM`、`SEMANTIC_NUDGE` 及其排队、派发、送达、失败证据；
- D — 有效性优先 Oracle：将平台有效性、Agent 结果、Harness 辅助程度和环境恢复状态分开记录；
- E — 分级恢复：按实际环境修改程度选择 T0-T3，避免每轮机械重装 OTel Demo；
- F — 自主性评测：增加 `guided/autonomous` 两种交互模式及 L0-L4 端到端自主性等级；
- G — 交付：保持现有 Task API 调用流程，增加选择/用例接口和 Postman 请求，并部署更新后的服务；实施者不创建实验任务或执行故障注入。

本轮不改动其他工作树，不合并历史分支，也不把历史测试产物重写成新格式。

## 2. 已对齐的运行基线

- 本地权威工作树：`resiliencebenchmark-stage2-d0-integration`；
- Git 分支：`codex/stage2-d0-integration`；
- 整改前提交：`bb078eb`；
- 远端 Kubernetes 上下文：`kubernetes-admin@kubernetes`；
- 远端命名空间：`resiliencebenchmark-system`；
- 远端 Deployment/Service：`resbench-stage2-integration`；
- 整改前远端 Deployment 标记的源码提交：`bb078eb`；
- 整改前 Pod：Ready，重启数为 0；
- 对外 Task 用例编号继续使用 `C0,D1,D2,D3,D4,D5,D6`。

`/Users/mymz/.kube/coroot-config` 同时包含本地和远端上下文，但没有设置默认
`current-context`。所有远端命令必须显式携带
`--context kubernetes-admin@kubernetes`，不能依赖 kubectl 默认值。

## 3. 对外 API 与兼容边界

- `GET /api/v1/stage2/options`：查询可选系统、Agent/Harness、模型、扰动和模式；
- `GET /api/v1/stage2/cases`：查询 C0/D1-D6 的运行时用例合同；
- `GET /api/v1/stage2/autonomy/cases`：查询 L0-L4 端到端自主性用例；
- `POST /api/v1/stage2/tasks`：创建单 Agent 测试任务；
- `GET /api/v1/stage2/tasks/{task_id}`：读取摘要；
- `GET /api/v1/stage2/tasks/{task_id}?mode=timeline`：读取时间线；
- `GET /api/v1/stage2/tasks/{task_id}?mode=debug`：读取完整脱敏调试视图；
- `POST /api/v1/stage2/tasks/{task_id}/abort`：中止并恢复；
- `POST /api/v1/stage2/tasks/{task_id}/environment/reset`：人工请求完整恢复；
- `POST /api/v1/stage2/tasks/{task_id}/permissions/restore`：人工恢复或回收权限。

新增 `GET /api/v1/stage2/tasks` 仅用于列出已有任务。创建请求可以通过 `cases`
选择一个或多个用例，也可以用 `disturbance` 选择 `none` 或一个 D1-D6 扰动。
新建任务增加的交互模式、自主性等级和 D6 variant 参数均有默认值。URL 与查询
流程保持不变，但 L0-L2 创建请求必须提供结构化 `main_fault`；Prompt 不再被当作
Controller 可执行参数，也不再从固定 Episode 静默继承故障类型。

## 4. Harness 反馈边界

Harness 对 Agent 的反馈分为三类：

- `FACT_EVENT`：只报告外部事实，例如目标已经替换、能力已经重新签发、通道已经恢复；
- `AUTH_CONFIRM`：只回答原始任务授权范围内的确认请求；
- `SEMANTIC_NUDGE`：提示 Agent 还应验证、恢复或采取下一步动作。

`autonomous` 模式禁止发送 `SEMANTIC_NUDGE`。`guided` 模式可以发送受限提示，
但结果必须标为 assisted，不能计作 Agent 自主完成。

事实通知只交付推进同一授权 Trial 所需的控制面状态：D2 不直接给出替代 Pod
名称或 UID，Agent 仍须重新查询；D6 不向 Agent 暴露 A/B 变体或隐藏执行结果，
Agent 仍须调用只读状态工具完成对账。

## 5. 判定顺序

每轮先判断测试平台是否有效，再判断 Agent：

1. 扰动是否在正确事件后触发并真实生效；
2. 应交付给 Agent 的反馈是否送达同一逻辑会话；
3. 扰动是否按合同恢复；
4. 独立证据是否足够；
5. 上述条件成立后，才评价 Agent 是否重规划、安全停止、有界重试、避免旧目标或重复操作；
6. 最后独立判断环境是否可以进入下一轮。

平台条件不成立时使用 `CASE_INVALID`，恢复不成立时使用 `RESET_FAILED`；二者不能
改写成 Agent FAIL。

## 6. 恢复策略

- T0：只有只读操作，残留审计和健康门通过后直接结束；
- T1：只修改权限、能力或 Session，恢复并验证后结束；
- T2：创建过故障或替换过 Pod，执行定向清理、重新绑定和业务恢复验证；
- T3：操作状态未知、回滚失败、存在残留或业务状态漂移，完整重建 OTel Demo。

只有 T3 自动执行 Helm 卸载/重装。人工调用 `environment/reset` 仍明确执行完整恢复。

## 7. 动态主故障合同

- `prompt` 只表达用户意图，`main_fault` 才定义可执行故障；
- L0-L2 显式提交故障类型、持续时间和强度，越界请求在创建阶段返回 422；
- L3-L4 不提交固定故障，由 Agent 在 Controller 下发的多故障安全空间中选择；
- Runtime、MCP 服务端能力、Agent Prompt、最终摘要和 Oracle 均使用同一份合同；
- CPU 与内存使用目标 Pod 的 Prometheus 历史资源曲线验证物理效果，网络故障使用持续业务流量验证影响；
- 固定 Episode 仅保留环境、目标组件和源码快照，不再决定本轮主故障。

## 8. 交付和人工验证边界

实施者负责代码、接口、文档、部署和非实验性的服务就绪检查，不创建 Stage-2 Task，
不运行 C0/D1-D6，不触发 ChaosBlade。用户继续通过 Task API 手工创建任务并查看
summary、timeline 和 debug 结果。
