# Kubernetes Scheduler 全景：从一个 Pending Pod 到业务平台、GPU 与源码

> 新手独立篇：写给会维护 Kubernetes、但第一次系统学习 scheduler 和 Go 源码的运维工程师

这篇讲义不要求你先读本目录的第 01 篇，也不要求你已经掌握 Go。你只需要知道 Pod、Node、Deployment、request、taint 这些日常运维概念。

本文只抓住一条中心因果链：

> 一个尚未绑定 Node 的 Pod，怎样进入 kube-scheduler，经过排队、硬条件筛选、软偏好打分、内存预占和绑定，最终把选中的 Node 持久化到 `Pod.spec.nodeName`；如果没有可行 Node，它又怎样等待真正可能改变结果的事实。普通业务 Pod 与 GPU Pod 共用这条主线，但 GPU 还多出设备发现、节点内设备分配、拓扑、共享、队列和成本等账本。

---

## 0. 先说学习结果：读完后你应该能做什么

第一遍读完，你应该能做到：

1. 看到 `Pending` 时，先判断问题是否真的还在 scheduler 责任域。
2. 不看源码也能手算一个 Pod 为什么被某些 Node 过滤掉。
3. 用大白话解释 `PreFilter -> Filter -> Score -> Assume -> Reserve -> Permit -> Bind`。
4. 说清楚 `NODE=<none>`、`SuggestedHost`、`nominatedNodeName` 和 `spec.nodeName` 的区别。
5. 解释为什么 CPU 使用率很低，仍可能出现 `Insufficient cpu`。
6. 解释为什么 priority 很高，也不能绕过硬约束。
7. 解释为什么 scheduler 选了 GPU Node，却不知道容器最后拿到哪个 GPU UUID。
8. 区分 kube-scheduler、Device Plugin、kubelet DeviceManager、Kueue 和 Volcano 的职责。

第二遍再追这些实现细节：

- informer cache 与 scheduler cache 的时间差；
- `activeQ`、`backoffQ`、`unschedulablePods` 和 in-flight 事件补偿；
- QueueingHint、并行 Filter、候选 Node 早停；
- Reserve/Unreserve、Permit Wait、Bind 失败补偿；
- 抢占候选、PDB best effort 和异步驱逐；
- 多 profile、extender、DRA、PodGroup 与当前源码中的实验性分支。

如果你第一遍只想抓主线，按这个顺序读：

```text
第 2 节：scheduler 到底管什么
  -> 第 3 节：手算生产案例
  -> 第 4 节：对象和状态所有者
  -> 第 5 节：为什么这样设计
  -> 第 6 节：Framework 流水线
  -> 第 7 节：源码调用链
  -> 第 8 节：request 账本
  -> 第 11 节：选中 Node 后怎样绑定
  -> 第 12 节：失败为什么不会空转
  -> 第 15 节：GPU 迁移
  -> 第 19 节：值班排障树
```

---

## 1. 当前源码基线、事实边界与阅读约定

### 1.1 固定版本

| 项目 | 本文使用的事实 |
|---|---|
| 本地源码目录 | `D:\datou\devops\kubernetes-master\kubernetes` |
| 完整 commit | `301946d15e67a4a2e8a5fb8292eb836acd366d78` |
| `git describe` | `v1.37.0-alpha.0-280-g301946d15e6` |
| commit 日期 | `2026-04-24T23:06:46+05:30` |
| 验证强度 | 当前 checkout 静态阅读、调用关系核对、定向机械检查；第 1.4 节如实记录已执行与未执行项 |

这份 checkout 是上游开发快照，不是某个生产发行版。它已经包含一些较新的分支，例如：

- `GangScheduling` / PodGroup；
- `GenericWorkload`；
- `OpportunisticBatching`；
- `NominatedNodeNameForExpectation`；
- DRA 及 extended resource 到 DRA 的委托路径；
- Topology-aware workload scheduling 的实验性代码。

所以本文分成两层：

- **稳定主线**：单 Pod 的排队、Filter、Score、Assume、Bind、失败重试。这是生产排障必须掌握的骨架。
- **当前快照增强**：受 feature gate、API 版本和发行版影响的功能。它们会单独标出，不能直接套到你的集群。

生产排障时，第一件事不是相信本文行号，而是先确认目标集群的 Kubernetes 版本、kube-scheduler 配置和 feature gates，再切到对应 tag 重新核对。

### 1.2 三类表述不要混

| 标签 | 含义 |
|---|---|
| **源码事实** | 能在上述固定 commit 的文件、函数和控制流中直接验证 |
| **官方语义** | 由 Kubernetes、NVIDIA、Kueue 或 Volcano 官方文档定义 |
| **教学模型** | 为了让新手快速建立因果关系所做的简化；它不能替代版本核验 |

### 1.3 源码阅读约定

本文 Go 代码块中的中文 `//` 注释是讲义新增，不是上游原注释。

- 标成“完整函数”的代码保留该函数全部业务分支，只增加中文解释。
- 标成“连续摘录”的代码来自同一连续区间；区间外内容会在代码块前交代。
- 标成“非连续检查点”的代码不能复制后独立编译，只用于对照控制流。
- 教学伪代码一律使用 `text`，不伪装成真实 Go。
- 每段真实源码后都按“输入、判断、动作、结果”收束，并只补当前真正需要的 Go 语法。

### 1.4 本文这次真正执行了哪些验证

截至本次落盘，实际完成的是：

```text
通过：固定 commit 的静态源码逐函数核对
通过：核心调度、GPU/DRA、平台运维三路独立只读审校并回修
通过：validate_lesson.py --self-test
通过：validate_lesson.py --require-java --require-gpu --strict
结果：errors=0，warnings=0

未执行：Kubernetes Go 单元测试
原因：本机 go version 为 1.19.4，而当前 go.work 要求 go 1.26.0；
      Go 1.19 在读取 go.work 时即因版本格式和 godebug 指令退出，测试尚未开始编译。

未执行：真实集群 apply、抢占、GPU 或 DRA 实验
原因：本文编写过程没有获得一套明确隔离的测试集群与设备环境。
```

因此，“讲义覆盖并通过静态审校”不等于“读者已完成实验”，也不等于当前开发快照已在本机通过全部测试。第 22 节是实验设计与验收思路，必须在隔离环境另行执行并保存证据。

---

## 2. 先用一句人话说清楚：scheduler 到底干什么

最短答案：

> kube-scheduler 给“还没有 Node 的 Pod”挑一台 Node，并通过 Binding 把结果持久化为 `Pod.spec.nodeName`。

这里有四个关键词。

### 2.1 “还没有 Node”

对普通 kube-scheduler 管理的 Pod，最直观的入口条件是：

```text
Pod.spec.nodeName == ""
Pod.spec.schedulerName 能匹配当前 scheduler 的某个 profile
```

如果用户或其他组件直接设置了 `spec.nodeName`，这个 Pod 已经被视为“已分配”，会绕过普通选点流程。静态 Pod 也不走这条普通调度主线。

### 2.2 “挑一台”

scheduler 不是找“宇宙中绝对最优”的 Node。它做的是：

1. 先排除绝对不能放的 Node；
2. 再给剩下的 Node 打分；
3. 在本轮评估集合里选择得分最高的候选；
4. 大集群还可能在找到足够数量的可行 Node 后提前停止继续扫描。

因此，“被选中”表示它通过了当前规则并在本轮候选里胜出，不表示它永远是全局最优，也不表示运行后性能一定最好。

### 2.3 “持久化”

在 Filter/Score 结束时，源码里先得到的是 `SuggestedHost`。它只是 scheduler 进程内的建议结果。

真正完成跨组件交接的是 Binding：API Server 接受绑定请求后，Pod 的 `spec.nodeName` 才成为其他组件可观察的持久事实。随后目标 Node 上的 kubelet通过 watch 看到这个 Pod，进入节点本地准入、挂卷、Sandbox、拉镜像、创建容器等流程。

### 2.4 “只负责选 Node”

scheduler 不负责：

- 拉镜像；
- 创建 Pod Sandbox；
- 调 CNI 配网络；
- 调 CSI 挂载卷的节点侧动作；
- 启动 Java 进程；
- 执行 readiness/liveness probe；
- 给 GPU Pod 选择具体 GPU UUID；
- 把 `/dev/nvidia*`、CDI device、驱动库注入容器；
- 保证应用真正达到延迟或吞吐 SLO。

这张图从左往右读。实线是本轮主动作，虚线是 watch/informer 驱动的异步传播；方框是组件或动作，箭头文字是交接事实。

```mermaid
flowchart LR
    U["用户或控制器创建 Pod<br/>spec.nodeName 为空"] --> A["API Server<br/>保存最终 Pod"]
    A -. "watch 对象变化通知" .-> S["kube-scheduler<br/>排队、筛选、打分"]
    S --> B["Binding API<br/>持久化 spec.nodeName"]
    B -. "watch 已绑定 Pod" .-> K["目标 Node 的 kubelet"]
    K --> R["本地准入、卷、网络、CRI、容器"]
    R --> APP["Java / GPU 业务开始运行"]
```

注意：图里的“对象变化通知”不是 `kubectl get events` 看到的 Kubernetes Event 对象。前者是 informer/watch 接收的对象变化；后者是组件额外写入 API 的可观察记录。

### 2.5 四个名字最容易混

| 名字 | 在哪里 | 大白话 | 是否已经正式绑定 |
|---|---|---|---|
| `NODE=<none>` | `kubectl get pod` 的展示 | API 中还看不到 `spec.nodeName` | 否 |
| `SuggestedHost` | scheduler 本轮内存结果 | Filter/Score 选出的建议 Node | 否 |
| `status.nominatedNodeName` | Pod status | 抢占等流程认为“未来很可能去这里” | 否 |
| `spec.nodeName` | Pod spec | 已经持久化的节点交接结果 | 是 |

`nominatedNodeName` 不是预绑定，也不是资源锁。被提名的 Pod 下一轮仍要重新通过 Filter。

---

## 3. 先不敲命令：手算一次 Java 生产发布

这是教学整理后的生产型案例，不是某个真实公司的原始事故记录。数字会贯穿整篇，不会在中途偷偷换题。

### 3.1 业务背景

- namespace：`prod`
- Deployment：`order-api`
- 应用：Spring Boot；启动阶段有类加载、JIT 和缓存预热，readiness 通过后才接流量
- 滚动策略：`maxSurge: 1`、`maxUnavailable: 0`
- 当前已有 4 个旧 Pod；发布新版本时多创建第 5 个 surge Pod
- 新 Pod 包含业务容器和 service-mesh sidecar
- 最终保存到 API Server 的 request：

| 容器 | CPU request | memory request |
|---|---:|---:|
| `order-api` | `1200m` | `1536Mi` |
| `mesh-proxy` | `200m` | `256Mi` |
| **整个 Pod 常驻阶段合计** | **`1400m`** | **`1792Mi`** |

这里故意使用 request，而不是 Java 进程此刻的 CPU 使用率。scheduler 做的是容量承诺：只要 Pod 还被视为占用该 Node，这份 request 就在账上。readiness 为 False 也不会自动把 request 从 scheduler 账本里减掉。

Pod 的相关约束可以简化成：

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: order-api-new-7f8d9
  namespace: prod
  labels:
    app: order-api
spec:
  schedulerName: default-scheduler
  nodeSelector:
    workload-tier: online
  containers:
    - name: order-api
      image: registry.example.invalid/order-api:v2
      resources:
        requests:
          cpu: 1200m
          memory: 1536Mi
    - name: mesh-proxy
      image: registry.example.invalid/mesh-proxy:v1
      resources:
        requests:
          cpu: 200m
          memory: 256Mi
```

镜像地址是教学占位值，不需要执行。

### 3.2 三台 Node 的当前调度账

只列与本案有关的事实：

| Node | `workload-tier` | taint | CPU Allocatable | 已计入 Requested | CPU 余额 | 对本 Pod 的结论 |
|---|---|---|---:|---:|---:|---|
| `worker-a` | `online` | 无 | `4000m` | `3200m` | `800m` | 资源不足 |
| `worker-b` | `batch` | 无 | `8000m` | `5000m` | `3000m` | label 不匹配 |
| `worker-c` | `online` | `dedicated=gpu:NoSchedule` | `8000m` | `5500m` | `2500m` | Pod 没有对应 toleration |

把本 Pod 的 `1400m` 代进去：

```text
worker-a: 1400m > 4000m - 3200m = 800m     -> NodeResourcesFit 拒绝
worker-b: CPU 足够，但 workload-tier != online -> NodeAffinity 拒绝
worker-c: CPU 足够、label 匹配，但不容忍 NoSchedule taint -> TaintToleration 拒绝
```

结果是 0 个可行 Node。正确动作不是“挑一个差不多的”，而是保持 Pod 未绑定并记录失败原因。

### 3.3 先预测，再往下读

请先自己回答：

1. `worker-b` CPU 很富余，scheduler 能不能忽略 nodeSelector？
2. `worker-c` 资源足够，scheduler 能不能因为发布着急就绕过 taint？
3. `worker-a` 此刻 `kubectl top node` 只显示 15% CPU，能不能据此判定它放得下？
4. 把 Pod priority 调到最高，能不能解决 label 或 taint 不匹配？
5. 失败一次后，scheduler 应不应该每毫秒重试一次？

答案全部是否定的。

### 3.4 唯一改变题目的事实

一分钟后，`worker-a` 上一个无关的批处理 Pod 完成并被删除，它原来占 `1000m` request。

```text
删除前：worker-a Requested = 3200m，余额 = 800m
删除后：worker-a Requested = 2200m，余额 = 1800m
本 Pod：request = 1400m
结论：1400m <= 1800m，资源条件现在通过
```

因为 `worker-a` 的 label 匹配且没有拒绝本 Pod 的 taint，它成为唯一可行 Node。当前源码在 `len(feasibleNodes) == 1` 时直接使用这个 Node，不需要再运行 Score 来比较多个候选。

### 3.5 过滤矩阵图

这张图从上往下读。菱形是必须回答“是/否”的硬条件；任何一项失败都会淘汰当前 Node。Score 只会接收全部硬条件都通过的 Node。

```mermaid
flowchart TD
    P["order-api-new<br/>CPU request=1400m"] --> A{"Node label 匹配吗？"}
    A -- "否：worker-b" --> X1["淘汰"]
    A -- "是" --> B{"NoSchedule taint 被容忍吗？"}
    B -- "否：worker-c" --> X2["淘汰"]
    B -- "是" --> C{"Allocatable - Requested >= 1400m？"}
    C -- "否：释放前的 worker-a" --> X3["淘汰"]
    C -- "是：释放后的 worker-a" --> F["Feasible Node"]
    F --> S["若有多个候选才进入 Score 比较"]
```

### 3.6 这个现场真正暴露的不是“scheduler 坏了”

它暴露的是发布容量与平台策略共同作用：

- `maxSurge: 1` 允许临时多出一个 Pod，但不凭空创造 Node 容量；
- `maxUnavailable: 0` 保护业务可用性，也使平台更需要预留发布余量；
- nodeSelector 把在线业务限制到特定节点池；
- GPU 节点 taint 防止普通业务误占昂贵资源；
- request 决定 scheduler 的容量承诺；
- readiness 决定业务能否接流量，但不替代 scheduler request 账。

正确的平台问题不是“怎么让 scheduler 强行选一台”，而是：发布冗余、节点池规模、request 基线和隔离策略是不是一起设计过。

---

## 4. 一张全景图：Pod 从创建到应用运行

这张图按时序从上往下读。实线是函数调用或 API 动作；虚线是异步对象传播。左侧是控制面，右侧是目标节点。

```mermaid
sequenceDiagram
    participant C as Deployment/用户
    participant A as API Server
    participant I as Informer/Cache
    participant Q as SchedulingQueue
    participant S as ScheduleOne
    participant F as Framework plugins
    participant B as Binding API
    participant K as kubelet
    participant D as DeviceManager/Runtime

    C->>A: 创建最终 Pod，spec.nodeName 为空
    A-->>I: watch 到 Pod 对象变化
    I->>Q: 未绑定且 schedulerName 匹配，尝试入队
    Q->>S: Pop 一个待调度 Pod
    S->>F: PreFilter / Filter / Score
    alt 没有可行 Node
        F-->>S: Unschedulable / FitError
        S->>Q: 进入等待或退避，等相关事实变化
        S->>A: 写 PodScheduled=False 与 FailedScheduling Event
    else 找到候选 Node
        F-->>S: SuggestedHost
        S->>S: Assume，本地先占账
        S->>F: Reserve / Permit
        S->>B: PreBind / Bind
        B->>A: 持久化 spec.nodeName
        A-->>K: watch 到属于本 Node 的 Pod
        K->>D: 节点本地准入、设备/卷/CRI
        D-->>K: 创建 Sandbox 和容器
    end
```

### 4.1 五个状态所有者

| 状态 | 主要所有者 | 谁读 | 谁写 | 是否持久化 |
|---|---|---|---|---|
| Pod 声明的 request/affinity/toleration | Pod spec | scheduler、kubelet、控制器 | 用户、控制器、admission | 是 |
| Node Capacity/Allocatable/labels/taints | Node API 状态与 spec/metadata | scheduler、平台控制器 | kubelet、节点/平台控制器 | 是 |
| 内部调度队列位置 | kube-scheduler | kube-scheduler | kube-scheduler | 否，进程内 |
| assumed Pod 与 NodeInfo Requested | scheduler cache | kube-scheduler | kube-scheduler + informer 事件 | 否，进程内 |
| 最终选中的 Node | `Pod.spec.nodeName` | kubelet、控制器、用户 | Binding 路径 | 是 |

### 4.2 五条必须守住的不变量

1. **硬约束失败的 Node 不能靠高分翻盘。**
2. **同一份资源不能同时承诺给多个 Pod。** 所以 Bind 尚未完成时也要先 Assume 记账。
3. **内部 Error 不能伪装成业务资源不足。** 否则运维会朝错误方向扩容或改 YAML。
4. **失败不能变成热循环。** 只有退避到期或相关集群事实变化后才值得重试。
5. **只有持久化绑定才完成跨组件交接。** 内存里的候选、提名和预占都要允许失败后补偿。

---

## 5. Kubernetes 为什么把 scheduler 设计成现在这样

### 5.1 为什么不直接看实时利用率

直觉方案是：“哪台机器 CPU 当前最闲，就把 Pod 放过去。”它的问题是：

- CPU 使用率是瞬时值，几秒后可能完全不同；
- 新 Pod 还没启动，实时指标里没有它未来的负载；
- Java 应用会经历启动、JIT、GC、流量峰值，当前低利用率不能代表未来安全；
- Metrics Server、Prometheus 与 scheduler cache 存在不同采样周期和延迟；
- GPU 利用率为 0 也不代表设备没有被某个长任务占用或保留。

所以默认资源 Fit 依据的是声明的 request 承诺：

```text
可调度条件：PodRequest <= NodeAllocatable - NodeRequested
```

收益是决策可重复、可手算、能做容量规划；代价是 request 填错时，调度结果也会跟着失真。

### 5.2 为什么 Filter 和 Score 不能混成一个总分

假设某 Node：

- CPU 很空闲，得 100 分；
- 但有一个 Pod 完全不能容忍的 `NoSchedule` taint。

如果所有规则都混成加减分，这台 Node 可能靠 CPU 高分抵消 taint 失败，最终得到一个根本不能接受的结果。

因此 scheduler 先做硬筛选：

```text
Filter：能不能放？任何硬条件失败就是不能。
Score：都能放时，更愿意放哪一台？
```

### 5.3 为什么不每次直接读 API Server

一个大集群每秒可能有大量 Pod、Node、PVC、PV 和其他对象变化。如果每评估一台 Node 都同步请求 API Server：

- 延迟会很高；
- API Server 压力会很大；
- 同一轮决策读到的对象版本可能前后不一致；
- 网络抖动会把正常调度变成大面积失败。

所以 scheduler 通过 informer/watch 维护本地对象视图，再构建 scheduler cache 和本轮 snapshot。收益是吞吐和一致的本轮视图；代价是它与 API Server 之间存在短暂时间差，源码必须设计 Assume、Forget、事件补偿和失败重试。

### 5.4 为什么选点串行、绑定并发

选点阶段会修改 scheduler 自己的资源承诺账。为了避免两个调度周期同时基于相同旧账做出冲突决定，普通主线的 scheduling cycle 按串行入口运行。

Binding 需要访问 API 或外部系统，延迟更不可控。Assume 已经在本地提前占账后，绑定可以放到 goroutine 并发执行，不必挡住下一个 Pod 的选点。

收益：提高吞吐。

代价：系统出现“已经 Assume、尚未 Bind”的中间状态，后续失败必须执行 Unreserve 和 Forget。

### 5.5 为什么做成 Framework 插件流水线

调度规则天然很多：资源、标签、污点、卷、端口、拓扑、亲和性、抢占、设备。把所有逻辑写进一个大函数会导致：

- 任意一条规则改动都影响核心；
- 很难替换企业策略；
- 很难单独测试和观测；
- 不同工作负载无法使用不同组合。

Framework 保留轻量主干，把具体规则放到扩展点。代价是插件顺序、状态码、回滚和性能都需要严格契约，平台团队不能只看插件名字就上线。

### 5.6 为什么失败后要等“相关事实”变化

本案由 `NodeResourcesFit` 拒绝。真正可能改变结果的事实包括：

- 已绑定 Pod 删除，request 账释放；
- Node 新增；
- Node Allocatable 增加；
- 目标 Pod 自己的 request 变小。

一个无关 ConfigMap 更新通常不会让 CPU 余额增加。如果任何事件都唤醒所有失败 Pod，大集群会发生无效重算风暴。

QueueingHint 的核心思想是：

> 上轮由哪个插件拒绝，就优先问那个插件“这次变化是否可能让结果不同”。

---

## 6. 调度器内部不是一条队列，也不是一个算法

### 6.1 四类内部状态先翻成人话

对新手来说，最容易记错的是把所有 Pending Pod 都叫“在 scheduler 队列里”。API 里的 `Pending` 只是 Pod phase；scheduler 内部还要区分它现在为什么没有被处理。

| 内部位置/状态 | 大白话 | 典型进入原因 |
|---|---|---|
| `activeQ` | 现在就值得拿出来试一次 | 新 Pod、退避结束、相关事实变化 |
| `backoffQ` | 值得再试，但先冷静一下 | 连续失败或内部错误，防止热循环 |
| `unschedulablePods` | 暂时没有新证据，先别白算 | 某插件明确拒绝，等待相关对象变化 |
| in-flight | 已经 Pop，当前正被某轮调度处理 | 用于记录这段窗口里发生的集群变化 |

有些资料把前三者简称“三队列”，但当前实现中 `unschedulablePods` 更准确地说是不可调度 Pod 池；in-flight 还会配套保存事件历史。首遍记状态含义，第二遍再记具体数据结构。

默认 `PrioritySort` 不是简单 FIFO：先比较 Pod Priority；priority 相同时，更早进入队列的 Pod 优先。高优先级只改变“先轮到谁”和“是否可能抢占谁”，不改变 Node 的硬约束。

### 6.2 队列状态机

这张图从左往右读。方框是内部位置，实线是正常迁移，虚线是事件或超时触发。箭头上的文字是迁移条件。

```mermaid
flowchart LR
    NEW["未绑定 Pod"] -->|"PreEnqueue 通过"| A["activeQ<br/>现在可尝试"]
    NEW -->|"SchedulingGate 等门控"| U["unschedulablePods<br/>gated"]
    A -->|"Pop"| F["in-flight<br/>正在调度"]
    F -->|"成功 Bind"| DONE["离开调度队列"]
    F -->|"插件拒绝，暂无有用变化"| U
    F -->|"Error 或需要退避"| B["backoffQ<br/>等待退避"]
    U -. "相关对象变化 + QueueingHint" .-> B
    U -. "可能立即重试" .-> A
    B -. "退避完成" .-> A
    F -. "调度期间发生有用事件；失败落队时重新判定" .-> A
    F -. "若仍需退避" .-> B
    F -. "若变化无关" .-> U
