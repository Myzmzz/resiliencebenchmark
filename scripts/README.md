# Scripts

## CodeGraph语义韧性扫描

`run_semantic_scan.py`是12类可故障验证模板的本地扫描入口。它验证代码库路径，由Controller创建/同步CodeGraph索引，解析Kubernetes/Helm期望配置，再调用LangChain/LangGraph子Agent与Verifier。它不读取集群密钥，不执行故障。

```bash
uv run python scripts/run_semantic_scan.py --validate-only
uv run python scripts/run_semantic_scan.py --run-id semantic-local-v1
uv run python scripts/run_semantic_scan.py --run-id semantic-local-v1 --resume
```

`generate_episodes.py`将每个已确认匹配编译为一个内部Episode和一个脱敏公开题目；`verify_episode_generation_experiment.py`验证正负对照、600秒窗口、ChaosBlade/清理命令、扰动、Oracle和零泄漏。

## Web 控制面的两个 Worker

`run_control_worker.py` 只处理 `CREATED → QUALIFYING`，负责扫描、模板匹配、题目生成和锁题；它不持有集群写权限。

```bash
uv run python scripts/run_control_worker.py --once \
  --kubeconfig /absolute/path/to/read-only-scan.kubeconfig
```

`run_execution_worker.py` 只处理已批准进入 `BASELINING` 的 Run。它在检查全部运行时配置之后才领取任务，并要求安装 runtime extra：

```bash
uv sync --extra test --extra runtime
uv run --extra runtime python scripts/run_execution_worker.py --once
```

执行 Worker 的 kubeconfig、模型网关、MCP Token、私有 runtime root、baseline ledger 和工作负载镜像均来自 Controller 主机运行时环境；这些值不能由前端或 RunSpec 传入。每个关卡/重试都执行复位、600 秒基线、最后 300 秒评价、UID 重绑和一次性 capability，结束后由 Controller 兜底销毁主故障并验证业务恢复。

## 本机只读 MCP 栈

远端 MCP host 不可达时，可使用显式测试 kubeconfig 启动本机认证栈并完成端点资格检查。该命令会创建三个只读 port-forward、四个 loopback MCP 和私有临时 Token；Chaos 写入始终关闭：

```bash
uv run python scripts/local_control_stack.py \
  --kubeconfig /absolute/path/to/test.kubeconfig \
  --qualify-and-exit
```

运行状态写入被 Git 忽略的 `runs/local-control-stack/`。`stack.env` 为 mode 0600，不能提交或复制到报告。

本机栈会通过 Kubernetes TokenRequest 生成三份 6 小时有效、mode 0600 的独立 kubeconfig，并从 `kube-public/cluster-info` 固定 CA；不会把原始 admin kubeconfig 交给 MCP 或 Worker。正式健康基线预检另行显式执行：

```bash
uv run python scripts/qualify_formal_baseline.py \
  --kubeconfig runs/local-control-stack/kubeconfigs/controller.kubeconfig \
  --image '<repository>:<tag>@sha256:<digest>' \
  --run-id formal-baseline-preflight \
  --execute
```

本目录用于提供环境初始化、单 Episode 运行、批量评测、恢复和结果导出入口。

脚本应保持薄封装：负责参数校验和组装模块，不在脚本中隐藏 Controller 或 Evaluator 的核心逻辑。

## Preparation validator

`benchmark_prepare.py` 是当前环境准备阶段的仓库级校验入口。它只做静态校验和只读集群资格检查，不执行真实故障注入，也不执行 `kubectl apply/delete/patch/exec`。

常用命令：

```bash
python3 scripts/benchmark_prepare.py validate-repo --repo .
python3 scripts/benchmark_prepare.py dry-run --repo .
python3 scripts/benchmark_prepare.py qualify-cluster \
  --kubeconfig /path/to/kubeconfig \
  --namespace train-ticket \
  --namespace sock-shop \
  --namespace otel-demo \
  --observability-namespace observability \
  --output artifacts/qualification/cluster.json
```

校验范围：

- YAML/JSON 必须可解析；已识别的 `ApplicationTarget`、`EpisodeSpec`、`HarnessSpec`、`McpEndpointSet` 会检查必需字段。
- Agent 可见目录不能出现 ground truth 配置键或 ground truth 文件路径。
- 仓库文件不能包含疑似真实密钥、公网 IPv4 或用户绝对路径。
- 集群资格检查必须显式传入 `--kubeconfig`，并只读取节点、目标 namespace、Deployment、load generator、observability service 和 ChaosBlade CR 数量/状态。

## Additional preparation tools

- `probe_models.py`：使用运行时网关变量探测七个模型的协议、流式、结构化输出和工具调用能力；`--dry-run` 不联网。
- `materialize_sources.py`：按 `source-locks.yaml` 的 commit 与归档 SHA-256 生成只读源码快照；不会覆盖非空目录。
- `render_sock_shop.py`：从固定上游 SHA 渲染 Sock Shop，只允许受控对象类型并强制镜像 digest。
- `mirror_images.py`：默认 dry-run；显式 `--execute` 后使用临时 Docker 配置和 Harbor Robot Account 复制固定镜像，不在输出中保留凭据。仅当 Harbor 明确是 HTTP registry 时设置 `HARBOR_INSECURE=true`；其他值或默认均保持 TLS 校验。
- `train_ticket_workload.py`：渲染和管理 controller-owned Train-Ticket Job；真实 start 要求 Secret 引用、host allowlist、PVC 和 digest-pinned workload image。
- `deploy_deepseek_harness.py`：默认 dry-run；真实安装仅允许 strict known_hosts 与公钥 BatchMode SSH，不支持 password 或 `sshpass`。
- `inventory_runtime_images.py`：显式 kubeconfig 的只读 Pod 镜像盘点；输出脱敏 repository tail、运行时 digest、readiness 和逐 namespace 资格状态，用于建立 source commit 与部署镜像之间的证据链。
- `build_train_ticket_workload.py`：默认 dry-run 的固定 buildx 入口，构建 controller-owned Python workload，并在 push 后返回可固定的 manifest digest。
- `deploy_mcp_host.py`：把当前已提交 HEAD 作为受管 release 传到目标主机，物化锁定源码并安装 4 个 Streamable HTTP 与 3 个 BladeAI SSE systemd unit；不创建或覆盖运行时 secret。
- `qualify_mcp_endpoints.py`：验证四个 loopback MCP 的 bearer 拒绝、initialize、精确 tool set、schema 边界与 destructive/read-only annotation。
- `qualify_remote_preparation.py`：主机恢复后以严格 SSH 公钥和显式 kubeconfig 验证 Node Lease、固定提交、源码 manifest、DeepSeek 运行身份、7 个 MCP unit/listener、Chaos 禁用和内存余量。
- `deploy_application.py`：三个被测系统统一的 dry-run-first `apply`、`activate`、`standby`、`delete` 入口；运行时渲染 Harbor/NFS/密码占位符，保护 live PVC，并维护单活动系统 marker。
- `reset_episode.py`：不删除 namespace 的轻量复位；只检查 ledger-owned ChaosBlade CR 消失和固定种子 workload SLO 回到基线带。
- `run_harness_trial.py`：Codex、Claude Code 与 DeepSeek Harness 的 dry-run-first 单 trial runner，负责隔离 Home、提示词组装、环境白名单、输出脱敏和 run-trace/agent-result schema 校验。

仓库自有 MCP 使用锁定的 Python 环境。先运行 `make sync`，再用 `make test-mcp` 验证 tool schema、scope、脱敏和 ChaosBlade 门禁。
