# Stage-2 Dx 轮修复记录（2026-09-11）

- 分支：`codex/stage2-dx-round-fixes-20260911`，从 `5746ecf`（`codex/stage2-d0-integration` 当时的 HEAD）拉出，独立 worktree `resiliencebenchmark-stage2-dx-round-fixes`。
- 目的：让新环境的 Dx 轮能跑 D7/D8。用户 2026-09-11 的决定：
  1. 评测接口要把"替代工具提示用哪个版本"传下去（本文第一节）；
  2. 这里所有评测都不考虑 BladeAI，它的接入另行修复（第二节）；
  3. D7/D8 的替代工具资格文件改为**自动生成**，既不手写，也不去掉检查（第四节）；
  4. D7/D8 的 A、B 两个版本都跑；已经跑完的两次 C0（codex、claude-code）保留。
- 影响范围：只影响 D7/D8。`code_execution` 只在 D7/D8 的门槛里用到（`task_service.py` 的 `_capability_loss_gap`），新加的 Lx 字段对其他用例必须为空。C0、P1、P2、D1–D6 的行为不变。
- 行号均为本分支改动后的文件行号。

## 一、评测接口把 D7/D8 的提示版本传下去

**问题。** `POST /api/v1/stage2/lx/runs` 能选 `case=D7/D8`，但 `LxService.create_run` 组装任务请求时没有带 `tool_substitution_variant`。任务服务的校验（`task_service.py` 的 `Stage2TaskCreateRequest`，"D7/D8 requires tool_substitution_variant"）因此把每一次 D7/D8 都拒成 422。Lx 请求模型也没有这个字段，调用方想传也传不了（模型 `extra="forbid"`）。

**改动（`stage2_service/lx.py`）。**

| 位置 | 改前 | 改后 | 为什么 |
|---|---|---|---|
| 30、33 | — | 引入 `ToolSubstitutionVariant`、`CAPABILITY_LOSS_CASE_IDS` | 字段类型与"哪些用例需要它"都沿用任务服务的定义，不另起一套 |
| 169–180 | `LxRunRequest` 没有该字段 | 新增可选字段 `tool_substitution_variant: "A" \| "B" \| None`，附说明：A 在智能体如实求助后点名合法替代工具，B 只给中性提示 | 让调用方能按任务接口同样的语义选版本 |
| 221–239 | — | 新增校验 `validate_tool_substitution_variant`：D7/D8 必须带 A 或 B；其他用例（含 C0）带了就拒 | 在 Lx 这一层用调用方实际发的字段名报错，而且在生成 run id 之前就拒；其他用例带这个值没有意义，拒掉可以防止误传 |
| 592–594 | 任务请求里没有该字段 | `tool_substitution_variant=request.tool_substitution_variant` 透传给任务服务 | 这是 422 的直接原因 |

运行记录的 `configuration` 本来就保存整份请求（`request.model_dump`），新字段会自动出现在每次运行的摘要里，事后可以查到每次 D7/D8 用的是哪个版本。

**文档。** `docs/design/stage2-lx-manual-test-module-20260909.md` 第 5.2 节参数表加了 `case`、`tool_substitution_variant` 两行（253–254），并在"已取消参数"的 `case_id` 一行注明它已被 `case` 取代（263）。

**测试（`tests/test_stage2_lx.py`）。**
- 4：引入 `pytest`。
- 181–200：辅助函数 `_run` 增加 `harness`、`tool_substitution_variant` 两个关键字参数，默认值与原来一致，旧测试不受影响。
- 209–235 新增三组测试：
  - `test_d7_d8_require_a_tool_substitution_variant`：D7、D8 不带版本被拒；
  - `test_tool_substitution_variant_is_refused_outside_d7_d8`：C0、D1、D6 带版本被拒；
  - `test_d7_d8_variant_reaches_the_task_request`（D7-A、D7-B、D8-A、D8-B 四组参数）：版本确实到了任务请求，运行摘要里也能看到。

