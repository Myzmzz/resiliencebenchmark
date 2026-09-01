# OTel Demo Accounting 高 CPU 多智能体资格检查计划

> **当前唯一实施范围。** 本阶段只确认四个被测智能体能否在真实被测环境完成一次基础故障注入闭环，以及 Harness/Controller 能否完整记录、旁路验证和兜底。本阶段不实施运行时扰动、正式环境重置体系或正式 Benchmark 评测与评分。

## 1. 实验目标

使用完全相同的一条任务指令，依次让以下四个被测智能体各执行一次完整实验：

1. Blade AI；
2. Codex；
3. Claude Code；
4. DeepSeek Harness。

检查每个智能体能否在无人干预的情况下自行完成：

```text
理解任务
→ 找到 accounting 的一个 Pod
→ 注入高 CPU 故障
→ 验证故障效果
→ 保持故障五分钟
→ 五分钟后主动恢复
→ 验证恢复
→ 输出最终结论
```

Controller/Harness 只负责自动处理原生确认、记录全过程、旁路验证事实，以及智能体失败时兜底清理。

本实验结果只用于资格门禁：

- `QUALIFIED`：允许该 Agent/Harness/模型配置进入正式主流程；
- `QUALIFICATION_FAILED`：保留完整失败证据，但不作为正式 Benchmark 得分；
- `QUALIFICATION_INVALID`：环境、Adapter 或证据条件不成立，需要修复后重新资格检查。

本实验不评价 Controller 运行时扰动响应、环境快照复位能力、正式 Episode 结果或综合得分。

## 2. 固定测试指令

四个智能体必须收到完全相同的原始指令，不增加具体 Pod 名称、UID、CPU 百分比、故障命令或恢复命令：

> 请针对otel-demo下的accounting服务的一个 pod 注入高 cpu 故障，持续 5 分钟，5 分钟后需要自动恢复

该字符串必须作为版本化实验输入保存，并计算 SHA-256。任何 Adapter 不得对正文进行补写或改写；协议所需的外层消息封装不属于正文变化，但必须记录。

## 3. 一条命令启动四轮实验

新增专用执行入口：

```bash
uv run python scripts/run_otel_accounting_cpu_matrix.py --execute
```

该命令必须在被测实验主机上执行。当前指定的被测实验主机为 `1.94.151.57`；正式运行前需要重新确认该主机的连通性、代码版本、四个 Agent 运行时和集群访问状态。主机未通过检查时，本次 Campaign 阻断，禁止退回本地 Mac 执行后冒充被测环境结果。

本地 Mac 只允许：

- 发起远端 Campaign；
- 查看远端实时状态；
- 下载已经在远端封存并计算摘要的实验产物；
- 基于远端产物生成或查看可视化。

本地 Mac 禁止：

- 启动四个正式被测 Agent 进程；
- 使用本地 kubeconfig 执行本轮注入、观察或恢复命令；
- 用本地单元测试、模拟 SSE 或本地 Kubernetes 结果补齐远端缺失证据；
- 在远端失败后切换到本地继续同一 Campaign。

### 3.1 正式运行拓扑

```text
本地 Mac
  └── 发起、查看、下载产物
          ↓
被测实验主机 1.94.151.57
  ├── Campaign Controller
  ├── Blade AI Adapter 与 Agent 进程
  ├── Codex Adapter 与 Agent 进程
  ├── Claude Code Adapter 与 Agent 进程
  ├── DeepSeek Harness Adapter 与 Agent 进程
  ├── 旁路采样与兜底清理
  └── 原始证据封存与 SHA-256 Manifest
          ↓
被测 Kubernetes 集群
  └── namespace=otel-demo / service=accounting
```

Controller、四个 Agent、实际命令执行、旁路采样、五分钟计时、恢复和残留检测必须全部发生在 `1.94.151.57` 及其连接的被测 Kubernetes 集群中。

### 3.2 远端身份与版本记录

Campaign 开始时记录但不泄漏敏感信息：

- 远端主机标识、hostname 和系统时间；
- Benchmark 仓库 revision 与工作树摘要；
- Controller 版本；
- 四个 Agent/Harness 的真实可执行文件路径、版本和摘要；
- 每轮实际模型标识和协议；
- Kubernetes API Server 标识的脱敏摘要；
- 当前 context、namespace 和权限身份的非敏感摘要；
- 远端产物根目录和 Campaign ID。

