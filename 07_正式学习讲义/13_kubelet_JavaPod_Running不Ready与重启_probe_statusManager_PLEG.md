# 第 13 课：`game-api` JVM 还活着，为什么先摘流量而不是立刻重启——probe、PLEG 与 statusManager 的三本账

> 延续第 11～12 课同一个 `prod/game-api-new-x`：Java 进程已经启动，但“进程活着”不等于“现在适合接流量”，更不等于“出了问题就该重启”。本课就把这三个判断拆开讲清楚。

第 12 课已经把同一个 Pod 推进到：

```text
镜像凭据修复
  -> game-api CreateContainer / StartContainer 成功
  -> JVM 进程出现
  -> startup probe（容器进程启动后，专门给慢启动应用留时间并判断它是否启动完成）才获得执行资格
```

先说一个你值班时很容易碰到的场景：数据库短暂抖动，Spring Boot 的就绪接口返回 503，可 JVM 进程本身没有死。如果 Kubernetes 此时直接重启 JVM，不但修不好数据库，反而会让更多 Pod一起冷启动，把小故障放大成发布事故。

所以“Java 进程还活着，为什么先摘流量而不是立刻重启”不是一句孤零零的规则，而是 Kubernetes 必须把下面五个问题分开回答：

```text
进程是否存在？
应用是否完成启动？
现在是否应该接流量？
当前故障是否值得重启？
apiserver 是否已经看到节点的新结论？
```

先用大白话记住结论：

1. **readiness（就绪探针）失败：先把 Pod从接流量名单里拿掉，不重启进程。**
2. **liveness（存活探针）失败：只是报告“这个进程可能要重启”，不会由探针线程自己直接杀进程。**
3. **真正决定杀不杀、杀完要不要再启动的，是 kubelet统一的 Pod同步逻辑。**它还要一起考虑 Pod期望状态、容器当前状态和重启策略。
4. **PLEG 负责从容器运行时核实“进程真的启动或退出了吗”；statusManager 再把运行事实和 probe结论整理成 PodStatus，异步写回 API Server。**因此节点已经处理完、`kubectl` 还没看到最新状态，短时间内是可能的；readiness变化也不需要先等PLEG产生一次新事件。

这里第一次出现的词，先翻成人话：

| 词 | 本课里的大白话 | 它负责什么，不负责什么 |
| --- | --- | --- |
| probe | kubelet定期做的“健康检查” | 产生检查结果，不直接完成整套重启 |
| startup | “应用启动完了吗” | 没通过前，先不让 readiness/liveness干活 |
| readiness | “现在适合接新流量吗” | 改流量资格，不负责杀容器 |
| liveness | “进程是不是已经坏到需要重启” | 提交失败结论，最终动作由统一同步逻辑决定 |
| runtime / CRI | 容器运行时，以及 kubelet调用它的标准接口 | 真正创建、启动、停止容器；不懂 Java业务是否健康 |
| result cache | kubelet内存里保存的“已经达到连续次数要求的稳定检查结论” | 不是每一次 HTTP检查的原始记录 |
| threshold | 连续成功或失败多少次，才允许改变稳定结论 | 防止一次网络抖动就摘流量或重启 |
| worker / goroutine | kubelet里长期干某一类小工作的任务；goroutine是Go启动这种并发任务的轻量方式 | 每种probe各自工作，不能把它理解成一个操作系统进程 |
| desired / actual | Pod规格里“希望变成什么样” / runtime里“现在实际上什么样” | kubelet要比较两边再决定动作 |
| PLEG | kubelet里的“容器现场巡查员” | 观察容器实际启动、退出；不判断数据库或 JVM业务健康 |
| statusManager | kubelet里的“状态上报员” | 先记节点本地状态，再异步写 API Server |
| fast-path / early return | 先走一条更快的小路 / 条件不满足就提前结束当前函数 | 都只是控制流程，不等于最终状态已经成功写到API |
| Patch | 只把PodStatus发生变化的部分写回API Server | 写失败会重试，不会让节点主控制链一直卡住 |

标题里的“**三本账**”现在也可以直接解释了：probe result cache 记“应用检查的稳定结论”，PLEG的 pod cache记“容器运行现场”，statusManager记“准备上报给API的 Pod状态”。它们不能合成一张表，因为三种信息的产生速度、身份标识和失败方式都不一样；也不能死记成固定串联顺序：readiness可以直接推动状态重算，进程自行退出则先由PLEG发现，liveness重启则要执行动作后再由PLEG核实结果。

本章后面写的 Kubernetes `Event`，是 `kubectl get events` 能看到的诊断记录；它不是控制器通过 Watch/Informer 收到的“API对象变了”通知，也不是 kubelet内部通过 channel（Go进程内的消息通道）传递的 PLEG 唤醒消息。它们名字都带 event/update，但不是同一本账。

> **表格读法：** 本章表格都先从上往下选一行，再在这一行里从左往右读“现象/对象 → 谁负责 → 作用或边界”。不要把同一列从上到下拼成一条调用链。

> **流程图读法：** 本章带 `->` 的文字图默认从上往下读，缩进表示上一步内部的子步骤；箭头表示后一步获得处理机会，不承诺同一毫秒完成，也不一定代表一次同步RPC调用。

先不要执行命令。带着六个问题读本章：

1. `state.running != nil`、`started=true`、`ready=true` 分别能证明什么？
2. 第一条 `Unhealthy` Event 为什么不等于 threshold 已达到？
3. readiness Failure 为什么应该先摘流量，而不是杀掉 Java 进程？
4. liveness worker 为什么不能直接调用 CRI `StopContainer`？
5. PLEG `Healthy=true` 为什么仍不能证明当前 Pod 的详细 status 已刷新？
6. liveness 重启后，为什么 Pod UID、Pod IP、Sandbox 和 `jmx-exporter` 都可以不变？

## 0. 本课定位、深度和两遍阅读路线

这是 Java 平台主线的收口课。这里的 **S2** 是“能沿关键函数找到状态怎么传”，**S3** 是“能继续追到异常分支，并用源码解释生产现象”。整体读到 S2，下面几条关键链再读到 S3：

- probe 实际执行、threshold 与 result cache；
- readiness fast-path 与正常 `SyncPod` 汇合；
- liveness Failure 怎样进入 `computePodActions -> killContainer`；
- GenericPLEG 怎样更新 podCache、处理 Event 丢弃与健康检查；
- statusManager 怎样去重、本地版本化并异步 Patch API。

本课不会重新讲探针 YAML 的基础用法，也不会展开：

- HTTP/TCP/exec/gRPC 健康检查到底怎样发请求、执行命令的全部底层实现；
- EndpointSlice controller（维护 Service后端地址名单的控制器）的完整队列与反复对账逻辑；
- EventedPLEG（通过事件流观察容器变化的新路径）的全部连接恢复、时间先后冲突和降级实现；
- container GC 怎样保存或丢失所有历史退出状态；
- Driver、CUDA、Device Plugin、DeviceManager 与 GPU UUID 分配；
- 第 14 课以后的 NVIDIA 节点栈。

建议分两遍，不要第一次就从头硬啃到尾：