## 二、D7/D8 门槛不再算 BladeAI

**问题。** `_capability_loss_support` 遍历全部四家（`for harness in HarnessKind`），要求每家都有平台内反馈通道，且 `code_execution == "platform_sandbox"`，只要有一家不满足，D7/D8 对所有人都关闭。BladeAI 的接入正在另外修，它的描述符是 `controller_driven`、`code_execution="none"`，这一条就把 D7/D8 对另外三家也永久关上了。

**改动（`stage2_service/task_service.py`）。**

| 位置 | 改前 | 改后 | 为什么 |
|---|---|---|---|
| 50–62 | — | 新常量 `CAPABILITY_LOSS_GATED_HARNESSES = (codex, claude-code, deepseek-harness)`，注释写明是用户 2026-09-11 的决定 | 门槛的本意是参与对比的各家都能公平参加，BladeAI 这轮不参与对比。等它接好后，把它加回这个元组即可 |
| 870–884 | — | 新方法 `_capability_loss_gap(capability, require_qualified)`，返回单家不能参加 D7/D8 的原因，能参加时返回 None | 同一套判断，门槛和单家各用一次，避免两处写法走偏 |
| 889–902 | 遍历全部四家 | 只遍历 `CAPABILITY_LOSS_GATED_HARNESSES`；报错格式不变（`capability_probe_missing`，或 `<harness>: <原因>`） | 同上 |
| 860–866 | 只要"整体门槛"通过，**每一家**（包括 BladeAI）的可选用例里都会加上 D7/D8 | 还要求这一家自己的描述符也满足（`_capability_loss_gap(...) is None`） | BladeAI 退出整体门槛后，不能因为另外三家都就绪，就被顺带放行。它自己仍然跑不了 D7/D8 |
| 939–944 | 拒绝信息写"D7/D8 require all four Harnesses …"，原因只取整体门槛 | 写明对比组"(codex, claude-code, deepseek-harness) and the requested one"。整体门槛通过、但被请求的这一家不满足时，给出这一家自己的原因，例如 `bladeai: platform_sandbox_missing` | 之前的说法在排除 BladeAI 后不再成立。调用方需要知道具体是哪一家缺了什么 |
| 1093–1095 | `/api/v1/stage2/options` 的 `capability_loss` 只有是否可用和原因 | 加 `gated_harnesses` 列表 | 让调用方看得出"可用"是就哪几家而言的，BladeAI 不在其中 |

**测试（`tests/test_stage2_task_service.py`）。**
- 636：`test_api_exposes_options_cases_and_autonomy_cases` 的期望值加上 `gated_harnesses`。
- 943–962：原测试 `test_d7_d8_are_not_runnable_when_one_harness_lacks_platform_sandbox` 用 BladeAI 缺沙箱来证明"整体关闭"，在新规则下这正是不该发生的事。现改名为 `…_one_compared_harness_…`，改用 codex 缺沙箱来证明整体关闭，拒绝信息的断言同步更新。
- 965–1002：新测试 `test_bladeai_without_platform_sandbox_does_not_close_d7_d8_for_the_others`，验证：
  - BladeAI 缺沙箱时，D7/D8 整体仍然可用；
  - `gated_harnesses` 为三家；
  - BladeAI 自己的可选用例里没有 D7；
  - 用 BladeAI 提交 D7-A 被拒，原因是 `bladeai: platform_sandbox_missing`；
  - 用 codex 提交 D7-A 被接受，版本为 A。

## 三、通过 D7/D8 替代档资格，即授予"平台沙箱"能力

**问题。** D7/D8 门槛要求 `code_execution == "platform_sandbox"`，但资格代码有两处写死了 `"none"`：`capability_qualification.py` 里 base 和 WP8 两个描述符的构造处。模块说明也写明"不授予 D7/D8 能力"。

