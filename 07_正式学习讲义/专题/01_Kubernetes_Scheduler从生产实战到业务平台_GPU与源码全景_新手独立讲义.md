# Kubernetes Scheduler：从新手值班到独立分析与平台设计

> 2026-09-08 教学重构版。本文是主教材，不再要求新手先读完整源码调用链。
>
> 学习目标不是“背完插件名”，而是：看到工作负载没起来，能找对责任方；看到调度失败，能用对象和算式证明原因；修改策略前，能解释可用性、容量和成本的取舍。
>
> 阅读不能代替操作经验。完成本文、配套实验和阶段考核，才具备向熟练运维、调度专项专家进阶的可检验基础；本文不把 Scheduler 专项能力等同于整个 Kubernetes 领域的专家能力。

## 0. 先选对学习路线

| 阶段 | 阅读与实践 | 过关标准 |
|---|---|---|
| 入门：能看懂 | 本文第 1–5 章；实验 cpu、affinity、binding | 分清未调度和未启动，手算资源账，解释硬条件与软偏好 |
| 熟练：能独立值班 | 本文第 6–11 章；实验 gates、rollout、preemption | 不靠删除 Pod 试运气，能保存现场、建立因果、给出安全修复 |
| 进阶：能做生产设计 | [生产设计与疑难排障](01_Scheduler配套/02_生产设计与疑难排障.md) | 能做发布容量、多可用区、扩缩容、调度 SLO 与变更验收 |
| 专项深入：能验证实现 | [GPU 与源码进阶](01_Scheduler配套/03_GPU与源码进阶.md) | 能区分设备供给模型，追踪状态所有权，用测试验证源码判断 |

[实验手册](01_Scheduler配套/04_实验手册.md) 提供实际存在的实验程序，不需要自行猜测 `lab-xxx.yaml` 内容。[审校记录](01_Scheduler配套/审校记录.md) 解释本次修正及原章节去向。

原始 327,726 字节全文完整保留在[历史归档](01_Scheduler配套/历史归档_重构前全文.md)，没有丢弃。**归档未经逐处修订，含已知错误，不能与新版正文视为同等有效的操作依据。**原文中的公司环境描述属于原材料背景，本次没有连接生产集群复核。下文 `activity`、节点余额、事件与发布时间线均为教学模型，不冒充现网实测。

### 0.1 版本与命令约定

日常原理以 Kubernetes 官方文档为依据；源码进阶沿用并核验原文固定提交 `301946d15e67a4a2e8a5fb8292eb836acd366d78`。开发提交中的 feature gate、函数签名和默认插件不代表你的生产版本。现场先保存 `kubectl version -o yaml`、发行版、schedulerName、可见配置和采集时间。[S1][S2]

正文命令以 Bash 为主，关键取证同时给 PowerShell。实验程序使用 Python 3.9+ 标准库，可以从 Linux、WSL 或 Windows 调用本地的 kind、kubectl 和容器运行环境。它使用自己创建的隔离集群与 kubeconfig，不借用当前生产 context。

正文只读命令不改变 Kubernetes 对象，但导出的 YAML 可能包含内部地址、环境变量与租户信息，不应直接公开。

---

## 1. 先弄清：Scheduler 到底负责哪一段

### 1.1 五个对象先用人话认识

**Node** 是集群中的工作节点。**Pod** 是要放到某一节点运行的工作单元，里面可以有多个容器。**Deployment** 管理某类 Pod 的期望副本和滚动更新。**API Server** 接收并保存对象，是组件交换状态的入口。**kubelet** 在每个节点上，负责把分配给本节点的 Pod 真正启动起来。[S3][S4]

对普通单 Pod 调度路径，kube-scheduler 的核心工作是：

```text
已经存在、尚未分配节点的 Pod
  → 找能满足硬条件的 Node
  → 在可行节点中比较软偏好
  → 把选定节点通过 Binding 写入 API
```

容器创建、镜像拉取、挂卷和应用启动，是后续链路。传统 Device Plugin GPU 路径中，具体设备分配也在节点侧；DRA 会改变设备选择参与调度的边界，后面单独学习。[S1][S3][S12][S13]

### 1.2 工作负载没起来，先分四站

