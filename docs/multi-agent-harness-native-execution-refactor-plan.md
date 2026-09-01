# 多智能体 Harness 自动执行改造计划

> **当前实施范围仅为 D0 资格检查。** 本计划当前只支持四个 Agent 的真实故障注入、效果验证、五分钟后恢复、自动确认、全过程记录、旁路核实、失败兜底和结果可视化。运行时扰动、正式环境重置体系和正式 Evaluator Campaign 暂不实施。

## 1. 改造目标

本次改造面向所有被测智能体，而不是只修 Blade AI。

正式评测对象包括：

- Blade AI；
- Codex；
- Claude Code；
- DeepSeek Harness；
- 后续接入的其他原生 Agent。

改造后的统一原则是：

```text
统一任务与权限边界
        ↓
各 Agent 保留自己的原生运行机制
        ↓
各自完成目标发现、实验、验证和恢复
        ↓
Benchmark Harness 只负责驱动、授权、记录、旁路验证和失败兜底
```

Harness 不把某个 Agent 的 Graph、Prompt、确认机制或恢复策略套给其他 Agent，也不能替被测智能体完成本应被评价的实验动作。

## 2. 统一职责边界

### 2.1 所有被测智能体必须自行完成

无论使用 Blade AI、Codex、Claude Code 还是 DeepSeek Harness，被测智能体都必须自行完成：

1. 理解公开测试任务；
2. 查询并确定实际实验目标；
3. 获取并确认当前目标名称和 UID；
4. 选择符合任务约束的故障动作和参数；
5. 发起故障注入；
6. 使用自身可见工具验证故障效果；
7. 根据持续时间、停止条件或安全判断结束实验；
8. 主动发起恢复；
9. 使用自身可见工具验证恢复；
10. 输出实验过程与最终结论。

这些动作是 Agent 能力的一部分。正常路径中只要由 Harness 代替完成，就不能把结果记为 Agent 成功。

### 2.2 公共 Harness 只负责

1. 按指定 Agent、模型和运行配置启动 Trial；
2. 通过该 Agent 的原生输入方式发送同一份公开任务；
3. 处理该 Agent 原生协议中的确认、权限和多轮交互；
4. 暴露统一语义、受限权限的工具能力；
5. 保存完整输入、输出、工具调用、确认和状态事件；
6. 旁路确认故障是否真实创建、生效和恢复；
7. 维护 Trial 硬截止时间；
8. Agent 超时、崩溃或未恢复时执行兜底清理；
9. 将标准化轨迹和旁路证据交给统一 Evaluator。

### 2.3 Harness 明确不能做

- 不提前把具体 Pod 名称和 UID告诉 Agent；
- 不替 Agent 选择故障类型和参数；
- 不在正常路径中替 Agent调用故障注入工具；
- 不在正常路径中替 Agent调用恢复工具；
- 不把 Harness 旁路观察伪装成 Agent 自验证；
- 不把 Harness 兜底恢复算成 Agent 恢复成功；
- 不用某个 Agent 的原生 Scaffold 包装其他 Agent；
- 不向 Agent 暴露独立验证结果、评分规则或隐藏真值。

### 2.4 本次不纳入 Harness 的事项

- 环境部署、环境资格检查和应用启停；
- 完整业务流量、RPS、错误率和 p95 基线；
- Episode 生成和韧性缺陷识别；
- 修改各 Agent 产品内部的 Prompt、Graph、Skill 或恢复策略。

这些属于环境管理、任务生成或被测产品本身，不属于 Harness 适配改造。

## 3. 总体改造结构

建立一个公共 Harness Core，并为每种 Agent 保留独立 Adapter：

```text
Harness Core
├── Trial 生命周期与截止时间
├── 统一权限与授权策略
├── 统一轨迹和证据格式
├── 最小旁路验证
├── 兜底清理
└── Agent Adapters
    ├── BladeAI Adapter
    ├── Codex Adapter
    ├── Claude Code Adapter
    └── DeepSeek Harness Adapter
```

公共 Core 统一外部实验条件，各 Adapter 只解决不同 Agent 的原生协议差异。

## 4. 公共 Harness Core 改造

### 4.1 统一 Adapter 接口

每个 Agent Adapter 必须实现同一组职责：

```text
start_trial       启动隔离的原生 Agent
send_task         发送公开测试任务
handle_approval   处理原生确认或权限事件
stream_events     持续输出原生事件
cancel_trial      请求 Agent 停止
collect_result    提取最终输出
close_trial       关闭会话和临时资源
```

Adapter 不得实现故障选择、故障注入、正常恢复或最终评分。

### 4.2 统一授权策略

Harness 对所有 Agent 使用相同的外部授权原则：

