# RD-14 单副本与中断保护缺失

以Kubernetes Service选择的Workload为单位，检查replicas、PDB选择器、maxUnavailable/minAvailable、TopologySpread/AntiAffinity和实际关键路径。

`replicas: 1`本身不是缺陷；必须用CodeGraph和Controller可信的`resiliencebenchmark.io/source-symbol/source-path/source-identity/business-critical`映射证明它在关键业务路径上，其中`source-identity`必须与CodeGraph Manifest一致，并且没有等价副本。D类为D1，故障仅允许pod-delete或pod-fail，首版不允许Node级故障。

当`kubernetes_scope.authoritative_for_namespace=true`时，Controller声明该输入是本次名称空间完整期望配置：可以用它排除未出现的PDB、额外Workload副本和拓扑保护，但仍不得编造运行时Ready状态。

注意：PDB保护的是Eviction类中断，不保护直接Pod Delete/Fail。对ChaosBlade `pod-delete/pod-fail`的区分性只能来自“是否仍有其他已声明副本可承担服务”，不得用“无PDB”单独论证。
