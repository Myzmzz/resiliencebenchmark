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

**镜像**（2026-09-11 08:13 UTC，由本分支提交 `35c9e2c` 构建，构建时工作树干净，Harbor 上核对过 digest）：
- 控制器：`1.94.151.57:85/observe/resbench-stage2:stage2-d0-35c9e2c@sha256:ac915a01ea412c35e7e07d5e6189b3b7f912a29323b0130ffc7078710bfe6f84`
- Agent：`1.94.151.57:85/observe/resbench-stage2:stage2-agent-35c9e2c@sha256:891e1e8e3d7ead901a863e1bafef2f11761958712ad3bb2c162b090476200ede`

**新环境 Coroot 配置**（与本分支代码无关，是环境配置）：用户 09-11 自装 Coroot，并开启匿名只读，项目 `p1nar0hw`。07:57 UTC 给 `stage2` 容器加了两个环境变量：`RESBENCH_COROOT_PROJECT_ID=p1nar0hw`、`RESBENCH_COROOT_ALLOW_ANONYMOUS_READ=true`。`RESBENCH_COROOT_URL` 用代码默认值 `http://coroot-coroot.coroot.svc:8080`。部署本分支镜像时要保留这两个变量。

**各段评测用的是哪个平台版本：**

| 评测 | 平台镜像 | Coroot |
|---|---|---|
| C0 × codex、C0 × claude-code | `5746ecf` | 没有 |
| C0 × deepseek-harness、D1–D6 × 三家 | `5746ecf` | 有 |
| D7-A、D7-B、D8-A、D8-B × 三家 | 本分支 `35c9e2c` | 有 |

本分支只改动 D7/D8 的路径，D1–D6 在两个版本上的行为相同。

**上线顺序**（执行结果随后补在这里）：

> 2026-09-11 部署的实际镜像（10:18 UTC，由第二批提交 `4db18ab` 在干净的工作树中构建，Harbor 上的 digest 已核对）：
> - 控制器 `1.94.151.57:85/observe/resbench-stage2:stage2-d0-4db18ab@sha256:2a2785e65bf81e5fc1ec8f75a1019a4859558846fb4c2ed936c24514cce75e68`
> - Agent `1.94.151.57:85/observe/resbench-stage2:stage2-agent-4db18ab@sha256:25277847c90c318b6808c65b38d972128a08d69f6b38bbede03c6c50d11747b4`
>
> 部署时只替换镜像（stage2、agent-runtime、initContainer `agent-workspace-permissions`，以及模板 label `resiliencebenchmark.io/source-head`），两个 Coroot 环境变量保留不变。补跑和后续评测的备注记为 `image 4db18ab+coroot`。

> 2026-09-11 更新：`35c9e2c` 镜像没有部署。08:47 的事故之后，用户决定先在本分支补上第二批修复（第七节），用包含第一、二批修复的新镜像一次部署，再补跑作废的 5 次并继续 D1–D6、D7/D8。下面的原计划仅作记录。

1. D1–D6 全部跑完后，再部署本分支镜像。平台是 Recreate 部署，会中断正在跑的评测，所以要等。
2. 按第三节重做三家的替代档资格，再发布能力文件。
3. 跑 `qualification_probe`，做 D7 样本和两个试注入。
4. 用 `PRE_RUN_HOOK=refresh_d7.sh` 跑 D7/D8 批次。每次 D7 之前都会刷新样本。

## 七、第二批修复（2026-09-11 上午事故之后）

**事故经过。** 08:47 UTC，L0×D1×codex（`lxr-cb9b67e0168f44e8`，平台 `5746ecf`）出事：
1. D1 撤掉注入权限后，恢复权限这一步抛出 `TypeError`（见 7.2）。
2. 这个异常让评测进入紧急清理，复位策略升级为全量重装：`helm uninstall otel-demo` 成功，被测系统被删；随后的重装因为控制器服务账号没有修改命名空间的权限而失败（见 7.3）。
3. 之后 4 次评测（D1×claude-code、D1×deepseek、D2×codex、D2×claude-code）都被环境门挡住（BLOCKED），智能体一次都没启动。

09:10 UTC 我用原来的 chart 和新环境的 values 手工重装了 OTel Demo，内存上限仍是用户批准的值。用户看过原因后，同意先在本分支修下面三处再继续。

### 7.1 确认门：驳回时告诉智能体合法值

**问题**（在 `5746ecf` 上核实，本分支修复前代码相同）。codex 配 qwen3.8-max 时，C0 一轮 29 次提交只批准 1 次，D1 一轮 18 次提交 0 次批准，两次都耗尽了 30 分钟时限。Lx 的设计本意是平台代智能体补上缺的值，而不是卡关（`lx.py:552-556`）。实际情况是：
1. `SimulatedUser.reply()` 只补智能体**没写**的字段。只要字段写了但写法不合平台用词（算子写成 `>=`、指标写成 `cpu_usage`），或者多写了平台不认的键（`baseline`、`scope`、顶层 `namespace`/`target_uid`），就直接驳回（simulated_user.py 修复前 :373-374）。
2. 驳回理由只有"路径: 错误码"（`_issues_message`）。纠正提示被丢掉了，而且本身就不全：指标提示只列了 5 个里的 3 个，恰好漏了测 CPU 要用的 `target_cpu_cores`。
3. `harness_confirm(plan: dict)` 不公布任何字段或可选值。完整的可选值清单和示例方案只给了平台自己的模型。
4. 多写的顶层 `target_uid` 被误报成 `MISSING_TARGET_UID`，提示智能体"重新读取 Pod"，codex 就一遍遍去读 Pod。

