# Kubernetes Scheduler：Pod 为什么放不下，怎样用数字和源码查清楚

> 2026-10-02 优化版。你已有 K8s 运维经验，本文从你熟悉的 Java 应用发布讲起；Go 和调度器内部流程在用到时解释。
>
> 读完要能回答三件事：Pod 卡在哪一步？哪个条件没满足？改这个条件以后，会影响什么？先自己算，再看实验，最后用一小段源码核对。
>
> 调度对照已经在云端 kind / Kubernetes v1.34.0 上实际运行。kind 是把 Kubernetes 节点跑在容器里、方便搭学习集群的工具。复跑步骤写在本文后半部分；哪些是实测、哪些只是教学推演，会在例子旁写明。GPU 和真实业务性能另需设备与应用环境。

## 0. 先选对学习路线

下面所有讲解、源码和实验都在本文里。先按问题读，不要求第一遍读完整份文档。

| 阶段 | 本文位置 | 过关标准 |
|---|---|---|
| 第一遍：解释眼前的 Pod | 第 1–5 章；不熟 Go 时先读 3.2.2 的 CPU 判断 | 分清没创建、没节点、没启动，并逐节点算出为什么放不下 |
| 第二遍：串起发布与调度 | 第 6–11 章 | 能解释副本分布、发布占用、抢占，以及谁等谁更新状态 |
| 做容量和变更方案 | [第 12 章](#production) | 算出故障后能接多少流量、发布时还缺几个位置 |
| 从 Java 迁移到 GPU | [第 13 章](#gpu) | 分清设备数量、具体设备、显存、队列和应用验证 |
| 沿源码继续追 | [第 14 章](#source) | 找到输入、判断、返回结果及失败后重试的位置 |
| 亲手验证 | [第 15 章](#experiments) | 先预测，再只改一个条件，保存 UID 和状态变化 |
| 对照实际结果 | [第 16 章](#verification) | 区分实测、教学算例和仍未运行的部分 |
| 遇到专业词 | [第 17 章：术语速查](#terms) | 用一句大白话复述它做什么，再回到原例子 |

**想先学会一个完整例子：**读 1.2 判断卡在哪一步，再读 3.1—3.2 算 CPU 余额；完成 15.1 的准备后，做[第 15.2 节 CPU 实验](#cpu-lab)。看到同一只 Pod 从等待变为 Ready，再读[第 14.1 节源码跟读](#cpu-source-walk)。后两部分使用同一组数字，不要求先学完 GPU 和所有内部机制。

下文 `activity` 是 Java/Spring Boot 教学服务，数字用于推演，没有连接公司生产集群。标为“云端实测”的段落来自独立实验集群，版本与结果直接写在本文第 16 章。

### 0.1 版本与命令约定

日常原理以 Kubernetes 官方文档为依据；源码进阶沿用并核验原文固定提交 `301946d15e67a4a2e8a5fb8292eb836acd366d78`。固定提交就是固定到这一份代码快照，后续源码变化不会悄悄改变本文依据。

开发提交的功能与默认设置不代表你的生产版本。现场先保存 `kubectl version -o yaml`、发行版、schedulerName、可见配置和采集时间。`schedulerName` 是这只 Pod 交给谁调度的名字，第 12.6 节再讲多个调度器。[S1][S2]

正文命令以 Bash 为主，关键只读取证同时给 PowerShell。第 15 章在 Linux/WSL 的 Bash 中运行，用 Python 3.9+ 标准库生成实验对象，实际操作由 kubectl 完成；不需要下载配套脚本。实验使用自己创建的 kind 集群与专用 kubeconfig。

Bash 和 PowerShell 是执行命令的工具；WSL 是在 Windows 里运行 Linux 环境的方式。kubeconfig 保存集群地址、访问身份等信息；其中的 context 是一组“集群、身份、默认 namespace”的选择，先核对它才能知道命令会查哪套环境。

正文只读命令不改变 Kubernetes 对象，但导出的 YAML 可能包含内部地址、环境变量与租户信息。源码摘录使用固定提交，中文注释是教学新增；上游代码归 Kubernetes，许可证与归属见[第三方说明](../../THIRD_PARTY_NOTICES.md)。

---

## 1. 先弄清：Scheduler 到底负责哪一段

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

读后面的现场时，用 `spec` 看对象的要求，用 `status` 看组件报告的结果。`metadata.uid` 标识这一次创建的对象；删掉再建，即使名字相同，UID 也会换。第 15.2 节就用它证明“还是原来那只 Pod 在重试”。

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

**此处先记一句：先看 nodeName，再判断该找谁。**但也不要倒过来认为 nodeName 有值就证明一定经过了默认 scheduler：手工设置 nodeName 等特殊路径会绕过它。[S5]

**云端实测：**第 15.6 节的坏镜像对照中，Pod 已有 nodeName，但镜像地址不存在，随后出现 `ErrImagePull` / `ImagePullBackOff`。这时选节点已经完成，卡住的是节点拉镜像。另一次 CPU 对照实验刚完成绑定时，Pod 的 phase 仍是 Pending，过一会儿才启动。两种现象都说明：只看 Pending 很容易找错方向。

### 1.3 同一 Pod 不会因为节点更空就自动搬家

常规调度为一个 Pod 对象选择一次节点。Score 就是对可行节点打分，权重决定某项分数在总分中占多大影响。已经绑定的 Pod 不会因新增节点、修改 Score 权重或节点标签变化，自动重新摆放。控制器重建出来的是新的 Pod 对象，具有新的 UID。Descheduler 是按策略挑选已有 Pod 并发起驱逐的工具；驱逐是让旧 Pod 退出，后续是否重建由其控制器决定。[S3][S5]

**自检：**一个 Pod 有 nodeName，事件是 FailedMount。第一步是调高 scheduler 日志还是检查卷与 CSI？答案是后者；修改调度权重不能修复已发生的挂载错误。

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

第 15.3 节的配额实验先等配额生效，再创建两副本 Deployment。它要求同时看到：一只 Pod Ready，ReplicaSet 出现原因包含 `exceeded quota` 的 FailedCreate Event，而且 Event 的对象 UID 对应这只 ReplicaSet。

随后只把配额从 150m 改为 200m。控制器再次尝试后，两只 Pod 都应 Ready，原来的 Ready 副本 UID 保留。改变配额只解除这一项拒绝；真正的节点资源、镜像和启动条件仍要满足。

**对应源码怎么找：**固定教学提交中的 ResourceQuota `CheckRequest` 用已有用量加本次申请量，与 `status.hard` 比较；超过就返回 Forbidden。创建调用返回错误后，`RealPodControl.createPods` 在所属 ReplicaSet 上记录 FailedCreate；ReplicaSet 后续还会重试。[N1][N2]

**自检：**如果你只给节点池扩容，配额仍是 150m，第二只会不会因此创建成功？为什么不能只查 `describe pod` 找到这条错误？

<details>
<summary>先回答，再展开</summary>

扩节点没有改变配额，仍不能通过这项创建检查。第二只 Pod 还不存在，要查已有的 ReplicaSet、其 Event 和配额。FailedCreate 也不总是配额问题，消息可能指向其他准入拒绝；必须读实际原因。

</details>

---

## 2. 第一个值班动作：保留证据，不要先删 Pod

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

## 3. CPU 明明很闲，为什么还会 Pending

### 3.1 四个数字不能混

先记一句：**scheduler 查的是已经申请了多少，不是此刻实际用了多少。** `request` 会进入调度资源账；容器此刻只 sleep，也不会让这笔请求自动归零。

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

GC 是 JVM 清理不再使用的对象、回收内存的工作；JIT 是把常用 Java 代码转成更快机器代码的工作。启动和负载变化时，它们可能带来额外 CPU、内存需求，所以只看平稳时的平均用量容易漏掉高峰。

#### 3.2.1 云端实测：只差 1m，结果就不同

这次实验不用 Java 镜像，容器只执行 sleep，专门验证请求账。目标节点可分配 `5000m`，系统 Pod 已申请 `100m`，一只已 Ready 的 holding Pod 申请 `4700m`。

```text
余额 = 5000 - 100 - 4700 = 200m
新 Pod 请求 201m：201 > 200，调度失败
删除 holding，原来的 201m Pod 不变：余额回到 4900m，同 UID 绑定并 Ready
删除已运行的 201m Pod，再恢复 holding：余额回到 200m
新建请求 200m 的 Pod：200 没有超过余额，绑定并 Ready
```

先前一次云端对照读取 kubelet stats 时，节点 CPU 采样约为 `13.2m`，holding 为 `0m`。这是当时的采样，不是全过程峰值。机器很闲和请求账只剩 200m，可以同时成立。

[第 15.2 节](#cpu-lab)分别验证超过节点 CPU Allocatable、超过当前余额，以及释放占用后同一只 Pod 重新成功。201m 与 200m 的比较会先恢复相同占用，不把空节点上的成功当边界证据。复跑时命令根据你的节点和已有 Pod 重新计算，CPU 采样值每次也可能不同。

**先预测再运行：**把新请求从 201m 改为 200m，只改变了哪一项？CPU 通过以后，能否直接说 Java 服务已恢复？不能，还要验证启动、探针和业务请求。

<a id="cpu-source"></a>

#### 3.2.2 首遍源码：把 201 > 200 代进真实判断

这段只回答“新 Pod 的 CPU 请求为何被拒绝”。来源为固定提交 `301946d15e67a4a2e8a5fb8292eb836acd366d78` 的 [fit.go / fitsRequest 连续摘录](https://github.com/kubernetes/kubernetes/blob/301946d15e67a4a2e8a5fb8292eb836acd366d78/pkg/scheduler/framework/plugins/noderesources/fit.go#L668-L677)。保留真实语句，只增加中文注释和缩进；不是可独立编译程序。

先把第 3.2.1 节实测数字代进来：`podRequest.MilliCPU=201`，节点可分配 5000m，已请求 4800m，余额 200m。你要在下面找的就是 `201 > 5000-4800`。

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
        Used:         nodeInfo.GetRequested().GetMilliCPU(), // 已有请求，不是实测消耗。
        Capacity:     nodeInfo.GetAllocatable().GetMilliCPU(), // 这里取的是 Allocatable。
        Unresolvable: podRequest.MilliCPU > nodeInfo.GetAllocatable().GetMilliCPU(), // 清空其他请求仍放不下？
    })
}
```

**大白话总结：**

- 输入：新 Pod 的 CPU 请求和候选节点的请求账。
- 判断：请求是否超过 `可分配量 - 已有请求`。
- 动作：超过就加一条 `Insufficient cpu` 记录，保存请求、已有请求和容量。
- 结果：这段没有删除 Pod，也没有直接绑定或返回；后面还能检查内存。

把 201m 改成 200m，`200 > 200` 不成立，就不会加这条 CPU 不足记录。CPU 通过不代表其他资源也通过。

**第二遍再读 Unresolvable：**新请求 201m 小于节点可分配的 5000m，所以该字段为 false，释放别的请求可能改善余额；若新请求为 6000m，则为 true，删光其他 Pod 也凑不出 6000m。它只说“靠删除别的 Pod 解决不了这个节点的容量问题”，不表示扩容或修改规格都没用。

**顺手学 Go：**先看两个挡路的写法：

这里的切片可以先理解为“按顺序放记录的一组列表”，不是另一份 Kubernetes 对象。

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

固定源码中，只要一条资源记录的 Unresolvable=true，就选用 UnschedulableAndUnresolvable；否则资源不足用 Unschedulable。没有不足记录时，Filter 返回 nil，在这个 Status 接口中表示成功。若读取前置状态失败，则返回 Error，需要另查执行异常。

**大白话总结：**一个资源插件内部可以同时记下 CPU 和内存不足；框架仍会在该插件失败后停下后续插件。这两个判断说的是不同范围。

这段 CPU 判断与 Unresolvable 分流已同时核对教学提交和实验 v1.34.0 的[发布源码](https://github.com/kubernetes/kubernetes/blob/f28b4c9efbca5c5c0af716d9f2d5702667ee8a45/pkg/scheduler/framework/plugins/noderesources/fit.go)。已核对的两个版本在此处结论一致；其他版本仍应查对应实现。真实源码与许可证归 Kubernetes 上游，见[第三方说明](../../THIRD_PARTY_NOTICES.md)。

### 3.3 调度器看的是最终 Pod，不是 Git 里的一小段模板

先翻译下面几个名字：limit 是运行时资源上限；LimitRange 是 namespace 内用于设置默认资源值和单个对象资源范围的规则；RuntimeClass 用来选择运行容器的方式。overhead 是这类运行方式额外需要的 Pod 开销，算请求时也要计入。webhook 则是保存对象时调用的外部检查或修改程序，它也可能改变输入。

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

**第二遍再读：原生 sidecar 会一直运行。**它是 init 列表中 `restartPolicy: Always` 的容器，后面的 init 启动时它还在。因此要算“这个阶段有哪些容器同时运行”。假设按顺序先启动日志 sidecar `200m/256Mi`，再运行普通 init `1500m/512Mi`，业务容器为 `1000m/1Gi`，没有其他资源设置：

| 阶段 | 同时运行的容器 | CPU / 内存请求 |
|---|---|---|
| 启动日志 | 日志 sidecar | 200m / 256Mi |
| 初始化业务数据 | 日志 sidecar + 普通 init | 1700m / 768Mi |
| 运行业务 | 日志 sidecar + 业务容器 | 1200m / 1280Mi |

CPU 峰值是 1700m，内存峰值是 1280Mi，最后再加适用的 overhead。init 顺序会改变重叠关系。Pod-level resources、原地 resize 也有版本规则；遇到这些情况再查 `resource.PodRequests`，不要拿简单求和命令当通用计算器。[S16]

Pod-level resources 是直接在整个 Pod 层设置资源预算；原地 resize 是保留原 Pod、调整运行中资源配置的能力。两者都要核对目标版本，不能把“每个容器直接相加”套到所有场景。

<a id="default-requests"></a>

#### 3.3.1 四组对照：只写 limit，最终 request 是多少

Java 团队在 Deployment 模板里只写了 CPU limit=300m，没有 CPU request。你查看模板确实找不到该 request，但最终 Pod 可能有 `requests.cpu=300m`。

这是两个对象在不同阶段的输入。Pod 创建时，缺少的同资源 request 会按 limit 补齐；已写明的 request 不会被这条默认规则覆盖。scheduler 看的是最后保存的 Pod。[N3]

第 15.4 节做四组对照，所有组都不使用 Pod-level resources 或修改 CPU 的额外 webhook：

| 组 | namespace 的 CPU defaultRequest | 提交时 CPU request / limit | 要核对的最终 CPU request |
|---|---|---|---:|
| A：Deployment 创建的 Pod | 无 | 未写 / 300m | 300m；Deployment 模板仍未写 CPU request |
| B：明确填写 request | 无 | 100m / 300m | 100m |
| C：两项都未写 | 100m | 未写 / 未写 | 100m，由 LimitRange 补上 |
| D：有 namespace 默认值，仍只写 limit | 100m | 未写 / 300m | 300m |

D 最容易看错。在本次核对的路径中，Pod 默认处理先把 limit 复制到缺少的 request；LimitRange 后面只补仍然缺少的项，不把已有的 300m 覆盖成 100m。不要仅看一份 LimitRange 就断定所有 Pod 都申请了 100m。[N3][N4]

对 Java 服务，还要分别考虑：request 影响调度承诺；CPU limit 可能造成运行时 throttling。明确写 100m/300m 只说明两项配置各是多少，是否适合 JVM 启动、JIT 和 GC，仍要靠真实应用数据。

#### 3.3.2 顺手读源码：只在缺少时复制

这段只回答“已有的 CPU request 会不会被 limit 覆盖”。本地源码在 `/workspace/kubernetes`，固定教学提交为 `301946d15e67a4a2e8a5fb8292eb836acd366d78`，describe 为 `301946d1`，工作区干净；实验 server 是 v1.34.0，提交为 `f28b4c9efbca5c5c0af716d9f2d5702667ee8a45`。生产分析使用目标版本。

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

**大白话总结：**输入是创建中的 Pod；判断某资源的 request 是否缺少；缺少就复制该资源的 limit。它修改的是这次 Pod 的默认输入，没有替你回写 Deployment 模板，也没有证明节点放得下。

**顺手学 Go：**map 像一张“按资源名找数量”的表；`range` 在这里逐项读取它，`key` 是资源名，`value` 是数量。`_, exists := map[key]` 同时返回“值”和“是否存在”，`_` 表示值这次不用。分号前先查，分号后的 `!exists` 表示“不存在时才执行”。`DeepCopy()` 得到数量的副本。

**反事实题：**把 B 的显式 request 从 100m 改为 200m，limit 仍为 300m，默认处理会把 request 变成多少？答案是保留 200m，仍需后续资源检查。

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

**实践：**按第 15.2 节先比较超过节点可分配量的请求，再比较余额 200m 时的 201m 与 200m。前者查 Allocatable，后者查已有请求与余额。若 Pod 创建时就被 Quota 拒绝，还没有走到 scheduler 的资源检查。

---

### 3.6 合上答案，独立完成一次资源验算

沿用第 3.3 节第一个算例（普通常驻 sidecar，非原生 sidecar）的有效请求 `2000m / 4352Mi`，明确再加 Pod overhead `100m / 128Mi`，得到 `2100m / 4480Mi`。

下表是已经扣除现有请求后的余额。假定节点健康、Pod 数未满，标签、污点、卷等其他硬条件全部满足，且没有并发资源变化。

| Node | CPU 余额 | 内存余额 |
|---|---:|---:|
| a | 2300m | 4608Mi |
| b | 3000m | 4096Mi |
| c | 1800m | 6144Mi |

先写答案：每个节点能否接纳，失败差额是多少？第一只占账后，第二只相同 Pod 能否继续放入？若只给 b 增加 512Mi 可分配内存、现有请求不变，结果如何？

<details>
<summary>完成计算后再展开答案</summary>

a 两维通过，放入后剩 `200m / 128Mi`；b 内存差 384Mi；c CPU 差 300m。第一只只能在 a，第二只没有位置。给 b 增加 512Mi 后，内存余额变成 4608Mi，CPU 和内存都通过。

不能用三个节点合计余额求解，也不能由此承诺真实集群扩了内存就必定成功；结论依赖题目中“其他硬条件均通过”的假设。

</details>

**过关标准：**统一单位、算出最终请求、逐节点比较，并写出假设。只答出节点名称尚未完成验算。

---

## 4. 一台节点必须同时满足哪些条件

### 4.1 一张表对应一个排查方向

nodeAffinity 是节点亲和，用节点标签表达想去哪里；podAffinity 是 Pod 亲和，用其他 Pod 的位置表达想靠近谁；podAntiAffinity 是 Pod 反亲和，表达想与谁分开。topologySpreadConstraints 是分布规则，用来限制同一组副本在节点或可用区之间偏得太多。表里的 Pod slots 只是“还能放几只 Pod”的位置数量。

taints/tolerations 就是节点的拒绝条件与 Pod 的接受声明，第 4.3 节用数字和配置说明。profile 是同一个调度器进程里的一套规则配置；addedAffinity 是这套配置额外要求的节点条件，Pod 自己的 YAML 未必显示它。

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

**实践：**第 15.6 节的污点对照先证明“有 required label，但不容忍 taint，仍被拒绝”，再仅改变 toleration，观察同一目标节点变为可行。不要用一次随机落点证明 toleration 的吸引力。

---

## 5. 贯穿案例：一次 activity 发布为什么卡住，又为什么恢复

### 5.1 先给出现场，再逐节点算

这是 Java/Spring Boot 教学场景：`activity` 发布后创建了新 Pod，要求 `1000m CPU / 4Gi`，必须进入 `pool=online`，没有 GPU 专池 toleration。request 来自已经通过准入的最终 Pod。JVM 尚未启动，readiness 也还没机会检查；先解决节点放置问题，再验证预热和接口。

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

**二遍提醒：**资源评分路径可能对未填写 CPU、内存 request 的容器使用非零默认值；Filter 与 Score 的输入口径需要分别核对。先算对显式 request 的本例，再追踪插件配置与评分实现，不把简例当万能公式。[S20]

### 5.4 “更喜欢 d”不等于“一定去 d”

教学示意：

```text
插件 A：d=62，e=31，权重=1
插件 B：d=0，e=100，权重=2
总分：d=62，e=231
```

最终 e 更高。这里 B 的分数是已完成该插件所需归一化后的示意值，不是说业务填写 preferred.weight=100 就一定直接贡献 100 分。

有些插件先把自己算出的值换算到框架要求的分数范围，这步叫归一化。只有提供相应扩展的插件才做这一步，NodeResourcesFit 没有提供该扩展。CPU、内存在一个插件里的权重，与 Framework 给整个插件的权重，也要分开算。[S1][S2][S18]

**自检：**一个节点有未容忍的硬污点，能否通过给它增加 10000 分获救？不能，它不在评分候选集中。

---

## 6. 多副本可靠性：先理解拓扑，再选硬规则还是软规则

### 6.1 hostname 分散不等于跨可用区

三只 Pod 分别在三台 Node 上，但三台 Node 同属一个可用区，仍可能在一次 AZ 故障中一起受影响。`kubernetes.io/hostname` 与 `topology.kubernetes.io/zone` 是不同故障域。[S7]

AZ 就是可用区；故障域是可能被同一次故障一起影响的一组位置。拓扑在这里指按 hostname、zone 等标签把节点分组，topologyKey 就是用哪一个标签分组。同标签值属于同一个域，例如 zone=a 的节点都算 a 域。

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

**这份清单没有承诺一定覆盖三个可用区。**未配置 `minDomains` 时，效果相当于 `minDomains: 1`。若仅有一个 eligible zone，已有两只匹配 Pod，下一只仍可满足 `2+1−2=1`，三只可能都在同区。硬分散限制参与计算的域之间的差值，不会创造可用区。[S7]

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

#### 6.2.1 云端实测：两个域，minDomains=3 会怎样

独立 kind 集群的两个 worker 临时标为 a、b 域；每只 Pod 只申请 `20m` CPU。每组从零个副本开始，创建三个副本，指定同一组的标签、`maxSkew=1` 和 `DoNotSchedule`。

| 唯一要比较的配置 | 实际结果 |
|---|---|
| 省略 minDomains，按默认 1 计算 | 分布为 2/1，三只均 Ready |
| minDomains=3 | 分布为 1/1，第三只没有 nodeName，报告拓扑分布条件不满足 |

为什么不是第一只就失败？在第二组，参与域只有两个，小于三个，基准按零。第一只放 a：`0+1−0=1`；第二只放 b 也是 1；第三只放任一域都是 `1+1−0=2`，这时才超过 maxSkew。

第 15.7 节给出两域复跑步骤。临时标签能验证这个算法；两个 worker 仍在同一宿主机上，实际 AZ 故障需要另做实验。

### 6.3 某个域没位置时，是等待还是允许集中

硬规则不满足，就不给新 Pod 选这个位置，因此某域容量不足时可能卡住发布。软规则只是偏好，必要时可以集中放置，但也不能保证总是均匀。

做方案前先回答：一个域故障后，业务需要保住多少副本？存活域放得下吗？宁愿部分 Pending，还是允许暂时集中？分散策略、存活域容量和流量摘除要一起考虑。

若业务希望按至少三个域计算，可在受支持版本的 zone 硬约束中明确设置 `minDomains: 3`。但这不是“少于三域就拒绝所有 Pod”：两个空域仍可先各放一只；当计数为 `1/1` 时，第三只放任一域都会得到 `1+1−0=2`，超过 maxSkew=1，因此等待。这就是最低域数目标带来的容量和恢复取舍。[S7]

`nodeTaintsPolicy` 未配置时相当于 Ignore，所以被污点拒绝的新 Pod 仍可能受该节点所属域的计数影响。“不能放置的节点”与“不参与统计的节点”是不同集合。[S7]

**必须会回答：**节点列表缩小、节点仅 NotReady、节点带 taint、节点被删除，这几种变化对 eligible domains 是否相同？不能凭直觉说相同，应核对实际策略和对象状态。[S7]

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

**实践：**第 15.8 节在隔离节点用请求账制造“旧 Pod Ready、新 Pod Pending”，再受控改变滚动策略。学习目的包含说明这个解法为什么不能直接用于单副本生产服务。

### 7.4 云端实测：更新卡住时，旧副本仍然 Ready

这一组和前面的 `1 CPU/4Gi` 教学题是两个例子，先看本组输入：节点可分配 `5000m`，系统 Pod 请求 `100m`，每个实验副本请求 `2751m`，只 sleep。

```text
只运行旧副本：100 + 2751 = 2851m，放得下
新旧同时存在：100 + 2751 × 2 = 5602m，放不下
```

`maxSurge=1, maxUnavailable=0` 时，实际看到旧副本 Ready，新副本没有 nodeName，事件里包含 `Insufficient cpu`。更新没完成，但不能据此说旧副本已经退出。

实验再改成 `maxSurge=0, maxUnavailable=1`：旧副本开始删除，Deployment 的 `availableReplicas` 采样降为 0，后来新副本绑定、Ready，更新完成。这个改法通过允许先下旧副本释放位置，代价是可能出现不可用窗口。

程序没有 Service 和 HTTP 探测。这里证明的是副本和发布状态，没有测出业务中断时长；在 Java 服务上还要检查 JVM 预热、readiness、退出摘流量及接口请求。

---

## 8. 别只看 CPU：卷、端口和节点可用性也能堵住调度

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

配置 `hostPort: 18080` 则是在申请节点上的端口位置。实验只选同一 worker，先让 A 申请 TCP 18080 并 Ready；再让 B 申请同一个 hostPort。B 即使只请求 50m、CPU 余额充足，也会被端口条件挡住。

本例没有填写 hostIP，检查时按 `0.0.0.0` 处理；两只都是 TCP。实际冲突判断还考虑 hostIP 和协议，不能把任何相同端口数字都判成冲突。[N5][N6]

第 15.5 节的端口实验再做两个动作：先创建只声明 `containerPort: 8080` 的 C，核对它 Ready；再删除 A，等同一个 UID 的 B 绑定并 Ready。CPU request 没改，变化是 A 原先申请的宿主机端口位置释放了。

这里还有个关键区别：sleep 容器没有监听 Java 8080。程序验证的是 Pod 声明进入调度端口账，不是用 `ss` 扫描宿主机上所有进程。你看到系统里某个 socket 空闲，也不能代替这个判断；同样，调度通过也不能保证真实应用的 bind/listen 成功。

socket 是程序收发网络数据的端点；网络 bind 是给它指定地址和端口，listen 是开始等连接。这里的网络 bind 与 Kubernetes 把 Pod 绑定到节点的 Bind 是两件事。`ss` 用来查看系统里的网络端点和连接状态。

#### 8.3.1 第二遍源码：找到一项冲突就返回 false

这是固定教学提交 `pkg/scheduler/framework/plugins/nodeports/node_ports.go / fitsPorts` 的完整函数，保留所有分支，只增加中文教学注释；它依赖上游类型，不能单独编译。[N5]

参数 `wantPorts` 是这只 Pod 的 hostPort 声明列表；`portsInUse` 是候选节点上已计入的端口账。你要找的是：B 的 TCP 18080 与 A 的账冲突时，在哪一行停止。

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

**大白话总结：**输入是想申请的端口和节点端口账；判断是否冲突；发现一项就返回 false。函数只读取，不删 Pod、不改端口，也不调用 API。

**顺手学 Go：**`[]v1.ContainerPort` 是一组端口记录。`for _, cp := range wantPorts` 中 `_` 忽略序号，`cp` 是本次记录；`string(cp.Protocol)` 把协议值转成字符串。这里的 false 是布尔结果，不是 error，也不是 Pod phase。

结果继续传递：`fitsPorts=false` → `NodePorts.Filter` 返回 Unschedulable 和原因 → 该节点的 Filter 检查失败。PreFilter 状态读取出错则走 Error；成功时 Filter 返回 nil，在这个接口里表示成功。没有 hostPort 的 Pod 在前面的 PreFilter 就会 Skip 这个插件，不能把每只 Pod 都画成跑完此函数。

v1.34.0 的 helper 参数与教学提交不同：它先接收 nodeInfo，再从中取端口账；冲突就返回 false 的判断一致。本次分别核对两个版本，没有把教学签名当成所有版本通用。[N7]

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

`status.nominatedNodeName` 是潜在落点提示，不是 API 已绑定，不是不会改变的设备或节点锁。具体删除与提名的可观察先后依版本及异步实现变化，不把教学箭头当严格事件时间承诺。[S8][S17]

### 9.2 抢占能改什么，不能改什么

删除其他 Pod 可能释放 CPU、内存、hostPort 或传统扩展资源请求；它不会自动改变 required 标签、污点关系、卷所在可用区，也不能把最大 8 卡的单节点变成 10 卡。[S8]

PDB 在调度抢占中是尽力遵守，不是绝对免死；drain 的 Eviction 路径对 PDB 的处理又不同。节点故障也不受 PDB 绝对保护。[S8][S9]

`preemptionPolicy: Never` 让高优先级 Pod 不主动抢占，但它仍有较高排队优先级，也可能被更高优先级 Pod 抢占。不要通过不断提高 priority 来替代容量规划。[S8]

### 9.3 实验必须先占位，再制造竞争

先创建 low，等它绑定并 Ready，确认位置已经占上。然后创建高优先级的 Never 对照，证明它放不下且 low 仍存活；删除对照，再创建允许抢占的 high。两个高优先级配置数值同为 20000，都高于 low=100，只比较是否允许抢占。最后分别检查 low 消失、high 绑定、high Ready。

本次实测还看到 low 的 `Preempted` 事件，消息中提到的 high UID 与实际 high 一致。保存这两个 UID，才能把“旧 Pod 被谁抢占”和“哪只新 Pod 绑定”对应起来。若把高低 Pod 一起 apply，高者可能先占到位置，那就没有发生抢占。

设置 terminationGracePeriodSeconds 只规定退出预算，不保证进程一定等满该时长。本套实验额外使用受控 preStop，才能更容易观察退出窗口。

terminationGracePeriodSeconds 是给容器正常退出的时间预算；preStop 是退出前执行的处理，本例让它 sleep 一会儿。它也占用退出预算，不能当成另加一段无限等待。优雅退出是让应用尽量完成清理和已有请求，而不是保证所有请求都成功。

---

## 10. 到这里再看内部流程：每个复杂机制都回答一个实际问题

Framework 可以先理解为调度器按固定阶段调用规则的框架；插件是负责某类判断的实现，例如 NodeResourcesFit 检查资源，NodeAffinity 检查节点亲和。不是每个阶段都必须配置自定义插件。

### 10.1 为什么 scheduler 不每次都远程查全量 Pod

假设集群有一万只 Pod，每选一次节点都远程读完它们，API 查询很容易拖慢调度。所以 scheduler 在内存中维护 Pod 和 Node 的资料；API 有变化时，再更新这些资料。

这条变化通知由 informer 接收，整理后的内存状态叫 cache；本轮判断所用的节点视图叫 snapshot。先理解它们各做什么，再记名字。[S1][S17]

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

**大白话总结：**先在自己账上扣掉 A 的请求，下一只 B 就不会重复使用这 1000m。这笔临时账只属于当前调度器进程，另一个独立 scheduler 不会自动共享；它也没有锁住具体 GPU UUID。[S17]

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

**第二遍再读：**资源也可能恰好在本轮计算时释放。如果 Pod 随后才记为失败，调度器要避免把这次有用变化漏掉。再去读 in-flight 事件跟踪和队列代码；第一遍先能解释“为什么等，以及什么变化可能让它再试”。[S17]

### 10.5 先读懂成功、拒绝、出错和等待

Unschedulable 表示条件当前不满足；Error 表示执行或依赖遇到异常，两者的运维处理不同。Permit 的 Wait 是绑定前协调等待，不等于 Pod Pending phase。开发版本中的其他状态码留到固定源码与测试一起读。[S1][S17]

**过关题：**A 的 Bind 失败后，为什么仅让 A 重试还不够？因为 A 的临时占账可能让 B 也失败；释放与唤醒需要共同考虑。

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

### 11.1 必须能独立完成的三份作业

**作业 A：新 Pod 不存在。**给出一个配额拒绝导致 ReplicaSet FailedCreate 的例子，证明它没有进入 scheduler；说明为什么新增 Node 不一定有用。

先用第 1.4 节推导，再按第 15.3 节复跑。配额、ReplicaSet 和 Event 的变化都要保留。

**作业 B：资源与拓扑联合失败。**给出三个节点的 labels、taints、allocatable、requests、同伴 Pod 计数；不运行变更，先算出每个节点为何失败，再只改变一个条件预测新结果。

**作业 C：发布与终止重叠。**保存新旧 ReplicaSet、Pod UID、nodeName、deletionTimestamp 和 requests；分别计算首波 surge 与终止期间真实占用，说明哪种修复会影响可用性。

评分不看术语多少，看输入是否齐全、推理能否复算、证据是否支持结论，以及修复是否明确代价。

每份作业交一页诊断记录：UID 与采集时间、实际输入、逐节点判断、修复方案及代价、变更后的三层验收。不要只交最终截图。

自评 10 分：责任方 2 分、单位与请求计算 2 分、硬条件交集 2 分、证据时间线 2 分、修复与验收 2 分。达到 8 分且没有关键误判，再读第 12 章的容量设计。把“Pod 不存在”判为资源 Filter 失败、用 usage 代替请求、把 Ready 当业务健康、把硬分散当跨区容量保证，都应返回相应章节补练。

再只改变一个输入并预测结果，例如增加 batch 节点是否能解决必须去 online 的 Pod。能够解释一个修复为何无效，与找到有效修复同样重要。

### 11.2 下一步不是继续背名词

继续读本文第 12 章：把节点位置、故障流量、HPA 和调度等待放进同一组输入。第 13 章再接 GPU 的设备问题；第 14 章用源码表追实现；第 15 章动手复跑，第 16 章对照实测。

---

<a id="production"></a>

## 12. 生产里还要算什么：位置、故障流量、HPA 和等待

下面的数字是教学假设，尤其是 Java 吞吐和预热时间，不是公司压测结果。先把输入写全，再做方案；算式对了还要验证约束和实际运行能力。

### 12.1. 先区分三种“慢”

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

### 12.2. 一共还空着 11 核，为什么只放得下一只

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

这是按采集时数据做的**粗估**。scheduler 可能已经给别的 Pod 临时记账，设备供给也可能变化。记录 Pod 规格、节点条件和采集时间，再用隔离实验或固定输入重放验证，别把估算写成一定能放下。[P3]

#### 12.2.1 发布窗口的容量预算

规划同时区分稳定态、首波 surge、终止重叠和故障态：

```text
需要的可行余量 ≠ 单纯 replicas × request
需要考虑：发布新增 + 尚未退出旧 Pod + 同时扩容 + 节点维护/故障
```

例如六个服务各新增一只 `1 CPU/4Gi`，首波需求是 `6 CPU/24Gi`，还必须有六个实际能放的位置。修改六个仓库不等于发生六次 rollout，要核对 PodTemplate 是否变化。旧 Pod 终止期间也可能继续占资源。[P4]

#### 12.2.2 故障预算不能只看“均匀部署”

教学例：三个域各有 6 个同规格可承载位置，稳定占用各 4 个。丢失一个域后，剩余两域总容量 12，恰好容纳原 12 只 Pod，没有额外 surge、维护与负载增长空间。若硬拓扑在故障期间仍要求缺失域参与计数，即便资源算式看似足够，也可能无法补齐。

因此要分别证明：存活实例能否立即接流量；存活域有没有余量承受原流量；控制器重建后能否满足调度约束；发布是否需要暂停。**热实例接管、补齐副本、恢复原分布是三件事。**这是设计审查，不是给出统一恢复秒数。

#### 12.2.3 把 N-1、启动时间和发布放进同一道题

这里 N-1 指“失去三个可用区中的任意一个”。每域能放 6 只、当前各 4 只；所有 Pod 同规格，跨域网络及依赖可用。再假设压测结果为每只 Ready 实例能在延迟目标内承载 100 请求/秒，总流量为 900 请求/秒，Java 新副本至少需要 60 秒预热。这些是假设的题目输入，本次没有执行 Java 压测。

先回答三个问题：丢失一个域后能否立刻接住全部流量？最终能否补齐？故障恢复中能否再并行新增 3 只发布副本？

<details>
<summary>展开推导，再检查你漏掉了哪一层</summary>

1. **立即接管：**存活 8 只的已验证能力只有 `8 × 100 = 800` 请求/秒，小于 900。即使容器仍 Ready、CPU 账还有余量，最初的服务能力仍不足。需要故障前增加热实例/单实例能力，或采用明确的流量降级方案；60 秒后才启动的新副本不能填补最初的窗口。
2. **最终补齐：**存活两域尚有 `(6−4) × 2 = 4` 个位置，数值上可补 4 只。但这只是资源上界：还须验证硬拓扑允许落到存活域、卷可用、控制器已创建替代 Pod。不能把丢失域的物理资源加进余额。
3. **同时发布：**补齐后 `12−12=0` 个空位，不能再承诺 3 只 surge。恢复和发布若同时进行，共需争用 `4+3=7` 个位置，而目前只有 4 个。暂停发布、减少并行度或先增加符合全部约束的供给，是不同取舍。
4. **终止与摘流量：**旧 Pod 收到删除请求后可能仍消耗资源。EndpointSlice 保存 Service 后端的地址、端口和就绪条件；外部注册中心是另一份服务实例名单。它们和 Ready 的更新时间不同。“摘流量”是让路由停止把新请求交给旧实例，已有连接还可能继续；资源释放、流量摘除和 JVM 退出必须分别取证。

</details>

**反事实：**将流量改为 700 请求/秒，第一问的容量判断改变；后两问的 Pod 位置数量不变。把每域容量改成 8 个位置，则存活两域补齐后还剩 4 个位置，但这仍没有自动解决最初 8 只的吞吐缺口。学会分别改变一个输入，是区分运行能力与调度容量的关键。

### 12.3. 多可用区设计必须连同故障退化一起讲

#### 12.3.1 四层问题分开

Pod 放在哪个 Node，由调度规则决定；流量发往哪个 Pod，由入口、Service 数据面、服务发现或客户端决定；数据库写往哪个主节点，又是另一套路由；故障实例何时被摘除，由相应健康检查与控制器决定。调度到同 AZ 不自动让调用路径同 AZ，Readiness 变化也不能被假设为所有外部注册中心同时下线。[P2][P5]

对现有平台，先画真实调用链，确认流量究竟经过 Service、网关还是直连实例地址。本次没有读取公司生产配置，不替代该核实。

#### 12.3.2 联合约束练习

给定 zone `a/b/c`，本服务现有 Pod 数为 `1/1/0`；准备再加一只。新 Pod 的 required affinity 只允许 a/b，分布规则为 `DoNotSchedule、maxSkew=1、minDomains=1`，其他资源和污点条件都通过。

| nodeAffinityPolicy | 参与计数的域 | 最小计数基准 | 新 Pod 放 a/b 的计算 | 结果 |
|---|---|---:|---|---|
| Honor | a/b | 1 | 1+1−1=1 | 两域都通过此分布规则 |
| Ignore | a/b/c | 0 | 1+1−0=2 | a/b 超过 maxSkew；c 仍被 required affinity 拒绝 |

Ignore 让 c 参与统计，没有让新 Pod 获得去 c 的资格。**参与统计的域与实际能放的位置，要分开判断。**这里用最小计数作基准，不是拿域数当分母算平均值。[P5]

同理，nodeTaintsPolicy 决定统计时如何处理污点，不会取消 TaintToleration 对真正落点的检查。

不同命名空间的同名 label 不一定是你的同伴。matchLabelKeys 是把新 Pod 上指定标签的值加入统计条件；pod-template-hash 是 Deployment 用来区分模板版本的标签。把它加入后，不同版本的副本可能分开统计；revision 就是这里说的版本。发布期间应明确要“全服务总体分散”还是“每个版本自己分散”。字段可用性与 selector 合并行为随版本变化，先 `kubectl explain` 并查目标版本。[P5]

#### 12.3.3 设计验收的三个故障输入

在隔离环境分别模拟：某域容量耗尽、某域 Node 仍存在但不可调度、某域节点被删除。三者不一定产生相同 eligible domains。记录每次对象初态、计数和预测，不能只把 Node label 删掉当作已经模拟网络级 AZ 故障。

方案验收需要给出具体取舍，例如“在线关键服务平时跨域；故障时允许在存活域增加副本，并预留 N-1 容量”。不要用“硬分散总是最好”代替业务目标。

### 12.4. HPA、节点扩容和调度器各做什么

HPA 是根据负载自动调整 Pod 副本数的控制器，回答“应该有几只 Pod”；scheduler 为已经创建的 Pod 选节点；节点 autoscaler 是调整节点供给的程序，根据等待需求、节点类型和配置，决定是否增加或调整节点。它们分别更新状态，一步完成后，下一步还要等组件看到变化。[P6][P7]

一只 16-GPU Pod，如果所有允许的节点类型最多 8 GPU，扩十台同型节点仍无解；Pod 被 required 标签限定到一个不允许扩容的池，也不应期待另一个池的余量自动救场。

#### 12.4.1 扩容链逐段验收

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

**应交的证据：**最终 Pod requests、HPA 使用的是 Utilization 还是 AverageValue、各副本的有效指标、HPA conditions 与期望副本。只盯着一张 CPU 使用率图不足以解释联动。

### 12.5. 可观测性：把成功者和仍在等待的人都算进去

以下指标名称来自 Kubernetes scheduler 指标体系。每次升级都对照目标版本 `/metrics` 与官方参考核验存在性、label 和稳定性；托管平台也可能没有暴露全部指标。示例仅适用于已选定单集群数据源，或已在选择器中加入集群过滤，不要意外汇总所有集群。下列指标名与维度也已对照本课固定源码核验。[P8][P13]

metrics 就是采集出的数字，`/metrics` 是组件提供这些数字的入口。指标 label 是用来分组的标记，例如 profile、result，和 Pod 的标签各有自己的用途。PromQL 是从 Prometheus 监控数据中查询、计算这些数字的语言。

#### 12.5.1 最小五张图

先看懂查询中三个词。`rate(...[5m])` 是用最近五分钟的计数变化估算每秒增加多少；直方图把耗时按范围统计，bucket 是一个范围桶，le 标明“累计统计小于等于这个上界的样本”。p99 是约 99% 的样本不超过的耗时；`histogram_quantile(0.99, ...)` 从这些桶估算它。

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

### 12.6. 同名 scheduler、多个 profile 与多个进程

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

定制前先写清：数据从哪里来，多久更新，谁保存临时状态，调用失败怎么办，撤销预留由谁做，以及支持哪些版本。比如每秒按利用率修改节点标签，要先测 API 写入压力和数据延迟，再看是否真能改善放置结果。

### 12.7. 调度变快以后，放置结果还对不对

可行节点比例会影响遍历工作量与评分候选集合。缩小候选范围可能降低耗时，也可能牺牲放置质量；扩大并行度可能转移瓶颈到 CPU、锁或外部依赖。配置项是否生效与默认值都应固定到目标版本，不从开发快照直接复制。[P12]

优化前保存：固定 Pod/Node 集、插件配置、硬约束通过率、绑定成功率、attempt 吞吐和 p99、插件耗时、节点分布及资源碎片。优化后用同一批输入比较。

关闭 Filter、给所有污点加 toleration、降低 requests，都会改变原来的要求。如果业务确实要调整这些条件，另做配置变更和验收；调度性能比较应使用同一组输入和要求。

### 12.8. 配置变更和回滚不等于 Pod 自动回家

新 Score 配置只影响未来决策。回滚配置后，已经绑定的 Pod 不会自动恢复此前分布。因此验收除了进程健康，还需要新副本分布、故障域、冷热节点、业务延迟和后续大 Pod 的可放置能力。[P10]

建议按固定版本重放、隔离环境、专用 profile、小流量、扩大范围的顺序验证。回滚单中同时说明：恢复哪些配置；尚未绑定 Pod 怎样处理；已绑定 Pod 是否保留；需要迁移时由哪个控制器和维护流程执行。

### 12.9. 先算有答案的题，再做自己的方案

先完成第 12.2.3、12.4.3、12.5.4 节的固定输入题，再做以下开放题。开放题允许多个正确方案，但必须附输入清单、复算过程和拒绝另一方案的理由。

#### 12.9.1 发布评审

输入：12 个服务，部分单副本，Pod request 不同，三域容量分布不均，还有 90 秒终止期。交付逐服务 rollout 触发判断、逐节点 shape 预算、批次安排、故障期间发布策略、回滚与业务验收。必须指出哪些输入还缺失。

#### 12.9.2 “有空闲却 Pending”的二线排障

要求同时提供两个成立的限制：例如 CPU 与 WFFC 拓扑，或 GPU 与内存交叉碎片。先写预期，保存全部证据，再只改一个变量。修复第一个约束后第二个浮现，是有效的负向验证，不算实验失败。

#### 12.9.3 调度器性能回归

给定新插件发布前后指标。区分相关性与因果：固定输入重放、单独禁用待测插件、比较扩展点长尾、控制 API 状态变化，最后说明是否回滚及代价。不能用一次成功落点或单个平均值证明改动正确。

**评分建议（教学设计）：**事实输入 25 分、复算与因果 30 分、反证 20 分、修复风险和回滚 25 分。把教学数值写成生产实测、无授权改生产或通过删对象抹掉证据，直接判不合格。具备这些能力比记住几十个函数名更接近调度专项专家。

---

<a id="gpu"></a>

## 13. 从 Java 接到 GPU：还要增加哪些判断

CPU 余额、硬条件、发布空间和失败重试仍适用。现在新增的是设备种类、健康、具体设备和显存等问题。本章先讲传统 Device Plugin，再讲共享、DRA 和批任务队列；本次云端没有真实 GPU，以下 GPU 算例与配置均未做硬件验证。

### 13.1. “还有几份 GPU”和“实际用了哪张卡”分开查

先回答一个问题：Pod 已绑定，为什么仍可能无法使用 GPU？先分清“数量满足”与“节点把设备准备好”，后面的证据才知道该找谁。

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

**健康变化练习：**8 张独占 GPU 中一张被插件报告 unhealthy（设备不健康），kubelet 会减少 GPU allocatable，capacity 不因此减少。已使用故障卡的 Pod 不会自动换卡或重新调度。应对齐 UUID、插件健康报告、Pod 使用设备和应用错误；不能因 capacity 仍为 8 就判断故障报告未生效。[G1]

**教学算例：**Pod 要 `2 GPU/32Gi`。a 剩 2 GPU，但内存只剩 8Gi，差 24Gi；b 剩 64Gi 内存，但没有 GPU。因此两台都放不下。再看另一个输入：四台各余一份 GPU，也放不下单个要四份的普通 Pod。分布式训练要用多个 Pod 和相应训练架构，调度器不会替你拆分一个 Pod。

### 13.2. “1 GPU”不一定是同一种产品

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

**资源名练习：**single 策略下 MIG 实例仍可通过 `nvidia.com/gpu` 表达；mixed 策略暴露类似 `nvidia.com/mig-1g.10gb` 的 profile 资源。具体名字和尺寸取决于 GPU 型号、布局与插件策略。[G4][G13]

MIG Manager 重配流程会终止相关 GPU Pod，部分场景还涉及重启。执行前确认 checkpoint、影响范围、窗口和回滚配置；之后核对 MIG 配置状态、Node 实际资源、实例 UUID 和业务运行。一个 `mig.config` 标签只是期望配置。[G4]

#### 13.2.3 节点间拓扑与节点内拓扑

zone/hostname 回答“Pod 放在哪些节点域”；NUMA、PCIe、NIC、NVLink 描述“同一节点内设备怎样连接”。CPU Manager、Topology Manager、设备插件或 DRA 驱动、应用通信库可能分别影响分配和运行结果。看到设备连接关系以后，还要核对实际 UUID、容器可见设备和通信测试，才能解释性能问题。[G1][G2]

| 专业词 | 在这道题里怎么理解 |
|---|---|
| NUMA | 一台机器的 CPU、内存可能分成几组；访问靠近自己的内存通常更快，跨组访问可能付出额外代价。 |
| PCIe | CPU 与显卡、网卡等设备连接的通道；共享通道与连接层级会影响数据传输。 |
| NIC | 网卡，训练成员跨机器传数据时会用到。 |
| NVLink | GPU 之间的高速连接；有连接不代表所有 GPU 之间距离都相同。 |
| CPU Manager | kubelet 里按策略管理 CPU 分配的部分；静态策略可给符合条件的容器分配固定 CPU。 |
| Topology Manager | kubelet 里汇合 CPU、设备等位置要求、按策略判断能否对齐的部分；不是跨可用区分散规则。 |

这里的训练 worker 是任务成员，通常由 Pod 承载；第 15 章的 worker 是 kind 工作节点。要先看上下文，别把“四个训练成员”直接读成“四台 Node”。

### 13.3. 默认能检查GPU数量，不代表默认按GPU装箱

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

资源在NodeResourcesFit内部的权重、Score插件是否归一化、整个插件在Framework内的权重，是不同层次。固定源码中 NodeResourcesFit 的 `ScoreExtensions()` 返回 `nil`，没有提供归一化扩展，不能为它画一个必经 NormalizeScore 步骤。设计实验时记录配置和分数，不用一次落点反推完整算法。[G5][G6]

固定实现会跳过新 Pod 没有请求的扩展资源评分项。因此，GPU 权重为 5 并不意味着不申请 GPU 的 Pod 也会按同样方式装箱。先核对最终 Pod 请求和 schedulerName，再分析配置是否生效。[G14]

### 13.4. DRA 改变了什么，没改变什么

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

**对象练习（以 v1.34 DRA 文档为基线）：**ResourceSlice 是驱动公布的供给；DeviceClass 定义设备选择规则；ResourceClaim 是具体申请；ResourceClaimTemplate 用于为每个 Pod 生成独立 Claim。[G15]

两个推理副本各要一块设备，应理解“每个 Pod 从模板得到自己的 Claim”。若两只显式引用同一个 Claim，表达的是共同使用其分配结果，不会自动变成各申请一块；能否共享还受实际资源和驱动能力约束。

显式创建的 Claim 由创建方管理生命周期；模板生成的 Claim 与对应 Pod 生命周期关联。因此删掉 Pod 不保证设备立即可用，还要看 Claim、分配、消费者和驱动清理结果。

排查按顺序走：Pod 引用谁 → Claim 是否同命名空间且存在 → Class 是否匹配供给 → allocation 是否生成 → Pod 是否绑定 → 节点驱动是否准备成功。API discovery 是询问服务器“提供哪些资源接口”；它只证明 API 被服务，不能证明设备供给链正常。

#### 13.4.1 版本卡比一张“GA/Beta表”更可靠

GA 表示功能进入上游稳定阶段，Beta 表示还处在测试发布阶段；这些是功能成熟度标签。feature gate 是功能开关。它们没有替你检查云厂商是否开放、开关是否启用、驱动是否安装，所以仍要核对下面的实际环境信息。

为目标环境填写：Kubernetes版本与发行版、DRA driver版本、API discovery结果、feature gates、scheduler插件配置、ResourceSlice内容、Claim请求类型、分配结果及节点准备状态。不要把开发分支的默认开关当成云厂商当前开放能力。

原文固定开发快照讨论了扩展资源到DRA的桥接。具体部署使用该能力时，同一个资源名可能经传统scalar或DRA路径处理；应根据目标版本、DeviceClass和实际Node供给分流，不能因Node上无传统GPU scalar就立即判设备插件损坏。

scalar 在这里就是“一个资源名对应一个数量”的数值账，比如 GPU=8；桥接是让原先按数量表达的申请接到 DRA 分配路径，具体是否支持、怎样处理以目标版本为准。

DRA 下的抢占、优先请求、可消耗容量和设备绑定条件，都要按版本核对。不能仅凭某个开发快照断言以后始终不支持，也不能承诺提高 Pod priority 就能拿到别人 Claim 的设备。结论要有目标版本源码、驱动规则和实验支持。[G2]

#### 13.4.2 跟一个具体现场：要两份设备，为什么申请一直没分配

**这是按 v1.34 API 编写的教学模拟，不是 GPU 实测输出。**假设一个推理 Pod `infer-demo` 位于 `ml-lab`，已限定到 worker-a；CPU、内存及其他普通硬条件通过。它引用同 namespace 的 `infer-gpu` Claim，要求两个不同的设备。管理员已核对：这个案例中只有一个匹配供给池，清单完整，其中只有一个空闲设备；没有共享分配或其他候选池。

先预测：Pod 会先绑定、到节点才发现少设备，还是在选点时就可能被挡住？这一题用来区分“申请没分配”与“已经分配，但节点准备失败”。

下面四块是**用于阅读的字段摘录，省略了无关字段，不是可直接 apply 的完整清单**。名字都是虚构教学名字。字段结构对照 v1.34 的 API 类型，尤其要注意 requests 下的 `exactly` 层级。[N13]

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
status: {}  # 本题假定查询时还没有 allocation。
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
# ResourceSlice：worker-a 可使用的完整池，本题只有一个设备。
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

在自己的真实现场，先把 context、namespace 和对象名换成实际值，再做这些只读查询。没有这些教学对象的环境返回 NotFound，并不是验证失败：

```bash
kubectl --context "$CTX" get pod infer-demo -n ml-lab -o yaml
kubectl --context "$CTX" get resourceclaim infer-gpu -n ml-lab -o yaml
kubectl --context "$CTX" get deviceclass gpu-lab -o yaml
kubectl --context "$CTX" get resourceslices -o yaml
kubectl --context "$CTX" describe pod infer-demo -n ml-lab
```

按下面顺序读查询结果，每一行都先核对实际字段，再下结论：

| 看到什么 | 本题怎样解释 | 还不能据此说什么 |
|---|---|---|
| Pod 的引用指向同 namespace 的 infer-gpu | 找到了本次具体申请 | 不能只查一个同名但不同 namespace 的 Claim |
| Claim 要 gpu-lab，ExactCount=2 | 请求是两份，不能按一份计算 | 不能假定 count=2 一定等于两张物理整卡，资源含义仍由驱动声明 |
| Class 匹配该驱动，完整供给池只有 gpu-0 | 在本题限定条件下，1 < 2，数量不足 | 单个 Slice 的局部输出不等于完整库存 |
| Claim 没有 allocation，Pod 没有 nodeName | 还没形成设备分配与节点绑定 | 缺 allocation 本身不足以证明数量不足，还可能是 Class、选择器等问题 |
| Pod 失败原因与设备分配对应 | 才把请求、供给与调度结果串起来 | 不凭一个 Pending 就去重启节点驱动 |

v1.34 的 DynamicResources.Filter 在分配器没有为全部 Claim 找到结果时，会返回包含 `cannot allocate all claims` 的拒绝结果；它是目标版本可能出现的消息片段，不是所有版本统一的完整 Event。[N14] 本题的判断先来自完整的“要 2、只有 1”，再用实际失败原因核对。

**只改一个输入再预测：**若创建一份同条件但 count=1 的新 Claim，并让测试 Pod 引用它，数量这一项可以满足；Class、节点条件、其他占用仍要检查。Claim 的 spec 不能就地随便改，所以不能写成“patch 原 Claim 的 count 就好了”。这只是下一步教学推演，没有在本轮执行设备分配或容器 GPU 验证。

如果实际看到的是 **Claim 已有 allocation、Pod 已有 nodeName、容器仍未启动**，问题已经换到另一段：对照分配结果中的 driver/pool/device，再查节点驱动准备与容器运行时。不要把这两种现场都写成“GPU 不够”。

### 13.5. Kueue、Volcano 与 kube-scheduler 不是三个同义词

Kueue关注工作负载准入、配额与队列；Volcano可以承担面向批任务的Pod/PodGroup节点调度；kube-scheduler负责其所处理Pod的节点选择和绑定。实际组合应先写职责，再选产品。[G7][G8]

这里的工作负载准入是任务先排队、获准占用额度后再推进运行；第 1 章的 API 准入是保存某个对象前的检查与补充。PodGroup 是把一批 Pod 标为同组、并写出最少需要几名成员等要求的对象。批任务是做完一批工作后结束的任务，例如训练或离线计算。

#### 13.5.1 Kueue 不能只用“管总配额”概括

先用一个申请四个 worker 的任务理解：LocalQueue 是它在命名空间里的排队入口；ClusterQueue 管配额和策略；ResourceFlavor 说明资源条件，例如节点类型或池标签；Workload 记录这份待准入任务。看到 QuotaReserved 或 Admitted，只能说明到了相应准入阶段，还要继续看四只 Pod 是否绑定和启动。[G7]

QuotaReserved 表示任务已占入配额账，Admitted 表示任务已满足该层准入条件、获准推进运行。它们是任务状态，不等于每只 Pod 的 Ready。WorkloadPriorityClass 保存任务层优先级；Pod PriorityClass 保存 Pod 层优先级，分别影响各自的排队和处理。

开启拓扑感知调度（TAS，Topology-Aware Scheduling）时，Kueue 还会基于节点及拓扑域容量检查放置并分配拓扑，也就是先检查任务在所需节点分组里能否放得下。即便如此，也要区分“准入时通过放置检查”与“后来每个 Pod 已经绑定并 Ready”；节点健康、其他占用与准备过程还会变化。[G9]

WorkloadPriorityClass与Pod PriorityClass分别影响对应层的优先级，不能在平台UI只显示一个含糊的数字。具体回退/推导规则以安装版本为准。[G10]

#### 13.5.2 gang解决的是成员协调，不是让所有进程同一纳秒启动

一个训练任务需要足够成员才能工作，逐Pod抢到少量资源却永远凑不齐时，需要all-or-nothing或gang相关策略。Volcano gang插件依赖实际PodGroup、最小成员与插件配置，不能把“安装Volcano”理解为全部批调度策略自动启用。[G8]

all-or-nothing 是“满足整组条件再推进，否则撤回或等待”；gang 是按一组成员协调调度。产品可能在准入、预留或绑定等待等不同位置实现，不能只见这个名字就认为所有进程同时启动。

也不要写“Kubernetes原生永远没有gang”：目标版本可能已有或正在演进原生分组调度能力。生产选型要比较成熟度、API、故障恢复、监控、升级和维护成本，而非凭产品名字判断。[G11]

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

## 14. 第二遍源码：沿同一只 Pod 追下去

第一遍的关键判断已经在 3.2.2、3.3.2、8.3.1 就地解释。这里才继续追入口、缓存、返回结果与失败补偿；它们对应第 10 章的具体例子。

<a id="cpu-source-walk"></a>

### 14.1 跟同一只 201m 的 Pod：第一次失败，后来为什么成功

本篇固定教学提交为 `301946d15e67a4a2e8a5fb8292eb836acd366d78`。本次在 `/workspace/kubernetes` 单独检出，`git describe --always` 为 `301946d1`，工作区干净。实验服务器另为 v1.34.0，发布提交是 `f28b4c9efbca5c5c0af716d9f2d5702667ee8a45`。这两个版本分开核对；生产排障则应使用目标集群版本。

先用[第 15.2 节](#cpu-lab)的 CPU 对照理解这一条链。这里的 Pod 叫 `one-over`，只有一个 sleep 容器，申请 201m；它用节点标签限定到同一 worker。内存等其他条件通过，没有调度 gate，也没有低优先级占位者可供它抢占。

数字沿用本次 5000m 节点；你复跑时，节点容量与占位请求按自己的实际值代入。按时间从上到下读：

| 时刻 | 目标节点已请求 CPU | 余额 | one-over 的结果 |
|---|---:|---:|---|
| holding 已 Ready | 系统 100m + holding 4700m = 4800m | 200m | 201 > 200，不能放 |
| holding 删除已被 scheduler 处理 | 系统 100m | 4900m | 下一次尝试时，201 ≤ 4900 |
| one-over 被记入请求账 | 系统 100m + one-over 201m = 301m | 4699m | 继续绑定；启动后才可能 Ready |

这次没有修改 `one-over` 的 request，也没有把它删掉重建。**变化的是别的 Pod 释放了请求，原 Pod 获得一次新的尝试。**sleep 不验证 Java 吞吐，但能把 Java 发布前“资源账不够”的判断单独拿出来观察。

下面追的是普通单 Pod、默认资源插件和默认绑定插件。源码快照与实验版的函数拆分不同，结尾有对照；不能把实验的成功结果当作每一条内部函数都已被逐行跟踪。

#### 14.1.1 201m 从哪里来：先算 Pod 请求，再逐台比较

进入一次尝试后，调度器准备两份输入：这只 Pod 的最终配置，以及这一轮使用的节点资料。NodeInfo 是某台节点及其已计入 Pod、资源等资料的汇总；CycleState 是本轮插件之间暂存数据的地方。

**这段只回答：怎样把 Pod 请求保存下来，供后面重复使用？**下面是 `pkg/scheduler/framework/plugins/noderesources/fit.go / Fit.PreFilter` 的完整函数，只加中文注释。[N8] 本组所有 Go 摘录都依赖上游类型，不能独立编译。

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

**大白话总结：**输入是最终 Pod；内部通过 `resource.PodRequests` 汇总请求；动作是把结果写进本轮暂存区。它没有扣节点资源，也没有调用绑定 API。[N8]

**顺手学 Go：**`(f *Fit)` 表示这个方法属于一个 Fit 插件实例，`f` 可暂时类比 Java 的 `this`，但 Go 不是 Java 的类继承模型。`*T` 是指向 T 类型值的指针；`[]T` 是一组 T 类型记录。两个返回位置各有用途：第一个 nil 表示不返回节点范围限制，第二个 nil Status 表示成功，不能合读成“两次失败”。`fwk` 在这一段是导入的框架包名。

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

**第二遍再读一个容易想错的分支：**固定提交中，PostFilter 自己返回 Error 时，`schedulingAlgorithm` 会记录异常，但这个 FitError 分支最终仍返回携带原 FitError 的 Unschedulable。不能只看到内层出现 Error，就断言最外层一定报告 SchedulerError。没有节点、普通找不到位置、非 FitError 的执行异常，也要分别读。[S17]

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

**大白话总结：**输入是本轮尝试；检查 status 是否成功；失败交给 FailureHandler 后结束本次调用，成功才启动绑定流程。本例第一次没有走到 `go` 那一行；变量名叫 `assumedPodInfo`，也不代表失败时已经完成 Assume。

**顺手学 Go：**多返回值按位置接到三个变量；`!` 是取反。`return` 结束当前函数，不是退出 scheduler 进程。`go` 启动另一段可并发推进的工作，原调用不用等 API 绑定完成才继续。

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

**大白话总结：**输入是 holding 的删除通知；动作一是撤掉它在缓存里的请求，动作二是让队列检查等待者。正常情况下余额从 200m 变为 4900m；如果移除失败，不能仅凭发了通知就宣布余额已经正确。

**顺手学 Go：**`if err := 调用(); err != nil` 是先调用并接住错误，再判断是否出错；err 在这个 if 及对应分支内使用。末尾两个 nil 分别表示没有新对象、没有额外的预检查函数，不是两个错误返回值。

这里的 AssignedPodDelete 是程序内部的对象变化通知。FailedScheduling 才是前面给 `kubectl get events` 查看的一条报告。队列不会因为你删除了某条 FailedScheduling Event 就认定 CPU 已释放。

#### 14.1.5 值得重试，为什么还不能承诺一定成功

资源插件通过 `EventsToRegister` 关注已绑定 Pod 的删除，并提供 `isSchedulableAfterAssignedPodDelete` 判断是否值得重新尝试。[N8]

下面是该判断函数的连续摘录。前文已把通知中的旧对象解析成 `deletedPod`；解析失败时返回 `Queue, err`，不在下面冒充解析成功。`pod` 是等待的 one-over，`deletedPod` 是删除的 holding，`logger` 来自参数。

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

**大白话总结：**holding 已绑定，所以得到 Queue。这个函数没有算 `4700 ≥ 1`，也没有验证其他硬条件；它只给出“值得再试”的建议。队列还要结合退避和其他失败插件决定何时可取出。[N11]

**顺手学 Go：**两个返回位置分别是“排队建议”和“错误”。`Queue, nil` 是建议重试且本函数没有错误；`QueueSkip, nil` 是不因这次变化重试且本函数没有错误。日志的 `V(5)` 表示详细程度，不是优先级数值。

**反事实：**如果只删除另一台不符合节点标签的 Node 上的 Pod，这个提示仍可能建议重试；但目标 worker 余额还是 200m，下一轮仍失败。不要把提示函数读成第二个完整 Filter。

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

**这段只回答：默认绑定怎样把结果交给 API？**下面是 `pkg/scheduler/framework/plugins/defaultbinder/default_binder.go / DefaultBinder.Bind` 的连续摘录。[N12] `binding` 已由当前 Pod 身份和目标 nodeName 构造，`b` 是绑定插件，`ctx` 是本轮上下文。固定提交还包含 APICacher 分支；下面只展示未使用该分支时的直接调用，不能当成完整函数。

```go
// 给 API 发送这只 Pod 到目标 Node 的 Binding 请求。
err := b.handle.ClientSet().CoreV1().Pods(binding.Namespace).Bind(ctx, binding, metav1.CreateOptions{})
if err != nil {
    return fwk.AsStatus(err) // 请求出错，转成插件处理状态交回上层。
}
return nil // 本插件绑定处理成功；不表示容器已经 Ready。
```

**大白话总结：**输入是 Pod 身份与目标节点；动作是请求 API 绑定；失败转换成 Status，成功返回 nil。APICacher 是帮调度器管理部分 API 写入的内部设施，另一分支会提交绑定并等待它的完成结果，仍要处理错误。[N12]

**顺手学 Go：**连着写的 `.方法()` 是逐步取得 API 客户端并发起调用；`metav1.CreateOptions{}` 构造空的创建选项。这里的 `AsStatus(err)` 是转换错误表示，不会把错误变成成功。

若 Reserve、Permit 或绑定失败，不能一直占着这 201m。固定路径按失败位置执行 Unreserve、ForgetPod 等清理；绑定失败处理还会通知队列资源可能释放，让其他受影响 Pod 有机会再试。清理自身出错也会记录错误，不能把“调用过清理”写成“必然清理成功”。Done 则按相应路径结束本轮跟踪，不必等到所有绑定步骤结束才执行，见第 10.3 节。[S17]

**合上源码，检查你能否回答：**第一次失败为何没有执行 Assume？删除 holding 为什么要同时更新账和队列？第二次返回 nil 为什么还不能宣布 Java 接口恢复？答案分别是：资源阶段已经返回失败；重试既要看到新的余额，也要获得尝试机会；nil 只说明对应处理成功，节点启动与应用验收还在后面。

#### 14.1.7 实验版本怎么对应，现场又能证明到哪一步

| 位置 | 教学提交 | v1.34.0 发布源码 |
|---|---|---|
| 单 Pod 的主要流程 | `ScheduleOne` 转到 `scheduleOnePod` | 主要逻辑直接在 `ScheduleOne` |
| 选点失败、PostFilter | 拆在 `schedulingAlgorithm` 等函数 | 相应逻辑主要在 `schedulingCycle` |
| 资源删除提示 | `isSchedulableAfterAssignedPodDelete` | `isSchedulableAfterPodEvent` 处理包括删除在内的变化 |
| 删除后重新尝试的结论 | 更新缓存，向队列传递变化，再检查条件 | 对本例的结论相同；函数名与参数不能照搬 |

表中差异已对照 v1.34.0 的 [schedule_one.go](https://github.com/kubernetes/kubernetes/blob/f28b4c9efbca5c5c0af716d9f2d5702667ee8a45/pkg/scheduler/schedule_one.go)、[eventhandlers.go](https://github.com/kubernetes/kubernetes/blob/f28b4c9efbca5c5c0af716d9f2d5702667ee8a45/pkg/scheduler/eventhandlers.go) 和 [fit.go](https://github.com/kubernetes/kubernetes/blob/f28b4c9efbca5c5c0af716d9f2d5702667ee8a45/pkg/scheduler/framework/plugins/noderesources/fit.go)。

第 15.2 节保存删除前后的 UID、request、PodScheduled、nodeName 和 Ready。它能证明原对象从 CPU 不足变为绑定并启动；**单凭这些 API 快照，不能分辨某次重试究竟由删除通知、退避到期还是其他队列路径触发。**内部因果说明来自上述源码核对，本轮没有用断点逐次追踪 scheduler。

需要继续找入口时，再用下面的表，不把整张表当第一遍必背内容：

| 阅读顺序 | 位置 | 本轮只回答的问题 |
|---|---|---|
| 1 | scheduler.go / Run | 谁启动循环，谁负责leader后的工作 |
| 2 | schedule_one.go / ScheduleOne、scheduleOnePod | Pod从哪里取，什么时候Done |
| 3 | schedulingCycle / schedulingAlgorithm / schedulePod | snapshot、Filter与Score怎样衔接 |
| 4 | framework/runtime/framework.go | 插件按何顺序调用，何时短路 |
| 5 | assumeAndReserve | 通用cache与插件状态分别改了什么 |
| 6 | bindingCycle | Permit等待、PreBind、Bind和PostBind责任 |
| 7 | cache实现 | Add/Assume/Forget与NodeInfo账怎样维护 |

上面的定位表对应固定教学提交。遇到别的版本，先查实际入口，再沿相同问题找代码。[G5]

### 14.2 用一页状态表读代码

PodInfo 是带着 Pod 及调度相关资料的记录；NodeInfo 是某台节点、已计入 Pod、资源和端口等资料的汇总；CycleState 是本轮调度供插件暂存、传递数据的地方。先认清每份记录装了什么，再看函数怎样读写它。

goroutine 是 Go 中可以另行推进一段工作的执行单元；启动后，原流程和这段工作可以交错进行。共享状态是两段工作都可能读取或修改的数据，锁等保护方式用来避免它们把数据改乱。别把 goroutine 理解成每次必定占用一个独立 CPU 核。

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

乐观占账就是“先按这次会成功登记，失败再撤销”；它能让下一只 Pod 看到本调度器的临时承诺。这个词里的“乐观”不表示忽略失败，也不表示锁住了真实硬件。

### 14.3 确认测试真的存在，再执行

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
go test ./pkg/scheduler/framework/plugins/noderesources -run '^TestEnoughRequests$' -count=1 -v
```

固定教学提交中确实存在 TestEnoughRequests，其中包含请求足够、CPU 不足、内存不足和 init 请求等用例。切换版本后仍先用第一条列表确认测试名。出现“no tests to run”不是通过；本轮只核对了测试源码，没有执行这条上游 Go 测试。截出的 Go 片段也不能直接独立编译，它依赖该版本的接口和生成文件。

### 14.4 分清四种验证

静态阅读核对路径与分支；单测验证给定输入；集群实验验证 API、控制器与调度协作；真实 GPU 实验才验证驱动、运行时与硬件。本文已做源码核对和隔离集群对照，没有执行上游 Go 测试或 GPU/CUDA 实验。

---

<a id="experiments"></a>

## 15. 在一个隔离集群里，按本文步骤复跑

先写预测，再操作。每组只回答一个问题，做完保存现场、清理该组 namespace，再进入下一组。CPU 大 request 用来占请求账，容器只 sleep，不做压测。kind 的两个 worker 共用宿主机，临时域标签只用于验证调度规则。

### 15.1 只准备一次：集群、镜像和小工具

需要 Docker 等 kind 支持的容器环境、kind、kubectl、Python 3.9+。下面在 Linux/WSL 的 Bash 中执行；Windows 可以进入 WSL 使用同一组步骤。

先确认工具可用，再创建**本次学习的独立集群**。`LAB_ROOT` 只是运行时临时目录，保存 kubeconfig 和证据，不需要在仓库里新增文件或目录。v1.34.0 是这里实测的教学版本，使用别的 kind 版本时先核对其 release notes 的兼容镜像。

```bash
set -euo pipefail
kind version
kubectl version --client
python3 --version
LAB_NAME="scheduler-md-$(python3 -c 'import uuid; print(uuid.uuid4().hex[:8])')"
LAB_ROOT=$(mktemp -d)
LAB_KUBECONFIG="$LAB_ROOT/kubeconfig"
LAB_CONTEXT="kind-$LAB_NAME"
LAB_IMAGE='docker.io/library/busybox:1.37.0'
cat > "$LAB_ROOT/kind.yaml" <<'YAML'
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
nodes:
- role: control-plane
- role: worker
- role: worker
YAML
kind create cluster --name "$LAB_NAME" \
  --image kindest/node:v1.34.0@sha256:7416a61b42b1662ca6ca89f02028ac133a309a2a30ba309614e8ec94d976dc5a \
  --config "$LAB_ROOT/kind.yaml" --kubeconfig "$LAB_KUBECONFIG" --wait 180s
chmod 600 "$LAB_KUBECONFIG"
```

镜像必须能拉取，且包含 sh/sleep。可把 `LAB_IMAGE` 换成自己允许使用的镜像。本次云端遇到 Docker Hub 限流，使用了本地准备的 `k8s-learning/sleeper:kind-v1.34.0`；这个本地标签不能直接从公有仓库拉取，也不代表 Java 镜像。

下面的 `lab` 函数固定使用专用 kubeconfig 和 context。后续每条实验命令都用它，避免与第 2 章的现场查询 context 混在一起。先看节点 Ready、版本和 worker 名，再继续。

```bash
lab() {
  kubectl --kubeconfig "$LAB_KUBECONFIG" --context "$LAB_CONTEXT" "$@"
}
lab wait --for=condition=Ready node --all --timeout=180s
lab version -o json > "$LAB_ROOT/version.json"
mapfile -t LAB_WORKERS < <(lab get nodes -l '!node-role.kubernetes.io/control-plane' \
  -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}')
test "${#LAB_WORKERS[@]}" -eq 2
LAB_NODE="${LAB_WORKERS[0]}"
lab get nodes -o wide
ALLOC_M=$(lab get node "$LAB_NODE" -o json | python3 -c '
import json, sys
from decimal import Decimal
s=json.load(sys.stdin)["status"]["allocatable"]["cpu"]
print(int(Decimal(s[:-1]) if s.endswith("m") else Decimal(s)*1000))')
test "$ALLOC_M" -ge 1000
```

下面的小工具负责命名、等待、保存现场，以及为后续实验生成重复对象。先复制一次即可；**第 15.2 节的第一组实验会完整展示 Pod YAML，不使用对象生成器创建 Pod。**

其中的**实验对象生成器**不是 Kubernetes 源码。它为后续各组省去重复 YAML：普通 Pod 都选同一 worker，默认请求 50m/64Mi，容器只 sleep。`study_pod` 创建单只 Pod，`study_deploy` 创建 Deployment；第三/第四个参数中的 JSON 只补本组要比较的字段。CPU 参数 `-` 表示不填写 CPU request，专门用于默认值实验。

<details>
<summary>展开并复制一次小工具，后面各组复用</summary>

```bash
new_case() {
  LAB_NS="$LAB_NAME-$1"
  lab create namespace "$LAB_NS"
}
study_object() {
  python3 - "$LAB_NS" "$LAB_NODE" "$LAB_IMAGE" "$@" <<'PY'
import json, sys
ns, node, image, kind, name, cpu, replicas, extra = sys.argv[1:]
resources = {"requests": {"memory": "64Mi"}, "limits": {"memory": "128Mi"}}
if cpu != "-":
    resources["requests"]["cpu"] = cpu
c = {"name": "sleeper", "image": image, "imagePullPolicy": "IfNotPresent",
     "command": ["sh", "-c", "sleep 3600"], "resources": resources}
patch = json.loads(extra)
for field, value in patch.pop("container", {}).items():
    if field == "resources":
        for category, quantities in value.items():
            c["resources"].setdefault(category, {}).update(quantities)
    else:
        c[field] = value
spec = {"nodeSelector": {"kubernetes.io/hostname": node},
        "restartPolicy": "Always" if kind == "Deployment" else "Never",
        "terminationGracePeriodSeconds": 5, "automountServiceAccountToken": False,
        "containers": [c]}
spec.update(patch)
labels = {"app": name}
obj = {"apiVersion": "apps/v1" if kind == "Deployment" else "v1", "kind": kind,
       "metadata": {"name": name, "namespace": ns, "labels": labels}, "spec": spec}
if kind == "Deployment":
    obj["spec"] = {"replicas": int(replicas), "selector": {"matchLabels": labels},
                   "template": {"metadata": {"labels": labels}, "spec": spec}}
print(json.dumps(obj))
PY
}
study_pod() {
  local extra='{}'
  if [ "$#" -ge 3 ]; then extra="$3"; fi
  study_object Pod "$1" "$2" 0 "$extra" | lab apply -f -
}
study_deploy() {
  local extra='{}'
  if [ "$#" -ge 4 ]; then extra="$4"; fi
  study_object Deployment "$1" "$3" "$2" "$extra" | lab apply -f -
}
wait_ready() {
  lab wait -n "$LAB_NS" --for=condition=Ready "pod/$1" --timeout=180s
}
wait_rejected() {
  lab wait -n "$LAB_NS" --for=condition=PodScheduled=false "pod/$1" --timeout=90s
  lab get pod "$1" -n "$LAB_NS" -o json | python3 -c '
import json, sys
p=json.load(sys.stdin)
c=next(c for c in p["status"]["conditions"] if c["type"]=="PodScheduled")
assert not p["spec"].get("nodeName")
assert c["reason"]=="Unschedulable" and sys.argv[1] in c.get("message", "")
print(c["reason"], c["message"])' "$2"
}
save_case() {
  lab get pods,deployments,replicasets,resourcequotas,limitranges,events \
    -n "$LAB_NS" -o json > "$LAB_ROOT/$LAB_NS-$1.json"
  lab get nodes -o json > "$LAB_ROOT/$LAB_NS-$1-nodes.json"
  lab get pods -A --field-selector "spec.nodeName=$LAB_NODE" \
    -o json > "$LAB_ROOT/$LAB_NS-$1-node-pods.json"
}
```

</details>

`wait_rejected` 不只等一个 False：它还核对未绑定、Unschedulable 和本组的失败方向。超时或断言失败就停止，不能记作通过。`save_case` 保存现场；各对象读取不保证是同一瞬间的原子快照。

<a id="cpu-lab"></a>

### 15.2 从完整 YAML 做一次 CPU 对照，再看原 Pod 怎样重试

本节按顺序执行，复用 15.1 的隔离集群和变量。先写三个预测：单只请求超过节点可分配 CPU 会怎样？余额 200m 时申请 201m 会怎样？不改这只 Pod，只删除占位者，它有没有机会成功？

所有容器只 sleep，CPU 大 request 用于占请求账，不能把结果当 Java 性能测试。命令中的 `lab` 已固定实验 kubeconfig；`new_case` 创建本组 namespace；等待函数检查状态；`save_case` 保存对象。真正提交什么 Pod，下面每次都写出来。

#### 15.2.1 先认清 YAML：请求超过节点可分配 CPU

本次节点可分配 5000m，所以下面计算出的请求是 6000m；别的环境会按其 Allocatable 加 1000m。它连可分配量都超过了，删掉其他 Pod 也不够；这里比较的是 Allocatable，不是第 3.1 节的总容量 Capacity。

```bash
new_case cpu
OVERSIZED_M=$((ALLOC_M + 1000))
lab apply -f - <<YAML
apiVersion: v1
kind: Pod
metadata:
  name: oversized
  namespace: $LAB_NS
spec:
  nodeSelector:
    kubernetes.io/hostname: $LAB_NODE
  restartPolicy: Never
  terminationGracePeriodSeconds: 5
  automountServiceAccountToken: false
  containers:
  - name: sleeper
    image: $LAB_IMAGE
    imagePullPolicy: IfNotPresent
    command: ["sh", "-c", "sleep 3600"]
    resources:
      requests:
        cpu: ${OVERSIZED_M}m
        memory: 64Mi
      limits:
        memory: 128Mi
YAML
wait_rejected oversized 'Insufficient cpu'
lab get pod oversized -n "$LAB_NS" \
  -o custom-columns='NAME:.metadata.name,CPU:.spec.containers[0].resources.requests.cpu,NODE:.spec.nodeName,SCHEDULED:.status.conditions[?(@.type=="PodScheduled")].status'
save_case oversized
```

`<<YAML` 到单独一行 `YAML` 之间是交给 kubectl 的内容；Bash 会先把 `$LAB_NS`、`$LAB_NODE` 等变量换成实际值。这里没有写 `nodeName`，只是用 nodeSelector 限定目标节点，所以仍由 scheduler 检查能否放下。

| 这一项 | 为什么写它 |
|---|---|
| `nodeSelector` | 固定候选 worker，避免 Pod 跑到另一台空节点 |
| `requests.cpu` | 本题真正比较的数；不是要求 sleep 实际烧满这些 CPU |
| `requests.memory: 64Mi` | 仍声明内存要求；本组要确认它没有先成为瓶颈 |
| 没有 CPU limit | 保持只讨论 CPU 请求账；不引入 CPU 限速对照 |
| `restartPolicy: Never` | 这是独立实验 Pod，结束后不由 kubelet 重启容器 |

5000m 节点上的预期关键值如下。CPU 数量输出可能规范化成 `6`，它等于 6000m；节点列应为空，PodScheduled 应为 False：

```text
NAME        CPU   NODE     SCHEDULED
oversized   6     <none>   False
```

这四列只说明输入和当前状态。还要看上面等待函数打印的 `Unschedulable` 与 `Insufficient cpu`，才能对应 CPU 原因。其余节点可能同时报告标签、污点等原因，不要求整条 message 与某次样例逐字相同。

#### 15.2.2 留出 200m：先把节点已有请求算清楚

清理 oversized 后，按当前实际请求计算 holding 要占多少。本次是 `5000−100−200=4700m`。这里的 100m 是目标 worker 原有系统 Pod 的最终请求合计，不是写死的 Kubernetes 默认值。

<details>
<summary>展开并执行余额计算；第一遍只需理解上面的减法</summary>

```bash
lab delete pod oversized -n "$LAB_NS" --wait=true
lab get node "$LAB_NODE" -o json > "$LAB_ROOT/node.json"
lab get pods -A --field-selector "spec.nodeName=$LAB_NODE" -o json > "$LAB_ROOT/bound-pods.json"
HOLDING_M=$(python3 - "$LAB_ROOT/node.json" "$LAB_ROOT/bound-pods.json" <<'PY'
import json, math, sys
from decimal import Decimal
def milli(s):
    s=str(s)
    return math.ceil(Decimal(s[:-1]) if s.endswith("m") else Decimal(s)*1000)
node=json.load(open(sys.argv[1]))
pods=json.load(open(sys.argv[2]))["items"]
used=0
for p in pods:
    spec=p["spec"]
    status=p.get("status", {})
    assert status.get("phase") not in ("Succeeded", "Failed"), "仍有终态 Pod"
    assert not any(spec.get(k) for k in ("initContainers", "overhead", "resources")), "这里只算简单 Pod"
    assert not status.get("resize"), "resize 需另行核算"
    assert not any(c.get("status")=="True" and c.get("type") in ("PodResizePending", "PodResizeInProgress")
                   for c in status.get("conditions", [])), "resize 尚未稳定"
    states={c["name"]: c for c in status.get("containerStatuses", [])}
    for c in spec["containers"]:
        request=milli(c.get("resources", {}).get("requests", {}).get("cpu", "0"))
        cs=states.get(c["name"], {})
        allocated=(cs.get("allocatedResources") or {}).get("cpu")
        actuated=((cs.get("resources") or {}).get("requests") or {}).get("cpu")
        assert all(v is None or milli(v)==request for v in (allocated, actuated)), "CPU 配置尚未稳定"
        used+=request
holding=milli(node["status"]["allocatable"]["cpu"])-used-200
assert holding>0, "没有足够余额做这个对照"
print(holding)
PY
)
printf '目标节点=%s，占位请求=%sm，预留余额=200m\n' "$LAB_NODE" "$HOLDING_M"
```

</details>

计算器只适用于这个干净集群中的简单 Pod，遇到 init、overhead、Pod-level resources、终态或未稳定 resize 会停止。正在退出的 Pod 仍在输入中时，不擅自扣除它。先把余额算出来再继续，不要把自己机器上的 holding 也硬填为 4700m。

把占位 Pod 的完整 YAML 保存到实验临时目录，后面做 200m 边界对照时还要恢复它。这是本次运行产生的文件，不需要放进学习仓库。

```bash
cat > "$LAB_ROOT/holding.yaml" <<YAML
apiVersion: v1
kind: Pod
metadata:
  name: holding
  namespace: $LAB_NS
spec:
  nodeSelector:
    kubernetes.io/hostname: $LAB_NODE
  restartPolicy: Never
  terminationGracePeriodSeconds: 5
  automountServiceAccountToken: false
  containers:
  - name: sleeper
    image: $LAB_IMAGE
    imagePullPolicy: IfNotPresent
    command: ["sh", "-c", "sleep 3600"]
    resources:
      requests:
        cpu: ${HOLDING_M}m
        memory: 64Mi
      limits:
        memory: 128Mi
YAML
lab apply -f "$LAB_ROOT/holding.yaml"
wait_ready holding
```

先等 holding Ready，证明它已经占到位置，再创建竞争者。否则两个 Pod 一起提交，先后顺序改变，就可能做成另一道题。

#### 15.2.3 只差 1m，也先不能放

下面的 one-over 申请 201m，目标节点余额是 200m。内存等其他条件通过时，预期仍是未绑定、CPU 不足。

```bash
lab apply -f - <<YAML
apiVersion: v1
kind: Pod
metadata:
  name: one-over
  namespace: $LAB_NS
spec:
  nodeSelector:
    kubernetes.io/hostname: $LAB_NODE
  restartPolicy: Never
  terminationGracePeriodSeconds: 5
  automountServiceAccountToken: false
  containers:
  - name: sleeper
    image: $LAB_IMAGE
    imagePullPolicy: IfNotPresent
    command: ["sh", "-c", "sleep 3600"]
    resources:
      requests:
        cpu: 201m
        memory: 64Mi
      limits:
        memory: 128Mi
YAML
wait_rejected one-over 'Insufficient cpu'
CPU_WAIT_UID=$(lab get pod one-over -n "$LAB_NS" -o jsonpath='{.metadata.uid}')
lab get pods holding one-over -n "$LAB_NS" \
  -o custom-columns='NAME:.metadata.name,CPU:.spec.containers[0].resources.requests.cpu,NODE:.spec.nodeName,READY:.status.conditions[?(@.type=="Ready")].status'
save_case cpu-201
lab get --raw "/api/v1/nodes/$LAB_NODE/proxy/stats/summary" > "$LAB_ROOT/cpu-stats.json"
```

预期 holding 已在目标 worker 上并 Ready，one-over 没有 nodeName。节点统计里的 usageNanoCores 除以 1000000 得到 m，时间在 cpu.time 中；它只是一次采样，不是全过程峰值。即使 CPU 很闲，也没有改变请求账只剩 200m 的事实。

#### 15.2.4 不改 one-over：删除占位者后检查同一个 UID

这一步只删除我们创建的 holding。它正常退出、删除变化被 scheduler 处理后，本次节点余额由 200m 回到 4900m，原来的 201m 就有机会通过。不能拿同样的动作直接删除生产中的健康业务 Pod。

```bash
lab delete pod holding -n "$LAB_NS" --wait=true
wait_ready one-over
test "$CPU_WAIT_UID" = "$(lab get pod one-over -n "$LAB_NS" -o jsonpath='{.metadata.uid}')"
test "201m" = "$(lab get pod one-over -n "$LAB_NS" -o jsonpath='{.spec.containers[0].resources.requests.cpu}')"
lab get pod one-over -n "$LAB_NS" \
  -o custom-columns='NAME:.metadata.name,UID:.metadata.uid,CPU:.spec.containers[0].resources.requests.cpu,NODE:.spec.nodeName,READY:.status.conditions[?(@.type=="Ready")].status'
save_case cpu-same-uid-ready
```

两个 `test` 分别检查“还是原对象”和“请求没被改”。预期对照如下，U 表示你实际记录的同一个 UID，节点名用“目标 worker”代称，并非命令原样输出：

| 观察时刻 | UID | CPU request | nodeName | Ready |
|---|---|---:|---|---|
| holding 仍占位 | U | 201m | 空 | 未就绪 |
| holding 删除后，等待成功 | U | 201m | 目标 worker | True |

**你刚证明的是：**原 Pod 没变，外部资源条件变好后，它重新获得位置并启动。为什么删除会影响队列、哪一步写入绑定，回到[第 14.1 节](#cpu-source-walk)逐步看。

如果没有成功，先保留现场：holding 是否真的消失？系统请求是否增加？失败原因是否仍是 CPU？同 UID 只能证明对象没换，不能代替这些检查，也不能单凭这份快照确定内部是哪次通知触发了重试。

#### 15.2.5 恢复相同占用，再验证刚好 200m

最后回到原来的 200m 余额。先删除已经运行的 one-over，恢复 holding 并等它 Ready，再创建申请 200m 的 exact-fit。**如果不恢复占位者，拿空节点跑成功就没有验证边界。**这一小组的对象名和 UID 会变化，只用来比较相同占用下的请求大小，不作为上一小组“同对象重试”的证据。

```bash
lab delete pod one-over -n "$LAB_NS" --wait=true
lab apply -f "$LAB_ROOT/holding.yaml"
wait_ready holding
lab apply -f - <<YAML
apiVersion: v1
kind: Pod
metadata:
  name: exact-fit
  namespace: $LAB_NS
spec:
  nodeSelector:
    kubernetes.io/hostname: $LAB_NODE
  restartPolicy: Never
  terminationGracePeriodSeconds: 5
  automountServiceAccountToken: false
  containers:
  - name: sleeper
    image: $LAB_IMAGE
    imagePullPolicy: IfNotPresent
    command: ["sh", "-c", "sleep 3600"]
    resources:
      requests:
        cpu: 200m
        memory: 64Mi
      limits:
        memory: 128Mi
YAML
wait_ready exact-fit
lab get pods -n "$LAB_NS" -o wide
save_case cpu-200
lab delete namespace "$LAB_NS" --wait=true
```

**合上命令复述：**超过节点可分配量，删占位者仍不够；只超过当前余额，释放请求可能有用；同一余额下 201m 失败、200m 通过，来自严格的大于判断；绑定之后还要等容器 Ready。能分别说明这四个结果，再继续下一组。

### 15.3 配额：查 ReplicaSet，而不是等待不存在的第二只 Pod

先预测：两只各 100m、配额 150m，应该是一只 Ready、一只没创建，还是两只都创建后其中一只 Pending？

```bash
new_case quota
lab apply -f - <<YAML
apiVersion: v1
kind: ResourceQuota
metadata:
  name: cpu-budget
  namespace: $LAB_NS
spec:
  hard:
    requests.cpu: 150m
YAML
lab wait -n "$LAB_NS" --for=jsonpath='{.status.hard.requests\.cpu}'=150m \
  resourcequota/cpu-budget --timeout=90s
study_deploy quota-demo 2 100m
lab wait -n "$LAB_NS" --for=jsonpath='{.status.readyReplicas}'=1 deployment/quota-demo --timeout=180s
lab get pods -n "$LAB_NS" -o wide
lab get events -n "$LAB_NS" --field-selector reason=FailedCreate --sort-by=.metadata.creationTimestamp
lab get replicasets,resourcequotas -n "$LAB_NS" -o yaml
save_case quota-150
QUOTA_OLD_UID=$(lab get pods -n "$LAB_NS" -l app=quota-demo -o jsonpath='{.items[0].metadata.uid}')
lab patch resourcequota cpu-budget -n "$LAB_NS" --type=merge \
  -p '{"spec":{"hard":{"requests.cpu":"200m"}}}'
lab rollout status deployment/quota-demo -n "$LAB_NS" --timeout=180s
lab get pods -n "$LAB_NS" -o custom-columns='NAME:.metadata.name,UID:.metadata.uid,READY:.status.conditions[?(@.type=="Ready")].status'
save_case quota-200
lab delete namespace "$LAB_NS" --wait=true
```

看 FailedCreate 的 involvedObject.uid 是否对应 ReplicaSet；消息应包含 requested=100m、used=100m、limited=150m。放宽后检查两只 Ready，原来的 `QUOTA_OLD_UID` 仍在。只是“没看到第二只”不足以证明配额原因，只有配额和创建失败证据对应上才算完成。

### 15.4 最终 request：四组输入都看一遍

先写四个预测值，再运行。第一组同时保留 Deployment 模板和最终 Pod，不把二者混为一份配置。

```bash
new_case defaults
study_deploy limit-only 1 - '{"container":{"resources":{"limits":{"cpu":"300m"}}}}'
lab rollout status deployment/limit-only -n "$LAB_NS" --timeout=180s
lab get deployment limit-only -n "$LAB_NS" -o jsonpath='{.spec.template.spec.containers[0].resources}{"\n"}'
lab get pods -n "$LAB_NS" -l app=limit-only -o jsonpath='{.items[0].spec.containers[0].resources}{"\n"}'
study_pod explicit-request 100m '{"container":{"resources":{"limits":{"cpu":"300m"}}}}'
wait_ready explicit-request
lab apply -f - <<YAML
apiVersion: v1
kind: LimitRange
metadata:
  name: cpu-default
  namespace: $LAB_NS
spec:
  limits:
  - type: Container
    defaultRequest:
      cpu: 100m
YAML
study_pod namespace-default -
wait_ready namespace-default
study_pod limit-with-default - '{"container":{"resources":{"limits":{"cpu":"300m"}}}}'
wait_ready limit-with-default
lab get pods -n "$LAB_NS" -o custom-columns='NAME:.metadata.name,REQUEST:.spec.containers[0].resources.requests.cpu,LIMIT:.spec.containers[0].resources.limits.cpu'
save_case defaults
lab delete namespace "$LAB_NS" --wait=true
```

结果应为 300m、100m、100m、300m。LimitRange 创建前后各只比较明确的输入；这里没有额外 webhook 或 Pod-level resources。模板 CPU request 仍省略、第一只最终 Pod 却已有 300m，才能证明第 3.3 节讨论的差别。

### 15.5 端口：删占位者后，看同一个 UID 是否继续成功

先预测 A、B 都申请同节点 TCP 18080，C 只声明 containerPort=8080。哪只会等待？下面只使用新建隔离节点；若该节点已有其他 Pod 申请 TCP 18080，先核对现场，不使用这组“干净对照”。

```bash
new_case hostport
study_pod port-holder 50m '{"container":{"ports":[{"containerPort":8080,"hostPort":18080,"protocol":"TCP"}]}}'
wait_ready port-holder
study_pod port-waiter 50m '{"container":{"ports":[{"containerPort":8080,"hostPort":18080,"protocol":"TCP"}]}}'
wait_rejected port-waiter 'free ports'
PORT_WAIT_UID=$(lab get pod port-waiter -n "$LAB_NS" -o jsonpath='{.metadata.uid}')
lab get pods -A --field-selector "spec.nodeName=$LAB_NODE" -o json > "$LAB_ROOT/hostport-bound-pods.json"
save_case port-blocked
study_pod container-port-only 50m '{"container":{"ports":[{"containerPort":8080,"protocol":"TCP"}]}}'
wait_ready container-port-only
lab delete pod port-holder -n "$LAB_NS" --wait=true
wait_ready port-waiter
test "$PORT_WAIT_UID" = "$(lab get pod port-waiter -n "$LAB_NS" -o jsonpath='{.metadata.uid}')"
save_case port-released
lab delete namespace "$LAB_NS" --wait=true
```

CPU 余额要用 `hostport-bound-pods.json` 的最终请求核对；若另有资源不足，就不能把本组解释成单纯端口问题。sleep 并未监听 8080，这里证明的是端口声明进入调度账。应用能否 bind/listen 还要实际启动业务验证。

### 15.6 已绑定、gate、污点：再做三种责任对照

这一组仍只回答“卡在哪一步”。坏镜像应该能绑定，带 gate 的 Pod 尚未进入普通选点，未容忍污点的 Pod 则因硬条件失败。

```bash
new_case stages
study_pod bad-image 50m '{"container":{"image":"registry.invalid/not-a-real-image:0"}}'
lab wait -n "$LAB_NS" --for=condition=PodScheduled=true pod/bad-image --timeout=90s
lab wait -n "$LAB_NS" --for=jsonpath='{.status.containerStatuses[0].state.waiting.reason}'=ImagePullBackOff \
  pod/bad-image --timeout=120s
save_case bound-image-failed
study_pod gated 50m '{"schedulingGates":[{"name":"scheduler-md.example/release"}]}'
lab get pod gated -n "$LAB_NS" -o jsonpath='{.spec.nodeName}{" | "}{.spec.schedulingGates}{"\n"}'
save_case gate-present
lab patch pod gated -n "$LAB_NS" --type=json \
  -p '[{"op":"remove","path":"/spec/schedulingGates"}]'
wait_ready gated
lab taint node "$LAB_NODE" "scheduler-md.example/block=$LAB_NAME:NoSchedule"
study_pod no-toleration 50m
wait_rejected no-toleration 'untolerated taint'
study_pod with-toleration 50m "{\"tolerations\":[{\"key\":\"scheduler-md.example/block\",\"operator\":\"Equal\",\"value\":\"$LAB_NAME\",\"effect\":\"NoSchedule\"}]}"
wait_ready with-toleration
save_case taint-control
lab taint node "$LAB_NODE" scheduler-md.example/block:NoSchedule-
lab delete namespace "$LAB_NS" --wait=true
```

gate-present 保存时应仍无 nodeName 且 gate 存在；解除后再检查 Ready。坏镜像最终失败是这组的正确结果，不能要求它 Ready。NoSchedule 不会因为加污点就把前面已绑定的 Pod 驱逐。

### 15.7 两个域：默认 minDomains 与 3 的区别

先预测第三只：两个空域、maxSkew=1，省略 minDomains 时能否三只 Ready？设为 3 时第几只开始等待？两组从零计数。

```bash
new_case topology
lab label node "${LAB_WORKERS[0]}" scheduler-md.example/pool="$LAB_NAME" scheduler-md.example/zone=a
lab label node "${LAB_WORKERS[1]}" scheduler-md.example/pool="$LAB_NAME" scheduler-md.example/zone=b
SPREAD_RULE=$(cat <<JSON
{"nodeSelector":{"scheduler-md.example/pool":"$LAB_NAME"},
 "topologySpreadConstraints":[{"maxSkew":1,"topologyKey":"scheduler-md.example/zone",
 "whenUnsatisfiable":"DoNotSchedule","nodeAffinityPolicy":"Honor","nodeTaintsPolicy":"Honor",
 "labelSelector":{"matchLabels":{"app":"spread-default"}}}]}
JSON
)
study_deploy spread-default 3 20m "$SPREAD_RULE"
lab rollout status deployment/spread-default -n "$LAB_NS" --timeout=180s
lab get pods -n "$LAB_NS" -l app=spread-default -o wide
save_case default-three
lab delete deployment spread-default -n "$LAB_NS" --wait=true
lab delete pods -n "$LAB_NS" -l app=spread-default --ignore-not-found --wait=true
SPREAD_MIN3=$(python3 - "$SPREAD_RULE" <<'PY'
import json, sys
spec=json.loads(sys.argv[1])
rule=spec["topologySpreadConstraints"][0]
rule["minDomains"]=3
rule["labelSelector"]["matchLabels"]["app"]="spread-min3"
print(json.dumps(spec))
PY
)
study_deploy spread-min3 3 20m "$SPREAD_MIN3"
lab wait -n "$LAB_NS" --for=jsonpath='{.status.readyReplicas}'=2 deployment/spread-min3 --timeout=180s
SPREAD_WAIT=$(lab get pods -n "$LAB_NS" -l app=spread-min3 -o json | python3 -c '
import json, sys
pods=json.load(sys.stdin)["items"]
pending=[p["metadata"]["name"] for p in pods if not p["spec"].get("nodeName")]
assert len(pending)==1
print(pending[0])')
wait_rejected "$SPREAD_WAIT" 'topology spread'
lab get pods -n "$LAB_NS" -l app=spread-min3 -o wide
save_case min3-two
lab delete namespace "$LAB_NS" --wait=true
lab label node "${LAB_WORKERS[0]}" scheduler-md.example/pool- scheduler-md.example/zone-
lab label node "${LAB_WORKERS[1]}" scheduler-md.example/pool- scheduler-md.example/zone-
```

核对第一组分布为 2/1，第二组为 1/1；副本总数够还不算完成。这里只验证假域上的计算，真实 AZ 的失联、存储和流量切换不在这组实验中。

### 15.8 发布：先确认旧副本仍 Ready，再改变预算

每只用 Allocatable 的约 55% 申请 CPU，目标是“一只可运行，两只放不下”。第一只若未 Ready，不能继续把后续失败当作发布余量或抢占证据。

```bash
new_case rollout
SHAPE_M=$((ALLOC_M * 55 / 100 + 1))
study_deploy rollout-demo 1 "${SHAPE_M}m"
lab patch deployment rollout-demo -n "$LAB_NS" --type=merge \
  -p '{"spec":{"strategy":{"rollingUpdate":{"maxSurge":1,"maxUnavailable":0}}}}'
lab rollout status deployment/rollout-demo -n "$LAB_NS" --timeout=180s
ROLLOUT_OLD_UID=$(lab get pods -n "$LAB_NS" -l app=rollout-demo -o jsonpath='{.items[0].metadata.uid}')
lab patch deployment rollout-demo -n "$LAB_NS" --type=merge \
  -p '{"spec":{"template":{"metadata":{"annotations":{"study-revision":"two"}}}}}'
lab wait -n "$LAB_NS" --for=jsonpath='{.status.replicas}'=2 deployment/rollout-demo --timeout=90s
ROLLOUT_WAIT=$(lab get pods -n "$LAB_NS" -l app=rollout-demo -o json | python3 -c '
import json, sys
pods=json.load(sys.stdin)["items"]
pending=[p["metadata"]["name"] for p in pods if not p["spec"].get("nodeName")]
assert len(pending)==1
print(pending[0])')
wait_rejected "$ROLLOUT_WAIT" 'Insufficient cpu'
save_case old-ready-new-waits
lab patch deployment rollout-demo -n "$LAB_NS" --type=merge \
  -p '{"spec":{"strategy":{"rollingUpdate":{"maxSurge":0,"maxUnavailable":1}}}}'
for i in $(seq 1 10); do
  save_case "budget-change-$i"
  lab get deployment rollout-demo -n "$LAB_NS" \
    -o custom-columns='GEN:.metadata.generation,OBS:.status.observedGeneration,UPDATED:.status.updatedReplicas,AVAILABLE:.status.availableReplicas'
  sleep 1
done
lab rollout status deployment/rollout-demo -n "$LAB_NS" --timeout=180s
lab get pods -n "$LAB_NS" -o custom-columns='NAME:.metadata.name,UID:.metadata.uid,DELETING:.metadata.deletionTimestamp,READY:.status.conditions[?(@.type=="Ready")].status'
save_case rollout-finished
lab delete namespace "$LAB_NS" --wait=true
```

对照 `ROLLOUT_OLD_UID`，区分旧副本仍 Ready、开始删除、对象消失、新副本可用。采样可能看到零可用副本；没采到短暂状态不代表没发生。Deployment 的汇总可能已不计入正在终止的旧 Pod，不能仅凭 replicas=1 推出旧容器退出。本组没有 Service/HTTP，不报告业务中断秒数。

### 15.9 抢占：两个高优先级数值相同，只有策略不同

先等 low Ready，再比较两个同为 20000 的高优先级 Pod。Never 应仍等待，low 存活；允许抢占后，再检查 low 消失与 high Ready。

```bash
new_case preemption
SHAPE_M=$((ALLOC_M * 55 / 100 + 1))
for role in low never high; do
  value=20000
  policy=PreemptLowerPriority
  if [ "$role" = low ]; then value=100; fi
  if [ "$role" = never ]; then policy=Never; fi
  lab apply -f - <<YAML
apiVersion: scheduling.k8s.io/v1
kind: PriorityClass
metadata:
  name: $LAB_NAME-$role
value: $value
globalDefault: false
preemptionPolicy: $policy
YAML
done
study_pod low "${SHAPE_M}m" "{\"priorityClassName\":\"$LAB_NAME-low\",\"terminationGracePeriodSeconds\":20,\"container\":{\"lifecycle\":{\"preStop\":{\"exec\":{\"command\":[\"sh\",\"-c\",\"sleep 5\"]}}}}}"
wait_ready low
LOW_UID=$(lab get pod low -n "$LAB_NS" -o jsonpath='{.metadata.uid}')
study_pod never "${SHAPE_M}m" "{\"priorityClassName\":\"$LAB_NAME-never\"}"
wait_rejected never 'Insufficient cpu'
wait_ready low
save_case never-low-survives
lab delete pod never -n "$LAB_NS" --wait=true
study_pod high "${SHAPE_M}m" "{\"priorityClassName\":\"$LAB_NAME-high\"}"
wait_ready high
lab wait -n "$LAB_NS" --for=delete pod/low --timeout=90s
lab get events -n "$LAB_NS" --field-selector "involvedObject.uid=$LOW_UID" --sort-by=.metadata.creationTimestamp
save_case high-ready
lab delete namespace "$LAB_NS" --wait=true
lab delete priorityclass "$LAB_NAME-low" "$LAB_NAME-never" "$LAB_NAME-high"
```

high 最后 Ready 还要与 low 的原 UID、删除和 Preempted 证据关联。nominatedNodeName 可能太短而未采到，它不是绑定承诺；不要为了截到该字段就改变优先级、重建 Pod 或打乱对照顺序。

### 15.10 做完以后：解释结果，再清理集群

每组交六句话，不只交“命令成功”：预期、实际输入、哪个 UID/字段支持判断、另一个原因会有什么不同、只改变了什么，以及放到生产会有什么代价。绑定、容器 Ready、业务恢复分别验收；本文的 sleep Ready 只验证实验容器。

保存 `LAB_ROOT` 下的版本、输入和 JSON 证据。kubeconfig 是访问凭据，不上传。确认集群名称还是 15.1 中本次创建的名称后，再删除它；证据目录留着复盘。

```bash
kind get clusters
kind delete cluster --name "$LAB_NAME" --kubeconfig "$LAB_KUBECONFIG"
```

有错误先保留现场。CSI/WFFC 需要合适驱动与存储拓扑；真实 GPU/DRA 需要设备与 driver；Bind 失败注入需要源码测试环境。它们是进一步实验，不把普通 kind 的这些结果当作全部链路验证。

<a id="verification"></a>

## 16. 实测记录：这次真的验证到了哪里

2026-10-01 在云端独立 kind 集群验证。环境为 kind v0.30.0、kubectl/server v1.34.0，server commit `f28b4c9efbca5c5c0af716d9f2d5702667ee8a45`。1 个控制面、2 个 worker，目标 worker Allocatable 为 5000m、原系统请求 100m。使用本地 sh/sleep 镜像，镜像 ID 为 `sha256:77529943b1c8f1d968af94870125a6ba4ffe88fbe97f089a8f7fcadb0f9901e1`。

2026-10-01 这一轮复用了已创建的这套独立 kind 集群，直接从当时版本提取第 15.2—15.9 节的八组实验命令，全部正常退出，覆盖下表的 11 项对照。保存了 27 份 namespace 对象快照，另有节点、跨 namespace 请求账和 CPU 采样 JSON；再核对 Ready、nodeName、最终请求、失败原因、新旧 UID 和 Event 对应关系。该轮没有重复执行创建、删除集群的命令。

每项结果都按自己的输入解释，不把这一份节点容量和 UID 当作你的环境必然相同。先前验证的 112 条现场快照属于上一轮，采集格式不同，不与这次的 27 份合并计数。

| 对照 | 实际结果 | 正文说明 |
|---|---|---|
| 超过节点容量 | 6000m 未绑定且 CPU 不足；小请求绑定并 Ready | 3.2.2 |
| 超过剩余余额 | 5000−100−4700=200m；201m 未绑定，200m Ready | 3.2.1 |
| 污点与标签 | selector 无法绕过 NoSchedule；加相应 toleration 后 Ready | 4.3 |
| 坏镜像 | 已绑定，随后 ErrImagePull / ImagePullBackOff | 1.2 |
| gate | 有 gate 时未绑定；移除后 Ready | 10.4、15.6 |
| 两域分布 | 默认三只 Ready，2/1；minDomains=3 时 1/1，第三只等待 | 6.2.1 |
| 发布余量 | 旧 Ready、新 CPU 等待；改变预算后的十次采样中，可用副本为 0×6、1×4，新副本 UID 保留 | 7.4 |
| 抢占策略 | Never 时 low Ready；允许抢占后 low 消失、high Ready，Preempted Event 对应双方 UID | 9.3 |
| namespace 配额 | 150m 只创建一只 100m；改为 200m 后两只 Ready，原 UID 保留 | 1.4 |
| 最终默认请求 | 模板未写 CPU request，Pod 为 300m；其余三组为 100/100/300m | 3.3.1 |
| hostPort | CPU 余额 4850m，50m 的 Pod 仍因 TCP 18080 等待；释放后同 UID Ready | 8.3 |

CPU usage 是不同采样时刻的测量，不是全过程峰值。配额的 FailedCreate Event 已按 ReplicaSet UID 对应；端口实验没有监听 Java 服务。发布没有 HTTP 探测，表里的零可用副本不代表已经测出中断时长。

源码核对使用 `/workspace/kubernetes` 的固定教学提交 `301946d15e67a4a2e8a5fb8292eb836acd366d78`，describe 为 `301946d1`，工作区干净；关键 CPU、默认请求、端口、拓扑与失败路径另对照实验版本。Go 摘录保留真实语句，只补教学注释。没有运行上游 Go 测试、真实 GPU/CUDA、DRA driver、CSI、Java 压测、PromQL 查询或真实 AZ 故障；相关内容按教学推演和目标版本核对。

### 16.1 2026-10-02 补跑：完整 YAML 与同 UID 重试

这次使用北京时间记录日期，复用上面同一套 v1.34.0 隔离集群与 sleep 镜像。从本版第 15.2 节直接提取六段 Bash，按正文顺序执行，正常退出；没有使用 `study_pod` 生成本组 Pod，也没有重新创建、删除整套集群。

新增保存四份 namespace 快照，每份另存节点与目标节点已绑定 Pod；分别对应 oversized、201m 失败、同 UID 的 201m 成功、恢复占用后的 200m 成功。数字与结果如下：

| 检查 | 实际结果 |
|---|---|
| 单只超过可分配量 | Allocatable=5000m，请求 6000m；nodeName 为空，PodScheduled=False，原因包含 Insufficient cpu |
| 仅超过余额 | 系统请求 100m，holding=4700m；one-over=201m 未绑定 |
| 删除 holding 后 | one-over 的 UID 和 201m 请求均保持不变，绑定到原目标 worker 并 Ready |
| 恢复后比较边界 | 先删除 one-over、恢复 holding=4700m，再创建 exact-fit=200m；它绑定并 Ready |
| 清理 | 本组 namespace 已删除，保留取证 JSON，整套学习集群未删除 |

这轮验证的是对象、请求和状态变化；没有用断点确认每次内部队列唤醒的来源，也没有测 Java 接口或 GPU 性能。第 14.1 节的内部路径来自固定源码核对，五段新增 Go 摘录均与相应源码的连续语句对应。

第 13.4.2 节的四段 DRA 摘录补齐 apiVersion、kind、Pod 镜像等必要字段后，也交给同一 v1.34.0 API 执行了 `kubectl create --dry-run=server --validate=strict`，四类对象全部通过。server dry-run 是让服务器检查请求、但不保存对象；它只验证这些字段可被 API 接受，没有创建真实设备、运行分配器或验证节点驱动。

<a id="terms"></a>

## 17. 专业词速查：看到这个词，先这样理解

**首遍只作查表，不用背。**正文在用到术语时解释它的作用；这里把本文的对象、字段、缩写和源码用语集中起来。按下面的分类找词，每行从左往右读：左边是你会在文档或输出里看到的名字，右边是它在本文中的大白话含义。

### 17.1 对象、组件和状态：谁负责，做到哪一步

| 词或字段 | 大白话解释 |
|---|---|
| Kubernetes / K8s | 管理容器应用的一套系统；K8s 是它的简称。 |
| Pod | Kubernetes 放置和运行容器的一组单位；普通 Pod 内的容器落在同一台节点上。 |
| Node | 集群里提供 CPU、内存等资源的节点；在 kind 中，节点本身跑在容器里。 |
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

### 17.2 资源账和 Java：申请了多少，实际用了多少

| 词或字段 | 大白话解释 |
|---|---|
| Capacity | 节点报告的总容量。 |
| Allocatable | 节点可分配给 Pod 的容量；它还没有扣除其他 Pod 的请求。 |
| request / Requested / usage | request 是本 Pod 的申请量；Requested 是节点账上已经计入的请求；usage 是测得的实际消耗。 |
| limit / limits | 运行时资源上限；CPU、内存、设备的实施规则不同，不是统一的“超了就排队”。 |
| 请求账 / 资源承诺 | 记录已经答应分配多少；sleep 没实际用 CPU，也不会自动把申请量归零。 |
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

### 17.3 选点和拓扑：哪个位置合格，副本怎样分布

| 词或字段 | 大白话解释 |
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

### 17.4 缓存、队列和返回结果：为什么等，失败怎样撤销

| 词或字段 | 大白话解释 |
|---|---|
| Framework / 插件 / 扩展点 | Framework 规定处理阶段；扩展点是这些阶段的接入口；插件实现一类具体规则。 |
| NodeResourcesFit / NodePorts | 分别是资源与节点端口相关插件的名字；前者可检查、评分资源，后者检查 hostPort 等端口声明。 |
| NodeAffinity / TaintToleration | 分别是处理节点亲和、污点容忍的插件名；前面的大白话规则对应到这些实现。 |
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
| Skip / `nil` Status | 本例 PreFilter 的 Skip 让后续省掉该插件 Filter；这个 Status 接口中的 nil 表示成功。 |
| 抢占 / victim / Preempted | 抢占尝试让低优先级 Pod 退出以腾位置；victim 是被选中的 Pod；Preempted 是相应事件原因。 |
| PriorityClass / priorityClassName / priority | PriorityClass 保存优先级配置，Pod 用 priorityClassName 引用它，priority 是对应数值。 |
| preemptionPolicy / Never / PreemptLowerPriority | 决定是否主动抢占；Never 不主动抢，PreemptLowerPriority 可抢低优先级；都不保证成功。 |
| nominatedNodeName / 提名 | 抢占等流程中的潜在落点提示；不是正式绑定，也不是保证不变的节点锁。 |
| leader / leader election / Lease | 当前负责人、选负责人过程、记录负责人及续约信息的对象；同一套主备用它协调谁工作。 |
| Extender / ignorable | Extender 是调度器通过网络调用的扩展服务；ignorable 决定调用报错时是否允许跳过，要核对实际路径。 |

### 17.5 卷、网络和发布：绑定之后还有哪些步骤

| 词或字段 | 大白话解释 |
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

### 17.6 GPU 和设备：数量、具体卡和性能分开看

| 词或字段 | 大白话解释 |
|---|---|
| GPU / 显存 | GPU 是做并行计算的处理器；显存是 GPU 内存，Pod 的 memory 算节点内存。传统 GPU 数量不表示显存余量，显存另查设备和应用数据。 |
| 扩展资源 / scalar | CPU、内存等内置类型以外的资源，如 nvidia.com/gpu；传统 scalar 只按资源名与数量计账，没有具体设备属性。 |
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
| worker | 在 kind 中是工作节点，在训练中是任务成员；训练成员通常由 Pod 承载，不能直接换算成 Node 数。 |

### 17.7 批任务和队列：任务获准，不等于成员都启动了

| 词或字段 | 大白话解释 |
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

### 17.8 指标、源码和 Go：数字与返回值怎样读

| 词或写法 | 大白话解释 |
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
| nil / error | nil 是没有值；error 是错误返回值类型。本文 Filter 的 nil Status 是成功，不能把所有 nil 都读成失败。 |
| DeepCopy / `对象.方法()` | DeepCopy 得到该值的独立副本；后者调用对象提供的方法，作用要看具体实现。 |
| goroutine / 共享状态 / 锁 | 可另行推进的 Go 工作、多个流程都可能读写的数据、保护访问顺序的方式；不等同独立 CPU 核。 |
| 编译 / 二进制 / 动态库 / 热加载 | 编译把源码转成程序；二进制在此指可执行程序；动态库是可被程序装载的代码文件；热加载是不重启换入代码，不能假定调度插件支持。 |

### 17.9 工具和验证：命令查什么，结果能证明什么

| 词或写法 | 大白话解释 |
|---|---|
| kubectl / kind / Docker | 查询和操作 Kubernetes 的工具、把节点跑在容器里的学习集群工具、运行这些容器的一种环境。 |
| 控制面 / control-plane | 负责 API、控制器、调度等管理工作的部分；kind 配置中的 control-plane 是控制面节点角色。 |
| kubeconfig / context | 保存连接和身份信息的文件，以及其中选择集群、身份、默认 namespace 的组合。 |
| Bash / PowerShell / WSL | 两种执行命令的工具，以及 Windows 中的 Linux 环境。 |
| YAML / JSON / JSONPath | 两种写对象数据的格式，以及按字段路径从 JSON 中取值的写法。 |
| Git / commit / SHA / 工作区 | 代码版本管理工具、一次保存的版本、它的标识、当前检出的文件；固定提交让来源可以复查。 |
| 镜像标签 / 摘要 / 镜像 ID | 标签可能被重新指向；@sha256 摘要按内容定位；镜像 ID 是本地镜像身份记录，不保证等于清单摘要。 |
| EKS / ACK / AWS / CloudWatch | AWS 和阿里云的托管 Kubernetes 服务、AWS 云平台、AWS 的日志与监控服务；入口和权限按厂商核对。 |
| 原子快照 / 采样 | 原子快照像同一状态点的一张照片；本文分批查询是几张照片。采样是某时刻或时间窗的测量，不是全过程峰值。 |
| usageNanoCores / cpu.time | 本文节点统计中的 CPU 用量值及采样时间；usageNanoCores 除以 1000000 得到 m。 |
| 断言 / 静态阅读 / 单测 / 回归 | 必须成立的检查、只看源码不执行、验证局部函数的给定输入、改动后比较原有行为是否受影响。 |
| 固定输入重放 / 负向验证 | 用同一批输入比较改动效果，以及故意保留一个不满足的条件，检查是否按预期拒绝。 |
| 集群实验 / 硬件实验 | 前者看 API、控制器、调度和节点的配合；后者才验证实际 GPU、驱动与应用能力，不能相互替代。 |
| server dry-run | 把请求交给 API 服务器检查，但不保存对象；不代表调度、设备准备或容器运行成功。 |

**查完一个词，再回到原例子问三句：谁用了它？它读或改了什么？哪个现场字段能证明结果？**能回答这三句，比记住英文全称有用。

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
