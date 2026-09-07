# 03｜GPU 与源码进阶：掌握边界，再追到实现

> 本篇不是新手第一遍必读。先完成主教材，再进入设备、工作负载队列和源码。原文的高级知识没有要求初学者在第一页全部吞下；历史全文保留供查阅，但以新版修正与目标版本为准。

## 1. GPU 先拆成两本账

传统 Device Plugin 路径有“节点资源数量账”和“设备身份/运行状态账”：

```text
驱动/设备插件发现设备
  → kubelet 通过注册与 ListAndWatch 获得资源及健康状态
  → Node status 发布 capacity / allocatable
  → scheduler 根据扩展资源 request 选择 Node
  → 本地 Assume、插件预留与 API Binding
  → 目标节点 kubelet 与设备插件协作分配/准备具体设备
  → 运行时注入，容器启动
  → CUDA 与业务验证
```

节点可以上报 `nvidia.com/gpu: 8`，但这个数字本身不告诉默认资源 Filter 每张卡的 UUID、显存碎片、NVLink 距离或温度。传统路径中，scheduler 选 Node 与节点侧具体设备分配是不同职责。DRA 在后文另讲，不能把这个边界泛化到所有设备模型。[G1][G2]

### 1.1 三个层次的成功

| 层次 | 最小证据 | 仍未证明 |
|---|---|---|
| 调度成功 | Pod 有预期 nodeName，相关资源请求已进入分配账 | 具体设备是否准备完成 |
| 设备可用 | 容器看到预期设备，驱动/运行时准备成功 | CUDA 镜像兼容与应用逻辑 |
| 业务达标 | 应用验收、通信与性能符合目标 | 长期故障隔离、容量和成本最优 |

如果已经绑定后设备准备失败，原 Pod 不会由默认 scheduler 随意改写 nodeName 去另一台机器。先判断是不是终态失败、是否由 Job/Deployment 管理、是否允许从 checkpoint 恢复，再按流程处置；“删 Pod 重试”不能替代根因与训练数据安全分析。

### 1.2 先看真实资源表达

```bash
kubectl get nodes -o custom-columns='NAME:.metadata.name,CAP:.status.capacity.nvidia\.com/gpu,ALLOC:.status.allocatable.nvidia\.com/gpu'
kubectl get pod -n ml-lab GPU_POD_NAME -o yaml
kubectl api-resources --api-group=resource.k8s.io
```

传统独占扩展资源以整数表达；只写 GPU limit 时，API 按相应资源规则处理 request。还必须为 CPU、内存、临时存储与节点池制定合同。[G1]

**教学算例：**a 有2 GPU余量但仅8Gi内存，b 有64Gi内存但无GPU；一个2GPU/32Gi Pod没有落点。另一个例子是4台各余1卡，不能承载单个4卡Pod。分布式训练要通过多个Pod和相应训练架构表达，不是调度器把一个普通Pod拆到多台Node。

## 2. “1 GPU”不一定是同一种产品

| 模型 | Kubernetes 常见表达 | 应对业务明确说明 |
|---|---|---|
| 整卡独占 | 厂商扩展资源整数 | 整卡类型、容量与独占边界 |
| MIG | 随 strategy 暴露 GPU 或 MIG profile 资源 | profile、父卡关系、硬件分区与重配影响 |
| time-slicing | 复制的逻辑资源份额，可能重命名 `.shared` | 非独占、显存与性能隔离限制 |
| DRA | Claim、Class、Slice 及版本相关桥接 | 驱动声明的设备属性、容量和分配规则 |

MIG 与时间切片不是“实现方式不同但效果一样”。MIG 提供硬件分区；time-slicing 的副本份额不等于同等份额的独立显存，也不能承诺恒定比例性能。[G3][G4]

### 2.1 为什么 4 张物理卡可能显示 16 个资源单位

若每卡暴露4份逻辑份额，调度器可能看见16。要确认该模型，必须把物理库存、实际设备插件配置、ConfigMap挂载/选择、节点标签与插件启动日志关联起来。不能仅看到一个名为 timeSlicing 的 ConfigMap，就断言目标节点已加载。

容量平台至少分开记录：物理卡数、逻辑可分配单位、已请求单位、GPU实际利用率、显存占用和健康状态。把这些混成“GPU使用率80%”，会让排队、售卖与故障处置互相误导。

### 2.2 MIG 碎片要按 profile 看

多个小profile空闲，不代表一个大profile立即可用。profile重新组合需要设备与驱动支持，还可能影响现有工作负载。应提供受控节点分组、变更窗口、目标profile余量与重配前影响清单，而不是让默认scheduler“自动把碎片拼起来”。[G4]

