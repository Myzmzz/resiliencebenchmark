# 多关卡顺序扰动系统实现状态

> **历史快照，已被更新状态取代。** 本文保留当时的离线实现评估；2026-08-23 的真实三关执行、评分和集群清理结果请以 [端到端控制系统状态](./end-to-end-control-system-status.md) 为准。

> 状态快照：2026-08-23
>
> 对应分支：`codex/multi-level-disturbance-20260823`
>
> 对应提交：`5b5f4759a9f2d7f0b29610ce8c41d42ed8a2fbd8`
>
> GitHub：Draft PR [#1](https://github.com/Myzmzz/resiliencebenchmark/pull/1)

## 核心判断

当前已经完成多关卡顺序扰动系统的 P0/P1 代码主体、数据契约和离线集成验证，可以构造关卡、控制顺序与重试、触发已接入的扰动适配器、生成 Controller 证据、执行简化评测并计算分数。但它还不是经过真实 Kubernetes、ChaosBlade、Telemetry HTTP 代理和轨道 A/B Harness 验证的生产运行系统；仓库中的三关运行产物是离线合同示例，不能作为真实实验结果引用。

本文使用以下状态：

- **已实现并离线验证**：代码、Schema 和受控假后端测试均已通过。
- **接口已实现，真实环境未验证**：执行接口存在，但尚无真实集群或代理证据。
- **仅完成契约**：类型、参数、安全和证据要求已定义，实际后端尚未接入。
- **未实现**：当前代码中不存在完整能力。

## 需求完成情况

| 需求 | 当前状态 | 说明 |
|---|---|---|
| 8 种分阶段扰动定义 | 已实现并离线验证 | 覆盖执行、观测、验证和中止阶段；每种均定义触发条件、动作、预期 Agent 行为和验证方法。 |
| Multi-level Episode Schema | 已实现并离线验证 | 支持 `base_task`、递增关卡、关卡重试预算、总预算和 Agent 无关的 replay seed。 |
| Level Result / Disturbance Event / Episode Score Schema | 已实现并离线验证 | 示例和测试均通过 Schema 校验。 |
| Level Sequence Builder | 已实现并离线验证 | L1 强制无扰动，后续关卡逐步增加复杂度，并按主故障类型选择相关扰动。 |
| 关卡进入与重试状态机 | 已实现并离线验证 | 只有上一关 `PASS` 才能进入下一关；支持关卡预算、Episode 总预算、`SKIP/CASE_INVALID` 和原子 checkpoint。 |
| 简化评测门禁 | 已实现并离线验证 | 包含前置检查、`fault_effect`、`diagnosis`、`recovery`、`safety` 和扰动应对判定。 |
| 扰动应对能力判定 | 已实现并离线验证 | 使用稳定 behavior id 和独立证据来源；Agent 自报不能直接使判定通过。 |
| 三维评分与跨 Episode 聚合 | 已实现并离线验证 | 支持完整度与重试惩罚、同组效率归一化、安全分和难度加权聚合。 |
| 单关向后兼容 | 已实现并离线验证 | 旧 `run_trial()` 保持不变；旧 Episode 可显式包装为只有 L1 的多关 Episode。 |
| 多关 Harness 编排接口 | 接口已实现，真实环境未验证 | `run_multi_level_episode()` 和 `MultiLevelOrchestrator` 已实现，但需要实时 streaming runner。 |
| 三关完整集成测试 | 已实现并离线验证 | 已验证 L1 通过、L2 首次失败后重试通过、L3 复合扰动通过的完整路径。 |
| 文档、使用指南和 artifacts 示例 | 已实现并离线验证 | 已提供设计文档、创建指南、扰动库说明和离线三关产物。 |

## 扰动类型的实际落地深度

| 扰动类型 | 阶段 | 当前状态 | 已有实现与缺口 |
|---|---|---|---|
| `target_drift` | 执行 | 接口已实现，真实环境未验证 | Kubernetes 适配器可按精确 Pod UID 请求重启并等待替代 UID；目前只通过假 Kubernetes 客户端测试。 |
| `resource_quota_reduction` | 执行 | 接口已实现，真实环境未验证 | 可读取、缩减和恢复 ResourceQuota，并保存 cleanup handle；尚未在真实 namespace 验证。 |
| `telemetry_instability` | 观测 | 接口已实现，真实环境未验证 | 已有确定性调用槽位、503/超时模式和 `TelemetryROService` hook；真实 HTTP 503 仍需代理映射。 |
| `metric_data_gap` | 观测 | 已实现并离线验证 | 规则引擎能够删除 Prometheus matrix 的确定性数据点并记录命中；尚未在部署后的 MCP 服务验证。 |
| `fault_effect_deviation` | 验证 | 仅完成契约 | 已定义强度偏离范围和验证要求，尚无 ChaosBlade effect proxy 后端。 |
| `baseline_drift` | 验证 | 仅完成契约 | 已定义背景流量和归因要求，尚无 workload interceptor 后端。 |
| `safety_threshold_pressure` | 中止 | 仅完成契约 | 已定义 80% 硬上限、Agent 中止行为和 Controller 清理要求，尚无真实资源压力后端。 |
| `cleanup_delay` | 中止 | 仅完成契约 | 已定义 ACK 延迟与实际清理分离语义，尚无 ChaosBlade/Controller 清理代理。 |

## 关键实现边界

### 触发机制

生命周期事件是默认触发机制；Telemetry 类扰动还支持“指定 MCP 工具第 N 次调用”触发，时间延迟只作为低精度回退。精确调用序列必须由 Harness 在 Agent 仍运行时实时上报。现有通用 CLI runner 在进程结束后统一读取 stdout，因此只能用于取证，不能声称已经在第 N 次工具调用时实时完成注入。

### 目标身份

安全动作必须绑定本次 trial 当前的精确 Pod name 和 UID。Episode 中保存的 UID 是初始资格检查证据，不是跨重试、跨关卡永久不变的身份；每次重试和进入下一关前必须恢复逻辑快照并重新解析当前 UID。无法重新绑定或基线未恢复时，本关应为 `SKIP/CASE_INVALID`，不能继续注入。

### 安全与证据

扰动执行器在调用后端前必须通过独立的 Controller safety gate，检查 namespace allowlist、精确 UID、run label、参数上限和变更型扰动并发数。触发、拒绝、执行失败、完成和清理均写入 `controller_record`。`blade destroy`、资源对象消失或 Agent 自报不能单独证明目标侧已经恢复。

### 输入契约

当前仓库已经取消独立的 `experiment-plans.json`；实验序列位于 `episode-designs.v0.1` 的 `experiment_sequence`。Level Sequence Builder 接收其中一个已资格检查的 Episode design 或等价 mapping，没有重新引入重复的 experiment-plan 产物。

## 已完成的验证

从本次提交的暂存快照和 GitHub 远端分支分别执行了干净环境验证：411 项测试全部通过，source distribution 和 wheel 构建成功，`git diff --cached --check`、敏感边界扫描和 `git fsck --full --no-dangling` 均通过。远端分支 SHA 与本地提交一致，PR 当前为 OPEN、Draft、MERGEABLE。

这些结果证明代码合同、离线状态机、适配器接口、规则引擎、Schema、评分和回归路径可重复；它们不证明真实集群上的故障效果、业务影响、恢复结果或不同 Agent 间的实测公平性。

## 尚未完成

1. 在明确授权的 Kubernetes 测试 namespace 中验证 Pod 重启、ResourceQuota 修改、失败回滚和每次 trial 的 UID 重绑定。
2. 接入并验证 ChaosBlade effect proxy、背景流量、安全逼近和清理延迟四类后端。
3. 为 Codex、Claude Code、BladeAI 等轨道 A/B Harness 实现实时生命周期和 MCP tool-call 事件流。
4. 在真实 Telemetry MCP 部署前增加 Controller-only 规则管理通道，并验证反向代理的真实 HTTP 503/超时映射。
5. 接入真实独立 Oracle、固定业务 SLO、恢复证据和 comparison group，运行多 Agent、多 Episode 正式评测。
6. 实现 P2 可视化报告，包括能力雷达图和关卡通过/重试瀑布图。
7. 任意用户自定义扰动脚本尚未开放；当前只允许注册的类型化适配器，以保证安全、确定性和清理所有权。
8. GitHub Draft PR #1 尚未合并到 `main`，仓库当前没有自动 CI Checks。

## 完成判据

只有当真实环境资格检查通过、每种启用扰动都有 Controller 触发与清理证据、streaming Harness 能证明精确触发时机、独立 Oracle 能判定主故障和目标侧恢复、轨道 A/B 使用同一冻结配置，并且正式回归与 CI 通过后，才能将状态从“代码和离线验证完成”提升为“端到端系统完成”。如果只能事后解析 stdout、目标 UID 未重新绑定、清理只检查 API ACK，或不同 Agent 使用了不同扰动 seed/权限/环境，则当前方案不成立，不能进入正式评分。