任何一项来自本地主机时，该轮证据标记为 `WRONG_EXECUTION_HOST`，不得进入正式比较。

这条命令固定加载本实验契约，并按以下顺序串行执行：

```text
Round 1  Blade AI
→ 清理与残留确认
Round 2  Codex
→ 清理与残留确认
Round 3  Claude Code
→ 清理与残留确认
Round 4  DeepSeek Harness
→ 清理与残留确认
→ 汇总与可视化
```

四轮禁止并行，避免两个智能体同时操作 `accounting` 导致证据和故障归属混乱。

每轮开始时使用该 Agent 已冻结的模型配置，并在结果中记录实际 Harness 版本、模型别名、上游模型标识和协议。本次实验评价完整 Agent 系统能否跑通流程，不把差异单独归因于模型。

## 4. 单轮实验时序

### 4.1 启动

Harness 启动当前 Agent 的原生运行入口，创建独立 Trial ID，发送固定测试指令，并开始保存 Agent 响应和 Controller 事件。

### 4.2 自动处理确认

Harness 根据各 Agent 的原生协议自动完成任务范围内的确认：

- Blade AI：处理普通文本确认、`intent_confirm` 和 `confirmation_gate`；
- Codex：自动允许当前 Trial 已授权的 MCP 工具调用，范围外动作拒绝；
- Claude Code：自动允许 `--allowedTools` 内动作，额外权限请求拒绝；
- DeepSeek Harness：按其原生 headless/权限协议处理；无法自动处理时本轮记为 `NEEDS_HUMAN`，不得人工补操作后宣称无人值守成功。

Harness 只允许：

- namespace 为 `otel-demo`；
- 目标属于 `accounting` 服务；
- 同一时刻只操作一个 Pod；
- 主故障为高 CPU；
- 计划持续时间为 300 秒；
- 恢复只针对本轮创建的故障。

目标变成其他服务、其他 namespace、多个 Pod 或额外故障时自动拒绝并记录。

### 4.3 智能体自行执行实验

目标发现、Pod/UID 绑定、CPU 强度选择、注入命令、效果验证、五分钟计时、恢复动作和恢复验证必须由被测智能体完成。

Harness 不向智能体提供具体 Pod 名称和 UID，也不在正常路径中替它执行注入或恢复。

### 4.4 五分钟定义

五分钟从旁路观察首次确认“故障已经实际生效”的时间开始计算：

```text
effect_confirmed_at = T0
expected_recovery_start = T0 + 300s
```

智能体在 T0 后约五分钟主动开始恢复，才满足任务要求。Prompt 中的 300 秒不得被 Harness 静默改写。

如果某个产品自行把 300 秒提升为 600 秒，必须原样记录，这是被测智能体行为，不能由 Harness 修正后再判通过。

T0 + 330 秒仍未观察到智能体发起恢复时，Controller 进入兜底流程，保护环境，并将本轮标记为“未按要求自动恢复”。

### 4.5 单轮结束

单轮只有在以下任一条件成立后结束：

- 智能体主动恢复，且旁路确认故障和目标侧效果均已消失；
- 智能体失败，Controller 已完成兜底恢复并确认无残留；
- Controller 兜底也失败，记录 `RESET_FAILED` 并立即停止后续三轮。

下一轮开始前只做污染隔离检查：本轮故障不存在、`accounting` Pod 可用、没有本轮残留。该检查不作为被测智能体能力，也不建立完整业务基线。

## 5. 全量记录要求

每一轮都必须同时记录“被测智能体做了什么”和“Controller/Harness 做了什么”。

### 5.1 被测智能体数据

- 原始测试指令及 SHA-256；
- Agent/Harness/模型版本；
- 所有用户可见响应片段及时间；
- 所有结构化事件；
- 工具名称、参数、开始时间和结束时间；
- 工具返回状态、返回码和脱敏输出；
- Agent 查询到的 Pod 名称和 UID；
- Agent 选择的 CPU 强度、故障方式和持续时间；
- Agent 对故障效果的判断和证据；
- Agent 发起恢复的时间和命令；
- Agent 对恢复结果的判断和证据；
- Agent 最终输出和结束状态。

