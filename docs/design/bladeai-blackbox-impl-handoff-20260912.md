# BladeAI 黑盒接入 — 实施交接说明（2026-09-12）

这份文档给**接手实施的智能体**。你在另一台机器上，没有本轮评测的上下文，
所以实施需要的接口细节、环境前提、硬规则都写在这里。

**必读两份**：
1. 本文档（前提、接口契约、规则、验收）
2. `docs/design/bladeai-070-blackbox-integration-plan-20260912.md`（六个工作包的详细内容）

**建议再读**：`docs/status/bladeai-070-blackbox-eval-20260911.md`
（18 个用例实跑报告，方案里每条结论的出处）

---

## 〇之前：2026-09-12 全量实测核验结果（**先看这张表**）

WP-A 实施期间，把本文档每一条可验证的论断都拿实测过了一遍：
**代码侧**逐条核 `文件:行`；**接口侧**用 17 个用例 197,687 条事件的语料 +
对活服务的联调（`1.94.151.57`，自起独立实例）。

### 代码侧：全部准确，可直接信任

所有 `文件:行` 引用**全部命中**（WP-A 到 WP-F、技术债共 26 处）；
代码量数字**精确**：BladeAI 专用代码 7,073 行、专用测试 20 个文件 5,949 行、
其他三家适配器 602 行；WP-F 待删清单里每个文件的行数**逐个对得上**。
唯一瑕疵：`evaluator.py:748-749` 缺目录前缀，仓库里有两个同名文件，
正确的是 **`stage2_service/evaluator.py:748-749`**（`evaluator/evaluator.py` 是无关文件）。

### 接口侧：4 条要改，3 条新增坑，7 条确证

| 论断 | 结论 | 影响 |
|---|---|---|
| 意图关卡"事件带 `interrupt_id`" | ❌ **改**：该字段全语料 0 次，实为事件的 `task_id` | WP-C 致命 |
| `delivered=False` 是"回退信号" | ❌ **改**：是重复投递所致，首投永远 True | WP-C 致命 |
| 确认分"两级" | ❌ **改**：实为三级，多一个 `tool_screener` | WP-C |
| 被守卫拒掉的调用不进事件流（盲区） | ❌ **改**：6/6 都有 `tool_start`，盲区不存在 | WP-B |
| `/confirm` 对不存在的 task_id 也返回 success | ➕ **新坑** | WP-C 致命 |
| `permission_mode` 不校验，静默接受非法值 | ➕ **新坑** | 配置风险 |
| 孤儿 `tool_start` = 注入状态未知（非"没注入"） | ➕ **新坑** | WP-B / WP-E |
| `cancel` 是服务级 | ✅ 确证（取消 B 打断了 A） | 架构：一试验一实例 |
| 600 秒硬下限 | ✅ 确证（17 用例 16 个 600s） | 口径 1 |
| `done` ≠ 任务完成 | ✅ 确证（11/17 用例 + 现场复现） | WP-C.3 |
| `tool_start` 不带参数 | ✅ 确证 | WP-B |
| `tool_end` 按 `call_id` 配对 | ✅ 确证（1,134 对，唯一孤儿是扰动造成） | WP-B |
| 六个端点路径 | ✅ 确证（逐个调通） | WP-A |
| 注入回合内不自行恢复 | ✅ 确证 | WP-D |

另有一条**报告级修正**：第八节称 D2 的 `error` 是上游模型 400。
补跑后的 `runs/D2/` 里那条 `error` 已是 `Turn cancelled`；
**全语料 10 条 `error` 无一例外全是评测方自己的 `/cancel`**，
没有一条是被测方失败。原始的 400 只存在于 `runs/D2-incomplete-20260911-2234/`。

> 逐条证据与复现方式见 `docs/status/bladeai-wpa-blackbox-driver-20260912.md`。
> 下文正文中，被推翻的句子用 ~~删除线~~ 标出，紧跟一段【2026-09-12 实测…】的修正。

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

