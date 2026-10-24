# 可信与安全计算的基本概念与分类（TDSC 2004）

- 全称：*Basic Concepts and Taxonomy of Dependable and Secure Computing*
- 作者：Avižienis、Laprie、Randell、Landwehr
- 缓存：`docs-cache/LIT-TAXONOMY-TDSC04.txt`
- 用途：**校准概念边界**。它不给微服务的超时/重试/熔断清单，但它能防止你把"缺陷、注入的扰动、内部错误状态、对外失效"混为一谈。

## 一、三个词与那条链

- **failure（失效）**：一个**事件**，发生在交付的服务偏离正确服务的时候。交付不正确服务的那段时间叫 **service outage**；从不正确服务回到正确服务叫 **service restoration**。
- **error（错误）**：系统总状态中**可能导致后续服务失效的那一部分**。注意"可能"——**很多错误永远到不了系统的外部状态，也就不会造成失效**。
- **fault（故障）**：被判定或假设为错误起因的东西。**产生错误时故障是 active 的，否则是 dormant（休眠）的。**

**传播链**（§3.5）：

1. 故障激活 = 把某个输入（激活模式）施加到含休眠故障的组件上，使其变为活跃；
2. **组件内传播**：一个错误被依次变换成别的错误；**跨组件传播**：错误到达组件 A 的服务接口 → A 交付给 B 的服务变得不正确 → **A 的服务失效对 B 而言表现为一个外部故障**，并经 B 的使用接口把错误传进 B；
3. 错误传播到系统服务接口并使交付的服务偏离正确服务时，发生服务失效。

> **"A 的失效 = B 的故障"这条递归关系，正是你 d 类（放大与级联）的形式基础。**你的 `amplification_chain` 字段逐跳记录的就是这条链的实例。建议在 taxonomy.md 里把 d 类的定义挂到这条上——它给了"为什么错误出现的位置和注入位置不一致"一个精确的解释：**中间每一跳都在做"失效→故障"的转换，而每次转换都可能改变错误的形态**。

还有一个直接可用的区分：**detected error（已检测错误，有错误消息或信号指示）vs latent error（潜伏错误，存在但未被检测）**。

## 二、失效模式的四个视角（§3.3.1）——把你的 a/b 锚定住

服务失效模式按四个视角分类：

- **域（domain）**：
  - **内容失效** vs **时序失效**（早到 / 晚到）
  - 两者兼有时的两个特例：**halt failure**（服务停止，外部状态变成常量）；其特例 **silent failure**（服务接口上完全不交付服务，如分布式系统中不再发消息）
  - **erratic failure**（服务仍在交付但错乱，如 babbling）
- **可检测性（detectability）**：损失被检测到并以警告信号发出 → **signaled failure**；否则 → **unsignaled failure**。
  > **关键：论文明确写出"检测机制本身有两种失效模式：1) 没有真的失效却发出功能丧失的信号，这是 false alarm；2) 没有为功能丧失发出信号，这是 unsignaled failure"。**
  >
  > 这正是你 a/b 两类的经典对应：**a（该动不动）= unsignaled failure；b（不该动乱动）= false alarm**。你的 taxonomy.md 说"a 与 b 是一对镜像，区别在机制动了没有"，这个判断在 2004 年的分类里就已经是标准结构。把这条挂上去，你的 a/b 就不再是自己发明的分类，而是有四十年谱系的。
- **一致性（consistency）**：服务交付给两个及以上用户时，所有用户看到相同的不正确服务 → **consistent failure**；不同用户看到不同的不正确服务 → **inconsistent failure**，也叫**拜占庭失效**。
- **后果（consequences）**：按严重度分级，从 minor 到 catastrophic，每级一般关联一个可接受的最大发生概率。

**fail-controlled 系统**：只以规格里描述的特定模式失效、且程度可接受的系统。fail-halt / fail-stop（只有停止型失效）、fail-passive（输出卡住）、fail-silent（沉默）、fail-safe（所有失效都是轻微的）。

> 这套词汇可以直接用来写你的 `trigger` 字段：**Chaos Mesh 的 pod-kill 制造的是 fail-stop，NetworkChaos 的 delay 制造的是时序失效，HTTPChaos 的 abort 制造的是 signaled 的内容失效，DNSChaos error 制造的是……** 用这套词汇写，比用工具名写更能说清"注入的到底是什么"，也更容易说明"为什么这个注入不足以暴露那类缺陷"——例如 **pod-kill 永远造不出灰色失效，因为它是 fail-stop 的**。

**degraded mode（退化模式）**：失效导致服务模式降低时，系统会向用户发出退化模式的信号，范围从轻微降低到紧急服务、安全停机。论文把这称为功能或性能的**部分失效（partial failure）**。
> 你的 g 类要求"返回过期或错误内容**而无降级标记**"——"降级标记"的规范依据就是这里：**有信号的退化模式是正常的容错行为，无信号的才是缺陷**。

## 三、最该搬走的：coverage（覆盖率）

