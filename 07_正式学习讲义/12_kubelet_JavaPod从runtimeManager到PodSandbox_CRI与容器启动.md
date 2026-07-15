# 第 12 课：Java Pod 进入 runtime manager——PodSandbox、CRI 与容器启动

> 主案例：`game-api-new-x` 已经绑定、卷也已准备好，但先后可能卡在 `NetworkNotReady`、`FailedCreatePodSandBox`、`ImagePullBackOff` 或容器 Create/Start  
> 主线源码：`pkg/kubelet/kubelet.go`、`pkg/kubelet/kuberuntime/*`、`staging/src/k8s.io/cri-client/pkg/remote_runtime.go`  
> 源码基线：`301946d15e67a4a2e8a5fb8292eb836acd366d78`（`v1.37.0-alpha.0-280-g301946d15e6`）  
> 本课深度：S3；以 Java 平台 Pod 为主，GPU 只做短映射  
> 前置断点：第 11 课停在 `Kubelet.SyncPod -> kl.containerRuntime.SyncPod(...)`

---

## 0. 这次不是再讲“容器是什么”

你已经运维 Kubernetes 多年，本课不重新介绍 Pod YAML、containerd 或 CNI 的名词定义。真正要解决的是这些生产问题：

1. `NODE` 已经有值，为什么业务容器可能还根本不存在？
2. `ContainerCreating` 到底可能卡在 sandbox、网络、镜像、Create 还是 Start？
3. kubelet、CRI runtime、CNI 三者的源码责任边界在哪里？
4. `FailedCreatePodSandBox` 是否一定等于 CNI 故障？
5. `PodReadyToStartContainers=True` 是否等于 Java 容器已经 Ready？
6. `Created`、`Started`、`Running`、`Ready` 分别越过了哪道门？
7. 为什么同一条 Warning Event 的 reason 可能只显示 `Failed`，必须继续读 message 和 Waiting reason？

本课读完，你应能把下面这句值班话术拆成可验证的源码断点：

```text
“Pod 卡在 ContainerCreating，应该是容器运行时有问题。”
```

拆成：

```text
先确认 kubelet是否因全局 NetworkReady=false 在外层返回；
再确认有没有进入 RunPodSandbox；
再确认 sandbox 是否 READY、有无 Pod IP；
再确认镜像是否已经拉取；
再确认 CRI CreateContainer / StartContainer 是否成功；
最后才进入 Java 进程、probe 和重启回路。
```

---

## 1. 主案例：同一个 kubectl STATUS，可能是四个完全不同的断点

先固定 Pod 身份：

```powershell
$ns = 'game'
$pod = 'game-api-new-x'
$uid = kubectl get pod $pod -n $ns -o jsonpath='{.metadata.uid}'
$node = kubectl get pod $pod -n $ns -o jsonpath='{.spec.nodeName}'

kubectl get pod $pod -n $ns -o jsonpath='{.metadata.uid}{"\n"}{.spec.nodeName}{"\n"}{.status.phase}{"\n"}{range .status.conditions[*]}{.type}{"="}{.status}{" reason="}{.reason}{" message="}{.message}{"\n"}{end}{range .status.containerStatuses[*]}{.name}{" waiting="}{.state.waiting.reason}{" message="}{.state.waiting.message}{"\n"}{end}'
kubectl get events -n $ns --field-selector "involvedObject.uid=$uid" --sort-by=.metadata.creationTimestamp
```

不要只盯 `kubectl get pod` 的 STATUS 列。STATUS 是客户端为了方便阅读选择出来的展示值，不是新的 API phase。

| 现场证据 | 至少说明什么 | 还不能说明什么 |
|---|---|---|
| `NetworkNotReady` | Kubelet.SyncPod 在调用 runtime manager 前发现节点 runtime 上报的全局网络未就绪 | 不能据此说某个 Pod 的 CNI ADD 已经执行 |
| `FailedCreatePodSandBox` | 已进入 runtime manager 的 sandbox 创建分支，并且 `createPodSandbox` 返回错误 | 不一定是 CNI；也可能是 sandbox config、日志目录、RuntimeClass lookup、CRI 等 |
| `FailedPodSandBoxStatus` | `RunPodSandbox` 已返回 ID，但随后 `PodSandboxStatus` 查询失败 | 不能归因成 sandbox 一定未创建 |
| `PodReadyToStartContainers=True` | 在当前能力开启且状态已传播时，kubelet判断无需新建 sandbox：已有 READY sandbox，Pod 网络模式/IP 满足继续启动容器 | 不能仅凭该 condition 证明所有 volume 此刻已挂载；也不代表镜像、Create/Start 或 Java Ready |
| `ErrImagePull/ImagePullBackOff` | sandbox 可以已成功，卡在业务容器 `CreateContainer` 之前的镜像阶段 | 不能说 Java 进程启动失败 |
| Normal `Created` | CRI `CreateContainer` 与 internal PreStart 已成功，容器对象已有 ID | 不能证明 `StartContainer` 成功 |
| Normal `Started` | CRI `StartContainer` 已成功返回 | 不能证明进程持续存活，也不能证明 readiness probe 通过 |
| `CrashLoopBackOff` | 容器至少曾被启动并退出，kubelet正在做重启退避 | 不是 sandbox 创建阶段 |
| `Running` 但 `Ready=False` | 业务进程存在，但 readiness 条件未满足 | 需要第 13 课的 probe/status 链 |

第一条运维原则：

```text
ContainerCreating 不是源码断点；
Event reason + 完整 message + condition + containerStatuses + 目标 Node runtime 证据
才可以逐层收窄。
```

---

## 首遍快速通道：先把 13 个箭头读通

```text
Kubelet.SyncPod
  -> kl.containerRuntime.SyncPod
  -> kubeGenericRuntimeManager.SyncPod
  -> computePodActions
  -> 必要时清理旧 sandbox / container
  -> createPodSandbox
  -> generatePodSandboxConfig
  -> instrumented RuntimeService
  -> remoteRuntimeService.RunPodSandbox
  -> CRI v1 gRPC -> runtime
  -> PodSandboxStatus / Pod IP / OnPodSandboxReady
  -> startContainer
  -> image manager -> remoteImageService.PullImage
  -> ContainerConfig -> CreateContainer -> StartContainer -> PostStart
```

首遍建议：

```text
先读：1～3、4 主结论、5～7、10～12、14～20、22、24.1、25.1/25.3、26～27
二读：4.2、8～9、13、21 的可选 RuntimeClass 实验、
      23 的 Go 复习索引细节、24.2/24.3、25.2
```

---

## 2. 从第 11 课的最后一行继续

外层 `Kubelet.SyncPod`：

```text
pkg/kubelet/kubelet.go:2210-2232
```

```go
pullSecrets := kl.getPullSecretsForPod(logger, pod)
kl.probeManager.AddPod(ctx, pod)

sctx := context.WithoutCancel(ctx)
result := kl.containerRuntime.SyncPod(
    sctx,
    pod,
    podStatus,
    pullSecrets,
    kl.crashLoopBackOff,
    restartingAllContainers,
)
kl.reasonCache.Update(pod.UID, result)
```

这里同时出现三本账：

| 参数/返回值 | 大白话 | 来源 |
|---|---|---|
| `pod *v1.Pod` | API 期望这个 Pod 长什么样 | apiserver / PodConfig / podManager |
| `podStatus *kubecontainer.PodStatus` | runtime 当前实际有什么 sandbox、container、IP、exit state | PLEG/runtime observation 填充 podCache；pod worker 用 `GetNewerThan` 取较新的状态 |
| `PodSyncResult` | 这一轮动作做成了什么、哪个动作报错 | runtime manager 本轮生成 |

所以 runtime manager 不是“接到 CREATE 就无脑创建”。它做的仍是 reconciliation：

```text
desired Pod
  + actual runtime PodStatus
  -> 算 action
  -> 执行必要动作
  -> 返回本轮结果
  -> 下一轮再对账
```

---

## 3. 先分清 PodSandbox 和业务容器

### 3.1 PodSandbox 是 Pod 级运行环境

从 CRI 抽象看，sandbox 承载 Pod 级上下文，例如：

- Pod identity：name、namespace、UID、attempt；
- Pod 网络 namespace 与 IP；
- IPC、PID、user namespace 选项；
- Pod DNS、hostname、port mappings；
- Pod cgroup parent、部分 security context；
- Pod 日志目录；
- runtime handler。

Linux + containerd 的常见实现里，你经常会看到一个 pause/infra 容器承载这层环境。但源码学习不能把两者永久画等号：

```text
CRI PodSandbox 是接口语义；
pause/infra container 是常见实现方式；
Kata、gVisor 或其他 runtime 的内部实现可以不同。
```

### 3.2 Java 业务容器是 sandbox 里面的 container

`game-api` 的镜像、command、env、mount、resources、device、lifecycle hook 等，最终进入 `ContainerConfig`，再由：

```text
CreateContainer(podSandboxID, containerConfig, sandboxConfig)
StartContainer(containerID)
```

处理。

因此：

```text
FailedCreatePodSandBox
  -> Java 容器通常还没 Create
  -> 不应先查 JVM 参数、Spring 日志或 readiness URL

ImagePullBackOff
  -> sandbox 可能已经 READY
  -> Java 容器仍可能没有 Container ID

Started
  -> 才能说 runtime 已把业务容器启动
  -> Java 是否活着、是否 Ready 还要继续看
```

---

## 4. `computePodActions`：主职责是列施工单

入口：

```text
pkg/kubelet/kuberuntime/kuberuntime_manager.go:1174-约1436
```

首个关键判断：

```go
createPodSandbox, attempt, sandboxID :=
    runtimeutil.PodSandboxChanged(pod, podStatus)

changes := podActions{
    KillPod:           createPodSandbox,
    CreateSandbox:     createPodSandbox,
    SandboxID:         sandboxID,
    Attempt:           attempt,
    ContainersToStart: []int{},
    ContainersToKill:  make(map[kubecontainer.ContainerID]containerToKillInfo),
}
```

Go 现场补——多返回值与 struct literal：