### 2.3 节点间拓扑与节点内拓扑

zone/hostname分散回答“Pod在哪些节点域”；NUMA、PCIe、NIC、NVLink回答“同一节点内设备的距离与协作”。CPU Manager、Topology Manager、设备插件/DRA驱动与应用通信库都可能参与节点内兑现。拓扑矩阵不是应用性能根因的充分证据，仍需把UUID、容器可见设备、通信测试和时间窗对上。[G1][G2]

## 3. 默认能检查GPU数量，不代表默认按GPU装箱

固定源码 `pkg/scheduler/apis/config/v1/defaults.go` 的默认评分资源是CPU和memory。普通扩展资源会参与相应的资源可行性检查，但不要仅因为Pod请求GPU，就认为NodeResourcesFit已经按GPU占用比例打分。[G5]

专用profile可以显式选择GPU参与评分。下列是相关配置片段，不是可直接替换生产控制面的完整部署包：

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

Pod需路由到对应schedulerName；同进程profiles的QueueSort必须一致。若改为独立scheduler进程，需另外处理二进制、配置、RBAC、leader election、监控与发布隔离。[G6]

### 3.1 手算一个纯GPU子项

两台8卡节点分别已请求1卡和6卡，新Pod请求1卡：MostAllocated的GPU占比分别为2/8与7/8，即25%和87.5%，后者在此子项更高。**只证明这一评分子项的方向**，最终还受CPU、memory、其他插件权重及候选集合影响。

相反，LeastAllocated对这个子项倾向空余更多的节点。装箱可能留下完整空节点方便大任务，但也可能集中热量、带宽与故障影响；在线关键推理与可恢复批任务不必采用同一策略。

### 3.2 三层评分不能混

资源在NodeResourcesFit内部的权重、Score插件是否归一化、整个插件在Framework内的权重，是不同层次。固定源码的NodeResourcesFit没有ScoreExtensions，不能为它凭空画一个必经NormalizeScore步骤。设计实验时记录配置和分数，不用一次落点反推完整算法。[G5][G6]

## 4. DRA 改变了什么，没改变什么

DRA通过DeviceClass、ResourceClaim、ResourceSlice等对象表达设备供给和请求，让设备分配参与调度过程。驱动在节点侧还需要准备资源；Claim已有allocation不等于容器已成功使用设备。[G2]

```text
供给：驱动发布Slice及设备属性
请求：Pod关联Claim或相应资源表达
调度：DynamicResources参与可行性和分配
持久化：记录分配与消费者关系，完成Pod节点绑定
节点：驱动准备资源，运行时与应用兑现
```

它能使用的是驱动通过API声明的事实，不会自动知道任意实时GPU性能，也不自动提供最优NVLink布局。

### 4.1 版本卡比一张“GA/Beta表”更可靠

为目标环境填写：Kubernetes版本与发行版、DRA driver版本、API discovery结果、feature gates、scheduler插件配置、ResourceSlice内容、Claim请求类型、分配结果及节点准备状态。不要把开发分支的默认开关当成云厂商当前开放能力。

原文固定开发快照讨论了扩展资源到DRA的桥接。具体部署使用该能力时，同一个资源名可能经传统scalar或DRA路径处理；应根据目标版本、DeviceClass和实际Node供给分流，不能因Node上无传统GPU scalar就立即判设备插件损坏。

DRA下的抢占、优先请求、可消耗容量和设备绑定条件同样必须逐版本核对。本次不将“某开发版本不支持某种抢占”写成永久规则，也不承诺给Pod提高priority就能抢走别人Claim正在使用的设备。需要目标版本源码、驱动协议和实验证据才能闭合结论。[G2]

## 5. Kueue、Volcano 与 kube-scheduler 不是三个同义词

Kueue关注工作负载准入、配额与队列；Volcano可以承担面向批任务的Pod/PodGroup节点调度；kube-scheduler负责其所处理Pod的节点选择和绑定。实际组合应先写职责，再选产品。[G7][G8]

### 5.1 Kueue 不能只用“管总配额”概括

LocalQueue是命名空间入口，ClusterQueue表达配额/策略，ResourceFlavor关联资源风味，Workload表达待准入工作。QuotaReserved与Admitted是准入状态，不是每个Pod已经Binding。[G7]

开启Topology-Aware Scheduling时，Kueue还会基于节点及拓扑域容量检查放置并分配拓扑，显著不同于只看聚合配额。即便如此，也要区分“准入时通过放置检查”与“后来每个Pod已经绑定并Ready”；节点健康、其他占用与准备过程还会变化。[G9]

WorkloadPriorityClass与Pod PriorityClass分别影响对应层的优先级，不能在平台UI只显示一个含糊的数字。具体回退/推导规则以安装版本为准。[G10]

