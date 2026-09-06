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