```

当前固定提交的默认参数是首次退避 1 秒、最大 10 秒；不可调度池还保留 5 分钟超时刷新。它们是版本和配置相关的实现事实，不是跨版本永恒常量。

### 6.3 `Pop` 和 `Done` 不等于普通队列的取出与确认

当前源码里，`Pop` 还会：

- 增加调度尝试次数；
- 把 Pod UID 标记为 in-flight；
- 在事件链表中插入一个时间边界；
- 增加 scheduling cycle 计数。

`Done(uid)` 只表示：这次 in-flight 跟踪可以清理。它不表示 Binding 一定成功，不会删除 API Pod，也不会释放 scheduler cache 的资源账。

后面会把三个经常混淆的动作彻底拆开：

```text
Unreserve：撤销插件自己的预留状态
ForgetPod：撤销 scheduler cache 中的 assumed Pod 资源账
Done：结束调度队列的 in-flight 跟踪
```

### 6.4 Framework 扩展点：像一条有回滚能力的审批流水线

这张图从左往右读。绿色概念是“选 Node 前”，蓝色概念是“选出 Node 后”，红色虚线表示后续失败会触发补偿。

```mermaid
flowchart LR
    PE["PreEnqueue<br/>能否进入可运行队列"] --> QS["QueueSort<br/>谁先被处理"]
    QS --> PF["PreFilter<br/>Pod 级预计算"]
    PF --> F["Filter<br/>逐 Node 硬筛选"]
    F -->|"0 个可行 Node"| POST["PostFilter<br/>抢占等补救"]
    F -->|"有可行 Node"| PS["PreScore<br/>打分预计算"]
    PS --> SC["Score / Normalize<br/>逐 Node 加权评分"]
    SC --> AS["Assume<br/>scheduler cache 先占账"]
    AS --> R["Reserve<br/>插件预留"]
    R --> P["Permit<br/>放行、拒绝或等待"]
    P --> PB["PreBind<br/>绑定前动作"]
    PB --> B["Bind<br/>写绑定"]
    B --> POB["PostBind<br/>成功后通知/清理"]
    R -. "后续失败" .-> UR["Unreserve + Forget"]
    P -. "拒绝或超时" .-> UR
    PB -. "失败" .-> UR
    B -. "失败" .-> UR
```

### 6.5 每个扩展点到底回答什么

| 扩展点 | 新手问题 | 调用频率 | 失败后的大方向 |
|---|---|---:|---|
| PreEnqueue | 这个 Pod 现在允许进入 active/backoff 队列吗 | 每次准备入队前 | 门控在不可调度池等待相关变化 |
| QueueSort | 两个待调度 Pod 谁先出队 | 队列比较时 | 只能配置一个 QueueSort 实现 |
| PreFilter | 这个 Pod 能否先做一次公共计算，或缩小候选集合 | 每个 Pod 每轮一次 | 拒绝或 Error，停止正常选点 |
| Filter | 当前 Node 能不能运行这个 Pod | 每个候选 Node | 非 Success 淘汰该 Node；Error 可中断整轮 |
| PostFilter | 0 个可行 Node 后，能否为未来一轮创造条件 | 本轮无可行 Node 时 | 典型实现是抢占，不等于本轮直接 Bind |
| PreScore | 多个可行 Node 打分前，先算共享信息 | 每个 Pod 每轮一次 | Error 中断本轮 |
| Score | 当前可行 Node 有多符合偏好 | 每个可行 Node | 归一化、乘权重、求和 |
| Reserve | 选中 Node 后，插件要不要在内存中占位 | 每个建议 Node | 失败触发逆序 Unreserve 和 Forget |
| Permit | 能否绑定，还是等其他成员/外部条件 | 每个建议 Node | Success、Wait、Reject/Error |
| PreBind | 真正 Bind 前必须完成什么 | 每个待绑定 Pod | 失败触发回滚并重试 |
| Bind | 谁实际提交 Binding | 每个待绑定 Pod | 首个处理者结束链；Error 失败 |
| PostBind | 成功绑定后通知或清理什么 | 成功后一次 | 无 Status 返回，不能撤销已完成 Bind |
| EnqueueExtensions | 哪类对象变化值得唤醒被本插件拒绝的 Pod | scheduler 启动时注册，事件到来时判断 | 提高重入队准确度与吞吐 |

### 6.6 Status 不是一个简单布尔值

| Status Code | 大白话 | 运维理解 |
|---|---|---|
| `nil` / `Success` | 插件正常通过 | `nil` 在 Framework 里明确等价于成功 |
| `Unschedulable` | 当前条件不允许，但集群变化或 PostFilter 可能有帮助 | 业务拒绝，不代表 scheduler 崩溃 |
| `UnschedulableAndUnresolvable` | 当前连抢占通常也改变不了 | 仍可等待 Pod/Node/配置变化，不等于永久失败 |
| `Error` | 插件内部、输入或外部依赖发生非预期问题 | 通常按临时错误退避重试，需查日志和指标 |
| `Wait` | Permit 要暂缓绑定 | Pod 已有建议 Node，但仍未正式绑定 |
| `Skip` | 这个插件本轮不处理 | 不是报错，也不是普通业务拒绝 |
| `Pending` | 插件把本轮标成尚待外部事实，当前 cycle 结束 | 它属于 rejected 范围并记录进 PendingPlugins；相关 QueueingHint 再次放行时可跳过 backoff 直接进 activeQ |

最重要的区分：

```text
FitError / Unschedulable：规则正常工作，结论是“当前放不下”
Error：调度计算本身遇到非预期问题
```

`Wait` 与 `Pending` 不能混：`Wait` 是 Permit 专用的原地等待，Pod 留在 `waitingPods`，binding cycle 卡在 `WaitOnPermit` 等待 Allow、Reject 或超时；`Pending` 不会阻塞 binding goroutine，而是结束本次 cycle、进入失败与重排语义，等待相关事实变化后重新尝试。

不能看到 `FailedScheduling` Event 就说 kube-scheduler 进程出故障；这个 Event 同时可以承载正常的不可调度结论。

### 6.7 当前快照默认插件，不要求背名单

当前固定 commit 的 `getDefaultPlugins()` 先用 `MultiPoint` 装配以下**基础列表**，再调用 `applyFeatureGates()` 按特性开关追加插件。表里只写运维最需要知道的责任：

| 规则领域 | 主要默认插件 | 大白话 |
|---|---|---|
| 入队门控 | `SchedulingGates` | Pod 还有 scheduling gate 时先不尝试选点 |
| 队列顺序 | `PrioritySort` | priority 高者先；同优先级较早者先 |
| 节点是否允许调度 | `NodeUnschedulable` | 处理 cordon/unschedulable 节点边界 |
| 显式节点与标签 | `NodeName`、`NodeAffinity` | 检查节点名、nodeSelector、required affinity；preferred affinity参与打分 |
| 污点 | `TaintToleration` | 不容忍的硬 taint 过滤；软偏好可参与打分 |
| 端口 | `NodePorts` | 防止 hostPort 等节点端口冲突 |
| 资源 | `NodeResourcesFit` | CPU、内存、Pod 数、临时存储、扩展资源的 Fit 与资源打分 |
| 卷 | `VolumeRestrictions`、`NodeVolumeLimits`、`VolumeBinding`、`VolumeZone` | 检查卷冲突、数量、延迟绑定和拓扑 |
| 拓扑与 Pod 关系 | `PodTopologySpread`、`InterPodAffinity` | 跨 zone/hostname 分布、Pod 亲和与反亲和 |
| 无可行 Node 后 | `DefaultPreemption` | 尝试通过驱逐较低优先级 Pod 为未来调度创造条件 |
| 打分 | `NodeResourcesBalancedAllocation`、`ImageLocality` 等 | 资源平衡、镜像本地性等偏好 |
| 绑定 | `DefaultBinder` | 调 Pod Binding 子资源 |

在本文固定快照的默认 feature-gate 状态下，`DynamicResourceAllocation` 已 GA 且锁定开启，`NodeDeclaredFeatures` 为 Beta 且默认开启，因此**当前有效默认集合**还会追加 `DynamicResources` 和 `NodeDeclaredFeatures`。`GangScheduling`、`TopologyPlacementGenerator` 等实验插件则仍取决于默认关闭的相关 feature gate。不要把这个 master 快照的有效集合反推到其他版本；目标集群必须同时核对 `getDefaultPlugins()`、`applyFeatureGates()` 与实际启动参数。

---

## 7. 源码主线：一个 Pod 到底怎样选出 Node

### 7.1 先看最小调用地图

这张图从上往下读。实线是普通 Pod 主路径；虚线是失败和补偿；灰色概念是第二遍再追的优化。

```mermaid
flowchart TD
    R["Scheduler.Run"] --> SO["ScheduleOne"]
    SO --> POP["NextPod = PriorityQueue.Pop"]
    POP --> ONE["scheduleOnePod"]
    ONE --> CY["schedulingCycle"]
    CY --> SNAP["Cache.UpdateSnapshot"]
    SNAP --> ALG["schedulingAlgorithm"]
    ALG --> SP["schedulePod"]
    SP --> FIT["findNodesThatFitPod"]
    FIT --> PF["RunPreFilterPlugins"]
    PF --> FF["findNodesThatPassFilters"]
    FF --> PR["prioritizeNodes"]
    PR --> SR["SuggestedHost"]
    SR --> AR["assumeAndReserve"]
    AR --> PER["RunPermitPlugins"]
    PER --> GO["go runBindingCycle"]
    GO --> BC["bindingCycle"]
    BC --> PB["PreBind"]
    PB --> B["Bind"]
    B --> PO["PostBind"]
    FIT -. "0 个可行 Node" .-> POST["PostFilter / preemption"]
    AR -. "失败" .-> FH["Unreserve + Forget + FailureHandler"]
    BC -. "失败" .-> FH
```

对应文件与符号：

| 问题 | 文件 | 关键符号 |
|---|---|---|
| scheduler 怎样持续工作 | `pkg/scheduler/scheduler.go` | `(*Scheduler).Run` |
| 一个实体怎样分流 | `pkg/scheduler/schedule_one.go` | `ScheduleOne` |
| 普通 Pod 总骨架 | 同上 | `scheduleOnePod` |
| 快照、算法、预占 | 同上 | `schedulingCycle` |
| 0/1/多个可行 Node | 同上 | `schedulePod` |
| PreFilter/Filter | 同上 | `findNodesThatFitPod`、`findNodesThatPassFilters` |
| Score | 同上 | `prioritizeNodes` |
| Assume/Reserve | 同上 | `assumeAndReserve`、`assume` |
| Permit/Bind | 同上 | `bindingCycle`、`bind` |
| 失败重排与 Condition/Event | 同上 | `handleSchedulingFailure` |
| 队列状态 | `pkg/scheduler/backend/queue/` | `PriorityQueue`、`activeQueue`、`backoffQueue` |
| Framework 调插件 | `pkg/scheduler/framework/runtime/framework.go` | `RunPreFilterPlugins` 等 |

### 7.2 第一组真实源码：0、1、多个可行 Node 为什么分叉

这段只回答一个问题：Filter 完成后，scheduler 怎样从可行 Node 数量得出下一步。

**摘录类型：完整函数。** 来自 `pkg/scheduler/schedule_one.go:570-624` 的 `schedulePod`。中文注释为讲义新增；所有业务分支都保留。`fwk` 是当前 profile 的 Framework，`state` 是本轮插件共享状态，`podInfo` 来自调度队列。

```go
// 这个方法只负责从当前快照计算建议节点，不在这里写 API Binding。
func (sched *Scheduler) schedulePod(
	ctx context.Context,
	fwk framework.Framework,
	state fwk.CycleState,
	podInfo *framework.QueuedPodInfo,
) (result ScheduleResult, err error) {
	// 从队列对象里取得本轮待调度 Pod。
	pod := podInfo.Pod

	// trace 只用于慢调度追踪，不改变选点结果。
	trace := utiltrace.New(
		"Scheduling",
		utiltrace.Field{Key: "namespace", Value: pod.Namespace},
		utiltrace.Field{Key: "name", Value: pod.Name},
	)
	defer trace.LogIfLong(100 * time.Millisecond)

	// 当前 placement 里一台 Node 都没有，直接返回特殊错误。
	if sched.nodeInfoSnapshot.NumNodesInPlacement() == 0 {
		return result, ErrNoNodesAvailable
	}

	// 运行 PreFilter、Filter 和 extender Filter，得到本轮可行 Node。
	feasibleNodes, diagnosis, nodeHint, err := sched.findNodesThatFitPod(ctx, fwk, state, podInfo)
	// 插件执行或内部基础设施异常会终止整轮，不伪装成普通节点不匹配。
	if err != nil {
		// 内部 Error 与“0 个可行 Node”分开返回。
		return result, err
	}
	// trace 的这个分段点只记录硬约束阶段耗时。
	trace.Step("Computing predicates done")

	// 规则正常运行，但所有 Node 都被拒绝：构造 FitError。
	if len(feasibleNodes) == 0 {
		return result, &framework.FitError{
			Pod:         pod,
			NumAllNodes: sched.nodeInfoSnapshot.NumNodesInPlacement(),
			Diagnosis:   diagnosis,
		}
	}

	// 只有一个可行 Node 时没有比较对象，直接选它，不运行 Score。
	if len(feasibleNodes) == 1 {
		node := feasibleNodes[0].Node().Name
		if utilfeature.DefaultFeatureGate.Enabled(features.OpportunisticBatching) {
			// 当前快照启用批处理优化时，缓存本轮结果供相同签名 Pod 复用。
			fwk.StoreScheduleResults(ctx, podInfo.PodSignature, nodeHint, node, nil, sched.CurrentCycle())
		}
		return ScheduleResult{
			SuggestedHost:  node,
			EvaluatedNodes: 1 + diagnosis.NodeToStatus.Len(),
			FeasibleNodes:  1,
		}, nil
	}

	// 多个 Node 都能放，才进入 PreScore/Score 和 extender Prioritize。
	priorityList, err := prioritizeNodes(ctx, sched.Extenders, fwk, state, pod, feasibleNodes)
	// 打分链任何内部异常都会让本轮无法安全选择节点。
	if err != nil {
		return result, err
	}

	// 建堆后弹出总分最高的 Node。
	// 把所有候选分数组织成可高效取最高分节点的堆。
	sortedPrioritizedNodes := newSortedNodeScores(priorityList)
	// 弹出的节点是当前参与评分候选集中的最高分者。
	node := sortedPrioritizedNodes.Pop()
	trace.Step("Prioritizing done")

	if utilfeature.DefaultFeatureGate.Enabled(features.OpportunisticBatching) {
		// 缓存已选 Node 和其余有序候选；这是当前版本优化旁支。
		fwk.StoreScheduleResults(ctx, podInfo.PodSignature, nodeHint, node, sortedPrioritizedNodes, sched.CurrentCycle())
	}

	// 注意这里只返回 SuggestedHost，API 中还没有 spec.nodeName。
	return ScheduleResult{
		SuggestedHost:  node,
		EvaluatedNodes: len(feasibleNodes) + diagnosis.NodeToStatus.Len(),
		FeasibleNodes:  len(feasibleNodes),
	}, err
}
```

**大白话总结：**

- 输入：一个 Pod、本轮 snapshot 和插件状态。
- 判断：先找可行 Node，再按 0、1、多个分叉。
- 动作：0 个返回 FitError；1 个直接选；多个运行 Score 后选最高分。
- 结果：得到的仍是 `SuggestedHost`，尚未完成 Binding。

代回本案：释放前 0 个可行 Node，返回 FitError；释放后只有 `worker-a` 可行，直接返回它，不会为了“流程完整”硬跑一次 Score。

**顺手学 Go：**

- `(result ScheduleResult, err error)` 是命名返回值；裸 `return` 才会隐式使用它们，本函数大部分分支仍显式写返回值。
- `:=` 在当前作用域创建变量；`feasibleNodes, diagnosis, nodeHint, err :=` 中的 `err` 与命名返回值属于同一函数作用域。
- `defer` 在函数返回前执行；这里无论走哪个 return，慢 trace 都有机会记录。
- `len(slice)` 返回 slice 元素数量。

### 7.3 scheduling cycle 为什么同步，binding cycle 为什么异步

这段只回答：选点与绑定怎样在 `scheduleOnePod` 中拆开。

**摘录类型：连续摘录。** 来自 `pkg/scheduler/schedule_one.go:125-148`。函数前面的日志、profile 查找和 skip 判断已省略，因为不改变这里的“同步选点、异步绑定”结论。可见的 `podInfo`、`fwk`、`pod` 来自该函数参数和前置语句。

```go
// 同步寻找适合当前 Pod 的 Node。
start := time.Now()

// CycleState 是本轮插件共享的临时书包，不写入 API。
state := framework.NewCycleState()

// 当前实现只抽样记录一部分插件执行指标，降低观测开销。
state.SetRecordPluginMetrics(rand.Intn(100) < pluginMetricsSamplePercent)

// 插件可以把需要激活的其他 Pod 放入这个结构。
podsToActivate := framework.NewPodsToActivate()
state.Write(framework.PodsToActivateKey, podsToActivate)

// 给同步 scheduling cycle 建立可取消的子 context。
schedulingCycleCtx, cancel := context.WithCancel(ctx)
defer cancel()

// 选 Node、Assume、Reserve、Permit 都在这个同步调用里。
scheduleResult, assumedPodInfo, status := sched.schedulingCycle(
	schedulingCycleCtx,
	state,
	fwk,
	podInfo,
	start,
	podsToActivate,
)
if !status.IsSuccess() {
	// 失败统一进入失败分类、重排和 Condition/Event 更新。
	sched.FailureHandler(schedulingCycleCtx, fwk, assumedPodInfo, status, scheduleResult.nominatingInfo, start)
	return
}

// Assume 已经在内存占账，所以 Bind 可以放到 goroutine，不挡住下一 Pod 的选点。
go sched.runBindingCycle(ctx, state, fwk, scheduleResult, assumedPodInfo, start, podsToActivate)
```

**大白话总结：**

- 输入：已经从队列取出的一个 Pod。
- 判断：同步 scheduling cycle 是否成功。
- 动作：失败走 FailureHandler；成功后启动异步 binding cycle。
- 结果：主循环可以尽快处理下一个 Pod，但前一个 Pod 的 API Binding 仍可能失败并回滚。

一个很细的源码点：异步 `runBindingCycle` 接收外层 `ctx`，不是即将因 `defer cancel()` 被取消的 `schedulingCycleCtx`。否则同步函数一返回，后台绑定会被自己误取消。

**顺手学 Go：**

- `go f()` 启动 goroutine；它只表示并发执行，不保证何时完成。
- `context.WithCancel` 返回子 context 和取消函数。`defer cancel()` 保证当前函数结束时释放相关资源。
- `status.IsSuccess()` 对 `nil Status` 也按成功处理，这是 Framework 的明确契约。

### 7.4 0 个可行 Node 后为什么先 PostFilter

`schedulingAlgorithm` 对返回错误做分类：

```text
ErrNoNodesAvailable
  -> UnschedulableAndUnresolvable

非 FitError
  -> 内部 Error

FitError 且有 PostFilter 插件
  -> 运行 PostFilter，例如 DefaultPreemption
  -> 当前这一轮仍返回 Unschedulable
