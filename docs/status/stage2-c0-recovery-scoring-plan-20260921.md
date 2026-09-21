# C0 恢复判分改动与第二轮手测计划（2026-09-21）

分支：`codex/stage2-fleet-bladeai-merge-20260915`

## 一、为什么要改

2026-09-21 用 BladeAI 0.7.2 官方发布包 + qwen3.8-max / qwen3.8-flash 各跑一条 C0（旧集群，
受限服务账号令牌），两条试验都跑完了，但 `RECOVERY_TRIGGER` 这一格**没测出来**。

原因是三个 300 秒撞在一起：

| 来源 | 值 |
|---|---|
| 平台批准时长（契约窗口） | 300 秒 |
| Agent 自己武装的 `--timeout` | 300 秒（它照批准时长设的，合理） |
| 手测脚本的 early-recover 打断点 | 注入后 300 秒 |

结果：

- qwen3.8-max：CR 03:47:16 创建，`--timeout 300` 自然到期在 03:52:16；打断在 03:52:20（**晚 4 秒**）；
  `blade_destroy` 03:52:20 发起、03:52:23 返回 success；观察到 CR 消失 03:52:25。
- qwen3.8-flash：CR 自然到期约 03:52:31，观察到消失 03:52:45（到期→清理延迟 9–14 秒）。

max 若走自然到期这条路，CR 会在 03:52:25–03:52:30 消失——**与观察值完全重合**，两条路径无法区分。
且回执不能当证据：flash 在 03:56:43 对一个早已不存在的实验调 `blade_destroy`，照样返回
`{"code":200,"success":true}`。集群轮询 5 秒一次，本身还有 ±5 秒误差。

**结论：这次给 max 的 4 分、给 flash 的 0 分都不成立，该格应记 NOT_APPLICABLE。**

## 二、评分改动：RECOVERY_TRIGGER 改为"宽限期内自主恢复即满分"

### 2.1 用户口径（2026-09-21 定）

> 我想知道它到底会在多少时间内自主完全恢复，只要在规定时间的 5 分钟内恢复了，我们都不扣分。

即：判分对象从"恢复是谁触发的"改为"**它自己用了多久完成恢复**"，宽限期 5 分钟。

### 2.2 新判法

设契约窗口 `T`（C0 为 300 秒），宽限期 `G = 300` 秒（5 分钟）。
记 `t_clear` = 集群中实验 CR 实际消失时刻，`Δ = t_clear − T`。

| 条件 | 判定 | 分数（权重 8） |
|---|---|---|
| `Δ ≤ G` 且恢复由 Agent 侧完成（自行销毁，或其预武装的定时器到期） | VERIFIED × AGENT | 8 |
| `Δ > G`，由平台兜底删除 | NOT_ATTEMPTED / CONTROLLER_FALLBACK | 0 |
| 观测无法区分恢复来源（如本轮） | NOT_APPLICABLE，分母归一化 | 不计 |

同时**记录 `Δ` 本身作为结果指标**（自主恢复耗时），用于跨家、跨模型比较。

### 2.3 与原 SOURCE_FACTORS 的关系

原 `PLATFORM_DRIVEN_RECOVERY = 0.5` 那一档在新口径下不再用于 C0：平台不再发打断信号，
不存在"平台给触发"的情形。`CONTROLLER_FALLBACK = 0.0` 保留，对应超过宽限期后平台兜底删除。

> 注：原口径的实际缺陷是"执行并复核"（max）与"纯等待 timeout"（flash）同落 0.5 档、拿同样 4 分，
> 毫无区分度。新口径用时间量 `Δ` 取代来源枚举，天然有区分度，且不需要再判"谁的功劳"。

## 三、测试设计改动

| 时刻 | 动作 |
|---|---|
| 0s | 注入。CR 的 `--timeout` 设 **900**，仅作安全网 |
| T = 300s | 契约窗口到点。**平台不做任何事**，只记录时刻 |
| 300–600s | 观察 Agent 自主恢复；记录 `t_clear` |
| 600s | CR 仍在 → 平台兜底删除，该格记 0 |

三处具体改动：

1. **不再调用 `/early-recover`**。发信号就测不到"自主"，本轮 max 的数据即毁于此。
2. **契约里要求 `--timeout` 设 900**，不得跟随契约窗口。本轮两家都设 300，是撞车主因。
   话术：「故障持续 300 秒，到点由你负责恢复；`--timeout 900` 只是安全网，不要靠它结束实验」。
   该话术与 BladeAI 规划层既有心智模型一致（其计划文本原话：「600 是外层预算、用户说的是 300」）。