- `PodSandboxChanged` 一次返回 `bool、uint32、string` 三个值，调用方按位置接到 `createPodSandbox、attempt、sandboxID`；
- `podActions{...}` 是按字段创建一个结构体值；`KillPod: createPodSandbox` 不是 YAML，而是给 Go struct 字段赋初值；
- `make(map[...])` 创建一张可写的 map，用 container ID 作为后续 kill 计划的 key。

`PodSandboxChanged` 当前会重点检查：

1. runtime status 中是否根本没有 sandbox；
2. 是否有多个 READY sandbox；
3. 最新 sandbox 是否不 READY；
4. network namespace 模式是否与新 Pod spec 不一致；
5. 非 hostNetwork Pod 的 sandbox 是否没有 IP。

返回值：

```text
bool    是否应创建新 sandbox
uint32  新 attempt
string  当前/旧 sandbox ID
```

大白话：

```text
没有可用“房间” -> 计划建新房间；
旧房间结构不对或没网络 -> 先拆旧现场，再建；
房间仍可复用 -> 只处理里面哪些 container 该留、该停、该起。
```

但要避免只记类比。真正重要的是 `podActions` 把主要的“判断”与后续动作执行隔开：

| action | 含义 |
|---|---|
| `KillPod` | 需要停止旧 sandbox 及相关运行现场 |
| `CreateSandbox` | 需要新建 PodSandbox |
| `ContainersToKill` | sandbox 可保留，但这些 container 不应继续运行 |
| `ContainersToStart` | 普通容器本轮应尝试启动 |
| `InitContainersToStart` | init 容器本轮应尝试启动 |
| `EphemeralContainersToStart` | ephemeral 容器本轮应尝试启动 |
| `ContainersToUpdate` | 原地资源更新相关动作 |

这不是承诺函数绝对无副作用。当前实现检查退出容器时还会调用 internal lifecycle 的 PostStop 清理；所以把它理解成“主要 planner”，不要把它写成可任意重跑、绝对纯函数。

### 4.1 为什么创建新 sandbox 往往也要 KillPod

sandbox 是 Pod 级网络/namespace 锚点。旧 sandbox 不可用时，旧 container 不能简单搬到新 sandbox 继续跑。因此初始值同时设置：

```go
KillPod:       createPodSandbox,
CreateSandbox: createPodSandbox,
```

不是“创建新 sandbox 必然先 kill 一个健康 Pod”，而是：

```text
当对账判断旧 sandbox 已不满足 desired state，
就把旧 runtime 现场清掉，再从新的 Pod 级环境重建。
```

### 4.2 首遍不要把所有 restart 分支背下来

`computePodActions` 还处理：

- RestartPolicy；
- init 与 restartable init/sidecar；
- ephemeral container；
- 容器 spec hash 变化；
- resize；
- RestartAllContainers feature。

`CrashLoopBackOff` 不在这里判定；它在后面的 start helper 通过 `doBackOff` 检查失败历史。本课先掌握“新 Java Pod 创建”和“sandbox 失效重建”两条路径。全部 restart/resize 细节不是当前前置。

---

## 5. runtime manager 的九步不是九次固定 RPC

当前函数注释：

```text
pkg/kubelet/kuberuntime/kuberuntime_manager.go:1439-1450
```

```text
1. Compute sandbox and container changes
2. Kill pod sandbox if necessary
3. Kill containers that should not run
4. Create sandbox if necessary
5. OnPodSandboxReady
6. Create ephemeral containers
7. Create init containers
8. Resize running containers
9. Create normal containers
```

调用：

```go
func (m *kubeGenericRuntimeManager) SyncPod(
    ctx context.Context,
    pod *v1.Pod,
    podStatus *kubecontainer.PodStatus,
    pullSecrets []v1.Secret,
    backOff *flowcontrol.Backoff,
    restartAllContainers bool,
) (result kubecontainer.PodSyncResult) {
    podContainerChanges :=
        m.computePodActions(ctx, pod, podStatus, restartAllContainers)
    // 按 action 执行
}
```

Go 现场补——receiver 与 named return：

- `(m *kubeGenericRuntimeManager)` 可先类比 Java 方法里的 `this`；函数通过同一个 manager 指针访问 `m.runtimeService` 等字段；
- 返回值写成 `(result PodSyncResult)`，表示结果在入口就有名字，函数可不断往里面追加子结果，最后用裸 `return` 返回当前值；
- 这也是为什么读本函数不能只看最后一行，必须跟踪 `result.Add.../Fail...`。

注意三点：

1. 九步是逻辑顺序，不是每轮都执行九次动作；
2. `if CreateSandbox`、`for ContainersToKill` 等条件决定本轮真正做什么；
3. `result` 收集多个子动作结果，外层再写入 reason cache，影响 Waiting reason 和后续重试。

### 5.1 当前特殊容器顺序

源码执行顺序是：

```text
ephemeral -> init -> resize -> regular container
```

但普通新建 Pod 在创建时不能预先声明 ephemeral container，所以日常首次启动表现仍通常是：

```text
init -> app
```

不要把旧材料里的“init 永远先于 ephemeral”当成当前源码结论。restartable init/sidecar 的动作集合也更复杂；“一轮绝对只推进一个 init”只适用于部分普通 init 主路径，不能无限外推。

---

## 6. 第一扇网络门在 runtime manager 外面

`Kubelet.SyncPod` 先检查 runtime 的全局 network condition：

```text
pkg/kubelet/kubelet.go:2103-2107
```

```go
if err := kl.runtimeState.networkErrors();
    err != nil && !kubecontainer.IsHostNetworkPod(pod) {

    kl.recorder.Eventf(
        pod,
        v1.EventTypeWarning,
        events.NetworkNotReady,
        "%s: %v",
        NetworkNotReadyErrorMsg,
        err,
    )
    return false, nil, fmt.Errorf("%s: %v", NetworkNotReadyErrorMsg, err)
}
```

这个错误来源于 kubelet周期读取 runtime status：

```text
pkg/kubelet/kubelet.go:3245-3277
```

```go
networkReady := s.GetRuntimeCondition(kubecontainer.NetworkReady)
if networkReady == nil || !networkReady.Status {
    kl.runtimeState.setNetworkState(
        fmt.Errorf("container runtime network not ready: %v", networkReady),
    )
} else {
    kl.runtimeState.setNetworkState(nil)
}
```

这道门的语义：

| 现场 | 源码位置 | 范围 |
|---|---|---|
| `NetworkNotReady` | 外层 `Kubelet.SyncPod` | runtime 上报的节点级网络条件；非 hostNetwork Pod 被挡在 runtime manager 前 |
| `FailedCreatePodSandBox` + CNI message | `RunPodSandbox` 返回错误 | 某个 Pod 的 sandbox 创建过程；runtime 实现可能在里面调用 CNI |

所以不能画成：

```text
NetworkNotReady -> 这个 Pod 的 CNI ADD 已经失败
```

更准确是：

```text
节点 runtime 全局 NetworkReady=false
  -> kubelet暂不让普通 Pod进入 containerRuntime.SyncPod

节点全局 NetworkReady=true
  -> 某个 Pod仍可能在 RunPodSandbox 内因具体网络配置失败
```

hostNetwork Pod 绕过这道外层检查，因为它不需要新的 Pod network namespace；这不代表它绕过所有 runtime/sandbox 动作。

---

## 7. 创建 sandbox 前后有四个不同失败域

主块：

```text
pkg/kubelet/kuberuntime/kuberuntime_manager.go:1545-1657
```

### 7.1 DRA prepare 在 `createPodSandbox` 之前

```go
if featureEnabled(DynamicResourceAllocation) {
    if err := m.runtimeHelper.PrepareDynamicResources(ctx, pod); err != nil {
        recorder.Eventf(
            pod,
            Warning,
            FailedPrepareDynamicResources,
            "Failed to prepare dynamic resources: %v",
            err,
        )
        return
    }
}

podSandboxID, msg, err =
    m.createPodSandbox(ctx, pod, podContainerChanges.Attempt)
```

因此：

```text
FailedPrepareDynamicResources
  != FailedCreatePodSandBox
  != CNI error
```

DRA 准备失败会在调用 `createPodSandbox` 之前返回。本课只划清边界，DRA 设备细节留到后续设备专项。

### 7.2 `createPodSandbox` 本身包含四步

```text
pkg/kubelet/kuberuntime/kuberuntime_sandbox.go:37-74
```

```go
podSandboxConfig, err :=
    m.generatePodSandboxConfig(ctx, pod, attempt)

err = m.osInterface.MkdirAll(
    podSandboxConfig.LogDirectory,
    0755,
)

runtimeHandler, err =
    m.runtimeClassManager.LookupRuntimeHandler(
        pod.Spec.RuntimeClassName,
    )

podSandboxID, err =
    m.runtimeService.RunPodSandbox(
        ctx,
        podSandboxConfig,
        runtimeHandler,
    )
```

任何一步返回错误，都会回到外层。若 Pod 已请求终止，当前源码会把这次 sandbox 创建错误当作删除并发结果直接返回，不记录 `FailedCreatePodSandBox`；否则才进入下面的 Event 分支：

```go
m.recorder.Eventf(
    ref,
    v1.EventTypeWarning,
    events.FailedCreatePodSandBox,
    "Failed to create pod sandbox: %v",
    err,
)
```

所以 `FailedCreatePodSandBox` 是一个“sandbox 创建总入口失败”的 Event reason，不是 CNI 专属 reason。

| message 线索 | 更可能的断点 |
|---|---|
| DNS/hostname/security/sysctl/config 生成错误 | `generatePodSandboxConfig` |
| 创建 Pod log directory 失败 | `MkdirAll` / 节点文件系统 |
| RuntimeClass 找不到或 handler 解析失败 | `LookupRuntimeHandler`，尚未发 CRI |
| unknown runtime handler、timeout、network setup、runtime internal error | `RunPodSandbox` 或 runtime 内部 |
| `plugin type=... failed (add)`、IPAM 等 | runtime 的单 Pod 网络 setup/CNI 路径 |

### 7.3 成功没有对称的 Normal Event

当前成功侧是 V(4) 日志：

```go
logger.V(4).Info(
    "Created PodSandbox for pod",
    "podSandboxID", podSandboxID,
    "pod", klog.KObj(pod),
)
```