项目里本来就有一套替代档资格运行，即 `channel_qualification.py` 的 `substitution` 档。它依次做四件事：停用遥测，让智能体求助拿到 D7-A 提示，用 Coroot 查询，再在 `code_sandbox.run_python` 里跑一段无害代码。但它的记录里只有"失败原因"，没有逐项的正向证据；发布器也只收 base 记录。所以这项能力永远判不下来。

**判定规则（改后）。** 一家非 BladeAI 的智能体要得到 `platform_sandbox`，同一次发布里必须同时有它的两份记录：一份通过的 base 记录，一份核验通过的 substitution 记录。以下情况一律是 `none`，缺证据就不给：
- 只有 base 记录；
- BladeAI；
- 证据缺失或含糊。

证据只认控制器账本，不认智能体自己的说法。

**改动（`stage2_service/channel_qualification.py`）。**

| 位置 | 改前 | 改后 | 为什么 |
|---|---|---|---|
| 88–103 | — | `SUBSTITUTION_CHECKS`：9 项正向检查，依次是遥测停用、提示往返、Coroot 查询、沙箱运行、通知回执、结果提交、调用顺序、工具证据、网关证据 | 记录里的 `passed` 只表示"没有失败原因"，旧版判定程序产出的记录也满足这一点，不能当作正向证据 |
| 169、202 | — | 记录新增 `substitution_checks` 字段并写入 JSON（base 记录里是空的 `{}`） | 发布器要逐项核对 |
| 728–799 | 判定程序只产出失败原因 | 逐项写出正向结果（`passed` 的含义不变）。只有沙箱检查为真时，才写出 `observed_capability_evidence.sandbox_run`，内容包括调用 id、调用起止序号、账本里 `SANDBOX_RUN` 的序号、状态、退出码、是否截断、代码哈希、产物引用。并按 base 的做法补上网关字段 | "代码确实在平台沙箱里跑过"的唯一证据，是沙箱服务自己写进账本的 `SANDBOX_RUN` |
| 417–420 | — | `run_one` 对 substitution 档用网关复核结果覆盖 `gateway_evidence_verified`，与 base 做法一致 | 网关证据由 `run_one` 另外复核 |

**改动（`stage2_service/capability_qualification.py`）。**

| 位置 | 改前 | 改后 | 为什么 |
|---|---|---|---|
| 1–9 | 说明写"不授予 D7/D8 能力" | 改为"核验过的 substitution 记录授予 platform_sandbox；BladeAI 始终是 none；不授予 D0 资格" | 与新行为一致 |
| 14、36–38 | — | 新增常量 `SUBSTITUTION_QUALIFICATION_TYPE`，并有测试把它与判定程序的常量钉在一起 | 用字面值，是为了只发布 base 时不必导入判定程序模块，那会拉起整套运行时 |
| 86–176 | `_native_tool_modes` | 抽成通用的 `_canonical_tool_evidence`：按服务列表放行，额外保留控制器端的工具返回内容。base 的覆盖检查和报错文字不变 | substitution 档要核对 coroot_ro、code_sandbox 等更多服务的调用 |
| 179–231 | — | 抽出 `_read_record`、`_GatewayIdentity`、`_verified_gateway_identity`，都是原样搬出来的代码 | base 与 substitution 共用 |
| 234–277 | — | `_entry` 改为接收已读好的记录；base 分支逻辑不变，只在写死 `none` 的地方加了注释 | 同上 |
| 279–333 | — | 新增 `_substitution_proof`，逐项核对：类型、档位、通过状态；失败原因和清理错误都为空；`scored_as_d7` 为假；遥测停用响应和提示内容与预期逐字一致；9 项检查全部为真；网关路由和请求回执与当前网关配置一致；归档里的工具调用逐一对得上，且没有写操作。BladeAI 的记录直接拒绝 | 只凭平台记录判定 |
| 336–371 | — | 新增 `_verified_sandbox_run`，要求：`sandbox_run` 属于第一次 `run_python` 调用；账本序号严格落在这次调用的起止之间；记录、控制器归档、智能体自己的调用记录三处都显示成功（`ok`、退出码是整数 0、未截断） | 防止把别的调用或失败的运行当成证据 |
| 374–384 | — | 新增 `_with_platform_sandbox`：在同一家已通过的 base 条目上改成 `platform_sandbox`，并附上证明 | 能力只在 base 资格之上叠加 |
| 606–640 | 发布器只收 base 记录，substitution 记录会被拒 | 两类记录分开收集：一家最多一份 substitution；substitution 必须配同一家已通过的 base；只要有一份提交的 substitution 没通过，整次发布就被拒，原文件保持不变 | 失败即关闭。操作上只把通过的记录交给发布器 |

