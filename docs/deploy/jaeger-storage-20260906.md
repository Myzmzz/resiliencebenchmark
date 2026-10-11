# 旧集群 Jaeger 存储整改

## 目标

`observability/jaeger` 使用 Jaeger 1.57 all-in-one。原配置使用无上限的
`SPAN_STORAGE_TYPE=memory`，在长期运行中会把 Trace 全部留在 Go 进程内存，
导致节点内存和文件页缓存压力，进而阻塞 BladeAI/MCP 依赖加载。

本次整改仅作用于旧集群 `coroot-config` 的 `observability/jaeger`：

- 改用 Badger 本地嵌入式存储，目录由专用 NFS PVC 提供；
- 设置 6 小时 Trace TTL；
- 设置 Jaeger 容器 requests/limits，防止再次无界占用节点内存；
- 保留单副本和原有 Service/端口；
- 重启会清空原有 memory backend Trace，已按用户授权接受；
- 不修改 OTel Demo、Stage2 主/e2e、ChaosBlade 或新集群。

## 运行参数

```yaml
SPAN_STORAGE_TYPE=badger
BADGER_EPHEMERAL=false
BADGER_DIRECTORY_KEY=/badger/key
BADGER_DIRECTORY_VALUE=/badger/data
BADGER_SPAN_STORE_TTL=6h
BADGER_MAINTENANCE_INTERVAL=5m
```

PVC 为 `observability/jaeger-badger-data`、`20Gi`、`nfs-client`，只挂载到
Jaeger 单副本 `/badger`。NFS 作为本集群可用的持久化介质；Badger 仍是单实例
嵌入式数据库，不扩展为多副本。若后续观测到 NFS 延迟影响查询，应迁移到节点
本地 SSD 或正式外部存储，而不是提高 BladeAI/MCP 启动超时。

## 验收

1. PVC 为 `Bound`，Jaeger Pod 1/1 Ready、零新重启。
2. 日志显示 `badger` backend、TTL 配置及成功监听 16686/4317/4318。
3. 通过 Jaeger query API 取得一条新生成 Trace；旧内存 Trace 不作为验收条件。
4. 写入和重启后 PVC 目录仍可读；记录实际磁盘使用量。
5. 节点 `MemAvailable` 恢复到安全水平，Stage2 integration `/healthz` 和 MCP
   组件启动不再因节点页缓存压力失败。

所有验证记录写入 `artifacts/remediation/20260906-jaeger-storage/`。
