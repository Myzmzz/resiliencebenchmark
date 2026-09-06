# 第一项手测：Codex / C0

状态：旧集群 integration 服务的 Codex 基础资格已通过并发布；`/options` 已实际
返回 `codex.runnable=true`、`C0` 位于 `runnable_cases`、`gpt-5.5=true`。
本项故障任务尚未提交或执行，由用户手动发起。

当前没有匹配本次网关的 D0 正式资格记录，因此本项会明确标记为
`qualification.mode=diagnostic`：可以进行真实单项故障调试，不纳入正式矩阵
成绩。不得把基础资格通过写成故障评测通过。

## Postman 请求

- Method：`POST`
- URL：`{{base_url}}/api/v1/stage2/tasks`
- Header：`Content-Type: application/json`
- Body：raw → JSON，复制[请求参数](codex-c0-first-20260906.request.json)。

`base_url` 必须指向本次更新的旧集群 `resbench-stage2-integration` 服务；
服务路径仍是原接口。本地尚未确认用户当前 Postman 的基础地址，不假定公网
IP 或端口，也没有新增 NodePort/Ingress。先用同一个 `base_url` 请求
`GET /api/v1/stage2/options`，确认上述 Codex 状态，再提交。

### 本机 Postman 的已连接入口

2026-09-06 已建立仅监听本机的端口转发，直接连接上述 integration 服务。
如果 Postman 运行在这台 Mac 上，可设置 `base_url=http://127.0.0.1:18080`；
这不是公网地址，也没有修改用户原有 Postman 配置。转发进程结束后，可在本机终端重建：

```sh
kubectl --kubeconfig /Users/mymz/.kube/coroot-config --context kubernetes-admin@kubernetes \
  -n resiliencebenchmark-system port-forward --address 127.0.0.1 \
  service/resbench-stage2-integration 18080:8080
```

选项接口的 `gateway_probe.status=running` 表示真实模型检查尚未完成，此时
不要提交。若是 `failed`，查看 `gateway_probe.model_catalog_error` 和
`model_probes`，不能把 HTTP 200 或服务健康当成模型已就绪。只有目标 Harness
及其 `gpt-5.5` 模型格均为 `runnable=true` 才进入本项测试。

`disturbance=none` 表示 C0 对照：不额外施加 D1–D8 的能力扰动；不代表不创建
Prompt 中要求的主故障。请求没有 Episode、权限 Profile、schema_version 或
request_id；这些都不由用户填写。

## 本轮只发一条

保存响应里的 `task_id`，用：

```text
GET {{base_url}}/api/v1/stage2/tasks/{{task_id}}
GET {{base_url}}/api/v1/stage2/tasks/{{task_id}}?mode=timeline
GET {{base_url}}/api/v1/stage2/tasks/{{task_id}}?mode=debug
```

任务未结束前不要重复提交。出现问题先保留 task_id 与响应，不进入 D1。
若需停止，调用该任务已有的 `POST .../abort`，随后确认清理与恢复结果；不能
仅凭故障对象删除或 Pod Ready 宣称业务恢复。

本参数不是 L0–L4 Prompt 文件的修改，也不是批量 Campaign。其它智能体的
基础资格、BladeAI 全链资格、D0 正式资格与 D7/D8 准备仍单独推进。
