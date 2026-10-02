# Kubernetes Scheduler：调度原理、资源计算与源码解析

一次 Java 应用发布可能停在不同环节：控制器没有创建出新 Pod，Pod 已创建但没有可行节点，或者 Pod 已分配节点却仍在准备容器。Scheduler 负责其中的节点选择和绑定；理解它的输入、约束与状态变化，才能解释发布为什么等待，以及哪些变化能够让发布继续。

本文从 Java/Spring Boot 服务 `activity` 的发布场景展开，依次说明资源请求、硬约束、评分、拓扑分布、发布容量和失败重试，再连接到生产容量设计、GPU 调度与源码实现。文中的名称与数字是用于解释机制的简化示例，所有关键推导都在正文中给出。Go 语法在对应源码旁解释。

## 0. 内容导览与版本说明

| 内容 | 位置 | 主要问题 |
|---|---|---|
| 调度职责与资源计算 | 第 1–5 章 | Pod 停在哪个环节，单台节点为什么放不下 |
| 拓扑、发布与失败处理 | 第 6–11 章 | 副本怎样分布，更新需要多少余量，失败后怎样继续 |
| 生产容量与可观测性 | [第 12 章](#production) | 故障后能承载多少流量，怎样衡量调度等待 |
| GPU 与批任务 | [第 13 章](#gpu) | 资源数量、设备分配、共享和队列之间是什么关系 |
| 源码实现 | [第 14 章](#source) | 请求怎样参与过滤，失败怎样重试，选点怎样持久化 |
| 术语速查 | [第 15 章](#terms) | 对象、字段、组件和 Go 写法的含义 |

第 1–11 章建立调度模型，第 12–13 章说明它在生产设计和 GPU 场景中的应用。第 14 章把前文的 CPU 算例接到具体函数；暂不深入 Go 时，前文的公式和状态变化已经给出相应结论。

### 0.1 源码与示例约定

本文的主要源码基线是 Kubernetes 提交 `301946d15e67a4a2e8a5fb8292eb836acd366d78`。固定提交对应一份确定的代码快照；生产分析需要使用目标集群的版本，因为函数拆分、功能开关和默认值可能变化。DRA 字段示例使用 Kubernetes v1.34 API，第 14.1.7 节列出与 v1.34.0 发布源码有关的实现差异。[S1][S2]

源码片段旁标明文件、函数和摘录范围。中文注释为讲解新增；摘录依赖上游类型，不能作为独立程序编译。源码与许可证归属见[第三方说明](../../THIRD_PARTY_NOTICES.md)。

正文保留用于理解对象字段的只读查询。`kubeconfig` 保存集群连接与身份信息，`context` 选择其中的集群、身份和默认 namespace；第 2 章的查询先指定目标 context，后文沿用同一选择。

---

## 1. Scheduler 的职责与组件边界

### 1.1 用一次 Java 发布串起组件

`activity` 发布卡住时，先分清三件事：**新 Pod 有没有创建？有没有选到节点？节点有没有把应用启动好？**

你修改镜像后，Deployment 管这次版本替换，ReplicaSet 管这一版的副本数量、创建新 Pod。它们都是控制器：反复检查目标和现状，再推进需要的创建或删除。

scheduler 为已经创建的 Pod 选节点。目标节点的 kubelet 再准备卷、拉镜像和启动容器。kubelet 就是每台节点上负责执行这些工作、报告结果的程序。[S3][S4]

容器里的 JVM 是运行 Java 程序的环境。它启动后，应用还可能加载缓存、做预热；readiness 是就绪检查，用来判断能否接请求。Pod 的必要就绪条件满足后才报告 Ready，接口和性能仍要单独验收。

这些组件通过 API Server 读写对象。API Server 是集群接收查询和修改请求的统一入口，对象就是其中保存的 Pod、Node 等记录。因此“发布没完成”还不能直接推出“scheduler 有问题”。

选节点时，硬条件是“任何一条不满足就不能选”；软偏好是“在已经能选的节点中更喜欢谁”。Binding 是把选定节点正式写进 Pod 的 `spec.nodeName`，后面的组件才知道由哪台节点接手。从上往下看它负责的这一段：

```text
已经存在、尚未分配节点的 Pod
  → 找能满足硬条件的 Node
  → 在可行节点中比较软偏好
  → 把选定节点通过 Binding 写入 API
```

`spec` 表达对象的要求，`status` 表达组件报告的结果。`metadata.uid` 标识这一次创建的对象；删除后重建，即使名称相同，UID 也会变化。比较重试前后的状态时，UID 能区分“原对象继续尝试”和“控制器创建了替代对象”。

Deployment 模板是创建 Pod 的输入。保存前的检查和补充叫准入，它可能改变最终 Pod；request 就是其中的资源申请量。具体怎样补默认值，到第 3.3 节再算。[S4]

后面有两种标签要分清：Node 标签用来找节点池、可用区；Pod 标签用来找同一应用的副本。`selector` 就是按标签筛选的规则。Namespace 本身不表示独占一批节点。设备相关的新增步骤留到第 13 章，在这条主线清楚以后再接着学。

### 1.2 工作负载没起来，先分四站

工作负载就是要运行的应用或任务。表里的 CNI 是给 Pod 接通网络的插件机制，CSI 是对接存储的插件机制，容器运行时负责真正创建和运行容器。scheduling gate 是 Pod 上的“暂缓调度”条件，条件未移除时，先不进行普通选点。

这里的 Event（事件）是组件写下的进展或失败报告，也就是 `kubectl get events` 查询的对象。第 10 章说的“对象变化通知”是通知组件重新看数据，作用不同。

| 现场事实 | 第一责任方向 | 优先证据 |
|---|---|---|
| 没有预期的新 Pod 对象 | 控制器、API 准入、配额或上层工作负载准入 | Deployment、ReplicaSet、Job、控制器事件 |
| Pod 存在，`spec.nodeName` 为空 | 调度路由、scheduling gate、scheduler 与约束 | schedulerName、gates、PodScheduled、事件 |
| `spec.nodeName` 已有值，容器未正常启动 | kubelet、镜像、CNI、CSI、设备、运行时 | 容器 waiting reason、节点侧事件与日志 |
| 容器已启动，但不 Ready 或性能差 | 探针、应用、运行时资源、网络与存储 | readiness、应用日志、CPU/内存与链路指标 |

phase 是 Pod 的粗略阶段；`Pending` 表示启动过程还没完成，不是“调度失败”的同义词。一个已经绑定节点、还在拉镜像的 Pod 也可能处于 Pending。`kubectl get pod` 的 STATUS 列还可能展示容器状态摘要：ErrImagePull 是拉镜像失败，ImagePullBackOff 是失败后等一会儿再拉。不要把这些字符串都当成 Pod phase。[S3]

排查时，`spec.nodeName` 是区分“节点尚未确定”和“已有目标节点”的重要字段。它有值时，镜像拉取、卷挂载与容器启动通常已经属于节点侧处理范围；但这不证明 Pod 一定经过默认 scheduler，手工指定 `nodeName` 也会绕过调度器。[S5]

例如，Pod 已有 `nodeName`，随后因镜像地址不存在而出现 `ErrImagePull` 或 `ImagePullBackOff`，说明目标节点已经确定，失败发生在拉镜像阶段。Pod 刚完成绑定、尚未启动容器时，phase 也可能仍为 Pending。因此，需要结合 `nodeName`、Condition 和具体原因判断处理进度。

### 1.3 同一 Pod 不会因为节点更空就自动搬家

常规调度为一个 Pod 对象选择一次节点。Score 就是对可行节点打分，权重决定某项分数在总分中占多大影响。已经绑定的 Pod 不会因新增节点、修改 Score 权重或节点标签变化，自动重新摆放。控制器重建出来的是新的 Pod 对象，具有新的 UID。Descheduler 是按策略挑选已有 Pod 并发起驱逐的工具；驱逐是让旧 Pod 退出，后续是否重建由其控制器决定。[S3][S5]

同样，Pod 已有 `nodeName` 且出现 `FailedMount` 时，应根据事件消息检查卷、挂载配置及相关 CSI 组件。修改 Score 权重不会修复已发生的挂载错误。

<a id="quota"></a>

### 1.4 配额用满了：没有第二只 Pod，不是第二只 Pending

设 `activity` 目标为两副本，每只 CPU request 为 100m，namespace 的 CPU 请求配额为 150m。数字只是为了方便看清差额，不是 Java 生产规格建议。

```text
第一只：已用 0m + 新申请 100m ≤ 配额 150m，创建成功
第二只：已用 100m + 新申请 100m > 配额 150m，创建被拒绝
```

这里不是“第二只已经创建，但没有地方放”。第二次创建没有成功保存出 Pod 对象，scheduler 没有这只 Pod 可处理。节点即使还有几核 CPU，也不会让 namespace 的配额自动增加。

FailedCreate 表示这次创建失败；消息里可能已经写了一个准备创建的 Pod 名称，甚至出现 `pods "名字" is forbidden`。这仍不代表对象已经保存成功。Forbidden 是请求被拒绝；RBAC 是按身份和角色规定“谁能做什么”的权限规则。本例来自配额拒绝，要读 exceeded quota（超过配额）和请求、已用、上限数字，不能只见 Forbidden 就判成权限问题。

为什么要在创建时挡住？节点容量回答“物理供给还够不够”，配额回答“这个 namespace 允许申请多少”。一家公司可能有很多空闲节点，但不能让一个租户把约定额度全部越过。两种检查各管一件事。

这个场景的关键证据是 ResourceQuota 的上限和已用量，以及 ReplicaSet 上包含 `exceeded quota` 的 FailedCreate Event。Event 的 `involvedObject.uid` 指向 ReplicaSet，说明是它创建副本的请求被拒绝；第二个 Pod 尚不存在，无法通过查询该 Pod 获得创建失败原因。

如果配额提高到 200m，两个 100m 请求就能同时满足配额限制，控制器后续重试可以继续创建第二个 Pod。新 Pod 仍需通过调度并完成启动；提高配额本身不保证 Ready，也不需要重建已有副本。

固定提交中的 ResourceQuota `CheckRequest` 用已有用量加本次申请量，与 `status.hard` 比较；超过就返回 Forbidden。创建调用返回错误后，`RealPodControl.createPods` 在所属 ReplicaSet 上记录 FailedCreate；ReplicaSet 后续还会重试。[N1][N2]

增加节点只改变供给，没有改变 150m 的 namespace 配额，因此仍不能解除这次创建拒绝。FailedCreate 也可能来自其他准入问题，需要读取具体 message，不能把事件原因名直接当成配额不足的结论。

---

## 2. 从对象与事件理解调度状态

### 2.1 最小只读取证

Bash：

```bash
CTX='替换为已核对的目标context'
NS=platform
POD=activity-example
kubectl config get-contexts "$CTX"
kubectl --context "$CTX" version -o yaml
kubectl --context "$CTX" get pod -n "$NS" "$POD" -o wide
kubectl --context "$CTX" get pod -n "$NS" "$POD" -o yaml
kubectl --context "$CTX" describe pod -n "$NS" "$POD"
UID_VALUE=$(kubectl --context "$CTX" get pod -n "$NS" "$POD" -o jsonpath='{.metadata.uid}')
kubectl --context "$CTX" get events -n "$NS" --field-selector "involvedObject.uid=$UID_VALUE" --sort-by=.metadata.creationTimestamp
```

PowerShell：

```powershell
$KubeContext = '替换为已核对的目标context'
$Ns = 'platform'
$PodName = 'activity-example'
kubectl config get-contexts $KubeContext
kubectl --context $KubeContext version -o yaml
kubectl --context $KubeContext get pod -n $Ns $PodName -o wide
kubectl --context $KubeContext get pod -n $Ns $PodName -o yaml
kubectl --context $KubeContext describe pod -n $Ns $PodName
$PodUid = kubectl --context $KubeContext get pod -n $Ns $PodName -o jsonpath='{.metadata.uid}'
kubectl --context $KubeContext get events -n $Ns --field-selector "involvedObject.uid=$PodUid" --sort-by=.metadata.creationTimestamp
```

先核对 context 对应的集群与身份，再执行查询；后文 Bash 命令沿用同一个 `CTX`。Pod 名需要替换为真实名称。`Forbidden` 表示当前身份看不到这份证据，不表示资源不存在；超时也不能直接证明 scheduler 停止工作。

Pod YAML 回答“实际输入是什么”，describe 与事件回答“组件报告了什么”，Node 和同节点 Pod 回答“候选节点能否容纳”。单独看到 FailedScheduling 只确定一次尝试失败，还需结合 UID、时间、最终约束与节点数据解释原因。

UID 用来区分对象实例，避免把重建前后的事件拼成同一事故；Event 会聚合、过期或缺失，因此“没有搜到”不等于“从未发生”。Condition 是一条状态检查记录，例如 `PodScheduled=False` 表示尚未完成调度；status 是判断结果，reason 是简短原因，message 是详细说明。它给当前汇总状态，也不等于完整历史。[S3]

### 2.2 对象字段怎样组成诊断依据

调度诊断需要把对象身份、实际输入和节点条件对应起来：

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

---

## 3. CPU 明明很闲，为什么还会 Pending

### 3.1 四个数字不能混

scheduler 按资源请求计算节点能否容纳新 Pod。`request` 会进入调度资源账，即使容器当前没有消耗 CPU，这笔请求也不会自动归零；实时 usage 用于观察运行状态。

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

### 3.2 CPU 与内存必须分别满足

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

GC 是 JVM 清理不再使用的对象、回收内存的工作；JIT 是把常用 Java 代码转成更快机器代码的工作。启动和负载变化时，它们可能带来额外 CPU、内存需求，所以只看平稳时的平均用量容易漏掉高峰。

#### 3.2.1 余额 200m 时，201m 与 200m 的区别

假设一个节点的 CPU Allocatable 为 `5000m`，系统 Pod 已请求 `100m`，已有业务 Pod 请求 `4700m`，其他硬条件均满足：

```text
CPU 余额 = 5000 − 100 − 4700 = 200m
新 Pod 请求 201m：201 > 200，CPU 检查失败
新 Pod 请求 200m：200 = 200，CPU 检查通过
```

资源检查使用严格的大于判断：超过余额才记录不足。1m 的差额虽小，仍代表请求超过了当前可分配余额；较低的实时 CPU 使用率不会改变这个结果。

如果原先请求 4700m 的 Pod 退出，其删除变化被 scheduler 正常处理后，余额变为 `4900m`。原来申请 201m 的 Pod 无需改请求或重建，下一次尝试时就可能通过 CPU 检查。第 14 章进一步说明资源账更新与重新入队之间的关系。

通过 CPU 检查只解决资源条件中的一项；后面仍有其他 Filter、绑定、容器启动与应用就绪过程。

<a id="cpu-source"></a>

#### 3.2.2 源码中的 CPU 余额判断

`fitsRequest` 将新 Pod 的 CPU 请求与候选节点余额比较。下面连续摘自固定提交 `301946d15e67a4a2e8a5fb8292eb836acd366d78` 的 [fit.go / fitsRequest](https://github.com/kubernetes/kubernetes/blob/301946d15e67a4a2e8a5fb8292eb836acd366d78/pkg/scheduler/framework/plugins/noderesources/fit.go#L668-L677)，保留真实语句并增加中文注释。

代入第 3.2.1 节的示例：`podRequest.MilliCPU=201`，节点可分配 5000m，已请求 4800m，判断式为 `201 > 5000-4800`。

变量从哪里来：`podRequest` 是参数，保存已经算好的 Pod 请求；`nodeInfo` 是参数，提供这台候选节点的账；`insufficientResources` 在前文初始化，用来保存每一条不足原因。摘录前检查 Pod 数，后面继续检查内存等资源。

`v1` 是导入的 Kubernetes API 包在这份代码里的名字；`v1.ResourceCPU` 是该包的资源名常量，表示 CPU，不是本函数声明的局部变量。

```go
// 请求为正且超过节点余额时，记下 CPU 不足；不读取实时 usage。
if podRequest.MilliCPU > 0 && podRequest.MilliCPU > (nodeInfo.GetAllocatable().GetMilliCPU()-nodeInfo.GetRequested().GetMilliCPU()) {
    // 把一条结构体记录加入已有失败列表，并保存 append 返回的新切片。
    insufficientResources = append(insufficientResources, InsufficientResource{
        ResourceName: v1.ResourceCPU, // 失败的资源是 CPU。
        Reason:       "Insufficient cpu", // 提供给上层的原因。
        Requested:    podRequest.MilliCPU, // 新 Pod 请求，单位毫核。
        Used:         nodeInfo.GetRequested().GetMilliCPU(), // 保存已有请求量。
        Capacity:     nodeInfo.GetAllocatable().GetMilliCPU(), // 这里取的是 Allocatable。
        Unresolvable: podRequest.MilliCPU > nodeInfo.GetAllocatable().GetMilliCPU(), // 清空其他请求仍放不下？
    })
}
```

请求超过 `可分配量 − 已有请求` 时，函数加入一条 `Insufficient cpu` 记录，其中保存新请求、已有请求和节点容量。这个分支完成后还会继续检查内存等资源，调用者再根据不足记录决定该节点能否通过。

把 201m 改成 200m，`200 > 200` 不成立，就不会加这条 CPU 不足记录。CPU 通过不代表其他资源也通过。

`Unresolvable` 区分的是单节点容量不足与当前余额不足。新请求 201m 小于节点可分配的 5000m，所以该字段为 false，释放别的请求可能改善余额；若新请求为 6000m，则为 true，删光其他 Pod 也凑不出 6000m。它只说“靠删除别的 Pod 解决不了这个节点的容量问题”，不表示扩容或修改规格都没用。

这里的 Go 切片保存一组有序记录；它是函数内的数据结构。相关的两种 Go 语法是：

1. `&&` 表示两个条件都要成立，左边不成立就不算右边。这里先看请求为正，再看是否超过余额。
2. `append(原列表, 新记录)` 返回更新后的列表，所以要再赋回 `insufficientResources`。`InsufficientResource{字段: 值}` 就是填一条结构体记录。

`对象.方法()` 是调用对象提供的方法。`Used` 在这里存的是 `GetRequested()`，即已请求量；字段名字像“用量”，但不能因此读成实时 usage。

再看结果怎样传出去，按箭头从左到右读：

这里的 Status 是插件交回的“本次处理结果”，与 Pod 的 status（对象当前状态）用途不同。

```text
fitsRequest 返回不足记录
  → NodeResourcesFit.Filter 把记录转成 Status 和原因
  → Framework 收到该插件失败，停止这台节点后面的 Filter 插件
  → 若本轮没有任何可行节点，进入后续失败处理
```

固定源码中，只要一条资源记录的 Unresolvable=true，就选用 UnschedulableAndUnresolvable；否则资源不足用 Unschedulable。没有不足记录时，Filter 返回 nil，按这个插件的返回约定表示成功。若读取前置状态失败，则返回 Error，需要另查执行异常。

一个资源插件内部可以同时记下 CPU 和内存不足；框架仍会在该插件失败后停下后续插件。这两个判断说的是不同范围。

Kubernetes v1.34.0 的[发布源码](https://github.com/kubernetes/kubernetes/blob/f28b4c9efbca5c5c0af716d9f2d5702667ee8a45/pkg/scheduler/framework/plugins/noderesources/fit.go)也采用这一 CPU 判断与 Unresolvable 分流。函数签名和周边功能仍可能随版本变化。

### 3.3 调度器看的是最终 Pod，不是 Git 里的一小段模板

limit 是运行时资源上限；LimitRange 是 namespace 内用于设置默认资源值和单个对象资源范围的规则；RuntimeClass 用来选择运行容器的方式。overhead 是这类运行方式额外需要的 Pod 开销，算请求时也要计入。webhook 则是保存对象时调用的外部检查或修改程序，它也可能改变输入。

Pod 创建时，缺少的同资源 request 会按 limit 补齐；明确填写的 request 不会被这条默认规则覆盖。LimitRange、注入容器、RuntimeClass overhead 等还可能影响最后的输入。因此容量计算以 API 中最终 Pod 为起点。[S4]

sidecar 是与业务容器配合工作的辅助容器，例如收集日志的容器。对于最简单的普通容器场景：

```text
业务容器：1000m / 4Gi
常驻 sidecar：200m / 256Mi
常驻阶段合计：1200m / 4352Mi
```

不是仍按“业务容器 1 核”计算。

init container 是业务容器启动前先运行的初始化容器。普通 init 完成后退出；常驻 sidecar 会继续运行，所以不能把二者都当成“启动时才占一次资源”。

再加入一个普通 init container，请求 2000m / 512Mi，在没有原生 sidecar、Pod-level resources 和 overhead 的简化条件下：

```text
Pod CPU request = max(常驻阶段 1200m，init 阶段 2000m) = 2000m
Pod 内存 request = max(常驻阶段 4352Mi，init 阶段 512Mi) = 4352Mi
```

CPU 和内存分别取较大的数。这里 CPU 峰值来自 init，内存峰值来自常驻容器，不能选中一个“最大容器”后把它的全部请求当成答案。[S4][S15]

原生 sidecar 的资源计算还要考虑它与后续容器重叠运行的阶段。它是 init 列表中 `restartPolicy: Always` 的容器，后面的 init 启动时它还在。因此要算“这个阶段有哪些容器同时运行”。假设按顺序先启动日志 sidecar `200m/256Mi`，再运行普通 init `1500m/512Mi`，业务容器为 `1000m/1Gi`，没有其他资源设置：

| 阶段 | 同时运行的容器 | CPU / 内存请求 |
|---|---|---|
| 启动日志 | 日志 sidecar | 200m / 256Mi |
| 初始化业务数据 | 日志 sidecar + 普通 init | 1700m / 768Mi |
| 运行业务 | 日志 sidecar + 业务容器 | 1200m / 1280Mi |

CPU 峰值是 1700m，内存峰值是 1280Mi，最后再加适用的 overhead。init 顺序会改变重叠关系。Pod-level resources、原地 resize 也有版本规则；遇到这些情况再查 `resource.PodRequests`，不要拿简单求和命令当通用计算器。[S16]

Pod-level resources 是直接在整个 Pod 层设置资源预算；原地 resize 是保留原 Pod、调整运行中资源配置的能力。两者都要核对目标版本，不能把“每个容器直接相加”套到所有场景。

<a id="default-requests"></a>

#### 3.3.1 limit 与 LimitRange 怎样影响最终 request

Java 团队在 Deployment 模板里只写了 CPU limit=300m，没有 CPU request。你查看模板确实找不到该 request，但最终 Pod 可能有 `requests.cpu=300m`。

这是两个对象在不同阶段的输入。Pod 创建时，缺少的同资源 request 会按 limit 补齐；已写明的 request 不会被这条默认规则覆盖。scheduler 看的是最后保存的 Pod。[N3]

以下四种配置均不使用 Pod-level resources，也没有修改 CPU 的额外 webhook：

| 配置情形 | namespace 的 CPU defaultRequest | 提交时 CPU request / limit | 最终 CPU request |
|---|---|---|---:|
| A：Deployment 创建的 Pod | 无 | 未写 / 300m | 300m；Deployment 模板仍未写 CPU request |
| B：明确填写 request | 无 | 100m / 300m | 100m |
| C：两项都未写 | 100m | 未写 / 未写 | 100m，由 LimitRange 补上 |
| D：有 namespace 默认值，仍只写 limit | 100m | 未写 / 300m | 300m |

D 中的 300m 来自默认处理的先后顺序：Pod 默认处理先把 limit 复制到缺少的 request；LimitRange 后面只补仍然缺少的项，不把已有的 300m 覆盖成 100m。不要仅看一份 LimitRange 就断定所有 Pod 都申请了 100m。[N3][N4]

对 Java 服务，还要分别考虑：request 影响调度承诺；CPU limit 可能造成运行时 throttling。明确写 100m/300m 只说明两项配置各是多少，是否适合 JVM 启动、JIT 和 GC，仍要靠真实应用数据。

#### 3.3.2 源码怎样补齐缺少的 request

`SetDefaults_Pod` 只在同一资源的 request 缺少时复制 limit；已有 request 会保留。以下源码使用第 0.1 节的固定提交。

下面是 `pkg/apis/core/v1/defaults.go / SetDefaults_Pod` 的连续摘录，只增加中文教学注释和缩进；不能独立编译。[N3] `obj` 是传入的 Pod，`i` 是外层循环的容器下标；前文已判断 limits 存在，并在需要时初始化 requests。后文还处理 init 和其他默认值，本组不涉及这些配置。

```go
// 逐个检查该容器配置了 limit 的资源，比如 CPU。
for key, value := range obj.Spec.Containers[i].Resources.Limits {
    // 查同一资源的 request 是否存在；只补缺少的项。
    if _, exists := obj.Spec.Containers[i].Resources.Requests[key]; !exists {
        // 复制 limit 的数量作为 request，不覆盖已存在的值。
        obj.Spec.Containers[i].Resources.Requests[key] = value.DeepCopy()
    }
}
```

这段默认处理补齐创建中的 Pod：某资源的 request 缺少时，复制同资源的 limit。Deployment 模板仍可保留原先省略 request 的形式，因此最终 Pod 与模板可能不同；节点是否放得下还由后续调度判断。

map 像一张“按资源名找数量”的表；`range` 在这里逐项读取它，`key` 是资源名，`value` 是数量。`_, exists := map[key]` 同时返回“值”和“是否存在”，`_` 表示值这次不用。分号前先查，分号后的 `!exists` 表示“不存在时才执行”。`DeepCopy()` 得到数量的副本。

例如，B 的显式 request 为 200m、limit 为 300m 时，`exists` 为 true，复制分支不会执行，request 仍为 200m。

### 3.4 request、limit 与 QoS 分别回答什么

request 主要参与调度资源账；limit 由节点运行时与内核路径实施，不同资源实施方式不同；usage 是测量结果。CPU limit 可能带来 throttling；内存上限与 OOM 是运行时问题。JVM 堆上限也不是整个容器的内存上限。[S4]

throttling 是 CPU 达到限额后暂时不能继续获得计算时间，即使节点整体还有空闲也可能发生。OOM 是内存不足；要结合容器是否被杀、应用报错来判断。JVM 堆只是 Java 对象主要存放的那部分内存，容器还可能使用线程栈等其他内存。

QoS 描述资源设置分类，PriorityClass 设置调度优先级；Guaranteed 不会因此自动比 Burstable 先调度。调度队列的顺序另看 priority 和实际 QueueSort。[S8]

QoS 可以先理解为“按 CPU、内存配置给 Pod 分档”。未设置 Pod-level resources 时，Guaranteed 要求所有普通及 init 容器的 CPU、内存 request 和 limit 都大于 0，并且同一资源的 request 与 limit 相等；都未设置有效 CPU、内存请求和上限的是 BestEffort；其余为 Burstable。Pod-level resources 场景另按版本核对。priority 是排队优先级数值，PriorityClass 保存这个数值，QueueSort 是队列比较谁先尝试的规则；资源分档与排队顺序各管一件事。

### 3.5 `describe node` 好用，但不是调度器内存快照

```bash
kubectl --context "$CTX" describe node worker-a
kubectl --context "$CTX" get pods -A --field-selector spec.nodeName=worker-a -o wide
```

这些命令能看到 API 中已经分配到节点的 Pod。但 scheduler 可能刚在内存里给 A 记了账，还没把绑定写入 API，你此时查不到 A。第 10.2 节会讲这个动作，名字叫 Assume。几条查询也不是同一瞬间完成，所以有短暂差额时，先对齐时间再判断。[S1][S17]

请求超过节点 Allocatable，与请求只超过当前余额，对应不同的容量问题：前者无法靠释放同节点其他 Pod 解决，后者可能改善。若创建时已被 ResourceQuota 拒绝，则还没有进入 scheduler 的资源检查。

---

### 3.6 包含 Pod overhead 的多节点计算

沿用第 3.3 节普通常驻 sidecar 的算例，有效请求为 `2000m / 4352Mi`。再加入 Pod overhead `100m / 128Mi`，最终请求为 `2100m / 4480Mi`。

假设节点健康、Pod 数未满，标签、污点和卷等其他硬条件全部满足，并且没有并发资源变化。各节点已经扣除现有请求后的余额如下：

| Node | CPU 余额 | 内存余额 | 对本 Pod 的判断 |
|---|---:|---:|---|
| a | 2300m | 4608Mi | 两项均满足 |
| b | 3000m | 4096Mi | 内存差 `4480−4096=384Mi` |
| c | 1800m | 6144Mi | CPU 差 `2100−1800=300m` |

第一只 Pod 只能放到 a。记入请求后，a 剩余 `200m / 128Mi`，b、c 的不足仍存在，因此第二只相同规格的 Pod 没有可行位置。

如果 b 的可分配内存增加 512Mi、已有请求不变，它的内存余额变为 `4096+512=4608Mi`，这时 CPU 和内存都能满足该规格。这个变化只修复 b 的内存条件；真实调度还要同时满足其他硬约束。

---

## 4. 一台节点必须同时满足哪些条件

### 4.1 一张表对应一个排查方向

nodeAffinity 是节点亲和，用节点标签表达想去哪里；podAffinity 是 Pod 亲和，用其他 Pod 的位置表达想靠近谁；podAntiAffinity 是 Pod 反亲和，表达想与谁分开。topologySpreadConstraints 是分布规则，用来限制同一组副本在节点或可用区之间偏得太多。表里的 Pod slots 只是“还能放几只 Pod”的位置数量。

taints/tolerations 就是节点的拒绝条件与 Pod 的接受声明，第 4.3 节给出配置示例。profile 是同一个调度器进程里的一套规则配置；addedAffinity 是这套配置额外要求的节点条件，Pod 自己的 YAML 未必显示它。

| 条件 | 它约束什么 | 典型取证 |
|---|---|---|
| nodeSelector / required nodeAffinity | 必须是哪类节点 | 最终 Pod 规则、Node labels、profile addedAffinity |
| taints / tolerations | 是否被节点拒绝 | key、value、operator、effect，不能漏第二条 taint |
| resources.requests | 单节点承诺是否够 | CPU、内存、临时存储、扩展资源 |
| Pod 数上限 | 节点还能容纳多少个 Pod | Node allocatable.pods、scheduler 已计入的 Pod 数 |
| podAffinity / podAntiAffinity | 和哪些 Pod 同域或不同域 | selector、namespace 范围、topologyKey |
| topologySpreadConstraints | 匹配 Pod 在域间的分布 | eligible domains、各域计数、maxSkew |
| PVC / PV / CSI | 卷与节点拓扑是否相容 | SC、PV nodeAffinity、attach 限制、延迟绑定 |
| hostPort | 节点上的端口是否冲突 | hostIP、协议、端口与其他 Pod |

同一台节点必须把这些硬条件全部满足，才有资格参加打分，也就是 Score。比如只有 a 区节点有 CPU，只有 b 区节点能使用这个卷，两边拼起来也放不下这只 Pod。

Filter 是逐项检查节点的过程。固定源码中，一个插件检查失败，就停止这个节点后续的 Filter 插件，这叫“短路”。因此先修好 CPU 后才显示卷错误，可能只是之前没检查到卷。一个插件内部仍可以报告多个资源不足；一条 Event 不保证列全该节点的问题。[S1][S5][S17]

Pod 数是独立限制，普通 Pod 不通过填写 `resources.requests.pods` 申领位置。即使 CPU、内存请求为零，一个新 Pod 仍占一个位置；资源检查比较已计 Pod 数加一是否超过节点允许值。[S19]

### 4.2 nodeAffinity 的 AND / OR

AND 就是“且，每条都满足”；OR 就是“或，满足其中一组即可”。term 是一组条件，matchExpressions 是组内逐条写出的标签检查；例如 `In [a,b]` 表示标签值是 a 或 b。

```text
nodeSelector 的多个 key                         → AND
同一 nodeSelectorTerm 的多个 matchExpressions  → AND
不同 nodeSelectorTerms                         → OR
nodeSelector 与 required nodeAffinity          → 都要满足
```

例子：`pool=online` 且 `zone in [a,b]`，需要放在同一个 term 内。写成两个 term，含义就可能变成“online 池或者 zone a/b”，意外扩大可行范围。[S5]

下面是 Pod.spec 下的规则片段；Deployment 中对应 spec.template.spec：

```yaml
affinity:
  nodeAffinity:
    requiredDuringSchedulingIgnoredDuringExecution:
      nodeSelectorTerms:
      - matchExpressions:
        - key: pool
          operator: In
          values: [online]
        - key: topology.kubernetes.io/zone
          operator: In
          values: [a, b]
```

含义是“online 池内，且位于 a 或 b 区”。反例：`pool=batch, zone=a` 必须失败；`pool=online, zone=c` 也失败；`pool=online, zone=b` 只通过这条亲和，仍需检查资源、污点和卷。

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

NoExecute 可能让已有 Pod 退出，tolerationSeconds 指可继续容忍多少秒。Eviction 是提出驱逐请求的 API；PDB 是维护驱逐时保护一定数量可用副本的预算，具体场景的限制见第 7、9 章。

例如，Pod 的 required affinity 匹配 GPU 节点标签，但没有容忍该节点的 `dedicated=gpu:NoSchedule`，节点仍会被过滤。增加匹配的 toleration 只解除这一项拒绝，不会额外提高该节点的评分。

---

## 5. 贯穿案例：一次 activity 发布为什么卡住，又为什么恢复

### 5.1 先给出现场，再逐节点算

这是 Java/Spring Boot 教学场景：`activity` 发布后创建了新 Pod，要求 `1000m CPU / 4Gi`，必须进入 `pool=online`，没有 GPU 专池 toleration。request 来自已经通过准入的最终 Pod。JVM 尚未启动，readiness 也还没机会检查；先解决节点放置问题，再验证预热和接口。

| Node | pool | taint | CPU 余额 | 内存余额 | 当前判断 |
|---|---|---|---:|---:|---|
| worker-a | online | 无相关拒绝 | 800m | 3Gi | 资源不足 |
| worker-b | batch | 无相关拒绝 | 3000m | 8Gi | 节点标签不匹配 |
| worker-c | online | dedicated=gpu:NoSchedule | 3000m | 8Gi | 未容忍污点 |

没有节点同时满足全部条件。表中综合了资源、标签和污点约束；单条 Event 可能受 Filter 短路影响，只报告其中部分失败原因。

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

只演示 CPU 的 LeastAllocated 教学分数。两节点都已通过硬条件，8000m 是 **Allocatable**；参与计算的容器都明确填写 CPU request，评分口径与表中请求一致。这不是完整默认 scheduler 总分。[S18][S20]

LeastAllocated 就是更偏好“放入这只以后，按请求账看还比较空”的节点；不是比较实时 CPU 使用率。第 13 章的 MostAllocated 相反，更偏好请求已占比例较高的节点。

| Node | CPU Allocatable | 放入前 request | 本 Pod request | 放入后 request | 剩余比例 |
|---|---:|---:|---:|---:|---:|
| worker-d | 8000m | 2000m | 1000m | 3000m | 62.5% |
| worker-e | 8000m | 4500m | 1000m | 5500m | 31.25% |

```text
d：100 × (8000 − 2000 − 1000) / 8000 = 62.5
e：100 × (8000 − 4500 − 1000) / 8000 = 31.25
```

实现用整数运算，所以 CPU 子项分别得到 62、31。这个例子只比较一项；真实评分还要看 memory、NodeAffinity、拓扑、镜像本地性和插件权重。

资源评分路径可能对未填写 CPU、内存 request 的容器使用非零默认值，因此 Filter 与 Score 的输入口径需要分别核对。本例中所有相关 request 均已明确填写；缺省请求的评分则由相应插件配置与实现决定。[S20]

### 5.4 “更喜欢 d”不等于“一定去 d”

教学示意：

```text
插件 A：d=62，e=31，权重=1
插件 B：d=0，e=100，权重=2
总分：d=62，e=231
```

最终 e 更高。这里 B 的分数是已完成该插件所需归一化后的示意值，不是说业务填写 preferred.weight=100 就一定直接贡献 100 分。

有些插件先把自己算出的值换算到框架要求的分数范围，这步叫归一化。只有提供相应扩展的插件才做这一步，NodeResourcesFit 没有提供该扩展。CPU、内存在一个插件里的权重，与 Framework 给整个插件的权重，也要分开算。[S1][S2][S18]

硬约束失败的节点不会进入评分候选集。例如，节点带有 Pod 未容忍的 NoSchedule 污点时，提高任何 Score 权重都不能让它通过 Filter。

---

## 6. 多副本可靠性：拓扑分布与软硬约束

### 6.1 hostname 分散不等于跨可用区

三只 Pod 分别在三台 Node 上，但三台 Node 同属一个可用区，仍可能在一次 AZ 故障中一起受影响。`kubernetes.io/hostname` 与 `topology.kubernetes.io/zone` 是不同故障域。[S7]

AZ 就是可用区；故障域是可能被同一次故障一起影响的一组位置。拓扑在这里指按 hostname、zone 等标签把节点分组，topologyKey 就是用哪一个标签分组。同标签值属于同一个域，例如 zone=a 的节点都算 a 域。

下面是 Deployment 的 `spec.template.spec` 中与拓扑有关的配置片段。Pod 模板带有 `app: activity` 标签，zone 层要求硬分散，hostname 层提供软分散偏好：

```yaml
topologySpreadConstraints:
- maxSkew: 1
  topologyKey: topology.kubernetes.io/zone
  whenUnsatisfiable: DoNotSchedule
  labelSelector:
    matchLabels:
      app: activity
- maxSkew: 1
  topologyKey: kubernetes.io/hostname
  whenUnsatisfiable: ScheduleAnyway
  labelSelector:
    matchLabels:
      app: activity
```

两条规则使用同一组 Pod 标签，但比较的节点分组不同。zone 硬规则可能排除某个位置；hostname 软规则只在可行节点中影响偏好。节点上的拓扑标签需要准确反映实际故障域。

该配置不保证副本覆盖三个可用区。未配置 `minDomains` 时，效果相当于 `minDomains: 1`。若仅有一个 eligible zone，已有两只匹配 Pod，下一只仍可满足 `2+1−2=1`，三只可能都在同区。硬分散限制参与计算的域之间的差值，不会创造可用区。[S7]

### 6.2 先算“多放这一只，会不会偏得太多”

先用三台节点说明。每台 Node 是一个 hostname 域，目前副本数为 `1/1/0`，规则只统计本服务的 Pod，允许偏差 `maxSkew=1`。要放的这只也匹配统计规则：

| 准备放的位置 | 现有数量 | 加上自己 | 减去最少域的数量 0 | 能否通过 |
|---|---:|---:|---:|---|
| 第一台 | 1 | 2 | 2 | 超过 1，失败 |
| 第三台 | 0 | 1 | 1 | 没超过 1，通过 |

这个偏差叫 skew。对 `DoNotSchedule`，本例的计算写成公式就是：

```text
目标域现有匹配 Pod 数 + 本 Pod 贡献 − global minimum ≤ maxSkew
```

先分两步计算：

1. **确定参与的节点域。**检查 topologyKey 对应的节点标签，再按 nodeAffinityPolicy、nodeTaintsPolicy 判断哪些节点参与。得到的域叫 eligible domains；某个域没有同伴 Pod，也可能参与，数量记为零。
2. **数每个域的同伴 Pod。**只统计与新 Pod 同 namespace、且匹配 labelSelector 等统计规则的 Pod。Pod 标签选择器决定数谁，不会单凭“这个域目前没同伴”就把域排除。

公式里作为基准的最小数量叫 global minimum。通常取参与域的最少数量；但参与域数小于 minDomains 时，**基准按 0**。所以不能在所有场景都简化成“最多减最少”。[S7]

再加一个条件：第三台只剩 `1.5 CPU/3Gi`，新 Pod 要 `2 CPU/4Gi`。第三台虽然分布规则通过，但资源不足；前两台虽然可能资源足够，但分布规则不通过。这时三台都不能放。

#### 6.2.1 只有两个域时，minDomains 怎样改变结果

假设可参与统计的域只有 a、b，初始计数为 `0/0`；三个待放置 Pod 都匹配选择器，其他硬条件均满足，分布规则为 `maxSkew: 1`、`DoNotSchedule`。

| minDomains | 前两只放置后的计数 | 第三只的计算 | 结果 |
|---|---|---|---|
| 省略，等效为 1 | 1/1 | `1+1−1=1` | 通过，分布可成为 2/1 |
| 3 | 1/1 | `1+1−0=2` | 超过 maxSkew，第三只等待 |

第二种配置中，参与域数 2 小于 minDomains 3，所以 global minimum 按 0。第一只放 a 时，`0+1−0=1`；第二只放 b 时也为 1。直到第三只准备放入任一域，偏差才变成 2。因此，`minDomains: 3` 并不表示少于三个域时所有 Pod 都立即被拒绝。

### 6.3 某个域没位置时，是等待还是允许集中

硬规则不满足，就不给新 Pod 选这个位置，因此某域容量不足时可能卡住发布。软规则只是偏好，必要时可以集中放置，但也不能保证总是均匀。

故障期间的分布策略取决于业务需要保留的服务能力和存活域容量。继续维持硬规则可能让部分副本等待；允许在存活域集中则能增加可运行副本，但会扩大这些域再次故障时的影响。

`minDomains` 决定域数不足时的计数基准。上一节的两个域即使资源充足，仍可能因 `minDomains: 3` 而无法补齐第三只副本；增加单节点 CPU 并不能解除这一分布限制。[S7]

`nodeTaintsPolicy` 未配置时相当于 Ignore，所以被污点拒绝的新 Pod 仍可能受该节点所属域的计数影响。“不能放置的节点”与“不参与统计的节点”是不同集合。[S7]

节点 NotReady、出现污点和节点对象被删除，对参与统计的域可能产生不同影响。计数取决于仍然存在的节点及其标签、亲和和污点策略，不能仅按“该域业务不可用”就从统计中删除它。[S7]

---

## 7. 发布容量：稳定态放得下，不代表更新时放得下

### 7.1 单副本发布为什么需要第二份位置

replicas 是目标副本数；maxSurge 是更新时允许额外增加多少只；maxUnavailable 是更新时最多允许多少目标副本暂时不可用。rollout 就是推进版本替换，PodTemplate 是用来创建新 Pod 的模板。terminating 表示正在退出，开始退出后资源不一定马上释放。

教学条件：replicas=1，maxSurge=1，maxUnavailable=0，单 Pod 为 1 CPU/4Gi。开始滚动更新时，控制器可创建一个新副本，同时保留旧副本等待新副本可用。因此这个阶段需要额外一个符合全部约束的 Pod 位置。[S10]

这不是“所有时刻实际总 Pod 数绝不会超过 2”。正在终止的 Pod、连续模板更新等会造成额外重叠，实际资源消耗可能超过 replicas+maxSurge。生产容量必须观察 terminating 占用和控制器时间线。[S10]

### 7.2 百分比也要算对

maxSurge 百分比向上取整，maxUnavailable 百分比向下取整。例如 replicas=3、两者均 25%，surge 为 1，unavailable 为 0。不要把 0.75 个 Pod 四舍五入成 1 个 unavailable。[S10]

六个单副本服务都发生 PodTemplate 变化，每个允许多一只，每只 `1 CPU/4Gi`，首波就可能新增 `6 CPU/24Gi` 请求。先逐节点核算：如果实际只有四个满足全部条件的位置，最多先接纳四只，至少两只等待。修改六个代码仓库不等于一定触发六次 rollout，要看模板是否真的变了。

### 7.3 三种修复的代价不同

预留可行容量，保留可用性但增加成本；减小发布批次，延长发布过程但降低峰值；允许先下旧副本，减少瞬时容量但可能引入不可用窗口。单副本尤其不能把 maxUnavailable=1 当无风险开关。

PDB 不是 Deployment 滚动更新副本策略的替代品。维护驱逐与应用滚动更新要分别设置预算、探针、终止行为和验收。[S9][S10]

### 7.4 容量不足时，新旧副本的状态

假设只有一台节点满足该应用的节点选择条件，其可分配 CPU 为 `5000m`，系统 Pod 已请求 `100m`，应用每个副本请求 `2750m`，内存等其他条件均满足：

```text
仅旧副本运行：100 + 2750 = 2850m，未超过 5000m
新旧同时存在：100 + 2750 × 2 = 5600m，超过 5000m
```

在 `replicas=1、maxSurge=1、maxUnavailable=0` 下，旧副本可以继续保持 Ready，新副本则因 CPU 余额不足而无法绑定。控制器需要新副本可用后才能缩减旧副本，但节点又没有容纳新副本的余量，所以发布会等待。

如果改为 `maxSurge=0、maxUnavailable=1`，控制器就允许先减少旧副本。旧副本退出、资源释放后，新副本才有机会绑定；从旧副本停止服务到新副本完成启动和预热之间，单副本服务可能没有可用实例。

Java 服务的 JVM 启动、缓存加载、readiness 和优雅退出都会影响这个窗口。副本数量、资源占用与实际流量需要分别分析，不能仅凭更新最终完成就认为整个过程没有中断。

---

## 8. 存储、端口与节点可用性约束

### 8.1 存储的两个阶段

PVC 是 Pod 申请存储的对象；PV 表示被提供的存储；StorageClass 定义供给方式与策略；CSI 驱动负责对接实际存储系统。先区分“卷是否找到合适供给”和“节点是否把卷挂好”。

Immediate 的卷可能先绑定，之后 Pod 受 PV 的节点拓扑限制；WaitForFirstConsumer 让卷与首个消费者的选点协同。PVC Pending 在 WFFC 场景可能是等待配合，不应直接判为 CSI 故障。[S11]

Immediate 是先处理卷供给和绑定，不等 Pod 选点；WaitForFirstConsumer 缩写 WFFC，是等到首个使用这份存储的 Pod 要选点，再配合决定存储放在哪里。attach 是把存储设备接到节点，mount 是把它挂到可访问的目录。FailedMount 是挂卷准备失败的报告，可能来自 attach、mount 等环节，要读 message 才能区分。

```bash
kubectl --context "$CTX" get pod,pvc -n "$NS" -o wide
kubectl --context "$CTX" get storageclass
kubectl --context "$CTX" get pv
kubectl --context "$CTX" describe pvc -n "$NS" PVC_NAME
```

Pod 要求 zone-a、可用 PV 只能在 zone-b，就需要同时解决存储和计算约束；扩一台 zone-a 的纯 CPU 节点不一定有用。Pod 已有 nodeName 之后的 FailedMount，则进一步检查节点侧挂载、CSI、网络与后端存储。

不要用手工 nodeName 强行绕过 WFFC 调度配合，也不要伪造 selected-node 注解来“帮助”控制器。[S11]

### 8.2 另外三类容量

hostPort 冲突不是 Service 的 port 冲突；Pod slots 不足不是 CPU 不足；临时存储 request 与节点磁盘压力也不是同一个信号。CPU、内存宽裕不能证明其他维度可用。[S4][S17]

**排查方法：**从失败方向找到对应 API 字段，再去同一候选节点上核对，而不是执行一套所有组件的全量命令。

<a id="hostport"></a>

### 8.3 Java 都用 8080，为何有的能同节点，有的不能

普通 Pod 使用各自的网络空间。两只 Java Pod 都监听自己的 8080，在没有 hostNetwork、hostPort 等特殊配置时，不会仅因为相同的 containerPort 就被 scheduler 判为宿主机端口冲突。

containerPort 是容器端口声明，填写它不会替应用启动监听；hostPort 是申请节点上的端口；hostNetwork 是直接使用节点网络。hostIP 指申请节点上哪个地址，TCP 是本例使用的网络协议。它们决定是否会争用同一份端口位置。

配置 `hostPort: 18080` 是申请节点上的端口。假设 Pod A 已在 worker-a 上申请 TCP 18080，Pod B 也要求放到 worker-a 并申请同一 hostPort。即使 B 只请求 50m CPU、节点 CPU 余额充足，端口冲突仍会让该节点的检查失败。

两者未填写 hostIP 时，检查按 `0.0.0.0` 处理；实际冲突判断还考虑 hostIP 和协议。因此，不能脱离地址与协议，把所有相同的端口数字都视为冲突。[N5][N6]

只声明 `containerPort: 8080` 的 Pod C 没有申请上述宿主机端口，不会因此与 A 冲突。A 退出且端口记录从 scheduler 缓存移除后，B 在下一次尝试时可能通过端口检查，无需修改 CPU request。

NodePorts 检查的是 Pod 声明形成的端口账，不会扫描宿主机上所有进程的 socket。应用是否实际执行了 bind/listen 属于运行时行为；调度通过也不能代替应用启动结果。

socket 是程序收发网络数据的端点；网络 bind 是给它指定地址和端口，listen 是开始等连接。这里的网络 bind 与 Kubernetes 把 Pod 绑定到节点的 Bind 是两件事。`ss` 用来查看系统里的网络端点和连接状态。

#### 8.3.1 NodePorts 的端口冲突判断

这是固定提交 `pkg/scheduler/framework/plugins/nodeports/node_ports.go / fitsPorts` 的完整函数，保留所有分支，只增加中文教学注释；它依赖上游类型，不能单独编译。[N5]

参数 `wantPorts` 是这只 Pod 的 hostPort 声明列表；`portsInUse` 是候选节点上已计入的端口账。B 的 TCP 18080 与 A 的端口记录冲突时，函数在 `return false` 处结束。

类型名也按名字读：`v1.ContainerPort` 是 API 包的端口记录类型；`fwk` 是导入的框架包在这里的名字，`fwk.HostPortInfo` 是它提供的端口账类型。参数接收这些类型的值，不是重新定义了这些类型。

```go
// 检查新 Pod 申请的端口列表，返回能否通过这项检查。
func fitsPorts(wantPorts []v1.ContainerPort, portsInUse fwk.HostPortInfo) bool {
    // 逐项检查；某项冲突就不必继续检查其他项。
    for _, cp := range wantPorts {
        // 同时交给端口账核对 hostIP、协议和 hostPort。
        if portsInUse.CheckConflict(cp.HostIP, string(cp.Protocol), cp.HostPort) {
            return false // 发现冲突：这台候选节点不能通过本项。
        }
    }
    return true // 没发现冲突；不代表全部 Filter 都已通过。
}
```

`fitsPorts` 逐项比较端口请求与节点现有端口记录，发现一项冲突就返回 false。它只读取端口账，节点是否通过完整过滤由外层插件框架决定。

`[]v1.ContainerPort` 是一组端口记录。`for _, cp := range wantPorts` 中 `_` 忽略序号，`cp` 是本次记录；`string(cp.Protocol)` 把协议值转成字符串。这里的 false 是布尔结果，不是 error，也不是 Pod phase。

结果继续传递：`fitsPorts=false` → `NodePorts.Filter` 返回 Unschedulable 和原因 → 该节点的 Filter 检查失败。PreFilter 状态读取出错则走 Error；成功时 Filter 返回 nil，在这个接口里表示成功。没有 hostPort 的 Pod 会在 PreFilter 阶段得到 Skip，后续不执行该插件的 Filter。

v1.34.0 的 helper 参数与本文固定提交不同：它先接收 nodeInfo，再从中取端口账；冲突就返回 false 的判断一致。函数签名应按对应版本读取。[N7]

---

## 9. 优先级与抢占：排队靠前，不等于保证成功

### 9.1 优先级高，可以先试，也可能腾出位置

抢占就是为了让当前高优先级 Pod 获得位置，尝试让一些低优先级 Pod 先退出；被选中退出的 Pod 常叫 victim，也就是受害者。退避则是失败后先等一段时间再试，避免相同条件下不断空转。

Pod 用 `priorityClassName` 引用 PriorityClass 中的优先级。更高的优先级通常能更早获得尝试机会；但如果还在退避或有 gate，并不保证下一只处理的就是它。

若没有可行节点，调度器可能先算一下：移除一些低优先级 Pod，能不能让它放下？找到方案后，再推进抢占。发出删除请求、容器退出、资源账释放和新 Pod 再次尝试，是几步工作。[S8]

```text
本轮无可行节点
  → 模拟可能的受害者方案
  → 选择方案并推进删除/提名
  → 等事实变化
  → 下一轮重新验证
  → 才可能绑定
```

`status.nominatedNodeName` 记录潜在落点，后续仍可能改变；它不表示 API 已绑定，也不锁住节点或设备。删除和提名的状态由不同处理步骤更新，具体可观察顺序取决于版本及异步实现。[S8][S17]

### 9.2 抢占能改什么，不能改什么

删除其他 Pod 可能释放 CPU、内存、hostPort 或传统扩展资源请求；它不会自动改变 required 标签、污点关系、卷所在可用区，也不能把最大 8 卡的单节点变成 10 卡。[S8]

PDB 在调度抢占中是尽力遵守，不是绝对免死；drain 的 Eviction 路径对 PDB 的处理又不同。节点故障也不受 PDB 绝对保护。[S8][S9]

`preemptionPolicy: Never` 让高优先级 Pod 不主动抢占，但它仍有较高排队优先级，也可能被更高优先级 Pod 抢占。不要通过不断提高 priority 来替代容量规划。[S8]

### 9.3 抢占策略与资源释放时间

假设只有一台节点满足 high 的节点选择条件，且只能容纳一个该规格的 Pod。当前运行的 low 优先级为 100，新来的 high 优先级为 20000；除资源余额外，其他硬条件都满足。high 的处理还取决于抢占策略：

| high 的 preemptionPolicy | 资源不足时的行为 |
|---|---|
| Never | 可以优先排队，但不会主动删除 low，只能等待条件变化 |
| PreemptLowerPriority | 调度器可以评估删除 low 后能否放下 high，并在找到有效方案后推进抢占 |

抢占不会瞬间归还资源。low 收到删除请求后，可能还在执行 `preStop`、处理已有连接或退出进程；scheduler 需要根据后续对象变化更新缓存，high 再次尝试时仍要检查全部硬条件。

`terminationGracePeriodSeconds` 是正常退出的时间预算，`preStop` 会消耗这份预算。该值不保证进程一定运行到时间用满，也不保证所有业务请求都能在退出前完成。[S3]

`Preempted` Event、low 的删除状态和 high 的绑定状态描述不同阶段。按双方 UID 关联这些状态，可以区分“发生了抢占”和“高优先级 Pod 只是先获得空位”；单独看到 high 已运行无法说明此前发生过哪条路径。

---

## 10. 调度器内部：缓存、绑定与失败重试

Framework 可以先理解为调度器按固定阶段调用规则的框架；插件是负责某类判断的实现，例如 NodeResourcesFit 检查资源，NodeAffinity 检查节点亲和。不是每个阶段都必须配置自定义插件。

### 10.1 为什么 scheduler 不每次都远程查全量 Pod

假设集群有一万只 Pod，每选一次节点都远程读完它们，API 查询很容易拖慢调度。所以 scheduler 在内存中维护 Pod 和 Node 的资料；API 有变化时，再更新这些资料。

informer 接收对象变化，相关处理更新 scheduler 的内存缓存 cache；snapshot 是当前调度轮次使用的节点视图。[S1][S17]

代价是变化传播要花时间。你刚从 API 查到 Pod 已删除，scheduler 可能还没处理对应通知；监控也可能还在显示上一次采样。所以不能把这三处数据当成同一瞬间的结果。

### 10.2 为什么先 Assume，再异步 Bind

cycle 是一轮处理：scheduling cycle 负责为这只 Pod 算节点，binding cycle 继续完成绑定相关步骤。串行是前一只的选点算完再算下一只；并发是前一只绑定尚未完成，下一只已可以选点。异步是发起后不一直等在原地，后续另有流程推进结果。

若等每次 API Binding 完成后才处理下一只 Pod，慢 API 调用会拖低吞吐。普通 Pod 路径中，scheduling cycle 串行，binding cycle 可并发。注意不是“两个普通 scheduling cycle 同时抢同一本账”。[S1]

```text
A 选中节点
  → Assume：先在本调度器 cache 计入 A 的请求
  → Reserve：插件维护自己的预留状态
  → Permit：允许、拒绝或等待协调条件
  → 异步 binding cycle
与此同时：下一只 B 可以开始选点，但应看到 A 已占的请求账
```

按时间从上到下读这个教学例子：

| 时刻 | 正在做什么 | 本 scheduler 的请求余额 | API 中能否看到 A 的 nodeName |
|---|---|---:|---|
| 开始 | A、B 都要 1000m | 1000m | 还没有 |
| 先处理 A | 给 A 选点，Assume 记入 1000m | 0m | 绑定可能还没写入 |
| 再处理 B | B 查余额，当前放不下 | 0m | 可能仍看不到 |
| A 绑定完成 | API 保存 A 的落点 | 0m | 可以看到 |

先在自己账上扣掉 A 的请求，下一只 B 就不会重复使用这 1000m。这笔临时账只属于当前调度器进程，另一个独立 scheduler 不会自动共享；它也没有锁住具体 GPU UUID。[S17]

### 10.3 三种清理分别管谁

补偿就是前面没办成时，把自己已经做过的临时动作撤销。这里 assumed Pod 是已在本调度器 cache 临时占账、还没收到绑定后对象通知来确认的 Pod；API 绑定可能已经成功，只是确认通知还没到。in-flight 是这轮已经取出、还在处理中的对象或变化跟踪。

| 动作 | 所有者 | 清理对象 |
|---|---|---|
| Unreserve | Framework 插件 | 插件私有的临时预留 |
| ForgetPod | scheduler cache | assumed Pod 的通用资源账 |
| Done | 调度队列 | in-flight 对象/事件跟踪 |

接着上例：A 最后绑定失败，这个位置不能继续算被 A 占着。插件先按自己的规则撤销临时预留，scheduler 再撤销请求账；队列也要结束本次处理中对象的跟踪。B 原先因为没余额而失败，余额恢复后还需要有机会再试。[S1][S17]

### 10.4 放不下的 Pod，什么时候再试

先沿用 activity 的教学数字：请求 1000m，目标节点只有 800m。刚失败后马上重复同样的计算，还是放不下。等旧任务释放 500m，请求余额变为 1300m，再试才可能有用。

scheduler 因此会让一部分失败的 Pod 等待，或者隔一段时间再试。`active` 表示准备尝试，`backoff` 表示退避中，`unschedulable` 表示当前条件不满足。带 gate 的 Pod 还没达到可尝试条件。这些内部状态都不能直接当作 Pod 的 phase。[S1][S17]

相关 Pod 或 Node 变化后，scheduler 会收到对象变化通知。QueueingHint 帮它判断“这次变化是否可能让失败条件改善”。只改一个无关标签，不必让所有 CPU 不足的 Pod 全部重算；释放请求则可能值得重算。下一轮仍要检查其他条件，不能保证成功。

watch 是持续接收对象新增、修改、删除通知的 API 方式；informer 在它基础上维护本地资料并把变化交给程序处理。这里要唤醒的是“值得再试”的 Pod，唤醒不等于已经给它找到了节点。

这里的对象变化通知来自 watch/informer；`kubectl get events` 看到的 Kubernetes Event 是组件保存的报告，二者不是同一种东西。

资源也可能恰好在本轮计算期间释放，早于 Pod 被登记为调度失败。in-flight 事件跟踪用于保留这段处理期间的相关变化，避免 Pod 随后进入等待状态却错过已经发生的资源释放。[S17]

### 10.5 成功、拒绝、异常与等待

Unschedulable 表示条件当前不满足；Error 表示执行或依赖遇到异常，两者的运维处理不同。Permit 的 Wait 是绑定前协调等待，不等于 Pod Pending phase。[S1][S17]

A 的 Bind 失败不仅影响 A。它的临时占账也可能让 B 因资源不足而等待，因此失败处理需要同时考虑撤销资源承诺和让受影响的 Pod 重新获得尝试机会。

---

## 11. 回到值班：按什么顺序查

下面的 leader 是同一套主备调度器中，当前获准执行调度工作的实例。检查它是为了确认“谁现在负责”，不是要求每个 Pod 自己选一个 leader。

```text
业务异常
  → 预期 Pod 是否创建？没有：查控制器与准入
  → 有 nodeName？有：查节点启动容器和应用
  → 无 nodeName：核对 schedulerName 和 gates
  → 有调度失败：读取最终 Pod 与候选节点条件
  → 列出每台候选节点通过、失败或未知的条件
  → 看有没有一台同时满足资源、分布、卷和污点等要求
  → 没有普通约束解释：查 leader、队列、插件、API 与缓存
  → 选择最小可解释变更，保留回滚与验收
```

修复后的验收至少有三层：新的 Pod 成功绑定；节点侧能够启动并 Ready；业务恢复且没有破坏节点池隔离、跨域目标或运行时稳定性。只看到 Pending 消失，不足以宣布生产问题解决。

### 11.1 三种相似现象，对应不同处理

| 表面现象 | 对象与状态 | 原因判断 | 改变条件的方向 |
|---|---|---|---|
| 两副本应用只有一只 Pod | ReplicaSet 的 FailedCreate 指向配额超限，第二只 Pod 不存在 | 创建请求被准入拒绝 | 调整授权配额或工作负载需求；增加节点不改变配额 |
| Pod Pending，集群仍有空闲 | Pod 存在且无 nodeName，资源与拓扑等条件没有共同可行节点 | 约束交集为空 | 按目标池、单节点规格和硬条件补足供给或修正规则 |
| 新版本一直不可用，旧版本仍服务 | 旧副本 Ready，新副本因资源不足无法绑定 | 更新所需的额外位置不足 | 预留发布余量、降低并行发布量，或评估允许暂时减少副本的代价 |

修复一个条件后，另一个条件可能继续阻止调度。原因之一是 Filter 短路：本轮遇到的第一个失败插件可能使后面的检查尚未执行。因此，新出现的错误不必然是变更制造的新问题，也可能是此前未暴露的限制。

### 11.2 从故障定位到容量设计

资源或硬约束导致的调度失败，说明某个 Pod 在当前条件下没有可行位置；容量设计还要考虑发布、扩容和节点故障同时发生时的业务目标。这需要把逐节点资源计算与服务吞吐、预热时间、故障域和调度等待联系起来。

---

<a id="production"></a>

## 12. 生产里还要算什么：位置、故障流量、HPA 和等待

生产容量同时受节点位置和应用处理能力约束。下面用假设的 Java 吞吐与预热时间说明两者怎样共同影响发布和故障恢复；实际规划需要代入目标应用的测量数据。

### 12.1 先区分三种“慢”

业务说“发布等了十分钟”，先问这十分钟花在哪里：还没创建 Pod，还是创建了但没节点，还是分配节点后应用迟迟没 Ready？每段等待由不同组件负责。scheduler 自己处理变慢，也要和“它很快算出当前放不下”分开。

先建立时间线：

```text
T0 用户提交工作负载
T1 控制器创建 Pod
T2 Pod 达到可调度条件，进入负责它的 scheduler
T3 开始一次调度尝试
T4 节点绑定在 API 中可见
T5 容器启动
T6 Ready
```

T0→T1 查控制器或批任务准入；T1→T2 查 scheduling gate；T2→T4 才包含调度排队、多次尝试与绑定；T4→T6 查节点启动容器和应用预热。仅凭创建时间与 Ready 时间，分不出这几段。[P1][P2]

**Java 教学例子：**09:00:00 创建新 Pod，09:00:01 已绑定节点，09:01:31 才 Ready；同时应用日志显示 Spring Boot 启动后仍在加载缓存、做预热。能确定绑定在创建后约 1 秒完成，后面约 90 秒发生在节点和应用阶段。再查镜像、挂载、启动日志和探针，才能把这 90 秒细分；不能全部算作 Filter 耗时。

#### 12.1.1 对照观察，而不是凭一条曲线猜原因

attempt 是给一只 Pod 做的一次调度尝试；同一只可以尝试很多次。扩展点是调度框架预留的处理阶段，比如 Filter、Score；插件是在该阶段执行某类规则的程序。吞吐是每秒处理多少，延迟是某一步花了多久，两种指标要一起看。

| 同窗事实 | 较优先的假设 | 还要核对 |
|---|---|---|
| active 积压、每秒完成量下降、插件耗时上升 | 执行路径变慢 | 是哪个 profile、扩展点、插件或依赖 |
| unschedulable 积压、attempt 很快、反复资源不足 | 容量或约束不满足 | 逐节点余额和目标池，而不是只看总余量 |
| 仅某 schedulerName 的 Pod 无尝试证据 | 路由/进程/leader/gate | 对应 profile 是否加载、准入是否放行 |
| 大量 Bind 或 API error | API 路径或权限/依赖异常 | API 延迟、错误码、RBAC、限流、组件日志 |
| Pod 已有 nodeName，Ready 很慢 | 节点或应用链路 | 镜像、CNI、CSI、设备、启动探针 |

表格只是待验证假设。队列指标是聚合，不能直接指出某一个 Pod 的内部位置。

### 12.2 一共还空着 11 核，为什么只放得下一只

在线服务每只 Pod 要 `2 CPU / 4Gi`，只能进入 online 池。先假设 a、b 的标签、污点、卷等条件都通过，余额分别是：

| 节点 | CPU 余额 | 内存余额 | 只看这两项能放几只 |
|---|---:|---:|---:|
| a | 3 CPU | 12Gi | min(向下取整 3/2，12/4) = 1 |
| b | 8 CPU | 3Gi | min(8/2，向下取整 3/4) = 0 |

a 卡在 CPU，b 卡在内存。总余量 `11 CPU/15Gi` 看起来不少，但一个普通 Pod 不能拿 a 的内存和 b 的 CPU 拼起来，实际只放得下一只。

这种“每只需要多少 CPU、内存及其他资源”的组合，后文叫 Pod 规格或 shape。逐节点估算式如下，floor 表示向下取整：

```text
slots(node, shape) = min(
  floor(CPU 剩余 / 2),
  floor(内存剩余 / 4Gi),
  Pod slots 剩余,
  其他约束允许的数量
)
```

如果还有同节点反亲和或卷限制，能放的数量可能比上述结果更少。先找到满足硬条件的节点，再对它们分别计算。

这是基于采集时数据的容量估算。scheduler 的临时占账、后续资源变化和其他硬约束都可能进一步减少可行位置，因此它表示当前输入下的估计能力。[P3]

#### 12.2.1 发布窗口的容量预算

规划同时区分稳定态、首波 surge、终止重叠和故障态：

```text
需要的可行余量 ≠ 单纯 replicas × request
需要考虑：发布新增 + 尚未退出旧 Pod + 同时扩容 + 节点维护/故障
```

例如六个服务各新增一只 `1 CPU/4Gi`，首波需求是 `6 CPU/24Gi`，还必须有六个实际能放的位置。修改六个仓库不等于发生六次 rollout，要核对 PodTemplate 是否变化。旧 Pod 终止期间也可能继续占资源。[P4]

#### 12.2.2 故障预算不能只看“均匀部署”

副本均匀分布只说明正常时的位置关系。失去一个域后，存活实例需要立即承接流量，存活节点需要容纳替代副本，而硬拓扑规则仍可能限制这些副本的落点。

热实例接管、补齐副本和恢复原分布是三个阶段。它们分别受当前服务能力、可行资源余量、应用启动时间和故障域恢复进度影响；下面的算例将这些因素放在一起计算。

#### 12.2.3 N-1 容量、应用预热与发布竞争

N-1 表示失去一个指定故障单位后仍需维持目标能力。本例以可用区为单位：三个域各能放 6 只同规格 Pod，当前各运行 4 只；假设跨域网络及依赖正常，每只 Ready 实例能在延迟目标内承载 100 请求/秒，总流量为 900 请求/秒，新 Java 副本需要至少 60 秒预热。

一个域故障后，存活的 8 只实例最多承载 `8×100=800` 请求/秒，小于 900。节点即使还有空余位置，也不能让尚未启动的新实例立即接流量。故障初期的缺口需要提前准备的热实例、较高的单实例处理能力，或明确的流量降级策略承担。

从调度容量看，存活两域仍有 `(6−4)×2=4` 个空位，数量上恰好可以补齐丢失的 4 只 Pod。但这还依赖存活域满足拓扑、卷和其他硬条件，以及控制器已创建替代副本。不可用域的物理资源不能继续计入恢复预算。

补齐后，两域共运行 12 只 Pod，空位变成 `12−12=0`。此时再发起需要 3 只额外副本的发布，就没有 surge 余量；恢复与发布同时进行时，需要 `4+3=7` 个位置，而现有空位只有 4 个。

| 容量问题 | 本例结果 | 影响 |
|---|---|---|
| 立即接住 900 请求/秒 | 存活能力只有 800 请求/秒 | 需要热容量或降级策略 |
| 补齐原有 12 只副本 | 数量上可补 4 只 | 仍受硬约束和启动时间影响 |
| 同时增加 3 只发布副本 | 总需求 7 个位置，现有 4 个 | 需要暂停发布、降低并行度或增加供给 |

如果流量降为 700 请求/秒，8 只存活实例的吞吐能够覆盖它，但节点空位数量不变。如果每域容量从 6 个位置增加到 8 个，存活两域补齐后还有 4 个空位，但故障刚发生时仍只有 8 只热实例。增加调度容量与增加即时服务能力，是两个不同的改进方向。

退出过程也影响恢复。旧 Pod 收到删除请求后可能仍占资源；EndpointSlice、外部注册中心和代理规则需要各自传播状态。停止向旧实例发送新请求、处理已有连接、JVM 退出和资源归还不一定同时完成。

### 12.3 多可用区中的调度、流量与故障恢复

#### 12.3.1 四层问题分开

Pod 放在哪个 Node，由调度规则决定；流量发往哪个 Pod，由入口、Service 数据面、服务发现或客户端决定；数据库写往哪个主节点，又是另一套路由；故障实例何时被摘除，由相应健康检查与控制器决定。调度到同 AZ 不自动让调用路径同 AZ，Readiness 变化也不能被假设为所有外部注册中心同时下线。[P2][P5]

例如，客户端直接从注册中心获取 Pod IP 时，其实例摘除依赖注册中心与客户端更新；使用 Service 时，还要分析 EndpointSlice 和实际数据面的更新。两种路径不能仅凭 Pod Ready 的变化推断相同的流量切换时间。

#### 12.3.2 节点亲和与拓扑计数的联合影响

给定 zone `a/b/c`，本服务现有 Pod 数为 `1/1/0`；准备再加一只。新 Pod 的 required affinity 只允许 a/b，分布规则为 `DoNotSchedule、maxSkew=1、minDomains=1`，其他资源和污点条件都通过。

| nodeAffinityPolicy | 参与计数的域 | 最小计数基准 | 新 Pod 放 a/b 的计算 | 结果 |
|---|---|---:|---|---|
| Honor | a/b | 1 | 1+1−1=1 | 两域都通过此分布规则 |
| Ignore | a/b/c | 0 | 1+1−0=2 | a/b 超过 maxSkew；c 仍被 required affinity 拒绝 |

Ignore 让 c 参与统计，没有让新 Pod 获得去 c 的资格。**参与统计的域与实际能放的位置，要分开判断。**这里用最小计数作基准，不是拿域数当分母算平均值。[P5]

同理，nodeTaintsPolicy 决定统计时如何处理污点，不会取消 TaintToleration 对真正落点的检查。

不同命名空间的同名 label 不一定是你的同伴。matchLabelKeys 是把新 Pod 上指定标签的值加入统计条件；pod-template-hash 是 Deployment 用来区分模板版本的标签。把它加入后，不同版本的副本可能分开统计；revision 就是这里说的版本。发布期间应明确要“全服务总体分散”还是“每个版本自己分散”。字段可用性与 selector 合并行为随版本变化，先 `kubectl explain` 并查目标版本。[P5]

#### 12.3.3 节点状态变化怎样影响域计数

域内容量耗尽、节点出现拒绝污点和节点对象被删除，对新 Pod 的影响不同：

| 变化 | 对可行位置的影响 | 对拓扑计数的影响 |
|---|---|---|
| CPU 或内存余额不足 | 资源 Filter 可能拒绝节点 | 该节点所属域仍可能参与统计 |
| 节点存在但带有未容忍污点 | TaintToleration 可能拒绝节点 | 取决于 nodeTaintsPolicy 等统计规则 |
| 域内全部节点对象被删除 | 该域没有候选节点 | 处理完对象变化后，该域不再由这些节点贡献 eligible domain；minDomains 仍可能影响基准 |

因此，故障恢复策略需要同时规定正常分布和退化行为。例如，关键在线服务平时跨域部署，故障时允许在存活域增加副本，并预留 N-1 容量。单独使用硬分散规则，既不会创建备用资源，也不自动保证故障时能够补齐副本。

### 12.4 HPA、节点扩容和调度器各做什么

HPA 是根据负载自动调整 Pod 副本数的控制器，回答“应该有几只 Pod”；scheduler 为已经创建的 Pod 选节点；节点 autoscaler 是调整节点供给的程序，根据等待需求、节点类型和配置，决定是否增加或调整节点。它们分别更新状态，一步完成后，下一步还要等组件看到变化。[P6][P7]

一只 16-GPU Pod，如果所有允许的节点类型最多 8 GPU，扩十台同型节点仍无解；Pod 被 required 标签限定到一个不允许扩容的池，也不应期待另一个池的余量自动救场。

#### 12.4.1 从扩容需求到可用节点

```text
不可调度需求被发现
  → 节点供给方案允许且云配额足够
  → 实例创建并加入
  → 节点基础组件就绪
  → labels / taints / allocatable 满足这组 Pod 的要求
  → 设备或存储资源可用
  → scheduler 看到新事实
  → Pod 绑定并启动
```

Node Ready 并不自动保证 GPU 资源已上报、业务镜像已缓存、特殊网络已准备完成。成本优化应区分冷启动能力与热余量；业务允许的等待短于供给准备链时，仅“出现 Pending 后再扩”无法满足目标。

#### 12.4.2 降 request 会同时改变多个系统的输入

它可能让更多 Pod 通过资源 Filter；若 HPA 使用 CPU 利用率百分比，相同 CPU usage 对更小 request 的比例也会变大；节点扩缩容的装箱判断也可能变化。这些影响应一起评估，不能只看调度面板变绿。[P3][P6][P7]

#### 12.4.3 一个降低 request 后 HPA 反而想扩容的算例

教学条件：2 只 Pod 都已 Ready，CPU 指标完整，每只持续使用 500m；HPA 目标为 CPU 平均利用率 60%。先忽略容差、稳定窗口、缩放速率限制和副本上下限，只计算原始建议。[P6]

| 每只 CPU request | 平均利用率 | 原始副本建议 |
|---|---:|---:|
| 1000m | 500/1000 = 50% | ceil(2 × 50/60) = 2 |
| 500m | 500/500 = 100% | ceil(2 × 100/60) = 4 |

ceil 表示向上取整。实际仍用 500m，但 request 从 1000m 改为 500m 后，利用率就从 50% 变成 100%。所以单只更容易放下，HPA 的原始计算却想要更多副本。

实际变更通常通过滚动更新逐步生效，新旧 Pod 的 request 可能暂时不同。HPA 还会考虑容差、稳定窗口等条件，所以表中的 4 是原始建议，不是承诺“改完立刻扩成四只”。

Utilization 是相对 request 的使用百分比；AverageValue 是每只 Pod 的平均实际指标值，例如 500m。容差是小幅波动时先不调整；稳定窗口是参考一段时间的建议，避免副本数来回跳。采用哪种指标目标，会改变降低 request 后的计算结果。

分析这类联动需要同时读取最终 Pod requests、HPA 的指标目标类型、各副本有效指标、HPA conditions 与期望副本。单独一张 CPU 使用率曲线无法区分“实际负载提高”和“request 变小导致百分比提高”。

### 12.5 可观测性：把成功者和仍在等待的人都算进去

以下指标来自 Kubernetes scheduler 指标体系，源码定义位于固定提交的 `pkg/scheduler/metrics/metrics.go`。指标名称、label 和稳定性随版本演进，托管平台也可能只暴露其中一部分。查询示例使用单集群数据源；共享数据源需要增加集群选择条件。[P8][P13]

metrics 就是采集出的数字，`/metrics` 是组件提供这些数字的入口。指标 label 是用来分组的标记，例如 profile、result，和 Pod 的标签各有自己的用途。PromQL 是从 Prometheus 监控数据中查询、计算这些数字的语言。

#### 12.5.1 最小五张图

查询中的 `rate(...[5m])` 用最近五分钟的计数变化估算每秒增量。直方图把耗时按范围统计，bucket 是一个范围桶，le 标明“累计统计小于等于这个上界的样本”。p99 是约 99% 的样本不超过的耗时；`histogram_quantile(0.99, ...)` 从这些桶估算它。

例如 p99 为 2 秒，是说所统计的样本约 99% 不超过 2 秒；不是平均每只等 2 秒，也没有自动把一直没结束的等待者算进去。topk(10, ...) 是取数值最大的十组，sum by 是按指定标记分组求和。

```promql
sum by (queue) (scheduler_pending_pods)
```

```promql
sum by (result, profile) (rate(scheduler_schedule_attempts_total[5m]))
```

```promql
histogram_quantile(0.99,
  sum by (le, result, profile) (
    rate(scheduler_scheduling_attempt_duration_seconds_bucket[5m])
  )
)
```

```promql
histogram_quantile(0.99,
  sum by (le, extension_point, status, profile) (
    rate(scheduler_framework_extension_point_duration_seconds_bucket[5m])
  )
)
```

```promql
topk(10, sum by (plugin, profile) (scheduler_unschedulable_pods))
```

每块是一条独立查询。扩展点延迟保留 status 维度，避免成功与错误路径的耗时互相掩盖。直方图聚合要保留 le；不能把单机 p99 再求平均当集群 p99。一个 Pod 可能被不同失败方向统计或在多次尝试中改变原因，插件维度不能简单相加当唯一 Pod 总数。指标并不自动提供每个 Pod 的 UID、业务等级和等待年龄。[P8][P9]

#### 12.5.2 成功者偏差

假设 40 个 Pod 在 0.2 秒绑定，另外 60 个一直无可行节点。只观察成功完成的延迟，可能得到漂亮的 p99，却遗漏六成等待者。于是应分开：

**调度器服务质量：**内部 error 比例、执行延迟、leader 与 API 路径。

**用户可调度体验：**进入责任范围的所有 Pod 中，多少在目标时限内绑定，多少仍等待，按业务类型和等待原因分层。

上面的 attempt 直方图统计已结束的单次尝试，按 result 分组可看到失败尝试；即使把所有 result 都画出，它仍不包含完整排队时长，也不按唯一 Pod 去重。

SLI 是用来衡量服务好不好的一个实际数值，本例可以用“30 秒内绑定的 Pod 占比”。用户等待 SLI 需要外部对象观察器或事件/状态存储补齐未完成样本。要明确开始时点、观察窗口、删除/取消如何处理、gated/批任务未准入是否在分母内。不能通过排除所有失败者来制造高成功率。

#### 12.5.3 告警模板

```text
现象：online/default-scheduler 中不可调度数量及最老年龄持续增长
影响：哪些 workload class，是否影响发布或故障补副本
主要证据：失败方向、同窗请求账、目标节点池
首查：确认资源不足还是约束交集为空
责任：业务合同 / 平台容量 / scheduler 内部错误分别路由
禁止自动操作：不因 Pending 就删健康旧副本或提高 priority
```

阈值应依业务目标和历史分布制定。本文不把“Pending>0”或“p99<1s”规定为所有集群统一标准。

#### 12.5.4 100 只 Pod，只有 40 只按时绑定，达标率怎么算

先定好目标：交给指定 scheduler 处理的 Pod，要在 30 秒内完成 API 绑定。记录每只 UID 何时进入统计范围，等它观察满 30 秒再结算。刚提交 1 秒的 Pod 还没超时。

一批共有 100 个 UID：40 个在 1 秒内绑定，20 个在第 45 秒绑定，40 个到第 60 秒仍无节点。在没有取消且指标完整的前提下，30 秒达标率为 `40/100=40%`，而不是 `40/60`。迟到成功可以另报，但不能回写成“30 秒内成功”。

一只 Pod 重试 20 次，也只对应这个分母里的 1 个 UID。尝试次数和每次执行耗时用于解释为什么等，不能替代用户等待时长。删除后同名新建会产生新 UID，需要工作负载层关联，避免通过反复重建掩盖等待。

指标缺失、观察器重启、取消和准入等待需要各自规则；无法恢复起点的样本标为未知并报告覆盖率，不能静默当成功。这个计算示例是教学 SLI 设计，不是假设 scheduler 原生已经暴露了这张按 UID 的表。

### 12.6 同名 scheduler、多个 profile 与多个进程

#### 12.6.1 名字决定谁负责，不创造一套调度器

写入 `spec.schedulerName` 不会自动部署对应组件。Pod 路由名拼错，或负责它的实例无可用 leader，就可能没有正常调度尝试。托管控制面不可见时，不能因 `kubectl get pod` 搜不到名字就断言组件不存在。[P10]

同进程多个 profiles 共用队列和 cache，QueueSort 必须一致；各 profile 可配置不同插件参数。`addedAffinity` 是用户 Pod YAML 未必显示的附加条件，所以平台界面应公开其含义。[P10]

#### 12.6.2 两个独立调度器最大的隐藏边界

假设只剩 1000m，调度器 A 先在自己的账上为 Pod-a 记入 1000m，但绑定还没写进 API。独立调度器 B 看不到 A 的这笔临时账，就可能仍以为这 1000m 空着。两个进程都监听 API，也不会自动共享尚未绑定的 Assume。

所以独立 scheduler 共用同一节点池时，要验证重复承诺、节点侧拒绝及恢复路径。只换 schedulerName，并没有把资源分开。

可以把节点池分开，也可以设计明确的分配协调规则；需求能满足时，还可以采用同进程的多个 profile。选择时再比较故障影响范围、升级方式和资源类型，不必因为名字不同就部署两个独立进程。

Lease 是保存“当前谁负责、何时续约”等信息的对象；leader election 就是多个副本靠它等机制协调出当前负责人。共享同一 Lease 的主备副本是高可用部署：一个实例故障后由其他实例接手。没有协调协议却同时认领同一 Pod 集的两个调度系统，可能发生竞争。[P10][P11]

#### 12.6.3 定制扩展的决策顺序

先确认原生 YAML 能否表达，再评估 profile 参数；只有能力缺口明确时才进入 Framework 插件或 Extender。Framework 插件需注册并构建进二进制，不是随便放一个动态库即可热加载。Extender 是外部调用，增加网络、超时与失败语义问题；`ignorable` 与资源责任不能脱离讨论。[P1][P10]

Extender 是调度器通过网络请求的外部扩展服务；Framework 插件则在调度器内部执行。二进制是编译后的可执行程序；热加载是程序不重启就换入新代码，不能假定这些插件支持。ignorable 用来规定扩展调用报错时是否允许跳过，还要核对具体调用路径及它负责的资源。

定制插件需要明确数据来源、更新频率、临时状态的维护者和失败补偿路径。例如，每秒按利用率修改节点标签，会增加 API 写入压力，调度器看到的数据仍存在传播延迟。只有同时分析这些成本和放置收益，才能判断该方案是否适合目标负载。

### 12.7 调度变快以后，放置结果还对不对

可行节点比例会影响遍历工作量与评分候选集合。缩小候选范围可能降低耗时，也可能牺牲放置质量；扩大并行度可能转移瓶颈到 CPU、锁或外部依赖。配置项是否生效与默认值都应固定到目标版本，不从开发快照直接复制。[P12]

性能比较需要固定 Pod/Node 输入和插件配置，同时观察硬约束通过率、绑定成功率、attempt 吞吐、p99、插件耗时、节点分布及资源碎片。只有输入与要求一致，才能区分执行路径变快和约束放宽带来的变化。

关闭 Filter、给所有污点加 toleration、降低 requests，都会改变原来的要求。如果业务确实要调整这些条件，另做配置变更和验收；调度性能比较应使用同一组输入和要求。

### 12.8 配置变更和回滚不等于 Pod 自动回家

新 Score 配置影响后续选点。回滚配置后，尚未绑定的 Pod 将在后续尝试中使用恢复后的规则；已经绑定的 Pod 保留现有落点，迁移仍需控制器重建或维护驱逐流程。[P10]

因此，变更结果包括新副本分布、故障域集中程度、节点负载和后续大规格 Pod 的可放置能力。调度器进程健康只能说明组件仍在工作，不能说明此前形成的分布已经恢复。

### 12.9 容量、可靠性与成本的取舍

调度方案往往需要同时满足多个目标。可行节点数增加不一定意味着业务能力提高；调度耗时降低，也不意味着放置结果更合理。

| 方案变化 | 可能获得的收益 | 需要同时考虑的代价 |
|---|---|---|
| 增加符合约束的热容量 | 缩短发布和故障恢复等待 | 平时保有资源的成本 |
| 缩小发布批次 | 降低新旧副本重叠需求 | 全部服务完成更新所需时间 |
| 放宽故障期间的跨域硬约束 | 提高存活域的可放置能力 | 副本集中后的故障影响 |
| 使用资源装箱评分 | 留出较完整节点供大规格 Pod 使用 | 热点、带宽竞争与故障集中 |
| 缩小评分候选范围 | 减少调度计算量 | 可能遗漏更合适的落点 |

判断方案是否有效，需要保持比较口径一致：同一组 Pod 规格、节点资源、硬约束和业务目标。如果同时降低 request、放宽亲和并修改插件，很难仅凭等待变短确定收益来自哪项改变。

容量设计需要覆盖后续发布、节点维护和故障恢复。第一批 Pod 成功绑定，只反映了其中一个时刻的供需关系。

---

<a id="gpu"></a>

## 13. 从 Java 接到 GPU：还要增加哪些判断

GPU 工作负载仍受 CPU、内存、节点池和拓扑等条件限制，同时新增设备种类、健康、具体设备分配和显存问题。下面先说明传统 Device Plugin 路径，再解释共享、DRA 与批任务队列；配置和数字用于说明机制，实际能力取决于设备、驱动及组件版本。

### 13.1 “还有几份 GPU”和“实际用了哪张卡”分开查

Pod 已绑定只说明节点选择已经完成；GPU 的具体分配、节点准备和应用使用仍可能失败。传统 Device Plugin 路径中，scheduler 的数量检查与节点侧的设备准备是两个阶段。

GPU 是擅长并行计算的处理器。传统 Device Plugin 是节点上的设备插件，向 kubelet 报告设备数量、健康状态，并协助准备设备。scheduler 先看资源名和申请数量，节点侧再准备实际设备。从上到下读下面的步骤；箭头表示工作先后，不表示组件间同步调用：

```text
驱动/设备插件发现设备
  → kubelet 通过注册与 ListAndWatch 持续获得设备列表及健康状态
  → Node status 发布 capacity / allocatable
  → scheduler 根据扩展资源 request 选择 Node
  → 本地 Assume、插件预留与 API Binding
  → 目标节点 kubelet 与设备插件协作分配/准备具体设备
  → 运行时注入：把设备入口及必要配置交给容器，容器启动
  → CUDA 计算环境与业务验证
```

Node 上报 `nvidia.com/gpu: 8`，默认资源 Filter 可以用这个数量算余额，却不能仅凭 8 知道具体卡的 UUID、显存、NVLink 距离或温度。选中节点以后，kubelet 和设备插件还要分配、准备实际设备。第 13.4 节的 DRA 会让设备分配参与调度，所以这段只讲传统 Device Plugin。[G1][G2]

驱动是负责与硬件打交道的系统软件；CUDA 是应用使用 NVIDIA GPU 做计算的软件平台。UUID 是用来核对某个具体设备或实例的唯一标识；显存是 GPU 使用的内存，和第 3 章的节点内存分开看。NVLink 是 GPU 之间的高速连接，连接关系会影响通信，名字本身不代表任务已用了最合适的设备。

#### 13.1.1 三个层次的成功

| 层次 | 最小证据 | 仍未证明 |
|---|---|---|
| 调度成功 | Pod 有预期 nodeName，相关资源请求已进入分配账 | 具体设备是否准备完成 |
| 设备可用 | 容器看到预期设备，驱动/运行时准备成功 | CUDA 镜像兼容与应用逻辑 |
| 业务达标 | 应用验收、通信与性能符合目标 | 长期故障隔离、容量和成本最优 |

如果已经绑定后设备准备失败，原 Pod 不会由默认 scheduler 随意改写 nodeName 去另一台机器。先判断是不是终态失败、是否由 Job/Deployment 管理、是否允许从 checkpoint 恢复，再按流程处置；删除重建前还要确认训练状态和 checkpoint，不能只看新 Pod 是否起来。

checkpoint 是应用保存的训练进度和必要状态，恢复时从那里继续。Job 是管理有限任务的控制器；Pod 到 Succeeded 或 Failed 等终态，表示这次运行已结束。训练是用数据调整模型；推理是用已有模型对新输入给出结果，两者的启动和性能验收可能不同。

#### 13.1.2 先看真实资源表达

先核对目标 context，下面沿用第 2.1 节的 `CTX`。要查的是 Node 上报多少、最终 Pod 申请多少，以及是否使用 DRA 对象：

```bash
kubectl --context "$CTX" get nodes -o custom-columns='NAME:.metadata.name,CAP:.status.capacity.nvidia\.com/gpu,ALLOC:.status.allocatable.nvidia\.com/gpu'
kubectl --context "$CTX" get pod -n ml-lab GPU_POD_NAME -o yaml
kubectl --context "$CTX" api-resources --api-group=resource.k8s.io
```

传统 Device Plugin GPU 资源按整数申请：可以只写 limits，request 默认等于 limit；两者都写时必须相等，不能只写 GPU request 而不写 limit。下面申请一个调度资源单位，其究竟是整卡还是共享份额，还要核对插件配置。[G12]

```yaml
resources:
  requests:
    cpu: "2"
    memory: 8Gi
  limits:
    nvidia.com/gpu: 1
```

Node allocatable 是可供分配的容量，不是扣除已占请求后的空闲量。allocatable 为 8，现有 Pod 请求 6，数量账余额才是 2；还要检查 CPU、内存、临时存储、节点池等条件。

8 张独占 GPU 中一张被插件报告 unhealthy（设备不健康），kubelet 会减少 GPU allocatable，capacity 不因此减少。已使用故障卡的 Pod 不会自动换卡或重新调度。应对齐 UUID、插件健康报告、Pod 使用设备和应用错误；不能因 capacity 仍为 8 就判断故障报告未生效。[G1]

Pod 要 `2 GPU/32Gi`。a 剩 2 GPU，但内存只剩 8Gi，差 24Gi；b 剩 64Gi 内存，但没有 GPU。因此两台都放不下。再看另一个输入：四台各余一份 GPU，也放不下单个要四份的普通 Pod。分布式训练要用多个 Pod 和相应训练架构，调度器不会替你拆分一个 Pod。

### 13.2 “1 GPU”不一定是同一种产品

| 模型 | Kubernetes 常见表达 | 应对业务明确说明 |
|---|---|---|
| 整卡独占 | 厂商扩展资源整数 | 整卡类型、容量与独占边界 |
| MIG：把支持的 GPU 划成有各自计算、显存资源的小实例 | 随 strategy 暴露 GPU 或 MIG profile 资源 | profile、父卡关系、硬件分区与重配影响 |
| time-slicing：多个任务共享、轮流使用同一物理卡 | 复制的逻辑资源份额，可能重命名 `.shared` | 非独占、显存与性能隔离限制 |
| DRA：通过设备申请与供给对象协作分配 | 申请对象、驱动发布的设备列表及版本相关桥接 | 驱动声明的设备属性、容量和分配规则 |

MIG 与时间切片不是“实现方式不同但效果一样”。MIG 提供硬件分区；time-slicing 的副本份额不等于同等份额的独立显存，也不能承诺恒定比例性能。[G3][G4]

MIG profile 是“小实例切成什么规格”，与调度器 profile（一套调度规则配置）含义不同。父卡就是这些小实例来自的物理 GPU；strategy 是设备插件选择怎样公布资源的策略。资源碎片是余量分散成不合规格的小块，例如只剩小实例，却要申请大实例。

#### 13.2.1 有 4 张卡，为什么 Node 显示 16 份

若每卡暴露4份逻辑份额，调度器可能看见16。要确认该模型，必须把物理库存、实际设备插件配置、ConfigMap挂载/选择、节点标签与插件启动日志关联起来。不能仅看到一个名为 timeSlicing 的 ConfigMap，就断言目标节点已加载。

ConfigMap 是 Kubernetes 保存的一份配置内容；挂载是把这份内容交给容器里的文件路径。这里的“逻辑份额”是插件对外公布的可申请次数，不能直接当成新增物理卡、独立显存或固定比例算力。

例如四张卡，每张暴露四份，Node 可分配量是 16；已有 Pod 请求 12，请求余额就是 4。这个数字没有告诉你哪张卡显存还够，也没证明新任务能达到吞吐目标。物理卡数、逻辑份数、已请求份数、GPU 利用率和显存分别记录，排队和性能才不会混在一起。

**请求两份为什么不等于两倍算力？**time-slicing 的 replicas 增加访问份额，并未建立按 request 比例兑现的独立算力或显存配额。申请 2 份不保证比申请 1 份获得两倍计算时间。若实际配置启用 `failRequestsGreaterThanOne: true`，超过 1 份的请求还可能在节点准入阶段失败，出现 UnexpectedAdmissionError；已绑定不能证明 GPU 可用。[G3]

`renameByDefault: true` 可将资源暴露为 `nvidia.com/gpu.shared`；未开启时共享资源仍可能叫 `nvidia.com/gpu`。平台规格必须明确独占或共享，不能仅展示资源名和数量。

#### 13.2.2 MIG 碎片要按 profile 看

多个小profile空闲，不代表一个大profile立即可用。profile重新组合需要设备与驱动支持，还可能影响现有工作负载。应提供受控节点分组、变更窗口、目标profile余量与重配前影响清单，而不是让默认scheduler“自动把碎片拼起来”。[G4]

single 策略下 MIG 实例仍可通过 `nvidia.com/gpu` 表达；mixed 策略暴露类似 `nvidia.com/mig-1g.10gb` 的 profile 资源。具体名字和尺寸取决于 GPU 型号、布局与插件策略。[G4][G13]

MIG Manager 重配流程会终止相关 GPU Pod，部分场景还涉及重启。执行前确认 checkpoint、影响范围、窗口和回滚配置；之后核对 MIG 配置状态、Node 实际资源、实例 UUID 和业务运行。一个 `mig.config` 标签只是期望配置。[G4]

#### 13.2.3 节点间拓扑与节点内拓扑

zone/hostname 回答“Pod 放在哪些节点域”；NUMA、PCIe、NIC、NVLink 描述“同一节点内设备怎样连接”。CPU Manager、Topology Manager、设备插件或 DRA 驱动、应用通信库可能分别影响分配和运行结果。看到设备连接关系以后，还要核对实际 UUID、容器可见设备和通信测试，才能解释性能问题。[G1][G2]

| 专业词 | 含义 |
|---|---|
| NUMA | 一台机器的 CPU、内存可能分成几组；访问靠近自己的内存通常更快，跨组访问可能付出额外代价。 |
| PCIe | CPU 与显卡、网卡等设备连接的通道；共享通道与连接层级会影响数据传输。 |
| NIC | 网卡，训练成员跨机器传数据时会用到。 |
| NVLink | GPU 之间的高速连接；有连接不代表所有 GPU 之间距离都相同。 |
| CPU Manager | kubelet 里按策略管理 CPU 分配的部分；静态策略可给符合条件的容器分配固定 CPU。 |
| Topology Manager | kubelet 里汇合 CPU、设备等位置要求、按策略判断能否对齐的部分；不是跨可用区分散规则。 |

训练中的 worker 指任务成员，通常由 Pod 承载；Kubernetes 工作节点则是 Node。四个训练成员可能分布在不同数量的 Node 上，二者不能直接换算。

### 13.3 默认能检查GPU数量，不代表默认按GPU装箱

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

#### 13.3.1 手算一个纯GPU子项

两台节点 GPU allocatable 都为 8，现有请求为 1 和 6，新 Pod 请求 1。加入新请求后，占比是 2/8=25%、7/8=87.5%；固定实现采用整数运算：[G14]

```text
GPU 子项 = (已有请求 + 新请求) × 100 / allocatable
A：2 × 100 / 8 = 25
B：7 × 100 / 8 = 87
```

87 是整数除法结果。假定 CPU、内存子项均为 50，权重 CPU=1、内存=1、GPU=5，则 NodeResourcesFit 得分为：

```text
A：(50 + 50 + 25×5) / 7 = 32
B：(50 + 50 + 87×5) / 7 = 76
```

Framework 随后还会乘整个插件的权重、汇总其他 Score 插件。这能复核 NodeResourcesFit 的贡献，不能单独保证 B 获选。

相反，LeastAllocated对这个子项倾向空余更多的节点。装箱可能留下完整空节点方便大任务，但也可能集中热量、带宽与故障影响；在线关键推理与可恢复批任务不必采用同一策略。

#### 13.3.2 三层评分不能混

资源在 NodeResourcesFit 内部的权重、Score 插件的归一化处理、整个插件在 Framework 中的权重，是不同层次。固定源码中 NodeResourcesFit 的 `ScoreExtensions()` 返回 `nil`，不执行该插件的 NormalizeScore；其评分仍会参与框架的加权汇总。分析最终落点时，需要结合各插件的配置和分数，单个落点不能反映完整评分过程。[G5][G6]

固定实现会跳过新 Pod 没有请求的扩展资源评分项。因此，GPU 权重为 5 并不意味着不申请 GPU 的 Pod 也会按同样方式装箱。需要根据最终 Pod 请求和 schedulerName 判断具体参与评分的资源。[S20]

### 13.4 DRA 改变了什么，没改变什么

Claim 就是一份设备申请；Class 是选择哪类设备的规则；Slice 是驱动分批公布的设备清单。allocation 是申请已经得到的分配结果，不是应用已经成功使用设备的证明。DRA driver 是把设备供给发布到 API、并在节点准备和清理设备的软件。

DRA 不只把设备数写在 Node 上，而是让驱动发布可用设备，再让应用用 Claim 申请。调度过程可以参与设备选择与分配；选好以后，节点驱动还要准备设备。Claim 已有 allocation，只说明已有分配结果，应用是否能用还要继续查。[G2]

```text
供给：驱动发布Slice及设备属性
请求：Pod关联Claim或相应资源表达
调度：DynamicResources参与可行性和分配
持久化：记录分配与消费者关系，完成Pod节点绑定
节点：驱动准备资源，运行时注入设备，应用尝试使用
```

它能使用的是驱动通过API声明的事实，不会自动知道任意实时GPU性能，也不自动提供最优NVLink布局。

在 v1.34 DRA API 中，ResourceSlice 是驱动公布的供给；DeviceClass 定义设备选择规则；ResourceClaim 是具体申请；ResourceClaimTemplate 用于为每个 Pod 生成独立 Claim。[G15]

两个推理副本各要一块设备，应理解“每个 Pod 从模板得到自己的 Claim”。若两只显式引用同一个 Claim，表达的是共同使用其分配结果，不会自动变成各申请一块；能否共享还受实际资源和驱动能力约束。

显式创建的 Claim 由创建方管理生命周期；模板生成的 Claim 与对应 Pod 生命周期关联。因此删掉 Pod 不保证设备立即可用，还要看 Claim、分配、消费者和驱动清理结果。

排查按顺序走：Pod 引用谁 → Claim 是否同命名空间且存在 → Class 是否匹配供给 → allocation 是否生成 → Pod 是否绑定 → 节点驱动是否准备成功。API discovery 是询问服务器“提供哪些资源接口”；它只证明 API 被服务，不能证明设备供给链正常。

#### 13.4.1 DRA 的版本与驱动依赖

GA 表示功能进入上游稳定阶段，Beta 表示还处在测试发布阶段；这些是功能成熟度标签。feature gate 是功能开关。它们没有替你检查云厂商是否开放、开关是否启用、驱动是否安装，所以仍要核对下面的实际环境信息。

DRA 能力由 Kubernetes 版本与发行版、功能开关、scheduler 插件及驱动共同决定。API discovery 显示接口可用，ResourceSlice 显示供给已发布，Claim allocation 显示分配已形成，节点准备状态则反映设备能否交给容器。这些信息分别对应不同阶段。

本文固定源码包含扩展资源到 DRA 的桥接路径。具体部署使用该能力时，同一个资源名可能经传统scalar或DRA路径处理；应根据目标版本、DeviceClass和实际Node供给分流，不能因Node上无传统GPU scalar就立即判设备插件损坏。

scalar 在这里就是“一个资源名对应一个数量”的数值账，比如 GPU=8；桥接是让原先按数量表达的申请接到 DRA 分配路径，具体是否支持、怎样处理以目标版本为准。

DRA 下的抢占、优先请求、可消耗容量和设备绑定条件，都要按版本核对。不能仅凭某个开发快照断言以后始终不支持，也不能承诺提高 Pod priority 就能拿到别人 Claim 的设备。具体能力取决于目标版本实现、驱动规则及设备行为。[G2]

#### 13.4.2 两份设备请求与一份可用供给

以下字段示例使用 v1.34 API。假设一个推理 Pod `infer-demo` 位于 `ml-lab`，已限定到 worker-a；CPU、内存及其他普通硬条件通过。它引用同 namespace 的 `infer-gpu` Claim，要求两个不同的设备。管理员已核对：这个案例中只有一个匹配供给池，清单完整，其中只有一个空闲设备；没有共享分配或其他候选池。

在这些条件下，设备数量 `1 < 2`，分配器无法满足整个请求，Pod 在节点选择阶段就可能被拒绝。这与已经完成分配、随后节点准备设备失败，是两种不同情况。

下面四段字段分别来自 Pod、ResourceClaim、DeviceClass 和 ResourceSlice，只保留用于解释引用关系与数量检查的部分。ResourceClaim 的请求在 `requests` 下通过 `exactly` 表达。[N13]

```yaml
# Pod：把 gpu-input 这个本地引用名接到 infer-gpu Claim。
metadata:
  name: infer-demo
  namespace: ml-lab
spec:
  nodeSelector:
    kubernetes.io/hostname: worker-a
  resourceClaims:
  - name: gpu-input
    resourceClaimName: infer-gpu
  containers:
  - name: inference
    resources:
      claims:
      - name: gpu-input
```

```yaml
# ResourceClaim，apiVersion 为 resource.k8s.io/v1。
metadata:
  name: infer-gpu
  namespace: ml-lab
spec:
  devices:
    requests:
    - name: cards
      exactly:
        deviceClassName: gpu-lab
        allocationMode: ExactCount
        count: 2
status: {}  # 当前示例尚无 allocation。
```

ExactCount 的意思是“要满足指定数量”，count=2 就是两个不同设备，不能因为只剩一个就默默少给一份。这里的 `cards` 是 Claim 中这条请求的名字；`gpu-input` 是 Pod 中的引用名，两者职责不同。

```yaml
# DeviceClass：只选择这个驱动公布的设备。
metadata:
  name: gpu-lab
spec:
  selectors:
  - cel:
      expression: 'device.driver == "gpu.example.com"'
```

CEL 是写筛选表达式的一种语言；这里整句就是“设备来自 gpu.example.com 驱动”。这个名字只是教学标识，不意味着节点已装有真实驱动。

```yaml
# ResourceSlice：worker-a 可使用的完整池，当前只有一个设备。
spec:
  driver: gpu.example.com
  pool:
    name: worker-a
    generation: 1
    resourceSliceCount: 1
  nodeName: worker-a
  devices:
  - name: gpu-0
```

pool 是驱动组织的一组设备供给。`resourceSliceCount: 1` 表示这一代完整池应有一份清单；不能只看见某一份 Slice 上有一台设备，就断言整个集群只有一台。`gpu-0` 是驱动清单里的设备名，也不能直接当成 NVIDIA 的硬件 UUID。

这些关联可通过以下只读查询查看；context、namespace 和名称应对应实际对象：

```bash
kubectl --context "$CTX" get pod infer-demo -n ml-lab -o yaml
kubectl --context "$CTX" get resourceclaim infer-gpu -n ml-lab -o yaml
kubectl --context "$CTX" get deviceclass gpu-lab -o yaml
kubectl --context "$CTX" get resourceslices -o yaml
kubectl --context "$CTX" describe pod infer-demo -n ml-lab
```

按下面顺序读查询结果，每一行都先核对实际字段，再下结论：

| 对象信息 | 在本例中的含义 | 结论范围 |
|---|---|---|
| Pod 的引用指向同 namespace 的 infer-gpu | 找到了本次具体申请 | 不能只查一个同名但不同 namespace 的 Claim |
| Claim 要 gpu-lab，ExactCount=2 | 请求是两份，不能按一份计算 | 不能假定 count=2 一定等于两张物理整卡，资源含义仍由驱动声明 |
| Class 匹配该驱动，完整供给池只有 gpu-0 | 在上述限定条件下，1 < 2，数量不足 | 单个 Slice 的局部输出不等于完整库存 |
| Claim 没有 allocation，Pod 没有 nodeName | 还没形成设备分配与节点绑定 | 缺 allocation 本身不足以证明数量不足，还可能是 Class、选择器等问题 |
| Pod 失败原因与设备分配对应 | 才把请求、供给与调度结果串起来 | 不凭一个 Pending 就去重启节点驱动 |

v1.34 的 DynamicResources.Filter 在分配器没有为全部 Claim 找到结果时，会返回包含 `cannot allocate all claims` 的拒绝结果；它是目标版本可能出现的消息片段，不是所有版本统一的完整 Event。[N14] 数量不足的结论来自完整的请求与供给信息；失败消息用来确认该问题确实影响了本次调度。

同样条件下，如果请求只需要一个设备，数量这一项就能满足；Class、节点条件及其他占用仍需满足。ResourceClaim 的设备请求不能作为普通可变字段任意修改，需要按目标 API 的生命周期和更新限制处理。

如果 Claim 已有 allocation、Pod 已有 nodeName，但容器仍未启动，分配与节点选择已经完成。此时应根据分配结果中的 driver/pool/device，检查节点驱动准备和容器运行时；仅凭容器未启动，无法判断为 GPU 数量不足。

### 13.5 Kueue、Volcano 与 kube-scheduler 不是三个同义词

Kueue 关注工作负载准入、配额与队列；Volcano 可以承担面向批任务的 Pod/PodGroup 节点调度；kube-scheduler 负责其所处理 Pod 的节点选择和绑定。三者组合时，需要明确各层负责的状态和调度决策。[G7][G8]

这里的工作负载准入是任务先排队、获准占用额度后再推进运行；第 1 章的 API 准入是保存某个对象前的检查与补充。PodGroup 是把一批 Pod 标为同组、并写出最少需要几名成员等要求的对象。批任务是做完一批工作后结束的任务，例如训练或离线计算。

#### 13.5.1 Kueue 不能只用“管总配额”概括

先用一个申请四个 worker 的任务理解：LocalQueue 是它在命名空间里的排队入口；ClusterQueue 管配额和策略；ResourceFlavor 说明资源条件，例如节点类型或池标签；Workload 记录这份待准入任务。看到 QuotaReserved 或 Admitted，只能说明到了相应准入阶段，还要继续看四只 Pod 是否绑定和启动。[G7]

QuotaReserved 表示任务已占入配额账，Admitted 表示任务已满足该层准入条件、获准推进运行。它们是任务状态，不等于每只 Pod 的 Ready。WorkloadPriorityClass 保存任务层优先级；Pod PriorityClass 保存 Pod 层优先级，分别影响各自的排队和处理。平台需要分别展示这两层含义，具体回退或推导规则取决于安装版本。[G10]

开启拓扑感知调度（TAS，Topology-Aware Scheduling）时，Kueue 还会基于节点及拓扑域容量检查放置并分配拓扑，也就是先检查任务在所需节点分组里能否放得下。即便如此，也要区分“准入时通过放置检查”与“后来每个 Pod 已经绑定并 Ready”；节点健康、其他占用与准备过程还会变化。[G9]

#### 13.5.2 gang解决的是成员协调，不是让所有进程同一纳秒启动

一个训练任务需要足够成员才能工作，逐Pod抢到少量资源却永远凑不齐时，需要all-or-nothing或gang相关策略。Volcano gang插件依赖实际PodGroup、最小成员与插件配置，不能把“安装Volcano”理解为全部批调度策略自动启用。[G8]

all-or-nothing 是“满足整组条件再推进，否则撤回或等待”；gang 是按一组成员协调调度。产品可能在准入、预留或绑定等待等不同位置实现，不能只见这个名字就认为所有进程同时启动。

Kubernetes 原生分组调度能力也在随版本演进。目标集群能否使用相关功能，取决于该版本的 API、功能开关和实现成熟度；选型还要比较故障恢复、监控、升级与维护成本。[G11]

**四成员任务的三种等待位置。**任务需要 4 个 worker 才能训练，却只有 2 个 Running、2 个 Pending：

- 普通配额准入通过后，节点碎片或硬约束仍可能阻止全部 Pod 落地。
- TAS 在准入时检查拓扑域内的放置能力并记录分配，之后仍有绑定与启动过程。
- Kueue `waitForPodsReady` 在准入后检查超时；未在期限内达到所需 Ready 状态时，按配置驱逐、释放配额并回队。它允许短暂部分运行，不能等同于所有 Pod 原子绑定。参见 [Kueue All-or-nothing](https://kueue.sigs.k8s.io/docs/concepts/all_or_nothing/)。

Volcano gang 需要核对 schedulerName、PodGroup 与插件配置。业务需要全部 4 个 worker，就不能随意把最低成员要求降到 2；即使满足组调度条件，镜像、设备、网络也不会同时准备好。平台应区分“未准入”“已准入但放置失败”“未 Ready 导致回队”，以便找对责任方。

#### 13.5.3 平台应显示哪四个状态

```text
等待工作负载准入
已准入，部分Pod还没绑定
成员已绑定，但设备/镜像/网络尚未Ready
工作负载正在运行或已失败
```

若同时使用Kueue与Volcano，明确外层准入、gang、配额、公平、抢占、失败回队分别由谁负责；避免两层各自驱逐、各自保留份额却缺少一致的资源释放协议。

---

<a id="source"></a>

## 14. 源码实现：从资源不足到重新调度和绑定

第 3.2.2、3.3.2 和 8.3.1 节分别解释了资源检查、默认请求和端口检查。本章沿同一个等待中的 Pod，连接这些判断与调度入口、队列、缓存和绑定流程。

<a id="cpu-source-walk"></a>

### 14.1 跟同一只 201m 的 Pod：第一次失败，后来为什么成功

以下源码使用第 0.1 节的固定提交。设 Pod `one-over` 只有一个容器，CPU request 为 201m，通过节点标签限定到 worker-a；内存等其他条件都满足，没有 scheduling gate，也没有可供它抢占的低优先级 Pod。

worker-a 的 CPU Allocatable 为 5000m，系统 Pod 请求 100m，已有 Pod `holding` 请求 4700m。holding 退出以后，资源和状态按以下过程变化：

| 阶段 | 目标节点已请求 CPU | 余额 | one-over 的结果 |
|---|---:|---:|---|
| holding 已计入请求 | 系统 100m + holding 4700m = 4800m | 200m | 201 > 200，无法通过 CPU 检查 |
| holding 的删除变化被正常处理 | 系统 100m | 4900m | 下一次尝试时，201 ≤ 4900 |
| one-over 被记入请求账 | 系统 100m + one-over 201m = 301m | 4699m | 继续完成绑定，节点随后准备容器 |

one-over 的 UID 和 request 始终不变。变化发生在外部资源条件：另一个 Pod 释放请求后，它获得新的尝试机会。下面分别说明请求计算、失败处理、删除通知和重新绑定，使用普通单 Pod、默认资源插件与默认绑定插件路径。

#### 14.1.1 201m 从哪里来：先算 Pod 请求，再逐台比较

进入一次尝试后，调度器准备两份输入：这只 Pod 的最终配置，以及这一轮使用的节点资料。NodeInfo 是某台节点及其已计入 Pod、资源等资料的汇总；CycleState 是本轮插件之间暂存数据的地方。

`PreFilter` 把请求计算一次后保存，后面的逐节点检查可以复用。下面是 `pkg/scheduler/framework/plugins/noderesources/fit.go / Fit.PreFilter` 的完整函数，只加中文注释。[N8] 本组所有 Go 摘录都依赖上游类型，不能独立编译。

参数 `pod` 是当前 Pod；`cycleState` 是本轮暂存区；`f` 是资源插件自身。`ctx` 传递本轮处理的上下文，如取消信号；`nodes` 是候选节点资料列表，本函数体没有读取它。

```go
func (f *Fit) PreFilter(ctx context.Context, cycleState fwk.CycleState, pod *v1.Pod, nodes []fwk.NodeInfo) (*fwk.PreFilterResult, *fwk.Status) {
    // 按最终 Pod 和功能配置计算请求；本例只有一个容器，CPU 得到 201m。
    result := computePodResourceRequest(pod, ResourceRequestsOptions{EnablePodLevelResources: f.enablePodLevelResources})
    // 保存计算结果，后面检查每台节点时从同一处取。
    cycleState.Write(preFilterStateKey, result)
    // 不额外缩小候选节点集合；本次前置处理成功。
    return nil, nil
}
```

`PreFilter` 通过 `resource.PodRequests` 汇总最终 Pod 的请求，并写入 CycleState 供后续节点检查复用。此时只是准备本轮计算数据，还没有为任何节点计入这只 Pod 的资源请求。[N8]

`(f *Fit)` 表示这个方法属于一个 Fit 插件实例，`f` 可暂时类比 Java 的 `this`，但 Go 不是 Java 的类继承模型。`*T` 是指向 T 类型值的指针；`[]T` 是一组 T 类型记录。两个返回位置各有用途：第一个 nil 表示不返回节点范围限制，第二个 nil Status 表示成功，不能合读成“两次失败”。`fwk` 在这一段是导入的框架包名。

#### 14.1.2 为什么失败：把现场数字送进同一个判断

继续到 `Fit.Filter`：它先用 `getPreFilterState` 取回刚才的请求，再调用第 3.2.2 节已经逐行读过的 `fitsRequest`。把那段代码里的变量填上：

| 源码中的值 | 本例数字 | 从哪里来 |
|---|---:|---|
| `podRequest.MilliCPU` | 201 | 最终 Pod 请求，经上一步汇总 |
| `nodeInfo.GetAllocatable().GetMilliCPU()` | 5000 | scheduler 所见的节点可分配量 |
| `nodeInfo.GetRequested().GetMilliCPU()` | 4800 | 此节点已计入的系统 Pod 与 holding |
| 可用余额 | 5000−4800=200 | 两项相减；不是实时 usage |

于是 `201 > 200` 成立，产生 `Insufficient cpu` 记录。`201 > 5000` 不成立，Unresolvable 为 false：删除其他请求有可能解决。这里记录的是一次条件不满足，程序并没有崩溃。

沿返回值追到外层，下面是文字路线，箭头表示结果交给谁，并非每一行都是相邻函数调用：[N8][N9][S17]

```text
fitsRequest：返回 CPU 不足记录
  → Fit.Filter：返回 Unschedulable Status，带 Insufficient cpu
  → RunFilterPlugins：标记失败插件，停止这台节点后续 Filter
  → schedulePod：本例其他节点也被硬条件挡住，没有可行节点，返回 FitError
  → schedulingAlgorithm：还可能跑 PostFilter，例如尝试寻找抢占方案
  → 本例没有可用方案，返回携带 FitError 的 Unschedulable Status
  → schedulingCycle 原样把这个失败结果向外交回
```

FitError 是“本轮没有合适节点”的诊断结果。它包含各节点的失败原因，可以用 Go error 传递，但不等于 scheduler 内部故障。对于读取前置状态失败等异常，`Fit.Filter` 会返回 Error；Framework 也会把不符合过滤阶段约定的返回状态包装成 Error。[N8][N9]

这里还有一个会影响故障判断的返回分支：固定提交中，PostFilter 自己返回 Error 时，`schedulingAlgorithm` 会记录异常，但这个 FitError 分支最终仍返回携带原 FitError 的 Unschedulable。不能只看到内层出现 Error，就断言最外层一定报告 SchedulerError。没有节点、普通找不到位置、非 FitError 的执行异常，也要分别读。[S17]

#### 14.1.3 谁让它继续等，谁把原因写给 kubectl 看

回到 `pkg/scheduler/schedule_one.go / scheduleOnePod`。下面是函数末尾的连续摘录，省略了前面的取配置、跳过不需调度的 Pod、创建本轮状态等步骤；不是完整函数。[S17]

`sched` 是调度器实例；`podInfo` 包含正在处理的 Pod；`state` 是本轮暂存区。`fwk` **在这一段是前文选出的调度配置实例变量**，不是上一段的导入包名。其他参数携带本轮的时间、上下文和待激活 Pod 记录。

```go
// 完成选点以及成功后的临时占账准备，得到结果与处理状态。
scheduleResult, assumedPodInfo, status := sched.schedulingCycle(schedulingCycleCtx, state, fwk, podInfo, start, podsToActivate)
if !status.IsSuccess() {
    // 本例在这里处理“放不下”：记原因、更新状态并安排后续尝试。
    sched.FailureHandler(schedulingCycleCtx, fwk, assumedPodInfo, status, scheduleResult.nominatingInfo, start)
    return // 这一次不会进入下面的绑定流程。
}
// 只有上面的准备成功，才另行推进绑定。
go sched.runBindingCycle(ctx, state, fwk, scheduleResult, assumedPodInfo, start, podsToActivate)
```

选点处理失败时，`scheduleOnePod` 调用 FailureHandler 后返回，成功才通过 `go` 启动绑定流程。本例第一次在资源检查时已失败，没有执行 `go` 那一行；返回变量名叫 `assumedPodInfo`，也不表示已经完成 Assume。

这里的 `status` 可以为 nil：`IsSuccess()` 经由 `Code()` 把 nil Status 解释为 Success。方法本身处理了 nil 接收者，第 14.3 节继续说明这一 Go 语法及其适用范围。[N15]

多返回值按位置接到三个变量；`!` 是取反。`return` 结束当前函数，不是退出 scheduler 进程。`go` 启动另一段可并发推进的工作，原调用不用等 API 绑定完成才继续。

默认的 FailureHandler 指向 `handleSchedulingFailure`，它要处理几份不同的记录：[S17]

| 它处理的记录 | 本例的动作 | 为什么不能省 |
|---|---|---|
| 失败插件资料 | 从 FitError 保存失败插件 | 后续变化到来时，要判断哪些等待者值得再试 |
| 队列里的 Pod | 确认对象仍存在、尚未绑定、UID 未换，再调用 `AddUnschedulableIfNotPresent` | 已绑定、删除或重建的对象不能按旧资料重新排队 |
| 对外状态与报告 | 记录 FailedScheduling Event，尝试把 PodScheduled 写为 False、reason=Unschedulable | 让现场查询能看见这次判断；写 API 失败还会另记错误 |

队列函数还会结合本轮发生过的变化、退避时间等选择等待位置，名字里有 Unschedulable 不表示所有路径都只塞进同一个容器。若插入队列等处理出错，这个 handler 记录错误；它没有把 error 返回给 `scheduleOnePod`，也不承诺插入一定成功。Done 负责结束本轮队列跟踪，不是释放已经运行的 Pod 资源。

#### 14.1.4 删除 holding 后，哪两份东西要更新

只向 API 发出删除请求还不够。holding 要退出，相关对象变化还要传到 scheduler。它处理已绑定 Pod 的删除时，先改资源账，再通知队列检查等待者。[N10]

下面是 `pkg/scheduler/eventhandlers.go / deleteAssignedPodFromCache` 的连续摘录。`pod` 是收到删除通知的 holding；`sched` 是调度器；`logger` 已在前文从 sched 取得。省略的是耗时统计和入口日志。

```go
// 从 scheduler 的缓存移除 holding，正常处理后会扣掉它的资源请求。
if err := sched.Cache.RemovePod(logger, pod); err != nil {
    // 移除失败会记错误；注意这里没有 return，后面的队列通知仍会发生。
    utilruntime.HandleErrorWithLogger(logger, err, "Scheduler cache RemovePod failed", "pod", klog.KObj(pod))
}
// 告诉队列：有一只已绑定 Pod 被删除了，可以检查哪些等待者值得再试。
sched.SchedulingQueue.MoveAllToActiveOrBackoffQueue(logger, framework.EventAssignedPodDelete, pod, nil, nil)
```

收到 holding 的删除通知后，调度器先尝试从缓存移除其请求，再通知队列检查等待者。正常情况下余额从 200m 变为 4900m；移除失败时仍会发出队列通知，因此通知本身不能证明资源账已正确更新。

`if err := 调用(); err != nil` 是先调用并接住错误，再判断是否出错；err 在这个 if 及对应分支内使用。末尾两个 nil 分别表示没有新对象、没有额外的预检查函数，不是两个错误返回值。

这里的 AssignedPodDelete 是程序内部的对象变化通知。FailedScheduling 才是前面给 `kubectl get events` 查看的一条报告。队列不会因为你删除了某条 FailedScheduling Event 就认定 CPU 已释放。

#### 14.1.5 值得重试，为什么还不能承诺一定成功

资源插件通过 `EventsToRegister` 关注已绑定 Pod 的删除，并提供 `isSchedulableAfterAssignedPodDelete` 判断是否值得重新尝试。[N8]

下面连续摘录该判断函数中解析成功后的分支。前文已把通知中的旧对象解析成 `deletedPod`；解析失败会在前面返回 `Queue, err`。`pod` 是等待的 one-over，`deletedPod` 是删除的 holding，`logger` 来自参数。

```go
// 没绑定、也没被提名的旧 Pod，不提供这里要找的释放线索。
if deletedPod.Spec.NodeName == "" && deletedPod.Status.NominatedNodeName == "" {
    logger.V(5).Info("the deleted pod was unscheduled and it wouldn't make the unscheduled pod schedulable", "pod", klog.KObj(pod), "deletedPod", klog.KObj(deletedPod))
    return fwk.QueueSkip, nil // 本插件认为这次删除不用触发重试。
}
// 有绑定或提名信息，先认为值得再看一遍，并未计算是否足够。
logger.V(5).Info("another scheduled pod was deleted, and it may make the unscheduled pod schedulable", "pod", klog.KObj(pod), "deletedPod", klog.KObj(deletedPod))
return fwk.Queue, nil // 建议重试；没有解析错误。
```

holding 已绑定，所以得到 Queue。这个函数没有算 `4700 ≥ 1`，也没有验证其他硬条件；它只给出“值得再试”的建议。队列还要结合退避和其他失败插件决定何时可取出。[N11]

两个返回位置分别是“排队建议”和“错误”。`Queue, nil` 是建议重试且本函数没有错误；`QueueSkip, nil` 是不因这次变化重试且本函数没有错误。日志的 `V(5)` 表示详细程度，不是优先级数值。

如果删除的是另一台不符合节点标签的 Node 上的 Pod，这个提示仍可能建议重试；目标 worker 的余额却仍为 200m，下一轮依旧失败。QueueingHint 只判断变化是否值得重新考虑，完整条件由下一轮 Filter 重新检查。

#### 14.1.6 再次选中以后，哪一步才让 API 出现 nodeName

one-over 再次被取出时，会重新更新本轮节点视图、计算请求、检查硬条件。正常删除已传播后，CPU 判断变为 `201 > 4900`，结果是假，所以不会新增 CPU 不足记录。只有这一台可行时无需比较多个候选分数。[S17]

按时间从上到下看后半段；这里的先后是处理路线，API 与缓存通知仍可能短暂不同步：

| 步骤 | 改了什么 | 此时能宣布什么 |
|---|---|---|
| `assumeAndReserve` 中的 `assume` | 给 Pod 的内存副本设置候选 nodeName，先把 201m 记入本 scheduler 的 cache | 本调度器余额变为 4699m；还不能据此说 API 已绑定 |
| Reserve、Permit | 执行配置的临时预留、准许或等待逻辑 | 失败需要撤销，等待还没有结束绑定 |
| `runBindingCycle` → `bindingCycle` | 等待 Permit、执行 PreBind，再调用绑定路径 | 仍需检查返回状态 |
| 默认绑定插件 | 把 Pod 的 namespace、name、UID 与目标 Node 组成 Binding 请求 | API 成功保存后才能查到这次落点 |
| kubelet 后续处理 | 准备并启动容器，报告状态 | Ready 是后来的节点与容器结果 |

默认绑定插件将选点结果提交给 API。下面是 `pkg/scheduler/framework/plugins/defaultbinder/default_binder.go / DefaultBinder.Bind` 的连续摘录。[N12] `binding` 已由当前 Pod 身份和目标 nodeName 构造，`b` 是绑定插件，`ctx` 是本轮上下文。固定提交还包含 APICacher 分支；下面只展示未使用该分支时的直接调用，不能当成完整函数。

```go
// 给 API 发送这只 Pod 到目标 Node 的 Binding 请求。
err := b.handle.ClientSet().CoreV1().Pods(binding.Namespace).Bind(ctx, binding, metav1.CreateOptions{})
if err != nil {
    return fwk.AsStatus(err) // 请求出错，转成插件处理状态交回上层。
}
return nil // 本插件绑定处理成功；不表示容器已经 Ready。
```

绑定插件将 Pod 身份与目标节点组成的 Binding 请求交给 API，失败时将错误转换成 Status，成功时返回 nil。APICacher 是调度器管理部分 API 写入的内部设施，另一分支会提交绑定并等待完成结果，再处理可能的错误。[N12]

连着写的 `.方法()` 是逐步取得 API 客户端并发起调用；`metav1.CreateOptions{}` 构造空的创建选项。这里的 `AsStatus(err)` 是转换错误表示，不会把错误变成成功。

若 Reserve、Permit 或绑定失败，不能一直占着这 201m。固定路径按失败位置执行 Unreserve、ForgetPod 等清理；绑定失败处理中，只有 Forget 成功后，才会向队列通知资源可能释放，让其他受影响 Pod 有机会再试。清理出错会被记录，调用过清理并不保证临时状态已经撤销。Done 则按相应路径结束本轮跟踪，不必等到所有绑定步骤结束才执行，见第 10.3 节。[S17]

这条路径中，第一次尝试在资源过滤阶段失败，没有进入 Assume。holding 的删除需要同时更新资源账和等待队列，one-over 才能带着新余额再次尝试。绑定插件返回成功以后，节点仍要准备容器，应用也要完成启动与就绪检查。

#### 14.1.7 版本差异与状态观察的范围

| 位置 | 本文固定提交 | v1.34.0 发布源码 |
|---|---|---|
| 单 Pod 的主要流程 | `ScheduleOne` 转到 `scheduleOnePod` | 主要逻辑直接在 `ScheduleOne` |
| 选点失败、PostFilter | 拆在 `schedulingAlgorithm` 等函数 | 相应逻辑主要在 `schedulingCycle` |
| 资源删除提示 | `isSchedulableAfterAssignedPodDelete` | `isSchedulableAfterPodEvent` 处理包括删除在内的变化 |
| 删除后重新尝试的结论 | 更新缓存，向队列传递变化，再检查条件 | 对本例的结论相同；函数名与参数不能照搬 |

表中差异已对照 v1.34.0 的 [schedule_one.go](https://github.com/kubernetes/kubernetes/blob/f28b4c9efbca5c5c0af716d9f2d5702667ee8a45/pkg/scheduler/schedule_one.go)、[eventhandlers.go](https://github.com/kubernetes/kubernetes/blob/f28b4c9efbca5c5c0af716d9f2d5702667ee8a45/pkg/scheduler/eventhandlers.go) 和 [fit.go](https://github.com/kubernetes/kubernetes/blob/f28b4c9efbca5c5c0af716d9f2d5702667ee8a45/pkg/scheduler/framework/plugins/noderesources/fit.go)。

相同 UID 的 Pod 从 `PodScheduled=False` 变为已有 `nodeName`，说明原对象后来完成了节点绑定。仅凭这些 API 快照，无法判断某次重试具体由删除通知、退避到期还是其他队列路径触发；需要结合对应版本的日志或内部跟踪分析。

相关实现按状态变化对应到以下位置：

| 顺序 | 源码位置 | 处理内容 |
|---|---|---|
| 1 | scheduler.go / Run | 谁启动循环，谁负责leader后的工作 |
| 2 | schedule_one.go / ScheduleOne、scheduleOnePod | Pod从哪里取，什么时候Done |
| 3 | schedulingCycle / schedulingAlgorithm / schedulePod | snapshot、Filter与Score怎样衔接 |
| 4 | framework/runtime/framework.go | 插件按何顺序调用，何时短路 |
| 5 | assumeAndReserve | 通用cache与插件状态分别改了什么 |
| 6 | bindingCycle | Permit等待、PreBind、Bind和PostBind责任 |
| 7 | cache实现 | Add/Assume/Forget与NodeInfo账怎样维护 |

上面的定位表对应固定提交。遇到别的版本，先查实际入口，再沿相同问题找代码。[G5]

### 14.2 缓存、插件与 API 分别保存什么

同一个 Pod 在不同阶段会出现在多份数据中。PodInfo 携带 Pod 及其调度资料；NodeInfo 汇总节点、已计入 Pod、资源和端口；CycleState 保存本轮插件需要复用的计算结果。

| 数据或状态 | 主要维护者 | 作用与变化 |
|---|---|---|
| API 中的 Pod | API Server 与相关组件 | 保存最终配置、nodeName 和组件报告的状态 |
| scheduler cache / NodeInfo | scheduler | 汇总已绑定与临时 Assume 的请求，供后续选点使用 |
| 本轮 snapshot | scheduler | 提供本轮检查使用的节点视图 |
| CycleState | 本轮插件 | 保存 PreFilter 等阶段计算的请求及中间结果 |
| 插件预留 | Reserve/Unreserve 插件 | 维护并撤销该插件自己的临时状态 |
| 等待与 in-flight 跟踪 | 调度队列 | 决定待处理 Pod 何时再次尝试，并跟踪处理期间的相关变化 |

Assume 先在本调度器缓存中计入一次预计会成功的资源承诺，失败后再撤销；Reserve 则维护插件自己的临时状态。两者不能合并为一把全局资源锁。[G6]

普通 scheduling cycle 串行推进，binding cycle 可以通过 goroutine 与后续选点交错执行。因此，API 还未显示 A 的 nodeName 时，本 scheduler 的资源账已经可能计入 A；另一个独立调度器却不会共享这笔临时账。

goroutine 是 Go 的并发执行单元，不保证每段工作独占一个 CPU 核。多个流程读写共享状态时，需要由相应实现使用锁等机制协调访问。对象从 API 经 informer 传播到缓存也需要时间，这解释了不同观察点之间的短暂差异。

### 14.3 返回值怎样影响后续流程

Go 函数可以返回多个值，每个位置都有独立含义。以下返回值来自前文不同函数，不能统一理解为“成功”或“失败”：

| 返回值或结果 | 所在位置 | 含义 |
|---|---|---|
| `nil, nil` | Fit.PreFilter | 不缩小节点范围，并且前置处理成功 |
| `false` | fitsPorts | 节点上存在端口冲突，由调用者转换为 Unschedulable |
| `nil` Status | Fit.Filter、DefaultBinder.Bind 等 | 当前插件阶段成功，不能据此判断容器是否 Ready |
| `Unschedulable` | 资源等 Filter | 当前条件不满足，由失败路径记录原因并处理后续等待 |
| `Error` | 读取前置状态等异常路径 | 当前执行遇到异常；外层是否保留这一分类还要看调用者 |
| `Queue, nil` | QueueingHint | 建议因这次变化重新考虑，没有错误；不保证能找到节点 |
| `QueueSkip, nil` | QueueingHint | 不因这次变化建议重试，没有错误 |
| 没有 error 返回值 | handleSchedulingFailure | 内部错误通过日志等途径记录，不向调用者返回一个 error |

`status.IsSuccess()` 能处理 nil，是因为该类型的方法显式支持这一情况：`IsSuccess()` 调用 `Code()`，而 `Code()` 在接收者为 nil 时返回 Success。Go 允许把 nil 指针作为方法接收者；方法能否安全执行取决于实现是否在解引用前处理 nil，不能推广为所有方法都能这样调用。[N15]

这也是 Java 类比的一个界限：不能把所有 nil 方法调用都直接等同于 Java 的空引用异常。理解当前调用链时，返回类型、方法实现和调用者的分支必须对应起来。

---

<a id="terms"></a>

## 15. 术语速查

本节集中列出正文中的对象、字段、缩写和 Go 写法，便于按主题查阅。

### 15.1 对象、组件和状态：谁负责，做到哪一步

| 词或字段 | 含义 |
|---|---|
| Kubernetes / K8s | 管理容器应用的一套系统；K8s 是它的简称。 |
| Pod | Kubernetes 放置和运行容器的一组单位；普通 Pod 内的容器落在同一台节点上。 |
| Node | 集群里提供 CPU、内存等资源并运行 Pod 的节点。 |
| Namespace / namespace | 给对象分组、设置权限和配额的范围；不是独占节点的承诺。 |
| 工作负载 / workload | 你要运行的应用或任务，以及管理它的对象。 |
| controller / 控制器 | 反复看目标和现状，再推进必要的创建、删除或状态更新。 |
| Deployment | 管应用副本和版本替换；更新模板后由它推进发布。 |
| ReplicaSet | 维持某一组模板对应的副本数；本例的 Pod 创建由它推进。 |
| Job | 管做完就结束的任务，并按配置处理成功、失败和重试。 |
| scheduler / kube-scheduler | 给它负责的未绑定 Pod 选择节点，再推进绑定。 |
| kubelet | 节点上的执行者：准备容器、协调网络/存储/设备，报告 Pod 状态。 |
| API / API Server | API 是程序查询和修改数据的入口；API Server 是集群接收这些请求的服务。 |
| `metadata` / `metadata.uid` | metadata 保存名字、标签等身份信息；UID 是这次对象创建的唯一标识，重建后会换。 |
| `spec` / `status` | spec 写要求和目标；status 是组件报告的当前情况。改 status 不能替代改目标配置。 |
| `spec.nodeName` | 这只 Pod 被交给哪台节点；有值还不代表容器已经启动。手工填写也可能绕过 scheduler。 |
| `spec.schedulerName` | 把 Pod 交给哪个调度名字；写一个名字不会自动创建调度器。 |
| Condition / `conditions` | 一项状态检查记录，通常有 True、False 或 Unknown；看 status、reason、message 才知道含义。 |
| `PodScheduled` | Pod 的调度状态检查；False 时还要读原因，不能单凭 False 判 CPU 不足。 |
| phase / Pending / Running | phase 是粗略阶段；Pending 尚未完成启动准备，Running 已到运行阶段，二者都不能替代 Ready。 |
| Ready / readiness | Ready 是就绪状态；readiness 是检查应用能否接请求的探针。就绪仍需实际业务验收。 |
| 探针 / 启动探针 | 探针定期检查容器；启动探针检查慢启动应用是否完成启动，在它通过前不进行该容器的就绪、存活探测。 |
| Node NotReady | 节点的 Ready 状态没有通过；要查节点条件和原因，它不是 Pod 的 Pending。 |
| Succeeded / Failed / 终态 | 这次 Pod 运行成功结束或失败结束；后续是否新建由控制器与策略决定。 |
| Event / 对象变化通知 | Event 是组件保存的进展或失败报告；变化通知是让程序知道某个对象新增、修改或删除。 |
| `involvedObject.uid` | Event 报告的是哪一次对象实例；用它把事件与正确的 Pod 或 ReplicaSet 对上。 |
| `reason` / `message` | reason 是简短原因名，message 是详细说明；原因名相同也可能有不同具体问题。 |
| FailedCreate / FailedScheduling / FailedMount | 分别是创建失败、调度尝试失败、挂卷准备失败的报告；最后一种也可能来自 attach，要看详细消息。 |
| ErrImagePull / ImagePullBackOff | 拉镜像失败，以及失败后先等一会儿再拉；已有 nodeName 时先查节点拉镜像路径。 |
| Forbidden / RBAC | Forbidden 是请求被拒绝；RBAC 是按身份和角色规定权限。拒绝也可能来自配额等其他检查。 |
| API 准入 / webhook | 保存对象前检查或补充字段；webhook 是这一过程中调用的外部程序。最终 Pod 可能与提交模板不同。 |
| label / labels / selector | label 是对象上的键值标签；selector 是“按哪些标签找对象”的规则。Node 标签和 Pod 标签要分别看。 |

### 15.2 资源账和 Java：申请了多少，实际用了多少

| 词或字段 | 含义 |
|---|---|
| Capacity | 节点报告的总容量。 |
| Allocatable | 节点可分配给 Pod 的容量；它还没有扣除其他 Pod 的请求。 |
| request / Requested / usage | request 是本 Pod 的申请量；Requested 是节点账上已经计入的请求；usage 是测得的实际消耗。 |
| limit / limits | 运行时资源上限；CPU、内存、设备的实施规则不同，不是统一的“超了就排队”。 |
| 请求账 / 资源承诺 | 记录已经计入的资源请求；容器当前使用量低，也不会自动把请求量归零。 |
| CPU 的 `m` / 内存的 `Mi`、`Gi` | 1000m CPU=1 CPU；1Gi=1024Mi。内存的 m 是毫字节数量单位，不是 Mi。 |
| ResourceQuota / Quota | namespace 的资源总额限制；本例超过配额会在创建 Pod 时被拒绝。 |
| LimitRange / defaultRequest | LimitRange 可规定默认值和单个对象资源范围；defaultRequest 是缺少请求时采用的默认申请量。 |
| defaulting / 默认处理 | API 按规则补缺少的字段；YAML 没写，不等于最后保存的 Pod 也没值。 |
| sidecar / init container | sidecar 是辅助容器；普通 init 是启动前执行、完成后退出的初始化容器。 |
| 原生 sidecar / `restartPolicy: Always` | 本文指 init 列表中会继续运行的辅助容器；算资源时要计入它与后续容器的重叠。 |
| RuntimeClass / overhead | RuntimeClass 选择容器运行方式；overhead 是这类方式额外需要的 Pod 资源开销。 |
| Pod-level resources / resize | 前者在整个 Pod 层设置预算；后者调整原 Pod 的资源配置，规则按版本核对。 |
| Pod slots | 节点还能放多少只 Pod 的位置数；CPU 有余量也可能先达到 Pod 数上限。 |
| shape / Pod 规格 | 一只 Pod 需要的 CPU、内存、GPU 等组合，例如 2 CPU/4Gi。 |
| 资源碎片 / 装箱 / bin packing | 碎片是余量分散后凑不出所需组合；装箱是把 Pod 按规格安排到节点，可能留下完整空节点。 |
| 临时存储 / 磁盘压力 | 临时存储请求进入资源检查；磁盘压力是节点实际存储状况，两者不等同。 |
| JVM / Spring Boot | JVM 是运行 Java 的环境；Spring Boot 是组织 Java 应用启动与配置的框架，本例以它的启动和就绪为背景。 |
| GC / JIT / 预热 | GC 回收不用的对象内存；JIT 把常用代码转成更快的机器代码；预热是加载数据、编译等启动准备。 |
| throttling / OOM / JVM 堆 | throttling 是 CPU 用到限额后暂时得不到计算时间；OOM 是内存不足；JVM 堆只是容器内存的一部分。 |
| QoS / Guaranteed / Burstable / BestEffort | 未设置 Pod 层资源时，所有普通及 init 容器的 CPU、内存请求和上限都大于 0 且分别相等，才是 Guaranteed；都没设置有效值是 BestEffort；其余为 Burstable。Pod 层配置另查版本。 |
| HPA / 节点 autoscaler | HPA 调整 Pod 副本数；节点 autoscaler 调整节点供给；scheduler 给已创建的 Pod 选位置。 |
| Utilization / AverageValue | 前者是相对 request 的使用百分比；后者是每只 Pod 的平均实际指标值，比如 500m。 |
| 容差 / 稳定窗口 | 小幅波动先不调整，以及参考一段时间建议来避免频繁扩缩容。 |
| min / floor / ceil | min 取较小值；floor 向下取整，1.8→1；ceil 向上取整，1.2→2。 |

### 15.3 选点和拓扑：哪个位置合格，副本怎样分布

| 词或字段 | 含义 |
|---|---|
| 硬条件 / 软偏好 | 硬条件任何一条失败都不能选；软偏好在合格位置里比较更喜欢谁。 |
| Filter / 短路 | Filter 检查节点资格；某插件失败就停止这台节点后续插件检查，所以报告不一定列全问题。 |
| Score / weight | Score 给可行节点打分；weight 是权重，决定该项分数占多大影响。 |
| LeastAllocated / MostAllocated | 看放入后的请求占比，分别偏好比较空或比较满的节点；不是比较实时利用率。 |
| NormalizeScore / 归一化 | 把某插件自己的分数换算到框架要求的范围；并非所有插件都有这一步。 |
| nodeSelector / nodeAffinity | 用节点标签表达必须或偏好去哪类节点；nodeAffinity 能写更复杂的条件组合。 |
| required / preferred | required 是必须满足；preferred 是更喜欢，但不能让不合格节点通过硬检查。 |
| pool / 节点池 | 按用途或规格管理的一组节点，例如 online、batch、gpu；本例靠节点标签限制可进入的池。 |
| AND / OR / term / matchExpressions | AND 是组内每条都满足；OR 是满足其中一组；term 是一组条件，matchExpressions 是组里的逐条检查。 |
| `IgnoredDuringExecution` | 运行后节点标签变化，不会仅凭这条调度亲和规则自动把 Pod 赶走。 |
| podAffinity / podAntiAffinity | 按其他 Pod 的位置表达想靠近谁、想与谁分开；要明确比较哪个节点域。 |
| taint / toleration | 节点的拒绝条件，与 Pod 表示可以接受的声明；容忍不负责指定目的地。 |
| `key`、`value`、`operator`、`effect` | 污点/容忍里分别是名字、值、怎样匹配、产生什么影响；本例 Equal 是值相等时匹配。 |
| NoSchedule / NoExecute / tolerationSeconds | NoSchedule 拒绝不容忍的新 Pod；NoExecute 还可能驱逐已有 Pod；tolerationSeconds 是继续容忍的秒数。 |
| AZ / zone / hostname / 故障域 | AZ 是可用区，zone 标签标出可用区；hostname 标出节点；故障域是可能被同一次故障影响的一组位置。 |
| topologyKey / topologySpreadConstraints | topologyKey 指定按哪个节点标签分组；分布规则限制匹配副本在这些组之间的偏差。 |
| skew / maxSkew / global minimum | skew 是本规则计算的数量偏差；maxSkew 是上限；global minimum 是计算时作为基准的最小数量。 |
| eligible domains / minDomains | 前者是按本规则参与计算的域；域数少于 minDomains 时，最小计数基准按 0，不是拒绝所有 Pod。 |
| DoNotSchedule / ScheduleAnyway | 分布规则不通过时，前者拒绝该位置，后者通过打分尽量分散；其他硬条件仍要满足。 |
| nodeAffinityPolicy / nodeTaintsPolicy | 决定统计分布时是否按亲和、污点条件排除节点；不替代实际选点的硬检查。 |
| Honor / Ignore | 在上述统计策略中，Honor 按该类条件筛节点，Ignore 不据它筛统计节点；不是“忽略全部调度限制”。 |
| labelSelector / matchLabelKeys / pod-template-hash | labelSelector 决定统计哪些 Pod；matchLabelKeys 加入指定标签值；pod-template-hash 用来区分 Deployment 模板版本。 |
| profile / addedAffinity | profile 是调度规则配置；addedAffinity 是该配置附加的节点条件。多个配置可以属于同一个进程。 |
| 硬条件交集 | 必须有同一台节点把所有要求一起满足；CPU 在 a、合适卷在 b，不能拼成一个位置。 |
| N-1 | 检查失去一个指定故障单位后，剩余供给能否达到目标；第 12 章的单位是可用区，算例并没有保证已经达标。 |

### 15.4 缓存、队列和返回结果：为什么等，失败怎样撤销

| 词或字段 | 含义 |
|---|---|
| Framework / 插件 / 扩展点 | Framework 规定处理阶段；扩展点是这些阶段的接入口；插件实现一类具体规则。 |
| NodeResourcesFit / NodePorts | 分别是资源与节点端口相关插件的名字；前者可检查、评分资源，后者检查 hostPort 等端口声明。 |
| NodeAffinity / TaintToleration | 分别是处理节点亲和、污点容忍的插件名，对应前文的节点条件检查。 |
| DynamicResources | 调度框架中处理 DRA 可行性、设备分配等步骤的插件名。 |
| KubeSchedulerConfiguration / pluginConfig / scoringStrategy | 调度器配置文件类型、给插件的参数、资源评分方式；第 13.3 节用它指定 GPU 如何参与评分。 |
| watch / informer | watch 持续提供对象变化通知；informer 维护本地资料，并把变化交给程序处理。 |
| cache / snapshot | cache 是内存中的资料和账；snapshot 是本轮节点判断所用的视图，可能暂时落后于 API。 |
| PodInfo / NodeInfo / CycleState | 分别是 Pod 的调度资料、节点及其账的汇总、本轮插件暂存数据的地方。 |
| attempt / cycle | attempt 是一次调度尝试；cycle 是一轮处理。失败重试后还是同一只 Pod，但多了尝试次数。 |
| scheduling cycle / binding cycle | 前者算节点；后者推进绑定相关步骤。普通选点串行，前一只绑定时下一只可以开始选点。 |
| 串行 / 并发 / 异步 | 依次做完、多个流程交错推进、发起后不一直等在原地；并发不保证同一瞬间都占一个 CPU 核。 |
| Assume / assumed Pod / 乐观占账 | 在本调度器 cache 先计入请求，下一只能看见临时承诺；尚未收到绑定后通知确认时仍可处于 assumed 状态，API 绑定可能已成功。失败按流程撤销，另一个独立调度器不共享这笔账。 |
| Reserve / Unreserve | 插件保存自己的临时预留状态，以及失败时撤销它。 |
| Permit / Wait | 绑定前检查协调条件，允许、拒绝或等待；这里的 Wait 是插件结果，不是 Pod phase。 |
| Bind / Binding | 把选中的节点通过 API 正式交给 Pod；写入 nodeName 后节点侧继续工作。 |
| PreFilter / PreBind / PostBind | 分别在逐节点过滤前准备数据、正式绑定前处理、绑定成功后处理；名字说明阶段，不保证每只 Pod 都执行全部插件。 |
| ForgetPod / Done / 补偿 | ForgetPod 撤掉 cache 的临时占账；Done 结束队列本次处理跟踪；补偿是把没办成的临时动作撤销。 |
| active / backoff / unschedulable | 队列里分别是准备尝试、先等一段时间、当前条件不满足；这些不是 Pod phase。 |
| QueueSort / QueueingHint / in-flight | 分别是决定谁先尝试、判断某变化值不值得再试、跟踪已取出但还在处理中的对象和变化。 |
| scheduling gate / schedulingGates | Pod 暂缓调度的条件；移除后才可进入普通尝试，但不保证其他条件通过。 |
| Status / Error / error | Status 是插件处理结果，Error 是其中一种异常结果；Go error 是函数的错误返回值，不能和 API 的 status 字段混读。 |
| Unschedulable / Unresolvable | 前者是当前条件不满足；本例 Unresolvable 为 true 表示删掉别的 Pod 仍解决不了该资源的单节点容量问题。 |
| UnschedulableAndUnresolvable | 插件用它指出当前这项拒绝不能靠抢占其他 Pod 消除；不等于以后扩容或改规格都无效。 |
| Skip / `nil` Status | 本例 PreFilter 的 Skip 让后续省掉该插件 Filter；这里的 nil Status 表示当前阶段成功。 |
| 抢占 / victim / Preempted | 抢占尝试让低优先级 Pod 退出以腾位置；victim 是被选中的 Pod；Preempted 是相应事件原因。 |
| PriorityClass / priorityClassName / priority | PriorityClass 保存优先级配置，Pod 用 priorityClassName 引用它，priority 是对应数值。 |
| preemptionPolicy / Never / PreemptLowerPriority | 决定是否主动抢占；Never 不主动抢，PreemptLowerPriority 可抢低优先级；都不保证成功。 |
| nominatedNodeName / 提名 | 抢占等流程中的潜在落点提示；不是正式绑定，也不是保证不变的节点锁。 |
| leader / leader election / Lease | 当前负责人、选负责人过程、记录负责人及续约信息的对象；同一套主备用它协调谁工作。 |
| Extender / ignorable | Extender 是调度器通过网络调用的扩展服务；ignorable 决定调用报错时是否允许跳过，要核对实际路径。 |

### 15.5 卷、网络和发布：绑定之后还有哪些步骤

| 词或字段 | 含义 |
|---|---|
| PVC / PV / StorageClass / SC | PVC 是存储申请，PV 表示供给的存储，StorageClass 规定供给方式和策略；SC 是它的简称。 |
| CSI / CNI / 容器运行时 | CSI 对接存储，CNI 接通 Pod 网络；容器运行时真正创建和运行容器。 |
| Immediate / WFFC | Immediate 先处理卷绑定；WFFC 即 WaitForFirstConsumer，等首个用卷 Pod 选点时协同处理。 |
| attach / mount / selected-node | attach 把存储设备接到节点，mount 挂到目录；selected-node 注解记录存储协作所选节点，不应手工伪造。 |
| containerPort / hostPort / hostNetwork | 容器端口声明、节点端口申请、直接使用节点网络；填 containerPort 不会替程序开始监听。 |
| hostIP / TCP / socket / bind / listen | hostIP 是节点端口对应地址，TCP 是通信协议，socket 是网络端点；网络 bind 指定地址端口，listen 等连接。 |
| Service / EndpointSlice / 服务发现 | Service 提供服务访问入口，EndpointSlice 记录后端地址和条件；服务发现是找到有哪些实例可访问的过程。 |
| HTTP / 数据面 / 摘流量 | HTTP 是本例接口请求的协议；数据面执行实际转发；摘流量是让新请求停止交给某实例，已有连接还可能存在。 |
| replicas / PodTemplate / revision / rollout | 目标副本数、创建 Pod 的模板、版本、推进版本替换。模板变化才可能触发相应更新。 |
| maxSurge / maxUnavailable | 更新时最多额外增加多少，以及最多允许多少目标副本暂时不可用；百分比取整规则不同。 |
| Available / availableReplicas / minReadySeconds | Pod 先 Ready，持续保持 Ready 达到 minReadySeconds 后才计作 Available；为 0 时无需再等。availableReplicas 是可用副本数，汇总可能暂时延迟。 |
| terminating / deletionTimestamp | 正在退出，以及服务端为删除流程设置的时间标记。它可能是将来时间；非空表示已进入删除流程，不等于容器已退出或资源已归还。 |
| terminationGracePeriodSeconds / preStop | 正常退出时间预算，以及退出前处理；preStop 用掉的是同一份预算。 |
| cordon / drain / Eviction | cordon 标记不再接普通新 Pod；drain 推进节点迁出；Eviction 是提出驱逐请求的 API。 |
| PDB / Descheduler | PDB 是维护驱逐的可用副本预算；Descheduler 按策略挑选已有 Pod 并发起驱逐，后续是否重建看控制器。 |
| 回滚 | 恢复此前版本或配置；恢复调度配置不会自动把已绑定 Pod 搬回原分布。 |

### 15.6 GPU 和设备：数量、具体卡和性能分开看

| 词或字段 | 含义 |
|---|---|
| GPU / 显存 | GPU 是做并行计算的处理器；显存是 GPU 内存，Pod 的 memory 算节点内存。传统 GPU 数量不表示显存余量，显存另查设备和应用数据。 |
| 扩展资源 / scalar | 扩展资源使用域名前缀声明，如 nvidia.com/gpu；scheduler 的 scalar 资源账还包含 hugepages 等类型，按资源名和数量计账。两者不是完全相同的分类。 |
| Device Plugin / ListAndWatch | 节点设备插件向 kubelet 报告设备并协助准备；ListAndWatch 持续报告设备列表与健康变化。 |
| driver / CUDA / 运行时注入 | driver 和硬件打交道，CUDA 让应用调用 NVIDIA GPU 计算，运行时注入把设备入口和必要配置交给容器。 |
| UUID / unhealthy | UUID 标识具体设备或实例；unhealthy 是设备不健康，不再计作可分配供给，不会因此自动给旧 Pod 换卡。 |
| 整卡独占 / MIG / 父卡 | 独占整张物理卡；MIG 把支持的卡划成小实例；父卡是这些实例来自的物理卡，相关故障仍可能影响它们。 |
| MIG profile / single / mixed | profile 是实例规格；single 策略可用 nvidia.com/gpu 表达 MIG 实例，mixed 用分规格资源名，细节看型号与插件配置。 |
| nvidia.com/mig-1g.10gb | 一种 MIG 规格的资源名示例，用来区分可申请的实例类型；不是具体实例的 UUID。 |
| MIG Manager / mig.config | 前者执行 MIG 重配流程，后者是期望配置标签；写了标签不证明重配完成。 |
| time-slicing / replicas / .shared | 多个任务共享物理卡；共享配置的 replicas 复制访问份额；.shared 是可能采用的共享资源名后缀。 |
| failRequestsGreaterThanOne / renameByDefault | 配置为 true 时，前者限制请求多于一份共享资源，后者改变公布的资源名；都要核对实际加载的配置。 |
| UnexpectedAdmissionError | 节点准入阶段的异常报告；共享 GPU 申请不符合插件规则可能走到这里，消息仍要读具体原因。 |
| NUMA / PCIe / NIC / NVLink | 机器内 CPU/内存分组、设备连接通道、网卡、GPU 高速连接；用它们看实际设备距离和通信条件。 |
| CPU Manager / Topology Manager | kubelet 内按策略管理 CPU 分配，以及汇合 CPU/设备位置要求的部分；不是 scheduler 的跨区规则。 |
| ConfigMap / 挂载 | ConfigMap 保存配置内容；挂载把内容放到容器可读取的文件路径，存在一份配置不证明程序已使用。 |
| DRA / DRA driver | DRA 用申请与供给对象协作分配设备；driver 发布供给并在节点准备、清理设备。 |
| ResourceSlice / Slice | 驱动公布的设备供给清单，可以分批发布。 |
| DeviceClass / Class | 定义选择哪类设备、使用什么匹配规则。 |
| ResourceClaim / Claim / allocation | Claim 是具体设备申请；allocation 是分配结果，节点准备和应用使用还要继续验证。 |
| ResourceClaimTemplate | 为每只 Pod 生成独立设备申请的模板；与多只 Pod 引用同一 Claim 的含义不同。 |
| `exactly` / ExactCount / count | 本例 exactly 写这条设备请求的要求；ExactCount 要求指定数量，count=2 表示两份，不能只给一份就算满足。 |
| CEL / pool / resourceSliceCount | CEL 写设备筛选表达式；pool 是驱动组织的供给池；resourceSliceCount 帮助判断同一代池清单是否已收齐。 |
| API discovery / GA / Beta / feature gate | 分别是查询提供哪些资源接口、功能稳定阶段、测试发布阶段、功能开关；这些信息都不代替实际设备验收。 |
| checkpoint / 训练 / 推理 | 保存应用进度和必要状态、用数据调整模型、用已有模型处理新输入；删 Pod 前先确认是否能恢复。 |
| worker | 可以指 Kubernetes 工作节点，也可以指训练任务成员；训练成员通常由 Pod 承载，不能直接换算成 Node 数。 |

### 15.7 批任务和队列：任务获准，不等于成员都启动了

| 词或字段 | 含义 |
|---|---|
| 批任务 / 工作负载准入 | 批任务做完一批工作就结束；任务准入先排队、判断能否占额度，再获准推进运行。 |
| Kueue / Volcano / PodGroup | Kueue 管任务准入等策略，Volcano 可做批任务节点调度；PodGroup 把成员归组并表达最低成员等要求。 |
| LocalQueue / ClusterQueue | namespace 内的排队入口，以及管理配额和策略的队列。 |
| ResourceFlavor / Workload | 指定资源条件如节点池，以及记录待准入任务；不是 GPU 的设备 UUID。 |
| QuotaReserved / Admitted | 已计入配额账，以及已满足该层准入条件；两者都不等于全部 Pod 已绑定或 Ready。 |
| WorkloadPriorityClass | 任务层的优先级配置；与 Pod PriorityClass 分别作用于不同的处理层。 |
| TAS / Topology-Aware Scheduling | 拓扑感知调度，在准入时结合节点分组和容量检查放置并分配拓扑；后面仍要完成绑定、启动。 |
| gang / all-or-nothing | 按一组成员协调，以及整组条件不满足时等待或撤回；产品实现位置不同，不承诺进程同一瞬间启动。 |
| waitForPodsReady / 回队 | Kueue 在准入后检查成员是否按时达到所需 Ready；超时按配置撤销准入并回到队列。 |

### 15.8 指标、源码和 Go：数字与返回值怎样读

| 词或写法 | 含义 |
|---|---|
| metrics / Prometheus / PromQL | 采集出的数字、保存和查询监控数据的系统、查询这些数字的语言。 |
| 指标 label / queue / result / profile / plugin | 指标分组标记，以及队列类别、尝试结果、调度配置、插件名；一个指标的维度要看它实际提供哪些标记。 |
| extension_point / status / le | 处理阶段、该次阶段执行结果、累计桶的上界；status 在这里不是 Pod 的 status 对象。 |
| 吞吐 / 延迟 / 长尾 | 每秒完成多少、一次花多久、少数特别慢的样本；平均快也可能存在很慢的尾部。 |
| p99 / 直方图 / bucket | 约 99% 样本不超过的值、按范围统计、一个范围桶；只算已结束尝试会遗漏完整等待。 |
| rate / sum by / topk / histogram_quantile | 算计数增速、分组求和、取最大几组、用桶估算分位点。 |
| SLI / 成功者偏差 | SLI 是衡量体验的实际指标；只统计成功者会把还在等待的人漏掉，结果看起来比实际好。 |
| 函数 / 参数 / 返回值 / helper | 一段被调用的处理、传入的数据、处理后交回的结果；helper 是辅助函数。 |
| 函数签名 / 作用域 | 函数参数与返回结果的形式和类型；以及某个变量在哪段代码中能使用。 |
| receiver / `*T` / context | receiver 是方法所属实例，本例为 f、sched 等；*T 是指向 T 的指针；context 传递取消等本轮处理信息，不是 kubeconfig 的 context。 |
| FitError / Queue / QueueSkip / APICacher | FitError 记录本轮没有合适节点；Queue、QueueSkip 是是否值得因某次变化重试的建议；APICacher 管理部分调度器 API 写入，不是 Pod 已绑定的证明。 |
| 导入包名 / `v1` / `fwk` | 导入包提供可使用的常量、类型和函数；本文 v1 指 API 包，PreFilter 等摘录中的 fwk 指框架包。第 14.1.3 节的同名变量 fwk 则是调度配置实例，要按当前作用域判断，不能把点后内容一律读成包名或对象方法。 |
| `v1.ResourceCPU` / `v1.ContainerPort` / `fwk.HostPortInfo` | 分别是包提供的 CPU 资源名常量、端口记录类型、端口账类型，不是本函数临时创建的变量。 |
| struct / 结构体 / 字段 | 把几项相关资料装成一条记录；字段就是其中某一项，例如 Requested。 |
| slice / `[]T` / append | 可按序访问的一组 T 类型记录；append 加入新记录并返回更新后的切片，调用者需要保存返回值。 |
| map / range / `_` | 按键找值的表、逐项读取、忽略本次不用的值；map 的 range 不能假定固定顺序。 |
| `:=` / `!` / `&&` | 简短声明并赋值、取反、两个条件都成立；&& 左边不成立时不算右边。 |
| bool / true / false / string | bool 表示是或否，值为 true 或 false；string 是一段文字。false 是否失败要看函数约定。 |
| nil / error | nil 是指针、接口、切片、map 等类型可使用的空值；error 是表示错误的接口类型。本文 Filter 的 Status 指针为 nil 时表示该阶段成功，含义由返回约定决定。 |
| DeepCopy / `对象.方法()` | DeepCopy 得到该值的独立副本；后者调用对象提供的方法，作用要看具体实现。 |
| goroutine / 共享状态 / 锁 | 可另行推进的 Go 工作、多个流程都可能读写的数据、保护访问顺序的方式；不等同独立 CPU 核。 |
| 编译 / 二进制 / 动态库 / 热加载 | 编译把源码转成程序；二进制在此指可执行程序；动态库是可被程序装载的代码文件；热加载是不重启换入代码，不能假定调度插件支持。 |

### 15.9 查询工具与版本信息

| 词或写法 | 含义 |
|---|---|
| kubectl | 查询和操作 Kubernetes API 对象的命令行工具。 |
| 控制面 / control plane | 负责 API、控制器、调度等管理工作的组件。 |
| kubeconfig / context | 保存连接和身份信息的文件，以及选择集群、身份、默认 namespace 的组合。 |
| Bash / PowerShell | 正文查询示例使用的两种命令解释器。 |
| YAML / JSON / JSONPath | 两种表达对象数据的格式，以及按字段路径从 JSON 中取值的写法。 |
| Git / commit / SHA | 代码版本管理工具、一次提交、提交标识；固定 SHA 用于定位本文引用的源码快照。 |
| EKS / ACK / CloudWatch | AWS 和阿里云的托管 Kubernetes 服务，以及 AWS 的日志与监控服务；可见接口和权限依厂商而定。 |
| 原子快照 / 采样 | 原子快照对应同一状态点；多次查询可能来自不同时刻。采样是特定时刻或时间窗的测量。 |

## 本文依据

概念与配置依据来自下列官方文档；源码链接固定到本文注明的提交。官方滚动文档可能随版本更新，应用到具体集群时需要核对目标版本。

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

- [S19 Pod 数与资源检查](https://github.com/kubernetes/kubernetes/blob/301946d15e67a4a2e8a5fb8292eb836acd366d78/pkg/scheduler/framework/plugins/noderesources/fit.go)
- [S20 评分口径与资源默认值](https://github.com/kubernetes/kubernetes/blob/301946d15e67a4a2e8a5fb8292eb836acd366d78/pkg/scheduler/framework/plugins/noderesources/resource_allocation.go)

[S1]: https://kubernetes.io/docs/concepts/scheduling-eviction/scheduling-framework/
[S2]: https://kubernetes.io/docs/reference/scheduling/config/
[S3]: https://kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle/
[S4]: https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/
[S5]: https://kubernetes.io/docs/concepts/scheduling-eviction/assign-pod-node/
[S6]: https://kubernetes.io/docs/concepts/scheduling-eviction/taint-and-toleration/
[S7]: https://kubernetes.io/docs/concepts/scheduling-eviction/topology-spread-constraints/
[S8]: https://kubernetes.io/docs/concepts/scheduling-eviction/pod-priority-preemption/
[S9]: https://kubernetes.io/docs/concepts/workloads/pods/disruptions/
[S10]: https://kubernetes.io/docs/concepts/workloads/controllers/deployment/
[S11]: https://kubernetes.io/docs/concepts/storage/storage-classes/
[S12]: https://kubernetes.io/docs/concepts/extend-kubernetes/compute-storage-net/device-plugins/
[S13]: https://kubernetes.io/docs/concepts/scheduling-eviction/dynamic-resource-allocation/
[S14]: https://docs.aws.amazon.com/eks/latest/userguide/control-plane-logs.html
[S15]: https://kubernetes.io/docs/concepts/workloads/pods/init-containers/
[S16]: https://kubernetes.io/docs/concepts/workloads/pods/sidecar-containers/
[S17]: https://github.com/kubernetes/kubernetes/blob/301946d15e67a4a2e8a5fb8292eb836acd366d78/pkg/scheduler/schedule_one.go
[S18]: https://kubernetes.io/docs/concepts/scheduling-eviction/resource-bin-packing/
[S19]: https://github.com/kubernetes/kubernetes/blob/301946d15e67a4a2e8a5fb8292eb836acd366d78/pkg/scheduler/framework/plugins/noderesources/fit.go
[S20]: https://github.com/kubernetes/kubernetes/blob/301946d15e67a4a2e8a5fb8292eb836acd366d78/pkg/scheduler/framework/plugins/noderesources/resource_allocation.go

[P1]: https://kubernetes.io/docs/concepts/scheduling-eviction/scheduling-framework/
[P2]: https://kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle/
[P3]: https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/
[P4]: https://kubernetes.io/docs/concepts/workloads/controllers/deployment/
[P5]: https://kubernetes.io/docs/concepts/scheduling-eviction/topology-spread-constraints/
[P6]: https://kubernetes.io/docs/concepts/workloads/autoscaling/horizontal-pod-autoscale/
[P7]: https://kubernetes.io/docs/concepts/cluster-administration/node-autoscaling/
[P8]: https://kubernetes.io/docs/reference/instrumentation/metrics/
[P9]: https://prometheus.io/docs/prometheus/latest/querying/functions/#histogram_quantile
[P10]: https://kubernetes.io/docs/reference/scheduling/config/
[P11]: https://github.com/kubernetes/kubernetes/blob/301946d15e67a4a2e8a5fb8292eb836acd366d78/pkg/scheduler/schedule_one.go
[P13]: https://github.com/kubernetes/kubernetes/blob/301946d15e67a4a2e8a5fb8292eb836acd366d78/pkg/scheduler/metrics/metrics.go
[P12]: https://kubernetes.io/docs/concepts/scheduling-eviction/scheduler-perf-tuning/
[G1]: https://kubernetes.io/docs/concepts/extend-kubernetes/compute-storage-net/device-plugins/
[G2]: https://kubernetes.io/docs/concepts/scheduling-eviction/dynamic-resource-allocation/
[G3]: https://docs.nvidia.com/datacenter/cloud-native/gpu-operator/latest/gpu-sharing.html
[G4]: https://docs.nvidia.com/datacenter/cloud-native/gpu-operator/latest/gpu-operator-mig.html
[G5]: https://github.com/kubernetes/kubernetes/tree/301946d15e67a4a2e8a5fb8292eb836acd366d78/pkg/scheduler
[G6]: https://kubernetes.io/docs/concepts/scheduling-eviction/scheduling-framework/
[G7]: https://kueue.sigs.k8s.io/docs/concepts/workload/
[G8]: https://volcano.sh/docs/scheduler/plugins/gang/
[G9]: https://kueue.sigs.k8s.io/docs/concepts/topology_aware_scheduling/
[G10]: https://kueue.sigs.k8s.io/docs/concepts/workload_priority_class/
[G11]: https://kubernetes.io/docs/concepts/scheduling-eviction/gang-scheduling/
[G12]: https://kubernetes.io/docs/tasks/manage-gpus/scheduling-gpus/
[G13]: https://github.com/NVIDIA/k8s-device-plugin#configuration-option-details
[G14]: https://github.com/kubernetes/kubernetes/blob/301946d15e67a4a2e8a5fb8292eb836acd366d78/pkg/scheduler/framework/plugins/noderesources/most_allocated.go
[G15]: https://v1-34.docs.kubernetes.io/docs/concepts/scheduling-eviction/dynamic-resource-allocation/
[N1]: https://github.com/kubernetes/kubernetes/blob/301946d15e67a4a2e8a5fb8292eb836acd366d78/staging/src/k8s.io/apiserver/pkg/admission/plugin/resourcequota/controller.go
[N2]: https://github.com/kubernetes/kubernetes/blob/301946d15e67a4a2e8a5fb8292eb836acd366d78/pkg/controller/controller_utils.go
[N3]: https://github.com/kubernetes/kubernetes/blob/301946d15e67a4a2e8a5fb8292eb836acd366d78/pkg/apis/core/v1/defaults.go#L174-L178
[N4]: https://github.com/kubernetes/kubernetes/blob/301946d15e67a4a2e8a5fb8292eb836acd366d78/plugin/pkg/admission/limitranger/admission.go
[N5]: https://github.com/kubernetes/kubernetes/blob/301946d15e67a4a2e8a5fb8292eb836acd366d78/pkg/scheduler/framework/plugins/nodeports/node_ports.go#L170-L178
[N6]: https://github.com/kubernetes/kubernetes/blob/301946d15e67a4a2e8a5fb8292eb836acd366d78/staging/src/k8s.io/kube-scheduler/framework/types.go
[N7]: https://github.com/kubernetes/kubernetes/blob/f28b4c9efbca5c5c0af716d9f2d5702667ee8a45/pkg/scheduler/framework/plugins/nodeports/node_ports.go
[N8]: https://github.com/kubernetes/kubernetes/blob/301946d15e67a4a2e8a5fb8292eb836acd366d78/pkg/scheduler/framework/plugins/noderesources/fit.go
[N9]: https://github.com/kubernetes/kubernetes/blob/301946d15e67a4a2e8a5fb8292eb836acd366d78/pkg/scheduler/framework/runtime/framework.go
[N10]: https://github.com/kubernetes/kubernetes/blob/301946d15e67a4a2e8a5fb8292eb836acd366d78/pkg/scheduler/eventhandlers.go
[N11]: https://github.com/kubernetes/kubernetes/blob/301946d15e67a4a2e8a5fb8292eb836acd366d78/pkg/scheduler/backend/queue/scheduling_queue.go
[N12]: https://github.com/kubernetes/kubernetes/blob/301946d15e67a4a2e8a5fb8292eb836acd366d78/pkg/scheduler/framework/plugins/defaultbinder/default_binder.go
[N13]: https://github.com/kubernetes/kubernetes/blob/f28b4c9efbca5c5c0af716d9f2d5702667ee8a45/staging/src/k8s.io/api/resource/v1/types.go
[N14]: https://github.com/kubernetes/kubernetes/blob/f28b4c9efbca5c5c0af716d9f2d5702667ee8a45/pkg/scheduler/framework/plugins/dynamicresources/dynamicresources.go

[N15]: https://github.com/kubernetes/kubernetes/blob/301946d15e67a4a2e8a5fb8292eb836acd366d78/staging/src/k8s.io/kube-scheduler/framework/interface.go
