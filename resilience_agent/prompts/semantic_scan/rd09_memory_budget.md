# RD-09 Runtime内存与容器内存预算失配

必须将Kubernetes `resources.requests/limits.memory`与同一容器的Runtime启动参数关联，例如JVM Xmx/MaxRAMPercentage、Node max-old-space-size、Go memory limit和Native/线程开销。

缺少Limit是另一类配置风险，本模板只匹配“机制存在但参数关系错误”，D类为D4。故障为memory-load或jvm-oom，目标必须绑定到有该配置的Pod/Container。

Deployment只是配置证据，不是本模板允许的最终注入目标。当证据来自Deployment时，`fault_injection_target.resource_kind`必须写`Container`，`resource_name`必须写实际容器名；只有在已有运行时Pod绑定证据时才能写`Pod`。不得输出`Deployment`作为注入目标类型。