```

PostFilter 的目标通常是为未来一轮创造条件，例如选择 victim 并发起驱逐。它不是“Filter 失败后偷偷绕过规则直接绑定”。

当前固定提交还有一个需要二遍知道的边界：PostFilter 自身若返回 Error，`schedulingAlgorithm` 会记录日志和诊断消息，但最后仍以原来的 FitError/Unschedulable 返回。不能用一句“所有 error 都原样逐层上抛”概括这里的真实控制流。

### 7.5 大集群为什么不保证扫描全部 Node

当前源码的 `numFeasibleNodesToFind` 有以下行为：

```text
本轮候选 Node 数 < 100：检查全部
显式配置 percentageOfNodesToScore：按配置计算
未显式配置：percentage = 50 - 本轮候选 Node 数 / 125，最低 5%
最终目标至少为 100 个可行 Node
没有 Score 和 extender Filter 时：找到 1 个可行 Node 就够
```

不同 Node 的 Filter 可以并行执行；找到足够数量的可行 Node 后，context 会被取消，剩余检查尽快停止。每轮还会旋转 `nextStartNodeIndex`，避免总从节点列表开头扫描。

这里传入计算的是 PreFilter 之后的候选 `nodes` 长度；若 PreFilter 已经缩小集合，它不一定等于集群 Node 总数。

平台含义：

- scheduler 的目标是可接受质量下的吞吐，不是每个 Pod 都做全局穷举；
- 降低 `percentageOfNodesToScore` 可能提高吞吐，也可能降低放置质量；
- 插件很慢时，扩容 scheduler 副本通常不能线性增加调度吞吐，因为同一 leader 仍承担主选点循环；
- 先用 extension point 和 plugin latency 指标定位，不能靠猜调参数。

---

## 8. 最重要的一本账：Pod request 与 Node Requested 怎样相遇

### 8.1 scheduler 不是只看业务主容器

本案的 Java 主容器只写了 `1200m`，但 scheduler 看到的常驻阶段是：

```text
order-api 1200m + mesh-proxy 200m = 1400m
```

完整 Pod request 还可能受以下内容影响：

- 所有普通业务/sidecar 容器的 request 相加；
- 普通 init container 的阶段峰值；
- restartable init container 与后续阶段的并发关系；
- Pod-level resources（目标版本启用时）；
- RuntimeClass 定义的 Pod overhead；
- admission 注入或默认化后的最终 Pod spec。

因此排障必须看 API Server 最终保存的 Pod，不能只看 Deployment 模板，也不能只抽查第一个 container。

### 8.2 为什么 init container 不是简单全部相加

教学版白板模型：

```text
常驻阶段：所有同时运行的 app/sidecar request 求和
初始化阶段：按可能同时存活的组合算每种资源峰值
整个 Pod：对每种资源取所有阶段中的最大值
最后再按 API 语义加入 Pod overhead 等项
```

CPU 与 memory 要分别算峰值，不是先找“最大的那个容器”再把整个资源向量照搬。

如果本案另有一个普通 init container 请求 `2000m CPU / 512Mi`：

```text
常驻阶段 CPU = 1400m
init 阶段 CPU = 2000m
Pod CPU request = max(1400m, 2000m) = 2000m
```

这时释放后的 `worker-a` 余额只有 `1800m`，仍然放不下。你不能因为 init 只跑几十秒就让 scheduler 忽略它的启动峰值。

### 8.3 NodeInfo 不是 Node API 对象的别名

`Node` API 对象提供 Capacity、Allocatable、labels、taints 等节点事实；scheduler 的 `NodeInfo` 还汇总该 Node 上被认为占用资源的 Pod 信息，例如 Requested、端口和亲和性相关结构。

Node 侧核心账可以先记成：

```text
NodeInfo.Allocatable
  <- Node.status.allocatable

NodeInfo.Requested
  <- 已绑定 Pod 的 request
  + 已 Assume 但尚未完成 Bind 的 Pod request
```

Assumed Pod 必须马上进入 Requested。否则两个连续调度周期都可能看到同一份余额，各自认为能放，最后超卖。

### 8.4 第二组真实源码：资源不足的公式到底在哪里

这段只回答：CPU、内存和 extended resource 如何被判定不足。

**摘录类型：非连续检查点。** 来自 `pkg/scheduler/framework/plugins/noderesources/fit.go:647-733` 的同一个 `fitsRequest`。为了控制长度，代码块只拼接 CPU、memory 与 scalar/extended resource 三个真实判断区间；中间被省略的是 ephemeral-storage 等同层判断，因而这个代码块不能脱离原函数独立编译。函数输入 `podRequest` 来自 PreFilter 写入的 `CycleState`；`nodeInfo` 是当前候选 Node 在本轮评估中的视图。

```go
// CPU request 大于 Node 可分配量减去已请求量，记一条不足原因。
if podRequest.MilliCPU > 0 &&
	podRequest.MilliCPU > (nodeInfo.GetAllocatable().GetMilliCPU()-nodeInfo.GetRequested().GetMilliCPU()) {
	insufficientResources = append(insufficientResources, InsufficientResource{
		// ResourceName 让上层知道不足的是 CPU 这一维。
		ResourceName: v1.ResourceCPU,
		// Reason 最终可参与调度诊断和 Event 聚合。
		Reason:       "Insufficient cpu",
		// Requested 是本 Pod 本轮需要新增的毫核数。
		Requested:    podRequest.MilliCPU,
		// Used 是 NodeInfo 已经计入账本的毫核数。
		Used:         nodeInfo.GetRequested().GetMilliCPU(),
		// Capacity 取 Node 的可分配 CPU，而不是物理总 CPU。
		Capacity:     nodeInfo.GetAllocatable().GetMilliCPU(),
		// 单个 Pod 已大于 Node 总 Allocatable 时，抢占其他 Pod 也没有用。
		Unresolvable: podRequest.MilliCPU > nodeInfo.GetAllocatable().GetMilliCPU(),
	})
}

// memory 使用相同的“请求 > 可用余额”公式，但单独核算。
if podRequest.Memory > 0 &&
	podRequest.Memory > (nodeInfo.GetAllocatable().GetMemory()-nodeInfo.GetRequested().GetMemory()) {
	insufficientResources = append(insufficientResources, InsufficientResource{
		// 内存不足使用独立资源名，便于插件级诊断。
		ResourceName: v1.ResourceMemory,
		Reason:       "Insufficient memory",
		// 这里记录本 Pod 的内存请求字节数。
		Requested:    podRequest.Memory,
		// 这里记录 NodeInfo 已承诺的内存字节数。
		Used:         nodeInfo.GetRequested().GetMemory(),
		Capacity:     nodeInfo.GetAllocatable().GetMemory(),
		Unresolvable: podRequest.Memory > nodeInfo.GetAllocatable().GetMemory(),
	})
}

// ScalarResources 包含 GPU 等扩展资源；每种资源名独立核算。
for rName, rQuant := range podRequest.ScalarResources {
	// request 为 0 时不用做容量判断。
	if rQuant == 0 {
		continue
	}

	if v1helper.IsExtendedResourceName(rName) {
		// 命中 NodeResourcesFit 的忽略配置时，核心 Fit 在这里跳过检查。
		var rNamePrefix string
		if ignoredResourceGroups.Len() > 0 {
			rNamePrefix = strings.Split(string(rName), "/")[0]
		}
		if ignoredExtendedResources.Has(string(rName)) || ignoredResourceGroups.Has(rNamePrefix) {
			continue
		}
	}

	// 当前资源若映射到 DRA 且不由传统 Node scalar 资源提供，这里委托给 DRA 插件判断。
	if shouldDelegateResourceToDRA(rName, nodeInfo, draManager, opts) {
		continue
	}

	// 传统 extended resource 使用与 CPU 相同形状的整数余额公式。
	if rQuant > (nodeInfo.GetAllocatable().GetScalarResources()[rName] -
		nodeInfo.GetRequested().GetScalarResources()[rName]) {
		insufficientResources = append(insufficientResources, InsufficientResource{
			ResourceName: rName,
			Reason:       fmt.Sprintf("Insufficient %v", rName),
			Requested:    podRequest.ScalarResources[rName],
			Used:         nodeInfo.GetRequested().GetScalarResources()[rName],
			Capacity:     nodeInfo.GetAllocatable().GetScalarResources()[rName],
			Unresolvable: rQuant > nodeInfo.GetAllocatable().GetScalarResources()[rName],
		})
	}
}
```

这里的 `ignoredExtendedResources` / `ignoredResourceGroups` 只表示 NodeResourcesFit 不再核算该资源，并不天然证明 extender 已经正确接管。它们可以来自组件配置，也可能由 extender 的 `managedResources[].ignoredByScheduler` 注入；“跳过”不会创建容量、选择设备或保证 kubelet 能 Allocate，而且该忽略语义不等于 Score 也自动交给同一组件。若没有其他可信插件/extender 完整负责，Pod 甚至可能先绑定、再在节点兑现阶段失败。这类配置属于高风险平台契约，必须审计资源名、唯一责任方与失败语义。

原始完整函数在这些判断前还会检查 Node 可容纳 Pod 数，在 CPU/memory 后检查 ephemeral-storage；它们与本文资源余额结论使用同一种“逐维度记不足原因”的模型。

**大白话总结：**

- 输入：Pod 每种资源 request、Node Allocatable、NodeInfo Requested。
- 判断：每种资源独立比较 `request > allocatable - requested`。
- 动作：不足就追加结构化原因；单 Pod 大于总容量时标记抢占也无法解决。
- 结果：可能同时返回多个不足原因，而不是只报第一项。

代回本案：

```text
podRequest.MilliCPU = 1400
worker-a Allocatable = 4000
worker-a Requested = 3200
1400 > 4000 - 3200 = 800
=> Insufficient cpu
```

**顺手学 Go：**

- `for rName, rQuant := range map` 遍历 map；Go 不保证 map 的稳定顺序，所以不要依赖多个资源原因的内部遍历顺序做自动化判断。
- `append(slice, value)` 返回扩展后的 slice，必须接回原变量。
- `failureReasons...` 这类三个点若在实参位置，是合法的 variadic slice 展开，不是“省略了源码”。
- `fmt.Sprintf` 生成字符串；Event 文本可随版本变化，不应作为稳定 API 解析。

### 8.5 request、limit、usage 与 OOM 的关系

| 数值 | 谁主要使用 | 它回答什么 | 不能回答什么 |
|---|---|---|---|
| request | scheduler、资源配额、容量规划等 | 我至少要为这个 Pod 承诺多少资源 | 应用此刻实际用了多少 |
| limit | kubelet/runtime/cgroup 相关路径 | 容器允许使用到什么上限（视资源类型语义而定） | Node 是否有调度余额 |
| usage | metrics/监控 | 某个采样窗口实际消耗多少 | scheduler 当初为何接受或拒绝 |
| working set / RSS / JVM heap 等 | 运行态排障 | 内存在哪里消耗 | Pod 调度 request 是否合理的唯一答案 |

常见错误直觉：

```text
kubectl top node 很低
  ≠ scheduler 的 Requested 很低
  ≠ 可以无风险降低 request
  ≠ GPU 设备当前没有被分配
```

### 8.6 QoS 不是独立的“调度优先级”

Guaranteed、Burstable、BestEffort 会影响 kubelet驱逐、cgroup 与运行态资源管理，但普通 NodeResourcesFit 的核心仍是最终 request 账。不能写成“Guaranteed Pod 会被 scheduler 自动优先挑 Node”或“BestEffort 永远排在队尾”。队列顺序主要由 PrioritySort 和 profile 决定。

---

## 9. Filter：业务 YAML 怎样落到不同的硬规则

### 9.1 运维最常见的硬筛选

| 业务声明/节点事实 | 典型插件 | 常见失败 | 真正应该检查什么 |
|---|---|---|---|
| `nodeSelector` / required nodeAffinity | `NodeAffinity` | label 不匹配 | 最终 Pod 规则、Node label、布尔表达式 |
| Node `spec.unschedulable` | `NodeUnschedulable` | cordon 后普通 Pod 不进 | 是否维护窗口；Pod 是否有对应特殊 toleration |
| taint/toleration | `TaintToleration` | 不容忍 `NoSchedule` | key/value/effect/operator 全部匹配关系 |
| CPU/memory/ephemeral/extended resource | `NodeResourcesFit` | `Insufficient ...` | 最终 Pod request、Node Allocatable、已计 request |
| `hostPort` | `NodePorts` | 端口冲突 | 协议、hostIP、端口和同 Node 现有 Pod |
| PVC/PV/CSI | 卷相关插件 | 未绑定卷、拓扑不合、数量上限 | StorageClass、binding mode、PV nodeAffinity、CSI 限制 |
| podAffinity/antiAffinity | `InterPodAffinity` | 必需关系不成立 | labelSelector、namespace、topologyKey、现有 Pod 分布 |
| topologySpread 硬约束 | `PodTopologySpread` | skew 不满足 | topologyKey、domains、selector、whenUnsatisfiable |

`spec.nodeName` 不应放进普通 Filter 手算主线：正常 informer 路径会把 `spec.nodeName != ""` 的 Pod 当作已分配对象加入 cache，而不是再放进普通 scheduling queue。源码中虽然存在 `NodeName` 插件，但不要因此理解成“用户手填 nodeName 后，scheduler 还会替我完整检查目标节点”。手填 `nodeName` 会绕过 kube-scheduler，应只用于清楚理解其后果的特殊组件。

### 9.2 required 和 preferred 一定分开

以 node affinity 为例：

```text
requiredDuringSchedulingIgnoredDuringExecution
  -> 硬条件，失败淘汰 Node

preferredDuringSchedulingIgnoredDuringExecution
  -> 软偏好，通过 Filter 的 Node 之间加权比较
```

名字里的 `IgnoredDuringExecution` 表示 Pod 运行后 Node label 再变化，Kubernetes 不会仅因此自动驱逐该 Pod。它不是“运行时忽略这条规则”的意思。

**布尔关系速记：**

```text
nodeSelector 中多个 key=value                    -> AND
一个 NodeSelectorTerm 内多个 matchExpressions   -> AND
多个 nodeSelectorTerms                           -> OR
Pod 的 nodeSelector 与 required nodeAffinity     -> 两者都要满足
required                                          -> Filter 硬条件
preferred                                         -> Score 软偏好
profile addedAffinity 与 Pod 自己的 affinity      -> 共同生效，用户 YAML 未必看得见前者
```

### 9.3 taint、cordon 与 drain 不是同一件事

| 动作/事实 | 主要效果 |
|---|---|
| `kubectl cordon` | 把 Node 标记为不可调度，阻止普通新 Pod 进入；不主动删除已有 Pod |
| `NoSchedule` taint | 不容忍的普通新 Pod 不得调度到 Node；不等于删除既有 Pod |
| `NoExecute` taint | 还可能驱逐不容忍的既有 Pod，语义不同 |
| `kubectl drain` | 客户端工作流：cordon 后尝试通过 eviction/delete 迁走已有 Pod，受 PDB 等影响 |

值班时看到 Node cordoned，不要把“为什么旧 Pod 还在”误判为 scheduler 没工作。

对**新 Pod**，scheduler 的 `TaintToleration` 会把未容忍的 `NoExecute` 也当作硬拒绝；对**已经绑定的 Pod**，因 `NoExecute` 产生的驱逐不是 kube-scheduler 执行。当前架构中主要由独立的 taint-eviction controller 处理，并结合 toleration 的 `tolerationSeconds` 决定是否及何时驱逐。两条链不要混在一个“污点插件”里。

### 9.4 卷为什么也是调度问题

Pod 还没去 Node，但本地盘、PV zone、CSI attach 上限、延迟绑定 StorageClass 等事实已经可能决定它能去哪里。`WaitForFirstConsumer` 的设计目的之一，就是让卷绑定和 Pod 选点协同，避免先绑定到错误拓扑的 PV 后再发现 Pod 无处可去。

两类 Pending 先区分：

```text
Immediate：PVC 通常先尝试绑定；没有合适 PV/动态供给失败时，Pod 可能在调度前受阻
WaitForFirstConsumer：等到 scheduler 有候选 Node，才把卷选择与 Node 拓扑一起决定
```

WFFC 场景不要手填 `spec.nodeName`：它会绕过 scheduler，PVC 可能因此一直等不到 scheduler 参与的消费者拓扑选择；若必须限制节点，应使用 node affinity/selector 等正常调度约束。

`VolumeBinding` 也不只是一个 Filter：它会跨 PreFilter、Filter、Reserve、PreBind 与 Unreserve 保存、假设、持久化或回滚卷选择。排障证据至少包括 PVC、PV、StorageClass、PV nodeAffinity、CSINode、CSIStorageCapacity；Binding 完成后还可能在 VolumeAttachment、attach 或 mount 阶段失败，那已经是控制器/kubelet/CSI 兑现链。

不过节点侧真正 mount/attach 仍可能在 Bind 后失败。看到 `PodScheduled=True` 但 `ContainerCreating`、`FailedMount`，责任域已从普通选点主线转向 kubelet/volume 路径。

### 9.5 topologySpread 是平台可靠性规则，不只是“尽量平均”

它可以表达：同一服务的 Pod 尽量或必须跨 zone/hostname 分散。平台要关注：

- Node 是否真的有相应 `topologyKey` label；
- selector 是否精确匹配本工作负载；
- `maxSkew` 与 `whenUnsatisfiable`；
- 新增一个 zone/Node 后 domain 集合如何变化；
- 与 podAntiAffinity、nodeSelector、GPU 型号标签组合后，交集是否变成空集。

先手算一个三 zone 例子。假设选择器匹配的现有 Pod 数是：

```text
zone-a = 3
zone-b = 2
zone-c = 2
maxSkew = 1
```

新 Pod 若放到 zone-a，放置后计数为 `4/2/2`，候选域与全局最小值的差为 `4-2=2`；若 `whenUnsatisfiable: DoNotSchedule`，这个节点会被硬过滤。放到 zone-b 后是 `3/3/2`，最大差仍为 1，可以通过。若使用 `ScheduleAnyway`，不满足理想分布不会硬淘汰，而会作为 Score 偏好影响排名。

还要知道五个细节：

- `maxSkew` 比的是假设放置后的目标域计数与 global minimum；
- eligible domain 数少于 `minDomains` 时，global minimum 按 0 处理；
- `nodeAffinityPolicy`、`nodeTaintsPolicy` 会改变哪些节点/域参与计算；
- selector 应明确匹配本工作负载的 Pod label，否则你以为统计“自己”，实际可能漏计自己；
- scale-to-zero 的 zone 若当前一台带该 topology label 的 Node 都没有，scheduler 不能凭云厂商未来可能创建它就把它当作现存可选域。

策略越多不是越安全。每加一条硬约束，都在缩小可行集合；多条单独合理的规则，组合后可能让整个业务无处可放。

---

## 10. Score：能放之后，scheduler 为什么更喜欢某台 Node

### 10.1 Score 不能复活被 Filter 淘汰的 Node

这个顺序必须刻进脑子：

```text
所有 Node
  -> Filter 后的 feasibleNodes
  -> 多个候选才运行 PreScore / Score
  -> 插件分数归一化
  -> 分数乘 weight
  -> 各插件 weighted score 求和
  -> 本轮候选中总分最高者
```

### 10.2 当前默认资源打分是什么倾向

当前固定 commit 中，`NodeResourcesFitArgs` 未显式配置时默认使用 `LeastAllocated`，默认评分资源是 CPU 和 memory，权重各 1。它偏好 request 占比更低的 Node。

单个资源的直观公式是：

```text
resourceScore = (capacity - requestedAfterPod) / capacity * 100
```

多个配置资源再按资源权重求平均。真实代码使用整数运算，且 `requested` 包含当前准备放入的 Pod。

例如有两个已通过 Filter 的 Node，本 Pod 为 `1400m`：

| Node | CPU capacity | 放入前 requested | 放入后 requested | CPU 剩余比例 |
|---|---:|---:|---:|---:|
| `worker-d` | `8000m` | `2000m` | `3400m` | `57.5%` |
| `worker-e` | `8000m` | `4500m` | `5900m` | `26.25%` |

只看 CPU LeastAllocated，`worker-d` 分数更高。但最终还要叠加 memory、taint 软偏好、node affinity 偏好、拓扑分布、镜像本地性等插件分数和权重。

### 10.3 三种常见资源策略

| 策略 | 倾向 | 适合的思路 | 风险 |
|---|---|---|---|
| `LeastAllocated` | 摊开，偏好更空的 Node | 通用在线业务、降低单点拥挤 | 可能产生资源碎片、更多 Node 被唤醒 |
| `MostAllocated` | 装箱，偏好已较满但仍放得下的 Node | 批处理、成本和集群缩容场景 | 故障域集中、热点和干扰风险 |
| `RequestedToCapacityRatio` | 自定义利用率到分数曲线 | 平台按资源类型塑造策略 | 曲线和权重错配时结果很反直觉 |

GPU 平台可能想对 GPU 采用装箱以减少碎片，却对 CPU/memory 或在线业务采用分散。不要只改一个总开关；要先定义资源池、工作负载类型和故障域目标。

### 10.4 打分平局与随机性

当前代码的最终 Node 堆先比较 `TotalScore`，总分相同时再比较 `Randomizer` 字段。但这不等于所有普通 in-tree Score 结果都会自动写随机值：本提交中，普通 Framework Score 路径并没有为每个节点填充随机数；当至少存在 extender 评分时，合并 extender 分数的路径才会给相关节点赋随机值。因此能否用随机值打破平局取决于实际评分链。无论如何，平分后的选择顺序都不应被当作跨版本稳定 API，更不要写依赖“永远选字典序第一台”的自动化。

### 10.5 ImageLocality 不是“镜像一定不用拉”

它只是打分偏好之一。最终选中的 Node 即使已有相关镜像层，也可能因 tag、digest、垃圾回收、认证、runtime cache 等事实仍需要拉取或失败。它不能覆盖 Filter 硬约束，也不能保证启动时间。

---

## 11. Assume、Reserve、Permit、Bind：为什么选完 Node 还没结束

### 11.1 `SuggestedHost` 到 `spec.nodeName` 中间有一个故意保留的窗口

如果 scheduler 选中 `worker-a` 后，必须等 API Server 完成 Binding 才能处理下一个 Pod，那么一次慢 API 调用就会卡住整个选点主循环。

当前设计是：

```text
选中 SuggestedHost
  -> DeepCopy 当前 Pod
  -> 只在内存副本中设置 Spec.NodeName
  -> Cache.AssumePod，先把资源计入 NodeInfo.Requested
  -> Reserve / Permit
  -> 异步 PreBind / Bind
  -> informer 最终看见真实已绑定 Pod，assumed 状态收敛为正式状态
```

这张图从上往下读。左侧是 scheduler 内存，右侧是 API Server；虚线表示两边存在短暂时间差。

```mermaid
sequenceDiagram
    participant S as scheduling cycle
    participant C as scheduler cache
    participant B as binding cycle
    participant A as API Server
    participant I as informer

    S->>S: Filter/Score 得到 worker-a
    S->>C: Assume，内存先记 order-api 占 1400m
    Note over C,A: 此时 API 中可能仍显示 NODE=<none>
    S->>B: goroutine 异步绑定
    B->>A: Binding(order-api, worker-a)
    A-->>I: watch 到 spec.nodeName=worker-a
    I->>C: 把 assumed Pod 收敛成真实已绑定 Pod
