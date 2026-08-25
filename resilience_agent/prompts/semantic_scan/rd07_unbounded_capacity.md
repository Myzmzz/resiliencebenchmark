# RD-07 无界并发、队列或连接池耗尽

定位队列/池/执行器的创建、容量、等待上限、Acquire和所有Release/异常路径，并与Kubernetes资源Limit及Worker并发配置关联。

“使用队列”不是缺陷。必须支持无界、容量明显超过Pod承载、等待无上限或异常路径泄漏之一，D类为D4。故障为network-delay、port-occupy、process-stop或jvm-threadfull。