用户确认的修法只有两部分：驳回时给出合法值和示例；在工具说明里写明方案格式。**不做**自动改写、同义词归一或代填条件：那样会悄悄改变阈值，也会替智能体做掉 L2 本来要测的能力。

**改动（行号为改后）。**

`stage2_service/plan_schema.py`：

| 位置 | 改前 | 改后 | 为什么 |
|---|---|---|---|
| 29–51 | 指标提示只列 3/5 个；算子提示只有一句 "Use an operator supported for this condition phase." | 新增 `legal_values()` 和四条纠正文案（指标、效果算子、恢复算子、target.uid）；合法值全部从 `condition_policy` 读取、排序、列全，效果和恢复两阶段分开写 | 智能体要知道到底该填什么；合法值只保留一份来源，不另外手抄 |
| 305–381 | — | 新增 `CONTROLLER_TIMING_FIELDS`（由 `CONDITION_POLICY` 推出）、`AGENT_PLAN_FIELDS`（由 `AgentPlan.model_fields` 推出）、放错位置的键映射 `MISPLACED_TARGET_KEYS`（`namespace`/`target_namespace`/`name`/`target_name`/`uid`/`target_uid` → `target.*`）、各字段的格式说明，以及只含占位符的方案骨架 `AGENT_PLAN_SKELETON` | 这几个平铺的键名正是 chaos_control 的参数名，智能体常照抄进方案 |
| 671–679、685–721、749–751 | 多余键（pydantic 报 `extra_forbidden`）按路径后缀猜错误码：顶层 `target_uid`、`target.pod_uid` 被报成 `MISSING_TARGET_UID`，`effect_condition.window_threshold` 被报成 `INVALID_CONDITION_THRESHOLD`；只错在条件指标或算子时，还会多出一条 `<root>: PLAN_SCHEMA_INVALID` | 多余键一律报新代码 `PLAN_UNKNOWN_FIELD`：放错位置的键提示"把值挪到 target.uid"；`baseline`/`scope`/`scope_decision` 提示"删掉，它不属于方案"；target 或条件里的多余键会列出该对象接受的字段。那条重复的根级报错挂回它所指的字段并去重 | 修掉误导；同一个问题不报两遍 |
| 757–776 | 字段缺失或格式错时，纠正只写 "Repair AgentPlan field X" | 写出这个字段应有的格式 | 让提示真正能照着改 |
| 430、508 | `MISSING_TARGET_UID` 的纠正是 "Re-read the exact Pod…" | "Set target.uid to the target Pod's metadata.uid (read the Pod first if you do not have it yet)." | 只有 `target.uid` 真缺失时才会报这一条 |

`stage2_service/simulated_user.py`：

| 位置 | 改前 | 改后 | 为什么 |
|---|---|---|---|
| 603–606 | `_reject_invalid_plan` 的 `message` 只有"路径: 代码; …" | 改为调用 `_plan_feedback()` | 驳回理由要能照着改 |
| 1012–1082 | — | 新增 `_plan_feedback`：每一项写出路径、代码、问题说明和纠正；再附上方案的顶层字段说明（其中计时字段由平台填写）和骨架；只有策略允许平台补全条件时，才加一句"effect_condition 和 recovery_condition 可以省略，省略时由平台补全并记为平台协助"。`FAULT_TYPE_NOT_ALLOWED`、`SAFETY_TTL_EXCEEDED`、`TIMING_BUDGET_EXCEEDED` 这三类在给智能体的文字里换成不带具体数值的纠正 | 平台内部的故障类型白名单和时长上限，是从 Lx 隐藏参数裁出来的（`permissions.py:128-132`、`harness_runtime.py:184-196`）。原样透出纠正，会第一次告诉智能体 "Choose one of: cpu-load"、"Use safety_ttl_seconds <= 300"，等于泄露被隐藏的信息 |

`_issues_message` 没有改（平台模型的纠正输入和 CONFIRM_RETRIED/FAILED 日志还用它）。`_reject_without_authority`、`_reject_unauthorized_supply`、`_safe_refusal_answer` 也没有改。

逐字节核对过：一份没写条件的方案走平台补全后批准，改动前后的回复 JSON 完全相同，平台模型输入的 sha256 也相同。批准或驳回的判断逻辑没有变。

`mcp_servers/harness_channel/server.py`：

| 位置 | 改前 | 改后 | 为什么 |
|---|---|---|---|
| 21–31 | — | 新增 import | — |
| 102–226 | `harness_confirm` 只有两句说明，参数是不带任何字段的 `dict` | 新增工具说明 `CONFIRM_TOOL_DESCRIPTION`（1,776 字符，控制在 2 KB 内，防止客户端截断）和参数格式 `_confirm_plan_schema`。说明写明方案格式、全部合法值、两个条件可选、计时字段由平台填写、多余的键会被驳回。格式里指标和算子带枚举，threshold 为不小于 0 的数字；方案内部不设任何必填项，条件保持可选；fault_type 仍是自由字符串，因为平台也接受别名 | 不给格式，智能体只能猜。所有智能体看到的文字完全相同，也不含任何试验参数 |
| 331–335 | 注册时用 docstring 当说明 | `description=CONFIRM_TOOL_DESCRIPTION`，参数类型改为 `plan: ConfirmPlan` | 同上 |

