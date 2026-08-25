# 实验设计决策：BladeAI 模型实验与原生 Agent 对比双轨制

- 状态：已接受（Accepted）
- 生效日期：2026-08-23
- 适用范围：Resilience Benchmark 的 Harness、模型和 Agent 对比实验
- 决策性质：实验主线约束；后续实验矩阵、执行脚本和报告必须遵守

## 核心决策

现阶段不抽离并强行共享 BladeAI 的核心 Harness。Benchmark 保留两条相互独立、结论不可混用的实验轨道：

1. **轨道 A：BladeAI 内的模型对比。** 固定 BladeAI 的 Agent Scaffold、版本、Prompt/Skill、工具、权限、环境、预算和评价方法，只改变底层模型，用于判断不同模型在同一 BladeAI Harness 中的表现差异。
2. **轨道 B：原生 Agent 系统对比。** BladeAI、Codex、Claude Code、DeepSeek Harness 等保留各自原生 Harness；Benchmark 只统一外部任务、环境、可见证据、动作预算、安全控制和独立评价，用于比较完整 Agent 系统完成韧性测试闭环的能力。

两条轨道回答不同问题。轨道 A 回答“在 BladeAI 中哪个模型更合适”；轨道 B 回答“哪个原生 Agent 系统更能完成任务”。轨道 A 的结果不能用于证明某模型的原生 Agent 产品能力，轨道 B 的结果也不能直接归因于底层模型。

## 为什么采用双轨制

一次实验的真实被测单元是：

```text
Agent Scaffold × Model × Prompt/Strategy × Tools/RBAC × Environment
```

如果比较模型时同时更换 Harness，就无法判断差异来自模型还是 Agent 编排；如果为了“公平”把 BladeAI 的 Graph、Prompt、安全门和恢复流程套到 Codex 或 Claude Code 上，被测对象又会变成“BladeAI Scaffold + 另一模型/推理后端”，不再是原生 Codex 或 Claude Code。双轨制通过分别固定 Scaffold 和保留原生 Scaffold，解决这两个不同的因果归因问题。

## 轨道 A：BladeAI 内的模型对比

### 被测对象

```text
BladeAI 固定配置 × 候选模型
```

BladeAI Harness 是控制变量，模型是主要自变量。每轮实验必须固定：

- BladeAI 版本、源码提交或制品摘要；
- Inject/Recover Graph、Prompt、Skill 和知识文件版本；
- 工具集合、MCP 接口、RBAC 和网络权限；
- Episode、环境快照、工作负载、故障预算和停止条件；
- 上下文窗口、推理强度、温度、重试和超时策略；
- Controller、独立 Oracle、评分规则和证据格式。

只有模型标识及模型必需、且被记录的协议兼容参数可以变化。模型在进入矩阵前必须完成流式输出、结构化输出、工具调用、错误恢复和长上下文等能力资格检查。

### 可以得出的结论

- 某模型在固定 BladeAI Scaffold 中的任务成功率、安全性、诊断能力和效率；
- BladeAI 对不同模型的适配性和敏感性；
- 模型变化对规划、工具使用、验证和恢复等阶段的影响。

### 不能得出的结论

- 某模型对应的原生 Agent 产品一定优于另一原生 Agent；
- BladeAI Harness 的能力就是模型自身能力；
- 仅凭一次成功故障命令即可证明模型完成了韧性测试闭环。

## 轨道 B：原生 Agent 系统对比

### 被测对象

```text
原生 Agent Harness × 其冻结的完整运行配置
```

BladeAI、Codex、Claude Code、DeepSeek Harness 等必须保留各自原生工作流、Prompt 组织、上下文管理、工具调度和恢复策略。Benchmark 不统一内部推理循环，只统一外部实验契约：

- 同一版本的 Agent 可见 Episode；
- 同一环境快照、工作负载和观测窗口；
- 语义等价的 Kubernetes、Telemetry、Source 和 Chaos Control 能力；
- 相同的动作范围、实验次数、时间预算和安全停止条件；
- 相同的隐藏 Ground Truth 和独立 Oracle；
- 相同的结果 Schema、轨迹要求和评价门禁。

若多个原生 Agent 都原生支持同一模型和同一协议，可增加“共同模型子组”以降低模型差异；否则必须把结果表述为完整系统比较，不得拆解成模型优劣结论。

### 可以得出的结论

- 各原生 Agent 在真实任务中的端到端完成能力；
- Agent Scaffold、模型、Prompt、工具权限和运行机制共同形成的系统效果；
- 不同 Agent 在安全、实验选择、证据分析、恢复和资源效率上的行为差异。

### 不能得出的结论

