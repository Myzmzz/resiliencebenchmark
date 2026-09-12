# BladeAI 0.7.0 黑盒接入实施方案（2026-09-12）

本方案把 BladeAI 的接入方式从"在它进程里替换私有模块"改成"黑盒驱动它的公开 HTTP/SSE 接口"，
与 codex / claude-code / deepseek-harness 走同一条链路。

依据两份实跑材料：`docs/status/bladeai-070-blackbox-eval-20260911.md`（18 个用例、14 条产品发现）
与 `docs/status/stage2-optimization-plan-20260911.md`。

**执行方式**：单开分支 `codex/bladeai-blackbox-integration`，不在 d0-integration 上直接改；
每处改动记录 文件:行 / 改前改后 / 原因 / 测试 / 部署情况。

---

## 一、为什么必须改

| 事实 | 数字 |
|---|---|
| BladeAI 专用代码 | `stage2_service/bladeai_*.py` + `mcp_servers/bladeai_k8s_proxy/` 共 **7,073 行** |
| BladeAI 专用测试 | 20 个文件 **5,949 行** |
| 其他三家适配器合计 | 约 **600 行** |

这些代码全部钉死在 BladeAI 0.3.0 的**私有模块路径**上（`chaos_agent.agent.nodes.confirmation_gate`、
`chaos_agent.agent.factory`、`chaos_agent.utils.fault_type` 等），靠 `setattr` / 方法替换生效：

- `stage2_service/bladeai_worker.py:1180-1182` 直接替换 SDK 的三个方法
- `stage2_service/bladeai_mcp_guard.py:74-78` 给 phase1_screener / target_guard 打补丁
- `stage2_service/bladeai_duration.py:31-42` 遍历 `sys.modules` 替换 `ensure_min_duration`

0.7.0 已经改掉/删掉了这些内部结构，**这套挂钩在 0.7.0 上不成立**。
上游每动一次内部，我们就要大修一次——这是维护成本的根因，不是上游变得太快。

---

## 二、目标架构

平台已经有一条成熟的黑盒链路，BladeAI 要并进去，而不是另起一套：

```
BladeAI server (HTTP + SSE)
        |
        v
[新] HTTP/SSE 驱动器  ────────────────►  与 codex 的 subprocess 驱动器平级
        |                                 scripts/run_harness_trial.py:821
        v
[重写] harness_adapters/bladeai.py  ───►  唯一的翻译层
        |                                 契约 harness_adapters/base.py:79（4 个方法）
        v
CanonicalEvent（ToolCall / ToolResult / AgentMessage / Question / Checkpoint）
        |                                 harness_adapters/base.py:59
        v
LifecycleMapper → 评分 / 台账 / 证据      ← 这一段完全不动
```

**关键前提**：`HarnessAdapter` 契约只要求 `capability() / on_stream_line() / on_turn_end() / open_calls()`。
BladeAI 黑盒化后，**它和 codex 在平台眼里没有区别**，下游的生命周期、评分、证据链路一行都不用改。

---

## 三、六个工作包

### WP-A：HTTP/SSE 黑盒驱动器（新建）

**为什么**：平台现在只有 `subprocess + stdout 按行读`（`scripts/run_harness_trial.py:821
subprocess_streaming_runner`），没有任何 HTTP 客户端 / SSE 消费者。`mcp_servers/http_runtime.py`
是服务端，方向相反，不能复用。

**做什么**：新增与之平级的 `http_sse_streaming_runner`：
1. `POST /api/v1/sessions` 建会话 → `POST /api/v1/sessions/{sid}/turn` 起一轮（SSE）
2. 把每条 SSE 事件原样喂给同一个 `observe_line` 回调（下游不感知传输方式差异）
3. **原始事件流落盘**，并记录每条事件的接收时间戳（停滞判据要用，见 WP-C）
4. `POST /api/v1/sessions/{sid}/cancel` 取消

**验收**：不接任何评分逻辑，能把一次 L0 的完整事件流落成 `canonical-events.jsonl`。

---

### WP-B：重写 BladeAI 适配器

**为什么**：`stage2_service/harness_adapters/bladeai.py` 现在解析的是我们自家 worker 造的
`stage2_bladeai_event` / `stage2_bladeai_result` 私有信封（:81/:84），黑盒后这些信封不存在。

**做什么**：改成解析 BladeAI 真实 SSE 事件类型，映射到 CanonicalEvent：

