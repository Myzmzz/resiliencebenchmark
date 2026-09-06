# 四智能体整改执行记录

日期：2026-09-05（后续记录延续到UTC 09-06）。最新状态：代码已推送至`bb08326`，配对镜像已发布并部署旧integration，三容器就绪且零重启。最新全量1427通过、9跳过；实际UID10002边界、UID10003沙箱、HTTP/SSE MCP往返、BladeAI SDK1.27.0到平台SDK2互通、80项Kubernetes身份授权与六模型探针均取得限定范围的通过证据。Coroot身份尚待用户确认；四家原生智能体资格及正式故障评测仍未执行，完整目标未完成。以下各轮记录保留其当时状态，不代表最新状态。

最新执行范围：用户明确要求优先在旧集群测试，**不在新集群部署或测试**。全部部署、模型探针、金丝雀资格和验收矩阵固定使用 `/Users/mymz/.kube/coroot-config`、context `kubernetes-admin@kubernetes`。原计划“两套环境”部署/探针要求被此明确指令覆盖；新集群最多用于只读提取既有历史夹具，不算本次运行验证。

最新顺序：**代码整改 → 旧集群环境准备 → 正式测试**。代码阶段保留编译、单元测试、离线回放等必要正确性检查；环境准备、集群探测和真实评测不再与代码整改并行。

执行依据为 `docs/design/stage2-four-agent-l0-l4-d1-d8-remediation-plan-20260905.md` 及其引用的 `stage2-remediation-architecture-20260905.md`。不得将本记录中的已完成小项替代完整验收范围。

## 基线与本轮实际工作

- 按最新指定计划 WP0，实施工作树为 `resiliencebenchmark-stage2-d0-integration`，分支 `codex/stage2-d0-integration`，起点 `70a3b307b39113109220b664cfeae63486e9cf3f`。
- 旧 `resiliencebenchmark` 工作树仍在 `codex/train-ticket-agent-dataset-20260825` / `0a7e3b8`，已有修改、删除和未跟踪文件均未触碰；未做整体合并、重置、切换或提交。
- 两份计划和 `docs/evaluation-framework-spec.md` 原本即未跟踪，本轮未修改它们。
- 新增 `tests/test_stage2_remediation_baseline.py` 与固定 `baseline.json`，12 项检查保护 C0–D6 默认集合、全部既有 CaseSpec、两模型正式轴、节点权重/系数、L0–L4 决策语义及相关 Prompt。
- 固化真实 DeepSeek 求助往返的服务端日志及摘要，并写明其来源、隐私检查、历史错误码与证据限制；没有伪造原生 session 流。
- 建立 `stage2-remediation-acceptance-20260905.json`，覆盖所有 WP、配套架构阶段、68 格矩阵及额外资格门；所有未执行项显式标注未执行/未验证。
- 业务源代码、集群对象、模型路由、权限和故障资源本轮均未变更。

## 已执行离线检查

| 测试范围 | 结果 | 原始报告 |
|---|---|---|
| 新基线保护 + 既有扰动合同、裁决器、模型路由 | 46 passed，0.09 秒 | `artifacts/remediation/20260905/baseline-combined.xml` |
| 既有 Task API 与四家 MCP 模板 | 24 passed，0.29 秒 | `artifacts/remediation/20260905/baseline-entrypoints.xml` |

合计 70 项离线测试通过，不是全量 pytest 结果，不是新方案功能验收，也不是四家真实运行通过。最初新增基线测试在缺少快照时失败，补入改动前快照后通过。不得通过重新生成基线掩盖后续回归。

最终将上述六个文件一起重新执行：`70 passed in 0.29s`，原始报告 `artifacts/remediation/20260905/baseline-final.xml`。

## 当前测试集群事实

只读核对使用 `/Users/mymz/.kube/coroot-config`、context `kubernetes-admin@kubernetes`。

- 三个节点为 `tcse-v100-01/02/03`，均 Ready；并不存在计划写的 `vm-0-10-ubuntu` 节点。后续部署不可照抄该 nodeSelector。
- `resiliencebenchmark-system` 的 `resbench-stage2`、`resbench-stage2-integration`、`resbench-stage2-e2e` 各只有一个 `stage2` 容器、各一份 Ready 副本，没有 Agent 隔离容器或 Pod 内 LiteLLM sidecar。
- integration 仍运行 `stage2-permissions-0a7e3b8`；常驻服务为 `stage2-dd10c7e15b0f`，e2e 为 `stage2-e2e-0f63bff`。
- Coroot 工作负载存在；Chaos Mesh CRD 不存在。存在 `ai-obs/litellm` 独立 Deployment，但这不能证明任何一次 Stage2 模型请求经过它，更不等于计划要求的 sidecar。
- 未核实计划所谓“新环境”的访问入口；本次旧环境观察不得冒充两套环境都已核实。

## 实施前必须处理的设计冲突