`scripts/publish_harness_capabilities.py` 第 2 行、21–23 行：说明文字和 `--record` 的帮助加上 substitution。preflight 读取器（`capability_preflight._qualified_descriptor`）本来就原样透传 `code_execution`，没有改。

**测试。** 共新增 26 个用例。
- `tests/test_capability_qualification.py`：
  - substitution 通过即得到 `platform_sandbox`，发布后经读取器读回仍是这个值，codex、claude-code、deepseek 三家都覆盖到；
  - 14 种缺证据的情况各自按指定原因被拒，读回仍是 `none`：没有 `run_python`、旧格式记录、沙箱检查为假、缺沙箱证据、账本事件在调用之外、证据属于别的调用、归档里退出码为 1、输出被截断、退出码是布尔值、智能体侧缺记录、有失败原因、档位不符、网关已过期、试验 id 不符；
  - 只交 substitution、缺 base、重复记录，都被拒；
  - BladeAI 的 substitution 记录被拒。
- `tests/test_channel_qualification.py`：
  - 用真实判定程序产出 base 和 substitution 记录，发布后读取器保持 `platform_sandbox`；
  - 判定程序会写出 9 项检查和沙箱证据；另有 4 种沙箱失败情形，此时不写沙箱证据；
  - `run_one` 用网关复核结果覆盖对应检查项。
- `tests/test_stage2_capability_preflight.py`：记录缺失或未通过时，即使文件里写着 `platform_sandbox`，读回也是 `none`。

直接相关的 4 个文件共 133 passed，改前是 107。在本地沙箱里跑更大范围的 79 个文件，结果是 56 failed、11 errors，和干净的 `5746ecf` 完全相同。原因是本地沙箱不允许写 `/tmp`、不允许建 socket，与本次改动无关。第五节会在沙箱外重跑全量确认。

**上线步骤**（第六节记录执行结果）：
1. 部署包含本改动的控制器镜像。
2. 在 `stage2` 容器里，对 codex、claude-code、deepseek-harness 各跑一次 `scripts/qualify_agent_channel.py --profile substitution --model qwen3.8-max`。
3. 用 `scripts/publish_harness_capabilities.py` 一起提交三家现有的 base 记录（`/var/lib/resbench-stage2/integration/qualification/base-<harness>-qwen38max-01/base-channel-qualification-<harness>.json`）和新的 substitution 记录。

前提：没有任务在跑，Coroot 能匿名只读，网关配置没有变（`403933e4…`）。

**风险与备注。**
- 发布器要求智能体自己的调用记录里也有完成的 `code_sandbox.run_python`。如果某家的原生工具名归一化后对不上，发布会被拒，这家保持 `none`。Lx 轮见过 qwen 在 codex 里发出不带服务前缀的工具名，届时看 `canonical-events.jsonl` 里智能体那一侧的记录。
- `SANDBOX_RUN` 只按时间窗口和调用来绑定，不比对代码哈希，因为调用参数可能被脱敏。
- preflight 返回的内容里会多出 substitution 证明：记录路径、试验 id、代码哈希、产物引用。都不含密钥。
- `docs/deploy/stage2-base-channel-qualification-20260906.md:75` 说 WP11 沙箱资格"仍需后续接线"，这一说法已过时。那是一份带日期的执行记录，没有改。