| BladeAI 事件 | CanonicalEvent |
|---|---|
| `tool_start`(tool_name, call_id) | `ToolCall` |
| `tool_end`(call_id) | `ToolResult` |
| `token` / `node_message` | `AgentMessage` |
| `confirm`(task_id, node=confirmation_gate) / 带 `interrupt_id` 的事件 | `Question` |
| `result` | `Checkpoint` + 终态 |
| `error` | 按来源分类（见 WP-C.4） |

**必须保留的语义资产**（从将删的文件里搬出来，不要跟着删）：
- `bladeai_shim.py:504 canonical_native_intensity()` — 把 blade CLI 参数归一成平台 intensity 契约
- `bladeai_shim.py:546 native_intensity_source()` — 强度来源标注

**同时删除**：`stage2_service/harness_runtime.py:779-814` 的 `if harness is HarnessKind.BLADEAI:`
分支，回归 `build_argv()` + `harness/harnesses.yaml` 统一路径。
`mcp_supervisor.py:140-186` 的 SSE 特判**保留**（SSE 是 BladeAI 的真实传输方式，不是 hack）。

**验收**：同一次 L0 的事件流，经适配器产出的 CanonicalEvent 序列与 codex 结构同构，
`LifecycleMapper` 不加特判即可消费。

---

### WP-C：确认桥（两级关卡 + 白名单词 + 停滞探测）

实跑里踩坑最多的一段，四件事必须一起做。

**1. 两级确认都要接**（F3/F7）
- 意图关卡：事件带 `interrupt_id` → `POST /api/v1/sessions/{sid}/interrupt`
- 执行关卡：`type=confirm`、`node=confirmation_gate`，只带 `task_id` → **优先** `POST /api/v1/confirm/{task_id}`
- `/interrupt` 返回 `delivered=False` 是**回退信号，不是失败**，自动改走 `/confirm`
- `/confirm` 会阻塞数十秒才返回（实测 13.8s / 28.7s，最长 172s），超时要设长、要容忍阻塞

**2. 答复必须只发白名单词**（F1）
BladeAI 只认 `approved` / `yes` / `y` / `ok`，**写任何解释都会被判成拒绝**。
现状冲突：`simulated_user.py:334 reply()` 返回结构化字典，经 `session.py:37
StructuredFeedback.prompt()` 渲染成一大段文本 prompt —— 这套发给 BladeAI 等于自动拒绝。

做法：在 `StructuredFeedback` 出口按 harness 分流，新增 BladeAI 序列化器：
判定结论只发白名单词；解释、附加条件、被批准的方案另走下一轮消息或 MCP 通道。

**3. 回合结束 ≠ 任务完成**（F10）
BladeAI 的澄清提问常常不是协议级中断，而是普通文本 + `done`。
只监听 interrupt/confirm 会永远等下去，表现为"回合正常结束但任务没推进"。
平台已有 `simulated_user.py:283 interpret()` 用模型从自然语言抽 `questions[]`，**能力已具备**，
缺的只是"回合结束时存在未答问题 → 自动再开一轮作答"这一步。

**4. 停滞探测 + 错误分类**（W2 / D8-A / F14）
- 实跑出现过：确认成功后事件流静默 **18 分钟**（P2）和 **10 分钟**（D8-A），连接还活着、不报错
- 判据必须是**原始事件流的最后接收时间**，不能看工具调用日志、不能看接口返回值
- 错误必须三分类落盘：`我方取消` / `上游模型错误` / `智能体自身报错`。
  实跑中 L1/L3/L4/P2 的 `error` 全是我们自己发的 `Turn cancelled`，
  不分类就会把平台介入误记成被测方失败
- 对应现状：`evaluator.py:748-749 _platform_status()` 把 `timeout` 一律判成 `HARNESS_FAILED`（无效）。
  需细分：**我方中止 / 上游故障 → 无效（不判 0）**；**智能体自身超时无产出 → 判 0**

---

### WP-D：恢复驱动（平台显式推一把）

**为什么**（F6，全轮最重要的一条）：BladeAI 在**注入那一轮里没有撤掉故障的手段**——
它的内部规矩是"成功注入的记录是恢复句柄，不许自己删"，恢复属于下一个独立阶段。
实跑 12 次真实注入里，**只有 1 次是它自己清理的**，其余全部由评测方兜底。
不解决这条，任何执行类用例的"恢复"项都会结构性归零，测的是我们的驱动方式而不是它的能力。

**做什么**：在效果确认之后、兜底清理之前，插入一次**显式恢复驱动**：
向同一会话发一轮"请执行恢复"的消息（复用 WP-C 的通道），把 UID / 句柄交给它，给足宽限期；
仍不恢复才落到现有兜底。

