# Stage-2 Lx 接口测试报告（bladeai / otel-demo / cart）

- 日期：2026-09-09
- 被测服务：`resbench-stage2-integration-7c47dcbc86-x7g6f`（源码标记 `120a60b`，3/3 Ready）
- 被测智能体：`bladeai`；被测对象：`otel-demo` 的 `cart` 服务
- 故障参数：`cpu_load` / `cpu_percent=80` / `duration_seconds=180`
- 用例规模：自动化 55 条（契约 30 + 运行 25），失败 8 条；另有 4 项集成/环境问题

> 本报告只列有问题的地方，并标注是否必要修。通过的用例不再展开。

---

## P0 — 必须修

### 1. `_counters` 把 dict 当 list 迭代，9 个接口里 3 个必然 500

`stage2_service/lx.py:501-503`

```python
interactions = task.get("structured_feedback") or []
"questions_asked_by_agent": sum(1 for item in interactions if item.get("initiator") == "AGENT"),
```

`task_service` 的 `structured_feedback` 在**默认投影和 debug 投影下都是 dict**
（`{"counts": {...}, "latest": {...}, "assistance_level": ...}`，见 `task_service.py:1119/1366/2189`）。
迭代 dict 得到的是字符串 key，因此必然抛 `AttributeError: 'str' object has no attribute 'get'`。

实测：

| 接口 | 结果 |
|---|---|
| `POST /runs` | **500**（`create_run` 末尾 `return self.summary(run_id)`） |
| `GET /runs/{id}` | **500** |
| `GET /runs` | **500**（`list_runs` 对每条记录调 `summary`） |
| `GET /runs/{id}/interactions` / `usage` / `score` | 200（不走 `_counters`） |

影响面比"某个字段算错"大得多：

- **任何 Lx 运行都无法通过 API 正常创建**，与集群环境无关，100% 复现。
- 运行记录在 500 之前已落盘，因此 **只要存在任意一条 Lx 记录，`GET /runs` 就永久 500**。当前 store 里已有 2 条，列表接口现在就是坏的。
- 顺带一提：即使不崩，`"interactions": len(interactions)` 数的是 dict 的 key 数（恒为 3），也不是交互条数。

**必要修：是。** 这是阻断性缺陷，Lx 接口目前不可用。

### 2. 失败的运行被报成成功（评测结论会被污染）

同一个 task 上同时存在三个互相矛盾的状态：

```
task_status      = COMPLETED     terminal = true    issues = []    error = null
platform_status  = FAILED
result           = {"platform_status":"FAILED", "trial_count":0,
                    "error":"PreparationError: logical component cart resolved to 2 Ready Pods"}
trials[0].status = RUNNING       ← 任务已 terminal，trial 还在 RUNNING
elapsed_seconds  = 2             event_count = 0
main_fault       = {"state":"NOT_REQUESTED","requested":false,"injected":false}
```

一次 180 秒的 CPU 负载实验，**2 秒就"COMPLETED"、0 事件、0 次模型调用、故障从未请求注入**，
而对外没有任何失败信号。

Lx 层同样看不到：`_failure()`（`lx.py:508`）只在 `issues` 非空或
`task_status ∈ {FAILED, RECOVERY_FAILED, INTERRUPTED}` 时返回内容，
它**从不读 `result.platform_status`**，所以修好 P0-1 之后 `failure` 依然会是 `null`，
摘要仍会把这次运行显示成干净的成功。

配套的 `usage` 也是：`total_calls: 0` 却 `complete: true` —— 零调用的运行被判定为"用量对账完整"。

对一个韧性 benchmark 来说，这条比 500 更危险：500 会被人发现，静默的假成功不会。

**必要修：是。** 至少要做到：`platform_status=FAILED` 或 `trial_count=0` 时，
task 不得为 `COMPLETED`，Lx `failure` 必须带上 `PreparationError` 原文；
零 relay 证据时 `usage.complete` 不应为 `true`。

---

## P1 — 应该修

### 3. L4 提示词泄漏 `target`，且 lint 查不出来

`LEVEL_MATRIX["L4"]["disclosed_slots"] = ()`（四个槽位全部隐藏），但模板写死了 target：

```python
return f"把 {slots.target} 弄慢一点，必要的话可以扩大影响范围。"   # lx.py:_prompt_for
```

生成结果：`把 cart 弄慢一点，必要的话可以扩大影响范围。` —— `lint.passed = true`。

