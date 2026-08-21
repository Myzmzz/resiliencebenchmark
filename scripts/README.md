# Scripts

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
- `mirror_images.py`：默认 dry-run；显式 `--execute` 后使用临时 Docker 配置和 Harbor Robot Account 复制固定镜像，不在输出中保留凭据。
- `train_ticket_workload.py`：渲染和管理 controller-owned Train-Ticket Job；真实 start 要求 Secret 引用、host allowlist、PVC 和 digest-pinned workload image。
- `deploy_deepseek_harness.py`：默认 dry-run；真实安装仅允许 strict known_hosts 与公钥 BatchMode SSH，不支持 password 或 `sshpass`。
- `inventory_runtime_images.py`：显式 kubeconfig 的只读 Pod 镜像盘点；输出脱敏 repository tail、运行时 digest、readiness 和逐 namespace 资格状态，用于建立 source commit 与部署镜像之间的证据链。
- `build_train_ticket_workload.py`：默认 dry-run 的固定 buildx 入口，构建 controller-owned Python workload，并在 push 后返回可固定的 manifest digest。
- `deploy_mcp_host.py`：把当前已提交 HEAD 作为受管 release 传到目标主机，物化锁定源码并安装 4 个 Streamable HTTP 与 3 个 BladeAI SSE systemd unit；不创建或覆盖运行时 secret。
- `qualify_mcp_endpoints.py`：验证四个 loopback MCP 的 bearer 拒绝、initialize、精确 tool set、schema 边界与 destructive/read-only annotation。
- `qualify_remote_preparation.py`：主机恢复后以严格 SSH 公钥和显式 kubeconfig 验证 Node Lease、固定提交、源码 manifest、DeepSeek 运行身份、7 个 MCP unit/listener、Chaos 禁用和内存余量。
- `run_harness_trial.py`：Codex、Claude Code 与 DeepSeek Harness 的 dry-run-first 单 trial runner，负责隔离 Home、提示词组装、环境白名单、输出脱敏和 run-trace/agent-result schema 校验。

仓库自有 MCP 使用锁定的 Python 环境。先运行 `make sync`，再用 `make test-mcp` 验证 tool schema、scope、脱敏和 ChaosBlade 门禁。
