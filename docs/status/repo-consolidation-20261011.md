# 仓库归并记录（2026-10-11）

把散在 11 个工作树、20 多个分支上的未提交改动与未并入分支一次性收口到 `main`。
本文记录**做了什么、刻意没做什么、以及怎么把没做的部分取回来**。

合并前的 main 备份在分支 `backup/pre-consolidation-20261011`（= `a7bea6b`，09-12）。

## 1. 先补提交：三处只存在于工作树里的成果

| 工作树 | 分支 | 提交 | 内容 |
| --- | --- | --- | --- |
| `resiliencebenchmark-stage2-d0-integration` | `codex/stage2-d0-integration` | `1981900` | 09-10 的评分修复（故障清理归属改看"是否由自设时长到期结束"）、MCP 时间窗错误文案、6 份文档、Jaeger PVC |
| `resiliencebenchmark-newenv-bladeai` | `claude/newenv-bladeai-20260922` | `98d5c12` | `scripts/bladeai_direct_eval/`：直连 BladeAI 的黑盒评测驱动，31 个模块（此前任何分支都没有） |
| `resiliencebenchmark`（陈旧树） | `codex/train-ticket-agent-dataset-20260825` | `1ad6f27` | 08-27~09-05 攒下的 148 个文件：Phase-1 缺陷工作台前后端、D0 手动资格认定脚本、语义扫描提示词新命名、任务 catalog、文档与报告 |

未纳入提交：陈旧树里的 `.codex-artifacts/`（18 MB 截图与拼图）、`.codex-tmp/`，属 agent 临时产物；
`scripts/bladeai_direct_eval/__pycache__` 由 `.gitignore` 排除。

## 2. 并入 main 的分支

按时间顺序，每一步都跑过全量 `pytest`：

| 提交 | 并入 | 说明 |
| --- | --- | --- |
| `8b7ae61` | `claude/stage2-d1-round-20260922`（116 提交，10-08） | 这段时间唯一在跑的主线，无冲突 |
| `bd14343` | `codex/stage2-d0-integration` | 与 main 上 09-22 的裁定改的是同一段逻辑，按"新裁定判分、旧改动补检测"合并，见下节 |
| `aee6dff` | `claude/stage1-toolmode-20260915` | Stage-1 工具模式与 telemetry_ro 的 OTel 标签修复，命名空间常量与测试取并集 |
| `a4181df` | `claude/newenv-bladeai-20260922` | BladeAI 直连驱动 + 共享集群准入围栏 + 0.7.2 边车 |
| `1bf9fc3` | `codex/d0-agent-qualification` | 只补 main 没有的 `harness/d0/facade.py` 与 `mcp_servers/d0_chaos_control/` |
| `06323a0` | `codex/train-ticket-agent-dataset-20260825` | **只加不改**，见第 3 节 |
| `078ef1f` | `origin/rules-v1-buildout`（含 `origin/codex/stage2-env3-bladeai-20260912`） | 规则库 v1 + 文献笔记 + 权限恢复账本，见第 3 节 |
| `1477fe6` | `origin/imp-evaluation` | `-s ours`：只记历史、不采用内容，见第 3 节 |

`codex/bladeai-blackbox-integration`、`codex/stage2-fleet-bladeai-merge-20260915`、
`codex/stage2-replica-fleet-20260912`、`codex/bladeai-blackbox-impl`、
`codex/stage2-disturbance-e2e`、`codex/stage2-dx-round-fixes-20260911`、
`codex/stage2-parallel-fleet-20260912`、`codex/frontend-migration-20260824`、
`codex/multi-level-disturbance-20260823`、`codex/d0-agent-qualification` 的内容
随上述合并或此前已在 main 中。合并后 `git rev-list --count <branch> ^main` 对所有本地与远端分支均为 0。

## 3. 刻意没有采用的部分（都还在历史里，可取回）

### 3.1 陈旧线对 main 已有文件的 09-05 版补丁

`codex/train-ticket-agent-dataset-20260825` 停了一个月，main 同期演进到 10-08。
它对 63 个 main 已有文件的修改一律不采用，包括 `backend/main.py`、`pyproject.toml`、
`deploy/stage2/Dockerfile`、`environment/kubernetes/otel-demo/values.yaml`、
`harness/harnesses.yaml`、`resilience_agent/semantic_scan/*.py`、前端路由等——
打上去等于把当前平台回退到 09-05。同理不采用它的 14 个删除与 7 个改名：
旧命名提示词 `rd01_request_lifecycle…rd14_replica_disruption`、`resilience-defects-v2.yaml`
与 deepseek-harness 安装脚本仍被 main 的 `resilience_agent/config/semantic-scan.otel-demo.yaml` 等引用。

**后果**：Phase-1 前后端（`backend/{api,models,services}/phase1.py`、
`frontend/src/pages/Phase1DefectsPage.tsx` 等）与语义扫描新命名提示词目前在 main 里是
**未接线**状态——文件在树里，但没有路由/注册入口。要启用需按当前 backend 与 frontend 结构重新接。

