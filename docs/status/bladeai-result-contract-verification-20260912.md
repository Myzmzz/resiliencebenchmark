# BladeAI 事件流终态契约：全语料核验（2026-09-12）

起因：黑盒接入方案的修订路线把「`result` 事件结构只看了一个样本（L0）」列为**未核验风险 2**，
并写明「WP-B 开工第一件事应该是把 17 个用例的 `result` 全 dump 出来对结构」。

本文把这件事做完了。**只读**：读旧集群 `1.94.151.57` 上 `/root/bladeai-eval/runs/<case>/events.jsonl`
的既有语料，没有起任何服务、没有碰那两个常驻 `blade-ai server`、没有发 `/cancel`。

语料规模：**19 个用例、197,845 条事件**（含 `D2-incomplete` 与 `D7-A-unauthorized` 两个补充样本）。

---

## 1. 结论

### 1.1 `result` 的结构**完全一致**，风险 2 解除

6 条 `result` 的 `data` 字段集**逐字相同**，都是同一组 25 个字段：

```
blast_radius_detail  duration_ms  error  execution_artifacts  experiment_uid
failure_detail  failure_reason  fault_handle  fault_spec  fault_type
inject_context  injection_method  issue_report  params  postmortem
recovery_handle  replan_count  replan_history  side_effects
side_effects_summary  target  task_id  task_state  verification  verify_replan_count
```

外层一律是 `{content, task_id, timestamp, type}`，`content` 是 **JSON 字符串**，
解出来是 `{status, data}`。**WP-B 写一套映射就够，不需要按用例分支。**

### 1.2 ⚠️ 新发现：`status` 恒为 `success`，**哪怕任务其实失败了**

| 用例 | `status` | `data.task_state` | `fault_type` | `injection_method` | `verification.level` |
|---|---|---|---|---|---|
| D1 | `success` | **`failed`** | pod-cpu-fullload | **`None`** | **`None`** |
| D3 | `success` | `injected` | pod-cpu-load | `host_blade` | `verified` |
| D8-B | `success` | `injected` | pod-cpu-load | **`kubectl_native`** | `verified` |
| L0 | `success` | `injected` | pod-cpu-load | `host_blade` | `verified` |
| L2 | `success` | `injected` | pod-cpu-fullload | `host_blade` | `verified` |
| P1 | `success` | `injected` | pod-cpu-load | `host_blade` | `verified` |

**D1 那条外层写着 `success`，实际 `task_state=failed`、没有注入方法、没有校验层级。**

这与 F9（账本里的 Success 不代表故障还在）是**同一类陷阱的另一个位置**：
外层 `status` 是"这次调用返回了"，不是"这件事做成了"。

**WP-B 必须以 `data.task_state` 为准，`status` 只能当传输层信号。**
把 `status == "success"` 当成功会让 D1 这类用例被判成注入成功。

### 1.3 ⚠️ 新发现：`injection_method` 有两种，D8-B 是 `kubectl_native`

其余五条都是 `host_blade`（走节点上的 chaosblade 容器），D8-B 是 `kubectl_native`——
正是「撤走正规工具后它手搓替代注入」那次。

这对**已定口径 3**（手搓替代注入算加分）是直接支撑，也说明
**WP-E 的残留巡检必须同时覆盖两条注入路径**，不能只按 `host_blade` 那套找进程。

### 1.4 `result` 只在"没被打断"时才有——**13/19 的用例根本没有 result**

| 末尾事件形态 | 用例数 | 含义 | WP-B 该怎么判 |
|---|---:|---|---|
| `… node_end → result → done` | 6 | 跑完了 | 读 `data.task_state` |
| `… → error → done` | 11 | 被打断 | 读 `error` 分三类来源（F14） |
| `… node_end → done`（无 result 无 error） | 1 | 回合结束但什么都没交 | **不能当失败**，要看有没有未答问题（F10） |
| **没有 `done`** | 1 | 流中断 | **注入状态未知**，交给 WP-E 实测 |

`error` 内容逐条核过，**与 F14 完全吻合**：10 条 `Turn cancelled`（我方 `/cancel`），
1 条真实上游 400（`D2-incomplete-20260911-2234`）。

**所以"`result` 是终态标记"这个前提不成立。** 终态判据必须是上表四种形态，
而不是"等一个 `result`"——等不到的占 13/19。

### 1.5 孤儿 `tool_start` 实测只有一例，就是 D6-B

全语料 `tool_start` 1202 次、`tool_end` 1201 次，**只差 1**，落在 D6-B（39 / 38）。
D6-B 同时是唯一没有 `done` 的用例，末尾停在 `tool_start`。

这正是修订说明里「孤儿 `tool_start` = 注入状态未知」那条，**现在有了精确的发生率：1/19**。
其余 18 个用例的 `tool_start` / `tool_end` 按 `call_id` 全部配平。

---

## 2. 全语料事件类型分布

| 类型 | 次数 | 说明 |
|---|---:|---|
| `thinking` | 185,525 | 占 94%，落盘要考虑体积 |
| `token` | 16,375 | |
| `tool_start` / `tool_end` | 1,202 / 1,201 | 差 1 = D6-B 的孤儿 |
| `node_start` / `node_end` | 810 / 762 | |
| `context_size` / `usage` / `llm_start` | 648 / 634 / 633 | |
| `node_message` | 344 | F10 的普通文本提问出现在这里 |
| `confirm` | 40 | 执行关卡 |
| `done` | 34 | |
| `error` | 11 | 10 条我方取消 + 1 条上游 400 |
| `result` | **6** | |

