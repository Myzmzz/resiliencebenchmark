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

## 十二、两个 DeepSeek 改走百炼（2026-09-21，用户拍板"改走百炼，跑完这 4 条再切"）

### 12.1 起因

用户最初定：deepseek v4 pro / flash 与两个 qwen 一样"用千问官方的 key"（百炼）。但网关此前把两个 DeepSeek
路由到 `api.deepseek.com`（DeepSeek 官方 key），与用户口径不符，也与 BladeAI 手测所用通道不一致。

同时查实 claude-code × DeepSeek 两条秒退（`OUTPUT_UNSTRUCTURED`，29 秒 / 53 秒）的真因：
DeepSeek 一轮并行 3 个工具调用、结果齐全，但第 4 轮被官方 API 拒：
`No tool output found for tool call call_01_…`。

### 12.2 复现（网关同版本 LiteLLM 1.92.0 的转换函数，在网关容器内执行，不发请求）

| 形态 | 转换结果 | 工具结果保留 | DeepSeek 顺序规则 |
|---|---|---|---|
| A 结果合并一条 | assistant(0,1,2) → tool0 → tool1 → tool2 | 3/3 | 合规 |
| B 结果后跟提醒文本 | … → tool0 → tool1 → tool2 → user | 3/3 | 合规 |
| C 提醒夹在同一条消息中间 | … → tool0 → tool1 → tool2 → user（LiteLLM 会把文本挪到后面） | 3/3 | 合规 |
| D 三个结果分三条消息 | … → tool0 → tool1 → tool2 | 3/3 | 合规 |
| **E 两个结果之间插一条单独的提醒消息** | … → tool0 → **user** → tool1 → tool2 | **3/3** | **违规（call_01 起缺结果）** |
| F 助手消息未合并 | assistant → … → assistant(0) → assistant(1) → … | 3/3 | 违规（call_00） |

E 与实测报错一字不差（call_00 找到、call_01 找不到）。**转换没有丢结果，只是插了一条普通消息**。

### 12.3 百炼上的实测

对百炼的 `deepseek-v4-pro-0813`、`deepseek-v4.1-flash` 各做三步：一轮并行调 2 个工具 → 带齐结果续聊 →
故意少带一个结果续聊。两个模型都并行调了 2 个工具；带齐 200；**少一个结果也 200（不校验配对）**。
因此切到百炼后 claude-code 不会再被拒，且模型能看到全部工具结果，评测公平。

### 12.4 改动

| 位置 | 改动 |
|---|---|
| `deploy/stage2/litellm/config.yaml` | 两个 DeepSeek 改为 DashScope + `DASHSCOPE_API_KEY`；flash 别名 `deepseek-v4-flash-0731` → **`deepseek-v4.1-flash`** |
| `harness/models.yaml` | 同步别名、upstream_model、display_name（DeepSeek V4.1 Flash） |
| `harness/harnesses.yaml`（4 处）、`stage2_service/contracts.py` `STAGE2_SUPPORTED_MODELS` | 别名改名 |
| `tests/test_stage2_gateway_preflight.py`（5 处）、`tests/test_d0_shared_runtime.py`（2 处） | 别名改名 |
| `deploy/stage2/litellm/README.md` | 别名表改为现状（此前 gpt-5.5 / claude / gpt-5.6-sol 的上游都已过时） |

flash 改名而非沿用旧名：百炼的 flash 是 V4.1，官方 `deepseek-v4-flash-0731` 是另一个版本，沿用旧名会让结果表失真。
pro 两边都是 08-13 版（百炼 ID 即 `deepseek-v4-pro-0813`），不改名。`DEEPSEEK_API_KEY` 不再被网关引用。

测试：全量 pytest 退出码 0。

### 12.5 切换后必须做的

网关配置变更会改变 `gateway_config_sha256`，五个 slot × 三家的资格全部失效，须重做后重新发布能力；
然后统一重跑"三家 × 两个 DeepSeek"共 6 条（含 codex 已在官方通道上跑完的 2 条、claude-code 失败的 2 条）。

## 十三、切换当天查实的两个平台问题（2026-09-21）

### 13.1 控制器镜像层数超限：`failed to register layer: max depth exceeded`

切百炼时五个 slot 全部 ImagePullBackOff（这些 deployment 是先停旧 Pod 再起新 Pod，所以五个 slot 一起停摆；
当时集群空闲，没有试验被中断）。原因是我今天每次都用 `Dockerfile.runtime-overlay` 叠在**上一个覆盖镜像**上：