```

### 11.2 第三组真实源码：Assume 与 Reserve 如何形成可回滚事务

这段只回答：为什么先 Assume，Reserve 失败后又怎样清理。

**摘录类型：完整函数。** 来自 `pkg/scheduler/schedule_one.go:313-359` 的 `assumeAndReserve`。中文注释为讲义新增。`podInfo` 是未绑定的队列对象；`scheduleResult.SuggestedHost` 是上一步选出的 Node。

```go
func (sched *Scheduler) assumeAndReserve(
	ctx context.Context,
	state fwk.CycleState,
	schedFramework framework.Framework,
	podInfo *framework.QueuedPodInfo,
	scheduleResult ScheduleResult,
) (*framework.QueuedPodInfo, *fwk.Status) {
	// 从 context 提取结构化 logger，后续错误能关联本轮上下文。
	logger := klog.FromContext(ctx)

	// DeepCopy，避免直接篡改来自队列/informer 视图的原 Pod。
	assumedPodInfo := podInfo.DeepCopy()
	assumedPod := assumedPodInfo.Pod

	// assume 会在内存副本写 NodeName，并把 Pod 加入 scheduler cache。
	err := sched.assume(logger, state, assumedPodInfo, scheduleResult.SuggestedHost)
	if err != nil {
		// Assume 失败按内部 Error 返回，让失败处理决定是否重试。
		return assumedPodInfo, fwk.AsStatus(err)
	}

	// 通知所有 Reserve 插件为这个 Pod/Node 预留自己的状态。
	if sts := schedFramework.RunReservePluginsReserve(
		ctx,
		state,
		assumedPod,
		scheduleResult.SuggestedHost,
	); !sts.IsSuccess() {
		// Reserve 失败必须先撤销插件预留，再从 scheduler cache 忘掉 assumed Pod。
		err := sched.unreserveAndForget(
			ctx,
			state,
			schedFramework,
			assumedPodInfo,
			scheduleResult.SuggestedHost,
		)
		if err != nil {
			// Forget 失败被记录；原 Reserve Status 仍决定本轮结果。
			utilruntime.HandleErrorWithContext(ctx, err, "ForgetPod failed")
		}

		if sts.IsRejected() {
			// 业务拒绝被包装成 FitError，记录拒绝插件和目标 Node。
			fitErr := &framework.FitError{
				// Reserve 已经针对唯一建议节点执行，所以这里只诊断一台节点。
				NumAllNodes: 1,
				// 保留原始未绑定 Pod，供上层生成失败诊断。
				Pod:         podInfo.Pod,
				// Diagnosis 收集目标节点的插件拒绝状态。
				Diagnosis: framework.Diagnosis{
					// 先创建节点到状态的可写映射。
					NodeToStatus: framework.NewDefaultNodeToStatus(),
				},
			}
			// 把 Reserve 拒绝挂到实际建议节点上。
			fitErr.Diagnosis.NodeToStatus.Set(scheduleResult.SuggestedHost, sts)
			fitErr.Diagnosis.AddPluginStatus(sts)
			return assumedPodInfo, fwk.NewStatus(sts.Code()).WithError(fitErr)
		}

		// 内部 Error 等非 rejected 状态原样返回。
		return assumedPodInfo, sts
	}

	// Assume 与全部 Reserve 都成功，交给 Permit 和 binding cycle。
	return assumedPodInfo, nil
}
```

**大白话总结：**

- 输入：一个未绑定 Pod 和已选中的 SuggestedHost。
- 判断：scheduler cache 能否 Assume；所有 Reserve 插件能否成功。
- 动作：成功就保留内存占账；失败就 Unreserve 并 Forget。
- 结果：为异步 Bind 建立了一份必须能撤销的临时资源承诺。

**顺手学 Go：**

- `(sched *Scheduler)` 是 pointer receiver，可暂时类比 Java 的 `this`，但 Go 没有 class 继承语义。
- `if sts := call(); !sts.IsSuccess()` 把 `sts` 的作用域限制在该 if/else 内。
- `*framework.QueuedPodInfo` 是指针；`DeepCopy()` 用新对象隔离修改。
- `nil` 在这里按返回位置理解：第二个返回值 `nil` 表示没有失败 Status，不是“对象不存在”。

### 11.3 Assume 和 Reserve 不是同一件事

| 动作 | 谁维护 | 记的什么账 |
|---|---|---|
| `AssumePod` | scheduler cache | 这个 Pod 已暂时占用目标 Node 的通用 Pod/资源账 |
| `Reserve` | 各 Framework 插件 | 插件自己的预留，例如卷或动态资源内部状态 |
| `Unreserve` | 各 Reserve 插件 | 逆序、幂等地撤销插件状态 |
| `ForgetPod` | scheduler cache | 删除 assumed Pod，释放 NodeInfo 中的通用资源占用 |

Framework 契约要求 `Unreserve` 幂等，甚至可能在对应 Reserve 没有执行时被调用。它没有 error 返回值；插件必须自行处理清理中的问题，不能指望再用一个 Status 把已经开始的回滚逆转。

### 11.4 Permit 的三种回答

Permit 位于选好 Node、Reserve 成功之后，Bind 之前：

```text
Success：允许继续
Wait：进入 waitingPods，等插件 Allow、Reject 或超时
Reject/Error：Unreserve + Forget，回到失败处理
```

Wait 适合表达“单个 Pod 已选好位置，但必须等一个协调条件”。例如 gang 或外部资源协调可以使用它。不过当前源码中的原生 GangScheduling 仍是 alpha、默认关闭；不能因此假定所有集群的普通 Pod 都会走 Permit Wait。

当前固定 commit 的 Permit 等待时长有框架上限，属于版本实现细节。生产上真正要关注的是：谁在等、由哪个插件放行、超时后是否回滚，而不是死背分钟数。

### 11.5 Bind 最终写了什么

默认 `DefaultBinder` 构造 `v1.Binding`：

**摘录类型：连续摘录。** 来自 `pkg/scheduler/framework/plugins/defaultbinder/default_binder.go:53-56`；下面保留 Binding 对象的完整构造，随后真正的 API 调用和错误分支用文字解释。

```go
// Binding 同时写入 Pod 身份和目标 Node；后续会提交给 API Server。
binding := &v1.Binding{
	// ObjectMeta 精确标识要绑定的那个 Pod 对象。
	ObjectMeta: metav1.ObjectMeta{
		// 命名空间与名称定位 Pod；UID 防止同名新对象被误绑定。
		Namespace: p.Namespace,
		Name:      p.Name,
		UID:       p.UID,
	},
	// Target 只描述本轮已经选中的 Node。
	Target: v1.ObjectReference{
		Kind: "Node",
		Name: nodeName,
	},
}
```

**大白话总结：** Binding 明确携带 Pod 身份和目标 Node。API Server 接受后，最终效果是 Pod `spec.nodeName` 持久化；它不是 scheduler 直接 SSH 到节点通知 kubelet。

**顺手学 Go：** `&v1.Binding{}` 取得新 struct 的指针；嵌套 `{}` 是复合字面量，不是 JSON。

当前源码的 Bind 优先级是 extender binder 在前，Framework Bind 插件在后。平台使用 extender 时必须知道真正执行 Binding 的到底是谁。

### 11.6 绑定失败为什么还要唤醒其他 Pod

假设 Pod A 已 Assume 了最后 `1400m` 余额，异步 Bind 很慢。这期间 Pod B 开始调度，它看到 A 的 assumed 账后因资源不足进入等待。随后 A Bind 失败：

```text
A: Unreserve + Forget，释放 1400m
B: 如果完全不知道这次释放，就可能继续睡在 unschedulablePods
```

所以 `handleBindingCycleError` 在 Forget 后产生 scheduler 内部的 `EventAssignedPodDelete`，让可能因此获益的 Pod 重新评估。

这仍然不是 Kubernetes Event 对象，而是 scheduler 内部 ClusterEvent。

### 11.7 `Done`、`Forget`、`Unreserve` 再对照一次

| 动作 | 清理什么 | 不清理什么 |
|---|---|---|
| `Done(uid)` | 队列的 in-flight Pod 与事件历史 | scheduler cache 资源、API Pod |
| `ForgetPod` | scheduler cache 中 assumed Pod 的 Node/资源账 | 插件自有预留、API Pod |
| `Unreserve` | Reserve 插件维护的自有状态 | 通用 NodeInfo、API Pod |

如果你能准确解释这张表，就已经抓住异步 Binding 最容易出错的地方。

---

## 12. 失败之后：为什么 Pod 不空转，却也不会永远睡死

### 12.1 正常拒绝、内部错误、等待和 no-op

| 结果 | 例子 | scheduler 大方向 | 运维动作 |
|---|---|---|---|
| 正常 no-op/等待 | 没有相关 Node 变化 | 留在不可调度池，不白算 | 等事实变化，治理根因 |
| 业务拒绝 | `Insufficient cpu`、label 不匹配 | 记录拒绝插件，按事件和退避重试 | 手算约束与容量 |
| 内部 Error | snapshot、插件、API/网络异常 | 不把它伪装成资源不足，退避后重试 | 查 scheduler 日志/指标/依赖 |
| Permit Wait | 协调条件暂未满足 | 保留临时状态，等 Allow/Reject/timeout | 查等待插件与外部控制面 |
| 补偿 | Reserve/Bind 后续失败 | Unreserve + Forget + 必要的唤醒 | 查原始错误与清理是否成功 |

### 12.2 FailureHandler 为什么重新查最新 Pod

调度失败到真正重排之间，API 对象可能已经变化：

- Pod 被删除；
- 同名 Pod 已重建但 UID 不同；
- 另一个组件或 extender 已经成功绑定；
- Pod spec 被更新。

因此当前源码的 `handleSchedulingFailure` 会从 informer lister 重新取最新 Pod：

```text
缓存中已不存在
  -> 不重排旧对象

最新 Pod 已有 spec.nodeName
  -> 可能其实已经绑定，不重排

同名但 UID 不同
  -> 旧对象已经死亡，不把失败状态写到新 Pod

仍是同一个未绑定 Pod
  -> DeepCopy 最新对象后重新入队
```

这是分布式系统里很典型的身份保护：名字可复用，UID 才标识这个具体对象实例。

### 12.3 失败重入队时如何避免错过事件

最危险的时序是：

```text
T1 Pod 从 activeQ Pop，开始用 snapshot 计算
T2 worker-a 上旧 Pod 删除，释放 1000m
T3 当前调度仍基于旧视图得出 Insufficient cpu
T4 如果只把新 Pod放入 unschedulablePods，而 T2 事件已经过去，它可能错过唤醒
```

in-flight event 账的作用，就是给每个已 Pop Pod 保存“从我开始调度之后发生过哪些变化”。失败落队时会补看这些变化，再决定去 activeQ、backoffQ 还是 unschedulablePods。

这张图按时间从上往下读。虚线是对象变化通知，实线是调度控制流。

```mermaid
sequenceDiagram
    participant Q as SchedulingQueue
    participant S as scheduling cycle
    participant E as Node/Pod informer event

    Q->>S: Pop order-api，并记 in-flight 边界
    E-->>Q: 旧 Pod Delete，资源可能释放
    Note over Q: 把事件记到 in-flight event 链
    S->>S: 本轮旧 snapshot 得到 FitError
    S->>Q: AddUnschedulableIfNotPresent
    Q->>Q: 回看该 Pod 边界后的相关事件
    Q->>Q: 选择 activeQ / backoffQ / unschedulablePods
```

### 12.4 QueueingHint 为什么问“上轮谁拒绝”

本案的拒绝插件集合可能有：

```text
worker-a -> NodeResourcesFit
worker-b -> NodeAffinity
worker-c -> TaintToleration
```

如果 `worker-a` 上一个已绑定 Pod 删除，NodeResourcesFit 可以判断它可能释放 request；这值得唤醒。本案若只是某个无关 Secret 更新，则上述插件通常没有理由认为结果会改变。

QueueingHint 是“值得重算”的提示，不是“保证下一次成功”。释放 `500m` 也可能触发重试，但本 Pod 仍需要 `1400m`，下一轮照样失败。

### 12.5 backoff 解决吞吐，不解决根因

当前默认值大致形成：

```text
1s -> 2s -> 4s -> 8s -> 10s -> 10s ...
```

它防止同一个失败 Pod 不断压住新工作。调大 backoff 不会创造 CPU/GPU，调小也不会解决 label、taint 或 PVC 根因。

`scheduler_pending_pods{queue="backoff"}` 很高说明大量 Pod 在退避，但不能仅靠这个指标知道每个 Pod 的失败原因。

### 12.6 FailedScheduling Event 与 PodScheduled Condition 谁先证明什么

当前失败处理会尝试：

- 写 Warning Event，reason 通常为 `FailedScheduling`；
- 更新 `PodScheduled=False` Condition；
- 根据结果写 `Unschedulable` 或 `SchedulerError` reason；
- 必要时更新 nomination。

证据边界：

- Event 文本是版本相关表现，可能聚合和限流；
- 一条 Event 是某次观察，不是完整历史；
- Condition 是 API 中当前汇总状态，也可能短暂落后于内存队列；
- `NODE=<none>` 只证明 API 中尚无持久 `spec.nodeName`；
- scheduler 日志和 metrics 才能进一步区分内部 Error、插件延迟和系统性队列压力。

### 12.7 返回值传播卡

```text
Filter 返回 Unschedulable
  -> Diagnosis 记录 Node 与 rejector plugin
  -> 0 个可行 Node 时组装 FitError
  -> schedulingAlgorithm 返回 rejected Status
  -> FailureHandler 记录插件集合并重排
  -> QueueingHint 用插件集合筛选有用事件
```

```text
Filter / Score / Snapshot 返回内部 Error
  -> fwk.AsStatus(error)
  -> FailureHandler 不把它记成普通资源拒绝
  -> ConsecutiveErrorsCount 增加
  -> backoff 后重试
```

```text
PostFilter 自身 Error
  -> 当前固定提交记录日志与 PostFilterMsg
  -> 外层仍以原 FitError 的 Unschedulable 结果返回
  -> 不能笼统说所有内部 error 都原样传到 worker
```

---
## 13. 优先级与抢占：不是“高优先级 Pod 直接把低优先级 Pod 踢掉”

### 13.1 先把两个概念拆开

`PriorityClass` 同时会影响两个不同阶段：

1. **排队次序**：默认 `PrioritySort` 让更高优先级的 Pod 更早被调度；
2. **抢占资格**：如果高优先级 Pod 正常 Filter 后一个节点也放不下，`DefaultPreemption` 才可能在 PostFilter 阶段尝试寻找受害者。

所以“优先”不等于“必定抢占”，“被提名”也不等于“已经绑定”。

```mermaid
flowchart TD
    A["高优先级 Pod 从队列 Pop"] --> B["正常 Filter"]
    B -->|"有可行节点"| C["正常 Score 和 Bind"]
    B -->|"没有可行节点"| D["PostFilter / DefaultPreemption"]
    D --> E["在候选节点上模拟移除更低优先级 Pod"]
    E --> F{"移除后能通过全部 Filter 吗"}
    F -->|"不能"| G["仍然 Unschedulable"]
    F -->|"能"| H["比较候选节点和受害者集合"]
    H --> I{"SchedulerAsyncPreemption 开启吗"}
    I -->|"开启：当前快照默认"| J["启动 goroutine 异步删除受害者"]
    I -->|"关闭"| K["同步准备候选并删除受害者"]
    J --> L["PostFilter 可先返回 nomination 建议"]
    K --> L
    L --> M["PreEnqueue 防止抢占删除尚未完成时过早重试"]
    M --> N["等待受害者真正终止和资源释放"]
    N --> O["未来调度轮次重新验证，再尝试绑定"]
```

### 13.2 抢占内部到底模拟了什么

当前固定源码中，主入口可从下面几处串起来：

- `pkg/scheduler/framework/plugins/defaultpreemption/default_preemption.go`：`PostFilter`、`SelectVictimsOnNode`；
- `pkg/scheduler/framework/preemption/preemption.go`：`Evaluator.Preempt`、`DryRunPreemption`；
- `pkg/scheduler/framework/preemption/executor.go`：准备并执行对受害者的删除；
- `pkg/scheduler/framework/plugins/defaultpreemption/default_preemption.go`：候选节点排序函数。

大白话步骤是：

1. 先拿一个候选节点的 `NodeInfo` 副本；
2. 找出节点上比待调度 Pod 优先级低的 Pod；
3. 先在模拟账本中把这些潜在受害者移走；
4. 再把能保留的受害者尽量一个个加回来；
5. 每加回一个都重新跑必要的 Filter；
6. 最终留下“为了让高优先级 Pod 能放下，不得不移除”的最小化受害者集合；
7. 对多个候选节点的受害方案再排序；
8. 选中方案后才进入真实删除与 nomination。

当前固定提交中，`SchedulerAsyncPreemption` 已是 Beta 且默认开启：选中候选后，受害者删除工作由独立 goroutine 推进，PostFilter 不必等全部删除 API 调用结束才返回 nomination 建议；`DefaultPreemption.PreEnqueue` 会结合 executor 的进行中状态，避免 preemptor 在异步删除未完成时过早反复调度。关闭该特性时则走同步候选准备路径。这里是明确的版本分支，不应拿当前默认行为描述所有 Kubernetes 版本。

这里的“最小化”不是一句简单的“数量最少”。默认候选比较还会关心 PDB 违反情况、最高受害者优先级、受害者优先级总和、数量等维度。当前 `DefaultPreemption.OrderedScoreFuncs` 没有追加插件自定义函数，默认通用六级比较落在 preemption 包的 `pickOneNodeForPreemption`；实现和排序细节是版本相关事实，升级时两处都要核对。

### 13.3 PDB 是重要约束，但不能把它误解成绝对保险

抢占在挑选受害者时会尽量优先选择不违反 `PodDisruptionBudget` 的方案。然而，PDB 在调度抢占里是“尽力遵守”的重要信号，不是任何条件下都不会越过的物理墙。

运维上应这样理解：

- PDB 描述应用可容忍的自愿中断预算；
- 节点内挑受害者时，会先尝试把可能违反 PDB 的 Pod 加回模拟节点、尽量避免选它们；跨候选节点比较时优先选择 PDB 违反数更少的方案；
- 如果所有可行抢占方案都要违反 PDB，调度器仍可能选择违反数更少的方案；
- PDB 也挡不住节点故障、内核崩溃等非自愿中断；
- 业务真正的高可用还需要足够副本、反亲和或拓扑分散、优雅终止和容量余量。

### 13.4 `nominatedNodeName` 只是预约提示

假设高优先级 Pod `pay-api-0` 抢占后被提名到 `worker-a`：

```text
status.nominatedNodeName = worker-a
spec.nodeName           = ""
```

这时含义是：调度器认为 `worker-a` 是一个潜在落点，并已经开始为它清理条件。它还没有真正绑定，原因可能包括：

- 受害 Pod 仍在 `terminationGracePeriodSeconds` 中；
- 节点状态又变了；
- 另一台节点后来更合适；
- 新的高优先级 Pod 竞争同一资源；
- 卷、亲和、动态资源等约束重新计算后不再满足；
- 被提名 Pod 自己被删除或更新。

因此排障时必须同时看 `spec.nodeName` 与 `status.nominatedNodeName`，不能只看后者就宣布“已经调度成功”。

### 13.5 哪些问题抢占通常救不了

| 根因 | 低优先级 Pod 腾位置能否解决 | 原因 |
|---|---:|---|
| 节点剩余 CPU/内存不足 | 可能 | 删除受害者能归还 request |
| 传统 Device Plugin scalar GPU 被低优先级 Pod 占满 | 可能 | 受害者真正退出后扩展资源 request 会释放；DRA 正在使用的设备不适用这一结论 |
| Pod 单次请求 10 张 GPU，而任何节点最多 8 张 | 不能 | 单节点总量不够 |
| required nodeAffinity 没有任何节点匹配 | 通常不能 | 删除 Pod 不会改变节点 label |
| 缺少 NoSchedule taint 的 toleration | 不能 | 删除受害者不会改变污点关系 |
| PVC 的拓扑与候选节点冲突 | 通常不能 | 资源腾空不等于存储拓扑改变 |
| Node 不存在指定端口 | 视情况 | 删除占端口 Pod 可能有用，硬件/网络约束则无用 |
| 调度器名写错 | 不能 | 根本没有调度器负责这个 Pod |

### 13.6 `preemptionPolicy: Never` 到底关闭了什么

一个 PriorityClass 可以这样定义：

```yaml
apiVersion: scheduling.k8s.io/v1
kind: PriorityClass
metadata:
  name: business-high-non-preempting
value: 100000
preemptionPolicy: Never
globalDefault: false
description: "高排队优先级，但不主动抢占其他 Pod"
```

使用它的 Pod 仍会因高优先级而排在普通 Pod 前面，但它不会主动发起抢占。它自己也不是天然免疫的；更高优先级且允许抢占的 Pod 仍可能把它当作受害者。

生产建议：PriorityClass 的等级数量要少而清晰，例如平台核心、在线关键、在线普通、离线批处理。不要让每个团队自由发明一个巨大数值，更不要把“解决 Pending”变成无脑加优先级。

---

## 14. 从单个 Pod 上升到业务平台：调度前、调度中、调度后分别由谁负责

### 14.1 一张图看清三层控制

```mermaid
flowchart LR
    subgraph A["第一层：业务准入与排队"]
        A1["GitOps / 发布平台"]
        A2["API Server Admission"]
        A3["ResourceQuota / LimitRange / Policy"]
        A4["Kueue 等工作负载准入"]
    end

    subgraph B["第二层：节点放置"]
        B1["kube-scheduler"]
        B2["Filter"]
        B3["Score"]
        B4["Assume / Bind"]
    end

    subgraph C["第三层：节点兑现"]
        C1["kubelet"]
        C2["CSI / Device Plugin / DRA Driver"]
        C3["container runtime"]
        C4["应用进程"]
    end

    A1 --> A2 --> A3 --> A4 --> B1
    B1 --> B2 --> B3 --> B4 --> C1
    C1 --> C2 --> C3 --> C4