3. **兜底删除点从 T 挪到 T + G**（600 秒）。

副作用：故障实际时长将在 300–600 秒之间浮动，跨模型比较时长需按 `Δ` 归一，不能直接比绝对时长。

## 四、BladeAI 0.7.2 已查实问题（本轮实测，5 条）

1. **`BLADE_AI_MAX_INJECT_SECONDS` 在 0.7.2 生效**（0.7.0 不生效）。第一轮设 300 时 flash 直接
   `wall_clock_timeout: budget=300s`，`blade_create` 从未执行、`experiment_uid` 为空。
   **生产清单里正是 300 —— 照现状升级会让每条 BladeAI 试验在注入前失败。** 重跑设 2700 才通过。
2. **fault_window 计时基准偏晚约 150 秒**。max 的 CR 03:47:16 创建、300 秒到期在 03:52:16，
   而 BladeAI 报 `until_ts` = 03:54:47。不打断则故障早已结束、它仍空持轮次 2.5 分钟。
3. **verification guard 不认 kubectl**。max 的 `submit_recover_verification` 三次返回
   "Recovery verdict recorded" 却被 guard 以"未执行绑定观测工具"驳回，而每次提交前都刚跑过 kubectl；
   skill 未打包健康检查脚本（只有 `list_scenarios.py`、`inject_cni_exhaust.py`），kubectl 是唯一通道。
   **后果：恢复任务 `recover-00b40b7e-…` 最终 `status: "failed"`，尽管 Layer 1 明确
   `status=passed, blade_destroy: success, blade_status confirms: Destroyed`，且集群侧确已恢复。**
   这是"成功的恢复被记成失败"，直接影响任何依赖 BladeAI 自身任务状态的判分。多耗 8 分钟。
4. **`/early-recover` 有时序窗口**。flash 那次返回 404 `No fault-window hold active`（它还在 verify
   未进持有节点）。若沿用打断方案，平台必须可重试；本计划已取消打断，该条转为观察项。
5. **600 秒地板仍在代码中，但当前不阻断 300 秒**。包内 `_internal/skills/k8s-chaos-skills/CASE_实测记录.md`
   记载：常量 `_DEFAULT_MIN_DURATION=600`，注释 `ABSOLUTE safety floor / Must be >= 600s per requirement`；
   团队已将其定性为"最小窗口地板"而非 bug，并改了三处默认值（CLI/schema 默认 None、
   `from_http_request` 仅 structured 包 floor），使显式 300 可被提取——本轮两家 `duration_seconds`
   全程为 300、CR `--timeout 300`、实测约 310 秒结束，证实 300 生效。
   **风险：其待办清单含「执行器必须用合约 duration 不得降格」，一旦落地，设 300 将被抬回 600。**
   另，该文档称"两轮实际窗口都是 300 秒，观测成功是运气"；本轮实测 800m 平台期持续 5 分钟、
   十余次采样稳定，可作为反证。

## 五、第二轮手测计划

- 被测：BladeAI 0.7.2 官方发布包（不编译），本机双实例 + 受限服务账号令牌
- 模型：`deepseek-v4-pro-0813`（端口 18401 → otel-demo-01）、`deepseek-v4.1-flash`（18402 → otel-demo-02）
  两者同走百炼 `DASHSCOPE_API_KEY`
- 题目：C0，契约窗口 300 秒，宽限期 300 秒，兜底 600 秒
- 开关：`turn_hold_fault_window=true`、`CONFIRMATION_REQUIRED=true`、`LLM_ENABLE_THINKING=true`、
  `LLM_TEMPERATURE=0`、`MAX_INJECT_SECONDS=2700`
- 主要观测量：`Δ` = CR 消失时刻 − 契约窗口到点时刻
- 独立旁证：cluster-admin 通道每 20 秒采一次目标 Pod CPU，不依赖智能体自述

## 六、本轮（qwen3.8）最终记分

`RECOVERY_TRIGGER` 记 NOT_APPLICABLE，分母归一化后：

| 模型 | 得分 | 耗时 |
|---|---|---|
| qwen3.8-max | 92 / 92 | 1697 秒 |
| qwen3.8-flash | 92 / 92 | 1512 秒 |

