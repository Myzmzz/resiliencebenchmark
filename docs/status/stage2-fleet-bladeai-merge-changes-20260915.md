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
  - **s02 为什么 timeout**：它的 bladeai-server 一直在意图澄清里打转——`chaos_agent.agent.nodes.planning.intent_clarification: Intent partially converged (unset), continuing dialogue` 反复出现，每次请求约 2.8–3.8 万 prompt token，绕满约 30 分钟预算。同一提示词、同一模型、同一时刻另外四个都一次过；s02 的 `failure_reasons` 只比 s01 多 `harness_report_not_completed` 一条，其余六条都是黑盒认定本就跳过的 MCP 通道类。**定性：BladeAI 自身的概率性行为，与本次归属改动无关。**
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
  - **本轮小结**：5 条里 1 条 PASS、2 条拿到归属（r1、r3），归属机制本身已验证可用；其余失败集中在 BladeAI 自身行为（意图澄清绕圈、输出不合规）和由残留引发的连锁阻断。

### 8.1 第八轮 `bladeai-parallel-20260916-08`：真 5 路并发验证（06:53:04–07:32:58）

用户 09-16 追问"并发是不是已经稳定"，据实回答"调度框架稳、但实际只跑过 2 路"后，用户同意直接上 5 路。批次 5 条 L0×C0、`max_concurrency: 5`、qwen3.8-max，**不重新部署、不重跑资格认定**（沿用第七轮镜像与已发布能力）。起跑前确认集群干净：无 CR、5 个副本各 6 个 Pod 全就绪、5 个 slot 控制器 3h28m 无重启。

- **结果：2 条 PASS（r2、r5，各 34.5 分），优于第七轮的 1/5。** 另外 r1、r3 判 CASE_INVALID（`HARNESS_TIMEOUT`），r4 判 FAIL（2.5 分）。
- **5 路并发下没有跨副本串扰，这是本轮最强的证据**：5 条各占一个 slot，operator 日志里 5 个实验分别落在 otel-demo-01…05，**每个命名空间恰好一个**。其中 r2 与 r5 的两个实验时间几乎完全重叠（`ed50b7bc3038425a` 07:11:18–07:21:27 在 otel-demo-02、`c0221e9a104c327c` 07:11:59–07:22:08 在 otel-demo-05），而判分里 r5（s02）的 `injection_id` 正是前者、r2（s05）正是后者，窗口分别为 07:11:28→07:21:34 和 07:12:06→07:22:11，两条 `ground_truth` 六项全 true。**光靠时间完全分不开，是命名空间 + Pod 名分开的。**
- **上游未见真实的限流或 5xx 错误行（口径见下）。** 需要纠正我自己中途的一个错误判断：我先用 `grep -c` 在 litellm 日志里数出"限流类 3–9 条、5xx 类 4–13 条"，但滤掉健康检查后 s01 一条真实错误都没有、s04 只剩一条 `POST /v1/chat/completions 200 OK`——**那些命中全是关键词误匹配，粗糙计数不能当证据**。**已用网关审计坐实**：审计行里表示结果的字段是 `outcome`（不是我最初猜的 `status_code`/`status`，后者恒为 `None`），本轮五个 slot 共 **347 条请求（s01 100、s02 72、s03 99、s04 34、s05 42），`outcome` 全部为 `received`**，模型全是 qwen3.8-max，没有任何失败或限流取值。**结论：5 路并发下 dashscope 上游零错误**，此前限制 2 路是 nexustokenai 时代的遗留，对 qwen3.8-max 没有必要。
- **r4 的阻断确认由残留引起**：其末次尝试 07:16:59–07:17:21 落在本副本实验 `4e9bb83c712f8b46`（07:09:08–07:19:18）**存活区间之内**，与第七轮 r5 同形态。
- **r3 的阻断不是残留，但真实原因待查实**：其末次尝试 07:32:28 发生在本副本实验 `ee751f61043ab6c5` 销毁（07:30:16）**之后**，判分给的是 `HARNESS_TIMEOUT`。我一度写成"更可能是前两次尝试各自耗尽预算"，**这个推测当时没有证据，已删除**。附带纠正一个我自己取样不足造成的错误数字：我先按 `--tail=4000` 数 s01 的 `Intent partially converged` 得到 0 次，扩大到 `--tail=20000` 后实际是 **4 次**——**行数取窄会直接得出相反结论，这类计数必须先确认取样范围**。**已查实**：r3 的 run `lxr-f4512cc657ef473d` 于 **06:52:50 被接收**、`elapsed_seconds` 2344 秒（约 39 分钟）、交互 15 次、智能体主动提问 5 次、事件 224 条，最终 `platform_status: BLOCKED`。批次里记的"末次 07:32:28→07:32:49（21 秒）"只是最后一次平台重试的外壳，不是该 run 的主体耗时。**所以 r3 是跑满预算后超时（与判分的 `HARNESS_TIMEOUT` 一致），不是被残留挡住**；s01 上确实出现过 4 次 `Intent partially converged`，绕圈现象存在。
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
- **仍需注意**：4 条超时都已正确归属（`ever_active: True`），输在 BladeAI 自身跑满预算，不是平台问题。三轮 PASS 率：第七轮 1/5、第八轮 2/5、第九轮 1/5。