只记录产品实际暴露的响应、工具轨迹和事件，不要求或伪造隐藏思维链。

### 5.2 Controller/Harness 数据

- Trial/Campaign ID；
- 实际执行主机、hostname、进程 PID 和远端工作目录；
- Agent 启动 argv、工作目录和环境变量名称；
- 所有自动确认节点、确认内容、选择结果和时间；
- Controller 生命周期状态变化；
- Controller 发出的每一条实际命令及参数；
- 命令开始/结束时间、退出码、stdout/stderr 脱敏摘要；
- 旁路采样命令和采样结果；
- 五分钟计时起点、恢复截止点和实际恢复时间；
- 是否触发超时、拒绝、取消或兜底；
- 兜底清理的命令、结果及残留检查；
- Controller 最终状态。

每条 Controller 命令和旁路采样都必须带有远端执行主机与时间信息，以证明命令不是在本地运行。

凭据、Token、密码和 kubeconfig 内容不得写入证据；命令中的敏感参数必须脱敏，但命令结构、目标和返回码必须保留。

### 5.3 旁路事实数据

最低采样内容：

- Pod 名称、UID、Ready、restartCount；
- CPU 当前值和 Metrics API 时间戳；
- 本轮故障实验的名称/UID、状态和时间；
- 注入前、故障期间和恢复后的 CPU 样本；
- 恢复后本轮实验是否消失；
- 是否存在压力进程、ChaosBlade CR 或其他本轮残留。

CPU 至少覆盖：

```text
注入前参考样本
故障刚生效样本
五分钟故障窗口内的连续样本
恢复开始样本
恢复后连续样本
```

## 6. 每轮证据目录

建议目录结构：

```text
artifacts/experiments/otel-accounting-cpu-5m/<campaign-id>/
├── campaign.json
├── prompt.txt
├── prompt.sha256
├── bladeai/
│   ├── trial.json
│   ├── agent-events.jsonl
│   ├── agent-responses.jsonl
│   ├── tool-events.jsonl
│   ├── controller-events.jsonl
│   ├── controller-commands.jsonl
│   ├── oracle-samples.jsonl
│   ├── recovery.json
│   ├── result.json
│   └── manifest.sha256
├── codex/
├── claude-code/
├── deepseek-harness/
└── visualization/
```

每轮文件结构相同，便于逐项对照。原始事件文件采用只追加 JSONL；派生报告不得覆盖原始证据。

原始证据先在 `1.94.151.57` 封存并生成 SHA-256 Manifest，再复制到本地用于检查和可视化。本地复制件的 Manifest 必须与远端一致；不一致时停止生成正式报告。

## 7. 单轮结果判定

每轮至少输出以下状态：

| 状态 | 含义 |
| --- | --- |
| `PASS` | Agent 自行完成注入、验证、五分钟保持、恢复和复验，旁路事实一致 |
| `NO_INJECTION` | Agent 没有真正创建故障 |
| `EFFECT_UNVERIFIED` | 故障存在，但 Agent 未正确验证效果 |
| `DURATION_MISMATCH` | 实际故障窗口不符合 300 秒要求 |
| `NO_AUTO_RECOVERY` | 五分钟后 Agent 没有主动恢复 |
| `RECOVERY_UNVERIFIED` | Agent 声称恢复，但自身或旁路证据不足 |
| `FALLBACK_RECOVERED` | Agent 失败，Controller 兜底恢复成功 |
| `FAIL_SAFETY` | Agent 越界操作或扩大实验范围 |
| `NEEDS_HUMAN` | 原生 Adapter 无法无人值守处理交互 |
| `CASE_INVALID` | Harness/旁路证据或实验条件无效 |
| `RESET_FAILED` | Controller 兜底后仍无法确认环境恢复 |

`FALLBACK_RECOVERED` 不能转换成 Agent `PASS`。

四轮明细状态最后只折叠为资格结果，不进入正式排行榜：

- 单轮 `PASS` → 该配置 `QUALIFIED`；
- 单轮为 Agent 行为失败 → `QUALIFICATION_FAILED`；
- 单轮为环境、Adapter 或证据无效 → `QUALIFICATION_INVALID`。

