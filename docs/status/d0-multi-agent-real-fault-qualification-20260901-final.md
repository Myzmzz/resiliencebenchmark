# D0 多 Agent 真实故障执行最终实施状态

## 核心判断

D0 资格检查 Harness 已完成实施并通过计划的 12 项验收：同一 prompt、四种原生 Agent 运行方式、无人值守协议处理、Agent/Controller/Oracle 事件分离、硬截止、独立效果判定、受限兜底、压力进程残留检查、命令审计、可视化、远端运行身份和远端—本地 Manifest 一致性均有真实证据。

但四个被测配置未通过 Agent 资格检查，最终 Campaign 状态是 `QUALIFICATION_INVALID`：BladeAI 和 Codex 未注入，Claude Code 真实注入但没有主动恢复，DeepSeek Harness 的冻结模型通道不可用。这里必须分开两个结论：

- `Harness plan compliance = COMPLIANT (12/12)`：实验系统能真实、安全、可追溯地跑完资格检查。
- `Agent qualification = QUALIFICATION_INVALID`：当前四个 Agent/Harness/模型配置不能全部进入后续正式 Benchmark。

## 实施范围

本次仍严格限定在 D0：

> 请针对otel-demo下的accounting服务的一个 pod 注入高 cpu 故障，持续 5 分钟，5 分钟后需要自动恢复

本次没有实施正式主流程中的运行时扰动、正式环境重置和 Evaluator 评分。Harness 的恢复后检查只是 D0 的无残留门禁，不是将正式环境重置伪装成已实施。

## 最终证据选择

最终可检查 Campaign 为 `d0-otel-accounting-20260901-compliance-002`，运行主机为 `1.94.151.57` / `tcse-v100-03`。四轮证据均来自远端真实 Agent 进程和 Kubernetes 集群：

| Agent | 选中证据 | 模型 | 结果 | 关键事实 |
|---|---|---|---|---|
| BladeAI | `compliance-002/bladeai` | `gpt-5.6-sol` | `NO_INJECTION` | 发现 accounting 目标，但仍停在原生 clarification 机制；未创建 CR |
| Codex | `compliance-003/codex` | `gpt-5.6-sol` | `NO_INJECTION` | 直接返回 blocked，未查询目标或调用故障工具；120 秒后被 Controller 取消 |
| Claude Code | `compliance-002/claude-code` | `claude-opus-5` | `FALLBACK_RECOVERED` | 真实注入，CPU 最高 6444m；没有主动恢复，T0+330 后 Controller 兜底 |
| DeepSeek Harness | `compliance-002/deepseek-harness` | `deepseek-v4-pro` | `CASE_INVALID` | 原生 DSH session 完整保留，但网关返回 `503 model_not_found`，未进入工具阶段 |

Claude Code 的客观效果持续 222.2 秒，ChaosBlade CR/故障周期为 365.9 秒，Pod restartCount 增加 1。CPU 效果因容器重启提前结束，Agent 未发起 destroy；因此不能把“最终没有 CR”等同于 Agent 完成恢复。

## 无效与污染轮次

完成审计保留了全部失败，没有覆盖原始证据：

- `compliance-001`：Runner 硬截止改造遗漏 `import time`，Codex Adapter 在工具调用前 `NameError`；Campaign 被显式封存为 `QUALIFICATION_INVALID`。
- `compliance-002` 原 Codex：Controller 取消状态写成 schema 未允许的 `cancelled_by_controller`，导致 run-trace 封存 ValidationError；已被 `compliance-003` 的合法 `aborted_by_controller` 轨迹替换。
- `compliance-003` DSH：试图使用 `gpt-5.6-sol`，但 trial-local DSH settings 尚未注册该模型，返回 `UNKNOWN_MODEL`。
- `compliance-004` DSH：注册后成功发现 Pod，但 `openai-completions` 流在无 `finish_reason` 时结束；同时外部 Stage-2 Campaign 创建了不属于 D0 Trial 的网络故障 CR。该轮仅作为传输错误和并发污染证据，不进入最终四 Agent 视图。

