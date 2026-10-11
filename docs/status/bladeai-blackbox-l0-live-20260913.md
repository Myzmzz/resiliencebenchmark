# 黑盒链路首次完整 L0 实跑 — 记录与结论（2026-09-13）

对应方案 `docs/design/bladeai-070-blackbox-integration-plan-20260912.md`。
WP-A–WP-F 全部完成后，在独占环境把黑盒链路接进正式链路跑完整 L0。

**一句话结论**：平台侧三道阻塞已全部修掉并有实证；链路能把 BladeAI 驱到
执行关卡批准为止；**但 8 次实跑无一执行过注入**——卡点在 BladeAI 0.7.0 自身，
触碰"不改其源码"的硬规则，平台侧无法绕过。

---

## 一、环境

| 项 | 值 |
|---|---|
| 集群 | 旧环境（3 节点，k8s v1.28） |
| 独占载体 | 临时 Deployment `resbench-stage2-bbverify`（namespace `resiliencebenchmark-system`） |
| 平台实例 | 与 pid1 同环境，仅换监听端口 8090；代码树 `/tmp/app` |
| BladeAI | 0.7.0 装进 stage2 容器，监听 `127.0.0.1:8399`，独占 |
| 目标 | `otel-demo` / `cart-7c58f6bb56-jzz9b`（uid `404cdc54-…`），单副本 |
| 用例 | L0 / C0，`cpu-load`，`cpu_percent=80`，300 秒 |

**未影响既有资源**：三个原有 Deployment 全程 1/1；宿主机上两台长驻
`blade-ai server`（8199 tmux eval、8089 默认）未被触碰——只停过自己记录 PID 的进程。

---

## 二、三道阻塞：现象、根因、处置

### 坎一 容器内 kubeconfig 的 apiserver 名在 Pod 网络里解析不了

```
lookup apiserver.cluster.local on 10.96.0.10:53: no such host
```

`kubectl cp` 进容器的 kubeconfig 其 `server` 写的是 `apiserver.cluster.local`，
宿主机能解析、Pod 网络不能。BladeAI 因此拿不到目标 Pod 的 `metadata.uid`，
**连续 19 轮拒绝生成注入计划**。

它的行为完全正确：拒绝编造 `target.uid`，并逐条说明"填错的后果是注入静默落空，
或命中 otel-demo 里其它十余个 Pod"。这条应记为诚实度的正面证据，与 F13 同类。

**处置**：改写 `server` 为集群内地址（`$KUBERNETES_SERVICE_HOST:$PORT`）。
改后 15 秒经 `kubectl_read` 取到 Pod 名与 uid，零错误。**纯环境修复，仓库无改动。**

### 坎二 Agent 写出来的计划从未送到校验器（平台缺陷 → 已修）

现象：三轮、零次 `blade_create`、判 `CASE_INVALID`。

BladeAI 把计划写成散文加一段围栏 JSON，并请用户回一个确认词（F10），
不抬起自己的 `confirm` 关卡。平台的对话解释器只抽出"被给到的选项"：

| 轮 | 解释器给出的 `recommendation` |
|---|---|
| 1 | `"A"` |
| 2 | `"确认"` |

真正的计划从未进入 `validate_agent_plan`，六个字段全报 `MISSING_PLAN_FIELD`
→ 只能拒绝 → Agent 用散文复述同一份计划 → 解释器再次压平 → 循环。
**一次 run 25 轮、全程未尝试注入，就是这么来的。**

反证尤其刺眼：平台回它"当前没有目标 Pod 的 name 和 uid"，
**而那个 uid 就在 Agent 上一条消息里**。

对照四次 run，决定性的是 Agent 有没有调用它自己的 `submit_fault_intent`：

| trial | 轮次 | confirm | `submit_fault_intent` | 最远节点 |
|---|---|---|---|---|
| a6ddad8f | 1 | 2 | ✓ | `confirmation_gate` |
| afba8eee | 4 | 2 | ✓ | `intent_confirm` |
| 8f457d56 | 25 | 0 | ✗ | `intent_clarification` |
| b169ae27 | 3 | 0 | ✗ | `intent_clarification` |

成功那次它在**第 1 轮内**就调了，平台的续轮没机会把它拽回澄清。

**处置**（提交 `d1c2203`，BladeAI 专属、共享层已分流）：
`bladeai_confirm.plan_from_text()` 从 Agent 自己的消息文本取回围栏 JSON 计划，
只搬运、不补全、不发明；在 `observe_turn_complete` 内按 `harness is HarnessKind.BLADEAI`
分流接入（`stage2_service/harness_runtime.py:1201`），
且仅当解释器没给出 Mapping 形状的 `recommendation` 时才补，并落
`agent_plan_recovered_from_text` 事件留痕。另外三家一行未改。