| 现场事实 | 第一责任方向 | 优先证据 |
|---|---|---|
| 没有预期的新 Pod 对象 | 控制器、API 准入、配额或上层工作负载准入 | Deployment、ReplicaSet、Job、控制器事件 |
| Pod 存在，`spec.nodeName` 为空 | 调度路由、scheduling gate、scheduler 与约束 | schedulerName、gates、PodScheduled、事件 |
| `spec.nodeName` 已有值，容器未正常启动 | kubelet、镜像、CNI、CSI、设备、运行时 | 容器 waiting reason、节点侧事件与日志 |
| 容器已启动，但不 Ready 或性能差 | 探针、应用、运行时资源、网络与存储 | readiness、应用日志、CPU/内存与链路指标 |

`Pending` 是 Pod phase，不是“调度失败”的同义词。一个已经绑定节点、还在拉镜像的 Pod 也可能处于 Pending。`kubectl get pod` 的 STATUS 列还可能展示容器状态摘要，例如 ImagePullBackOff；不要把这些字符串都当成 Pod phase。[S3]

**此处先记一句：先看 nodeName，再判断该找谁。**但也不要倒过来认为 nodeName 有值就证明一定经过了默认 scheduler：手工设置 nodeName 等特殊路径会绕过它。[S5]

### 1.3 同一 Pod 不会因为节点更空就自动搬家

常规调度为一个 Pod 对象选择一次节点。已经绑定的 Pod 不会因新增节点、修改 Score 权重或节点标签变化，自动重新摆放。控制器重建出来的是新的 Pod 对象，具有新的 UID；Descheduler 等驱逐组件也是另外的责任方。[S3][S5]

**自检：**一个 Pod 有 nodeName，事件是 FailedMount。第一步是调高 scheduler 日志还是检查卷与 CSI？答案是后者；修改调度权重不能修复已发生的挂载错误。

---

## 2. 第一个值班动作：保留证据，不要先删 Pod

### 2.1 最小只读取证

Bash：

```bash
NS=platform
POD=activity-example
kubectl config current-context
kubectl version -o yaml
kubectl get pod -n "$NS" "$POD" -o wide
kubectl get pod -n "$NS" "$POD" -o yaml
kubectl describe pod -n "$NS" "$POD"
UID_VALUE=$(kubectl get pod -n "$NS" "$POD" -o jsonpath='{.metadata.uid}')
kubectl get events -n "$NS" --field-selector "involvedObject.uid=$UID_VALUE" --sort-by=.metadata.creationTimestamp
```

PowerShell：

```powershell
$Ns = 'platform'
$PodName = 'activity-example'
kubectl config current-context
kubectl version -o yaml
kubectl get pod -n $Ns $PodName -o wide
kubectl get pod -n $Ns $PodName -o yaml
kubectl describe pod -n $Ns $PodName
$PodUid = kubectl get pod -n $Ns $PodName -o jsonpath='{.metadata.uid}'
kubectl get events -n $Ns --field-selector "involvedObject.uid=$PodUid" --sort-by=.metadata.creationTimestamp
```

命令里的 Pod 名需要替换为真实名称。UID 用来区分对象实例，避免把重建前后的事件拼成同一事故；Event 会聚合、过期或缺失，因此“没有搜到”不等于“从未发生”。Condition 给当前汇总状态，也不等于完整历史。[S3]

### 2.2 把输出翻译成一句诊断，而不是截图堆砌

先填这张记录：

```text
对象：namespace/name，UID，创建时间
当前：nodeName，schedulerName，schedulingGates
状态：PodScheduled 的 status / reason / message
时间线：何时创建、何时失败、是否后来成功
输入：最终 Pod request、标签/亲和/污点容忍、PVC
节点：目标池有哪些节点，逐节点为什么通过或失败
缺口：哪些数据没有权限看到，哪些只是事后快照
```

例如：“Pod 已进入默认调度器，但唯一符合 online 标签的可用节点仅剩 800m CPU，请求为 1000m；其他节点因标签或污点被排除。因此当前没有可行节点。”这比“CPU 不够，建议扩容”多了可验证的节点集合。

### 2.3 托管集群不等于能直接看控制面 Pod

自管集群可能能够执行 `kubectl logs -n kube-system -l component=kube-scheduler`。托管 EKS 不应预设该 Pod 对租户可见；应查询已启用的 CloudWatch scheduler 日志。AWS 文档说明控制面日志导出级别为 2，不能把自管 scheduler 修改 `--v` 的方法直接套到 EKS。日志未提前启用，就不能补回未采集的历史。[S14]

ACK 等其他发行版同样先核对厂商暴露的能力，不沿用另一厂商的入口或权限假设。

**此阶段的合格线：**能够明确说出“知道什么、还不知道什么、下一条只读取证能排除哪个假设”。

