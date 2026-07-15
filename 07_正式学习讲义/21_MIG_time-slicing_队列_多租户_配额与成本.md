# 第 21 课：MIG、time-slicing、队列、多租户、配额与成本

> 主案例：一套原本按“8 张物理 GPU”规划的集群，在开启 time-slicing 后显示 `nvidia.com/gpu=80`；团队把这 80 个逻辑访问槽当成 80 张卡出售，随后在线推理 P99、批训练排队、成本分摊和故障归因同时失真
> 组件主线：物理 GPU → MIG/time-slicing/MPS/DRA → Device Plugin→kubelet Node扩展资源账，或DRA Driver→DeviceClass/ResourceSlice/ResourceClaim账 → Kueue 配额入场 → kube-scheduler 节点放置 → 容器与 GPU 进程 → DCGM/vLLM/成本账
> 源码主线：Kueue `v0.18.3`、commit `afd60c3` 的 API、队列、cache、admission 与 job framework；复用本地 Kubernetes commit `301946d15e67a4a2e8a5fb8292eb836acd366d78` 的调度主线
> NVIDIA 基线：GPU Operator `v26.3.3`、k8s-device-plugin `v0.19.3`、MIG Manager `v0.14.2`
> Kueue 基线：`v0.18.3`，release date `2026-07-10`，本课只使用 `kueue.x-k8s.io/v1beta2` 示例
> 事实核对日期：`2026-07-14`
> 本课深度：S1。必须能区分物理容量、共享访问、命名空间门槛和批任务预算，能设计 GPU 池、处理重配置、解释排队与成本；不要求实现 MIG firmware、CUDA MPS server 或 Kueue 调度器

---

## 0. 生产事故：集群为什么突然“多了 72 张 GPU”

周一早上，容量平台显示：

```text
物理节点：1
物理GPU：8
Node.status.capacity["nvidia.com/gpu"]：80
已分配：56
剩余：24
```

业务负责人看到的是：

```text
还有24张卡
  -> 可以再接24个独占任务
  -> 当前GPU利用率只有70%
  -> 每个任务按1/80节点成本分摊
```

现场实际配置却是：

```yaml
sharing:
  timeSlicing:
    resources:
    - name: nvidia.com/gpu
      replicas: 10
```

这表示 8 张物理卡各自允许 10 个逻辑访问名额，总共广告 80 个扩展资源。

它不表示：

- 物理机新增了 72 张 GPU；
- 每个逻辑名额拥有十分之一显存；
- 每个逻辑名额稳定获得十分之一算力；
- 10 个 Pod 之间有硬件安全隔离；
- 一张卡发生 Xid 或 reset 时只影响一个名额；
- 一张卡的成本可以天然、线性、无争议地除以 10。

事故继续发展：

1. 在线 vLLM 和批训练共用同一个 time-slicing 节点池；
2. 批任务开始大 kernel 后，在线请求 TTFT 与 ITL 抖动；
3. 某个进程吃满显存，邻居进程 CUDA OOM；
4. 同一物理 GPU 出现 Xid，多个 Pod 同时失败；
5. Kueue 已把训练 Job 标为 `Admitted`，但 Pod 因节点显存形态和 taint 一直 `Pending`；
6. 成本系统把设备级利用率复制给每个共享 Pod，再求和得到 560%；
7. 财务按 56/80 认定只消耗 70% 卡时，物理机器实际已经按整机计费；
8. 值班同学看到 Namespace ResourceQuota 还有 8，误以为 Kueue 一定还能放行 8 个任务。

这不是一个单点故障，而是四本概念账被混成了一本：

```text
物理硬件账
  != Kubernetes Node资源广告账
  != Namespace ResourceQuota接纳账
  != Kueue批任务配额账
  != 财务成本分摊账
```

### 0.1 本章必须建立的总链路

```text
业务提交Job
  -> Job对象经过API准入与RBAC
  -> LocalQueue选择租户入口
  -> ClusterQueue检查配额、flavor、借用与公平性
  -> Kueue形成quota reservation
  -> 所需AdmissionChecks就绪后Admit
  -> Job解除suspend
  -> Job controller创建Pod
  -> 每个Pod经过API准入与GPU ResourceQuota
  -> kube-scheduler执行Filter/Score并选择真实Node
  -> kubelet按Device Plugin/DRA结果分配设备
  -> 容器启动、模型加载、GPU进程运行
  -> DCGM、PodResources、vLLM指标和成本标签联表
```

这里必须单独说明 Plain Pod：

```text
直接创建Plain Pod
  -> Pod API准入与ResourceQuota
  -> Kueue通过scheduling gate管理入场
  -> gate移除后进入kube-scheduler
```

batch Job 创建时，GPU requests 位于 Pod template。

Job 对象通过创建准入时，不会因为这份 Pod template 立刻扣除 `requests.nvidia.com/gpu`。

真正创建每个 Pod 时才进入 Pod ResourceQuota admission。

其中最重要的两个不等号是：

```text
Kueue Admitted
  != kube-scheduler Scheduled

共享资源数量增加
  != 物理GPU数量增加
```

### 0.2 从 Java 平台经验迁移，但不把 GPU 当 CPU

在 Java 平台上，你可能熟悉：

```text
Namespace ResourceQuota允许创建Pod
  != 节点上真有足够CPU和内存

Deployment副本已创建
  != 所有Pod都Ready

线程池允许100个任务排队
  != 机器新增100个CPU核心
```

GPU 场景也有类似的控制面分层。

但 GPU 多了三类不能靠 CPU 直觉替代的事实：

- 显存通常不能像 CPU time 那样被 Kubernetes 原生公平切分；
- 一块物理 GPU 的 reset、Xid、链路和温度仍可能形成共享故障域；
- MIG 几何、整卡、共享槽位、GPU 型号会改变资源名称和可放置性。

因此，本章会借 Java 平台经验解释“队列”和“配额”，但所有最终判断都回到 GPU 设备事实。

---

## 1. 先钉死四十个结论

1. 整卡独占、MIG、time-slicing、MPS 和 NVIDIA DRA 是不同层面的方案，不能只按“利用率高不高”选择。
2. time-slicing 的 `replicas` 是共享访问槽位，不是保底算力份额。
3. 8 张卡乘以 10 个 replicas 会广告 80 个逻辑资源，但物理卡仍然只有 8 张。
4. time-slicing 没有显存隔离，同卡进程共享故障域，也会承受邻居噪声。
5. `failRequestsGreaterThanOne=true` 是 time-slicing 场景的推荐保护，`v0.19.3` 默认仍是 `false`；MPS schema 没有这个同名字段。
6. 在 time-slicing 下请求 2 个 `nvidia.com/gpu` 不代表获得两倍稳定计算性能。
7. `renameByDefault=true` 会把共享资源改名为带 `.shared` 的资源名，帮助调用方看见语义差异。
8. MPS 比纯 time-slicing 多了由 MPS control daemon 管理的空间分区和平均额度，但仍不等于 MIG 的硬件隔离。
9. 当前 `v0.19.3` 的 MPS 支持仍标为实验性。
10. 当前同一节点的 MPS 与 time-slicing 互斥。
11. 当前插件的 MPS 不支持 MIG，只支持完整 `nvidia.com/gpu`。
12. MIG 的 GPU Instance，简称 GI，是从物理 GPU 中划出的一组硬件资源。
13. MIG 的 Compute Instance，简称 CI，是 GI 内供计算上下文使用的计算分区。
14. MIG profile 名称中的 `g` 描述分配的 GPU slice 规模；显存后缀描述该 profile 的显存容量，不能跨型号凭名称猜支持性。
15. MIG 能提供比进程级共享更强的显存和计算隔离，但同一物理卡仍存在卡级故障、供电、散热和链路共同故障域。
16. 并非所有 NVIDIA GPU、driver 和 profile 都支持 MIG；生产必须按硬件清单与官方矩阵核验。
17. `single` MIG 策略把节点上的 MIG 设备按统一资源语义广告；`mixed` 会出现按 profile 区分的扩展资源名。
18. 资源请求名写错时，即使物理上有空闲 MIG slice，Pod 仍会 Pending。
19. MIG 几何变化不是普通 label 改名；它会停止 GPU operand、可能停止 host client、重配设备，某些环境还会重启节点。
20. GPU Operator `v26.3.3` 的 MIG Manager watch `nvidia.com/mig.config`，并使用 `mig-parted` 执行几何配置。
21. MIG Manager 状态至少要观察 `pending`、`rebooting`、`success`、`failed`，不能只看命令退出码。
22. `<node-name>-mig-config` 是 `26.3` 动态生成的每节点默认配置入口之一，不应继续假定所有节点只共用老静态 ConfigMap。
23. 传统 Device Plugin 的 Capacity/Allocatable 是 kubelet 对扩展资源的节点账；DRA 主要看 DeviceClass、ResourceSlice 与 ResourceClaim allocation，不能强行套用同一本 Node 数量账。
24. Namespace ResourceQuota 是 API 接纳门槛，不是物理容量证明。
25. 传统扩展资源的 ResourceQuota 应使用 `requests.nvidia.com/gpu` 这一类 requests key。
26. ResourceQuota 数值可以大于全集群物理容量，API 不会替你验证容量规划。
27. Kueue quota 是批工作负载的入场预算，不等于 kube-scheduler 的节点可放置性。
28. LocalQueue 是 namespace 内租户入口；ClusterQueue 是集群级配额池。
29. Workload 的 PodSet 资源量是 `count × 单Pod requests`，不能只看单个 Pod 模板。
30. Kueue `Admitted` 只表示配额和 admission 条件成立，不表示 Pod 已经放到节点或开始运行。
31. Kueue 不是 kube-scheduler；最终 Node placement 仍由 kube-scheduler 完成。
32. `waitForPodsReady` 可在 Pod 未及时 Ready 时取消 admission 并重新排队，但它不是原子 gang scheduler。
33. 取消 admission 前可能已有部分 Pod Running、拉取镜像或下载模型，副作用不会自动回滚。
34. Cohort 允许 ClusterQueue 分享同 flavor 的空闲配额；未定义该 flavor/resource 时不能凭空借。
35. 想从 Cohort 借某资源，即便自己的 `nominalQuota` 为 0，也要声明该 flavor/resource。
36. `borrowingLimit` 限制“最多借多少”，`lendingLimit` 限制“最多借出去多少”；二者方向相反。
37. 抢占能回收配额，但不能把已经完成的训练计算、下载和 checkpoint 成本还给业务。
38. 两类 Fair Sharing 分别参与同一 CQ 内的 admission 排序，或跨 CQ/Cohort 的 admission 与 quota 抢占判断；它们都不是 GPU kernel 性能隔离，也不是绝对轮转。
39. time-slicing 的物理 GPU 成本不能简单除以 replicas；replicas 是访问并发配置，不是财务权重。
40. 一套可信 GPU 成本账必须同时保留物理卡时、分配卡时、业务产出、排队、失败、碎片和冷启动。

---

## 2. 固定版本与证据边界

### 2.1 本课版本账本

| 组件 | 固定版本 | 本课使用它回答什么 |
|---|---|---|
| Kubernetes | commit `301946d15e67a4a2e8a5fb8292eb836acd366d78`，`v1.37.0-alpha.0-280-g301946d15e6` | kube-scheduler、kubelet资源账和DRA/扩展资源边界 |
| GPU Operator | `v26.3.3` | 组件编排、MIG Manager部署与变更边界 |
| k8s-device-plugin | `v0.19.3` | MIG策略、time-slicing、MPS资源广告语义 |
| MIG Manager | `v0.14.2` | MIG几何状态机和`mig-parted`执行 |
| Kueue | `v0.18.3`，commit `afd60c3` | 队列、quota reservation、admission、公平与抢占 |
| Kueue API | `kueue.x-k8s.io/v1beta2` | 本章所有可复制的对象示例 |

版本账本不是装饰。

它防止四类常见错误：

1. 从 Kueue 老文章复制 `v1beta1`；
2. 在 `v1beta2` 继续写旧字段 `spec.cohort`，而不是 `spec.cohortName`；
3. 把 device-plugin `main` 未来行为当成 `v0.19.3` 已有保证；
4. 把旧 GPU Operator 静态 MIG ConfigMap 流程当成 `26.3` 的唯一行为。

### 2.2 官方固定入口