### 5.2 gang解决的是成员协调，不是让所有进程同一纳秒启动

一个训练任务需要足够成员才能工作，逐Pod抢到少量资源却永远凑不齐时，需要all-or-nothing或gang相关策略。Volcano gang插件依赖实际PodGroup、最小成员与插件配置，不能把“安装Volcano”理解为全部批调度策略自动启用。[G8]

也不要写“Kubernetes原生永远没有gang”：目标版本可能已有或正在演进原生分组调度能力。生产选型要比较成熟度、API、故障恢复、监控、升级和维护成本，而非凭产品名字判断。[G11]

### 5.3 平台应显示哪四个状态

```text
等待工作负载准入
已准入，部分Pod还没绑定
成员已绑定，但设备/镜像/网络尚未Ready
工作负载正在运行或已失败
```

若同时使用Kueue与Volcano，明确外层准入、gang、配额、公平、抢占、失败回队分别由谁负责；避免两层各自驱逐、各自保留份额却缺少一致的资源释放协议。

## 6. 源码的第一条线：成功Pod究竟改变了谁的状态

本篇使用原文固定提交 `301946d15e67a4a2e8a5fb8292eb836acd366d78`。提交对象已核对，但不把原作者本地git describe当成已复现的生产版本。实际学习应优先选目标集群tag；开发快照用于研究差异。

| 阅读顺序 | 位置 | 本轮只回答的问题 |
|---|---|---|
| 1 | scheduler.go / Run | 谁启动循环，谁负责leader后的工作 |
| 2 | schedule_one.go / ScheduleOne | Pod从哪里取，什么时候Done |
| 3 | schedulingCycle / schedulePod | snapshot、Filter与Score怎样衔接 |
| 4 | framework/runtime/framework.go | 插件按何顺序调用，何时短路 |
| 5 | assumeAndReserve | 通用cache与插件状态分别改了什么 |
| 6 | bindingCycle | Permit等待、PreBind、Bind和PostBind责任 |
| 7 | cache实现 | Add/Assume/Forget与NodeInfo账怎样维护 |

第一遍只画输入、输出与副作用，不钻每个插件。第二遍才读失败路径和队列，否则容易被几百个函数名淹没。[G5]

### 6.1 用一页状态表读代码

```text
函数：
输入：PodInfo / snapshot / CycleState / NodeInfo 哪些对象
读取：来自API、informer、cache还是插件私有状态
写入：是否只改内存；是否调用API；是否改变队列
返回：Status还是error；成功/拒绝/等待的语义
并发：谁启动goroutine；谁等待；共享状态如何保护
失败：Unreserve、Forget、Done分别是否需要发生
证据：单测名、复现输入、实际输出、未覆盖分支
```

Assume是通用缓存的乐观占账，Reserve是插件自己的临时状态；二者不能合并成一个“资源锁”。普通scheduling cycle串行而binding cycle可能并发，是理解该机制的关键。[G6]

## 7. 第二条线：一次失败怎样成为下一次尝试

```text
某节点插件Status
  → 本轮诊断与无可行节点结果
  → 可能进入PostFilter/抢占
  → 更新Condition、事件、队列状态
  → 等待相关ClusterEvent
  → QueueingHint判断是否可能受益
  → 后续尝试重新验证全部条件
```

### 7.1 本次纠错最值得亲自读的函数

固定源码的RunFilterPlugins按配置次序执行，遇到失败后返回。逻辑上“所有硬条件必须通过”，执行上却不等于“为每个节点跑完所有插件收集全部失败”。[G5]

由此得到三个运维结论：修复CPU后出现卷错误可能是之前被短路隐藏；一条事件不构成完整约束矩阵；对所有节点的失败次数求和不应被当作精确资源库存。

PreFilter有自己的返回与合并规则，不要因为看到PreFilter某分支继续执行，就推断Filter也相同。节点间可以并发评估，与同一节点的插件顺序又不是同一个并发维度。

### 7.2 为什么失败补偿要通知别人

A先Assume最后一个资源单位，B随后因资源不足失败；A绑定失败释放临时账。此时只重试A，而不让B获得有用变化，就可能造成不必要等待。读队列代码要追“什么事件使B重新可尝试”，而不只是寻找一个固定退避秒数。

延迟、队列结构、QueueingHint和in-flight跟踪有版本差异。不要把老版本固定TTL或队列名字写成永远不变的规范。[G5]

## 8. 第三个阅读目标：只完整读一个插件

建议顺序：NodeResourcesFit → NodeAffinity → TaintToleration → VolumeBinding → DefaultPreemption → DynamicResources。