---

## 3. 资源账：为什么 CPU 使用率很低，还会 Pending

### 3.1 四个数字不能混

| 名词 | 含义 | 现场来源 |
|---|---|---|
| Capacity | 节点报告的总容量 | Node status.capacity |
| Allocatable | 节点可分配给 Pod 的容量 | Node status.allocatable |
| Requested | 调度器已计入的资源请求 | 已绑定 Pod，加上调度器内部临时占账等 |
| Usage | 实际消耗的采样值 | metrics、监控、运行时 |

通用资源判断先按这个模型理解：

```text
新 Pod request ≤ Node Allocatable − 已计入的 Requested
```

比较必须逐节点、逐资源做。不能把 Node A 的 CPU 与 Node B 的内存拼给一个普通 Pod；也不能把 CPU usage 当作请求账余额。[S4]

`1 CPU = 1000m`。内存 `1Gi = 1024Mi`；内存后缀 `m` 不是 Mi，不能把 `400m` 当成 400Mi。单位错误会改变请求含义。[S4]

### 3.2 一道必须独立算对的题

```text
worker-a:
  allocatable = 4000m CPU / 16Gi
  已计 request = 3200m CPU / 13Gi
  余额 = 800m CPU / 3Gi

新 Pod:
  request = 1000m CPU / 4Gi
```

CPU：1000 > 800；内存：4 > 3。两维都不足。即使此刻 CPU usage 只有 15%，这个调度结论也不矛盾。

**为什么不能直接降低 request？**那改变的是平台的资源承诺，不是释放了真实机器。原来等待调度的问题可能变成运行时争用或内存压力。是否调整，应以启动峰值、稳定负载、GC/JIT、业务延迟目标和压测为依据；不应只为清掉 Pending。

### 3.3 调度器看的是最终 Pod，不是 Git 里的一小段模板

只写某资源 limit、没有 request，而且没有准入默认 request 时，Kubernetes 会用 limit 作为该资源 request。LimitRange、注入容器、RuntimeClass overhead 等又可能改变最后结果。因此容量计算以 API 中最终 Pod 为起点。[S4]

对于最简单的普通容器场景：

```text
业务容器：1000m / 4Gi
常驻 sidecar：200m / 256Mi
常驻阶段合计：1200m / 4352Mi
```

不是仍按“业务容器 1 核”计算。

再加入一个普通 init container，请求 2000m / 512Mi，在没有原生 sidecar、Pod-level resources 和 overhead 的简化条件下：

```text
Pod CPU request = max(常驻阶段 1200m，init 阶段 2000m) = 2000m
Pod 内存 request = max(常驻阶段 4352Mi，init 阶段 512Mi) = 4352Mi
```

每种资源分别取峰值，不是选择“最大容器”的整个资源向量。原生 sidecar，即 restartable init container，会与后续初始化阶段重叠，不能机械套用普通 init 的简式。Pod-level resources 和原地 resize 也要按目标版本核算。进阶时应对照 `resource.PodRequests`，而不是长期维护一条不完整的手抄求和命令。[S4][S15][S16]

### 3.4 request、limit 与 QoS 分别回答什么

request 主要参与调度资源账；limit 由节点运行时与内核路径实施，不同资源实施方式不同；usage 是测量结果。CPU limit 可能带来 throttling；内存上限与 OOM 是运行时问题。JVM 堆上限也不是整个容器的内存上限。[S4]

QoS 与 PriorityClass 是不同维度：不能说 Guaranteed 自动比 Burstable 先调度。调度队列的优先级另看 priority 和实际 QueueSort。[S8]

### 3.5 `describe node` 好用，但不是调度器内存快照

```bash
kubectl describe node worker-a
kubectl get pods -A --field-selector spec.nodeName=worker-a -o wide
```

这些命令有助于核对 API 可见的分配。它们不能直接看到刚 Assume、尚未持久化绑定的 Pod，也不是与所有其他查询同时完成的原子快照。小的暂时差异应沿时间线调查，不要凭差额就断言“调度器丢账”。[S1][S17]

**实践：**运行实验 `cpu`，确认过大的请求先到达 scheduler、随后被拒绝，再创建小请求对照。若 Pod 创建阶段就被 Quota 拒绝，你验证到的是准入层，不是资源 Filter。

---

## 4. 节点选择不是玄学：先做硬条件交集

### 4.1 一张表对应一个排查方向

