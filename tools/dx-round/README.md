# Dx 轮运维脚本

2026-09-11 到 12 的 Dx 轮评测用的脚本，整改和重跑继续用它们。环境信息见 `docs/status/stage2-new-env-operations-20260912.md`。

**通用约定**

- kubeconfig 默认取 `$HOME/.kube/resbench-new-config`，可用环境变量 `KCFG` 覆盖。命名空间固定 `resiliencebenchmark-system`。
- 平台地址默认 `http://127.0.0.1:28080`，需要先起常驻隧道（见操作手册第 5 节）。
- 产物（`runs/`、`fixtures/`、`*.log`）写在本目录下，已在 `.gitignore` 里忽略，不会弄脏工作树。
- 这些脚本要在沙箱外执行：它们要连集群、Harbor 和本机隧道。

**脚本**

| 脚本 | 用途 |
|---|---|
| `run_dx.py` | 提交一次评测并等到结束。退出码：10 被拒/跳过，20 连不上，30 未结束，40 另有评测在跑，50 平台复位失败或阻塞（必须停） |
| `chain_dx.sh` | 按"用例 × 三家"串行批跑，每次跑完查故障残留并冷却 120 秒。`HARNESSES="claude-code deepseek-harness"` 可限定智能体，`IMAGE` 记录用的镜像标记 |
| `round2c.sh` | 续跑 D4（两家）+ D5 + D6，开跑前等 qwen3.8-max 可用 |
| `round3.sh`、`refresh_d7.sh` | 跑 D7-A/B、D8-A/B；每次 D7 前刷新绑定当前 cart Pod 的样本 |
| `build_boundary.sh` | 构建控制器与 Agent 镜像。要求工作树干净，且 Harbor 上没有同名 tag。需要环境变量 `BLADEAI_REPO`、`BASES`（各约 200MB，不在仓库内），可选 `PY`（默认 `uv run python`） |
| `deploy_boundary.sh` | 替换新环境部署的镜像。`--dry-run` 只校验不改动。会确认没有在跑的评测，只替换 4 处镜像引用，保留 fsGroup 策略和 Coroot 环境变量 |
| `fetch_fixture.sh` | 按运行目录名从集群导出该次 campaign 记录。**已知限制**：只认 `<case>-1` 形式的 trial 目录，`d6-a-1`、D7/D8 这类带变体的要手动导出（见操作手册第 3 节） |
| `diag_trial.py` | 分析一份导出的记录：失败项、生命周期事件、被拒证据、智能体结论 |
| `aggregate_dx.py` | 把 `runs/` 下的所有运行汇总成一张表 |
| `variants.json` | `run_dx.py` 用的提示变体 |
| `images/` | 当前线上镜像（60309d3）和上一版（1807322）的构建元数据，部署与回滚时用 |