- **首遍抓主线：** [Java现场](#ch13-case) → [三本账](#ch13-books) → [总图](#ch13-map)（顺着读到[§5.6核心源码](#ch13-core-source)） → [回到Java现场](#ch13-java-loop) → [值班决策表](#ch13-runbook) → [首遍验收](#ch13-first-check)。一共六站，第一次可以跳过其余 Go 代码。读完要能讲清：readiness为什么只摘流量、liveness为什么要回到统一同步、PLEG和API状态为什么可能慢半拍。
- **二遍补边界：** [精读第6～11节源码](#ch13-source-deep) → [反事实](#ch13-counterfactual) → [生产取证](#ch13-evidence) → [深度边界](#ch13-depth) → [二遍验收](#ch13-second-check) → [语法/测试/版本查表](#ch13-go-index)。重点看阈值、新旧 container ID、重启策略、Event丢失和API写入失败。

两遍的合格线也分开：

| 阅读遍次 | 读完后能做到什么 |
| --- | --- |
| 首遍 | 不看代码，也能用同一个 Java事故说清“摘流量”和“重启”为什么是两件事 |
| 二遍 | 能指出关键判断在哪个函数，并能解释一条异常证据为什么不足以下结论 |

## 1. 当前源码基线与阅读约定

```text
源码目录：D:\datou\devops\kubernetes-master\kubernetes
commit：301946d15e67a4a2e8a5fb8292eb836acd366d78
describe：v1.37.0-alpha.0-280-g301946d15e6
源码分支：master
源码 go.mod / go.work：go 1.26.0
本机 Go：go1.19.4 windows/amd64
```

本机 Go 低于当前源码要求。本课完成固定提交静态源码核对、现有测试代码核对、三路独立审校和讲义机械校验；定向 `go test` 的真实执行结果见第 22 节，不能写成“相关测试已在本机通过”。生产排障必须切换到目标集群对应的 Kubernetes、CRI、kube-proxy/数据面与应用版本，重新核对 feature gate（功能开关）、Event文本、指标稳定性和函数行号。

主文件：

```text
kubernetes/pkg/kubelet/prober/prober_manager.go
kubernetes/pkg/kubelet/prober/worker.go
kubernetes/pkg/kubelet/prober/prober.go
kubernetes/pkg/kubelet/prober/results/results_manager.go
kubernetes/pkg/kubelet/kubelet.go
kubernetes/pkg/kubelet/kubelet_pods.go
kubernetes/pkg/kubelet/status/generate.go
kubernetes/pkg/kubelet/status/status_manager.go
kubernetes/pkg/kubelet/kuberuntime/kuberuntime_manager.go
kubernetes/pkg/kubelet/kuberuntime/kuberuntime_container.go
kubernetes/pkg/kubelet/pleg/generic.go
kubernetes/pkg/kubelet/container/cache.go
kubernetes/staging/src/k8s.io/endpointslice/utils.go
```

> **源码阅读约定：** 标有“教学注释版”的 Go 代码，控制流、变量名、判断顺序和返回关系来自本课固定提交；中文 `//` 是讲义新增，不是 Kubernetes 上游原注释。每条影响控制或业务语义的语句都会就地解释，多行调用只解释一次，单独括号不机械标注。每个代码块会说明是完整函数、连续摘录还是非连续检查点；不会用孤立省略号冒充被删除的源码。不能独立编译的摘录会明确说明。

<a id="ch13-case"></a>

## 2. 先不执行命令：固定同一个 Java 发布现场

以下是为了教学整理的脱敏现场，不是生产原始证据。namespace、Pod、UID、Node、Pod IP、Sandbox 和 sidecar（辅助容器）都沿用第 11～12 课。

### 2.1 Deployment 与 Pod 身份不变

```text
Deployment: prod/game-api
replicas: 3
maxSurge: 1
maxUnavailable: 0

Service: prod/game-api
publishNotReadyAddresses: false（默认值；主案按常规Ready条件发布EndpointSlice）

Pod:       prod/game-api-new-x
UID:       3f5f4a90-1111-2222-3333-444444444444
Node:      worker-05
PodIP:     192.0.2.41（教学文档地址）
Sandbox:   attempt=0 / READY

jmx-exporter:
  containerID = containerd://jmx-001
  state       = Running
  ready       = true
```

第 12 课的 registry credential 已修复，`game-api` 镜像、Create 与 Start 都已成功。新 Java container 的第一代 ID 记为：

```text
containerd://game-001
```

### 2.2 第 12 课只展示了 startup，本课补全同一份 probe spec

```yaml
spec:
  restartPolicy: Always
  containers:
  - name: game-api
    image: registry.example.com/game/game-api:2026.07.18-1
    env:
    - name: JAVA_TOOL_OPTIONS
      value: -Xms1g -Xmx1536m
    startupProbe:
      httpGet:
        path: /actuator/health/startup
        port: 8080
      timeoutSeconds: 2
      periodSeconds: 5
      failureThreshold: 24
    readinessProbe:
      httpGet:
        path: /actuator/health/readiness
        port: 8080
      timeoutSeconds: 2
      periodSeconds: 5
      failureThreshold: 2
      successThreshold: 2
    livenessProbe:
      httpGet:
        path: /actuator/health/liveness
        port: 8080
      timeoutSeconds: 2
      periodSeconds: 10
      failureThreshold: 3
  - name: jmx-exporter
    image: registry.example.com/ops/jmx-exporter:1.0
```

参与推理的 Java 事实：

- Spring Boot 类加载、连接池建立、缓存加载与 JIT 预热约 45 秒；
- startup 的配置预算通常被口头估算成 `5 × 24 = 120 秒`，但它不是严格墙钟 SLA（承诺的完成时限）：worker 调度、单次 probe 耗时、kubelet 重启抖动和 readiness 手动触发都会影响实际时刻；
- readiness group 包含数据库连接池，因此数据库临时不可用时返回 HTTP 503；
- liveness group 不包含数据库，只检查 JVM 是否仍能推进关键内部心跳；
- 数据库故障是可逆依赖故障，杀 JVM 会扩大问题；
- 后续独立发生一次 JVM 线程死锁，liveness endpoint 连续超时；
- `jmx-exporter` 始终健康，它不应跟随 `game-api` 的局部重启被删除。

### 2.3 四个固定时刻

#### 时刻 A：CRI 已启动进程，但 startup 尚未成功

```text
Pod UID                         = 3f5f4a90-1111-2222-3333-444444444444
game-api containerID            = containerd://game-001
game-api state.running          = true
game-api started                = false
game-api ready                  = false
game-api restartCount           = 0
Pod phase                       = Running
Pod Ready                       = False
jmx-exporter containerID        = containerd://jmx-001
```

这里最容易误读：CRI `StartContainer` 已成功、进程已经 Running，但 API `started=false`。本章会证明，`ContainerStatus.started` 表达的是 startup probe 语义，不是 CRI RPC 名字的直接回显。

#### 时刻 B：startup/readiness 成功，Pod 已具备常规 Service接流量资格

```text
game-api ID                     = containerd://game-001
state.running                   = true
started                         = true
ready                           = true
restartCount                    = 0
Pod Ready                       = True
EndpointSlice conditions        = ready=true / serving=true / terminating=false
```

新 Pod 成为 Ready 后，Deployment 可以继续推进。`maxUnavailable=0` 约束的是 rollout 动作，不是一个永久可用性保险；已经投入服务的 Pod 后续变 NotReady，Deployment 不会因为这个参数自动恢复旧 Pod。

#### 时刻 C：数据库故障，readiness 达阈值

```text
第一次 readiness 503：
  Unhealthy Event 可以已经出现
  稳定 readiness cache 仍可能是 Success
  API Ready 可能暂时仍是 True

连续第二次普通 Failure，达到 failureThreshold=2 后：
  game-api ID                  = containerd://game-001（不变）
  state.running                = true
  started                      = true
  ready                        = false
  restartCount                 = 0
  Pod Ready                    = False
  EndpointSlice                = ready=false / serving=false / terminating=false
  Sandbox / PodIP / jmx ID     = 不变
```

#### 时刻 D：数据库已恢复，随后 JVM 独立死锁

数据库恢复并连续达到 `successThreshold=2` 后，Pod 再次 Ready。稍后 JVM 死锁使 liveness 连续失败：

```text
第一、第二次 liveness Failure：
  Unhealthy Event 可以出现或被聚合
  liveness stable result 尚未变 Failure
  container ID / restartCount 仍可不变

第三次普通 Failure，达到 failureThreshold=3 后：
  liveness result cache = Failure
  -> syncLoop 请求同一 Pod 重新对账
  -> runtime manager 计划 kill game-001，并按 Always 计划重启
  -> Killing Event 说明已进入 kill 路径
  -> CRI StopContainer 成功后旧 ID 退出
  -> 新 ID containerd://game-002 启动
  -> PLEG 观察实际变化并刷新 podCache
  -> API 最终看到 restartCount=1、lastState 指向旧实例（受 runtime 保留边界约束）

始终不变：
  Pod UID / Pod IP / Sandbox / jmx-exporter ID
```

这些时间点是教学排序，不是用操作者时钟给 Node、apiserver 和 Prometheus 做毫秒级对齐；各系统时钟与异步传播都可能产生偏差。

### 2.4 先写下你的预测

1. 时刻 A 为什么 `phase=Running`，但 `started=false`？
2. 时刻 C 第一条 Event 出现后，哪些字段仍可能没有变化？
3. readiness 达阈值后，`game-api` 的 container ID 是否应该改变？
4. liveness 达阈值后，probe worker 是否应该直接调用 CRI？
5. `Killing` Event 能不能证明 `StopContainer` 已成功？
6. PLEG `Healthy=true` 能不能证明 `game-api` 的 `GetPodStatus` 刚刚成功？
7. API 已显示新 ID 后，为什么 `lastState` 与 `restartCount` 仍只能视为 best-effort（尽力汇总、但不保证永久完整）历史？

## 3. Kubernetes 在这里解决的不是“定时 curl”，而是五种事实不能混成一张表

### 3.1 错误方案一：进程 Running 就等于可以接流量

JVM 可以存在，却仍在加载 Spring context；也可以存活但数据库连接池不可用。若 `Running` 自动等于 Ready：

- 冷启动请求会打进尚未完成预热的实例；
- 可逆依赖故障无法只摘流量；
- phase 与流量状态被迫绑定；
- 控制器和数据面无法区分“进程存在”与“业务可服务”。

Kubernetes 因此把 runtime state、`Started`、container `Ready`、Pod `Ready` 分开。

### 3.2 错误方案二：任意 probe Failure 都立即杀容器

单次网络抖动、一次 GC pause 或一次数据库切换都可能产生 Failure。立即 kill 会：

- 把瞬时故障放大成重启风暴；
- 让 readiness 失去“可逆摘流量”的价值；
- 让 GPU/大模型冷启动重复消耗更昂贵；
- 把 Event 观察误当成稳定控制结论。

worker 先累计连续结果，达到 threshold 后才更新稳定 result cache。

### 3.3 错误方案三：probe worker直接调用 CRI `StopContainer`

probe worker只知道一次应用探测结果，不拥有完整 desired/actual：

- Pod 可能正在删除；
- spec 可能已经变化；
- restart policy 可能是 Never、OnFailure 或 container-level rule；
- runtime 中的 container ID 可能已经换代；
- 同一个 Pod 还有健康 Sandbox 和 `jmx-exporter` 要保留；
- kill 失败后不能继续假装已经启动新实例。

所以 worker发布结果并唤醒 `SyncPod`，由 runtime manager统一计算 kill/start action。

### 3.4 错误方案四：Event 就是当前状态

Event 是 best-effort 诊断：可能聚合、限流、丢失或过期。更关键的是，普通 Failure Event 在 threshold 之前就可以记录；`probe errored` 甚至不会进入 threshold 计数。Event 只能说明 recorder 观察过某个分支，不能代替 result cache、CRI state 或 API condition。

### 3.5 错误方案五：让 PLEG 同时判断 Java 业务健康

PLEG 观察的是 runtime container 状态变化。它不知道数据库连接池、Spring readiness group、JVM deadlock 心跳、GPU Xid（驱动报告的GPU错误码）或业务 P99（99%请求都不超过的延迟线）。让它判断业务健康会把 CRI 责任域与应用语义耦合，也无法替代 HTTP/exec/gRPC probe。

### 3.6 错误方案六：每次本地状态变化都同步等待 apiserver

若 kubelet每改一次 container Ready 都阻塞等待 API：

- API 抖动会直接阻塞节点执行主链；
- 多次快速状态变化会制造重复 Patch；
- 同名新 Pod 的 UID 竞争更难防；
- 大量 Pod 会把本地状态收敛与控制面可用性绑死。

statusManager 因此先更新本地版本化 cache，再由单 goroutine即时/周期同步 API。

<a id="ch13-books"></a>

## 4. 状态所有者、三本本地账和十一条不变量

### 4.1 谁拥有哪份事实

| 事实 | 主要所有者 | key / 内容 | 本章用途 |
| --- | --- | --- | --- |
| probe 配置与 restart policy | PodSpec / apiserver | probe type、period、threshold、policy | desired 输入 |
| probe worker 身份 | probe manager | Pod UID + container name + probe type | 每类 probe 独立运行 |
| 稳定 probe 结果 | 三个 result manager | container ID -> Success/Failure/Unknown | readiness、kill action 输入 |
| runtime actual state | CRI runtime | Sandbox/container ID、Running/Exited、时间、exit code | 进程真实状态 |
| runtime podCache | Generic/Evented PLEG | Pod UID -> `kubecontainer.PodStatus` + 时间/error | pod worker 的 actual 输入 |
| 准备上报给API的本地状态 | statusManager | Pod UID -> `v1.PodStatus` + local version | Ready/Started/lastState 等 |
| API PodStatus | apiserver | 最近成功 Patch 的状态 | controller、kubectl、EndpointSlice 输入 |
| Service backend 条件 | EndpointSlice controller | endpoint ready/serving/terminating | 数据面资格 |

三本最容易混淆的本地账是：

```text
probe result cache
runtime podCache（PLEG）
statusManager PodStatus cache
```

### 4.2 十一条设计不变量

1. **CRI Running 只证明 runtime 进程状态，不证明 startup 或 readiness。**
2. **有 startup probe 时，startup 未成功前 readiness/liveness 不执行真实探测。**
3. **普通 Failure Event 可以早于 threshold；只有稳定 result cache 变化才驱动后续链。**
4. **prober执行 error 被 worker丢弃，不进入本轮 threshold；一次 worker探测最多完成固定次数的底层尝试后才返回。**
5. **readiness Failure 只能改变流量资格，不是 runtime kill 原因。**
6. **liveness/startup worker不直接 kill；runtime manager结合 actual 与 restart policy 计算 action。**
7. **probe result 以 container ID 为 key；新进程不能继承旧 ID 的健康结论。**
8. **健康 Sandbox、Pod IP 和其他普通 container 不因 `game-api` 局部重启自动回滚。**
9. **PLEG 先尝试更新 podCache，再推进 podRecord 与投递 lifecycle Event；Event 仍可能被丢弃。**
10. **PLEG Healthy 只约束最近全局 `GetPods` 时间，不保证每个 Pod 详细 status 已成功刷新。**
11. **statusManager 本地状态、API PodStatus、EndpointSlice 与数据面允许短暂不同步。**

### 4.3 收益与代价

| 设计选择 | 得到什么 | 付出什么 |
| --- | --- | --- |
| 进程/启动/流量/重启分层 | 可逆摘流量、慢启动保护、独立重启 | 状态组合更多 |
| threshold + 稳定 cache | 抗瞬时抖动、减少 sync storm | Event 与最终动作存在时间差 |
| worker只发布事实 | 统一 restart policy、删除竞态和多容器处理 | 需要再走 Pod worker/SyncPod |
| PLEG 观察 runtime | 能发现主动退出、OOM、外部停止 | cache、Event 与健康有不同边界 |
| status 异步写 API | 节点不被 API Patch阻塞、可批量重试 | kubectl 与本地事实可能暂时不同 |

<a id="ch13-map"></a>

## 5. 白板总图：两条控制链、一条 runtime 观察链、一个 API 反馈链

> **下面四张图都从上往下读。** `A -> B` 表示 A 发生后，B 才获得继续处理的机会；它表示因果方向，不保证两个组件在同一毫秒完成，也不表示它们之间一定是一次同步 RPC调用。

### 5.1 readiness：改变流量资格

```text
readiness worker
  -> 普通 Failure达到threshold
  -> readiness result cache=Failure
  -> syncLoop readiness分支
     -> SetContainerReadiness(false) fast-path（可能early return）
     -> HandlePodSyncs
  -> pod worker / Kubelet.SyncPod
  -> generateAPIPodStatus再次读取probe cache
  -> statusManager本地版本
  -> PatchPodStatus
  -> EndpointSlice controller
  -> endpoint ready/serving=false

不会进入：ContainersToKill
```

### 5.2 liveness/startup：请求重新计算是否重启

```text
liveness/startup worker
  -> Failure达到threshold
  -> 对应 result cache=Failure
  -> syncLoop只唤醒同一Pod同步
  -> pod worker读取PLEG podCache
  -> Kubelet.SyncPod
  -> runtimeManager.computePodActions
  -> ContainersToKill[旧ID]
  -> restart policy允许时 ContainersToStart[index]
  -> Killing Event
  -> CRI StopContainer
  -> startContainer新ID
```

### 5.3 runtime 自己先变化：不需要 probe

```text
Java System.exit / OOM / runtime外部停止
  -> runtime actual state先变化
  -> PLEG Relist或EventedPLEG观察
  -> 更新podCache
  -> lifecycle Event唤醒syncLoop（可能丢，仍有周期/其他更新）
  -> runtime manager按restart policy收敛
```

### 5.4 API 反馈链

```text
probe cache + PLEG podCache
  -> generateAPIPodStatus
  -> statusManager local version
  -> 不携带完整状态的非阻塞“门铃”提醒
  -> 单goroutine syncBatch
  -> GET当前Pod并核对UID
  -> PatchPodStatus
  -> controller / kubectl / EndpointSlice继续收敛
```

### 5.5 本章停止线

```text
主读：worker门控/threshold、readiness/liveness分流、PLEG cache/Event、status API回传
旁读：restartPolicy Never、kubelet restart兼容、PLEG unhealthy、status metric边界
下一章：NVIDIA Driver/CUDA/Toolkit/containerd/CDI
```

<a id="ch13-core-source"></a>

### 5.6 第一眼先看核心源码：三种 probe 的结果，走的根本不是同一条路

不要先钻进 worker、缓存和 channel。先看 kubelet收到“稳定探测结果”以后做什么，这段代码已经把本课最重要的职责分工写出来了。

源码：`pkg/kubelet/kubelet.go:2758-2779`，`syncLoopIteration` 中三个相邻 `case` 的**连续摘录**。它们处在同一个 `select` 中；这里只展示 probe相关分支，代码块不能独立编译。

```go
case update := <-kl.livenessManager.Updates(): // 收到“存活检查的稳定结果变化”。
	if update.Result == proberesults.Failure { // 只有稳定失败，才请求重新同步这个Pod。
		handleProbeSync(ctx, kl, update, handler, "liveness", "unhealthy") // 这里只是交回统一Pod同步，不在这里调用CRI杀容器。
	}
case update := <-kl.readinessManager.Updates(): // 收到“是否能接流量”的稳定结果变化。
	ready := update.Result == proberesults.Success // Success翻成true；其他结果翻成false。
	kl.statusManager.SetContainerReadiness(logger, update.PodUID, update.ContainerID, ready) // 先尝试更新节点本地就绪状态；找不到当前UID/ID时允许提前返回。

	status := "not ready" // 下面几行只是在准备日志文字。
	if ready { // 如果稳定结果是成功，
		status = "ready" // 日志就写ready。
	}
	handleProbeSync(ctx, kl, update, handler, "readiness", status) // 再让统一Pod同步做一次完整对账；这里仍不杀容器。
case update := <-kl.startupManager.Updates(): // 收到“应用是否启动完成”的稳定结果变化。
	started := update.Result == proberesults.Success // Success翻成true；其他结果翻成API里的started=false。
	kl.statusManager.SetContainerStartup(logger, update.PodUID, update.ContainerID, started) // 先尝试更新节点本地启动状态；它也有身份与基线检查。

	status := "unhealthy" // 默认日志文字表示启动检查未通过。
	if started { // 如果启动检查成功，
		status = "started" // 日志改成started。
	}
	handleProbeSync(ctx, kl, update, handler, "startup", status) // 最后同样回到统一Pod同步。
```

**大白话总结：** readiness先尝试改“能不能接流量”；liveness只有在稳定失败时才敲门让 Pod重新对账；startup维护“应用是否启动完成”。readiness/startup结果变化与 liveness Failure 都会唤醒统一同步，但入口动作并不一样。最关键的是：这段代码里没有任何 `StopContainer`，所以“探针失败”与“容器已经被杀”之间还隔着一次完整的动作计算。

**顺手学 Go：** `case update := <-channel` 表示从 channel（可以理解成 Go里的消息通道）取出一条更新；`:=` 是声明并赋值。这里的 `select` 会等待多个消息来源，多个来源同时有消息时不承诺固定先后顺序。

<a id="ch13-source-deep"></a>

## 6. 第一层源码：为什么一个 container 要有三个 worker，而不是一个“健康线程”

### 6.1 `AddPod` 怎样按 probe type 建独立 worker

源码：`pkg/kubelet/prober/prober_manager.go:185-230`，`manager.AddPod` **完整函数，教学注释版**。当前函数也覆盖 restartable init container；普通一次性 init 不进入这组三类长期 worker。

```go
func (m *manager) AddPod(ctx context.Context, pod *v1.Pod) {
	// workers map 是共享状态；创建和查重期间持有写锁。
	m.workerLock.Lock()
	defer m.workerLock.Unlock()

	logger := klog.FromContext(ctx) // 日志继承本轮 SyncPod 上下文。
	key := probeKey{podUID: pod.UID} // key 先固定 Pod UID，后面补 container 与 probe type。
	// 普通 containers 与 restartable init containers 都可能拥有长期 probe。
	for _, c := range append(pod.Spec.Containers, getRestartableInitContainers(pod)...) {
		key.containerName = c.Name // 同一个 Pod 内再按 container name 区分。

		// startupProbe 存在才创建 startup worker。
		if c.StartupProbe != nil {
			key.probeType = startup
			if _, ok := m.workers[key]; ok {
				// 任一同 key worker 已存在时直接结束本次 AddPod，防止重复 goroutine。
				logger.V(8).Info("Startup probe already exists for container",
					"pod", klog.KObj(pod), "containerName", c.Name)
				return
			}
			w := newWorker(m, startup, pod, c) // 组装 startup 专属 spec/result manager。
			m.workers[key] = w                 // 先登记，避免并发重复创建。
			go w.run(ctx)                      // goroutine 周期执行，不阻塞 AddPod。
		}

		// readiness 使用独立 key、worker 与 result cache。
		if c.ReadinessProbe != nil {
			key.probeType = readiness
			if _, ok := m.workers[key]; ok {
				logger.V(8).Info("Readiness probe already exists for container",
					"pod", klog.KObj(pod), "containerName", c.Name)
				return
			}
			w := newWorker(m, readiness, pod, c)
			m.workers[key] = w
			go w.run(ctx)
		}

		// liveness 同样不是复用 readiness worker。
		if c.LivenessProbe != nil {
			key.probeType = liveness
			if _, ok := m.workers[key]; ok {
				logger.V(8).Info("Liveness probe already exists for container",
					"pod", klog.KObj(pod), "containerName", c.Name)
				return
			}
			w := newWorker(m, liveness, pod, c)
			m.workers[key] = w
			go w.run(ctx)
		}
	}
}
```

**大白话总结：** `game-api` 同时配置三种 probe 时，kubelet创建三个独立 goroutine。worker key 是 Pod UID、container name 和 probe type，不是只按 Pod 名。重复 `SyncPod` 不应不断创建新 worker；查重分支直接结束本次 AddPod。

**顺手学 Go：** `defer m.workerLock.Unlock()` 表示函数任意返回路径都会解锁。`go w.run(ctx)` 启动 goroutine，调用方不等待长期循环结束。`append(sliceA, sliceB...)` 末尾三个点是真实 variadic 展开，把第二个 slice 的元素逐个追加，不是讲义省略号。

### 6.2 三种初始值为什么故意不同

源码：`pkg/kubelet/prober/worker.go:112-125`，`newWorker` 的 **连续摘录**。worker 基础字段已在上方初始化，后面的 metrics labels 与本段结论无关，因此不展示；代码块不可独立编译。

```go
// probeType 决定读取哪份 spec、写哪本 result cache，以及新实例的初始结论。
switch probeType {
case readiness:
	w.spec = container.ReadinessProbe          // 使用 readinessProbe 配置。
	w.resultsManager = m.readinessManager      // 写 readiness 专属 cache。
	w.initialValue = results.Failure           // 未证明能接流量前，先视为不 Ready。
case liveness:
	w.spec = container.LivenessProbe           // 使用 livenessProbe 配置。
	w.resultsManager = m.livenessManager       // 写 liveness 专属 cache。
	w.initialValue = results.Success           // 未证明坏死前，不先误杀。
case startup:
	w.spec = container.StartupProbe            // 使用 startupProbe 配置。
	w.resultsManager = m.startupManager         // 写 startup 专属 cache。
	w.initialValue = results.Unknown           // 新进程尚未完成启动判断。
}
```

**大白话总结：** 初始值体现安全偏好：readiness 偏保守流量，liveness 偏保守重启，startup 明确未知。它们是新 container ID 的 probe cache 初值，不是三个 API Condition，也不保证 kubelet重启接管旧实例时无条件重种，相关例外见 6.4。

**顺手学 Go：** Go 的 `switch` 默认命中一个 case 后停止，不像旧式 C/Java switch 自动贯穿；不需要每个 case 写 `break`。`w` 是 `*worker` 指针，点号会自动解引用 receiver 指向的字段。

### 6.3 API `started` 为什么不是 CRI `StartContainer` 的同义词

源码：`pkg/kubelet/prober/prober_manager.go:275-295`，`isContainerStarted` **完整函数，教学注释版**。

```go
func (m *manager) isContainerStarted(pod *v1.Pod, containerStatus *v1.ContainerStatus) bool {
	// runtime 都没有报告 Running，API started 不可能为 true。
	if containerStatus.State.Running == nil {
		return false
	}

	// 当前 container ID 已有 startup 结果时，只认 Success。
	if result, ok := m.startupManager.Get(kubecontainer.ParseContainerID(containerStatus.ContainerID)); ok {
		return result == results.Success
	}

	// 兼容旧 kubelet 重启语义：feature 关闭时，可以沿用 API 中旧的 started=true。
	if !utilfeature.DefaultFeatureGate.Enabled(features.ChangeContainerStatusOnKubeletRestart) && containerStatus.Started != nil && *containerStatus.Started {
		return true
	}

	// 有 startup worker，但当前 ID 尚无成功结果：仍未 Started。
	if _, exists := m.getWorker(pod.UID, containerStatus.Name, startup); exists {
		return false
	}

	// 没有 startup probe：只要 runtime Running，就视为 Started。
	return true
}
```

**大白话总结：** 时刻 A 的 `game-api` 已经 runtime Running，但 startup cache 尚未 Success，因此 `started=false`。若容器根本没有 startup probe，Running 就足以让 `started=true`。所以 CRI `Started` Event、API `state.running` 与 API `started` 是三种不同证据。

**顺手学 Go：** `result, ok := mapLike.Get(...)` 是多返回值：第一个是结果，第二个说明 key 是否存在。`containerStatus.Started` 是 `*bool`，先判断非 nil，再用 `*containerStatus.Started` 解引用读取值，避免 nil pointer。

### 6.4 新 container ID 为什么通常重置结果，但 kubelet重启是例外

源码：`pkg/kubelet/prober/worker.go:250-287`，`doProbe` 的 **连续摘录**。上文已经从 statusManager 找到当前 container status，并计算了 restartable init标记；下文才进入 `onHold`（当前旧实例先暂停继续探测）、Running、删除和 probe门控。

```go
// worker 发现 status 中的 container ID 与自己上次记录不同。
if w.containerID.String() != c.ContainerID {
	// 先删除旧进程 ID 的 probe 结果，防止健康结论串到新进程。
	if !w.containerID.IsEmpty() {
		w.resultsManager.Remove(w.containerID)
	}

	// 把 API containerID 字符串解析成 kubelet内部 ContainerID。
	w.containerID = kubecontainer.ParseContainerID(c.ContainerID)

	// 当前 feature 默认关闭时，保留一段 kubelet重启兼容逻辑。
	if !utilfeature.DefaultFeatureGate.Enabled(features.ChangeContainerStatusOnKubeletRestart) {
		isRestart := false
		if c.State.Running != nil {
			containerStartTime := c.State.Running.StartedAt.Time
			// 早于 kubelet 重启宽限边界，说明可能是重启前已存在的旧进程。
			if !containerStartTime.IsZero() && containerStartTime.Before(kubeletRestartGracePeriod(w.probeManager.start)) {
				isRestart = true
			}
		}

		// restartable init 的 startup 在 kubelet重启后可沿用旧 Started=true，避免初始化倒退。
		if isRestartableInitContainer && w.probeType == startup {
			if c.Started != nil && *c.Started {
				w.resultsManager.Set(w.containerID, results.Success, w.pod)
			}
		}

		// 只有真正新实例，而不是 kubelet重启后重新发现的旧实例，才写默认初值。
		if !isRestart {
			w.resultsManager.Set(w.containerID, w.initialValue, w.pod)
		}
	} else {
		// feature 开启时，不做旧实例兼容，直接写当前 probe 的初值。
		w.resultsManager.Set(w.containerID, w.initialValue, w.pod)
	}

	// 新 ID 出现后恢复 probe；旧 ID failure 造成的 hold 到此结束。
	w.onHold = false
}
```

**大白话总结：** 主案是 kubelet正常运行期间真正创建的 `game-002`，所以三个 worker会为新 ID 建自己的初值。若只是 kubelet进程重启后重新发现早已运行的 container，当前默认行为可能不重种初值；不能把“ID 首次被这个 worker看见”都等同于“container 刚刚新建”。

**顺手学 Go：** `if init; condition` 可以把局部变量声明和判断写在同一个 if 中。`time.Time.IsZero()` 判断是否为零时间。内层 `:=` 创建的 `containerStartTime`、`isRestart` 只在相应作用域可见。

### 6.5 startup 为什么能挡住 readiness/liveness

源码：`pkg/kubelet/prober/worker.go:316-346`，`doProbe` 的 **连续摘录**。前面已经确认 container Running 且 worker不在 onHold；后面才真正调用 prober。

```go
// Pod 正在优雅删除时，liveness/startup 不应再触发一次重启。
if w.pod.ObjectMeta.DeletionTimestamp != nil && (w.probeType == liveness || w.probeType == startup) { // 删除只特殊处理会触发重启的两类 probe。
	logger.V(3).Info("Pod deletion requested, setting probe result to success", // 记录为何要把结果改成成功。
		"probeType", w.probeType, "pod", klog.KObj(w.pod), "containerName", w.container.Name) // 日志带齐 probe、Pod 与 container 身份。
	if w.probeType == startup { // startup 尚未成功就删除时，再补一条更醒目的日志。
		logger.Info("Pod deletion requested before container has fully started", // 说明删除发生在启动完成前。
			"pod", klog.KObj(w.pod), "containerName", w.container.Name) // 用对象 key 和 container name定位现场。
	}
	// 最后写 Success，让终止链保持安静，然后永久停止这个 worker。
	w.resultsManager.Set(w.containerID, results.Success, w.pod) // 当前 ID不再向同步链发布失败。
	return false // false表示这个 worker永久退出，不是“probe失败”。
}

// 每种 probe 先遵守自己的 InitialDelaySeconds。
if int32(time.Since(c.State.Running.StartedAt.Time).Seconds()) < w.spec.InitialDelaySeconds { // 运行时长还没达到延迟预算就不探测。
	return true // true表示保留 worker，等待下一周期。
}

// Started指针存在且为true，说明startup门已经打开。
if c.Started != nil && *c.Started { // 必须先判nil，才能解引用读取bool。
	// startup 已成功后，startup worker不再对当前 ID执行真实 probe。
	if w.probeType == startup { // 当前正是 startup worker时进入空转等待。
		return true // 保留worker，以便以后识别新的container ID。
	}
} else { // Started缺失或为false，startup门尚未打开。
	// startup 尚未成功时，readiness/liveness worker只等待，不执行请求。
	if w.probeType != startup { // 只有startup worker可以继续做真实探测。
		return true // readiness/liveness本周期提前返回，但worker不退出。
	}
}
```

**大白话总结：** startup 是 readiness/liveness 的执行门，不是第四个 Ready Condition。时刻 A 中 readiness/liveness worker虽然存在，却在 `started=false` 分支返回；删除期间 liveness/startup 还会主动写最后一次 Success 并停止，避免终止过程被误判成应重启。

**顺手学 Go：** `&&` 和 `||` 都是短路运算。`return true` 在这里不是“probe 成功”，而是“worker 以后继续运行”；`return false` 才表示永久退出循环。读多返回值或 bool 时必须按函数契约解释，不能统一翻译成成功/失败。

## 7. 第二层源码：为什么第一条 `Unhealthy` Event 不等于 threshold 已达到

### 7.1 `prober.probe` 先区分普通 Failure 与执行 error

源码：`pkg/kubelet/prober/prober.go:102-128`，`prober.probe` 的 **连续摘录**。probe type/spec 已在前文选好；`runProbeWithRetries` 对一次 worker 执行最多进行 3 次底层尝试（不是初次之外再重试 3 次），返回最后结果。

```go
// 执行具体 HTTP/exec/TCP/gRPC probe；本轮最多进行固定次数的底层尝试。
result, output, err := pb.runProbeWithRetries(ctx, probeType, probeSpec, pod, status, container, containerID, maxProbeRetries) // 同时取得底层结果、输出文本和Go error。

if err != nil { // Go error表示执行链本身出错，不是普通业务Failure。
	// 执行 error 会记录独立 message；worker稍后丢弃这次结果。
	logger.V(1).Info("Probe errored", "probeType", probeType, "pod", klog.KObj(pod), "podUID", pod.UID, "containerName", container.Name, "probeResult", result, "err", err) // 日志保留结果与原始error。
	pb.recordContainerEvent(ctx, pod, &container, v1.EventTypeWarning, events.ContainerUnhealthy, "%s probe errored and resulted in %s state: %s", probeType, result, err) // Event告诉运维这是执行error。
	return results.Failure, err // 虽返回Failure值，但非nil error会让上层丢弃这次结果。
}

// 没有 transport error 时，再按底层 probe result分类。
switch result { // 同一个底层枚举被映射成results manager使用的枚举。
case probe.Success: // 普通成功直接向上返回Success。
	logger.V(3).Info("Probe succeeded", "probeType", probeType, "pod", klog.KObj(pod), "podUID", pod.UID, "containerName", container.Name) // 成功只写详细级别日志。
	return results.Success, nil // nil error允许worker累计threshold。

case probe.Warning: // Warning在控制语义上仍属于可用。
	// Warning 会记录 ProbeWarning，但控制语义仍按 Success。
	pb.recordContainerEvent(ctx, pod, &container, v1.EventTypeWarning, events.ContainerProbeWarning, "%s probe warning: %s", probeType, output) // Event保留warning输出。
	logger.V(3).Info("Probe succeeded with a warning", "probeType", probeType, "pod", klog.KObj(pod), "podUID", pod.UID, "containerName", container.Name, "output", output) // 日志仍明确成功但有警告。
	return results.Success, nil // 上层按Success累计。

case probe.Failure: // 普通失败会参与failureThreshold。
	// 每一次普通 Failure 都可以先记录 Unhealthy Event。
	logger.V(1).Info("Probe failed", "probeType", probeType, "pod", klog.KObj(pod), "podUID", pod.UID, "containerName", container.Name, "probeResult", result, "output", output) // 日志记录底层输出。
	pb.recordContainerEvent(ctx, pod, &container, v1.EventTypeWarning, events.ContainerUnhealthy, "%s probe failed: %s", probeType, output) // 每次普通Failure都可产生Unhealthy。
	return results.Failure, nil // nil error表示这是可计数的普通Failure。

case probe.Unknown: // 没有Go error的Unknown走当前特殊映射。
	// 当前实现把“无 error 的 Unknown”折成 Failure交给worker计数。
	logger.V(1).Info("Probe unknown without error", "probeType", probeType, "pod", klog.KObj(pod), "podUID", pod.UID, "containerName", container.Name, "probeResult", result) // 日志显式区分Unknown。
	return results.Failure, nil // 上层仍按普通Failure累计。

default: // 防御未来或非法枚举值。
	// 不支持的底层值同样折成无 error Failure。
	logger.V(1).Info("Unsupported probe result", "probeType", probeType, "pod", klog.KObj(pod), "podUID", pod.UID, "containerName", container.Name, "probeResult", result) // 把意外值留在日志中。
	return results.Failure, nil // 保守折成Failure而不是Success。
}
```

**大白话总结：** Event 在 threshold 之前产生。普通 HTTP 503 是 `Failure,nil`，会交给 worker累计；transport error 是 `Failure,err`，worker会丢弃；Warning 记 Event 但按 Success。值班时必须读完整 message，不能只看 reason 都叫 `Unhealthy`。

**顺手学 Go：** `(results.Result, error)` 是两个返回位置。这里 `Failure,nil` 与 `Failure,err` 的控制语义不同。`args...` 出现在 recorder 的 variadic 调用中时是 Go 展开语法，不是省略源码。

### 7.2 threshold 只改变稳定 cache，不抹掉每次探测事实

源码：`pkg/kubelet/prober/worker.go:348-393`，`doProbe` 尾部的 **连续摘录**。

```go
// 真正执行一次worker级probe；exec probe不会自动继承Pod env/downward API。
result, err := w.probeManager.prober.probe(ctx, w.probeType, w.pod, status, w.container, w.containerID) // 返回稳定枚举候选和执行error。
if err != nil { // 执行error不属于普通Failure计数。
	// transport error不计metrics、不累计threshold，也不改变稳定cache。
	return true // 本轮丢弃，但worker下周期继续。
}

// 只有无error的实际执行才进入probe metrics。
switch result { // 指标按Success、Failure和其他值分支。
case results.Success: // 成功同时计次数和耗时。
	ProberResults.With(w.proberResultsSuccessfulMetricLabels).Inc() // 成功counter加一。
	ProberDuration.With(w.proberDurationSuccessfulMetricLabels).Observe(time.Since(startTime).Seconds()) // 成功histogram记录耗时。
case results.Failure: // 普通失败只计次数。
	ProberResults.With(w.proberResultsFailedMetricLabels).Inc() // 失败counter加一，不Observe duration。
default: // 其他值记为Unknown。
	ProberResults.With(w.proberResultsUnknownMetricLabels).Inc() // Unknown counter加一。
	ProberDuration.With(w.proberDurationUnknownMetricLabels).Observe(time.Since(startTime).Seconds()) // Unknown histogram记录耗时。
}

// 连续相同结果才累加；结果方向改变就从1重新开始。
if w.lastResult == result { // 与上次方向相同才保持连续性。
	w.resultRun++ // 连续次数加一。
} else { // Success和Failure互换时重新起算。
	w.lastResult = result // 先记住新的方向。
	w.resultRun = 1 // 本次就是新连续段的第一条。
}

// Failure/Success 未达到各自threshold时，只保留旧稳定cache。
if (result == results.Failure && w.resultRun < int(w.spec.FailureThreshold)) ||
	(result == results.Success && w.resultRun < int(w.spec.SuccessThreshold)) { // 两个方向分别比较自己的阈值。
	return true // 不发布新稳定结论，等待下一次。
}

// 达到threshold后才把稳定结论写入对应result manager。
w.resultsManager.Set(w.containerID, result, w.pod) // key是当前container ID，避免跨实例串值。

// liveness Failure以及startup任一达阈值结果都hold，等待新container ID。
if (w.probeType == liveness && result == results.Failure) || w.probeType == startup { // 这两类结果会等待容器动作收敛。
	w.onHold = true // 当前ID先停止真实probe。
	w.resultRun = 0 // 连续计数清零，避免旧段重复触发。
}

return true // worker仍存在，之后可识别新的container ID。
```

**大白话总结：** 时刻 C 第一次 readiness 503 可以有 Event，但 `resultRun=1<2`，稳定 cache 仍是旧 Success。第二次连续 Failure 才 Set。startup 成功和失败都会让当前 worker进入 onHold；liveness 只在 Failure 达阈值后 hold，避免旧进程停机期间继续 exec/probe。

**顺手学 Go：** `w.resultRun++` 等价于加一。跨行复合条件先计算括号内两组，再用 `||`；任一“未达 threshold”成立就早返回。`int(w.spec.FailureThreshold)` 是显式类型转换，因为 API 字段和本地计数类型不同。

### 7.3 result manager 为什么只在稳定值变化时发 update

源码：`pkg/kubelet/prober/results/results_manager.go:98-128`，`NewManager`、`Set` 与 `setInternal` 的 **连续摘录**。

```go
// 每种probe各创建一本空cache和一个容量20的update channel。
func NewManager() Manager {
	return &manager{ // 返回实现Manager接口的指针。
		cache:   make(map[kubecontainer.ContainerID]Result), // map保存每个container ID的稳定结果。
		updates: make(chan Update, 20), // channel暂存稳定结果变化事件。
	}
}

func (m *manager) Get(id kubecontainer.ContainerID) (Result, bool) { // 同时返回值和是否存在。
	// 并发读cache时使用读锁。
	m.RLock() // 多个读者可以并发进入。
	defer m.RUnlock() // 函数退出前保证释放读锁。
	result, found := m.cache[id] // map双返回值区分零值与不存在。
	return result, found // 原样交给调用方判断。
}

func (m *manager) Set(id kubecontainer.ContainerID, result Result, pod *v1.Pod) { // 写cache并在变化时发布update。
	// 只有cache新增key或值变化，才向syncLoop发送Update。
	if m.setInternal(id, result) { // bool告诉外层稳定值是否真的变化。
		m.updates <- Update{id, result, pod.UID} // 这是携带container ID、结果、Pod UID三项数据的阻塞发送。
	}
}

func (m *manager) setInternal(id kubecontainer.ContainerID, result Result) bool { // 内部函数只负责原子比较并写map。
	// 写cache时独占锁；defer保证所有return都解锁。
	m.Lock() // 写入期间阻止其他读写者看到半步状态。
	defer m.Unlock() // 所有返回分支都释放锁。
	prev, exists := m.cache[id] // 同时取旧值和key存在性。
	if !exists || prev != result { // 新key或值改变才算一次更新。
		m.cache[id] = result // 覆盖当前稳定结果。
		return true // 通知外层需要发送Update。
	}
	return false // 值完全相同，不产生重复Update。
}
```

**大白话总结：** readiness 已经 Failure 时，后续普通 Failure仍会执行、记 metrics/Event，但不会每个 period 都给 syncLoop塞同值 update。注意它的 channel 与 statusManager 的 doorbell（只提醒“有事待办”的门铃）不同：这里携带 container ID/result/Pod UID，容量20且发送可阻塞；statusManager 后面使用可合并的非阻塞空通知。

**顺手学 Go：** `make(map[...])` 创建 map，`make(chan Update, 20)` 创建带缓冲 channel。嵌入的 `sync.RWMutex` 让 `m.RLock()` 直接可用。`Update{id, result, pod.UID}` 是按字段顺序构造值，阅读内部代码时要回到 struct 定义确认三个位置。

## 8. 第三层源码：readiness 为什么只摘流量，不进入 kill action

### 8.1 syncLoop 对三种 probe 的处理故意不对称

核心代码已在 §5.6 逐行读过，这里不重复粘贴。现在只补两个边界：

- **fast-path（快速路径）**：readiness结果变化时，kubelet先尝试直接修改已有的本地 PodStatus，目的是尽快推动摘流量；“快”不等于绕过后续完整对账。
- **early return（提前返回）**：如果 Pod已删除、本地状态还没建立、container ID已经换代，快速路径会直接结束。随后触发的正常 `SyncPod` 仍会重新读取 probe cache并生成完整状态。

因此，readiness有“先快改、再完整核对”两层保险；liveness没有所谓 `SetContainerLiveness`，它必须进入后面的动作计算，才能决定是否停止或重启容器。

### 8.2 `SetContainerReadiness` 为什么既有 fast-path，也允许 early return

源码：`pkg/kubelet/status/status_manager.go:490-557`，`SetContainerReadiness` **完整函数，教学注释版**。

```go
func (m *manager) SetContainerReadiness(logger klog.Logger, podUID types.UID, containerID kubecontainer.ContainerID, ready bool) { // 用Pod UID和container ID精确定位要改的实例。
	var notification *podStatusNotification // 先声明可空通知，稍后在锁内赋值。
	// 解锁后才调用notifier，避免持锁执行外部回调。
	defer func() { // defer保证所有return分支最终都执行这段收尾。
		if notification != nil { // 只有本地status真正变化才有通知对象。
			m.sendNotification(notification) // 此时函数体锁已经由另一个defer释放。
		}
	}() // 末尾括号立即调用这个匿名函数注册defer。

	// status cache与版本账由同一把锁保护。
	m.podStatusesLock.Lock() // 独占锁保护后面的读取、复制和版本更新。
	defer m.podStatusesLock.Unlock() // 注册在后，所以返回时先解锁、再发送通知。

	// Pod已经从desired manager删除：旧probe update直接忽略。
	pod, ok := m.podManager.GetPodByUID(podUID) // 重新取得当前desired Pod，不信任旧update里的对象。
	if !ok { // UID已经不在desired集合中。
		logger.V(4).Info("Pod has been deleted, no need to update readiness", "podUID", podUID) // 留下忽略原因。
		return // 不创建本地status，也不发通知。
	}

	// Pod尚未完成过一次本地status生成：没有可安全修改的基线。
	oldStatus, found := m.podStatuses[pod.UID] // 从本地版本账读取完整旧status。
	if !found { // fast-path早于首次SyncPod时会命中。
		logger.Info("Container readiness changed before pod has synced", // 说明还没有可修改基线。
			"pod", klog.KObj(pod), // 日志记录namespace/name。
			"containerID", containerID.String()) // 同时记录实例ID。
		return // 等正常SyncPod从runtime重新生成完整status。
	}

	// result按container ID归属；旧ID update不能误写到新实例。
	containerStatus, _, ok := findContainerStatus(&oldStatus.status, containerID.String()) // 在普通、init等status中按ID查找。
	if !ok { // 本地status已经没有这个旧实例。
		logger.Info("Container readiness changed for unknown container", // 记录陈旧update被拒绝。
			"pod", klog.KObj(pod), // 绑定Pod对象。
			"containerID", containerID.String()) // 绑定旧container ID。
		return // 不允许按container name误改新实例。
	}

	// 值未变化时不制造重复本地版本。
	if containerStatus.Ready == ready { // 比较当前API-facing值与新结果。
		logger.V(4).Info("Container readiness unchanged", // 仅记录一次no-op。
			"ready", ready, // 带上目标bool。
			"pod", klog.KObj(pod), // 带上Pod身份。
			"containerID", containerID.String()) // 带上实例身份。
		return // 不递增version，不敲doorbell。
	}

	// 深拷贝后修改，不能直接改共享cache中的对象。
	status := *oldStatus.status.DeepCopy() // 得到独立可写副本。
	containerStatus, _, _ = findContainerStatus(&status, containerID.String()) // 在副本里重新取得字段指针。
	containerStatus.Ready = ready // 只修改目标container实例的Ready。

	// helper按condition type替换已有项；缺失时补入并记录异常信息。
	updateConditionFunc := func(conditionType v1.PodConditionType, condition v1.PodCondition) { // 闭包复用“按type更新condition”的算法。
		conditionIndex := -1 // -1表示暂时没找到旧condition。
		for i, condition := range status.Conditions { // 遍历当前副本中的所有condition。
			if condition.Type == conditionType { // type匹配即找到目标位置。
				conditionIndex = i // 保存slice下标。
				break // 找到后立即结束循环。
			}
		}
		if conditionIndex != -1 { // 旧condition存在时原位替换。
			status.Conditions[conditionIndex] = condition // 保持slice结构并写新值。
		} else { // 理论上缺失时走防御分支。
			logger.Info("PodStatus missing condition type", "conditionType", conditionType, "status", status) // 记录不完整基线。
			status.Conditions = append(status.Conditions, condition) // 仍补入新condition让状态收敛。
		}
	}

	// Ready计算还要把restartable init与普通container状态放在一起。
	allContainerStatuses := append(status.InitContainerStatuses, status.ContainerStatuses...) // 合并两类会影响Ready的container状态。
	updateConditionFunc(v1.PodReady, GeneratePodReadyCondition(pod, &oldStatus.status, status.Conditions, allContainerStatuses, status.Phase)) // 重算Pod Ready，包含readiness gates。
	updateConditionFunc(v1.ContainersReady, GenerateContainersReadyCondition(pod, &oldStatus.status, allContainerStatuses, status.Phase)) // 同时重算ContainersReady。
	// 写入新本地version，并取得锁外通知对象。
	_, notification = m.updateStatusInternal(logger, pod, status, false, false) // 忽略changed bool，只保留可能的通知payload。
}
```

**大白话总结：** readiness update不是“收到 channel 就一定成功翻转 Ready”。Pod已删、status尚未建立、container ID已换代、值未变化都会 early return。但 syncLoop随后仍调用 `handleProbeSync`，正常 `SyncPod -> generateAPIPodStatus` 会再次从 probe cache计算状态，两条路径最终汇合。

**顺手学 Go：** `defer func() { ... }()` 定义并登记匿名 closure，在外层函数返回前执行。`:=` 只要左侧至少一个变量是新变量就合法；后面 `containerStatus, _, _ =` 使用普通赋值，是因为变量已经存在。`_` 表示明确丢弃返回位置。

### 8.3 `phase`、`started`、container Ready 与 Pod Ready 在同一轮怎样分别生成

源码：`pkg/kubelet/kubelet_pods.go:1896-1900,1947,1988-1991`，`generateAPIPodStatus` 的 **非连续检查点**。中间省略 terminal phase、eviction、host IP 与 resize 等旁支；代码块不可独立编译。

```go
// 先把PLEG/runtime PodStatus转换成API container状态。
s := kl.convertStatusToAPIStatus(ctx, pod, podStatus, oldPodStatus)
// phase先根据container生命周期计数生成，不读取readiness bool。
allStatus := append(append([]v1.ContainerStatus{}, s.ContainerStatuses...), s.InitContainerStatuses...)
s.Phase = getPhase(logger, pod, allStatus, podIsTerminal, kubecontainer.HasAnyActiveRegularContainerStarted(&pod.Spec, podStatus))
logger.V(4).Info("Got phase for pod", "pod", klog.KObj(pod), "oldPhase", oldPodStatus.Phase, "phase", s.Phase)

// phase之后，再按startup/readiness cache填每个container的Started/Ready。
kl.probeManager.UpdatePodStatus(ctx, pod, s)

// 最后用container Ready及readiness gates生成Pod级conditions。
allContainerStatuses := append(s.InitContainerStatuses, s.ContainerStatuses...)
s.Conditions = append(s.Conditions, status.GeneratePodInitializedCondition(pod, &oldPodStatus, allContainerStatuses, s.Phase))
s.Conditions = append(s.Conditions, status.GeneratePodReadyCondition(pod, &oldPodStatus, s.Conditions, allContainerStatuses, s.Phase))
s.Conditions = append(s.Conditions, status.GenerateContainersReadyCondition(pod, &oldPodStatus, allContainerStatuses, s.Phase))
```

**大白话总结：** 当前函数顺序直接解释 `Running + Ready=False`：phase先从生命周期状态得出，随后才把 probe cache写进 Started/Ready，最后生成 Pod Ready conditions。Pod Ready还会叠加 readiness gates；container ready=true 也不必然推出 Pod Ready=true。

**顺手学 Go：** 嵌套 `append` 先创建一个空 slice，再依次追加普通和 init status，避免直接复用原 slice。传入 `s` 时没有 `*`，是因为 `s` 本身是 `*v1.PodStatus`；Go会按函数签名检查指针和值。

源码：`pkg/kubelet/kubelet_pods.go:1809-1824`，`getPhase` 决策尾部的 **连续摘录**。前面已经统计 init、waiting、running、stopped、unknown；后面还有全停止与 restart policy 分支，所以本段不能被概括成 phase 的全部规则。

```go
switch { // 无表达式switch按从上到下第一个true条件选分支。
// init未完成或任一container仍是普通Waiting时，先判Pending。
case pendingRegularInitContainers > 0 ||
	(pendingRestartableInitContainers > 0 && !podHasInitialized): // 两类init条件合并判断。
	fallthrough // 不直接返回，显式落到下一个Pending处理体。
case waiting > 0: // 普通container存在Waiting也属于Pending。
	logger.V(5).Info("Pod waiting > 0, pending") // 记录phase选择依据。
	return v1.PodPending // 返回PodPending。
// 没有前述Pending条件、至少一个被计数container Running、且unknown为0。
case running > 0 && unknown == 0: // Running计数可包含当前版本规则下的restartable init语义。
	return v1.PodRunning // 这里只决定phase，不读取readiness。
```

**大白话总结：** 在主案中两个普通 container 都 Running、没有 waiting/unknown，因此 phase=Running。但不能把 Running 的通用定义缩成“此刻至少一个普通 container 一定运行”：restartable init也进入计数，后面的 restartPolicy 分支还允许 `running=0`、容器等待重启时保持 Running；API 本身也可能滞后于当前 CRI。

**顺手学 Go：** 不带表达式的 `switch` 等价于按顺序判断多个布尔条件。`fallthrough` 明确继续执行下一个 case；Go 默认不会自动贯穿，所以这里是有意把两类条件汇入同一个 Pending 返回。

### 8.4 EndpointSlice 为什么通常保留 endpoint，只翻 conditions

源码：`staging/src/k8s.io/endpointslice/utils.go:37-50`，`podToEndpoint` 开头的 **连续摘录**。后面继续填 targetRef、NodeName、zone 与 hostname；本段只读流量条件。

```go
func podToEndpoint(pod *v1.Pod, node *v1.Node, service *v1.Service, addressType discovery.AddressType) discovery.Endpoint { // 把一个Pod转换成EndpointSlice中的一个endpoint。
	// serving直接读取API Pod Ready，而不是节点本地probe cache。
	serving := endpointutil.IsPodReady(pod) // bool表示API对象当前是否Ready。
	// terminating来自Pod deletionTimestamp。
	terminating := pod.DeletionTimestamp != nil // 有删除时间戳就处于终止中。
	// 普通Service要求Pod serving且未terminating；publishNotReady可强制ready。
	ready := service.Spec.PublishNotReadyAddresses || (serving && !terminating) // Service特殊契约可以覆盖常规Ready条件。
	ep := discovery.Endpoint{ // 组装Endpoint值对象。
		Addresses: getEndpointAddresses(pod.Status, service, addressType), // 从PodStatus选择匹配地址族的地址。
		Conditions: discovery.EndpointConditions{ // 三个指针bool分别表达可用、服务和终止语义。
			Ready:       &ready, // 数据面通常主要消费Ready。
			Serving:     &serving, // 保留Pod实际Ready事实。
			Terminating: &terminating, // 标记endpoint是否正在终止。
		},
	}
```

**大白话总结：** 时刻 C 中 endpoint对象通常仍存在，但 `ready=false/serving=false`。它读取的是已写入 API 的 Pod Ready，所以还要经过 statusManager、apiserver和EndpointSlice controller；本地 readiness cache刚变时，数据面不保证同一瞬间完成更新。`publishNotReadyAddresses=true` 会让 ready 强制为 true，但 serving仍反映Pod Ready。

**顺手学 Go：** `&ready` 取得局部 bool 的指针，API 用 `*bool` 区分 true、false 与 nil。复合字面量 `discovery.Endpoint{...}` 类似一次性构造结构体，但 Go 没有 Java constructor 语义。

## 9. 第四层源码：liveness 为什么不直接 kill，而要回到 `SyncPod`

### 9.1 `handleProbeSync` 只用最新 desired Pod 唤醒 pod worker

源码：`pkg/kubelet/kubelet.go:2817-2828`，`handleProbeSync` **完整函数，教学注释版**。

```go
func handleProbeSync(ctx context.Context, kl *Kubelet, update proberesults.Update, handler SyncHandler, probe, status string) {
	logger := klog.FromContext(ctx)
	// result manager保存的Pod对象不会持续更新，所以按UID重新拿最新desired Pod。
	pod, ok := kl.podManager.GetPodByUID(update.PodUID)
	if !ok {
		// Pod已经删除：旧probe结果不应复活工作负载。
		logger.V(4).Info("SyncLoop (probe): ignore irrelevant update", "probe", probe, "status", status, "update", update)
		return
	}
	logger.V(1).Info("SyncLoop (probe)", "probe", probe, "status", status, "pod", klog.KObj(pod))
	// 这里只提交一次Pod同步请求，不调用CRI。
	handler.HandlePodSyncs(ctx, []*v1.Pod{pod})
}
```

**大白话总结：** probe update只是“这份 desired Pod 需要再对账”。Pod若已删就忽略；Pod仍存在才交给 pod worker。pod worker还能合并同 UID 更新，因此一条 probe Update不保证独占执行一轮 SyncPod。

**顺手学 Go：** `[]*v1.Pod{pod}` 构造只含一个 Pod 指针的 slice。函数参数里的 `probe, status string` 表示两个相邻参数共用 string 类型。

### 9.2 runtime manager 怎样把 liveness Failure 翻译成 kill/start action

源码：`pkg/kubelet/kuberuntime/kuberuntime_manager.go:1327-1374`，`computePodActions` 普通 container Running 分支的 **连续摘录**。前面已处理不存在/非 Running container；后面会判断是否需要 KillPod。

```go
// 当前container确实Running，才检查spec变化、liveness/startup和resize。
var message string // 供Event和日志解释为何kill。
var reason containerKillReason // 供宽限期等下游逻辑区分kill来源。
// Pod级Never以外默认允许故障后重启；Deployment主案为Always。
restart := shouldRestartOnFailure(pod) // 先按Pod级restartPolicy计算默认值。
if utilfeature.DefaultFeatureGate.Enabled(features.ContainerRestartRules) { // feature开启才读取container级规则。
	// probe发生时container仍Running，只读取container-level restartPolicy，不匹配exit-code rules。
	if container.RestartPolicy != nil { // 指针非nil表示container覆盖了默认策略。
		restart = *container.RestartPolicy != v1.ContainerRestartPolicyNever // Never才明确禁止重新start。
	}
}

// spec变化优先级高于probe结果，而且无视restart policy强制重启。
if _, _, changed := containerChanged(&container, containerStatus); changed { // if初始化语句只把changed留在本分支作用域。
	message = fmt.Sprintf("Container %s definition changed", container.Name) // 生成人类可读原因。
	restart = true // spec变化必须回到新定义，强制重新start。
} else if liveness, found := m.livenessManager.Get(containerStatus.ID); found && liveness == proberesults.Failure { // 只读取当前Running ID的稳定结果。
	// 只对当前Running ID读取稳定liveness Failure。
	message = fmt.Sprintf("Container %s failed liveness probe", container.Name) // message稍后可追加will be restarted。
	reason = reasonLivenessProbe // 结构化标记kill来源。
} else if startup, found := m.startupManager.Get(containerStatus.ID); found && startup == proberesults.Failure { // liveness未命中才检查startup。
	// startup Failure排在liveness之后，形成另一种kill reason。
	message = fmt.Sprintf("Container %s failed startup probe", container.Name) // 记录startup原因。
	reason = reasonStartupProbe // 标记为startup kill。
} else if !m.computePodResizeAction(ctx, pod, idx, false, containerStatus, &changes) { // false表示resize helper已填好restart所需的kill/start action。
	continue // action已记录，跳到下一个container，不能再重复加入通用kill map。
} else { // true表示当前Running实例可保留，可能无需resize或走in-place resize。
	// 没有任何kill理由：保留当前container，是正常no-op。
	keepCount++ // 统计仍应保留的container。
	continue // 跳到下一个container。
}

if restart { // kill以后还要恢复desired实例时进入。
	// kill后还想恢复desired时，明确加入start index。
	message = fmt.Sprintf("%s, will be restarted", message) // Event明确告诉运维将重启。
	changes.ContainersToStart = append(changes.ContainersToStart, idx) // 记录Pod spec中的container下标。
}

// 无论是否restart，只要命中kill理由都把当前ID加入kill map。
changes.ContainersToKill[containerStatus.ID] = containerToKillInfo{ // map key锁定当前runtime实例ID。
	name:      containerStatus.Name, // 保存container name供日志和hook使用。
	container: &pod.Spec.Containers[idx], // 保存desired spec指针。
	message:   message, // 保存人类可读原因。
	reason:    reason, // 保存结构化kill reason。
}
logger.V(2).Info("Message for Container of pod", "containerName", container.Name, "containerStatusID", containerStatus.ID, "pod", klog.KObj(pod), "containerMessage", message) // 最后记录本轮action解释。
```

**大白话总结：** 主案 `game-001` Running、liveness cache=Failure、restartPolicy=Always，于是同一轮同时得到 `ContainersToKill[game-001]` 和 `ContainersToStart[game-api index]`。readiness cache根本不在这个判断里。spec变化优先于probe，说明现场必须确认 kill message，不能看到重启就自动归因 liveness。

S3边界：`ContainersToKill` 是 Go map；若一轮有多个 kill目标，遍历顺序不保证。某个 kill中途失败会停止后续流程，但之前已经成功停止的 container不会自动回滚。主案只有 `game-api` 一个 kill目标，因此不引入这个多目标不确定性。

**顺手学 Go：** `else if value, found := call(); found && ...` 把缓存值和存在标记限制在当前分支。map 下标 `changes.ContainersToKill[id] = info` 按 container ID 写一条 action；slice append存的是 spec index，两本 action 数据结构用途不同。

### 9.3 `Killing` Event 为什么只证明进入 kill 路径，不证明 CRI 已停止成功

源码：`pkg/kubelet/kuberuntime/kuberuntime_container.go:878-916`，`killContainer` 的 **连续摘录**。上文已恢复 Pod/container spec；下文处理成功日志与 termination ordering。

```go
// 根据Pod、probe级配置和kill reason计算终止宽限期。
gracePeriod := setTerminationGracePeriod(ctx, pod, containerSpec, containerName, containerID, reason) // 得到本次kill可用秒数。

if len(message) == 0 { // 上游未提供原因时生成默认文案。
	message = fmt.Sprintf("Stopping container %s", containerSpec.Name) // Event至少不会是空message。
}
// Event在PreStop和CRI StopContainer之前记录。
m.recordContainerEvent(ctx, pod, containerSpec, containerID.ID, v1.EventTypeNormal, events.KillingContainer, "%v", message) // 这里只证明进入kill函数。

if gracePeriodOverride != nil { // 调用方显式覆盖时优先使用覆盖值。
	gracePeriod = *gracePeriodOverride // 解引用取得秒数。
	logger.V(3).Info("Killing container with a grace period override", "pod", klog.KObj(pod), "podUID", pod.UID, // 第一行绑定Pod身份。
		"containerName", containerName, "containerID", containerID.String(), "gracePeriod", gracePeriod) // 第二行绑定container和宽限期。
}

// 有PreStop且仍有宽限时间时，先执行hook并扣减耗时。
if containerSpec.Lifecycle != nil && containerSpec.Lifecycle.PreStop != nil && gracePeriod > 0 { // 三个条件同时满足才执行hook。
	gracePeriod = gracePeriod - m.executePreStopHook(ctx, pod, containerID, containerSpec, gracePeriod) // 剩余时间扣除hook耗时。
}

// 多container termination ordering可能继续消耗剩余时间。
if ordering != nil && gracePeriod > 0 { // 存在顺序约束且还有时间才等待。
	gracePeriod -= int64(ordering.waitForTurn(containerName, gracePeriod)) // 把等待秒数从预算中扣除。
}

// 即使前面耗尽，也给runtime最小关停窗口。
if gracePeriod < minimumGracePeriodInSeconds { // 防止传给CRI负数或过小值。
	gracePeriod = minimumGracePeriodInSeconds // 提升到实现规定的最小值。
}

logger.V(2).Info("Killing container with a grace period", "pod", klog.KObj(pod), "podUID", pod.UID, // 记录最终调用前的Pod身份。
	"containerName", containerName, "containerID", containerID.String(), "gracePeriod", gracePeriod) // 记录目标ID和最终宽限期。

// 真正请求CRI停止旧ID。
err := m.runtimeService.StopContainer(ctx, containerID.ID, gracePeriod) // 这是本段第一次真正调用runtime stop。
if err != nil && !crierror.IsNotFound(err) { // NotFound视为目标已经不存在，其余error才失败。
	// kill失败直接返回error；本轮后续start不会假装继续成功。
	logger.Error(err, "Container termination failed with gracePeriod", "pod", klog.KObj(pod), "podUID", pod.UID, // 第一行记录Pod与error。
		"containerName", containerName, "containerID", containerID.String(), "gracePeriod", gracePeriod) // 第二行记录具体实例和参数。
	return err // 把失败交给上层SyncPod处理。
}
```

**大白话总结：** `Killing` Event message含 `failed liveness probe, will be restarted`，能把“为何进入 kill path”与本次探针关联起来；但 Event 发生在 PreStop/StopContainer 之前。必须继续看 kubelet/runtime日志和 CRI 旧ID状态，才能证明停止成功。kill error是硬停止线，不会继续 start新实例。

**顺手学 Go：** `err != nil && !crierror.IsNotFound(err)` 把 NotFound视为幂等成功。`gracePeriod = gracePeriod - ...` 是普通赋值；后面的 `-=` 是复合赋值。局部 `err :=` 在当前函数作用域首次声明。

### 9.4 为什么第一次 liveness restart通常不会立刻变成 CrashLoopBackOff

第 12 课已经读过 `doBackOff`。关键输入是**进入本轮 SyncPod 前**的 runtime podStatus：

```text
本轮开始：game-001 仍是 Running
  -> computePodActions因liveness计划kill/start
  -> doBackOff找不到“刚刚被本轮kill后”的Exited A作为旧输入
  -> B通常可以立即Create/Start

若B随后快速退出
  -> PLEG把Exited B写入下一轮podStatus
  -> doBackOff第一次看到这个key通常尚不在backoff窗口
  -> backOff.Next记录第一档退避，但本轮仍允许game-003启动

若game-003又在有效窗口内快速退出
  -> 后续doBackOff判断IsInBackOffSince=true
  -> 返回BackoffError，StartContainer SyncResult失败
  -> Kubelet把该SyncResult写入reasonCache
  -> 再下一轮API status转换读取reasonCache
  -> Waiting.reason才显示CrashLoopBackOff
```

因此 liveness kill 本身不等于已经进入 CrashLoopBackOff；在没有既有 backoff entry的干净主案里，`game-002` 第一次快速退出通常也只是建立第一档 backoff并允许下一实例启动。真正的 `Waiting.reason=CrashLoopBackOff` 需要后续实例继续快速失败、`doBackOff` 返回 BackoffError、reasonCache记录失败，再经过下一轮 status生成。

### 9.5 `restartPolicy=Never` 为什么不能只说“kill但不start”

对 Running container 的 probe Failure，Never 不加入 `ContainersToStart`。但 `computePodActions` 循环末尾还有：

```text
keepCount == 0 && ContainersToStart == 0
  -> KillPod = true
```

所以单 container、Never 的 Pod 可能升级为停止整个 Pod runtime 现场；本章双 container主案里 `jmx-exporter` 被 keep，`keepCount>0`，因此只杀 `game-api`，不需要重建 Sandbox。不能脱离其余 container 状态讲 restartPolicy。

## 10. 第五层源码：PLEG 为什么在动作之后重新观察 runtime，而不是相信“我刚刚调用成功”

### 10.1 probe 驱动与 runtime 主动变化是两个方向

```text
probe驱动：
Failure -> SyncPod决定kill/start -> runtime变化 -> PLEG观察结果

runtime先变：
System.exit/OOM/crictl外部动作 -> PLEG观察 -> SyncPod决定是否恢复desired
```

控制器必须依赖重新观察的 actual state，而不是只依赖上一次 RPC 返回。runtime、kubelet或节点可能在任意中间点崩溃；下一轮仍要从 CRI 事实恢复。

### 10.2 GenericPLEG 每轮全局列举，但只对变化或需复查的 Pod取详细 status

这里的 `reinspect` 就是“上一轮没查清楚，这一轮再详细复查一次”。源码：`pkg/kubelet/pleg/generic.go:292-329`，`GenericPLEG.Relist` **完整函数，教学注释版**。

```go
func (g *GenericPLEG) Relist() {
	// 全局Relist与按Pod Relist共享锁，避免同时改podRecords/cache。
	g.relistLock.Lock()
	defer g.relistLock.Unlock()

	ctx := context.Background() // 当前Relist不继承外部取消。

	g.logger.V(5).Info("GenericPLEG: Relisting")

	// 本轮GetPods之前，指标记录“当前Relist开始”距上次成功relist timestamp的间隔。
	if lastRelistTime := g.getRelistTime(); !lastRelistTime.IsZero() {
		metrics.PLEGRelistInterval.Observe(metrics.SinceInSeconds(lastRelistTime))
	}

	timestamp := g.clock.Now() // 本轮观察时间在GetPods前取得。
	defer func() {
		metrics.PLEGRelistDuration.Observe(metrics.SinceInSeconds(timestamp))
	}()

	// 每次全局Relist先向runtime列出全部Pod/container概要。
	podList, err := g.runtime.GetPods(ctx, true)
	if err != nil {
		g.logger.Error(err, "GenericPLEG: Unable to retrieve pods")
		return
	}

	// 注意：GetPods成功后就更新Healthy使用的relistTime，逐Pod详细status尚未开始。
	g.updateRelistTime(timestamp)

	pods := kubecontainer.Pods(podList)
	updateRunningPodAndContainerMetrics(pods)
	g.podRecords.setCurrent(pods) // 保存本轮概要视图。

	// reconcile内部只有发现event或标记reinspect时才GetPodStatus/updateCache。
	for pid := range g.podRecords {
		g.reconcilePodRecord(ctx, pid)
	}

	// 所有Pod reconcile结束后推进cache全局观察时间并唤醒等待者。
	g.cache.UpdateTime(timestamp)
}
```

**大白话总结：** GenericPLEG 不是每秒给每个 Pod 都无条件调用详细 `GetPodStatus`。它每轮 `GetPods` 比较概要；只有状态事件或 reinspect 才查详细 status。更重要的是，Healthy时间戳在 GetPods 成功后就更新，早于逐Pod cache刷新。

**顺手学 Go：** `defer func(){...}()` 常用于无论成功失败都记录耗时。`for pid := range map` 只取 key，不取 value。`context.Background()` 创建根context，意味着这段当前不随调用方取消。

### 10.3 为什么要先更新 cache，再推进 podRecord 和投递 Event

源码：`pkg/kubelet/pleg/generic.go:350-400`，`reconcilePodRecord` 的 **连续摘录**。前面已比较 old/current 并生成 events。

```go
// 没有状态事件、也没被要求reinspect：本Pod不查详细status。
if len(events) == 0 && !reinspect {
	return
}

// 先向runtime取详细PodStatus；updateCache无论成功还是error都会尝试把status/error写podCache。
status, updated, err := g.updateCache(ctx, pod, pid)
if err != nil {
	// 默认仅GenericPLEG路径已保存本次error并可唤醒等待者；EventedPLEG并存时旧timestamp可能被cache拒绝。无论哪种都不能推进podRecord。
	g.logger.V(4).Info("PLEG: Ignoring events for pod", "pod", klog.KRef(pod.Namespace, pod.Name), "err", err)
	// 明确安排下一轮再次详细检查。
	g.podsToReinspect.Store(pid, empty)
	return
} else if utilfeature.DefaultFeatureGate.Enabled(features.EventedPLEG) {
	// EventedPLEG并存时，旧timestamp更新被cache拒绝就不发旧Event。
	if !updated {
		return
	}
}

if len(events) == 0 {
	// 纯reinspect也要产生PodSync，让消费者用新cache继续收敛。
	events = append(events, &PodLifecycleEvent{ID: pid, Type: PodSync})
}

// cache成功以后才把current概要升级成old基线。
g.podRecords.update(pid)

containerExitCode := make(map[string]int) // 供后面的Died日志查exit code。

for i := range events {
	// ContainerChanged当前不可靠且无消费者，直接过滤。
	if events[i].Type == ContainerChanged {
		continue
	}
	select {
	case g.eventChannel <- events[i]:
		// channel有空间，syncLoop稍后收到。
	default:
		// cache和podRecord已经前进；满channel只丢Event，不回滚状态。
		metrics.PLEGDiscardEvents.Inc()
		g.logger.Error(nil, "Event channel is full, discard this relist() cycle event")
	}
```

`updateCache` 函数尾部的真实返回语句揭示了 error分支仍会写 cache：

```go
// status可以为空、err可以非nil；cache.Set仍尝试保存，EventedPLEG并存时可能按timestamp拒绝旧结果。
return status, g.cache.Set(pod.ID, status, err, timestamp), err
```

**大白话总结：** 正常顺序是“详细status写入cache -> podRecord前进 -> Event投递”。若 `GetPodStatus` 失败，默认 GenericPLEG-only路径会把 error写入 podCache并可能唤醒 `GetNewerThan` 等待者，pod worker随后看到 error并进入 FailedSync/重试；启用 EventedPLEG并存在更新鲜timestamp时，这次 Generic状态/error可能被 cache拒绝。两种情况下 PLEG都不会推进 podRecord、不会投递该 lifecycle Event，还会安排下一轮 reinspect。若 Event channel满，cache和record已经前进，原Event通常不会原样重发，只能依赖cache等待者、周期sync或其他更新继续收敛。

**顺手学 Go：** `return status, g.cache.Set(...), err` 会先求值函数调用，再一次返回三个值；即使第三个值是非 nil error，第二个位置的 `cache.Set` 也已经执行。`select { case ch <- value: default: }` 是非阻塞发送；channel满就走 default。它与 result manager 的阻塞发送不同。

### 10.4 pod worker 为什么等 `GetNewerThan`，成功动作后还可请求按 Pod relist

源码：`pkg/kubelet/container/cache.go:108-111` 与 `pkg/kubelet/pleg/generic.go:573-582`，两个文件的 **非连续检查点**；代码块不可独立编译。

```go
// pod worker要求返回的status观察时间新于上一轮lastSyncTime；不满足就阻塞等待通知。
func (c *cache) GetNewerThan(id types.UID, minTime time.Time) (*PodStatus, error) { // 函数契约要求指定Pod的新鲜status。
	ch := c.subscribe(id, minTime) // 注册一次按UID和最小时间过滤的订阅。
	d := <-ch // channel未收到满足条件的数据前，当前goroutine阻塞。
	return d.status, d.err // 同时返回status与观察错误。
}

// 当前PLEGOnDemandRelist为Beta、默认开启；关闭时请求直接no-op。
func (g *GenericPLEG) RequestRelist(podUID types.UID) { // 请求只针对一个Pod UID。
	if !utilfeature.DefaultFeatureGate.Enabled(features.PLEGOnDemandRelist) { // feature关闭时不启用按Pod加速路径。
		return // no-op，不影响周期性全局Relist。
	}

	select { // 非阻塞尝试写入有界请求channel。
	case g.relistRequests <- relistRequest{podUID, time.Now()}: // payload包含UID和请求时间。
		// 请求进入容量200的按Pod队列。
	default: // channel满时立即走降级分支。
		g.logger.Error(nil, "Relist request channel full; dropping relist request", "podUID", podUID) // 丢请求但保留诊断日志。
	}
}
```

**大白话总结：** 第 12 课的成功 runtime action会让 `postSync` 调用 `RequestPodRelist`；当前默认 gate下可进入单 Pod relist，不必只等下一次全局周期。但请求队列也会满、gate也可能关闭；最终仍要保留周期与其他事件作为收敛路径。

**顺手学 Go：** `<-ch` 单独出现在表达式右侧表示阻塞接收。`relistRequest{podUID, time.Now()}` 是位置式结构体字面量，必须按定义顺序理解；跨包教学更推荐先查 struct 字段。

### 10.5 PLEG `Healthy=true` 到底只证明什么

源码：`pkg/kubelet/pleg/generic.go:238-249`，`GenericPLEG.Healthy` **完整函数，教学注释版**。

```go
func (g *GenericPLEG) Healthy() (bool, error) { // 同时返回健康bool和不健康原因。
	// 读取最近一次GetPods成功后写入的relistTime。
	relistTime := g.getRelistTime() // atomic.Value读取最近全局观察时间。
	if relistTime.IsZero() { // 零值表示PLEG从未完成成功GetPods。
		return false, fmt.Errorf("pleg has yet to be successful") // 返回明确启动期错误。
	}
	// 指标暴露这个时间戳，而不是每个Pod的详细cache时间。
	metrics.PLEGLastSeen.Set(float64(relistTime.Unix())) // gauge写Unix秒供外部告警。
	elapsed := g.clock.Since(relistTime) // 用可注入clock计算已经过去多久。
	if elapsed > g.relistDuration.RelistThreshold { // 超过节点配置阈值才判不健康。
		return false, fmt.Errorf("pleg was last seen active %v ago; threshold is %v", elapsed, g.relistDuration.RelistThreshold) // 错误同时带实际值和阈值。
	}
	return true, nil // 最近GetPods仍在阈值内。
}
```

**大白话总结：** Healthy=true只支持“最近全局 GetPods 成功且时间未超阈值”。它不能证明 `game-api` 的 `GetPodStatus` 成功、podCache刚刷新、Event已投递或channel没有丢事件。单 Pod详细status持续失败时，PLEG仍可能整体Healthy。

**顺手学 Go：** `(bool, error)` 两个返回位置允许 `false,error` 表达不健康原因，`true,nil` 表达当前检查通过。`float64(relistTime.Unix())` 是显式数值转换，用于写 Prometheus gauge。

### 10.6 PLEG unhealthy 与 EventedPLEG 的二遍边界

Kubelet把 GenericPLEG health 加入 `runtimeState`。当 `runtimeErrors()` 包含 PLEG unhealthy 时，主 syncLoop会短退避并跳过正常 Pod synchronization，因此它是 Node级问题，不是某个 Java readiness URL慢。

当前固定提交：

```text
EventedPLEG：Alpha，默认关闭
PLEGOnDemandRelist：Beta，默认开启
```

启用 EventedPLEG 时，GenericPLEG仍保留较低频率的 fallback（兜底）/校验；cache还会按 timestamp拒绝旧状态。首遍不要把 GenericPLEG 1秒周期、Evented stream 或发行版默认值写成跨版本常量。

## 11. 第六层源码：statusManager 为什么先改本地，再异步写 API

### 11.1 相同状态不生成新版本，channel只是一只门铃

源码：`pkg/kubelet/status/status_manager.go:969-999`，`updateStatusInternal` 尾部的 **连续摘录**。前面已检查非法状态转换、设置 transition time 并 normalize status。

```go
// 本地status与缓存语义相同且未force：正常no-op，不发API更新。
if isCached && isPodStatusByKubeletEqual(&cachedStatus.status, &status) && !forceUpdate { // 三个条件共同确认无需创建新版本。
	logger.V(3).Info("Ignoring same status for pod", "pod", klog.KObj(pod), "status", status) // 记录被去重的status。
	return false, nil // false表示本地语义未变化。
}

// 新版本保存完整PodStatus与本地递增version。
newStatus := versionedPodStatus{ // 组装statusManager自己的版本包装对象。
	status:        status, // 保存完整API PodStatus值。
	version:       cachedStatus.version + 1, // 在旧本地版本上加一。
	podName:       pod.Name, // 保存后续GET/Patch使用的name。
	podNamespace:  pod.Namespace, // 保存后续API调用使用的namespace。
	podIsFinished: podIsFinished, // 保存终态标记供删除判断使用。
}

// 多次本地变化在API写入前合并时，沿用第一笔待同步时间。
if cachedStatus.at.IsZero() { // 当前没有尚未同步的起始时间。
	newStatus.at = time.Now() // 第一笔变化记录当前时间。
} else { // API账还没追上旧本地版本。
	newStatus.at = cachedStatus.at // 沿用最早待同步时间，不重置计时。
}

// payload先落map；channel不承载完整status。
m.podStatuses[pod.UID] = newStatus // UID key下覆盖为最新完整本地版本。

// doorbell已有待处理通知时不阻塞，也不重复塞次数。
select { // 非阻塞尝试敲一次门铃。
case m.podStatusChannel <- struct{}{}: // 空struct只表达“有工作”，不携带status。
default: // 已有门铃或channel不可写时直接返回。
}
```

**大白话总结：** statusManager 的真数据在 `podStatuses` map；channel只说“有新版本待同步”。十次快速 Ready抖动可以合并成最新本地版本，不需要十个 API Patch。它与 probe result channel的payload/阻塞语义不同。

**顺手学 Go：** `struct{}{}` 是零字段空结构体，常用作只表达信号的值。`cachedStatus.version + 1` 是本地逻辑版本，不是 apiserver `resourceVersion`。`IsZero()` 在这里判断是否已经有一笔待同步开始时间。

### 11.2 为什么即时通知和周期对账必须由同一个 goroutine执行

源码：`pkg/kubelet/status/status_manager.go:267-295`，`manager.Start` **完整函数，教学注释版**。

```go
func (m *manager) Start(ctx context.Context) { // 启动唯一的status API同步循环。
	logger := klog.FromContext(ctx) // 从context取得本组件logger。
	// 没有kube client的特殊kubelet不启动API status同步器。
	if m.kubeClient == nil { // nil client无法访问apiserver。
		logger.Info("Kubernetes client is nil, not starting status manager") // 明确记录为何不启动。
		return // 避免后续nil pointer调用。
	}

	logger.Info("Starting to sync pod status with apiserver") // 记录同步器生命周期开始。

	// 周期ticker用于全量对账；Start只调用一次。
	syncTicker := time.NewTicker(syncPeriod).C // 这里只保留ticker的只读时间channel。

	// syncBatch即时和周期路径共用一个goroutine，避免彼此竞态。
	go wait.Forever(func() { // 新goroutine长期运行该匿名函数。
		for { // 内层无限循环持续等待两类信号。
			select { // 同一个goroutine复用同一批处理函数。
			case <-m.podStatusChannel: // 收到本地变化doorbell。
				logger.V(4).Info("Syncing updated statuses") // 记录即时同步原因。
				m.syncBatch(ctx, false) // 只选本地version领先API账的Pod。
			case <-syncTicker: // 周期时间到时执行全量对账。
				logger.V(4).Info("Syncing all statuses") // 记录周期同步原因。
				m.syncBatch(ctx, true) // 周期检查更新、错位与删除状态。
			}
		}
	}, 0) // 零间隔表示函数返回后立即开始下一轮Forever调用。
}
```

**大白话总结：** API 短暂失败不会丢掉本地最新 status：即时通知失败后，下一次其他 Pod通知或周期全量对账还会再试。单 goroutine串行执行 syncBatch，避免即时与周期线程同时 Patch同一状态账。

**顺手学 Go：** `time.NewTicker(syncPeriod).C` 直接取得只接收时间值的 channel。`go wait.Forever(func(){...}, 0)` 把匿名函数交给循环工具并发运行；第二个参数是崩溃后重启间隔，不是 status sync period。

### 11.3 写 API 前为什么必须重新 GET 并核对 UID

源码：`pkg/kubelet/status/status_manager.go:1151-1207`，`syncPod` 的 **非连续检查点**。删除处理与部分日志保留，代码块只展示 GET、UID保护、Patch和版本推进；不可独立编译。

```go
// 按本地缓存的namespace/name读取当前API对象。
pod, err := m.kubeClient.CoreV1().Pods(status.podNamespace).Get(ctx, status.podName, metav1.GetOptions{}) // 先拿API最新对象和UID。
if errors.IsNotFound(err) { // name在API中已经不存在。
	// 对象已不存在：等待orphan清理，不把旧status写给任何新对象。
	return // 结束本Pod本轮同步。
}
if err != nil { // 其他读取错误也不能安全Patch。
	logger.Error(err, "Failed to get status for pod", // 记录API读取失败。
		"podUID", uid, // 带本地账UID。
		"pod", klog.KRef(status.podNamespace, status.podName)) // 带namespace/name。
	return // 保留版本差，等待后续批次重试。
}

// static/mirror转换后比较真正UID；同名重建时拒绝旧status。
translatedUID := m.podManager.TranslatePodUID(pod.UID) // 把mirror UID翻译到可比较身份。
if len(translatedUID) > 0 && translatedUID != kubetypes.ResolvedPodUID(uid) { // 同名API对象已经不是本地那一个Pod。
	logger.V(2).Info("Pod was deleted and then recreated, skipping status update", // 记录拒绝旧status原因。
		"pod", klog.KObj(pod), // 当前API对象。
		"oldPodUID", uid, // 本地旧status归属UID。
		"podUID", translatedUID) // 当前API对象翻译后的UID。
	m.deletePodStatus(uid) // 删除已失效的本地旧UID账。
	return // 绝不把旧status写给新对象。
}

// 合并API已有字段与kubelet拥有的本地status，再发/status Patch。
mergedStatus := mergePodStatus(pod, pod.Status, status.status, m.podDeletionSafety.PodCouldHaveRunningContainers(pod)) // 按字段所有权合并两边状态。
newPod, patchBytes, unchanged, err := statusutil.PatchPodStatus(ctx, m.kubeClient, pod.Namespace, pod.Name, pod.UID, pod.Status, mergedStatus) // Patch时再次携带当前UID。
logger.V(3).Info("Patch status for pod", "pod", klog.KObj(pod), "podUID", uid, "patch", string(patchBytes)) // 详细日志保留patch内容。

if err != nil { // API未接受Patch时不能推进版本账。
	// Patch失败不推进apiStatusVersions，后续批次继续重试。
	logger.Error(err, "Failed to update status for pod", "pod", klog.KObj(pod)) // 记录失败对象和error。
	return // 留下本地领先状态供后续重试。
}
if !unchanged { // Patch真实改变了API对象。
	pod = newPod // 使用API返回的新resourceVersion对象。
	m.podStartupLatencyHelper.RecordStatusUpdated(pod) // 更新启动延迟跟踪器。
}

// 只有成功/unchanged后才把API账推进到本地version。
m.apiStatusVersions[kubetypes.MirrorPodUID(pod.UID)] = status.version // 记录控制面已经追到哪个本地版本。
```

**大白话总结：** statusManager不能凭 name把旧 `game-api-new-x` 的状态写到同名新 UID。GET/Patch失败时本地cache仍保留，API版本账不推进；所以 kubectl可以落后，但节点主链不需要同步阻塞等待这次 Patch成功。

**顺手学 Go：** `errors.IsNotFound(err)` 按 Kubernetes API error类型判断，不靠字符串。`newPod, patchBytes, unchanged, err :=` 一次接四个返回值。`if !unchanged` 中 `!` 是布尔取反。

### 11.4 `kubelet_pod_status_sync_duration_seconds` 为什么不能给单 Pod 精确定时

指标注释想表达“本地变化到API成功”的传播耗时，但固定提交有重要实现边界：

```text
versionedPodStatus.at
  -> 第一笔待同步变化时设置
  -> 后续本地变化沿用旧at
  -> 成功sync后没有在该对象上清零
  -> API失败尝试不Observe
```

因此该 Alpha 指标只能作为 Node级成功同步样本的辅助趋势，不能把某一个 bucket样本直接解释为“本次 game-api readiness用了X秒到API”；持续API故障期间还可能暂时没有新样本。单 Pod因果必须用 Condition变化时间、kubelet日志、API `resourceVersion`（只能排序的对象版本号）、watch（持续接收对象变化）与多时钟边界交叉验证。

<a id="ch13-java-loop"></a>

## 12. 回到同一个 Java 现场：把四个时刻闭成一条因果链

### 12.1 冷启动：Running 先于 Started，Started 先于 Ready

```text
第12课 StartContainer(game-001) 成功
  -> runtime/PLEG看到Running
  -> phase可为Running
  -> startup cache尚未Success
  -> started=false
  -> readiness/liveness真实probe被门控
  -> ready=false

约45秒后startup普通Success达到threshold=1
  -> startup cache=Success
  -> startup worker对当前ID onHold
  -> syncLoop SetContainerStartup(true) + HandlePodSyncs
  -> readiness/liveness获得执行资格
  -> readiness连续2次Success后ready=true
```

`5×24=120秒`只是配置预算的常用估算。若 worker尚未执行、probe自身耗时、kubelet刚重启或状态传播延迟，不能拿墙钟到120秒就机械断言源码已经命中 failureThreshold。

### 12.2 数据库故障：先摘流量，不动container ID

```text
game-001已经Ready
  -> 第一次readiness 503
     -> Unhealthy Event可见
     -> resultRun=1<2
     -> stable cache仍Success
     -> API/EndpointSlice仍可能暂时ready

  -> 连续第二次普通Failure
     -> readiness cache从Success变Failure
     -> SetContainerReadiness(false)或后续SyncPod更新本地status
     -> Pod Ready=False
     -> EndpointSlice ready/serving=false

保持不变：
  game-001仍Running / started=true
  restartCount=0
  Pod UID / Pod IP / Sandbox不变
  jmx-001不变
```

数据库恢复后必须连续达到 `successThreshold=2` 才把 stable cache改回 Success。readiness适合表达可逆依赖与过载；把外部数据库直接放进 liveness，可能在数据库故障时把所有 JVM一起重启，形成级联事故。

### 12.3 JVM 死锁：liveness发布事实，runtime manager执行独立重启

```text
第三次普通liveness Failure达到threshold
  -> liveness cache=Failure
  -> syncLoop / pod worker
  -> computePodActions
     ContainersToKill[game-001]
     ContainersToStart[game-api index]
  -> Killing Event: failed liveness probe, will be restarted
  -> PreStop/termination grace
  -> CRI StopContainer(game-001)
  -> Create/Start game-002
  -> worker发现新ID，删除旧probe结果并种新初值
  -> PLEG更新podCache并投递Died/Started等event
  -> statusManager异步Patch API
```

局部重启不要求删除：

```text
Pod UID
Sandbox attempt=0
PodIP=192.0.2.41
jmx-exporter ID=jmx-001
```

这延续了第 12 课的设计：健康 Pod级环境和其他普通 container 是应该保留的成果。

### 12.4 哪些证据会推翻“这次重启由 liveness 导致”

下面任一事实都要求重新判断：

- `Killing` Event message 是 spec changed、startup probe、Pod deletion或其他 reason，不是 liveness；
- kubelet日志显示 `StopContainer` 失败，随后并没有新ID；
- Pod UID改变，说明controller重建了Pod，不是同Pod内container restart；
- `game-001` 已先因 OOM/System.exit退出，PLEG先观察到Died，liveness只是附近的旧Event；
- 新ID时间早于 liveness failure观察窗；
- Event来自其他 Node或同名旧 UID；
- runtime GC已删除旧ID，无法把当前 `lastState` 当完整历史。

生产上最强的闭环是：

```text
同UID/同Node
  + liveness Unhealthy
  + Killing message明确failed liveness probe
  + kubelet发起StopContainer旧ID
  + CRI旧ID退出、新ID启动
  + API containerID变化/restartCount推进
```

其中每一层仍有自己的时钟与保留期限，不能要求所有时间戳完全相等。

<a id="ch13-counterfactual"></a>

## 13. 反事实推演：只改一个条件，源码会走向哪里

真正掌握一条源码链，不是只能复述主案，而是能回答：**如果只改变一个前提，哪一个分支会先变，后面的证据又会怎样变化？**

### 13.1 只有第一次 readiness Failure

假设 `game-api` 已 Ready，readiness 配置仍是：

```yaml
periodSeconds: 5
failureThreshold: 2
successThreshold: 2
```

第一次普通 Failure 会发生两件容易混淆的事：

1. prober 可以记录一次 `Unhealthy` Event；
2. worker 把 `resultRun` 记为 1，但因为 `1 < failureThreshold`，不改稳定 result cache。

因此此刻完全可能同时成立：

```text
最近一次HTTP探测 = Failure
Unhealthy Event已经出现
稳定readiness cache = Success
container Ready = true
Pod Ready = true
EndpointSlice ready = true
container ID = game-001
```

这不是状态冲突，而是两个时间尺度：Event 在描述“这一枪打中了什么”，稳定 cache 在描述“是否已经连续失败到足以改变控制结论”。

### 13.2 probe 执行 error，而不是普通 Failure

如果 kubelet 连请求都没能按 probe 契约执行完，例如 exec 调用链本身报错，`worker.doProbe` 会在 `err != nil` 分支丢弃这次 result：

```text
执行error
  -> 不进入Success/Failure/Unknown指标分支
  -> 不推进resultRun
  -> 不更新稳定result cache
  -> 不直接改变Ready或触发restart action
```

所以排障时不能把日志里的“probe error”机械换算成一次 failureThreshold 计数。还要注意当前源码里的另一种情况：prober 已正常返回 `probe.Unknown` 且没有 Go error 时，HTTP/TCP/gRPC 结果映射可能把它当作 Failure；**“业务结果 Unknown”与“执行函数返回 error”不是一回事。**

### 13.3 kubelet 自己重启，但 `game-001` 一直没重启

这时 kubelet 内存里的 worker 和 result manager 会重建，可 CRI 里的旧 JVM 仍然运行。当前基线下，`ChangeContainerStatusOnKubeletRestart` 默认关闭，worker 会用 container start time 判断它是不是 kubelet重启前就存在的进程。

因此不能简单推导：

```text
kubelet第一次看见game-001
  = game-001刚刚创建
  = 三种probe一定重新种默认初值
```

正确问题是：

- `game-001` 的 `StartedAt` 是否早于 kubelet本次启动宽限边界；
- API 中旧的 `started` 值是否可能被兼容沿用；
- feature gate 是否改变了重启后的状态处理；
- kubelet重启窗口内 API、内存 cache 与新 probe 结果是否尚未收敛。

这也是为什么线上必须同时看 kubelet启动时间和 container `StartedAt`，不能只看 `restartCount`。

### 13.4 `restartPolicy=Never`，只有一个业务 container

如果把主案改成单 container Pod，并将 `restartPolicy` 改成 `Never`：

- liveness Failure 仍可以使 `computePodActions` 判断旧 container 应被 kill；
- 但策略不允许把它重新 start；
- 若没有其他需要保留或启动的 container，计算结果还可能升级到 `KillPod=true`，结束整个 Pod sandbox。

若仍保留 `jmx-exporter`：

- 是否保留 sandbox，取决于本轮完整 action 计算；
- 不能只截取 `ContainersToKill[game-api]` 一行，就断言“Pod 一定不动”。

所以 `restartPolicy` 不是 CRI 的重启开关，而是 kubelet在**观察当前所有 container 状态后**计算下一步 desired state 的输入之一。

### 13.5 JVM 先 `System.exit(1)` 或被 OOM kill，没有等到 liveness

这条链不需要 probe 才能启动：

```text
JVM退出
  -> CRI中的container变成Exited
  -> PLEG比较新旧runtime状态
  -> podCache写入新的PodStatus
  -> ContainerDied等PLEG event唤醒SyncPod
  -> computePodActions按restartPolicy决定是否重启
  -> 若短时间反复退出，doBackOff开始生效
```

此时附近即便存在旧的 `Unhealthy` Event，也不能证明 liveness 是退出原因。优先看：

- terminated reason、exit code、signal 与 OOM 证据；
- PLEG / runtime 观察到退出的时间；
- `Killing` Event 的 message；
- kubelet是否主动发起了 `StopContainer`。

### 13.6 Pod Ready=false，却仍然有请求进入

通常 Service controller会把 EndpointSlice 中该 endpoint 的 `ready/serving` 条件翻为 false，常规流量代理据此停止转发。但若 Service 设置了 `publishNotReadyAddresses: true`，控制面会按该特殊契约发布尚未 Ready 的地址；客户端也可能有连接池、DNS缓存、长连接或绕过 Service 直连 Pod IP。

所以：

```text
Pod Ready=false
```

只能证明 kubelet发布的就绪结论，不足以单独证明“线上已经没有任何请求”。必须继续核对 Service 配置、EndpointSlice 条件、数据面规则和应用连接行为。

### 13.7 PLEG `Healthy=true`，但某个 Pod status仍然旧

`Healthy()` 使用的是最近一次全局 `GetPods` 成功后写入的 relist time。它不保证：

- 该轮每个变化 Pod 的 `GetPodStatus` 都成功；
- 每个 Pod 都产生了 PLEG event；
- event channel没有满；
- statusManager 已经写入 apiserver。

因此下面两件事可以同时发生：

```text
kubelet健康检查认为PLEG最近活跃
某个game-api的详细runtime status因GetPodStatus失败而等待下轮reinspect
```

节点级告警用于发现“全局观察循环停滞”，单 Pod 排障仍要沿 pod UID 看详细 status/cache/reinspect 日志。

### 13.8 PLEG event channel满了

当前 GenericPLEG 的顺序是：先成功更新详细 pod cache，再推进 `podRecords`，最后用带 `default` 的 `select` 投递 event。channel 满时本轮 event 会被丢弃并增加 `pleg_discard_events`，不会为了等消费者而阻塞整个 relist。

这项取舍保护了节点观察循环，却带来一个重要结论：

```text
没有收到某条PLEG event
  != runtime状态一定没变
  != podCache一定没更新
```

周期性 SyncPod、其他来源的更新和 cache 对账承担了恢复收敛的责任。Event 是加速信号，不是唯一事实库。

### 13.9 本地 Ready 已变，apiserver Patch失败

statusManager 先在本地 status cache 生成新版本，再异步写 API。若网络、鉴权、apiserver或资源版本冲突使这次写入失败：

```text
本地desired status = Ready false
apiserver仍可能暂时显示Ready true
EndpointSlice也可能暂时未翻转
```

status sync goroutine会在后续通知或周期同步中重试收敛。这里选择的是“节点控制不能同步阻塞在控制面写入上”，代价是控制面观察存在传播窗口。

### 13.10 `game-002` 启动后立刻退出

第一次 liveness kill 本身通常只是把 `game-001` 换成 `game-002`。如果 `game-002` 又很快 Exited，下一轮 runtime status 才会提供 terminated 状态；`computePodActions` 随后调用 `doBackOff`，它只根据退出实例、结束时间、Pod/container backoff key与 backoff状态判断是否应退避，**不读取 reasonCache**。

因此时序是：

```text
liveness让旧ID进入kill/start链
  -> 新ID真的启动
  -> 新ID再次退出
  -> PLEG/runtime status观察到Exited
  -> 第一次通常先backOff.Next并允许再启动
  -> 后续实例继续快速退出才可能返回BackoffError
  -> reasonCache记录StartContainer SyncResult失败
  -> 再下一轮API status才可能显示CrashLoopBackOff
```

“第三次 liveness Failure”与“CrashLoopBackOff”之间没有一条立即赋值语句。第 14 课之外的后续 backoff 专题可以继续沿这条链深入。

### 13.11 `restartCount` 与 `lastState` 没保留完整历史

这两个字段非常有用，但它们是 kubelet根据仍可取得的 runtime 状态做出的 best-effort 汇总。以下动作都可能缩短证据链：

- container runtime回收旧 container metadata；
- kubelet或节点重启后内存状态重建；
- Pod被删除并以新 UID重建；
- 日志轮转或 previous log只保留最近一次实例。

所以事故复盘不能把 `restartCount=0` 当作“历史上绝对从未重启”，也不能把 `lastState` 当成完整审计日志。长期证据要由日志、指标、Event采集和外部可观测系统承担。

## 14. 一张语义总表：看到一个字段时，先问它属于哪本账

| 观察对象 | 谁产生 | key / 身份边界 | 何时改变 | 直接控制效果 | 最容易犯的错 |
| --- | --- | --- | --- | --- | --- |
| CRI container Running/Exited | container runtime | runtime container ID | 进程真实启动或退出 | 给 kubelet提供事实 | 把 Running 当 Ready |
| API `state.running` | kubelet根据 runtime status转换 | Pod UID + container name + ID | status生成并同步后 | 展示 runtime运行态 | 把它当 startup已成功 |
| API `started` | startup result + `isContainerStarted` | 当前 container ID | startup成功，或无 startup 时 Running | 决定 readiness/liveness是否开始探测 | 把它当 CRI Start成功时间 |
| 单次 `Unhealthy` Event | prober | Pod UID + probe type + container | 显式普通 Failure，或probe执行error；无error的Unknown当前只记日志并折成Failure | 只提供一次探测事实/执行异常 | 看到一条就认定 threshold已到 |
| 稳定 probe result cache | results manager | container ID | 连续次数达到阈值后 | readiness更新或唤醒 SyncPod | 忽略 threshold 与新ID重置 |
| container `ready` | prober/statusManager/SyncPod | Pod UID + container ID | readiness稳定结果变化或状态重算 | 参与 Pod Ready condition | 误以为 false 会 kill |
| Pod `Ready` condition | kubelet status生成逻辑 | Pod UID | 所有门控条件重新计算 | 表达 Pod流量资格 | 与 `phase=Running` 混为一谈 |
| EndpointSlice conditions | endpoint controller | Service + targetRef UID/IP | 控制面观察 Pod/Service 后更新 | 供 kube-proxy/数据面消费 | 只看 Pod，不核对 Service特殊配置 |
| PLEG event | Generic/Evented PLEG | Pod UID + runtime container ID | runtime新旧状态有事件差异 | 唤醒 kubelet同步 | 当成不可丢的审计日志 |
| pod cache status/error | PLEG/runtime | Pod UID + cache timestamp | 详细 `GetPodStatus` 后尝试写入status或error；EventedPLEG可按timestamp拒绝旧结果；Pod从runtime消失时删除 | 给 pod worker提供已观察事实或失败 | 以为只有成功才更新cache，或PLEG每轮都查每个Pod详情 |
| statusManager local version | statusManager | Pod UID + version | 本地 status语义变化 | 标记待同步状态 | 把 doorbell channel当完整队列 |
| apiserver PodStatus | statusManager Patch | namespace/name + UID校验 | API写成功 | 控制面可见并驱动下游 | 认为与本地状态零延迟一致 |
| `restartCount` / `lastState` | kubelet汇总 runtime可见历史 | 当前 Pod UID + container name | status重建/重启后 | 辅助排障 | 当成永久、完整审计记录 |

这张表的使用方法不是背字段，而是每次都问四句话：

1. 这条证据描述的是**探测事实、稳定控制结论、runtime事实，还是 API副本**？
2. 它以 Pod UID、container name，还是 container ID作为身份？
3. 它是状态库、唤醒信号，还是短期历史？
4. 从它到下一个组件，中间是否还有异步队列、阈值或重新计算？

<a id="ch13-evidence"></a>

## 15. 生产取证：先建立身份，再按因果顺序看证据

下面命令是**读模型后的取证工具**，不是本章的开场白。示例只执行只读查询；在生产节点上不要用 `crictl stop/rm` 验证猜想。

### 15.1 第一步：锁定 Pod UID、Node、IP 和当前 container ID

```bash
NS=prod
POD=game-api-new-x

UID_NOW=$(kubectl get pod -n "$NS" "$POD" -o jsonpath='{.metadata.uid}')
NODE_NOW=$(kubectl get pod -n "$NS" "$POD" -o jsonpath='{.spec.nodeName}')
APP_ID_OLD_API=$(kubectl get pod -n "$NS" "$POD" \
  -o jsonpath='{.status.containerStatuses[?(@.name=="game-api")].containerID}')
OLD_RUNTIME_ID=${APP_ID_OLD_API#*://}

kubectl get pod -n "$NS" "$POD" -o json \
| jq '{
    uid: .metadata.uid,
    node: .spec.nodeName,
    phase: .status.phase,
    podIP: .status.podIP,
    conditions: [.status.conditions[] | {
      type, status, reason, lastTransitionTime
    }],
    containers: [.status.containerStatuses[] | {
      name, containerID, ready, started, restartCount, state, lastState
    }]
  }'

printf 'uid=%s\nnode=%s\noldRuntimeID=%s\n' \
  "$UID_NOW" "$NODE_NOW" "$OLD_RUNTIME_ID"
```

先保存 UID，而不是只复制 Pod name；还要在重启发生前保存 API中真实的旧 container ID并去掉 `containerd://` scheme。Deployment滚动发布可能很快产生另一个相似名字；同名 Pod在极端情况下也可能已经换 UID。后面的 Event、EndpointSlice、节点日志和 CRI查询都要回到这个 UID/旧ID。若事故发生后才开始取证且旧ID已经被覆盖或GC，应明确写“旧ID未在事前保存”，再从 `lastState`、Event、kubelet/runtime日志等补证，不能虚构完整链。

### 15.2 第二步：区分单次 `Unhealthy` 与真正进入 `Killing`

```bash
kubectl get events -n "$NS" \
  --field-selector "involvedObject.uid=$UID_NOW" \
  --sort-by='.metadata.creationTimestamp' \
  -o custom-columns='CREATED:.metadata.creationTimestamp,EVENT_TIME:.eventTime,FIRST:.firstTimestamp,LAST:.lastTimestamp,COUNT:.count,TYPE:.type,REASON:.reason,REPORTER:.reportingComponent,INSTANCE:.reportingInstance,SOURCE_COMPONENT:.source.component,SOURCE_HOST:.source.host,MESSAGE:.message'
```

读 Event 时至少核对：

- `involvedObject.uid` 是不是当前 UID；
- reporter/source 是否指向 `worker-05` 上的 kubelet；
- `reason=Unhealthy` 只说明 probe事实，还是已经出现 `reason=Killing`；
- `Killing` 的 message 是 `failed liveness probe, will be restarted`，还是删除、spec变化、startup等其他原因；
- Event是否发生聚合，`count` 与 first/last timestamp 是否覆盖了多次同类事件。

这里按 Event对象创建时间排序，只是方便阅读，不等于每一次实际发生时间；聚合后的一个 Event对象可能覆盖多次探测，仍要结合 `count/first/last/eventTime`。Event有保留期、聚合和写入失败边界。没有查到 Event 不能反向证明事情没发生；查到 `Killing` 也只证明 kubelet进入了 kill 路径，不证明 CRI stop已经成功。

### 15.3 第三步：确认 EndpointSlice 是否对这个 UID翻转了流量条件

```bash
SERVICE=game-api

kubectl get endpointslice -n "$NS" \
  -l "kubernetes.io/service-name=$SERVICE" -o json \
| jq --arg uid "$UID_NOW" '
    .items[]
    | .metadata.name as $slice
    | .endpoints[]
    | select(.targetRef.uid == $uid)
    | {slice: $slice, addresses, conditions, nodeName, targetRef}'
```

如果 Pod Ready=false，而对应 endpoint仍被发布或仍接到流量，继续核对：

- Service 是否设置 `publishNotReadyAddresses: true`；
- 业务是否使用 Pod IP、headless DNS或自维护服务发现；
- 旧长连接是否仍存在；
- 数据面规则是否已完成同步。

### 15.4 第四步：到目标 Node 上找主动 kill 与 runtime事实

先在控制端取得 API当前的新 ID。若尚未重启，它会与旧 ID相同；只有新 ID已经出现，才继续做新旧实例闭环：

```bash
APP_ID_NEW_API=$(kubectl get pod -n "$NS" "$POD" \
  -o jsonpath='{.status.containerStatuses[?(@.name=="game-api")].containerID}')
NEW_RUNTIME_ID=${APP_ID_NEW_API#*://}

printf 'uid=%s\nnode=%s\nold=%s\nnew=%s\n' \
  "$UID_NOW" "$NODE_NOW" "$OLD_RUNTIME_ID" "$NEW_RUNTIME_ID"
```

不要假定控制端变量会自动出现在 SSH后的节点 shell。下面让控制端变量直接参与远程只读命令，并先用 CRI的 `io.kubernetes.pod.uid` label列出属于该 UID的 sandbox；从输出中确认 namespace/name、状态和 attempt，再粘贴当前 READY sandbox的真实 ID：

```bash
ssh "$NODE_NOW" sudo crictl pods -a \
  --label "io.kubernetes.pod.uid=$UID_NOW"

read -r -p '粘贴上面当前READY sandbox的真实ID: ' SANDBOX_ID

ssh "$NODE_NOW" sudo crictl ps -a --pod "$SANDBOX_ID"
ssh "$NODE_NOW" sudo crictl inspect "$OLD_RUNTIME_ID"
ssh "$NODE_NOW" sudo crictl inspect "$NEW_RUNTIME_ID"
```

再按同一 UID与真实新旧哈希过滤 kubelet日志。`grep -E` 比 `rg` 更常见；仍需确认目标节点是否安装相应工具：

```bash
ssh "$NODE_NOW" sudo journalctl -u kubelet \
  --since '30 minutes ago' --lines=5000 --no-pager \
| grep -E "$UID_NOW|$OLD_RUNTIME_ID|$NEW_RUNTIME_ID|Unhealthy|Killing|StopContainer|SyncPod"
```

真实 `crictl` container ID通常是不带 `containerd://` scheme 的长哈希。`game-001/game-002` 只是本章纸面追踪身份的教学短名，不能直接传给 `crictl inspect`。仅用 `crictl ps -a --name game-api` 也可能混入节点上其他 Pod，必须先用 Pod UID锁定 sandbox，再在该 sandbox内列 container。

最强证据顺序是：

```text
kubelet因liveness决定kill旧ID
  -> kubelet调用CRI StopContainer旧ID
  -> runtime报告旧ID Exited
  -> runtime创建并启动新ID
  -> PLEG/cache观察到变化
  -> API最终显示新ID
```

若只找到前半段，要明确写成“已经发起”而不是“已经成功”。日志级别、journald轮转和 runtime GC也会造成证据缺口。把 Event、probe output、journal或 `inspect` 内容贴进工单/聊天前，应先脱敏内部地址、镜像仓库、header、业务参数和可能的凭据片段。

### 15.5 第五步：应用日志只能回答应用层问题

```bash
kubectl logs -n "$NS" "$POD" -c game-api \
  --since=30m --tail=2000 --timestamps
kubectl logs -n "$NS" "$POD" -c game-api --previous \
  --since=30m --tail=2000 --timestamps
kubectl logs -n "$NS" "$POD" -c jmx-exporter \
  --since=30m --tail=1000 --timestamps
```

`--previous` 通常只能拿到当前 Pod/该 container name最近一个已终止实例的日志，而且依赖节点仍保留对应日志。它可以证明 JVM 在退出前做了什么，却不能单独证明 kubelet为何发起 Stop；因果原因仍要和 kubelet Event/日志及 CRI状态交叉验证。应用日志、Actuator输出和 probe message同样可能包含内部依赖地址或业务数据，对外转发前要脱敏。

### 15.6 第六步：指标用来发现模式，不替代单次因果链

probe 结果趋势：

```promql
sum by (probe_type, result, pod, pod_uid, container) (
  rate(prober_probe_total{
    namespace="prod",
    pod="game-api-new-x"
  }[5m])
)
```

当前源码中 `prober_probe_total` 是 BETA counter，标签包含 `probe_type/result/container/pod/namespace/pod_uid`。执行函数直接返回 Go error 的那次 probe在 worker中被丢弃，不进入这个计数；显式普通 Failure，以及无error的 Unknown/default被折成 Failure后，都会记入 `result="failed"`。kubelet重启、worker删除和短生命周期序列也会让单条时序在聚合查询里消失，counter更适合看模式，不替代Event与ID闭环。

probe duration：

```promql
histogram_quantile(
  0.99,
  sum by (le, probe_type) (
    rate(prober_probe_duration_seconds_bucket{namespace="prod"}[5m])
  )
)
```

该 histogram 是 ALPHA，而且当前 worker只对 Success 和默认/Unknown分支 Observe，普通 Failure分支没有 Observe。它不能被解释成“所有失败 probe 的耗时分布”。

PLEG 全局观察循环：

```promql
(time() - kubelet_pleg_last_seen_seconds)
and
(kubelet_pleg_last_seen_seconds > 0)
```

```promql
histogram_quantile(
  0.99,
  sum by (le, instance) (
    rate(kubelet_pleg_relist_duration_seconds_bucket[5m])
  )
)
```

这些 PLEG指标当前为 ALPHA。`pleg_last_seen_seconds` 反映全局 `GetPods` 最近成功后更新的 relist time，不是“所有 Pod 的详细 `GetPodStatus` 最近都成功”的时间。查询先排除启动期 gauge=0；真正告警还要容忍 Prometheus服务器时钟与 Node时钟偏差，以及 scrape/ingestion延迟，不能把一次负值或尖峰直接判成 PLEG停摆。

status同步延迟：

```promql
histogram_quantile(
  0.99,
  sum by (le, instance) (
    rate(kubelet_pod_status_sync_duration_seconds_bucket[5m])
  )
)
```

这个 ALPHA histogram没有 Pod标签。若同一 Pod在 API成功前发生多次本地 status变化，源码保留第一次生成时刻，最终成功时才 Observe；失败尝试本身不 Observe。它适合看节点/集群级传播模式，不适合精确回答“本次 readiness从 false 到 API可见用了多少毫秒”。

### 15.7 五类时间来源不要强行对齐到同一秒

| 时间来源 | 代表什么 | 常见偏差 |
| --- | --- | --- |
| probe worker本地时间 | worker何时开始/完成一次探测 | 调度、probe耗时、kubelet停顿 |
| kubelet Event字段 | reporter构造/聚合该Event时记录的时间信息 | 聚合、旧/新Event API字段差异、写入失败、apiserver延迟 |
| runtime container时间 | 进程 StartedAt/FinishedAt | runtime实现与节点时钟 |
| API对象与观察时间 | `lastTransitionTime`由kubelet按Node时钟生成；resourceVersion只能排序；watch接收时刻属于观察端时钟 | 没有通用字段直接给出本次PodStatus/EndpointSlice服务端成功写入墙钟；精确需求要结合watch或API audit（服务端审计日志） |
| Prometheus时间 | `time()`来自Prometheus服务器，PLEG gauge来自Node，再经过抓取入库 | server/Node clock skew、scrape interval、ingestion延迟 |

事故时间线应使用“先后约束 + 身份闭环”，例如旧 ID必须先被决定 kill，新 ID才能随后出现；不要用任意两个系统的时间戳相差 1～2 秒就武断判因。需要测量单次 API传播时，记录客户端 watch接收时刻并说明观察端时钟，或使用 API audit获得服务端证据；`resourceVersion`只能说明相对顺序，不能换算成毫秒耗时。

<a id="ch13-runbook"></a>

## 16. 运维决策表：从现场症状反查哪一段源码

| 现场症状 | 第一判断 | 先看什么 | 暂时不要下的结论 |
| --- | --- | --- | --- |
| `phase=Running`，`started=false`，`ready=false` | startup门尚未通过 | startup Event、worker日志、JVM冷启动耗时 | Java进程没启动 |
| 第一条 readiness `Unhealthy`，Ready仍 true | 可能未达阈值 | Event count/时间、probe spec、稳定结果变化 | statusManager坏了 |
| 同一 container ID，Ready从 true变 false | readiness摘流量 | EndpointSlice、Service配置、依赖健康 | kubelet重启了container |
| 同一 Pod UID/IP，业务 ID变化，sidecar ID不变 | 同 Pod内局部重启 | Killing reason、StopContainer、CRI新旧ID | Deployment重建了Pod |
| Pod UID变化 | controller创建了新 Pod | ReplicaSet/Deployment Event与revision | 只是 liveness restart |
| JVM exit code非零，没有 liveness Killing | runtime主动观察链 | terminated reason、PLEG、restartPolicy | probe主动杀死了它 |
| 新 ID反复快速退出并出现 backoff | 后续 SyncPod进入退避 | terminated时间、reason cache、`doBackOff` | 第一次 liveness Failure直接赋值CrashLoopBackOff |
| 同一节点大量 Pod状态停滞，PLEG last seen变旧 | 节点级 runtime/PLEG风险 | CRI延迟、relist指标、kubelet健康 | 每个 Java应用同时坏了 |
| PLEG健康，但单 Pod状态旧 | 可能是 per-Pod详情或API传播问题 | `GetPodStatus`/reinspect、cache、status sync | PLEG健康等于该Pod全链成功 |
| Ready=false仍有流量 | 服务发布/数据面/连接行为需核对 | EndpointSlice、`publishNotReadyAddresses`、长连接 | kubelet Ready无效 |

一个可复用的值班表达模板是：

> 当前确认的是哪本账、哪个 UID/ID、到哪一步成功；尚未确认的是哪个异步边界；下一条证据能够排除哪一个竞争解释。

例如：

> 已确认 `prod/game-api-new-x` 当前 UID未变，`game-001` 收到 liveness `Unhealthy` 并出现明确的 `Killing` 原因；尚未确认 CRI stop是否成功。下一步在 `worker-05` 核对旧 ID的 runtime state及新 ID创建记录，以排除“只发起 kill但 runtime调用失败”。

## 17. 最小练习：只验证两个不变量，不搭一个巨型实验平台

### 17.1 首选：在已有非生产 Java Pod上做只读取证

选择一个允许观察、不会承载真实业务流量的 Java Pod，完成两张表：

1. **身份表**：namespace/name、UID、Node、PodIP、sandbox ID、各 container ID；
2. **四时刻表**：startup前、Ready后、readiness失败、一次允许的重启后，分别记录 Started/Ready/ID/restartCount/EndpointSlice。

如果环境不允许注入故障，只观察一次自然发布也可以。练习目标不是一定制造事故，而是证明你能区分：

- JVM Running 与 startup成功；
- readiness摘流量与 container restart；
- 同 Pod内新 ID与新 Pod UID；
- Event、runtime事实与 API副本。

### 17.2 可选：隔离命名空间中的最小代理实验

这不是 Java本身，只用一个很小的 HTTP容器验证 kubelet控制语义。必须使用专用非生产集群或已批准的实验节点；`study.k8s.io/probe-lab=true` 应由集群管理员预先标在允许实验的节点上。

先查看 context，人工确认它确实是获批的非生产实验集群，再把**真实 context名**填入变量。后面的脚本会二次比较；若仍是示例文字或中途切换 context，修改/清理都会停止。

```bash
kubectl config get-contexts
kubectl config current-context

# 人工确认后，把上一条命令的真实非生产context名填在这里。
export APPROVED_LAB_CONTEXT='这里填写已批准的非生产context名'
```

然后在**同一个 Bash 会话**中创建随机 namespace和随机 owner token。`kubectl create namespace` 遇到同名对象会直接失败，不使用 `apply` 接管已有 namespace；同时保存 context与 namespace UID，清理时一起核对。

```bash
set -euo pipefail

: "${APPROVED_LAB_CONTEXT:?必须先设置已批准的非生产context名}"
CURRENT_CONTEXT=$(kubectl config current-context)
if [ "$CURRENT_CONTEXT" != "$APPROVED_LAB_CONTEXT" ]; then
  printf '拒绝创建：当前context=%s，批准context=%s\n' \
    "$CURRENT_CONTEXT" "$APPROVED_LAB_CONTEXT" >&2
  exit 1
fi

LAB_TOKEN="$(date +%Y%m%d%H%M%S)-${RANDOM}-${RANDOM}"
LAB_NS="probe-lab-${LAB_TOKEN}"
LAB_OWNER="datou-${LAB_TOKEN}"
LAB_CONTEXT="$CURRENT_CONTEXT"

kubectl --context "$LAB_CONTEXT" create namespace "$LAB_NS"
kubectl --context "$LAB_CONTEXT" label namespace "$LAB_NS" \
  "study.k8s.io/owner=$LAB_OWNER" --overwrite=false

LAB_NS_UID=$(kubectl --context "$LAB_CONTEXT" get namespace "$LAB_NS" \
  -o jsonpath='{.metadata.uid}')

printf 'LAB_CONTEXT=%s\nLAB_NS=%s\nLAB_OWNER=%s\nLAB_NS_UID=%s\n' \
  "$LAB_CONTEXT" "$LAB_NS" "$LAB_OWNER" "$LAB_NS_UID"
```

把下面内容保存为 `probe-state-lab.yaml`。Namespace不写进清单，避免误改同名共享对象；Pod由后面的 `-n "$LAB_NS"` 精确放入刚创建的随机 namespace。

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: probe-state-lab
  labels:
    app: probe-state-lab
spec:
  automountServiceAccountToken: false
  restartPolicy: Always
  nodeSelector:
    study.k8s.io/probe-lab: "true"
  securityContext:
    runAsNonRoot: true
    runAsUser: 65532
    runAsGroup: 65532
    fsGroup: 65532
    seccompProfile:
      type: RuntimeDefault
  containers:
    - name: app
      image: registry.k8s.io/e2e-test-images/busybox:1.36.1-1
      command: ["sh", "-c"]
      args:
        - |
          printf 'ok\n' > /health/live
          printf 'ok\n' > /health/ready
          exec httpd -f -p 8080 -h /health
      ports:
        - name: http
          containerPort: 8080
      startupProbe:
        httpGet:
          path: /live
          port: http
        periodSeconds: 2
        failureThreshold: 15
      readinessProbe:
        httpGet:
          path: /ready
          port: http
        periodSeconds: 2
        failureThreshold: 2
        successThreshold: 2
      livenessProbe:
        httpGet:
          path: /live
          port: http
        periodSeconds: 3
        failureThreshold: 3
      resources:
        requests:
          cpu: 10m
          memory: 16Mi
        limits:
          cpu: 100m
          memory: 64Mi
      securityContext:
        allowPrivilegeEscalation: false
        capabilities:
          drop: ["ALL"]
        readOnlyRootFilesystem: true
      volumeMounts:
        - name: health
          mountPath: /health
  volumes:
    - name: health
      emptyDir:
        sizeLimit: 1Mi
```

先应用并保存初始身份：

```bash
kubectl --context "$LAB_CONTEXT" apply -n "$LAB_NS" -f probe-state-lab.yaml
kubectl --context "$LAB_CONTEXT" label pod -n "$LAB_NS" probe-state-lab \
  "study.k8s.io/owner=$LAB_OWNER" --overwrite=false

kubectl --context "$LAB_CONTEXT" wait -n "$LAB_NS" \
  --for=condition=Ready pod/probe-state-lab --timeout=120s

kubectl --context "$LAB_CONTEXT" get pod -n "$LAB_NS" probe-state-lab \
  -o jsonpath='uid={.metadata.uid}{"\n"}id={.status.containerStatuses[0].containerID}{"\n"}ready={.status.containerStatuses[0].ready}{"\n"}restart={.status.containerStatuses[0].restartCount}{"\n"}'
```

第一阶段只破坏 readiness：

```bash
kubectl --context "$LAB_CONTEXT" exec -n "$LAB_NS" probe-state-lab -- rm /health/ready

kubectl --context "$LAB_CONTEXT" get pod -n "$LAB_NS" probe-state-lab -w
```

预期要验证的是：Ready最终变 false，但 UID、container ID和 restartCount保持不变。不要把“恰好等待 4 秒”写成硬断言；worker调度和状态传播都可能带来偏差。

恢复 readiness，再确认连续成功阈值已经收敛：

```bash
kubectl --context "$LAB_CONTEXT" exec -n "$LAB_NS" probe-state-lab -- \
  sh -c "printf 'ok\n' > /health/ready"

kubectl --context "$LAB_CONTEXT" wait -n "$LAB_NS" \
  --for=condition=Ready pod/probe-state-lab --timeout=60s
```

第二阶段才破坏 liveness：

```bash
kubectl --context "$LAB_CONTEXT" exec -n "$LAB_NS" probe-state-lab -- rm /health/live

kubectl --context "$LAB_CONTEXT" get pod -n "$LAB_NS" probe-state-lab -w
```

预期要验证的是：同一 Pod UID下 container ID变化，restartCount推进；container重启后入口脚本重新创建 `/health/live`，新实例应恢复。随后用 UID查询 `Unhealthy/Killing` Event，并按第 15 节闭环。

清理不是“先打印再无条件删除”，而是在 shell 中强制校验。只要变量丢失、前缀不符、owner被改或 namespace UID变化，命令都会在 delete 之前退出：

```bash
case "$LAB_NS" in
  probe-lab-*) ;;
  *)
    printf '拒绝清理：namespace前缀不符合实验约定：%s\n' "$LAB_NS" >&2
    exit 1
    ;;