## 四、D7/D8 替代工具资格文件自动生成

**问题。** D7/D8 触发扰动前，平台要读一份私有的资格文件 `capability-loss-qualification.json`，证明备用工具在当前环境真的能用。读取方是 `capability_loss/factory.py` 的 `_qualification`、`_d7_precheck`、`_d8_precheck`，路径由 `STAGE2_SUBSTITUTION_QUALIFICATION_FILE` 指定，默认 `<private>/capability-loss-qualification.json`。

文件里要有两类证据：
- D7 的"历史样本"：备用观测工具查到过这次目标 Pod 的数据。D7 停掉的是 telemetry_ro 和 coroot_ro 中智能体先用的那个，另一个就是备用，所以两个都要有样本。
- D8 的"试注入记录"：备用注入通道真的建出过故障，也真的撤掉了。

仓库里没有任何代码生成这份文件。缺了它，D7/D8 在扰动触发时就被判无效，原因码是 `qualification_evidence_missing_or_invalid`、`alternative_canary_missing` 等。用户决定自动生成：不手写，也不去掉这道检查。

**新增文件。**
- `stage2_service/capability_loss/qualification_probe.py`（1323 行）。放在 `stage2_service` 包里而不是 `scripts/` 下，是因为控制器镜像的 runtime overlay 只复制固定几个脚本，但会带上整个 `stage2_service` 包。
- `tests/test_capability_loss_qualification_probe.py`（565 行）。

没有改动任何已有文件。

**用法。** 在 `stage2` 容器里运行：

```
python -m stage2_service.capability_loss.qualification_probe --namespace otel-demo --target cart --ttl-hours 24
```

- 不带参数：D7 样本和两个试注入都做。
- `--d7`：只刷新 D7 样本。
- `--d8`：只做两个试注入。
- `--d8-server chaos_mesh_control` 或 `--d8-server chaos_control`：只做其中一个试注入（可写多次）。
- `--dry-run`：只列出计划，不做任何动作。

标准输出是一行 JSON 摘要，失败原因写到标准错误，退出码 0 表示成功、1 表示失败。

**它和评测抢同一把锁。** 它持有平台运行锁 `/run/resbench/stage2-active-run.lock`，这也是每次评测要拿的锁。有评测在跑时它拒绝启动；它在跑时，评测也启动不了。所以它只能在两次运行之间执行，批跑脚本用 `PRE_RUN_HOOK` 在每次 D7/D8 之前调用它。

**D7 样本怎么取。**
- 找目标 Pod：直接调用 `KubernetesTrialPreparer._resolve_target`，也就是评测绑定目标用的同一段代码，取唯一一个 Ready 且带 `app.kubernetes.io/component=cart` 或 `opentelemetry.io/name=cart` 标签的 Pod。
- 取配置：Coroot、Prometheus、Chaos Mesh 的配置都经 `Stage2RuntimeConfig.from_env()` → `build_runtime(...)` 取得，和评测时挂给智能体的 MCP 服务是同一份，没有另起一套。
- Coroot：通过 coroot_ro 的实现，查这个 Pod 容器最近 600 秒的 `container_resources_cpu_usage_seconds_total`。
- Prometheus：通过 telemetry_ro 的 `telemetry_prom_metric_range` 实现，查 `container_cpu_usage_seconds_total{pod=<pod>}`，步长 60 秒。

同时满足下面三条，才写一条样本 `{server, target_uid, observed_at, record_ref}`：
- 返回的序列标签确实指向这个 Pod；
- 在 Pod 创建之后、查询窗口之内有有效数据点；
- 查询前后 Pod 的 UID 没有变。

`observed_at` 取最后一个数据点的时间。原始查询、参数、返回摘要写进证据文件，失败的探测也写。

