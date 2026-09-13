# 第三套环境 P7：把 `/api/v1/stage2/options` 跑通

分支 `codex/stage2-env3-bladeai-20260912`。P6 部署验收通过之后，`/options` 里
**七个模型、四家智能体全部不可跑**。查下来是两个互不相干的原因叠在一起，一个是
仓库的问题，一个是这套集群的既有故障。本文记录查证过程、改了什么、没改什么。

---

## 症状

```
model_probes:  七个全 runnable=false，available_models=[]，gateway_probe=running
harnesses:     四家全 runnable=false，reason=qualification_not_passed
litellm 日志:  OpenAIException - Connection error.. Received Model Group=gpt-5.6-sol
```

一开始我以为是没给 `AIGCBEST_API_KEY` / `ACUCOMPUTE_API_KEY` 的占位凭据把整条探测
拖垮了。**这个判断是错的**，两处都错：

- `available_models=[]` 不是网关坏了，是那一刻探测还在飞。探测跑完后
  `/v1/models` 一直是 HTTP 200、七个别名齐全。
- 单个模型探测失败**不会**波及其他模型。`probe_models.py:774-825` 里 ERROR 级
  issue 只留给「缺 base_url / 缺 key / 超时参数非法 / 整轮抛异常」四种；单模型失败
  只落到自己的 `overallStatus` 和 `failureClasses`。
  （`runtime_factory.py:1811` 的 `runnable` 里确实有一个全局 `has_error_issue`，
  一旦有 ERROR 级 issue 会把七个模型一起打成不可跑——但单模型失败不产生这种 issue。）

---

## 原因一：仓库清单少了第四样「渲染即丢」的设置

`STAGE2_HARNESS_CAPABILITIES_FILE` 在 `deploy/stage2/stage2.yaml` 和
`stage2-integration.yaml` 里 **`grep -c` 都是 0**，而第二套环境的线上 Deployment
是设了的。少了它，`harness_capabilities_from_qualification()` 拿不到资格文件，
四家一律 `qualification_not_passed`。

这和已知的三样（`fsGroupChangePolicy`、`RESBENCH_COROOT_PROJECT_ID`、`nodeSelector`）
是同一类：**部署当场不报错，下一次运行才炸**。已并入 env3 overlay，并让
`verify_stage2_deployment.py` **无条件**核对——它的值不随环境变，任何环境少了都是错。

---

## 原因二：这套集群两个 CoreDNS 副本只有一个能用

这是真正压住七个模型的那个。证据链：

**1. 不是出网不通，是域名解析不出来。** 在 litellm 容器里分别测 DNS / TCP / TLS：

```
dashscope.aliyuncs.com  FAIL gaierror [Errno -3] Temporary failure in name resolution  6.53s
api.deepseek.com        ip=119.188.175.46  dns=4.08  conn=4.44  tls=0.05     ← 解析出来就通
```

解析成功时 TCP 和 TLS 都正常。所以出网是好的。

**2. 两个副本，一个全好一个全坏。** 绕开 Service 直接问 pod IP，各 10 次：

| 副本 | 节点 | 结果 |
|---|---|---|
| `10.244.0.156` (`coredns-…-gw58z`) | otcaix-62 | **10/10 通** |
| `10.244.2.53` (`coredns-…-krh8m`) | otcaix-60 | **0/10 通** |

**3. 坏的那个自己在日志里说原因**——它到所有上游 DNS 的 UDP 53 全部超时：

```
[ERROR] plugin/errors: 2 frontend. AAAA: read udp 10.244.2.53:56803->8.8.8.8:53: i/o timeout
[ERROR] plugin/errors: 2 image-provider. AAAA: read udp 10.244.2.53:49023->159.226.8.6:53: i/o timeout
[ERROR] plugin/errors: 2 otel-collector. AAAA: read udp 10.244.2.53:39101->1.1.1.1:53: i/o timeout
```

`8.8.8.8`、`1.1.1.1`、`159.226.8.6` 一个都不通。三台节点的 kubelet 都是
`resolvConf: /run/systemd/resolve/resolv.conf`，但 **otcaix-60 那份把 8.8.8.8 和
1.1.1.1 排在前面**（`8.8.8.8, 1.1.1.1, 159.226.8.6, 8.8.8.8`），otcaix-62 那份是
`159.226.8.6, 8.8.8.8`——CSTNET 自己的解析器在前。

**4. Service 在两者之间轮询，于是一半查询失败。** 经 `10.96.0.10` 查
`dashscope.aliyuncs.com` 八次，**严格交替**：

