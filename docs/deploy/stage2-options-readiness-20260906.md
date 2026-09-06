# Stage2 选项接口与模型检查解耦

## 故障与修复范围

旧集群 integration 的 `47f4f04` 版本中，`GET /api/v1/stage2/options`
在本轮分别超过 15 秒和 45 秒未返回；同时 `/healthz` 返回正常。
源码显示 `preflight()` 在请求线程内调用六个模型的完整能力探针，并在整个
网络调用期间持有缓存锁。并发请求会排队；缓存还从探测开始计时，慢探测
消耗的是结果尚未产生时的有效期。这是接口执行方式问题，不是模型密钥错误的证据。

修复保留 WP12 的全部模型检查和原有超时/重试规则：后台单次检查网关目录与
六个模型，HTTP 请求读取状态快照，不等待模型回复。相同配置只允许一轮在途
检查；没有有效结果、结果过期或配置变化时，模型格不允许执行。缓存有效期
从该轮实际结束起算，失败也被有限缓存，避免页面轮询触发连续模型请求。

接口新增 `gateway_probe` 状态与 `model_probes` 详情。`running` 表示检查中，
不是通过；此时提交任务被拒绝且不启动 Agent。客户端确认模型与 Harness 的
`runnable=true` 后再提交。本次没有改变请求 Body、模型路由、Prompt、评分、
默认用例集或 D7/D8 的准入条件。

改动位置说明（对应方案第 8.12 条）：除 WP12 列出的 `runtime_factory.py`
及 WP4 的 `task_service.py`，同步更新 `scripts/run_stage2_matrix.py`，使显式
执行的 CLI 等待同一轮检查，而不把 HTTP 的“检查中”当成最终结果；相关测试
更新为先完成探针再验证通过状态。没有增加另一个模型检查实现或兼容分支。

## 验证记录

- 全量测试：1617 项，1607 通过、10 跳过，无失败；记录
  `artifacts/remediation/20260905/options-readiness-final-full.xml`。
- 定向测试：51 项通过，覆盖冷缓存、慢目录/慢探针、20 个并发调用只启动一轮、
  TTL 从完成起算、过期拒绝旧成功、刷新异常、线程启动失败、配置缺失与路由变化。
  记录 `options-readiness-final-focused.xml`。
- 同步 CLI 显式等待模型检查；HTTP 的检查中状态不启动任务，有独立接口回归测试。
- 旧集群部署前：26 条历史任务均已终结；ChaosBlade 清单与 otel-demo 下三种
  Chaos Mesh 清单为空。当前 18 份 D0 inventory 枚举耗时 0.042 秒，因此本次
  不扩展 D0 索引重构。

以上不构成正式故障评测通过。

## 旧集群实际部署

integration 已更新到 `edd7799`，Pod 为
`resbench-stage2-integration-67c98d6b7c-xgkhg`。两个运行镜像和初始化镜像均与
构建记录一致，节点仍为 `tcse-v100-03`，模板的其余字段逐项比较未变。
main/e2e 未更新，历史 26 条任务保留且全部终结。新 Pod 三容器就绪、零重启，
ChaosBlade 与 otel-demo 下 Chaos Mesh 清单为空。记录：
`options-readiness-rollout-ready.json`、`options-readiness-images.json`。

验证保留了两类真实状态：

- 冷启动时首次选项查询耗时 0.493 秒，返回 `running` 且所有模型格关闭。
  当时网关尚未监听，首次目录与探针连接失败；未冒充通过，失败缓存保留。
- 随后的 20 次并发查询最大耗时 0.730 秒，均返回同一轮的失败记录，没有
  启动重复探测。其后网关目录独立查询在 0.502 秒内成功，返回全部六个必需别名。

`options-readiness-rollout-verification.json` 是 Pod 尚未全部就绪的早期快照，
其中 `pod_ready=false`；它没有被修改或替换成通过记录。

仅本机端口转发为 `127.0.0.1:18080 -> resbench-stage2-integration:8080`。
没有新增公网端口或修改原有 Postman 配置。接口响应正常不代表模型资格通过，
仍需等待实际探针成功后才允许提交手测。