**D8 试注入怎么做。** 两个试注入按顺序执行，都走平台正式的执行通道，正式评测要过的安全检查一道不少：
1. 申请基线凭证。要看到应用自己的业务流量才会发放，和评测一样。
2. `validate_plan`。
3. `create_experiment`。
4. 每秒轮询一次、最多 12 秒，等平台报告"已生效且在运行"。
5. 在 `finally` 里一定销毁，身份用 `CONTROLLER_FALLBACK`。
6. 核验 8 项清理事实：
   - 销毁响应确认对象已不存在；
   - 账本状态为已销毁；
   - 执行身份读不到这个对象；
   - 清理身份读不到这个对象；
   - 这次运行没有留下任何资源；
   - Pod 上没有遗留围栏标签；
   - Pod 的 UID 没变；
   - Pod 仍然 Ready。

"已生效"和"销毁已核实"两项都成立，才写一条试注入记录。

| 备用执行器 | 故障 | 选择理由 | 探针中途被杀时靠什么收尾 |
|---|---|---|---|
| `chaos_mesh_control`（Chaos Mesh） | `NetworkChaos`：delay 1 ms，方向 to，mode one，时长 15 s。目标是用评测同款绑定找到的 cart Pod，靠 UID 围栏标签圈定 | WP8 资格运行用的也是 1 ms；影响是单个 Pod 出方向多 1 毫秒、最多 15 秒，不会重启容器 | Chaos Mesh 到 15 秒自动结束，平台 TIMER 再回收账本 |
| `chaos_control`（ChaosBlade） | `ChaosBlade` CR：scope pod，target cpu，action fullload，`cpu-percent=1`，账本时长 15 s，matcher 精确到这个 Pod 名 | 这台集群上 ChaosBlade 只验证过 cpu-load（1.8.0 operator 加 nsenter cgroup v2 包装），网络类故障从没跑过，可能打不上的试注入证明不了任何事。策略接受 0 < cpu_percent ≤ 100，而 ChaosBlade 这个参数要整数，所以 1% 是最小值 | ChaosBlade 的 manifest 不带时长，只能靠平台 TIMER 回收；只要还有 ChaosBlade CR，环境门就拒绝启动新评测，所以残留只会挡住下一次评测，不会悄悄影响评分。告警里给出 CR 名和 cleanup handle，方便手动删除 |

为什么要两个方向：L0 提示词只说"注入高 CPU 负载"，没有点名用哪个工具。D8 里 chaos_mesh_control 从一开始就挂着，智能体可能先用 Chaos Mesh 校验方案，这时 `runtime._other_execution_server` 会把 ChaosBlade 当成备用。

前一个试注入的清理没核实时，后一个直接跳过，不在同一个 Pod 上叠第二个故障。ChaosBlade 的核验比"没有非 Destroyed 阶段的 CR"更严：这次运行在任何阶段都不能留 CR。原因是共享的销毁逻辑本来就要求对象消失，而且 Destroyed 状态的 CR 同样会让环境门拒绝新评测。

**写文件。**
- 合并旧文件时，先丢掉以下旧条目：
  - 没有来源记录的（即手写的）；
  - 已过期的；
  - UID 已不存在的；
  - 被这一轮同一项探测失败推翻的；
  - 重复的；
  - 试注入不完整的。
- 按备用执行器分别合并：一个执行器失败，不影响另一个执行器仍有效的记录。
- 每条记录的产生时间和 `valid_until` 放在顶层 `provenance.entries` 里。原因是记录模型不允许多余字段，加了会被读取方静默丢弃。
- `expires_at` 取 `now + ttl` 与所有保留条目 `valid_until` 中较早的一个，这样只跑 `--d7` 不会给旧的试注入续期。
- 写入方式：同目录临时文件，设为 0600，fsync，再 `os.replace` 原子替换；父目录收紧为 0700，不能是符号链接，属主必须是当前用户；合并时加文件锁。
- 写完后用读取方自己的 `_qualification`、`_d7_precheck`、`_d8_precheck` 回读自检。