```

新手最常见的定位错误，是看到 Pod 没运行就直接找 scheduler。正确分界是：

- **Pod 都没创建出来**：先查 API 准入、配额、发布控制器；
- **Pod 已创建、`spec.nodeName` 为空**：主要查准入门、调度队列和 scheduler；
- **已经有 `spec.nodeName`，但容器没运行**：优先查 kubelet、镜像、卷、设备、runtime 和应用；
- **容器运行但性能差**：优先查运行时用量、CPU throttling、NUMA、GPU 利用率、存储与网络，而不是直接怪 Score。

### 14.2 平台应把 Pod 规格变成“可治理的合同”

调度器不是意图识别器。业务只写 `replicas: 10`，平台必须进一步把意图翻译为明确合同：

| 业务意图 | 应落到的 Kubernetes 合同 | 主要消费者 |
|---|---|---|
| 这是在线订单服务 | label、命名空间、ServiceAccount、PriorityClass | 策略、队列、审计 |
| 每实例最低需要 1.2 核和 2 GiB | `resources.requests` | NodeResourcesFit、容量规划 |
| 最多可用 2 核和 4 GiB | `resources.limits` | kubelet/cgroup；CPU limit 还涉及 throttling |
| 只能进在线节点池 | required nodeAffinity 或受控 nodeSelector | NodeAffinity |
| 可以容忍 online 专用污点 | toleration | TaintToleration |
| 三个可用区尽量分散 | topologySpreadConstraints | PodTopologySpread |
| 发布时最多同时多 25% | Deployment `maxSurge` | Deployment；间接制造调度峰值 |
| 必须使用 A100 80GB | 受控节点标签、资源类型或 DRA 属性 | NodeAffinity / DRA / 驱动 |
| 批任务最多占某团队 16 张 GPU | Kueue 配额或平台准入 | Kueue/平台，不是单 Pod Filter |

`request` 是这里最关键的合同字段之一。它不是平均利用率，也不是“想用多少就填多少”的报价；它决定调度账本认为这个 Pod 至少占多少容量。

### 14.3 节点池不要只靠一个 label

生产节点池通常需要成套设计：

```text
云厂商/机型标签         -> 机器事实，例如实例族、可用区
平台稳定标签            -> 平台承诺，例如 pool=online、accelerator=a100-80gb
taint                   -> 默认拒绝不属于此池的 Pod
toleration               -> 表示某类 Pod 被允许进入
required affinity        -> 表示这个 Pod 必须去哪个池
preferred affinity/Score -> 在允许范围内表达偏好
```

只加 taint+toleration 不足以“吸引”Pod。toleration 的含义只是“门卫不因这条污点赶你走”，并没有说“你必须来这里”。通常要配合 node affinity：

```yaml
spec:
  tolerations:
  - key: workload.platform.example.com/class
    operator: Equal
    value: online
    effect: NoSchedule
  affinity:
    nodeAffinity:
      requiredDuringSchedulingIgnoredDuringExecution:
        nodeSelectorTerms:
        - matchExpressions:
          - key: platform.example.com/pool
            operator: In
            values: [online]
```

注意 `requiredDuringSchedulingIgnoredDuringExecution` 后半句：调度完成后 label 被改掉，并不会因为这个字段由 scheduler 自动驱逐现有 Pod。平台修改节点标签前要另做影响评估。

### 14.4 发布系统要把“滚动升级”换算成瞬时容量

假设订单服务：

```text
replicas = 100
单 Pod request.cpu = 1.4 核
maxSurge = 25%
```

稳定态账面 CPU 是 `140` 核，滚动升级时最多额外产生 25 个新 Pod，瞬时调度需求可再增加 `35` 核。还没有算 DaemonSet、系统保留、故障域余量和其他业务同时发布。

因此平台容量公式至少要包含：

```text
需要的可调度容量
= 稳定态 requests
+ 发布 surge
+ 节点故障余量
+ 集群扩容生效前的缓冲
+ 系统组件和 DaemonSet
+ 业务突发/批任务受控额度
```

如果平台只按平均 CPU 使用率采购，发布时 Pending 并不是 scheduler 异常，而是合同容量根本不够。

### 14.5 软规则过多会制造“每条都满足一点、整体谁也看不懂”

Score 插件会把多个偏好归一化和加权后求和。业务平台应限制可选策略组合，否则容易出现：

- 团队以为 preferred affinity 一定生效，实际被其他高权重 Score 覆盖；
- 拓扑分散和资源装箱互相拉扯；
- 镜像本地性让一次发布看起来偏向旧节点；
- 自定义 GPU 分数与默认 CPU/内存分数目标相反；
- 调整某个权重后，全部工作负载的放置分布发生非局部变化。

平台治理建议：

1. 硬约束只表达真正不可违反的条件；
2. 软约束必须说明“可能不满足”；
3. 每类工作负载提供少量经过容量仿真的模板；
4. 保存 scheduler 配置版本、变更记录和回滚方案；
5. 变更权重前用真实 Pod/Node 快照做离线重放或影子验证；
6. 不要根据一次 Pod 落点反推整个 Score 策略。

### 14.6 四类配额不是同一本账

| 账本 | 回答的问题 | 是否直接决定某个节点可放下 Pod |
|---|---|---:|
| `ResourceQuota` | 某命名空间允许创建多少对象、请求多少总资源 | 否；它先决定 API 请求能否通过 |
| Kueue 配额 | 某团队/队列的批工作负载能否获准使用某种资源风味 | 否；先决定 Workload admission |
| Node `allocatable` 与 scheduler `requested` | 这个节点账面还有多少资源 | 是 |
| 物理监控/DCGM/节点实际用量 | 设备真实负载、健康、温度和性能如何 | 默认 Filter/Score 通常不直接消费 |

看到 Kueue 额度还有 8 张 GPU，不代表集群此刻必有一台节点空出 8 张；看到整集群还剩 8 张，也不代表它们集中在同一节点。调度是单节点装箱问题，配额是组织层面的准入问题。

---

## 15. GPU 调度完整链路：scheduler 只负责“哪台节点”，不负责“哪块卡”

### 15.1 先记住传统 Device Plugin 路径的一句话

> scheduler 看见的是某节点还有几个名为 `nvidia.com/gpu` 的整数资源；真正挑 GPU UUID 并把设备交给容器的是目标节点上的 kubelet DeviceManager 与设备插件。

这条边界是 GPU 排障的地基。

```mermaid
sequenceDiagram
    participant DP as NVIDIA Device Plugin
    participant K as kubelet DeviceManager
    participant API as API Server / Node status
    participant S as kube-scheduler
    participant C as scheduler cache
    participant R as container runtime

    DP->>K: 注册 nvidia.com/gpu
    DP-->>K: ListAndWatch 返回设备 ID 与 Healthy/Unhealthy
    K->>API: 更新 Node Capacity / Allocatable
    API-->>C: Node informer 更新调度缓存
    S->>C: Filter 比较 Allocatable - Requested
    S->>API: Bind Pod 到某个 nodeName
    API-->>K: 目标节点观察到已绑定 Pod
    K->>K: DeviceManager 选择具体设备 ID
    K->>DP: Allocate
    DP-->>K: 返回设备节点、挂载、环境变量等注入信息
    K->>R: 创建容器并应用设备分配
```

### 15.2 `nvidia.com/gpu: 1` 在 scheduler 眼中只是标量

GPU 通常以扩展资源出现：

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: gpu-smoke
spec:
  restartPolicy: Never
  containers:
  - name: cuda
    image: nvcr.io/nvidia/cuda:12.8.1-base-ubuntu22.04
    command: ["bash", "-lc", "nvidia-smi && sleep 3600"]
    resources:
      limits:
        nvidia.com/gpu: 1
```

扩展资源的基本规则：

- 数量必须是整数，不能写 `0.5`；
- 不能像 CPU 那样原生超卖；
- request 与 limit 必须相等；
- 只写 limit 时，API 默认行为通常会把相同数量用于 request；
- 设备共享若产生多个逻辑份额，是设备插件/驱动把逻辑资源数量上报得更多，不是 scheduler 理解了“半张物理卡”。

对 scheduler 来说，资源名可以是 `nvidia.com/gpu`，也可以是厂商定义的其他扩展资源。核心比较仍近似为：

```text
本 Pod 请求的该标量
<= 节点 Allocatable 里的该标量 - NodeInfo.Requested 里的该标量
```

### 15.3 GPU 也有四本经常被混为一谈的账

```mermaid
flowchart TB
    A["Capacity<br/>节点曾报告的设备总容量"] --> B["Allocatable<br/>当前可供新 Pod 调度的健康逻辑资源"]
    B --> C["scheduler Requested<br/>已绑定 + 已 Assume Pod 的请求账"]
    C --> D["可调度余量<br/>Allocatable - Requested"]
    E["DCGM / nvidia-smi 实际使用<br/>利用率、显存、温度、ECC、功耗"]

    D -."默认不等于".-> E
```

举例：某节点 8 张卡：

```text
Capacity nvidia.com/gpu     = 8
Allocatable nvidia.com/gpu  = 7   # 一张设备 Unhealthy
Requested                   = 6   # 其中可能含刚 Assume、尚未写入 API 的 Pod
scheduler 可用余量         = 1
DCGM 显示实际忙碌卡数      = 2   # 不改变上面的 request 账
```

不能因为 DCGM 显示 GPU 利用率低，就断言 scheduler 应该继续塞 Pod。现有 Pod 可能请求独占 6 张卡但阶段性空闲；调度合同仍然占 6。

当前固定源码中，kubelet DeviceManager 的 `GetCapacity` 会把健康设备计入 allocatable，并把它知道的 unhealthy 数量保留在 capacity 统计中。这解释了为什么一张卡变坏后可能看到 `capacity=8`、`allocatable=7`。具体传播存在 informer 与 Node status 更新延迟，排障时要按时间线取证。

### 15.4 一次 2-GPU 业务调度的逐步推演

假设训练 Pod 的合同是：

```yaml
spec:
  schedulerName: gpu-binpack-scheduler
  tolerations:
  - key: nvidia.com/gpu
    operator: Exists
    effect: NoSchedule
  affinity:
    nodeAffinity:
      requiredDuringSchedulingIgnoredDuringExecution:
        nodeSelectorTerms:
        - matchExpressions:
          - key: platform.example.com/gpu-product
            operator: In
            values: [NVIDIA-A100-SXM4-80GB]
  containers:
  - name: trainer
    image: registry.example.com/ml/trainer:v42
    resources:
      requests:
        cpu: "8"
        memory: 64Gi
      limits:
        nvidia.com/gpu: 2
```

集群视图：

| 节点 | 产品标签 | GPU Allocatable | GPU Requested | 其他条件 | Filter 结果 |
|---|---|---:|---:|---|---|
| `gpu-a` | A100 80GB | 8 | 7 | CPU 充足 | 失败：只剩 1 |
| `gpu-b` | A100 80GB | 8 | 4 | CPU/内存充足 | 通过 |
| `gpu-c` | L40S 48GB | 8 | 0 | 很空 | 失败：required affinity 不匹配 |
| `gpu-d` | A100 80GB | 8 | 2 | 缺少对应 PV 拓扑 | 失败：VolumeBinding |

如果只有 `gpu-b` 可行，scheduler 不会为它运行无意义的多节点 Score 比较，直接建议 `gpu-b`。然后：

1. cache Assume 后 `gpu-b` 的 GPU Requested 立刻从 4 变成 6；
2. API 中可能短时间仍没看到 `spec.nodeName`；
3. Bind 成功后 kubelet 才在 `gpu-b` 选择两张具体设备；
4. 设备插件 `Allocate` 返回注入信息；
5. runtime 创建容器；
6. 应用进程才可能执行 `nvidia-smi` 或 CUDA 初始化。

因此“Binding 已成功、`spec.nodeName` 已持久化”只证明节点决策落到 API，不是一个叫 `Bound` 的 Pod Phase，也不证明驱动、设备分配、CUDA、镜像里的库都正常。

### 15.5 默认 NodeResourcesFit 会检查 GPU，但默认 Score 不一定给 GPU 打分

这是 GPU 平台最值得单独圈出来的细节：

- **Filter 阶段**：NodeResourcesFit 会检查普通扩展资源，所以 `nvidia.com/gpu` 不够会拒绝节点；
- **Score 阶段**：当前默认 NodeResourcesFit 的评分资源列表主要是 CPU 和 memory；没有配置时，不能想当然认为它会因为“这台节点剩余 GPU 多/少”而做 GPU 装箱或打散。

换句话说，两个节点都能放下 1 张 GPU 时，最终落点可能被 CPU/内存、污点偏好、拓扑、亲和、镜像本地性等分数影响，而不是你脑中期待的 GPU 最紧凑放置。

可以在专用 profile 中显式加入 GPU 评分。下面展示的是**同一个 kube-scheduler 进程中保留默认 profile、再增加 GPU profile 的最小相关片段**；真实完整配置还可能包含 leader election、client connection 等字段：

```yaml
apiVersion: kubescheduler.config.k8s.io/v1
kind: KubeSchedulerConfiguration
profiles:
- schedulerName: default-scheduler
- schedulerName: gpu-binpack-scheduler
  pluginConfig:
  - name: NodeResourcesFit
    args:
      scoringStrategy:
        type: MostAllocated
        resources:
        - name: cpu
          weight: 1
        - name: memory
          weight: 1
        - name: nvidia.com/gpu
          weight: 5
```

同一进程的 profiles 必须使用相同 QueueSort 插件及其参数。GPU Pod 还必须显式设置 `spec.schedulerName: gpu-binpack-scheduler`；普通 Pod 继续由 `default-scheduler` profile 负责。如果这是独立 GPU scheduler 进程而非同进程 profile，则要另外处理 leader-election 资源名、RBAC、可用性与监控，不能原样套用这个上下文。

这个配置的业务意图是让满足硬约束的节点中，GPU 使用比例更高者得到更高 NodeResourcesFit 分数，从而尽量把零散空卡合并成整节点余量。但上线前必须回答：

- CPU/内存和 GPU 的权重是否会造成热点；
- 训练任务的磁盘、网络、NUMA 与温度是否承受装箱；
- 节点故障会不会一次影响过多任务；
- 与 topology spread、pod affinity 的总分如何交互；
- 调度器版本中的配置 API 是否仍兼容；
- 是否用仿真或影子调度验证过真实 Pod 集。

这不是复制 YAML 就能结束的“最佳实践”，而是明确的容量取舍。

### 15.6 Assume 为什么对昂贵 GPU 尤其重要

普通 Pod 的节点计算不是两个 scheduling cycle 同时跑；`ScheduleOne` 会串行执行这部分。真正重叠的是：Pod-A 的 **binding cycle** 已异步运行时，主循环可以开始 Pod-B 的 **scheduling cycle**。假设一台节点只剩 1 张 GPU：

```text
Pod-A scheduling cycle 的 Filter 看见余量 1
Pod-A Assume 后进入异步 binding cycle
Pod-B 的 scheduling cycle 随后开始
如果 A 只能等 API Bind 后才记账，B 仍可能看见余量 1
```

Assume 先把 Pod-A 加入 scheduler cache，相当于在本地账本占住最后一张卡。Pod-B 随后构建 snapshot 时会看到 Requested 增加，从而被 Filter 拦住。这是乐观并发控制，不是 GPU 锁；真正设备 ID 仍由 kubelet 决定。

绑定失败时必须 `ForgetPod` 清除这笔假设账，否则珍贵 GPU 会在调度器视图里被“幽灵占用”。当前链路通过显式错误处理和 cache 状态转换管理它，不能套用很早版本里“等某个固定 TTL 自动消失”的旧印象。

### 15.7 scheduler 默认看不见哪些 GPU 事实

传统 `nvidia.com/gpu` 标量路径默认看不见：

- 具体 GPU UUID；
- 每张卡当前显存占用和剩余连续显存；
- SM 利用率、Tensor Core 利用率、功耗、温度；
- GPU 之间是否有 NVLink/NVSwitch 以及拓扑距离；
- GPU 与 CPU NUMA、NIC、NVMe 的局部性；
- 某个逻辑 time-slice 背后和谁共享同一物理卡；
- MIG 实例之间的父卡关系与碎片形状，除非资源类型或驱动把它暴露出来；
- 训练框架的通信模式、batch size 和预计持续时间。

标签只能把少量、相对稳定的事实粗粒度暴露给 NodeAffinity。不要让控制器每几秒根据 GPU 利用率修改 Node label 再让 scheduler 追热点：这会产生高频 API 更新、调度抖动、滞后反馈和难以重现的决策。动态遥测更适合容量控制、队列准入、告警或专门设计的扩展策略。

---

## 16. GPU 进阶：MIG、时间切片、DRA 与拓扑分别改变了什么

### 16.1 四种常见供给模型对 scheduler 的呈现

| 模型 | Pod 常见请求 | scheduler 看到的东西 | 主要隔离边界 | 主要风险 |
|---|---|---|---|---|
| 整卡独占 | `nvidia.com/gpu: 1` | 一个整数标量 | 物理 GPU | 利用率可能低，但边界清晰 |
| MIG | 驱动暴露的 MIG profile 资源名 | 不同 profile 的整数标量 | 硬件分区的显存/计算实例 | profile 碎片、重配影响、父卡拓扑不透明 |
| time-slicing | 常仍是 `nvidia.com/gpu: 1`，但节点上报更多逻辑份额 | 逻辑整数份额 | 时间复用，不等于显存硬隔离 | 抢显存、性能抖动、逻辑容量被误当物理卡数 |
| DRA | ResourceClaim/设备类请求，或特性开启后的扩展资源桥接 | 驱动声明的设备、属性、容量与分配约束 | 取决于驱动和设备模型 | API/驱动复杂度、特性版本、观测链更长 |

平台对外不能都叫“1 GPU”。至少要向用户说清它是整卡、MIG profile，还是共享逻辑份额；否则用户会把容量单位和隔离承诺理解错。

### 16.2 MIG 解决的是切分，不自动解决碎片

MIG 可以把支持的 NVIDIA GPU 切成硬件隔离实例。设备插件可以把不同 profile 暴露为不同扩展资源。调度器仍按资源名和整数数量做账，它不会自己推理：

```text
两个小 MIG 实例是否能即时合并成一个大实例
某个实例属于哪张父卡
重新配置 MIG 会杀伤哪些现有工作负载
哪种 profile 组合能最大化未来可用性
```

所以 MIG 平台还需要：

- 受控的节点分组和 profile 策略；
- 变更窗口与排空流程；
- 不同 profile 的配额和价格模型；
- 空闲但碎片化的监控；
- 对“资源总数够、目标 profile 不够”的单独告警；
- 验证设备插件使用何种 MIG strategy 以及资源命名。

### 16.3 time-slicing 增加的是逻辑份额，不是物理 GPU

假设 4 张物理 GPU，每张配置 4 个 time-slice，节点可能向 Kubernetes 暴露 16 个逻辑 `nvidia.com/gpu`。scheduler 只会做 `16 - requested` 的整数账。

这不代表：

- 有 16 份独立显存；
- 一个 Pod 性能等于独占卡的四分之一且稳定；
- 一个份额故障只影响一个 Pod；
- DCGM 的物理设备指标能直接按 16 个 Pod 一一拆分。

共享策略适合容忍抖动、显存可控的推理/开发负载，不应只因为“利用率好看”就套到所有训练任务。是否允许共享应通过资源类、命名空间、准入策略和明确 SLO 控制。

### 16.4 DRA 把“选设备”更早带进调度过程

Dynamic Resource Allocation 的思路，不只是报一个节点标量，而是通过 `DeviceClass`、`ResourceClaim`、`ResourceSlice` 等对象，让驱动声明设备及属性，让 scheduler 在调度期间参与分配适合的设备。

```mermaid
sequenceDiagram
    participant U as Pod / ResourceClaim
    participant API as API Server
    participant DR as DRA Driver
    participant S as DynamicResources plugin
    participant K as kubelet

    DR->>API: 发布 ResourceSlice 与设备属性/容量
    U->>API: 创建 Pod 与 Claim 请求
    API-->>S: informer 提供 Pod、Claim、Slice
    S->>S: PreFilter/Filter 检查候选设备
    S->>S: Score 主要表达 FirstAvailable 子请求优先顺序
    S->>S: Reserve 暂留分配
    S->>API: PreBind 持久化 Claim 分配/消费者关系
    S->>API: Bind Pod 到节点
    API-->>K: kubelet 观察绑定与 Claim
    K->>DR: NodePrepareResources
    DR-->>K: 准备设备并返回运行时信息
```

DRA 改变了传统路径里“scheduler 完全不知道具体设备”的边界，但不要过度宣传：scheduler 能看到的是**驱动通过 API 声明出来的属性、容量与约束**，并不会凭空获得实时温度、业务性能模型或完美拓扑知识。

当前 `DynamicResources.Score` 也不是通用的“设备越多、利用率越低、NVLink 越好就越高分”。它主要针对 `FirstAvailable` 请求，按最终命中了第几个优先 subrequest 给分；普通 `Exactly` 请求通常不会靠这个插件拉开节点分数。如果平台把 DRA extended resource 显式加入 NodeResourcesFit 的 scoring resources，NodeResourcesFit 还能基于 DRA 推导出的声明式数量参与资源评分，但这仍不是实时 GPU 性能评分。

当前固定源码还包含 `DRAExtendedResource` 相关桥接逻辑。一个重要检查点在 `NodeResourcesFit`：对某个可由 DRA 管理的扩展资源，如果节点传统 scalar allocatable 大于 0，仍走传统扩展资源账；否则相关资源才可能交给 DynamicResources 路径继续处理。

固定提交 `301946d15e67a4a2e8a5fb8292eb836acd366d78` 中的源码状态是：

```text
DynamicResourceAllocation     -> GA，默认开启并锁定
DRAExtendedResource           -> Beta，默认开启
DRAConsumableCapacity         -> Beta，默认开启
DRADeviceBindingConditions    -> Beta，默认开启
DRANodeAllocatableResources   -> Alpha，默认关闭
```

这些是 v1.37 开发快照中的 feature 状态，不能反推旧生产版本、云厂商发行版或实际启动参数；生产使用必须以目标集群版本的 feature gate、API 与官方文档为准。

还有一个和第 13 节抢占有关的关键限制：传统 Device Plugin scalar GPU 被低优先级 Pod 占用时，受害者真正退出后 request 可以释放，高优先级 Pod可能受益；当前 DRA 路径不支持为了高优先级 Pod 而抢占另一个正在使用 DRA 设备的 Pod。`DynamicResources.PostFilter` 能处理某些空闲、未被使用或遗留 Claim，不等于它会驱逐正在使用设备的低优先级 Pod。DRA 设备被占满时，高优先级 Workload 通常要等正常释放，或依赖另一个明确的控制/运维流程。

### 16.5 节点级拓扑和设备级拓扑不是一回事

Kubernetes 常用的 topology spread 约束，按 zone、hostname 等**节点标签域**分散 Pod。例如让 8 个推理副本跨 3 个可用区。它并不解决单台 8-GPU 服务器内部的：

- GPU 0 与 GPU 1 是否 NVLink 直连；
- GPU 距哪个 NUMA node 更近；
- RDMA NIC 与某组 GPU 是否同 PCIe root；
- 4 卡任务应该拿哪四块卡。

节点内部设备选择通常落到 DeviceManager、Topology Manager、设备插件/DRA 驱动以及应用通信库。平台排障必须分两级：

```text
集群级：为什么选了这台节点？          -> scheduler 证据
节点级：为什么拿到这几块设备、性能怎样？ -> kubelet/驱动/runtime/DCGM/应用证据
```

### 16.6 GPU 健康变化的时间线

一张卡从 Healthy 变为 Unhealthy，大致会经历：