- [GPU Operator v26.3.3 release](https://github.com/NVIDIA/gpu-operator/releases/tag/v26.3.3)
- [GPU Operator 26.3 MIG 文档](https://docs.nvidia.com/datacenter/cloud-native/gpu-operator/26.3/gpu-operator-mig.html)
- [k8s-device-plugin v0.19.3 固定 tag](https://github.com/NVIDIA/k8s-device-plugin/tree/v0.19.3)
- [k8s-device-plugin v0.19.3 sharing 文档](https://github.com/NVIDIA/k8s-device-plugin/blob/v0.19.3/README.md#shared-access-to-gpus)
- [MIG Manager v0.14.2 固定 tag](https://github.com/NVIDIA/mig-parted/tree/v0.14.2)
- [NVIDIA MIG User Guide](https://docs.nvidia.com/datacenter/tesla/mig-user-guide/)
- [Kueue v0.18.3 release](https://github.com/kubernetes-sigs/kueue/releases/tag/v0.18.3)
- [Kueue commit afd60c3](https://github.com/kubernetes-sigs/kueue/tree/afd60c3)
- [Kueue v1beta2 API](https://kueue.sigs.k8s.io/docs/reference/kueue.v1beta2/)
- [Kueue ClusterQueue](https://kueue.sigs.k8s.io/docs/concepts/cluster_queue/)
- [Kueue Cohort](https://kueue.sigs.k8s.io/docs/concepts/cohort/)
- [Kubernetes ResourceQuota](https://kubernetes.io/docs/concepts/policy/resource-quotas/)
- [Kubernetes Dynamic Resource Allocation](https://kubernetes.io/docs/concepts/scheduling-eviction/dynamic-resource-allocation/)

### 2.3 关于“当前”的严格说法

本章说“当前”时，只表示上述固定版本和核对日期。

它不表示：

```text
任何未来Kueue版本都使用相同字段
任何GPU型号都支持相同MIG profile
任何云厂商都允许同样的MIG mode切换
任何Operator安装都启用了相同operand
任何DRA driver都暴露相同DeviceClass
```

生产执行前仍要核对：

```text
实际镜像digest
Helm release values
ClusterPolicy
CRD served/storage版本
Node GPU型号、driver、firmware
Device Plugin ConfigMap
MIG Manager ConfigMap和日志
Kueue feature gates与controller配置
云厂商节点生命周期限制
```

---

## 3. 企业方案决策矩阵：先问隔离与故障域，再问利用率

### 3.1 五种方案放在同一张表里

| 维度 | 整卡独占 | MIG | time-slicing | MPS | NVIDIA DRA |
|---|---|---|---|---|---|
| Kubernetes广告 | 通常`nvidia.com/gpu`整数 | `single`统一资源或`mixed`按profile资源名 | 一个物理资源乘`replicas`，可改名`.shared` | 完整GPU资源乘replicas，当前插件仅`nvidia.com/gpu` | 通过DeviceClass/ResourceClaim选择设备，取决于driver |
| 物理数量变化 | 不变 | 不变，只改变卡内几何 | 不变 | 不变 | 不变 |
| 显存隔离 | 一个Pod/容器独占时最清晰 | 硬件分区提供较强隔离 | 无 | 有MPS额度控制，但不是MIG硬隔离 | 取决于所选设备和driver配置 |
| 计算隔离 | 独占时确定性最高 | profile提供硬件资源分区 | 无稳定份额保证 | 当前插件按replicas平均计算额度 | 取决于设备、claim和driver |
| 故障域 | 一张卡/一个任务为主 | slice隔离较强，但同物理卡仍共享卡级故障域 | 同卡进程共享reset/Xid/显存压力故障域 | 仍共享物理卡和MPS服务故障域 | 取决于最终分配的物理设备 |
| 性能确定性 | 高 | 较高，但受profile与同卡公共部件影响 | 低，邻居噪声明显 | 高于纯time-slicing，但不要承诺等同MIG | 取决于分配模式，不是DRA名字自动保证 |
| 重配置代价 | 低到中 | 高；需清空GPU client，可能重启 | 中；改插件配置并滚动验证 | 中；改sharing方法与daemon行为 | 中到高；API、driver、claim生命周期均要治理 |
| 监控归属 | device ID到Pod通常一对一 | GI/CI/MIG UUID到Pod | 多Pod共享同一device，不能复制后求和 | 需要进程/MPS/device多层视角 | ResourceClaim、allocation、device ID联合 |
| 不互信租户 | 节点/卡独占更合适 | 可作为硬件分区层，但仍需系统安全控制 | 不适合硬隔离诉求 | 不应把实验性MPS当安全边界 | 取决于driver与设备模式，仍需其他安全层 |
| 适合 | 严格SLO在线推理、大模型整卡训练 | 可预测的小规格推理、分区批任务 | 可信内部低风险开发、短任务、吞吐优先 | 可信任务且希望比time-slicing更可控 | 需要结构化设备声明、动态选择和新API能力 |
| 不适合 | 小任务多且碎片严重 | 频繁变几何、型号不支持、跨profile弹性要求高 | 不互信、严格延迟、显存硬隔离 | MIG混用、不互信、要求成熟稳定边界 | 团队尚无CRD/driver/claim运维能力时盲目迁移 |

### 3.2 选择顺序

不要先问：

```text
哪种方案能把利用率图做得最高？
```

先依次问：

1. 租户是否互相信任？
2. 业务是否有严格 TTFT、ITL 或训练时限？
3. 单任务显存上限是否可预测？
4. 单卡故障最多允许影响多少业务？
5. 是否接受维护窗口和节点重启？
6. 当前 GPU 型号是否支持所需 MIG profile？
7. 监控能否把物理设备、slice、进程和 Pod 正确关联？
8. 成本制度是按物理占用、预留、业务产出还是内部转移定价？

然后才选择共享方式。

### 3.3 “共享访问”不等于“切出多块 GPU”

用一间厨房类比：

```text
整卡独占
  = 一支团队独占厨房

MIG
  = 厨房被硬隔断成多个带独立工作台和储物区的隔间

time-slicing
  = 给更多厨师发门卡，大家轮流或并发使用同一厨房

MPS
  = 仍是同一厨房，但有一名管理员控制每组可使用的空间和时间额度

DRA
  = 用更结构化的申请单描述“我要哪类厨房设备”，由driver完成分配
```

发 10 张门卡不会把厨房变成 10 间。

这就是 replicas 最需要用大白话解释的地方。

---

## 4. MIG：先看清 GI、CI 和 profile

### 4.1 MIG 是什么

MIG 是 Multi-Instance GPU。

大白话：

> 在支持的 NVIDIA GPU 上，把一张物理卡的部分计算单元、显存和相关硬件资源划成可独立使用的实例。

它不是 Kubernetes 发明的。

Kubernetes 看到的是 NVIDIA 栈最终广告出来的资源。

### 4.2 三层对象

```text
Physical GPU
  └─ GPU Instance，GI
       └─ Compute Instance，CI
            └─ CUDA workload
```

#### GPU Instance

GI 是物理 GPU 内的一组资源分区。

它决定：

- 分到多少 GPU slice；
- 分到多少显存及内存带宽相关资源；
- 能创建哪些 CI；
- 对外可形成什么 MIG 设备。

#### Compute Instance

CI 位于 GI 内。

它把 GI 的计算资源进一步组织成可运行计算上下文的实例。

在日常 Kubernetes 运维里，你通常不需要手工编排每个 CI。

但必须知道：

```text
MIG设备
  != 一张新物理卡
  = 物理卡内GI/CI形成的可分配设备实例
```

### 4.3 profile 名称怎么读

示意名称：

```text
1g.10gb
2g.20gb
3g.40gb
```

读法：

- `1g`、`2g`、`3g` 表示该 profile 使用的 GPU slice 规模；
- `10gb`、`20gb`、`40gb` 表示该 profile 的显存规格；
- 具体可用组合依赖 GPU 型号和 MIG generation；
- 不能看到 `1g.10gb` 就假设任何 A100、H100、H200 或未来型号都支持；
- 不能把 profile 名称当作线性性能承诺。

错误推理：

```text
2g.20gb
  -> 一定是1g.10gb的两倍吞吐
```

实际还受以下因素影响：

```text
模型结构
batch size
kernel形态
显存带宽
Tensor Core路径
CPU与PCIe供给
同卡公共部件
软件版本
```

### 4.4 MIG 能保证什么

可以把它理解为“比进程轮流使用更接近硬件切片”。

它能提供的核心价值包括：

- 分开的显存地址空间和容量边界；
- 分区后的计算资源；
- 更清晰的设备枚举和资源请求；
- 相比纯 time-slicing 更可预测的干扰边界；
- 一个租户不能简单通过申请更多共享槽位吃掉整卡显存。

### 4.5 MIG 不能保证什么

它不能让人忽略物理卡：

- 多个 MIG instance 仍在同一张物理 GPU；
- 某些卡级 Xid、reset、掉总线、供电或温度故障可能影响整卡实例；
- PCIe、CPU、NUMA、网络、存储仍可能共享；
- 驱动、容器运行时、Node OS 仍是共同软件栈；
- Kubernetes Namespace、RBAC、网络与 Secret 安全不会由 MIG 自动完成；
- profile 不能保证业务吞吐严格线性；
- 重配几何会影响该节点上的 GPU client。

因此更准确的说法是：

```text
MIG提高卡内资源隔离与可预测性
  != 每个slice成为完全独立的物理服务器
```

---

## 5. `single` 与 `mixed`：资源名会改变调度事实

### 5.1 `single` 策略

在 `single` 策略下，节点上的 MIG 使用统一资源语义。

典型调度请求仍可能表现为：

```yaml
resources:
  limits:
    nvidia.com/gpu: 1
```

它适合：

- 节点内几何统一；
- 平台希望屏蔽 profile 资源名；
- 业务只需表达“一个统一规格实例”；
- 节点池通过标签和准入保证规格一致。

风险是：

> 如果不同节点实际 profile 或性能基线没有被平台约束，业务只看 `nvidia.com/gpu: 1` 可能误以为所有实例等价。

### 5.2 `mixed` 策略

在 `mixed` 策略下，不同 MIG profile 以不同扩展资源名出现。

示意：

```yaml
resources:
  limits:
    nvidia.com/mig-1g.10gb: 1
```

以及：

```yaml
resources:
  limits:
    nvidia.com/mig-3g.40gb: 1
```

具体资源名必须从现场 Node Capacity/Allocatable 获取。

不要凭文档示意猜名字。

### 5.3 mixed 的调度后果

假设节点有：

```text
nvidia.com/mig-1g.10gb = 4
nvidia.com/mig-3g.40gb = 1
```

业务请求：

```text
nvidia.com/gpu = 1
```

不能得出“总共有 5 个设备，所以一定可调度”。

扩展资源名是严格匹配的。

对 kube-scheduler 来说：

```text
请求nvidia.com/gpu
  != 请求nvidia.com/mig-1g.10gb
  != 请求nvidia.com/mig-3g.40gb
```

这会在 `NodeResourcesFit` 阶段形成资源不满足。

### 5.4 一个 Java 平台类比

把它类比成：

```text
普通CPU节点
  != 大内存节点
  != ARM节点
```

即使都能运行容器，资源与节点约束也不能互换。

但 GPU 更严格：

> 扩展资源名本身就进入 requests/limits 和 scheduler 资源账，不只是一个偏好标签。

### 5.5 上线前的资源名契约

平台必须发布一个受版本控制的资源目录：

| 资源名 | 物理含义 | 节点池 | MIG策略 | 是否共享 | SLO等级 | 允许租户 |
|---|---|---|---|---|---|---|
| `nvidia.com/gpu` | 整卡或single策略实例，按池说明 | online-h100 | none/single | 否 | strict | online |
| `nvidia.com/mig-1g.10gb` | 指定profile | batch-h100-mig | mixed | 否 | normal | batch |
| `nvidia.com/gpu.shared` | time-slicing访问槽 | dev-shared | none | 是 | best-effort | trusted-dev |

同名资源如果在不同节点池代表完全不同服务等级，会制造长期事故。

---

## 6. MIG Manager：改 label 前必须由平台先清空业务 GPU workload

### 6.1 控制链

GPU Operator `v26.3.3` 下，MIG Manager 的核心链路是：

```text
观察Node标签nvidia.com/mig.config
  -> 标记mig.config.state=pending
  -> 停止Operator管理且访问GPU的operands
  -> 如配置了host clients，停止相应systemd服务
  -> 启用或调整MIG mode
  -> 必要时进入rebooting并重启Node
  -> 使用mig-parted应用MIG geometry
  -> 重启GPU operands与host clients
  -> 成功标记success，失败标记failed
```

这不是：

```text
kubectl label成功
  -> MIG立刻无损切换
```

### 6.2 MIG Manager 自动停止谁，又不会替你停止谁

至少考虑：

- NVIDIA Device Plugin；
- GPU Feature Discovery；
- DCGM Exporter；
- 其他 Operator 管理且访问 GPU 的 operand；
- 配置在 GPU clients ConfigMap 中的 host systemd service。

官方要求重配置前没有用户 GPU workload。

必须明确：

```text
MIG Manager自动停止Operator-managed GPU operands
  + 配置过的host systemd clients
  != 自动清空普通业务GPU Pods
```

平台必须在改 label 之前：

1. 通知业务并完成 checkpoint；
2. cordon 精确节点；
3. 停止、迁移或按批准流程排空业务 GPU Pod；
4. 检查所有 namespace 的 `spec.nodeName`；
5. 验证业务 GPU workload 数量为 0；
6. 检查未被 ConfigMap 管理的 host CUDA client；
7. 然后才触发 `nvidia.com/mig.config` 变更。

不能依赖 MIG Manager 替你安全停止训练任务。

### 6.3 每节点动态 ConfigMap

`26.3` 的常规路径会动态生成每节点 MIG 配置。

默认入口形如：

```text
<node-name>-mig-config
```

它的意义是：

- MIG Manager 按该节点可用硬件生成标准 profile；
- 不同 GPU 型号节点不再被迫共享一份假定完全相同的静态配置；
- 平台仍可以配置自定义 ConfigMap；
- 自动生成不等于可以跳过变更评审。

但“常规路径”不是“唯一合法路径”。GPU Operator `26.3` 还存在两个必须知道的边界：

1. 对于旧驱动分支的特定组合，例如文档点名的 `535` 驱动且节点当前未启用 MIG 时，动态发现可用 MIG profile 的条件可能不成立；此时 Operator 会使用静态 `default-mig-parted-config` 作为回退来源；
2. 平台可以通过 `ClusterPolicy.spec.migManager.config.name` 指向自定义 ConfigMap，并通过同一配置下的 `default` 指定默认几何；这时实际配置源既不一定是每节点 ConfigMap，也不一定是内置默认 ConfigMap。

所以，查不到 `<node-name>-mig-config` 不能直接下结论说“MIG Manager 未部署”。而且，内置回退 ConfigMap 即使存在，也不代表它就是当前生效源：显式配置的自定义源优先级更高。正确判断顺序是：

```text
先查ClusterPolicy.spec.migManager.config.name/default
  -> name显式非空：优先读取它指向的ConfigMap
  -> name为空：再查每节点动态ConfigMap
  -> 每节点源也没有：再查default-mig-parted-config静态回退
  -> 必要时核对MIG Manager DaemonSet实际参数
  -> 最后用Node状态、日志和资源广告确认哪条路径正在生效
```

`default-mig-parted-config` 是兼容回退，不是要求所有集群长期固定使用它。驱动、Operator 或 ClusterPolicy 变化后，都要重新确认配置来源，不能把上一次巡检结论永久缓存。

值班时应同时查：

```text
Node nvidia.com/mig.config
Node nvidia.com/mig.config.state
按ClusterPolicy显式引用 -> per-node动态 -> 静态fallback确认的实际MIG配置源
MIG Manager Pod与日志
Device Plugin重建后的Capacity/Allocatable
GFD标签
nvidia-smi -L或经批准的节点设备清单
```

### 6.4 状态机怎么解释

| state | 大白话 | 值班动作 |
|---|---|---|
| `pending` | 控制器已接单，正在停组件或准备重配 | 不要恢复业务调度；看日志和对象变化 |
| `rebooting` | 该变更需要或正在等待节点重启 | 查云平台/Node生命周期与重启进度 |
| `success` | MIG Manager声明本次配置成功 | 仍需核对设备、资源广告、监控与canary |
| `failed` | 重配链路失败 | 停止扩散，保留日志，按已知良好配置回滚 |

`success` 仍不等于业务验收通过。

完整验收是：

```text
state=success
  + Node Ready
  + GPU operands Ready
  + 资源名与数量正确
  + PodResources能看到设备
  + DCGM实体与指标正确
  + canary加载模型并完成业务探针
```

### 6.5 MIG 变更前的只读检查

下面脚本只读取对象。

它要求操作者显式提供 context 和精确 Node 名。

```powershell
param(
    [Parameter(Mandatory = $true)]
    [string]$ExpectedContext,

    [Parameter(Mandatory = $true)]
    [string]$NodeName,

    [string]$OperatorNamespace = "gpu-operator"
)

$ErrorActionPreference = "Stop"

function Test-Command {
    param([Parameter(Mandatory = $true)][string]$Name)
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "Required command not found: $Name"
    }
}

Test-Command -Name "kubectl"

function Invoke-Kubectl {
    $KubectlArguments = @($args)
    $Output = & kubectl @KubectlArguments
    $ExitCode = $LASTEXITCODE
    if ($ExitCode -ne 0) {
        throw "kubectl failed with exit code $ExitCode. args=$($KubectlArguments -join ' ')"
    }
    return $Output
}

$ActualContext = Invoke-Kubectl config current-context
if ($ActualContext -ne $ExpectedContext) {
    throw "Context mismatch. expected=$ExpectedContext actual=$ActualContext"
}

$NodeCount = [int](Invoke-Kubectl --context $ExpectedContext get node $NodeName --no-headers 2>$null |
    Measure-Object |
    Select-Object -ExpandProperty Count)
if ($NodeCount -ne 1) {
    throw "Expected exactly one Node named $NodeName, found $NodeCount"
}

Invoke-Kubectl --context $ExpectedContext get node $NodeName -o yaml
Invoke-Kubectl --context $ExpectedContext get pods -A --field-selector "spec.nodeName=$NodeName" -o wide

$ClusterPolicyJson = Invoke-Kubectl --context $ExpectedContext get clusterpolicy -o json
$ClusterPolicyList = ($ClusterPolicyJson -join "`n") | ConvertFrom-Json
$ClusterPolicies = @($ClusterPolicyList.items)

if ($ClusterPolicies.Count -ne 1) {
    throw "Expected exactly one ClusterPolicy, found $($ClusterPolicies.Count). Resolve the operator control source first."
}

$ClusterPolicy = $ClusterPolicies[0]
$ConfiguredConfigName = [string]$ClusterPolicy.spec.migManager.config.name
$ConfiguredDefaultProfile = [string]$ClusterPolicy.spec.migManager.config.default

Write-Host "=== ClusterPolicy MIG config selection ==="
[pscustomobject]@{
    ClusterPolicy = $ClusterPolicy.metadata.name
    ConfigMapName = $ConfiguredConfigName
    DefaultProfile = $ConfiguredDefaultProfile
} | Format-List

if (-not [string]::IsNullOrWhiteSpace($ConfiguredConfigName)) {
    $ConfiguredConfigRef = Invoke-Kubectl --context $ExpectedContext get configmap -n $OperatorNamespace $ConfiguredConfigName --ignore-not-found -o name

    if ([string]::IsNullOrWhiteSpace(($ConfiguredConfigRef -join "`n"))) {
        Invoke-Kubectl --context $ExpectedContext get daemonset -n $OperatorNamespace -o yaml
        throw "ClusterPolicy explicitly selects ConfigMap $ConfiguredConfigName, but it was not found in namespace $OperatorNamespace. Do not silently fall back."
    }

    Write-Host "=== ClusterPolicy-selected MIG config: $ConfiguredConfigName ==="
    Invoke-Kubectl --context $ExpectedContext get configmap -n $OperatorNamespace $ConfiguredConfigName -o yaml
}
else {
    $PerNodeConfigName = "$NodeName-mig-config"
    $PerNodeConfigRef = Invoke-Kubectl --context $ExpectedContext get configmap -n $OperatorNamespace $PerNodeConfigName --ignore-not-found -o name

    if (-not [string]::IsNullOrWhiteSpace(($PerNodeConfigRef -join "`n"))) {
        Write-Host "=== Per-node dynamic MIG config: $PerNodeConfigName ==="
        Invoke-Kubectl --context $ExpectedContext get configmap -n $OperatorNamespace $PerNodeConfigName -o yaml
    }
    else {
        $FallbackConfigName = "default-mig-parted-config"
        $FallbackConfigRef = Invoke-Kubectl --context $ExpectedContext get configmap -n $OperatorNamespace $FallbackConfigName --ignore-not-found -o name

        if (-not [string]::IsNullOrWhiteSpace(($FallbackConfigRef -join "`n"))) {
            Write-Warning "ClusterPolicy has no explicit config name and the per-node config is absent; reading static fallback $FallbackConfigName. Confirm whether the driver/MIG state requires this path."
            Invoke-Kubectl --context $ExpectedContext get configmap -n $OperatorNamespace $FallbackConfigName -o yaml
        }
        else {
            Write-Warning "ClusterPolicy has no explicit config name, and neither the per-node source nor the built-in static fallback exists. Inspect the MIG Manager DaemonSet output below."
            Invoke-Kubectl --context $ExpectedContext get daemonset -n $OperatorNamespace -o yaml
            throw "MIG config source is unresolved; do not approve a MIG geometry change."
        }
    }
}

Invoke-Kubectl --context $ExpectedContext get pods -n $OperatorNamespace -o wide
```

安全边界：

- 脚本不执行 label、cordon、drain、delete 或 reboot；
- `OperatorNamespace` 必须以实际安装命名空间为准；
- `--ignore-not-found` 只把“对象确实不存在”转换为空结果；鉴权失败、API 不可达等其他 `kubectl` 错误仍由包装函数立即中止，不能被误判为回退条件；
- 脚本始终先读 `ClusterPolicy.spec.migManager.config.name/default`；只要 `name` 显式非空，就优先读取该引用，不能因为内置回退对象也存在而误报生效源；
- 某些安装会使用自定义 ConfigMap 或不同命名空间，必须继续核对 MIG Manager 实际参数与日志；
- 每节点 ConfigMap 缺失可能是 `535` 等旧驱动与 MIG disabled 组合触发的静态回退，也可能是自定义配置，不能自动解释成“能力不存在”；
- 如果显式引用不存在，或在没有显式引用时动态源与静态回退都不存在，脚本会输出 DaemonSet 后失败关闭；先解析真实配置源，再谈变更；
- 没有 GPU Operator 的学习集群可以把查不到这些对象记录为环境事实，但不能据此模拟通过生产变更审批。

### 6.6 MIG 几何变更 runbook

#### 前置检查

1. 记录 context、Node UID、provider ID、GPU UUID、型号、driver；
2. 读取当前 `mig.config`、`mig.config.state` 和资源广告；
3. 按“ClusterPolicy 显式引用 -> 每节点动态 ConfigMap -> `default-mig-parted-config`”的优先级确认实际配置源，并以 DaemonSet 参数和日志复核；
4. 确认目标 profile 被该型号支持；
5. 检查是否有 GPU Pod、host CUDA client、MPS daemon，并把业务 GPU workload=0 设为硬断言；
6. 与任务方确认 checkpoint 和停止窗口；
7. 确认 Node 可重启以及云平台不会更换错误实例；
8. 准备已知良好配置名和回滚窗口。

#### Canary

只选择一台精确节点。

必须确认：

```text
NodeName精确
Node UID精确
节点已cordon
业务已checkpoint或确认可终止
所有namespace的GPU业务Pod数量为0
host GPU client已停止
同池其他节点仍有容量
```

#### 变更动作

生产变更必须由审批系统生成。

下面仅展示 `server-side dry-run` 形态：

```powershell
param(
    [Parameter(Mandatory = $true)]
    [string]$ExpectedContext,

    [Parameter(Mandatory = $true)]
    [string]$NodeName,

    [Parameter(Mandatory = $true)]
    [string]$ExpectedNodeUID,

    [Parameter(Mandatory = $true)]
    [string]$TargetMigConfig
)

$ErrorActionPreference = "Stop"

if (-not (Get-Command kubectl -ErrorAction SilentlyContinue)) {
    throw "kubectl not found"
}

function Invoke-Kubectl {
    $KubectlArguments = @($args)
    $Output = & kubectl @KubectlArguments
    $ExitCode = $LASTEXITCODE
    if ($ExitCode -ne 0) {
        throw "kubectl failed with exit code $ExitCode. args=$($KubectlArguments -join ' ')"
    }
    return $Output
}

$ActualContext = Invoke-Kubectl config current-context
if ($ActualContext -ne $ExpectedContext) {
    throw "Context mismatch. expected=$ExpectedContext actual=$ActualContext"
}

$PatchOperations = @(
    @{
        op = "test"
        path = "/metadata/uid"
        value = $ExpectedNodeUID
    },
    @{
        op = "add"
        path = "/metadata/labels/nvidia.com~1mig.config"
        value = $TargetMigConfig
    }
)
$JsonPatch = $PatchOperations | ConvertTo-Json -Compress

Invoke-Kubectl --context $ExpectedContext patch node $NodeName `
    --type=json `
    --patch $JsonPatch `
    --dry-run=server `
    -o yaml
```

这不会执行真实 patch。

`test /metadata/uid` 与修改 MIG label 位于同一个 JSON Patch 请求。

如果名称相同的 Node 已被重建、UID 发生变化，`test` 失败，后续 `add` 不会执行，从而关闭“先 GET UID、再按 name 修改”之间的 TOCTOU 竞态窗口。

JSON Pointer 中的 `~1` 表示 label key 里的 `/`，因此目标才是 `nvidia.com/mig.config`。

真实执行必须在维护窗口内，由审批系统复用同一份、包含 UID `test` 的原子 JSON Patch。

审批系统应显式生成执行模式和完整审计，不能让操作者把本段命令简单删掉 `--dry-run=server` 后手工执行，更不能退回裸 `kubectl label`。

#### 停止扩散条件

任一条件触发立即暂停后续节点：

- 状态长时间停在 `pending`；
- 进入意外 `rebooting`；
- `failed`；
- Node UID 或 provider ID 改变；
- driver/operator operand 未恢复；
- Capacity/Allocatable 与目标不符；
- DCGM、PodResources 或 canary 失败；
- 同池可用容量跌破业务安全线。

#### 验收

```text
mig.config.state=success
Node Ready=True
目标资源名与数量正确
非目标资源未意外消失
Device Plugin/GFD/DCGM Ready
PodResources映射正确
canary完成GPU计算和业务探针
在线池SLO无回归
```

#### 回滚

回滚不是删 label。

它是：

1. 保持 cordon；
2. 应用已知良好 `mig.config`；
3. 允许必要的重启；
4. 等待 `success`；
5. 重做资源、指标和 canary 验收；
6. 经审批再 uncordon。

#### 证据包

```text
变更单与审批人
Node YAML前后快照
按ClusterPolicy显式引用 -> per-node动态 -> 静态fallback确认的实际MIG配置源
MIG Manager日志
Operator operand状态
Capacity/Allocatable差异
PodResources快照
DCGM实体和关键metric
canary日志
业务SLO
回滚或放量决定
```

---

## 7. time-slicing：80 个资源只是 80 个访问槽

### 7.1 配置语义

固定 `v0.19.3` 的示意配置：

```yaml
version: v1
sharing:
  timeSlicing:
    renameByDefault: true
    failRequestsGreaterThanOne: true
    resources:
    - name: nvidia.com/gpu
      replicas: 10
```

若节点有 8 张完整 GPU：

```text
物理GPU = 8
replicas = 10
逻辑访问资源 = 8 × 10 = 80
```

这个 `8×10` 只是在 `name: nvidia.com/gpu` 下共享 8 张完整 GPU 的例子。

time-slicing 也可以配置支持的 mixed MIG 资源名。

这时应分别计算：

```text
某MIG profile的逻辑访问资源
  = 该profile当前MIG实例数 × replicas
```

不能用物理整卡数直接乘 replicas，也不能把不同 MIG profile 相加后统称整卡。

因为 `renameByDefault=true`，示意资源名会成为：

```text
nvidia.com/gpu.shared
```

调用方应该请求：

```yaml
resources:
  limits:
    nvidia.com/gpu.shared: 1
```

### 7.2 replicas 不是什么

`replicas: 10` 不表示：

```text
每个Pod固定10% SM
每个Pod固定10%显存
每个Pod固定10%显存带宽
每个Pod固定10%功耗预算
每个Pod的P99互不干扰
每个Pod有独立reset域
```

它只改变 Device Plugin 向 kubelet 广告的可分配逻辑数量。

CUDA 进程仍共享同一物理 GPU 的执行与显存环境。

### 7.3 为什么推荐 `failRequestsGreaterThanOne=true`

默认 `false` 时，一个容器可以请求多个共享资源。

但：

```text
请求2个共享槽
  != 获得2倍计算份额
```

这会诱导业务把普通扩展资源的“数量越多得到设备越多”直觉带入共享场景。

启用：

```yaml
failRequestsGreaterThanOne: true
```

后，单容器请求超过 1 个该共享资源会在分配阶段失败。

它是一条防误用保护。

它不是性能隔离。

### 7.4 `renameByDefault` 为什么值得开启

不开启时：

```text
独占整卡：nvidia.com/gpu
time-slicing：nvidia.com/gpu
```

资源名相同，语义可能完全不同。

开启后：

```text
共享访问：nvidia.com/gpu.shared
```

这让以下控制更容易：

- Admission Policy 阻止严格 SLO 业务请求 `.shared`；
- ResourceQuota 分开限制共享池；
- Kueue 建立独立 flavor；
- 成本系统不把共享槽当独占卡；
- Dashboard 给共享工作负载明显标识。

### 7.5 GFD 标签如何帮助识别

GPU Feature Discovery 会为节点发布与 sharing 相关的标签。

现场应读取而不是背诵猜值：

```text
nvidia.com/gpu.sharing-strategy
nvidia.com/gpu.replicas
nvidia.com/gpu.product
```

当 `renameByDefault=false` 时，产品标签可能带共享语义后缀，具体以固定版本和现场 Node 标签为准。

重要的是建立验证：

```text
Device Plugin配置
  <-> GFD标签
  <-> Node Capacity/Allocatable
  <-> Pod实际请求资源名
```

四者不一致时先停止扩散。

### 7.6 time-slicing 适合什么

适合：

- 同一组织内互信的开发环境；
- 单任务短、显存可控；
- 对延迟抖动不敏感；
- 吞吐和设备复用优先；
- 有明确 OOM/Xid 共享故障处置；
- 监控能区分物理设备与共享 Pod。

不适合：

- 不互信租户；
- 严格在线推理 TTFT/ITL/P99；
- 单进程可能不受控吃满显存；
- 需要硬件显存隔离；
- 业务把 GPU 数量作为性能承诺；
- 无法承受一张卡影响多个 Pod。

### 7.7 共享池的最小平台契约

```text
资源名必须带.shared
单容器最多请求1
只允许trusted-dev namespace
禁止hostPID与特权容器
模型大小和显存预算必须准入
在线strict-SLO工作负载拒绝进入
物理device指标不得复制后求和
Xid处置按物理GPU故障域
成本按物理卡时与约定权重分摊
```

### 7.8 time-slicing/MPS 变更 runbook

#### 前置检查

1. 固定 Device Plugin ConfigMap 名、namespace 和 checksum；
2. 列出目标 NodeSelector 命中的精确 Node；
3. 记录每个 Node 的物理 GPU 数和当前广告数；
4. 检查 MIG 策略；MPS 不得与 MIG 混用；
5. 检查线上严格 SLO Pod 是否误入目标池；
6. 确认 ResourceQuota、Kueue flavor 与成本规则同步变更；
7. 准备旧 ConfigMap 和旧 DaemonSet revision。

#### Canary

先建立单节点 canary 池。

不要直接修改覆盖全部 GPU Node 的默认配置。

canary 验收：

```text
GFD sharing标签
Node Capacity/Allocatable
请求1个.shared资源能启动
请求2在failRequestsGreaterThanOne=true时按预期失败
同卡并发显存与延迟实测
进程退出恢复；Xid仅做桌面推演或历史证据复盘，不主动注入
DCGM与PodResources归属
成本系统只计一次物理设备
```

这里“按预期失败”必须来自你自己的实验记录。

本章不声称已在你的集群通过。

#### 停止扩散

- 资源名未按预期改为 `.shared`；
- 逻辑数量与 `物理数×replicas` 不一致；
- 非目标节点发生变化；
- Device Plugin Allocate 错误；
- 在线延迟越过 SLO；
- 共享 Pod 显存互相挤压不可接受；
- 指标产生重复归因；
- 回滚后资源广告不能恢复。

#### 回滚

1. 停止新工作负载进入共享池；
2. 等待或迁移 canary 任务；
3. 恢复旧配置；
4. 滚动 Device Plugin/GFD；
5. 检查 Capacity/Allocatable；
6. 删除或拒绝已过时资源名的业务模板；
7. 重做监控和成本对账；
8. 再解除节点隔离。

---

## 8. MPS：比 time-slicing 多一层控制，但不是 MIG

### 8.1 MPS 是什么

MPS 是 CUDA Multi-Process Service。

大白话：

> 多个 CUDA 进程通过 MPS control daemon 更协调地共享一张 GPU，而不是只靠普通进程时间交错。

在 k8s-device-plugin `v0.19.3` 的当前边界下：

- MPS sharing 仍是实验性能力；
- MPS 与 time-slicing 互斥；
- 同一节点只选一种 sharing 方法；
- MPS 不支持 MIG；
- 只支持完整 `nvidia.com/gpu`；
- 插件按 replicas 为共享客户端提供平均内存和计算额度的空间分区。

### 8.2 比 time-slicing 多了什么

time-slicing 更接近：

```text
增加可访问同一设备的逻辑名额
```

MPS 在此基础上多了：

```text
MPS control daemon
  + 客户端协调
  + 按replicas平均的内存额度
  + 按replicas平均的计算额度
```

因此它能减少部分“一个客户端无边界吃掉全部资源”的问题。

### 8.3 为什么仍不能写成硬隔离

MPS 仍然：

- 共享一张物理 GPU；
- 依赖同一个 MPS 服务；
- 共享卡级故障域；
- 不等于 MIG 的硬件分区；
- 不自动完成 Namespace、进程权限和网络隔离；
- 当前插件支持处于实验性边界；
- 不能和 MIG 混用来获得“二次切分”。

所以决策表达应是：

```text
可信内部任务
  + 需要比time-slicing更明确的额度
  + 接受实验性运维边界
  -> 可以评估MPS

不互信租户
  或严格硬件隔离
  -> 不把MPS当安全边界
```

### 8.4 MPS 配置示意

以下只用于固定版本实验环境审阅，不得直接 apply 生产：

```yaml
version: v1
sharing:
  mps:
    renameByDefault: true
    resources:
    - name: nvidia.com/gpu
      replicas: 4
```

`v0.19.3` 的 MPS schema 没有 time-slicing 的 `failRequestsGreaterThanOne` 同名字段。

因此不能复制 time-slicing 配置把该字段塞进 `sharing.mps`。

平台若要限制 MPS 单容器请求数量，必须使用经过版本审阅的准入策略，并验证它与当前插件实际 Allocate 行为一致；不能声称插件内置了同名保护。

上线前必须核对：

```text
当前插件tag确为v0.19.3
节点没有MIG资源
timeSlicing配置已移除
GPU型号与driver受支持
MPS control daemon日志
Node Capacity/Allocatable变化
单客户端显存上限
计算隔离的实测范围
进程退出与节点重启恢复
DCGM/PodResources归属
```

---

## 9. NVIDIA DRA：解决设备声明，不自动解决隔离

### 9.1 为什么本章只讲边界

DRA 是 Dynamic Resource Allocation，动态资源分配。

大白话：

> 工作负载不只写一个整数扩展资源，而是通过 DeviceClass、ResourceClaim 等对象更结构化地声明和取得设备。

这能表达：

- 设备类别；
- 选择条件；
- claim 生命周期；
- driver 特定的设备配置；
- 共享或管理策略，取决于具体 NVIDIA DRA driver。

但 DRA 这个 API 名称本身不保证：

- 显存隔离；
- 性能隔离；
- 成本公平；
- MIG 几何无损变更；
- 队列公平；
- 多租户安全。

最终仍要看：

```text
DeviceClass
ResourceClaim
NVIDIA DRA driver
分配到的实际物理设备或MIG实例
节点与运行时注入
监控和回收状态
```

### 9.2 与传统扩展资源共存

迁移期可能同时存在：

```text
传统扩展资源nvidia.com/gpu
  + DRA DeviceClass/ResourceClaim
```

平台要避免：

- 同一物理设备被两套管理路径重复广告；
- 成本系统只识别旧资源；
- ResourceQuota 只限制旧扩展资源，DRA claim 无治理；
- Kueue flavor 与 DeviceClass 选择互相矛盾；
- 业务不知道自己使用的是哪种 API。

### 9.3 DRA 的 ResourceQuota 边界

当前 Kubernetes DRA 支持按 DeviceClass 管理 quota。

显式 ResourceClaim 按 DeviceClass 计数时，官方定义的 quota key 形态是：

```text
<device-class-name>.deviceclass.resource.k8s.io/devices
```

实际 DeviceClass 名必须从集群读取，不能在章内捏造一个 NVIDIA DeviceClass 名。

若启用了 DRA extended resource allocation：

- DeviceClass 配置了 `spec.extendedResourceName` 时，可用 `requests.<that-extended-resource-name>` 做 quota；
- 没有显式扩展资源名时，可使用派生资源 `requests.deviceclass.resource.kubernetes.io/<device-class-name>`；
- 官方当前说明，从 ResourceClaim 或扩展资源请求得到的设备会同时计入适用的这些 quota；
- 同一个扩展资源名还可能由传统 Device Plugin 在其他节点提供，因此迁移期必须审计实际提供者。

这与显式 ResourceClaim 路径的治理边界必须按现场 feature gate 和 API 版本核对。

安全验证顺序：

```text
kubectl api-resources
  -> 查DeviceClass/ResourceClaim是否served
  -> 列出实际DeviceClass名字
  -> 查spec.extendedResourceName
  -> 查ResourceQuota hard/used
  -> 用server-side dry-run验证对象
```

传统 `requests.nvidia.com/gpu` quota 与 DRA DeviceClass quota 可以共存。

共存不代表它们会自动相互折算。

---

## 10. 三本数量账：不要再问“到底还剩几张卡”

### 10.1 第一本：传统 Device Plugin 的 Node Capacity/Allocatable

传统 Device Plugin 路径的来源：

```text
Device Plugin ListAndWatch
  -> kubelet DeviceManager
  -> Node.status.capacity/allocatable
```

在传统 Device Plugin 路径中，它回答：

> 这个 Node 当前向 kube-scheduler 广告多少个特定资源名的可分配单位？

它不直接回答：

- 集群有多少物理卡；
- 共享槽位的性能份额；
- Namespace 还能创建多少 Pod；
- Kueue 是否会 admit；
- 业务还剩多少预算；
- 一张卡当前是否真正空闲。

第 15–17 课已经逐层拆过这条链：

- [第15课：Device Plugin注册与资源广告](15_DevicePlugin_注册_ListAndWatch_Capacity_Allocatable.md)
- [第16课：DeviceManager分配与容器注入](16_DeviceManager_deviceID_Allocate与容器注入.md)
- [第17课：checkpoint、健康、PodResources与CDI](17_checkpoint_健康状态_PodResources与CDI恢复账本.md)

### 10.2 第二本：Namespace ResourceQuota

它回答：

> API Server 是否允许这个 namespace 再接纳相应资源请求？

传统扩展资源示意：

```yaml
apiVersion: v1
kind: ResourceQuota
metadata:
  name: gpu-requests
  namespace: team-a
spec:
  hard:
    requests.nvidia.com/gpu: "8"
```

这里要特别钉死：

```text
传统扩展资源配额
  -> 使用requests.nvidia.com/gpu
```

不要把 `limits.nvidia.com/gpu` 当成同等可替换 quota key。

扩展资源在 Pod 中通常要求 requests 与 limits 相等，或只写 limits 后由 Kubernetes 推导 request；但 ResourceQuota 的扩展资源治理仍看 requests key。

### 10.3 ResourceQuota 不核对物理容量

即使集群只有 8 张物理 GPU，也可以创建：

```yaml
spec:
  hard:
    requests.nvidia.com/gpu: "100"
```

API Server 不会因此替你报“集群只有 8 张”。

它只执行接纳规则。

所以：

```text
ResourceQuota hard=100
  != 集群容量100
  != 一定能调度100
```

### 10.4 第三本：Kueue quota

它回答：

> 这个批任务现在是否可以占用某个 ClusterQueue/flavor 的入场预算？

Kueue quota 可能比 ResourceQuota 小：

```text
Namespace ResourceQuota剩余8
Kueue nominal剩余2
  -> 当前最多容纳总请求量为2个该资源单位的Workload
  -> 只有每个Workload恰好请求1个单位时，才可能对应2个Workload
```

也可能相反：

```text
Kueue quota剩余8
Namespace ResourceQuota剩余0
  -> Job对象仍可能创建并被Kueue处理
  -> Job解除suspend后，Job controller创建Pod
  -> Pod在ResourceQuota admission被拒绝
  -> Job出现FailedCreate且可能没有Pod
```

还可能两边都有余量，但节点放不下：

```text
ResourceQuota剩余8
Kueue quota剩余8
Node上目标MIG resource=0
  -> Workload可能Admitted，Pod仍Pending
```

### 10.5 三账对照表

| 账本 | 作用域 | 写入者/控制者 | 关键对象 | 能证明什么 | 不能证明什么 |
|---|---|---|---|---|---|
| 传统扩展资源 Capacity/Allocatable | Node | kubelet + Device Plugin | Node status | 该扩展资源名的节点广告 | 物理卡数、业务SLO、DRA claim状态 |
| DRA设备可用与分配账 | Cluster/Node/Namespace | DRA driver + Kubernetes控制面 | DeviceClass、ResourceSlice、ResourceClaim allocation | 可选设备与claim分配 | 传统扩展资源Node账、业务SLO |
| ResourceQuota | Namespace | API admission | ResourceQuota | namespace接纳上限与used | 节点存在、Kueue会放行 |
| Kueue quota | ClusterQueue/Cohort | Kueue controller | ClusterQueue/Workload | 批任务配额预留与admission | kube-scheduler已找到Node |

### 10.6 第四本隐藏账：物理库存

前三本都不是物理资产账。

平台还必须维护：

```text
Node
GPU PCI Bus ID
GPU UUID
型号
显存
MIG capability
当前MIG mode/geometry
采购或云实例ID
成本率
保修与维护状态
```

在 time-slicing 下，Node Capacity 是 80，物理库存仍是 8。

成本、故障域和容量采购必须从物理库存出发。

### 10.7 DRA 为什么不能硬塞进传统 Node 账

DRA 的主要对象链是：

```text
DeviceClass
  -> ResourceSlice广告设备
  -> ResourceClaim声明需求
  -> scheduler/driver形成allocation
```

它一般不把这条设备账简单写成传统 Device Plugin 的 `Node.status.capacity/allocatable` 数量。

若启用了 DRA extended resource allocation，则 Pod 可以继续用扩展资源请求形式，但仍要辨认背后提供者是 DRA 还是 Device Plugin。

因此盘点 DRA 时主要查：

```text
DeviceClass
ResourceSlice
ResourceClaim spec/status/allocation
Pod resourceClaims或扩展资源请求
feature gates
driver状态
```

### 10.8 一次查询不能替代时间序列

假设你在 10:00 查：

```text
Node Allocatable = 80
ResourceQuota used = 56
ClusterQueue admitted usage = 48
```

不能直接算：

```text
80 - 56 - 48 = -24
```

原因是三个数：

- 作用域不同；
- 资源名可能不同；
- 统计对象集合不同；
- 更新时间不同；
- admitted workload 可能尚未创建 Pod；
- Namespace 中可能有非 Kueue 管理的 Pod；
- 共享访问数不是物理库存。

正确做法是先定义问题：

```text
我要算物理容量？
我要算namespace准入余量？
我要算ClusterQueue剩余nominal？
我要算当前可立即放置的Pod？
我要算财务占用？
```

再选择对应账本。

---

## 11. Kueue：它决定“谁先拿预算”，不决定“Pod放哪台Node”

### 11.1 五个核心对象

#### LocalQueue

Namespace 内的入口。

大白话：

> 团队提交任务时选择的本地排队窗口。

它引用一个 ClusterQueue。

#### ClusterQueue

集群级配额池。

大白话：

> 平台管理员定义的 GPU、CPU、内存和 flavor 入场预算。

它不是 Kubernetes scheduler queue。

#### Workload

Kueue 用来表示一次需要 admission 的工作负载。

它记录：

- 一个或多个 PodSet；
- 每个 PodSet 的 count 与模板资源；
- queue 与 priority 信息；
- quota reservation；
- admission 状态；
- conditions。

#### ResourceFlavor

一种资源“口味”。

大白话：

> 同样叫 GPU，但它可能是 H100、A100、MIG 1g、在线保留池或 spot 批处理池；Flavor 用节点标签和 taint/toleration 等信息表达这种差异。

#### Cohort

多个 ClusterQueue 的共享配额组织。

大白话：

> 每个团队有自己的基本份额，空闲时可以按规则借给同组团队。

### 11.2 真实控制链

以 Kubernetes Job 为例：

```text
用户创建Job并指定LocalQueue
  -> Kueue集成把Job保持suspend
  -> Kueue生成或观察Workload
  -> Workload包含PodSets资源总量
  -> LocalQueue映射到ClusterQueue
  -> ClusterQueue/Cohort检查flavor和quota
  -> 成功时建立quota reservation
  -> 若配置了AdmissionChecks，等待所需checks为Ready
  -> 所需checks满足后Workload进入Admitted
  -> Kueue解除Job的suspend
  -> Job controller创建Pod
  -> 每个Pod经过API admission与ResourceQuota
  -> kube-scheduler执行Node Filter/Score
  -> kubelet分配GPU并启动容器
```

没有 AdmissionCheck 时，`QuotaReserved` 与 `Admitted` 常常紧邻。

有 AdmissionCheck 时，二者之间可能等待 provisioning、准入控制或其他外部检查。

不能把 `QuotaReserved=True` 直接当作 `Admitted=True`。

对直接管理的 Pod 集成，Kueue 可能通过 scheduling gate 控制入场。

admit 后移除 gate，仍由 kube-scheduler 放置。

### 11.3 反复钉死两层判断

第一层：

```text
Kueue quota fit
```

回答：

> 配额池愿不愿意让这个工作负载入场？

第二层：

```text
kube-scheduler node fit
```

回答：

> 当前是否存在满足资源、标签、taint、拓扑、亲和性、端口和其他插件约束的 Node？

所以：

```text
Admitted=True
  != PodScheduled=True
  != ContainersReady=True
  != 业务SLO达标
```

### 11.4 PodSet 资源必须乘 count

假设一个训练任务：

```text
worker count = 4
每个worker请求 nvidia.com/gpu = 2
```

总 GPU 配额需求是：

```text
4 × 2 = 8
```

不是 2。

如果还有 launcher PodSet：

```text
launcher count=1, GPU=0
worker count=4, GPU=2
总GPU=1×0 + 4×2 = 8
```

Kueue 的 Workload admission 针对完整 PodSets 资源总量做判断。

### 11.5 Kueue 与第 8–10 课 scheduler 的连接点

本地 Kubernetes 源码主线已经讲过：

```text
scheduleOne
  -> PreFilter
  -> Filter
  -> Score
  -> Assume
  -> Bind
  -> 失败后进入Unschedulable/requeue/preemption路径
```

本章不重复逐行读。

只增加一个前置层：

```text
Kueue admission
  -> 工作负载被允许进入真正的Pod调度阶段
  -> kube-scheduler才执行上述节点放置主线
```

如果 `Admitted=True` 后 Pod 因 `Insufficient nvidia.com/mig-1g.10gb` Pending，问题已经从 Kueue quota 层进入 kube-scheduler NodeResourcesFit 层。

### 11.6 quota reservation、admission check 与 placement 的时间不能混

一条完整生命周期至少要记录：

```text
Job creation time
Workload queue time
quota reservation time
所需AdmissionChecks全部Ready time
Workload Admitted time
Pod scheduled time
Pod Ready time
首个业务token或训练step time
completion time
```

由此派生：

```text
quota wait
  = quota reservation time - Workload queue time

admission-check wait
  = Workload Admitted time - quota reservation time

total Kueue wait
  = Workload Admitted time - Workload queue time

placement wait
  = Pod scheduled time - Workload Admitted time

cold start
  = Ready或首token time - Pod scheduled time

service time
  = completion time - Ready time
```

没有 AdmissionCheck 时，`admission-check wait` 通常接近控制器收敛延迟，但不能把 `QuotaReserved` 和 `Admitted` 两个 condition 的时间戳合成一个字段。

多 Pod Workload 的 `placement wait` 还要明确取首个 Pod、最后一个 Pod 还是全体 PodScheduled；多 Pod 训练通常同时保留首个和最后一个，才能看到 partial placement。

只看 Job 总耗时会把 quota 排队、admission check、节点放置、模型冷启动和真实计算混在一起。

---

## 12. Kueue `v1beta2` 对象：把 API 字段写对

### 12.1 ResourceFlavor 示例

以下是审阅用示意，不能直接 apply 生产：

```yaml
apiVersion: kueue.x-k8s.io/v1beta2
kind: ResourceFlavor
metadata:
  name: h100-mig-batch
spec:
  nodeLabels:
    accelerator.platform.example.com/model: h100
    accelerator.platform.example.com/pool: batch-mig
  nodeTaints:
  - key: accelerator.platform.example.com/dedicated
    value: batch
    effect: NoSchedule
```

字段意义：

- `nodeLabels` 会参与工作负载的节点选择约束；
- `nodeTaints` 描述 flavor 对应节点的 taint；Kueue 在 flavor assignment/admission 时就检查 PodSet 是否能容忍这些 taint；
- 如果平台配置并使用 ResourceFlavor 的 `spec.tolerations` 注入能力，才能按固定版本语义补充 toleration；否则不能等 admission 后才发现 PodSet 根本不容忍；
- 标签 key 应由企业域名管理，不要直接假装是 NVIDIA 官方标签；
- ResourceFlavor 不会创建节点或修改 GPU 几何。

### 12.2 ClusterQueue 示例

```yaml
apiVersion: kueue.x-k8s.io/v1beta2
kind: ClusterQueue
metadata:
  name: team-a-batch
spec:
  namespaceSelector:
    matchLabels:
      kubernetes.io/metadata.name: team-a
  cohortName: shared-batch
  resourceGroups:
  - coveredResources:
    - cpu
    - memory
    - nvidia.com/mig-1g.10gb
    flavors:
    - name: h100-mig-batch
      resources:
      - name: cpu
        nominalQuota: "64"
      - name: memory
        nominalQuota: 256Gi
      - name: nvidia.com/mig-1g.10gb
        nominalQuota: "8"
        borrowingLimit: "4"
        lendingLimit: "6"
```

版本陷阱：

```text
v1beta2正确字段：spec.cohortName
不要复制旧示例：spec.cohort
```

### 12.3 LocalQueue 示例

```yaml
apiVersion: kueue.x-k8s.io/v1beta2
kind: LocalQueue
metadata:
  name: batch
  namespace: team-a
spec:
  clusterQueue: team-a-batch
```

LocalQueue 是 namespaced。

ClusterQueue 是 cluster-scoped。

同名 LocalQueue 可以存在于不同 namespace，但它们可能引用不同 ClusterQueue。

### 12.4 Job 只展示队列入口

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: gpu-batch-example
  namespace: team-a
  labels:
    kueue.x-k8s.io/queue-name: batch
spec:
  suspend: true
  parallelism: 4
  completions: 4
  template:
    spec:
      restartPolicy: Never
      tolerations:
      - key: accelerator.platform.example.com/dedicated
        operator: Equal
        value: batch
        effect: NoSchedule
      containers:
      - name: worker
        image: registry.example.invalid/team/model-runner:review-only
        resources:
          requests:
            cpu: "4"
            memory: 16Gi
            nvidia.com/mig-1g.10gb: "1"
          limits:
            cpu: "4"
            memory: 16Gi
            nvidia.com/mig-1g.10gb: "1"
```

这段不应直接 apply，原因有三：

1. `registry.example.invalid` 是明确不可运行的示例域；
2. 生产镜像必须在审批变更中固定真实 digest；
3. 队列、profile 和资源名必须来自现场。

这个 Pod template 的 toleration 与 `h100-mig-batch` ResourceFlavor 中声明的 `nodeTaints` 契约一致。

如果删掉它，Kueue 在 flavor assignment/admission 阶段就可能判定 PodSet 不容忍目标 flavor；不能指望 admit 后再由 kube-scheduler“自动补上”。

其数学是：

```text
parallelism/completions语义由Job controller处理
Kueue生成的PodSet count与集成行为以实际Workload为准
若同时运行4个worker，每个1个MIG资源
  -> GPU quota需求4
```

最可靠的方法不是只读 Job YAML 猜，而是读取生成的 Workload。

### 12.5 API 版本检查

执行任何示例前，先查：

```powershell
param(
    [Parameter(Mandatory = $true)]
    [string]$ExpectedContext
)

$ErrorActionPreference = "Stop"

if (-not (Get-Command kubectl -ErrorAction SilentlyContinue)) {
    throw "kubectl not found"
}

function Invoke-Kubectl {
    $KubectlArguments = @($args)
    $Output = & kubectl @KubectlArguments
    $ExitCode = $LASTEXITCODE
    if ($ExitCode -ne 0) {
        throw "kubectl failed with exit code $ExitCode. args=$($KubectlArguments -join ' ')"
    }
    return $Output
}

$ActualContext = Invoke-Kubectl config current-context
if ($ActualContext -ne $ExpectedContext) {
    throw "Context mismatch. expected=$ExpectedContext actual=$ActualContext"
}

Invoke-Kubectl --context $ExpectedContext api-resources --api-group=kueue.x-k8s.io
Invoke-Kubectl --context $ExpectedContext get crd workloads.kueue.x-k8s.io -o jsonpath='{range .spec.versions[*]}{.name}{" served="}{.served}{" storage="}{.storage}{"\n"}{end}'
Invoke-Kubectl --context $ExpectedContext explain clusterqueue.spec --api-version=kueue.x-k8s.io/v1beta2
Invoke-Kubectl --context $ExpectedContext explain workload.spec.podSets --api-version=kueue.x-k8s.io/v1beta2
```

这段只读。

如果 `v1beta2` 未 served：

- 不要自动改用 `v1beta1` 执行本章 YAML；
- 先记录现场 Kueue 版本和 CRD storage version；
- 查升级与转换计划；
- 用现场版本官方文档重新审阅字段。

---

## 13. Cohort 配额数学：nominal、借入和借出

### 13.1 三个 Quantity

#### nominalQuota

ClusterQueue 自己的名义份额。

#### borrowingLimit

该 ClusterQueue 在自己 nominal 之外最多借多少。

#### lendingLimit

该 ClusterQueue 的空闲 nominal 最多允许借出去多少。

方向图：

```text
team-a借入
  受team-a borrowingLimit限制

team-b借出
  受team-b lendingLimit限制
```

### 13.2 具体数字算例

同一 Cohort、同一 flavor `h100-mig-batch`：

```text
team-a nominal = 8
team-a borrowingLimit = 4

team-b nominal = 12
team-b lendingLimit = 6

当前team-b使用 = 4
```

team-b 名义空闲：

```text
12 - 4 = 8
```

但 team-b 最多允许借出 6。

team-a 最多允许借入 4。

因此 team-a 的最大额外可借量：

```text
min(4, 6, cohort中当前可借空闲)
= 4
```

team-a 在其他条件满足时最多用：

```text
8 + 4 = 12
```

不是 20。

### 13.3 nominal 为 0 也必须声明

一个“只借不保底”的 ClusterQueue 可以：

```yaml
- name: nvidia.com/mig-1g.10gb
  nominalQuota: "0"
  borrowingLimit: "4"
```

关键点：

> 它仍必须在对应 flavor 下定义这个 resource。

否则不能凭 Cohort 中别人有 quota 就借到一个自己未声明的 flavor/resource。

### 13.4 只能借同 flavor 的意义

```text
h100-mig-batch空闲4
  != a100-full自动可借4
```

资源名相同也不够。

Kueue 的借用与 flavor 绑定。

这是为了避免把性能、节点标签和硬件类型完全不同的资源当成可互换数字。

### 13.5 CohortTree

CohortTree 是分层 Cohort。

大白话：

> 团队先在部门内共享，再由部门与更上层组织共享。

它能表达：

```text
公司
  ├─ 推荐平台部
  │    ├─ online
  │    └─ batch
  └─ 训练平台部
       ├─ pretrain
       └─ finetune
```

但树越深，解释借用、回收和公平越难。

初次落地建议：

- 先从一层 Cohort 开始；
- 配额和抢占规则可人工算清；
- Dashboard 能展示 nominal、borrowed、lent；
- 事故中能在五分钟内回答“谁借了谁多少”；
- 再评估 CohortTree。

### 13.6 再算一个整卡与MIG不能混借的例子

假设：

```text
team-a:
  h100-full / nvidia.com/gpu nominal=4

team-b:
  h100-mig / nvidia.com/mig-1g.10gb nominal=28
```

即使二者属于一个 Cohort，也不能说：

```text
team-b空闲7个MIG实例
  -> team-a可借1张整卡
```

Kueue 不负责把 7 个 profile 自动重组为整卡。

MIG 几何重配是第 6 节那种设备维护。

配额算术不能跨越物理重配置。

---

## 14. 抢占、优先级与 Fair Sharing

### 14.1 抢占解决什么

当高优工作负载需要 quota，而 quota 被低优或借用 workload 占用时，Kueue 可以按策略选择 preemption candidate。

常见策略边界包括：

- `withinClusterQueue`：同一 ClusterQueue 内按策略抢占；
- `reclaimWithinCohort`：收回本 ClusterQueue 的 nominal quota；
- `borrowWithinCohort`：围绕 Cohort 借用状态处理抢占。

具体枚举值和默认值必须用 `v0.18.3` 的 `kubectl explain` 或固定 API 文档确认。

不要凭本章文字直接修改生产。

还要注意固定版本的组合边界：

- `borrowWithinCohort` 属于 Classical Preemption 路径；
- 它不能和 Fair Sharing preemption 当成同时叠加的同一策略；
- 它还受 `reclaimWithinCohort` 相关配置依赖约束；
- 修改前必须用 `kubectl explain clusterqueue.spec.preemption` 和 `v0.18.3` API validation 核对可用组合。

### 14.2 抢占不是免费回收

一个训练 Job 已运行 7 小时，被抢占时：

```text
释放8张GPU的quota
  != 返还7小时训练进度
```

真实损失可能包括：

```text
未checkpoint的计算
checkpoint上传时间
重新拉取镜像
重新下载模型
数据shuffle
重新建立通信组
重新warmup
失败重试占用
```

平台必须把“可抢占”与“可恢复”分开登记。

### 14.3 Priority

优先级用于表达业务先后。

它不应直接等于：

```text
谁声音大谁优先
谁预算大谁永不排队
线上任务可以随时杀掉任何训练
```

推荐至少区分：

| 等级 | 示例 | 是否允许借用 | 是否可被抢占 | 恢复要求 |
|---|---|---|---|---|
| critical-online | 在线核心推理扩容 | 受控 | 极低 | 多副本与跨节点 |
| scheduled-training | 有交付期限训练 | 是 | 受策略限制 | checkpoint |
| opportunistic-batch | 离线探索 | 是 | 是 | 幂等、可重跑 |
| dev-shared | 开发验证 | 小额 | 是 | 无状态优先 |

### 14.4 先拆开两种“公平”：它们不是一个机制

`v0.18.3` 里至少要区分两层。

#### AdmissionFairSharing

对象范围：

```text
同一个ClusterQueue内部
  -> 比较来自不同LocalQueue的等待Workload
```

核心依据是来源 LocalQueue 的历史 consumed resources。

大白话：

> 同一个配额池里，如果某个 LocalQueue 过去已经消耗很多，admission 时可以优先考虑历史消费较少的 LocalQueue。

在本课固定版本中它是 beta 且默认开启，但仍要核对实际 feature gate。

它的时间含义来自 LocalQueue 历史消费，不是物理 GPU 当前 utilization。

feature gate 默认开启，只表示 API/controller 具备这项能力。

它不表示每个 ClusterQueue 自动采用 Usage-Based Admission Fair Sharing。

目标 ClusterQueue 还要显式选择 admission mode：

```yaml
apiVersion: kueue.x-k8s.io/v1beta2
kind: ClusterQueue
metadata:
  name: example-review-only
spec:
  admissionScope:
    admissionMode: UsageBasedAdmissionFairSharing
```

这只是字段示意，缺少完整 ResourceGroup，不能直接 apply。

生产判断要同时核对：

```text
AdmissionFairSharing feature gate
ClusterQueue spec.admissionScope.admissionMode
来源LocalQueue
历史consumed resources
```

#### ClusterQueue/Cohort Fair Sharing

对象范围：

```text
多个ClusterQueue或Cohort层级
  -> 比较跨ClusterQueue的共享占用
  -> 参与admission和preemption判断
```

它使用 `weightedShare` 等状态表达超出保障份额后的相对共享占用。

但对象里出现 Cohort、weight，甚至 API 类型里存在 `weightedShare` 字段，都不代表这套机制已经生效。

在本课固定版本中，还必须检查 Kueue controller 实际加载的 Configuration，重点是：

```text
fairSharing.preemptionStrategies
```

该配置决定 Fair Sharing 相关的抢占策略是否、以及如何参与决策。生产判断必须读取 controller 的生效配置和启动日志，不能只读安装仓库中的期望 ConfigMap，因为“仓库里写了”不等于运行中的 controller 已加载。

`weightedShare` status 也只有在 Fair Sharing 已启用并完成相应状态计算时才应作为证据使用。字段缺失不能自动解释成 share 为 `0`；先排查 feature/config 是否生效、controller 是否已调和，再解释数值。

两者对照：

| 机制 | 比较对象 | 主要时间/状态含义 | 用在什么判断 |
|---|---|---|---|
| AdmissionFairSharing | 同一ClusterQueue内的来源LocalQueue | LocalQueue历史consumed resources | 同池等待Workload的admission顺序 |
| Cohort Fair Sharing | ClusterQueue/Cohort | 当前层级quota使用、借用和weightedShare | 跨CQ admission与preemption |

不能把 AdmissionFairSharing 的 LocalQueue 历史消费指标叫成 ClusterQueue weightedShare。

也不能看到 weightedShare 就推断某个 LocalQueue 的历史消费。

#### 两者共同不是什么

它们都不是：

- GPU kernel 性能隔离；
- time-slicing 算力份额；
- 严格轮转；
- 物理 GPU 利用率；
- 财务扣费算法。

### 14.5 Classic 与 Fair Sharing

Classic admission 更依赖队列顺序、优先级、时间和 quota fit。

AdmissionFairSharing 把 LocalQueue 历史消费用于同一 ClusterQueue 内的 admission 排序；ClusterQueue/Cohort Fair Sharing 则可把当前共享份额用于跨队列 admission 与 quota 抢占判断，避免一个主体长期占用共享池。

启用哪一层公平、它的对象是谁，以及 controller 的 `fairSharing.preemptionStrategies` 是否实际生效，必须按上一节拆开核对。

### 14.6 weightedShare 怎么理解

大白话：

> 把某主体超过自身保障份额的资源使用，按权重换算成一个可比较的共享占用程度。

在 ClusterQueue/Cohort Fair Sharing 生效时，它可以参与 admission 选择和 preemption 比较。

它不是：

- 物理 GPU 利用率；
- 业务吞吐；
- 单任务性能；
- 财务成本；
- 每分钟严格轮转；
- 永久历史惩罚。

具体计算还受当前 quota、借用、层级、权重和 Kueue 版本实现影响。

运维要关注：

```text
weightedShare当前值
controller生效配置中的fairSharing.preemptionStrategies
quota使用
借用量
pending workload
priority
admission时间
Cohort层级与Fair Sharing weight配置
```

不要只凭一个 share 数值判断某团队“浪费 GPU”。

### 14.7 公平不等于性能隔离

即使 Kueue 公平地让 team-a 和 team-b 各自 Admit 4 个共享槽：

```text
Kueue公平
  != time-slicing下每个Pod性能公平
```

Kueue 控制入场数量。

GPU 上的 kernel、显存和进程调度由更下层控制。

想要性能隔离，要选 MIG、整卡或经验证的 MPS 等设备方案。

### 14.8 Kueue quota/cohort/preemption 变更 runbook

#### 前置检查

1. 导出 ClusterQueue、Cohort、ResourceFlavor 和 LocalQueue；
2. 记录 admitted、pending、borrowed、weightedShare；
3. 计算每个现有 Workload 的 PodSet 总资源；
4. 标出可抢占任务及最近 checkpoint；
5. 检查目标 flavor 的真实 Node Capacity；
6. 评估变更是否会让现有借用者进入回收候选；
7. 明确业务负责人和最大可接受训练损失。

#### Server-side dry-run

变更对象先进入代码评审和 API dry-run。

不要在终端临时 `kubectl edit`。

#### Canary

先修改一个非核心 ClusterQueue 的小额度：

- 只影响一个 LocalQueue；
- 提交一个小 Workload；
- 观察 admission、flavor、Pod placement；
- 制造可控的 quota 不足，验证 pending reason；
- 不用真实长训练验证抢占。

#### 停止扩散

- 非目标 ClusterQueue quota 变化；
- 借用计算与评审表不一致；
- 高优任务触发意外大范围 eviction；
- Admitted 后持续 Pending；
- controller error 或 status 不收敛；
- cost/chargeback 无法识别新 flavor。

#### 回滚

1. 停止新提交；
2. 恢复已审阅的旧对象；
3. 检查 status 与 cache 收敛；
4. 不要假定已被抢占的 Job 会恢复进度；
5. 必要时从 checkpoint 重提；
6. 对 admission 延迟和业务损失复盘。

#### 审批门槛

只要变更涉及：

```text
preemption策略
已有borrowed workload
在线保留quota
跨部门Cohort
长训练任务
```

就必须由平台、业务和容量/成本负责人共同审批。

---

## 15. `waitForPodsReady`：管理全体就绪，不是原子 gang scheduler

### 15.1 它解决的典型问题

多 Pod 训练已经 admission，但只有一部分 Pod 能启动：

```text
worker-0 Running
worker-1 Running
worker-2 Pending
worker-3 Pending
```

已运行 worker 可能空等，继续占用 quota 和 GPU。

`waitForPodsReady` 可以：

- 等待工作负载 Pod 在规定时间内 Ready；
- 超时后取消 admission；
- 释放 quota；
- 将 Workload 重新排队；
- 按配置使用 backoff。

### 15.2 为什么不是原子 gang scheduler

原子 gang 的理想直觉是：

```text
所有Pod同时获得节点
否则一个都不启动
```

`waitForPodsReady` 的真实边界是：

```text
先Admit
  -> Pod进入普通调度和启动
  -> 可能部分Running
  -> 在超时后再取消Admission并requeue
```

所以它不是“所有 Pod 在同一时刻原子绑定”。

### 15.3 已发生的副作用

取消 admission 不会时间倒流。

部分 Pod 可能已经：

- 拉取几十 GB 镜像；
- 下载数百 GB 模型；
- 初始化 NCCL；
- 创建临时文件；
- 占用本地 NVMe cache；
- 写入部分 checkpoint；
- 发出外部 API 请求；
- 在监控中产生一次运行记录。

重新排队后，这些副作用可能再次发生。

### 15.4 循环风险

```text
Admit
  -> 3/4 Pod Ready
  -> timeout
  -> cancel admission
  -> requeue
  -> 再次Admit
  -> 仍然3/4 Ready
```

如果根因是：

```text
节点只有3个实际可放置GPU
拓扑无法满足
目标MIG profile数量不足
镜像拉取持续失败
第四个Pod有不可满足的taint
```

反复 requeue 不会修复它。

只会放大：

- 镜像下载；
- Job 启停；
- checkpoint；
- 日志噪声；
- 成本；
- 排队时间。

### 15.5 backoff 的运维含义

`v0.18.3` 当前文档中的 `requeuingStrategy` 可配置：

- `timestamp`；
- `backoffLimitCount`；
- `backoffBaseSeconds`；
- `backoffMaxSeconds`。

若不设置 `backoffLimitCount`，超时 Workload 可能持续重新排队。

默认基准与最大 backoff 要从实际 controller 配置核对。

不要只看“队列里还有任务”而忽略它已第几次 timeout。

### 15.6 多 Pod 任务的上线条件

在启用 `waitForPodsReady` 前，至少明确：

```text
PodSet总资源数学
目标ResourceFlavor
所需节点数量
每节点GPU数量和profile
网络/NVLink/拓扑要求
模型与镜像冷启动P95
Ready探针真实含义
checkpoint周期
重排队backoff
最大尝试次数
外部副作用幂等
```

### 15.7 拓扑缺口

Kueue 可以为 8 个 GPU 预留 quota。

但训练可能要求：

```text
2台Node
每台4张同型号GPU
节点间特定网络
同一可用区
特定NVLink/NVSwitch拓扑
```

如果集群实际是：

```text
8个可用GPU
分散在8台Node
```

quota 数字足够，拓扑仍不满足。

这就是：

```text
配额可容纳
  != 拓扑可放置
```

---

## 16. ResourceFlavor 与企业节点池：在线和批处理不要混成一个池

### 16.1 Flavor 解决的是“哪种配额”

两个 ClusterQueue 都有：

```text
nvidia.com/gpu nominalQuota=8
```

仍可能代表完全不同的东西：

```text
online-h100:
  H100整卡
  on-demand
  严格SLO
  预热模型

batch-a100:
  A100整卡
  spot
  可抢占
  冷启动
```

ResourceFlavor 把这种差异带入 Kueue admission。

### 16.2 Flavor 不是 Node

ResourceFlavor 包含标签和 taint 语义。

它不会：

- 创建 Node；
- 安装 driver；
- 开启 MIG；
- 给云厂商扩容；
- 保证目标节点已经 Ready；
- 保证节点有足够可用设备。

所以：

```text
Kueue选择了h100-online flavor
  != 当前已经有h100-online Node可放置
```

### 16.3 kube-scheduler 仍做最终 Filter

先把普通 ResourceFlavor 路径与 Topology Aware Scheduling 路径分开。

#### 普通 ResourceFlavor 路径

普通 ResourceFlavor 主要表达 quota 口径以及要注入 Pod 的 Node label、taint/toleration 等约束。Kueue 在 admission 时选择 flavor，并不因此选定某一台具体 Node。

这条路径中，Node 级 Filter、Score 和 Bind 由 kube-scheduler 完成。

#### 启用 Kueue TAS 时的例外

如果集群和 Workload 显式启用了 Kueue Topology Aware Scheduling，Kueue 在 admission 前会多做一层拓扑容量判断：它可以利用 Ready、可调度 Node 的 topology domain 与 allocatable/已占用容量证据，计算满足 PodSet 的 topology assignment，并把所选拓扑域写入 Workload admission 结果。

大白话：

> 普通 flavor 只是在说“这类节点可以”；TAS 还会在入场前判断“哪些机架、可用区或节点拓扑域有机会一起装下这一组 Pod”。

这确实比普通 flavor 更接近节点容量，因此不能笼统地说“Kueue 从不看 Node”。但 TAS 仍不执行 kube-scheduler 的完整 Filter/Score/Bind，也不直接完成 Pod 到 Node 的最终绑定。Node readiness、cordon/unschedulable、可用资源和拓扑缓存还会随时变化，所以 topology assignment 之后仍可能在最终调度阶段失败。

边界可以记成：

```text
普通ResourceFlavor
  -> Kueue选择quota/flavor并注入约束
  -> kube-scheduler最终Filter/Score/Bind

Kueue TAS
  -> Kueue在admission前读取可调度拓扑容量
  -> 生成topology assignment
  -> kube-scheduler仍最终Filter/Score/Bind
```

admission 后，Pod 仍要满足：

```text
NodeResourcesFit
NodeAffinity
TaintToleration
PodTopologySpread
InterPodAffinity
VolumeBinding
其他启用的调度插件
```

Kueue 选择 flavor 是 quota 和约束注入的一部分。

kube-scheduler 始终负责最终 Node 级可行性判断与绑定；TAS 的 admission 前拓扑容量判断不能替代它。

Pod 绑定 Node 之后，还要经过 kubelet 本地 admission。

设备、CPU 和内存管理器提供 NUMA topology hints，Topology Manager 按节点本地 policy 合并并决定是否接纳：

```text
kube-scheduler Filter/Score/Bind
  -> Pod获得spec.nodeName
  -> kubelet DeviceManager/CPUManager/MemoryManager提供topology hints
  -> kubelet TopologyManager policy执行本地admission
  -> 通过后才继续容器设备分配与启动
```

因此 Topology Manager 不是 kube-scheduler 插件，也不应列在 scheduler Filter 清单里。

Scheduler 认为 Node 可放置，仍可能在严格 Topology Manager policy 下被 kubelet 拒绝；此时应查 kubelet admission 事件和 NUMA/device hints，而不是继续调整 Kueue quota。

### 16.4 在线池设计

在线 vLLM 池通常优先：

- 整卡独占或经过验证的 MIG profile；
- 严格禁止 time-slicing 进入核心 SLO；
- 稳定 GPU 型号与 driver；
- on-demand 或企业自有固定容量；
- 模型镜像和权重预热；
- 跨故障域副本；
- DCGM 与 vLLM SLO 联动；
- 低抢占概率；
- 明确保留容量；
- 发布期间额外 surge 容量。

典型标签契约：

```text
accelerator.platform.example.com/pool=online
accelerator.platform.example.com/model=h100
accelerator.platform.example.com/sharing=exclusive
accelerator.platform.example.com/capacity=reserved
```

这些是企业自定义示意，不是 NVIDIA 官方 label。

### 16.5 批处理池设计

批处理池可以更积极地提高复用：

- 整卡训练；
- 固定 MIG profile；
- 可信任务使用 time-slicing；
- 经专项验证后评估 MPS；
- spot 或可回收节点；
- Kueue Cohort 借用；
- 允许 checkpoint 后抢占；
- 允许更长 queue wait；
- 对模型下载和 cache 做成本治理。

典型契约：

```text
accelerator.platform.example.com/pool=batch
accelerator.platform.example.com/model=h100
accelerator.platform.example.com/sharing=mig
accelerator.platform.example.com/capacity=spot
```

### 16.6 为什么物理分池比逻辑规则更可靠

如果在线与批任务共用同一物理 GPU，只靠：

```text
PriorityClass
Kueue quota
Namespace
```

不能消除：

- time-slicing 邻居噪声；
- 同卡 Xid 故障域；
- 显存竞争；
- 批任务 kernel 对在线延迟的影响；
- MIG 重配维护影响；
- 节点重启影响。

严格 SLO 场景应优先物理节点池或至少物理卡级隔离。

### 16.7 Reserved capacity

Reserved capacity 是为关键业务保留的可用能力。

大白话：

> 平时看起来可能空闲，但不能随便借光，因为突发流量、发布和故障切换要用。

它不是：

```text
当前空闲
  -> 可以永久卖给批任务
```

在线容量至少要覆盖：

```text
正常峰值
+ 单节点或单故障域损失
+ 发布surge
+ 模型切换重叠
+ autoscaler到位前缓冲
```

允许批任务借用保留容量时，必须满足：

- 可在业务要求时间内回收；
- 批任务能 checkpoint；
- 回收不会造成控制面风暴；
- 回收时镜像和模型 cache 不拖慢在线扩容；
- Kueue preemption 与 Node provisioning 时间经过演练。

### 16.8 Autoscaler 不是即时容量

Cluster autoscaler 或 provisioning controller 可能看到 Pending Pod 后创建节点。

GPU 节点从零到可用还要：

```text
云实例创建
OS启动
Node注册
driver安装或加载
Container Toolkit
Device Plugin注册
GFD标签
MIG配置
镜像拉取
模型下载
模型加载
warmup
```

因此：

```text
可扩容上限
  != 当前可用容量
```

Kueue 已 Admit 的 Workload 可能等待 provisioning。

这段等待属于 placement/provisioning，不应算成 Kueue queue wait。

### 16.9 碎片

#### 整卡碎片

节点分别剩 1 张卡，但任务要单节点 4 张：

```text
集群总空闲GPU=4
单节点最大连续空闲GPU=1
  -> 任务Pending
```

#### MIG 碎片

物理显存总空闲足够，但没有目标 profile：

```text
空闲多个1g实例
请求一个3g实例
  -> 不能自动拼接
```

#### 队列碎片

ClusterQueue 剩余 3，队首任务要 4，后面的任务各要 1。

能否越过队首、如何受优先级和策略影响，要看固定 Kueue 版本配置。

不能只看剩余 quota。

### 16.10 一个推荐的双池结构

```text
GPU集群
  ├─ online-exclusive
  │    ├─ 固定H100型号
  │    ├─ nvidia.com/gpu
  │    ├─ 不共享
  │    ├─ 无batch Cohort借用
  │    └─ vLLM严格SLO
  │
  ├─ batch-mig
  │    ├─ 固定MIG geometry
  │    ├─ mixed profile资源名
  │    ├─ Kueue Cohort借用
  │    └─ checkpoint后可抢占
  │
  └─ dev-shared
       ├─ nvidia.com/gpu.shared
       ├─ trusted namespace
       ├─ failRequestsGreaterThanOne=true
       └─ best-effort
```

这是思考模板，不是所有企业都必须三池。

硬件规模小的时候，也应至少通过节点标签、taint、准入和资源名把语义分开。

---

## 17. 多租户安全：配额只是门票数量，不是隔离墙

### 17.1 九层控制

#### 第一层：Namespace 与 RBAC

控制：

- 谁能创建 Job、Pod、ResourceClaim；
- 谁能使用哪个 LocalQueue；
- 谁能读取 Secret；
- 谁能查看其他团队对象；
- 谁能修改 ResourceQuota。

危险授权：

```text
普通租户可patch ClusterQueue
普通租户可改Node label
普通租户可改GPU Operator ConfigMap
普通租户可创建cluster-scoped DeviceClass
普通租户可读其他namespace模型凭据
```

#### 第二层：Admission Policy 或 Policy Engine

用于拒绝不符合平台契约的对象：

- 在线 namespace 请求 `.shared`；
- time-slicing 请求数量大于 1；
- 未声明 queue；
- 使用未批准 RuntimeClass；
- 特权容器或 hostPID；
- 未固定镜像 digest；
- 未设置成本标签；
- 资源请求名与节点池不匹配。

#### 第三层：ResourceQuota 与 LimitRange

它们限制：

- Namespace 可接纳的资源总量；
- Pod/容器 CPU、内存默认值与范围；
- 对象数量；
- GPU 扩展资源 requests；
- DRA DeviceClass 设备数量。

它们不隔离 GPU 内存和 kernel。

#### 第四层：Kueue quota

它控制批任务入场预算、公平、借用和抢占。

它不阻止一个已经运行的 time-slicing 进程吃满显存。

#### 第五层：Node 池、标签和 taint

把：

```text
online
batch
dev-shared
untrusted
```

分到不同物理故障域。

taint 不是完整安全边界，但能减少误调度。

#### 第六层：RuntimeClass 与容器权限

审计：

- privileged；
- hostPID；
- hostIPC；
- hostPath；
- Linux capabilities；
- seccomp；
- AppArmor/SELinux；
- CDI device 注入；
- 容器运行时配置；
- MPS socket 与 daemon 访问。

GPU 容器能访问设备，不代表它应获得 Node 管理权限。

#### 第七层：镜像与模型供应链

至少控制：

- 镜像 digest；
- 签名和来源；
- CUDA/框架版本；
- 模型权重来源与 checksum；
- 自定义 CUDA extension；
- Python package 安装；
- 模型代码 `trust_remote_code` 风险；
- 机密模型的 cache 和销毁。

#### 第八层：网络与 Secret

控制：

- 模型仓库访问；
- 对象存储凭据；
- 推理入口；
- 训练数据出口；
- metadata service；
- exporter 与 metrics；
- 跨租户 Service；
- Secret 挂载最小化。

#### 第九层：GPU 共享故障域

最终必须回答：

```text
一个租户CUDA OOM会影响谁？
一个Xid会影响谁？
一次GPU reset会影响谁？
MPS daemon退出会影响谁？
MIG几何变更会影响谁？
```

如果答案是“同卡多个不互信租户”，time-slicing 方案就不成立。

### 17.2 Quota 为什么不是安全隔离

ResourceQuota：

```text
允许team-a最多请求4个共享槽
```

不表示：

```text
team-a只能用40%显存
team-a不能影响team-b
team-a看不到共享设备状态
team-a的kernel不会造成抖动
```

Kueue quota 同理。

它限制入场数量，不隔离设备内部行为。

### 17.3 MIG 也需要系统安全层

即使采用 MIG：

- 容器仍运行在同一 Node OS；
- kubelet 和 runtime 仍共享；
- 网络、Secret、ServiceAccount 仍需隔离；
- hostPath 和 privileged 仍可能越界；
- 卡级故障仍可能跨 slice；
- DCGM 和 PodResources 数据可能泄露租户身份；
- 成本和队列仍需治理。

MIG 是重要硬件隔离层，不是多租户平台的全部。

### 17.4 不互信场景的底线

对于不互信租户，优先级通常是：

```text
独立集群/独立节点
  > 独立物理GPU
  > 经安全评估的MIG
  > MPS
  > time-slicing
```

这不是绝对产品排名。

它表达的是：

> 越往下，共享软件栈、进程和故障域通常越多，越不能把它当硬隔离。

### 17.5 多租户上线检查

| 检查 | 必须回答 |
|---|---|
| 身份 | 用户、ServiceAccount、CI机器人分别是谁 |
| 授权 | 谁能提交、看队列、改quota、改节点 |
| 设备 | 整卡、MIG、time-slicing、MPS还是DRA |
| 故障域 | 单进程、单slice、单卡、单节点影响范围 |
| 数据 | 模型、训练数据、cache、日志是否跨租户 |
| 网络 | 入站、出站、metadata、metrics是否最小化 |
| 运行时 | privileged、host namespace、capability |
| 供应链 | 镜像与模型是否固定、签名、扫描 |
| 成本 | 物理账与分摊规则是否可审计 |
| 退出 | 任务结束后Secret、cache、claim和设备如何回收 |

---

## 18. 成本账：先算物理卡时，再谈怎么分摊

### 18.1 Physical GPU-hours

物理 GPU 卡时：

```text
Physical GPU-hours
  = 物理GPU数量 × 计费时长
```

一台 8 卡节点运行 24 小时：

```text
8 × 24 = 192 physical GPU-hours
```

即使：

- 没有 Pod；
- time-slicing 广告 80；
- 只用了 3 个共享槽；
- Kueue 没有任务；

只要资产或云实例持续计费，物理成本仍可能是 192 卡时对应金额。

### 18.2 Allocated GPU-hours

独占整卡分配卡时：

```text
Allocated GPU-hours
  = Σ(每个工作负载独占GPU数 × 分配时长)
```

任务 A 独占 4 卡 3 小时：

```text
4 × 3 = 12 allocated GPU-hours
```

“分配时长”必须定义起止：

```text
Admitted到Finished？
PodScheduled到Terminated？
容器Running到Exit？
GPU进程实际存在？
```

推荐同时保留控制面分配时长和业务运行时长，不把两者混成一个数。

### 18.3 MIG slice 的财务分摊

可以定义内部权重：

```text
MIG slice charge
  = 物理GPU小时单价
    × profile财务权重
    × 使用时长
```

示例政策，不是硬件事实：

```text
一张物理卡每小时成本 = 100元
1g profile财务权重 = 1/7
某任务使用1个1g profile 3小时

内部归集 = 100 × 1/7 × 3
           ≈ 42.86元
```

必须在报表中注明：

> `1/7` 是企业财务分摊规则，不宣称吞吐、显存带宽、功耗或故障风险严格线性等于整卡七分之一。

### 18.4 MIG 空闲成本

一张卡被切成 7 个 slice，只使用 4 个。

如果物理卡持续计费：

```text
物理成本 = 1张卡 × 小时单价
业务已归集 = 4个slice × 财务权重 × 小时
未归集/平台闲置 = 物理成本 - 业务已归集
```

不能让“没人认领”的 3 个 slice 成本从账上消失。

### 18.5 time-slicing 绝不能按 replicas 简单除

错误公式：

```text
一张卡100元/小时
replicas=10
每个Pod固定10元/小时
```

问题：

- 10 是访问槽，不是资源份额；
- 可能只运行 2 个 Pod；
- 一个 Pod 可能用 90% SM，另一个用 10%；
- 显存无固定十分之一边界；
- 多个 Pod 的设备利用率不能直接相加；
- 卡级故障成本由所有同卡业务共同承担；
- slot 空闲不等于物理成本消失。

可选的企业分摊规则：

#### 按预约时长等额

```text
同一物理GPU在一个时间窗内
  -> 对实际占用该卡的活跃Pod等额分摊
```

优点：简单。

缺点：不反映计算差异。

#### 按进程利用证据加权

```text
Pod charge weight
  = 约定的进程GPU active time或其他可审计指标
```

优点：更接近活动量。

缺点：

- 指标支持依赖 GPU/DCGM/运行模式；
- MIG/time-sharing 的 per-process 能力有版本边界；
- 短采样和缺失会失真；
- 仍不是业务价值。

#### 固定服务等级价格

```text
共享开发槽按套餐价
物理差额由平台成本中心承担
```

优点：业务可预测。

缺点：平台必须承担超卖和闲置风险。

无论哪种，都必须先保证：

```text
所有Pod分摊总额
  + 平台闲置/差额
  = 物理GPU实际成本
```

### 18.6 业务效率成本

推理：

```text
Cost per 1M successful tokens
  = 时间窗内GPU总成本
    / 成功输出token数
    × 1,000,000
```

训练：

```text
Cost per successful training run
  = 该run及失败重试的GPU成本
    + checkpoint/storage/network等约定成本
```

或：

```text
Cost per 1,000 successful steps
  = 总成本 / 成功训练step数 × 1,000
```

必须排除或单列：

- 失败请求 token；
- 重试 token；
- warmup；
- benchmark；
- speculative 分支；
- 被抢占后丢失的 step；
- 重复数据处理。

### 18.7 排队和冷启动成本

排队中的任务可能没有占 GPU，但仍有业务成本：

```text
交付延迟
工程师等待
SLA违约
上游数据过期
下游流水线阻塞
```

冷启动可能直接占 GPU：

```text
容器Running
  -> 模型加载20分钟
  -> 尚未提供token
```

这 20 分钟：

- 是 allocated GPU-hours；
- 不是成功业务产出；
- 应计入冷启动损耗；
- 会降低 token/GPU-hour。

### 18.8 失败重试成本

任务：

```text
第一次运行6小时后被抢占
checkpoint已保存运行到第4小时的进度
第二次再运行5小时成功
```

物理消耗至少：

```text
6 + 5 = 11 GPU-hours × GPU数
```

业务有效进度可能只有：

```text
4小时checkpoint进度 + 5小时后续
```

中间 2 小时是可避免损失。

抢占报表若只看最终成功 run 的 5 小时，会严重低估成本。

### 18.9 利用率不等于价值

```text
GPU utilization=95%
```

可能是：

- 高效推理；
- 有效训练；
- 错误 benchmark；
- 死循环 kernel；
- 失败任务重复；
- 大量无价值 token；
- 邻居噪声；
- 数据加载错误后的重复计算。

价值指标至少联看：

```text
成功token或step
SLO
任务成功率
模型质量或业务目标
成本
排队
失败重试
```

### 18.10 一个完整数字算例

一台 8×H100 节点，小时成本 800 元，运行 10 小时：

```text
物理成本 = 800 × 10 = 8,000元
物理卡时 = 8 × 10 = 80 GPU-hours
```

其中：

```text
在线池使用4卡×10小时 = 40 allocated GPU-hours
批任务使用2卡×6小时 = 12 allocated GPU-hours
故障重试使用2卡×2小时 = 4 allocated GPU-hours
其余物理容量空闲或碎片 = 24 GPU-hours
```

总核对：

```text
40 + 12 + 4 + 24 = 80 GPU-hours
```

如果在线产出 4000 万成功 token：

先按财务规则归集在线 4000 元，则：

```text
每百万成功token成本
  = 4000 / 40
  = 100元
```

这只是示例。

真实成本还可能包含 CPU、内存、存储、网络、许可证和平台分摊。

---

## 19. 可观测性联表：对象、设备、进程和业务必须用键连接

### 19.1 六类数据源

#### Kueue

关注：

```text
LocalQueue/ClusterQueue
pending workload
quota reservation
admission
borrowed/lent
preemption/eviction
queue wait
weightedShare
ResourceFlavor
```

#### Job、Workload 与 Pod

关注：

```text
Job UID
Workload UID
Pod UID
PodSet
priority
suspend
conditions
nodeName
container state
restart count
completion
```

#### kube-scheduler

关注：

```text
PodScheduled condition
FailedScheduling events
Insufficient extended resource
taint/affinity/topology reason
preemption result
调度等待
```

#### Node 与 PodResources

关注：

```text
Capacity/Allocatable
资源名
Node UID
GPU/MIG device ID
Pod namespace/name
container name
CPU/NUMA信息
更新时间
```

kubelet PodResources API `v1` 的 PodResources 响应没有 Pod UID 字段。

可靠联表步骤是：

```text
记录PodResources采样时间
  -> 取得namespace/name、container与device ID
  -> 在相同时间窗回查Pod API
  -> 核对Pod metadata.creationTimestamp与当前metadata.uid
  -> 得到Pod UID后再和Workload、DCGM、成本事实表连接
```

如果 Pod 已删除、同名 Pod 已重建或 API 历史对象不可用，只能标记身份未确认；不能把当前同名 Pod 的 UID 追认给旧 PodResources 样本。

#### DCGM

关注：

```text
GPU UUID
MIG UUID/GI/CI
utilization
framebuffer memory
temperature
power
Xid
ECC
NVLink/PCIe
health
```

详细边界见：

- [第19课：DCGM指标、Xid、ECC与GPU健康](19_DCGM_指标告警_Xid_ECC与GPU健康.md)

#### vLLM 或训练框架

推理关注：

```text
running requests
waiting requests
KV cache usage
TTFT
ITL
e2e latency
successful tokens
preemption
request result
```

详细边界见：

- [第20课：vLLM模型加载、显存、探针、吞吐延迟与SLO](20_vLLM_模型加载_显存_探针_吞吐延迟与SLO.md)

训练关注：

```text
global step
samples
loss
checkpoint
worker rank
NCCL状态
成功/失败
```

### 19.2 推荐身份键

控制面键：

```text
cluster_id
namespace
job_uid
workload_uid
pod_uid
container_name
node_uid
```

设备键：

```text
physical_gpu_uuid
mig_device_uuid
gi_id
ci_id
device_id
```

成本键：

```text
tenant
cost_center
project
model_id
environment
service_tier
capacity_type
```

名称可以重复。

UID 和时间窗更适合做事实关联。

### 19.3 非原子一致性

这些数据不是同一事务写入：

```text
Kueue status在t0更新
Job在t1解除suspend
Pod在t2创建
Scheduler在t3绑定
PodResources在t4可见
DCGM在t5采样
vLLM在t6暴露首个metric
```

因此一次瞬时查询可能出现：

- Workload 已 Admitted，Pod 尚未创建；
- Pod 已绑定，PodResources 尚未映射；
- Pod 已退出，DCGM label cache 仍短暂存在；
- Node sharing 配置已变，Prometheus 仍保留旧 series；
- Job 名复用，但 UID 已不同。

联表必须有：

```text
事件时间
采集时间
对象UID
有效时间窗
数据来源
staleness判断
```

### 19.4 time-slicing 的重复归因陷阱

物理 GPU 利用率：

```text
GPU-abc utilization=70%
```

同卡有 5 个 Pod。

错误做法：

```text
把70%复制到5个Pod
再求和
=350%
```

正确方式之一：

1. 物理设备层只保留一次 70%；
2. Pod 层若无可靠 per-process 指标，只标记“共享该设备”；
3. 不把 device total 当成每 Pod 使用量；
4. 成本按已公告规则分摊；
5. Dashboard 明示“共享、不可精确归因”。

即使启用 per-process metric，也要核对：

- 当前 DCGM/exporter 版本；
- MIG/time-sharing 支持范围；
- PID 到容器映射；
- 进程退出竞态；
- 采样缺失；
- MPS 代理进程边界。

### 19.5 MIG 的父卡重复陷阱

父物理卡的温度、功耗或某些 health metric 可能对多个 MIG instance 都相关。

不能：

```text
父卡功耗700W
复制给7个slice
求和=4900W
```

应区分：

```text
physical GPU metric
MIG entity metric
process metric
Pod attribution
```

成本归集和告警聚合必须知道 metric 的实体层级。

### 19.6 一张排障时间线

```text
10:00:00 Workload进入ClusterQueue
10:02:10 quota reservation
10:02:11 Admitted=True
10:02:15 Job suspend=false
10:02:16 4个Pod创建
10:02:20 3个Pod Scheduled
10:02:20 第4个Pod FailedScheduling
10:04:00 前3个Pod开始下载模型
10:12:11 waitForPodsReady超时
10:12:12 admission取消
10:12:20 3个Pod终止
10:13:12 Workload backoff
```

从这条线可以拆出：

- queue wait 约 2 分 10 秒；
- placement 不完整；
- 约 8 分钟模型下载副作用；
- 不是“整个任务排队 13 分钟”这么简单；
- 根因要查第 4 个 Pod 的 scheduler event。

### 19.7 告警分层

#### 入场层

- queue wait 超 SLO；
- pending workload 激增；
- borrowed quota 异常；
- preemption/eviction 激增；
- waitForPodsReady 循环。

#### 放置层

- Admitted 但长期无 PodScheduled；
- 特定 flavor 无 Node；
- Insufficient MIG profile；
- taint/affinity 不满足；
- autoscaler provisioning 超时。

#### 设备层

- Xid/ECC；
- GPU 掉总线；
- 温度/功耗限频；
- MIG entity 缺失；
- Node Allocatable 突变；
- Device Plugin 重注册失败。

#### 业务层

- vLLM waiting queue；
- TTFT/ITL/P99；
- KV cache pressure；
- 训练 step 停滞；
- checkpoint 失败；
- 成功 token/step 下降。

#### 成本层

- 物理卡时与归集不平；
- 共享槽被当物理卡；
- idle/fragmentation 增长；
- 失败重试成本增长；
- 高利用率但成功产出下降。

### 19.8 联表查询的安全原则

```text
先设备事实
  -> 再资源广告
  -> 再queue/admission
  -> 再Pod placement
  -> 再进程/业务
  -> 最后成本归因
```

不要从一条 Grafana 曲线直接执行：

- 驱逐；
- reset；
- MIG 重配；
- 抢占；
- 结算扣费。

自动动作必须有多源证据、冷却、审批边界和回滚。

---

## 20. Kueue 源码阅读：只读能解释事故的骨架

### 20.1 本节阅读目标

本章不是要你成为 Kueue 开发者。

要达到的是：

1. 从 API type 找到字段真实含义；
2. 看懂 Job 为什么 suspend 和 resume；
3. 找到 Workload 如何进入内部 queue；
4. 区分 queue manager 与 cache；
5. 找到 admission、flavor、quota 和 preemption 的决策入口；
6. 将 Admitted 后的 Pod 交回 Kubernetes scheduler 主线。

### 20.2 固定源码入口

以下链接固定到 `v0.18.3` tag。

若 tag 页面与 commit 有差异，继续用本课 commit `afd60c3` 核对。

- [`apis/kueue/v1beta2`](https://github.com/kubernetes-sigs/kueue/tree/v0.18.3/apis/kueue/v1beta2)
- [`workload_types.go`](https://github.com/kubernetes-sigs/kueue/blob/v0.18.3/apis/kueue/v1beta2/workload_types.go)
- [`clusterqueue_types.go`](https://github.com/kubernetes-sigs/kueue/blob/v0.18.3/apis/kueue/v1beta2/clusterqueue_types.go)
- [`resourceflavor_types.go`](https://github.com/kubernetes-sigs/kueue/blob/v0.18.3/apis/kueue/v1beta2/resourceflavor_types.go)
- [`pkg/controller/jobframework`](https://github.com/kubernetes-sigs/kueue/tree/v0.18.3/pkg/controller/jobframework)
- [`pkg/controller/jobs/job`](https://github.com/kubernetes-sigs/kueue/tree/v0.18.3/pkg/controller/jobs/job)
- [`pkg/queue`](https://github.com/kubernetes-sigs/kueue/tree/v0.18.3/pkg/queue)
- [`pkg/cache`](https://github.com/kubernetes-sigs/kueue/tree/v0.18.3/pkg/cache)
- [`pkg/scheduler`](https://github.com/kubernetes-sigs/kueue/tree/v0.18.3/pkg/scheduler)

源码目录可能继续拆分文件。

正确阅读方法是：

```text
先在固定tag目录确认文件存在
  -> 再搜索type或method
  -> 再跟调用
```

不要凭一个旧博客的行号跳到 `main`。

### 20.3 第一站：API types

先搜索：

```text
type WorkloadSpec struct
type WorkloadStatus struct
type PodSet struct
type Admission struct
type ClusterQueueSpec struct
type ResourceGroup struct
type FlavorQuotas struct
type ResourceQuota struct
type ResourceFlavorSpec struct
```

读 API type 时做一张表：

| 字段 | 用户写入还是controller写入 | 是否可选 | 作用域 | 进入哪条决策 |
|---|---|---|---|---|
| `spec.podSets` | 用户/集成controller | 必需 | Workload | 资源总量 |
| `spec.queueName` | 用户/集成controller | 依版本字段定义 | Namespace | LocalQueue入口 |
| `status.admission` | controller | status | Workload | quota reservation结果 |
| `spec.resourceGroups` | 管理员 | 必需 | ClusterQueue | flavor与quota |
| `spec.cohortName` | 管理员 | 可选 | ClusterQueue | 借用与共享 |

实际 required/optional 以 Go tag、kubebuilder validation 和 CRD schema 为准。

### 20.4 第二站：Job framework

Job framework 的任务不是替代 Job controller。

它负责把不同 Job 类型翻译成 Kueue 可管理的共同动作：

```text
判断是否应由Kueue管理
读取queue name
构造Workload
Suspend
Run/恢复
同步PodSets
处理完成和失败
记录Event
```

阅读 `pkg/controller/jobframework` 时，围绕这些概念搜索。

具体 interface 和方法名以固定 tag 代码为准，不依赖本章示意名。

### 20.5 Reconcile 的运维解释

controller-runtime 的 Reconcile 是：

```text
收到对象事件
  -> 读取期望和现实
  -> 尝试把现实推进到期望
  -> 返回是否重排队及error
```

它不是一次性脚本。

同一个对象可能被 Reconcile 多次。

因此 controller 代码必须尽量幂等。

值班看到重复日志时，先判断：

- 正常收敛重试；
- API conflict；
- 外部条件未满足；
- status 写入触发新事件；
- 真实 hot loop。

### 20.6 第三站：queue

`pkg/queue` 管理待 admission 的 Workload 视图和顺序。

关注：

```text
Workload如何加入
LocalQueue/ClusterQueue如何映射
priority与timestamp如何排序
inadmissible Workload何时重新激活
Cohort或ClusterQueue变化如何触发requeue
```

这里的 queue 是 Kueue 工作负载队列。

不是 kube-scheduler 的 `activeQ`、`backoffQ`、`unschedulablePods`。

### 20.7 第四站：cache

cache 保存 Kueue admission 判断需要的集群视图，例如：

```text
ClusterQueue
Cohort
ResourceFlavor
已QuotaReserved Workload的reservation usage
借用/共享关系
拓扑或其他启用能力
```

quota usage 不能只统计 `Admitted=True` 的 Workload。

Workload 已取得 quota reservation、处于 `QuotaReserved=True`，但仍等待 AdmissionChecks 时，这份 reservation 已进入配额占用视图；否则同一份 quota 可能被重复承诺。

queue 与 cache 的区别：

```text
queue
  -> 谁在等、按什么顺序尝试

cache
  -> 当前配额和已占用状态是什么
```

这和 Kubernetes scheduler 的“队列 + snapshot/cache”思路相似，但对象和决策层不同。

### 20.8 第五站：scheduler

`pkg/scheduler` 是 Kueue admission scheduler。

不要因为目录叫 scheduler 就把它当 kube-scheduler。

它主要围绕：

```text
取候选Workload
计算PodSets资源
选择ResourceFlavor
检查nominal与borrow
必要时评估preemption
写入quota reservation并形成QuotaReserved
```

它不执行 Kubernetes Node Filter/Score/Bind。

reservation 之后的职责必须拆开：

```text
pkg/scheduler
  -> flavor/quota/borrowing/preemption
  -> 写quota reservation
  -> QuotaReserved=True

各AdmissionCheck controller
  -> 更新Workload status.admissionChecks中的state与message

workload/admission controller路径
  -> 观察quota reservation
  -> 确认所有必需AdmissionChecks为Ready
  -> 设置Admitted=True

job framework
  -> 观察Workload已Admitted
  -> resume Job或推进对应集成
```

所以 `status.admission` 存在主要证明 quota assignment/reservation，不应直接解释成“scheduler 已完成所有 checks 并写了 Admitted”。

### 20.9 一条源码追踪练习

问题：

> 一个 Job 为什么一直 `spec.suspend=true`？

追踪顺序：

1. Job label/annotation 是否指向 LocalQueue；
2. job integration 是否管理该 namespace 和 Kind；
3. 是否生成 Workload；
4. Workload 是否进 queue；
5. LocalQueue 是否 Active；
6. ClusterQueue 是否 Active；
7. ResourceFlavor 与 quota 是否可 fit；
8. `status.admission` 是否存在、`QuotaReserved` 是否为 True；
9. admission checks 是否 Ready；
10. `Admitted` condition 是否为 True；
11. Job framework 是否收到更新并执行 resume；
12. patch 是否发生 conflict 或权限错误。

这比先搜“为什么 Kueue 不工作”更接近源码决策。

### 20.10 另一条源码追踪练习

问题：

> Workload 已 `Admitted=True`，为什么 Pod 还 Pending？

在 Kueue 源码侧只确认：

- admission 已写入；
- Job 已 resume 或 gate 已移除；
- flavor 信息已注入；
- controller 没有持续报错。

然后切到 Kubernetes：

```text
PodScheduled condition
  -> scheduler Events
  -> NodeResourcesFit
  -> taint/affinity/topology
  -> Preemption
  -> kubelet DeviceManager
```

不要继续在 Kueue scheduler 里寻找 Node Score。

### 20.11 复用本地 Kubernetes 源码

本课固定本地 commit：

```text
301946d15e67a4a2e8a5fb8292eb836acd366d78
```

复习入口：

```text
pkg/scheduler/schedule_one.go
pkg/scheduler/framework
pkg/scheduler/framework/plugins/noderesources
pkg/scheduler/backend/queue
pkg/scheduler/framework/preemption
```

对应课程：

- 第 8 课：主调度链；
- 第 9 课：NodeResourcesFit；
- 第 10 课：不可调度、重排队与抢占。

本章只要求你画出边界：

```text
Kueue Workload queue
  -> quota admission
  -> Job/Pod进入调度
  -> kube-scheduler scheduling queue
  -> Node Filter/Score/Bind
```

---

## 21. 为本章定向补 Go：只学读源码真正用到的语法

### 21.1 struct 与嵌套

示意代码，不是从 Kueue 原样复制：

```go
type WorkloadSpec struct {
    PodSets   []PodSet       `json:"podSets"`
    QueueName LocalQueueName `json:"queueName,omitempty"`
}
```

读法：

- `type WorkloadSpec struct` 定义一个结构体；
- `PodSets []PodSet` 是 PodSet 切片；
- `QueueName LocalQueueName` 是自定义类型；
- 反引号中的内容是 struct tag，控制 JSON 序列化。

嵌套结构体意味着 YAML 层级。

Go：

```go
type ClusterQueueSpec struct {
    ResourceGroups []ResourceGroup `json:"resourceGroups,omitempty"`
}
```

对应 YAML：

```yaml
spec:
  resourceGroups:
  - coveredResources:
    - cpu
```

### 21.2 pointer 与 `omitempty`

示意：

```go
type ResourceQuota struct {
    NominalQuota   resource.Quantity  `json:"nominalQuota"`
    BorrowingLimit *resource.Quantity `json:"borrowingLimit,omitempty"`
}
```

`*resource.Quantity` 是指针。

这里指针能区分：

```text
nil
  = 用户没有设置

指向0
  = 用户明确设置为0
```

这在 defaulting 和语义判断中很重要。

`omitempty` 表示为空时 JSON 可以省略。

但“省略”的默认语义必须看 API 注释与 defaulting，不能仅靠 Go 猜。

### 21.3 slice 与 map

Slice：

```go
var podSets []PodSet
podSets = append(podSets, onePodSet)
```

大白话：

> 一个有顺序、长度可变化的列表。

Map：

```go
usage := map[corev1.ResourceName]resource.Quantity{}
usage[corev1.ResourceName("nvidia.com/gpu")] = resource.MustParse("4")
```

大白话：

> 用资源名当 key，快速找到该资源的数量。

读 Kueue quota 代码时，经常遇到：

```text
flavor -> resource -> quantity
ClusterQueue -> usage
Workload -> assignment
```

### 21.4 interface

示意代码：

```go
type GenericJob interface {
    Object() client.Object
    IsSuspended() bool
    Suspend()
    RunWithPodSetsInfo([]PodSetInfo) error
}
```

大白话：

> 只要一种 Job 类型实现这些方法，通用 controller 就可以按同一种方式管理它。

具体固定版本 interface 方法以源码为准。

### 21.5 `context.Context`

常见签名：

```go
func (r *Reconciler) Reconcile(
    ctx context.Context,
    req ctrl.Request,
) (ctrl.Result, error) {
    return ctrl.Result{}, nil
}
```

`context.Context` 传递：

- 请求取消；
- deadline；
- tracing/logging 关联；
- 下游 API 调用生命周期。

不要把 `ctx` 当业务对象。

### 21.6 receiver

```go
func (r *Reconciler) Reconcile(
    ctx context.Context,
    req ctrl.Request,
) (ctrl.Result, error) {
    return ctrl.Result{}, nil
}
```

`(r *Reconciler)` 是方法 receiver。

大白话：

> 这个函数属于 Reconciler，函数内通过 r 访问 client、cache、recorder 等成员。

### 21.7 Reconcile result

```go
return ctrl.Result{}, nil
```

通常表示：

> 当前没有错误，也不主动要求定时 requeue；后续对象事件仍可能再次触发。

```go
return ctrl.Result{RequeueAfter: delay}, nil
```

表示一段时间后再检查。

```go
return ctrl.Result{}, err
```

表示本轮失败，由 controller-runtime 的错误重试处理。

### 21.8 errors

常见模式：

```go
if err != nil {
    return ctrl.Result{}, fmt.Errorf("get workload: %w", err)
}
```

`%w` 包装原始 error。

上层仍可用 `errors.Is` 或 `errors.As` 判断原因。

对值班有意义：

- 外层错误补充操作上下文；
- 内层错误保留 NotFound、Conflict 等类型；
- 日志不能只截取最外层一句；
- API conflict 通常应重读对象再重试。

### 21.9 Kubernetes Quantity

示意：

```go
gpu := resource.MustParse("8")
memory := resource.MustParse("256Gi")
cpu := resource.MustParse("500m")
```

`resource.Quantity` 能处理整数 GPU、millicpu、二进制内存单位和精确比较。

Kubernetes API 对扩展资源执行整数数量约束，不只是“通常建议写整数”。

`3000m` 等价于整数 `3`，因此可表示 3 个扩展资源单位；`1500m` 等价于 1.5，`0.5` 也是小数，两者对扩展资源都非法。

不要为 `nvidia.com/gpu` 写：

```yaml
nvidia.com/gpu: "0.5"
```

同样不要写：

```yaml
nvidia.com/gpu: "1500m"
```

共享通过 Device Plugin/DRA 提供的资源单位表达，不是原生小数 GPU request。

### 21.10 range

```go
for i, podSet := range workload.Spec.PodSets {
    _ = i
    _ = podSet
}
```

读法：

> 依次遍历 slice，`i` 是索引，`podSet` 是当前元素。

源码阅读阶段要继续问：

```text
这里修改的是副本？
还是slice中的原元素？
```

### 21.11 defer

```go
defer timer.ObserveDuration()
```

表示当前函数返回前执行。

常用于指标计时、unlock、close 和清理。

### 21.12 本章 Go 学习边界

必须会：

- struct、tag、pointer；
- slice、map、range；
- interface；
- method receiver；
- context；
- error 包装；
- Reconcile result；
- Quantity。

只需理解接口：

- controller-runtime cache；
- workqueue rate limiter；
- generic 类型；
- client patch helper；
- metrics recorder。

暂不钻：

- 自己实现 Kueue scheduler；
- Go runtime 调度器；
- unsafe；
- code generation 内部；
- CRD conversion webhook 实现；
- 高级并发性能优化。

---

## 22. 安全实验：没有 GPU 也能验证控制面

### 22.1 实验分两档

#### A档：无 GPU 学习集群

可以验证：

- Kueue CRD 是否存在；
- served API version；
- LocalQueue → ClusterQueue；
- Workload condition；
- ResourceQuota；
- Node 是否没有 GPU 资源；
- server-side dry-run；
- Pending/Admitted 对象链。

不能验证：

- MIG 几何；
- time-slicing 实际资源广告；
- CUDA 干扰；
- MPS 内存/计算额度；
- DCGM 实体；
- vLLM SLO。

#### B档：有 GPU 实验集群

在 A 档基础上，可以读取：

- GPU Node Capacity/Allocatable；
- GFD label；
- MIG state；
- Device Plugin ConfigMap；
- PodResources；
- DCGM；
- canary workload。

但本章不声称你的实验已通过。

### 22.2 只读盘点脚本

```powershell
param(
    [Parameter(Mandatory = $true)]
    [string]$ExpectedContext,

    [Parameter(Mandatory = $true)]
    [string]$Namespace,

    [Parameter(Mandatory = $true)]
    [string]$NodeName
)

$ErrorActionPreference = "Stop"

function Test-Command {
    param([Parameter(Mandatory = $true)][string]$Name)
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "Required command not found: $Name"
    }
}

Test-Command -Name "kubectl"

function Invoke-Kubectl {
    $KubectlArguments = @($args)
    $Output = & kubectl @KubectlArguments
    $ExitCode = $LASTEXITCODE
    if ($ExitCode -ne 0) {
        throw "kubectl failed with exit code $ExitCode. args=$($KubectlArguments -join ' ')"
    }
    return $Output
}

$ActualContext = Invoke-Kubectl config current-context
if ($ActualContext -ne $ExpectedContext) {
    throw "Context mismatch. expected=$ExpectedContext actual=$ActualContext"
}

$NamespaceObject = Invoke-Kubectl --context $ExpectedContext get namespace $Namespace -o name
if ($NamespaceObject -ne "namespace/$Namespace") {
    throw "Namespace assertion failed: $Namespace"
}

$NodeObject = Invoke-Kubectl --context $ExpectedContext get node $NodeName -o name
if ($NodeObject -ne "node/$NodeName") {
    throw "Node assertion failed: $NodeName"
}

Write-Host "=== API resources ==="
Invoke-Kubectl --context $ExpectedContext api-resources --api-group=kueue.x-k8s.io
Invoke-Kubectl --context $ExpectedContext api-resources --api-group=resource.k8s.io

Write-Host "=== Kueue objects ==="
Invoke-Kubectl --context $ExpectedContext get localqueue -n $Namespace -o wide
Invoke-Kubectl --context $ExpectedContext get clusterqueue -o wide
Invoke-Kubectl --context $ExpectedContext get workload -n $Namespace -o wide
Invoke-Kubectl --context $ExpectedContext get resourceflavor -o yaml

Write-Host "=== Namespace admission ledger ==="
Invoke-Kubectl --context $ExpectedContext get resourcequota -n $Namespace -o yaml
Invoke-Kubectl --context $ExpectedContext get job -n $Namespace -o wide
Invoke-Kubectl --context $ExpectedContext get pod -n $Namespace -o wide
Invoke-Kubectl --context $ExpectedContext get event -n $Namespace --sort-by=.metadata.creationTimestamp

Write-Host "=== Exact Node ledger ==="
Invoke-Kubectl --context $ExpectedContext get node $NodeName -o yaml
Invoke-Kubectl --context $ExpectedContext get pod -A --field-selector "spec.nodeName=$NodeName" -o wide
```

安全边界：

- 必须输入现有 namespace 和精确 Node；
- 没有 wildcard Node 变更；
- 所有命令只读；
- 输出可能包含内部标签和镜像信息，应存入受控证据目录；
- 不要把完整 Secret 或 kubeconfig 放进证据包。

### 22.3 针对一个 Workload 的证据脚本

```powershell
param(
    [Parameter(Mandatory = $true)]
    [string]$ExpectedContext,

    [Parameter(Mandatory = $true)]
    [string]$Namespace,

    [Parameter(Mandatory = $true)]
    [string]$WorkloadName
)

$ErrorActionPreference = "Stop"

if (-not (Get-Command kubectl -ErrorAction SilentlyContinue)) {
    throw "kubectl not found"
}

function Invoke-Kubectl {
    $KubectlArguments = @($args)
    $Output = & kubectl @KubectlArguments
    $ExitCode = $LASTEXITCODE
    if ($ExitCode -ne 0) {
        throw "kubectl failed with exit code $ExitCode. args=$($KubectlArguments -join ' ')"
    }
    return $Output
}

$ActualContext = Invoke-Kubectl config current-context
if ($ActualContext -ne $ExpectedContext) {
    throw "Context mismatch. expected=$ExpectedContext actual=$ActualContext"
}

$WorkloadJson = Invoke-Kubectl --context $ExpectedContext get workload $WorkloadName -n $Namespace -o json
$Workload = ($WorkloadJson -join "`n") | ConvertFrom-Json
if ($Workload.metadata.name -ne $WorkloadName -or $Workload.metadata.namespace -ne $Namespace) {
    throw "Workload assertion failed: $Namespace/$WorkloadName"
}
$WorkloadUID = [string]$Workload.metadata.uid
if ([string]::IsNullOrWhiteSpace($WorkloadUID)) {
    throw "Workload UID is empty: $Namespace/$WorkloadName"
}

Write-Host "=== Authoritative Workload JSON snapshot; UID=$WorkloadUID ==="
$WorkloadJson

Write-Warning "The following describe output is supplementary only. It is name-based and must not be used as the Workload identity fact."
Invoke-Kubectl --context $ExpectedContext describe workload $WorkloadName -n $Namespace
Invoke-Kubectl --context $ExpectedContext get event -n $Namespace --field-selector "involvedObject.uid=$WorkloadUID" --sort-by=.metadata.creationTimestamp
Invoke-Kubectl --context $ExpectedContext get pod -n $Namespace -o wide
Invoke-Kubectl --context $ExpectedContext get event -n $Namespace --sort-by=.metadata.creationTimestamp

$FinalWorkloadJson = Invoke-Kubectl --context $ExpectedContext get workload $WorkloadName -n $Namespace --ignore-not-found -o json
if ([string]::IsNullOrWhiteSpace(($FinalWorkloadJson -join "`n"))) {
    throw "Sampling window invalid: Workload $Namespace/$WorkloadName disappeared. Discard the object, Event, describe, and Pod evidence collected in this run."
}

$FinalWorkload = ($FinalWorkloadJson -join "`n") | ConvertFrom-Json
$FinalWorkloadUID = [string]$FinalWorkload.metadata.uid
if ($FinalWorkloadUID -ne $WorkloadUID) {
    throw "Sampling window invalid: Workload UID changed from $WorkloadUID to $FinalWorkloadUID. Discard all evidence collected in this run."
}

Write-Host "Sampling window identity check passed: UID remained $WorkloadUID"
```

Pod 与 Workload 的精确 owner/label 关联应从实际 integration 生成的对象读取。

脚本直接输出首次捕获的 `$WorkloadJson`，它与其中的 `metadata.uid` 共同构成本次采样的权威对象快照；随后只用该 UID 过滤 Event。

`describe`、namespace 全量 Event 和 Pod 清单仅用于补充上下文。`describe` 按 name 再读对象，不能充当身份事实；不能因为 name 相同，就把对象重建前后的证据混在一起。

脚本最后列出 namespace Pod，是为了人工建立关联，不是声称所有 Pod 都属于该 Workload。

所有补充读取结束后，脚本再次按精确 namespace/name 读取对象并比较 UID。对象消失或 UID 变化都会失败关闭，并明确宣布整个采样窗口无效；此时本轮对象、Event、`describe` 和 Pod 输出都必须丢弃后重采。

### 22.4 读取 sharing 与 MIG 标签

```powershell
param(
    [Parameter(Mandatory = $true)]
    [string]$ExpectedContext,

    [Parameter(Mandatory = $true)]
    [string]$NodeName
)

$ErrorActionPreference = "Stop"

if (-not (Get-Command kubectl -ErrorAction SilentlyContinue)) {
    throw "kubectl not found"
}

function Invoke-Kubectl {
    $KubectlArguments = @($args)
    $Output = & kubectl @KubectlArguments
    $ExitCode = $LASTEXITCODE
    if ($ExitCode -ne 0) {
        throw "kubectl failed with exit code $ExitCode. args=$($KubectlArguments -join ' ')"
    }
    return $Output
}

$ActualContext = Invoke-Kubectl config current-context
if ($ActualContext -ne $ExpectedContext) {
    throw "Context mismatch. expected=$ExpectedContext actual=$ActualContext"
}

$NodeJson = Invoke-Kubectl --context $ExpectedContext get node $NodeName -o json
if ([string]::IsNullOrWhiteSpace($NodeJson)) {
    throw "Node not found or empty response: $NodeName"
}

$Node = $NodeJson | ConvertFrom-Json
if ($Node.metadata.name -ne $NodeName) {
    throw "Exact Node mismatch"
}

$Labels = $Node.metadata.labels
$Capacity = $Node.status.capacity
$Allocatable = $Node.status.allocatable

[pscustomobject]@{
    Node = $Node.metadata.name
    NodeUID = $Node.metadata.uid
    MigCapable = $Labels.'nvidia.com/mig.capable'
    MigStrategy = $Labels.'nvidia.com/mig.strategy'
    MigConfig = $Labels.'nvidia.com/mig.config'
    MigState = $Labels.'nvidia.com/mig.config.state'
    SharingStrategy = $Labels.'nvidia.com/gpu.sharing-strategy'
    Replicas = $Labels.'nvidia.com/gpu.replicas'
    Product = $Labels.'nvidia.com/gpu.product'
    GpuCapacity = $Capacity.'nvidia.com/gpu'
    GpuAllocatable = $Allocatable.'nvidia.com/gpu'
} | Format-List
```

如果 `GpuCapacity` 为空：

- 可能没有 GPU；
- 可能资源使用 MIG profile 名；
- 可能 Device Plugin 未注册；
- 可能目标 Node 错；
- 不能自动结论为 GPU 故障。

### 22.5 变更只能 dry-run

下面示意验证 ResourceQuota API，不执行：

```powershell
param(
    [Parameter(Mandatory = $true)]
    [string]$ExpectedContext,

    [Parameter(Mandatory = $true)]
    [string]$Namespace
)

$ErrorActionPreference = "Stop"

if (-not (Get-Command kubectl -ErrorAction SilentlyContinue)) {
    throw "kubectl not found"
}

function Invoke-Kubectl {
    $KubectlArguments = @($args)
    $Output = & kubectl @KubectlArguments
    $ExitCode = $LASTEXITCODE
    if ($ExitCode -ne 0) {
        throw "kubectl failed with exit code $ExitCode. args=$($KubectlArguments -join ' ')"
    }
    return $Output
}

$ActualContext = Invoke-Kubectl config current-context
if ($ActualContext -ne $ExpectedContext) {
    throw "Context mismatch. expected=$ExpectedContext actual=$ActualContext"
}

Invoke-Kubectl --context $ExpectedContext create quota gpu-review-example `
    --namespace $Namespace `
    --hard='requests.nvidia.com/gpu=4' `
    --dry-run=server `
    -o yaml
```

注意：

- PowerShell 反引号必须是行尾最后一个字符；
- 这是 server-side dry-run；
- quota 名只是审阅示例；
- 不会创建对象；
- 生产配额应使用版本控制 YAML 和审批。

### 22.6 实验记录模板

```text
实验ID：
操作者：
日期：
kubectl context：
cluster UID：
namespace：
Node name/UID：
Kubernetes version：
GPU Operator：
Device Plugin：
MIG Manager：
Kueue version/commit：
GPU型号：
driver：
MIG策略：
sharing方法：
预期：
实际对象证据：
命令退出码：
是否有GPU：
结论：
未验证项：
回滚：
```

结论必须写：

```text
PASS
FAIL
NOT RUN
NOT APPLICABLE
```

没有 GPU 时，MIG/MPS/time-slicing 实验应标 `NOT RUN`，不能为了文档完整写 PASS。

---

## 23. 十个事故推演：从生产现象一路追到控制面

### 23.1 事故一：Workload 已 Admitted，Pod 为什么还 Pending

#### 现象

```text
Workload:
  QuotaReserved=True
  Admitted=True

Job:
  suspend=false

Pod:
  Pending
  PodScheduled=False
```

#### 常见误判

```text
Kueue已经Admitted
  -> Kueue调度成功
  -> kube-scheduler坏了
```

或：

```text
Admitted后Pending
  -> 增大ClusterQueue quota
```

#### 证据链

第一步，确认 Kueue 层：

- Workload UID；
- `status.admission`；
- assigned flavor；
- conditions；
- admission checks；
- Job 是否解除 suspend；
- Pod 是否已创建。

第二步，确认 Kubernetes 调度层：

- Pod `spec.nodeName`；
- `PodScheduled` condition；
- `FailedScheduling` Event；
- 请求的精确扩展资源名；
- Node Capacity/Allocatable；
- nodeSelector/affinity；
- taint/toleration；
- topology；
- PVC binding。

第三步，确认设备层：

- 目标 Node 是否广告该 MIG profile；
- Device Plugin 是否 Ready；
- GFD label；
- MIG state；
- Node 是否刚重配；
- 资源是否被已绑定 Pod 占用。

#### 控制面与源码

```text
Kueue scheduler
  -> 通过quota与flavor
  -> 写quota reservation
  -> QuotaReserved=True

AdmissionCheck controllers
  -> 更新所需check states

workload/admission controller
  -> 所需checks全部Ready后设置Admitted=True

Job framework
  -> 观察Admitted并resume Job

kube-scheduler
  -> NodeResourcesFit/其他Filter
  -> 找不到Node
  -> Pod进入不可调度路径
```

这时应转到第 9、10 课的 scheduler 证据链。

#### 安全处置

1. 不要盲目扩大 quota；
2. 冻结同类 Job 新提交；
3. 读取 FailedScheduling 原因；
4. 若是资源名错误，修正模板后重新提交，不直接 patch 运行中 Pod；
5. 若是节点 provisioning，观察创建链和超时；
6. 若是 topology 不可满足，重新做 PodSet/节点池设计；
7. 若 waitForPodsReady 将超时，先评估部分 Pod 副作用。

#### 防复发

- admission 后 placement wait 告警；
- Flavor 到 Node label 的持续对账；
- 扩展资源名目录；
- 提交前 server-side dry-run；
- 典型 PodSet 的可放置性仿真；
- quota 与物理容量分开 Dashboard。

---

### 23.2 事故二：把 80 个共享槽当成 80 张物理卡

#### 现象

一台 8 卡节点启用 `replicas=10`：

```text
Node Capacity nvidia.com/gpu=80
容量平台：80卡
采购报表：新增72卡
财务利用率：56/80=70%
```

#### 常见误判

把 kubelet 扩展资源广告直接当 CMDB 物理资产。

又把：

```text
共享槽占用率
```

当成：

```text
物理GPU利用率
```

#### 证据链

1. CMDB/云实例规格：物理 GPU 数；
2. GPU UUID 与 PCI Bus ID；
3. Device Plugin ConfigMap：`replicas=10`；
4. GFD：sharing strategy 与 replicas；
5. Node Capacity/Allocatable；
6. PodResources：多个 Pod 对应同一物理 GPU；
7. DCGM：设备级 metric 实体数；
8. 成本系统：计费实例和小时单价。

#### 控制面与源码

```text
Device Plugin为每个物理设备复制逻辑可分配资源
  -> ListAndWatch
  -> kubelet更新Node Capacity
  -> scheduler只看到可请求单位
```

kubelet 不负责告诉财务这些单位是不是物理卡。

#### 安全处置

1. 立即停止新增承诺；
2. 标记容量报表口径错误；
3. 从物理 GPU UUID 重建资产账；
4. 将共享槽指标更名为 `logical_access_slots`；
5. 重算历史成本；
6. 核对是否已过度接入严格 SLO 业务；
7. 不因报表修正而直接驱逐业务，另开容量止损计划。

#### 防复发

任何 GPU Dashboard 顶部固定展示：

```text
physical_gpu_count
mig_instance_count
shared_access_slot_count
```

三者禁止使用同一个“GPU 数量”标签。

---

### 23.3 事故三：time-slicing 邻居 OOM，随后多个 Pod 同时异常

#### 现象

同一物理 GPU 上 6 个共享 Pod：

```text
Pod A模型突然放大batch
Pod B/C CUDA OOM
Pod D延迟抖动
kernel log出现Xid
多个Pod重启
```

#### 常见误判

```text
每个Pod请求1个GPU
  -> 每个Pod有独立显存
```

或者：

```text
只有Pod A OOM
  -> 只重启A即可，其他业务无关
```

#### 证据链

1. Node sharing label；
2. PodResources 的 device ID；
3. 多个 Pod 是否映射同一 GPU UUID；
4. 每个容器退出原因；
5. CUDA OOM 应用日志；
6. DCGM FB_USED 与进程指标；
7. kernel NVRM/Xid 原始日志；
8. Xid 是否卡级；
9. 业务 TTFT/ITL；
10. Device Plugin health 与 Node Allocatable。

#### 控制面与源码

time-slicing 增加访问槽。

它不创建显存 cgroup。

Kubernetes scheduler 看到每个 Pod 请求 1 个逻辑资源，无法从该整数推导实际显存峰值。

Device Plugin 的 Allocate 成功也不表示显存预留成功。

#### 安全处置

1. 停止该 Node 新调度；
2. 标出同物理 GPU 的所有 Pod；
3. 保存 DCGM、kernel、PodResources 和业务证据；
4. 判断是否需要迁移整张卡上的业务；
5. 未经窗口不要直接 GPU reset；
6. 对持续 Xid 按第 19 课 runbook；
7. 将严格 SLO 服务迁出共享池；
8. 限制模型、batch 和并发参数。

#### 防复发

- `.shared` 资源名；
- 可信 namespace；
- 模型显存准入；
- canary 并发压测；
- 同卡故障域 Dashboard；
- 在线池物理隔离；
- per-process metric 只在能力确认后使用；
- Xid 影响范围按物理 GPU 展开。

---

### 23.4 事故四：MIG 重配置卡在 pending、failed 或 rebooting

#### 现象

变更单只写：

```text
把gpu-node-07改成all-1g.10gb
```

执行 label 后：

```text
mig.config.state=pending
GPU operands Terminating
Node上的业务未完全退出
```

另一台进入：

```text
mig.config.state=rebooting
Node NotReady
```

还有一台：

```text
mig.config.state=failed
Capacity中GPU资源消失
```

#### 常见误判

```text
kubectl label退出码0
  -> 变更完成
```

或：

```text
Node重启
  -> kubelet偶发问题
```

#### 证据链

1. 精确 Node name、UID、provider ID；
2. 变更前后 `mig.config`；
3. `mig.config.state` 时间线；
4. 按 ClusterPolicy 显式引用 -> per-node 动态 -> 静态 fallback 确认的实际 MIG 配置源；
5. MIG Manager Pod 和日志；
6. GPU Operator operands；
7. host GPU clients；
8. 云平台 reboot 事件；
9. driver 与 `nvidia-smi -L` 证据；
10. Capacity/Allocatable；
11. GFD label；
12. DCGM entity。

#### 控制面与源码

```text
Node label变化
  -> MIG Manager controller观察
  -> 停Operator GPU Pods和配置的host clients
  -> 启用MIG mode
  -> 必要时重启
  -> mig-parted应用geometry
  -> 恢复operands
  -> success或failed
```

#### 安全处置

1. 保持 Node cordon；
2. 停止后续节点 rollout；
3. 不删除 MIG Manager Pod 来“清状态”；
4. 保存 ConfigMap 和日志；
5. 确认用户 workload 与 host client 是否残留；
6. 对 rebooting 检查云平台生命周期；
7. 对 failed 按已知良好 profile 回滚；
8. 资源、设备、指标和 canary 全部通过后再 uncordon。

#### 防复发

- 单节点 canary；
- GPU workload=0 断言；
- host client 清单；
- 明确允许重启；
- 每节点 profile 兼容性；
- 状态超时告警；
- Operator operand 恢复验收；
- 变更批次容量安全线。

---

### 23.5 事故五：mixed 策略下资源请求名写错

#### 现象

Node：

```text
nvidia.com/mig-1g.10gb=7
```

Pod：

```yaml
resources:
  limits:
    nvidia.com/gpu: 1
```

Pod 长期 Pending。

#### 常见误判

```text
Node上明明有7个GPU
  -> scheduler cache过期
```

或：

```text
把ResourceQuota从1改成10
```

#### 证据链

1. Node Capacity 的完整资源 key；
2. Pod request 的完整 key；
3. `nvidia.com/mig.strategy=mixed`；
4. `FailedScheduling`；
5. ClusterQueue coveredResources；
6. ResourceFlavor；
7. ResourceQuota hard/used；
8. Device Plugin 配置；
9. 业务模板来源。

#### 控制面与源码

NodeResourcesFit 按资源名分别计算。

`nvidia.com/gpu` 与 `nvidia.com/mig-1g.10gb` 是两个 `ResourceName`。

没有“都是 NVIDIA GPU 所以自动转换”的逻辑。

Kueue 也不能把一个 flavor 的 MIG quota 自动兑换成整卡。

#### 安全处置

1. 不改 Node 资源；
2. 确认业务需要整卡还是 profile；
3. 修正 Job 模板并重建；
4. 同步 ResourceQuota 和 ClusterQueue coveredResources；
5. 若确实要整卡，调度到整卡池；
6. 不为一个 Pod 临时重配整节点 MIG 几何。

#### 防复发

- 资源目录；
- ValidatingAdmissionPolicy；
- CI 对照现场资源名；
- Flavor 与模板绑定；
- Dashboard 不把不同 key 相加；
- 业务界面用“规格”而非自由文本。

---

### 23.6 事故六：ResourceQuota 有余但 Kueue 不 admit，或反过来

#### 现象 A

```text
ResourceQuota:
  hard=16
  used=4

ClusterQueue:
  nominal=4
  admittedUsage=4

新Workload:
  Pending
```

#### 现象 B

```text
ClusterQueue:
  availableQuota=4

ResourceQuota:
  hard=8
  used=8

新Job:
  created
  Workload可能Admitted
  Job出现FailedCreate
  Pod未创建
```

#### 常见误判

```text
只要ResourceQuota有余
  -> Kueue就应Admit
```

或：

```text
Kueue Dashboard显示有余
  -> Job解除suspend后Pod一定创建成功
```

#### 证据链

1. Job API create 响应；
2. ResourceQuota `spec.hard` 和 `status.used`；
3. Job 是否创建；
4. LocalQueue；
5. ClusterQueue active condition；
6. coveredResources；
7. flavor；
8. nominal、borrow、lend；
9. Workload conditions；
10. admission checks；
11. PodSet 总资源。

#### 控制面与源码

```text
Job API admission
  -> 创建Job对象；Pod template里的GPU request此时不扣Pod资源配额

Kueue admission
  -> 决定批任务何时取得队列quota

Job controller
  -> 解除suspend后创建Pod

Pod API admission / ResourceQuota
  -> 决定每个Pod能否接纳
```

它们是串联的两个控制面。

任何一个都可以阻止任务继续。

#### 安全处置

1. 先确定失败发生在哪一层；
2. Job `FailedCreate` 或无 Pod 时查 Job Event 与 ResourceQuota；
3. Workload Pending 就查 ClusterQueue/Flavor/AdmissionCheck；
4. 不同时放大两边配额；
5. 对 PodSet 重新计算 count×request；
6. 检查 quota key 是否是正确扩展资源名；
7. 变更前模拟会影响的其他租户。

#### 防复发

同一页面并排展示：

```text
Namespace admission ledger
Kueue admission ledger
Node placement ledger
```

每个“剩余”都带作用域和资源名。

---

### 23.7 事故七：借用后，高优任务抢占导致长训练损失

#### 现象

```text
team-a nominal=8
team-a当前使用=12，借入4

team-b高优任务需要4
Kueue触发reclaim/preemption

team-a训练已运行9小时
最近checkpoint在3小时前
```

#### 常见误判

```text
借用就是空闲资源
  -> 随时收回没有成本
```

或：

```text
Priority高
  -> 可以忽略被抢占业务
```

#### 证据链

1. Cohort 和 ClusterQueue 配置；
2. nominal、borrow、lend；
3. Workload priority；
4. preemption policy；
5. victim Workload condition/Event；
6. Kueue controller decision log；
7. Job deletion/suspend/eviction；
8. 最近 checkpoint 时间；
9. 已消耗 GPU-hours；
10. 重启预计成本；
11. 交付 SLA。

#### 控制面与源码

Kueue preemption 在 quota 层选择 victim。

它知道资源和优先级。

它通常不知道：

- 模型质量；
- 未落盘训练进度；
- checkpoint 是否损坏；
- 数据 shuffle 成本；
- 财务损失；
- 业务发布日期。

#### 安全处置

1. 若策略允许，暂停新的抢占扩散；
2. 保留 victim 与决策证据；
3. 检查可用 checkpoint；
4. 评估是否让任务先完成下一 checkpoint；
5. 从 checkpoint 重提而不是无脑重跑；
6. 把丢失 GPU-hours 计入失败成本；
7. 核对高优任务是否真的可放置，避免“抢占后仍 Pending”。

#### 防复发

- 只让可恢复任务进入可抢占队列；
- checkpoint 周期小于回收 SLA；
- 借用上限；
- 预抢占通知；
- 训练剩余时间和 sunk cost 纳入人工策略；
- 高优任务预留容量；
- 抢占演练；
- 成本报表记录 victim 损失。

---

### 23.8 事故八：waitForPodsReady 反复循环

#### 现象

```text
Workload admitted
3/4 workers Ready
1/4 Pending
timeout
evicted
requeue
再次admitted
仍然3/4 Ready
```

循环数十次。

对象存储显示模型被重复下载。

#### 常见误判

```text
waitForPodsReady是gang scheduler
  -> 下次一定能四个一起启动
```

或：

```text
增加requeue次数就会恢复
```

#### 证据链

1. Workload condition 时间线；
2. `PodsReadyTimeout` reason；
3. requeuingStrategy；
4. backoff count；
5. 每轮 Pod UID；
6. 每个 Pod Scheduled/Ready 时间；
7. 第四个 Pod Event；
8. Node topology；
9. 模型下载日志与对象存储流量；
10. checkpoint 和临时文件；
11. Kueue queue wait 与 placement wait。

#### 控制面与源码

```text
Kueue先Admit
  -> 普通Pod调度发生
  -> 部分Pod可能运行
  -> Ready超时后取消admission
  -> Workload重新排队
```

没有原子回滚已发生的下载和初始化。

#### 安全处置

1. 暂停或 deactivate 该 Workload；
2. 停止无限 requeue；
3. 找到固定不可满足条件；
4. 清点重复下载与临时副作用；
5. 修正 topology、flavor、taint 或 PodSet；
6. 确认模型 cache 可以安全复用；
7. 重新提交前进行可放置性检查。

#### 防复发

- `backoffLimitCount`；
- placement reason 告警；
- PodSet 与节点拓扑预检；
- 模型下载幂等；
- checkpoint；
- 本地 cache 生命周期治理；
- 对循环次数和浪费成本计量；
- 不把 timeout 当自动修复。

---

### 23.9 事故九：在线和批任务同池，SLO 被“高利用率”吞掉

#### 现象

```text
GPU utilization从55%升到95%
batch吞吐提升
online TTFT P99从1.2s升到7.8s
ITL持续抖动
错误率尚未明显升高
```

容量团队认为“利用率更健康”。

#### 常见误判

```text
GPU利用率高
  -> 集群更高效
```

或：

```text
Kueue按quota公平
  -> GPU性能也公平
```

#### 证据链

1. Node pool 和 ResourceFlavor；
2. 在线/批 Pod 是否共享物理 GPU；
3. sharing strategy；
4. PodResources device ID；
5. DCGM utilization、FB、power；
6. vLLM running/waiting；
7. KV cache usage；
8. TTFT、ITL、e2e；
9. batch Job 开始时间；
10. Kueue admission；
11. token/GPU-hour；
12. 错误和取消请求。

#### 控制面与源码

Kueue 只控制哪些 Workload 入场。

time-slicing 不提供稳定计算份额。

DCGM 95% 只表示设备忙，不说明忙的是哪个业务，也不说明 SLO。

#### 安全处置

1. 停止新的 batch admission；
2. 若安全，等待或 checkpoint 当前批任务；
3. 将 batch 从在线物理卡迁出；
4. 恢复在线保留容量；
5. 检查 vLLM queue 与 cache；
6. 不为降利用率直接 reset GPU；
7. 以 SLO 恢复作为验收，而非 DCGM utilization 降低。

#### 防复发

- online 与 batch 物理分池；
- Flavor/taint；
- 在线禁用 `.shared`；
- Kueue 在线保留 quota；
- vLLM SLO 与 DCGM 联合告警；
- 发布和流量峰值容量；
- 利用率与成功 token/GPU-hour 同看。

---

### 23.10 事故十：设备指标被复制到多个 Pod，账单与告警同时翻倍

#### 现象

同一物理 GPU：

```text
真实功耗=600W
真实utilization=80%
共享Pod数=5
```

监控联表后：

```text
总功耗=3000W
总utilization=400%
五个Pod都收到“使用80%GPU”的账单
```

#### 常见误判

```text
exporter给metric加了pod label
  -> metric一定是Pod独占值
```

#### 证据链

1. metric HELP/type；
2. 指标实体是 physical GPU、MIG 还是 process；
3. `UUID`/`GPU_I_ID`/`GPU_CI_ID`；
4. PodResources 映射；
5. 同一设备对应多少 Pod；
6. exporter 是否复制 device series；
7. per-process feature 是否启用；
8. PromQL group key；
9. recording rule；
10. 成本 ETL join；
11. 时间窗与 staleness。

#### 控制面与源码

Pod label 是归属提示。

它不自动改变 metric 的物理含义。

一个 device gauge 被关联到多个 Pod 后，仍是同一物理设备样本。

PromQL `sum by (pod)` 可以把重复样本变成看似合理的错误结果。

#### 安全处置

1. 暂停错误账单；
2. 冻结相关 recording rule；
3. 按物理 GPU UUID 去重；
4. 区分 device、MIG、process 指标；
5. 对无法精确归因的时段明确标注；
6. 重算历史数据；
7. 不删除原始 series；
8. 通知受影响租户。

#### 防复发

- metric catalog 记录 entity scope；
- 联表前唯一键约束；
- physical total 只聚合一次；
- 共享模式显式 label；
- per-process 能力测试；
- 财务对账恒等式；
- Dashboard 同时显示物理总量和归集总量；
- 规则变更 canary。

### 23.11 十个事故的共同模式

它们都在混淆两个层：

```text
访问名额
  != 物理设备

配额入场
  != 节点放置

对象状态
  != 业务Ready

设备指标
  != Pod独占指标

公平入场/配额抢占
  != 性能隔离

利用率
  != 业务价值
```

值班时先找被混淆的两层，再找对应证据源。

---

## 24. 企业平台规则清单：把本章结论变成可执行契约

### 24.1 资源命名规则

1. 整卡独占与共享访问不使用相同服务目录名称；
2. time-slicing 优先 `renameByDefault=true`；
3. mixed MIG profile 使用现场真实扩展资源名；
4. ResourceFlavor 名包含型号、形态和服务等级；
5. 自定义 Node label 使用企业域名；
6. 禁止把 `gpu=80` 展示为“80张物理卡”；
7. 资源目录记录版本、节点池、故障域和SLO。

### 24.2 工作负载准入规则

1. 所有批任务必须指定受批准 LocalQueue；
2. 在线严格 SLO 禁止请求 `.shared`；
3. time-slicing 单容器请求大于 1 时拒绝；
4. MPS 使用独立准入规则，不能复制不存在的插件字段；
5. 每个容器都必须声明合适的 CPU、内存 requests；只有真正执行 GPU 计算的 worker 容器声明 GPU，launcher、sidecar 或纯控制容器不得被平台规则强迫占用 GPU；
6. GPU 扩展资源 request/limit 按 Kubernetes 规则一致；
7. 镜像固定 digest；
8. 必须有 tenant、project、cost-center、service-tier；
9. 可抢占任务必须声明 checkpoint 和恢复方式；
10. 多 Pod 任务必须声明 topology 与最大启动时间。

### 24.3 MIG 规则

1. profile 必须在 GPU 型号支持矩阵中；
2. 重配前 checkpoint；
3. 精确 Node cordon；
4. 业务 GPU workload=0；
5. host CUDA client=0 或已纳入 clients ConfigMap；
6. 单节点 canary；
7. `failed` 立即停止；`pending` 或 `rebooting` 是允许的过渡态，只有超过变更单批准的阶段时限或进入未经批准的重启路径才判异常；
8. 当前节点未到 `success` 且未完成设备、资源、指标和业务验收前，不扩下一批；
9. 回滚走已知良好 profile；
10. 不为单个临时任务频繁重配整池几何。

### 24.4 sharing 规则

1. time-slicing 只用于可信内部租户；
2. 不承诺显存或计算份额；
3. 共享 Pod 显示物理故障域；
4. device total 不复制后求和；
5. 线上核心推理不与批任务共享物理卡；
6. replicas 变更同时审计 quota、Kueue 和成本；
7. `failRequestsGreaterThanOne=true` 只属于 time-slicing 当前 schema；
8. MPS 当前实验性；
9. MPS 与 time-slicing 互斥；
10. MPS 不与 MIG 混用。

### 24.5 Kueue 规则

1. 所有 YAML 使用 `v1beta2`；
2. Cohort 字段使用 `spec.cohortName`；
3. PodSet 资源按 count×request；
4. `QuotaReserved`、`Admitted`、`Scheduled`、`Ready` 分开；
5. AdmissionCheck 未 Ready 时不把 quota reservation 当 admission；
6. ResourceQuota 与 Kueue quota 分开；
7. Flavor 必须能映射现存 Node，或映射经过验证的 autoscaler/provisioner Node template；允许 scale-to-zero，但最终 admission/placement 必须获得真实容量证据；
8. 借用必须有 borrowing/lending 上限评审；
9. 抢占前核对 checkpoint；
10. waitForPodsReady 有最大循环和 backoff；
11. AdmissionFairSharing 与 Cohort weighted-share Fair Sharing 分开监控；
12. 变更 preemption 策略需要跨团队审批。

### 24.6 成本规则

1. 每日核对 physical GPU-hours；
2. 共享槽不进入物理资产数；
3. MIG 权重声明为财务规则；
4. time-slicing 不按 replicas 天然平分；
5. 保留平台 idle/fragmentation 差额；
6. 失败重试计入成本；
7. 抢占损失计入 victim 成本；
8. 冷启动单列；
9. 推理至少看成功 token 成本；
10. 训练至少看成功 run 或 step 成本；
11. 利用率不能单独作为价值 KPI；
12. 原始物理成本与分摊总额必须守恒。

---

## 25. 值班一页纸

### 25.1 先问五句话

```text
1. 现在看到的是物理GPU、MIG实例还是共享槽？
2. 失败发生在API准入、Kueue admission、scheduler placement还是GPU运行？
3. 资源名和ResourceFlavor是否精确匹配？
4. 故障影响的是进程、slice、物理卡还是整台Node？
5. 当前动作会不会重配、重启、抢占或扩大故障域？
```

### 25.2 看到 Pending

```text
Job未创建
  -> API/RBAC/对象ResourceQuota

Job suspend=true
  -> LocalQueue/ClusterQueue/Workload/quota/checks

QuotaReserved但未Admitted
  -> AdmissionChecks

Admitted且Job FailedCreate
  -> Pod ResourceQuota/admission

Pod Pending且未Scheduled
  -> scheduler Event/资源名/Node/taint/topology

Pod Scheduled但未Running
  -> kubelet/CRI/image/DeviceManager

Pod Running但业务未Ready
  -> 模型加载/GPU进程/vLLM或训练框架
```

### 25.3 看到 GPU 数量突变

查：

```text
物理GPU UUID
Device Plugin config
MIG strategy
sharing replicas
GFD labels
Node Capacity/Allocatable
DRA objects
最近Operator或ConfigMap变更
```

不要立刻：

- 扩容业务；
- 调整财务；
- 删除 Device Plugin；
- 重配 MIG；
- 重启 kubelet。

### 25.4 看到 Xid 或多个共享 Pod 失败

查：

```text
物理GPU UUID
同卡Pod集合
kernel log
DCGM
PodResources
MIG实体
容器退出
业务SLO
```

动作：

1. 停止新调度；
2. 保存证据；
3. 评估整卡影响；
4. 迁移业务；
5. 经审批诊断/reset/RMA。

### 25.5 看到 queue wait 上升

分解：

```text
LocalQueue入口正常？
ClusterQueue Active？
quota不足？
flavor不足？
借用上限？
AdmissionCheck未Ready？
Fair Sharing排序？
高优大Job阻塞？
```

不要把 admission 后的 Node provisioning 时间算成 queue wait。

### 25.6 看到成本对不上

先做守恒：

```text
物理实际成本
  = 全部Pod/任务已分摊成本
    （成功 + 失败 + 重试 + 被抢占victim）
  + 未分摊物理差额
```

失败、重试和被抢占任务已经属于实际消耗 GPU 的任务成本，不能再作为一笔“差额”重复相加。

如果平台继续拆分“未分摊物理差额”，可以按约定分成 idle、fragmentation、平台保留等原因，但分类必须互斥：同一张 GPU 的同一个时间片只能进入一类，不能既算 idle 又算 fragmentation。

再查：

- time-slicing 是否按 replicas 错分；
- 父 GPU metric 是否复制；
- MIG 权重是否漏记；
- Job/Pod UID 是否复用；
- 时间窗是否重叠；
- 抢占前 run 是否漏计。

---

## 26. 专业名词表

| 名词 | 大白话 | 最容易误解的点 |
|---|---|---|
| Physical GPU | 真实插在机器里或云实例提供的一张卡 | 不等于逻辑资源数量 |
| MIG | 把支持的物理卡做硬件分区 | 不是独立服务器 |
| GI | GPU Instance，卡内一组硬件资源 | 不是 Kubernetes Node |
| CI | Compute Instance，GI内计算实例 | 不等于新的物理GPU |
| MIG profile | 一种slice几何规格 | 性能不保证严格线性 |
| MIG geometry | 一张卡当前slice组合 | 改它是设备维护 |
| `single` | 统一MIG资源语义 | 可能隐藏profile差异 |
| `mixed` | 按profile广告资源 | 资源名必须精确请求 |
| time-slicing | 多进程共享访问同卡 | 无显存/性能硬隔离 |
| replicas | 每个设备复制的逻辑访问槽数 | 不是算力比例 |
| MPS | CUDA多进程协调服务 | 当前插件实验性，不等于MIG |
| DRA | 结构化声明和分配设备 | API能力不自动等于隔离 |
| DeviceClass | DRA设备类别 | 不是物理库存 |
| ResourceClaim | 工作负载设备申请 | 要看status allocation |
| ResourceSlice | DRA driver广告设备的对象 | 不等于MIG slice名称 |
| Capacity | Node广告资源总量 | sharing下可能是逻辑数 |
| Allocatable | Node可供调度的资源量 | 不代表当前free |
| ResourceQuota | Namespace API接纳门槛 | 不验证物理容量 |
| LocalQueue | Namespace租户入口 | 不是实际Node队列 |
| ClusterQueue | 集群级批任务quota池 | 不是kube-scheduler |
| Workload | Kueue的admission对象 | 不等于Pod |
| PodSet | 一组同模板Pod及数量 | 总量要乘count |
| ResourceFlavor | 资源规格与节点约束 | 不创建节点 |
| Cohort | 多个CQ共享quota的组织 | 只能按定义的flavor借 |
| nominalQuota | 自己的名义份额 | 不等于物理库存 |
| borrowingLimit | 最多借入多少 | 方向不要和lending混 |
| lendingLimit | 最多借出多少 | 不等于保留未借部分全部可用 |
| quota reservation | Kueue为Workload预留quota | 不一定已经Admitted |
| AdmissionCheck | quota外的准入检查 | 未Ready时不能Admit |
| Admitted | Kueue允许工作负载开始 | 不等于Scheduled |
| Scheduled | kube-scheduler已绑定Node | 不等于Ready |
| waitForPodsReady | 超时管理全体Ready | 不是原子gang |
| preemption | 回收quota给其他任务 | 计算损失不会返还 |
| AdmissionFairSharing | 同CQ内按LocalQueue历史消费公平入场 | 不等于weightedShare |
| weightedShare | 跨CQ/Cohort共享占用指标 | 不等于GPU utilization |
| fragmentation | 总量有余但形态放不下 | MIG和拓扑尤其明显 |
| physical GPU-hours | 物理卡数乘计费时间 | 最基础成本账 |
| allocated GPU-hours | 分配给任务的卡时 | 起止口径必须固定 |
| TTFT | 首token时间 | 与queue wait不是一回事 |
| ITL | token间延迟 | 对共享噪声敏感 |
| Xid | NVIDIA driver报告的GPU错误事件 | 编号是诊断起点，不是自动RMA |

---

## 27. 学习深度边界：哪些必须深读，哪些知道接口就够

### 27.1 必须深读

| 主题 | 达标标准 |
|---|---|
| 物理卡、MIG、共享槽 | 看到任意数量能说出单位和故障域 |
| single/mixed | 能从Node资源名反推请求方式 |
| time-slicing | 能解释replicas、rename和显存边界 |
| MIG变更 | 能独立审阅完整runbook |
| 三本数量账 | 能定位ResourceQuota、Kueue和Node各自结论 |
| Kueue链路 | 能解释LocalQueue到Admitted再到scheduler |
| PodSet数学 | 能正确算count×requests |
| quota借用 | 能算nominal、borrow、lend |
| Admitted != Scheduled | 能从Event切换到K8s scheduler排障 |
| 共享指标归因 | 不复制物理device total后求和 |
| 成本守恒 | 能对平物理成本、归集和差额 |

### 27.2 必须理解接口

| 主题 | 需要理解 | 暂不要求 |
|---|---|---|
| Kueue API types | 字段、status、Quantity | 写CRD生成器 |
| Job framework | suspend/resume/Workload | 新增一种Job integration |
| queue/cache/scheduler | 责任边界 | 实现admission算法 |
| Fair Sharing | 两种机制、对象和指标 | 推导所有内部公式 |
| DRA | DeviceClass/Claim/Slice | 编写NVIDIA DRA driver |
| MPS | 当前限制与故障域 | 调试CUDA MPS server源码 |
| DCGM per-process | 能力和归因边界 | 实现collector |
| vLLM metrics | SLO与GPU联表 | 修改engine scheduler |

### 27.3 可以一笔带过

- MIG firmware 内部；
- 每代 GPU 所有 profile 编码；
- Kueue 所有 integration；
- MultiKueue；
- 高级 CohortTree 优化；
- DRA partitionable device 全部实现细节；
- 自定义 scheduler plugin；
- 成本会计系统产品选型。

一笔带过不等于永远不学。

它表示当前 GPU 运维转型阶段，投入回报不如控制面证据链。

### 27.4 暂时不要钻

- 从零实现 GPU 调度器；
- 修改 NVIDIA driver；
- 修改 MIG firmware；
- 自己发明共享安全承诺；
- 仅为读 Kueue 学完整 Go 并发模型；
- 在生产尝试未验证的 MPS/MIG 组合；
- 用复杂 PromQL 掩盖身份键缺失；
- 在没有成本口径时做精细 chargeback。

### 27.5 你的达标画像

完成本章后，应该能在白板上画：

```text
物理GPU
  -> 资源形态
  -> Node/DRA设备账
  -> Namespace准入
  -> Kueue quota与公平
  -> kube-scheduler节点放置
  -> kubelet设备分配
  -> GPU进程
  -> DCGM/业务SLO
  -> 成本
```

并能为每一条箭头说出：

- 关键对象；
- 关键状态；
- 最小证据；
- 常见误判；
- 安全动作。

---

## 28. 自测题

### 28.1 判断题

1. 8 张 GPU 配置 replicas=10 后，集群物理容量变为 80 张。
2. time-slicing 请求 2 个共享资源一定获得两倍算力。
3. MPS `v0.19.3` 可与 MIG 同时使用。
4. MIG instance 在卡级 Xid 时一定互不影响。
5. ResourceQuota hard 可以大于集群物理容量。
6. Kueue Admitted 表示 kube-scheduler 已绑定 Node。
7. `QuotaReserved=True` 在有 AdmissionCheck 时一定等于 `Admitted=True`。
8. waitForPodsReady 是原子 gang scheduler。
9. 同一 physical GPU gauge 复制给五个共享 Pod 后可以求和。
10. Kueue Fair Sharing 能保证 time-slicing 每个进程性能公平。

### 28.2 简答题

11. 为什么 `renameByDefault=true` 有平台价值？
12. `single` 和 `mixed` 最重要的调度差异是什么？
13. MIG Manager `success` 后为什么仍不能立即 uncordon？
14. ResourceQuota 与 Kueue quota 分别解决什么？
15. 为什么 Job 的 GPU Pod template 不在 Job 创建时扣 Pod GPU quota？
16. AdmissionFairSharing 与 Cohort weighted-share Fair Sharing 有什么区别？
17. 为什么 MPS 仍不能作为不互信租户的硬隔离？
18. 为什么可扩容 8 张 GPU 不等于当前可用 8 张？

### 28.3 计算题

19. 4 张整卡配置 time-slicing replicas=6，`renameByDefault=true`。逻辑资源名和数量是什么？物理卡数是多少？
20. team-a nominal=8、borrowingLimit=5；team-b nominal=12、当前用8、lendingLimit=3。忽略其他队列时，team-a 最多借多少？最多总用多少？
21. Workload 有 worker PodSet：count=8，每个请求2 GPU；launcher count=1，请求0 GPU。Kueue GPU quota需求是多少？
22. 一台8卡机器运行12小时。物理卡时是多少？任务分配6卡10小时，失败重试2卡2小时，剩余未归集卡时是多少？
23. 一张卡100元/小时，切7个1g profile。企业定义每个财务权重1/7。4个slice运行3小时，按该政策业务归集多少？未归集的物理成本是多少？

### 28.4 场景题

24. Workload Admitted，Job suspend=false，但 Job Event 显示 FailedCreate，namespace GPU ResourceQuota used=hard。根因在哪一层？
25. ClusterQueue 显示剩余8，Pod Event 是 `Insufficient nvidia.com/mig-3g.40gb`。应该扩大 quota 吗？
26. online vLLM 与 batch 共用 time-slicing 卡，TTFT P99 抖动但 GPU utilization 更高。先做什么？
27. MIG label 变更后 state=rebooting。为什么不能删 Pod 强行恢复？
28. 五个共享 Pod 都带同一 GPU UUID，Dashboard 求和为400%。如何修？
29. waitForPodsReady 第六次超时，固定第四个 Pod 因 taint Pending。继续增加 timeout 能解决吗？
30. 高优 Job 要4卡，抢占了运行9小时但三小时未checkpoint的训练，抢占后高优 Pod 又因 topology Pending。平台设计哪里失误？

---

## 29. 自测答案

### 29.1 判断题答案

1. 错。80 是逻辑访问槽，物理仍是 8。
2. 错。共享槽数量不承诺成比例算力。
3. 错。当前插件 MPS 不支持 MIG。
4. 错。同卡仍有卡级共同故障域。
5. 对。ResourceQuota 不验证物理容量。
6. 错。Admitted 是 Kueue 入场，Node placement 仍由 kube-scheduler。
7. 错。checks 未 Ready 时可只完成 quota reservation。
8. 错。它是超时取消 admission 并重排队的管理机制。
9. 错。物理 device total 只能计一次。
10. 错。入场公平不是 GPU kernel 性能隔离。

### 29.2 简答题答案

11. 它把共享语义放进资源名，便于准入、quota、成本和业务识别，减少把共享当独占。
12. `single` 使用统一资源语义；`mixed` 按 profile 暴露不同扩展资源名，Pod 必须精确请求。
13. 还需核对 Node Ready、operator operands、资源数量、设备实体、PodResources、DCGM 和业务 canary。
14. ResourceQuota 是 Namespace API 接纳门槛；Kueue quota 是批 Workload 的队列入场预算。
15. Job 只是保存 Pod template；Job controller 真正创建 Pod 时，每个 Pod 才经过 Pod API ResourceQuota admission。
16. AdmissionFairSharing 在同一 CQ 内依据来源 LocalQueue 历史消费排序；weighted-share Fair Sharing 比较跨 CQ/Cohort 的共享占用，参与 admission/preemption。
17. 它仍共享物理卡、daemon 和卡级故障域，而且当前支持是实验性，不能替代系统安全层。
18. 节点创建、driver、Device Plugin、MIG、镜像、模型和 warmup 都有 provisioning 时间和失败面。

### 29.3 计算题答案

19. 逻辑资源通常为 `nvidia.com/gpu.shared=24`；物理卡仍为 4。最终名称以现场配置和 Node 为证。
20. team-b 名义空闲4，但最多借出3；team-a最多借入受自身5和对方3限制，所以借3，总用11。
21. `8×2 + 1×0 = 16` GPU。
22. 物理卡时 `8×12=96`；任务60，失败4，剩余 `96-60-4=32` 卡时。
23. 物理成本 `100×3=300` 元；业务归集 `100×4/7×3≈171.43` 元；未归集约 `128.57` 元。权重只是财务规则。

### 29.4 场景题答案

24. Kueue 已放行，但 Job controller 创建 Pod 时被 Pod ResourceQuota admission 拒绝；查 Job Event 与 quota，不是先查 scheduler。
25. 不应先扩大。quota 已够，目标 profile 在 Node 放置层不足；查 geometry、Flavor、资源名和 provisioning。
26. 停止新 batch admission，建立同卡关联证据，安全迁移批任务，恢复在线保留容量，以 TTFT/ITL 恢复验收。
27. reboot 是 MIG mode/geometry 变更状态的一部分；删 Pod不能撤销硬件状态，反而丢证据并可能扩大不一致。
28. physical GPU 指标按 UUID 去重，只保留一次；Pod 仅标共享关系，只有可靠 per-process 指标才做进程归因。
29. 不能。固定 taint 不匹配不会被时间修复；暂停循环，修 toleration/Flavor/节点池。
30. 抢占前没有确认高优任务可放置，也没有考虑 victim checkpoint 成本；需要 capacity/topology 预检和恢复策略。

---

## 30. 跨第 14–21 课 Capstone：把 Java 平台能力迁移成 GPU 平台能力

### 30.1 项目背景

你负责的现有平台已经稳定运行 Java 应用，具备：

- Namespace 与 RBAC；
- Deployment/Job；
- ResourceQuota；
- Prometheus/Grafana；
- 发布与回滚；
- SLO；
- 值班和变更管理。

现在要接入：

```text
在线vLLM推理
  + 离线GPU批任务
  + 可信开发共享任务
```

目标不是“装一个 GPU Operator”。

目标是交付一套可解释、可运维、可计量、可回滚的平台。

### 30.2 固定技术基线

```text
Kubernetes:
  301946d15e67a4a2e8a5fb8292eb836acd366d78

GPU Operator:
  v26.3.3

k8s-device-plugin:
  v0.19.3

MIG Manager:
  v0.14.2

DCGM Exporter:
  4.5.3-4.8.2

Kueue:
  v0.18.3 / afd60c3 / v1beta2

vLLM:
  v0.25.0
```

### 30.3 目标架构

```text
业务入口
  ├─ online namespace
  │    └─ online-exclusive ResourceFlavor
  │         └─ H100整卡节点池
  │
  ├─ batch namespace
  │    └─ LocalQueue
  │         └─ ClusterQueue/Cohort
  │              └─ 固定MIG profile节点池
  │
  └─ trusted-dev namespace
       └─ dev-shared ResourceFlavor
            └─ time-slicing共享池

设备控制面
  -> Operator/Driver/Toolkit/Device Plugin/MIG Manager

调度控制面
  -> ResourceQuota/Kueue/kube-scheduler/kubelet

运行与观测
  -> PodResources/DCGM/vLLM/Job metrics

治理
  -> RBAC/Admission/成本/变更/值班
```

### 30.4 第 14 课交付物：节点栈

提交：

1. driver、CUDA、Container Toolkit、containerd、CDI 版本账本；
2. Node runtime 配置快照；
3. `nvidia-smi`、runtime、CDI 的证据链；
4. 驱动升级和回滚 runbook；
5. 无 GPU 环境的控制面验证边界。

验收：

```text
容器能获得预期设备
Node重启后栈恢复
版本与digest可追溯
不靠手工临时修改
```

### 30.5 第 15–17 课交付物：设备账本

提交：

1. Device Plugin 注册路径图；
2. ListAndWatch 到 Capacity/Allocatable；
3. Allocate 到容器注入；
4. checkpoint 恢复说明；
5. health 与资源广告边界；
6. PodResources 联表；
7. CDI device ID 证据。

验收问题：

> 给出一个 Pod UID，能否在十分钟内找到 Node、container、device ID、GPU UUID/MIG UUID 和当前 health？

### 30.6 第 18 课交付物：Operator 生命周期

提交：

1. ClusterPolicy 与 operands 清单；
2. image digest；
3. 安装、升级、回滚；
4. air-gapped 依赖；
5. PSA/RBAC；
6. must-gather 或等价证据包；
7. managed/unmanaged 边界。

验收：

```text
Operator Running
  != operands Ready
```

必须逐 operand 验收。

### 30.7 第 19 课交付物：GPU 健康

提交：

1. DCGM/exporter 版本与 counters；
2. metric entity catalog；
3. Xid、ECC、温度、功耗、NVLink 告警；
4. device/MIG/process/Pod 归因；
5. 诊断安全等级；
6. 证据保留；
7. RMA 前置条件。

验收：

> 任意 Xid 告警不能仅凭编号自动 reset 或 RMA。

### 30.8 第 20 课交付物：业务 SLO

提交：

1. vLLM 版本与启动参数；
2. 模型加载与显存预算；
3. startup/readiness/liveness；
4. TTFT、ITL、e2e、queue、KV；
5. CUDA OOM 与 OOMKilled 区分；
6. rollout、冷启动与 autoscaling；
7. token 成本。

验收：

> GPU utilization 上升时，平台仍能判断 SLO 是变好还是变坏。

### 30.9 第 21 课交付物：共享、队列与成本

提交：

1. 五方案决策矩阵；
2. 三个节点池；
3. MIG geometry 目录；
4. time-slicing/MPS 使用边界；
5. ResourceQuota；
6. LocalQueue、ClusterQueue、Flavor、Cohort；
7. waitForPodsReady；
8. preemption/checkpoint；
9. 成本守恒；
10. 十类事故 runbook。

验收：

> 任何报表中的“GPU 数”必须能回答它是物理卡、MIG instance、共享槽、Kueue quota 还是财务权重。

### 30.10 演练一：Admitted 但 Pending

注入条件：

```text
ClusterQueue有quota
目标ResourceFlavor存在
Pod请求一个集群中不存在的MIG资源名
```

期望团队：

1. 看到 Workload Admitted；
2. 不扩大 Kueue quota；
3. 从 Pod Event 找到 `Insufficient resource`；
4. 对照 Node resource key；
5. 修复模板；
6. 记录 queue wait 与 placement wait。

### 30.11 演练二：共享邻居噪声

只允许在有审批的隔离实验集群执行。

设计：

- 两个可信 canary 共享一张 GPU；
- 一个逐步增加负载；
- 另一个记录稳定请求；
- 同时记录 DCGM、PodResources、TTFT/ITL；
- 设置停止阈值；
- 不触发失控显存或破坏性 kernel。

本章不提供可直接压垮 GPU 的命令。

验收：

- 能证明共享槽不等于性能份额；
- 能将两个 Pod 关联到同一物理 GPU；
- 到阈值自动停止实验；
- 没有把设备 total 重复求和。

### 30.12 演练三：MIG 变更桌面推演

不执行真实重配。

团队拿一份变更单，回答：

1. 哪台 Node 和 UID；
2. 哪些业务要 checkpoint；
3. 如何证明 GPU workload=0；
4. 哪些 host clients；
5. 是否可能 reboot；
6. 按 ClusterPolicy 显式引用 -> per-node 动态 -> 静态 fallback 确认的实际 MIG 配置源；
7. 观察哪些 state；
8. 什么条件停止扩散；
9. success 后如何验收；
10. 如何回滚。

少一个答案，变更不批准。

### 30.13 演练四：抢占损失

给定：

```text
victim已运行10小时
最近checkpoint在2小时前
8张GPU
单卡成本100元/小时
高优任务需要8张GPU
但目标topology尚未验证
```

要求：

1. 计算最大直接丢失：`2×8×100=1600元`；
2. 加上重启与冷启动；
3. 先验证高优任务 placement；
4. 再决定抢占；
5. 记录 victim 恢复。

### 30.14 Capstone 最终评审

评审不是看 PPT 页数。

现场随机抽一个：

- Node；
- Workload；
- Pod；
- GPU UUID；
- 成本时间窗。

要求在受控只读权限下完成：

```text
对象身份
版本
资源形态
queue/admission
placement
device allocation
health
业务SLO
成本
安全动作
```

每个结论都必须有证据。

---

## 31. 官方资料索引

### 31.1 NVIDIA

- [GPU Operator v26.3.3 release](https://github.com/NVIDIA/gpu-operator/releases/tag/v26.3.3)
- [GPU Operator 26.3 文档](https://docs.nvidia.com/datacenter/cloud-native/gpu-operator/26.3/)
- [GPU Operator MIG](https://docs.nvidia.com/datacenter/cloud-native/gpu-operator/26.3/gpu-operator-mig.html)
- [k8s-device-plugin v0.19.3](https://github.com/NVIDIA/k8s-device-plugin/tree/v0.19.3)
- [k8s-device-plugin shared access](https://github.com/NVIDIA/k8s-device-plugin/blob/v0.19.3/README.md#shared-access-to-gpus)
- [MIG User Guide](https://docs.nvidia.com/datacenter/tesla/mig-user-guide/)
- [mig-parted v0.14.2](https://github.com/NVIDIA/mig-parted/tree/v0.14.2)
- [DCGM documentation](https://docs.nvidia.com/datacenter/dcgm/latest/)
- [DCGM Exporter 4.5.3-4.8.2](https://github.com/NVIDIA/dcgm-exporter/releases/tag/4.5.3-4.8.2)

### 31.2 Kueue

- [Kueue v0.18.3 release](https://github.com/kubernetes-sigs/kueue/releases/tag/v0.18.3)
- [Kueue fixed commit afd60c3](https://github.com/kubernetes-sigs/kueue/tree/afd60c3)
- [Kueue concepts](https://kueue.sigs.k8s.io/docs/concepts/)
- [ClusterQueue](https://kueue.sigs.k8s.io/docs/concepts/cluster_queue/)
- [Cohort](https://kueue.sigs.k8s.io/docs/concepts/cohort/)
- [ResourceFlavor](https://kueue.sigs.k8s.io/docs/concepts/resource_flavor/)
- [Workload](https://kueue.sigs.k8s.io/docs/concepts/workload/)
- [Fair Sharing](https://kueue.sigs.k8s.io/docs/concepts/fair_sharing/)
- [waitForPodsReady](https://kueue.sigs.k8s.io/docs/tasks/manage/setup_wait_for_pods_ready/)
- [Kueue metrics](https://kueue.sigs.k8s.io/docs/reference/metrics/)
- [Kueue v1beta2 API](https://kueue.sigs.k8s.io/docs/reference/kueue.v1beta2/)

### 31.3 Kubernetes

- [Device Plugins](https://kubernetes.io/docs/concepts/extend-kubernetes/compute-storage-net/device-plugins/)
- [Schedule GPUs](https://kubernetes.io/docs/tasks/manage-gpus/scheduling-gpus/)
- [ResourceQuota](https://kubernetes.io/docs/concepts/policy/resource-quotas/)
- [Dynamic Resource Allocation](https://kubernetes.io/docs/concepts/scheduling-eviction/dynamic-resource-allocation/)
- [Kubernetes Scheduler](https://kubernetes.io/docs/concepts/scheduling-eviction/kube-scheduler/)
- [Scheduling Framework](https://kubernetes.io/docs/concepts/scheduling-eviction/scheduling-framework/)
- [Taints and Tolerations](https://kubernetes.io/docs/concepts/scheduling-eviction/taint-and-toleration/)
- [Pod Scheduling Readiness](https://kubernetes.io/docs/concepts/scheduling-eviction/pod-scheduling-readiness/)

### 31.4 本课程交叉阅读

- [第14课：NVIDIA节点栈](14_NVIDIA节点栈_Driver_CUDA_ContainerToolkit_containerd_CDI.md)
- [第15课：Device Plugin](15_DevicePlugin_注册_ListAndWatch_Capacity_Allocatable.md)
- [第16课：DeviceManager](16_DeviceManager_deviceID_Allocate与容器注入.md)
- [第17课：checkpoint、健康、PodResources与CDI](17_checkpoint_健康状态_PodResources与CDI恢复账本.md)
- [第18课：GPU Operator](18_GPU_Operator_组件安装升级与故障定位.md)
- [第19课：DCGM](19_DCGM_指标告警_Xid_ECC与GPU健康.md)
- [第20课：vLLM](20_vLLM_模型加载_显存_探针_吞吐延迟与SLO.md)

---

## 32. 课程收束：从“会用 Kubernetes”到“能解释 GPU 平台”

这 21 课最终不是让你背更多名词。

它训练的是同一套方法：

```text
先固定版本
  -> 找生产对象
  -> 画控制链
  -> 读关键源码
  -> 用证据验证
  -> 区分状态层
  -> 设计安全动作
  -> 留下可回滚记录
```

从 Java 平台运维转向 GPU 运维，不需要丢掉过去的能力。

你已经熟悉的：

```text
发布
探针
调度
配额
监控
SLO
值班
变更
成本
```

都仍然有用。

真正新增的是：

```text
设备身份
显存
GPU故障域
MIG几何
共享语义
驱动与容器注入
设备级可观测性
模型业务指标
GPU物理成本
```

最后请保留六个不等号：

```text
逻辑GPU资源数
  != 物理GPU数

ResourceQuota有余
  != Kueue会Admit

Kueue Admitted
  != Pod Scheduled

Pod Scheduled
  != 模型Ready

GPU utilization高
  != 业务价值高

公平入场/配额抢占
  != 性能隔离
```

当你能在真实事故中守住这些边界，并用对象、日志、源码、指标和时间线证明结论，就已经不只是“会操作 GPU 集群”。

你是在运维一套可解释的 GPU 平台。
