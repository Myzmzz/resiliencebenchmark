# 本轮收尾（2026-09-13）

分支 `codex/stage2-env3-bladeai-20260912`，从 `main` 的 `a7bea6b` 拉出。
**未推送、未动 main、未发起对 main 的合并。**

启动语原有三件事。**第三件（BladeAI 0.7.0 接入）经用户 2026-09-13 指示改到
另一条分支去做，本分支上与它相关的改动已全部撤销**，本文只记前两件。

---

## 一、第三套环境：验收三条全齐

| 验收项 | 结果 |
|---|---|
| 资格检查通过 | codex / claude-code / deepseek-harness 各自 `status=passed`，七项基础检查全 true |
| `/options` 三家可跑 | 三家 `runnable=true`，各带 4 个可跑模型 |
| 完整跑通一次 C0 | `experiment_gate=PASS`、`trial_validity=VALID`、`recovery_status=VERIFIED`、70/100 |

详见 [P7](stage2-env3-p7-options-20260912.md) 与 [P8](stage2-env3-p8-c0-20260913.md)。

### 两件值得记住的

**1. 七个模型全不可跑，真凶是这套集群半个 CoreDNS 是坏的。**
`otcaix-60` 上那个副本到 `8.8.8.8` / `1.1.1.1` / `159.226.8.6` 的 UDP 53 全部超时，
`otcaix-62` 上那个 10/10 正常。Service 在两者之间轮询，实测经它查
`dashscope.aliyuncs.com` 是 `SERVFAIL/2.0s` 与 `NOERROR/0.0s` **严格交替**，
于是 litellm 报 `OpenAIException - Connection error.`。

**共享集群的既有故障**（该副本 95 天没重启，otel-demo 和别人的 `aiops` 也在同样
报错），**没有动共享组件**，只给平台 Pod 加了一层 `dnsConfig` 兜底
（`dnsPolicy: ClusterFirst` 下是追加在 `10.96.0.10` 之后，集群内名字仍先问 CoreDNS，
只有 SERVFAIL 才落到下一台）。改完 15/15 全通，之前约 25%。真正的修法两条留给集群方。

**2. 仓库清单少第四样「渲染即丢」的设置。** `STAGE2_HARNESS_CAPABILITIES_FILE`
在 `stage2.yaml` 和 `stage2-integration.yaml` 里 `grep -c` 都是 0，而第二套环境线上
是设了的。少了它四家一律 `qualification_not_passed`，`/options` 一个可跑的都没有。
已并入 env3 overlay，`verify_stage2_deployment.py` 无条件核对这一项。

### C0 第一次没过，根因不是环境

第一次 `qwen3.8-max` 判 `CASE_INVALID`，五项实验闸只差 `business_recovery_verified`，
`reason_codes=["HARNESS_TIMEOUT"]`。从 `gateway-usage.jsonl` 算出来
**96% 的墙钟是模型推理**（24 次调用 1453s，最慢 190s，峰值输入 272,693 tokens），
调用间隔只有 4–7s。`run_harness_trial.py:60` 的 30 分钟是硬常量且无处可配——
**没有去改它**，为了让自己的验收过而改平台代码是不能干的。第二次只换成
`deepseek-v4-flash-0731`，664s 跑完，PASS。

otel-demo 是共享的，两次真注入前后都查了残留：CR 归零、任务归零、cart pod
139 天 0 重启、到 cart 的 TCP 回到 0.0001s（**独立实测的地面真相**，因为 P4b
已证明手工删 CR 并不会停）。

## 二、整改第一批：6 条全做完，未部署未跑评测

O03 / O04 / O08 / O10 / O18 / O19，代码 + 测试 + 改动文档
（[整改说明](stage2-dx-remediation-notes-20260912.md)）。线上跑的仍是原有镜像
`stage2-d0-60309d3`，两次 C0 用的都是它，**这批代码没有部署过**。

### O03 后来自查出一个真 bug，已修

拿一条**真实的欠费报文**去试，归类器返回的是 `BAD_REQUEST` 而不是 `ARREARAGE`——
**正是 O03 要消灭的那个错判**。报文里没有 `Arrearage`、没有 `quota`、没有 `balance`，
我原来那几条 marker 一条都不命中，因为我照着问题描述里的「HTTP 400 Arrearage」写，
**没照着真实报文写**。

更该记一笔的是：**原来的测试是绿的**——夹具 `ARREARAGE_BODY` 是我自己编的，
里面同时塞了 `Arrearage` 这个词和「in good standing」这句话，**夹具和代码共用了
同一个错误假设，只验证了我的假设，没验证现实**。

后果不只是标签难看：`ARREARAGE` 走**立即熔断**，`BAD_REQUEST` 不熔断，
归错类等于欠费时整轮评测继续往上撞。已补三条 marker 并用逐字抄来的真实报文做回归，
另加一条「普通 400 仍是 `BAD_REQUEST`」确保放宽没有吞掉正常客户端错误。

### 顺带修好的一项自查

部署后的 `private-file-modes` 检查原本只看文件位、不看目录链，
于是**任何跑过评测的环境都会失败**（MCP 日志是 0644，但被 0700 的目录挡着，
uid 10001 之外根本打不开）。P6 当时报「1 files」只是因为环境还干净。
已改成按**实际可达**判：某个类别要读到文件，既要文件对它可读，也要每一层祖先
目录对它可穿越，group 和 other 分开算；位松但被挡住的仍然列出来，只是不判失败。
采集格式也从 `-type f -printf '%m %p'` 改成 `-printf '%y %m %p'`——先前
「25 files」里混进了空目录。

---

## 三、已知未决

1. **本分支 33 个提交只在这台机器上，没推。** 推送是对外动作，等授权。
2. **要不要补 `AIGCBEST_API_KEY` / `ACUCOMPUTE_API_KEY`**。不补就是 7 个模型里
   4 个可跑（含 C0 用的 `qwen3.8-max`），够用；补了另外三条路由也能进矩阵。
   现在那三个报的是干净的「认证/权限被拒」，不再是会误导人的 `Connection error.`。
3. **`tests/test_system_snapshot.py::test_observation_adapter_uses_fixed_service_proxy_queries`
   仍然失败**，原因是测试里写死了前一位操作者主目录下的绝对路径
   （`configured kubeconfig does not exist`），与被测行为无关。
   **另一条分支上已有修复**（改用 `tmp_path`），随那边一起进来即可，本分支不重复带。

除上面第 3 条外，全量 `uv run pytest` 通过。
