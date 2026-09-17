# 副本并行 × BladeAI 黑盒 合并分支 · 改动记录（2026-09-15）

- 分支：`codex/stage2-fleet-bladeai-merge-20260915`
- 基线：`codex/stage2-replica-fleet-20260912`（56c4c8b）合入 `codex/bladeai-blackbox-impl`（5b2977c），共同基点 9cb52bc
- 目标（用户 09-15 定）：合并、修相关 bug，能设 5 个副本并行跑 BladeAI 评测即可；模型 gpt-5.5 且只走 nexustokenai；不追求边界条件
- 提交：436087d（合并）→ ad0838c（合并遗留修复）→ 90fdd30（BladeAI 上副本 + 网关）→ 本文档所在提交

## 一、合并本身（436087d）

| 位置 | 改前 | 改后 | 原因 |
|---|---|---|---|
| `scripts/qualify_bladeai_task.py`、`stage2_service/bladeai_qualification_runner.py` | fleet 分支为绑定副本命名空间改过；blade 分支删除 | 接受删除 | 这是进程内 WP8 资格认定的 CLI 与 runner，blade 分支已整体移除、改由 `capability_qualification.py` 基础通道认定；fleet 的改动无处可施加 |
| `stage2_service/contracts.py`、`stage2_service/harness_runtime.py`、`scripts/build_stage2_image.py` | 两边都改 | git 自动合并 | 改动区域不重叠；合并后 `harness_runtime.py` 的三处默认目标已用 `current_target_binding()` |

合并前实测：试合并后全量测试与两分支各自相比，**合并引入的新失败为 0**。

## 二、合并遗留修复（ad0838c）

| 位置 | 改前 | 改后 | 原因 | 测试 |
|---|---|---|---|---|
| `tests/test_bladeai_channel_only.py` | import 已删除的 `stage2_service.bladeai_worker` | 删除 | 测的是已删模块；它让 pytest 收集中止，45 条用例没跑 | — |
| `tests/test_stage2_simulated_user.py:10` | `from stage2_service.bladeai_shim import NATIVE_INTENSITY_FLAGS, parse_create` | 改从 `stage2_service.harness_adapters.bladeai_intensity` 导入；删除测已删垫片的 `test_vocabulary_chaosblade_command_is_one_the_blade_shim_accepts` | 同上 | 35 通过 |
| `fleet_service/scheduler.py:36-40` | `PLATFORM_REASON_CODES` 不含黑盒 BladeAI 的服务侧故障码 | 加入 `BLADEAI_SERVER_URL_MISSING`、`BLADEAI_SESSION_UNAVAILABLE`、`BLADEAI_GATEWAY_CONFIG_REJECTED` | 未命中默认判 agent：平台故障被记到智能体头上且不重试 | `tests/test_fleet_service.py:519-527` 参数化 3 例通过 |
| `stage2_service/target_binding.py:66-75`、`stage2_service/runtime_factory.py:325-328` | 副本上 `RESBENCH_SOURCE_ALLOWED_APPLICATIONS` = 命名空间名 `otel-demo-01` | 改用 `source_application`（即去掉副本后缀的 `bundle`，`otel-demo`） | 源码锁 `environment/shared/source-locks.yaml` 登记的是 `otel-demo`，`source_ro/core.py:344` 会拒绝所有请求——副本上智能体读不了源码 | `tests/test_stage2_target_binding.py:92-101` 通过 |

## 三、BladeAI 上副本 + 网关（90fdd30 及本提交）

| 位置 | 改前 | 改后 | 原因 |
|---|---|---|---|
| `fleet_service/manifests.py:25-66` | 无 | `BLADEAI_SERVER_PORT=8399`、bundle 路径、专用 SA 令牌挂载点、`bladeai-server` 启动脚本（生成指向 `kubernetes.default.svc` 的 kubeconfig 后 `exec blade-ai server`） | 黑盒运行时强制要求 `RESBENCH_BLADEAI_SERVER_URL` 指向"每试验专用"的 server（`harness_runtime.py` `_bladeai_http_session`），两个分支都没有部署任何 server |
| `fleet_service/manifests.py:288` | 控制器容器无此变量 | `RESBENCH_BLADEAI_SERVER_URL=http://127.0.0.1:8399` | 同上；一个 slot 一次只跑一个试验，所以一 slot 一 server 即一试验一 server（`/cancel` 会取消整台 server 的任务） |
| `fleet_service/manifests.py:398-437` | 无 | 新容器 `bladeai-server`：用控制器镜像（内含 0.7.0 包），环境变量照搬 09-13 L0 验证时的设置，**但不再设 `BLADE_AI_SKILL_SCRIPT_DEFAULT_ALLOW=false`**（沿用 BladeAI 默认 true） | 09-13 的 L0 在批准后卡死，服务端报 `no catalogue use-case loaded`：技能脚本被禁时规划拿不到用例目录，在 planning 与 agent_loop 间空转 |
| `fleet_service/manifests.py:457-463`、`fleet_service/contracts.py:109-112` | agent-runtime 的 AppArmor 注解写死 | 新增 `FleetConfig.agent_runtime_apparmor_profile`，默认值不变，置空则不加注解 | 老集群只有 tcse-v100-03 装了该档案且 CPU 请求已占 90%，副本必须调度到另外两台 |
| `fleet_service/manifests.py:525-529` | — | 新增 `bladeai-state`、`bladeai-tmp` emptyDir 与 `bladeai-sa-token` Secret 卷 | server 的 HOME/配置/记忆目录与令牌 |
| `deploy/stage2/bladeai-server-rbac.yaml`（新增） | — | SA `resbench-bladeai-server` + 令牌 Secret；ClusterRole：chaosblades 读写、pods/deployments 等只读；`default` 命名空间内 pods/exec | BladeAI 用原生 provider 注入（CR + chaosblade-tool 内执行 blade），控制器身份对 chaosblades 只有 get/list |
| `deploy/stage2/Dockerfile.agent:19-23,81-90`、`scripts/build_stage2_image.py:28-35` | 钉 v0.6.2 / d8c5473 / 0.3.0；COPY 7 个已删的 `bladeai_*` 模块与两个垫片 | 钉 98a9ddb / 0.7.0；删掉这些 COPY 与垫片安装步骤 | 原样构建必失败；所有 v0.x 标签都仍指 0.3.0，所以直接用 commit |
| `deploy/stage2/Dockerfile.runtime-overlay:30-31` | COPY 两个已删脚本 | 删除 | 同上 |
| `tests/test_stage2_image_build.py`、`tests/test_stage2_agent_runtime_assets.py`、`tests/test_bladeai_agent_image_contract.py` | 断言旧 COPY 行存在 | 改为断言不存在，钉 0.7.0 | 跟随上面两项 |
| `deploy/stage2/litellm/config.yaml:25-37` | `gpt-5.5` 走 aigcbest | 走 nexustokenai（`NEXUSTOKENAI_API_KEY`） | 用户指令 |
| `stage2_service/capability_qualification.py`：`_entry` 及其上方新增的 `BLADEAI_BLACKBOX_*` 常量（67fed38） | BladeAI 的基础认定记录必须通过全部 `BASE_CHECKS`，包括 MCP 通道类检查 | 设了 `STAGE2_BLADEAI_BLACKBOX_QUALIFICATION` 后，BladeAI 只需满足：会话完成、网关证据已验证、没有清理错误；跳过 `_native_tool_modes`；发布的记录带 `acceptance=BLADEAI_BLACKBOX_HTTP_CHANNEL` 和 `skipped_checks`。不设这个变量时行为不变 | 黑盒 BladeAI 的确认走 HTTP interrupt，结果从 SSE 的 `result` 事件取；平台也从不给常驻 server 写 mcp.json（`harness/bladeai/mcp.json.template` 只有测试引用）。所以 MCP 通道类检查永远是 false，新 slot 上的 BladeAI 永远判不合格。09-13 的 L0 能过门禁，是因为 bbverify 的能力文件里还留着 09-08 旧 WP8 链路的记录 |
| `stage2_service/runtime_factory.py`：`Stage2System.__init__` 里探测函数的选择；新增 `GATEWAY_MODEL_PROBE_ENV`、`_skipped_model_probe_runner`；`_model_probe_statuses` 的可运行条件 | 控制器启动时、以及每次 300 s 缓存过期后，用 `scripts/probe_models.py` 对网关里全部 7 个别名发真实请求（工具调用、流式、结构化输出）；探测没通过的模型判为不可运行 | 默认不再探测：只读网关 `/v1/models`（不耗 token），列表里有的别名记为 `not_probed` 并判为可运行；模型实际不可用时，由试验报出上游错误。`STAGE2_GATEWAY_MODEL_PROBE=on` 可恢复原探测；显式注入的探测函数（测试用）照常使用 | 用户 09-15 要求：探测浪费 token 且没必要。实测 5 个 slot 同时探测 7 个别名，把 nexustokenai 打到限流，gpt-5.5 因此被判不可运行，挡住了本轮 BladeAI 批次 |
| `stage2_service/gateway_config.py`：`_reject_active_routing_policy` 与新增的 `RETRY_ONLY_ROUTER_SETTINGS`；`deploy/stage2/litellm/config.yaml` 新增 `router_settings` | 网关配置里只要出现 `router_settings` 就整体拒绝；网关对任何路由都不重试（`num_retries: 0`） | `router_settings` 只允许 `retry_after`、`model_group_retry_policy` 两个键，其余（fallbacks、routing_strategy 等）照旧拒绝。只给 gpt-5.5 配重试：429 重试 4 次，上游 5xx 重试 3 次，每次间隔 5–8 s；超时不重试，保证单次请求落在中继 180 s 预算内。其他路由仍是 0 次重试 | 用户 09-15 选定"网关退避重试"：第二轮 5 路并行时 nexustokenai 返回 429/500/502，BladeAI 自带的 2 次快速重试扛不住，结果一次都没注入成功 |

## 四、测试

- 门禁（提交 90fdd30 前）：`test_fleet_service`、`test_stage2_image_build`、`test_stage2_agent_runtime_assets`、`test_bladeai_agent_image_contract`、`test_render_litellm_gateway`、`test_stage2_target_binding`、`test_stage2_simulated_user` 全部通过。
- 全量（沙箱外）：见第五节补记。

## 五、部署与验证（老集群，用户 09-15 指定）

### 5.1 部署物（老集群 `~/.kube/coroot-config`，命名空间 `resiliencebenchmark-system`）

| 对象 | 值 |
|---|---|
| 控制器镜像 | `1.94.151.57:85/observe/resbench-stage2:stage2-d0-90fdd30-bladeai070@sha256:16eff16139a1a6edab36d563361ef4d98eb3cd7ba800b70e454fbb34cc4ff4e4`（label source-head = 90fdd30） |
| Agent 镜像 | `stage2-agent-60309d3@sha256:cae928c7…`（沿用 fleet 分支的，未重建） |
| LiteLLM 镜像 | `resbench-litellm:1.92.0@sha256:237ed94c…`（Harbor 上的 `1.92.0` 标签已被覆盖成别的 digest，只能按 digest 钉） |
| 网关配置 | ConfigMap `litellm-config-fleet`：本分支 `config.yaml` + `gateway_audit_callback.py`；其他部署共用的 `litellm-config` 没动 |
| BladeAI 身份 | `deploy/stage2/bladeai-server-rbac.yaml` 已 apply |
| Fleet | `deploy/stage2/fleet.yaml`，替换了镜像，storageClass 改为 `nfs-client` |
| Fleet 配置 | replicas 5；nodes tcse-v100-01/02；`agent_runtime_apparmor_profile=""`；requests 200m / 1Gi；storage_class `nfs-client`；`litellm_config_map=litellm-config-fleet` |

### 5.2 部署中遇到并已处理的环境问题

1. 新环境被清空，且已被他人用来装 astronomy-shop / observe → 用户改定用老集群。
2. 只有 tcse-v100-03 装了 AppArmor 档案，而它 CPU 请求已占 90% → 注解改为可配置并置空。
3. 集群没有 `openebs-hostpath` → 改用 `nfs-client`。
4. chaosblade-tool 在 `default` 命名空间 → exec Role 放到 `default`。
5. 节点运行时是 Docker（cri-dockerd），只写 `@sha256` 的引用会被当成 `:latest`，Harbor 报 not found → 镜像引用一律写 `tag@sha256`。
6. 5 个 slot 在 01/02 上并发首次拉镜像，s04 的 litellm 拉取遇到一次 `context canceled`，kubelet 自动重试后成功。

### 5.3 验证（截至 2026-09-15 07:30 UTC）