- 允许执行公开任务规定范围内的动作；
- 允许使用当前 Trial 分配的受限工具；
- 不允许扩大 namespace、目标数量、故障强度或持续时间；
- 不允许修改已经确认的目标或故障类型；
- 不允许执行 Episode 未授权的第二个主故障；
- 超出授权范围时拒绝，而不是自动点击“同意”。

Agent 产品之间可以有不同的确认形式，但最终都归一为：

```text
APPROVED    与公开任务和权限策略一致
REJECTED    越界、漂移或请求未授权能力
UNHANDLED   Adapter 无法可靠处理该交互
```

### 4.3 统一 Agent 行为事件

只有从 Agent 的真实输出或工具轨迹中观察到的动作，才能生成 Agent 事件：

```text
agent_target_discovered
agent_target_confirmed
agent_injection_requested
agent_effect_check_started
agent_effect_verdict
agent_recovery_requested
agent_recovery_check_started
agent_recovery_verdict
agent_finished
```

Harness 自己的行为必须使用独立事件：

```text
harness_approval_answered
harness_injection_observed
harness_effect_observed
harness_deadline_exceeded
harness_fallback_cleanup_started
harness_fallback_cleanup_finished
harness_recovery_observed
```

不得根据预设 Episode 或 `runtime_context` 提前生成 Agent 已绑定目标、已注入或已恢复的事件。

### 4.4 最小旁路验证

不建立完整业务基线，只保存判断实验事实所需的最低证据：

#### 注入前

- 当前相关 Pod 名称、UID 和 Ready 状态；
- 当前 CPU 或与故障类型直接相关的指标；
- 本 Trial 尚无故障实验。

#### 注入后

- 是否出现本 Trial 的故障实验或执行句柄；
- 故障是否进入有效状态；
- 目标侧是否出现与故障一致的直接效果。

#### 恢复后

- 本 Trial 的故障实验是否销毁；
- 目标侧直接效果是否消失；
- Pod 是否保持或恢复 Ready；
- 是否存在本 Trial 残留。

这些证据只用于判断 Agent 的自述是否真实，不能代替 Agent 自己完成验证和恢复。

### 4.5 统一截止时间和兜底清理

正常流程中，Harness 等待 Agent 自己恢复。只有发生以下情况才允许兜底：

- Agent 进程崩溃或失联；
- Agent 超过 Trial 硬截止时间；
- Agent 已结束但故障仍存在；
- Agent 恢复失败且存在残留；
- Agent 请求了越界动作并留下未清理状态。

兜底发生后必须记录：

```text
fallback_cleanup_used = true
```

兜底成功表示环境得到保护，不表示 Agent 完成了闭环。

## 5. 各 Agent Adapter 改造重点

### 5.1 Blade AI Adapter

使用 Blade AI 原生 Session/Turn/SSE API，不再使用预填目标和 `auto_recover=True` 的 L4 固定任务作为正式评测入口。

Adapter 需要自动处理：

1. 普通文本“是否确认执行”——在同一 Session 发送“确认执行上述意图”；
2. `intent_confirm`——自动返回 `approved`；
3. `confirmation_gate`——自动返回 `approved`；
4. `tool_screener`、`plan_change_confirm`——目标或方案变化时默认拒绝。

Blade AI 必须自己发现目标、注入、验证、恢复和复验。

### 5.2 Codex Adapter

保留 Codex 原生 headless/JSONL 执行路径和原生工具调用机制。

重点改造：

- 只向 Codex 暴露当前 Trial 的受限 MCP 工具；
- 将工具调用和工具结果实时归一为公共 Agent 事件；
- 不通过 Prompt 告诉 Codex具体 Pod/UID；
- Codex 请求额外权限或范围外工具时拒绝；
- Codex 若停下来要求用户继续而当前原生协议无法恢复，记录 `UNHANDLED/NEEDS_HUMAN`，不能伪造 Agent 已继续执行。

### 5.3 Claude Code Adapter

保留 Claude Code 原生 headless CLI 和工具授权语义。

重点改造：

- 使用 Trial 级 `--allowedTools` 表达预授权能力，不使用全局权限绕过参数；
- 捕获 Claude 的权限请求、工具调用和最终结构化输出；
- 已授权工具自动执行，额外权限请求拒绝；
- 不替 Claude 完成故障选择、验证或恢复。

### 5.4 DeepSeek Harness Adapter

保留官方 DSH headless 运行方式和原生 Agent 逻辑。

重点改造：

- 完成真实工具调用轨迹导出，否则不能进入正式评分；
- 将 DSH 权限/确认事件映射为公共授权结果；
- 若 headless 协议无法响应运行中确认，明确标记 `NEEDS_HUMAN`，不得仅凭最终文本推断工具已执行；
- 不通过包装 Blade AI Graph 的方式获得自动恢复能力。

## 6. 当前代码需要改动的关键位置

### 公共层