| 条件 | 它约束什么 | 典型取证 |
|---|---|---|
| nodeSelector / required nodeAffinity | 必须是哪类节点 | 最终 Pod 规则、Node labels、profile addedAffinity |
| taints / tolerations | 是否被节点拒绝 | key、value、operator、effect，不能漏第二条 taint |
| resources.requests | 单节点承诺是否够 | CPU、内存、临时存储、Pod 数、扩展资源 |
| podAffinity / podAntiAffinity | 和哪些 Pod 同域或不同域 | selector、namespace 范围、topologyKey |
| topologySpreadConstraints | 匹配 Pod 在域间的分布 | eligible domains、各域计数、maxSkew |
| PVC / PV / CSI | 卷与节点拓扑是否相容 | SC、PV nodeAffinity、attach 限制、延迟绑定 |
| hostPort | 节点上的端口是否冲突 | hostIP、协议、端口与其他 Pod |

这些条件共同生效。硬条件不通过，Score 再高也救不了。实际 Filter 执行可能短路：本次核验的固定源码在某节点遇到一个失败插件便返回，并不保证遍历所有插件后列全错误。因此修完 CPU 后又出现卷错误，并不说明“修复制造了新故障”。一个插件自身也可能产生多个原因；Event 不是完整节点约束矩阵。[S1][S5][S17]

### 4.2 nodeAffinity 的 AND / OR

```text
nodeSelector 的多个 key                         → AND
同一 nodeSelectorTerm 的多个 matchExpressions  → AND
不同 nodeSelectorTerms                         → OR
nodeSelector 与 required nodeAffinity          → 都要满足
```

例子：`pool=online` 且 `zone in [a,b]`，需要放在同一个 term 内。写成两个 term，含义就可能变成“online 池或者 zone a/b”，意外扩大可行范围。[S5]

required 是硬门槛；preferred 是软偏好。名字中的 `IgnoredDuringExecution` 表示运行后标签变化不会仅凭这条调度亲和规则自动驱逐已有 Pod。[S5]

### 4.3 污点像拒绝条件，容忍不是目的地

假设 GPU 池有：

```yaml
key: dedicated
value: gpu
effect: NoSchedule
```

Pod 带相匹配的 toleration，只表示不会因为这条 taint 被拒绝，不表示必须进入该池。要表达“必须进入专池”，还要使用 required affinity / nodeSelector 或其他明确约束。GPU request 本身也只限定提供相应资源的节点，不自动限定你想要的那一种显卡。[S5][S6][S12]

NoSchedule 不因该污点驱逐已有 Pod；NoExecute 还涉及已有 Pod 的驱逐与 tolerationSeconds。cordon 标记不可调度，不等于迁走已有 Pod；drain 是迁出工作流，通常涉及 Eviction、PDB 和节点维护，不是 scheduler 的一个打分开关。[S6][S9]

**实践：**实验 `affinity` 先证明“有 required label，但不容忍 taint，仍被拒绝”，再仅改变 toleration，观察同一目标节点变为可行。不要用一次随机落点证明 toleration 的吸引力。

---

## 5. 贯穿案例：一次 activity 发布为什么卡住，又为什么恢复

### 5.1 固定输入，后面不偷偷更换数字

这是教学快照：新 Pod 要 `1000m CPU / 4Gi`，必须进入 `pool=online`，没有 GPU 专池 toleration。

| Node | pool | taint | CPU 余额 | 内存余额 | 当前判断 |
|---|---|---|---:|---:|---|
| worker-a | online | 无相关拒绝 | 800m | 3Gi | 资源不足 |
| worker-b | batch | 无相关拒绝 | 3000m | 8Gi | 节点标签不匹配 |
| worker-c | online | dedicated=gpu:NoSchedule | 3000m | 8Gi | 未容忍污点 |

没有节点同时满足全部条件。这里的“判断”是我们的完整教学分析，不声称某条 Event 一定同时打印这张表的所有行和所有错误。

### 5.2 哪个变化真正有用

worker-a 上一个占用 `500m / 2Gi` 的旧任务结束，并且其资源占用已从调度器账本释放：

```text
CPU 余额：800m + 500m = 1300m
内存余额：3Gi + 2Gi = 5Gi
新 Pod：1000m / 4Gi
```

worker-a 现在可行；b 的标签没变；c 的污点关系没变。若当前只有 a 可行，普通路径无需多候选 Score，直接选它。注意是实际占用释放并传播后才有这个结果，不能把“已发删除请求”当作资源立即归还。[S17]