```mermaid
sequenceDiagram
    participant HW as GPU/Driver
    participant DP as Device Plugin
    participant K as kubelet
    participant API as Node status
    participant S as scheduler cache

    HW-->>DP: 驱动或健康检查发现异常
    DP-->>K: ListAndWatch 标记设备 Unhealthy
    K->>API: 下次 Node status 更新减少 Allocatable
    API-->>S: informer 事件更新 NodeInfo
    S->>S: 后续 Filter 使用较小 Allocatable
```

其中任何箭头都可能有短暂延迟。已经绑定并使用故障设备的 Pod，不会仅因 scheduler 看到 allocatable 下降就自动迁移；这属于节点故障处理、设备插件/kubelet 状态、控制器重建和业务恢复策略的范围。

---

## 17. Kueue、Volcano 与 kube-scheduler：都谈调度，但决定的不是同一件事

### 17.1 Kueue 先决定“这批活现在能不能进场”

面向训练、批处理、AI Job，单纯让每个 Pod 立即进入 kube-scheduler 队列会产生问题：

- 一个 64-GPU Job 的前几个 Pod 占到卡，剩余 Pod 长期凑不齐；
- 小任务不断穿插，大任务永远等不到整块容量；
- 团队之间没有公平配额和借用规则；
- 业务看到大量 Pending，却分不清是队列等待还是节点放不下。

Kueue 的位置是工作负载准入：

```mermaid
flowchart LR
    A["Job / RayJob / MPIJob 等"] --> B["Workload"]
    B --> C["LocalQueue<br/>命名空间入口"]
    C --> D["ClusterQueue<br/>集群配额与策略"]
    D --> E["ResourceFlavor<br/>资源类型/节点特征"]
    E --> F{"额度、借用、公平与 AdmissionChecks 允许吗"}
    F -->|"否"| G["保持等待/挂起"]
    F -->|"是"| H["Workload Admitted"]
    H --> I["控制器允许 Pod 进入 kube-scheduler"]
    I --> J["kube-scheduler 逐 Pod 选 Node"]
    J --> K["kubelet 兑现资源"]
```

几个对象的大白话解释：

- `LocalQueue`：某命名空间的提交入口，像业务柜台；
- `ClusterQueue`：跨命名空间的资源池、配额和准入规则，像总调度室；
- `ResourceFlavor`：把某类资源额度关联到节点特征，例如 A100 池；
- `Workload`：Kueue 用来表示“一项需要整体准入的工作”；
- `Cohort`：允许多个 ClusterQueue 按策略共享/借用额度的组。

`QuotaReserved=True` 表示 Kueue 已为 Workload 记录资源风味/配额分配，`Admitted=True` 还表示所需 admission checks 已就绪；二者都不是在具体 Node 上加了一把物理原子锁。它们不保证每个 Pod 此刻都能在节点层面放下。节点碎片、污点、卷拓扑、节点故障以及准入与实际创建之间的状态变化，仍可能让后续 Pod Pending。

Kueue 还要分清两套优先级：

```text
WorkloadPriorityClass -> Kueue 中 Workload 的排队、准入与相关抢占顺序
Pod PriorityClass      -> Pod 进入 kube-scheduler 后的队列顺序与 Pod 抢占
```

二者可独立配置。仅设置 WorkloadPriorityClass 不会自动改变 Pod priority；若只设置 Pod PriorityClass，Kueue 在没有独立 WorkloadPriorityClass 时可按其规则推导 workload priority。平台必须明确哪一层在“插队”，不能只显示一个模糊的优先级数字。

Kueue 的 Topology-Aware Scheduling（TAS）还能在**准入阶段**按 rack/block/node 等层级计算可用容量并写入 topology assignment，必要时通过 PodSet 更新约束后续 Pod 范围；但最终每个 Pod 的 Node Binding 仍由它指定的 scheduler 完成。TAS 比“只做总配额”更接近物理拓扑，但仍不能把 `Admitted` 当成已完成 Bind。

### 17.2 Volcano 更像“另一套面向批任务的节点调度系统”

Volcano scheduler 可以作为 Pod scheduler，提供面向批处理/HPC 的队列、gang 等插件能力。常见 gang 诉求是：一个 Job 需要的最小成员数不能同时满足时，不要让少数 Pod 先长期占资源。

对比边界：

| 组件 | 主要决策单位 | 核心问题 | 最终会不会选 Node |
|---|---|---|---:|
| Kueue | Workload/Job 准入 | 团队配额、队列、公平、借用、何时进场 | 通常不替代 kube-scheduler 逐 Pod 选节点 |
| kube-scheduler | Pod，当前源码也在演进 PodGroup 能力 | 这个 Pod/组能去哪些节点、哪个最好、如何绑定 | 会 |
| Volcano scheduler | Pod/PodGroup/Queue | 批任务、gang、队列策略与节点放置 | 会 |

当前固定的 Kubernetes master 源码已经包含受 feature gate 控制、仍处早期阶段的内置 gang/PodGroup 相关实现。因此不能写成“Kubernetes 原生永远没有 gang”；更准确的工程说法是：目标生产版本若没有可用且成熟的原生能力，仍需评估 Kueue、Volcano 或其他批调度方案，并承担相应 CRD、控制器、升级和可观测性成本。

VolcanoJob 的最小语义大致如下，真正版本与 CRD 字段以已安装 Volcano 为准：

```yaml
apiVersion: batch.volcano.sh/v1alpha1
kind: Job
metadata:
  name: gpu-train
spec:
  schedulerName: volcano
  minAvailable: 3
  tasks:
  - name: trainer
    replicas: 3
    template:
      spec:
        restartPolicy: Never
        containers:
        - name: trainer
          image: registry.example.com/ml/trainer:v42
          resources:
            limits:
              nvidia.com/gpu: 1
```

`schedulerName: volcano` 表明实际节点放置交给 Volcano；`minAvailable` 是 gang 可运行最小成员约束。Volcano 调度会按配置执行 enqueue、allocate、backfill、preempt、reclaim 等 action，并通过 gang、priority、DRF、proportion/capacity、nodeorder 等插件组合队列公平、抢占和节点排序。插件是否启用、执行次序和参数决定实际语义，不能看到安装了 Volcano 就假定全部能力同时生效。

若 Kueue 与 Volcano 组合，必须指定唯一的职责合同：Kueue 是否只做外层 admission，Volcano 是否做 gang 与 Node 放置；WorkloadPriorityClass、Pod PriorityClass、两边队列、配额/公平、抢占和失败重排怎样映射。没有这张合同，两套系统可能各自正确，却在准入、抢占或重排上互相打架。

### 17.3 一个 8-Pod、每 Pod 1-GPU 训练任务的状态拆解

```text
阶段 A：Kueue 未准入
  - 业务原因：队列配额、借用/公平次序、AdmissionCheck、Provisioning 等尚未完成
  - 不应该用 kube-scheduler FailedScheduling 解释

阶段 B：已准入，8 个 Pod 已出现
  - scheduler 要逐个找节点
  - 若只有 6 个可放，可能出现 6 Running + 2 Pending

阶段 C：使用 gang 语义
  - 只有满足最小成员/整体约束才继续
  - 具体谁负责整体判断，取决于选用的实现

阶段 D：都已绑定但一个容器 CUDA 初始化失败
  - 已越过节点调度，查 kubelet、设备、镜像和应用
```

平台 UI 应把这四个阶段分开展示，不能统一显示一个模糊的“排队中”。

---

## 18. 多 Profile、多 scheduler、Framework 插件和 Extender 怎么选

### 18.1 `schedulerName` 是责任路由，不是普通标签

Pod 默认使用 `default-scheduler`。也可以写：

```yaml
spec:
  schedulerName: gpu-binpack-scheduler
```

这意味着只有声明负责 `gpu-binpack-scheduler` 的 scheduler profile/进程会处理它。名字写错而集群没有对应调度器时，Pod 可以长期保持未绑定；这不是 NodeResourcesFit 拒绝，因为它可能根本没有进入任何负责它的调度循环。

排障第一屏就应核对：

```powershell
$Namespace = 'production'
$PodName = 'order-api-7c8d9f6b5d-k9x2m'
kubectl get pod -n $Namespace $PodName -o jsonpath='{.spec.schedulerName}{"\n"}'
```

### 18.2 同一 kube-scheduler 进程的多个 Profile

一个 `KubeSchedulerConfiguration` 可定义多个 profile，每个有自己的 `schedulerName` 与插件配置。它适合：

- 共用同一套 informer/cache，运维组件数较少；
- 给 GPU、批任务或特殊业务设置不同 Score/Filter 组合；
- 不需要进程级故障隔离。

但要知道边界：

- profiles 共用调度队列和 cache；
- 同一进程中所有 profile 的 QueueSort 插件名称与参数必须兼容一致；
- 一个进程卡顿或崩溃会影响它承载的所有 profile；
- profile 名称会出现在部分 metrics label 中，要控制基数；
- profile 的 `addedAffinity` 可能给 Pod 追加用户 YAML 看不见的 NodeAffinity，平台必须文档化。

```mermaid
flowchart TB
    P1["Pod schedulerName=default-scheduler"] --> Q["共享 SchedulingQueue"]
    P2["Pod schedulerName=gpu-binpack-scheduler"] --> Q
    Q --> C["共享 scheduler cache / informer"]
    C --> F1["default profile plugins"]
    C --> F2["gpu profile plugins"]
```

### 18.3 独立 scheduler 进程

独立部署第二个 scheduler 适合需要更强隔离、不同发布节奏或完全不同实现的场景，但成本更高：

- 唯一的 schedulerName；
- 独立配置、Deployment/静态 Pod、证书与 RBAC；
- leader election 的资源名不能冲突；
- 独立日志、metrics、告警和升级演练；
- 明确哪些业务被路由过去；
- 避免两个 scheduler 都声称负责同一 Pod 集，否则可能产生竞争和难以解释的行为。

### 18.4 Framework 插件与 Extender

| 维度 | Scheduling Framework 插件 | Scheduler Extender |
|---|---|---|
| 运行位置 | 编译进 kube-scheduler 进程 | 外部 HTTP 服务 |
| 能力 | 可接入多个 extension point，状态共享更深 | 主要在筛选、打分、绑定、抢占等有限接口 |
| 延迟 | 进程内调用，通常更低 | 网络调用，受超时和服务可用性影响 |
| 发布 | 需要维护自定义 scheduler 二进制 | 服务可单独发布，但仍要维护协议兼容 |
| 故障影响 | panic/慢插件可直接拖垮 scheduler | 服务慢或失败会按 extender 策略影响调度 |
| 适合 | 深度、长期、性能敏感的定制 | 历史集成或需要外部系统决策的场景 |

Extender 还有几条生产契约必须写进设计：Filter extenders 按配置顺序调用并逐步缩小节点列表；Prioritize extenders 可并行贡献分数；`ignorable` 决定网络/调用错误是忽略还是让本轮失败；`managedResources[].ignoredByScheduler` 可让核心 NodeResourcesFit 跳过某些扩展资源；整个配置只能有一个 extender binder。若核心检查被跳过而 extender 没真正兑现资源，Pod 可能先绑定、再在 kubelet 失败。

Framework 插件通常也不是把一个 `.so` 扔进目录就运行时热加载：实现需要注册并编进自定义 kube-scheduler 二进制，再通过 profile 配置启用。它带来更深集成，也意味着要维护与目标 Kubernetes 版本匹配的构建、测试与供应链。

不要因为 Go 插件开发麻烦，就把一个毫秒级高频决策随手变成跨网络 RPC。也不要因为进程内性能好，就忽略自定义插件对整个控制面的故障半径。生产定制至少要有：超时预算、并发压测、失败语义、指标、版本兼容测试和回滚。

### 18.5 配置变更的安全发布顺序

```text
1. 固定目标 Kubernetes 版本和配置 API
2. 用真实 Node/Pod 清单做离线或测试集群重放
3. 验证硬约束不会扩大可行节点集合
4. 比较新旧 Score 排名和分布，不只看第一名
5. 验证抢占、Permit、卷和 GPU 场景
6. 小流量 schedulerName/profile 灰度
7. 观察队列延迟、错误、失败插件和节点分布
8. 扩大范围，并保留旧配置回切路径
```

Scheduler 不是典型无状态 Web 服务。一次配置变化会改变未来所有 Pod 的放置状态，影响会在节点上长期保留，不能只用“进程健康、接口 200”判断发布成功。

同理，scheduler 只决定尚未绑定 Pod 的未来位置；它不会因为 Score 权重、节点标签或 topology 策略后来改变，就自动把已绑定 Pod 重新摆一遍。若平台引入 Descheduler，它是另一个根据策略驱逐 Pod、再让控制器重建和 scheduler 重新放置的组件；驱逐有业务影响，也不保证重建 Pod 回到你预想的节点。

---

## 19. 生产排障：先判断责任域，再做集合交集

### 19.1 第一棵树：这个 Pod 真的卡在 scheduler 吗

```mermaid
flowchart TD
    A["业务说工作负载没起来"] --> B{"Pod 对象存在吗"}
    B -->|"不存在"| C["查 Deployment/Job 控制器、API Admission、Quota、Kueue 准入"]
    B -->|"存在"| D{"Pod.spec.nodeName 有值吗"}
    D -->|"有值"| E["节点已经选定：查 kubelet、镜像、CSI、设备、runtime、应用"]
    D -->|"无值"| F{"Pod 是否被 scheduling gate / 工作负载准入挡住"}
    F -->|"是"| G["查 gate 所属控制器和准入条件"]
    F -->|"否"| H{"spec.schedulerName 有对应调度器吗"}
    H -->|"没有"| I["修正责任路由或部署对应 scheduler"]
    H -->|"有"| J{"PodScheduled Condition / Event 说什么"}
    J -->|"Unschedulable"| K["按失败插件求硬约束交集"]
    J -->|"SchedulerError"| L["查 scheduler 内部错误、API、插件、snapshot/bind"]
    J -->|"没有调度尝试证据"| M["查 active/backoff/gated 队列、leader、profile 与 informer"]
```

一个很实用的状态表：

| 表象 | `spec.nodeName` | scheduler 是否仍是第一责任域 | 下一站 |
|---|---|---:|---|
| Pending，Node 为空，FailedScheduling | 空 | 是 | Filter/队列/抢占 |
| Pending，Node 为空，没有调度事件，带 schedulingGates | 空 | 部分 | 放 gate 的控制器 |
| Pending，Node 为空，schedulerName 不存在 | 空 | 配置责任 | 调度器路由 |
| Pending，Node 已有值，`FailedMount` | 有 | 否 | kubelet/CSI/存储 |
| Pending，Node 已有值，`FailedCreatePodSandBox` | 有 | 否 | CNI/runtime |
| ContainerCreating，设备分配失败 | 有 | 否 | kubelet/Device Plugin/DRA driver |
| Running，但 GPU 利用率为 0 | 有 | 否 | 应用、CUDA、数据管道和指标 |

### 19.2 先保存现场，再动对象

下面命令对 Kubernetes API 是只读的，但会在当前本地目录创建和写入证据文件。先把同一时间点的证据保存下来，避免你边改 label、删 Pod、扩节点，边把原始因果链抹掉。

```powershell
$Namespace = 'production'
$PodName = 'order-api-7c8d9f6b5d-k9x2m'
$CollectedAt = Get-Date -Format 'yyyyMMdd-HHmmss'
$PodUid = kubectl get pod -n $Namespace $PodName -o jsonpath='{.metadata.uid}'
$EvidenceDir = Join-Path (Get-Location) "scheduler-evidence-$CollectedAt-$PodUid"
New-Item -ItemType Directory -Path $EvidenceDir -Force | Out-Null

kubectl get pod -n $Namespace $PodName -o yaml |
  Set-Content -LiteralPath (Join-Path $EvidenceDir 'pod.yaml') -Encoding utf8

kubectl describe pod -n $Namespace $PodName |
  Set-Content -LiteralPath (Join-Path $EvidenceDir 'pod-describe.txt') -Encoding utf8

kubectl get nodes -o wide |
  Set-Content -LiteralPath (Join-Path $EvidenceDir 'nodes-wide.txt') -Encoding utf8

kubectl get nodes --show-labels |
  Set-Content -LiteralPath (Join-Path $EvidenceDir 'nodes-labels.txt') -Encoding utf8

kubectl get nodes -o yaml |
  Set-Content -LiteralPath (Join-Path $EvidenceDir 'nodes.yaml') -Encoding utf8

kubectl get events -n $Namespace --field-selector "involvedObject.uid=$PodUid" --sort-by='.lastTimestamp' -o yaml |
  Set-Content -LiteralPath (Join-Path $EvidenceDir 'pod-events.yaml') -Encoding utf8
```

说明：

- `describe` 方便人看，但不是稳定机器接口；自动化尽量解析结构化 JSON/YAML 字段；
- Event 可能聚合、限流和过期，保存现场越早越好；
- 生产工单中记录 Pod UID，不只记录名字；控制器重建后同名/相似名 Pod 已不是同一实例；
- 不要第一步就 `kubectl delete pod`，删除会改变 requested、队列和事件现场。
- 若 Pod 使用卷或 DRA，再保存 PVC/PV/StorageClass/CSINode/CSIStorageCapacity，或 DeviceClass/ResourceClaim/ResourceSlice；同时记录 schedulerName、scheduler 配置版本、采集时区与时间。

### 19.3 把失败消息翻译成“节点集合交集为空”

面对下面这类 Event：

```text
0/12 nodes are available:
4 Insufficient cpu,
3 node(s) didn't match Pod's node affinity/selector,
5 node(s) had untolerated taint.
```

不要把数字相加后就认定三类节点互不重叠；消息是诊断聚合，某节点可能有多个失败原因。正确方法是逐层构造集合：

```text
全集 N：所有 12 台候选 Node
N1 = N 中满足 required node affinity 的节点
N2 = N1 中能容忍目标 taint 的节点
N3 = N2 中端口、卷、拓扑等通过的节点
N4 = N3 中 CPU/memory/GPU request 余额足够的节点

若 N4 为空，Pod Pending
```

这也是 Filter 插件模型的本质：多个硬条件做交集，不是让一个“万能算法”给出玄学结论。

### 19.4 按失败插件取证

| 失败方向 | 必看对象/字段 | 常见误判 |
|---|---|---|
| `NodeAffinity` | Pod `nodeSelector`、required affinity、Node labels、profile addedAffinity | 只看 Pod YAML，不知道 profile 还追加了条件 |
| `TaintToleration` | Node `spec.taints`、Pod tolerations、effect/operator/value | 以为 toleration 会吸引 Pod 去该节点 |
| `NodeResourcesFit` | Pod 最终 request、Node allocatable、节点上所有 Pod requests | 用 `kubectl top` 的 usage 代替 request |
| `PodTopologySpread` | constraint、selector、namespace、topologyKey、各域匹配 Pod 数 | 只看目标 Pod，不统计它选择的同伴 |
| `InterPodAffinity` | namespaceSelector/namespaces、labelSelector、topologyKey | 忽略 namespace 范围或现有 Pod 标签 |
| `NodePorts` | Pod hostPort、节点上已占用 hostPort | 把 Service port 当成 hostPort 冲突 |
| `VolumeBinding` | PVC/PV/StorageClass、bindingMode、CSI 拓扑、selected-node annotation | 只查 CPU，不查 WFFC 卷拓扑 |
| `DynamicResources` | ResourceClaim、DeviceClass、ResourceSlice、allocation 状态、驱动 | 只看 Node 的传统 scalar 资源 |
| `DefaultPreemption` | PriorityClass、preemptionPolicy、候选受害者、PDB、nominatedNodeName | 以为高优先级一定能腾出一个可行节点 |

### 19.5 CPU/内存 request 怎么对账

`kubectl describe node` 的 Allocated resources 区域可快速浏览已绑定 Pod requests，但它看不到 scheduler 内存里刚 Assume、尚未持久绑定的瞬时对象。更严谨时应同时看 scheduler 指标/日志与 API 对象时间线。

下面脚本汇总某节点上 API 已绑定 Pod 的容器 requests；它用于人工核对思路，不替代 scheduler 当前版本的完整 Pod request 计算，尤其不涵盖所有 init container、Pod-level resources、overhead、原地 resize 等细节：

```powershell
$NodeName = 'worker-a'
kubectl get pods -A --field-selector "spec.nodeName=$NodeName" -o json |
  ConvertFrom-Json |
  Select-Object -ExpandProperty items |
  ForEach-Object {
    $pod = $_
    $pod.spec.containers | ForEach-Object {
      [PSCustomObject]@{
        Namespace = $pod.metadata.namespace
        Pod       = $pod.metadata.name
        Container = $_.name
        CPU       = $_.resources.requests.cpu
        Memory    = $_.resources.requests.memory
        GPU       = $_.resources.requests.'nvidia.com/gpu'
      }
    }
  } | Format-Table -AutoSize
```

为什么这里只称“人工核对”：真正的 `PodRequests` 还要处理：

- 普通 app containers 的和；
- init containers 的阶段峰值；
- restartable init container 的特殊累计规则；
- Pod overhead；
- Pod-level resources 和原地 resize 特性带来的版本差异；
- 扩展资源从 limit 默认到 request 的 API 行为。

若要得出字节级/毫核级准确结论，应按目标源码版本的 `resource.PodRequests` 与 `NodeInfo` 逻辑复算，而不是手抄一个永远不变的公式。

### 19.6 GPU Pending 的专用排障树

```mermaid
flowchart TD
    A["GPU Pod nodeName 为空"] --> B["确认请求形式与目标资源名"]
    B --> C{"候选 Node 的传统 scalar Allocatable 大于 0 吗"}
    C -->|"是"| D["传统 Device Plugin 路径"]
    D --> E{"Allocatable - Requested 数量够吗"}
    E -->|"否"| F["查 ListAndWatch、健康、已绑定/assumed 占账与碎片"]
    E -->|"是"| G["继续查 label、taint、卷、拓扑与 profile"]
    C -->|"否或不存在"| H{"存在匹配 extendedResourceName 的 DeviceClass，或 Pod 显式使用 Claim 吗"}
    H -->|"是"| I["DRA 路径"]
    I --> J["查 DeviceClass、ResourceSlice、ResourceClaim、DynamicResources 与 AllocationResult"]
    H -->|"否"| K["该 Node 没有可识别供给；再查驱动/插件/资源声明"]
```