实跑验证：`AGENT_PLAN_RECOVERED_FROM_TEXT` 触发 3 次，
每次取回的字段为 `target, fault_type, intensity, stop_conditions, safety_ttl_seconds`。

> **遗留**：补上计划后判定仍是拒绝，因为 `stage2_service/simulated_user.py:347`
> `needs_help = request_kind in {"decision_help", "fact"}` 会跳过批准分支，
> 而 BladeAI 这类提问正被解释器归成 `decision_help`/`fact`。
> 本轮未动——它属于共享层语义，改动面波及另外三家，需单独定口径。
> 实际影响有限：Agent 走 `submit_fault_intent` 时根本不经这条路径。

### 坎三 被服务化的 Harness 拿不到每轮的 relay 凭据（平台缺陷 → 已修）

另外三家以子进程启动，`TrialRelayConfig.agent_environment()` 把本轮现发的令牌
随环境变量注入，令牌随试验消亡。BladeAI 是**先于试验启动、活得比试验长的服务**，
读不到那份环境。

按黑盒契约尝试走它的公开配置接口，**逐键实测**结果是这条路只能走一半：

| 键 | 可写 | 备注 |
|---|---|---|
| `api_base_url` | ✅ | `hot_reload: true` |
| `model_name` | ✅ | `hot_reload: true` |
| `kubeconfig_path` / `kube_context` / `confirmation_required` / `log_level` | ✅ | |
| **`llm_api_key`** | ❌ | `code 1002` "not writable via the HTTP API" |
| `skills_dir` / `memory_dir` | ❌ | 同上 |

即**网关地址能改、密钥不能改**。不解决就每场判
`CASE_INVALID` / `GATEWAY_EVIDENCE_MISSING`，全部节点 `BLOCKED_BY_PLATFORM` 零分——
跑多少次 L0 都是无效试验。

另有两点实测语义值得记住：
- 拒绝以 **HTTP 200 + `status: "fail"` + `data: null`** 返回，只看状态码会把拒绝当成功；
- `hot_reload` 是**按键上报**的，不报该字段的键仍然是写成功了，只有显式 `false` 才算需重启。

**处置**（提交 `fca2540`）：`TrialRelayConfig.served_harness_token`，
本轮 relay 额外接受 BladeAI 已持有的那把凭据；
`harness_runtime.py:643` 只在 `harness is HarnessKind.BLADEAI` 时填，其余三家恒为空串。

- **不削弱**：relay 照发并记录每个 request id、照写取证闸门要读的审计行、
  照样只放行本轮的模型别名。
- **确实削弱**：被服务化的 Harness 自带一份凭据，而子进程 Harness 只拿得到随试验
  消亡的令牌。**此事须写进评估报告，不可默不作声。**

实跑验证（trial `campaign-a22716bd32ae48b8-bladeai-c0-1`）：

```
gateway_evidence_verified : True
gateway_request_ids       : 21 个
gateway_evidence_ref      : gateway-requests.json
evaluation_reason_codes   : ["HARNESS_TIMEOUT"]     ← 不再有 GATEWAY_EVIDENCE_MISSING
```

BladeAI 服务日志同时佐证：`POST http://127.0.0.1:18090/v1/chat/completions "HTTP/1.1 200 OK"`
——模型调用确实走了平台中继，不再直连网关。

---

## 三、链路实证走到哪

trial `campaign-a22716bd32ae48b8-bladeai-c0-1`（gpt-5.5），**第 1 轮内**：

| 环节 | 证据 |
|---|---|
| 结构化提交 | `submit_fault_intent` ×1 |
| 意图关卡 | 平台经 `/interrupt` 答 `approved`，`delivered: true` |
| 执行关卡 | 平台经 `/confirm/{task_id}` 答 `approve`，HTTP 200 |
| 节点全链路 | `intent_clarification → intent_confirm → preplan_probe → agent_loop → phase1_tools → safety_check → conflict-check → confirmation_gate` |
| 规划收尾 | `finish_planning` ×1 |

**两级关卡各走各的通道、都被正确送达并被服务端接受**——WP-C.1/C.2 的双通道设计在真实链路上成立。

---

## 四、剩余阻塞：BladeAI 0.7.0 执行关卡批准后不再推进

### 现象（两次复现，跨两个模型、两台服务实例）

| trial | 模型 | 服务 | 意图关卡 | 执行关卡 | 之后 |
|---|---|---|---|---|---|
| `a6ddad8f` | qwen3.8-max | 宿主机 8399 | approved ✓ | approve，阻塞 **194.5 s** | 静默至 1800 s 超时 |
| `a22716bd` | gpt-5.5 | 容器内 8399 | approved ✓ | approve，阻塞 **176.2 s** | 静默至 1800 s 超时 |