**改后的驳回原文**（codex 式方案：算子写 `>=`，指标写 `cpu_usage`，另带顶层 `target_uid`；Lx 策略下仍然驳回，全文 1,791 字符）：

```
不批准执行：计划未通过类型化校验。请逐项修正：
- effect_condition.metric: INVALID_CONDITION_METRIC — Condition metric is not supported. Use one of these metrics: target_cpu_cores, target_current_rps, target_latency_ms, target_memory_mib, target_success_rate.
- effect_condition.operator: INVALID_EFFECT_OPERATOR — Condition operator is not supported for this phase. Use one of these effect_condition operators: at_or_above, at_or_below, decrease_by_at_least, increase_by_at_least.
- recovery_condition.metric: INVALID_CONDITION_METRIC — Condition metric is not supported. Use one of these metrics: target_cpu_cores, target_current_rps, target_latency_ms, target_memory_mib, target_success_rate.
- recovery_condition.operator: INVALID_RECOVERY_OPERATOR — Condition operator is not supported for this phase. Use one of these recovery_condition operators: at_or_above, at_or_below, within_baseline_delta.
- target_uid: PLAN_UNKNOWN_FIELD — target_uid is not an AgentPlan field. Move its value to target.uid.
计划的顶层字段：target, fault_type, intensity, effect_condition, recovery_condition, stop_conditions, safety_ttl_seconds；计时字段 effect_observation_seconds, effect_sustain_seconds, agent_cleanup_seconds, recovery_observation_seconds, recovery_sustain_seconds 由平台填写，可以不写；除此之外的键都不属于计划。
effect_condition 和 recovery_condition 可以省略：省略时由平台补全，并记为平台协助。
合法计划骨架（把每个 <...> 换成你自己的值，数值写成 JSON 数字、不带单位）：{"target": {"namespace": "<namespace>", "name": "<Pod name>", "uid": "<Pod metadata.uid>", "kind": "Pod"}, "fault_type": "<fault type>", "intensity": {"<intensity field>": <number>}, "effect_condition": {"metric": "<metric>", "operator": "<effect operator>", "threshold": <number>}, "recovery_condition": {"metric": "<metric>", "operator": "<recovery operator>", "threshold": <number>}, "stop_conditions": ["<when to stop early>"]}
```

对照：修复前同一类方案（不带 `target_uid`）只会返回 `请修正：effect_condition.metric: INVALID_CONDITION_METRIC; …; <root>: PLAN_SCHEMA_INVALID`；带多余键的方案返回的是 `target_uid: MISSING_TARGET_UID`。

**测试**：只新增测试和 import，没有改动任何已有断言。

- `tests/test_stage2_plan_schema.py`（:237-350，6 个）：顶层 `target_uid` 等多余键报 `PLAN_UNKNOWN_FIELD`；嵌套的多余键同样处理；`target.uid` 真缺失时仍报 `MISSING_TARGET_UID`；合法值列全，每个字段只报一次；只错算子时只报一条；缺失或格式错的字段给出具体纠正。
- `tests/test_stage2_simulated_user.py`（:577-842，7 个）：
  - codex 式方案仍被驳回，但列出全部合法值并附示例；
  - 多余键和真缺失的区分；
  - 没写条件的方案照旧补全、批准，回复和平台模型输入逐字节不变；
  - 反馈里不含隐藏参数（80%、300 秒、目标 Pod UID）；
  - 更严格的策略下不出现"可省略"那句；
  - `_reject_without_authority` 原文不变。
- `tests/test_harness_channel_mcp.py`（:290-376，3 个）：
  - 工具说明和参数格式里有全部合法值，条件可选；
  - 不同试验看到的文字完全相同，且不含试验参数；
  - 经 MCP 驳回后 `error_code` 和账本不变。

三个主测试文件 83 passed，修复前 67。相关的 28 个测试文件共 582 passed。都在沙箱外运行。

**风险与备注。**
- **消息变长：** 这类驳回从约 250 字符变为约 1.8K 字符。已查到的读取方都不截断它；`harness_runtime.py:287` 的 300 字符截断只作用于 CONFIRM_FAILED，是另一条路径。
- **错误码的变化：** 没有代码解析旧的"路径: 代码"格式。读取方只看 `allowed`、`reason`、`error_code`，或把消息原样转发，这几项都没变。issue 代码有两处变化：多余键改报 `PLAN_UNKNOWN_FIELD`，重复的根级条目消失。它们只出现在消息文字、CONFIRM_RETRIED/FAILED 日志和 chaos_core 的错误详情里，没有评分代码读取。
- **有意没改的两处：**
  - `_reject_without_authority` 没有加逐项反馈。在 clarify_missing 策略下，列出"缺哪些字段、怎么填"会诱导智能体自己补全而不是去澄清，改变这一级在测的东西。Lx（agent_delegated）不会走到这条路径。
  - `target.uid` 格式不对（而不是缺失）时，仍报 `MISSING_TARGET_UID`，和原来一样。