这棵树故意先分供给模式。启用 `DRAExtendedResource` 时，同一个资源名可在某些 Node 由传统 scalar 提供，在另一些 Node 上 scalar 为 0/不存在、却通过 `DeviceClass.spec.extendedResourceName` 与 `ResourceSlice` 由 DRA 提供。因此“Node status 没有 `nvidia.com/gpu`”不能直接判为 Device Plugin 故障。显式 ResourceClaim 的 Pod 更不应从传统 Node scalar 开始排障。

传统 Device Plugin 路径的只读检查：

```powershell
kubectl get nodes -o custom-columns='NAME:.metadata.name,GPU-CAP:.status.capacity.nvidia\.com/gpu,GPU-ALLOC:.status.allocatable.nvidia\.com/gpu'

kubectl get pods -A -o json |
  ConvertFrom-Json |
  Select-Object -ExpandProperty items |
  Where-Object { $_.spec.nodeName } |
  ForEach-Object {
    $gpuTotal = 0
    foreach ($container in $_.spec.containers) {
      $quantity = $container.resources.requests.'nvidia.com/gpu'
      if ($null -ne $quantity) { $gpuTotal += [int]$quantity }
    }
    if ($gpuTotal -gt 0) {
      [PSCustomObject]@{
        Namespace = $_.metadata.namespace
        Pod       = $_.metadata.name
        Node      = $_.spec.nodeName
        GPUReq    = $gpuTotal
      }
    }
  } | Sort-Object Node, Namespace, Pod | Format-Table -AutoSize
```

生产环境中 GPU 资源名可能是 MIG profile、其他厂商资源或平台抽象名，命令必须替换成真实名称。time-slicing 下这里统计的是逻辑份额，不能当成物理 GPU 台账。

### 19.7 已经绑定却拿不到 GPU，为什么不是重新调度一下就好

若 `spec.nodeName` 已写入，后续 DeviceManager 分配或 `Allocate` 失败，原 Pod 通常不会由 scheduler 自动把 `spec.nodeName` 改成另一台节点。Kubernetes 的绑定不是可随意重写的“建议”。恢复往往需要：

- **传统 DeviceManager 的 Allocate 在 kubelet Pod admission 阶段失败**：通常形成 `UnexpectedAdmissionError`，Pod 被拒绝并进入 `Failed`；设备插件恢复不会复活这个终态 Pod，应由 Deployment/Job 等控制器创建替代 Pod，独立 Pod 则按变更流程删除并重建；
- **其他非终态的容器创建/runtime 错误**：才可能由 kubelet 的容器重试机制原地重试，必须先确认失败层次；
- 节点健康控制触发驱逐/重建；
- 人工隔离故障节点并按变更流程处置。

具体动作有业务影响，必须先确认控制器、重启策略、checkpoint、训练容错和数据一致性。排障讲义只给证据路径，不授权在生产直接删除训练 Pod。

### 19.8 日志该怎样开，才不会把控制面打爆

建议从低成本证据逐级升级：

```text
Pod YAML/Condition/Event
  -> Node/PVC/Claim 等对象
  -> scheduler 稳定 metrics
  -> 当前常规日志关联 Pod UID
  -> 临时、受控提高 verbosity
  -> 必要时 profile 影子/测试环境复现
```

高 verbosity 会显著增加 CPU、磁盘与日志平台压力，还可能输出大量对象元数据。若必须提高：限定时间窗、单副本/测试 scheduler、明确回退时间，遵守集群日志隐私规范。不要为了找一个 Pod，把所有控制面长期开到最高日志级别。

---

## 20. 可观测性：指标告诉你“系统性问题”，Event 告诉你“这个 Pod 的一次观察”

### 20.1 当前固定源码中的关键 scheduler 指标

指标名带 `scheduler_` 前缀。稳定级别是 API 承诺的一部分；ALPHA 指标可能改名、删掉或调整 label，升级时不能无审查继承面板。

| 指标 | 当前稳定度 | 主要含义 | 运维用法 |
|---|---|---|---|
| `scheduler_pending_pods{queue}` | STABLE | active/backoff/unschedulable/gated 各队列数量 | 区分排队、退避、硬条件失败和从未尝试的 gate |
| `scheduler_schedule_attempts_total{result,profile}` | STABLE | 调度尝试按成功、unschedulable、error 等结果累计 | 看失败率与内部错误率趋势 |
| `scheduler_scheduling_attempt_duration_seconds{result,profile}` | STABLE | 一次尝试的算法加绑定延迟 | 看 p95/p99 调度尝试延迟 |
| `scheduler_pod_scheduling_sli_duration_seconds{attempts}` | BETA | Pod 从进入队列到最终成功，可能跨多次尝试 | 更贴近用户等待体验 |
| `scheduler_pod_scheduling_attempts` | STABLE | 成功 Pod 经历的尝试次数 | 发现反复重试 |
| `scheduler_framework_extension_point_duration_seconds{extension_point,status,profile}` | STABLE | 某扩展点所有插件总延迟 | 定位慢在 Filter、Score、Bind 等哪段 |
| `scheduler_plugin_execution_duration_seconds{plugin,extension_point,status}` | ALPHA | 单插件执行延迟 | 深挖慢插件，注意版本和采样/开销 |
| `scheduler_unschedulable_pods{plugin,profile}` | BETA | 被每个插件拒绝的 Pod 数 | 找系统性 NodeResourcesFit/affinity/volume 问题 |
| `scheduler_queue_incoming_pods_total{queue,event}` | STABLE | 哪类事件把 Pod 放入哪个队列 | 分析重排风暴和有用事件 |
| `scheduler_permit_wait_duration_seconds{result}` | BETA | Permit 等待时长 | 查 gang/协调插件等待 |
| `scheduler_preemption_attempts_total` | STABLE | 抢占尝试总数 | 发现容量/优先级压力 |
| `scheduler_preemption_victims` | STABLE | 每次选中的受害者数量分布 | 评估抢占破坏面 |
| `scheduler_inflight_events{event}` | ALPHA | 队列正在跟踪的 in-flight 事件 | 研究事件压力与当前实现 |
| `scheduler_pod_scheduled_after_flush_total` | ALPHA | 因超时 flush 后才成功的 Pod 数 | 辅助发现 QueueingHint/事件遗漏风险 |

指标定义可在 `pkg/scheduler/metrics/metrics.go` 对照当前提交。面板里不要只画平均值；调度延迟通常要看直方图分位数和流量分母。

### 20.2 四种队列高分别说明什么

```text
active 高且持续增长
  -> scheduler 吞吐可能跟不上、leader/插件/API 变慢，或突然发布洪峰

backoff 高
  -> 大量 Pod 经历失败并在退避；要结合 result=error/unschedulable 区分

unschedulable 高
  -> 已尝试且硬条件不满足；看 scheduler_unschedulable_pods 的 plugin 分解

gated 高
  -> Pod 被 scheduling gate/PreEnqueue 类机制挡住，可能是预期的准入等待
```

单看总 Pending 会把四种完全不同的处置方式混在一起。

### 20.3 一组起点 PromQL

以下是思路模板，实际抓取 job、label 和时间窗要按集群监控栈调整：

```promql
# 各队列当前积压
sum by (queue) (scheduler_pending_pods)

# 最近 5 分钟每秒调度尝试，按结果与 profile
sum by (result, profile) (rate(scheduler_schedule_attempts_total[5m]))

# 调度尝试 p99；必须先按 le 聚合直方图桶
histogram_quantile(
  0.99,
  sum by (le, result, profile) (
    rate(scheduler_scheduling_attempt_duration_seconds_bucket[5m])
  )
)

# 哪些插件造成最多不可调度 Pod
topk(10, sum by (plugin, profile) (scheduler_unschedulable_pods))

# 哪个 Framework 扩展点变慢
histogram_quantile(
  0.99,
  sum by (le, extension_point, profile) (
    rate(scheduler_framework_extension_point_duration_seconds_bucket[5m])
  )
)
```

`scheduler_unschedulable_pods` 的一个 Pod 可能同时计入多个拒绝插件，不能把各 plugin 值相加当成唯一 Pod 总数。

### 20.4 SLO 要分“调度器健康”和“业务可调度性”

建议至少拆成两类：

**调度器服务 SLO：**

- scheduler leader 可用；
- 内部 `result="error"` 比例；
- activeQ 等待与成功调度延迟；
- Bind/API 错误；
- extension point/plugin 延迟；
- informer/cache 异常。

**业务容量 SLO/信号：**

- 因资源、affinity、taint、volume、GPU 等原因的 unschedulable 数与年龄；
- 各节点池/ResourceFlavor 的可调度余量和碎片；
- Kueue admission 等待时间；
- 发布 surge 导致的等待；
- GPU 逻辑分配率、物理利用率、健康与碎片分别展示。

如果“任何 FailedScheduling 都算 scheduler 不可用”，业务 requests 写错也会让平台 SLO 红；如果只看 scheduler 进程活着，整个 GPU 池资源名消失也可能仍是绿。两类目标必须分开。

一个**用于启动讨论、不是通用标准答案**的度量模板：

| 目标 | SLI/分母 | 示例目标与窗口 | 排除/注意 |
|---|---|---|---|
| 调度器内部可靠性 | `1 - result="error" 的 attempts / 全部 attempts` | 30 天不低于 99.9% | Unschedulable 是业务结论，不算内部 error |
| 成功尝试延迟 | 成功 result 的 attempt duration p99 | 例如 5 分钟窗口 p99 < 1s | 只覆盖一次成功尝试，不等于 Pod 全部等待 |
| 用户绑定体验 | 进入 scheduler 责任域的 Pod 中，X 秒内出现 `spec.nodeName` 的比例 | 在线业务可先讨论 99%/10s/30 天 | 排除 Kueue 未 Admitted、显式 scheduling gate、无匹配 scheduler 的错误路由；需外部观测器 |
| 业务可调度性 | 各 workload class 中 Unschedulable 持续超过阈值的 Pod 比例 | 在线与批任务分别设 2 分钟/30 分钟等阈值 | 失败原因和容量责任要分类，不能都算 scheduler 故障 |

数值必须根据集群规模、发布峰值和业务 SLO 校准。告警应基于误差预算做多窗口 burn-rate，例如短窗口 5m+1h 发现快速燃烧、长窗口 30m+6h 发现慢性燃烧，而不是见一个 Pending 就翻页。

特别注意成功者偏差：当前 `scheduler_pod_scheduling_sli_duration_seconds` 在 Pod 成功完成 Binding 后观察；永远没成功的 Pod不会进入该直方图。仅看它可能得到“成功者都很快”的漂亮结论，所以用户绑定 SLI 要由平台控制器、Condition/Event 库或其他外部状态观察补上未完成样本。scheduler 也没有直接给出“每个不可调度 Pod 年龄”的低基数聚合指标；这通常要结合 kube-state-metrics、对象 Condition/Event 或平台库存计算。

### 20.5 告警要带处置上下文

一个好的告警不是“PendingPods > 0”，而是类似：

```text
范围：gpu-binpack-scheduler profile
现象：unschedulable 队列持续 15 分钟增长
主拒绝插件：NodeResourcesFit / NodeAffinity
受影响命名空间和 PriorityClass：通过事件侧或平台库存关联
GPU 池：A100-80GB
容量证据：allocatable、requested、物理健康、Kueue quota
推荐第一步：确认是单节点碎片还是整池不足
禁止自动动作：不要直接删低优先级训练 Pod
```

指标标签不一定包含 namespace 和 Pod，这是为了控制基数。细粒度归因可由 Event、审计日志、平台控制器状态和定期库存快照补全，不要贸然把 Pod UID 加进每个 Prometheus 指标。

---

## 21. 容量规划：总量够不等于任何一个 Pod 放得下

### 21.1 三个层级都要算

```text
集群总量：整个集群够不够
节点池总量：目标标签/污点/资源类型的池够不够
单节点形状：一个 Pod 或一组约束能不能在同一节点满足
```

例如集群有 4 台 GPU 节点，各剩 1 张卡，总余量 4；一个请求 4 张卡的 Pod 仍然一个节点也放不下。scheduler 不会把一个普通 Pod 跨 4 台节点拆开。

### 21.2 碎片不是只有 GPU 数量碎片

一个节点必须同时满足多维余额：

```text
CPU >= Pod CPU request
memory >= Pod memory request
目标 GPU resource >= GPU request
ephemeral-storage >= request
Pod slots >= 1
host ports 无冲突
volume topology 可达
required labels/taints/affinity 全通过
```

GPU 还有“交叉碎片”：

- `gpu-a` 剩 2 张 GPU，但只剩 8 GiB memory；
- `gpu-b` 剩 128 GiB memory，但 GPU 已满；
- 两者总和看起来都够，一个 2-GPU/64-GiB Pod 仍无落点。

所以容量面板需要“可承载典型 Pod shape 的节点数”，而不只是按每种资源分别求和。

### 21.3 用 Pod shape 做可调度容量

定义一个线上推理 shape：

```text
cpu=8
memory=48Gi
nvidia.com/gpu=1
pool=a100-80gb
```

对每个满足硬标签/污点的节点，估算：

```text
该节点还能放的 shape 数
= min(
  floor(cpu_remaining / 8),
  floor(memory_remaining / 48Gi),
  floor(gpu_remaining / 1),
  remaining_pod_slots,
  其他不可分约束允许数
)
```

再对节点求和，才是这个 shape 的粗略可调度余量。卷、端口、亲和和动态资源会让真实值更低，因此平台应把它标为估算，并用 scheduler 仿真校验。

### 21.4 扩容为什么不能立刻消除 Pending

从触发 Cluster Autoscaler 或云扩容到可调度，链路可能包括：

```text
发现不可调度 Pod
  -> 判定可扩的 node group
  -> 云厂商创建实例
  -> 操作系统启动
  -> kubelet 加入
  -> CNI/CSI/DaemonSet 就绪
  -> GPU 驱动和 Device Plugin 就绪
  -> Node label/taint/allocatable 正确
  -> informer 把变化送到 scheduler
  -> Pod 被重新激活并调度
```

GPU 节点通常比普通 CPU 节点准备时间更长。若业务 SLO 小于冷启动时间，必须做 warm pool、预留容量或排队准入，不能只依赖看到 Pending 后再扩。

### 21.5 装箱和打散没有全局唯一答案

| 策略 | 好处 | 代价 | 常见适用 |
|---|---|---|---|
| GPU 装箱 | 留出整节点，便于大任务；可能缩容更多节点 | 热点和故障半径增大 | 可迁移批任务、成本优先 |
| GPU 打散 | 降低单节点故障影响，平衡热量/带宽 | 产生碎片，未来多卡任务难进 | 在线关键推理 |
| CPU/内存打散、GPU 装箱 | 可能兼顾资源，但多维分数会互相拉扯 | 配置和解释复杂 | 需仿真验证的平台 |
| 按业务亲和共置 | 减少网络延迟 | 争抢本地资源、连带故障 | 有明确通信收益 |

平台应按 workload class 做少量 profile，而不是寻找一个让所有业务都满意的万能权重。

---

## 22. 实验设计与验收思路：亲眼看见 Filter、Score、抢占与 GPU 边界

> 以下实验面向测试集群。不要在生产节点随意加污点、改标签、部署占资源 Pod 或修改 scheduler 配置。

本节给出实验目的、关键输入、观察点和清理方向；除实验一外，没有为每种发行版拼成可直接执行的一键实验包。镜像仓库、节点名、准入策略、Volcano/Kueue/DRA 版本和 GPU 供给方式必须按隔离环境补齐。每次实验都先做 `kubectl diff`/对象审阅并确认清理方式。

### 22.1 实验记录模板

每次实验都按下面格式记，避免只记“成功/失败”：

```text
假设：我认为哪个插件会在什么阶段做什么
前置：Kubernetes 版本、scheduler 配置、feature gate、Node/Pod 初态
输入：完整 YAML 与命令
观察：Pod Condition、Event、nodeName、scheduler metrics/log
源码：对应函数与分支
反证：修改哪个单一变量，结果应该如何改变
结论：本版本验证了什么；哪些还没验证
清理：删除哪些测试对象，恢复哪些 label/taint
```

### 22.2 实验一：用 request 制造 `Insufficient cpu`

先看测试节点 allocatable，选择一个不会影响他人的隔离测试节点。创建一个 request 明显高于单节点总量的 Pod：

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: scheduler-lab-too-large
  namespace: default
spec:
  restartPolicy: Never
  containers:
  - name: pause
    image: registry.k8s.io/pause:3.10
    resources:
      requests:
        cpu: "1000"
        memory: 1Mi
```

预期：NodeResourcesFit 报 CPU 不足，而且因为单 Pod request 大于任何节点总 allocatable，抢占也不能解决。若测试 namespace 有 ResourceQuota/LimitRange，`cpu: "1000"` 可能先在 API 准入阶段被拒绝；应使用隔离 namespace，或改成“略高于最大单节点 allocatable、但低于准入上限”的值，确保实验真的到达 scheduler。反证变量：把 CPU request 调整到测试节点可承载值，重新创建新 Pod，观察可行集合变化。

### 22.3 实验二：证明 toleration 不是吸引力

准备两台测试节点，其中一台：

```powershell
kubectl label node lab-node-a platform.example.com/pool=special
kubectl taint node lab-node-a workload.platform.example.com/class=special:NoSchedule
```

Pod A 只写 toleration，不写 node affinity；Pod B 同时写 toleration 与 required node affinity。预期：

- A 被允许进入特殊节点，但也可能被调度到其他可行节点；
- B 必须进入带 `pool=special` 标签且污点可容忍的节点。

清理时使用精确键恢复测试节点：

```powershell
kubectl taint node lab-node-a workload.platform.example.com/class:NoSchedule-
kubectl label node lab-node-a platform.example.com/pool-
```

这些是有状态变更，只能对明确的测试节点执行。

### 22.4 实验三：证明 `nodeName` 出现后责任域已经切换

创建一个镜像名故意不存在、但 request 很小的 Pod。预期过程：

```text
先成功得到 spec.nodeName
随后进入 ErrImagePull / ImagePullBackOff
```

这证明 scheduler 的节点选择可以成功，而 kubelet 的镜像兑现失败。不要把所有 Pending 都统计成 scheduler 失败。

### 22.5 实验四：抢占不是瞬移

在隔离节点池：

1. 创建低优先级、占用大部分 request 的可终止测试 Pod；
2. 创建较高优先级 Pod；
3. 让高优先级 Pod 只能匹配该池；
4. 观察 FailedScheduling、抢占 Event、受害者 deletionTimestamp、`nominatedNodeName`、最终 `spec.nodeName`；
5. 把受害者优雅终止时间设置为可观察但安全的短值；
6. 对比 `preemptionPolicy: Never` 的高优先级 Pod。

实验前确认没有真实业务和 PDB 依赖该节点池。抢占会真实删除受害 Pod，不能在共享生产池练习。

### 22.6 实验五：没有 GPU 也能验证扩展资源 Filter

Kubernetes 允许通过设备插件或节点状态上报扩展资源，但不要手工篡改生产 Node status。最安全的路径是在本地测试集群部署官方示例设备插件或测试用设备插件，暴露例如 `example.com/fake-device`：

```yaml
resources:
  limits:
    example.com/fake-device: 1
```

观察：

- 资源名如何出现在 Node capacity/allocatable；
- 一个 Pod 绑定后 requested 如何变化；
- 超过 allocatable 时 NodeResourcesFit 如何报不足；
- scheduler 仍只选择 Node，kubelet 侧插件才执行 Allocate。

这样可在没有昂贵 GPU 的环境先掌握传统设备调度的主要控制链。

### 22.7 实验六：有 GPU 时验证“调度成功不等于 CUDA 成功”

分两层验收：

```text
调度层：Pod 有正确 nodeName；Node 的 GPU request 账增加
节点层：容器创建成功；nvidia-smi 可见预期设备；CUDA sample 成功
```

再设计三个单变量故障：

- 错误的 GPU 产品 required affinity：预期 Filter 阶段失败；
- 正确节点但故意使用不兼容 CUDA 镜像：预期已绑定后应用/运行时失败；
- 将测试设备标记异常或停止测试设备插件：观察 allocatable 传播与 Event，严格限定隔离环境。

记录每一步的 Node status、Pod UID、Condition、Event、设备插件日志和时间戳，才能把异步传播延迟与真正错误区分开。

### 22.8 实验七：比较 GPU 默认 Score 与显式 GPU Score

构造两台都能放下 1-GPU Pod、但 GPU 使用比例不同的同型测试节点。先用默认 profile 多次创建等价 Pod，记录可行节点和落点；再在独立测试 profile 中配置 NodeResourcesFit 对 `nvidia.com/gpu` 的 MostAllocated 评分。

验收不是“每次都必须去某节点”，而是：

- 从 scheduler 日志/测试断言确认 GPU 进入评分资源表；
- 手算 NodeResourcesFit 原始/归一化分数；
- 把其他 Score 插件和权重纳入总分；
- 确认平分时仍可能有不同选择；
- 观察装箱对 CPU/内存、故障域和后续大 Pod 的影响。

---

## 23. 八类生产事故复盘：表象相似，根因横跨不同组件

### 23.1 发布时老 Pod 正常，新 Pod 全 Pending

**表象：** 稳定运行时没问题，一滚动升级就出现 `Insufficient cpu`。

**因果链：**

```text
旧副本 requests 已占大部分节点池
  -> Deployment 按 maxSurge 创建额外新 Pod
  -> scheduler 把新 Pod 的 request 与剩余账比较
  -> 没有节点余额达到单 Pod shape
  -> 新 Pod Pending；旧 Pod 因可用性策略暂不缩减
```

**证据：** Deployment strategy、replicas、旧新 ReplicaSet 数量、Pod requests、目标池 allocatable/requested、FailedScheduling 时间线。

**修复方向：** 评估并调整 surge/容量/requests/发布批次，而不是先改 scheduler 权重。Score 不能让硬余额不足的节点通过 Filter。

### 23.2 扩了 GPU 节点，Pod 还是看不到 GPU

**表象：** 云平台显示新实例带 GPU，Kubernetes Node 也 Ready，但 `nvidia.com/gpu` 不在 allocatable。

**可能链路：**

```text
实例创建
  -> Node Ready
  -> GPU 驱动尚未可用，或 Device Plugin 未注册
  -> kubelet 没有收到有效 ListAndWatch
  -> Node status 没有 GPU 扩展资源
  -> scheduler 不可能拿这台 Node 满足 GPU request