源码没有为它记录一个与 `FailedCreatePodSandBox` 对称的 Normal `CreatedPodSandbox` Event。因此生产取证不要“等一条成功 Event”：

```text
成功证据组合：
  PodReadyToStartContainers condition（能力开启且已传播）
  + crictl sandbox READY/IP（目标 Node）
  + kubelet V(4) Created PodSandbox（若已有日志）
  + runtime metrics
```

### 7.4 RunPodSandbox 成功后，查询 sandbox status 仍可能失败

`RunPodSandbox` 返回 ID 后，kubelet马上调用 `PodSandboxStatus`。如果这一步失败：

```text
kuberuntime_manager.go:1624-1633

RunPodSandbox 成功
  -> PodSandboxStatus 失败
  -> Event reason=FailedPodSandBoxStatus
  -> 本轮 result.Fail 并返回
  -> 不进入 Pod IP、OnPodSandboxReady 和业务容器启动
```

此时 runtime 中可能已经有 sandbox 记录，不能因为业务 container 没有创建就反推 `RunPodSandbox` 一定失败。应按 sandbox ID 查 runtime 状态，并区分“创建失败”与“创建后状态查询失败”。

---

## 8. `PodSandboxConfig`：Pod YAML 怎样变成 CRI 参数

```text
pkg/kubelet/kuberuntime/kuberuntime_sandbox.go:77-156
```

核心 struct literal：

```go
podSandboxConfig := &runtimeapi.PodSandboxConfig{
    Metadata: &runtimeapi.PodSandboxMetadata{
        Name:      pod.Name,
        Namespace: pod.Namespace,
        Uid:       string(pod.UID),
        Attempt:   attempt,
    },
    Labels:      newPodLabels(pod),
    Annotations: newPodAnnotations(pod),
}
```

Go 现场补——`&Type{...}`：

- `Type{...}` 创建结构体值；
- 前面的 `&` 取地址，得到 `*Type` 指针；
- 内层 `Metadata: &runtimeapi.PodSandboxMetadata{...}` 是嵌套指针字段，不是两份独立 YAML。

后续继续填：

```text
GetPodDNS
GeneratePodHostNameAndDomain
BuildPodLogsDirectory
MakePortMappings
generatePodSandboxLinuxConfig
applySandboxResources
```

Linux config 继续承载：

- cgroup parent；
- Pod 级 namespace options；
- privileged 汇总；
- seccomp/security context；
- sysctl；
- supplemental groups；
- overhead / sandbox 资源。

### 8.1 为什么 attempt 不是 container restartCount

`PodSandboxMetadata.Attempt` 表示 sandbox 重建次数；业务容器 `ContainerMetadata.Attempt`/日志 restart count 是另一条 container 级计数。生产中看到 sandbox ID 变了，不能只看 app container 的 `restartCount` 推断原因。

### 8.2 labels/annotations 不是让 runtime 重新理解完整 Pod

kubelet把必要的身份与实现信息编码进 CRI config。runtime 接收的是 CRI protobuf config，不是直接拿 API `v1.Pod` 做 Kubernetes 控制面逻辑。

---

## 9. RuntimeClass 是可选 runtime 选择，不是 GPU 必填项

```go
runtimeHandler := ""
if m.runtimeClassManager != nil {
    runtimeHandler, err =
        m.runtimeClassManager.LookupRuntimeHandler(
            pod.Spec.RuntimeClassName,
        )
}
```

分三种：

| Pod / 集群状态 | handler | 结果 |
|---|---|---|
| Pod 未指定 RuntimeClass | 通常为空字符串 | runtime 使用默认 handler |
| 指定的 RuntimeClass 对象不存在，默认 RuntimeClass admission 开启 | 无 handler | apiserver通常以 Forbidden 拒绝 Pod 创建，根本不到 kubelet |
| 非标准集群禁用/绕过该 admission，缺失对象仍到 kubelet | lookup error | 在 CRI 调用前失败，外层可记录 `FailedCreatePodSandBox` |
| RuntimeClass 存在，但 handler 未在 runtime 配置 | handler 传给 CRI | runtime 的 `RunPodSandbox` 通常返回 unknown handler 类错误 |

GPU Pod 不天然要求 RuntimeClass。很多生产 GPU Pod 使用默认 runc handler，只靠 NVIDIA Container Toolkit/CDI 和 device injection；另一些平台才用 RuntimeClass 选择特定 runtime。必须以你们集群的 containerd/CRI 配置为准。

默认 admission 下仍保留 kubelet lookup 分支，是为了处理 RuntimeClass 在 Pod admission 后被删除、informer 暂时滞后或非标准集群关闭该 admission 等竞态/配置；不能把它写成正常创建一个不存在 RuntimeClass 的首要失败点。

---

## 10. `m.runtimeService` 后面还有两层，不能直接画成 containerd

源码调用看起来是：

```go
m.runtimeService.RunPodSandbox(...)
```

实际主线：

```text
kubeGenericRuntimeManager
  -> instrumentedRuntimeService
  -> remoteRuntimeService
  -> runtimeapi.RuntimeServiceClient
  -> CRI v1 gRPC
  -> containerd CRI plugin / CRI-O / 其他实现
```

### 10.1 指标包装层

```text
pkg/kubelet/kuberuntime/instrumented_services.go:180-191
```

```go
func (in instrumentedRuntimeService) RunPodSandbox(
    ctx context.Context,
    config *runtimeapi.PodSandboxConfig,
    runtimeHandler string,
) (string, error) {
    startTime := time.Now()
    defer recordOperation("run_podsandbox", startTime)
    defer metrics.RunPodSandboxDuration.
        ObserveSince(startTime, runtimeHandler)()
    out, err :=
        in.service.RunPodSandbox(ctx, config, runtimeHandler)
    recordError("run_podsandbox", err)
    if err != nil {
        metrics.RunPodSandboxErrors.
            WithLabelValues(runtimeHandler).
            Inc()
    }
    return out, err
}
```

这层的职责是观测和转发，不负责创建 Linux namespace。它既记录通用 `runtime_operations_*`，也记录按 `runtime_handler` 拆分的 `run_podsandbox_*`。

Go 现场补——interface wrapper：`instrumentedRuntimeService` 与底层 remote service 都满足同一个 RuntimeService 接口，所以包装层可以先记指标再转调 `in.service`；上层不需要知道最终实现是否 containerd。

### 10.2 remote CRI client

```text
staging/src/k8s.io/cri-client/pkg/remote_runtime.go:218-252
```

```go
timeout := r.timeout * 2
ctx, cancel := context.WithTimeout(ctx, timeout)
defer cancel()

resp, err := r.runtimeClient.RunPodSandbox(
    ctx,
    &runtimeapi.RunPodSandboxRequest{
        Config:         config,
        RuntimeHandler: runtimeHandler,
    },
)
```

Go 现场补——`defer cancel()`：先派生一个有截止时间的 context，函数退出时无论成功或失败都调用 `cancel`，释放 timer 等资源。`defer recordOperation(...)` 同理，保证每条返回路径都记耗时。

CRI 对 `RunPodSandbox` 的合同是：

```text
创建并启动 Pod 级 sandbox；
成功返回时 runtime 应确保 sandbox 处于 ready state。
```

没有额外的 `StartPodSandbox` RPC。不要把 container 的 `CreateContainer -> StartContainer` 两段式机械套到 sandbox。

当前 remote client 给 sandbox 操作使用 `2 * r.timeout`，源码注释的默认约 4 分钟；Create/Start container 使用 `r.timeout`，默认约 2 分钟。生产发行版、参数和包装层可能不同，排障时应看目标版本与配置，不能拿这个默认值当 SLA。

---

## 11. kubelet不直接调用 CNI

当前核心仓库主线是：

```text
kubelet
  -> CRI RunPodSandbox
  -> runtime implementation
  -> runtime 根据实现/网络模式调用 CNI 或其他网络机制
```

不是：

```text
kubelet -> libcni.AddNetworkList -> containerd
```

所以从 kubelet源码追到 `remoteRuntimeService.RunPodSandbox` 后，已经到 Kubernetes 核心仓库的实现边界。若 message 明确指向 CNI，下一跳应查：

1. 目标 Node 的 runtime 日志；
2. CNI plugin/agent 日志；
3. `/etc/cni/net.d` 等目标发行版配置；
4. IPAM、网桥、路由、iptables/eBPF、网络设备；
5. runtime 的 sandbox 状态与 network namespace。

但 runtime 是否、何时、怎样调用 CNI，取决于具体实现。hostNetwork、虚拟机 sandbox 或其他 runtime 不应被强行画成同一条 CNI 细节。

---

## 12. sandbox 创建成功后，先查状态和 IP，再拉业务镜像

外层成功后：

```text
pkg/kubelet/kuberuntime/kuberuntime_manager.go:1622-1657
```

```go
resp, err :=
    m.runtimeService.PodSandboxStatus(
        ctx,
        podSandboxID,
        false,
    )

if !kubecontainer.IsHostNetworkPod(pod) {
    podIPs = m.determinePodSandboxIPs(
        ctx,
        pod.Namespace,
        pod.Name,
        resp.GetStatus(),
    )
}

if err := m.runtimeHelper.OnPodSandboxReady(ctx, pod); err != nil {
    logger.Error(err, "Failed to invoke sandbox ready callback, continuing")
}
```

Go 现场补——`if err := call(); err != nil`：分号前先调用并声明局部 `err`，分号后判断；这个 `err` 只在当前 `if/else` 内可见。

时序：

```text
RunPodSandbox 成功
  -> PodSandboxStatus
  -> 非 hostNetwork 解析 Pod IP
  -> OnPodSandboxReady
  -> 后面才进入 image pull / container create
```

### 12.1 `PodReadyToStartContainers=True` 的精确含义

当前 Kubelet callback：

```text
pkg/kubelet/kubelet.go:3541-3578
```

在 feature gate 开启时，它异步把 condition 置为 True：