反过来，往 batch 池扩十台节点也不一定有用，因为新 Pod 必须进入 online。解除 GPU 专池污点可能让它有地方放，却破坏隔离策略。重启 scheduler 不会创造 CPU。

### 5.3 有两台都能放，才需要比较偏好

只演示 CPU 的 LeastAllocated 教学分数；此表不是完整默认 scheduler 总分。[S18]

| Node | CPU 容量 | 放入前 request | 本 Pod request | 放入后 request | 剩余比例 |
|---|---:|---:|---:|---:|---:|
| worker-d | 8000m | 2000m | 1000m | 3000m | 62.5% |
| worker-e | 8000m | 4500m | 1000m | 5500m | 31.25% |

```text
d：100 × (8000 − 2000 − 1000) / 8000 = 62.5
e：100 × (8000 − 4500 − 1000) / 8000 = 31.25
```

实现中的整数运算会截断；上述简单 CPU 分别得到 62、31。真实评分还可能有 memory、NodeAffinity、拓扑、镜像本地性和不同插件权重。原文第 10.2 节把 1000m 新请求按 1400m 加入，本版已修正。

### 5.4 “更喜欢 d”不等于“一定去 d”

教学示意：

```text
插件 A：d=62，e=31，权重=1
插件 B：d=0，e=100，权重=2
总分：d=62，e=231
```

最终 e 更高。这里 B 的分数是已完成该插件所需归一化后的示意值，不是说业务填写 preferred.weight=100 就一定直接贡献 100 分。

归一化不是每个插件必做的步骤；只有提供相应扩展的插件才运行它。不同资源在一个插件内的权重，与 Framework 对整个插件的权重，也是两层概念。[S1][S2][S18]

**自检：**一个节点有未容忍的硬污点，能否通过给它增加 10000 分获救？不能，它不在评分候选集中。

---

## 6. 多副本可靠性：先理解拓扑，再选硬规则还是软规则

### 6.1 hostname 分散不等于跨可用区

三只 Pod 分别在三台 Node 上，但三台 Node 同属一个可用区，仍可能在一次 AZ 故障中一起受影响。`kubernetes.io/hostname` 与 `topology.kubernetes.io/zone` 是不同故障域。[S7]

下面是完整的教学 Deployment。它要求 zone 层满足硬分散，同时在 hostname 层尽量分散。**它是讨论可靠性的输入，不是无条件适合生产的默认模板。**

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: spread-demo
  namespace: scheduler-study
spec:
  replicas: 3
  selector:
    matchLabels:
      app: spread-demo
  template:
    metadata:
      labels:
        app: spread-demo
    spec:
      topologySpreadConstraints:
      - maxSkew: 1
        topologyKey: topology.kubernetes.io/zone
        whenUnsatisfiable: DoNotSchedule
        labelSelector:
          matchLabels:
            app: spread-demo
      - maxSkew: 1
        topologyKey: kubernetes.io/hostname
        whenUnsatisfiable: ScheduleAnyway
        labelSelector:
          matchLabels:
            app: spread-demo
      containers:
      - name: web
        image: nginx:1.27.5
        resources:
          requests:
            cpu: 250m
            memory: 256Mi
          limits:
            cpu: '1'
            memory: 512Mi