```

**证据：** Node capacity/allocatable、GPU Operator/驱动状态、设备插件 Pod 是否落到节点、插件与 kubelet 日志、节点标签/污点是否齐全。

**修复方向：** 修复设备供给链和节点就绪门槛。不要给 Node 手工贴一个“有 GPU”的普通 label 就宣称资源可用；label 不会创建 `nvidia.com/gpu` allocatable。

### 23.3 GPU 总余量很多，4-GPU Job 仍 Pending

**表象：** 面板显示整池空闲 6 张，但没有一台节点空 4 张。

**根因：** 单节点碎片。普通 Pod 的 4 张 GPU request 必须由同一 Node 满足。

**证据：** 每节点目标资源的 allocatable-requested、CPU/内存交叉余量、required affinity 和卷拓扑。

**修复方向：** Kueue/队列准入、装箱 profile、任务规格、节点池形状、可控迁移/排空。不能直接把 4-GPU Pod 拆成 4 台节点；分布式训练需要控制器、通信配置和多个 Pod 的业务设计。

### 23.4 Event 说 CPU 不足，但 `kubectl top node` 很低

**根因：** scheduler 按 request 账，不按瞬时 usage。应用可能申请 8 核、实际只用 1 核。

**证据：** Pod spec 最终 request、LimitRange/Admission 修改、节点上所有 Pod requests、VPA 推荐、较长时间利用率分布。

**修复方向：** 通过容量分析和压测重新定 request；若只是临时为了让 Pod 进场而大幅降低 request，可能把调度 Pending 变成节点 CPU 争用、延迟抖动和 OOM。

### 23.5 Event 只有污点问题，明明 Pod 写了 toleration

常见细节：

- key 或 value 不一致；
- `operator: Equal` 与 `Exists` 理解错；
- effect 不一致；
- Node 还有第二条未容忍 taint；
- Pod 的目标节点集合其实由 affinity 限到另一批有不同污点的节点；
- `NoExecute` 还会影响已运行 Pod，和 `NoSchedule` 的行为不一样。

证据必须是完整 `Node.spec.taints` 与最终 Pod tolerations，而不是平台模板截图。

### 23.6 Pod 被提名到节点，却迟迟没绑定

**可能原因：** 受害者优雅退出、PDB/抢占方案变化、目标节点新变化、卷/动态资源等待、Permit、API 更新或 Pod 已被替换。

**证据：** `status.nominatedNodeName`、`spec.nodeName`、受害者 deletionTimestamp、PriorityClass、PDB、Permit metrics、scheduler Event/log 时间线、Pod UID。

**不要做的事：** 看到 nomination 就把目标节点标成故障；也不要认为 nomination 是容量预留的强保证。

### 23.7 Kueue 已 Admitted，Pod 仍然 Pending

**解释：** `Admitted` 说明 Workload 已完成当前 Kueue 配置要求的准入流程：至少已经 quota reservation，所有配置的 AdmissionChecks 等条件已满足；启用 TAS 等能力时，还可能已经计算过准入时的物理拓扑可行性。但它仍不是每个 Pod 的 Node Binding，kube-scheduler 还要解决节点级硬约束，且准入后的集群事实会继续变化。可能是：

- 配额按总资源允许，但节点已碎片化；
- ResourceFlavor 对应的节点标签/污点合同不一致；
- Job Pod 还有 PVC、topology 或 affinity；
- 设备健康下降发生在 admission 后；
- 另一个已准入工作负载先占用了物理节点。

平台状态页应同时呈现 Workload admission 与每个 Pod 的 `PodScheduled` 状态。

### 23.8 scheduler 进程正常，却大面积调度变慢

可能不是“算法算不动”这么简单：

```text
activeQ 发布洪峰
API Server / etcd 写入慢导致 Bind 变慢
自定义 Filter/Score 插件延迟
Extender 网络超时
大量抢占模拟
复杂 affinity/topology 匹配
Node/Pod 规模增长与扫描比例
事件风暴导致反复入队
Permit 等待或异步 API 调用堆积
```

证据顺序：队列长度 -> attempt latency -> result -> extension point -> plugin/extender -> API Server 指标 -> 规模和变更时间线。不要只抓一个 goroutine profile 就跳过上游事实。

---

## 24. 源码带读路线：从会排障到能改插件，按状态所有权前进

### 24.1 第一遍：只追一个成功 Pod

当前固定提交的入口地图如下。行号只对 `301946d15e67a4a2e8a5fb8292eb836acd366d78` 有效：

| 顺序 | 文件与函数 | 当前起始行附近 | 这一站只问一个问题 |
|---:|---|---:|---|
| 1 | `pkg/scheduler/scheduler.go` `Scheduler.Run` | 554 | worker 怎样启动并持续调度 |
| 2 | `pkg/scheduler/schedule_one.go` `ScheduleOne` | 67 | 单次 worker 怎样拿一个 Pod |
| 3 | 同文件 `scheduleOnePod` | 99 | profile、cycle 与异步 binding 怎样衔接 |
| 4 | 同文件 `schedulingCycle` | 175 | 选点、Assume、Reserve、Permit 的同步边界 |
| 5 | 同文件 `schedulingAlgorithm` | 256 | snapshot 与核心选点函数怎样串联 |
| 6 | 同文件 `schedulePod` | 570 | Filter、0/1/多节点、Score 的总分支 |
| 7 | 同文件 `findNodesThatFitPod` | 628 | PreFilter、Filter、extender 怎样组成可行集合 |
| 8 | 同文件 `prioritizeNodes` | 943 | Score 插件与 extender 分数怎样合并 |
| 9 | 同文件 `assumeAndReserve` | 313 | cache Assume 与插件 Reserve 的事务边界 |
| 10 | 同文件 `bindingCycle` | 397 | Permit 等待、PreBind、Bind、PostBind |
| 11 | 同文件 `bind` | 1148 | extender binder 与 Framework Bind 的次序 |

第一遍不要跳进每个插件。你只要能在纸上画出：`PodInfo -> CycleState -> ScheduleResult -> assumedPodInfo -> Binding`。

### 24.2 第二遍：追一个失败 Pod

| 顺序 | 文件/函数 | 要验证的状态 |
|---:|---|---|
| 1 | `findNodesThatPassFilters` | 每个节点的 Status 由谁产生 |
| 2 | `Diagnosis.NodeToStatus` / `UnschedulablePlugins` | 失败节点与拒绝插件怎样汇总 |
| 3 | `FitError` | 0 个可行 Node 如何形成调度失败 |
| 4 | PostFilter / `DefaultPreemption.PostFilter` | 正常失败后是否存在抢占机会 |
| 5 | `handleSchedulingFailure` | Event、Condition、nomination 与重排怎样分开 |
| 6 | scheduling queue `AddUnschedulableIfNotPresent` | Pod 去 active/backoff/unschedulable 哪个队列 |
| 7 | 插件 `EventsToRegister` / QueueingHint | 什么变化值得唤醒该 Pod |

读失败链时，给每个 Status 标三项：`Code`、`Reasons`、`FailedPlugin`。不要只在日志里搜索字符串。

### 24.3 第三遍：只读一个插件的所有 extension point

建议顺序：

1. `NodeResourcesFit`：最贴近日常 request/allocatable；
2. `NodeAffinity`：把 YAML selector/terms 变成集合判断；
3. `TaintToleration`：同时观察 Filter 与 Score；
4. `VolumeBinding`：学习 CycleState、Reserve/PreBind 与外部对象；
5. `DefaultPreemption`：学习 PostFilter、模拟 NodeInfo 和候选排序；
6. `DynamicResources`：最后再学，因为对象、异步分配和特性状态都更复杂。

对每个插件填写同一张卡：

```text
插件名：
注册在哪些 extension point：
PreFilter 写了什么 CycleState：
Filter 只读哪些对象：
成功时返回什么：
失败时 Code/Reason/FailedPlugin：
监听什么 ClusterEvent：
QueueingHint 为什么认为事件有用：
Reserve 后怎样 Unreserve：
有哪些 feature gate / config args：
对应哪条生产故障：
```

### 24.4 新手读 Go 只补七个语法点

| Go 语法/概念 | 在 scheduler 中为什么必须懂 | 对运维的类比 |
|---|---|---|
| interface | Framework 通过接口调用不同插件 | 同一扩展点的标准插槽 |
| struct 与指针 | PodInfo、NodeInfo、CycleState、Status 都以结构体传递 | 对象快照/状态载体 |
| slice/map | 节点列表、分数、资源名、诊断集合 | 清单与索引 |
| `defer` | 保证 `Done`、metrics、清理在函数退出时执行 | finally/收尾钩子 |
| goroutine/channel | Filter 并行、异步 binding、Permit 等待 | 并发 worker 与消息协调 |
| `context.Context` | 取消、trace、超时沿调用链传播 | 一次请求的生命周期令牌 |
| `error` 与 `*framework.Status` | 区分 Go 内部错误和调度语义状态 | 异常 vs 可解释业务结果 |

源码里看见 goroutine 时固定问四句：

```text
谁启动它？
谁等待它？
失败怎样回传？
它读写的对象是否仍有效？
```

例如 `scheduleOnePod` 的调度 cycle 同步执行，而 binding cycle 可异步进行；这就是为什么 cache Assume 必须先发生，为什么绑定失败还要显式回滚和唤醒其他 Pod。

### 24.5 断点与日志观察点

本地调试建议优先在这些位置打断点或加临时、受控日志：

```text
ScheduleOne                  -> 看 Pop 出的 PodInfo/Attempts
schedulePod                  -> 看 feasibleNodes 数量
findNodesThatPassFilters     -> 看每节点失败插件和 Status
prioritizeNodes              -> 看各插件 NodeScoreList 与总分
assumeAndReserve             -> 看 cache Assume 前后 Requested
bindingCycle                 -> 看 Permit/PreBind/Bind Status
handleBindingCycleError      -> 看 Unreserve/Forget/唤醒
handleSchedulingFailure      -> 看 latest Pod UID 与重排目标
```

不要在生产二进制临时改日志。推荐本地 kind/集成测试、自定义构建或现有结构化日志，并控制对象数据的敏感性。

### 24.6 源码验证的最小测试组合

源码学习不是只读函数名。每个结论至少找一种可复查证据：

```text
静态：函数连续代码、接口契约、配置默认值
单测：插件输入 -> Status/Score/QueueingHint
集成：真实 API 对象 -> scheduler -> Binding/Event
生产只读：对象/指标/日志时间线
```

可从小范围测试开始，例如：

```powershell
go test ./pkg/scheduler/framework/plugins/noderesources -run 'TestFit' -count=1
go test ./pkg/scheduler/framework/plugins/defaultpreemption -run 'TestPodEligibleToPreemptOthers' -count=1
go test ./pkg/scheduler -run 'Test.*Scheduling' -count=1
```

测试名会随源码变化。先用 `go test -list` 或 `rg '^func Test'` 核对当前提交，避免把“没有匹配到测试、退出成功”误当成真的验证通过。

---

## 25. 一页生产速查卡

### 25.1 成功链

```text
未绑定 Pod
-> SchedulingQueue
-> Pop / Done 生命周期
-> snapshot
-> PreFilter
-> 并行 Filter 节点
-> 0 个：FitError/PostFilter；1 个：直选；多个：Score
-> Assume scheduler cache
-> Reserve
-> Permit
-> 异步 binding cycle
-> WaitOnPermit / PreBind / Bind / PostBind
-> API 中 spec.nodeName
-> kubelet / CSI / Device Plugin / DRA / runtime
-> 容器与应用
```

### 25.2 失败链

```text
插件 Status
-> Diagnosis 保存节点失败与插件集合
-> FitError 或内部 Error
-> FailureHandler 核对最新 Pod 与 UID
-> Event + PodScheduled Condition
-> activeQ / backoffQ / unschedulablePods / gated
-> 有用 ClusterEvent + QueueingHint 唤醒
-> 新一轮必须重新验证全部硬条件
```

### 25.3 GPU 链

```text
设备插件发现设备
-> kubelet ListAndWatch
-> Node capacity/allocatable
-> scheduler 按扩展资源整数 request 选 Node
-> Assume 先占账
-> Bind
-> kubelet 选具体设备 ID
-> Device Plugin Allocate
-> runtime 注入
-> CUDA/应用验证
```

DRA 是另一条更丰富的设备声明/Claim/分配路径，不要把两条证据链混用。

### 25.4 十个禁止混淆

1. usage 不等于 request；
2. Capacity 不等于 Allocatable；
3. Allocatable 总和不等于单节点可行；
4. toleration 不等于必须去该节点；
5. preferred 不等于保证；
6. nominatedNodeName 不等于 nodeName；
7. Assume 不等于 API 已绑定；
8. Workload Admitted 不等于每个 Pod 已调度；
9. scheduler 选 Node 不等于 GPU 已分配、CUDA 已成功；
10. 进程存活不等于业务可调度容量健康。

### 25.5 事故现场七问

```text
1. 这是哪个 Pod UID，何时创建？
2. spec.nodeName 是否已经有值？
3. spec.schedulerName 谁负责？是否有 gate/上层准入？
4. PodScheduled Condition 与 Event 的时间线是什么？
5. 失败插件对应哪些硬约束集合？
6. request/allocatable/requested 与实际 usage 各是多少，是否混账？
7. 哪个事实变化才能让结果改变，QueueingHint 是否应当唤醒？
```

---

## 26. 自测题与答案：能讲清因果，才算真正理解

### 26.1 问题

1. 为什么 scheduler 不直接看 `kubectl top` 决定能否放 Pod？
2. 为什么有多个可行节点才需要 Score？
3. 一个节点 Filter 返回 Unschedulable 与返回 Error，对整轮调度有什么不同？
4. 为什么 Assume 要在 Bind 前？
5. Reserve 与 Assume 的状态所有者分别是谁？
6. Permit 返回 Wait 后，Pod 是否已经绑定？
7. 为什么 `nominatedNodeName` 不能当作最终落点？
8. 为什么抢占不能解决“任何节点最多 8 GPU、Pod 请求 10 GPU”？
9. toleration 为什么不能保证 GPU Pod 去 GPU 节点？
10. Device Plugin 路径中谁选择具体 GPU UUID？
11. 为什么默认 Filter 会检查 GPU，而默认 Score 未必按 GPU 剩余量排序？
12. MIG 和 time-slicing 都让资源数量看起来更多，它们的隔离语义为什么不同？
13. Kueue Workload 已 Admitted，为什么 Pod 仍可能 Pending？
14. `scheduler_pending_pods{queue="gated"}` 高，应该先查什么？
15. 为什么 Event 文本不适合作为唯一稳定自动化接口？
16. 绑定失败后为什么要 Forget assumed Pod，还要唤醒别的 Pod？
17. 集群总共剩 4 张 GPU，为什么 4-GPU Pod 仍可能放不下？
18. 为什么降低 request 可能把调度问题变成运行态事故？
19. profile 的 addedAffinity 为什么是平台治理风险？
20. 一条来源于 master 的结论，怎样安全用于旧生产版本？

### 26.2 答案

1. scheduler 负责按资源承诺做确定性准入，usage 是瞬时且波动的观测；二者是不同账本。
2. 只有一个可行节点时排名没有选择价值；多个节点才需要比较软偏好。
3. Unschedulable 通常只排除该节点并留下可解释诊断；内部 Error 可中止整轮并按 scheduler error 重试。
4. 异步 Bind 有延迟，Assume 先在 cache 占账，防止并发调度周期对同一余额重复承诺。
5. Assume 属于 scheduler 通用 cache；Reserve 属于各 Framework 插件的领域状态。
6. 没有。它只是在选点和预留后等待放行，Bind 尚未完成。
7. 它是抢占/期望落点提示，受害者、节点和约束都可能继续变化；最终看 `spec.nodeName`。
8. 删除其他 Pod 也不能让单节点物理/逻辑总量从 8 变成 10，属于 Unresolvable 的形状问题。
9. toleration 只取消某条 taint 的拒绝；还需 affinity/selector 或资源 request 共同限定目标池。
10. 目标节点的 kubelet DeviceManager 与设备插件协作选择并 Allocate；scheduler 传统路径只选 Node。
11. 扩展资源进入 NodeResourcesFit 的硬余额判断，但当前默认 scoring resources 主要是 CPU/memory；GPU 评分需显式设计或其他插件提供。
12. MIG 是硬件分区；time-slicing 是时间共享逻辑份额，显存和性能隔离承诺不同。
13. admission 是组织配额层，节点层仍受碎片、label、taint、卷、健康等约束。
14. 先查 schedulingGates、PreEnqueue/准入控制器和上层 Workload 状态，不要先扩 Node。
15. Event 会聚合、限流、过期，reason/message 还可能随版本改变；应结合结构化 Condition、对象、指标与日志。
16. Forget 释放错误的本地占账；其他 Pod 可能正因这笔 assumed 账被拒绝，因此还需事件让它们重评。
17. 四张可能分散在四台节点，或与 CPU/内存/标签条件不在同一台满足。
18. 更小 request 让 scheduler 承诺更多 Pod，真实峰值可能造成 CPU 争抢、延迟、驱逐和 OOM。
19. 它给 profile 中的 Pod 追加用户 YAML 看不见的硬/软亲和，容易造成“明明 YAML 匹配却 Pending”的隐性合同。
20. 先固定目标版本，找同一函数/配置/feature gate，运行对应测试或测试集群实验；找不到同构证据时标记为待验证，不能直接套用。

---

## 27. 官方资料、固定源码入口与继续学习顺序

### 27.1 Kubernetes 官方概念与配置

- [Scheduling Framework](https://kubernetes.io/docs/concepts/scheduling-eviction/scheduling-framework/)
- [Scheduler Configuration](https://kubernetes.io/docs/reference/scheduling/config/)
- [Assigning Pods to Nodes](https://kubernetes.io/docs/concepts/scheduling-eviction/assign-pod-node/)
- [Pod Priority and Preemption](https://kubernetes.io/docs/concepts/scheduling-eviction/pod-priority-preemption/)
- [Pod Topology Spread Constraints](https://kubernetes.io/docs/concepts/scheduling-eviction/topology-spread-constraints/)
- [Device Plugins](https://kubernetes.io/docs/concepts/extend-kubernetes/compute-storage-net/device-plugins/)
- [Dynamic Resource Allocation](https://kubernetes.io/docs/concepts/scheduling-eviction/dynamic-resource-allocation/)
- [Storage Classes / WaitForFirstConsumer](https://kubernetes.io/docs/concepts/storage/storage-classes/)

### 27.2 队列与 GPU 官方资料

- [Kueue Concepts](https://kueue.sigs.k8s.io/docs/concepts/)
- [Kueue ClusterQueue](https://kueue.sigs.k8s.io/docs/concepts/cluster_queue/)
- [Kueue Workload](https://kueue.sigs.k8s.io/docs/concepts/workload/)
- [Kueue WorkloadPriorityClass](https://kueue.sigs.k8s.io/docs/concepts/workload_priority_class/)
- [Kueue Topology-Aware Scheduling](https://kueue.sigs.k8s.io/docs/concepts/topology_aware_scheduling/)
- [Volcano Gang Plugin](https://volcano.sh/docs/scheduler/plugins/gang/)
- [Volcano Scheduler Overview](https://volcano.sh/docs/scheduler/overview/)
- [NVIDIA GPU Operator](https://docs.nvidia.com/datacenter/cloud-native/gpu-operator/latest/index.html)
- [NVIDIA GPU Sharing / Time-Slicing](https://docs.nvidia.com/datacenter/cloud-native/gpu-operator/latest/gpu-sharing.html)
- [NVIDIA MIG in Kubernetes](https://docs.nvidia.com/datacenter/cloud-native/kubernetes/latest/index.html)

### 27.3 本文固定提交的源码入口

- [`pkg/scheduler/schedule_one.go`](https://github.com/kubernetes/kubernetes/blob/301946d15e67a4a2e8a5fb8292eb836acd366d78/pkg/scheduler/schedule_one.go)
- [`pkg/scheduler/framework/runtime/framework.go`](https://github.com/kubernetes/kubernetes/blob/301946d15e67a4a2e8a5fb8292eb836acd366d78/pkg/scheduler/framework/runtime/framework.go)
- [`pkg/scheduler/backend/queue/scheduling_queue.go`](https://github.com/kubernetes/kubernetes/blob/301946d15e67a4a2e8a5fb8292eb836acd366d78/pkg/scheduler/backend/queue/scheduling_queue.go)
- [`pkg/scheduler/framework/plugins/noderesources/fit.go`](https://github.com/kubernetes/kubernetes/blob/301946d15e67a4a2e8a5fb8292eb836acd366d78/pkg/scheduler/framework/plugins/noderesources/fit.go)
- [`pkg/scheduler/framework/plugins/defaultpreemption/default_preemption.go`](https://github.com/kubernetes/kubernetes/blob/301946d15e67a4a2e8a5fb8292eb836acd366d78/pkg/scheduler/framework/plugins/defaultpreemption/default_preemption.go)
- [`pkg/scheduler/framework/plugins/dynamicresources/dynamicresources.go`](https://github.com/kubernetes/kubernetes/blob/301946d15e67a4a2e8a5fb8292eb836acd366d78/pkg/scheduler/framework/plugins/dynamicresources/dynamicresources.go)
- [`pkg/kubelet/cm/devicemanager/manager.go`](https://github.com/kubernetes/kubernetes/blob/301946d15e67a4a2e8a5fb8292eb836acd366d78/pkg/kubelet/cm/devicemanager/manager.go)
- [`pkg/scheduler/metrics/metrics.go`](https://github.com/kubernetes/kubernetes/blob/301946d15e67a4a2e8a5fb8292eb836acd366d78/pkg/scheduler/metrics/metrics.go)

### 27.4 推荐继续学习顺序

```text
第一轮：手算第 3 节案例 + 画第 4 节全景图
第二轮：只读 schedule_one.go 成功链
第三轮：做实验一到四，能解释每个 Status
第四轮：读 NodeResourcesFit 与 NodeAffinity
第五轮：接入 GPU 设备供给链，做传统 Device Plugin 对账
第六轮：学习 Kueue、MIG/time-slicing 与容量碎片
第七轮：再进入 DRA、抢占和自定义插件
```

最后把整篇压成一句话：

> kube-scheduler 是一个基于缓存、插件化规则和乐观预占的节点决策控制器。它把一个尚未绑定的 Pod 与当前集群事实做硬约束交集，在可行节点中计算软偏好，然后先在内存账本占位、再通过 API 持久化 Node；业务平台负责把意图变成可治理合同，GPU 供给链负责把设备事实上报并在节点兑现，队列系统负责决定整项工作何时入场。排障的关键不是背 Event，而是先找状态所有者，再沿事实传播链验证哪本账、哪个约束、哪个时间点出了问题。
