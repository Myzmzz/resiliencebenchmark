# 任务：BladeAI 0.7.0 试装评估（2026-09-11）

## 目标

用官方 BladeAI 0.7.0 源码构建一个**试验版** Agent 镜像，跑平台里和 BladeAI 相关的测试，给出一份"升级到 0.7.0，平台哪里会坏、要改什么、工作量多大"的确切清单。

只做评估和让构建跑通所需的最小改动，**不做正式升级、不合并、不部署到现有评测环境**（集群上的验证见"第二阶段"，需要用户点头才做）。

## 一、先知道的事实（已核实）

### 1. 源码来源：不要按 git 标签取

- BladeAI 源码仓库在本机：`/Users/mymz/work/国家重点研发/韧性测试工具 benchmark/bladeai-source`（origin 是 github.com/chaosblade-io/chaosblade，BladeAI 在 `blade-ai/` 子目录）。
- **官方仓库里 `blade-ai-v0.6.0` 到 `blade-ai-v0.7.0` 的所有标签都是轻量标签，全部指向 master 上 2026-07-28 的 `d8c5473`**，那里的包版本还是 `0.3.0`；master 之后没再改过 blade-ai。
- **真正的发布源码在官方 `feature/blade-ai` 分支**，按 `blade-ai/pyproject.toml` 的版本号对应：
  - 0.6.2 → `8d8c3cf`（2026-08-10）
  - 0.6.3 → `d2e3791`（2026-08-12）
  - 0.7.0 → `7a81c82`（2026-08-25，版本号提交）；分支头 `98a9ddb`（同日，"close conflict-check blind spots"，发布说明里列了这一项，所以 **试装用 `98a9ddb`**）
- 这些引用已拉到本机的独立命名空间（没动本机标签）：`refs/upstream-heads/feature-blade-ai`、`refs/upstream-heads/master`、`refs/upstream-tags/blade-ai-v0.6.2|v0.6.3|v0.7.0`。
- **平台现在的 Agent 镜像是用 `d8c5473`（0.3.0）构建的，却标着 `blade-ai-v0.6.2`**：`scripts/build_stage2_image.py:27-33` 的 `BLADEAI_RELEASE_TAG` / `BLADEAI_RELEASE_COMMIT` / `BLADEAI_PACKAGE_VERSION`，以及 `deploy/stage2/Dockerfile.agent:19-22` 的同名构建参数。报告里请写清这一点。
- 发布说明：<https://github.com/chaosblade-io/chaosblade/releases/tag/blade-ai-v0.7.0>、<https://github.com/chaosblade-io/chaosblade/releases/tag/blade-ai-v0.6.3>。要点：0.6.3 抽出了模型连接层、链路观测改用 `BaseCallbackHandler`、意图约定里 `duration_seconds` 改为必填；0.7.0 把故障执行拆成 `chaosblade` / `host_shell` / `k8s_native` 三个 provider，实验编号 `blade_uid` 改名 `experiment_uid`，前端改成 npm workspaces（`core` / `tui` / `web`），任务数据库不兼容旧版（平台不读这个库，不受影响）。

### 2. 规模

`d8c5473` → `98a9ddb`：`blade-ai/` 下 47 个提交、1067 个文件、+220,858 / −63,537 行。依赖锁文件没变：langchain-core 1.3.0、langgraph 1.1.8、mcp 1.27.1，Python ≥ 3.11，`mcp>=1.0,<2.0`。

### 3. 静态对比已经看出的断点（请逐项证实或推翻）

平台对接代码在 `stage2_service/bladeai_*.py`（11 个文件，共 6482 行）。

