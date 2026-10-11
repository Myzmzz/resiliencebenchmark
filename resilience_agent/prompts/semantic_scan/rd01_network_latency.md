# RD-01 风险：下游调用缺少超时和取消边界；后果：请求长时间阻塞，超时后内部工作仍继续

本模板只使用ChaosBlade原子故障`network-delay`，对应`k8s pod-network delay`。原子故障只提供触发，不是缺陷证据。

先证明适用性：从可达Route、Handler、RPC入口或消息处理入口出发，用CodeGraph追踪到精确的跨进程依赖。如果没有远程依赖或路径不可达，返回`not_found`，不得套用此模板。

对真实调用链逐层检查Deadline/超时的创建、传播和消费，取消是否继续传入子调用，子调用预算是否小于父请求剩余预算，以及父请求结束后内部工作是否仍继续。必须反向查找网关、Service Mesh、统一客户端与拦截器中的实际超时/取消保护。

只有“可达依赖边+预算/取消边界缺口+父请求失败后子工作持续+可绑定Pod”都有证据时才输出candidate。单次`context`未找到超时不能证明全局缺失；覆盖不足时返回`insufficient_evidence`并记录未覆盖的入口、包装层和配置面。

边界必须严格：多层重试/远程写入未知结果属于RD-02；两条路径共享同一可耗尽资源属于RD-09。本模板不得因其他模板的主导机制重复输出候选。

`available_fault_types`只能包含`network-delay`，目标必须是精确Pod、Container或ServiceDependency。D类按实际静态机制在模板允许范围内选择。Oracle必须同时覆盖故障生效、独立业务SLO、父子请求时序和故障清理后的恢复。