证据文件放在 `<private>/capability-loss/qualification-evidence/`，D7 为 `d7-<server>-<uid>-<时间>-<随机>.json`，D8 为 `d8-canary-<mesh|blade>-<时间>-<随机>.json`。`record_ref` 形如 `private://capability-loss/qualification-evidence/<文件>`。试注入的执行账本放在共享的 `private/chaos-control/active/<handle>.json`，这样进程中途退出时，平台回收也能找到它。

**测试**（15 个）：
- 完整一轮：生成的文件被读取方接受，coroot/telemetry 对应 UID 的 D7 预检通过，换一个 UID 就不通过；两个方向的 D8 预检都通过；ChaosBlade manifest 内容符合预期。
- 试注入失败（参数化 4 例）：对象仍被删掉，另一个执行器不受影响，不写记录。
- 按执行器合并（两个方向）：一边失败时，另一边的旧记录保留。
- 第一个试注入清理未核实时，第二个被跳过。
- Pod 被替换后跑 `--d7`：旧 UID 的样本被丢掉，试注入记录保留，过期时间不延长。
- 其他：文件权限 0600、目录 0700；过期后读取方拒绝；手写条目被丢掉；服务器没返回该 Pod 数据时不写样本；锁被占用时不运行也不写文件；dry-run 没有副作用。

13 个涉及 capability_loss 的测试文件在沙箱外共 241 passed。

**已知限制与备注。**
- **OOM 不会让样本失效**：OOM 只重启容器，Pod UID 不变；只有 Pod 被删除、驱逐、滚动更新或被 D2 替换，UID 才会变。本轮每次运行都是单独的 campaign，批跑脚本在每次 D7 前刷新样本，D2 换 Pod 不会让后面的 D7 失效。
- **智能体自选目标时没有样本**：由智能体自己选目标的评测，可能打到非 cart 的 Pod。Lx 的目标由控制器指定为 cart，所以不受影响。
- **账本标注**：共享执行服务把创建一步的操作者写死为 `AGENT_MCP`，销毁一步记为 `CONTROLLER_FALLBACK`。这只是账本上的标注，共享代码没改。
- **1% CPU 还没实跑过**：`cpu_percent=1` 走的代码路径和已验证的 80% 那次相同，只是数值不同。如果 ChaosBlade 不接受，试注入会被判失败、照样销毁、不写记录，届时再调高。
- 证据文件不会自动清理；`--ttl-hours` 限定在 (0, 168] 之间。
- **没加"开跑前清过期租约"**：它会回收所有已过期的账本条目，不只是试注入的，影响面太大。

## 五、测试结果

所有测试都用 Python 3.13 运行，命令是 `python -m pytest … -o addopts="" -q -p no:cacheprovider`，而且都在本机 Claude 沙箱外跑。沙箱内不能写 `/tmp`，也不能建 socket，会让一批与改动无关的测试失败。

| 范围 | 结果 |
|---|---|
| 第一、二节：`tests/test_manual_stage2_requests.py`、`tests/test_stage2_lx.py`、`tests/test_stage2_manual_postman_contract.py`、`tests/test_stage2_remediation_baseline.py`、`tests/test_stage2_task_service.py` | 93 passed |
| 第三节：`tests/test_capability_qualification.py`、`tests/test_channel_qualification.py`、`tests/test_stage2_capability_preflight.py`、`tests/test_bladeai_evidence_publication.py` | 133 passed（改前 107） |
| 第四节：`tests/test_capability_loss_qualification_probe.py` 加上另外 12 个涉及 capability_loss 的文件 | 241 passed |
| **全量 `tests/`**（2026-09-11 08:10 UTC，四节改动都已包含） | **1960 passed，10 skipped，0 failed** |

同一套全量测试在只有第一至三节改动时（07:53 UTC）是 1955 passed、10 skipped。多出的 5 个，是第四节后来补的 ChaosBlade 反向试注入测试。

## 六、部署与使用

（待补：镜像标签与 digest、Coroot 相关环境变量、资格重做记录、生成器运行记录、哪些评测运行用了哪个镜像。）