| 平台位置 | 0.7.0 的变化 | 预计 |
|---|---|---|
| `bladeai_mcp_guard.py:74-91`：import `chaos_agent.agent.nodes.phase1_screener`、`tool_screener`，并改写 `target_guard` / `phase1_screener` 的 `infer_effective_target` | `nodes/phase1_screener.py`、`nodes/tool_screener.py` 在原路径被删（筛选逻辑挪到 `agent/nodes/planning/`）；`target_guard/classifier.py` 从约 1150 行重写成约 240 行 | 必坏，护栏挂钩要重写 |
| `bladeai_shim.py` 解析 BladeAI 调用 `blade create k8s …` / `blade destroy …` 的命令行参数形状，并按 BladeAI 的 UID 提取器能识别的格式回显输出（`bladeai_shim.py:192`）；`bladeai_task.py:270-296`、`bladeai_worker.py:1286` 读取 `save_fault_plan` 规范块里的 `blade_target` / `blade_action` / `blade_scope` 字段（注：垫片里 44 处 `blade_uid` 是它自己的局部变量，不是对 BladeAI 的耦合） | 0.7.0 把 `blade` 调用搬进 `agent/providers/chaosblade/{cli,python_provider}.py`，`blade_target` 等字段在源码里几乎消失（149→3、110→3、36→2），改为 provider 结构和 `experiment_uid`；`blade_create` / `blade_destroy` / `blade_status` 工具名还在 | 要核实 0.7.0 的 chaosblade provider 是否仍以子进程方式调 `BLADE_AI_BLADE_PATH` 指向的二进制、argv 和输出格式变了多少；规范块字段是平台提示词自己定义的，受影响的主要是从 BladeAI 方案里回读这些字段的代码 |
| `bladeai_worker.py:516、522、834-835`：`L4AgentError` / `L4TaskResult` / `L4TestTask`、`make_llm`、`L4ResilienceAgent` | 类和函数都还在，但 `l4/agent.py` −645/+82、`agent/factory.py` −300/+683、`l4/schemas.py` +178 | 启动方式和模型网关接入要重新核实 |
| `bladeai_events.py:8-9`：`BaseCallbackHandler`、`merge_configs` | 0.6.3 改了模型调用的链路上报方式 | 事件采集要核实 |
| `bladeai_duration.py` | 0.6.3 起 `duration_seconds` 必填 | 时长兜底逻辑要核实 |
| `chaos_agent.mcp.adapter._safe_tool_name`（私有函数） | 没变 | 不受影响 |
| `Dockerfile.agent:4-14`：在 `/opt/blade-ai/tui` 里单独 `npm ci` 和 `npm run build` | 0.7.0 在 `blade-ai/` 根目录新增 `package.json`、`package-lock.json`，并有 `core/`、`web/`；`tui` 依赖 `@blade-ai/core` | 界面构建步骤要改成从工作区根目录构建 |
| `scripts/build_stage2_image.py:27-33、121-150`：按标签 `rev-parse` / `git archive`，校验标签指向写死的提交、包版本等于 `0.3.0`，要求存在 `.github/workflows/release-blade-ai.yml` | 标签不可用；包版本变为 `0.7.0` | 构建脚本要能按提交取源码 |

### 4. 安全：必须回答的问题

0.7.0 新增的 `host_shell`、`k8s_native` provider（`blade-ai/src/chaos_agent/agent/providers/`）能不能**绕过平台的注入通道**（BladeAI → 平台 shim → `chaos_control`），直接用主机 shell 或 kubectl 动集群？平台的 Agent 容器有出站限制（`RESBENCH_AGENT_EXEC_EGRESS_POLICY=required`），BladeAI 的 Kubernetes 访问走 `mcp_servers/bladeai_k8s_proxy`。请查清 provider 的选择与开关、平台能否关掉这两个 provider，以及就算不关，代理会不会挡住写操作。**这一项不清楚就不能上线 0.7.0。**

## 二、工作约束

- **不要动共享工作树** `resiliencebenchmark-stage2-d0-integration`：另有会话在改评分代码（`evaluator.py`、`finalization.py`、`node_evaluation.py`）。在当前 `codex/stage2-d0-integration` 的 HEAD 上新开工作树和分支，例如 `git worktree add -b codex/bladeai-070-trial <新目录> HEAD`，所有改动都提交在试验分支上；不 push、不合并，不用 `git stash`。
- **不要在 `bladeai-source` 里新建或移动标签**，也不要改全局 git / Docker 配置。取源码用提交号，例如 `git -C <bladeai-source> archive --format=tar 98a9ddb blade-ai`。
- 网络：本机 git 全局代理指向已经不存在的 `127.0.0.1:7890`，Clash 实际监听 `7897`。在 Claude 沙箱外执行时加 `-c http.proxy=http://127.0.0.1:7897`，沙箱内加 `-c http.proxy="$HTTPS_PROXY"`。
- 镜像标签必须是新的，例如 `stage2-agent-bladeai070-trial-<试验分支提交号>`，**不能覆盖 Harbor（`1.94.151.57:85/observe/resbench-stage2`）上已有的任何标签**。
- 本机 `python3` 是 3.9，仓库代码需要 `datetime.UTC`：用项目自带的 `.venv/bin/python`，或在镜像里跑。

## 三、步骤

### 第一阶段（只在本机，不动任何集群）

1. **固定源码**：记录 `98a9ddb` 的完整提交号、`blade-ai/pyproject.toml` 版本号（应为 0.7.0）、发布说明链接。
2. **逐项核实第一节第 3 点的断点表**：每一行给出结论（坏 / 不坏）、0.7.0 里对应的新位置或新接口、建议改法。
3. **在 0.7.0 上跑平台测试**：准备一个装了 0.7.0 `blade-ai`（在 `blade-ai/` 下 `uv sync`）和平台依赖的环境，或者等第 4 步构建出镜像后在镜像里跑。
   - 重点跑：`tests/` 下文件名含 `blade` 的 22 个文件（约 200 个用例），尤其是会真正导入 `chaos_agent` 的 `test_bladeai_mcp_guard.py`、`test_bladeai_worker_sdk_lifecycle.py`、`bladeai_worker_sdk_lifecycle_probe.py`、`test_bladeai_agent_image_contract.py`。
   - 另有 8 个测试文件引用了 bladeai 模块：`test_agent_exec_environment_contract.py`、`test_agent_exec_harness_session.py`、`test_capability_loss_factory.py`、`test_capability_loss_orchestrator.py`、`test_capability_loss_runtime.py`、`test_capability_qualification.py`、`test_d0_campaign.py`、`test_manual_stage2_requests.py`。
   - 失败按原因归类，不要一条条贴日志。