同时移除了 6 个只能对着旧接口跑的测试（内容在第二父提交里）：
`test_permission_profiles`、`test_stage2_model_matrix`、`test_harness_environment_boundary`、
`test_manual_permission_campaigns`、`test_phase1_api`、`test_stage2_bladeai_permission_disturbances`。
它们分别依赖 main 已改掉的 `McpServerName`、`HarnessModelSelection`、
`CompositeDisturbanceExecutor(rbac_permissions=)`、`DisturbanceType.PERMISSION_CHANGE`、
campaign schema v2（main 是 v4）、Dockerfile 策略与 `EvidenceRef` 契约。

### 3.2 rules-v1 的 45 MB 文档缓存

`rules-v1/docs-cache/` 与 `literature-v1/docs-cache/`（338 个文件、约 45 MB 抓取的上游文档原文，
含单个 9.5 MB 的 Redis HTML）没有并入，已写进 `.gitignore`；仓库没有配 LFS，
一旦推上远端就会永久留在历史里。规则库本体只有 0.6 MB。

**后果**：`rules-v1/rules.yaml`、`documents.yaml` 的出处引用按路径指向缓存，
所以 `python3 rules-v1/validate.py` 在没有缓存时会 FAIL。核对出处前先取回本地副本
（有 `.gitignore` 在，不会被再次提交）：

```bash
git checkout origin/rules-v1-buildout -- rules-v1/docs-cache literature-v1/docs-cache
# 或重新抓取
python3 rules-v1/tools/fetch.py
```

### 3.3 cd0289d「Dx 整改第一批」的平台代码

`origin/rules-v1-buildout` 的平台部分是 09-12 快照。它新增的
`stage2_service/provider_failures.py`（供应商欠费/超时归类与熔断）、
`mcp_tool_catalog.py`（裸工具名归属）、`image_manifest.py`（镜像清单自检）
main 上确实没有等价实现，但它们的接线点在 `llm_relay.py`、`harness_runtime.py`、
`runtime_factory.py`、`scripts/run_harness_trial.py`、`scripts/build_stage2_image.py`、
`deploy/stage2/Dockerfile.runtime-overlay` —— 这些文件 main 上已是 10-08 的新实现
（SSE 逐行命名空间还原、codex 工具命名空间桥接等），冲突处一律保留 main。
半接会让 11 个测试变红，所以这三个模块连同
`test_stage2_{provider_failures,tool_identity,image_manifest,env3_overlay}` 一并留在分支上。

要恢复这批能力，需要按 main 现在的中继结构重新接一次，这不是合并能解决的。
同批的 `stage2_service/recovery_state.py`（权限恢复账本）只依赖标准库、接线干净、测试自洽，已并入。

### 3.4 imp-evaluation 的前端改动

08-27/28 的前端工作落在迁 pnpm 之前的结构上，会给 main 现有 16 个前端文件打旧补丁，
并额外带进 `frontend/package-lock.json`；本机没有 `node_modules`，无法验证类型检查与构建。
因此用 `-s ours` 记录合并、树保持不变。需要时：

```bash
git show origin/imp-evaluation:frontend/src/pages/ApplicationsCreatePage.tsx
```

## 4. 合并中真正改到的代码（不是照搬）

`bd14343` 里两处需要人工判断的语义合并：

1. **`stage2_service/finalization.py`**：`timer_cleaned` 改用 09-10 的
   `_fault_ended_by_own_timer()`（Agent 在自设 `--timeout` 到期后补发 destroy 会把账本的
   `expired_cleaned` 改写成 `destroyed`，只看最终状态会误判成平台介入），
   这个检测结果同时喂给 main 的 `planned_automatic_recovery`；
   `controller_intervened` 保留 main 的外来故障分支，同时排除按时到期的计时器。
2. **`stage2_service/node_evaluation.py`**：判分口径保持 main 的 09-22 裁定
   （credit 只走 `planned_automatic_recovery`），条件驱动任务只撞上计时器仍然不得分。
   09-10 版的 `test_agent_timer_cleanup_in_time_earns_both_recovery_nodes` 期望与此冲突，
   已改写为 `test_timer_credit_follows_the_ruling_not_the_ledger_flag`，
   断言"账本标志记录事实、裁定决定是否给分"。
3. **`stage2_service/evidence_assessment.py`**：两侧规则取并集后修正优先级——
   09-10 的"恢复阶段句式不算打脸效果"要让步于 09-23 的"点名效果本身仍算矛盾"
   （加了 `and not re.search(EFFECT_ITSELF, text)`），否则
   `test_a_gap_that_names_the_effect_still_contradicts_it` 会漏判。

## 5. 验证

- 全量 `pytest` 通过（含新增的 recovery_state / permission_restoration_ledger /
  verify_stage2_deployment / env3_gap_report / bladeai_direct_eval 等测试）。
- 其中 `tests/test_stage2_unattended_integration.py` 与 `tests/test_coroot_ro_mcp.py`
  需要在沙箱外跑：它们要往 `/tmp` 建目录并 bind 本地端口。
- 前端未验证：本机 `frontend/node_modules` 不存在，`make test-frontend` 没跑。

## 6. 回滚

```bash
git reset --hard backup/pre-consolidation-20261011   # 回到 09-12 的 main
```

各分支 ref 都还在，合并只是让 main 前移，没有删除任何分支。
