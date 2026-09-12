# 本轮交接（2026-09-12）

分支 `codex/stage2-env3-bladeai-20260912`，从 `main` 的 `a7bea6b` 拉出，11 个提交，
工作树干净，**未推送、未发起合并、未动 main**。
全量测试 `uv run pytest`：**2231 passed**，1 failed（整改前既有），11 skipped。

三件事里**第二件完成**，第一、三件推到了各自的授权边界。本文是"解锁之后怎么接着走"。

---

## 一句话状态

| | 交付 | 卡在 |
|---|---|---|
| 一、第三套环境 | 准备方案、盘点脚本、差距表生成器、四项参照资产、部署验收脚本 | **进不去那三台机器** |
| 二、整改第一批 | 6 条 + 95 条测试 + 改动文档，未部署未跑评测 | — |
| 三、BladeAI | 设计核验、两条风险处理、`result` 与三级关卡契约全语料钉死 | **3 个拍板项 + 1 次实跑授权** |

---

## 卡点（按解锁价值排序）

### 1. 第三套环境的网络放行 —— 解锁一整条线

三台 `124.16.138.60/61/62` 的 SSH **在 banner 交换前被对端断开**。技术排查已穷尽：

| 试过 | 结果 |
|---|---|
| 直连 22 端口 | 无 banner（`kex_exchange_identification: Connection closed`） |
| 另外 6 个常见 SSH 端口 | 行为完全一致 —— 是路径不通，不是换了端口 |
| 从旧集群 `1.94.151.57` 当跳板 | 三台 `tcp22 不通` |
| `~/.ssh/config` | 没有指向这三台的配置 |
| `known_hosts` | **0 条记录** —— 这台机器从未连通过 |
| 本机 VPN（Shadowrocket） | Disconnected，未自行启用 |
| 应用层 80 / 443 / 6443 | 无任何响应 |

对照组：同一台机器连旧集群与第二套环境的 22 端口都能走到 `Permission denied`，
说明本机 SSH 出网正常。

**需要**：把出口 IP **`12.104.14.23`** 加进放行名单，**或**给一台能连到它们的跳板机。

### 2. 要不要推 `codex/bladeai-blackbox-impl` —— 风险最高

该分支**只存在于这台机器**（`git branch -r --contains` 为空），内容：
WP-A 黑盒驱动器 `harness/bladeai_http/` 930 行、44 条测试、
真跑抓下来的 golden 语料、以及实测校准过的设计修订。
远端 `origin/codex/bladeai-blackbox-integration` 只停在最早两份设计文档（`07b3e9d`）。

**机器出问题就没了。** 推送是对外动作，等你点头。
（`codex/stage2-replica-fleet-20260912` 同样未推，不在本轮范围内，但风险相同。）

### 3. `tool_screener` 关卡口径 —— 挡住 WP-C 完工

全语料只有 D8-B 一个样本。实际 payload：

```
type    : target_change
reason  : scope drift: approved=pod effective=chaosblade
original: scope=pod        namespace=otel-demo  names=["cart-7c58f6bb56-jzz9b"]
proposed: scope=chaosblade namespace=default    names=["b1f4bbf51e82d051"]   ← 实验 uid
```

**漂移跨了命名空间。** 选项 A（按"是否仍在授权目标的等效操作面内"判）实际要批准的是
"为了压 `otel-demo/cart` 这个 Pod，进 `default` 里的 chaosblade 工具容器操作"。
拍板前 WP-C 会记录该关卡但不自动应答。

### 4. 镜像仓库地址 + WP-A.2 的实跑授权

- 启动语里仍是 `<仓库地址待填>`。**范围比原计划大**：除平台那两个镜像，
  可观测栈 / Coroot / ChaosBlade operator 的十几个第三方镜像也在旧集群那台
  HTTP 明文 Harbor `1.94.151.57:85` 上，第三套环境大概率路由不到。
- WP-A.2（MCP 挂载连通性）需要在装了 BladeAI 的旧集群 `1.94.151.57` 上
  **起一个服务**。那台机器有两个不能碰的常驻 server（8199 / 8089），
  且 `/cancel` 是服务级的。我会用空闲端口、绝不复用、绝不 `pkill`——但这是写操作，等你授权。