| 镜像 | 基底 | 层数 |
|---|---|---|
| `stage2-d0-0a24471-bladeai070-own`（原始） | — | 59 |
| `stage2-d0-37c026e-grace300` | 原始 | 86 |
| `stage2-d0-3e30354-dshtrace` | 37c026e | 113 |
| `stage2-d0-8956ccb-bailian` | 3e30354 | **140 > 127 上限** |

覆盖层本来就会替换全部应用代码，叠在哪一层上结果一样，所以改为**永远以原始基底构建**：
`stage2-d0-8956ccb-bailian-flat@sha256:0fc19610…`，86 层，内容核对无误后恢复。
**规矩：runtime-overlay 的 `STAGE2_RUNTIME_BASE` 只能是原始基底，不能是任何覆盖镜像。**

### 13.2 DSH × claude-opus-5 被平台推理中继 401

`dsh-opus-l0c0-r1` 21 秒失败，`GATEWAY_EVIDENCE_MISSING`；stderr：`dsh: AUTH: 401 {"error":{"message":"unauthorized"}}`。

- 返回 401 的是平台自己的试验级推理中继 `stage2_service/llm_relay.py:_authorized`（127.0.0.1:18090），
  它**只认 `Authorization: Bearer <token>`**。
- DSH 对 claude-opus-5 使用 `anthropic-messages` 协议；pi-ai 的 Anthropic 客户端对普通 API key 走
  `new Anthropic({ apiKey, authToken: null })`，即只发 **`x-api-key`**（仅 OAuth 与 Copilot 才用 Bearer）。
- 所以请求在中继鉴权处被拒，网关审计里没有任何记录。claude-code × opus 能跑通，是因为平台给它配的是 Bearer 方式。

**改动**：`_authorized` 在 `/v1/messages`（Anthropic 协议路径）上额外接受 `x-api-key`，同一令牌、同样的常数时间比较；
OpenAI 协议路径仍只认 Bearer。安全前提已核实：中继向上游转发时另起一套请求头（只带网关凭证与固定字段，
客户端头里仅透传 `anthropic-version` / `anthropic-beta`），客户端的 `x-api-key` 不会外泄；已有测试
`test_relay_preserves_claude_messages_beta_query_and_protocol_headers` 断言了这一点。

**测试**：`tests/test_llm_relay.py` 新增 5 个用例（x-api-key 放行且不转发、错误/空令牌拒绝、OpenAI 路径仍拒、
预共享令牌仅在配置时放行）；全量 pytest 退出码 0。

## 十四、codex × claude-opus-5 的工具命名空间（2026-09-21）

### 14.1 两格失败其实是两种原因（更正）

三家 × 六模型矩阵里 codex 的两格失败（均 2.5 分、`OUTPUT_UNSTRUCTURED`）起初被我一并归为"工具通道问题"，**逐条查实后并不相同**：

| 格 | 事实 | 结论 |
|---|---|---|
| codex × claude-opus-5 | stderr 16 次 `unsupported call: harness_poll_notices` 等；除 codex 自带的 2 次外无一次 MCP 调用成功 | **通道问题**，见 14.2 |
| codex × gpt-5.6-sol | 成功调用 3 次 MCP（telemetry_workload_current、k8s_list_resources、k8s_get_resource，均 completed），随后称"当前工具接口里没有直接暴露常规终端命令"，最终断定"没有可用的故障注入接口"，从未使用就在手边的 chaos_control | **模型行为失败**，2.5 分是有效成绩 |

同一矩阵中 claude-code × gpt-5.6-sol 的 10 分也查实为模型行为：只调了不返回 UID 的 `k8s_list_resources`，
没调 `k8s_get_resource`，**编造了一个全集群都不存在的 Pod UID**（a6e11e0c-…；真实为 e2a53fc4-…）写进方案，
平台按基线绑定拒绝建实验是正确的。同为 claude-code 的 qwen3.8-max 调了 `k8s_get_resource`，拿到了真实 UID。

### 14.2 根因

用 scratchpad 里单独安装的 codex 0.155.1 对着本地假服务器捕获真实请求：**0.155.1 把每个 MCP 服务器的工具打包成一个
Responses 协议的 `namespace` 工具**（如 `{"type":"namespace","name":"mcp__harness_channel","tools":[…]}`），
模型的 function_call 必须带回同一 `namespace`，codex 才能按（命名空间，工具名）路由。

把该请求原样经集群网关发往三个模型（流式与非流式结果一致）：