```
SERVFAIL/an=0/2.0s, NOERROR/an=5/0.0s, SERVFAIL/an=0/2.0s, NOERROR/an=5/0.0s,
SERVFAIL/an=0/2.0s, SERVFAIL/an=0/2.0s, NOERROR/an=5/0.0s, SERVFAIL/an=0/2.0s
```

这就是 litellm 那句 `Connection error.` 的全部来历。

**这不是本项目造成的，也不该由本项目修。** 坏的那个副本已经 95 天没重启，
otel-demo 和别人的 `aiops` 项目也在同样报错——是这套共享集群的既有故障。

### 只给平台 Pod 兜一层

`dnsPolicy` 为 `ClusterFirst` 时，`dnsConfig.nameservers` 是**追加**在 `10.96.0.10`
之后的：集群内名字仍然先问 CoreDNS，只有 CoreDNS 回 SERVFAIL 时才落到下一台。
关键前提查实过两条：

- 坏副本回的是 **SERVFAIL**（不是 NOERROR/NODATA）。glibc 遇 SERVFAIL 会换下一台，
  遇 NODATA 则认定为终局、不再往下问——所以这个办法成立与否全看这一点。
- 三个容器都是 glibc（stage2 / agent-runtime 是 Debian 2.36，litellm 是 Wolfi，
  也是 glibc 不是 musl）。
- 平台 Pod 钉在 otcaix-62，实测从它直连 `159.226.8.6` 五次全中、均 0.00s。

改完之后实测 **15/15 全通**（改之前约 25%）：

```
dashscope.aliyuncs.com: 5/5 ok, times=[4.04, 2.01, 2.01, 2.01, 2.01]
api.deepseek.com:       5/5 ok, times=[2.17, 2.01, 4.01, 2.01, 4.01]
api2.aigcbest.top:      5/5 ok, times=[4.01, 2.06, 2.01, 4.01, 0.01]
```

**代价**：每次解析多花 2–4 秒。`options ndots:5` 下一个外部域名要先试 4 个搜索域，
其中 `tailf68265.ts.net` 那个会被转发上游，碰到坏副本就是一次 2 秒的 SERVFAIL 等待，
bare 名字再来一次，所以是 2 秒或 4 秒。**没有**顺手去调 `ndots`——那会改变集群内
短名字的解析顺序，为了省几秒钟不值得担这个风险。连接复用之后每条连接只解析一次，
现状可接受。

---

## 结果

```
deepseek-v4-pro-0813     runnable=True   supported
deepseek-v4-flash-0731   runnable=True   supported
qwen3.8-max              runnable=True   supported      ← C0 要用的就是它
qwen3.8-flash            runnable=True   supported
gpt-5.5                  runnable=False  upstream model authentication or permission rejected
gpt-5.6-sol              runnable=False  upstream model authentication or permission rejected
claude-opus-5            runnable=False  upstream model authentication or permission rejected
```

**七个里四个可跑。** 剩下三个正是没给凭据的那三条路由（`AIGCBEST_API_KEY`、
`ACUCOMPUTE_API_KEY`），现在报的是干净的「认证/权限被拒」，不再是会误导人的
`Connection error.`——要不要补这两把钥匙仍然是待拍板项。

---

## 改了什么

| 文件 | 改动 |
|---|---|
| `deploy/stage2/env3/values.yaml` | 加 `harnessCapabilitiesFile`、`dnsFallback` |
| `deploy/stage2/env3/render.py` | 套用这两项；`dnsConfig` 只在配了 nameservers 时才写 |
| `deploy/stage2/env3/README.md` | 四样「渲染即丢」列成表；DNS 那一节写清是共享集群故障 |
| `scripts/verify_stage2_deployment.py` | `STAGE2_HARNESS_CAPABILITIES_FILE` 无条件核对；`--dns-fallback` 选择性核对，且 `dnsPolicy` 不是 `ClusterFirst` 时直接判失败（那种情况下追加语义不成立） |
| `tests/test_stage2_env3_overlay.py` | 基线清单现在应失败 5 项（原 3 项）；新增能力文件与 DNS 追加语义的用例 |
| `tests/test_verify_stage2_deployment.py` | 夹具补上能力文件；新增缺失/空值/选择性开启三组用例 |

全量 `uv run pytest`：**只有 `test_observation_adapter_uses_fixed_service_proxy_queries`
一条失败，是整改前就有的那条**，无新增回归。

---

## 给集群管理员的建议（本项目没做）

真正的修法是二选一，都需要改共享组件，所以留给集群方拍板：

1. 把 otcaix-60 的 `/run/systemd/resolve/resolv.conf` 里 `8.8.8.8` / `1.1.1.1` 挪到
   `159.226.8.6` 后面，或直接去掉——这两个在 CSTNET 网内本来就不可用；
