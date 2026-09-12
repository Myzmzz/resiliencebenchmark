# 新环境操作手册（2026-09-12）

面向在新环境（腾讯云两节点集群）上跑 Stage-2 评测、部署平台镜像的人。下面的事实都是 2026-09-12 在集群上实测的。

## 1. 集群与接入

| 节点 | 角色 | 内网 IP | 外网 IP |
|---|---|---|---|
| `vm-0-13-ubuntu` | 控制面 | 172.21.0.13 | 62.234.93.223 |
| `vm-0-10-ubuntu` | 工作节点 | 172.21.0.10 | 152.136.19.189 |

- k8s v1.31.14，Ubuntu 26.04，Docker 29.6，每台 32 vCPU / 123 GiB。
- **被测系统、平台、观测栈全部用 nodeSelector 固定在 `vm-0-10-ubuntu`**，因为 AppArmor 配置 `resbench-agent-runtime` 只装在那台。
- kubeconfig 由环境所有者提供，API server 是 `https://62.234.93.223:6443`。**不要把 kubeconfig 内容贴进任何对话，也不要提交进仓库。**
- **这是共享集群**：`sregym` 命名空间和 `default/mysql` 属于别的项目，不要碰。
- **另有一套老集群**（kubeconfig 名字里带 `coroot-config`），上面有同名的 Deployment `resbench-stage2-integration`。**两套千万别混。**

## 2. 命名空间与组件

- **`resiliencebenchmark-system`（平台）**
  - Deployment `resbench-stage2-integration`：Recreate 策略，1 副本。
  - 容器：`litellm`（模型网关，只监听 Pod 内 `127.0.0.1:4000`）、`stage2`（控制器，Service 8080）、`agent-runtime`（智能体执行环境）；初始化容器 `agent-workspace-permissions`。
  - 存储：PVC `resbench-stage2-data`（openebs 本地卷），挂 `/var/lib/resbench-stage2`。
  - Secret：`resbench-stage2-runtime`（挂 `/etc/resbench-stage2`）、`resbench-stage2-gateway-client`、`litellm-upstream`；ConfigMap `litellm-config`；服务账号 `resbench-stage2-controller`。
- **`otel-demo`（被测系统）**：OpenTelemetry Demo 2.2.0，Helm release `otel-demo`（chart 0.40.5），23 个 Deployment，评测目标一般是 `cart`（1 副本）。内存限额是环境所有者批准过的（accounting 192Mi、ad 448Mi、fraud-detection 512Mi），**不要擅自改**。`load-generator` 提供业务流量。
- **`coroot`**：Coroot（operator 0.8.2），服务 `coroot-coroot.coroot.svc:8080`，项目 `p1nar0hw`，开了匿名只读。
- **`chaos-mesh`**：Chaos Mesh 2.8.0，D8 的替代注入工具。
- **`default`**：ChaosBlade 1.8.0（operator + tool DaemonSet）。CPU、内存故障依赖 ConfigMap `chaosblade-cgroupns-wrapper` 的包装，**不要动它**。
- **`observability`**：prometheus、loki、jaeger、otel-collector、kube-state-metrics、node-exporter、promtail。

## 3. 平台 Pod 内的路径

| 用途 | 路径 |
|---|---|
| 运行产物 | `/var/lib/resbench-stage2/integration/artifacts/campaign-<id>/` |
| 私有文件 | `/var/lib/resbench-stage2/integration/private/`（能力文件、能力损失资格文件、服务 kubeconfig） |
| 资格记录 | `/var/lib/resbench-stage2/integration/qualification/` |
| 运行锁 | `/run/resbench/stage2-active-run.lock` |
| 运行时环境文件 | `/etc/resbench-stage2/otel-demo.env` |

私有文件必须是 0600，组和其他用户都不能读写，否则平台会拒绝运行。手动做预检时，先把环境文件复制成 0600 的临时文件再用。

**导出某次运行的完整记录**（做判定回放对照时用）：

```bash
POD=$(kubectl -n resiliencebenchmark-system get pods -o name | grep resbench-stage2-integration- | head -1)
ART=/var/lib/resbench-stage2/integration/artifacts
# 按"用例 × 智能体"找 campaign（trial 目录名形如 campaign-<id>-<harness>-<case>-1）
kubectl -n resiliencebenchmark-system exec "$POD" -c stage2 -- \
  sh -c "cd $ART && ls -dt campaign-*/campaign-*-claude-code-d3-1 | head -3"
# 导出（不带 stdout/stderr，体积小很多）
kubectl -n resiliencebenchmark-system exec "$POD" -c stage2 -- \
  tar czf - -C "$ART" --exclude='stdout*' --exclude='stderr*' campaign-<id> > campaign-<id>.tgz
```