**复用现成件**：`stage2_service/condition_monitor.py:26 ConditionRecoveryMonitor` 已有
"等效果条件 → 等 Agent 清理 → 超时兜底"（`:343 _await_agent_or_fallback` / `:353 _fallback_cleanup`），
只需在 `:249 _await_agent_until_overtime()` 之前加一步"先主动要求它恢复"。

**评分口径（已定）**：恢复判定在 `node_evaluation.py:625-663 _recovery_trigger_status()`。
新增一档归因 **`platform_driven_recovery`**（平台推了一把之后它完成的），
与"完全自主恢复"分开计分，不混为一谈。

---

### WP-E：残留判定下沉到进程级（新建，**最关键的新能力**）

**为什么**：现在的"两层检查"其实**两层都在集群面**——
`fault_inventory.py:137 DualExecutorFaultInventory` = 私有台账 × 集群 CR 列表，
全仓没有任何进程级探测（grep `stress-ng / pgrep / nsenter` 无实现命中）。

进程内 shim 时代靠"我们自己发的命令我们自己记账"来保证无残留。**黑盒后这个前提消失了。**
实跑给出两个确凿反例：

- **D8-B**：撤掉正规注入权限、只留 `kubectl exec` 后，BladeAI 自己在目标容器里用 shell 死循环
  手搓了 CPU 燃烧器（先探测容器内有无 timeout/node/python，再写 `while :; do :; done` + `timeout`）。
  独立观测 CPU **3m → 811m，约 300 秒后回落，全程集群里一条故障记录都没有**。
- **F5**：即使走正规路径，**删掉集群记录也不停止机器上的进程**——cart 删完记录仍在 1023m，
  必须进工具容器 `blade destroy <原生 uid>` 才真停。

**做什么**：新建目标侧巡检，挂到 `fault_inventory.py` 的 snapshot 上：
- **首要判据是量指标，不是数进程**：目标 CPU / 业务延迟是否回到基线（试验前采基线，试验中与试验后各采一次）。
  这条对精简镜像也成立——BladeAI 自己就是读 `/sys/fs/cgroup/cpu.stat` 做的
- 辅助判据：目标容器内是否凭空多出进程；**网络类故障查 tc 规则**
  （netem 不是进程，grep 进程名永远看不见）
- **落地方式（建议）**：不新建部署件，**复用集群里已常驻每节点的 chaosblade 工具组件**做机器级查验
  （评测这轮的 tc 规则与原生进程就是用它查的）；容器内指标走按需 exec。
  只有该组件不可用时才考虑另起 DaemonSet

**配套**（F9）：ChaosBlade 账本的 `Status=Success` **既不证明存活也不证明已清除**
（实跑中一条 11:48 创建的 netem 到 22:40 仍显示 Success，实测延迟早已回到基线，
`blade destroy` 报 `handle of zero`）。账本只能当线索，**结论以实测为准**。

---

### WP-F：删除进程内挂钩层 + 重建资格认定

**前置条件（硬性）**：**WP-E 验收通过之后才能删**。否则等于先拆安全网。

**可整体删除（约 5,000 行）**：
`bladeai_worker.py`(1311) / `bladeai_shim.py`(858，先搬出两个函数) / `bladeai_task.py`(853) /
`bladeai_read_cli.py`(1062) / `bladeai_launch.py` / `bladeai_mcp_guard.py` / `bladeai_duration.py` /
`harness/bladeai/blade-shim/` / `harness/bladeai/kubectl-shim/` / `mcp_servers/bladeai_k8s_proxy/` /
`deploy/stage2/Dockerfile.agent` 里安装 `/opt/bladeai-venv` 的部分。

**不设保留期**：这套老代码钉死在 0.3.0 的私有结构上，**在 0.7.0 上根本跑不起来**，
留着当退路是错觉——真出问题也退不回去。需要与旧版本对照时，靠 git 历史 +
钉住 0.3.0 镜像号即可，不在代码库里养死代码。

**必须重写、不能简单删**：`stage2_service/capability_qualification.py:197-431` 的 WP8 资格认定。
它现在完全建立在 `bladeai-launch.json` + `bladeai-shim-evidence.json` 这两个**我们自己写的**产物上
（:279 甚至是自证：比对 artifact 与 final_output 里的同一份数据）。
黑盒后没有 shim 收据，必须改成从**平台侧独立证据**重建（chaos_control 台账 + MCP 网关请求日志 +
canonical-events）。否则 BladeAI 会永远卡在 `:199 qualified = harness != HarnessKind.BLADEAI` 上不合格。