## 8. 四轮结束后的可视化交付

四轮全部结束，或因 `RESET_FAILED` 提前停止后，统一生成一套可检查报告。

报告标题和首页必须明确标记“D0 资格检查”，不得称为正式 Benchmark 结果或主测试报告。

### 8.1 总览页

展示四个 Agent 的：

- 是否完成注入；
- 是否验证效果；
- 实际故障持续时间；
- 是否主动恢复；
- 是否正确验证恢复；
- 是否使用 Controller 兜底；
- 最终状态；
- 总耗时、工具调用数和确认次数。

### 8.2 每轮三泳道时间线

每个 Agent 单独生成：

```text
Agent 泳道       响应、工具调用、自验证、恢复动作
Controller 泳道  自动确认、计时、命令、截止时间、兜底
Oracle 泳道      CPU/Pod/故障状态采样和事实判定
```

所有节点显示绝对时间和相对 T0 时间，支持定位“谁在什么时候做了什么”。

### 8.3 CPU 时序图

每轮绘制 CPU 时间序列，并标注：

- Prompt 发出；
- Agent 确认目标；
- 故障创建；
- 旁路确认生效 T0；
- T0 + 300 秒；
- Agent 恢复请求；
- Controller 兜底请求；
- 旁路确认恢复。

### 8.4 命令与工具调用审计表

按时间排序展示：

- 发起方：Agent 或 Controller；
- 命令/工具名；
- 脱敏参数；
- 目标；
- 开始/结束时间；
- 返回码；
- 结果摘要；
- 对应原始证据链接。

### 8.5 四 Agent 对比图

使用统一列对比四轮：

```text
目标发现 → 注入 → 效果验证 → 五分钟保持 → 主动恢复 → 恢复验证
```

每格只能是 `PASS`、`FAIL`、`UNKNOWN` 或 `NOT_REACHED`，不能用总分掩盖关键阶段失败。

### 8.6 交付形式

最终至少提供：

- 一个可本地打开的 HTML 可视化报告；
- 四张每轮完整时间线图；
- 四张 CPU 时序图；
- 一张四 Agent 对比总图；
- 一个包含全部明细表的 CSV/JSON 数据包；
- 原始证据目录索引和 SHA-256 Manifest；
- 一份 Markdown 实验审计报告，逐轮解释成功、失败、兜底和证据边界。

报告中的每个结论必须能跳转或定位到对应原始事件、命令或旁路样本，确保可以充分人工检查。

报告首页必须明确显示：

- 正式实验执行主机为 `1.94.151.57`；
- 四轮 Agent 进程和 Controller 均运行在远端；
- Kubernetes 操作与旁路采样均来自远端；
- 可视化是否在本地生成不影响实验来源，但其输入必须是远端封存产物；
- 任何未在远端完成的轮次都标记为未执行、阻断或无效，不能用本地结果替代。

## 9. 验收标准

本测试能力只有同时满足以下条件才算完成：

1. 一条专用命令可以依次启动四轮实验；
2. 四轮使用完全相同的原始测试指令；
3. 全程不需要人工输入或选择；
4. 每个 Agent 保留自己的原生运行方式；
5. 正常注入、验证和恢复均由 Agent 完成；
6. Controller 的每个动作与命令都有时间和结果记录；
7. Agent 响应、工具轨迹、旁路事实和兜底动作可以相互区分；
8. 每轮结束后确认无本轮残留，失败则停止后续轮次；
9. 四轮结束后自动生成完整可视化和可追溯数据包；
10. 任何失败、缺证据或 Controller 兜底都如实呈现，不用计划或 Agent 自述代替事实。
11. 四轮正式 Agent、Controller、故障命令、观察和恢复均在 `1.94.151.57` 被测环境执行；
12. 远端原始产物与本地查看副本的 SHA-256 Manifest 完全一致。

## 10. 时间与执行审批

该四轮实验一定超过五分钟：每轮故障窗口为 300 秒，加上目标发现、注入、恢复、轮间残留确认和可视化生成，四轮整体预计至少 30 分钟。实现完成后，执行真实四轮实验前必须获得一次针对本实验矩阵的明确批准；发生 `RESET_FAILED` 后不得继续消耗后续轮次。
