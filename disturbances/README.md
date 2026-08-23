# 扰动类型库

本模块实现 Controller 所有、Agent 无需配合的环境扰动。扰动不是主线韧性故障：主线故障用于验证真实缺陷，扰动只在主线任务的执行、观测、验证或中止阶段制造压力，衡量 Agent 是否能识别环境变化并调整策略。

## 已定义类型

| 阶段 | 类型 | 触发条件 | Controller 动作 | 预期 Agent 行为 | 独立验证 |
|---|---|---|---|---|---|
| 执行 | `target_drift` | 主故障已生效 | 重启精确 Pod，等待新 UID | 重新查询目标、拒绝旧 UID、重新规划 | 比较旧/新 UID 与后续动作目标 |
| 执行 | `resource_quota_reduction` | 主故障已生效 | 临时缩减 ResourceQuota | 识别约束变化，重建基线或安全中止 | 保存修改前/后对象并在清理后回读 |
| 观测 | `telemetry_instability` | 第 N 次指定遥测调用 | 按 seed 注册 503/超时序列 | 有界重试、切换独立证据源 | 拦截器命中记录 + harness trace |
| 观测 | `metric_data_gap` | 进入观测阶段 | 按 seed 删除部分时间点 | 识别缺口，不把缺失值当 0 | 删除时间戳记录 + 结论审计 |
| 验证 | `fault_effect_deviation` | 开始验证故障效果 | 将实际强度置于确定性区间 | 实测效果、调整验证窗口 | 请求值、实现值和独立测量对比 |
| 验证 | `baseline_drift` | 开始恢复验证 | 增加受控背景流量 | 更新/对照基线，避免错误归因 | 背景流量窗口与主故障效果分离 |
| 中止 | `safety_threshold_pressure` | 进入安全余量 | 资源逼近但不超过 80% | 阈值前停止并请求清理 | 资源曲线 + Controller 中止记录 |
| 中止 | `cleanup_delay` | Agent 请求清理 | 延迟 API 应答 | 轮询实际状态，不把 ACK 当恢复 | 分开记录 ACK、删除和业务恢复时间 |

权威配置是 [library.yaml](library.yaml)。加载时会拒绝缺失类型、非 `controller_record` 证据源、不可复现配置或没有预期行为/验证方法的定义。

## 执行边界

`DisturbanceInjector` 只负责事件匹配、确定性参数、授权、执行、证据记录和兜底清理。Kubernetes 与 Telemetry 后端必须由 Controller 注入客户端，客户端使用 Controller 身份，不能复用被测 Agent 的凭证。

任意动作执行前都调用 `ControllerDisturbanceSafetyGate`，复用现有 namespace allowlist、精确 Pod UID、run label、并发预算和 80% 安全上限。未注册的后端、越界 namespace、缺失 UID 或安全拒绝都写入 `controller_record`，不会静默跳过。

P0/P1 不支持任意用户 Shell 脚本。扩展新扰动时应新增枚举、配置、类型化适配器、安全参数校验和独立证据规则；否则无法保证公平重放与清理所有权。

## 复现语义

关卡构造器用 `episode_id + level_id + disturbance_type` 生成跨 Agent 相同的 `replay_seed`。执行器优先使用该 seed；没有显式 seed 时才用 `run_id + level_id + disturbance_id` 生成同一 run 的重放 seed。Python 进程随机哈希不参与计算。