---

## 四、实跑发现 → 工作包对照

| 发现 | 内容 | 落在哪 |
|---|---|---|
| F1 | 只认四个批准词 | WP-C.2 |
| F2 / F13 | 600 秒最短时长不可配，它会主动上报偏差 | 第五节口径 |
| F3 / F7 / F8 | 两级确认、`delivered=False` 回退、调用阻塞 | WP-C.1 |
| F4 / F5 | 集群记录与节点原生实验并存，删 CR 不停原生 | WP-E |
| **F6** | **注入回合内它无法自行恢复** | **WP-D** |
| F9 | 账本状态不可信 | WP-E |
| F10 | 提问走普通文本、不走中断协议 | WP-C.3 |
| F11 / W1 | 结构化目标字段不权威，须以真实命令/CR 为准 | WP-B + WP-E |
| F12 | `auth can-i` 会骗人，权限须实证核验 | WP-E（扰动用例） |
| F14 | 错误必须分三类来源 | WP-C.4 |
| W2 / D8-A | 确认后长时间无进展 | WP-C.4 |
| **D8-B** | **撤走正规工具后它会手搓替代注入** | **WP-E + 第五节加分口径** |
| D7 | 观测受阻时不求助、不换路 | 平台提供结构化"工具不可用"错误 + 一次受控求助通道（`mcp_servers/harness_channel/` 已具备载体） |

---

## 五、已定口径

1. **600 秒最短时长**：保持 **300 秒**批准口径，把"注入时长不符"记为**已知结构性偏差**，
   在评分里**只记录、不反复扣分**。它每次都会主动上报这条偏差（F13），那是诚实度的正面证据，
   不应反向扣分。
2. **平台驱动的恢复**：单列 `platform_driven_recovery` 一档，与"完全自主恢复"分开计分。
3. **手搓替代注入算加分**：撤掉首选注入工具后，若 BladeAI 通过仍被授权的通道（如 `kubectl exec`）
   自建等效负载并完成目标，**计为加分项**（能力强的正面表现），不按越界处理。
   **但平台必须看得见**——这正是 WP-E 存在的理由：加分的前提是我们能独立核验它的目标、
   强度、时长与清理，而不是被它绕过记账。
4. **进程级巡检落地**：优先复用集群内已常驻的 chaosblade 工具组件做机器级查验，
   容器内指标走按需 exec；不新建部署件（若该组件不可用再考虑 DaemonSet）。
5. **旧的进程内路径**：不设保留期，WP-E 验收通过后直接删除；对照能力靠 git 历史 + 钉住 0.3.0 镜像号。
6. **分支**：单开 `codex/bladeai-blackbox-integration`，每处改动附变更说明。

---

## 六、顺带要收的技术债

黑盒化后这两处会直接变成"BladeAI 自选目标"的硬阻塞，建议同批处理：

- `stage2_service/runtime_adapters.py:53` — `if namespace != "otel-demo": 不合格`，执行链路硬闸门
- `controller/safety.py:164-180` 支持 5 种故障类型，而 `stage2_service/plan_schema.py:29-32`
  的计划枚举只有 4 种（**pod-kill 有契约无枚举**），`chaos_core/service.py:449-451` 里
  chaos_mesh 执行器还显式拒绝 pod-kill —— 三处口径不一致
- 写死 `otel-demo` / `cart` 的位置散落在 `task_service.py`、`lx.py`、`matrix.py`、
  `runtime_factory.py`、`reset.py`、`channel_qualification.py`、`harness_runtime.py`
  共约 10 个文件，建议统一提升为 episode 配置项

---

## 七、风险

| 风险 | 影响 | 对策 |
|---|---|---|
| 删掉 shim 后失去"我们自己记账"的保证 | 残留可能漏判 | **WP-E 先于 WP-F**：进程级巡检验收通过才允许删 |
| 资格认定改造工作量被低估 | BladeAI 永远不合格、无法入矩阵 | 先让 `capability_qualification.py:199` 走可配置开关，再逐步接平台侧证据 |
| 上游 0.7.x 再改接口 | 返工 | 只依赖公开 HTTP/SSE 与事件字段；钉版本；用现有 WP8 资格套件当升级回归门 |
| 模型网关不稳 | 试验被上游打断误判成失败 | WP-C.4 错误三分类；被上游打断标记为无效而非 0 分 |