```

先在隔离环境创建 namespace 并确认节点都有相应标签。镜像可达性、准入策略与版本应先验证。不存在 zone 标签的环境不能把这份清单当跨 AZ 实验。kind 的假 zone 标签只能验证调度规则，不能模拟真实可用区故障。

### 6.2 skew 怎样算

对 `DoNotSchedule`，普通自匹配 Pod 可先理解成：

```text
目标域现有匹配 Pod 数 + 本 Pod 贡献 − global minimum ≤ maxSkew
```

global minimum 来自 eligible domains；若域数小于指定 minDomains，可能按 0 处理。它不是任何场景都可替换成“最终全局最大数减最小数”。selector、namespace、nodeAffinityPolicy、nodeTaintsPolicy 和域标签会改变统计范围。[S7]

三个 eligible hostname 域现有 `1/1/0`，maxSkew=1。新 Pod 放第一域：`1+1−0=2`，失败；放第三域：`0+1−0=1`，通过。若第三域只剩 `1.5 CPU/3Gi`，而 Pod 要 `2 CPU/4Gi`，拓扑允许的位置又被资源拒绝，最终仍无解。

### 6.3 硬分散不是免费可靠性

硬分散能够约束未来放置，也可能在某域容量不足时阻止扩容或发布。软分散允许退化，但不会保证副本一定均匀。应先决定：域故障时宁愿保持严格分散而部分 Pending，还是允许集中到存活域维持服务？这要和业务最低存活副本、存活域容量及流量摘除一起评估。

**必须会回答：**节点列表缩小、节点仅 NotReady、节点带 taint、节点被删除，这几种变化对 eligible domains 是否相同？不能凭直觉说相同，应核对实际策略和对象状态。[S7]

---

## 7. 发布容量：稳定态放得下，不代表更新时放得下

### 7.1 单副本发布为什么需要第二份位置

教学条件：replicas=1，maxSurge=1，maxUnavailable=0，单 Pod 为 1 CPU/4Gi。开始滚动更新时，控制器可创建一个新副本，同时保留旧副本等待新副本可用。因此这个阶段需要额外一个符合全部约束的 Pod 位置。[S10]

这不是“所有时刻实际总 Pod 数绝不会超过 2”。正在终止的 Pod、连续模板更新等会造成额外重叠，实际资源消耗可能超过 replicas+maxSurge。生产容量必须观察 terminating 占用和控制器时间线。[S10]

### 7.2 百分比也要算对

maxSurge 百分比向上取整，maxUnavailable 百分比向下取整。例如 replicas=3、两者均 25%，surge 为 1，unavailable 为 0。不要把 0.75 个 Pod 四舍五入成 1 个 unavailable。[S10]

六个单副本服务都发生 PodTemplate 变化、每个允许多一只、每只 1 CPU/4Gi，那么首波可新增 6 CPU/24Gi 请求。若目标池只剩四个可放置 shape，最多先接纳四只，至少两只等待。这个结论要求真的有六次模板更新，而且四个 shape 是逐节点及硬约束核算的结果，不是把散落余量简单相加。

### 7.3 三种修复的代价不同

预留可行容量，保留可用性但增加成本；减小发布批次，延长发布过程但降低峰值；允许先下旧副本，减少瞬时容量但可能引入不可用窗口。单副本尤其不能把 maxUnavailable=1 当无风险开关。

PDB 不是 Deployment 滚动更新副本策略的替代品。维护驱逐与应用滚动更新要分别设置预算、探针、终止行为和验收。[S9][S10]

**实践：**实验 `rollout` 在隔离节点用请求账制造“旧 Pod Ready、新 Pod Pending”，再受控改变滚动策略。学习目的包含说明这个解法为什么不能直接用于单副本生产服务。

---

## 8. 别只看 CPU：卷、端口和节点可用性也能堵住调度

### 8.1 存储的两个阶段

Immediate 的卷可能先绑定，之后 Pod 受 PV 的节点拓扑限制；WaitForFirstConsumer 让卷与首个消费者的选点协同。PVC Pending 在 WFFC 场景可能是等待配合，不应直接判为 CSI 故障。[S11]

```bash
kubectl get pod,pvc -n "$NS" -o wide
kubectl get storageclass
kubectl get pv
kubectl describe pvc -n "$NS" PVC_NAME
```

Pod 要求 zone-a、可用 PV 只能在 zone-b，就需要同时解决存储和计算约束；扩一台 zone-a 的纯 CPU 节点不一定有用。Pod 已有 nodeName 之后的 FailedMount，则进一步检查节点侧挂载、CSI、网络与后端存储。

不要用手工 nodeName 强行绕过 WFFC 调度配合，也不要伪造 selected-node 注解来“帮助”控制器。[S11]

### 8.2 另外三类容量

hostPort 冲突不是 Service 的 port 冲突；Pod slots 不足不是 CPU 不足；临时存储 request 与节点磁盘压力也不是同一个信号。CPU、内存宽裕不能证明其他维度可用。[S4][S17]

**排查方法：**从失败方向找到对应 API 字段，再去同一候选节点上核对，而不是执行一套所有组件的全量命令。

---

## 9. 优先级与抢占：排队靠前，不等于保证成功

### 9.1 一条正常顺序

高优先级 Pod 先获得调度尝试机会；正常约束判断没有可行节点时，抢占机制可能模拟移除较低优先级 Pod，寻找未来可行的方案。实际删除、优雅终止、资源释放和后续重新调度不是同一个动作。[S8]

```text
本轮无可行节点
  → 模拟可能的受害者方案
  → 选择方案并推进删除/提名
  → 等事实变化
  → 下一轮重新验证
  → 才可能绑定