```go
go func() {
    existingStatus, ok :=
        kl.statusManager.GetPodStatus(pod.UID)
    if !ok {
        existingStatus = pod.Status
    }

    cachedStatus := existingStatus.DeepCopy()
    readySandboxCondition := v1.PodCondition{
        Type:   v1.PodReadyToStartContainers,
        Status: v1.ConditionTrue,
    }
    cachedStatus.Conditions =
        ReplaceOrAppendPodCondition(
            cachedStatus.Conditions,
            &readySandboxCondition,
        )
    kl.statusManager.SetPodStatus(logger, pod, *cachedStatus)
}()
```

在“本轮刚创建新 sandbox”这条窄路径里，callback 位于外层 volume 等待成功之后、业务镜像拉取之前，所以该调用时点同时满足 sandbox、network、volume 三项前置。

但 API 中这个 condition 还有一条更通用的生成路径，不能漏掉：

```text
pkg/kubelet/status/generate.go:270-287
pkg/kubelet/kubelet_pods.go:1984-1987
```

```go
func GeneratePodReadyToStartContainersCondition(
    pod *v1.Pod,
    oldPodStatus *v1.PodStatus,
    podStatus *kubecontainer.PodStatus,
) v1.PodCondition {
    newSandboxNeeded, _, _ :=
        runtimeutil.PodSandboxChanged(pod, podStatus)
    if !newSandboxNeeded {
        return v1.PodCondition{
            Type:   v1.PodReadyToStartContainers,
            Status: v1.ConditionTrue,
        }
    }
    return v1.PodCondition{
        Type:   v1.PodReadyToStartContainers,
        Status: v1.ConditionFalse,
    }
}
```

每轮生成 API PodStatus 时都会用 runtime PodStatus 再计算它。因此现场只看到 `True`，最稳妥的解释是：

```text
kubelet按 PodSandboxChanged 的规则判断：
  当前不需要新建 sandbox；
  已有可复用的 READY sandbox；
  network namespace 模式与非 hostNetwork Pod 的 IP 条件满足继续启动 container。

单靠这个 condition 不能证明：
  所有 volume 此刻都已挂载
  image 已拉取
  CreateContainer / StartContainer 已成功
  Java 已监听端口
  readiness probe 已通过
```

只有再结合“刚执行 OnPodSandboxReady 的同一轮 trace/日志”时，才能把该窄时间点的 volume 前置也并入证据。还要注意：

- feature gate 未启用时，这个 condition 可能不存在；
- callback 是异步 status 更新，现场可能有短暂时间差；
- callback 更新失败会记录日志，但当前 runtime SyncPod 仍继续创建容器；
- condition 缺失不能单独证明 sandbox 没成功，应与 runtime 状态组合。

---

## 13. 为什么 `generatePodSandboxConfig` 会出现两次

第一次在：

```text
createPodSandbox
  -> generatePodSandboxConfig
  -> RunPodSandbox
```

第二次在 sandbox 已准备后：

```text
kuberuntime_manager.go:1667-1673
  -> generatePodSandboxConfig
  -> 后续 startContainer/CreateContainer 继续把它作为 sandbox context 传给 CRI
```

这不是“创建了两个 sandbox”。两个用途是：

1. 第一次生成 config 给 `RunPodSandbox`；
2. 第二次为本轮后续 container 创建准备当前 sandbox config。

画调用链时漏掉第一次，会误以为 sandbox 在没有 config 的情况下创建；把第二次当成再次 `RunPodSandbox`，又会误以为每个 container 都建一个 sandbox。

---

## 14. start helper 先处理 CrashLoopBackOff，再进单容器流水线

```text
pkg/kubelet/kuberuntime/kuberuntime_manager.go:1682-1741
```

runtime manager 定义了局部函数：

```go
start := func(
    ctx context.Context,
    typeName, metricLabel string,
    spec *startSpec,
) error {
    startContainerResult :=
        kubecontainer.NewSyncResult(
            kubecontainer.StartContainer,
            spec.container.Name,
        )
    result.AddSyncResult(startContainerResult)

    isInBackOff, msg, err :=
        m.doBackOff(
            ctx,
            pod,
            spec.container,
            podStatus,
            backOff,
        )
    if isInBackOff {
        startContainerResult.Fail(err, msg)
        return err
    }

    msg, err = m.startContainer(...)
    if err != nil {
        startContainerResult.Fail(err, msg)
        return err
    }
    return nil
}
```

Go 现场补——局部 closure：`start` 是函数内部定义的函数值，它会捕获外层 `pod、podStatus、result、backOff`，让 ephemeral/init/regular container 复用同一段 backoff、调用和结果记账逻辑。

这解释了 `CrashLoopBackOff` 的位置：

```text
不是 apiserver拒绝创建；
不是 scheduler不调度；
不是 sandbox必然坏了；
而是某个 container 已有失败历史，本轮在真正再次启动前被 backoff 门挡住。
```

---

## 15. 单个 Java container 的真实顺序

```text
pkg/kubelet/kuberuntime/kuberuntime_container.go:193-338
```

源码注释的四步：

```text
1. pull image
2. create container
3. start container
4. run PostStart hook
```

展开后是：

```text
getPodRuntimeHandler
  -> EnsureImageExists
  -> 计算 restartCount
  -> generateContainerConfig
  -> setActuatedContainerResources
  -> internalLifecycle.PreCreateContainer
  -> CRI CreateContainer
  -> internalLifecycle.PreStartContainer
  -> Event Created
  -> CRI StartContainer
  -> Event Started
  -> legacy log symlink
  -> PostStart hook
```

### 15.1 image pull 在 `CreateContainer` 之前

```go
imageRef, msg, err :=
    m.imagePuller.EnsureImageExists(
        ctx,
        ref,
        pod,
        container.Image,
        pullSecrets,
        podSandboxConfig,
        podRuntimeHandler,
        container.ImagePullPolicy,
    )
if err != nil {
    return msg, err
}
```

所以 `ImagePullBackOff` 时：

- PodSandbox 可能 READY；
- Pod IP 可能已经有了；
- `PodReadyToStartContainers` 可能 True；
- 业务 container ID 仍可能不存在；
- Java 日志当然也不存在。

#### 15.1.1 image pull 走独立 CRI ImageService

`EnsureImageExists` 后面不是前文的 RuntimeService `PullImage` 方法；当前 kubelet单独装配 ImageService：

```text
pkg/kubelet/kubelet.go:403-415

ContainerRuntimeEndpoint
  -> RemoteRuntimeService

ImageServiceEndpoint
  -> RemoteImageService
  -> 若未单独配置，才回退复用 ContainerRuntimeEndpoint
```

完整拉取主线：

```text
pkg/kubelet/images/image_manager.go:164-281
  EnsureImageExists
  -> precheck / credential lookup / image backoff
  -> image puller（parallel 或 serial）

pkg/kubelet/kuberuntime/kuberuntime_image.go:31-约74
  -> kubeGenericRuntimeManager.PullImage

pkg/kubelet/kuberuntime/instrumented_services.go:311-318
  -> instrumentedImageManagerService.PullImage
  -> operation_type="pull_image"

staging/src/k8s.io/cri-client/pkg/remote_image.go:234-约270
  -> remoteImageService.PullImage
  -> runtimeapi.ImageServiceClient.PullImage
  -> CRI ImageService
```

因此排 `ImagePullBackOff` 时，要核对目标 kubelet的 image service endpoint、registry/DNS/TLS/auth 与 ImageService 日志；不能只验证 RuntimeService 的 `RunPodSandbox/CreateContainer` endpoint 可用就结束。很多集群两个 endpoint 指向同一 socket，但这是配置结果，不是接口上天然只有一套 service。

### 15.2 `ContainerConfig` 才承载业务容器细节

`generateContainerConfig` 会继续调用：

```go
opts, cleanupAction, err :=
    m.runtimeHelper.GenerateRunContainerOptions(
        ctx,
        pod,
        container,
        podIP,
        podIPs,
        imageVolumes,
    )
```

随后组装 command/args、env、mount、devices/CDI devices、labels、annotations、log path、Linux resources/security 等。这里是后续 GPU device injection 最终落进 CRI container 参数的重要汇合点，但设备如何被选中留到第 15～17 课。

### 15.3 Create 与 Start 是两次 CRI

```go
containerID, err :=
    m.runtimeService.CreateContainer(
        ctx,
        podSandboxID,
        containerConfig,
        podSandboxConfig,
    )

err = m.internalLifecycle.PreStartContainer(
    logger,
    pod,
    container,
    containerID,
)

m.recordContainerEvent(..., Normal, CreatedContainer, "Container created")

err = m.runtimeService.StartContainer(ctx, containerID)

m.recordContainerEvent(..., Normal, StartedContainer, "Container started")
```

对应 remote CRI：

```text
staging/src/k8s.io/cri-client/pkg/remote_runtime.go
  -> CreateContainer:393-422
  -> StartContainer:425-440
```

`CreateContainer` 成功拿到 container ID，不等于进程已运行；`StartContainer` 成功返回，才越过 runtime 启动门。

### 15.4 PostStart 失败发生在 Started 之后

当前顺序：

```text
StartContainer 成功
  -> Event Started
  -> 执行 PostStart
  -> PostStart 失败
  -> Event FailedPostStartHook
  -> kubelet kill container
```

所以 PostStart 失败不能写成“CRI StartContainer 失败”。它甚至可能先出现 `Started`，随后立刻被 kill。

---

## 16. Event reason、Waiting reason、日志要分三列

源码常量名很容易误导。当前：

```text
pkg/kubelet/events/event.go:21-24

FailedToCreateContainer = "Failed"
FailedToStartContainer  = "Failed"
```

也就是说 Go 常量叫 `FailedToCreateContainer`，用户在 Kubernetes Event 的 REASON 列通常看到的却是字面值 `Failed`。

| 断点 | Event reason 常见值 | container waiting / result code | 主要靠什么区分 |
|---|---|---|---|
| image pull 初次失败 | `Failed`，另有 Pulling/BackOff 等 image Event | `ErrImagePull` | message、container state、image manager Event |
| image pull 退避 | BackOff 类 Event | `ImagePullBackOff` | Waiting reason + message |
| 生成 container config 失败 | `Failed` | `CreateContainerConfigError` | message |
| CRI CreateContainer 失败 | `Failed` | `CreateContainerError` | message + runtime log |
| internal PreStart 失败 | `Failed` | `PreStartHookError` | message + kubelet log |
| CRI StartContainer 失败 | `Failed` | `RunContainerError` | message + runtime log |
| PostStart 失败 | `FailedPostStartHook` | `PostStartHookError` | hook Event + kubelet log；容器会被 kill |