esac

CURRENT_CONTEXT=$(kubectl config current-context)
if [ "$CURRENT_CONTEXT" != "$LAB_CONTEXT" ]; then
  printf '拒绝清理：context已变化。current=%s expected=%s\n' \
    "$CURRENT_CONTEXT" "$LAB_CONTEXT" >&2
  exit 1
fi

CURRENT_OWNER=$(kubectl --context "$LAB_CONTEXT" get namespace "$LAB_NS" \
  -o jsonpath='{.metadata.labels.study\.k8s\.io/owner}')
CURRENT_UID=$(kubectl --context "$LAB_CONTEXT" get namespace "$LAB_NS" \
  -o jsonpath='{.metadata.uid}')

if [ "$CURRENT_OWNER" != "$LAB_OWNER" ] || [ "$CURRENT_UID" != "$LAB_NS_UID" ]; then
  printf '拒绝清理：owner或UID不匹配。ns=%s owner=%s uid=%s\n' \
    "$LAB_NS" "$CURRENT_OWNER" "$CURRENT_UID" >&2
  exit 1
fi

kubectl --context "$LAB_CONTEXT" delete namespace "$LAB_NS"
```

实验中没有出现某条 Event也不能作为失败硬判据，应以 API状态、container ID和 runtime证据为主。若中途换了 shell，请从可信记录恢复 `LAB_CONTEXT/LAB_NS/LAB_OWNER/LAB_NS_UID` 并重新核对；不确定时宁可保留这个随机实验 namespace，让环境负责人确认后再清理，也不要手工改掉保护条件。

## 18. 只铺一小段 GPU：同一套状态分层为什么以后仍然有用

这一课仍以 Java平台 Pod为主，不提前展开 NVIDIA驱动、Device Plugin、CUDA 或模型调度。但当业务换成 GPU推理服务时，今天的三本账不会消失，只会多出一层设备事实。

| Java平台主案 | GPU推理服务中的对应问题 | 仍由谁负责 | 不能混淆的边界 |
| --- | --- | --- | --- |
| JVM进程 Running | 推理进程已启动 | runtime / PLEG | 进程启动不等于模型已加载 |
| startup等待 Spring/JIT | startup等待权重加载、CUDA context和模型 warmup（预热） | application probe + kubelet | 不要用过短 liveness杀死正常长启动 |
| readiness检查是否能接业务 | readiness检查模型、队列和服务端口是否可服务 | application probe + status/EndpointSlice | readiness失败不等于 GPU设备应该重置 |
| liveness判断 JVM是否失去进展 | liveness判断推理进程死锁或完全失去响应 | application probe + SyncPod | 不要把外部模型仓库或共享依赖直接变成重启风暴 |
| PLEG观察 container退出 | PLEG观察 GPU container退出 | runtime / PLEG | PLEG不会解释 Xid、ECC（显存纠错相关硬件错误）、温度或显存故障 |
| JMX/业务指标 | DCGM/驱动日志/设备健康指标 | GPU telemetry栈 | 设备指标不是 Pod Ready字段的天然替代品 |
| 同 Pod内业务 container重启 | 推理 container局部重启 | kubelet runtime manager | container重启未必修复节点级 GPU故障 |

以后碰到“GPU Pod Running但不可服务”，先把问题拆成四层：

```text
应用服务层：模型是否加载、请求是否能完成
container runtime层：进程是否Running/Exited
Kubernetes资源层：Pod是否拿到设备资源、Device Plugin是否正常
节点设备层：driver/GPU是否出现Xid、掉卡、ECC或硬件故障
```

probe适合回答应用容器自己的服务健康；PLEG适合回答 runtime状态变化；Device Plugin与节点遥测回答设备供给和硬件健康。把这四层混成一个“GPU健康检查脚本”，常见后果是：外部依赖抖一下就重启模型、单 Pod问题触发整卡动作，或者节点硬件坏了却只反复重启 container。

下一课进入 NVIDIA节点栈时，仍沿用今天的方法：先问事实所有者是谁、身份 key是什么、状态通过哪条异步链传播，再谈命令和组件安装。

<a id="ch13-depth"></a>

## 19. 学习深度边界：哪些必须深入，哪些读懂即可

### 19.1 必须深入到能画图、能反推事故

| 知识点 | 你需要达到的程度 |
| --- | --- |
| startup/readiness/liveness三种语义 | 能解释初值、门控、threshold与不同控制效果 |
| Event与稳定 result cache | 能从第一条 Failure判断为什么状态可能不变 |
| container ID身份隔离 | 能解释新实例为什么不能继承旧实例健康结论 |
| readiness到 EndpointSlice | 能说明摘流量链和 API传播窗口 |
| liveness到 `computePodActions` | 能说明为何 probe worker不直接调 CRI，以及局部 kill/start边界 |
| PLEG、pod cache与 pod worker | 能说明“动作后重新观察”和 `GetNewerThan` 的设计价值 |
| statusManager本地版本与UID保护 | 能解释异步写 API、doorbell channel和防同名误写 |
| 生产证据闭环 | 能用 UID + 旧/新 ID + reason + CRI事实排除竞争解释 |

### 19.2 需要读懂，不要求现在背源码行号

- `AddPod` 如何为普通 container与 restartable init创建 worker；
- feature gate如何影响 kubelet重启后的 started/probe初值兼容；
- GenericPLEG的 reinspect、event丢弃和按 Pod relist；
- statusManager periodic sync与即时通知共用一个 goroutine；
- `restartPolicy=Never` 下 `KillPod` 的完整 action计算；
- 指标稳定级别、标签和采样边界。

你应该能在事故时重新定位这些函数，而不是脱离版本背每一个局部变量。

### 19.3 可以一笔带过，等专题需要时再深入

- mirror Pod的特殊 status与删除流程；
- terminal phase、resize、ephemeral container等本章旁支；
- 每个 Prometheus histogram bucket的具体数值；
- EventedPLEG连接恢复的全部状态机；
- Windows probe/runtime差异；
- fake clock、fake client和每个单元测试辅助结构的写法。

本章合格线不是“能独立改 kubelet”，而是：面对 Java Pod `Running/NotReady/restart`，你能从设计不变量出发读对关键分支，并用真实身份和多层证据讲清楚因果。

## 20. 课后推演题：先口述，再展开参考答案

<a id="ch13-first-check"></a>

### 20.1 首遍验收：这是进入下一课的门槛

1. `game-api` 的 CRI状态已经 Running，为什么 API `started` 仍可能是 false？
2. 为什么看到第一条 readiness `Unhealthy` 后，Pod仍可能保持 Ready=true？
3. readiness达到 FailureThreshold后，为什么通常不会调用 `StopContainer`？
4. liveness worker为什么不直接执行 kill，而只通过 result update唤醒 pod worker？
5. `PLEG Healthy=true` 能证明什么，不能证明什么？
6. statusManager本地已经把 Ready改成 false，为什么 `kubectl get pod` 仍可能短暂显示 true？

首遍通过标准：6题至少答对5题，并能画出三只独立方框：“probe稳定结果账、PLEG runtime现场账、statusManager待上报账”。还要标出 readiness会直接影响状态计算，而 liveness要先让统一同步执行动作、再由PLEG观察runtime结果；不能把三本账画成每次都固定顺序直传的一条流水线。做到这里即可进入下一课。

<a id="ch13-second-check"></a>

### 20.2 二遍加深：检查异常边界，不作为进入下一课的门槛

1. 哪几类证据组合起来，才比较有把握证明“这次重启由 liveness触发”？
2. 同一个 Pod name下 container ID变了，与 Pod UID变了，分别意味着什么？
3. JVM自己 `System.exit(1)` 时，即使没有 probe Failure，kubelet为什么仍能重启它？
4. kubelet进程重启后第一次看到旧 container ID，为什么不一定重新写三种 probe默认初值？
5. 为什么 `restartCount` 和 `lastState` 不能作为永久审计记录？
6. GPU推理 Pod Running但模型未加载完成，应该优先映射到哪种 probe？PLEG能不能替代它？

### 20.3 参考答案

<details>
<summary>展开首遍参考答案</summary>

1. `state.running` 来自 runtime事实；若定义了 startup probe，`started` 还要等当前 container ID的 startup result为 Success。没有 startup probe时，Running通常才直接等价于 Started。

2. `Unhealthy` 描述一次探测事实。worker先累计连续结果；当 `resultRun < failureThreshold` 时保持稳定 cache不变，因此 container Ready、Pod Ready与 EndpointSlice可以暂时不变。

3. readiness的设计职责是表达“是否应该接新流量”。稳定结果进入 status链，更新 container ready和 Pod condition；它不进入 runtime manager的 liveness/startup kill分支。

4. kubelet必须在统一的 `SyncPod` 中同时考虑 desired Pod、当前 runtime status、restartPolicy、init/sidecar、backoff和删除状态。probe worker直接 kill会绕过这些约束，并产生多条并发执行路径。

5. 它证明最近一次全局 `GetPods` 在阈值内成功；不证明每个变化 Pod的详细 `GetPodStatus` 成功、内部唤醒消息已投递、pod worker已同步，也不证明 API已更新。

6. statusManager先写本地 cache，再异步 Patch apiserver。网络、apiserver重试和下游 controller都产生传播窗口；本地控制链不为一次 API写入同步阻塞。

</details>

<details>
<summary>展开二遍参考答案</summary>

1. 至少核对：同一 Pod UID/Node、liveness `Unhealthy`、message明确的 `Killing`、kubelet对旧 ID发起 `StopContainer`、CRI旧 ID退出与新 ID启动、API最终 ID/restartCount变化。越靠后的证据越能证明动作真正完成。

2. UID不变而 container ID变，通常是同 Pod内实例重启；UID变化说明旧 Pod对象已经被另一个 Pod替代，即使 name相似也不是同一身份。

3. PLEG会观察 runtime新旧状态差异并更新 pod cache、投递 `ContainerDied` 等内部消息，随后 `SyncPod` 按 restartPolicy计算是否重新启动，不依赖 probe先发现退出。

4. 当前默认兼容逻辑会根据 container `StartedAt` 判断它是否是 kubelet重启前已存在的旧进程，并可能沿用旧 started语义；feature gate开启时行为又会变化。

5. runtime GC、日志轮转、kubelet/节点重启和 Pod UID替换都会丢失部分旧实例信息。字段是当前可见历史的 best-effort 汇总，不是不可变审计账本。

6. 长模型加载和 warmup优先放在 startup；startup成功后 readiness再表达是否可接流量。PLEG只观察 runtime状态变化，不知道模型是否加载完成，也不理解 GPU业务健康。

</details>

二遍通过标准：6题至少答对5题，并能指出一条证据为什么可能因阈值、身份换代、内部消息丢失或API传播延迟而不足。没有通过时可以以后回补，不阻塞后续GPU课程。

<a id="ch13-go-index"></a>

## 21. 本章 Go 语法索引：只补读这条链真正用到的部分

| 源码写法 | 大白话 | 本章位置 |
| --- | --- | --- |
| `func (m *manager) AddPod(...)` | `m` 是指向 manager的指针 receiver，方法可以修改其内部状态 | prober manager |
| `result, ok := manager.Get(id)` | 函数返回两个值；`ok` 表示是否真的找到 | started判定 |
| `if init; condition {}` | 先声明局部变量，再在同一个 if判断 | container ID与feature gate |
| `*bool`、`nil`、`*value` | 字段可以没有值；先判 nil，再解引用读取 | `ContainerStatus.Started` |
| `switch value { case ... }` | 命中 case后默认停止，不必写 `break` | 三种 probe初值与类型分支 |
| `append(slice, other...)` | `...` 把另一个 slice展开为多个参数，是真实语法 | 拼接 restartable init containers |
| `defer unlock()` | 函数返回前执行，常用来保证锁释放或记录耗时 | PLEG relist/status更新 |
| `select { case ch <- x: default: }` | channel能写就写，不能写立即走 default，不阻塞 | PLEG event丢弃策略 |
| `select { case <-ch: case <-ticker.C: }` | 同一个 goroutine等多个事件源 | statusManager即时/周期同步 |
| `map[key]value`与 `value, ok := m[key]` | 用 key定位本地状态，并区分零值与不存在 | worker/results/status cache |
| `sync.Mutex/RWMutex` | 保护多个 goroutine共享的 map和状态 | prober/status/PLEG |
| `atomic.Value` | 以原子方式发布一个整体值，读者不拿普通锁 | PLEG relist time |
| `context.Context` | 传递取消、超时和调用范围，不是业务状态对象 | runtime/API调用 |
| `return true/false` | bool含义由函数契约决定，本章有时表示 worker是否继续 | `doProbe` |

读 Go 源码时先找“这个返回值由谁解释”，再给 true/false翻译。最危险的习惯是把所有 `return true` 都理解成健康、成功或已完成。

## 22. 源码与测试锚点：以后按函数重新定位，不背页码

### 22.1 主源码锚点

| 主题 | 文件与函数 |
| --- | --- |
| worker创建与 Started计算 | `pkg/kubelet/prober/prober_manager.go`：`AddPod`、`isContainerStarted` |
| 初值、ID变化、门控、阈值 | `pkg/kubelet/prober/worker.go`：`newWorker`、`doProbe` |
| 单次 probe结果映射 | `pkg/kubelet/prober/prober.go`：`probe` |
| 稳定 result cache与 update channel | `pkg/kubelet/prober/results/results_manager.go`：`Set`、`setInternal` |
| probe update进入 syncLoop | `pkg/kubelet/kubelet.go`：`syncLoopIteration`、`handleProbeSync` |
| readiness fast-path与本地状态 | `pkg/kubelet/status/status_manager.go`：`SetContainerReadiness`、`updateStatusInternal` |
| phase/started/Ready生成 | `pkg/kubelet/kubelet_pods.go`：`getPhase`、`generateAPIPodStatus` |
| liveness翻译成 kill/start | `pkg/kubelet/kuberuntime/kuberuntime_manager.go`：`computePodActions` |
| Killing与 CRI stop | `pkg/kubelet/kuberuntime/kuberuntime_container.go`：`killContainer` |
| backoff到 API Waiting reason | `pkg/kubelet/kuberuntime/kuberuntime_manager.go`：`doBackOff`；`pkg/kubelet/kubelet.go`：`reasonCache.Update`；`pkg/kubelet/reason_cache.go`；`pkg/kubelet/kubelet_pods.go`：`convertToAPIContainerStatuses` |
| PLEG relist/cache/event/healthy | `pkg/kubelet/pleg/generic.go`：`Relist`、`reconcilePodRecord`、`Healthy` |
| cache新鲜度等待 | `pkg/kubelet/container/cache.go`：`GetNewerThan` |
| status异步写 API | `pkg/kubelet/status/status_manager.go`：`Start`、`syncPod` |
| EndpointSlice conditions | `staging/src/k8s.io/endpointslice/utils.go`：`podToEndpoint` |
| probe与PLEG/status指标 | `pkg/kubelet/prober/prober_manager.go`、`pkg/kubelet/metrics/metrics.go` |

### 22.2 最值得跟读的单元测试

| 设计结论 | 测试文件与入口 |
| --- | --- |
| startup/readiness/liveness阈值、hold与kubelet重启 | `pkg/kubelet/prober/worker_test.go`：`TestDoProbe`、`TestStartupProbeSuccessThreshold`、`TestStartupProbeFailureThreshold`、`TestResultRunOnLivenessCheckFailure`、`TestChangeContainerStatusOnKubeletRestart` |
| probe执行与结果映射 | `pkg/kubelet/prober/prober_test.go`：`TestProbe` |
| readiness状态合并 | `pkg/kubelet/prober/prober_manager_test.go`：`TestUpdateReadiness` |
| liveness/restartPolicy action | `pkg/kubelet/kuberuntime/kuberuntime_manager_test.go`：`TestComputePodActions`、`TestDoBackOff` |
| PLEG old/current、cache、丢Event与健康 | `pkg/kubelet/pleg/generic_test.go`：`TestRelisting`、`TestRelistWithCache`、`TestEventChannelFull`、`TestHealthy`、`TestReinspect`、`TestWorkerLoop` |
| cache时间契约 | `pkg/kubelet/container/cache_test.go`：`TestGetNewerThan` |
| readiness/startup fast-path与UID保护 | `pkg/kubelet/status/status_manager_test.go`：`TestSetContainerReadiness`、`TestSetContainerStartup`、`TestSyncPodChecksMismatchedUID` |
| backoff reason传播 | `pkg/kubelet/reason_cache_test.go`：`TestReasonCache` |

### 22.3 本次实际测试尝试与环境边界

在源码目录尝试了四组聚焦测试：

```bash
go test ./pkg/kubelet/prober \
  -run 'Test(Probe|DoProbe|StartupProbeSuccessThreshold|StartupProbeFailureThreshold|ResultRunOnLivenessCheckFailure|ResultRunOnStartupCheckFailure|UpdateReadiness)$' \
  -count=1

