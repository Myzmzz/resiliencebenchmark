# Stage-2 Dx 轮整改改动记录（2026-09-12）

分支：`codex/stage2-env3-bladeai-20260912`（从 `main` 的 `a7bea6b` 拉出）
范围：`docs/status/stage2-dx-remediation-20260912.md` 第 5 节「第一批：平台健壮性与可诊断性」的 6 条（O03、O04、O08、O10、O18、O19）
状态：**代码与测试完成，未部署，未跑评测**（按整改说明第 5 节要求，这一批不部署）

## 0. 测试基线

用 Python 3.12（`uv sync --extra test --python 3.12`）。

| 时点 | `uv run pytest tests` | `uv run pytest`（含 `controller/tests`、`evaluator`，等同 `make test`） |
|---|---|---|
| 整改前（`a7bea6b`） | 1 failed, **2077 passed**, 11 skipped | 1 failed, **2092 passed**, 11 skipped |
| 整改后 | — | 1 failed, **2187 passed**, 11 skipped |

新增 95 条测试，全部通过；通过数不低于基线。

那 1 条失败是**整改前就存在的**，与本批无关：
`tests/test_system_snapshot.py::test_observation_adapter_uses_fixed_service_proxy_queries`
在 [tests/test_system_snapshot.py:154](../../tests/test_system_snapshot.py#L154) 硬编码了上一任操作者笔记本上的路径 `/Users/mymz/.kube/coroot-config`，换任何一台机器都会报 `configured kubeconfig does not exist`。本批不动它，列在第 8 节不确定项里。

---

## 1. O03 供应商欠费、限流没有监控也没有熔断

**现场**：2026-09-11 百炼账户欠费，供应商返回 `HTTP 400` + body 里的 `Arrearage`，智能体中途退出，平台归成「执行失败／输出不是结构化结果」——把供应商的账单问题写成了智能体的判定，而且后面每一条都照样提交、照样判。

### 改动

| 文件 | 位置 | 改前 | 改后 |
|---|---|---|---|
| `stage2_service/provider_failures.py` | 新增 403 行 | 无 | 故障分类 + 按路由熔断 + 归因汇总 |
| `stage2_service/llm_relay.py` | `TrialRelayConfig` +65..99、+115、+135；`infer` +232..293 | 上游状态码原样透传，平台不看 | 非 2xx 读回有界 body 分类后再转发，超时/网络错按类型分类，2xx 记一次成功 |
| `stage2_service/harness_runtime.py` | +40（import）、+464、+484、+716..724、+733、+1561..1572 | 无 | 运行器持有 `provider_breaker`；relay 观测到的故障进熔断器并累积；Trial 失败记录带上供应商归因 |
| `stage2_service/task_service.py` | +45（import）、+630、+637..639、+664..669、+920..930、+1126..1127 | 提交只看模型矩阵 | 路由熔断打开时直接拒绝提交并给出理由；`/options` 增加 `provider_circuits` |

**分类口径**（[provider_failures.py](../../stage2_service/provider_failures.py)）：`ARREARAGE`、`AUTHENTICATION`、`RATE_LIMIT`、`UPSTREAM_ERROR`、`TIMEOUT`、`NETWORK`、`BAD_REQUEST`、`UNKNOWN`。
**关键一条**：先看 body 再看状态码。欠费在一家是 400、在另一家是 403，只按状态码读就会变成「请求格式错」——这正是本轮出事的那一步。`BAD_REQUEST` 不算供应商责任，不会开熔断。

**熔断策略**：`ARREARAGE` / `AUTHENTICATION` 一次就开（再问一次不会变好）；限流和 5xx 累计 3 次才开。默认开 300 秒，到点转半开放一条进去探路，成功即关闭。**按路由隔离**——`dashscope:qwen3.8-max` 熔断不影响 `deepseek:deepseek-v4-pro-0813`。

**归因不会泄密**：账本和摘要里只留 `token_sha256` 级别的信息，错误正文经 `failure_detail()` 截断到 240 字并抹掉 `sk-*` / `Bearer *` / `api_key=*`。

### 测试

[tests/test_stage2_provider_failures.py](../../tests/test_stage2_provider_failures.py)，31 条：
- 400/401/403/429/5xx/超时/网络/200 各自归类（14 条参数化）；欠费不被读成 bad request
- 一次开闸（欠费）、阈值开闸（5xx）、bad request 永不开闸、跨路由不互相影响、冷却后自动放行、成功即关闭
- relay 观测：欠费响应被分类**且原样转发给智能体**、超时被分类、成功被记录、观测器抛异常不影响推理
- 提交闸：熔断打开后 `create()` 抛 `TaskTemporarilyUnavailable`，理由里带 `ARREARAGE`、路由名、`HTTP 400`；恢复后放行；`/options` 能看到熔断路由

---

## 2. O04 恢复一失败就升级成全量重装

**现场**：恢复权限报错 → 证据被归成「回滚失败/结果未知」→ `_infer_tier` 推出 `T3_FULL_REINSTALL` → 平台先卸载了被测系统，重装又失败。

### 改动

| 文件 | 位置 | 改前 | 改后 |
|---|---|---|---|
| `stage2_service/recovery_state.py` | 新增 157 行 | 无 | `RecoveryState`（`NOT_REQUIRED`/`VERIFIED`/`UNVERIFIED`）与四条路径的证据键映射 |
| `stage2_service/reset_policy.py` | +10、+38..43、+54..56、+71..74、+86..88 | `ResetPolicyDecision` 只有 tier/verified | 增加 `recovery_state`、`reinstall_authorized`、`reinstall_block_reason`；schema 升到 `.v2` |
| `stage2_service/reset.py` | +121..125、+137..152 | tier 是 T3 就 `_full_reinstall()` | 未授权时走 `_recovery_unverified()`，**一条命令都不发**，记 `reinstall_withheld` 并停下 |

**分档不是取消了 tier，而是把「该用哪种补救」和「平台能不能自己动手」拆开**：tier 仍然是 `T3_FULL_REINSTALL`（说明确实需要重装），但 `reinstall_authorized=False` 意味着平台不知道自己要覆盖的是什么状态，于是停下来交给人。

四条必须挡住的路径及其证据键：

| 路径 | 证据键 | 归因码 |
|---|---|---|
| 恢复抛异常 | `recovery_exception` / `recovery_failed` / `rollback_error` / `restore_failed` / `permission_restore_failed` | `RECOVERY_RAISED` |
| 控制器重启 | `controller_restarted` / `restoration_state_missing` / `recovery_state_missing` | `CONTROLLER_RESTARTED` |
| 清理句柄缺失 | `cleanup_handle_missing` / `cleanup_handle_unresolved` | `CLEANUP_HANDLE_MISSING` |
| D7/D8 回滚失败 | `capability_loss_rollback_failed` / `substitute_rollback_failed` | `CAPABILITY_LOSS_ROLLBACK_FAILED` |

**操作者显式指定 tier 仍然放行**（`reset_tier: T3_FULL_REINSTALL`）——那是人已经做过的决定，平台只是不再自己推。

### 测试

[tests/test_stage2_recovery_state.py](../../tests/test_stage2_recovery_state.py)，16 条。四条路径各自参数化三遍：分类层、策略层、`reset_with_policy` 实际行为层。最后一层用一个**任何命令都会断言失败**的 runner——改后 `runner.argv == []`。

**改前行为（实测）**：证据 `{rollback_attempted: True, rolled_back: False, main_fault_ever_active: True, recovery_exception: True}` → `tier = T3_FULL_REINSTALL`，`reset_with_policy` 实际调起了 `scripts/deploy_application.py`（即重装路径的第一步）。改后同样证据 → 零命令。

---

## 3. O08 令牌恢复依赖控制器进程内存

**现场**：撤权前的原始令牌只存在 `McpTokenStateRegistry._original` 这个进程内 dict 里（[runtime_adapters.py:137](../../stage2_service/runtime_adapters.py#L137)），控制器一重启，恢复就报 `MCP permission restoration state is missing`。

### 改动（[stage2_service/runtime_adapters.py](../../stage2_service/runtime_adapters.py)）

| 位置 | 改前 | 改后 |
|---|---|---|
| +5 | — | `import hashlib` |
| +150..153 | `initialize()` 只写活令牌 | **在初始化时就落盘快照**（不是等到撤权时），控制器在两者之间重启也能恢复 |
| +178..192 | `revoke()` 直接轮换 | 轮换前确保快照存在（`_ensure_restoration_snapshot`），并写 `MCP_PERMISSION_REVOKED` 账本事件 |
| +196..221 | `restore()` 只查 `_original`，取不到就抛 | 先读持久快照（`ledger_snapshot`），退化到进程内存（`process_memory`），都没有才抛**带 server 名和期望路径**的错误；重复恢复幂等 |
| +224..279 | — | `_restore_path` / `_write_restoration_snapshot` / `_ensure_restoration_snapshot` / `_read_restoration_snapshot` / `_append_permission_event` |
| +899..912 | — | `_read_token()`、`_token_fingerprint()` |
| +915 | `path.with_suffix(".tmp")` | `path.with_name(f".{path.name}.tmp")`——`x.token` 和 `x.restore` 原本会抢同一个临时名 |

**令牌不进账本**：快照写在 `<private_root>/<trial>/<server>.restore`，0600，跟活令牌同目录同权限（不新增暴露面）；账本里只记 `token_sha256`、server、路径、时间戳。

**公开证据形状没变**：`revoke()` / `restore()` 的返回值仍然是 `{server, capability, revoked|verified}`——这是 2026-09-11 D1/D3/D4 真实记录钉住的形状（`tests/test_stage2_disturbance_cases.py`）。`already_restored`、`snapshot_source` 走账本事件，不塞进扰动证据。

### 测试

[tests/test_stage2_permission_restoration_ledger.py](../../tests/test_stage2_permission_restoration_ledger.py)，6 条。核心那条构造一个**新的 registry 实例**（`_original == {}`，等价于控制器重启）再恢复。

**改前**：6 条全部失败（`restoration state is missing` / 方法不存在）。**改后**：6 条全过，且 `test_stage2_disturbance_cases.py` 的历史记录回放仍然通过。

---

## 4. O10 每次评测前网关探测 2–5 分钟，探测期间提交返回 503

**现场**：缓存一过期（300 秒），`_gateway_readiness_snapshot` 就把条目置成 `running` 且 `available_models` 为空，于是整个重探窗口里所有提交都被 503 挡掉。

### 改动（[stage2_service/runtime_factory.py](../../stage2_service/runtime_factory.py)）

| 位置 | 改前 | 改后 |
|---|---|---|
| +127..130 | `GatewayReadinessEntry` 无历史 | 增加 `last_good` / `last_good_age_seconds` |
| +1393、+1408..1410 | 只有 `probe_cache_ttl_seconds` | 增加 `probe_stale_grace_seconds`（默认 900 秒）——TTL 决定**何时去刷新**，grace 决定**旧结果还能答多久** |
| +1602、+1619..1628 | 刷新丢弃旧结果 | 刷新继承上一次 complete 的结果 |
| +1719..1746 | `running` 时无数据可答 | grace 内用 last-good 作答并标 `serving_stale_result` + `stale_age_seconds`；超出 grace 则失效关闭 |
| +1442 | 探测中一律把 model_probe 标 `running` | 只有在**没有**可用 stale 结果时才标 |

对口径「只在模型未过期且可运行时放行提交」：「未过期」= 在 grace 窗口内。超过 900 秒的结果不再算证据，失效关闭。

### 测试

[tests/test_stage2_gateway_preflight.py](../../tests/test_stage2_gateway_preflight.py)，新增/改写 4 条（该文件共 17 条全过）：
- 过期触发刷新但**仍然可答**（`serving_stale_result=True`，`READY`）
- 超出 grace **失效关闭**（`ERROR`，不可跑）
- 6 个线程并发查询只触发一次探测，答案一致
- 刷新落地后不再标 stale

**改了一条既有测试的语义**：`test_expired_success_fails_closed_and_ttl_counts_from_completion` → 拆成上面前两条。原测试钉的是「过期即失效关闭」，而 O10 要求的正是「保留最后有效结果」。这是整改说明授权的口径变更，已在测试 docstring 里写明原因。

---

## 5. O18 控制器镜像的脚本清单手写

**现场**：镜像内容在两个地方各写一遍——Dockerfile 的 `COPY` 行，和 `scripts/build_stage2_image.py` 里手抄的文件列表。本轮 `deploy_application.py` 加进了前者没加进后者，于是构建摘要没变、镜像保留了基础镜像里的旧版本，复位预检因为旧脚本不认 `--server-dry-run` 而失败——失败原因跟真正要检查的事情完全无关。

### 改动

| 文件 | 位置 | 改前 | 改后 |
|---|---|---|---|
| `stage2_service/image_manifest.py` | 新增 362 行 | 无 | 解析 Dockerfile 的 `COPY`（含续行、注释、`--flag`、跳过 `--from=`），生成带 sha256 的清单，三处校验 |
| `scripts/build_stage2_image.py` | +19..28（import）、+83..106（`source_digest`）、+329..331、+424..426 | 手抄 12 个脚本路径 | 从 Dockerfile 推导；构建前 `build_manifest()` 先跑，COPY 了不存在的源就**直接失败**；元数据写入 `runtime_manifest` |
| `deploy/stage2/Dockerfile.runtime-overlay` | +43、+48、+51 | 镜像不带自己的配方 | COPY 自身进 `/app/`，构建阶段跑 `--verify-in-image` 自检，`ENV RESBENCH_SOURCE_HEAD` 供启动时比对版本 |

三处校验，对应验收的三种情况：

| 情况 | 在哪查 | 怎么报 |
|---|---|---|
| 缺脚本 | 构建时 `build_manifest()`；镜像内 `--verify-in-image` | `Dockerfile copies sources that do not exist: <路径>` / `missing from image: <路径>` |
| 旧脚本 | 部署时 `verify_image_contents()` / `diff_manifests()` | `stale copy in image: <路径> does not match the built revision` |
| 版本不一致 | 启动时 `verify_revision()` | `runtime manifest revision X does not match image revision Y` |

`frontend/dist` 是构建产物，显式声明在 `GENERATED_SOURCES` 里：记录存在性但不参与哈希。**未声明的缺失源仍然是错误**。

### 测试

[tests/test_stage2_image_manifest.py](../../tests/test_stage2_image_manifest.py)，18 条：解析（含多源 COPY 展开、跳过 stage 间 COPY）、真实 Dockerfile 可解析、**改一个被 COPY 的脚本会改变构建摘要**、缺源失败、缺文件/旧文件/版本不符各自可检出、CLI 三种模式、Dockerfile 自检三行都在。

**改了两条既有测试**：`tests/test_capability_qualification.py::test_publisher_is_in_controller_image_and_build_inputs` 和 `tests/test_runtime_lock.py::test_review_entrypoint_is_updated_in_runtime_overlay` 原本断言「路径同时出现在 Dockerfile 和 build 脚本的字面量里」——正是本条要消灭的重复。改为断言路径出现在 Dockerfile 且被 `copied_sources()` 解析到，保留原意。

**顺手修了自己引入的一个 bug**：`verify_revision()` 的不匹配分支原本 `return ("...")` 少了逗号，返回的是字符串而不是元组，会让 `verify()` 里的 `tuple + str` 抛 `TypeError`。已修，并加了断言类型的测试。

---

## 6. O19 智能体用裸工具名，客户端报「工具不存在」，这类错误不进平台记录

**现场**：codex 偶尔用不带前缀的工具名。`normalize_tool_name` 在没有 server 提示时补不出前缀，`allowed_mcp_tool_call` 因为名字里没有 `.` 直接判 False；而客户端自己就把调用挡了，请求根本没离开 Agent 运行时——平台既没看到调用也没看到错误。codex 因此以为平台在扰动它，提前清理了故障。

### 改动

| 文件 | 位置 | 改前 | 改后 |
|---|---|---|---|
| `stage2_service/mcp_tool_catalog.py` | 新增 188 行 | 无 | 工具目录唯一真相 + `resolve_tool_identity()` |
| `stage2_service/harness_adapters/base.py` | +15、+30..34、+270..281、+283..289 | `normalize_tool_name` 只在给了 server 时补前缀 | 走 resolver；`ToolCall` 增加 `raw_tool` / `tool_resolution`（可选，默认 `None`，不破坏既有构造）；新增 `tool_identity_fields()` |
| `harness_adapters/{codex,claude_code,deepseek,bladeai}.py` | 各 1–2 处 | 只记归一化后的名字 | 同时记原始名和归一化方式 |
| `scripts/run_harness_trial.py` | +41、+152..154、+156、+959..995、+1502、+1558 | `ALLOWED_MCP_TOOLS` 字面量重复一遍；`allowed_mcp_tool_call` 自己实现前缀规则 | 从目录派生；走 resolver；新增 `unknown_tool_events()` 并在两处落账 |
| `harness/schemas/run-trace.schema.json` | `events.items.properties` | `additionalProperties: false`，无处放身份 | 增加可选 `call_id` 和 `tool_identity` |

**归一化只在确定时才补全**：裸名只有在**恰好一个** server 拥有它时才补前缀（`inferred_unique`）；多个拥有者标 `ambiguous`；不认识的保持原样标 `unknown`——**猜不出来就如实记成未知，而不是猜一个**。当前目录里没有重名工具，有一条测试专门守着这个前提。

解析结果分档：`qualified`（`server.tool`）、`client_prefixed`（`mcp__server__tool`）、`server_hint`、`inferred_unique`、`ambiguous`、`unknown`。

### 测试

[tests/test_stage2_tool_identity.py](../../tests/test_stage2_tool_identity.py)，15 条：四种拼写（带前缀 / 裸名 / 别名 / 未知）各自解析正确；空名不被凭空发明；没有跨 server 重名；Agent 运行时与目录一致；裸名不再被判为非 MCP 工具；未知名落账时**两个名字都在**；落账行符合 run-trace schema。

**改前行为（实测）**：`normalize_tool_name('chaos_create_experiment')` → `'chaos_create_experiment'`，`allowed_mcp_tool_call` → `False`，`ToolCall` 没有 `raw_tool` 字段，`run_harness_trial` 没有 `unknown_tool_events`。

---

## 7. 影响面

这一批 6 条里有 **3 处判定/展示语义变化**，都是整改说明明确授权的：

| 编号 | 变化 | 谁会看到 |
|---|---|---|
| O03 | 供应商故障导致的 Trial 失败，`error_code` 变成 `MODEL_PROVIDER_<类别>`，带 `platform_fault: true`、`agent_attributable: false` | 结果摘要；**原本会被记成智能体失败的记录，现在归给平台** |
| O04 | 恢复未验证时 `reset_policy` 多出 `recovery_state` / `reinstall_authorized`，schema `.v1` → `.v2`；结果多出 `reinstall_withheld` | 复位记录 |
| O10 | 探测期间 `/options` 的 `gateway_probe` 多出 `serving_stale_result` / `stale_age_seconds`；**过期后不再立即 503** | 提交方 |

O08、O18、O19 不改判定语义：O08 只换存储位置（公开证据形状不变），O18 只改构建与校验，O19 只补记录（原本被判 forbidden 的裸名现在会正常解析——这是修正误判，不是放宽边界）。

**不需要回放对照**：本批不含 O01/O02/O07/O11/O12/N04/N05 那些判定口径项，整改说明第 10 节要求的回放对照属于第二批。

## 8. 不确定项

1. **`tests/test_system_snapshot.py:154` 硬编码他人笔记本路径** `/Users/mymz/.kube/coroot-config`，任何机器上都失败。整改前就在。不属于这 6 条，**没动**。建议改成 `tmp_path` 造一个假 kubeconfig，需要确认后再做。
2. **O10 的 grace 窗口默认 900 秒是我定的**。整改说明只写「保留最后有效结果」+「只在模型未过期且可运行时放行提交」，没给数值。依据是：探测本身 2–5 分钟、TTL 300 秒、一次 Trial 约 20 分钟。**如果你认为该更短或更长，说一声，改一个常量的事。**
3. **O03 熔断阈值（瞬时类 3 次、冷却 300 秒）同样是我定的**，说明里没给数值。欠费和鉴权失败设成一次即开，是因为这两类再试不会变好。
4. **O03 的欠费识别靠 body 关键词**（`arrearage`、`insufficient_quota`、`欠费`、`余额不足` 等）。百炼那条是实测的；其他供应商的措辞是按常见写法列的，**没有实测样本**。新供应商接入时可能要补词。
5. **O18 的启动自检只查存在性，不查哈希**——镜像里没有源文件可以重新哈希。旧脚本（内容对不上）要靠部署时用构建元数据里的 `runtime_manifest` 比对。`deploy_boundary.sh` **我没改**，因为整改说明第 8 节要求构建部署都得先跟你确认；需要的话我把这一步加进去。
6. **`ToolCall` 加了两个可选字段**，虽然默认 `None` 不破坏既有构造，但 `ContractModel` 是 `extra="forbid"` + `frozen`。如果有外部消费者按精确字段集校验 `ToolCall`，会受影响。仓库内全量测试没有发现这种消费者。

## 9. 还没做的

- **没有部署**，没有构建镜像，没有跑任何评测（按整改说明第 5 节，这一批不部署）。
- 第二批（O01、O11、O07、O12、O13、O14、O17、重判发布）和第三批（用例设计）**未开始**。