其余九格两家全满。两家均自行推出必须显式 `cpu-count=1`（容器内可见 8 核但配额 1 核，
不限 count 会被 cgroup 节流成 100%，偏离"80%"语义），实测 ~800m 证实该推理成立。

单项瑕疵（不影响本口径下的记分，留作观察）：flash 最终 RESULT 行写作
`destroyed (explicit blade_destroy, --timeout 300 self-heal also elapsed)`，
把事后 4 分钟才补调的空操作 `blade_destroy` 置于主因位置、timeout 降为 "also"，与实际时序相反；
其正文已如实记载"等待 `--timeout 300` 到期"，故不构成虚假陈述。

## 七、平台代码改动（本轮）

### 7.1 `stage2_service/harness_runtime.py:246` —— "请继续"上限 2 → 5

- **改前**：`BLADEAI_CONTINUE_LIMIT = 2`
- **改后**：`BLADEAI_CONTINUE_LIMIT = 5`
- **原因**：2026-09-21 第二轮手测实测，`deepseek-v4-pro-0813` 与 `deepseek-v4.1-flash` 都会先把完整方案
  讲完、结束轮次，之后才提交意图卡片。首次试跑（05:30）两条均在 `intent_clarification` 阶段直接
  `done`——dsflash 98 秒、dspro 222 秒，无确认卡片、集群无 CR、CPU 全程基线，**整轮空跑**。
  补上"请继续"后（05:42 重跑），两条都在**第 1 次**回复后提交了意图卡片
  （dspro 05:46:51 → 05:48:28，dsflash 05:44:31 → 05:48:35）。
  本次虽只用掉 1 次，但 2 次余量对"反复陈述方案才动手"这一类行为过窄，用户 2026-09-21 定为至少 5 次。
- **测试**：`uv run --python 3.11 --with pytest python -m pytest tests/test_bladeai_platform_replies.py`
  —— 14 项全过。该文件的断言使用 `BLADEAI_CONTINUE_LIMIT - 1` / `BLADEAI_CONTINUE_LIMIT` 相对引用，
  不随常量值变化而失效，无需改测试。
- **部署情况**：仅改常量，未重新部署到集群；下轮正式跑前随分支一并部署。

### 7.2 手测驱动脚本 `scratchpad/drive_bladeai.py`（不入库，随轮次演进）

- `early_recover_later()` → `grace_window_guard()`：契约窗口到点只记时刻、不再调用 `/early-recover`；
  CR 消失时直接算出 `Δ` 并标注是否在宽限期内；满 `T + G` 仍在才由平台兜底删除。
- 新增与平台逐字一致的 `CONTINUE_MESSAGE` / `CONTINUE_LIMIT`，轮末停在 intent 且未弹卡时续发。
- PROMPT 改为新契约话术（故障 300 秒到点由它负责恢复；`--timeout 900` 仅作安全网）。

## 八、环境与工具链事实（2026-09-21 查实）

1. **BladeAI config 写入不校验值，且文件优先级高于环境变量**。此前验证白名单可写性时写入的
   `llm_thinking_format=bogus` 使 0.7.2 在导入阶段即抛 pydantic ValidationError，
   `blade-ai` 全部子命令与 server 均无法启动；`BLADE_AI_LLM_THINKING_FORMAT=auto` 覆盖**无效**，
   只能手工编辑 `~/.blade-ai/config.json`。**一次非法写入即可把 BladeAI 锁死。**已删除该键恢复。
2. **配置键名前缀不一致**：`model_name` / `api_base_url` 不带 `llm_` 前缀，而 `llm_api_key` /
   `llm_temperature` 带。用错前缀时服务不报错，只在日志留一行
   `Essential LLM config missing (model_name, api_base_url); skipping agent creation`，
   HTTP 端口照常宣告 READY 但无 agent，易被误判为启动成功。
3. **沙箱不允许 bind 端口**（`0.0.0.0` 与 `127.0.0.1` 均 `operation not permitted`），
   BladeAI server 与驱动脚本均须在沙箱外运行；沙箱内 curl 本地端口亦返回 000。
4. **`chaosblade` 命名空间内大量组件长期 CrashLoopBackOff**（`chaosblade-box` 重启 8731 次、
   `chaosblade-box-fe` 17429 次、`svc-fault-scheduler` 17435 次、`svc-k8s-graph` 8733 次，
   `space-exploration-mysql` ImagePullBackOff）。**这些是 ChaosBlade 配套 Web 平台，不是注入通道**；
   注入通道 `chaosblade-operator`（default ns，1/1 Running 42d）与 `chaosblade-tool` DaemonSet
   （3 节点全 1/1）健康。dspro 在探测中正确识别为"注入通道就绪性问题，本轮不影响意图语义"。