---

## 解锁后怎么走

### 网络一通，第一件事的 1、2 步是两条命令

```bash
# 三台各跑一次（纯只读，不装不改不启服务，没 root 也能跑完）
bash tools/env3/inventory.sh > inventory-node60.txt

# 自动出差距表 + 待装清单
python tools/env3/gap_report.py inventory-node*.txt
```

差距表会直接告诉你：哪些是**阻塞级**（资源不够、不是 Docker、cgroup 不是 v2、
内核 < 5.12、没有 AppArmor profile……）、哪些只是**提醒级**（出网不通 = 要多搬镜像）、
哪些**未采集到**（不算通过）。每条期望都注明出处。

然后按准备方案的 **P2 → P8** 走，每步先说一声。P6 的验收已经脚本化：

```bash
python scripts/verify_stage2_deployment.py \
  --coroot-project <第三套环境自己的 Coroot 项目 id> \
  --require-node-selector \
  --private-listing <在 stage2 容器里抓的 "mode path" 清单>
```

### 三件事就绪后，BladeAI 按 A→B→C→D→E→F 走

`WP-E 必须先于 WP-F` 是硬规则。WP-A.2（MCP 挂载连通性）建议插在 WP-B 之前或并行——
不通会让 WP-B 之后全部返工。

---

## 本轮产出清单

**代码与工具**

| 路径 | 是什么 |
|---|---|
| `stage2_service/provider_failures.py` | O03：供应商故障分类 + 按路由熔断 + 归因 |
| `stage2_service/recovery_state.py` | O04：恢复状态机，未验证时不授权重装 |
| `stage2_service/image_manifest.py` | O18：镜像清单从 Dockerfile 推导 + 三处校验 |
| `stage2_service/mcp_tool_catalog.py` | O19：工具身份解析唯一真相 |
| `scripts/verify_stage2_deployment.py` | 部署后三件事的自动核对 |
| `tools/env3/inventory.sh` | 12 章只读盘点 + 机器可读事实 |
| `tools/env3/gap_report.py` | 差距表自动生成 |
| `deploy/chaosblade/` | cgroup 包装 + operator/tool 参照清单 |
| `deploy/observability/` | 可观测栈 21 个对象的参照清单 |

**文档**（全部在 `docs/status/`）

| 文件 | 内容 |
|---|---|
| `stage2-dx-remediation-notes-20260912.md` | 整改第一批：每条的文件行号、改前改后、原因、测试、影响面、不确定项 |
| `stage2-env3-preparation-plan-20260912.md` | 环境准备：目标状态、资产、资源、回滚、P0–P8 |
| `stage2-env3-reference-state-20260912.md` | 第二套环境实测参照（镜像来源、版本、管理方式） |
| `bladeai-070-design-confirmation-20260912.md` | BladeAI 设计确认单（3 个拍板项 + WP-A.2） |
| `bladeai-result-contract-verification-20260912.md` | 全语料契约核验（`result`、三级关卡、MCP 挂载） |

---

## 三处值得记住的发现

1. **八个 MCP 服务不是八个待装组件** —— Stage-2 流程里它们由控制器 Pod 内的
   `McpSupervisor` 每次试验现起现停。装好 stage2 Pod 就都有了。
   `qualify_mcp_endpoints.py` 验的是另一条主机路线的四个端点。

2. **`chaosblade-cgroupns-wrapper` 就两行** ——
   `exec nsenter -t 1 -C -- /opt/chaosblade/blade.real "$@"`，
   让 `blade` 回到 PID 1 的 cgroup 命名空间。没有它，`pod-cpu`/`pod-mem`
   **会报成功但什么都没压到**。依赖 `hostPID: true`。

3. **BladeAI 的 `result` 外层 `status` 恒为 `success`** ——
   D1 那条 `status=success` 但 `task_state=failed`。
   必须以 `task_state` 为准，否则失败的注入会被判成成功。
   而且 13/19 的用例**根本没有 `result`**，"等一个 result"这个前提不成立。
