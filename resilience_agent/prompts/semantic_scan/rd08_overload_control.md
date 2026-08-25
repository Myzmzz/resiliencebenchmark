# RD-08 过载保护、限流或拒绝策略缺失

检查真实入口Route上的Gateway/Ingress限流、应用Middleware注册、令牌桶/并发Limiter和队列生产者入场上限。同时核对HPA是扩容机制还是被错当成限流机制。

无Limiter只有在存在有界容量和可达过载入口时才是候选，D类为D1。故障为cpu-load或traffic-spike，故障对象为具体Pod/Service，不得写成“整个集群”。