- **副本与控制器**：5 个副本 `otel-demo-01…05` 各 6 个 Pod 全部 Running；5 个 slot 控制器 Pod 4/4 就绪。
- **`bladeai-server`**（在 s01 实测）：
  - 日志 `Blade AI Server ready - 3 skills loaded`，前置工具齐全。
  - 用生成的 kubeconfig 能列出副本 Pod，`can-i create chaosblades` 为 yes，能看到 3 个 chaosblade-tool。
  - `/api/v1/health`、`/api/v1/sessions` 返回 200。
- **回环端口隔离**：8399 不在 agent-runtime 的回环白名单里（18081–18088、18181–18188、18090、18481）。
- **gpt-5.5（nexustokenai）**：
  - 从 s01/s02 经本 slot 网关调用返回 200，约 12 s，回答开头无 U+200B。
  - 直连 TLS 握手成功 11/12 和 12/12，偶发 7–8 s。
  - 对比：在 tcse-v100-03 上经旧控制器的网关探测，两次都 120 s 超时，还有一次握手超时——不同节点的出网质量不一样。
- **Fleet preflight**：5 个 slot 应用都可运行、环境门禁 ready；`available_models` 为空，因为资格认定还没发布。

### 5.4 资格认定与并行批次

1. **首次基础通道认定**（07:28 UTC 起，5 个 slot 并行，gpt-5.5）：
   - 5 个 BladeAI 会话都跑完了（`harness_report_status=completed`，判定 INCONCLUSIVE），网关证据和工具证据都已验证。
   - MCP 通道类检查全部为 false（mcp_read、confirmation 往返、consult 往返、notice ack、result submission），记录被判 failed，`available_models` 仍为空。
   - 原因见第三节 `capability_qualification.py` 那一行。
2. **修复后重新发布**：67fed38 修复后打出镜像 `stage2-d0-67fed38-bladeai070@sha256:a6380bf4…`，按以下顺序进行：
   1. 5 个 slot 滚动到新镜像；
   2. 用已有的认定记录、带上开关重新发布，不重跑认定；
   3. preflight 确认每个 slot 上 bladeai/gpt-5.5 都可运行；
   4. 提交批次 `bladeai-parallel-20260915-01`：5 条 L0×C0，bladeai，gpt-5.5，每个 slot 一条，同一波并行。

3. **网关探测把 gpt-5.5 挡住**（07:43–07:54）：5 个 slot 同时探测 7 个别名，nexustokenai 返回限流，gpt-5.5 被判为不可运行。于是在 1519f8c 里把探测默认关掉（见第三节），滚动 slot 后 5 个 slot 上 bladeai/gpt-5.5 都可运行。
4. **第一轮批次 `bladeai-parallel-20260915-01`**（08:04:36 提交，5 条同时进入 Running；**并行执行本身跑通**）：
   - **09-13 的批准后空转没有再出现**：s02 日志出现 `Intent confirmed by user: pod-cpu-load` 和 `Bootstrapped task session task=inject-…`。
   - **注入流程随即崩溃**：`PermissionError: [Errno 1] Operation not permitted: '/opt/bladeai-070/blade-ai/_internal/vendor/chaosblade/blade'`。
     - 原因：BladeAI 的 `get_bundled_blade_path()` 每次查找都会 `chmod(mode | 0o111)` 自带的 blade，blade 还要在自己目录里写 `chaosblade.dat` 和 `logs/`；而我追加镜像层时把整个包的属主改成了 root（原包属主是 10001），运行用户 10001 不能 chmod。
     - 这是我打包造成的平台问题。
   - **5 条结果**：r1 Done / OUTPUT_UNSTRUCTURED，r2 Failed / OUTPUT_UNSTRUCTURED，r3 Failed / PERMISSION_DENIED_OBSERVED，r5 Done / PERMISSION_DENIED_OBSERVED（得分 2.5，VALID），r4 被人工停止。**这一轮的判定和分数都是平台缺陷造成的，不能计入结果**；Fleet 把其中几条归为 agent 失败，这个归因也不成立。
   - 集群里没有残留的 ChaosBlade CR。
5. **重跑**：BladeAI 层按属主 10001 重新打包，推送为 `stage2-d0-1519f8c-bladeai070-own`；停掉第一轮，slot 滚动后先核实 blade 属主为 10001，再以批次 `bladeai-parallel-20260915-02` 重新提交。

6. **第二轮批次 `bladeai-parallel-20260915-02`**（08:50:41 提交，5 条同时进入 Running，09:07:30 全部结束）：
   - **平台链路全部正常**：
     - slot 已滚动到 `stage2-d0-1519f8c-bladeai070-own@sha256:fcf5f6e2…`，blade 属主核实为 10001，EPERM 不再出现。
     - s04 走到了 `Intent confirmed by user: pod-cpu-fullload`，注入任务已创建，进入注入流程。
   - **一条都没有真正注入**：ChaosBlade operator 在这段时间没有实验记录，集群里也没有 CR。原因在 gpt-5.5 上游（nexustokenai）：
     - s01 在意图澄清阶段连续收到 429 `Upstream rate limit exceeded`，BladeAI 自身的 2 次快速重试（间隔 0.1–1 s）用尽后结束。
     - s04 在注入流程中多次收到 500，最后是 Cloudflare 502 Bad Gateway，注入流程失败。
     - 5 个会话同时打到上游，每次调用都带约 1.6–1.9 万 token 的上下文；网关设置是 `num_retries: 0`，429/5xx 原样回给 BladeAI。
   - **结果**：r1/r2/r4/r5 判 VALID FAIL（MAIN_FAULT_ACTIVE、GATE_MAIN_FAULT_RUNNING，0–2.5 分），r3 判 CASE_INVALID（HARNESS_EXECUTION_FAILED）。Fleet 记录的失败码是 OUTPUT_UNSTRUCTURED 或 PERMISSION_DENIED_OBSERVED。**平台把上游限流判成了 agent 侧的失败，这一轮同样不能算作 BladeAI 的成绩**；这个归因缺陷本轮不修。
   - **PERMISSION_DENIED_OBSERVED 的来源之一**：BladeAI 预检时执行 `kubectl get apiservice v1beta1.metrics.k8s.io`，`resbench-bladeai-server` 没有 apiservices 读权限，被拒绝。已在集群上补上，并同步到 `deploy/stage2/bladeai-server-rbac.yaml`。
   - **待用户决定**：上游限流下怎样保证 5 路并行——给网关加退避重试、降低并发，还是提高账户限额。
7. **用户选定网关退避重试**（bcd3ae9）；同时补齐 BladeAI 服务账号的只读权限（664117b 补 apiservices，e27ca92 补 resourcequotas 等）。
8. **第三轮批次 `bladeai-parallel-20260915-03`**（09:30:15 提交，5 条同时进入 Running，10:01:05 全部结束）：
   - **网关重试起了作用**：各 slot 重试 14–56 次，本轮 429 为 0。
   - **出现新的上游瓶颈**：nexustokenai 返回 `Concurrency limit exceeded for user, please retry later`，各 slot 14–56 次，其中 6–24 次发生在流式响应中途。
     - 已开始的流，网关无法重试。
     - 5 个 slot 各有独立网关，在网关层限制不了全局并发。
   - **BladeAI 走得最远的一次**：
     - s03、s04 经 HTTP interrupt 批准（`delivered=True`），创建了注入任务；
     - 查看 `blade create k8s pod-cpu fullload` 用法，确认 `can-i create chaosblades`，在 chaosblade-tool 里执行 `blade status --type create`；
     - 随后调用模型重试用尽（09:45:45、09:47:21），会话停住，10:00 被平台超时取消。
   - **没有注入，也没有残留**（已核实）：
     - 集群里没有 ChaosBlade CR；
     - 3 个 chaosblade-tool 里 90 分钟内没有新建实验；
     - otel-demo-03/04 的 cart CPU 为 16–19m，属于正常。
   - **结果**：r1、r4 判 CASE_INVALID（HARNESS_TIMEOUT）；r2、r3、r5 判 VALID FAIL（2.5 分）。
     - r2 的 PERMISSION_DENIED_OBSERVED 是真实的 RBAC 缺口（列不出 resourcequotas，已补）。
     - 其余失败码仍然没有反映真实原因，即上游的并发上限。
     - **本轮同样不能算作 BladeAI 的成绩。**
   - **待用户决定**：降低同时运行的试验数，或者在 nexustokenai 提高账户并发上限。
9. **用户选定 2 路并发：第四轮 `bladeai-parallel-20260915-04`**（16:40 提交，`max_concurrency: 2`，约 16:55 人工停止）：
   - **2 路并发下上游不再报错**：s03、s05 在 15 分钟内并发超限 0 次，模型调用成功 20–22 次。
   - **暴露出确认桥的两个真实 bug**：
     1. **执行关卡走错了接口**：
        - BladeAI 0.7.0 的 `/turn` 会话里，执行关卡和意图关卡处理方式相同：确认事件用轮次 id（`turn-…`）作 `task_id`，再在 `wait_for_confirmation` 里按这个 id 等待 `/sessions/{sid}/interrupt` 的回答。
        - 平台却对执行关卡调用了 `POST /api/v1/confirm/{turn id}`。这个接口按路径 id 恢复 LangGraph 线程，恢复出来的检查点里只有 `skill_name`。
        - 结果 BladeAI 报 `state.fault_spec missing`，以"没有指定故障类型"为由拒绝了自己的规划并终止；真正在等的回答始终没人给（r4：16:46:33 发出请求，阻塞 58.6 s）。
        - 09-13 那次 L0"批准后不再推进"也是这个原因。**问题在平台驱动，并非只能等上游修。**
     2. **意图关卡的计划翻译不认 0.7.0 的写法**：
        - `fault_intent` 用的是技能写法 `fault_type: "pod-cpu-load"`，没有 scope/target；Pod 名在 `names`，uid 在 `params.pod_uid`。
        - `plan_from_intent` 只认 ChaosBlade 三元组，送到校验器的计划缺 target/fault_type/intensity，被判"计划未通过类型化校验"（r3）。
   - 修复在 ff4a999，第五轮结果见下一条。
10. **第五轮 `bladeai-parallel-20260915-05`**（17:04:54 提交，2 路并发，约 17:22 全部结束）：
   - **执行关卡修复生效**：
     - s05 在 17:08:38、s04 在 17:16:19，执行关卡都经 `/interrupt` 回答（`delivered=True`）。
     - 不再有 `state.fault_spec missing` 和自行终止。
   - **新的阻塞是上游接口不兼容**：
     - 批准后约 27 s，BladeAI 发出的下一次模型请求被 nexustokenai 以 HTTP 400 拒绝：`function_call_output requires item_reference ids matching each call_id on HTTP requests; continuation via previous_response_id is only supported on Responses WebSocket v2`。
     - 重试 3 次后 `Turn failed`，任务 failed。
     - **根因已复现**：BladeAI 恢复执行后发出的请求里带有"孤立的工具结果"——`tool` 消息找不到发起它的 assistant `tool_calls`。
       - 在 slot 网关上，只发一条孤立工具结果：返回与 BladeAI 逐字相同的 400。
       - 正常的两步工具续写（带或不带推理参数）：200。
       - 工具调用存在但 id 对不上：200。
     - **定性**：这是 BladeAI 0.7.0 在中断恢复路径上拼接消息的缺陷；nexustokenai 只是把它暴露了出来，换严格校验的上游同样会被拒，与限流无关。
     - **token 开销**：网关实测 BladeAI 每次请求约 1.9 万输入 token，约 $0.106/次。
11. **用户选定：BladeAI 换模型**，平台和网关都不改。
   - 先在 slot 网关上逐个实测候选模型：普通请求能否访问，以及是否接受孤立工具结果。

     | 模型 | 普通请求 | 孤立工具结果 |
     |---|---|---|
     | deepseek-v4-pro-0813 | 200 | **400**（`Messages with role 'tool' must be a response to a preceding message with 'tool_calls'`） |
     | deepseek-v4-flash-0731 | 200 | **400**（同上） |
     | qwen3.8-max | 200 | 200 |
     | qwen3.8-flash | 200 | 200 |
     | claude-opus-5 | 200 | 200 |

   - 选定 **qwen3.8-max**，理由三点：
     - 它接受孤立工具结果；
     - 09-11 直接驱动 BladeAI 0.7.0 时用的就是它，那一轮确实完成了注入；
     - 比 claude-opus-5 便宜，协议转换环节也更少。
   - 模型由平台在每次试验时经 config API 推给 server，只改批次里的 `model` 字段即可，不用重新部署。
