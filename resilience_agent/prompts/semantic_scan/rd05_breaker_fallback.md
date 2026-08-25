# RD-05 熔断、降级或回退机制失效

先确定依赖是关键还是可选，再检查调用链上的Circuit Breaker、Fallback、Partial Response、Cache/Source-of-Truth分工和异常映射。可选性契约可以来自feature flag定义、异常处理默认值、proto/schema可选字段、独立可选Service或配置开关。

熔断/回退完全不存在为D1；将可选失败错误升级为整体失败为D3；同步阻塞的保护路径反过来依赖脆弱组件为D6。故障限于network-delay、network-loss、process-stop和http-exception。