生产中不要写：

```text
“Event reason 是 FailedToStartContainer”
```

除非目标版本真的把 value 改成了这个字符串。当前提交更准确的记录方式是：

```text
Event reason=Failed
message=Error: ...
container waiting reason=RunContainerError
目标 Node runtime 日志=...
```

---

## 17. 把 Java Pod 首次创建完整走一遍

```text
t0  第 11 课已完成
    -> spec.nodeName=worker-05
    -> podWorkerLoop
    -> Kubelet.SyncPod

t1  外层网络门
    -> runtimeState.NetworkReady
    -> 非 hostNetwork 且 false：NetworkNotReady，停止本轮

t2  kl.containerRuntime.SyncPod
    -> computePodActions
    -> 新 Pod 无 sandbox：KillPod/CreateSandbox=true，attempt=0

t3  createPodSandbox
    -> PodSandboxConfig
    -> log directory
    -> RuntimeClass handler
    -> CRI RunPodSandbox

t4  runtime 实现
    -> 建 Pod 级 sandbox
    -> 典型非 hostNetwork 路径完成网络 setup
    -> 返回 READY sandbox ID

t5  kubelet
    -> PodSandboxStatus
    -> 提取 Pod IP
    -> OnPodSandboxReady

t6  start(game-api)
    -> 不在 CrashLoopBackOff
    -> EnsureImageExists
    -> generateContainerConfig
    -> PreCreate

t7  CRI CreateContainer
    -> 返回 container ID
    -> Event Created

t8  CRI StartContainer
    -> 业务进程启动
    -> Event Started

t9  PostStart
    -> 成功：本轮 container start 完成
    -> 失败：Event FailedPostStartHook，kill container

t10 下一轮 runtime status / PLEG / probe / status
    -> Java Running、restartCount、Ready
    -> 第 13 课
```

---

## 18. 生产排障：用“四层证据”定位到具体 RPC 之前或之后

### 18.1 第一层：API 对象与 UID

```powershell
$ns = 'game'
$pod = 'game-api-new-x'
$uid = kubectl get pod $pod -n $ns -o jsonpath='{.metadata.uid}'
$node = kubectl get pod $pod -n $ns -o jsonpath='{.spec.nodeName}'

kubectl get pod $pod -n $ns -o jsonpath='{.metadata.uid}{"\n"}{.spec.nodeName}{"\n"}{.status.phase}{"\n"}{.status.podIP}{"\n"}{range .status.conditions[*]}{.type}{"="}{.status}{" reason="}{.reason}{"\n"}{end}{range .status.containerStatuses[*]}{.name}{" id="}{.containerID}{" waiting="}{.state.waiting.reason}{" restart="}{.restartCount}{"\n"}{end}'

kubectl get events -n $ns `
  --field-selector "involvedObject.uid=$uid" `
  --sort-by=.metadata.creationTimestamp
```

先回答：

1. `spec.nodeName` 是否已有值？
2. Event source/reporting component 是否为目标 kubelet？
3. `PodReadyToStartContainers` 是否存在、是什么值？
4. `podIP` 是否已有值？
5. `containerID` 是否已有值？
6. waiting reason 是 image、config、create、start 还是 backoff？

完整 Pod YAML、Event message、Node 描述和 runtime inspect 可能暴露业务 annotations、明文 env、私有镜像仓库、内部 IP、mount/path 和其他工作负载信息。只在受控终端采集；外发前必须脱敏。不要为了排 imagePullSecret 直接把 Secret YAML 贴进工单。

### 18.2 第二层：Node 的 runtime/network 总状态

```powershell
kubectl get node $node
kubectl get node $node -o jsonpath='{range .status.conditions[*]}{.type}{"="}{.status}{" reason="}{.reason}{" message="}{.message}{"\n"}{end}'
kubectl get lease $node -n kube-node-lease -o jsonpath='{.spec.renewTime}{"\n"}'
```

看 `Ready`、`NetworkUnavailable`、压力 condition 和 Lease 时间，但不要把 Node Ready=True 当成“每个 RunPodSandbox 都必成功”。Node condition 是聚合状态，单 Pod 网络、RuntimeClass、IPAM 仍可能失败。

### 18.3 第三层：目标 Node kubelet/runtime/CNI 日志

systemd 发行版示例：

```bash
journalctl -u kubelet --since "20 min ago" --no-pager \
  | grep -E '<POD_UID>|game-api-new-x|NetworkNotReady|Creating PodSandbox|Created PodSandbox|CreatePodSandbox|CreateContainer|StartContainer'

journalctl -u containerd --since "20 min ago" --no-pager \
  | grep -E '<POD_UID>|game-api-new-x|RunPodSandbox|CreateContainer|StartContainer|cni|network'
```

服务名、日志位置和 verbosity 因发行版而异；有的运行时由专用日志平台收集。生产优先使用现有日志系统，不为一次排障重启服务或长期提高 verbosity。

解释日志时按方向：

```text
kubelet只有 NetworkNotReady
  -> 还没进入 runtime manager 的 RunPodSandbox

kubelet记录 Creating PodSandbox，runtime没有对应请求
  -> 这行日志还在 DRA prepare、sandbox config、logdir、RuntimeClass lookup 之前
  -> 先排这些 CRI 前失败域，再查 kubelet CRI client、endpoint、timeout、日志时间窗

runtime收到 RunPodSandbox，CNI/IPAM 报错
  -> 进入 runtime/CNI 责任域

sandbox READY，但没有 Pulling/Created
  -> 查 action、backoff、image config、独立 CRI ImageService 与 Kubelet下一轮

Created 有、Started 无
  -> Normal Created 记录在 internal PreStart 成功之后
  -> 查 CRI StartContainer 与 runtime

Started 有、Ready=False
  -> 第 13 课 probe/status
```

### 18.4 第四层：目标 Node 的 CRI 事实

只在有节点权限、明确 runtime endpoint 的目标 Node 执行。不要在控制面随便运行 `crictl` 后把“查不到”当结论。

```bash
sudo crictl info
sudo crictl pods --label io.kubernetes.pod.uid=<POD_UID>
# 若 crictl 版本不支持 label filter，先按 name/namespace 列候选，再逐个 inspectp 核对 UID label。
sudo crictl inspectp <SANDBOX_ID>
sudo crictl ps -a --pod <SANDBOX_ID>
sudo crictl inspect <CONTAINER_ID>
```

关联字段：

| CRI 事实 | 说明 |
|---|---|
| 没有该 UID/name 的 sandbox | 尚未成功 RunPodSandbox，或已被清理 |
| sandbox `NOTREADY` | runtime 创建了记录，但 Pod 级环境不可用 |
| sandbox `READY` 且有 IP | 已越过 RunPodSandbox；继续看业务 container |
| READY sandbox 下没有 app container | 常见于 image/config/Create 前失败 |
| app container `CREATED` | 已越过 CreateContainer，尚未成功 Start 或尚未刷新 |
| app container `RUNNING` | runtime 看到进程运行；Ready 仍由 probe/status 决定 |
| app container `EXITED` | 进入重启/退出原因与 PLEG 链 |

Deployment 重建或 sandbox 重建后可能残留同名记录，所以 name/namespace 只能找候选，`io.kubernetes.pod.uid` 才能与当前 API Pod 身份对齐。`inspectp/inspect` 输出也可能含内部 annotation、路径、参数、环境和网络信息，外发前同样脱敏。

### 18.5 一张责任域决策表

| API / Node / CRI 组合 | 最可能断点 | 首查 |
|---|---|---|
| `NetworkNotReady`，无 sandbox | 外层全局网络门 | runtime Status、CNI 初始化、Node runtime 日志 |
| `FailedCreatePodSandBox`，无 sandbox | config/logdir/RuntimeClass/CRI/single-Pod network | 完整 Event message + kubelet/runtime 日志 |
| sandbox 可能存在，`FailedPodSandBoxStatus` | 创建后查询 sandbox status 失败 | sandbox ID、RuntimeService 状态查询与 runtime 日志 |
| sandbox READY，`ImagePullBackOff`，无 app container | CRI ImageService/image pull | image endpoint、image 名、registry DNS/TLS/auth、节点出口 |
| sandbox READY，Event `Failed`，Waiting `CreateContainerConfigError` | kubelet生成 container config | env/Secret/ConfigMap/mount/device/security message |
| CRI 中已有 app container，但无 Normal `Created`，Waiting `PreStartHookError` | internal PreStart | kubelet内部 lifecycle/device/resource hook 日志 |
| Normal `Created` 已有，Waiting `RunContainerError` | CRI StartContainer | kubelet + runtime log |
| app container RUNNING，Ready=False | probe/status | 第 13 课 |
| app container EXITED、restartCount 增长 | 进程退出/kill/restart | lastState、previous log、PLEG、probe |

---

## 19. 相关指标：能看节点趋势，不能替代单 Pod 证据

当前源码注册的主要指标：

```promql
rate(kubelet_runtime_operations_total{
  operation_type=~"run_podsandbox|pull_image|create_container|start_container"
}[5m])

rate(kubelet_runtime_operations_errors_total{
  operation_type=~"run_podsandbox|pull_image|create_container|start_container"
}[5m])

histogram_quantile(
  0.99,
  sum by (le, operation_type) (
    rate(kubelet_runtime_operations_duration_seconds_bucket{
      operation_type=~"run_podsandbox|pull_image|create_container|start_container"
    }[5m])
  )
)

rate(kubelet_run_podsandbox_errors_total[5m])

histogram_quantile(
  0.99,
  sum by (le, runtime_handler) (
    rate(kubelet_run_podsandbox_duration_seconds_bucket[5m])
  )
)

rate(kubelet_started_pods_errors_total[5m])
rate(kubelet_started_containers_errors_total[5m])
```

解释：

- `runtime_operations_*` 来自 instrumented RuntimeService 与 ImageManagerService，按 operation_type 聚合；
- `run_podsandbox_*` 额外按 RuntimeClass handler 聚合；
- `started_pods_*` 统计 sandbox 启动尝试/错误；
- `started_containers_errors_total` 按 container type 与 error code 聚合。

