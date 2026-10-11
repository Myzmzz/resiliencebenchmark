# RD-06 风险：健康检查与真实业务状态不一致；后果：故障实例继续接收流量或正常实例被重启

本模板使用`network-delay`或`pod-fail`中能区分候选机制的一个最小ChaosBlade原子故障。故障本身不是缺陷；必须证明Probe/Health Handler对真实服务能力做出错误分类，并改变了Kubernetes重启或Service Endpoint路由。

从Deployment/StatefulSet的livenessProbe、readinessProbe、startupProbe或exec命令开始，精确定位应用侧Health Handler。用CodeGraph追踪Handler返回条件、下游调用、初始化标志和Serving状态，同时核对timeoutSeconds、periodSeconds、failureThreshold和StartupProbe。必须继续映射到Service与EndpointSlice，不能只停在一个`/health`符号。

主动查找反证：Liveness只检查进程内部不可恢复状态；Readiness在必需能力缺失时移出Endpoint；StartupProbe与阈值覆盖最坏启动时间。“没有Probe”不足以匹配，还必须有可证明的健康路由契约与业务损害路径。

稳态Health误分类属于本模板；只有Deadline/取消错误属于RD-01；只在新实例启动/恢复期间过早Ready或振荡属于RD-08。`available_fault_types`只能选`network-delay`或`pod-fail`中一个，Oracle必须联合Probe、Pod Condition、Ready Endpoint、重启与独立业务SLO。