4. **构建试验镜像**，只做让构建跑通的最小改动：
   - 构建脚本能按提交取 BladeAI 源码（新增参数或常量），版本校验改为 0.7.0，同时检查 `98a9ddb` 下是否还有 `.github/workflows/release-blade-ai.yml`；另外留意"源码包里不能含原生 ChaosBlade 二进制"那项检查——0.7.0 的发布包捆绑了 ChaosBlade tool v1.9.0-alpha，要确认源码目录里没带。
   - `Dockerfile.agent` 的界面构建改成 npm workspaces：在工作区根目录 `npm ci`，先构建 `core` 再构建 `tui`，保证 `tui/dist/cli.js` 存在；`web` 不需要。
   - 已知构建坑（2026-09-10 实测的绕法）：
     - Docker Desktop 代理指向 7890 而 Clash 是 7897，构建器拉基础镜像会 EOF。先用 `HTTPS_PROXY=http://127.0.0.1:7897 crane pull --format oci --platform linux/amd64` 把 `node:24.13.0-bookworm-slim`、`node:22.21.1-bookworm-slim`、`python:3.12.13-slim-bookworm` 拉到本地 OCI 目录，再给 `scripts/build_stage2_image.py` 传三次 `--agent-base-context "<镜像>=oci-layout://<目录>@sha256:<digest>"`，加上 `--agent-build-proxy http://host.docker.internal:7897 --builder ischaos-builder`。
     - 构建脚本把**当前目录**当构建上下文，所以要在试验分支的干净工作树里跑。
     - 前端依赖：在试验工作树的 `frontend/` 里执行 `pnpm install --frozen-lockfile --offline`，否则会报 `tsc: command not found`。
     - 整个构建约 15 分钟，要放后台跑，日志末尾追加退出码。
5. **镜像内冒烟**：在构建出的镜像里确认 `/opt/bladeai-venv/bin/python -c "import chaos_agent; from chaos_agent.l4.agent import L4ResilienceAgent; from chaos_agent.agent.factory import make_llm"` 能通过，`tui/dist/cli.js` 存在；能跑的话再跑一次 `tests/bladeai_worker_sdk_lifecycle_probe.py`。
6. **安全检查**：回答第一节第 4 点。
7. **（可选，给用户一个便宜的替代选项）** 用同样的方法静态比较 `d8c5473` → 真正的 0.6.2（`8d8c3cf`）和 0.6.3（`d2e3791`），看断点表里哪些已经在这两个版本出现，估算"只切到真正的 0.6.2/0.6.3"的工作量。

### 第二阶段（需要用户确认后才做）

在新环境（腾讯云两节点，kubeconfig `~/.kube/resbench-new-config`，服务 `resiliencebenchmark-system/resbench-stage2-integration`）上，把 Agent 容器和初始化容器临时换成试验镜像，跑一次 BladeAI 全链资格验证和一次 L0×C0 的 bladeai 运行（步骤见 `docs/deploy/stage2-three-agent-base-qualification-20260906.md`、`docs/deploy/stage2-base-channel-qualification-20260906.md` 和 `scripts/build_bladeai_qualification.py`）。做完换回原镜像：`stage2-agent-596b10f@sha256:d2736590ccbe5cce58367226e0d2ec512f919828df36df7037179a21e14a4584`。控制器镜像不用换，因为 BladeAI 的适配文件是随 Agent 镜像生效的。

## 四、交付物

1. 报告：试验分支上的 `docs/status/bladeai-070-trial-report-20260911.md`，内容：
   - 精确的版本来源（提交号），并说明现有镜像实际是 0.3.0；
   - 构建结果（镜像 digest，或失败在哪一步）；
   - 测试结果（通过数 / 失败数，失败按原因归类）；
   - 断点清单：每一处写"平台文件:行 → 0.7.0 的新位置或新接口 → 建议改法 → 工作量（小/中/大）"；
   - 安全结论（两个新 provider）；
   - 总工作量估计和建议：是否值得升 0.7.0，还是先切到真正的 0.6.2/0.6.3。
2. 试验分支上让构建跑通的最小改动提交（构建脚本、Dockerfile），提交说明写清原因；不合并，由用户决定后续。
