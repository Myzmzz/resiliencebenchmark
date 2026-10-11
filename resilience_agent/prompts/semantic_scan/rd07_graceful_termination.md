# RD-07 风险：服务关闭前未排空在途工作；后果：已接收的请求或消息丢失、重复执行

本模板只使用一次性`pod-delete`，对应`k8s pod-pod delete`。Pod可被删除不是缺陷；候选必须定位到已接收的请求、长连接、消息或后台任务，并证明终止时序会造成丢失、重复或SLO损害。

用CodeGraph追踪SIGTERM/Shutdown Hook、Server Shutdown、停止接收新工作、在途计数与等待、连接关闭、Worker Stop、消息确认和副作用完成顺序。同时核对Kubernetes preStop、terminationGracePeriodSeconds、Service Endpoint摘除与应用关闭的时序。框架默认关闭机制也是反证，不得因没有自定义Signal Handler就断言缺陷。

候选至少要证明：真实在途工作路径、终止/摘流/排空的缺口、可绑定Pod，以及能区分丢失/重复的独立Oracle。注入必须对齐到已证明的在途窗口，否则实验无效，不能进入pass/fail。

仅因单副本容量清空属于RD-05；替换Pod启动/就绪属于RD-08；持久状态部分写或重放错误属于RD-10。Oracle必须检查旧UID、Endpoint摘除、在途工作结果、状态对账与真实业务SLO，不得用Pod Ready代替。