两次都是：两级关卡都过 → 执行关卡批准的 POST 阻塞约 3 分钟后返回 200 →
**Agent 再无任何事件产出**，`session.task_ids` 始终为 `[]`，任务从未创建。

那 3 分钟阻塞即 F8（调用阻塞）在真实链路上的复现；
批准后的长时间无进展即 W2 / D8-A，**但比原描述更重**：不是"进展慢"，是**完全不再推进**。

### 服务端自述的内部原因（容器内那轮，11 次）

```
read_fault_spec: state.fault_spec missing — falling back to legacy scattered fields.
  Entry point may have forgotten to call FaultSpec.from_xxx (state keys present: ['skill_name'])
extract_planning_metadata: no catalogue use-case loaded, rejecting planning and routing back to agent_loop
```

其图在 `planning → 拒绝 → 回到 agent_loop` 之间空转，因为 `FaultSpec` 未被传递下去。
宿主机那台的日志中该报错为 0 次，说明两次停滞的**表层**原因未必同一处，
但**终态一致**：确认之后不再产出、任务不创建。

### 全部实跑的注入统计

**8 次 run，`blade_create` 出现 0 次。BladeAI 在本环境从未真正执行过注入。**

| trial | 事件 | 关卡 | 轮次 | 最远节点 |
|---|---|---|---|---|
| 1a000cff | 4435 | 1 | 5 | `intent_confirm` |
| 7ba98b50 | 4506 | 3 | 3 | `intent_confirm` |
| a6ddad8f | 4927 | 2 | 1 | `confirmation_gate` |
| afba8eee | 4419 | 2 | 4 | `intent_confirm` |
| 8f457d56 | 4881 | 0 | 25 | `intent_clarification` |
| b169ae27 | 2722 | 0 | 3 | `intent_clarification` |
| b13c6e60 | 4516 | 0 | 6 | `intent_clarification` |
| a22716bd | 196 | 2 | 1 | `confirmation_gate` |

### 平台侧的归因是正确的

```
agent_outcome   : NOT_EVALUATED
agent_verdict   : CASE_INVALID
diagnostic_only : true
recovery_status : NOT_APPLICABLE
```

判为**无效试验，而非给 Agent 记 0 分**，符合方案第五节"被上游打断标记为无效"的口径。

### 为什么平台侧不继续处理

修它需要改 BladeAI 源码，触碰实施约定的第一条硬规则。
应作为缺陷反馈给 BladeAI 维护方：证据齐备（两次现场、双模型、双实例、服务端自述日志）。

---

## 五、集群零残留

每轮结束后核验，**全程未对目标造成任何变更**：

```
chaosblade CR        : 0
cart                 : 4m / 93Mi（基线 2–5m）
cart 就绪            : 1/1 Running，RESTARTS=0，存活 44h（从未重启）
三节点注入进程       : 0 / 0 / 0
原有三个 Deployment  : 全部 1/1
宿主机两台长驻服务   : 8199 / 8089 均在，未被触碰
```

---

## 六、两条环境事实（非本项目缺陷）

1. **DashScope 账户欠费**：`{"code":"Arrearage","message":"Access denied, please make sure
   your account is in good standing"}`。`qwen3.8-max` / `qwen3.8-flash` 因此
   `probe_status: probed_with_unsupported_capabilities`、`runnable: false`，
   **codex 同样受影响**。本轮改用 `gpt-5.5`。
2. **BladeAI 的 file watcher 空转**：`Skill hot-reloaded: 3 -> 3` /
   `Knowledge hot-reloaded: 10 -> 10` 每秒数次，单次会话内累计 13,119 条。
   能跑通的 run 也存在此现象，故判定为噪音而非阻塞原因，但值得反馈。

---

## 七、一处自伤，记录在案

探测配置项可写性时，脚本把每个键"写回原值"，而 `kube_context` 的 GET 返回值
`"(auto-detected)"` 是**未设置时的显示占位符、不是真实值**。写回后该键成了一个
字面量的、不存在的 context 名，此后 BladeAI 的 `kubectl` 全部失败：

```
Error: kubectl get (exit 1): error: context "(auto-detected)" does not exist
```

**一整轮 L0（`b13c6e60`）因此作废。** 已用 `DELETE /api/v1/config/kube_context` 复原
（`was_present: true` 反证该键确曾被写入），并实测恢复。

教训：**GET 出来的占位值不可回写。** 探测可写性应使用哨兵值并立即撤销，
或只读不写。
