# RD-03 风险：CPU过载时缺少准入和拒绝保护；后果：请求持续堆积，延迟和超时不断上升

本模板只使用ChaosBlade原子故障`cpu-load`，对应`k8s pod-cpu fullload`。CPU使用率升高不是缺陷；候选必须证明有限CPU与入场、并发、队列或负载拒绝之间的静态边界存在弱点。

从Kubernetes Deployment/StatefulSet的容器CPU request/limit、command/args/env开始，映射到CodeGraph中的CPU检测、Worker/线程池建立、Executor、有界Queue、入场限流、负载抛弃和拒绝策略。必须选择可达业务入口并证明存在实际容量上限。

主动查找反证：基于cgroup的CPU感知Worker计算、有界队列、并发Limiter、负载拒绝以及能在队列耗尽前生效的扩容。单有HPA不能自动排除过载保护缺陷，必须检查指标、启动时间和扩容窗口。

只有静态边界错误、可达业务路径、精确Pod绑定与独立Oracle同时成立才输出candidate。Oracle至少包含CPU/throttling故障效果、入口SLO/拒绝率、队列/在途数和故障后排空恢复。如果主导机制是两条路径共享同一资源对象，必须路由到RD-09。

`available_fault_types`只能包含`cpu-load`，目标只能是精确Pod或Container。过载保护缺失通常属D1，参数/预算失配通常属D4，最终必须依静态机制证据选择。