12. **第六轮 `bladeai-parallel-20260915-06`**（qwen3.8-max，2 路并发，17:38:58 提交，18:35:16 全部结束）：
   - **BladeAI 第一次真正完成注入**（前五轮都是 0 次）。chaosblade-operator 日志里有 4 个 `cri cpu fullload` 实验，目标都是 cart 容器、80% CPU、`--timeout=600`：

     | 实验 | 命名空间 / Pod | 创建 → 删除 | 对应条目 |
     |---|---|---|---|
     | `437dada9fc368f28` | otel-demo-01 / cart-7ffd4d6f-gcds8 | 17:50:14 → 18:00:19 | r1 |
     | `d6de78237ff8a7ef` | otel-demo-05 / cart-7ffd4d6f-lhw8j | 17:50:47 → 18:00:52 | r3 |
     | `39196ee993783c83` | otel-demo-01 / cart-7ffd4d6f-gcds8 | 18:22:21 → 18:32:27 | r2 |
     | `6d8b7ad676e6d6a4` | otel-demo-05 / cart-7ffd4d6f-lhw8j | 18:26:45 → 18:36:50 | r5 的早期尝试 |

   - **结果**：r1、r3 判 CASE_INVALID（`HARNESS_TIMEOUT`，各跑满约 1824 s）；r2、r4、r5 判 VALID FAIL（2.5 分，`raw_score` 12.5 / `verified_nodes` 1）。**5 条都不能算作 BladeAI 的成绩。**
   - **注入没有被计分**：三条 VALID 条目的失败规则都是 `MAIN_FAULT_ACTIVE`（期望 true、实测 false），`effect_observation` 写的是"actual fault window is not established"、`window.injection_id` 为 null。
   - **两个互相独立的根因**：
     1. 计划体始终缺 `effect_condition` / `recovery_condition`，被模拟用户反复拒绝。r3 的重试原因逐字为 `ConversationError: confirmation completion failed validation: effect_condition: MISSING_PLAN_FIELD; recovery_condition: MISSING_PLAN_FIELD`；r1 的 17 次交互全部停在 `C1_PLAN`，关卡名一路变成"提供合法的 metric 取值""提供合法的 operator 取值"，30 分钟预算就这样耗光。**已修，见第七节。**
     2. 平台只承认自己账本里的实验，BladeAI 原生建的 CR 永远算 foreign。**未修，见第六节 12。**
   - **残留**：只有 r5 的 `CHAOS_INVENTORY_CLEAR` 没过、并判了 `POST_TRIAL_ENVIRONMENT_NOT_READY`，原因是上一次尝试留下的 `6d8b7ad676e6d6a4` 还在（18:36:50 由 operator 清掉）。批次结束后复查：集群无 ChaosBlade CR，5 个副本各 6 个 Pod 全部 Ready，chaosblade-tool 上只剩 08-28、09-04 两条历史记录。

## 六、已知限制（本轮刻意不做）

1. **镜像不是用 `build_stage2_image.py` 构建的**：为赶时间，控制器镜像是在 Harbor 上 fleet 的 `stage2-d0-77a11bd@sha256:f3b1ffc1…` 之上用 `crane append` 追加两层——本分支代码层（`/app`，按 runtime-overlay 的同一批路径）和 BladeAI 0.7.0 PyInstaller 包（`/opt/bladeai-070`，取自老集群 bbverify Pod，sha256 分片校验一致）。下层残留的已删模块文件不会被导入。`Dockerfile.agent` 的新写法没有实际构建过。
2. 单系统部署（`deploy/stage2/stage2.yaml`）没有加 `bladeai-server`，BladeAI 只在副本 slot 上可跑。
3. nexustokenai 的 chat completions 回答带 U+200B 前缀（09-05 实测），未在网关层处理。
4. 老集群上 slot 关闭了 agent-runtime 的 AppArmor 注解；BladeAI 黑盒不经过 agent-runtime，其他三家在这些 slot 上少一层防护。
5. 放开技能脚本后，BladeAI 目标守卫不再把技能脚本判为 banned，这是 BladeAI 的默认行为。
6. BladeAI 0.7.0 的 `GET /api/v1/config` 会明文返回 `llm_api_key`（实测，值此处不记）。server 只绑 pod 回环 8399；同 pod 的 agent-runtime 按回环端口白名单放行出站，8399 不在白名单内（见 5.3），同 pod 的其他智能体访问不到这个接口。
7. **黑盒认定分支不检查 `failure_reasons`**：记录里即使有 `native_boundary_violation_attempt` 或 `runner_error` 也会被接受。本轮 s02/s03/s05 的记录已人工核对，失败原因只有 6 个 MCP 通道类；这个开关不适合在没人核对记录时使用。后续应改为只容忍 MCP 通道类原因。
8. **黑盒认定发布的 capability 字段与跳过的检查不一致**：仍写 `feedback_channels=in_band_mcp`、`supports_mid_turn_feedback=True`，而证明这两项的通道检查恰好被跳过了。对 L0×C0 没有影响；做 D 类扰动（带内通知）之前需要改正。
9. **slot 刚启动时要等网关探测**：已由 1519f8c 默认关闭探测解决（见第三节）。关闭前，5 个 slot 同时探测把 nexustokenai 打到限流，gpt-5.5 被判为不可运行。
10. **上游模型错误被判成 agent 侧失败**：三轮中上游的 429、5xx、并发超限，要么被 Fleet 记为 agent 失败，要么被评分判为 FAIL/CASE_INVALID（失败码为 OUTPUT_UNSTRUCTURED、PERMISSION_DENIED_OBSERVED、HARNESS_TIMEOUT）。正式评测前，评分需要能识别上游模型错误，并归为平台原因。
11. **账户级并发上限在网关层管不住**：每个 slot 有独立的 LiteLLM，只能通过 Fleet 的 `max_concurrency` 控制同时运行的试验数，或者提高账户额度。
12. **黑盒 BladeAI 的原生注入永远拿不到 `MAIN_FAULT_ACTIVE`**（第六轮查实，未修，需用户定夺）：
    - `finalization.py:83` 的 `ever_active` 取自 `runtime_factory.py:1317` 的 `ledger.get("ever_active")`，而账本行只有平台自己的 chaos_core 服务会写——实验转 Running 时置 `ever_active=True`（`mcp_servers/chaos_core/service.py:806`）。
    - `fault_inventory.py:45` 的 `owned_by_trial` 另外要求 `owner == "chaos_control"`、`run_id`、`target_uid`、`ledger_matched` 四项齐全，而这四项全部来自平台注入时打的标签（`mcp_servers/chaos_core/backends/chaosblade.py:249-255`）。
    - BladeAI 黑盒用自己的 ServiceAccount 直接建 CR，四项一个都没有，只会落进 `foreign_present` / `foreign_active`（`fault_inventory.py:131-133`）。foreign 只影响 `CHAOS_INVENTORY_CLEAR`，不会被算作主故障。
    - 结论：**只要 BladeAI 走原生注入，L0×C0 最好也只是 VALID FAIL 2.5 分，重跑多少轮都一样**。第一到第六轮的判分由此得到统一解释。
    - 两条出路：(a) 让平台按"命名空间 + 目标 Pod uid + 故障类型 + 时间窗"承认观察到的 foreign 实验——快照里已有 `foreign_active` 和每个资源的 `namespace`/`target_name`/`fault_type`/`phase`，但 `run_id`、`target_uid` 对 foreign 资源是空串，匹配逻辑要新写，且改的是判分语义；(b) 不走 L0–L4 判分，改用 WP8 执行通道认定口径（`BLADEAI_BLACKBOX_ACCEPTANCE`）评价 BladeAI。
13. ~~第七节的桥接修复还没有构建镜像、没有部署~~ **已解决**：第六轮跑的是 `stage2-d0-ff4a999-bladeai070-own`；第七节和第八节的改动已一起打进 `stage2-d0-078d039-bladeai070-own@sha256:71534a76…`（见第八节"部署"）。

## 七、第六轮后的修复（本提交）

**`stage2_service/harness_adapters/bladeai_confirm.py`**

- **改前**：`plan_from_intent` 只产出 `fault_type`、`intensity`、`additional_native_constraints` / `native_params`、`target`、`duration_seconds` 六个键；`NON_NATIVE_INTENT_PARAMS`（:305-309）把 `effect_metric` / `effect_operator` / `effect_threshold` 和 `recovery_*` 从原生 flag 里剔掉之后，就直接丢弃了。
- **改后**：
  - 新增 `_condition_from_params()`（:312-351）：把 BladeAI 的三个扁平键拼成平台要的 `{"metric", "operator", "threshold"}`；阈值 0.7.0 写成字符串（`"0.5"`），这里转成 JSON 数字。三项缺一就返回 `None`。
  - `plan_from_intent` 在写 `target` 之前补出这两个条件（:422-429）。
- **原因**：`AgentPlan` 必填 `effect_condition` 和 `recovery_condition`（`plan_schema.py:235-236`），而 L0 的 `_may_supply` 是空集（`simulated_user.py:1442-1444`），平台不允许替智能体补这两个字段，`_OMITTABLE_CONDITIONS` 也只在 L1/L2 才生效。于是第六轮每条试验的计划体都缺这两项，被模拟用户逐次拒绝（r3 的重试原因逐字记在 5.4 第 12 条）。BladeAI 自己其实带了合法取值——`target_cpu_cores` / `increase_by_at_least` / `within_baseline_delta` 都在平台词表里（`condition_policy.py:65-87`），只是桥接没有翻译。
- **09-17 更正**：上面"L0 的 `_may_supply` 是空集、平台不允许替智能体补"说错了。`_may_supply` 先看决策策略：策略为 `agent_delegated` 时返回全部决策节点（`simulated_user.py:1511-1512`），只有非 `agent_delegated` 的 L0 才是空集（`:1513-1514`）；而平台的 L0 任务定义就是 `agent_delegated`（`task_service.py` `_autonomy_case`，L0 条目的 `decision_policy`）。所以第六轮平台其实允许代补，被拒的直接原因以 5.4 第 12 条逐字记录为准，不能归结为"不允许代补"。本节修复本身仍然成立：BladeAI 自己给出了合法条件，桥接译出来就不必依赖代补。
- **为什么缺一项就整条不发**：`_has_blocking_issues`（`simulated_user.py:865-869`）只把 `MISSING_PLAN_FIELD` 当可恢复，其余问题一律致命；发半条残缺条件比不发更糟。
- **测试**：`tests/test_bladeai_confirm_bridge.py` 新增两条——`test_plan_from_intent_emits_the_two_conditions_the_platform_never_supplies`（:272）用第四轮真实载荷断言两个条件都译出且不漏进原生参数，`test_plan_from_intent_omits_a_condition_it_cannot_complete`（:301）断言缺 `recovery_operator` 时整条不发。全文件 22 条通过（`run_snapshot_pytest.py`，d0-integration venv，Python 3.13.12）。
- **部署**：未构建镜像、未上线（见第六节 13）。修好之后 BladeAI 的计划能通过校验、试验能正常走完，但因第六节 12 的账本归属问题，`MAIN_FAULT_ACTIVE` 仍然不会为真。

## 八、承认智能体自建的故障（用户 09-15 拍板："扩归属 + 重跑"）

**目的**：让平台把「智能体用自己的客户端建的、且作用在本试验目标上的实验」算作主故障。这是第六节 12 的唯一出路，否则黑盒 BladeAI 永远停在 VALID FAIL 2.5 分。

**开关**：`STAGE2_FOREIGN_FAULT_ATTRIBUTION`，**默认关**。关着时行为和以前逐字一致——账本仍是唯一证据，另外三家（走 `chaos_control` 注入）完全不受影响。

**改动**

1. **新文件 `stage2_service/foreign_fault_observer.py`（116 行）**：`ForeignFaultObserver`，在计划批准时武装、每 5 秒调一次 `inventory_trial`，试验结束时 `finish()`。
   - **为什么必须有它**：`inventory_trial` 的调用点只有 finalization（开始、等待循环、结束）和 capability_loss，全都在 harness 返回之后。而 BladeAI 的实验带 `--timeout`，operator 会提前回收——第六轮 r1 的 CR 存活 17:50:14–18:00:19，试验却跑到 18:09:39，等到收尾再看，什么都没有了。所以必须有人在故障还活着的时候去看一眼。
   - 它只读，不创建、不删除、不自己做归属判断。