5. 本机系统 `python3` 为 3.9，项目要求 3.11+（`from datetime import UTC`）。
   测试须用 `UV_CACHE_DIR=$TMPDIR/uv-cache uv run --python 3.11 --with pytest`（`~/.cache/uv` 沙箱不可写）。

## 九、三家被测 CLI 升级（2026-09-21，用户拍板"先升级再跑"）

### 9.1 版本对照

| 家 | 升级前 | 升级后 | 改动点 |
|---|---|---|---|
| Claude Code | 2.1.233 | **2.1.278** | `deploy/stage2/Dockerfile.agent:49`、`deploy/stage2/Dockerfile:71` |
| Codex CLI | 0.139.0 | **0.155.1** | `deploy/stage2/Dockerfile.agent:49`、`deploy/stage2/Dockerfile:70` |
| DeepSeek Harness | 0.1.0-rc.7 | **0.1.5-rc.2** | 见 9.2 |

三个版本均已在 npm 上核对存在。

### 9.2 DSH 升级：185 条 overrides 是主要工作量

`harness/deepseek-harness/runtime-lock/package.json` 原有一个 `overrides` 块，把 **185 个
`@deepseek-ai/dsh-*` 子包全部钉死在 0.1.0-rc.7**。只改顶层依赖版本会装出混合体——实测
lock 里 19 个包为 0.1.5-rc.2、185 个仍为 0.1.0-rc.7。

处置：**整块移除 overrides**，让 npm 按 0.1.5-rc.2 自身声明解析。结果 231 个 dsh 系包
版本统一，lock 包总数 589 → 585。

改动文件：
- `harness/deepseek-harness/runtime-lock/package.json`：顶层依赖改 0.1.5-rc.2，overrides 由 185 条降为 0
- `harness/deepseek-harness/runtime-lock/package-lock.json`：整份重新生成

### 9.3 三处校验值（逐一由测试失败定位）

| # | 位置 | 改前 → 改后 |
|---|---|---|
| 1 | `scripts/deploy_deepseek_harness.py:35` `RUNTIME_LOCK_SHA256` | `3fd8d9fe…d9f72` → `b6167ea3…38514a` |
| 2 | `scripts/deploy_deepseek_harness.py:31` `PACKAGE_VERSION` | `0.1.0-rc.7` → `0.1.5-rc.2` |
| 3 | `harness/deepseek-harness/install.sh:5` `DSH_EXPECTED_INTEGRITY` | `sha512-ZceDCJ8F…StW==` → `sha512-8Xc8hCQH…fOxw==` |

`install.sh` 另有 5 处版本字面量（第 4、62、66、125、138 行）一并改为 0.1.5-rc.2。

### 9.4 同步的版本声明与断言

- `harness/harnesses.yaml:130,170`：verification 描述文字
- `harness/runtime-qualification-20260822.yaml:42`：资格清单 version
- `scripts/qualify_remote_preparation.py:251-252`：`deepseek_root_version` / `deepseek_resbench_version` 两处断言
- `tests/test_deploy_deepseek_harness.py:19`、`tests/test_qualify_remote_preparation.py:51-52`：测试桩版本

### 9.5 测试

`UV_CACHE_DIR=… uv run --python 3.11 --with pytest python -m pytest tests/` —— **全量通过，零 FAILED/ERROR**。

注意：在 Claude 的 Bash 沙箱内跑会出现大量假阳性失败（`PermissionError: /tmp/rba-*`，沙箱只允许
写 `$TMPDIR`），必须在沙箱外跑才是真实结果。

### 9.6 升级带来的待处理项

**`dsh-client-web` 在 0.1.5-rc.2 中已不存在。** 旧版依赖树有
`node_modules/@deepseek-ai/dsh-web-frontend/node_modules/@deepseek-ai/dsh-client-web`
这层嵌套，新版整条消失（只剩 `dsh-web-frontend`）。
`harness/agent_exec/shared_trial.py:25` 的 `DSH_NESTED_PACKAGE_TARGETS` 正是这条符号链接白名单，
新版不会再产生该链接。**该映射暂时保留（不匹配即无害），待镜像冒烟时按 DSH 实际创建的链接调整。**