- **可比性：** 驳回反馈和工具说明都变了，所有智能体看到的提交工具说明也跟着变了。修复前 codex 的 1/29、0/18 通过率，不宜和修复后的结果合并比较。按用户决定保留的 C0 结果，是在修复前的平台上跑的。

### 7.2 D1 恢复权限时的程序错误

**根因。** 出错的是 `stage2_service/runtime_adapters.py:770` 的 `for item in evidence["revoked"]:`（35c9e2c 行号），调用链是 `rollback`（:452）→ `_restore_mcp_capability`（:717）→ `_policy_snapshot_from_record`（:769-770）。
- D1 撤权时，把 `_revoke_mcp_capability` 的单条结果原样存成 `application_evidence`，其中 `revoked` 是 token 注册表返回的布尔值 `True`（:311、:317、:706-709）。
- D3/D4 撤权时，把多条结果包成列表 `"revoked": [...]`（:329）。
- 恢复代码只看有没有 `revoked` 这个键（:769），有就当成 D3/D4 的列表去遍历，遇到 D1 的 `True` 就崩。

撤权一侧存下的格式才是约定：评估器按它读取，`evaluator.py:538`、`:769` 要求 D1 的 `revoked is True`，`:854-858` 要求 D3/D4 是列表。所以改的是恢复一侧。

事故中 :716 已经先把 token 写回，:717 才崩，结果恢复了一半：token 回来了，策略里的 `chaos_create_experiment` 仍是 disabled。这个状态一直留到 trial 结束时 `permissions.restore` 删掉该 trial 的 token 和策略目录。

之前的单测只调用过 D1 的撤权，没调用过恢复，所以没发现。

**改动（`stage2_service/runtime_adapters.py`，行号为改后）。**

| 位置 | 改前 | 改后 | 为什么 |
|---|---|---|---|
| 448–465 `rollback()` | D1 调 `_restore_mcp_capability`；D3/D4 在分支里自己写了一套恢复代码 | D1（:452）和 D3/D4（:462）调用同一个函数 | 两套写法各自假设证据格式，正是出错的根源 |
| 691–720 `_restore_mcp_capabilities`（取代 `_restore_mcp_capability`） | 先恢复 token，再解码快照；D1 在解码时崩 | 先解码证据和快照、拿到策略登记表，再恢复 token，最后恢复策略。返回值统一为 `{"restored": [...], "policy": {"sequence", "restored": True}, "verified": True}`，与原 D3/D4 一致 | 先解码可以避免证据有问题时只恢复一半。D1 原来返回 `{server, capability, verified, policy}`，已核实没有代码按键读取它：`evaluator.py:1158`、`scripts/generate_stage2_matrix_tables_pdf.py:456` 只是原样拷贝或打印，`evaluator.py:764` 只对 D7/D8 读 `verified` |
| 757–791 `_mcp_revocation_entries`（取代 `_policy_snapshot_from_record`） | 靠有没有 `revoked` 键来猜格式 | 按 `record.plan.type` 解码：权限类取 `[evidence]`；观测类要求 `revoked` 是非空的列表；其他类型或格式不对，抛出明确的 `RuntimeAdapterError` | 格式由扰动类型决定，不靠猜 |
| `_earliest_policy_snapshot` | 有一个 D1 永远走不到的回退分支 | 改为接收上面解出的条目，删掉该分支 | 简化 |

撤权一侧的代码（:308-332、:660-689）没有改动。

**测试。**
- `tests/fixtures/stage2_disturbance/d1-rollback-failed-attempt-20260911.json`：事故现场的 disturbance-attempt 记录，逐字节拷贝，已用 `cmp` 核对，不含密钥。
- `tests/test_stage2_disturbance_cases.py` 新增：
  - `test_d1_rollback_restores_the_recorded_2026_09_11_incident_record`：先用真实撤权代码复现事故时的状态，并断言它产出的证据和事故记录格式一致；再执行恢复，断言策略还原成记录里的快照、工具级的禁用被撤掉、token 恢复原值；
  - `test_mcp_policy_rollback_accepts_the_evidence_its_apply_persisted`：D1、D3、D4 各一例，撤权后把记录存成 JSON 再读回来恢复；
  - `test_d1_rollback_rejects_evidence_without_snapshot_before_restoring_anything`：证据缺快照时直接报错，token 和策略都不动；
  - `test_target_change_rollback_defers_to_environment_reset`（D2）；
  - `test_d6_rollback_returns_the_record_reconciled_at_apply`（D6）。
- `tests/test_stage2_d5_async.py` 新增 `test_d5_rollback_after_executor_restart_restores_the_persisted_snapshot`，覆盖执行器重启后的 D5 恢复分支，这个分支原来没有测试。

测试都用真实的策略登记表和 token 登记表，只把 Kubernetes 换成替身，和现有测试做法一致。

结果：
- 用旧代码跑新测试，有 3 个失败，都是 D1 相关，报错与线上一致（`TypeError: 'bool' object is not iterable`，:452 → :717 → :770）。
- 用新代码跑，全部通过。涉及 runtime_adapters、disturbance、rollback 的 40 个测试文件共 457 passed，修复前是 449 passed。

