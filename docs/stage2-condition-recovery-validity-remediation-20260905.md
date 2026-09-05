# Stage-2 条件恢复与用例有效性整改

## 整改目标

本轮只解决两个问题：

1. 主故障不再按固定时长作为正常结束方式。Agent 应在效果条件持续成立后主动清理，Controller 只负责安全兜底。
2. 实验结果与下一轮环境状态分开。环境复查失败不再把已经完成的当前 Trial 覆盖成 `CASE_INVALID`。

## 条件恢复合同

Controller 统一提供以下时间预算，故障类型、目标、强度、效果阈值和恢复阈值仍由 Agent 提议并由 Harness 确认：

| 字段 | 含义 | 值 |
|---|---|---:|
| `safety_ttl_seconds` | Chaos 自动清理的绝对安全上限 | 600 |
| `effect_observation_seconds` | 等待效果出现的最长时间 | 300 |
| `effect_sustain_seconds` | 效果条件需要连续成立的时间 | 60 |
| `agent_cleanup_seconds` | 效果成立后留给 Agent 主动清理的时间 | 60 |
| `recovery_observation_seconds` | 清理后的最长恢复观察时间 | 180 |
| `recovery_sustain_seconds` | 恢复条件需要连续稳定的时间 | 60 |

正常链路为：

```text
确认计划
→ 故障进入 Running
→ Agent 与独立 Oracle 分别观察原始业务指标
→ 效果条件连续成立 60 秒
→ Agent 在 60 秒内主动清理
→ 故障对象消失
→ 恢复条件连续稳定 60 秒
→ 完成
```

如果 Agent 未及时清理、会话提前退出，或效果观察预算耗尽后仍不安全结束，Controller 使用 Trial cleanup handle 兜底。兜底保证环境安全，但 Agent 对应恢复节点不得分。

Agent 可使用 `telemetry_workload_current` 读取当前 cart 请求数、失败数、RPS、平均延迟、P95 和成功率。该工具只返回原始读数，不返回隐藏 Oracle 结论。请求数不足时状态为 `insufficient`，不能等同于业务失败。

## 结果边界

每个 Trial 分别输出：

- `trial_validity`：平台是否支持公平判断；
- `experiment_verdict`：注入、效果、清理和恢复是否完成；
- `agent_outcome`：Agent 的节点行为结论；
- `next_trial_readiness`：环境能否进入下一轮。

`CASE_INVALID` 只用于初始环境、Harness、Controller、必需扰动或关键观测能力发生平台故障，导致无法公平判断。平台正常但注入、效果、清理或业务恢复失败时，`experiment_verdict=FAILED`。

Reset 只更新 `next_trial_readiness`。单次零请求是样本不足，系统在 180 秒内继续采样并要求连续 60 秒稳定；即使最终环境仍不适合下一轮，也不得覆盖当前 Trial 已经形成的 PASS 或 FAILED。

## 手动验收重点

1. Harness 批准结果包含完整效果条件、恢复条件和上述时间预算。
2. `chaos_create_experiment.duration_seconds` 为 600，并明确表示安全 TTL。
3. Timeline 出现 `EFFECT_CONDITION_MET` 后，Agent 在 60 秒内主动调用清理时，`cleanup_executor=AGENT_TOOL`。
4. Agent 未清理时，Controller 兜底且 Agent 的 `RECOVERY_TRIGGER`/`FAULT_CLEARED` 不得获得自主完成分。
5. 效果和恢复满足时，`experiment_verdict=PASS`。
6. Reset 复查失败时，当前 Trial 仍保持原结论，只把 `next_trial_readiness` 设为 `BLOCKED`。