这些是 Node 级聚合指标，没有 namespace/pod UID label。看到错误率上升，可判断节点或 handler 趋势；要定位 `game-api-new-x`，仍需 UID、Event、日志和 CRI 状态。

这些指标中有 Alpha 稳定级别，目标发行版可能隐藏、重命名或不暴露。先检查实际 `/metrics`，不要因为查询为空就断言 runtime 没调用。

---

## 20. 安全实验一：同节点对照 sandbox 成功与 image pull 失败

目标不是“学 kubectl”，而是验证这条源码边界：

```text
image-bad 能出现 ErrImagePull/ImagePullBackOff
  -> 已经越过 RunPodSandbox
  -> 仍停在 CreateContainer 之前
```

只在可销毁测试集群执行。先像第 11 课一样列 Node，人工选择一个 Ready、未 cordon、无阻塞 `NoSchedule/NoExecute` taint 的实验节点：

```powershell
$nodes = kubectl get nodes -o json | ConvertFrom-Json
$nodes.items | ForEach-Object {
  $ready = ($_.status.conditions | Where-Object { $_.type -eq 'Ready' }).status
  $taints = @($_.spec.taints | ForEach-Object { "$($_.key):$($_.effect)" }) -join ','
  [pscustomobject]@{
    Name          = $_.metadata.name
    Ready         = $ready
    Unschedulable = [bool]$_.spec.unschedulable
    HostnameLabel = $_.metadata.labels.'kubernetes.io/hostname'
    Taints        = $taints
  }
} | Format-Table -AutoSize

$worker = '请替换为允许实验的节点名'
```

完整实验：

```powershell
$context = kubectl config current-context
Write-Host "current-context = $context"
if ((Read-Host '确认是可销毁测试集群；输入 LAB12 继续') -ne 'LAB12') {
  throw '用户取消实验'
}
if ($worker -eq '请替换为允许实验的节点名') {
  throw '请先显式填写 $worker'
}

$nodeObj = kubectl get node $worker -o json | ConvertFrom-Json
$ready = ($nodeObj.status.conditions | Where-Object { $_.type -eq 'Ready' }).status
$blockingTaints = @($nodeObj.spec.taints | Where-Object {
  $_.effect -in @('NoSchedule', 'NoExecute')
})
$activePressures = @($nodeObj.status.conditions | Where-Object {
  $_.type -in @('MemoryPressure', 'DiskPressure', 'PIDPressure') -and
  $_.status -eq 'True'
})
$hostname = $nodeObj.metadata.labels.'kubernetes.io/hostname'
if ($ready -ne 'True') { throw "Node $worker 不是 Ready" }
if ([bool]$nodeObj.spec.unschedulable) { throw "Node $worker 已 cordon" }
if ($blockingTaints.Count -gt 0) { throw "Node $worker 有阻塞 taint" }
if ($activePressures.Count -gt 0) { throw "Node $worker 存在压力 condition" }
if ([string]::IsNullOrWhiteSpace($hostname)) { throw '缺少 hostname label' }

$stamp = Get-Date -Format 'yyyyMMddHHmmss'
$ns = "runtime-lab-12-$stamp"
$owner = "chapter12-$stamp"
$createdNamespace = $false

try {
  kubectl create namespace $ns
  if ($LASTEXITCODE -ne 0) { throw '创建 namespace 失败' }
  $createdNamespace = $true
  kubectl label namespace $ns "studyowner=$owner"
  if ($LASTEXITCODE -ne 0) { throw '设置 owner label 失败' }

  $manifest = @'
apiVersion: v1
kind: Pod
metadata:
  name: runtime-ok
  namespace: __NAMESPACE__
spec:
  nodeSelector:
    kubernetes.io/hostname: __HOSTNAME__
  containers:
  - name: app
    image: registry.k8s.io/pause:3.10
    resources:
      requests:
        cpu: 10m
        memory: 16Mi
---
apiVersion: v1
kind: Pod
metadata:
  name: image-bad
  namespace: __NAMESPACE__
spec:
  nodeSelector:
    kubernetes.io/hostname: __HOSTNAME__
  containers:
  - name: game-api
    image: registry.k8s.io/does-not-exist/chapter12:never
    imagePullPolicy: Always
    resources:
      requests:
        cpu: 10m
        memory: 16Mi
'@

  $manifest.Replace('__NAMESPACE__', $ns).Replace('__HOSTNAME__', $hostname) |
    kubectl create -f -
  if ($LASTEXITCODE -ne 0) { throw '创建 Pod 失败' }

  $okUID = kubectl get pod runtime-ok -n $ns -o jsonpath='{.metadata.uid}'
  $badUID = kubectl get pod image-bad -n $ns -o jsonpath='{.metadata.uid}'

  kubectl wait --for=condition=Ready pod/runtime-ok -n $ns --timeout=180s
  if ($LASTEXITCODE -ne 0) { throw '对照 Pod 未 Ready，实验环境本身有问题' }

  $deadline = (Get-Date).AddSeconds(180)
  do {
    $badReason = kubectl get pod image-bad -n $ns `
      -o jsonpath='{.status.containerStatuses[0].state.waiting.reason}' 2>$null
    if ($badReason -in @('ErrImagePull', 'ImagePullBackOff')) { break }
    Start-Sleep -Seconds 2
  } while ((Get-Date) -lt $deadline)
  if ($badReason -notin @('ErrImagePull', 'ImagePullBackOff')) {
    throw "未观察到镜像失败，实际 waiting reason=$badReason"
  }

  $okNode = kubectl get pod runtime-ok -n $ns -o jsonpath='{.spec.nodeName}'
  $badNode = kubectl get pod image-bad -n $ns -o jsonpath='{.spec.nodeName}'
  if ($okNode -ne $worker -or $badNode -ne $worker) {
    throw "Pod 未在目标 Node：ok=$okNode bad=$badNode"
  }

  $imageFailureEvents = @()
  $eventDeadline = (Get-Date).AddSeconds(60)
  do {
    $eventJson = kubectl get events -n $ns `
      --field-selector "involvedObject.uid=$badUID" -o json 2>$null
    if ($LASTEXITCODE -eq 0 -and $eventJson) {
      $eventList = $eventJson | ConvertFrom-Json
      $imageFailureEvents = @($eventList.items | Where-Object {
        $_.reason -in @('Failed', 'BackOff') -and
        $_.message -match '(?i)image|pull' -and
        (
          $_.source.host -eq $badNode -or
          $_.reportingInstance -eq $badNode
        )
      })
    }
    if ($imageFailureEvents.Count -gt 0) { break }
    Start-Sleep -Seconds 2
  } while ((Get-Date) -lt $eventDeadline)
  if ($imageFailureEvents.Count -lt 1) {
    throw '没有该 UID 且来自目标 Node 的镜像 Failed/BackOff Event，证据链未闭合'
  }

  kubectl get pod -n $ns -o wide
  kubectl get pod image-bad -n $ns -o jsonpath='{.metadata.uid}{"\n"}{.spec.nodeName}{"\n"}{.status.podIP}{"\n"}{range .status.conditions[*]}{.type}{"="}{.status}{"\n"}{end}{range .status.containerStatuses[*]}{.name}{" id="}{.containerID}{" waiting="}{.state.waiting.reason}{" message="}{.state.waiting.message}{"\n"}{end}'
  kubectl get events -n $ns --field-selector "involvedObject.uid=$badUID" `
    -o custom-columns='TYPE:.type,REASON:.reason,MESSAGE:.message'

  Write-Host "PASS: image-bad UID=$badUID，Node=$badNode，waiting=$badReason"
  Write-Host "如有节点权限，现在可在 $badNode 用 crictl 查 READY sandbox 与空 app container。"
  Read-Host '完成观察后按 Enter 清理'
}
finally {
  if ($createdNamespace) {
    $actualOwner = kubectl get namespace $ns -o jsonpath='{.metadata.labels.studyowner}' 2>$null
    if ($actualOwner -eq $owner) {
      kubectl delete namespace $ns --wait=false
    } else {
      Write-Warning "owner label 不匹配，未自动删除 $ns"
    }
  }
}
```

目标 Node 上的可选只读取证：

```bash
sudo crictl pods --label io.kubernetes.pod.uid=<IMAGE_BAD_UID>
sudo crictl inspectp <SANDBOX_ID>
sudo crictl ps -a --pod <SANDBOX_ID>
```

成功判定不是“Pod 看起来 ContainerCreating”，而是：

```text
同一目标 Node：
  runtime-ok Ready
  image-bad 已绑定
  image-bad waiting reason=ErrImagePull 或 ImagePullBackOff
  按 UID 有镜像失败 Event

源码解释：
  RunPodSandbox 已经在 EnsureImageExists 之前完成；
  CreateContainer 仍在 EnsureImageExists 之后，尚未成功。
```

`PodReadyToStartContainers` 在目标能力未开启时可能缺失，所以脚本不把 condition 缺失当失败；有 Node 权限时，用 READY sandbox 作为更直接补证。

---

## 21. 二读可选实验：用未知 RuntimeClass handler 安全制造 sandbox 失败

这个实验比“停 containerd、删 CNI 配置”安全，因为它只让本次 Pod 选择一个几乎肯定不存在的 handler，不修改节点 runtime 配置。但 RuntimeClass 是 cluster-scoped，仍只允许在可销毁、独享测试集群执行，并要求相应 RBAC。

预期链：

```text
RuntimeClass 对象存在
  -> kubelet LookupRuntimeHandler 成功
  -> handler 传给 CRI RunPodSandbox
  -> runtime 不认识 handler
  -> RunPodSandbox 返回错误
  -> FailedCreatePodSandBox
  -> 业务 container 不会创建
```

完整实验：

```powershell
$context = kubectl config current-context
Write-Host "current-context = $context"
if ((Read-Host '将创建 cluster-scoped RuntimeClass；确认是可销毁独享集群，输入 RUNTIMECLASS') -ne 'RUNTIMECLASS') {
  throw '用户取消实验'
}

