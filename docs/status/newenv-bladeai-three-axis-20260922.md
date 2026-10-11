# 新环境 BladeAI 三轴评测：部署与改动记录（2026-09-22 起）

分支：`claude/newenv-bladeai-20260922`（基于 fork 会话的 `claude/stage2-d1-round-20260922` @ 69d73b7）
环境：腾讯云 2 节点集群（kubeconfig `~/.kube/resbench-new-config`），**与其他租户共享**
（train-ticket 66 Pod、observe 16、sock-shop 15、sregym 12，另有他人安装的 Chaos Mesh）。

## 一、用户决定（2026-09-22）

| 项 | 决定 |
|---|---|
| 被测 | BladeAI 0.7.2（带 turn_hold_fault_window） |
| 模型 | 百炼四个：qwen3.8-max、qwen3.8-flash、deepseek-v4-pro-0813、deepseek-v4.1-flash（同一把 DASHSCOPE_API_KEY） |
| 用例 | Lx（C0×L0–L4，用户称 Cx）、Dx（D1–D8，D7/D8 各 A/B）、Px（P1、P2），每模型 17 格 |
| 次数 | 每格 1 次，共 68 条 |
| D1、D3–D8 | **做进平台**：黑盒 BladeAI 不经平台 MCP，平台现有扰动打不到它，改为在它自己的 ServiceAccount 上按触发点撤权/还权；每 slot 独立 SA |
| D7/D8 沙箱门槛 | 对 BladeAI 放开（它能自己跑 skill 脚本、写 sh，不需要平台 code_sandbox） |
| D8 备选执行器 | 借用新环境现有的 Chaos Mesh |
| D6 | 这轮一起做：在 BladeAI 与 API Server 之间加代理，A 拦下不转发、B 转发但吃掉响应 |

## 二、集群改动与回滚点（按时间顺序）

### 2.1 ChaosBlade 补齐集群级对象
- 现象：operator/tool 在 default 正常运行，但**缺 CRD `chaosblades.chaosblade.io`，且 SA `default/chaosblade` 无 ClusterRoleBinding**；operator 日志 400 行全是 forbidden / 找不到资源。
- 处置：从旧环境导出 CRD（线上对象，Helm 的 crds/ 不在 `get manifest` 输出里）和 Helm 发布里的
  ClusterRole/ClusterRoleBinding `chaosblade`，服务端预演后应用，重启 operator，重启后 0 报错。
- **未装** MutatingWebhookConfiguration `chaosblade-operator`：它拦截全集群所有 Pod 的 CREATE/UPDATE
  （failurePolicy=Ignore），是给 JVM/文件系统类故障注入边车用的，本轮 Pod 级 CPU 故障用不到；共享集群上不去碰其他租户的 Pod 创建。
- 镜像核对：operator 与 tool 在两边的运行时 digest 相同（131725…/85a225…）。
- 回滚：`kubectl delete crd chaosblades.chaosblade.io; kubectl delete clusterrole,clusterrolebinding chaosblade`（清单：会话 scratchpad `chaosblade-newenv-apply.yaml`）。

### 2.2 StorageClass `nfs-client`
- 09-05 建的别名已不在。复制 `openebs-hostpath` 参数新建（provisioner `openebs.io/local`，WaitForFirstConsumer），
  **BasePath 改为 1TB 数据盘 `/data/openebs/local/`**（原 openebs-hostpath 在系统盘 `/var/openebs/local/`）。
- 用临时 PVC + Pod 验证绑定与读写通过；顺带确认 worker 能从 Harbor 拉到控制器镜像。

### 2.3 Coroot（命名空间 `coroot`）
- 照旧环境：coroot-operator chart 0.8.2（Helm 发布清单，含 CRD `coroots.coroot.com`）+ Coroot CR（组件镜像全在 Harbor）。
- 与旧环境的差异（共享集群、节点有公网 IP）：服务 **NodePort 30800 → ClusterIP**；匿名角色 **Admin → Viewer**
  （Fleet 配置 `coroot_allow_anonymous_read: true`，平台只需匿名只读）；去掉不存在的 `harbor-registry` 拉取密钥。
- 两处"清单 ≠ 线上"：旧环境线上对象被手工打过补丁，Helm 清单里没有——
  operator 容器 `command: [coroot-operator]`、cluster-agent 的 kube-state-metrics 容器 `command: [/kube-state-metrics]`。
  不加时镜像默认入口 `/wrapper.sh` 报 `permission denied`。已照线上补丁补上，operator 不会改回。
- 结果：8 个 Pod 全部 Running；`/health` 200；匿名 Viewer 只读；**项目 id `ip1mqpq8`**（写入 FleetConfig `coroot_project_id`）。
- 回滚：`kubectl delete coroot coroot -n coroot`，再删 operator 清单与命名空间。

### 2.4 Prometheus 2.52.0 → 3.14.0 + OTLP（命名空间 `observability`，属 09-18 审计部署）
- 原因：平台要求 `--web.enable-otlp-receiver`、`--enable-feature=otlp-deltatocumulative`，以及配置段
  `otlp.promote_resource_attributes: [service.name, k8s.namespace.name, k8s.pod.name, k8s.pod.uid]`
  （否则请求指标无 Pod 标签，判定报 `request metrics lack target identity`）；该配置项 2.52 不支持。
- **镜像陷阱**：旧环境清单写的 `quay.io/prometheus/prometheus@sha256:60f8…` 实际是 **2.52.0**；
  cri-dockerd 把"只有 digest"的引用当成 `:latest` 拉，旧环境因此实际跑着 3.14.0（`sha256:5ce754…`）。
  一度把 60f8… 推成了 Harbor 的 `observe/prometheus:v3.14.0`，已立即用正确内容覆盖并核对 created 日期。
- 过程：先建回滚点 ConfigMap `prometheus-config-backup-20260922`；合并 otlp 段后用 promtool 3.14 在集群内
  `check config` 通过；切换镜像为 `1.94.151.57:85/observe/prometheus:v3.14.0@sha256:5ce7540c…`，保留原 7d 保留期与 `--web.enable-lifecycle`。
- 验证：版本 3.14.0；抓取任务均 up；两个 sock-shop 目标 down，但**升级前一小时 up 均值即为 0**，非本次引起；OTLP 端点可写。
- 回滚：`kubectl -n observability patch cm prometheus-config` 恢复为备份内容，镜像/参数改回原值（原始清单见 scratchpad `prom-deploy-new-original.yaml`）。

### 2.5 Loki 2.9.8（命名空间 `observability`）
- 新环境原本没有，平台 telemetry_ro 写死 `loki.observability.svc:3100`。照旧环境线上对象部署（ConfigMap + Deployment + Service，数据 emptyDir）。
- **又一处"清单 ≠ 线上"**：Harbor 的 `train-ticket/loki:2.9.8` 标签后来被覆盖为 2026-07-01 的重构建（61f8…），
  旧环境实际跑的是 2024-05-03 的官方构建（101829…）。按后者部署。`/ready` 返回 ready。