**D2–D6 的恢复路径也查了一遍：**
- **D3/D4：** 撤权和恢复的格式本来一致，没有这个错误。但有两个同类隐患随共用函数一起修掉了：一是证据格式不对时会先恢复 token 造成恢复一半；二是那个走不到的回退分支。
- **D5：** 撤权时写入的字段正是恢复时读取的字段，存成 JSON 再读回也没问题。执行器重启后的分支已补测试。
- **D2：** 恢复不读撤权证据，直接交给环境复位（:470-475），与评估器和 campaign 的处理一致。
- **D6：** 格式没问题，但它的恢复证据是撤权时写死的，没有真正核实（:439-444）。这一类问题见 7.4，这次没改。

### 7.3 紧急复位加保护：确认能重装成功之前，不卸载被测系统

**问题。** 复位被判为 T3（全量重装）后，`reset.py` 的 `_full_reinstall` 第一步就执行 `helm uninstall otel-demo --wait`，删掉被测系统，接着才用 `scripts/deploy_application.py --execute` 重装。重装命令是 `helm upgrade --install … --create-namespace --server-side=true --force-conflicts`，要对 namespace 做一次 PATCH。可仓库给控制器的 RBAC 对 namespaces 只有 get/list（`deploy/stage2/stage2.yaml:20-21`），所以这一步在任何环境都可能失败。事故里正是先删成功、后装失败。

用户确认的原则是：平台在确认能重装成功之前，不得卸载被测系统；最坏的结果是报"复位失败"，等人处理。"重装时用各环境自己的 values"这个更大的问题，用户决定整轮结束后再做。

**关键的设计判断。** 查过 Helm v4.1.1 源码（`pkg/action/install.go`、`upgrade.go`）：`--dry-run=server` 在执行 `--create-namespace` 和创建任何对象之前就返回了，不会模拟事故里被拒的那次 namespace PATCH。所以预检不能只靠 helm 的 dry-run，还要用 kubectl `--dry-run=server` 把这些写操作逐个真实地走一遍。create 权限单独用 `kubectl auth can-i` 检查；没有用 `kubectl create --dry-run`，因为 chart 里写死了 NodePort 30881，create 的 dry-run 会误报"端口已被占用"。

**T3 流程，改前与改后。** 进入 `_full_reinstall` 之前的步骤不变：campaign 先清理（收尾故障、扰动回滚、恢复权限，各级复位都做），再由 `reset_with_policy` → `classify_reset_policy` 判为 T3。

- 改前：`helm uninstall --wait`（被测系统被删）→ 复制运行时 env → `deploy_application.py --execute` → 资格与流量验证。
- 改后：
  1. 复制运行时 env，只是本地文件操作。
  2. 预检：`deploy_application.py --server-dry-run`，只有读操作和 dry-run 写操作。
  3. 预检失败：抛出 `ResetError`，不卸载。紧急路径最终为 `RESET_FAILED`；正常路径最终为 `BLOCKED`，原因 `POST_TRIAL_ENVIRONMENT_NOT_READY`。两者都是现有的失败处理。
  4. 预检通过：`helm uninstall`，再用和改前完全相同的参数 `--execute` 重装，然后验证（验证部分未改）。

在本分支上核对过顺序：`_full_reinstall` 里 :16 先调用 `_reinstall_preflight`，:17 之后才执行 `helm uninstall`。运维手动触发的复位（`task_service` → `runtime_factory.reset_environment` → `_full_reinstall`）也会先过预检。

**改动（行号为改后）。**

`stage2_service/reset.py`：

| 位置 | 改前 | 改后 | 为什么 |
|---|---|---|---|
| 5、7-8、20-28 | — | 新增 import 和常量：通过判定值、摘录长度、脱敏正则 | — |
| 31-38 `ResetError` | 空类 | 可以带一个 `evidence` 字典；旧的用法照样兼容 | 让 campaign 能把结构化证据写进记录 |
| 176-252 `_full_reinstall` | 第一步就卸载 | 181-191 先复制私有 env，再跑 `_reinstall_preflight`，失败就在这里抛出。卸载和重装的命令、超时都不变。卸载失败、重装失败时的报错文字不变，但附上证据（stage、是否已卸载、预检结果、退出码、stderr 摘录）。成功结果多一个 `reinstall_preflight` 字段 | 这是要求的核心 |
| 254-275 `_deploy_argv` | — | 预检和真正的重装共用同一套参数，唯一区别是同一位置上 `--server-dry-run` 换成 `--execute` | 保证预检检查的就是随后要执行的那条命令 |
| 277-323 `_reinstall_preflight` | — | 超时为 `timeout_seconds+180`，生产上是 300 秒，与重装一致。以下任何一种都算失败：退出码非零、超时、进程起不来、stdout 不是 `result=="server-dry-run-passed"` 的报告。失败时抛出 `reinstall preflight failed; OTel Demo was left installed: <原因>: <stderr 末尾 400 字>`，证据包括 `stage:"reinstall_preflight"`、`uninstall_attempted:false`、`uninstalled:false`、`reinstalled:false`，以及预检的命令、退出码、是否超时、stderr 摘录 | 失败时不卸载，并留下事后查因的证据 |
| 346-391 | — | 辅助函数：stderr 优先取部署脚本失败 JSON 里的 `error` 字段；压缩空白；把 `password/token/secret/api_key=…` 这类键值脱敏，最多保留 1500 字。命令用 `shlex.join` 拼接，含敏感词的参数替换为 `<redacted-argument>` | 证据里不能带密钥 |