2. **`stage2_service/runtime_factory.py`**（归属判定本体，在 `DirectChaosCleanup` 里，因为账本归它管）
   - `:1239` 新增 `FOREIGN_FAULT_ATTRIBUTION_ENV`，`:1241-1244` 新增 `foreign_fault_attribution_enabled()`（照搬 `gateway_model_probe_enabled` 的写法）。
   - `:1261` 新增 `self._foreign_observations`：trial_id → 首次/末次看到的时间，**粘性**保存。
   - `:1382` 新增 `_observe_foreign_fault()`：匹配键是**命名空间 + 目标 Pod 名 + 故障类型 + 该资源处于活动态**。
     - **与用户原话的一处偏离**：用户说的是"目标 Pod uid"，但 foreign CR 上**没有 uid**——`target_uid` 和 `run_id` 都是只有 `chaos_control` 才写的标签（`backends/chaosblade.py:249-255`）。CR 上能读到的是 matchers 里的命名空间和 Pod 名、以及 target/action 推出的故障类型。因此改用 Pod 名匹配，再由控制器用自己已知的 uid 回填。
   - `:1338` 调用；紧随 `snapshot["trial"]` 之后写回 `ever_active=True`、`fault_attribution="observed_foreign"`、`experiment_name`、`started_at`/`ended_at`、`resource_absent`。
     - `target_name` / `target_uid` **仍取运行时的值**，因为 finalization 要拿它们和批准计划里的 target 逐字段比对（`finalization.py:87-99`）。
     - `started_at` / `ended_at` 正是 finalization 用来定效果窗口的两个字段（`finalization.py:166-167`），第六轮那句 "actual fault window is not established" 就是它们为空导致的。
   - `:1995` 在 `condition_monitor_factory` 旁边加 `foreign_fault_observer_factory`，只在开关打开时才构造。
   - **一处连带好处**：`node_evaluation.py:331` 的关卡项 `main_fault_running` 本来就取自 `recovery.main_fault_ever_active`，所以这一处修好，`MAIN_FAULT_ACTIVE` 和 `GATE_MAIN_FAULT_RUNNING` 一起通过。
3. **`stage2_service/campaign.py`**：`:177` 新增构造参数、`:194` 赋值、`:316` 每条试验建一个、`:511-527` 在 `user_decision_received`+`approved_plan` 分支武装（批准是智能体最早可以注入、也是故障尚不存在的最后时刻）、`:808-816` 与 `condition_monitor.finish()` 并列收尾并写进 `final_output["foreign_fault"]`、`:1333-1334` 异常清理路径上一并停掉，避免轮询线程泄漏。
4. **`fleet_service/contracts.py:107`** 新增 `foreign_fault_attribution: bool = False`；**`fleet_service/manifests.py:292-293`** 把它渲染成 slot 控制器的 `STAGE2_FOREIGN_FAULT_ATTRIBUTION` 环境变量。

**测试**（`tests/test_stage2_fault_inventory.py` 新增 4 条，全部通过）

- `:191` 开关打开时，作用在本试验 cart 上的自建实验被判为主故障；CR 消失后**仍然**是 `ever_active=True`、`resource_absent=True`、`ended_at` 有值（粘性）。
- `:225` 不开开关时，同样的输入仍是 `ever_active=False`、`fault_attribution="ledger"`——默认行为不变。
- `:237` 打在**别的 Pod** 上的自建实验不算本试验的主故障（5 个副本共用一个集群，必须排除邻居）。
- `:251` 观察器本身：轮询、只在第一次看到时发一次事件、`finish()` 汇总。
- 回归：`test_fleet_service.py`、`test_stage2_campaign.py`、`test_stage2_fault_inventory.py` 共 75 条通过；`test_stage2_finalization.py`、`test_bladeai_confirm_bridge.py` 一并跑过 66 条。

**部署**

- 控制器镜像 `1.94.151.57:85/observe/resbench-stage2:stage2-d0-078d039-bladeai070-own@sha256:71534a765febaa8ae618cc25b3d3d6201d8c845ea8d9e838babac6e327672e35`，仍按 5.1 的办法在 `stage2-d0-77a11bd@sha256:f3b1ffc1…` 上 `crane append` 两层（代码层 253 个文件、BladeAI 0.7.0 包 665 个成员）。
  - 核对过：新镜像的 Entrypoint、Cmd、WorkingDir、User 与在跑的镜像逐字一致，层数 59（基线 57 + 2），所以当初那步 `crane mutate` 没有改动任何配置，不必重放。
- Fleet 配置加 `foreign_fault_attribution: true`，由 `manifests.py` 渲染成每个 slot 的 `STAGE2_FOREIGN_FAULT_ATTRIBUTION=on`；滚动脚本在提交批次前会逐个 slot 核对这个环境变量确实是 `on`（`rollout_round7.sh` 第 1b 步），不是只看部署成功。
- **第一次下发失败，值得记一笔**：`rollout_round7.sh` 在第 1 步等了 20 分钟、5 个 slot 原地不动（exit 2），但 `provision` 返回的是 0。
  - 真正的错在前一步：`fleet config --from-file` 被**旧的 Fleet** 以 `HTTP 422 {"type":"extra_forbidden","loc":["body","foreign_fault_attribution"]}` 拒绝——它的代码里没有这个字段。`deploy_fleet_old.sh` 不会因为这个中止，紧接着的 `provision --execute` 就拿**旧配置**（还是 `ff4a999` 镜像）重新下发了一遍并返回 0，脚本因此以为成功。
  - **口径**：校验 `FleetConfig`、渲染 slot 清单的是 **Fleet Pod 自己**，不是本机脚本（本机那步 `FleetConfig.model_validate` 用的是工作树代码，当然通过）。所以但凡给 `FleetConfig` 加字段，必须**先把 Fleet 服务本身滚到新镜像**，再发新配置。
  - 连带一条：换掉 Fleet Pod 会打断 `svc/resbench-fleet` 的 28090 端口转发，而后续每个 `fleet_ctl` 调用都依赖它，必须重启并等它应答。
  - 处置：新增 `rollout_round7b.sh`——`PHASE=apply` 先把 Fleet 滚到新镜像 → 核对 Fleet 确实在新 digest 上（不在就停，不碰 slot）→ 重启并自持端口转发 → 再跑原来的链路。
- **第二次下发：滚动成功，但发布能力被拒**（exit 3）。5 个 slot 都已在新镜像上、`STAGE2_FOREIGN_FAULT_ATTRIBUTION` 都是 `on`（脚本第 1b 步逐个核过），但 `publish_harness_capabilities.py` 在 5 个 slot 上一致返回 `{"status": "rejected", "reason": "qualification uses a different gateway configuration or route"}`。
  - 原因查实：`capability_qualification._verified_gateway_identity`（:226-245）要求资格记录里的 `gateway_config_sha256` 等于**当前**网关配置的哈希。各 slot 上最新的记录是 `base-bladeai-20260915072820`（07:28 UTC），记的是 `97b77318…`，而现在的 `/etc/litellm/config.yaml` 是 `902aa6a7…`——差别正是我自己在第五轮加的 `router_settings` 重试块（bcd3ae9）。记录里的 `gateway_route` 已经指向 nexustokenai，所以对不上的是哈希。
  - **这个检查是对的**，它就是为了防止拿旧网关下取得的资格去发布能力，所以只能重跑资格认定，不能改记录。
  - 处置：`round7_requalify_and_run.sh`——在 5 个 slot 上重跑 base 资格认定，**改用 qwen3.8-max**（第七轮真正要跑的模型，路由检查比的就是它的 dashscope 路由；也避免 5 路并发打 nexustokenai，第二轮就是在那里被限流的），然后发布 → preflight → 提交 → 跟踪。
- **第三次下发：重跑资格认定，4/5 通过，卡在 s02**（exit 3）。
  - 重跑后的记录 `gateway_config_sha256` 都是 `902aa6a7…`，与当前网关一致，上一次的哈希问题确实解决了。s01、s03、s04、s05 四个 `harness_report_status` 是 `completed`、`cleanup_errors` 为空、`gateway_evidence_verified` 为真，发布成功，口径写的是 `BLADEAI_BLACKBOX_HTTP_CHANNEL`。
  - s02 的记录是 `timeout`，发布被拒：`{"status": "rejected", "reason": "black-box BladeAI qualification needs a completed session with gateway evidence"}`（`capability_qualification.py:290` 硬性要求 `completed`）。s02 上那条状态合格的老记录又是旧网关的 `97b77318`，所以它当时没有任何一条可用记录。
  - **s02 为什么 timeout（09-17 更正）**：我当时依据日志里反复出现的 `Intent partially converged (unset), continuing dialogue`，判定"它在意图澄清里打转、属 BladeAI 自身的概率性行为"——**这个判断是错的**。源码 `intent_clarification.py:1163` 说明这条日志的意思是"调用方开启了新一轮对话"，每条用户消息都会打一次，是正常流程日志。09-17 复盘平台记录并绕过平台亲自实测后查实：超时主因在平台——把 BladeAI 的文字确认请求当成计划审批、空计划校验失败就否决（答非所问），回复排队投递、正确答复在超时时被取消；叠加 BladeAI 引擎写死的最短 600 秒与任务"最长 300 秒"的冲突。详见第十节。s02 重跑一次即 completed 的事实不变。
  - 处置：`round7_fix_s02_and_run.sh` 只对 s02 重跑（最多 2 次，连续失败就停下交给人判断，不无限重试），另外四个不动，然后发布 → preflight → 提交 → 跟踪。
- **第四次下发：资格认定与发布全部通过，卡在 preflight 的时序上**（exit 4）。
  - s02 第 1 次补跑就是 `completed`（记录 `base-bladeai-20260916041354`，哈希 `902aa6a7…`、`cleanup_errors` 为空），印证了它上一次的 timeout 是概率性的，重跑一次即可，不必调超时。
  - 5 个 slot 的能力全部发布成功、`qualified=True`、口径 `BLADEAI_BLACKBOX_HTTP_CHANNEL`。**资格认定这一关到此彻底通过。**
  - 退出在 preflight：脚本按"5 个 slot 都有 `model_matrix.bladeai[qwen3.8-max]` 为真"来计数，结果算出 0。
  - **已查实：是我的判定脚本读错了地方，5 个 slot 本来就可以跑。**
    - Fleet 的 preflight 文档里，每个 slot 只有 `applications`、`available_models`、`capability_loss`、`environment`、`gateway_probe`、`namespace`、`reachable`、`slot_id` 八个键，**没有 `model_matrix`**；而脚本要找的正是 `model_matrix.bladeai[qwen3.8-max]`，所以永远算出 0。
    - `model_matrix` 是**控制器**侧的字段（`runtime_factory.py:1581` 生成，经控制器的 `/api/v1/stage2/options` 暴露）。
    - 直接查 s01 控制器：`model_matrix.bladeai["qwen3.8-max"] = true`，`model_probes["qwen3.8-max"]` 为 `runnable: true`、`visible_in_gateway_models: true`、`probe_status: "not_probed"`（探测按用户要求默认关闭），路由指向 dashscope。
    - 这段判定是从第一版滚动脚本 `rollout_publish_submit.sh` 抄下来的（它判 gpt-5.5 时同样错），前几轮没暴露是因为那几轮用的是另一条直接查控制器的链路。
  - **另外记一个我判断错过的弯路**：退出当时 5 个 slot 的 `gateway_probe` 还是 `"running"`，我一度据此判成"slot 刚重启、模型列表没刷新"的时序问题（第六轮确有过这种情况）。几分钟后复查 `gateway_probe` 全部变成 `complete`、而计数仍是 0，才排除了这个猜测。凭旧经验套用是这次绕路的原因。
  - 处置：`round7_submit_and_watch.sh` 改成逐个 slot 端口转发、直接读控制器的 `model_matrix`，确认 5/5 后再提交批次并跟踪；资格认定和发布都不再重复。
  - （顺带：preflight 里 `codex: platform_sandbox_missing` 导致 codex 那栏 `supported=false`，与 bladeai 这条路无关。）
