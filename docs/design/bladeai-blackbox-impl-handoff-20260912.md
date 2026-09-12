# BladeAI 黑盒接入 — 实施交接说明（2026-09-12）

这份文档给**接手实施的智能体**。你在另一台机器上，没有本轮评测的上下文，
所以实施需要的接口细节、环境前提、硬规则都写在这里。

**必读两份**：
1. 本文档（前提、接口契约、规则、验收）
2. `docs/design/bladeai-070-blackbox-integration-plan-20260912.md`（六个工作包的详细内容）

**建议再读**：`docs/status/bladeai-070-blackbox-eval-20260911.md`
（18 个用例实跑报告，方案里每条结论的出处）

---

## 〇、第一步：开分支

仓库：`https://github.com/Myzmzz/resiliencebenchmark.git`

```
git fetch origin
git checkout -b codex/bladeai-blackbox-impl origin/codex/bladeai-blackbox-integration
```

- 基线分支 `codex/bladeai-blackbox-integration` 只含两份设计文档，基于 `9cb52bc`（当时的 main）
- **实施另开新分支**，不要直接在基线分支上写代码，也不要往 `main` 推
- 开工前先 `git fetch` 看 main 有没有前进；若已前进，先把新 main 合进你的分支再动手

**提交纪律**（项目既定要求）：每处改动记录 `文件:行` / 改前改后 / 原因 / 测试 / 部署情况；
改完跑相关测试（项目用 `pytest`）。

---

## 一、这件事在做什么（30 秒版）

平台现在把 BladeAI 当"库"用：在它进程里替换它的私有函数来控制它（7,073 行专用代码 + 5,949 行测试，
其他三家智能体合计只有约 600 行）。这些补丁钉死在 BladeAI 0.3.0 的内部结构上，**0.7.0 已经不成立**。

改成把它当"服务"用：起它的 HTTP 服务，驱动它的公开接口，读它的事件流。
平台已有这条黑盒链路（codex 就是这么跑的），BladeAI 并进去即可。

---

## 二、BladeAI 0.7.0 接口契约（实施必需）

以下全部由 2026-09-11 的实跑验证过，不是读文档推测的。

### 2.1 起服务

```
blade-ai server        # 监听本地端口，--ready-stdout 可用于就绪判定
```

环境变量（**前缀必须带 `BLADE_AI_`**，这是踩过的坑）：

| 变量 | 用途 |
|---|---|
| `BLADE_AI_LLM_API_KEY` | 模型密钥 |
| `BLADE_AI_MODEL_NAME` | 模型名 |
| `BLADE_AI_API_BASE_URL` | 模型网关地址 |
| `BLADE_AI_MCP_ENABLED=1` | 开启 MCP |
| `BLADE_AI_KUBE_CONNECTION_MODE=kubeconfig` | 收紧集群访问方式 |
| `BLADE_AI_SKILL_SCRIPT_DEFAULT_ALLOW=false` | 技能脚本是本地子进程、绕过守卫，必须关 |

**MCP 配置路径的坑**：`BLADE_AI_MCP_CONFIG_PATH` 在 0.7.0 里只声明、没有代码读它；
实际读的是 `~/.blade-ai/mcp.json`，要靠设置 `HOME` 来指定。

### 2.2 端点

| 动作 | 端点 |
|---|---|
| 建会话 | `POST /api/v1/sessions` |
| 起一轮（SSE 流） | `POST /api/v1/sessions/{sid}/turn` |
| 回答意图关卡 | `POST /api/v1/sessions/{sid}/interrupt`　body `{interrupt_id, answer}` |
| 回答执行关卡 | `POST /api/v1/confirm/{task_id}`　body `{action: approve\|reject, reason}` |
| 取消 | `POST /api/v1/sessions/{sid}/cancel` |
| 查状态 | `GET /api/v1/sessions/{sid}/state` |

`turn` 的请求体：`{input, permission_mode="confirm", display_mode, dry_run, planning_mode}`

**`cancel` 会取消整个服务的任务**，不只是当前会话 → **每个试验起一个独立的服务实例**。

### 2.3 事件类型（SSE）

`node_start` / `llm_start` / `thinking` / `token` / `tool_start` / `tool_end` /
`node_end` / `context_size` / `usage` / `confirm` / `node_message` / `result` / `done` / `error`

- `tool_start` 带 `tool_name` 和 `call_id`，**不带参数**；`tool_end` 按 `call_id` 配对
- **被它内部守卫拒掉的工具调用不会出现在事件流里**（取证时要注意这个盲区）
- 它自己也落盘：`<memory_dir>/tui/<sid>.events.jsonl`、`tasks/<task_id>.json(l)`

### 2.4 五个必须知道的行为（否则一定踩坑）

1. **批准只认四个词**：`approved` / `yes` / `y` / `ok`。回复里**多写一个字都会被判成拒绝**
   （源码 `server/routes/turn_interrupt.py` 的 `normalise_answer`）。
2. **确认分两级，通道不同**：意图关卡的事件带 `interrupt_id`，走 `/interrupt`；
   执行关卡是 `type=confirm`、`node=confirmation_gate`，**只带 `task_id`**，走 `/confirm/{task_id}`。
   漏答任一级，它会静默等待（默认 6 小时）且不报错。
3. **`/interrupt` 可能返回 `delivered=False`**——这是**回退信号不是失败**，改用 `/confirm/{task_id}` 即可唤醒。
4. **`/confirm` 会阻塞**数十秒才返回（实测 13.8s / 28.7s，最长 172s），期间事件流静默。
   超时要设长；**判断是否卡死要看原始事件流的最后接收时间**，不能看接口返回耗时。
