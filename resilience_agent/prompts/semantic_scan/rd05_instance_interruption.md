# RD-05 风险：关键服务缺少可接替的有效副本；后果：单个实例故障导致整体服务不可用

本模板使用两个ChaosBlade原子故障：一次性`pod-delete`对应`k8s pod-pod delete`，持续`pod-fail`对应`k8s pod-pod fail`。两者分别验证替换恢复和持续容量丢失，不得混合为一个复合故障。

先用CodeGraph证明组件位于可达的关键业务路径，再通过Controller可信映射关联到同一源码快照的Service、Workload和Pod。检查有效Ready副本数、拓扑分散、Service/EndpointSlice、PDB以及是否存在等价容量。`replicas: 1`本身不是缺陷，PDB也不能单独证明对直接Pod Delete/Fail有保护。

本模板只调查等价副本、拓扑隔离、Service Endpoint与故障转移容量。SIGTERM/排空属于RD-07，替换实例启动/就绪属于RD-08，持久状态/重放属于RD-10。

只有“关键路径+可绑定实例+单Pod中断会清空实际服务容量+独立业务Oracle”成立才输出candidate。如果只能证明Pod可被删除或`replicas: 1`，必须返回`insufficient_evidence`或`not_found`。

`available_fault_types`只能从`pod-delete`和`pod-fail`中选择；每个finding应选择能区分其机制的最小原子故障。Oracle必须检查旧Pod UID、Ready Endpoint、剩余副本的实际业务承载和容量，不得用ChaosBlade Running、Pod Ready或destroy成功代替业务恢复。
