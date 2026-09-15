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

## 六、已知限制（本轮刻意不做）

1. **镜像不是用 `build_stage2_image.py` 构建的**：为赶时间，控制器镜像是在 Harbor 上 fleet 的 `stage2-d0-77a11bd@sha256:f3b1ffc1…` 之上用 `crane append` 追加两层——本分支代码层（`/app`，按 runtime-overlay 的同一批路径）和 BladeAI 0.7.0 PyInstaller 包（`/opt/bladeai-070`，取自老集群 bbverify Pod，sha256 分片校验一致）。下层残留的已删模块文件不会被导入。`Dockerfile.agent` 的新写法没有实际构建过。
2. 单系统部署（`deploy/stage2/stage2.yaml`）没有加 `bladeai-server`，BladeAI 只在副本 slot 上可跑。
3. nexustokenai 的 chat completions 回答带 U+200B 前缀（09-05 实测），未在网关层处理。
4. 老集群上 slot 关闭了 agent-runtime 的 AppArmor 注解；BladeAI 黑盒不经过 agent-runtime，其他三家在这些 slot 上少一层防护。
5. 放开技能脚本后，BladeAI 目标守卫不再把技能脚本判为 banned，这是 BladeAI 的默认行为。
6. BladeAI 0.7.0 的 `GET /api/v1/config` 会明文返回 `llm_api_key`（实测，值此处不记）。server 只绑 pod 回环 8399；同 pod 的 agent-runtime 按回环端口白名单放行出站，8399 不在白名单内（见 5.3），同 pod 的其他智能体访问不到这个接口。
7. **黑盒认定分支不检查 `failure_reasons`**：记录里即使有 `native_boundary_violation_attempt` 或 `runner_error` 也会被接受。本轮 s02/s03/s05 的记录已人工核对，失败原因只有 6 个 MCP 通道类；这个开关不适合在没人核对记录时使用。后续应改为只容忍 MCP 通道类原因。
8. **黑盒认定发布的 capability 字段与跳过的检查不一致**：仍写 `feedback_channels=in_band_mcp`、`supports_mid_turn_feedback=True`，而证明这两项的通道检查恰好被跳过了。对 L0×C0 没有影响；做 D 类扰动（带内通知）之前需要改正。
9. **slot 刚启动时不能马上跑**：控制器的网关探测会依次探测配置里全部 7 个模型别名，探完之前所有智能体×模型都判为不可运行（首次观察 07:43 起）。