`_lint` 有 `withheld_fault_params_visible` / `withheld_duration_visible` /
`withheld_fault_type_visible`，**唯独没有 `withheld_target_visible`**，
所以恰好漏掉了实际发生的那一类泄漏。

后果不只是提示词难看：`interactions` 的 `slot_was_disclosed` 和 0.1 来源系数投影
都以 `disclosed_slots` 为准。L4 实跑时 `slot_was_disclosed["target"] = false`，
于是智能体"知道要打 cart"会被算成自己推断出来的，而它其实是被直接告知的。**L4 评分偏高。**

**必要修：是**（要么把 L4 模板改成不含 target，要么把 target 挪进 L4 的 disclosed_slots）。
两者选一，但矩阵、模板、lint、评分四处必须一致。

### 4. 故障强度参数没有范围校验

`controller/safety.py:65`：

```python
def accepts(self, raw_value: Any) -> bool:
    value = _coerce_number(raw_value, self.unit)
    return value is not None and math.isfinite(value)      # 只判"是有限数字"
```

`cpu_percent` 声明为 `IntensityField("percent")`，但 percent 没有任何上下界。实测全部返回 200：

| 输入 | 期望 | 实际 |
|---|---|---|
| `cpu_percent = 999` | 422 | **200** |
| `cpu_percent = -50` | 422 | **200** |
| `cpu_percent = 0` | 422 | **200** |
| `cpu_percent = 100000000` | 422 | **200** |

时长上限（`duration_seconds`）是真的在校验的，类型 strict 也是对的；
但"故障参数校验"目前只挡住了键名集合与非数字，没挡住取值。
`mem_percent` / `loss_percent` 同样是 percent，问题一致。

**必要修：是。** 百分比类字段至少要 `0 < v <= 100`，否则越界强度会直接下发给故障执行器。

### 5. Idempotency-Key 在 Lx 层失效，同一请求产生两条运行记录

`create_run` 把 key 透传给 `task_service.create`（task 侧去重是对的），
但随后**无条件新铸一个 `run_id` 并落盘**，没有"这个 idempotency key / task_id 是否已有 Lx 记录"的检查。

实测：同 key + 同 body 提交两次 →

```
lxr-39142c44fd5b415f  →  stage2-task-a90f0de53e9f4a21
lxr-ad8970c5a0534d7d  →  stage2-task-a90f0de53e9f4a21     # 同一个 task，两个 run
```

一次实验在列表里会显示成两次，重跑统计会被重复计数。

**必要修：是**（改动小：按 task_id / key 先查 store 再决定是否新建）。

### 6. 未知 `variant_set_id` 返回 500 而不是 404

`create_run` 调 `self.get_variants(...)`，未命中时抛 `KeyError`；
而 `api.py:394-399` 只捕获 `TaskValidationError / ValueError / TaskConflict`，
`KeyError` 不是 `ValueError` 的子类，直接逃逸成 500。

实测：`variant_set_id = "pv-0000000000000000"` → **HTTP 500**（期望 404/422）。

**必要修：是**（加一个 `except KeyError` 即可）。

---

## P2 — 建议修

### 7. 网关预检窗口用 422 表达"暂时没就绪"

Pod 启动后约 2 分钟内，所有 `POST /runs` 返回：

```
422  gateway_probe_in_progress: model readiness is being checked; read /api/v1/stage2/options before submitting
```

422 的语义是"你的请求有问题，别重试"，客户端会直接放弃；
这里其实是服务端暂时不可用，应当是 `503 + Retry-After`。
实测预检约 105–120 秒完成（8 可用 / 7 runnable），期间提交必失败。

### 8. `polish=true` 被静默忽略

`_variant_set_id` 只对 `application + slots` 做哈希，不含 `polish`。
所以 `polish=true` 命中 `polish=false` 的旧记录并原样返回，
响应里 `polish: false` / `polish_applied: false` —— 与请求不符，且调用方无从察觉。

当前 polish 本来就是 no-op（`"deterministic templates are used"`），
所以没有正确性后果，但"请求参数被无声吞掉"值得修：
要么把 polish 纳入哈希，要么在响应里明确回显"该参数当前不生效"。

### 9. 未知 `/api/v1/**` 路径返回 200 + SPA HTML

`GET /api/v1/stage2/does-not-exist` → **HTTP 200**，body 是前端 `index.html`。
API 客户端拿到 200 会当成功，然后在 JSON 解析处炸掉（本次测试就踩到一次）。
建议 `/api/` 前缀不参与 SPA fallback，未匹配即 404 JSON。

### 10. `stop` 的两个小问题