这是全篇对你最有用的一个概念，它给了"你的 benchmark 究竟在测什么"一个精确的名字。

> **任何给定容错技术的有效性度量称为它的 coverage。** 容错的不完美（即 coverage 的缺失）严重限制了可获得的可信性提升。

coverage 的缺失分成两支（论文 Fig. 18）：

1. **error and fault handling coverage 的缺失**：容错机制自身相对于开发时所声明的故障假设存在开发缺陷。定义为**在错误或故障已经发生的条件下，该技术有效的条件概率**。
2. **fault assumption coverage 的缺失**：实际发生的故障与假设的故障不同。再分两种：
   - **failure mode coverage 的缺失**：失效的组件没有按假设的方式行为。
     > **灰色失效就是这个**：机制假设组件 fail-stop，实际组件 fail-slow。你可以把 Gray Failure 整篇挂在这个概念下。
   - **failure independence coverage 的缺失**：假设失效独立，实际发生了共因失效。
     > **亚稳态失效的"多触发器"（45% 的事故有多于一个触发器）和"高冗余反而伤害可用性"都属于这一支。**

论文还给了一条重要的警告：**保守的故障假设（例如假设拜占庭故障）会带来更高的 failure mode coverage，但代价是需要增加冗余和更复杂的容错机制，这反而可能导致整体可信性与安全性下降。**

> **这三层分解可以直接成为你 benchmark 报告的顶层结构。**你测的不是"系统好不好"，而是**系统里每个韧性机制的 coverage，以及 coverage 缺失属于哪一层**：
> - 机制自身有实现缺陷（a/b/c 类的大部分）；
> - 机制的故障假设与实际故障不符——组件行为不符合假设（灰色失效、fail-slow）；
> - 机制的故障假设与实际故障不符——独立性假设不成立（共因、级联、亚稳态）。
>
> 这比"56 条表现"更容易向外部读者解释你在测什么，而且它是 2004 年就定下来的标准语言。

## 四、其他有用的定义

- **fault tolerance 是递归的**：实现容错的机制本身必须被保护，以免受到可能影响它们的故障。例子：表决器复制、自检的检查器、恢复程序和数据用的"稳定"存储。
  > 这一句是你整个研究方向的**一句话依据**：机制已经存在为什么仍然失效——因为机制本身也会有故障，而且通常没有人保护它。
- **容错的四个动作**（§5.2.1）：错误处理（rollback 回滚 / rollforward 前滚 / compensation 补偿）+ 故障处理（diagnosis 诊断 / isolation 隔离 / reconfiguration 重构 / reinitialization 重新初始化）。
  - rollback 与 rollforward **不互斥**：可以先试回滚，错误还在就再试前滚。
  - **fault masking（故障屏蔽）= 系统性地使用补偿**。论文警告：**这种屏蔽会掩盖保护性冗余的一种可能是渐进的、最终致命的丧失**，所以实践中的屏蔽一般要配合错误检测（masking and recovery）。
    > 这条正是你 c 类里"看似正常"场景的经典表述，1980 年代的语言。**一个把所有失败都屏蔽掉的系统，看起来最健康，实际最危险**，因为它耗尽冗余的过程是不可见的。
  - **preemptive error detection and handling（先发性错误检测与处理）**：系统上电时常做，运行期也有各种形式——备件检查、内存擦洗、审计程序，以及**软件复壮（software rejuvenation）**，目的是在软件老化导致失效之前消除其影响。
    > 这是 Microreboot"按部分复壮系统而不用整体停机"的理论出处。
- **dependability benchmark 的定义**：**"一种在故障存在的情况下评估计算机系统行为度量的流程"**，使各种故障预测技术能统一在一个框架下，用途是 1) 刻画系统的可信性与安全性，2) 按一个或多个属性比较可替代或竞争的方案。
  > 这句话可以直接放在你论文引言里定义你在做什么。
- **四种手段**：fault prevention（防止故障发生或被引入）、fault tolerance（在故障存在时避免服务失效）、fault removal（减少故障数量与严重度）、fault forecasting（估计当前数量、未来发生率和可能后果）。评估容错系统时，**错误与故障处理机制提供的 coverage 对可信性度量有决定性影响；coverage 的评估可以通过建模或通过测试（即故障注入）来进行**。
- **omission fault（遗漏故障）vs commission fault（过失故障）**：该做的动作没做 vs 做了错误的动作。**又一个 a/b 的谱系来源。**
- **elusive fault（难以捉摸的故障）与 intermittent fault（间歇性故障）**：难以捉摸的开发故障与瞬时物理故障的表现相似，因此被归为一类叫间歇性故障，它们产生的错误通常称为 **soft errors**。

## 五、怎么用这篇

不要整篇引用，它太长太抽象。**用它做三件事**：

1. **锚定 a/b**：unsignaled failure / false alarm，omission / commission。
2. **锚定 d**：失效→故障的递归传播链。
3. **给整个 benchmark 一个顶层叙述**：你测的是机制的 coverage，缺失分三层（机制自身缺陷 / failure mode coverage / failure independence coverage）。