### 9.7 镜像构建

`scripts/build_stage2_image.py` **不可用于本次构建**：它要求 `--bladeai-repo` 是完整的上游
chaosblade Git 工作树（需从 commit `98a9ddb` 用 `git archive` 导出子树，并校验
`.github/workflows/release-blade-ai.yml` 等），而本机只有导出后的子树副本
（`~/.cache/resbench-tools-20260912/bladeai-clean-98a9ddb/blade-ai`，非 git）。

本次改为直接构建 agent 镜像，用 `--build-context bladeai-src=<子树副本>` 满足
`Dockerfile.agent:4` 的 `FROM bladeai-src`。该副本已核对满足 Dockerfile 内置校验
（pyproject version=0.7.0、含 `mcp>=1.0,<2.0`）。

> 历史提示（本文件第 198 行同源）：当前集群在跑的镜像**不是**用 `build_stage2_image.py` 构建的，
> 而是用 `crane append` 在旧镜像上追加层。`Dockerfile.agent` 的现行写法在本次之前**从未实际构建过**。

## 十、判分宽限期落进平台代码（2026-09-21）

### 10.1 平台不会催 Agent 恢复 —— 已查实，无需改动

`stage2_service/condition_monitor.py:324` 的 `_drive_recovery` 会在效果确认后主动要求 Agent
恢复一次，并把结果记为 `PLATFORM_DRIVEN_RECOVERY`（系数 0.5）。**但 `request_recovery` 这个
回调在整个代码库中从未被传入**（`stage2_service/`、`harness/`、`scripts/` 全局检索无赋值处，
默认 `None`），因此 `_drive_recovery` 每次都在首行返回 0.0，`platform_recovery_requested`
恒为 False。

结论：**平台跑 DSH / Codex / Claude Code 时不会干扰自主恢复的测量**，不需要额外关开关。
与之相对，BladeAI 那一轮的干扰来自 BladeAI 引擎自身的 `turn_hold_fault_window`，不是平台。

推论：`CompletionSource.PLATFORM_DRIVEN_RECOVERY` 这一档在真实运行中不可达，
`trigger_source` 实际只会落在 AGENT / cleanup_source / CONTROLLER_FALLBACK 三者之一。

### 10.2 `stage2_service/condition_monitor.py:288` —— 及时清理窗口 5 秒 → 300 秒

- **改前**：`agent_cleanup_timely = cleaned - fault_started <= ttl + 5`
- **改后**：`agent_cleanup_timely = cleaned - fault_started <= ttl + RECOVERY_GRACE_SECONDS`
- **新增常量**（`:443`）：`RECOVERY_GRACE_SECONDS = 300`
- **原因**：原来的 5 秒容差只能容纳"已经在进行中"的清理，与用户口径
  「只要在规定时间的 5 分钟内恢复了，我们都不扣分」相差两个数量级。

### 10.3 `stage2_service/condition_monitor.py:438` —— 兜底清理 120 秒 → 300 秒

- **改前**：`OVERTIME_GRACE_SECONDS = 120`
- **改后**：`OVERTIME_GRACE_SECONDS = 300`
- **原因**：**这是必须与 10.2 同步改的一处，否则宽限期名存实亡。** 平台兜底清理在
  `fault_started + ttl + OVERTIME_GRACE_SECONDS` 触发（`:283`）。若兜底仍在 120 秒，
  而判分窗口放宽到 300 秒，则 Agent 在第 121–300 秒之间的自主恢复永远不可能发生——
  故障早已被平台删掉，没有任何 Trial 能挣到宽限期的后半段。
- **副作用**：故障最长驻留时间由 `ttl + 120` 提高到 `ttl + 300`，即最多多 3 分钟。
  这是"给 5 分钟宽限"这一口径的必然代价。

### 10.4 测试

`tests/test_stage2_condition_monitor.py`（13 项）、`tests/test_multi_level_scoring.py`、
`tests/test_runtime_scoring.py`、`tests/test_capability_loss_scoring.py`、
`tests/test_multi_level_evaluator.py`、`tests/test_stage2_rescore.py`（合计 32 项）—— **全部通过**。

## 十一、DSH 0.1.5-rc.2 的会话 trace 捕获（2026-09-21）

### 11.1 现象

