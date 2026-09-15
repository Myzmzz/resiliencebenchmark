# Stage-1 工具查询模式：MCP 最小改动记录（2026-09-15）

- **分支**：`claude/stage1-toolmode-20260915`
- **工作树**：`resiliencebenchmark-stage1-toolmode`
- **基线**：`codex/stage2-disturbance-e2e` 的 `bafe030`

**目的**：让 Stage-1 的四家被测智能体经本机只读 MCP，查询老环境（tcse-v100）的 K8s 对象和可观测数据。

**兼容性**：改动全部是只读的，或默认关闭的开关；不加新参数时，Stage-2 的行为与改前一致。

## 1. k8s_ro 放开 PDB、HPA、PVC

- **文件**：`mcp_servers/k8s_ro/service.py`，`NAMESPACED_RESOURCES`（第 40–44 行为新增）。
- **改前**：只允许 pods、deployments、statefulsets、daemonsets、replicasets、services、endpoints、jobs、configmaps。
- **改后**：新增三项映射：
  - `poddisruptionbudgets` → `poddisruptionbudgets.policy`
  - `horizontalpodautoscalers` → `horizontalpodautoscalers.autoscaling`
  - `persistentvolumeclaims` → `persistentvolumeclaims`
- **原因**：用户 09-15 决定放开。判断"单副本且无中断预算""有无自动扩缩""有无持久卷"都要读这三类对象。
- **影响**：
  - 仍然只执行 get/list，走原有的通用脱敏逻辑。
  - Secret、ServiceAccount、Role 等禁读清单不变。

## 2. 只读 RBAC 同步

- **文件**：`environment/mcp/rbac.yaml`，ClusterRole `resbench-mcp-k8s-namespaced-read`（第 80 行、第 88–93 行）。
- **改前**：
  - core 组：`pods, pods/log, services, endpoints, events, configmaps`
  - apps 组：四类工作负载
  - batch 组：`jobs`
  - 以上均为 get/list。
- **改后**：
  - core 组增加 `persistentvolumeclaims`。
  - 新增 `policy/poddisruptionbudgets`。
  - 新增 `autoscaling/horizontalpodautoscalers`。
  - 均为 get/list。
- **集群同步**：2026-09-15 已在老环境（context `kubernetes-admin@kubernetes`）用 `kubectl patch --type=json` 写入集群。
- **验证**：以 `system:serviceaccount:resiliencebenchmark-system:resbench-k8s-ro-otel-demo` 的身份执行 `kubectl auth can-i list`：
  - PDB、HPA、PVC、deployments 返回 yes。
  - secrets 返回 no。
- **回滚**：用 JSON patch 删掉这三处，或重新 apply 改前的 `rbac.yaml`。
- **腾讯云集群**：未改动。该集群 09-15 已被他人清空。

## 3. 本机起栈脚本增加三个默认关闭的开关

**文件**：`scripts/local_control_stack.py`

| 位置 | 改动 |
|---|---|
| 第 36–44 行 | 新增常量 `BLADEAI_SSE_PORTS`、`BLADEAI_SSE_ENDPOINTS`、`DEFAULT_JAEGER_ALLOWED_SERVICES` |
| 第 67–81 行 | 新增命令行参数 `--allow-raw-telemetry`、`--jaeger-allowed-services`、`--bladeai-sse` |
| `build_stack_environment` | 新增 `allow_raw_telemetry`，默认 False；新增 `jaeger_allowed_services`，默认值即原白名单 |
| `LocalStack` | 新增 `bladeai_sse`，默认 False。开启后为 k8s_ro、telemetry_ro、source_ro 各起一个 SSE 实例（端口 18181–18183，路径 `/sse`），并把 SSE 地址写进 `stack.env`。新增 `_all_ports()` 统一端口检查 |
| `main` | 把三个参数传入 |

**原因**：
- **原始查询**：用户 09-15 决定放开原始 PromQL、LogQL 和按 trace-id 查询。
- **调用链白名单**：Stage-1 要查 otel-demo 全部服务的调用链，原白名单只有 6 个服务。
- **SSE 实例**：Blade AI 0.7.0 的 MCP 客户端只支持 SSE（`chaos_agent/mcp/client.py` 使用 `sse_client`）。

**默认行为**：三个开关都不开时，与改前完全一致；原有测试未改动，全部通过。