- **第七轮 `bladeai-parallel-20260915-07`（BladeAI + qwen3.8-max，5 条 L0×C0，2 路并发，04:18:50 提交，05:12:29 结束）：归属改动生效，BladeAI 第一次拿到 PASS。**
  - **r1 判 PASS，34.5 分**（`raw_score` 59.5，`verdict`/`experiment_verdict` 均 PASS，`trial_validity` VALID，原因码为空）。七项检查全部通过：`CONTROLLER_CLEANUP_VERIFIED`、`FAULT_ABSENT`、`MAIN_FAULT_ACTIVE`、`MAIN_FAULT_TARGET_VERIFIED`、`CHAOS_INVENTORY_CLEAR`、`BUSINESS_RECOVERY_VERIFIED`、`NEXT_TRIAL_READY`；`ground_truth` 里 `main_fault_ever_active`、`main_fault_target_verified`、`fault_effect_verified`、`business_recovery_verified` 全为 true；实验关卡 `status: PASS`、`main_fault_running: true`。
  - **归属确实来自智能体自建的 CR**：r1 的效果窗口 `injection_id = d3dd227dbee9f41d`、`time_source = controller_first_observed_state`、窗口 04:35:21→04:45:26；operator 日志里这个实验 04:35:15 创建、04:45:20 删除，目标是 `otel-demo-01/cart-7ffd4d6f-gcds8`。也就是**创建后约 6 秒被观察器看到、删除后约 6 秒被记为结束**。第六轮那句 "actual fault window is not established" 和 `injection_id: null` 不再出现。
    - **证据口径要说清**：判分文档里并没有直接透传 `fault_attribution: observed_foreign` 这个标记（`snapshot["trial"]` 的字段不会整体进判分文档），我在 r1 的判分文档里能看到的是 `effect_observation.window.injection_id`。所以"归属生效"的结论是由三条合起来支撑的：`injection_id` 指向一个平台账本里没有的实验、其时间戳与 operator 日志逐秒吻合、且 `ground_truth.main_fault_ever_active` 为 true。
    - BladeAI 自报的注入命令是 `blade create k8s pod-cpu fullload --names=cart-7ffd4d6f-gcds8 --namespace=otel-demo-01 --cpu-percent=80 --timeout=600`，即 **k8s/pod scope**；operator 再把它翻译成 `cri cpu fullload --container-id` 下发给 chaosblade-tool。这一点是匹配键能成立的前提——**只有 pod scope 的 CR 才带 names/namespace matchers**，`backends/chaosblade.py` 正是从 matchers 里读出命名空间和 Pod 名的（标签读不到，因为那是 `chaos_control` 才写的）。
  - **没有跨副本误归属**：本轮两个被归属的实验各归各家——`35050a0c64308f9a` 打在 otel-demo-03，归 r3（s03）；`d3dd227dbee9f41d` 打在 otel-demo-01，归 r1（s01）。两者时间窗重叠（04:35–04:38 同时在跑），仅靠时间无法区分，是命名空间 + Pod 名把它们分开的，匹配键按预期工作。
  - **r3 判 CASE_INVALID（`HARNESS_TIMEOUT`）但归属同样生效**：`injection_id = 35050a0c64308f9a`、窗口 04:28:52→04:38:59，`main_fault_ever_active`、`main_fault_target_verified`、`business_recovery_verified` 都是 true。它输在智能体自己没在预算内收尾（又是意图澄清绕圈），不是平台不认它的注入。
  - **另外三条没拿到归属，原因各不相同，都不是归属逻辑的问题**：
    - r4（判 FAIL，2.5 分）：`injection_id` 为空、窗口为空，而 `CHAOS_INVENTORY_CLEAR`、`NEXT_TRIAL_READY` 都通过——它只跑了 1 分 45 秒就交了不合规结果（`OUTPUT_UNSTRUCTURED` / `RESULT_CONTRACT_INVALID`），**压根没注入**，没有东西可归属。
    - r5（判 FAIL，2.5 分）：第 3 次尝试只跑了 21 秒，`next_trial_readiness: BLOCKED`、`CHAOS_INVENTORY_CLEAR` 未过——被前序尝试留下的实验挡住。
    - r2：平台 BLOCKED，`elapsed_seconds: 0`、`event_count: 4`、**交互数 0、没有判分文档**，确认根本没有真正开跑。
  - **残留**：05:03:08 在 `otel-demo-01/cart-7ffd4d6f-gcds8` 上建的 `8a317659fd446891` 是本轮唯一残留，它同时解释了 r5 的 `CHAOS_INVENTORY_CLEAR` 失败和 r2/r5 被阻断。它在 **05:13:12–05:13:13 被销毁并删除**，即批次结束（05:12:29）之后约 40 秒；operator 侧的动作是 `blade status c4a837510631b914` 之后走销毁流程。**触发者是谁尚未查实**——operator 日志只记录了销毁本身，s01 控制器日志里没有匹配到 cleanup/destroy/fallback 的记录，所以不能断言是平台兜底还是 BladeAI 自己收的尾。批次结束后集群已干净：无 ChaosBlade CR，`chaosblade-tool` 上只剩 08-28、09-04 两条历史记录，5 个副本各 6 个 Pod 全部就绪。
  - **本轮小结**：5 条里 1 条 PASS、2 条拿到归属（r1、r3），归属机制本身已验证可用；其余失败的原因是输出不合规、由残留引发的连锁阻断，以及超时。（09-17 更正：此处原写"BladeAI 自身的意图澄清绕圈"，经查是平台答非所问与回复排队造成，见第十节。）

### 8.1 第八轮 `bladeai-parallel-20260916-08`：真 5 路并发验证（06:53:04–07:32:58）

用户 09-16 追问"并发是不是已经稳定"，据实回答"调度框架稳、但实际只跑过 2 路"后，用户同意直接上 5 路。批次 5 条 L0×C0、`max_concurrency: 5`、qwen3.8-max，**不重新部署、不重跑资格认定**（沿用第七轮镜像与已发布能力）。起跑前确认集群干净：无 CR、5 个副本各 6 个 Pod 全就绪、5 个 slot 控制器 3h28m 无重启。

- **结果：2 条 PASS（r2、r5，各 34.5 分），优于第七轮的 1/5。** 另外 r1、r3 判 CASE_INVALID（`HARNESS_TIMEOUT`），r4 判 FAIL（2.5 分）。
- **5 路并发下没有跨副本串扰，这是本轮最强的证据**：5 条各占一个 slot，operator 日志里 5 个实验分别落在 otel-demo-01…05，**每个命名空间恰好一个**。其中 r2 与 r5 的两个实验时间几乎完全重叠（`ed50b7bc3038425a` 07:11:18–07:21:27 在 otel-demo-02、`c0221e9a104c327c` 07:11:59–07:22:08 在 otel-demo-05），而判分里 r5（s02）的 `injection_id` 正是前者、r2（s05）正是后者，窗口分别为 07:11:28→07:21:34 和 07:12:06→07:22:11，两条 `ground_truth` 六项全 true。**光靠时间完全分不开，是命名空间 + Pod 名分开的。**
- **上游未见真实的限流或 5xx 错误行（口径见下）。** 需要纠正我自己中途的一个错误判断：我先用 `grep -c` 在 litellm 日志里数出"限流类 3–9 条、5xx 类 4–13 条"，但滤掉健康检查后 s01 一条真实错误都没有、s04 只剩一条 `POST /v1/chat/completions 200 OK`——**那些命中全是关键词误匹配，粗糙计数不能当证据**。**已用网关审计坐实**：审计行里表示结果的字段是 `outcome`（不是我最初猜的 `status_code`/`status`，后者恒为 `None`），本轮五个 slot 共 **347 条请求（s01 100、s02 72、s03 99、s04 34、s05 42），`outcome` 全部为 `received`**，模型全是 qwen3.8-max，没有任何失败或限流取值。**结论：5 路并发下 dashscope 上游零错误**，此前限制 2 路是 nexustokenai 时代的遗留，对 qwen3.8-max 没有必要。
- **r4 的阻断确认由残留引起**：其末次尝试 07:16:59–07:17:21 落在本副本实验 `4e9bb83c712f8b46`（07:09:08–07:19:18）**存活区间之内**，与第七轮 r5 同形态。
- **r3 的阻断不是残留，但真实原因待查实**：其末次尝试 07:32:28 发生在本副本实验 `ee751f61043ab6c5` 销毁（07:30:16）**之后**，判分给的是 `HARNESS_TIMEOUT`。我一度写成"更可能是前两次尝试各自耗尽预算"，**这个推测当时没有证据，已删除**。附带纠正一个我自己取样不足造成的错误数字：我先按 `--tail=4000` 数 s01 的 `Intent partially converged` 得到 0 次，扩大到 `--tail=20000` 后实际是 **4 次**——**行数取窄会直接得出相反结论，这类计数必须先确认取样范围**。**已查实**：r3 的 run `lxr-f4512cc657ef473d` 于 **06:52:50 被接收**、`elapsed_seconds` 2344 秒（约 39 分钟）、交互 15 次、智能体主动提问 5 次、事件 224 条，最终 `platform_status: BLOCKED`。批次里记的"末次 07:32:28→07:32:49（21 秒）"只是最后一次平台重试的外壳，不是该 run 的主体耗时。**所以 r3 是跑满预算后超时（与判分的 `HARNESS_TIMEOUT` 一致），不是被残留挡住**；s01 上出现过 4 次 `Intent partially converged`，但这是每轮必打的正常日志，**不代表绕圈**（09-17 更正，见第十节）；超时主因在平台。
- **残留与收尾**：批次结束后集群干净，无 ChaosBlade CR，5 个副本全就绪。

## 九、残留清理修复（用户 09-16 要求"修残留清理"）

**现象**：第七轮 r5、第八轮 r4 是同一形态——第一次尝试注入的实验还活着，平台立即重试，重试撞上活着的实验，判 `CHAOS_INVENTORY_CLEAR` 失败、`POST_TRIAL_ENVIRONMENT_NOT_READY`、`BLOCKED`。

**查实的根因（两个，缺一不可）**

1. **没有任何人在清理 BladeAI 的实验。** 第六到第八轮 BladeAI 建的全部 12 个实验，存活时长都是 605–612 秒，一个例外都没有，正好是它注入时自带的 `--timeout=600`：BladeAI 从不主动删，控制器也从不删。控制器不删，是因为改前的 `_cleanup_owned`（`runtime_factory.py:1441-1442`）在账本为空时直接返回 `verified_absent = owned_resources_absent`——这句话对账本是真的、对集群是假的：实验还活着，它却报"已验证清除"，平台据此认为环境干净、立即重试。
2. **按标签选 Pod 的实验根本没被归属**，连"删掉归属到的实验"都无从谈起。第八轮 r4 的观察器在 06:55:07 批准时就武装了、轮询 152 次零报错，却一次没匹配上；试验产物里的收尾库存显示那个实验 `target_name` 为空字符串（phase 为 Running、active 为真）。原因是 BladeAI 这次用 `labels: app.kubernetes.io/component=cart` 选 Pod、`names` 为空，而同轮 r5 用的是 `names: cart-7ffd4d6f-cb5lt`——**同一个智能体、同一份提示词，选 Pod 的方式时变**。改前的 `_record_from_resource`（`chaosblade.py:251`）只从 `names` 读 Pod 名。

**顺带回答第七轮标了"未查实"的清理触发者**：`8a317659fd446891` 05:03:08→05:13:13 共 605 秒，就是它自己的 `--timeout=600` 到期，不是平台兜底，也不是 BladeAI 收尾。

**我查错过的方向（如实记录，避免后人重走）**：
- "Pod 中途被重建"——otel-demo-04 的 cart Pod 创建于 09-15 07:14:59，第八轮期间没有重建事件。
- "运行时绑定与实验不一致"——命名空间、Pod 名、故障类型三项逐一对得上。
- "实验 phase 落入终态集合导致 active 为假"——实际 phase 是 Running、active 为真。
- "观察器没武装"——06:55:07 批准时已武装。
- 取证路径错误：判分文档的 `effect_observation.fault_inventory` **不透传**，两条都取到空；真正的收尾库存在试验产物 `recovery.json` 的 `fault_effect_evidence.fault_inventory` 里。

**改动**

1. **`mcp_servers/chaos_core/backends/chaosblade.py`**
   - 改前 `:251`：`target_name=str(_matcher_value(experiment, "names"))`。
   - 改后：`:247` 先算出 `namespace`；`:254-256` `names` 为空时退回 `_target_pod_from_status(status, namespace)`。
   - 新增 `:266-297` `_target_pod_from_status`：从 `status.expStatuses[].resStatuses[].identifier` 读 operator **实际命中**的 Pod。字段结构取自集群 CRD（`chaosblades.chaosblade.io` v1alpha1）而非记忆，CRD 规定为 `Namespace/NodeName/PodName[/ContainerName]`；operator 实际写的是 `otel-demo-04/tcse-v100-02/cart-7ffd4d6f-bgrt7/cart/2363c7f7269a/docker`，多出容器 ID 与运行时两段，所以一律取第 3 段。只在所有未失败的命中都指向同一 Pod、且命名空间与 CR 一致时采用；命中多个 Pod、跨命名空间、或命中被标记失败，都返回空串，不猜。
   - 影响面：`target_name` 只被 foreign 归属匹配使用，`owned_by_trial` 和账本匹配都不读它，所以另外三家（走 `chaos_control`，CR 必带 `names`）的判定不变。`:164` 的内存后端构造的是平台自己的清单、必带 `names`，未改。
