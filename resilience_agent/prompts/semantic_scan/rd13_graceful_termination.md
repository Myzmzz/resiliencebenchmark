# RD-13 优雅终止与连接排空缺失

将Kubernetes preStop、terminationGracePeriodSeconds、Service Endpoint摘除与应用SIGTERM Handler、Server Shutdown、Keep-Alive关闭、在途请求等待链关联。

暴露HTTP/gRPC/长连接入口且未发现SIGTERM处理、Server Shutdown、preStop或连接排空证据时即可输出候选；应用可能依赖框架默认优雅终止时写入残余假设。D类为D1，故障为pod-delete、process-kill或process-stop。