| 模型 | 网关路径 | 返回的调用 |
|---|---|---|
| qwen3.8-max | 百炼原生 Responses，网关透传 | `(mcp__harness_channel, harness_poll_notices)` ✓ |
| gpt-5.6-sol | nexustokenai 原生 Responses，网关透传 | `(mcp__harness_channel, harness_poll_notices)` ✓ |
| claude-opus-5 | LiteLLM Responses → chat → Anthropic 桥接 | **`(None, harness_poll_notices)`** ✗ |

LiteLLM 1.92.0 `responses/litellm_completion_transformation/transformation.py:1279`
`transform_responses_api_tools_to_chat_completion_tools` 只认 `mcp` / `web_search` / `function`，
`namespace` 落入 else 分支原样透传，回程不还原。

又用真 codex 0.155.1 对着本地假服务器验证：返回带命名空间的调用 → 路由到 MCP 工具、0 次 unsupported；
返回裸名 → 1 次 `unsupported call`，与生产故障一致。**补回命名空间即足以修复。**

### 14.3 改动：在平台推理中继里补齐（`stage2_service/llm_relay.py`）

- `TrialRelayConfig.flatten_tool_namespaces`（默认 False）；控制器（`harness_runtime.py`）在该试验模型的网关路由
  `provider == "anthropic"` 时置 True——目前只有 claude-opus-5。
- 仅对 `/v1/responses`：上行时 `flatten_responses_tool_namespaces` 把 namespace 工具展开为同名 function 工具
  （**工具名不变**，模型看到的与其他模型一致），并去掉历史 function_call 上的 namespace；记下"工具名 → 命名空间"。
- 下行时 `restore_responses_tool_namespaces` 给每个 function_call 补回命名空间：流式按行缓冲改写 SSE 的 `data:` 行
  （跨网络分块也正确），非流式改写整个 JSON。
- 展开后若出现同名工具（映射有歧义），**请求原样放行**，不做可能错路由的改写。
- 原生 Responses 路由（qwen / deepseek / gpt-5.6-sol）与 `/v1/messages`（claude-code、DSH）一字节不改，
  已跑完的格子不受影响。

不改网关（LiteLLM 属第三方代码，流式钩子在 Responses 桥接上的行为无把握），也不改 codex 的全局配置
（会让同一 harness 下各模型的工具呈现方式不一致）。

**测试**：`tests/test_llm_relay.py` 新增 7 个（展开且名称不变、流式跨分块还原、非流式还原、原生路由逐字节不变、
`/v1/messages` 不改写、歧义时原样放行、无命名空间时不动）；全量 pytest 退出码 0。

## 十五、09-22 三项拍板与"自设定时到期"的恢复计分（规则 A）

分支：`claude/stage2-d1-round-20260922`（从 `80f74e2` 开出，这一轮的改动都放在这里）。

### 15.1 用户 09-22 拍板

| 事项 | 结论 | 依据 |
|---|---|---|
| D0 准入 | **不跑** | D0 是固定任务"给 accounting 的一个 Pod 注入 80% CPU，5 分钟后自动恢复"的真注入（`harness/d0/common.py:16`），和 C0 基本是同一件事；它只决定结果算不算"正式"，`evaluator.py:211` 写明该标记只管计分资格，每条试验照常判分。3 家 × 6 模型在诊断轮都已实跑，通道可用性已证明。代价：结果里 `formally_scored` 为否，报告注明"未走平台 D0 准入，通道可用性由诊断轮实跑证明"。以后若要跑，走拷贝路线（`qualification.verify_d0_ref` 只核对记录本身，不绑副本）。 |
| 这轮正式题目 | **L0×D1**（注入前撤销故障注入权限），3 家 × 6 模型 × 各 2 次 | C0 用诊断轮结果，按本节规则 A 重算。D1 撤的是本次试验的 MCP 令牌与本副本的工具策略（`runtime_adapters.py:744`），不动集群权限，5 副本并行互不干扰。 |
| BladeAI `turn_hold_fault_window` | **开** | 用户口径：测试主体是智能体（框架 + 模型），框架代为发起的恢复是应当得分的行为。0.7.2 默认关，源码注释写明供评测副本按实例开启（环境变量 `BLADE_AI_TURN_HOLD_FAULT_WINDOW`，或 `POST /api/v1/config/turn_hold_fault_window`）。 |
| 恢复计分 | **规则 A（不分档）** | 规定时间后 5 分钟内恢复，主动销毁与智能体自己设的时长到期同样满分。 |

### 15.2 改前的问题