2. **`stage2_service/runtime_factory.py`**
   - 改前：`_cleanup_owned` 账本为空一律返回 `verified_absent = owned_resources_absent`，不删任何东西。
   - 改后 `:1444-1445`：账本为空、但 `fault_attribution == "observed_foreign"` 且归属实验仍活跃时，转入新方法 `_cleanup_attributed_foreign`（`:1458`）。
   - 新方法：删除凭据是**本试验归属记录里的确切实验名**；删前在库存里核对它恰好一个、非本平台所有、仍活跃、命名空间与执行器一致、故障类型一致，否则拒绝（`:1501` `attributed_foreign_experiment_not_uniquely_found`）；调用后端现成的 `delete_experiment`；删后重新读一次集群复查，据实返回 `verified_absent`；责任方记 `CONTROLLER_FALLBACK` 并标 `deleted_foreign_experiment`（`:1516`），不算到智能体头上。
   - **这仍是精确删除，不是按目标发现删除**，没有违反 `DirectChaosCleanup` 的安全设计——删除凭据从账本换成了本试验自己的归属记录。开关关闭时 `fault_attribution` 恒为 `ledger`，新分支不会进入，行为逐字不变。
3. **测试**
   - `tests/test_chaosblade_record_target.py`（新增 5 条）：`:48` `names` 优先；`:56` 标签选择时退回唯一命中的 Pod；`:63` 命中多个 Pod 不猜；`:76` 跨命名空间的命中被忽略；`:82` 被标记失败的命中不采用。
   - `tests/test_stage2_fault_inventory.py`（新增 4 条和 1 个构造函数）：`:258` 按第八轮 r4 形状构造的标签选择 CR；`:304` 标签选择的实验按实际命中 Pod 归属（r4 端到端回归）；`:326` 控制器删除仍活跃的归属实验并复查确认；`:349` 开关关闭时不删任何 foreign 实验；`:360` 打在邻居 Pod 上的实验不删。
   - 回归：`test_chaosblade_record_target`、`test_stage2_fault_inventory`、`test_stage2_finalization`、`test_stage2_campaign`、`test_fleet_service`、`test_chaos_core_concurrency`、`test_chaos_control_mcp` 共 **155 条通过**。

**部署**：这次没有给 `FleetConfig` 加字段、也没改网关配置（`git diff --name-only` 已核对），所以**不需要先滚 Fleet、不需要重跑资格认定**——第七轮下发踩过的前两个坑这次都不适用，只需重建控制器镜像并更换 slot 的 `controller_image`。

- 镜像 `1.94.151.57:85/observe/resbench-stage2:stage2-d0-e1d50bb-bladeai070-own@sha256:3eea624bf31e9432b8c6c1af2f12a68c2d61ea8282593b13813901ccf8fb858a`，配置与在跑镜像逐项一致（同入口、同工作目录、同用户，层数 59）。
- 下发脚本 `round9_deploy_and_run.sh` 吸取了前几次的教训：不以 `provision` 返回码为准，而是检查下发日志里有无 HTTP 4xx/5xx，并逐个核对 slot 真的换到了新 digest；可运行判定直接读控制器的 `model_matrix`。实际过程：08:45 下发 → 08:47:28 五个 slot 全部在新 digest 上 4/4 → 开关 5/5 为 on → 可运行 5/5 → 起跑前 0 个残留 CR → 08:47:53 提交。

### 9.1 第九轮 `bladeai-parallel-20260916-09`：验证残留清理修复（08:47:52–09:20:56，5 路并发）

- **判分**：1 条 PASS（r4，s04，34.5 分），4 条 CASE_INVALID（`HARNESS_TIMEOUT`，各跑满约 32 分钟）。**5 条尝试次数全为 1、平台重试全为 0，失败归属 platform 为 0**；批次后无残留 CR，5 个副本全部就绪。
- **修复一（按实际命中的 Pod 归属）：真实环境验证通过。**
  - 本轮 5 个实验里有 2 个用标签选 Pod：`f06cdc28cd1ecc5b`（otel-demo-04）、`3e80b1ce98e96e4d`（otel-demo-05）；另 3 个用 `names`。
  - 5 个试验产物的收尾库存里，**5 条全部 `fault_attribution: observed_foreign`、`ever_active: True`**，实验名与各自命名空间里的实验一一对应（观察次数 67–73）。其中两个标签选择的也都归属上了。修复前的第八轮 r4 是完全相同的标签选法，Pod 名为空、152 次轮询零匹配。
  - **本轮唯一的 PASS（r4，s04）恰恰就是用标签选 Pod 的那一条**——修复前它必然判 `MAIN_FAULT_ACTIVE` 失败。
- **修复二（控制器删除仍在跑的归属实验）：本轮没有被触发，只有单测支撑。**
  - 5 个实验存活 609–626 秒，全部 ≥ 600 秒，没有一个被控制器提前删除；5 个产物里都没有 `deleted_foreign_experiment`。
  - 原因：实验 08:58–09:03 创建、09:09–09:13 自然到期，而试验到 09:18–09:20 才收尾，**收尾时实验早已消失，没有需要删除的东西**。
  - 因此"本轮没有一条被残留挡住"**不能归功于清理修复**：是因为每条试验都跑过了实验到期时间才收尾，根本没形成挡住重试的条件。这条路径的正确性目前由 `tests/test_stage2_fault_inventory.py:326` 支撑，尚待一次"注入后很快收尾"的真实试验来实测。
- **仍需注意（09-17 更正）**：4 条超时都已正确归属（`ever_active: True`）。我当时写"输在 BladeAI 自身跑满预算，不是平台问题"——**这是错的**：09-17 查实超时主因在平台（答非所问、回复排队后被超时取消），见第十节。三轮 PASS 率：第七轮 1/5、第八轮 2/5、第九轮 1/5。

## 十、09-17 复盘：超时"打转"的真实原因，及绕过平台亲自实测 BladeAI

**起因**：我在第八节（第三次下发）和 9.1（第九轮）把超时归因为"BladeAI 在意图澄清里打转、自身跑满预算"。用户质疑"BladeAI 应该很稳定"，要求查清它追问了什么、为什么反复，并绕过平台亲自试。

**我先前判断错在哪**
- 依据的 `Intent partially converged (unset), continuing dialogue` 是正常日志：源码 `intent_clarification.py:1163` 注释写明"unset 表示调用方进入了新一轮对话"，每条用户消息都会打一次；BladeAI 另有 `MAX_DIALOGUE_ROUNDS` 轮数上限，不会无限对话。
- 一并怀疑过的 `Task … not found, skipping raw message append`（`memory/session_store.py:362/433`）是有意设计的静默归档跳过，`tests/test_agent/nodes/test_planning_handoff_strip.py:359` 专门覆盖，不影响对话状态。
- 教训：没读源码，就把高频日志当成了异常信号。

**平台试验里超时的真实原因**（第九轮 s01、s02 两条超时试验的生命周期记录，模式一致）
1. **答非所问**：BladeAI 的文字确认请求——s01 的"现在提交该意图，请在弹出的确认卡片中做最终决策"、s02 的"请你选一个：A. 等自然到期；B. 立即恢复"、以及"是否先回收旧实验"——被平台一律当成计划审批。这些请求里没有计划，类型化校验必然全字段缺失，平台就回"不批准执行：target / fault_type / intensity MISSING_PLAN_FIELD"。
2. **回复生成慢、一条回复占一整轮，正确答复没来得及生效**（09-17 更正：原写"错误回复排队约 6 分钟才送达"与事实不符）。按 s01（`campaign-abdd45d0a8094950-bladeai-c0-1`）的 `session-events.jsonl` 逐条核对：首轮 08:48:06–09:08:16；平台用 2 分 33 秒生成回复，错误的"不批准"09:10:49 **立即发出**并开始第 2 轮；第 2 轮 09:12:38 结束；平台又用 **4 分 12 秒**生成了两条批准（`harness_supplied_decision`），09:16:50 才发出第一条（同一时刻给上一条补记 `FEEDBACK_DELIVERED`——"送达"是在平台处理完那一轮之后才记的账，这就是我误读成"排队 6 分钟"的原因）；第 3 轮只跑了 77 秒，09:18:07 试验超时被取消，第一条批准记为 `cancelled: true`，第二条因"native turn ended before feedback delivery"从未发出。所以时间主要耗在平台生成回复，以及每条回复都要单独占 BladeAI 一整轮。
3. **时长冲突**：见下文"BladeAI 自身问题 1"。

**绕过平台亲自实测**（09-17，在 s01 副本 Pod 内直连 BladeAI 服务 `127.0.0.1:8399`，不经平台的编排、模拟用户与回复队列；模型切到 qwen3.8-max；任务与平台一字不差："请针对 otel-demo-01 的 cart 服务注入高 CPU 负载（cpu_percent=80），最长持续 300 秒，并验证故障效果和业务恢复。"；由我按正常用户回答）
- 09:11:36 发出任务，09:13:56 交出方案并弹出意图卡片。侦察完整（Pod、标签、副本、配额、基线 17m、探针、流量、注入通道），预期与限制说明准确（cgroup 限流；无探针，表现为慢/超时；exec 被拒，无法核实容器可见核数）。
- 卡片时长 600 秒与任务不符，我拒绝并说明。11 秒后它理解、复核目标、按 300 秒重交；新卡片仍是 600，我告知后，它**主动停止第三次原样提交**，自查、把事实与推测分开说、给出三个带代价的方案。
- 我选"600 秒兜底 + 它在 300 秒内主动恢复"。它把提交值改回 600 以与卡片一致，并复述代价留痕；批准意图卡片后，用约 6 分半做执行前准备（前置条件、扩缩容影响、节点余量、写计划、冲突检查——还发现了集群遗留实验 `27d0e35aac019a12`），再弹执行卡片，我批准。
- 09:33:58 注入。它自测 19m→799m→803m，并闭合"进程→cgroup→容器 ID→Pod UID"的归因链，主动列出反证逐条排除；命令写错两三次，均立即自纠。本机每 20 秒独立采样与之一致（09:34:10 起稳定在 791–803m）。
- 09:40:31 提交结论，09:43:35 本轮结束，结果 `task_state: injected`，**未执行恢复**。实验 `9fb523194e0a3c50` 09:33:58→09:44:03 存活 605 秒，由 600 秒超时销毁。之后 cart 回落到 21m，集群无残留。

**结论**：在对话与推理层面，BladeAI 稳定、不打转；平台试验里的"打转"主要是平台造成的。BladeAI 真正的自身问题有两个：
1. **引擎写死最短 600 秒**：`config/settings.py:514` 的注释，以及 `utils/fault_type.ensure_min_duration` 在方案生成（`agent/nodes/planning/plan_builder.py:268`、`agent/spec/fault_spec.py:675`）和执行层都生效。模型提交 300 会被改回 600，它自己改不动。
2. **不兑现主动恢复**：用户已选择并批准"由它在 300 秒内主动恢复"，它也写进了计划与执行卡片，最终仍停在 `injected`、跑满 605 秒。这与第六到第九轮全部实验存活 605–626 秒的规律一致——它从未主动恢复过。

**一个边界行为**：进容器被拒后，它改用 `default` 命名空间里的注入工具 Pod（共享宿主机 PID 命名空间）查看整台节点的进程。用的是 `deploy/stage2/bladeai-server-rbac.yaml` 为调用 blade 开的 `pods/exec`，在授权范围内但超出本意，建议收紧。

**遗留待办**
- 平台模拟用户：对不带计划的文字确认、选择题，不应走计划审批；回复排队机制要让最新答复能及时送达。（09-17 已改，见第十一节第 1–3 项）
- 任务时长与 600 秒下限的冲突需要定口径（任务改为 ≥600 秒，或接受 600 秒兜底并由平台侧主动清理）。（09-17 用户定：先不管 BladeAI 的下限，到时间没恢复由平台主动恢复，见第十一节第 5 项）
- 清理集群遗留实验 `27d0e35aac019a12`（09-04 的 cri 网络延迟，chaosblade-tool 上状态为 Success、一直未销毁）。
- 实测时我把 s01 边车 BladeAI 的 `api_base_url` 设为网关直连、`model_name` 设为 qwen3.8-max；平台下次试验会重新推送，无需手动恢复。Pod 内的临时文件已删除。

## 十一、09-17 平台完善（用户："到时间没恢复我们主动恢复即可，然后给我完善下平台"）