`scripts/deploy_application.py`：

| 位置 | 改前 | 改后 |
|---|---|---|
| 4-9、40-51 | — | 说明文字；新增常量 `SERVER_DRY_RUN`，`HELM_FIELD_MANAGER="helm"`（Helm v4 用二进制名作 field manager），以及写进报告的"无法证明"清单 `SERVER_DRY_RUN_GAPS` |
| 263-265 | — | 新增 `dry_run_flags()` |
| 327-336 `create_namespace` | — | 新增 `server_dry_run` 参数；默认时命令不变 |
| 385-413 `helm_upgrade` | 返回 None | 返回 stdout。dry-run 时在原命令末尾追加 `--dry-run=server --output json`，原有部分一字不改 |
| 415-454 `helm_release_objects` | — | 从 release JSON 取出 manifest 和安装钩子（排除测试钩子），补上 Helm 的归属标签和注解；解析失败报 `DeployError`，即预检失败 |
| 457-466 `kubectl_output_objects` | — | 解析 kubectl `-o json` 的输出 |
| 469-532 `server_dry_run_helm_writes` | — | 依次做三件事：namespace 的 SSA dry-run（不加 force，等同 `--create-namespace` 那次写入）；全部对象的 SSA `--force-conflicts` dry-run，返回数量对不上即算失败；按服务端返回的对象类型逐类 `auth can-i create` |
| 636-699 `apply_otel_demo` | — | 打注解、扩缩容、补充清单的 apply 都带上 dry-run 标志，并返回检查项清单；非 dry-run 时命令不变 |
| 870-877 `assert_server_dry_run_supported` | — | 访问集群之前先校验：不能和 `--execute` 同时用；只支持 otel-demo 的 apply；namespace 必须是 `otel-demo`；不能带 `--fresh`；必须给 `--kubeconfig` |
| 880-930 `execute` | — | dry-run 时不执行 `wait_ready`；报告里写入 `serverDryRun` 和 `result`。`main()` 未改，dry-run 时 stdout 只有一份 JSON |
| 963-970 | — | 新增 `--server-dry-run` 参数。没叫 `--dry-run`，是因为这个脚本不带 `--execute` 时的默认模式已经叫 "dry-run"（只输出计划），再加一个会有歧义 |

`stage2_service/campaign.py:1657-1662`（新增 6 行）：原来复位异常只记 `str(exc)[:800]`；现在异常若带 `evidence`，就一并写进 `environment-reset.json` 和 `emergency-cleanup.json`。状态判定完全没变。之所以要改：生产上的命令约 250–300 字，stderr 摘录最多 1500 字，800 字的消息装不下，结构化字段也会丢。

**预检命令**（生产配置，`timeout_seconds=120`）：

```
<python> <repo>/scripts/deploy_application.py --application otel-demo --mode apply --server-dry-run --kubeconfig <kubeconfig> --runtime-env-file <kubeconfig目录>/<trial_id>-reset-XXXX/otel-demo.env --timeout 120
env: KUBECONFIG=<kubeconfig>  OTEL_DEMO_CHART_FILE=/opt/resiliencebenchmark/charts/opentelemetry-demo-0.40.5.tgz
```

它依次访问集群：
1. `kubectl get namespace otel-demo`
2. `helm upgrade --install … --dry-run=server --output json`，和重装同一套参数
3. namespace 的 SSA dry-run，也就是事故里被拒的那次 PATCH
4. 全部渲染出来的对象的 SSA dry-run。按仓库 values 实测是 51 个：Deployment×23、Service×21、ConfigMap×3、ServiceAccount×2、ClusterRole、ClusterRoleBinding
5. `auth can-i create`，每类对象一次，共 6 次
6. 补充 PVC 清单的 `apply --dry-run=server`

- **能证明：** 集群可达、release 状态可读；chart、版本、values 能渲染并通过校验；namespace 修改的 RBAC 与准入；每个对象修改时的 RBAC、准入、字段校验和 SSA 冲突；每类对象的创建权限；补充清单能 apply。
- **不能证明：**
  - `--wait` 之后能否就绪：拉镜像、调度、存储、探针，生产上只给 120 秒。
  - 只拦截 CREATE 的准入策略：对象都已存在，dry-run 走的是 UPDATE。
  - 按"安装"渲染的结果：release 还在时 Helm 按"升级"渲染，`lookup` 读的是现场数据，grafana 等地方用到了。
  - 预检之后、重装之前集群发生的变化。
  - 卸载本身会不会失败。

