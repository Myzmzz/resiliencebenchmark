# RD-01 请求生命周期边界失效

从真实入口或Route沿CodeGraph追踪到下游HTTP/gRPC/DB/MQ调用。必须穿过客户端包装、拦截器和框架层，分别检查超时的创建、传递、消费和子预算关系。

仅出现`fetch`、`requests`、`http.Client`等调用不是证据。必须主动查找统一包装中的AbortSignal、Context Deadline、CancellationToken、网关/Service Mesh timeout和默认客户端配置。

D类按实例选择：机制完全缺失=D1；进入边界存在但子调用未覆盖=D2；子预算大于父预算=D4。适用故障只能从network-delay、network-loss、process-stop、http-delay中选择。