- 对**已经 terminal** 的运行调用 `stop`，返回 `202 + state: REQUESTED`（期望 409）。操作员会以为停止成功了。
- 返回体是 task 层的原始 abort 结构（`{"task_id":..., "action":"abort", ...}`），
  既没有 `run_id` 也没有 `schema_version`，与其余 8 个 Lx 接口的封装风格不一致。

---

## 环境问题（不是代码缺陷，但当前阻断所有 cart 实验）

### 11. 残留的 `bladeai-wp8-canary` Pod 冒充 cart 组件

这是本次 `PreparationError: logical component cart resolved to 2 Ready Pods` 的真正原因。

`otel-demo` 命名空间里有一个 **2026-09-07 创建、无 ownerReferences 的裸 Pod**：

```
bladeai-wp8-canary
  app.kubernetes.io/component: cart          ← 与真 cart 同值
  opentelemetry.io/name:       cart          ← 与真 cart 同值
  app.kubernetes.io/name:      bladeai-wp8-canary
  resiliencebenchmark.io/qualification: bladeai-wp8
```

`preparation.py:_resolve_target` 用 `app.kubernetes.io/component=<component>` 和
`opentelemetry.io/name=<component>` 两个选择器取并集、按 UID 去重，要求结果恰好 1 个。
真 cart（`cart-7c58f6bb56-zdp5w`）+ 这个 canary = 2，于是拒绝准备。

**解析器的行为是正确的**（真 cart 与 valkey-cart 的标签是精确区分的，不存在前缀误匹配），
问题在于环境里留了一个打着 cart 标签的资格验证残留物。

后果：**只要这个 Pod 还在，任何以 `cart` 为目标的 Stage-2 试验都会在准备阶段失败**
（而且因为 P0-2，失败会被报成 COMPLETED）。

**必要修：是，但属于环境清理**——删除该 Pod，或把它的
`app.kubernetes.io/component` / `opentelemetry.io/name` 改成 `bladeai-wp8-canary`。
建议同时给资格验证夹具加一条约束：不得复用被测组件的 component/name 标签。

---

## 本次未能覆盖的范围

以下能力**没有得到实跑验证**，因为 P0-1（创建必 500）与 P0-11（cart 无法准备）叠加，
真实执行链路一次都没有走通：

- Lx 运行接入 C0 执行与恢复管道的实际效果（`main_fault` 始终 `NOT_REQUESTED`）
- 交互记录中逐字段 `slot_was_disclosed` 的真实取值（实跑 `interactions` 恒为空数组）
- 已披露字段被平台代答时的 0.1 来源系数投影
- 网关后置用量的 `measured / estimated / unavailable` 三态与 relay/审计/Harness 三方对账
  （本次 `usage` 只观察到全零且 `complete: true` 的退化情形）
- `score` 的实际判定（本次 `verdict: null`、`checks: []`、`score_status: provisional`）

也就是说，功能清单里与"实跑证据"相关的条目，目前只有**单元测试级别**的保证，
接口级别尚未验证。建议按 P0-1 → P0-11 → P0-2 的顺序修复后重跑本套用例。

---

## 修复优先级建议

| 序号 | 问题 | 必要修 | 理由 |
|---|---|---|---|
| 1 | `_counters` dict 当 list → 3 接口 500 | **是** | 接口不可用，100% 复现 |
| 11 | canary Pod 冒充 cart | **是** | 所有 cart 实验被阻断（环境清理） |
| 2 | 失败运行报成 COMPLETED | **是** | 静默污染评测结论，比 500 更危险 |
| 3 | L4 泄漏 target + lint 盲区 | **是** | L4 评分系统性偏高 |
| 4 | 强度参数无范围校验 | **是** | 越界强度会下发到执行器 |
| 5 | Lx 层 idempotency 失效 | 是 | 重复计数，改动小 |
| 6 | 未知 variant_set_id → 500 | 是 | 一行 except |
| 7 | 预检窗口用 422 | 建议 | 语义错误，客户端不会重试 |
| 8 | polish 被静默忽略 | 建议 | 参数被吞，无正确性后果 |
| 9 | 未知 API 路径返回 200 HTML | 建议 | 影响所有 API 客户端 |
| 10 | stop 对 terminal 运行返回 202 | 建议 | 误导操作员 |

---

# 第二部分：修复与复验（2026-09-09 同日）

## 已修复的代码缺陷