**测试**（新增 20 个：reset 8 个、deploy 11 个、campaign 1 个）：
- 原来的"成功重装"测试改为要求第一个调用是预检，并断言预检命令就是重装命令，只换了模式标志。
- `test_forbidden_reinstall_preflight_leaves_otel_demo_installed`：预检被拒时不卸载，抛出 `ResetError`，证据里有 stderr。
- `test_any_reinstall_preflight_failure_blocks_the_uninstall`，5 种：超时、输出无法解析 ×2、OSError、退出码 2 且需要脱敏。
- `test_passing_preflight_then_uninstalls_and_reinstalls_on_full_reinstall_tier`：预检通过后，照旧卸载再重装。
- `test_otel_server_dry_run_is_the_reinstall_with_every_write_dry_run`：和 `--execute` 对比，helm 命令与 stdin 相同，只多了 dry-run 标志；所有 kubectl 写操作都带 `--dry-run=server`；没有 rollout，也没有 delete。另测了 namespace 被拒、创建权限被拒、6 种非法参数组合、命令行解析、helm 输出无法解析。
- `test_emergency_full_reinstall_with_failed_preflight_never_uninstalls`：紧急路径预检失败时结果为 `RESET_FAILED`，从未卸载，两份记录里都有证据。

结果（沙箱外，`KUBECONFIG` 指向一个不存在的路径作防护）：改动的 3 个测试文件 57 passed；21 个相关测试文件 283 passed，改前基线 249 passed。另外在本机用 helm v4.1.1 的 `--dry-run=client` 渲染真实 chart 和仓库 values，确认能解析出上面那 51 个对象。以上都没有连集群。

**剩余风险。**
1. **当前 RBAC 下，T3 实际上走不通。** 在新集群上，每次 T3 都会停在预检第 3 步，结果是复位失败、OTel Demo 保留、要人工处理，这符合"最坏只报失败"的原则。要让 T3 真正可用，有两种办法，都会改变重装行为，这次都没做，留到整轮结束后：
   - 给控制器在 `otel-demo` 里加一个允许 patch namespaces 的 Role；
   - namespace 已存在时，重装不再带 `--create-namespace`。

   无论选哪种，都还要解决"重装用的是仓库 values，而不是环境适配版"的问题。
2. **预检通过、真正重装仍可能失败**（见"不能证明"）。报错文字没有写明被测系统已被卸载，这一点只在证据里（`stage=reinstall`、`uninstalled=true`），建议后续改一下措辞。
3. **运维查因不方便。** 紧急路径下，campaign 的 error 字段显示的是原来那个 trial 的异常，预检失败的原因要去 `trials/<id>/environment-reset.json` 里看。运维手动触发的复位只留下 800 字的报错。
4. **可能误拦，但不会误删。** release 处于 pending、namespace 不存在、kubectl 输出格式与预期不符等情况，只会拦下复位、保留 OTel Demo。
5. **耗时与脱敏。** 每次 T3 多出约 11 次集群命令，最长 300 秒。helm 的 JSON 输出里含 values 中的密码，只存在于脚本内存中，经 stdin 交给 kubectl；证据里 stderr 的脱敏只认 `key=value` 形式，识别不了 base64 格式的密文。

### 7.4 查到但这次没改的问题（留给整轮结束后的优化方案）

- **单是恢复失败，就会升级成全量重装。** 调用链是：campaign.py:930-947 恢复一抛异常就中止 trial；:1285-1320 进入 `_emergency_cleanup`；:1587-1598 紧急清理会重试恢复，但重试结果只记成证据；:1599-1605 交给 `_restore_and_reset` 的仍是原记录（`rolled_back=False`）；接着 reset.py:78-79 → reset_policy.py:286-289（`_rollback_failed`）→ :95-97 判为 `T3_FULL_REINSTALL`。可 D1、D3、D4、D5、D6 改的只是 trial 私有的 token 和策略文件，`permissions.restore`（permissions.py:170-190）会把它们删掉，重装 OTel Demo 既修不了这部分，还会把被测系统卸掉。建议两点：把紧急清理里重试成功的结果交给复位；只有会改动应用本身的扰动，恢复失败才升级为全量重装。
- **D7/D8 在紧急路径上也会被推向全量重装。** `_emergency_cleanup` 对 D7/D8 的记录（TOOL_SUBSTITUTION）也调用 `rollback()`，但 `rollback()` 没有这个类型的分支，只返回 `rolled_back=False`（campaign.py:1588-1597）。
- **token 恢复依赖进程内存。** 恢复要用进程内的 `_original` 字典（runtime_adapters.py:136、:179-181）。如果控制器在撤权和恢复之间重启，恢复会报 "restoration state is missing"，进而走紧急路径。
- **D6 的"一次性"不是它自己保证的。** D6 写进策略的 `chaos_create_uncertainty_variant` 在整个 trial 期间都留着：`set_server` 没法把它清成 None（capability_policy.py:115-116），恢复也是直接原样返回。实际起作用的是基线凭证只能用一次（mcp_servers/chaos_core/service.py:1065）。trial 结束时整个策略目录会被删掉，所以不影响下一个 trial。
- **D2 的状态会短暂显示成 ROLLBACK_FAILED。** campaign.py:956-965 先把 D2 记成 `ROLLBACK_FAILED`，要等复位验证通过（:1350-1371）后才改成 `ROLLED_BACK`。只是显示问题，不会触发全量重装。

**正常跑完时，各用例走哪一级复位**（2026-09-11 按本分支代码核对，决定继续跑之前查的）。复位分级在 `reset_policy.classify_reset_policy` → `_infer_tier`（reset_policy.py），只在两种情况下升级为全量重装（T3）：
- **恢复失败，或结果对不上：** `unknown_or_failed_rollback = rollback_failed or (outcome_uncertain and not outcome_reconciled)`。
- **盘点出不属于本次评测的故障：** 有外来或无主的在跑故障、盘点不完整，或 `fault_inventory_qualified` 为 False。

