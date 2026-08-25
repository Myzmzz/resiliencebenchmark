# RD-02 多层重试放大与全局预算缺失

以一条真实业务调用边为单位，检查网关/Service Mesh、应用包装、SDK和数据库驱动是否同时重试。尽量给出各层attempt、timeout/deadline、backoff以及是否共享全局预算；缺失的参数写入残余假设。

只有单层有界重试不匹配。同一逻辑操作上存在两层及以上可独立重试机制，且未发现共享预算证据时即可输出候选；乘法放大由network-loss、network-drop、http-exception或process-stop实验区分。D类固定为D5。