- 在 Harness、模型或权限不同的情况下，把结果差异单独归因于模型；
- 用 BladeAI 专有流程替代其他 Agent 的原生流程后，仍称其为“原生 Agent 对比”；
- 把 Agent 自己声称成功当作独立 Oracle 结论。

## 两条轨道共享与不共享的内容

| 对象 | 是否共享 | 规则 |
| --- | --- | --- |
| Episode 与 Agent 可见材料 | 共享 | 内容、版本和泄漏边界一致 |
| 环境、工作负载和基线窗口 | 共享 | 每轮执行前恢复到可验证基线 |
| MCP/外部工具语义 | 共享 | 能力和权限等价，传输和客户端实现可不同 |
| Controller、安全预算与兜底清理 | 共享 | 独立于被测 Agent，Agent 失联后仍可收敛 |
| Ground Truth、Oracle 与评分 | 共享 | 对 Agent 隐藏，使用独立证据判定 |
| 结果与轨迹 Schema | 共享 | 支持统一取证、回放和审计 |
| BladeAI Graph、Prompt、Skill 编排 | 仅轨道 A 共享 | 轨道 B 中不得套给其他原生 Agent |
| 各 Agent 的内部记忆和会话状态 | 不共享 | 每轮隔离，禁止跨 Agent 污染 |
| 各 Agent 的原生 Harness | 轨道 B 不共享 | 保留原生行为，只通过适配器接入外部契约 |

可以继续抽取和复用中性的基础设施，例如 MCP 服务、Episode/Result/Trace 契约、脱敏、隔离、超时和 Artifact 管理；当前不抽取 BladeAI 的领域 Graph、状态、安全语义、Prompt 和恢复逻辑作为所有 Agent 的公共 Harness。

## 实验执行顺序

正式实验按以下顺序推进：

1. **冻结共同基础。** 固定 Episode、环境制品、源码和镜像摘要、工作负载、Oracle、预算、工具权限及评分版本。
2. **完成资格检查。** 分别验证环境基线、模型协议、各 Harness 工具轨迹、权限边界、超时和清理能力；未通过者不得进入正式评分。
3. **先运行轨道 A。** 使用完整生命周期正向控制与最小完整意图对照，确认 BladeAI Harness 和各候选模型能稳定完成基本闭环，再执行模型矩阵。
4. **再运行轨道 B。** 在同一批 Episode 上运行各原生 Agent，每个 Agent 使用冻结且可复现的完整配置。
5. **独立评价。** Evaluator 分别检查 Episode 有效性、安全、故障效果、SLO、因果机制、诊断和恢复，不接受 Agent 自证替代外部证据。
6. **分轨报告。** 两条轨道分别形成结果表、失败分析和结论，不合并成一个无条件总排行榜。

每次 Trial 结束后必须完成清理和恢复验证，确认环境回到基线后才能开始下一次 Trial；不能验证恢复时，应停止后续实验并记录为证据或环境问题。

## 结果解释与报告规则

报告必须明确标记 `Track A` 或 `Track B`，并记录 Agent/Harness、模型、Prompt/Strategy、工具权限、环境和版本摘要。主结果继续使用独立门禁：Episode 有效性、安全、故障效果、SLO 违反、因果解释、诊断和恢复；效率与诊断细分分数只能解释差异，不能抵消安全失败或错误结论。

以下结果不得进入正式比较：

- Harness、模型、Prompt 或权限在实验中途发生未记录变化；
- 缺少完整工具轨迹或关键 Oracle 证据；
- Ground Truth、标准答案或评分信息泄漏给 Agent；
- Trial 前基线不健康、上一轮故障未清理或环境不可复现；
- Harness 未完成该运行模式的资格检查；
- Episode 条件不成立。此时应标记 `CASE_INVALID`，不能计为 Agent 的通过或失败。

## 完成判据

该决策在实现层面完成，需要满足：

- 实验清单中的每个 Trial 都显式声明轨道；
- 轨道 A 能证明除模型外的关键变量已冻结；
- 轨道 B 能证明使用的是各 Agent 原生 Harness，而非 BladeAI Harness 的替换版本；
- 两条轨道使用同一套独立 Oracle 门禁并分别报告；
- Artifact 能还原每次 Trial 的完整配置、工具轨迹、外部证据和清理结果；
- 任何第三方都能根据文档判断某个结论究竟属于“模型在 BladeAI 中的表现”还是“原生 Agent 系统表现”。

## 何时重新考虑抽离公共 Harness

只有在两条轨道完成至少一轮可复现实验，并证明重复代码已成为主要维护成本后，才重新评估抽离公共 Harness。抽离方案必须同时证明：不会改变原生 Agent 行为、不会弱化 Controller/Oracle 独立性、不会把 BladeAI 领域状态泄漏到其他 Agent，并通过相同 Episode 的前后兼容回归。未满足这些条件前，本决策保持有效。