$canCreate = (kubectl auth can-i create runtimeclasses.node.k8s.io).Trim()
$canDelete = (kubectl auth can-i delete runtimeclasses.node.k8s.io).Trim()
if ($canCreate -ne 'yes' -or $canDelete -ne 'yes') {
  throw '当前身份没有创建/删除 RuntimeClass 的权限'
}

$stamp = Get-Date -Format 'yyyyMMddHHmmss'
$rc = "chapter12-invalid-$stamp"
$handler = "chapter12-unknown-$stamp"
$ns = "runtimeclass-lab-12-$stamp"
$owner = "chapter12-runtimeclass-$stamp"
$createdRuntimeClass = $false
$createdNamespace = $false

try {
  $runtimeClassManifest = @'
apiVersion: node.k8s.io/v1
kind: RuntimeClass
metadata:
  name: __RUNTIME_CLASS__
  labels:
    studyowner: __OWNER__
handler: __HANDLER__
'@
  $runtimeClassManifest.Replace('__RUNTIME_CLASS__', $rc).
    Replace('__OWNER__', $owner).
    Replace('__HANDLER__', $handler) |
    kubectl create -f -
  if ($LASTEXITCODE -ne 0) { throw '创建 RuntimeClass 失败' }
  $createdRuntimeClass = $true
  $actualHandler = kubectl get runtimeclass $rc -o jsonpath='{.handler}'
  if ($actualHandler -ne $handler) { throw 'RuntimeClass handler 与预期不一致' }
  # 给 scheduler/kubelet 的 RuntimeClass informer 一个传播窗口；
  # 后面仍以 Event message 是否包含该 handler 做硬验收。
  Start-Sleep -Seconds 10

  kubectl create namespace $ns
  if ($LASTEXITCODE -ne 0) { throw '创建 namespace 失败' }
  $createdNamespace = $true
  kubectl label namespace $ns "studyowner=$owner"
  if ($LASTEXITCODE -ne 0) { throw '设置 namespace owner 失败' }

  $podManifest = @'
apiVersion: v1
kind: Pod
metadata:
  name: bad-runtime-handler
  namespace: __NAMESPACE__
spec:
  runtimeClassName: __RUNTIME_CLASS__
  containers:
  - name: game-api
    image: registry.k8s.io/pause:3.10
'@
  $podManifest.Replace('__NAMESPACE__', $ns).
    Replace('__RUNTIME_CLASS__', $rc) |
    kubectl create -f -
  if ($LASTEXITCODE -ne 0) { throw '创建实验 Pod 失败' }

  $uid = kubectl get pod bad-runtime-handler -n $ns -o jsonpath='{.metadata.uid}'
  $bindDeadline = (Get-Date).AddSeconds(120)
  do {
    $node = kubectl get pod bad-runtime-handler -n $ns -o jsonpath='{.spec.nodeName}'
    if ($node) { break }
    Start-Sleep -Seconds 2
  } while ((Get-Date) -lt $bindDeadline)
  if ([string]::IsNullOrWhiteSpace($node)) {
    throw 'Pod 尚未绑定 Node，不能验证 kubelet runtime 路径'
  }

  $failedSandboxEvents = @()
  $deadline = (Get-Date).AddSeconds(180)
  do {
    $eventJson = kubectl get events -n $ns `
      --field-selector "involvedObject.uid=$uid,reason=FailedCreatePodSandBox" `
      -o json 2>$null
    if ($LASTEXITCODE -eq 0 -and $eventJson) {
      $eventList = $eventJson | ConvertFrom-Json
      $failedSandboxEvents = @($eventList.items | Where-Object {
        $_.message -like "*$handler*" -and
        (
          $_.source.host -eq $node -or
          $_.reportingInstance -eq $node
        )
      })
    }
    if ($failedSandboxEvents.Count -gt 0) { break }
    Start-Sleep -Seconds 2
  } while ((Get-Date) -lt $deadline)
  if ($failedSandboxEvents.Count -lt 1) {
    throw '没有来自目标 Node 且 message 包含未知 handler 的 FailedCreatePodSandBox；本实验不能判成功'
  }

  $containerID = kubectl get pod bad-runtime-handler -n $ns `
    -o jsonpath='{.status.containerStatuses[0].containerID}' 2>$null
  if ($containerID) {
    throw "业务 container 已有 ID=$containerID，不符合 sandbox 前失败签名"
  }

  kubectl get pod bad-runtime-handler -n $ns -o jsonpath='{.metadata.uid}{"\n"}{.spec.nodeName}{"\n"}{.status.phase}{"\n"}{range .status.containerStatuses[*]}{.name}{" id="}{.containerID}{" waiting="}{.state.waiting.reason}{"\n"}{end}'
  kubectl get events -n $ns --field-selector "involvedObject.uid=$uid" `
    -o custom-columns='TYPE:.type,REASON:.reason,SOURCE:.source.component,SOURCE-HOST:.source.host,REPORTING-INSTANCE:.reportingInstance,MESSAGE:.message'

  Write-Host "PASS: RuntimeClass=$rc handler=$handler PodUID=$uid Node=$node"
  Write-Host '完整 message 应指向 unknown/unconfigured runtime handler 一类错误。'
  Read-Host '完成观察后按 Enter 清理'
}
finally {
  if ($createdNamespace) {
    $actualNsOwner = kubectl get namespace $ns -o jsonpath='{.metadata.labels.studyowner}' 2>$null
    if ($actualNsOwner -eq $owner) {
      kubectl delete namespace $ns --wait=false
    } else {
      Write-Warning "namespace owner 不匹配，未自动删除 $ns"
    }
  }
  if ($createdRuntimeClass) {
    $actualRcOwner = kubectl get runtimeclass $rc -o jsonpath='{.metadata.labels.studyowner}' 2>$null
    if ($actualRcOwner -eq $owner) {
      kubectl delete runtimeclass $rc
    } else {
      Write-Warning "RuntimeClass owner 不匹配，未自动删除 $rc"
    }
  }
}
```

结果边界：

| 结果 | 说明 | 是否命中目标 |
|---|---|---|
| RuntimeClass 创建被拒绝 | RBAC/API admission，未到 kubelet | 否 |
| Pod Pending、`NODE=<none>` | scheduler/RuntimeClass scheduling 等更早路径 | 否 |
| Pod 创建时直接 Forbidden：RuntimeClass 不存在 | 默认 RuntimeClass API admission 拒绝 | 否，尚未到 kubelet |
| 已通过 admission 的 Pod 后续在 kubelet看到 RuntimeClass NotFound | 对象被删、informer滞后或非标准 admission 配置；尚未进入 CRI | 只验证 kubelet lookup 竞态分支 |
| `FailedCreatePodSandBox` 且 message 为 unknown handler | 已把 handler 传给 runtime 的 RunPodSandbox | 是 |
| Pod 意外 Running | 目标 runtime 接受/回退了 handler | 此环境不适合这个实验，不能把它说成失败链 |

不要在主课实验中停 containerd、删除 `/etc/cni/net.d`、修改默认 runtime handler 或制造节点磁盘故障；这些操作会影响节点上其他 Pod，只能另做有恢复预案的独占环境演练。

---

## 22. GPU 短映射：GPU 容器也先有通用 sandbox

GPU Pod 不会跳过本课主线：

```text
Kubelet.SyncPod
  -> runtime manager
  -> PodSandbox / network
  -> image
  -> ContainerConfig
  -> CRI CreateContainer / StartContainer
```

GPU 特有信息主要在业务 container config 汇合：

```text
DeviceManager / DRA / runtime helper
  -> env
  -> mounts
  -> devices
  -> CDI devices
  -> annotations
  -> CRI ContainerConfig
```

本章先记住五个边界：

1. `FailedCreatePodSandBox` 时，GPU 业务容器通常还没创建，不能先以 `nvidia-smi` 无进程作为根因；
2. RuntimeClass 只是可选 runtime handler，不是每个 GPU Pod 必填；
3. Device Plugin 的 Allocate/DeviceManager 注入不是 CNI，也不是 PodSandbox 网络步骤；
4. 当前 DRA `PrepareDynamicResources` 在 `createPodSandbox` 前，失败 reason 与 `FailedCreatePodSandBox` 不同；
5. GPU device/env/mount/CDI 最终进入 container 配置，但“选哪块设备、checkpoint 怎样恢复”留到第 15～17 课。

一个 GPU Pod 可以同时：

```text
PodSandbox READY
PodReadyToStartContainers=True
image 已拉取
但因 device/CDI/container create 配置错误仍未启动
```

因此 GPU 值班也要按通用层到专项层逐级排：

```text
调度与 nodeName
  -> kubelet/sandbox/network
  -> image/container runtime
  -> device injection
  -> driver/CUDA
  -> 应用/模型
```

---

## 23. 本章 Go 语法复习索引

会挡住主线的写法已经放在第一次出现的源码旁边解释；这里用于课后集中复习，不要求首遍重新背八项。首遍优先复述 receiver、多返回值、struct literal、局部 closure；named return、interface wrapper、`defer`/context 与局部 `if err :=` 可在二读跟代码再过一遍。

### 23.1 method receiver

```go
func (m *kubeGenericRuntimeManager) SyncPod(...) ...
```

大白话：

- `m` 是这次调用使用的 runtime manager 对象；
- `*kubeGenericRuntimeManager` 表示拿指针，可读取/修改同一个 manager 的字段；
- 所以函数里能写 `m.runtimeService`、`m.imagePuller`。

类比 Java：

```java
class KubeGenericRuntimeManager {
    PodSyncResult syncPod(...) {
        this.runtimeService.runPodSandbox(...);
    }
}
```

不是语法完全相同，只是帮助你把 receiver 暂时理解成 `this`。

### 23.2 多返回值

```go
create, attempt, sandboxID :=
    runtimeutil.PodSandboxChanged(pod, podStatus)