2. 查 otcaix-60 上 pod 网段（`10.244.2.0/24`）出网 UDP 53 为什么不通（其余两台通），
   多半是 SNAT/masquerade 或该节点的出口策略。

修好之后本项目这层 `dnsFallback` 可以直接去掉，`values.yaml` 删掉那一段即可，
`--dns-fallback` 不传就不核对。

---

## 附带查出来的一件事：私有文件检查会每跑一次误报一次

资格流程一跑起来，P6 那项 `private-file-modes` 就失败了，指着六个 0644 的
MCP 日志。**这是误报**，目录链挡着：

```
700 .../private          700 .../private/mcp-runtime
755 .../mcp-runtime/campaign-<id>        700 .../mcp-logs        644 .../mcp-logs/k8s_ro.log
```

`private` 和 `mcp-logs` 都是 0700，uid 10001 之外根本穿不进去。P6 当时报
「1 files, none group- or other-accessible」，只是因为那会儿环境还是干净的，
私有根下只有一个 `service.kubeconfig`——**任何跑过评测的环境都会失败**。

检查本身的毛病：只看文件位，不看目录链。已改成按**实际可达**判——某个类别要能
读到文件，既要文件对它可读，也要每一层祖先目录对它可穿越，group 和 other 分开算；
文件位松但被目录挡住的仍然列出来，只是不判失败。采集格式也从
`find -type f -printf '%m %p'` 改成 `-printf '%y %m %p'`：先前「25 files」里混进了
空目录（`telemetry/events` 是 0755 空目录，被前缀启发式当成文件），改用 `%y`
明确拿类型。两种旧格式继续兼容，且只会多报不会漏报。

**平台侧的真问题留给单独一轮**（不在本批 6 条整改范围内）：这些日志建的时候没显式
给 mode，落在 umask 0022 上就是 0644，而令牌、策略、kubeconfig 都是显式 0600。
另外 `campaign-<id>/` 是 0755 而它的上级和下级都是 0700，说明 mode 纪律是不齐的。

线上部署现在 9 项全过：

```
ok  containers / fsGroupChangePolicy / fsGroup
ok  RESBENCH_COROOT_PROJECT_ID: po24tcoz
ok  RESBENCH_COROOT_ALLOW_ANONYMOUS_READ: true
ok  STAGE2_HARNESS_CAPABILITIES_FILE: /var/lib/resbench-stage2/integration/private/harness-capabilities.json
ok  nodeSelector: {"kubernetes.io/hostname": "otcaix-62"}
ok  dns-fallback: 159.226.8.6
ok  private-file-modes: 23 files, none reachable; 6 have loose modes but sit under a private directory that blocks them
```

---

## P7 验收：三家智能体可跑

`/api/v1/stage2/options`（2026-09-13 03:10，第三套环境实测）：

```
codex             runnable=True   可跑模型 [deepseek-v4-pro-0813, deepseek-v4-flash-0731, qwen3.8-max, qwen3.8-flash]
claude-code       runnable=True   可跑模型 [同上]
deepseek-harness  runnable=True   可跑模型 [同上]
bladeai           runnable=False  qualification_not_passed
```

**BladeAI 不可跑是对的，不是缺陷**：发布器对只有 base 记录的 BladeAI 明确保留
`bladeai_full_chain_qualification_required`，它要走 WP8 全链验证——那是第三件事。
启动语要求的是「三家智能体可跑」，已满足。

### 资格是怎么拿到的

不能手写那个能力文件——`capability_preflight.py` 的设计就是**只认真跑出来的证据**，
手搓等于把整个 fail-closed 拆掉。按 `docs/deploy/stage2-base-channel-qualification-20260906.md`
的正规流程走，一家一跑（每家 10 次真实模型请求，模型用 `qwen3.8-max`）：

| Harness | 尝试目录 | 结果 | 七项基础检查 |
|---|---|---|---|
| codex | `env3-20260913-a1` | `status=passed` | 全 true |
| claude-code | `env3-20260913-a2` | `status=passed` | 全 true |
| deepseek-harness | `env3-20260913-a3` | `status=passed` | 全 true |

三家的 `failure_reasons` 与 `cleanup_errors` 都是空。跑之前确认过环境干净
（`task_count=0`、无 chaosblade CR、四类 chaos-mesh 故障均为 0）。

三份记录**一次性**发布（发布器不隐式合并旧文件，要保留谁就得全列出来）：

```
{"status": "published", "output": ".../private/harness-capabilities.json"}
```

`collective.all_passed=false` 不是失败：它是 `complete_harness_set=false` 的映射，
而「一次只跑一家」正是操作手册自己的要求。发布看的是每份记录的 `status`。