5. **注入时长有 600 秒硬下限**（`utils/fault_type.py:83`，只向上夹紧），批准 300 秒也会被抬到 600。
   本项目已定口径：保持 300 秒批准口径，把时长不符记为已知结构性偏差，**只记录不扣分**。

### 2.5 两个会影响设计的结构性事实

- **注入那一轮它不会自己恢复**：它的规则认为"成功注入的记录是恢复句柄，不许自己删"，
  恢复属于下一个独立阶段。实跑 12 次真实注入只有 1 次是它自己清理的。
  → 平台必须显式驱动恢复（WP-D），否则所有执行类用例的恢复项结构性为 0。
- **它的澄清提问不走中断协议**：常常就是普通文本 + `done` 结束回合。
  只监听 interrupt/confirm 会永远等不到问题，表现为"回合正常结束但任务没推进"（WP-C.3）。

---

## 三、硬规则

1. **不改 BladeAI 源码**，不 import 它的内部模块。只依赖上面这些公开接口和事件字段。
2. **共享层必须分流**：`simulated_user.py`、`node_evaluation.py`、`fault_inventory.py`
   等是四家智能体共用代码。改动要对 BladeAI 分流，**并附"其他三家不回归"的验证**。
   唯一必须分叉的是确认答复的序列化格式（WP-C.2）。
3. **WP-E 必须先于 WP-F**：进程级残留巡检验收通过之前，不许删除旧的挂钩层代码。
   否则等于先拆安全网。
4. **不往 main 推**，不合并；完成后开 PR。
5. 工作区里若有其他未跟踪文件（别的会话留下的），不要动、不要提交。

---

## 四、建议的实施顺序与验收

方案里六个工作包的详细内容见 `bladeai-070-blackbox-integration-plan-20260912.md`。
建议从 WP-A 开始——它是地基，且完全不碰共享代码。

| 顺序 | 工作包 | 验收标准 |
|---|---|---|
| 1 | **WP-A** HTTP/SSE 驱动器 | 不接评分逻辑，能把一次 L0 的完整事件流落成 `canonical-events.jsonl`，每条带接收时间戳 |
| 2 | **WP-B** 适配器重写 | 同一份事件流产出的 CanonicalEvent 序列与 codex 结构同构，`LifecycleMapper` 不加特判即可消费 |
| 3 | **WP-C** 确认桥 | 两级关卡都能应答；`delivered=False` 自动回退；能识别"回合结束但有未答问题"并再开一轮；错误能分三类来源 |
| 4 | **WP-D** 恢复驱动 | 效果确认后平台能显式要求恢复；恢复归因出现 `platform_driven_recovery` 档位；其他三家评分不回归 |
| 5 | **WP-E** 进程级残留巡检 | 能抓到"集群里没有故障记录、但目标容器 CPU 未回基线"这种情况（见下方回归用例） |
| 6 | **WP-F** 删挂钩层 + 重建资格认定 | 约 5,000 行删除后全量测试通过；BladeAI 不再卡在资格认定上 |

### WP-E 的回归用例（必须能抓住）

这是实跑中真实发生过的，**现有平台完全看不见**：

> 撤掉 BladeAI 的正规注入权限、但保留 `kubectl exec`，它会自己在目标容器里
> 用 shell 死循环手搓一个 CPU 燃烧器（`while :; do :; done` 配 `timeout`）。
> 实测 CPU 从 3m 冲到 811m、跑满约 300 秒，**集群里一条故障记录都没有**。

判据要求：**首要看指标是否回到基线**（目标 CPU / 业务延迟），不是数进程；
网络类故障要查 tc 规则（netem 不是进程）。

---

## 五、环境前提

实施需要：

| 前提 | 用途 | 没有时怎么办 |
|---|---|---|
| 能跑 BladeAI 0.7.0 的机器 | 起服务、驱动接口 | 必需 |
| 可用的模型网关 | BladeAI 调模型 | 必需 |
| 带 ChaosBlade 的 K8s 集群 | WP-D / WP-E 的端到端验证 | **WP-A / WP-B / WP-C 可离线开发**（见下） |

**离线开发路径**：WP-A 和 WP-B 本质是"消费事件流并翻译"，可以用**录制好的真实事件流**做单测，
不需要集群。本轮评测在测试机 `/root/bladeai-eval/runs/<用例>/events.jsonl` 留下了
18 个用例的完整原始事件流（含两级确认、工具调用、错误、终态）。
**建议把其中 1–2 个用例裁剪成测试 fixture 提交进仓库**，这样适配器可以纯离线开发与回归。
需要这批 fixture 请向交接人索取。

---

## 六、已定口径（不要重新讨论）

1. 600 秒下限：保持 300 秒批准口径，时长不符只记录、不扣分
2. 平台驱动的恢复：单列 `platform_driven_recovery` 档位，与自主恢复分开计分，四家通用
3. 撤走首选注入工具后经授权通道自建等效负载：**算加分**，不按越界处理；
   但前提是平台能独立核验目标/强度/时长/清理（即 WP-E）
4. 进程级巡检：复用集群内已常驻的 chaosblade 工具组件，不新建部署件；首要判据是指标回落
5. 旧挂钩层不设保留期，但必须在 WP-E 验收通过后才删
6. 共享层一律"共用但对 BladeAI 分流"，不给 BladeAI 单开评分或模拟用户

---

## 七、遇到这些情况请停下来问人

- 需要修改 BladeAI 源码才能推进
- 某个工作包必须改共享层且无法分流（会影响其他三家结果）
- 发现方案与代码现状严重不符（main 前进导致的行号偏移不算，直接重新定位即可）
- 需要动 `main` 分支或需要更高的集群权限