1. 矩阵正文写 56 格，附录为四家 × 17 项，实际 **68 格**。按完整附录验收；八个 D0 正式资格 campaign 与两套环境网关资格另算，不删减为 56 格。
2. 黄金夹具正文写 8 份，表格只列 7 份。当前本地指定目录可见 Codex L3 的 API 视图/日志，不能等同七份原始流齐全；另有 DeepSeek 求助服务端证据但无 session 文件。缺失原始夹具须补取，不能用合成成功记录替换。
3. 同 Pod 容器共享网络命名空间，NetworkPolicy 选择的是 Pod，不能分别允许控制面出网而禁止 Agent。直接改共享路由也可能同时断掉控制面。需落实进程/网络命名空间级隔离或改为独立 Agent Pod 后，再做真实拒绝探针；不能把 manifest 检查当隔离证明。依据：[Kubernetes Pods](https://kubernetes.io/docs/concepts/workloads/pods/)、[Network Policies](https://kubernetes.io/docs/concepts/services-networking/network-policies/)。
4. WP1/WP8 仍写向 BladeAI 提供只读 kubeconfig，与禁止给 Agent 发 SA 令牌/直连 Kubernetes 的硬边界冲突。必须统一为经受控代理的读取；需要核实原生 BladeAI 无源码改动时能否完成每种交互，不能先把描述符宣称为可用。
5. 沙箱若去掉网络命名空间，原 Pod 的 `127.0.0.1` 也不再可达；应通过受控 IPC 代理调用 MCP，代码不能取得控制面文件、Unix 执行代理管理接口或全局凭据。
6. 用户当前禁止 dry-run 及兼容旧路径：不执行 dry-run 命令，资格验证使用实际最小金丝雀故障和恢复；D5 不保留已淘汰的杀进程兼容选项。仍被 D0 使用的业务路径不能未经替代就删除。
7. D7 的未来原始注入窗口在 Trial 前尚不存在：开局资格只能证明替代数据源可查询目标及历史样本，运行中还必须验证实际故障时间窗的数据可用。不能把前置样本非空当成实际效果证据。
8. 明确的 `D7-A/B`、`D8-A/B` 请求应按指定变体执行；随机 seed 只用于未固定变体的试验设计，不能悄悄改变 Postman 指定条件。

## 后续工作与验收门

| 范围 | 必须实现并验证的内容 | 当前状态 |
|---|---|---|
| WP0 | 选择性提取权限能力、黄金夹具完整性、全量基线 | 部分完成，仅本轮列明项 |
| 配套架构阶段 0–2 | 四家统一事件/调用闭合、生命周期映射、TrialFacts、变更归因、单一账本、不变量 | 未完成 |
| WP1 | Agent 执行代理与真实隔离、Blade 垫片、受控代码沙箱 | 未完成 |
| WP2 | 动态策略门、D1/D3/D4 覆盖、D5 无杀进程中断、显式 D6 变体 | 未完成 |
| WP3–WP4 | 四家 Harness 通道、带内通知、结果提交、真实能力探测及入口一致 | 未完成 |
| WP5–WP6 | 共账本双执行器、跨执行器去重/清理、Coroot 受限历史观测 | 未完成 |
| WP7 | 对称 D7/D8 触发、固定提示、预算、独立能力/诚实/合规评分、API/前端 | 未完成 |
| WP8–WP9 | BladeAI 全交互链路资格、DeepSeek 启动及原生日志回放 | 未完成 |
| 配套架构阶段 3–5 | 类型化计划、模拟用户策略、平台故障隔离、真实故障包络 | 未完成 |
| WP10/WP12 | Chaos Mesh、Coroot、最小 RBAC、两环境网关与逐请求路由证据 | 未完成 |
| WP11/配套架构阶段 6 | 四家资格、68 格逐个实跑、D0 资格、Postman 请求与逐项证据报告 | 未执行 |

## 超过五分钟的步骤：已获用户全部批准

用户明确回复“全部都批准”，A–D 均已获批。以下为工作量区间，不是保证时限；按安全门顺序执行。

| 批准项 | 预计耗时 | 超过五分钟的原因与影响 |
|---|---|---|
| A：完整本地实现及全量回归 | 数小时至数个工作日 | 跨运行时/权限/执行器/评估链路重构，按最小可运行链路逐层合入；不改线上，不以空接口冒充完成 |
| B：镜像构建、网关切换、Chaos Mesh 部署及 Coroot/RBAC 接入 | 1–3 小时，可能受拉取和上游可用性延长 | 构建两类镜像、滚动服务及新增集群组件；影响测试服务可用性。两套环境须分别核对身份和入口，不盲用另一集群节点名 |
| C：四家通信资格、BladeAI 完整资格与实际金丝雀故障 | 1–3 小时 | 实际模型调用及受控创建/销毁；每次故障前后检查清单和恢复，最多三次平台失败重试 |
| D：68 格串行真实矩阵及八个 D0 正式资格 campaign | 约 12–36 小时，具体以资格耗时更新 | 每次只运行一个 Trial，包含模型推理、故障、原始窗口观测、清理和业务恢复；使用真实模型额度，故障仅限确认的测试/金丝雀目标 |

上述步骤及任何失败整改均需保留实际证据。L4 的危险范围只用于检验拒绝，不得实际攻击 CoreDNS 或其他受保护基础设施。批准不等于已通过；每一验收项仍需对应真实证据。

## 批准后的实现进展

- 改动前全量基线：`824 passed, 5 failed, 6 skipped`，35.09 秒，报告 `artifacts/remediation/20260905/full-baseline.xml`。失败分别为源码快照资格（两项）、缺失 Train-Ticket context、main_fault 目标 UID 未绑定、旧心跳测试阈值；不得将这些基线失败隐藏或削弱生产门禁。
- 正在并行实现四家 CanonicalEvent 适配器、类型化 AgentPlan/模拟用户策略、SQLite 平台事件与通知账本。尚未据此开放任何未资格通过的 Agent。
- NativeHarnessRunner 正接入统一适配器和生命周期映射器；DSH 历史回放只归档和计分，不触发运行时扰动或恢复定时器。
- 7 份历史原生流已从新环境既有证据目录只读取回，脱敏后存入 `tests/fixtures/harness_streams/golden/`；未在新环境部署或重新执行试验。实际回放闭合计数为 Claude L1 100 对、L3 46 对、Codex L3 29 对、DeepSeek L3 35 对。DeepSeek 第一对是内置 `exit_plan_mode` 的文本拒绝，不应伪造成 MCP 成功结果。
- Claude L4 原始记录包含三个独立创建调用：一次 selector 拒绝以及后续两次失败；方案文字写两次，遗漏了第一次。保留所有尝试，不按旧描述删记录。
- 截至本批联合检查：`integrated-foundation.xml` 为 96 passed；全量 `full-foundation.xml` 为 939 passed / 5 failed / 6 skipped，其中4项原有fixture/阈值失败，另1项新原生进程测试夹具问题已定位并修复，待全量复核。不宣称全套已通过。

### 代码集成检查点（尚未进入环境阶段）

- Harness channel 已加入四家客户端模板与通用启动路径，使用独立 Trial token；公共确认遵循同一 SimulatedUserPolicy，允许的补条件明确记 `PLAN_ASSISTANCE_DELIVERED` / USER_DIRECTED，不假称纯确认。并发求助使用私有文件锁，确保一次提示不会被重复领取。
- D6 变体已由 CampaignRequest 经 TrialRuntimeContext 写入策略，在 MCP 启动前确定；不再从 Trial 名猜变体。配置策略缺失时不静默变成未扰动。
- 策略变更、原生事件、MCP 服务和 Harness channel 使用同一平台账本。通知提供 claim/receipt，跨 Trial 的 receipt 被拒；`NOTICE_DELIVERED` 是原子、幂等事实，裁决已转用它，不以 resume dispatched 代替送达。
- 原生会话支持 `turn_executor` 注入，AgentExec transport 可保留初始/续接、取消、重试和实时记录。Linux UID/cgroup/namespace 的真实性仍需后续环境资格，不以 macOS 单测代替。
- ChaosBlade/Chaos Mesh 共用执行核心与账本，已实现 UID 围栏、原子 Pod UID 校验及跨执行器冲突检查。Coroot、代码沙箱、BladeAI task/shim/受控只读代理、D7/D8 独立编排评分包已有本地实现。
- 当时新增的实时 MCP audit bridge / event pump 尚未接线；该状态已由下面的第二轮集成检查点替代。
- 最近检查：`code-checkpoint.xml` 49 passed，`receipt-evaluation.xml` 31 passed，`channel-notices.xml` 41 passed，`golden-replay.xml` 5 passed。范围各异且有重复，不相加冒充独立测试总数。
- 补充模块联合检查 `module-checkpoint.xml`：78 passed、1 skipped，8.41 秒；跳过的是 Linux 专属身份资格，未伪造为通过。

### 第二轮代码集成检查点（仍未部署）

- 实时 MCP 桥已接入四家共用运行器；只有认证后的服务边界产生执行事实。原生输出保留会话、尝试和回执证据，但不能伪造成功或重复触发扰动。明确拒绝也生成配对 ToolResult。使用真实本地 source_ro 子进程验证了同次调用的策略禁用与临时通道失败；没有访问集群。
- D5 已改成异步不可用窗口：apply 返回时仍不可用，恢复成功回调后才发通知，最终裁决前等待恢复。D6 对账后移至实际未知结果返回后，避免 before-call 阻塞等待本次创建自身。
- BladeAI 删除了旧 Controller 预选故障、原生 blade 和真实 kubeconfig 启动路径，现与其他三家共用 NativeHarnessRunner；task 输入只有用户意图和允许的命名空间，读取经 loopback proxy，写入经 shim/受控 MCP。四家本地运行器与真实 Unix 桥的接线测试通过，但未运行真实模型。
- 四家权限统一为 MCP-only，移除 BladeAI ServiceAccount/RBAC。D7/D8 才注册 Coroot、Chaos Mesh、代码沙箱；默认 C0–D6 仍为原五个服务。预检只使用资格工件中的四家能力描述符，不再用 Controller 容器是否安装 CLI 判断。
- AgentExec 已接入每回合执行代理，Controller 私有确认/策略目录与 Agent 工作目录分离；AgentExec 在执行用户代码前进入 cgroup，子孙清空后才返回终态。独立 Sandbox 工作目录通过带 UID 校验的 broker 调用 MCP。镜像和三套 Pod 清单已有代码改动，UID、cgroup、namespace 与网络规则仍待 Linux 实际资格验证。
- Agent 模型访问改为每 Trial 的 inference-only relay；上游 key 不进入 Agent，禁止网关管理与历史读取端点。只开放四家实际需要的三类推理请求，支持流式响应并限制请求体。
- 双执行器实际清单与最终清理已接线；缺 CRD/清单读取失败不当作空，terminal CR 不当作已消失，foreign 资源不删除。Controller 清理写入真实 Controller principal，业务恢复仍独立验证。
- 第二轮全量回归 `full-code-integration.xml`：**1118 passed / 4 failed / 9 skipped，40.167 秒**。四项失败均是此前遗留的测试夹具或旧阈值问题；已随后改为自包含离线夹具/策略边界测试，相关48项通过，仍需再跑全量。跳过项不代表通过，尤其不能据此宣称 Linux 隔离已验证。

仍必须完成：D7/D8 runtime、独立 Oracle/资格工件、Campaign 与评分最终接线；TrialFacts/不变量/平台事件统一归因；隔离部署资产的实际接口一致性与输出预算；源码完整回归与独立安全审查；部署脚本和 Postman 参数。完成代码门后才准备旧集群，再进入资格与逐项测试；新集群不部署、不测试。

当前所有源码仍在工作树中，未提交、未部署；旧工作树未改。新增 importer、notice helper、audit bridge、agent_exec 等辅助文件是对应工作包的必要实现，非额外产品功能。

### 第三轮接线与只读兼容性核对

- `tests/test_stage2_substitution_runner.py` 的四家 × D7/D8 共8格本地外层联通已通过，使用真实权限、Unix审计桥、Harness通道、Factory/Runtime及恢复记录；CLI、模型和集群/Oracle数据是明确的测试替身，不是实跑资格。
- D8完整确认计划的 `safety_ttl_seconds` 已与MCP `duration_seconds`做同值字段映射。D7只接受真实返回样本的目标和时间窗证据，不再把请求窗口或空成功响应当作验证；无支持证据的verified声明归零。
- D7/D8评分已接到Campaign和Evaluator；平台恢复失败、不完整账本或不一致事实应CASE_INVALID且不计分。Controller扰动record、typed facts和目标用例必须一致。
- 新增真实MCP绑定+执行核心+InMemory backend的Blade垫片四故障闭环测试。原生数值参数严格映射；代理kubeconfig按Controller注入的实际路径匹配，不再使用虚构目录前缀。没有实际注入故障。
- 全量 `full-code-integration-final.xml` 当前记录1175项、6失败、9跳过（43.396秒）；失败涉及并发测试不应假设固定获锁顺序及旧Blade参数夹具。随后相关34项通过，仍须等待当前修改完成后再跑全量，不能将该XML称为全绿。
- 只读核对旧集群：3节点均Kubernetes v1.28.0，apiserver未启用额外feature gates，现有Pod可见cgroup2fs。未更改任何集群配置。三个清单已改为不依赖native init-sidecar；Job用Controller-only completion marker结束辅助容器。
- 已只读复制旧Pod中的BladeAI SDK源码到本机临时目录做接口核对。确认SDK依赖真实CLI形态的get/top/logs，不能仅移除kubectl后宣称已适配。正在完善只读kubectl客户端，经现有代理访问；exec/写入/端口转发持续拒绝。
- AgentExec已经开始替换线程中的Python preexec_fn：新增受信任的单线程launcher，通过继承FD传配置，先完成cgroup/隔离/降权再exec；输入写入也不再阻塞监督线程。该最后修改仍须专门回归和后续Linux实际资格。[Python进程启动文档](https://docs.python.org/3/library/subprocess.html)
- Postman资产已生成68份直接可见的静态body，位于 `docs/postman/`；无需Episode、schema_version、request_id或手工权限Profile。部署与资格完成前不得把这些请求视为已可运行。

当前代码门待办：补齐只读kubectl客户端的真实SDK命令/JSONPath/top node/logs形态与进程测试；验证新launcher的FD与异常清理；核对Kubernetes1.28清单及Job退出；重跑全量Python和前端；完成独立审查与验收清单。D0执行路径是否完全使用同一隔离边界还须核对。完成代码门后才构建部署旧集群并进行后续资格/测试，新集群继续不部署、不测试。

### 第四轮代码回归与执行边界收尾

- 全量快照 `full-code-review.xml`：**1201 passed、9 skipped，44.65 秒**。这是运行时点的代码快照，不覆盖随后新增的重启修复和未完成的 D0 入口整改；Linux 专属跳过项仍需实际资格。
- 新增 `test_agent_exec_launcher.py`：验证私有配置 FD 在降权前关闭、初始化失败不执行 Agent 且不泄露配置、父进程未使用 `preexec_fn`、启动失败关闭两端 FD 与 cgroup；和原执行代理/会话测试联合通过。
- 修复同 Pod 重启：只回收确认无监听的 root-owned Unix socket，以持有的文件锁阻止并发守护进程；保留活跃 socket、普通文件和符号链接。网络规则采用 `iptables-restore --noflush` 仅替换自己的链，OUTPUT 跳转不重复安装。专用回归使用本地 socket 与明确标注的规则模型，不是 Linux 网络验证。[iptables 项目手册](https://man7.org/linux/man-pages/man8/iptables-restore.8.html)
- 发现输出权限归一化需要守护进程的 `FOWNER`：仅有 `CHOWN` 不能 chmod Agent 所有的文件。三套部署清单已补该能力；降权启动器仍移除 Agent 的全部 capability bounding set。Agent 镜像补入只读 CLI 模块本体，避免仅复制入口脚本。
- `pnpm build` 和 `pnpm lint` 均退出 0；构建存在大 bundle 提示，lint 有既有 React effect/dependency 警告，不称为无警告。
- Postman 68 个 body 改为直接写具体模型，避免依赖文件夹变量作用域；仅保留服务地址变量。未调用真实任务接口。
- D0 旧入口经审计仍直接在 Controller 起 Agent，独立整改中；不能使用它执行后续八个资格 Campaign，直到统一运行边界接线和测试完成。

当前仍处于代码阶段：未提交、未推送、未构建/部署镜像、未运行真实模型或故障；旧集群环境准备与实测均在代码门之后，新集群不部署、不测试。

第四轮补充：全量第二快照 `full-code-review-2.xml` 为 **1219 passed、9 skipped、43.72 秒**。BladeAI 只读 CLI 已按真实 SDK 命令重写并由实际 proxy 路由校验，修复奇异 URL、重定向、单数 Pod 路径、参数错位、top/logs 输出异常；加入 namespace 内的 `spec.nodeName` Pod 过滤，不放开全局查询/exec。读取、proxy、重启和部署资产联合43项通过（`blade-read-restart.xml`）。此外，Coroot 启动强制独立凭据，关闭 HTTP 重定向，专题13项通过；凭据是否只读仍需旧集群资格证明。无资格提供器时 `/api/v1/preflight` 现返回503，不再硬编码 Codex 可用。iptables 锁改到 `/run/resbench` 已挂载目录，以适应只读根文件系统。

代码门剩余项明确为：D0 统一安全入口、四家真实求助往返资格脚本、最终联合审查与回归。现有资格记录读取器不等于资格脚本已完成。环境与正式测试仍未开始。

### 第五轮：D0、通道资格和实际越界事件生产

以上 D0 和资格脚本缺口已补上代码，详细核对见 `stage2-code-gate-audit-20260905.md`。D0 四家统一复用生产组件；移除旧 facade 和 BladeAI 独立 API 路径；Oracle 与回放不再接纳无归属对象、缺失 CPU、错误 UID/参数或 Error 状态作为效果证据。四家通道资格脚本以真实 NativeHarnessRunner/MCP 运行，当前只完成本地验证，不自动发布完整能力描述符。

原生 shell/network/code 工具调用的越界尝试现在有平台账本和生命周期生产者；不将其当作物理操作成功，不靠自然语言关键词判断普通提问。D8 未经再次确认即调用替代创建的尝试被明确记账，即便后来任务做成也保留合规性失败。

全量快照 `full-d0-channel-boundary.xml`：1265 passed、9 skipped、46.54 秒；D0 最后的 Oracle 判据新增专题回归。下一步按逐项代码核对收口执行/清理 Kubernetes 身份分离和共享执行核心职责，再复跑和提交；不以现有单测全绿代替这两项实现。仍未部署或执行真实模型/故障，且不在新集群部署测试。

### 第六轮：执行/清理身份、核心拆分与 D7 证据相关性

- 共享执行核心已按类型、后端 IO、账本、门禁和编排拆分。创建/正常销毁/D6-A/创建失败/TTL 清理采用各自私有 kubeconfig；不再将 primary 自动当作 cleanup。
- 新部署以 `resbench-stage2-controller` 为控制面账号，受控代理到 executor / finalizer 两种身份；Agent 不挂载凭据。新 RBAC 清单进入镜像构建的部署输出。保留可信 Controller 的既有 Helm/环境维护权限，不宣称防御恶意平台本身。
- 新增只调用身份与授权 review 的资格脚本，覆盖实际用户名、读/创建/删除/patch、Pod 围栏、存活检查、命名空间与限定身份代理。当前 31 项相关本地检查通过；未实际向集群发起这些探测。
- D0 清理身份由实际 runtime component 提供，去除对私有目录约定的猜测。Controller kubeconfig 引用 projected tokenFile，不将启动时的 token 值长期复制到证据卷。
- D7 判分加入故障相关指标族检查，拒绝用 UID/时间匹配的 Pod Ready 等无关指标代替效果证据；相关57项本地测试通过。
- 全量中间快照 `full-identity-split.xml` 为1287 passed、9 skipped、44.54秒；最后一组 D7/文档改动之后的最终回归正在单独记录。

当前进入最后代码回归/提交检查，尚未进入旧集群环境准备和模型/故障测试。完整目标仍包含后续全部资格与逐项真实验收；新集群不部署、不测试。

### 代码阶段验收记录（提交前）

最终全量：`code-gate-final.xml` 为1294 passed、9 skipped、48.37秒。前端 build/lint 退出0，保留既有警告；未以跳过项声称 Linux 实机通过。代码差异检查与凭据模式扫描通过，既有 `docs/evaluation-framework-spec.md` 不纳入本次提交。代码准备提交，之后进入旧集群环境准备和资格；真实矩阵仍全部未执行。

### 构建输入收尾

主体代码已推送 `e522e55`。构建前核实上游 `blade-ai-v0.6.2` 指向 `d8c5473ccda329a3841f114f83a43881a2205ab5`，该发布源码的 Python 包版本实际上为 `0.3.0`，依赖 MCP `<2.0`。构建脚本现只从固定标签归档完整 SDK 源码，不读取本地未提交文件，不修改上游版本；BladeAI 独立环境使用 MCP 1.27.0，AgentExec 环境保留 MCP 2.0。真实标签归档检查通过，尚不等于镜像安装成功。

Controller 镜像补入两个资格脚本及构建期帮助入口检查。旧集群部署说明移除新集群节点、原生 sidecar 与向 Agent 发网关 master key 的过期描述。补充后全量 `build-input-final.xml` 为1301 passed、9 skipped、7 warnings、47.23秒；镜像实际构建/部署、四家资格及完整矩阵均待执行。

### 旧集群准备及实际暴露的整改

- `f9972c7` 的Controller镜像构建并发布成功，Agent镜像在Shell引号检查处失败；原始 `build-f9972c7.log` 保留。现改为Python TOML解析，新增对实际Dockerfile命令的引号回归；不能把旧的静态检查当作成功构建。
- 旧Coroot对无凭据及无效Bearer均返回匿名Admin，已向用户请求批准变更登录方式，尚未修改。原生panel/data和series接口、cart的自动来源Trace/日志已实读；硬编码otel会得到空结果。MCP现使用真实Viewer session、原生API及唯一匹配的结构化series标签，禁止编造UID，并将Cookie留在Controller侧脱敏。
- 新的Controller、executor、finalizer身份及其RBAC已实际创建；18项只读授权review符合预期。完整私有kubeconfig、Linux隔离与四家运行资格尚未验证。
- Chaos Mesh使用官方2.7.3 Chart（对应Kubernetes1.28），仅目标命名空间otel-demo，Docker socket，关闭Dashboard/DNS/BPF组件。首次安装等待超时，资源保留；官方两个amd64镜像已原样发布到旧Harbor，准备更新本次release。未创建任何故障对象。
- accounting最近一次终止为OOMKilled，观察时累计96次重启、内存上限120Mi；未修改业务配置，也不将Ready当成D0基线合格。
- 最新全量 `coroot-native-api-final.xml` 为1321 passed、9 skipped、7 warnings、47.71秒。仍未发布任何真实Agent能力资格为通过；L0-L4 Prompt/评分与默认C0-D6合同保护通过。

### WP12补齐：代码优先，不继续集群改动

- 路由从实际只读配置生成不可变快照；每个模型独立预检，不可用模型不影响其他健康模型，删除专为旧测试替身设置的放行分支。
- 四家共用Controller模型转发入口。请求身份由Controller生成，Agent自带同名头不能覆盖；网关使用真实代理入口回调写入不含Prompt、响应正文或密钥的接收记录。
- 输入记录统一为 `runtime-request.redacted.json`；终态、D0结果与矩阵携带实际模型、路由版本、请求ID和持久化记录引用。记录缺失是平台 `CASE_INVALID`，不是Agent能力失败；D0导入重新校验记录内容，拒绝错误身份、空/重复ID、符号链接和仅有“verified=true”的伪证明。
- 实际LiteLLM1.92镜像验证发现自定义回调从配置同目录加载，而不是任意Python模块路径。按正式ConfigMap布局修正后，四家身份标识×四种接口共16次HTTP请求全部200、各产生唯一接收记录。使用本地模拟模型服务和 `--network none`，不计入任何智能体实测或模型资格；复现入口 `tests/integration/gateway_proxy_probe.py`，原始报告 `gateway-real-proxy-four-identities.log`。早期失败日志保留。
- 全量回归暴露SQLite WAL/SHM在多进程关闭连接时消失的竞争。仅对可消失的辅助文件忽略FileNotFoundError，主数据库缺失及其他权限/磁盘错误仍抛出；确定性反例与并发测试通过。
- 此轮不修改L0-L4 Prompt、节点评分、默认C0-D6用例集，不访问新集群，不滚动旧Stage2。

### 旧集群已完成的准备与尚未完成的资格

- `4f9ba9e` 的Controller与Agent镜像已成对发布，完整构建记录为 `artifacts/stage2/image-4f9ba9e.json`。BladeAI真实TUI构建成功；非root/只读/无网络容器中四个CLI/入口和blade垫片检查通过，不代表实际智能体工具调用成功。
- Chaos Mesh2.7.3已在旧集群部署，Controller1/1、daemon3/3曾实际就绪。安装/升级等待命令曾超时，不改写为Helm成功退出；镜像拉取和RemoteCluster只读启动RBAC缺口已定位并处理，未注入任何Mesh故障。`controller-bootstrap-rbac.yaml`使启动所需最小授权可复现，故障写权限仍限定otel-demo。
- 已创建Controller/executor/finalizer身份并完成18项授权review；完整运行时身份/隔离资格尚待执行。网关基础ConfigMap/Secret先前已准备，最新回调配置尚未刷新；三个旧Stage2工作负载均未切换为新版本。
- Coroot匿名Admin仍不满足只读身份资格；更改登录方式需用户明确同意，目前未执行。accounting曾出现OOMKilled，不能用Pod Ready代替D0基线资格。
- 下一阶段仍为：代码回归与提交 → 配对新镜像及旧集群准备 → 单项资格/测试；完整范围包括八个D0 Campaign与68格，不缩减为离线测试。

WP12最终代码回归：`wp12-code-gate-final.xml` 共1414项，1405通过、9跳过、0失败/错误，48.608秒；保留7项既有Pydantic弃用警告。通过后冻结代码准备提交，尚未将此次补丁部署到旧Stage2。

### ad07f49发布与旧integration首次切换

WP12代码已提交并推送 `ad07f4955f6eedbb9ee6182f1b27e1ca84c051c2`。配对构建/推送成功，记录 `artifacts/stage2/image-ad07f49.json`。旧集群网关ConfigMap已加入实际回调，独立客户端Secret `resbench-stage2-runtime-integration`只包含网关地址与客户端密钥；未修改主服务/e2e使用的原共享客户端配置。临时明文渲染文件已删除，受保护的源凭据保留。

UTC 2026-09-06 02:55开始仅切换integration；保留tcse-v100-03、integration各数据路径、原PVC和8080端口。切换前26个任务状态文件均为终态，未发现活动Agent；原采集器未记录精确采集时间，因此审计报告明确写未记录，不回填虚构时间。主服务/e2e仍为原版本。

180秒rollout等待超时。新Pod的init成功，Controller和网关Ready，agent-runtime拒绝启动：镜像安装的iptables位于/usr/sbin，但受限PATH不包含该目录。实际容器已验证绝对路径可用；代码改为守护进程仅允许固定三个系统二进制路径，构建期也执行版本检查，不扩展Agent PATH、不禁用出网限制。补丁全量 `firewall-path-code-gate.xml` 为1407通过、9跳过、0失败/错误，50.830秒。当前等待修复镜像重新部署，不能称此次切换成功；真实模型和故障测试仍未开始。

`0492363`路径修复已推送，配对镜像已构建并预拉至旧节点。第二轮启动通过网络设置，但创建cgroup前缀失败；实查节点根目录为root:root、0555，受限daemon未获DAC_OVERRIDE。没有AppArmor拒绝日志，不将其归因于AppArmor。部署改为Kubelet预建并只挂载专用子目录，不改全局cgroup权限、不增daemon能力。另补Unix socket就绪检查：旧模板没有Agent readiness，rollout曾捕捉短暂容器启动而退出0，但后续仍为CrashLoop；不能据此写成功。三模板、重启逻辑与不变量27项定向测试通过，真实部署验证另记。

`6b16736`部署资产修补已发布。Kubelet成功预建了host专用目录，但runc在只读的容器默认/sys/fs/cgroup下创建嵌套挂载点时失败，进程未启动、退出128。最终挂载位置改为容器 `/run/resbench-cgroups`，host仍仅委派原专用子树；daemon严格要求预挂载cgroup v2，不创建普通目录、不兼容旧挂载路径，并从实际委派根/leaf自身读取controller可用性。`cgroup-prefix-code-gate.xml`全量1407通过、9跳过、55.145秒；随后新增4个确定性文件系统模拟反例，最终专项31通过、2个Linux/root跳过。部署成功与实际子进程隔离资格仍需后续独立核实。

`f1efe28`已推送、配对镜像已发布，integration达到3/3、零重启。首次真实AgentExec子进程资格仍失败，记录 `linux-agent-boundary-031745.json`；不能把Ready当成可评测。实际暴露两点：串流把剩余预算而非发送字节计入总量，59字节即被当作截断；另一次独立受限资源组诊断证明cgroup.procs存在、写入自己的子进程PID仍返回ENOENT，Docker private cgroup namespace与host委派范围不一致。诊断只启动3秒无任务子进程，资源组已确认空并删除，没有模型或故障调用。

代码改为计实际发送字节；真实本地子进程/socket回归覆盖短包、多包、恰达及超过上限，临时在测试进程恢复旧错误后4项全部按预期失败，未改动磁盘生产源码。受信任daemon只读挂载host cgroup namespace描述符，启动时仅加入该namespace；每个Agent/Sandbox子进程先加入限额组，再创建自己的private cgroup namespace，最后降权。hostPID/hostNetwork不启用，不增加capabilities、不调整限额；加入失败仍拒绝启动。`cgroup-namespace-code-gate-final.xml`全量1418通过、9跳过、0失败/错误，51.235秒；真实资格待新版部署后复跑。

`db8e03e`已推送并部署旧integration，三容器稳定就绪。真实UID10002子进程的11项基础边界检查和Controller/executor/finalizer的80项实际身份/RBAC检查通过；六个模型网关接口探针均supported。证据与范围详见 `docs/deploy/stage2-old-cluster-qualification-20260906.md`，不升级为四家原生资格或正式扰动结果。

代码沙箱首次实际初始化失败，独立诊断定位到私有mount propagation的EACCES。Docker默认AppArmor明确禁止mount；为本项目增加保留默认proc/sys保护且只放行四类沙箱操作的专用强制策略，在旧tcse-v100-03实际加载成功，不改全局docker-default、不使用unconfined。另修复空环境下相对python3定位，默认固定 `/opt/agent/.venv/bin/python`。`sandbox-startup-code-gate.xml`全量1420通过、9跳过、49.091秒，0失败/错误；待该补丁部署后复跑sandbox。Coroot登录变更再次以非阻塞问题向用户请求确认，目前未修改。

`ed9314f`已提交推送，配对镜像最终构建成功；期间Docker凭据助手曾等待，未中断或重复构建。专用AppArmor策略应用到integration后，实际sandbox以UID10003成功执行、断网、通过Unix broker调用诊断echo并拒绝未授权工具；但新增文件反例发现/tmp和共享目录仍可写，因此 `sqc07933` 仍判失败，不算资格通过。

根因是只读remount只覆盖根文件系统而不覆盖嵌套emptyDir/tmpfs。改为新命名空间内通过libc的mount_setattr递归只读，然后只bind本次临时目录为可写，缺少内核/libc支持即拒绝，不保留不完整旧路径。旧节点实际运行提取自生产代码的初始化函数，确认三处越界写均EROFS、专属临时目录可写；没有扩大AppArmor策略。全量 `sandbox-recursive-mount-full.xml` 为1423通过、9跳过、52.328秒，0失败/错误；真实完整sandbox链路待新镜像复跑。

`5d7a498`已推送、成对镜像已构建并部署integration。实际`sq460f4d`完成UID10003、禁网、临时目录正反写入、诊断echo代理和未授权工具拒绝，6项全部通过且无socket残留。这不是原生Agent/真实MCP资格。

真实sandbox→broker→带Bearer的harness_channel MCP通知/回执检查`smb603cf`暴露剩余SDK属性错误。按mcp-builder检查流程及当前官方SDK v2文档，代理改用is_error/structured_content，不添加v1兼容路径；BladeAI隔离venv仍为受上游约束的MCP1.x，其原生适配字段不改。新增实际SDK CallToolResult类型回归，以及后台异常不泄漏正文、授权调用失败不当越权的socket测试。全量结果和真实复验另行记录。

`bb08326`已推送并部署。`sandbox-mcp-v2-full.xml`全量1427通过、9跳过、49.626秒；实际`smafbfc3` HTTP链路和`sm05a3a8` SSE链路完成UID10003→broker→生产Harness MCP通知读取/回执，未经认证返回401、平台留下通知送达和工具调用记录，socket无残留。`sm76ebe2`另外通过实际BladeAI venv的MCP1.27.0、UID10002走SSE读取并确认新通知，证明协议互通，不证明BladeAI规划器能力。普通Agent边界在当前部署复验通过，记录`linux-agent-boundary-041520.json`。仍未创建故障，也未开始原生智能体或正式评测任务；完整范围未缩减。

### 旧集群WP10金丝雀（UTC04:24–04:28）

在两个临时Pod之间实际注入1000ms NetworkChaos，创建/删除分别使用executor与finalizer。
HTTP延迟中位数1.022ms→2001.251ms→0.913ms，所有请求成功，故障对象删除后先验证恢复再删除临时Pod。初始检查器将空的已同步PodNetworkChaos缓存当作残留而报告failed；原记录保留，后续检查确认spec为空且观测版本一致，两个临时Pod删除后内部缓存也被回收，最终五类故障清单为空，复核结果通过。只证明NetworkChaos引擎链路，不证明完整MCP执行或Agent D8行为。

实跑状态同时暴露后端错误地返回期望phase=Run。现基于真实AllInjected和目标containerRecords归一化Running/Recovering/Completed；只有期望Run、缺记录、目标不符、暂停/删除状态均不能产生Running事实。新增真实夹具及共享核心时间窗联动回归。`mesh-observed-phase-final.xml`全量1439通过、9跳过、48.427秒，0失败/错误。详见计划要求的 `docs/deploy/chaos-mesh-and-coroot-20260905.md`。未改L0-L4提示/评分或默认C0-D6用例。