> **【2026-09-12 实测修正】** 请求体**只有 `input` 是必填**（缺了返回 422）。
> `permission_mode` **不做任何校验**——传 `NOT_A_MODE` 照样返回 200 并开始执行，
> 未知字段也被静默接受。**拼错模式名不会报错**，平台必须自己保证这个值正确。
> （已实测：非法值不会跳过确认关卡，但不要依赖这一点。）

**`cancel` 会取消整个服务的任务**，不只是当前会话 → **每个试验起一个独立的服务实例**。

> **【2026-09-12 实测确证】** 在会话 B 上调 `cancel`，把会话 A 正在跑的回合打断了；
> 返回体 `{"ok":true,"cancelled":["turn-3f99ae987745"]}` 里正是**会话 A 的 turn id**。
> 这条论断成立，架构上必须一试验一实例。
> 附带：`cancelled` 列表会列出实际被取消的 turn，是可用的取证字段（原文未提）。
> 另：取消后的终态是 **`error`（内容 `Turn cancelled`）紧跟一条 `done`**，共两条。

> **【2026-09-12 实测新增坑】** `POST /api/v1/confirm/{task_id}` 对**完全不存在**的
> task_id 也返回 `{"status":"success","code":0,"message":"success",...}`。
> **接口返回成功完全不能证明关卡被应答了。** 唯一判据是原始事件流里出现了新事件。

### 2.3 事件类型（SSE）

`node_start` / `llm_start` / `thinking` / `token` / `tool_start` / `tool_end` /
`node_end` / `context_size` / `usage` / `confirm` / `node_message` / `result` / `done` / `error`

- `tool_start` 带 `tool_name` 和 `call_id`，**不带参数**；`tool_end` 按 `call_id` 配对
- ~~**被它内部守卫拒掉的工具调用不会出现在事件流里**（取证时要注意这个盲区）~~
- 它自己也落盘：`<memory_dir>/tui/<sid>.events.jsonl`、`tasks/<task_id>.json(l)`

> **【2026-09-12 实测：上面划掉那条不成立】**
> 服务端日志 `server-qwen.log` 里 `chaos_agent.tools.guard` 的 `rejected:true` 共 6 条，
> **每一条在事件流里都有对应的 `tool_start`**（时间差均在 5 毫秒内，逐条比对过）。
> 被守卫拒绝的调用**照样产生 `tool_start` 事件**，不存在这个盲区。
> 适配器（WP-B）可以把事件流当作工具调用的完整记录，不必为"看不见的被拒调用"做补偿。
> （这 6 条都是语法/策略层拒绝，例如"不支持 shell 管道"、"kubectl config 只允许 view"。
> 权限层失败则是工具真的执行后返回 Forbidden，本来就有完整的 start/end 对。）

> **【2026-09-12 实测补充：`call_id` 配对的唯一例外】**
> 全语料 17 个用例共 1,134 对工具调用，**只有 1 个孤儿 `tool_start`**：
> D6-B 的 `blade_create`，它是整条流的最后一条事件，后面没有 `tool_end` 也没有 `done`
> ——因为 D6-B 的扰动设计就是"`blade_create` 之后切断 SSE"。
> **但独立观测证实那次故障真的注入了**（CPU 3m → 约 783m）。
> 所以：**孤儿 `tool_start` 不是噪声，是"注入状态未知"的信号**。
> 适配器遇到它绝不能当成"没注入"，必须交给 WP-E 的进程级巡检去实测核验。

### 2.4 五个必须知道的行为（否则一定踩坑）

1. **批准只认四个词**：`approved` / `yes` / `y` / `ok`。回复里**多写一个字都会被判成拒绝**
   （源码 `server/routes/turn_interrupt.py` 的 `normalise_answer`）。
   > **【2026-09-12 语料确证】** 服务端日志里能直接看到这条被触发：
   > `interrupt[turn-1b4f6902e392] answer='CPU 负载 80%。' delivered=True`，
   > 紧接着下一行 `intent_confirm: Intent rejected by user, returning to conversation`
   > ——投递成功（True）但判定为拒绝。**注意 `delivered=True` 不代表被批准**，
   > 这两件事互相独立，别用 delivered 判断批准与否。
