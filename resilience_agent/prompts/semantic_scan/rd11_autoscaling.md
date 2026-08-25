# RD-11 自动扩缩容策略失效

以具体Deployment为单位关联HPA/KEDA targetRef、资源Request、扩容指标、target、min/max replicas、scaleUp窗口、启动耗时和Readiness。

不得仅因为“没有HPA”就匹配；必须先证明该组件的容量契约要求自动扩缩。本模板主要匹配已有策略的指标/参数错误，D类为D4。故障为traffic-ramp、cpu-load或metric-delay。
