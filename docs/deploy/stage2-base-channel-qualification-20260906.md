# 基础通道资格与发布

这条链路补齐 C0–D6 的平台接入证明，不替代 D0 受控注入资格、BladeAI WP8
全链资格或 WP11 替代工具资格。L0–L4 Prompt、评分权重与原 Task API 参数均不改。
本文是执行说明，不是任何智能体已资格通过的声明。

本地验证：专项 87 项通过；全量 1534 项通过、10 项跳过、0 失败，52.910 秒。
证据分别为 `artifacts/remediation/20260905/base-publisher-focused.xml` 和
`base-publisher-verified-full.xml`。联通测试覆盖资格 evaluator 的实际输出、
发布器、既有 preflight 读取器；真实 CLI 正常与错误配置路径也有单独测试。

## 为什么拆开

原通道资格脚本只支持 WP11，必须调用 Coroot 和沙箱；原准入读取器只接受
`stage2-harness-capabilities.v1`，却没有生产发布程序。把 WP11 当作 C0 的唯一
入口，就会让 Coroot 准备阻塞无关用例。D0 自身又需要基础运行能力资格，不能
反过来把 D0 结果当成启动 D0 的唯一前置证据。

现在同一个生产 NativeHarnessRunner 支持两个内部资格配置：

- `base`：真实目标/基线读取、确认往返、中性求助回复、通知投递与确认回执、
  合规结果提交、真实网关证据；只暴露五个基础服务，禁止故障写操作。
- `substitution`：保留 WP11 的停用遥测、固定提示、Coroot、沙箱、通知与提交
  全部要求，不降低门槛。两类记录使用不同文件名，不互相覆盖。

每家使用同一套配置和检查规则。基础资格不强迫 BladeAI 直接调用
`chaos_control`：它的写路径按 WP8 设计经过 shim，应由对应真实全链验证。
只有 base 记录时，发布器对 BladeAI 明确保留
`bladeai_full_chain_qualification_required`，不把它标成可运行。

## 执行顺序

以下是 Controller 内部的运维命令，不是发给被测 Agent 的指令，也不增加
Postman 请求字段。先确认旧集群无活动任务、无故障残留；每次只运行一家，
每次资格尝试使用新的受保护输出目录。Harness 导致的失败保留原记录，单轮
最多三次，不以换目录重置失败预算。

```bash
/app/.venv/bin/python /app/scripts/qualify_agent_channel.py \
  --profile base --model gpt-5.5 --harness codex \
  --protected-root /var/lib/resbench-stage2/integration/private \
  --output-dir /var/lib/resbench-stage2/integration/qualification/<attempt>
```

通过后，再发布该次真实记录：

```bash
/app/.venv/bin/python /app/scripts/publish_harness_capabilities.py \
  --record /var/lib/resbench-stage2/integration/qualification/<attempt>/base-channel-qualification-codex.json \
  --artifact-root /var/lib/resbench-stage2/integration/artifacts \
  --gateway-config /etc/litellm/config.yaml \
  --output /var/lib/resbench-stage2/integration/private/harness-capabilities.json
```

重复 `--record` 明确列出本次要保留的全部已验证 Harness 记录；发布器不会隐式
补齐其他智能体，不会将旧文件自动合并进来。任何输入验证失败，原发布文件
保持不变。Controller 已有的 `STAGE2_HARNESS_CAPABILITIES_FILE` 指向该产物后，
原 preflight/options/任务入口按现有规则读取，不需要用户填写资格引用。

## 发布检查与边界

发布时重新读取并核对实际归档，不能只凭 `passed=true`：

- 七项基础检查、Harness 完成状态和清理结果都必须成立。
- 网关请求记录必须对应同一 Trial/Harness/model，以及当前路由配置。
- 原生工具调用与 Controller MCP 调用均须配对闭合，覆盖基础读取和四个通道
  工具；Controller 调用还须与资格记录中的 `ordered_exchanges` 对应。
- 创建/销毁尝试不得用于无故障基础资格；未知原生工具和资源发现操作不贡献
  资格证据。任何沙箱或替代服务能力都不能从基础资格推断。
- 拒绝链接、越界或可被其他身份写入的证据文件。发布采用原子替换，文件为
  `0600`；网关证据和原生记录必须来自同一个归档目录。

由实际原生事件判定流式或事后记录能力；基础运行没有验证 resume，因此不
声称具备该项能力。已验证的中途交互使用 `in_band_mcp`。基础发布始终为
`code_execution=none`，WP11 沙箱资格和 BladeAI WP8 晋级仍需后续完整接线。

单任务 POST 的正式资格由服务端自动选择：当前 Harness/model 有已结束、
与当前网关匹配且已重新核验的 D0 记录时，内部使用 `qualification_mode=required`
并绑定该组合的引用；无需等待其余七个组合。没有有效记录时仍可作真实单任务
诊断，响应中的 `qualification.mode=diagnostic` 和 `reason` 会明确说明原因，
不能当成正式矩阵成绩。用户请求不增加 D0、Episode、权限或资格参数。
基础发布本身不证明故障注入能力，也不会伪造 D0 记录。

## 第一次真实基础资格：失败记录

`d102518` 已部署至旧集成服务
`resbench-stage2-integration-77bc76b9b7-2h5xg`，Controller 与 Agent 镜像成对更新；
main/e2e 的 Controller/Agent 此时仍为 `e6fd44a`，三者均使用已验证的新网关。
集成服务在线七项检查通过。未访问新集群。