go test ./pkg/kubelet/kuberuntime \
  -run 'Test(ComputePodActions|DoBackOff)$' \
  -count=1

go test ./pkg/kubelet/pleg ./pkg/kubelet/container \
  -run 'Test(Relisting|RelistWithCache|GetNewerThan)$' \
  -count=1

go test ./pkg/kubelet/status \
  -run 'Test(SetContainerReadiness|SetContainerStartup)$' \
  -count=1
```

四组都在编译测试代码之前失败，原因相同：当前机器是 `go1.19.4 windows/amd64`，而本仓库 `go.work` 要求 Go `1.26.0`，旧工具链还无法识别 `godebug` directive。实际错误为：

```text
reading go.work: D:\datou\devops\kubernetes-master\kubernetes\go.work:3:
invalid go version '1.26.0': must match format 1.23

D:\datou\devops\kubernetes-master\kubernetes\go.work:5:
unknown directive: godebug
```

这表示本次没有得到单元测试通过结论，也不表示测试断言失败；阻塞点发生在旧 Go解析 workspace文件的阶段。后续安装仓库要求的工具链后，应原样重跑这四组测试。

## 23. 版本固定与延伸资料

### 23.1 本课对应的源码版本

```text
repository: kubernetes/kubernetes
branch: master
describe: v1.37.0-alpha.0-280-g301946d15e6
commit: 301946d15e67a4a2e8a5fb8292eb836acd366d78
```

源码会继续演进，尤其是 feature gate、EventedPLEG和指标稳定级别。以后换版本复习时，应先重新定位函数与测试，再判断本课结论是否仍成立。

### 23.2 官方概念与 API资料

- [Liveness、Readiness 与 Startup Probes](https://kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle/#container-probes)
- [Configure Liveness, Readiness and Startup Probes](https://kubernetes.io/docs/tasks/configure-pod-container/configure-liveness-readiness-startup-probes/)
- [Pod lifecycle 与 container states](https://kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle/)
- [EndpointSlice v1 API](https://kubernetes.io/docs/reference/kubernetes-api/discovery-resources/endpoint-slice-v1/)
- [Kubernetes component metrics](https://kubernetes.io/docs/reference/instrumentation/metrics/)
- [Kubernetes Device Plugins](https://kubernetes.io/docs/concepts/extend-kubernetes/compute-storage-net/device-plugins/)

### 23.3 设计历史资料

- [Pod Lifecycle Event Generator 原始设计提案](https://github.com/kubernetes/design-proposals-archive/blob/main/node/pod-lifecycle-event-generator.md)
- [KEP-3386：Evented PLEG](https://github.com/kubernetes/enhancements/tree/master/keps/sig-node/3386-kubelet-evented-pleg)
- [本课固定 commit 的 `worker.go`](https://github.com/kubernetes/kubernetes/blob/301946d15e67a4a2e8a5fb8292eb836acd366d78/pkg/kubelet/prober/worker.go)
- [本课固定 commit 的 `generic.go`](https://github.com/kubernetes/kubernetes/blob/301946d15e67a4a2e8a5fb8292eb836acd366d78/pkg/kubelet/pleg/generic.go)
- [本课固定 commit 的 `status_manager.go`](https://github.com/kubernetes/kubernetes/blob/301946d15e67a4a2e8a5fb8292eb836acd366d78/pkg/kubelet/status/status_manager.go)

历史设计文档用来理解当年的问题与取舍，当前行为仍以固定 commit的实现和测试为准。

## 24. 本课收束：把 `Running / NotReady / Restart` 翻译成三条不同问题

以后值班看到 Java Pod异常，不要先问“kubelet是不是坏了”，而按顺序问：

```text
一、应用现在能不能接新流量？
   -> startup/readiness、container Ready、Pod Ready、EndpointSlice

二、container进程实际上发生了什么？
   -> CRI ID与状态、PLEG、pod cache、terminated reason

三、是谁决定了下一步动作，动作是否真的完成？
   -> liveness/startup结果、SyncPod、computePodActions、Killing、CRI stop/start

四、控制面看到的副本收敛到哪一步？
   -> statusManager local version、UID保护、API Patch、下游controller
```

本课最重要的设计思想可以压缩成五句话：

1. **事实与动作分离**：probe发布健康结论，统一的 pod sync计算动作。
2. **瞬时事实与稳定结论分离**：Event可以先出现，threshold决定何时改变 cache。
3. **desired与observed分离**：调用 CRI成功与否，最终仍由 PLEG/runtime status重新观察。
4. **本地控制与 API发布分离**：status先在节点收敛，再异步写控制面。
5. **身份必须贯穿全链**：Pod用 UID，进程实例用 container ID；名字只用于人类阅读。

如果你已经能在不看答案的情况下解释主案四个时刻，并能说出什么证据会推翻自己的判断，这一课就达标了。下一课开始进入 NVIDIA节点栈，但不会抛开 Kubernetes源码方法：仍从设计背景、状态所有者、调用边界和生产事故反查入手。