当前 Observer 已改为强归属：Codex/Claude/DSH 只认 `run_id == trial_id` 的 CR；BladeAI 只认目标为当前 accounting Pod 的原生 CPU CR。任何其他 CR 都会记为 `foreign_crs_observed`、取消当前 Agent 并判为 `CASE_INVALID`，Harness 不会删除外部 CR。

## Harness 实施结果

最终实现包括：

1. 一条专用远端执行命令，默认串行 BladeAI、Codex、Claude Code 和 DeepSeek Harness。
2. 固定 prompt 文本和 SHA-256，Adapter 不向 prompt 填入 Pod 名称、UID 或命令。
3. BladeAI Session/Turn/SSE 自动交互；Codex、Claude Code 和 DSH 保留原生 headless 路径。
4. Trial-bound `d0_chaos_control` 只允许 accounting 单 Pod、`cpu-load`、300 秒和 50–80% CPU，并在服务端复验 Pod UID。
5. Agent 线程与 Observer 并行：120 秒内无效果则取消；T0+330 无恢复则先撤销 Agent/工具通道，再兜底。
6. Observer 持续保留 Pod/UID/Ready/restartCount、Metrics API CPU 时间、CR UID/创建时间/状态/归属，恢复后另外检查 `/proc/*/comm` 中的压力进程。
7. Agent MCP 调用、facade 底层 kubectl、Observer kubectl、自动确认、Oracle 和 fallback 分类记录。
8. 已实现已知 ChaosBlade 容器重启/finalizer 卡住场景的精确兜底：只在归属、Pod UID、Ready/低 CPU、Destroying、已知 finalizer 和旧 container-id 失效全部成立时移除 finalizer。
9. 结果只从封存的原始 Agent 事件与 Oracle 样本重算，不使用 Agent 自述代替事实。
10. 自动生成 HTML、四张 CPU 图、四张时间线、对比图、每 Agent 命令/工具审计页、全量 CSV/JSON 和 Markdown 报告。
11. 结构化 Kubernetes 原文不再写入 Controller 命令日志；只保留解析事实、脱敏摘要和 SHA-256。Controller 私有 capability/ledger 在 Trial 结束后删除，不进入公开证据包。

## 验收证据

- 本地相关回归：60 项通过。
- 远端隔离 release 回归：60 项通过。
- 文件完整性：233 个 Manifest 条目逐一通过，`complete=true`、`issues=[]`。
- 计划合规审计：12/12 PASS。
- 命令/工具审计 CSV：413 条数据行加表头。
- 远端与本地 `manifest.sha256` 文件哈希均为 `7dddaaf31f1c2280275e37e0cc1ea05c3c49d9f66a543cfdb247b1a6bfe7b100`。
- 当前远端终态：无 D0 进程、无 ChaosBlade CR、accounting `Running/Ready`、CPU 3m、无压力进程残留。
- 生产指针仍为 `/opt/resiliencebenchmark/releases/134692e2d3b0b4bd84c188940b4856354567ac1c`；D0 代码只位于隔离 release `/opt/resiliencebenchmark/releases/d0-20260901-001`。

远端 release 不是 Git checkout，因此 `git HEAD` 不可用；Campaign 另外记录了 33 个 D0 实施文件的联合摘要 `8d0e33d6540c28b053b0ac94bb2e50c3b2d10f127e0f1eda26dca294c1a58631`，不把“非 Git release”伪装成已知 commit。

## 交付入口

- 本地可视化：`artifacts/d0/d0-otel-accounting-20260901-compliance-002/visualization/index.html`
- 四 Agent 结构化结果：`artifacts/d0/d0-otel-accounting-20260901-compliance-002/campaign.json`
- 全量命令/工具审计：`artifacts/d0/d0-otel-accounting-20260901-compliance-002/visualization/command-tool-audit.csv`
- 计划合规报告：`artifacts/d0/plan-compliance-audits/d0-otel-accounting-20260901-compliance-002.json`
- 一键执行入口：`scripts/run_otel_accounting_cpu_matrix.py`
- 文件完整性审计：`scripts/audit_d0_campaign.py`
- 12 项计划合规审计：`scripts/audit_d0_plan_compliance.py`

## 时间约束

“1 小时内完成”没有达成。真实四轮、补跑、并发污染等待以及完成审计显著超出了该限制。实施和证据交付已完成，但不能声称按时完成。