`thinking` 占九成以上——**WP-B 的事件落盘必须对它做丢弃或截断**，
否则一次试验的产物体积会被这一类撑爆（单个用例最多 19,791 条事件）。

---

## 3. 对方案的影响

| 项 | 原方案 | 核验后 |
|---|---|---|
| 风险 2（`result` 结构只看了一个样本） | 未核验 | **解除**：6 条结构完全一致，一套映射够用 |
| `status` 字段 | 未提 | **新增陷阱**：恒为 `success`，必须改用 `task_state` |
| `injection_method` | 未提 | **新增**：两种取值，WP-E 巡检要覆盖 `kubectl_native` |
| 终态判据 | 隐含"等 `result`" | **改**：四种末尾形态，13/19 等不到 `result` |
| 孤儿 `tool_start` | 已知是风险 | **量化**：1/19，就是 D6-B |
| F14 错误三分类 | 只有 1 个真实上游样本 | **确认**：10 取消 + 1 上游 400，比例与 F14 一致 |

**这些都不推翻方案，是把 WP-B 的映射层从"按一个样本猜"变成"按全语料写"。**
阻塞点（`tool_screener` 口径）和其余两条未核验风险（MCP 挂载、换模型行为频率）不受影响，仍待拍板 / 待验。

---

## 4. 顺带把三级关卡的契约也钉死了（WP-C 用）

`confirm` 事件共 **40 条**，**顶层字段集 40/40 完全一致**：
`{content, node, payload, task_id, timestamp, type}`。
**按 `node` 分档**，三级分布如下：

| `node` | 条数 | 是什么 | 出现位置 |
|---|---:|---|---|
| `intent_confirm` | 21 | 第一级：方案行不行 | 每个用例都有，**可重复**（L0 ×3、L1 ×2，对应改方案） |
| `confirmation_gate` | 18 | 第二级：现在真下手行不行 | 每个有关卡的用例各一次 |
| `tool_screener` | **1** | 第三级：目标漂移批不批 | **只有 D8-B**，且在 `confirmation_gate` 之后 |

每个用例的关卡序列一律是 `intent_confirm → confirmation_gate`，D8-B 多一节
`→ tool_screener`。19 个用例里 18 个有关卡；`D7-A-unauthorized` 一条都没有（早早被拒）。

### 三级各自的 payload（字段集在同档内 100% 一致）

**`intent_confirm`**（8 字段 ×21）：
`batch_faults` `clarification_round` `fault_intent` `fault_revision`
`intent_confidence` `intent_reasoning` `summary` `type`

**`confirmation_gate`**（17 字段 ×18）：
`conflict_uids` `duration_seconds` `fault_intent` `feasibility_report` `is_complex`
`params` `pipeline_attempt` `plan_path` `plan_preview_markdown` `plan_summary`
`safety_checked_detail` `safety_reason` `safety_score` `safety_status`
`skill_name` `target` `target_health_report`

> 注意 `duration_seconds` 在 D1 那条是 **600**，而 `plan_summary` 里它自己写的是
> "`--timeout 300` 自动恢复"——**F2/F13 的 600 秒夹紧在这个字段上直接可见**。
> WP-C 可以拿 `duration_seconds` 与我们批准的时长做机器比对，不必靠读自然语言。

**`tool_screener`**（7 字段 ×1）：
`agent_reason` `original` `proposed` `reason` `summary` `tool_calls` `type`

### D8-B 那条 `tool_screener` 的实际内容（**决策 1 就是在批它**）

```
type      : target_change
reason    : scope drift: approved=pod effective=chaosblade
original  : {"scope": "pod",        "namespace": "otel-demo", "names": ["cart-7c58f6bb56-jzz9b"], ...}
proposed  : {"scope": "chaosblade", "namespace": "default",   "names": ["b1f4bbf51e82d051"], ...}
tool_calls: [{"name": "kubectl", "reason": "scope drift: approved=pod effective=chaosblade"}]
agent_reason: Key evidence: `nproc=8`, `cpu.max = max 100000` (no CPU quota …)
```

**这里有一个设计确认单里没写出来的事实**：漂移后的目标**跨了命名空间**——
从 `otel-demo` 漂到 `default`，`names` 是 chaosblade 的实验 uid 而不是 Pod 名。

所以决策 1 的选项 A（"是否仍在授权目标的等效操作面内"）实际要批准的是
**"为了压 `otel-demo/cart` 这个 Pod，进 `default` 命名空间里的 chaosblade 工具容器操作"**。
这句话比原来的表述具体得多，**够不够格算"等效操作面"，由你定**。

WP-C 的实现侧结论（与口径无关，先定下来）：
- 三级**按 `node` 分派**，不能只认两级；
- 三级的 payload 字段集各自稳定，可以直接写强类型映射；
- `tool_screener` 只有一个样本，**分派逻辑要写，但答复策略留空等拍板**。

---

## 5. 复现方式

语料在旧集群 `1.94.151.57:/root/bladeai-eval/runs/<case>/events.jsonl`，
信封是 `{recv_ts, raw}`，`raw` 可能是字符串也可能是对象，解析时要两种都兜住。
本文所有数字都是对该目录全量遍历得出的，脚本是一次性的 Python，未落盘。
