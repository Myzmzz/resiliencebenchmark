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

## 四、测试

- 门禁（提交 90fdd30 前）：`test_fleet_service`、`test_stage2_image_build`、`test_stage2_agent_runtime_assets`、`test_bladeai_agent_image_contract`、`test_render_litellm_gateway`、`test_stage2_target_binding`、`test_stage2_simulated_user` 全部通过。
- 全量（沙箱外）：见第五节补记。

## 五、部署与验证（老集群，用户 09-15 指定）

（部署后补记：镜像 digest、集群对象、slot 状态、资格认定、批次结果）

## 六、已知限制（本轮刻意不做）

1. **镜像不是用 `build_stage2_image.py` 构建的**：为赶时间，控制器镜像是在 Harbor 上 fleet 的 `stage2-d0-77a11bd@sha256:f3b1ffc1…` 之上用 `crane append` 追加两层——本分支代码层（`/app`，按 runtime-overlay 的同一批路径）和 BladeAI 0.7.0 PyInstaller 包（`/opt/bladeai-070`，取自老集群 bbverify Pod，sha256 分片校验一致）。下层残留的已删模块文件不会被导入。`Dockerfile.agent` 的新写法没有实际构建过。
2. 单系统部署（`deploy/stage2/stage2.yaml`）没有加 `bladeai-server`，BladeAI 只在副本 slot 上可跑。
3. nexustokenai 的 chat completions 回答带 U+200B 前缀（09-05 实测），未在网关层处理。
4. 老集群上 slot 关闭了 agent-runtime 的 AppArmor 注解；BladeAI 黑盒不经过 agent-runtime，其他三家在这些 slot 上少一层防护。
5. 放开技能脚本后，BladeAI 目标守卫不再把技能脚本判为 banned，这是 BladeAI 的默认行为。