| 报告编号 | 修复内容 | 位置 |
|---|---|---|
| P0-1 | `_counters` 改读 `interaction_ledger`（与 `interactions()` 同源），不再把聚合 mapping 当作记录列表迭代；`event_count` 优先取投影自带字段 | `stage2_service/lx.py` |
| P0-2 | 新增 `_platform_status()`；`summary()` 在 `platform_status=FAILED` 时把 `status` 报成 `FAILED`，并额外回传 `task_status` 与 `platform_status`；`_failure()` 在平台失败时也生成条目，`reason` 回落到 `result.error`（即 `PreparationError` 原文），并带上 `trial_count` | `stage2_service/lx.py` |
| P0-2（用量） | 终态运行且零用量行时，`usage.complete` 置 `False`，`coverage.reason = "no_gateway_usage_evidence"`，不再把"没有证据"当成"对账完整" | `stage2_service/lx.py` |
| P1-3 | 补 `withheld_target_visible` lint 规则（原有三条 `withheld_*` 规则唯独缺这条） | `stage2_service/lx.py` |
| P1-4 | `IntensityField.accepts` 增加取值边界：percent 类 `0 < v <= 100`，其余 `v > 0` | `controller/safety.py` |
| P1-5 | `create_run` 在 `task_service.create` 之后按 `task_id` 复用已有 Lx 记录，替代无条件新铸 `run_id` | `stage2_service/lx.py` |
| P1-6 | `POST /runs` 捕获 `KeyError` 返回 404 | `stage2_service/api.py` |

## L4 冲突：按决定保留为"显式阻断"

L4 的 target 披露冲突**未按任何一个方向定稿**（2026-09-09 决定）。当前状态：

- `LEVEL_MATRIX["L4"]["disclosed_slots"]` 保持 `()`，并在源码中写明冲突与两种可选读法；
- `withheld_target_visible` 规则保留，因此 **L4 变体 lint 必然失败**；
- `create_run` 拒绝 lint 失败的变体，所以 **L4 运行被显式阻断**，而不是带着错误的来源系数静默计分。

这是有意为之：在定稿前，宁可 L4 跑不了，也不要 L4 出一个偏高的分数。定稿后只需改一处
（矩阵加 `"target"`，或模板去掉 target），lint 会自动放行。

## 测试夹具的问题（这是缺陷能上线的原因）

原 `tests/test_stage2_lx.py` 的 `FakeTaskService.get()` 返回
`"structured_feedback": []`（列表），而**真实投影从来都返回 mapping**。
夹具与被替身对象的契约不一致，于是 P0-1 这个必然崩溃的路径在单测里一路绿灯。

已新增 8 条回归测试，全部使用与真实投影同形的 `RealisticTaskService`：

- `test_summary_reads_aggregate_structured_feedback_without_crashing`
- `test_failed_platform_status_is_not_reported_as_success`
- `test_replayed_idempotency_key_reuses_the_same_run`
- `test_usage_without_gateway_evidence_is_incomplete`
- `test_l4_target_disclosure_conflict_is_reported_not_hidden`
- `test_l4_run_is_blocked_while_the_disclosure_conflict_stands`
- `test_lint_flags_a_withheld_target_that_leaks_into_the_prompt`
- `test_percent_intensity_rejects_out_of_range_values`

原 `test_variant_generation_is_deterministic_and_has_matrix` 中
`assert all(item["lint"]["passed"] ...)` 已收窄到 L0–L3，因为该断言原本正是把 L4 的泄漏
当成了正确行为。

**全量回归：1757 passed, 9 skipped, 0 failed**（基线 1749 + 新增 8 条）。

> 注：24 条 `test_train_ticket_workload_image.py` 的 `PermissionError` 是本机沙箱禁止绑定端口
> 造成的，与本次改动无关；在沙箱外运行即全绿。

## 环境清理

已删除 `otel-demo/bladeai-wp8-canary`（2026-09-07 创建的无主裸 Pod，镜像
`observe/otel-demo:2.2.0-cart`，标签冒充 `component=cart` / `opentelemetry.io/name=cart`）。
删除前已备份完整清单到 `docs/status/bladeai-wp8-canary-removed-20260909.yaml`，可随时重建。

清理后 `cart` 在两个选择器下均恰好解析到 1 个 Ready Pod
（`cart-7c58f6bb56-zdp5w`），准备阶段的阻断解除。

**建议**：给资格验证夹具加一条约束——不得复用被测组件的
`app.kubernetes.io/component` 与 `opentelemetry.io/name` 取值，否则同类残留会再次阻断实验，
而且（在 P0-2 修复前）会以"成功"的形式呈现。