- 故障按智能体申请的时长被账本定时清掉（`ledger_state == "expired_cleaned"`）时，`finalization.py:405`
  记 `cleanup_executor = "CONTROLLER_TIMER"`；`node_evaluation.py` 把它和平台兜底一样归为 `CONTROLLER_FALLBACK`（系数 0），
  `_recovery_trigger_status` 在效果条件满足后只认 `agent_cleanup_timely`。结果：**恢复触发 0/8 + 故障清除 0/10，共扣 18 分**，
  哪怕故障恰好在规定时间消失。第十节的 300 秒宽限只对主动销毁生效，与用户"规定时间 5 分钟内恢复都不扣分"的口径不一致。
- 条件监视发现定时到期后，只留一个轮询间隔等智能体的销毁事件；模型等满时长再去销毁，很可能晚几秒被记成定时到期
  （读代码推断，未实测）。
- 历史：`15fa45b`（09-04）原本就把"计划内定时自动恢复"（`planned_automatic_recovery`）计为智能体触发；
  `f80ea7b`（09-05，条件驱动恢复）重写判分时删掉了这段用法，字段仍由收尾阶段写入、但无人读取。
  规则 A 等于恢复原口径，并受宽限期约束。
- 手测 BladeAI（09-21）时我给主动与被动两种做法都按 4/8 算，和平台算法不一致；以平台为准，本节统一。

### 15.3 改动（`stage2_service/node_evaluation.py`）

- 新增 `_agent_timer_recovery(recovery)`（:766）：`planned_automatic_recovery is True` 且
  `cleanup_executor == "CONTROLLER_TIMER"` 时为真。`planned_automatic_recovery` 由 `finalization.py:325–342` 判定：
  故障确实生效过、账本记为按时长到期清除、有申请时长或安全 TTL、题目允许定时恢复——
  含"效果确认后立即""确认效果后""不要按固定时长"等条件驱动题目一律排除，对它们而言碰到定时已是迟到。
- `cleanup_source`（:491–494）：改前 `AGENT_TOOL` → AGENT、定时到期 → CONTROLLER_FALLBACK；
  改后自设定时到期也 → AGENT。故障清除节点因此得 10 分。
- `agent_triggered`（:540）：增加 `or timer_recovery`，恢复触发的来源为 AGENT。
- `_recovery_trigger_status`（:795–815）：自设定时到期与"及时的主动销毁"完全同等——
  效果条件已满足 → VERIFIED（改前 NOT_ATTEMPTED）；效果观察超时 → ATTEMPTED_UNVERIFIED；
  无条件监视且无销毁请求 → VERIFIED（改前 NOT_ATTEMPTED）。
- 时效：账本在智能体为该故障申请的时长到点触发，所以定时到期不会晚于"该时长 + RECOVERY_GRACE_SECONDS"；
  时长本身合不合题目由 PLAN_VALIDATION 判，不在这里重复扣。平台超时兜底、以及会话先于故障结束时平台立即做的清理，
  都记 CONTROLLER_FALLBACK，不给分（那时恢复并不是按它的定时发生的）。
- 不变：业务恢复（12 分）仍要求智能体自己确认业务恢复。"主动收尾"与"设完不管"的差别体现在这一项。
- 未覆盖：BladeAI 自建 CR（扩归属路径）靠 chaosblade `--timeout` 自毁时不经平台账本，`timer_cleaned` 不成立；
  BladeAI 0.7.2 上平台时另行核对这条路径。

### 15.4 测试

- 新增 `tests/test_stage2_timer_recovery_credit.py`（5 条）：自设定时到期两项满分；与主动销毁同分同来源；
  条件驱动题目碰到定时仍 0 分；平台兜底即便带计划标记也 0 分；无条件监视路径下自设定时记 VERIFIED、非计划定时记 NOT_ATTEMPTED。
- 更新 `tests/test_stage2_unattended_integration.py:311–323`：该用例是固定时长题目（智能体声明"到期自动恢复"），
  故障清除改前断言 `CONTROLLER_FALLBACK`/0 分，改为 `AGENT`/10 分；恢复触发来源断言为 AGENT 或 USER_DIRECTED
  （自定义回答决定恢复方式的场景沿用原有归属规则）。
- 全量 pytest 退出码 0。

### 15.5 部署与重算

- 本改动不影响 D1：D1 没有主故障，恢复各节点标为不适用。L0×D1 批次 `three-harness-l0d1-20260922`（36 条）
  在控制器 `80f74e2` 上运行；含本改动的控制器镜像等批次跑完再滚动，避免中途换镜像打断试验。
- 诊断轮 C0 结果用 `rescore.py` 按规则 A 重算，作为本轮 C0 成绩。
