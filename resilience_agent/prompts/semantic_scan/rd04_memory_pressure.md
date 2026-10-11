# RD-04 风险：内存预算和数据结构容量缺少上限；后果：服务OOM、卡顿或反复重启

本模板只使用ChaosBlade原子故障`memory-load`，对应`k8s pod-mem load`。内存压力、OOM或Pod重启都只是可能的触发/现象，不能单独当作缺陷。

从同一容器的memory request/limit、command/args/env出发，查找JVM Heap/MaxRAMPercentage、Node max-old-space-size、Go memory limit等Runtime预算，并保留Native内存、线程栈和运行时开销的区分。用CodeGraph检查Cache、Queue、Buffer、聚合结构和本地状态的容量上限、淘汰/释放、异常路径和背压。

主动查找硬上限、LRU/TTL、spill、backpressure、软阈值退化和超限拒绝等反证。状态持久化、事务、重放与业务对账属于RD-10，不得在本模板重复输出。

候选必须形成“预算/无界结构弱点→memory-load激活→GC/分配/OOM/重启传播→SLO或有效吞吐损害”的证据链。仅“没有memory limit”不足以输出此模板候选，因为它没有给出可区分的预算错误。

`available_fault_types`只能包含`memory-load`，目标必须是精确Pod或Container。Oracle要同时检查working set/RSS与GC/OOM/restart、业务SLO、有效吞吐以及清理后是否仍有压力进程或重启循环。