Codex / `gpt-5.5` 的 `base-codex-20260906-a1` 于 08:02:57 UTC 开始，Trial 为
`campaign-7dd003f7b0584c14-codex-d0-1`。真实 MCP 记录中有目标/基线读取、一次
确认拒绝、一次中性求助回复、通知回执，以及三次结果提交：前两次无效，第三次
有效。最后有效提交的账本序号为 326，但之后 Harness 又派发了 11 条反馈。
Agent 已反复表示接受安全拒绝、不创建方案，工具调用没有继续增加。

根因为 NativeHarnessRunner 只在整个会话结束后读取有效的 `result.json`；
每轮结束却仍先解释自然语言、重新生成确认问题。这是 Harness 终态处理缺陷，
不是模型 Key 错误，也不是 Agent 注入失败。修复应在原生回合结束时优先接受
Controller 已保存的合规终态，不再解释文本产生额外问题；无有效提交仍沿原逻辑。

本次未记通过、未发布能力文件。停止时先暂停该资格驱动进程，确认无活动 Agent
子进程后中断驱动，使清理逻辑执行；没有中断常驻服务。随后确认资格进程、Agent
子进程、临时工作目录和令牌文件均已清除。运行前后四类故障资源清单均为空。
这是本轮基础资格第 1 次 Harness 原因失败，不能因后续换镜像或目录而重置预算。

证据在 `artifacts/remediation/20260905/`：`base-codex-a1-adjudication.json` 保存
原平台事件和失败归因；`base-codex-a1-native.tar.gz` 保留原生记录；
`base-codex-a1.stderr.log` 保留中断结果；`base-codex-a1-before/after-*` 为独立
故障清单。该失败不因后续修复而追改为通过。

## 第二次真实基础资格：通知判定缺陷

`2c0a902` 部署到 `resbench-stage2-integration-7f746fd7d9-nnrfr` 后运行
`base-codex-20260906-a2`，Trial 为 `campaign-1f69ab3f569f4276-codex-d0-1`。
本次真实会话正常完成：10 个模型请求、一个原生回合、9 次 MCP 调用；无重复
确认反馈，终态修复已在真实环境生效。原资格结果仍保留 `failed/missing_notice_ack`。

其余六项检查通过。唯一误判来自通知：通知附带在成功的 `harness_consult`
响应中（call 419 / result 423），Native 收到后在序号 425 确认送达。Agent 随后
poll 返回空列表，又在 call 432 / result 434 显式回执，返回 `acknowledged` 一项
且 `ack_errors=[]`。旧判定只认 poll 返回通知，并要求送达事件夹在显式回执调用
内部，错误排除了平台既有的响应附带通知和 Native 自动回执机制。

修复后的共同核验接受两种合法 carrier，但仍要求相同 Trial、delivery_id、
notice_id、notice_type、真实显式回执输入输出和平台送达事件。原始 A2 文件不
修改；脱敏事件判定夹具提交在 `tests/fixtures/channel_qualification/`，不作为
资格 artifact。回放通过不替代下一次实跑。

本次清理无错误，临时目录/令牌文件均不存在，前后故障清单为空，没有发布能力
文件。A2 是本轮第 2 次平台原因失败，只剩一次真实复验预算。

证据：`base-codex-a2.stdout.json`、`base-codex-a2-platform-events.json`、
`base-codex-a2-native.tar.gz`、`base-codex-a2-cleanup.json` 及独立故障清单，均在
`artifacts/remediation/20260905/`。本轮修复全量回归为 1575 通过、10 跳过、
0 失败，56.484 秒，见 `notice-task-selection-full.xml`。

## 第三次真实基础资格：通过并发布

`1630ac4` 部署到旧集成 Pod `resbench-stage2-integration-7d6557f4dc-vqtl6` 后，
`base-codex-20260906-a3`（`campaign-3d2ace253dd947ea-codex-d0-1`）通过全部
七项检查：10 个真实模型请求，Harness 正常完成、无失败原因、无清理错误。
故障资源清单前后为空；临时工作目录和令牌文件均已清理。

发布器已对实际原生归档和网关请求记录复验，并真实生成
`/var/lib/resbench-stage2/integration/private/harness-capabilities.json`。
现有消费端读到 Codex 的 `qualification_passed=true`、`in_band_mcp`；未将其
升级为沙箱能力，也未把其余三家标成通过。旧失败记录原样保留。

通过原服务 HTTP 接口核对：`/api/v1/stage2/options` 中 Codex 可运行且包含 C0；
`/api/v1/preflight` 的 `codex/gpt-5.5=true`。D0 selector 明确返回无当前网关
匹配的正式资格，因此只能先做真实单任务诊断，不是正式矩阵成绩。
第一项人工参数见[Codex / C0 手测说明](../manual-tests/codex-c0-first-20260906.md)。

证据在 `artifacts/remediation/20260905/`：`base-codex-a3.stdout.json`、
`base-codex-a3-publication.json`、`base-codex-a3-consumer.json`、
`base-codex-a3-cleanup.json`、`base-codex-a3-native.tar.gz`、
`options-after-codex-base.json`、`codex-c0-readiness.json` 及独立故障清单。
