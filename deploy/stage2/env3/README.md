# 第三套环境的清单差异

`values.yaml` 是这套环境与仓库基线清单的差异值，`render.py` 把它套到
`deploy/stage2/stage2-integration.yaml` 上，产出可用的清单。

```bash
python deploy/stage2/env3/render.py --out /tmp/env3-stage2.yaml
python scripts/verify_stage2_deployment.py --manifest /tmp/env3-stage2.yaml \
    --coroot-project po24tcoz --require-node-selector --dns-fallback 159.226.8.6
```

**为什么不直接 apply 仓库清单**：会丢四样东西。

| 丢掉的 | 仓库里的状态 | 丢了会怎样 |
|---|---|---|
| `fsGroupChangePolicy` | 任何清单里都没有 | kubelet 每次挂载递归改权限，私有文件变组可读，下一次运行卡 QUEUED 或 `KubernetesIdentityError` |
| `RESBENCH_COROOT_PROJECT_ID` | 写的是旧集群的 `9auios5b` | 取不到这套集群的可观测数据 |
| `nodeSelector` | 没有 | 落到没装 AppArmor profile 的节点，`agent-runtime` 起不来 |
| `STAGE2_HARNESS_CAPABILITIES_FILE` | **两份清单里都是 0 次**，但第二套环境线上是设了的 | 四个智能体一律 `qualification_not_passed`，`/api/v1/stage2/options` 里没有一个可跑 |

四样都是「部署当场不报错、下一次运行才炸」。`verify_stage2_deployment.py` 无条件核对这四项。

**外加一项只属于这套集群的**：`dnsFallback`。这套集群两个 CoreDNS 副本只有一个能用——
`otcaix-60` 上那个（pod `10.244.2.53`）到 `8.8.8.8` / `1.1.1.1` / `159.226.8.6` 的 UDP 53
全部超时，对外域名一律 SERVFAIL；`otcaix-62` 上那个 10/10 正常。Service `10.96.0.10`
在两者之间轮询，实测经它查 `dashscope.aliyuncs.com` 是 `SERVFAIL/2.0s` 与 `NOERROR/0.0s`
**严格交替**，于是 litellm 报 `OpenAIException - Connection error.`，七个模型五个探测不过。

这是共享集群的既有故障（该副本已 95 天没重启，otel-demo 和别人的 `aiops` 也在同样报错），
**不由本项目修改共享组件**；这里只给平台 Pod 兜一层。`dnsPolicy` 为 `ClusterFirst` 时
`dnsConfig.nameservers` 是**追加**到 `10.96.0.10` 之后的，集群内名字仍然先问 CoreDNS，
只有 CoreDNS 回 SERVFAIL 才落到下一台。这一项是 `--dns-fallback` 选择性开启的，
别的集群不需要，也就不该失败。

**镜像引用不在这里换**——那仍然走 `tools/dx-round/deploy_boundary.sh`，它只替换 4 处
镜像引用并带空闲闸。本渲染器不碰镜像占位符。