```

`status.nominatedNodeName` 是潜在落点提示，不是 API 已绑定，不是不会改变的设备或节点锁。具体删除与提名的可观察先后依版本及异步实现变化，不把教学箭头当严格事件时间承诺。[S8][S17]

### 9.2 抢占能改什么，不能改什么

删除其他 Pod 可能释放 CPU、内存、hostPort 或传统扩展资源请求；它不会自动改变 required 标签、污点关系、卷所在可用区，也不能把最大 8 卡的单节点变成 10 卡。[S8]

PDB 在调度抢占中是尽力遵守，不是绝对免死；drain 的 Eviction 路径对 PDB 的处理又不同。节点故障也不受 PDB 绝对保护。[S8][S9]

`preemptionPolicy: Never` 让高优先级 Pod 不主动抢占，但它仍有较高排队优先级，也可能被更高优先级 Pod 抢占。不要通过不断提高 priority 来替代容量规划。[S8]

### 9.3 实验必须先占位，再制造竞争

正确实验顺序是：先创建低优先级 Pod，等待它绑定并 Ready，确认请求账已占用；然后创建高优先级且不能共存的 Pod；保留两者 UID 与时间线；最后与 Never 做对照。把高低 Pod 一起 apply，可能高优先级先调度成功，根本没有发生你打算观察的抢占。

设置 terminationGracePeriodSeconds 只规定退出预算，不保证进程一定等满该时长。本套实验额外使用受控 preStop，才能更容易观察退出窗口。

---

## 10. 到这里再看内部流程：每个复杂机制都回答一个实际问题

### 10.1 为什么用 informer、cache 和 snapshot

调度器需要反复读取大量 Pod/Node 状态。informer 监听 API 对象变化，cache 整理可用视图；一次调度使用 snapshot 进行判断。这样降低逐次远程读取成本，也意味着它与 API、监控之间存在传播时间差。snapshot 不是整个集群绝对实时、跨组件的强一致照片。[S1][S17]

### 10.2 为什么先 Assume，再异步 Bind

若等每次 API Binding 完成后才处理下一只 Pod，慢 API 调用会拖低吞吐。普通 Pod 路径中，scheduling cycle 串行，binding cycle 可并发。注意不是“两个普通 scheduling cycle 同时抢同一本账”。[S1]

```text
A 选中节点
  → Assume：先在本调度器 cache 计入 A 的请求
  → Reserve：插件维护自己的预留状态
  → Permit：允许、拒绝或等待协调条件
  → 异步 binding cycle
与此同时：下一只 B 可以开始选点，但应看到 A 已占的请求账
```

假设只剩 1000m，A 要 1000m。A 的 API nodeName 尚未出现，B 也来要 1000m。Assume 的作用就是避免 B 仍把同一余额当空闲。它是当前进程内的乐观占账，不是全局分布式锁，也不是具体 GPU UUID 锁。[S17]

### 10.3 三种清理分别管谁

| 动作 | 所有者 | 清理对象 |
|---|---|---|
| Unreserve | Framework 插件 | 插件私有的临时预留 |
| ForgetPod | scheduler cache | assumed Pod 的通用资源账 |
| Done | 调度队列 | in-flight 对象/事件跟踪 |

Reserve 或 Bind 后续失败，需要走相应补偿，不能让资源长期幽灵占用；释放临时占账后，其他可能受益的等待 Pod 还需要重新获得调度机会。[S1][S17]

### 10.4 队列为什么不一直重试

active 表示准备尝试；backoff 表示退避；unschedulable 表示当前条件没有满足；gate 表示尚未达到可尝试条件。具体内部结构和指标标签以版本为准。它们都不直接等于 Pod phase。[S1][S17]

CPU 不够时每毫秒重试并不会多出 CPU，只会浪费控制面。相关 Node/Pod 变化可能让失败条件改变，QueueingHint 用于判断是否值得重算，而不是保证下次一定成功。调度过程中的事件跟踪还要避免“释放发生在计算期间，失败落队后却错过唤醒”的竞态。[S17]

### 10.5 先理解返回语义，不急着背全部状态码

Unschedulable 表示条件当前不满足；Error 表示执行或依赖遇到异常，两者的运维处理不同。Permit 的 Wait 是绑定前协调等待，不等于 Pod Pending phase。开发版本中的其他状态码留到固定源码与测试一起读。[S1][S17]

**过关题：**A 的 Bind 失败后，为什么仅让 A 重试还不够？因为 A 的临时占账可能让 B 也失败；释放与唤醒需要共同考虑。

---

## 11. 把知识组合成一条可执行的值班路径

```text
业务异常
  → 预期 Pod 是否创建？没有：查控制器与准入
  → 有 nodeName？有：查节点兑现和应用
  → 无 nodeName：核对 schedulerName 和 gates
  → 有调度失败：读取最终 Pod 与候选节点条件
  → 按失败方向形成逐节点矩阵，不把 Event 当完整矩阵
  → 对照请求、拓扑、卷、污点等求交集
  → 没有普通约束解释：查 leader、队列、插件、API 与缓存
  → 选择最小可解释变更，保留回滚与验收