导出后用仓库里的重判工具做对照：`uv run python -m stage2_service.rescore --campaign-dir <解压后的目录> --out <输出目录> --code-revision <提交号>`。

## 4. 两项仓库清单里没有、重新部署必须保留的配置

1. **`securityContext.fsGroupChangePolicy: OnRootMismatch`**（fsGroup 10001）。新集群的 openebs 卷支持 fsGroup，kubelet 每次挂载都会递归改权限；少了这一项，Pod 一重启私有文件就变成组可读，新运行会卡在 QUEUED 或直接报 `KubernetesIdentityError`。
2. **`stage2` 容器的两个环境变量**：`RESBENCH_COROOT_PROJECT_ID=p1nar0hw`、`RESBENCH_COROOT_ALLOW_ANONYMOUS_READ=true`。

**因此：永远不要 `kubectl apply` 仓库渲染出来的清单。** 换镜像只走 `tools/dx-round/deploy_boundary.sh`，它只替换 4 处镜像引用（stage2、agent-runtime、初始化容器、模板上的 source-head 标签），其余原样保留。

## 5. 访问平台接口

```bash
kubectl -n resiliencebenchmark-system port-forward service/resbench-stage2-integration 28080:8080
```

平台地址 `http://127.0.0.1:28080`。常用接口：

| 接口 | 用途 |
|---|---|
| `GET /api/v1/stage2/options` | 三家智能体能不能跑、各模型探测状态 |
| `GET /api/v1/stage2/lx/runs` | 有没有正在跑的评测（`terminal=false` 就是还在跑） |
| `POST /api/v1/stage2/lx/runs` | 提交一次评测 |

**隧道要放在常驻会话里。** 脚本在后台起的 port-forward 会随命令会话退出，导致预检或批跑中途连不上。

## 6. 镜像与模型

- **Harbor**：`1.94.151.57:85`，HTTP 明文。镜像 `observe/resbench-stage2`，tag 形如 `stage2-d0-<sha>`（控制器）和 `stage2-agent-<sha>`（Agent）。**同名 tag 绝不覆盖**，要重建就用新的提交号。
- **当前线上**：`60309d3`。元数据在 `tools/dx-round/images/build-60309d3-image.json`，回滚用同目录的 `build-1807322-image.json`。
- **构建**：`tools/dx-round/build_boundary.sh`。它要求工作树干净，并先查 Harbor 有没有同名 tag。除仓库代码外还需要：Docker、buildx 构建器 `ischaos-builder`、bladeai 上游仓库（环境变量 `BLADEAI_REPO`）、三个离线基础镜像目录（环境变量 `BASES`）。后两样各约 200 MB，不在仓库里，需要环境所有者提供。
- **模型路由**：被测模型 `qwen3.8-max` 走阿里云百炼；模拟用户用 `deepseek-v4-pro-0813` 走 DeepSeek 官方。百炼账户欠费会让整批评测作废（2026-09-11 发生过），开跑前先确认 `/api/v1/stage2/options` 里它是 `runnable`。
- **网关探测**：结果过期后要有人查 `/options` 或提交评测才会重新探测，一次 2–5 分钟；探测期间提交返回 503，`run_dx.py` 会自动重试。

## 7. D7/D8 的资格文件

D7/D8 触发前，平台要读一份私有资格文件，里面是替代观测工具的历史样本和替代注入工具的试注入记录。**有效期 24 小时**，过期要在 `stage2` 容器里重新生成：

```bash
python -m stage2_service.capability_loss.qualification_probe \
  --d7 --d8 --namespace otel-demo --target cart --ttl-hours 24
```

先加 `--dry-run` 看它打算做什么。它和评测抢同一把运行锁，只能在没有评测在跑时执行。D7 样本绑定当前 `cart` Pod 的 UID，所以每次跑 D7 之前都要刷新（`tools/dx-round/round3.sh` 的钩子已经做了）。

## 8. 已知的坑

- **shell 是 zsh 时**：未加引号的变量不会按空格拆开（用函数包装 kubectl）；不要用 `path` 当变量名，它和 `PATH` 绑定；带方括号的参数要加引号。
- **`kubectl exec` 配 heredoc 要加 `-i`**；平台 Pod 里没有 `ps`。
- **Python**：仓库代码要 3.12（用到 3.11 之后的 API），用 `uv run` 最省事。
- **Lx 接口下 D6 永远是 D6-A**（请求里没有变体字段）；D7/D8 必须带变体，批跑时写成 `D7-A` 这种形式。
- **复位保护**：平台走到全量重装时会先做预检，预检不过就不卸载，记为"复位失败"并停下。遇到这种情况不要手动 helm 卸载重装，先报告环境所有者。
- **连续试验复用同一个 cart Pod**（D2 除外，它会换 Pod），前一次的负载峰值可能落进后一次的观测窗口。