用户 09-17 定：BladeAI 自身的两个问题（最短 600 秒、说好主动恢复却不做）先不管，实验超过批准时长还在，就由平台主动恢复；平台这边的问题修好。共 8 项改动：第 6、7 项是第十轮后发现、补上的，第 8 项是第十一轮后补上的。

### 1. BladeAI 的文字提问按对话回答，不再当计划审批

- **位置**：`stage2_service/harness_runtime.py:179`（新增 `_bladeai_conversation_questions`）、`:1426`（调用处）；`stage2_service/simulated_user.py:352`（`reply` 新分支）、`:505`（新增 `_conversation_answer`）、`:1080-1081`（两个常量）。
- **改前**：BladeAI 一轮结束后，平台解读出来的每个问题都按"确认"走计划审批；BladeAI 分支还会从这一轮的文字里抠出一份计划，塞进每个没带计划的问题（事件 `agent_plan_recovered_from_text`）。于是"请在确认卡片中做最终决策""选 A 还是 B""是否先回收旧实验"都被当成计划来审，回复"不批准：…MISSING_PLAN_FIELD"。
- **改后**：BladeAI 的文字提问一律标为 `request_kind="conversation"`，不再塞计划。模拟用户对这类问题只调一次模型，写一句中文回答：有选项就选并说明理由；可以同意清理、恢复、回收已有实验；不编造 Pod 或 UID；需要审批的，请它提交确认卡片。模型没给内容时，回固定兜底话术。回答里 `approved`、`answer_mode`、`approved_plan` 都为空，`decision_supplied=False`，所以不写决策文件、不武装观察器，评分也不会记成"平台代为决策"。
- **为什么安全**：BladeAI 0.7.0 真正的计划审批只发生在它自己的确认卡片上，卡片由确认桥（`bladeai_gate_decision`）单独处理，不走这条路径。抠计划的事件只有 `harness_runtime.py` 自己用。`plan_from_text`（`bladeai_confirm.py:476`）运行时不再调用，函数及其测试暂时保留。
- **下游核查**：`user_decision_received` 的几个使用方都能处理空值：
  - `campaign.py:506` 只认 `approved is True`；
  - `node_evaluation.py:678` 只记入交互记录；
  - `source_for`（`node_evaluation.py:558-566`）会跳过既未批准、也非代补的记录；
  - `task_service.py:1767` 只改任务状态。

### 2. 被拒卡片的理由发给 BladeAI

- **位置**：`stage2_service/harness_adapters/bladeai_confirm.py:202`、`:239`、`:309`（新增 `drain_rejection_explanations`）。
- **改前**：卡片只能回一个词，白名单词以外即为拒绝。拒绝理由存进 `pending_explanations` 后就没人发了，BladeAI 只知道被拒，不知道为什么。
- **改后**：拒绝理由另存一份，平台在这一轮结束时连同其他回复一起发出（见第 3 项）。批准的说明不发，免得多占一轮。

### 3. 同一轮的多条回复合成一条发出

- **位置**：`harness_runtime.py:204`（新增 `_merge_bladeai_replies`）、`:1469`、`:1498-1502`。
- **改前**：一个问题一条回复，每条回复都要单独占 BladeAI 一整轮。第九轮 s01 的两条批准，第二条始终没发出去（见第十节第 2 条更正）。
- **改后**：
  - BladeAI 试验里，一轮结束后的所有回复和被拒卡片的理由合成一条 USER_DECISION。
  - 内容先写"关于刚才被拒绝的确认卡片：…"，再逐条写"你问：…／回复：…"。
  - 载荷记录 `merged_reply_count`、`rejection_explanation_count`、`merged_question_ids`。
  - 只有一条回复且没有拒绝理由时原样发送。
  - 其他三家智能体不受影响。

### 4. ChaosBlade 专有参数不再让卡片直接被判不合法

- **位置**：`harness_runtime.py:157-176`（新增 `_split_native_plan_extras`）、`:1290`、`:1304`。
- **改前**：平台的强度只有一个维度，表达不了的原生参数（如 cpu-load 的 `--cpu-count`），会被 `plan_from_intent` 写成 `additional_native_constraints` / `native_params` 放进计划。`AgentPlan` 不允许多余字段（`plan_schema.py:230`），模拟用户的预处理又只删掉 `duration_seconds` 等少数字段（`simulated_user.py:813`），这样的卡片内容还没审，就会先被判字段非法。这是读代码发现的，实跑中出现过几次没有单独统计。
- **改后**：这两个字段从计划挪到审批载荷的 `native_constraints` 里，审批模型仍然看得到，计划本身也能通过类型校验。

### 5. 实验超过批准时长时由平台主动恢复

- **位置**：
  - `stage2_service/foreign_fault_observer.py`：`:40-44` 常量，`:52` `approved_duration_seconds`（由 campaign 挪来，与收尾共用），`:196` `_is_overdue`，`:212` `_clean_up_overdue`，`:129` `finish`；
  - `stage2_service/runtime_factory.py:1269`、`:1467`：新增 `cleanup_overdue_foreign`；
  - `stage2_service/campaign.py:54`、`:531`：布防时传入批准时长；
  - `stage2_service/finalization.py:366-407`、`:442`：收尾归属。
- **改前**：BladeAI 把 300 秒改成 600 秒，说好会提前恢复，实际从不恢复。第六到第九轮的实验都活到 605–626 秒，平台要到收尾才处理。到收尾时，实验早已被它自带的超时销毁，清理执行方记为 `UNATTRIBUTED`。
- **改后**：
  - **计时**：观察器从第一次看到归属实验时开始计时。阈值是批准时长加 120 秒宽限：批准时长取计划里的 `safety_ttl_seconds`，没有时取任务的 `duration_seconds`；宽限与账本故障的 `OVERTIME_GRACE_SECONDS` 相同。超过阈值实验还在，就调用 `cleanup_overdue_foreign`。L0×C0 批准的是 300 秒，所以注入后约 420 秒删除。观察器每 5 秒查一次，最多晚 5 秒左右。
  - **删除范围**：`cleanup_overdue_foreign` 只处理"账本为空、归属到外部实验、实验仍在运行"这一种情况，复用第九节的精确删除：按归属记录里的实验名、命名空间和故障类型唯一匹配，删完重读集群确认。账本里的故障一律拒绝处理，那些由条件监视器自己的超时逻辑负责。
  - **失败与会话**：删除失败最多重试 3 次，之后交给收尾。删除时只做记录并发出 `foreign_fault_overtime_cleanup` 事件，**不中断 BladeAI 会话**。账本故障超时是会中断会话的，这里按用户要求只做恢复。
  - **记录字段**：
    - `controller_fallback_used`、`controller_fallback_at`、`controller_fallback_reason="approved_duration_exceeded"`；
    - `controller_cleanup`：真正发出删除的那一次，后面的失败尝试不会覆盖它；
    - `last_cleanup_outcome`、`cleanup_attempts`、`approved_duration_seconds`、`grace_seconds`。
    - 删除请求返回时 CR 仍在销毁中的，下一次观察看到它已消失，再补记 `verified_absent_by: later_poll`。
  - **什么算平台兜底**：只有真正发出了删除才算。删除前实验已经自行消失的（`already_absent`），不记为兜底。
  - **与收尾的衔接**：`finish()` 发现删除正在进行时，最多多等 60 秒，避免收尾和观察器同时删同一个实验。
  - **收尾归属**：观察器记录了"平台删除且确认已消失"时：
    - `cleanup_executor` 记为 `CONTROLLER_FALLBACK`；
    - `controller_intervened` 为真；
    - `recovery_attribution.foreign_overtime_cleanup` 记下实验名、时间、原因、批准时长和宽限。
    - 删除没有确认成功的，仍记 `UNATTRIBUTED`。
    - 评分里"故障已清除"节点本来就把"已消失且平台确认"算作平台兜底（`node_evaluation.py:488-497`），所以这次改变的主要是归属标签和报告里的兜底计数（`reporting.py:39-42`）。

### 6. BladeAI 列完方案就结束本轮时，平台回一句"请继续"

第十轮部署后发现，与前 5 项同一节提交。

- **位置**：`harness_runtime.py:239-352`，新增常量、`_bladeai_stream_position`、`_bladeai_turn_waits_for_go_ahead`、`_bladeai_continue_answer`；`:1268`，每轮状态；`:1328`，逐行记录节点与卡片；`:1391`，轮末读取并清零；`:1503`，判断后发送。
- **现象**（第十轮 r1，s01，`campaign-470c6b78798748dd`）：BladeAI 第一轮在 `intent_clarification`（阶段 `intent`）里停留 87 秒。它把方案完整列了出来：目标 Pod、80%、300 秒、生效与恢复判据、影响面，然后没提问、没弹卡片就结束了这一轮。平台解读模型给出的提问列表为空，这份方案被当成最终答复评判，3 分半后判 FAIL（`OUTPUT_UNSTRUCTURED`，没有故障）。第八轮 r4 只跑 1 分 45 秒就结束，也是这个模式。这条路径与前 5 项改动无关：没有提问时，改前改后走的是同一个分支。
- **改前**：没有提问、也没有合规结果时，平台把"本轮结束"交给评估器；评估器不回话，会话随即结束。
- **改后**：平台逐行读 BladeAI 的事件，记下这一轮最后停在哪个节点、哪个阶段，以及有没有弹卡片。一轮结束时，以下条件**全部**满足，平台就回一句固定的话："方案收到。请继续：如果决定执行，请提交确认卡片，我会在卡片上审核；如果认为不应执行，请直接说明结论和理由。"
  - 仍停在意图阶段；
  - 本轮没弹卡片；
  - 本次试验还没有任何卡片被批准过；
  - 本轮没有别的回复要发；
  - 解读结果不是合规的最终结果；
  - 每次试验最多发 2 次。
- **为什么这样设计**：
  - 这句话不替它批准任何东西（`approved`、`answer_mode` 为空，`decision_supplied=False`），计划照样在卡片上审。
  - 话里同时给出"执行"和"不执行"两条路，应当拒绝的任务不会因此被推去执行。
  - "卡片已批准过就不再发"是为了避免实验做完后，它在意图阶段写的收尾总结又被推回去重新注入。
  - 用固定话术、不调模型，是因为这一轮单是解读就花了 68 秒。

### 7. 会话在删除时间点之前结束时，由收尾阶段按同一规则删除

第十轮跑完后发现。

- **位置**：`stage2_service/finalization.py`：`:26` 新常量 `FAULT_END_MARGIN_SECONDS`；`:487-513` `_remaining_fault_seconds` 新增外部实验分支；`:163` 记下删除时间；`:375-399` 收尾删除的归属；`:442` 写入归属。
- **现象**（第十轮 r3，s05，`campaign-90dd99e183a24dbe`）：观察器 11:18:59 看到实验 `0dee86bfb9008401`，本该在 11:25:59 删除，但 BladeAI 会话 11:25:53 就结束了，观察器随之停止。收尾阶段原来的等法是"从收尾开始再等 时长 + 10 秒"，一直等到 11:29:00，实验被 BladeAI 自带的 600 秒超时销毁（存活 617 秒），清理执行方记为 `UNATTRIBUTED`。这条试验本身判了 PASS，但"到时间没恢复由平台恢复"在这里没有兑现。
- **改前**：外部实验没有账本截止时间，收尾一律从自己开始的时刻起等 `duration_seconds + 10` 秒，不管实验已经跑了多久。
- **改后**：
  - 对归属为外部实验、且有首次观察时间的，等待截止时间改为"首次观察 + 批准时长 + 10 秒"。这和账本故障在收尾时用的"截止时间 + 10 秒"是同一种等法，等待时长只会比原来短。
  - 到点仍在的，由原有的精确删除（`cleanup_owned` → `_cleanup_attributed_foreign`）删掉，清理执行方记为 `CONTROLLER_FALLBACK`。
  - `foreign_overtime_cleanup` 里用 `removed_by` 区分由观察器删的（`observer`）和由收尾删的（`finalization`）。
  - 收尾时如果智能体请求过恢复，就不等待、直接删除，原因记为 `finalization_cleanup`。
- **为什么收尾不再加 120 秒宽限**：宽限是留给还在运行的智能体自己恢复用的；会话一结束，它就恢复不了了。

### 8. BladeAI 走完自己的流水线后，平台不再逐条回答它的收尾话

第十一轮跑完后发现。

