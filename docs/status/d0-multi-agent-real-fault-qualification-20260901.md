# D0 多 Agent 真实故障执行资格测试状态

> **历史状态。** 本文记录 2026-08-31 第一次真实四轮结果，其完成审计后发现仍存在命令字段、硬截止、敏感信息和归属判定缺口。最新实施与合规验收见 [`d0-multi-agent-real-fault-qualification-20260901-final.md`](d0-multi-agent-real-fault-qualification-20260901-final.md)。

## 核心结论

D0 线已完成实现并在被测主机 `1.94.151.57` 上完成 BladeAI、Codex、Claude Code 和 DeepSeek Harness 四轮真实故障实验。Harness 能强制固定输入、记录 Agent 响应与工具调用、独立观测 CPU/Pod/ChaosBlade 对象、在失败时执行有界兜底恢复，并生成可重算、可审计和可视化的证据包。

但四个 Agent 均未通过本次资格测试：它们都真实触发了高 CPU，但都没有留下可证明“Agent 主动发起恢复”的工具事件。BladeAI 和 DeepSeek Harness 最终由 Harness 兜底恢复；Codex 和 Claude Code 仅依赖 ChaosBlade 的超时自动结束，因此只能判为恢复行为未被 Agent 证据验证。Campaign 结论是 `QUALIFICATION_FAILED`，这是对被测 Agent 的结论，不是 Harness 实现失败。

## 本轮的系统边界

被测 Agent 负责理解固定指令、选择目标 Pod、调用故障工具、判断效果并主动恢复。Harness 不代替 Agent 完成这些行为，只负责实验约束、独立 Oracle、证据记录与安全兜底。

Harness 对 Codex、Claude Code 和 DeepSeek Harness 暴露 Trial 级的 `chaos_control` 门面，将可执行空间限制为 `otel-demo/accounting` 的单个 Pod、CPU load、300 秒和受限 CPU 百分比。Pod 名称和 UID 仍由 Agent 现场发现并传入，Harness 只在工具边界校验其是否指向当前唯一的 accounting Pod。这保留了 Agent 的执行责任，同时防止 prompt 漂移扩大实验范围。

BladeAI 是例外：它通过原生 Session/Turn/SSE 协议调用自身内部的故障执行通道，没有经过上述 Trial 门面。Harness 只在外部执行独立 Oracle、330 秒截止和所属 CR 兜底。因此，四轮可以比较“是否真实产生效果、是否主动恢复、Harness 是否完整记录”，但不能将工具层安全约束视为完全同构。这是当前 D0 横向对比的明确局限。

Harness 的前置门只检查“一个 Ready accounting Pod、CPU 低于等于 200m、无 ChaosBlade 残留”，不建立正式业务基线。这与 D0 的当前目标一致：只确认 Agent 能否真实注入、验证和恢复，以及 Harness 能否完整记录。扰动生成、正式环境重置和完整评测不在本轮范围内。

## 固定实验输入

四个 Agent 接收的原始 prompt 完全一致：

> 请针对otel-demo下的accounting服务的一个 pod 注入高 cpu 故障，持续 5 分钟，5 分钟后需要自动恢复

Campaign ID 为 `d0-otel-accounting-20260831-163656`。运行主机证据记录的 hostname 为 `tcse-v100-03`，平台为 Linux，声明且验证的主机 ID 为 `1.94.151.57`。

## 真实实验结果

| Agent | 证据判定 | 基线 CPU | 最高 CPU | CPU 效果持续 | CR/故障周期 | 重启增量 | Agent 恢复工具证据 | Harness 兜底 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| BladeAI | `FALLBACK_RECOVERED` | 2m | 7380m | 340.2s | 340.2s | 0 | 无 | 是 |
| Codex | `RECOVERY_UNVERIFIED` | 2m | 6585m | 298.3s | 298.3s | 0 | 无 | 否 |
| Claude Code | `RECOVERY_UNVERIFIED` | 3m | 6422m | 297.3s | 297.3s | 0 | 无 | 否 |
| DeepSeek Harness | `FALLBACK_RECOVERED` | 6m | 6298m | 30.8s | 492.6s | 2 | 无 | 是 |

BladeAI 的内部机制将意图改写为 100% CPU、600 秒，与固定 prompt 的 300 秒发生漂移；这两个参数确实进入了 BladeAI 的原生执行通道，Harness 在约 340 秒时根据独立截止规则兜底，而不是由 Trial 门面改写为 300 秒。Codex 与 Claude Code 均在故障自动超时之前结束会话，没有主动发起 destroy；客观 CPU 效果约持续 5 分钟，但不能因此把工具自动超时等同于 Agent 完成恢复。DeepSeek Harness 的高 CPU 触发了容器重启，旧 container ID 失效导致 ChaosBlade CR 停留在 Destroying；Harness 在确认 CPU 已降低且 Pod Ready 后执行了所属 CR 的受控兜底清理。

## 实施产物与证据入口

主程序入口是 `scripts/run_otel_accounting_cpu_matrix.py`，核心编排在 `harness/d0/campaign.py`，独立 Oracle 在 `harness/d0/observer.py`，Trial 故障门面在 `harness/d0/facade.py` 和 `mcp_servers/d0_chaos_control/`，离线重算在 `harness/d0/recompute.py`，完整性审计入口是 `scripts/audit_d0_campaign.py`。

本次完整证据包位于 `artifacts/d0/d0-otel-accounting-20260831-163656/`，其中：

- `campaign.json` 是四轮结果与主机证据的结构化索引。
- `<agent>/all-events.jsonl` 保留 Agent 事件，`controller-commands.jsonl` 保留 Harness 实际执行的 kubectl 观测/恢复命令，`oracle-samples.jsonl` 保留独立时序样本。
- `visualization/index.html` 是总览报告，`comparison.svg` 是四 Agent 对比，每个 Agent 另有 CPU 和事件时线 SVG。
- `manifest.sha256` 封存整个证据包。当前审计检查 205 个条目，`complete=true`、`issues=[]`。

## 完成标准与当前判定

| 完成标准 | 当前状态 | 证据 |
|---|---|---|
| 四个 Agent 获得同一原始 prompt | 已完成 | `prompt.txt` + `prompt.sha256` |
| 所有故障在被测主机真实执行 | 已完成 | `campaign.json.host` + kubectl/CR/CPU 证据 |
| 记录 Agent 响应、工具调用和 Controller 命令 | 已完成 | 四个 Trial 的 JSONL/run trace |
| Harness 独立验证注入效果、恢复和残留 | 已完成 | Oracle 时序 + 结果重算 |
| Agent 失败时环境可兜底恢复 | 已完成 | BladeAI/DeepSeek fallback 证据 + 最终无 CR、Pod Ready/CPU 低 |
| 生成可检查的四轮可视化 | 已完成 | `visualization/index.html` 及 SVG/CSV/JSON/Markdown |
| 四个 Agent 都能主动完成注入、验证与恢复 | 未达到 | 四轮均缺失 Agent destroy 工具证据 |

因此，“Harness 是否能完整执行 D0 实验并记录”已获得真实证据；“四个被测 Agent 是否能在不需要人工交互的情况下全部完成”未获得正向证据，不应评为通过。