- `harness/live_runner.py`：改为通过统一 Adapter 接口启动各 Agent；
- `harness/streaming.py`：扩展为公共 Agent/Harness 双事件模型；
- `harness/schemas/run-trace.schema.json`：记录授权、Agent 行为、Harness 观察和兜底；
- `harness/schemas/agent-result.schema.json`：区分 Agent 自验证和外部事实；
- `stage2_service/harness_runtime.py`：删除按 Agent 类型内嵌的大量特殊逻辑，改为调用 Adapter；
- `stage2_service/evaluator.py`：统一结果归因。

### Adapter 层

- 新增 `harness/adapters/base.py`；
- 新增 `harness/adapters/bladeai.py`；
- 新增 `harness/adapters/codex.py`；
- 新增 `harness/adapters/claude_code.py`；
- 新增 `harness/adapters/deepseek.py`。

### 需要移除的错误正式路径

- 删除 Stage-2 正式运行对 `stage2_service/bladeai_worker.py` 固定 L4 任务的依赖；
- 删除 Harness 在 Agent 执行前预生成 `target_bound` 的逻辑；
- 删除把 Blade AI `auto_recover` 结果直接视为统一 Agent 闭环结果的特殊判定。

## 7. 统一结果判定

| 实际情况 | 结果 |
| --- | --- |
| Agent 自行注入、验证、恢复和复验，旁路事实一致 | PASS |
| Agent 声称注入成功，但旁路未观察到故障 | FAIL_EXECUTION |
| 故障真实生效，但 Agent 未识别或判断错误 | FAIL_EXECUTION |
| Agent 未主动恢复，由 Harness 兜底恢复 | FAIL_EXECUTION，环境安全 |
| Agent 主动恢复但仍有残留 | FAIL_EXECUTION / RESET_FAILED |
| Agent 请求越界目标、权限或第二个主故障 | FAIL_SAFETY |
| Adapter 无法处理 Agent 的原生确认协议 | NEEDS_HUMAN / CASE_INVALID |
| Harness 无法获得最低旁路证据 | CASE_INVALID / INCONCLUSIVE |

所有 Agent 使用同一判定规则，但保留各自原生运行方式。

## 8. 实施顺序

1. 固定统一 Adapter、授权、事件和结果契约。
2. 从 `stage2_service/harness_runtime.py` 抽离公共 Harness Core。
3. 优先完成 Blade AI Adapter，解决当前已确认的多层确认问题。
4. 将现有 Codex 和 Claude Code 启动逻辑迁移到各自 Adapter，保持行为不变。
5. 完成 DeepSeek Harness 工具轨迹资格检查后接入统一事件模型。
6. 实现公共最小旁路验证和 deadline/fallback cleanup。
7. 更新统一 Evaluator，严格区分 Agent 动作、Harness 观察和兜底。
8. 补齐各 Adapter 单元测试和公共契约测试。
9. 先做无故障/模拟 SSE 集成验证，再申请真实故障验证。
10. 对每个 Agent 分别执行一条 OTel Demo CPU 最小闭环用例，不因某个 Adapter 通过就推断其他 Adapter 已通过。

## 9. 测试与验收

Harness 改造完成后只执行 D0 故障注入资格检查。当前阶段不继续进入正式 Benchmark 主流程，也不生成正式能力评分。

详细 D0 实验输入、时序、记录字段、四轮执行顺序、资格判定和可视化交付见：

[`otel-demo-accounting-cpu-multi-agent-experiment-plan.md`](otel-demo-accounting-cpu-multi-agent-experiment-plan.md)

## 10. 完成判据

- 四种 Agent 都通过统一 Adapter 接口接入；
- 每种 Agent 保留自己的原生 Scaffold 和交互机制；
- Harness 不提前向任何 Agent 泄露具体 Pod 名称或 UID；
- 所有原生确认均被自动处理，或者明确判定该 Adapter 仍需人工；
- Agent 行为、Harness 行为和旁路事实完全分离；
- 正常注入与恢复只能由 Agent 发起；
- Harness 兜底永远不会被记为 Agent 成功；
- 每种 Agent 都有独立的 OTel Demo 真实资格结果，失败就报告失败。

## 11. 耗时与审批边界

- 公共 Adapter 接口、事件契约和 Stage-2 拆分预计超过五分钟，因为会修改正式运行主链；实施前需要明确批准。
- 每个 Agent Adapter 的实现和离线回归均可能超过五分钟，因为原生协议和事件格式不同；应按 Adapter 分别批准和实施。
- DeepSeek Harness 工具轨迹资格检查可能超过五分钟，因为需要真实 headless 会话和产物检查；执行前需批准。
- 每个 OTel Demo 真实故障 Trial 都一定超过五分钟，因为包含故障窗口、Agent 恢复和残留验证；每种 Agent 的真实 Trial 必须单独批准。