2. **确认分两级，通道不同**（原文方向正确，但字段名写错了，见下方修正）：
   意图关卡走 `/interrupt`；执行关卡走 `/confirm/{task_id}`。
   漏答任一级，它会静默等待（默认 6 小时）且不报错。
3. ~~**`/interrupt` 可能返回 `delivered=False`**——这是**回退信号不是失败**，改用 `/confirm/{task_id}` 即可唤醒。~~

> ### 【2026-09-12 实测修正：第 2、3 条必须改，否则实现一定卡死】
>
> **(a) 事件流里根本没有 `interrupt_id` 这个字段。**
> 全语料 17 个用例、197,687 条事件中 `interrupt_id` 出现 **0 次**；今天对活服务的
> 联调同样 0 次。要传给 `/interrupt` 的那个 `interrupt_id`，**取自事件自身的
> `task_id` 字段**（形如 `turn-1b4f6902e392`）。评测机 `runs/L0/driver.log` 正是
> 这样取用的，日志里 `"interrupt_id": "turn-1b4f6902e392"` 与该 confirm 事件的
> `task_id` 逐字相同。
> **照字面去找 `interrupt_id` 键的实现，一个都找不到 → 永远不应答 → 静默等 6 小时。**
>
> **(b) 两级关卡都是 `type=confirm`，靠 `node` 区分；而且其实有三级。**
>
> | `node` | 全语料出现 | 通道 | 答复体 |
> |---|---|---|---|
> | `intent_confirm` | 20 | `POST /api/v1/sessions/{sid}/interrupt` | `{interrupt_id: <事件的 task_id>, answer: <白名单词>}` |
> | `confirmation_gate` | 17 | `POST /api/v1/confirm/{task_id}` | `{action: approve\|reject, reason}` |
> | `tool_screener` | 1（D8-B） | **原文完全未提** | 待定，WP-C 必须决策 |
>
> 第三级 `tool_screener` 的 `payload.type` 是 `target_change`，是"目标漂移"复核
> （D8-B 实例：`scope drift: approved=pod effective=chaosblade`），payload 里带
> `original` / `proposed` 两个目标结构。**不处理它，D8 类用例会卡住。**
>
> **(c) `delivered=False` 不是"回退信号"，是"重复投递"。**
> 服务端日志里 56 条 interrupt 记录：`delivered=True` 38 条、`False` 18 条。
> 按 turn id 分组看，模式是同一个关卡被连发 2–4 次，且 **第一次永远是 `True`**，
> 后续重发才 `False`：
> ```
> turn-c75e6a5c32d3  approved  delivered=True
> turn-c75e6a5c32d3  approved  delivered=False
> turn-c75e6a5c32d3  approved  delivered=True
> ```
> 成因是评测侧自己的去重缺陷（见第六节坑 4：同一张卡片连答三四次），
> **不是产品要求你回退到另一个通道**。
> 两级关卡本来就各走各的通道（见上表），不存在"意图关卡投递失败改走执行关卡"这回事。
> 按原文实现的风险：把**意图关卡**的答复误送进**执行关卡**通道。
>
> **正确做法**：`delivered=False` 当作"这个关卡此刻没有在等你应答"——
> 要么已被应答过，要么还没走到。判断是否真的推进了，**只看原始事件流有没有新事件**
> （这也是上面 `/confirm` 那条坑的结论：接口返回成功证明不了任何事）。
4. **`/confirm` 会阻塞**数十秒才返回（实测 13.8s / 28.7s，最长 172s），期间事件流静默。
   超时要设长；**判断是否卡死要看原始事件流的最后接收时间**，不能看接口返回耗时。
5. **注入时长有 600 秒硬下限**（`utils/fault_type.py:83`，只向上夹紧），批准 300 秒也会被抬到 600。
   本项目已定口径：保持 300 秒批准口径，把时长不符记为已知结构性偏差，**只记录不扣分**。
   > **【2026-09-12 语料确证】** 17 个用例的确认卡片里，16 个 `Duration: 600s`
   > （唯一例外 L4 是 `900s`，题面本就不同），而其中 13 个用例的题面明写"最长 300 秒"。
   > 这条论断完全成立。

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