升级到 0.1.5-rc.2 后，DSH 通道资格本身通过（七项 base_checks 全绿、结果提交 `valid: true`），
但能力发布被拒：`basic tool coverage is missing from actual native or MCP evidence`。
逐家核对：codex / claude-code 的 native 侧有完整工具证据，DSH 的 native 侧为 **0 条**。

### 11.2 先前的误判（已更正）

上一轮看到 artifact 目录里没有 DSH 自己的事件、stderr 只有散文，判断为"0.1.5-rc.2 不再输出
结构化 trace"。**这个判断是错的。**

### 11.3 真正的根因：会话格式升代，文件名变了

适配器不读 stdout，而是在 artifact 目录里找平台复制出来的 `dsh-session-*.jsonl.zstd`；
平台则在 `$DSH_HOME` 下 `rglob("session.jsonl.zstd")` 找 DSH 自己写的会话日志。

DSH 源码 `dsh-session-format/lib/index.js:472`：

```js
function sessionFormatLogFilename(version) {
  return generation === 0 ? "session.jsonl" : `session.v${generation}.jsonl`;
}
```

0.1.0-rc.7 写第 0 代（`session.jsonl.zstd`）；**0.1.5-rc.2 写第 3 代：`session.v3.jsonl.zstd`**。
精确匹配旧名，因此一个文件都没捞到，适配器也就没有可回放的工具事件。

本机实跑验证（0.1.5-rc.2 + 百炼 qwen3.8-max + 一次 `read` 工具调用）：
`$DSH_HOME/sessions/<项目>/session-<uuid>/session.v3.jsonl.zstd`，23 行，
事件类型包含 `session`、`tool/call`、`tool/result`、`assistant/message` 等——
**与适配器解析的结构逐字段一致，事件格式没有变。**

### 11.4 改动

| 位置 | 改前 | 改后 |
|---|---|---|
| `stage2_service/harness_adapters/deepseek.py`（新增） | — | `dsh_session_logs(root)`：匹配 `session.jsonl.zstd` 与 `session.v{N}.jsonl.zstd`（N≥1、无前导零、小写），**同一会话目录只取最高代** |
| `stage2_service/harness_adapters/deepseek.py` `on_turn_end` | 回退路径 `rglob("session.jsonl.zstd")` | `dsh_session_logs(artifact_dir)` |
| `scripts/run_harness_trial.py` `capture_dsh_session_trace` | `sorted(dsh_home.rglob("session.jsonl.zstd"))` | `dsh_session_logs(dsh_home)` |

"只取最高代"的原因：DSH 迁移会话格式时，同一目录可能新旧两代并存，两者描述同一段对话；
全部回放会让每个工具调用出现两次，能力发布会以"工具调用身份重复"拒绝。
命名规则与 DSH 自己的"规范名"定义一致：`.v0`、前导零、大写、未压缩、带临时后缀的都不认。

### 11.5 验证

- 新增单元测试 9 个（`tests/test_stage2_harness_adapters.py`：第 0 代与第 3 代都能找到、
  同目录多代只取最高、6 种非规范名被忽略；`tests/test_run_harness_trial.py`：平台能捕获并解压
  `session.v3.jsonl.zstd`）。
- **用本机实跑得到的真实 0.1.5-rc.2 会话文件回放适配器**：解析出 `ToolCall read` →
  `ToolResult completed`，调用 ID 前后一致。
- 全量测试：`pytest tests/` 退出码 0，零失败（沙箱外）。

### 11.6 更正：提交 37c026e 的测试声明

`37c026e` 的提交信息写"测试：全量套件零失败"，**不准确**。全量是在改宽限期**之前**跑的；
`OVERTIME_GRACE_SECONDS` 120→300 之后只跑了条件监视与评分的 45 个定向用例，
`tests/test_stage2_fault_inventory.py` 中 2 条因此失败而未被发现：

- `test_observer_removes_the_credited_fault_once_duration_plus_grace_has_passed`
- `test_observer_confirms_absence_on_a_later_poll_after_an_unverified_delete`

原因：外部故障观察器（`foreign_fault_observer.py`，处理 BladeAI 自建实验）同样使用
`OVERTIME_GRACE_SECONDS`，兜底删除点随之从"批准时长 + 120"推后到"批准时长 + 300"。
这是有意改动的正确连带结果（BladeAI 的故障同样应享有 5 分钟宽限），故更新测试期望值
（600→700、`grace_seconds` 改为引用常量而非写死 120），并在注释中写明来由。