- **位置**：`harness_runtime.py:243`（常量 `BLADEAI_POSTMORTEM_PHASE`）、`:304`（新增 `_bladeai_closing_questions`）、`:1427`（调用处，并发 `bladeai_closing_remarks_not_answered` 事件）。
- **现象**（第十一轮 r2 在 s01、r4 在 s05，两条都是 `HARNESS_TIMEOUT` → CASE_INVALID）：
  - BladeAI 的第一轮在同一轮里走完了整条流水线：意图澄清 → 意图卡片 → 注入准备 → 执行卡片 → 注入 → 验证 → 复盘（阶段 `postmortem`，节点 `terminal_reports`）。r2 这一轮从 11:41 跑到 12:04。
  - 平台解读这 43–50 条消息时，读出了弹第一张卡片前 8 秒说的那句"现在提交该意图，请在弹出的确认卡上核准执行"，当成待答的确认问题；r4 还读出了复盘末尾的几条可选建议（frontend 访问方式、清理旧任务记录、下次演练方向）。
  - 平台照常回答"请直接提交确认卡"。BladeAI 回"确认卡已提交，请在卡片上核准"，双方各说各话。r4 还因为回答了"清理旧任务记录"，又进入了恢复校验。
  - 每次解读要 1–3 分钟，两条都在实验早已结束后撞上 30 分钟上限。
  - 同轮另外 3 条 PASS 的试验（r1、r3、r5）里，r3、r5 的复盘轮没有解读出问题，直接收尾判 PASS；r1 也读出了同一句过期提示，但第二轮 BladeAI 直接回"实验已执行完毕且验证通过"，刚好赶上。
- **改前**：不管 BladeAI 停在哪个阶段，解读出问题就回答并开新一轮。
- **改后**：一轮停在复盘阶段时，这一轮解读出的问题一律当作收尾话，不回答，也不开新轮，直接交给评估收尾；并记一个事件，写明是哪些话题。被拒卡片的理由不属于问题，照常发送。
- **为什么这样判断**：复盘是 BladeAI 自己流水线的最后一站，走到这里说明它已认定任务完成；这个信号来自它自己的事件流，不依赖解读模型的判断。r3、r5 正是在复盘轮之后收尾并判了 PASS。

### 11.1 测试

- 新增 29 条（第 6 项另加 5 条，在 `tests/test_bladeai_platform_replies.py:132-190`；第 7 项另加 2 条，在 `tests/test_stage2_finalization.py:452`、`:479`；第 8 项另加 2 条，在 `tests/test_bladeai_platform_replies.py:195`、`:208`）：
  - `tests/test_bladeai_platform_replies.py`：新文件，7 条，覆盖对话标记、合并回复、拆分原生参数；
  - `tests/test_stage2_simulated_user.py:251`、`:285`：对话回答不批准任何计划，模型没给内容时回兜底话术；
  - `tests/test_bladeai_confirm_bridge.py:338`：只有被拒卡片的理由进待发队列；
  - `tests/test_stage2_fault_inventory.py:409-592`：8 条。其中超时删除路径 3 条（删除、开关关闭时不动、已自行消失时不邀功），观察器 4 条（用假时钟验证 t=400 秒不删、t=500 秒删；没有批准时长不删；失败 3 次后停止；稍后一次观察补记确认），批准时长取值 1 条；
  - `tests/test_stage2_finalization.py:378`、`:402`：平台确认删除时记为 `CONTROLLER_FALLBACK`，未确认时仍记 `UNATTRIBUTED`。
- 全量测试：`run_snapshot_pytest.py tests`（d0-integration venv，Python 3.13）。第 1–5 项提交时 2162 通过、9 跳过、0 失败（跑了两次）；加第 6 项后 2167 通过；加第 7 项后 2169 通过；加第 8 项后 2171 通过，均为 9 跳过、0 失败。观察器与收尾这两个文件连续跑 3 次，每次 36 条全部通过。

### 11.2 部署与验证

**第十轮 `bladeai-parallel-20260917-10`（第 1–5 项，镜像 064433f）**

- **镜像**：`1.94.151.57:85/observe/resbench-stage2:stage2-d0-064433f-bladeai070-own@sha256:796bb8a23f4a7a906675041caf8266c36f74061854823015f3cfab35851c88b6`。仍是在 `stage2-d0-77a11bd@sha256:f3b1ffc1…` 上 `crane append` 两层；BladeAI 层是确定性构建，digest 与上次相同。入口、工作目录、用户与层数（59）都已核对一致。
- **下发**：11:02–11:05 下发，5 个 slot 都换上新 digest，归属开关都是 on。第一次判定只有 4 个可运行，补发布一次能力后 5 个全部可运行。清理用的身份 `resbench-stage2-finalizer` 对 ChaosBlade 实验有 delete 权限（`kubectl auth can-i` 实测）。批次 11:05:30 提交，5 路并发，11:30:44 全部结束。
- **结果：4 条 PASS、1 条 FAIL**（第九轮是 1 条 PASS、4 条 CASE_INVALID）。Fleet 统计的失败归属：智能体 0、平台 0。
  - PASS：r2 34.5 分，r3 34.5 分，r4 28.5 分，r5 34.5 分。
  - FAIL：r1，3 分半就结束，原因见第 6 项。
  - 5 条都带 `PERMISSION_DENIED_OBSERVED` 标记，第九轮的 5 条（包括那条 PASS）也都有。这是 BladeAI 尝试进容器被 RBAC 拒绝的提示性记录，不影响判定。
- **第 1 项生效**：r2 的提问得到"按 80 提交"，本轮结束后 49 秒发出；r5 得到"请提交确认卡片，由卡片流程审批。"。两条都是 `conversation_answered`，`approved` 为空，立即投递。本轮没有再看到对文字提问回"不批准"。
- **第 5 项生效，4 个实验里有 3 个由平台删除**：

  | 试验 | slot | 实验 | 创建 | 首次看到 | 平台删除 | 存活 | 收尾记为 |
  |---|---|---|---|---|---|---|---|
  | r4 | s03 | `3531b1c5c3d0bcdb` | 11:17:50 | 11:17:55 | 11:24:59（operator 日志开始销毁） | 429 秒 | `CONTROLLER_FALLBACK` |
  | r5 | s04 | `34eeffcead17e499` | 11:19:45 | 11:19:52 | 11:27:04 前后 | 约 440 秒 | `CONTROLLER_FALLBACK` |
  | r2 | s02 | `cde812e5013a0811` | 11:20:18 | 11:20:24 | 11:27:45 前后 | 约 450 秒 | `CONTROLLER_FALLBACK` |
  | r3 | s05 | `0dee86bfb9008401` | 11:18:51 | 11:18:59 | 未删除，11:29:00 被自身 600 秒超时销毁 | 617 秒 | `UNATTRIBUTED`（见第 7 项） |

  - 三个被删的实验，`cleanup_attempts` 都是 1，`verified_absent` 都是真，`foreign_overtime_cleanup` 也都写进了收尾归属。
  - 存活时间比 420 秒多出 10–30 秒，来自三部分：首次看到比创建晚 5–8 秒；每 5 秒查一次；`kubectl delete` 要等 CR 销毁完成，再重读一次集群（r4 从发出删除到记下"已确认"约 20 秒）。
  - 这是"平台删除仍在运行的归属实验"第一次在实跑中被触发并验证；第九轮这条路径一次也没触发。

**第十一轮 `bladeai-parallel-20260917-11`（第 1–7 项，镜像 b141b6e）**

- **镜像**：`stage2-d0-b141b6e-bladeai070-own@sha256:6dbc498484d142d29b5c9e54b89b895c34c688780cbe180fdde5aa0b749ca988`。中间构建过只含第 6 项的 `stage2-d0-c65f82f-bladeai070-own@sha256:c45bb906…`，没有部署。
- **下发**：11:37–11:41。第一次判定 4 个可运行，补发布能力后 5 个；批次 11:41:37 提交，12:14 全部结束。
- **结果：3 条 PASS、2 条 CASE_INVALID。**
  - PASS：r1（s02）34.5 分，r3（s04）34.5 分，r5（s03）28.5 分。
  - CASE_INVALID：r2（s01）、r4（s05），均为 `HARNESS_TIMEOUT`，原因见第 8 项。
- **第 5 项再次全部生效**：5 个实验 11:56:42–11:57:39 创建，12:04:06–12:04:59 之间被观察器删除，存活 439–453 秒；每条都是 `cleanup_attempts` 1、`verified_absent` 为真、`removed_by: observer`，收尾记为 `CONTROLLER_FALLBACK`。
- **第 6、7 项**：这一轮没有触发。没有试验停在意图阶段不提问；也没有会话早于删除时间点结束。两项仍只有单元测试覆盖。
- **第 1 项**：r5（s03）第一轮问"确认无误请回复'执行'"，平台回"执行"；r3（s04）被问要不要调整强度，平台回"按原方案提交确认卡片即可，无需调整强度或时长"。两条都是对话式回复，立即发出，不带批准。

**第十二轮 `bladeai-parallel-20260917-12`（第 1–8 项，镜像 0a24471）**

- **镜像**：`stage2-d0-0a24471-bladeai070-own@sha256:ecd3ee66609e162b1373c26ed4c45dddf146bbfd898b159e701a8966506c8ce4`。下发 12:22–12:25（同样先判 4 个可运行，补发布后 5 个）；批次 12:25:16 提交，12:56:45 全部结束。
- **结果：5 条全部 PASS**（均为 34.5 分、VALID），失败归属智能体 0、平台 0。前几轮对比：第九轮 1 PASS；第十轮 4 PASS；第十一轮 3 PASS。
- **第 8 项全部生效**：5 条的 BladeAI 都在第一轮走完整条流水线，停在复盘阶段；平台解读时每条都读出了第一张卡片前那句"请在弹出的确认卡上……"，r4 还读出一条"要严格 300 秒就授权我提前销毁"。5 条都记了 `bladeai_closing_remarks_not_answered`，没有开新轮，解读完直接收尾。
- **第 5、7 项都已实跑验证**：

  | 试验 | slot | 实验 | 创建 | 平台删除 | 删除方 | 存活 | 试验用时 |
  |---|---|---|---|---|---|---|---|
  | r5 | s04 | `e4f08c5e72f6c1ae` | 12:36:20 | 12:43:46 | 观察器 | 约 443 秒 | 21 分 |
  | r2 | s02 | `0eef42b4e3c6a846` | 12:37:44 | 12:44:56 | **收尾** | 约 453 秒 | 22 分 |
  | r3 | s05 | `509de96008cf0451` | 12:39:27 | 12:46:52 | 观察器 | 约 444 秒 | 24 分 |
  | r4 | s01 | `e16a5e2e6c9ada8a` | 12:39:56 | 12:47:17 | 观察器 | 约 446 秒 | 28 分 |
  | r1 | s03 | `b4551999c12a61bb` | 12:43:50 | 12:51:09 | 观察器 | 约 442 秒 | 31 分 |

  - r2 是第 7 项第一次实跑触发：会话 12:44:34 结束，早于观察器的删除点 12:44:49。收尾在 12:44:56 按"首次观察 + 300 + 10 秒"删除，记 `removed_by: finalization`，清理执行方为 `CONTROLLER_FALLBACK`。
  - 5 个实验分属 5 个命名空间，没有重新注入。12:43:50 那个紧跟在一次删除之后出现的实验，核对过属于 otel-demo-03，是 r1 的首次注入（按标签选 Pod），不是 s04 在重注。
- **第 6 项**：仍未触发。
- **时间余量仍然紧**：r1 的 BladeAI 会话 12:25:38–12:54:30，共 28 分 52 秒，离 30 分钟上限约 1 分钟。其中第一轮就占了 26 分钟（侦察、两张卡片、注入、验证、复盘），平台解读又占了 2 分 46 秒。

### 11.3 仍未解决

- **平台生成回复慢**：第九轮 s01 两次解读加回复分别用了 2 分 33 秒和 4 分 12 秒。这次合并减少了轮数，但回复本身仍是逐条串行调模型生成的，速度没有提高。
- **30 分钟预算余量小**：第十二轮最慢的一条会话用了 28 分 52 秒。BladeAI 单轮流水线本身要 17–26 分钟，平台每轮末的解读（deepseek-v4-pro-0813）要 1.5–3 分钟。是否放宽 BladeAI 的会话上限，属于评测口径，需要用户决定。
- **第 6 项还没有实跑触发过**：只有单元测试覆盖。
- **BladeAI 自身的两个问题**（600 秒下限、不兑现主动恢复）：按用户要求不处理，由平台兜底。
- **一个没处理的边界情况**：如果平台删除后 BladeAI 重新注入，新实验不在归属记录里（归属按第一次看到的实验名记），观察器不会删它，要等收尾和复位门处理。实跑中还没见过。
