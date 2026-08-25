# RD-10 CPU配额与线程或Worker并发失配

关联Kubernetes CPU Request/Limit、Runtime识别到的CPU数、线程池/Worker配置、请求并发与阻塞操作。需要给出参数来源，不得使用跨语言统一的任意“最佳比例”。

满足以下任一静态信号即可输出候选：存在CPU limit但未发现运行时CPU感知参数；线程池或worker为硬编码常量且明显高于CPU limit；阻塞调用运行在固定大小池内。持续Throttling移入Oracle验证。D类为D4，故障为cpu-load或cpu-quota-reduction。