## 测试

- **新增**：
  - `tests/test_local_control_stack.py` 3 项：默认值不变；开启后环境变量正确；SSE 端口只在开启时绑定。
  - `tests/test_k8s_ro_mcp.py` 1 项：三类资源能正确解析，secrets 仍被禁止。
- **运行**：在本工作树根目录执行，解释器用 `resiliencebenchmark-stage2-disturbance-e2e/.venv`（Python 3.13）：
  ```bash
  ../resiliencebenchmark-stage2-disturbance-e2e/.venv/bin/python -m pytest -q tests/test_local_control_stack.py tests/test_k8s_ro_mcp.py tests/test_mcp_rbac.py
  ```
- **结果**：25 项全部通过。

## 本次 Stage-1 试跑的启动方式

端口转发和本机监听要在 Claude 沙箱外运行。

```bash
cd resiliencebenchmark-stage1-toolmode
../resiliencebenchmark-stage2-disturbance-e2e/.venv/bin/python -m scripts.local_control_stack \
  --kubeconfig runs/stage1-toolmode-stack/private/admin-old-env.kubeconfig \
  --source-root /Users/mymz/resbench-stage1-inputs \
  --runtime-root runs/stage1-toolmode-stack \
  --allow-raw-telemetry \
  --jaeger-allowed-services "frontend,frontend-proxy,checkout,cart,payment,shipping,product-catalog,recommendation,ad,product-reviews,currency,quote,email,accounting,fraud-detection,kafka,llm,flagd,load-generator,image-provider,valkey-cart,postgresql" \
  --bladeai-sse
```

- `admin-old-env.kubeconfig` 由 `kubectl config view --minify --flatten --context kubernetes-admin@kubernetes` 从 `~/.kube/coroot-config` 导出。原文件没有设置 current-context。
- 该文件权限为 0600，并且被 git 忽略。

## 4. telemetry_ro 认 OTel 指标的命名空间标签

- **文件**：`mcp_servers/telemetry_ro/service.py`，`NAMESPACE_LABEL_KEYS`（第 39–41 行）。
- **改前**：只认 `namespace`、`kubernetes_namespace`、`exported_namespace`。
- **改后**：增加 `k8s_namespace_name`。
- **原因**：老环境的 Prometheus 通过 OTLP 接收 otel-demo 应用指标，命名空间落在 `k8s_namespace_name` 标签上。
  改前查询 otel-demo 的应用指标会被命名空间过滤全部剔除（实测 `scopedOutCount=8`、`returned=0`），
  智能体只能看到 kube-state-metrics 这类带 `namespace` 标签的指标，拿不到业务延迟和错误率。
- **影响**：
  - 仍只放行白名单里的命名空间（otel-demo）。
  - 这个标签和另外三个一样，调用方不能在结构化查询里自己传。
  - 结构化指标工具注入的仍是 `namespace` 选择器；应用指标要通过原始 PromQL 查询。
- **测试**：新增 `test_prometheus_raw_query_scopes_otel_metrics_by_k8s_namespace_name`。

## 已知情况

- **本机栈自检**：开启 `--allow-raw-telemetry` 后，`qualification.json` 会把 telemetry_ro 判为 failed。
  自检要求工具集合恰好是 10 个结构化工具（`scripts/qualify_mcp_endpoints.py:171`），放开后多出 6 个原始查询工具。
  这是预期内的，不影响使用。
- **压测统计工具**：`telemetry_workload_current` 在这套配置下没有注册（需要另外配置压测统计地址），Stage-1 prompt 里不提"压测实时统计"。
- **直接调用验证**（2026-09-15，老环境）：
  - k8s_ro：读 cart Deployment 正常；PDB 列表为空（老环境没有 PDB）；读 secrets 返回 `resource_forbidden`。
  - telemetry_ro：Jaeger 服务列表返回 18 个服务；按 `namespace` 查 `kube_pod_info` 正常；Loki 标签正常（namespace 由服务端自动注入，调用方不能传）。
  - source_ro：能列出 `otel-demo-2.2.0`，并能全文搜索。
- **测试**：`tests/test_telemetry_ro_mcp.py`、`tests/test_local_control_stack.py`、`tests/test_k8s_ro_mcp.py`、`tests/test_mcp_rbac.py` 共 70 项全部通过。