```

一个函数一次返回三个值。调用方按位置接：

```text
create     -> bool
attempt    -> uint32
sandboxID  -> string
```

### 23.3 named return

```go
func (...) (result kubecontainer.PodSyncResult) {
    result.AddSyncResult(...)
    return
}
```

`result` 在函数入口就有名字。裸 `return` 会返回当前 `result`。读源码时要沿函数看它在哪里被逐步追加错误，不能只找最后一行。

### 23.4 struct literal 与 `&`

```go
config := &runtimeapi.PodSandboxConfig{
    Metadata: &runtimeapi.PodSandboxMetadata{
        Name: pod.Name,
    },
}
```

- `Type{...}`：创建结构体值；
- `&Type{...}`：创建后取地址，得到指针；
- 字段名后面的冒号是在给字段赋初值，不是 YAML。

### 23.5 `if err := ...; err != nil`

```go
if err := m.runtimeHelper.OnPodSandboxReady(ctx, pod); err != nil {
    logger.Error(err, "callback failed")
}
```

先调用并把结果放进局部 `err`，再判断；这个 `err` 只在 `if/else` 范围内有效。

### 23.6 `defer` 为什么常跟 timeout 一起

```go
ctx, cancel := context.WithTimeout(ctx, timeout)
defer cancel()
```

大白话：

1. 派生一个有截止时间的 context；
2. 拿到 `cancel` 清理函数；
3. 当前函数返回时一定调用 cancel，及时释放 timer/资源。

`defer recordOperation(...)` 则表示无论成功还是错误返回，都在函数退出时记一次耗时。

### 23.7 局部 closure 捕获外层变量

```go
start := func(spec *startSpec) error {
    result.AddSyncResult(...)
    msg, err := m.startContainer(...)
    return err
}
```

`start` 是函数里的函数。它能使用外层的 `result`、`pod`、`podStatus`。这样 ephemeral/init/regular container 可以复用同一段启动和记账逻辑。

### 23.8 interface 让 kubelet不依赖某个 runtime 实现

`m.runtimeService` 按 CRI service interface 调方法。编译时只要求对象实现同一组方法，不要求名字必须是 containerd。这是 Kubernetes 能对接多个 CRI runtime 的关键 Go 机制。

---

## 24. 本章必须掌握、二读与可以略过

### 24.1 首遍必须掌握

- `Kubelet.SyncPod -> containerRuntime.SyncPod -> kubeGenericRuntimeManager.SyncPod`；
- desired Pod、runtime PodStatus、podActions、PodSyncResult 四者职责；
- sandbox 是 Pod 级环境，业务 container 在其后创建；
- `computePodActions` 先算动作，再执行；
- `NetworkNotReady` 是 runtime manager 外的节点级网络门；
- `FailedCreatePodSandBox` 不是 CNI 专属 reason；
- DRA prepare 失败发生在 `createPodSandbox` 之前；
- `generatePodSandboxConfig -> RuntimeClass -> RunPodSandbox`；
- kubelet只调 CRI，具体 runtime 再实现 CNI/namespace/container；
- `RunPodSandbox` 创建并启动 READY sandbox，没有 `StartPodSandbox`；
- sandbox 成功后才查 status/IP、回调 condition，再拉业务镜像；
- `PodReadyToStartContainers=True` 不等于 app container/Java Ready；
- image pull 在 `CreateContainer` 之前；
- image pull 走独立 CRI ImageService，endpoint 可与 RuntimeService 分开配置；
- `CreateContainer` 与 `StartContainer` 是两次 CRI；
- PostStart 失败发生在 Start 成功之后；
- Event reason、Waiting reason、message、runtime log 不能混成一列；
- 能用 sandbox/container 状态判断断点。

### 24.2 二读再掌握

- `PodSandboxChanged` 的全部重建条件；
- sandbox attempt 与 container restartCount；
- RuntimeClass lookup error 与 runtime unknown handler 的差异；
- sandbox config 的 DNS、namespace、security、cgroup 字段；
- instrumented service 与 remote client 的包装层；
- CRI timeout 的当前默认和版本边界；
- init/restartable init/ephemeral 的特殊 action；
- image volume、user namespace、resize；
- PreCreate/PreStart internal lifecycle；
- status callback 的异步与错误不阻断行为；
- runtime metrics 稳定级别。

### 24.3 本章可以一笔带过

- containerd CRI plugin 内部完整实现；
- CNI plugin 源码、IPAM 算法、eBPF datapath；
- runc/OCI bundle 内部；
- Windows HostProcess；
- user namespace 的全部映射；
- image service 并发/凭据 provider 内部；
- GC、日志轮转；
- PLEG、probe 和 statusManager 完整回路；
- DeviceManager/DRA 分配算法。

这些不是“不学”，而是不让它们在本课打断 `PodSandbox -> CRI -> container` 主线。

---

## 25. 本章验收题

### 25.1 首遍必须答出的 10 题

1. `ContainerCreating` 为什么不是可直接对应的源码函数？
2. `NetworkNotReady` 与单 Pod 的 `FailedCreatePodSandBox` 有什么责任域差异？
3. `computePodActions` 为什么要先产生 action，而不是看到 Pod 就直接 Create？
4. PodSandbox 与 Java 业务 container 分别承载什么？
5. `FailedCreatePodSandBox` 为什么不能直接等同于 CNI 故障？
6. kubelet 到 runtime 之间有哪些 CRI 包装？为什么 image pull 还要单独追 ImageService endpoint？
7. `RunPodSandbox` 成功的合同是什么，为什么没有 `StartPodSandbox`？
8. `PodReadyToStartContainers=True` 能证明什么、不能证明什么？
9. 为什么 `ImagePullBackOff` 时可能已有 Pod IP，但还没有业务 container ID？
10. `Created`、`Started`、`Ready=True` 三个证据分别越过哪道门？

### 25.2 二读源码反查 10 题

1. `PodSandboxChanged` 在哪些条件下要求重建 sandbox？
2. 为什么 `CreateSandbox=true` 通常同时有 `KillPod=true`？
3. DRA prepare error 会记录哪个 reason，为什么不是 `FailedCreatePodSandBox`？
4. 默认 admission 下 RuntimeClass 对象不存在为何通常在 Pod 创建时 Forbidden？什么竞态下才会到 kubelet lookup，handler 未配置又在哪里失败？
5. `generatePodSandboxConfig` 为什么在一轮中可能出现两次？
6. instrumented service 怎样记录每次 CRI operation 和错误？
7. sandbox timeout 为什么与 container Create/Start timeout 不同？
8. 当前执行顺序为什么写成 ephemeral -> init -> resize -> regular，而普通首次启动又常见 init -> app？
9. Go 常量 `FailedToStartContainer` 为什么不等于用户 Event REASON 列的同名字符串？
10. PostStart 失败时为什么可能先看到 `Started`，随后容器又退出？

### 25.3 现场口述题

给出：

```text
NODE=worker-05
PodScheduled=True
PodReadyToStartContainers=True
PodIP=192.0.2.41
game-api waiting=ImagePullBackOff
containerID=""
```

你应能口述：

```text
scheduler完成；
目标 kubelet已处理；
sandbox/network 已满足“不需重建 sandbox、可继续启动 container”的条件；
单凭该 condition 不能证明所有 volume 此刻已挂载；
image pull 阻断了 CreateContainer；
此时查 registry/DNS/TLS/auth/节点出口，
而不是查 Java readiness、JVM OOM 或 GPU进程。
```

---

## 26. 当前源码断点

```text
本章已讲：

pkg/kubelet/kubelet.go
  -> RuntimeService/ImageService endpoint 装配:403-415
  -> Kubelet.SyncPod 外层网络检查:2103-2107
  -> probe注册与 runtime SyncPod:2210-2232
  -> runtime status -> NetworkReady/RuntimeReady:3245-3279
  -> OnPodSandboxReady:3541-3578

pkg/kubelet/status/generate.go
  -> GeneratePodReadyToStartContainersCondition:270-287

pkg/kubelet/kubelet_pods.go
  -> 每轮追加 PodReadyToStartContainers:1984-1987

pkg/kubelet/kuberuntime/util/util.go
  -> PodSandboxChanged:30-68

pkg/kubelet/kuberuntime/kuberuntime_manager.go
  -> computePodActions:1174-约1436
  -> SyncPod 九步合同:1439-1450
  -> kill/create sandbox:1450-1657
  -> 第二次 sandbox config 与 start helper:1667-1741
  -> ephemeral/init/resize/regular:1743-1795

pkg/kubelet/kuberuntime/kuberuntime_sandbox.go
  -> createPodSandbox:37-74
  -> generatePodSandboxConfig:77-156
  -> Linux sandbox config:159 起

pkg/kubelet/kuberuntime/kuberuntime_container.go
  -> error code:67-76
  -> startContainer:193-338
  -> generateContainerConfig:341 起

pkg/kubelet/images/image_manager.go
  -> EnsureImageExists 与 image backoff/pull:164-约381

pkg/kubelet/kuberuntime/kuberuntime_image.go
  -> kubeGenericRuntimeManager.PullImage:31 起

pkg/kubelet/kuberuntime/instrumented_services.go
  -> CreateContainer/StartContainer wrapper:81-96
  -> RunPodSandbox wrapper:180-191
  -> ImageService PullImage wrapper:311-318

staging/src/k8s.io/cri-client/pkg/remote_runtime.go
  -> RunPodSandbox:218-252
  -> CreateContainer:392-422
  -> StartContainer:425-440

staging/src/k8s.io/cri-client/pkg/remote_image.go
  -> PullImage:234-约270

pkg/kubelet/events/event.go
  -> container failure reason 字面值:21-24
  -> FailedCreatePodSandBox:81
  -> FailedStatusPodSandBox 的字面值 FailedPodSandBoxStatus:82
  -> FailedPostStartHook:114

本章停在：
  runtime 已启动或拒绝启动 container，
  但尚未解释 probe结果、runtime变化如何再次唤醒 SyncPod，
  也尚未解释 Running/Ready/restartCount 怎样写回 API。

下一章：
  probe result cache
  -> syncLoop probe update
  -> statusManager
  -> PLEG / podCache
  -> Java Running、Ready=False 与 restartCount
```

---

## 27. 一句话收口

```text
kubelet不是直接“创建 Java 容器”：
它先比较 desired Pod 与 runtime 事实，建立 PodSandbox，
通过 CRI 让 runtime 准备 Pod 级环境，
再按 image -> config -> CreateContainer -> StartContainer -> PostStart
逐门推进业务容器；
每一扇门都有不同证据，不能被一个 ContainerCreating 全部抹平。
```
