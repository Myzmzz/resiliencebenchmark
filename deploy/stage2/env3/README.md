# 第三套环境的清单差异

`values.yaml` 是这套环境与仓库基线清单的差异值，`render.py` 把它套到
`deploy/stage2/stage2-integration.yaml` 上，产出可用的清单。

```bash
python deploy/stage2/env3/render.py --out /tmp/env3-stage2.yaml
python scripts/verify_stage2_deployment.py --manifest /tmp/env3-stage2.yaml \
    --coroot-project po24tcoz --require-node-selector
```

**为什么不直接 apply 仓库清单**：会丢三样东西——`fsGroupChangePolicy`（仓库里根本没有）、
正确的 Coroot 项目 id（仓库里是旧集群的 `9auios5b`）、`nodeSelector`（仓库里没有，
而只有装了 AppArmor profile 的那台能跑 `agent-runtime`）。
渲染器只动这三处加存储类，其余原样保留。

**镜像引用不在这里换**——那仍然走 `tools/dx-round/deploy_boundary.sh`，它只替换 4 处
镜像引用并带空闲闸。本渲染器不碰镜像占位符。
