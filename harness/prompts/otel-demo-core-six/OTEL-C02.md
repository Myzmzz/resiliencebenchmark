请在 OTel Demo 2.2.0 中验证 Valkey 购物车在内存压力下是否会发生 OOM、进程重启和购物车数据丢失。

请只使用当前 Harness 授权的 `k8s_ro`、`telemetry_ro`、`source_ro` 和 `chaos_control`。开始前确认 `otel-demo` 命名空间、Valkey 当前 Pod 名称和 UID、容器内存限制、无故障基线以及全局故障清单。还必须先准备一组能够由独立 Oracle 在故障前后读取的购物车数据；如果没有这组数据、没有 Controller 短期授权，或者无法观测内存和重启事件，请停止并报告实验条件不足。

前置条件满足后，向 `chaos_control` 提交有界的内存压力计划，只允许作用于 Controller 授权的单个 `valkey-cart` Pod。持续访问预置购物车，同时观察 Valkey 和 Cart 的内存、OOM、容器状态、Pod UID、重启次数、请求成功率、延迟、Trace 和日志。不要把内存压力进程启动、ChaosBlade Running 或 Pod 重启本身当作购物车缺陷证据，关键是购物车内容是否损坏或丢失以及业务是否违反基线。

达到停止条件或观察窗口结束后，只清理由本次 Trial 创建的故障句柄，确认内存压力消失、Pod 状态稳定、Cart 接口恢复，并重新读取每个预置购物车。最后用自然语言说明故障是否真实生效、是否出现 OOM 或重启、购物车是否完整，以及恢复是否完成；无法完成独立数据对账时必须给出“证据不足”。