先为NodeResourcesFit做四组输入：恰好够、差1m CPU、CPU够但内存不足、扩展资源名缺失。检查PodRequests的最终形状、NodeInfo资源账、Status与评分。遇到init/sidecar/overhead/resize，再进入对应helper，不先跳读全部设备源码。

VolumeBinding值得第二轮深入，因为它展示了只读可行性判断、预留、外部API和失败补偿之间的关系。它能帮助你理解“Filter成功为什么不保证最终绑定成功”。

### 8.1 可执行的源码阅读起点

以下命令在**已经存在且版本选定的Kubernetes源码目录**运行，不是实验集群命令。先确认工作区与构建环境，命令可能写Go编译缓存和下载依赖：

```bash
git rev-parse HEAD
git status --short
git grep -n 'func .*RunFilterPlugins' -- pkg/scheduler
git grep -n 'func .*ScoreExtensions' -- pkg/scheduler/framework/plugins/noderesources
git grep -n 'func Test' -- pkg/scheduler/framework/plugins/noderesources
```

核对go.mod与源码构建说明后：

```bash
go test ./pkg/scheduler/framework/plugins/noderesources -list 'Test.*'
go test ./pkg/scheduler/framework/plugins/noderesources -run '^TestFit$' -count=1 -v
```

第二条测试名必须先在第一条列表中确认存在。出现“no tests to run”不是通过。不能把一段截出的Go函数放到独立文件就期待编译，源码依赖版本内的接口和生成文件。

### 8.2 验证层级必须如实记录

静态阅读只证明代码路径；单测验证给定输入；集成测试验证API与调度协作；真实GPU实验才验证驱动、运行时与硬件。本文交付不声称已运行上游Go测试或GPU/CUDA实验。阅读记录应区分四层，不把上一层证据包装成下一层结果。

## 9. 走向专家的结业任务

交付一个小型专项，而不是再读一遍全文：

**资源专项：**选择一种真实Pod shape，比较两种评分配置下容量碎片、故障域和发布空间；提供固定输入、手算、配置、重放与回滚判断。

**设备专项：**记录某目标环境从物理设备到API供给、调度、Claim或Allocate、容器可见设备的证据链；明确实际使用的是整卡、MIG、共享还是DRA。

**源码专项：**选择一个可复现调度现象，给出对应固定提交、连续调用链、至少一个负向测试与一次不同版本比较。能够指出某条教程结论为何只适用于特定版本，比背出该结论更重要。

本篇加实验能够建立专项能力路线，但不会用一次阅读替代生产事故处理、容量规划和代码验证经验。

## 官方与固定源码依据

- [G1 Device Plugins](https://kubernetes.io/docs/concepts/extend-kubernetes/compute-storage-net/device-plugins/)
- [G2 Dynamic Resource Allocation](https://kubernetes.io/docs/concepts/scheduling-eviction/dynamic-resource-allocation/)
- [G3 NVIDIA Time-Slicing](https://docs.nvidia.com/datacenter/cloud-native/gpu-operator/latest/gpu-sharing.html)
- [G4 NVIDIA MIG](https://docs.nvidia.com/datacenter/cloud-native/gpu-operator/latest/gpu-operator-mig.html)
- [G5 固定源码入口](https://github.com/kubernetes/kubernetes/tree/301946d15e67a4a2e8a5fb8292eb836acd366d78/pkg/scheduler)；[默认资源](https://github.com/kubernetes/kubernetes/blob/301946d15e67a4a2e8a5fb8292eb836acd366d78/pkg/scheduler/apis/config/v1/defaults.go)；[Filter](https://github.com/kubernetes/kubernetes/blob/301946d15e67a4a2e8a5fb8292eb836acd366d78/pkg/scheduler/framework/runtime/framework.go)
- [G6 Framework](https://kubernetes.io/docs/concepts/scheduling-eviction/scheduling-framework/)；[Scheduler Configuration](https://kubernetes.io/docs/reference/scheduling/config/)
- [G7 Kueue Workload](https://kueue.sigs.k8s.io/docs/concepts/workload/)
- [G8 Volcano Gang](https://volcano.sh/docs/scheduler/plugins/gang/)；[Scheduler Overview](https://volcano.sh/docs/scheduler/overview/)
- [G9 Kueue TAS](https://kueue.sigs.k8s.io/docs/concepts/topology_aware_scheduling/)；[All-or-nothing](https://kueue.sigs.k8s.io/docs/concepts/all_or_nothing/)
- [G10 Workload Priority Class](https://kueue.sigs.k8s.io/docs/concepts/workload_priority_class/)
- [G11 Kubernetes Gang Scheduling](https://kubernetes.io/docs/concepts/scheduling-eviction/gang-scheduling/)