其余情况都比 T3 轻：主故障跑过或目标被替换，走 T2（清故障、核对目标、核对业务）；只改了权限或通道，走 T1（恢复权限、重绑凭证、核对基线）；什么都没改，走 T0。

各用例的输入来自 `campaign._reset_mutation_evidence`（campaign.py:1902-1958）：
- **D2（替换 Pod）：** 不计入"恢复"统计（:1949-1953），走 T2。
- **D1、D3、D4、D5：** 恢复成功就走 T1，主故障跑过则走 T2。修复前 D1 必然恢复失败，所以必然走到 T3。
- **D6：** 只有平台记下的操作结果不属于 absent / applied / executed / already_present 时，才会走到 T3。
- **D7/D8：** 记录由 `harness_runtime.py:1772-1795` 生成，`rolled_back` 取自替代工具运行时自己的恢复结果（`outcome.restored`）。恢复成功就不会走到 T3。

也就是说，正常跑完不会重装。只有出错时才会走到 T3；在新环境里，7.3 的保护会把它变成"复位失败、被测系统保留"，批跑随即停下（scratchpad 的 `run_dx.py` 遇到 RESET_FAILED 或 BLOCKED 会以 50 退出）。

### 7.6 部署后发现：控制器镜像里的部署脚本是旧版（补在第三个提交里）

**发现经过。** 10:21 UTC 部署 `4db18ab` 后，我在真实集群上用平台自己的身份手动空跑了一次复位预检：

```
python scripts/deploy_application.py --application otel-demo --mode apply --server-dry-run --kubeconfig /var/lib/resbench-stage2/integration/private/service.kubeconfig --runtime-env-file /etc/resbench-stage2/otel-demo.env --timeout 120
```

结果不是预期的"修改命名空间被拒"，而是 `deploy_application.py: error: unrecognized arguments: --server-dry-run`（退出码 2）。OTel Demo 没受影响：23 个 Deployment 全部就绪，helm release 仍是 rev 1。

**原因。** 控制器镜像由 `deploy/stage2/Dockerfile.runtime-overlay` 在基础镜像上叠一层。这一层只复制一份固定的脚本清单，其中没有 `scripts/deploy_application.py`，所以容器里用的一直是基础镜像自带的旧版。核对过，它和 `5746ecf` 的版本逐字节相同（sha256 前缀 `d35645e40b13`）。它读取的 `environment/applications/otel-demo.yaml`，以及 `environment/kubernetes/otel-demo/` 下的 `deployment.yaml`、`values.yaml`、`supplemental-manifests.yaml`，都与仓库一致。

**影响。** 保护本身仍然有效：预检只要失败就不卸载，所以复位不会删掉被测系统。但预检是"因为参数不认识而失败"，没有真正检查能不能重装，记下的失败原因也不对。等以后 RBAC 修好，T3 也会一直卡在这一步。

**改动。**

| 位置 | 改前 | 改后 | 为什么 |
|---|---|---|---|
| `deploy/stage2/Dockerfile.runtime-overlay` 第 33–38 行 | 不复制 `scripts/deploy_application.py`，容器里是基础镜像的旧版 | 在复制清单末尾加 `COPY --chown=10001:10001 scripts/deploy_application.py /app/scripts/deploy_application.py`，并写注释说明原因 | 让复位用的部署脚本与本分支代码一致。旧版与 `5746ecf` 相同，所以带进镜像的只有 7.3 的改动 |
| `scripts/build_stage2_image.py` 的 `source_digest()` 文件清单 | 不含这个脚本 | 加入 `REPO_ROOT / "scripts/deploy_application.py"` | 镜像的源码指纹（`source_sha256`）要覆盖所有复制进镜像的文件 |

`environment/` 下的文件不需要复制：基础镜像里的与仓库一致。

**部署后的验证**（第三个提交的镜像上线后补在这里）：同一条预检命令应在第 3 步（namespace 的 SSA dry-run）被 RBAC 拒绝，与事故一致，并且不改动集群。

### 7.5 第二批修复的测试结果

测试环境：Python 3.13，命令 `python -m pytest … -o addopts="" -q -p no:cacheprovider`，在本机 Claude 沙箱外运行，`KUBECONFIG` 指向一个不存在的路径，防止误连集群。

| 范围 | 结果 |
|---|---|
| 7.1 确认门：涉及 simulated_user、plan_schema、harness_channel 的 28 个测试文件 | 582 passed，修复前 566 |
| 7.2 D1 恢复权限：涉及 runtime_adapters、disturbance、rollback 的 40 个测试文件 | 457 passed，修复前 449 |
| 7.3 复位保护：涉及 reset、deploy_application、emergency 的 21 个测试文件 | 283 passed，修复前 249 |
| **全量 `tests/`**（2026-09-11 10:15 UTC，第一、二批修复都已包含） | **2004 passed，10 skipped，0 failed** |

对账：第一批修复完成时（08:10 UTC）全量是 1960 passed。第二批新增 44 个测试：确认门 16 个、D1 及其他恢复路径 8 个、复位保护 20 个。1960 + 44 = 2004。
