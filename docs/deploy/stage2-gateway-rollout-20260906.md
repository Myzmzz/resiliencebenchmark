# 旧集群网关更新与真实模型复验

2026-09-06，代码提交 `6092578`。范围只含旧集群 `coroot-config` 的
`resiliencebenchmark-system`，未访问或部署新集群。

## 已完成

旧网关在处理故意不带 Key 的请求时，因缺少 `prisma` 抛出异常并返回 500；
不是现有模型 Key 配错。已改用同版本官方 LiteLLM 1.92.0 的私库镜像
`1.94.151.57:85/observe/resbench-litellm:1.92.0`，保持现有路由与凭据不变。
使用镜像内实际 CLI `/app/.venv/bin/litellm`，显式监听 `127.0.0.1:4000`，
健康检查改为容器内执行。`python -m litellm` 不能启动该版本，已在部署前排除。

| 服务 | 更新后 Pod | 在线检查 |
|---|---|---|
| integration | `resbench-stage2-integration-789b8db476-mjwwn` | 7 项全部通过 |
| main | `resbench-stage2-797b6bcf88-rv96h` | 7 项全部通过 |
| e2e | `resbench-stage2-e2e-67b4f8764b-m6ms7` | 7 项全部通过 |

检查包括健康接口、网关地址、六个必需别名、匿名请求返回 401、Controller
身份、私有令牌引用，以及实际监听表中仅有回环地址。不是只以 Pod Ready 判定。
只更新网关容器的镜像、命令、参数和探针，未改变三套服务各自的数据路径、
节点、运行配置或 Controller/Agent 镜像；后两者仍为 `e6fd44a`。

更新前确认无活动任务与原生 Agent 子进程。历史数据核对结果：

- integration：2967 份普通文件、510 个链接，更新前后内容和链接目标一致；
  保留 PVC 原位，不提取或跟随历史链接。
- main：339 份文件，更新前后内容一致。
- e2e：临时卷中的 86 份历史文件已备份、恢复到原路径，并逐份核对内容一致；
  未复制旧私有凭据。新 Pod 自动创建的空 `tasks` 目录不含需覆盖的数据。

integration 首次流式归档传输不完整，未用于恢复或验收。随后使用容器内完整
归档及带续传的复制完成验证；保留失败记录，不把失败归档宣称为有效备份。

## 验证证据

证据目录：`artifacts/remediation/20260905/`。

- `gateway-192-full.xml`：1492 通过、10 跳过、0 失败，55.273 秒。
- `gateway-auth-official-image-verified.log`：真实代理镜像、容器内模拟上游，
  无外部网络和模型调用；16 个授权协议/审计请求通过，匿名拒绝通过，未授权
  请求到达模拟上游的数量为 0。无虚拟 Key 数据库时，非 master Key 的拒绝为
  400，与匿名 401 分开检查，不接受 500。
- `integration-gateway-192-verification.json`、`main-gateway-192-verification.json`、
  `e2e-gateway-192-verification.json`：各服务实际在线检查。
- 三份 `*-gateway-192-preservation.json`：上述历史记录保留证据。
- `services-after-gateway-192.json`：更新后 Deployment 规格记录。
- `gateway-192-six-model-probe.json`：通过 integration 新网关执行的真实模型
  探针，六个别名均为 `supported`，`issues=[]`。检查项按各模型配置执行，
  不是四个原生 Harness 的完整交互资格，也不是故障注入或正式评测成绩。

## 尚未完成

网关缺陷已关闭。C0 的后续工作是基础通道资格实跑、能力记录发布及任务 API
准入接线；D0 受控注入资格与正式计分仍需独立证据，不能由网关探针或只读
smoke 替代。BladeAI 仍需计划 WP8 的真实全链资格。Coroot Viewer 准备与
WP11 替代通道资格继续服务 D7/D8，不应成为 C0 的额外依赖。

正式任务仍由用户通过原接口按单智能体、单扰动逐项执行。本次没有启动正式
扰动任务，也没有把此前三个只读闭环升级为正式通过。