```

修复后的验收至少有三层：新的 Pod 成功绑定；节点侧能够启动并 Ready；业务恢复且没有破坏节点池隔离、跨域目标或运行时稳定性。只看到 Pending 消失，不足以宣布生产问题解决。

### 11.1 必须能独立完成的三份作业

**作业 A：新 Pod 不存在。**给出一个配额拒绝导致 ReplicaSet FailedCreate 的例子，证明它没有进入 scheduler；说明为什么新增 Node 不一定有用。

**作业 B：资源与拓扑联合失败。**给出三个节点的 labels、taints、allocatable、requests、同伴 Pod 计数；不运行变更，先算出每个节点为何失败，再只改变一个条件预测新结果。

**作业 C：发布与终止重叠。**保存新旧 ReplicaSet、Pod UID、nodeName、deletionTimestamp 和 requests；分别计算首波 surge 与终止期间真实占用，说明哪种修复会影响可用性。

评分不看术语多少，看输入是否齐全、推理能否复算、证据是否支持结论，以及修复是否明确代价。

### 11.2 下一步不是继续背名词

进入[生产设计与疑难排障](01_Scheduler配套/02_生产设计与疑难排障.md)，学习调度慢与不可调度的区分、可用区容量、HPA/节点扩容边界、SLO 成功者偏差、多调度器风险和变更验证。之后再进入 GPU 与源码，而不是倒过来。

---

## 本文依据

以下是本版学习入口，访问核验日为 2026-09-08。官方滚动文档会更新，生产应用应切换到目标版本；源码链接固定到原文提交，不依赖漂移行号。

- [S1 Scheduling Framework](https://kubernetes.io/docs/concepts/scheduling-eviction/scheduling-framework/)
- [S2 Scheduler Configuration](https://kubernetes.io/docs/reference/scheduling/config/)
- [S3 Pod Lifecycle](https://kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle/)
- [S4 Resource Management](https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/)
- [S5 Assigning Pods to Nodes](https://kubernetes.io/docs/concepts/scheduling-eviction/assign-pod-node/)
- [S6 Taints and Tolerations](https://kubernetes.io/docs/concepts/scheduling-eviction/taint-and-toleration/)
- [S7 Pod Topology Spread](https://kubernetes.io/docs/concepts/scheduling-eviction/topology-spread-constraints/)
- [S8 Pod Priority and Preemption](https://kubernetes.io/docs/concepts/scheduling-eviction/pod-priority-preemption/)
- [S9 Disruptions](https://kubernetes.io/docs/concepts/workloads/pods/disruptions/)
- [S10 Deployments](https://kubernetes.io/docs/concepts/workloads/controllers/deployment/)
- [S11 Storage Classes](https://kubernetes.io/docs/concepts/storage/storage-classes/)
- [S12 Device Plugins](https://kubernetes.io/docs/concepts/extend-kubernetes/compute-storage-net/device-plugins/)
- [S13 Dynamic Resource Allocation](https://kubernetes.io/docs/concepts/scheduling-eviction/dynamic-resource-allocation/)
- [S14 EKS Control Plane Logs](https://docs.aws.amazon.com/eks/latest/userguide/control-plane-logs.html)
- [S15 Init Containers](https://kubernetes.io/docs/concepts/workloads/pods/init-containers/)
- [S16 Sidecar Containers](https://kubernetes.io/docs/concepts/workloads/pods/sidecar-containers/)
- [S17 固定源码 schedule_one.go](https://github.com/kubernetes/kubernetes/blob/301946d15e67a4a2e8a5fb8292eb836acd366d78/pkg/scheduler/schedule_one.go)；[Filter 短路实现](https://github.com/kubernetes/kubernetes/blob/301946d15e67a4a2e8a5fb8292eb836acd366d78/pkg/scheduler/framework/runtime/framework.go)
- [S18 Resource Bin Packing](https://kubernetes.io/docs/concepts/scheduling-eviction/resource-bin-packing/)
