# RD-06 隔舱与关键请求容量隔离缺失

在CodeGraph中分别定位关键和非关键入口，追踪到Executor、Connection Pool、Queue、Semaphore或Worker Group。同一可耗尽资源对象被两个及以上业务入口共享，且未发现分区配额或独立池证据时即可输出候选。

共用一个进程或Pod不等于共享容量。扫描阶段给出“慢非关键路径→共享池耗尽→关键路径饥饿”的可验证机制假设；是否真的饥饿由network-delay、process-stop或http-delay实验区分。D类为D1，目标优先选择非关键依赖。
