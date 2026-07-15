# 第 13 课：Java Pod Running 为什么仍不接流量，又为什么会重启——probe、statusManager 与 PLEG

> 主案例：`game-api` 的 JVM 进程已经 Running，但 readiness 因数据库依赖失败；随后 liveness 连续失败，容器被重启  
> 主线源码：`pkg/kubelet/prober/*`、`pkg/kubelet/status/*`、`pkg/kubelet/pleg/*`、`pkg/kubelet/kubelet.go`、`pkg/kubelet/kuberuntime/*`  
> 源码基线：`301946d15e67a4a2e8a5fb8292eb836acd366d78`（`v1.37.0-alpha.0-280-g301946d15e6`）  
> 本课深度：整体 S2；“liveness Failure -> runtime kill/start”和 PLEG cache 顺序窄读到 S3  
> 前置断点：第 12 课已经走到 `StartContainer/PostStart`，但还没解释状态怎样再收敛到 API

---

## 0. 这次也不重新讲探针基础

你已经使用 Kubernetes 多年，本课不从“liveness、readiness、startup 分别是什么”开始。我们直接解决生产中真正容易误判的六个问题：

1. `phase=Running` 为什么可以与 `Ready=False` 同时成立？
2. 已经出现 `Readiness probe failed`，为什么 Ready 可能暂时仍是 True？
3. 已经出现 `Liveness probe failed`，为什么 `restartCount` 可能仍是 0？
4. liveness worker 是否直接调用 CRI KillContainer？
5. Java 进程主动退出时没有 probe，kubelet又怎样发现？
6. `kubectl` 看到的 PodStatus 为什么可能比 kubelet本地事实慢一小段？

本章最重要的不是背字段，而是区分三个本地事实源：

```text
probe result cache
PLEG 填充的 runtime podCache
statusManager 保存的 API-facing PodStatus cache
```

它们不是一张表，也不是同一个 goroutine。

---

## 1. 一个 Java Pod 的三个生产时刻

### 1.1 慢启动

`game-api` 启动时要：

```text
JVM 初始化
  -> 读取配置
  -> 建数据库连接池
  -> 加载缓存
  -> Spring context 完成
  -> 开始接流量
```

若只配激进 liveness，慢启动可能被误杀。startup probe 的作用不是“第四种健康状态”，而是在容器真正 Started 前给 readiness/liveness 加一道门。

### 1.2 Running，但 readiness 失败

生产证据可能是：

```text
phase=Running
container state=Running
Ready=False
ContainersReady=False
restartCount=0
Event: Readiness probe failed
```

这并不矛盾：

- phase 看进程生命周期；
- readiness 看是否应接流量；
- readiness Failure 不要求杀容器。

### 1.3 liveness 连续失败并重启

稍后 Java 线程死锁：

```text
第一次 liveness 失败
  -> Unhealthy Event 可能已出现
  -> 还没达到 failureThreshold
  -> restartCount 仍可能不变

达到 failureThreshold
  -> probe result cache 变 Failure
  -> 请求重新 SyncPod
  -> runtime manager 计划 kill/start
  -> PLEG 观察实际 container ID 变化
  -> statusManager 上报 restartCount/lastState
```

一次重启也不等于 `CrashLoopBackOff`。只有持续失败且再次启动前仍处于 backoff，才出现该 Waiting reason。

---

## 2. 首遍先读三条链

### 2.1 readiness 链

```text
probe worker
  -> readiness result cache
  -> syncLoop readiness update
  -> statusManager.SetContainerReadiness
  -> ContainerStatus.Ready
  -> ContainersReady / PodReady
  -> PatchPodStatus
  -> EndpointSlice endpoint conditions.ready
```

### 2.2 liveness/startup 链

```text
probe worker 达到 threshold
  -> result cache=Failure
  -> syncLoop probe update
  -> HandlePodSyncs
  -> podWorkers.UpdatePod
  -> Kubelet.SyncPod
  -> runtime manager computePodActions
  -> ContainersToKill
  -> 按 restart policy 决定 ContainersToStart
```

### 2.3 runtime/PLEG 链

```text
runtime container state 变化
  -> GenericPLEG Relist
  -> GetPods / 比较 old-current
  -> GetPodStatus
  -> 先更新 runtime podCache
  -> 再发 ContainerStarted/ContainerDied/PodSync
  -> syncLoop
  -> pod worker 从新 cache 状态继续 SyncPod
```

首遍建议：

```text
先读：1～3、4 主结论、5～12、13 主结论、14～19、21、23.1、24.1、25～26
二读：4 的 worker重复保护、13 的全部 PLEG分支、14.2、15 的 status patch细节、
      20 的节点资源边界、22 的 Go复习、23.2/23.3、24.2
```

---

## 3. 三本本地事实账

创建位置：

```text
pkg/kubelet/kubelet.go:687-696
```

```go
klet.livenessManager = proberesults.NewManager()
klet.readinessManager = proberesults.NewManager()
klet.startupManager = proberesults.NewManager()

podCache := kubecontainer.NewCache()
klet.podCache = podCache

klet.statusManager = status.NewManager(...)
```

| 本地事实源 | key/主要内容 | 写入者 | 主要消费者 |
|---|---|---|---|
| probe result manager | `containerID -> Success/Failure/Unknown` | probe worker | syncLoop、runtime manager、PodStatus生成 |
| PLEG podCache | `Pod UID -> kubecontainer.PodStatus`，含 sandbox/container实际状态 | Generic/Evented PLEG 的 runtime observation | podWorkers、Kubelet.SyncPod |
| statusManager cache | `Pod UID -> v1.PodStatus + local version` | Kubelet/status/probe回路 | status sync worker、apiserver `/status` |

这三本账之间可能有短暂时差：

```text
probe已经失败
  != API Ready 已经翻转

runtime container 已退出
  != kubectl 已经看到 lastState

statusManager 本地已更新
  != apiserver patch 已完成
```

运维要用 UID、container ID、Event 时间和各层日志组合，不把一次 `kubectl get` 当成所有组件同一时刻的原子快照。

---

## 4. `probeManager.AddPod` 为每个 container/probe type 启 worker

第 12 课已经见过：

```text
pkg/kubelet/kubelet.go:2213-2214

kl.probeManager.AddPod(ctx, pod)
```

内部：

```text
pkg/kubelet/prober/prober_manager.go:185-230
```

```go
for _, c := range append(
    pod.Spec.Containers,
    getRestartableInitContainers(pod)...,
) {
    if c.StartupProbe != nil {
        w := newWorker(m, startup, pod, c)
        m.workers[key] = w
        go w.run(ctx)
    }
    if c.ReadinessProbe != nil {
        w := newWorker(m, readiness, pod, c)
        m.workers[key] = w
        go w.run(ctx)
    }
    if c.LivenessProbe != nil {
        w := newWorker(m, liveness, pod, c)
        m.workers[key] = w
        go w.run(ctx)
    }
}
```

worker key：

```text
Pod UID + container name + probe type
```

因此一个 `game-api` container 同时配三种 probe 时，会有三个不同 worker，不是一个 worker 顺序执行三项。

Go 现场补——`go w.run(ctx)`：

- `go` 启动一个 goroutine；
- `AddPod` 不会等这个 probe loop 永久结束；
- 每个 worker 自己按 period 运行，并通过 result manager channel 与 syncLoop 交接；
- 它与第 11 课的 pod worker 不是同一种 worker：probe worker 做检测，pod worker 做单 Pod 收敛。

当前还会为 restartable init container 建 probe worker。普通一次性 init 没有同样的长期健康语义，首遍不展开 sidecar 特殊分支。

---

## 5. 三种 worker 的初始值故意不同

```text
pkg/kubelet/prober/worker.go:100-154
```

```go
switch probeType {
case readiness:
    w.spec = container.ReadinessProbe
    w.resultsManager = m.readinessManager
    w.initialValue = results.Failure
case liveness:
    w.spec = container.LivenessProbe
    w.resultsManager = m.livenessManager
    w.initialValue = results.Success
case startup:
    w.spec = container.StartupProbe
    w.resultsManager = m.startupManager
    w.initialValue = results.Unknown
}
```

大白话：

| probe | 初始值 | 为什么这样更安全 |
|---|---|---|
| readiness | Failure | 没证明能接流量前，先不接流量 |
| liveness | Success | 没实际证明进程坏掉前，不先误杀 |
| startup | Unknown | 还没完成启动验证，状态未知 |

Go 现场补——`switch`：

它按 `probeType` 这个枚举式值选择不同配置。Go 的 `case` 默认不会像 C/Java 老式 switch 那样自动贯穿到下一 case，不需要每项写 `break`。

### 5.1 结果按 container ID，不只按名字

worker拿到新的 `containerID` 时，会移除旧 ID 的结果并为新 ID 放初始值：

```text
worker.go:250-287
```

这解释了为什么容器重启后：

```text
Pod UID 不变
container name 不变
container ID 改变
probe结果要为新进程重新建立
```

只按 Pod 名或 container name 查日志，会把两次不同进程混在一起。

---

## 6. startup probe 是另外两种 probe 的门

核心：

```text
pkg/kubelet/prober/worker.go:330-346
```

```go
if int32(time.Since(c.State.Running.StartedAt.Time).Seconds()) <
    w.spec.InitialDelaySeconds {
    return true
}

if c.Started != nil && *c.Started {
    if w.probeType == startup {
        return true
    }
} else {
    if w.probeType != startup {
        return true
    }
}
```

时序：

```text
container CRI Started
  -> status.Started 还不是 startup成功

startup 尚未 Success
  -> readiness worker不执行实际 probe
  -> liveness worker不执行实际 probe

startup 达到 SuccessThreshold
  -> startup result cache=Success
  -> syncLoop 设置 ContainerStatus.Started=true
  -> readiness/liveness 后续可以执行
```

这里的 worker实现是三种 probe共用的，所以代码里有通用 `SuccessThreshold`；但当前 Pod API校验有更窄的配置边界：

```text
livenessProbe.successThreshold 必须等于 1
startupProbe.successThreshold  必须等于 1
readinessProbe.successThreshold 才允许大于 1
```

对应：

```text
pkg/apis/core/validation/validation.go
  -> validateLivenessProbe
  -> validateReadinessProbe
  -> validateStartupProbe
```

因此，对一个通过 API校验的 startup probe，“达到 SuccessThreshold”实际就是第一次成功；liveness也不能靠连续多次Success才恢复。readiness才常用 `successThreshold > 1`抑制刚恢复时的抖动。

`initialDelaySeconds` 是每个 worker 自己的延迟门；startup probe 是容器 Started 状态门。二者可以同时存在，但配置时通常用 startup 的总容忍窗口表达慢启动，不要靠巨大的 liveness initial delay 猜 JVM 最坏启动时间。

### 6.1 startup Failure 不是只把 Started 设 False

startup 达到 failure threshold 后：

```text
startup result cache=Failure
  -> syncLoop startup update
  -> HandlePodSyncs
  -> runtime manager 读 startup Failure
  -> 计划 kill container
  -> 按 restart policy 决定是否重新启动
```

所以 startup 是“启动期存活门”，不是 readiness 的别名。

---

## 7. 第一条 `Unhealthy` Event 与 threshold 不是同一时刻

真正执行 probe：

```text
pkg/kubelet/prober/prober.go:102-128
```

```go
result, output, err :=
    pb.runProbeWithRetries(...)

case probe.Failure:
    pb.recordContainerEvent(
        ctx,
        pod,
        &container,
        Warning,
        events.ContainerUnhealthy,
        "%s probe failed: %s",
        probeType,
        output,
    )
    return results.Failure, nil
```

worker随后才累计连续结果：

```text
pkg/kubelet/prober/worker.go:366-390
```

```go
if w.lastResult == result {
    w.resultRun++
} else {
    w.lastResult = result
    w.resultRun = 1
}

if result == Failure &&
    w.resultRun < int(w.spec.FailureThreshold) {
    return true
}

w.resultsManager.Set(w.containerID, result, w.pod)
```

所以：

```text
每次实际 probe Failure
  -> 可以先记录/聚合 Unhealthy Event

连续 Failure 尚未达到 threshold
  -> result manager中的稳定状态不变
  -> readiness可能还没翻转
  -> liveness还不会请求kill
```

Event broadcaster 会聚合同类 Event，不能把 Event 对象 count 当作精确无损的每次 probe 调用日志。

### 7.1 probe error 更要谨慎

`prober.probe` 遇到执行错误会记录 `probe errored` Event 并返回 error；worker当前在 `err != nil` 时丢弃这次 result，不进入 threshold 计数。

因此：

```text
看到 Unhealthy Event
  != 这次一定计入 failureThreshold
  != Ready 已经 False
  != 容器已经重启
```

必须继续看完整 message 是 `probe failed` 还是 `probe errored`，以及后续 condition/container ID。

---

## 8. result manager 只在稳定结果变化时通知 syncLoop

```text
pkg/kubelet/prober/results/results_manager.go:106-138
```

```go
func (m *manager) Set(
    id kubecontainer.ContainerID,
    result Result,
    pod *v1.Pod,
) {
    if m.setInternal(id, result) {
        m.updates <- Update{id, result, pod.UID}
    }
}

func (m *manager) setInternal(...) bool {
    prev, exists := m.cache[id]
    if !exists || prev != result {
        m.cache[id] = result
        return true
    }
    return false
}
```

这说明：

```text
通过门控并实际执行，且 prober 无 error 返回
  -> worker 才累计 prober_probe_total
  -> 普通 Failure 可能先产生/聚合 Event

probe error
  -> 可能产生 "probe errored" Event
  -> worker 在 metrics 与 threshold 之前直接丢弃该 result

initialDelay / startup门控 / onHold 等未实际执行路径
  -> 也不能当成一次已计数 probe

只有缓存不存在或稳定结果发生变化
  -> Updates channel 才有消息
```

例如 readiness 已经 Failure，后续继续 Failure：

- worker仍执行；
- 这类无 error 的普通 Failure metrics继续累计；
- Event可能聚合 count；
- result cache值没变；
- 不会每两秒都因同一个 Failure 给 syncLoop塞一条新 update。

Go 现场补——`result, found := m.cache[id]`：

- map读取可返回值和 `found bool`；
- `found=false` 与“值刚好等于某个零值”不同；
- 这也是源码经常写 `result, ok := manager.Get(id)` 的原因。

---

## 9. readiness Failure：先改状态，不直接杀 container

syncLoop：

```text
pkg/kubelet/kubelet.go:2762-2770
```

```go
case update := <-kl.readinessManager.Updates():
    ready := update.Result == proberesults.Success
    kl.statusManager.SetContainerReadiness(
        logger,
        update.PodUID,
        update.ContainerID,
        ready,
    )
    handleProbeSync(
        ctx,
        kl,
        update,
        handler,
        "readiness",
        status,
    )
```

`SetContainerReadiness`：

```text
pkg/kubelet/status/status_manager.go:490-557
```

```go
status := *oldStatus.status.DeepCopy()
containerStatus, _, _ =
    findContainerStatus(
        &status,
        containerID.String(),
    )
containerStatus.Ready = ready

updateConditionFunc(
    v1.PodReady,
    GeneratePodReadyCondition(...),
)
updateConditionFunc(
    v1.ContainersReady,
    GenerateContainersReadyCondition(...),
)

m.updateStatusInternal(...)
```

Go 现场补——`DeepCopy` 与 `*`：

- `DeepCopy()` 返回一份新对象的指针，避免在锁保护之外直接修改共享 cache；
- 前面的 `*` 把指针解引用成一个 `PodStatus` 值；
- 后面只修改副本，再交给 `updateStatusInternal` 生成新版本。

readiness update 同时：

1. 直接更新 statusManager 本地 Ready 状态；
2. 再调用 `HandlePodSyncs`，让 Pod 做一轮正常收敛。

但 runtime manager 的 kill 判断读的是 liveness/startup result，不把 readiness Failure 当 kill 原因。

---

## 10. 为什么 `phase=Running` 不读取 readiness

phase 计算：

```text
pkg/kubelet/kubelet_pods.go:1809-1824
```

```go
switch {
case waiting > 0:
    return v1.PodPending
case running > 0 && unknown == 0:
    return v1.PodRunning
// ...
}
```

它根据 container 是 waiting/running/stopped/unknown 判断，不读取 `ContainerStatus.Ready`。

因此：

| 字段 | 回答的问题 |
|---|---|
| `phase=Running` | 至少一个普通 container 正在运行，且没有 unknown |
| `containerStatuses[].ready` | 这个 container 当前是否通过 readiness |
| `ContainersReady` | 所有需要考虑的 container 是否 Ready |
| `PodReady` | containers ready 再叠加 readiness gates 等 Pod 级条件 |

`Running + Ready=False` 是源码允许且常见的正常组合，不是 API 状态损坏。

### 10.1 EndpointSlice 通常不是“删掉 endpoint”

```text
staging/src/k8s.io/endpointslice/utils.go:37-50
```

```go
serving := endpointutil.IsPodReady(pod)
terminating := pod.DeletionTimestamp != nil
ready :=
    service.Spec.PublishNotReadyAddresses ||
    (serving && !terminating)

ep.Conditions = discovery.EndpointConditions{
    Ready:       &ready,
    Serving:     &serving,
    Terminating: &terminating,
}
```

一般情况下，readiness Failure 后 endpoint仍可存在于 EndpointSlice，但：

```text
conditions.ready=false
```

代理/负载均衡据此停止把普通流量送给它。例外：

- Service `publishNotReadyAddresses=true`；
- terminating endpoint 的 serving/terminating 语义；
- 具体数据面更新延迟。

因此生产话术应是“EndpointSlice endpoint ready 变 False”，不要绝对说“readiness一失败 endpoint 立刻从对象里删除”。

---

## 11. liveness/startup Failure：worker不直接 kill，先要求 SyncPod

syncLoop：

```text
pkg/kubelet/kubelet.go:2758-2779
```

```go
case update := <-kl.livenessManager.Updates():
    if update.Result == proberesults.Failure {
        handleProbeSync(
            ctx, kl, update, handler,
            "liveness", "unhealthy",
        )
    }

case update := <-kl.startupManager.Updates():
    started := update.Result == proberesults.Success
    kl.statusManager.SetContainerStartup(
        logger,
        update.PodUID,
        update.ContainerID,
        started,
    )
    handleProbeSync(...)
```

`handleProbeSync`：

```text
kubelet.go:2817-2828
  -> podManager.GetPodByUID
  -> handler.HandlePodSyncs

kubelet.go:3186-3210
  -> podWorkers.UpdatePod(SyncPodSync)
```

然后复用第 11～12 课：

```text
podWorkerLoop
  -> 从 PLEG podCache 取较新的 runtime PodStatus
  -> Kubelet.SyncPod
  -> containerRuntime.SyncPod
  -> computePodActions
```

所以准确表述是：

```text
liveness worker只写结果并唤醒同步；
runtime manager在下一轮对账中决定 container 应被 kill/restart；
真正的 CRI Stop/Kill 不在 probe worker里直接调用。
```

---

## 12. runtime manager 怎样把 probe Failure 变成 kill/start action

```text
pkg/kubelet/kuberuntime/kuberuntime_manager.go:1327-1374
```

```go
restart := shouldRestartOnFailure(pod)

if liveness, found :=
    m.livenessManager.Get(containerStatus.ID);
    found && liveness == proberesults.Failure {

    message =
        fmt.Sprintf(
            "Container %s failed liveness probe",
            container.Name,
        )
    reason = reasonLivenessProbe

} else if startup, found :=
    m.startupManager.Get(containerStatus.ID);
    found && startup == proberesults.Failure {

    message =
        fmt.Sprintf(
            "Container %s failed startup probe",
            container.Name,
        )
    reason = reasonStartupProbe
}

if restart {
    changes.ContainersToStart =
        append(changes.ContainersToStart, idx)
}
changes.ContainersToKill[containerStatus.ID] =
    containerToKillInfo{...}
```

对 Deployment 创建的 Pod，restartPolicy 通常是 `Always`，所以主案例：

```text
ContainersToKill  有旧 container ID
ContainersToStart 有 game-api index
```

随后 `SyncPod` 先执行 kill，再经过 start helper 创建/启动新 container。

### 12.1 restartPolicy 仍然参与决定

不能把“liveness失败必然重启”写成无条件规则：

- `restartPolicy=Never` 时，运行中的容器会被 probe kill，但不加入重新启动计划；
- `OnFailure/Always` 与当前 container-level restart policy feature 会影响 restart；
- Deployment 场景是 `Always`，所以本章主案例会重启。

### 12.2 一次重启不等于 CrashLoopBackOff

第 12 课的 start helper：

```text
kuberuntime_manager.go:1906-1939
```

只有旧 container 已退出，且 backoff key 在当前时间仍处于退避：

```go
if backOff.IsInBackOffSince(key, ts) {
    return true, message,
        NewBackoffError(
            ErrCrashLoopBackOff,
            ts.Add(backoff),
        )
}
```

所以：

```text
单次 liveness restart
  -> restartCount +1
  -> 不必出现 CrashLoopBackOff

新 container又快速失败，多轮累积退避
  -> start helper暂不重启
  -> Waiting reason=CrashLoopBackOff
```

---

## 13. PLEG：它观察 runtime 事实，不执行 probe

PLEG 全称 Pod Lifecycle Event Generator。首遍只记一句：

```text
probe问“应用接口现在健康吗”；
PLEG问“runtime里的 sandbox/container 状态变了吗”。
```

装配：

```text
pkg/kubelet/kubelet.go:859-892
```

GenericPLEG 与 probe managers 共享的是 Kubelet主流程，不共享同一张 cache：

```go
eventChannel :=
    make(chan *pleg.PodLifecycleEvent, plegChannelCapacity)

klet.pleg = pleg.NewGenericPLEG(
    logger,
    klet.containerRuntime,
    eventChannel,
    relistDuration,
    podCache,
    clock.RealClock{},
)

klet.runtimeState.addHealthCheck(
    "PLEG",
    klet.pleg.Healthy,
)
```

启动：

```text
Kubelet.Run
  -> statusManager.Start
  -> pleg.Start
  -> syncLoop
```

### 13.1 GenericPLEG 主链

```text
pkg/kubelet/pleg/generic.go:290-417
```

```text
Relist
  -> runtime.GetPods
  -> podRecords.setCurrent
  -> 对每个 UID reconcilePodRecord
  -> compare old/current container states
  -> generate ContainerStarted/ContainerDied/Removed...
  -> updateCache
       -> runtime.GetPodStatus
       -> podCache.Set
  -> podRecords.update
  -> eventChannel <- lifecycle event
```

最关键的顺序：

```text
先 updateCache
再发 lifecycle Event
```

源码明确先在 `reconcilePodRecord` 调 `updateCache`，成功后才向 channel 发 Event。这样 syncLoop 收到 `ContainerDied` 再叫 pod worker 时，worker更有机会拿到与事件一致的新 runtime PodStatus。

### 13.2 pod worker 等的就是这本 cache

```text
pkg/kubelet/pod_workers.go:1259-1280
```

```go
status, err =
    p.podCache.GetNewerThan(
        update.Options.Pod.UID,
        lastSyncTime,
    )
```

当前注释说明它最多等待新 PLEG refresh。第 12 课传给 runtime manager 的 `podStatus`，正是从这里来，不是每个 probe update 都在 pod worker里直接同步 RPC 查询 runtime。

### 13.3 两种方向不要画反

探针驱动：

```text
probe Failure
  -> SyncPod决定kill/start
  -> runtime状态改变
  -> PLEG观察结果、刷新cache、再发event
```

进程主动退出/OOM/runtime侧变化：

```text
runtime状态先改变
  -> PLEG发现 ContainerDied
  -> 刷新podCache并唤醒SyncPod
  -> runtime manager按restartPolicy收敛
```

所以 Java 进程主动 `System.exit`、OOMKilled 或 runtime 外部停止，不需要 probe 才能被发现。

---

## 14. PLEG 不健康为什么是节点级问题

健康检查：

```text
pkg/kubelet/pleg/generic.go:236-249
```

```go
relistTime := g.getRelistTime()
if relistTime.IsZero() {
    return false,
        fmt.Errorf("pleg has yet to be successful")
}

elapsed := g.clock.Since(relistTime)
if elapsed > g.relistDuration.RelistThreshold {
    return false,
        fmt.Errorf(
            "pleg was last seen active %v ago; threshold is %v",
            elapsed,
            g.relistDuration.RelistThreshold,
        )
}
```

Kubelet把 PLEG health 加入 `runtimeState`。主 syncLoop 每轮先检查：

```text
pkg/kubelet/kubelet.go:2644-2650
```

```go
if err := kl.runtimeState.runtimeErrors(); err != nil {
    logger.Error(err, "Skipping pod synchronization")
    time.Sleep(duration)
    duration = exponentialBackoff(duration)
    continue
}
```

因此：

```text
PLEG unhealthy
  -> runtime事实长期没被可靠刷新
  -> Kubelet暂停正常 Pod synchronization
  -> 影响可能覆盖整个 Node
```

不要把它归因成某个 `game-api` readiness URL 慢。反过来，单 Pod readiness Failure 也不等于 PLEG unhealthy。

### 14.1 生产证据

```text
日志：
  pleg has yet to be successful
  pleg was last seen active ...
  Skipping pod synchronization

指标：
  time() - kubelet_pleg_last_seen_seconds
  kubelet_pleg_relist_duration_seconds
  kubelet_pleg_relist_interval_seconds
  kubelet_pleg_discard_events
```

常见方向：

- CRI `GetPods/GetPodStatus` 卡顿；
- runtime 或节点 I/O/CPU严重异常；
- PLEG relist耗时过长；
- kubelet/runtime连接异常。

这里必须把两个容易混在一起的观测面拆开：

```text
最后一次成功 relist 长期不更新
  -> GenericPLEG.Healthy 按 lastRelistTime 判断 unhealthy
  -> runtimeState 出现 error
  -> 主 syncLoop 暂停正常 Pod synchronization

event channel 已满、discard 增加
  -> podCache 已经先完成更新
  -> 某次 lifecycle Event 没有投递给消费者
  -> 表示消费者背压或事件投递丢失
  -> 不会仅凭 discard 直接把 GenericPLEG health 置成 false
```

所以 `kubelet_pleg_discard_events` 可以与 PLEG Healthy 同时出现。排障时：

- 用 `kubelet_pleg_last_seen_seconds` 和 kubelet 的 health 日志判断“relist 是否陈旧”；
- 用 `kubelet_pleg_discard_events` 判断“lifecycle Event 投递是否有背压”；
- 不能看到 discard 就倒推出 kubelet 已进入 `PLEG unhealthy`。

不为了演示主链主动停 kubelet/containerd 或把节点压到 PLEG unhealthy；这会影响该 Node 所有 Pod。

### 14.2 EventedPLEG 当前边界

当前 commit 中 `EventedPLEG` 仍是 Alpha、默认关闭。启用时 GenericPLEG 仍参与 fallback/周期校验，但实现、timestamp竞态与 stream恢复更复杂。

首遍只读 GenericPLEG。看到目标生产版本启用了 EventedPLEG，再按该版本重新校准，不能把本章 Generic relist周期当作所有集群固定事实。

---

## 15. statusManager：本地 PodStatus 不等于 API 已写成功

### 15.1 本地版本先更新

`updateStatusInternal`：

```text
pkg/kubelet/status/status_manager.go:969-1011
```

```go
if isCached &&
    isPodStatusByKubeletEqual(
        &cachedStatus.status,
        &status,
    ) &&
    !forceUpdate {
    return false, nil
}

newStatus := versionedPodStatus{
    status:  status,
    version: cachedStatus.version + 1,
    // ...
}
m.podStatuses[pod.UID] = newStatus

select {
case m.podStatusChannel <- struct{}{}:
default:
    // 已有通知待处理
}
```

大白话：

1. 相同 status 不重复生成无意义版本；
2. 变化先写本地 cache，version +1；
3. 向 channel 发“有状态待同步”的通知；
4. channel只负责唤醒，真正最新值仍在 cache；通知已存在时不需要重复塞满。

### 15.2 一个 goroutine 同时处理即时通知和周期对账

```text
status_manager.go:267-294
```

```go
go wait.Forever(func() {
    for {
        select {
        case <-m.podStatusChannel:
            m.syncBatch(ctx, false)
        case <-syncTicker:
            m.syncBatch(ctx, true)
        }
    }
}, 0)
```

源码注释明确让 `syncPod` 与 `syncBatch` 共享同一个 goroutine，避免 status sync race。

### 15.3 写 API 前还要防同名新 Pod

```text
status_manager.go:1150-1207
```

```text
GET 当前 Pod
  -> TranslatePodUID
  -> 若同名对象 UID 已变，丢弃旧 UID status
  -> mergePodStatus
  -> PatchPodStatus
  -> 成功后记录 apiStatusVersions
```

这避免：

```text
旧 game-api-abc 的 status
误写到删除后新建的同名 game-api-abc
```

Deployment通常生成不同后缀，但静态 Pod、手工同名重建或快速测试仍要靠 UID 做身份。

### 15.4 为什么 kubectl会有短暂延迟

```text
probe/PLEG事实改变
  -> Kubelet本地cache改变
  -> status notification
  -> statusManager GET/merge/PATCH
  -> apiserver持久化
  -> kubectl下一次GET/watch看到
```

中间任何 API timeout、冲突、网络延迟或批处理都会造成短暂时差。`kubelet_pod_status_sync_duration_seconds` 记录从本地 status首个待同步时间到 server更新的传播耗时，但它是 Node聚合指标，不直接给某个 Pod 定位。

---

## 16. 两条完整时间线

### 16.1 readiness 失败

```text
t0  game-api Running / Ready=True

t1  第一次实际 readiness Failure
    -> prober记录 Unhealthy Event
    -> resultRun=1
    -> 若 failureThreshold=2，缓存仍是Success
    -> API Ready仍可能True

t2  连续第二次 Failure
    -> resultRun达到2
    -> readinessManager.Set(Failure)
    -> 缓存从Success变Failure，发Update

t3  syncLoop读取readiness Update
    -> statusManager.SetContainerReadiness(false)
    -> 重算ContainersReady/PodReady
    -> HandlePodSyncs

t4  statusManager patch API
    -> phase仍Running
    -> Ready=False
    -> restartCount不变

t5  EndpointSlice controller读取Pod Ready
    -> endpoint通常仍存在
    -> conditions.ready=false
```

### 16.2 liveness 失败并重启

```text
t0  game-api containerID=A / restartCount=0

t1  第一次liveness Failure
    -> Unhealthy Event
    -> 未达threshold，不kill

t2  达到failureThreshold
    -> livenessManager缓存=Failure
    -> syncLoop -> HandlePodSyncs

t3  podWorkerLoop
    -> 从PLEG podCache取runtime status
    -> Kubelet.SyncPod
    -> runtime manager读到A的liveness Failure
    -> ContainersToKill[A]
    -> Deployment Pod restartPolicy=Always
    -> ContainersToStart[game-api]

t4  runtime kill A
    -> start helper检查backoff
    -> Create/Start container B

t5  PLEG观察 A Died / B Started
    -> 先刷新podCache
    -> 再发lifecycle Event
    -> 再次唤醒SyncPod

t6  statusManager
    -> Pod UID不变
    -> containerID=B
    -> restartCount=1
    -> lastState.terminated保存A的退出信息
```

快速切换时，`kubectl get pod -w` 可能来不及展示一个短暂的当前 `Terminated` state；`lastState`、`restartCount`、container ID 和 `kubectl logs --previous` 更可靠。

---

## 17. 生产排障：不要只问“探针为什么失败”

### 17.1 先固定四个身份字段

```powershell
$ns = 'game'
$pod = 'game-api-new-x'
$container = 'game-api'

$uid = kubectl get pod $pod -n $ns -o jsonpath='{.metadata.uid}'
$node = kubectl get pod $pod -n $ns -o jsonpath='{.spec.nodeName}'
$containerID = kubectl get pod $pod -n $ns -o jsonpath="{.status.containerStatuses[?(@.name=='$container')].containerID}"
$restartCount = kubectl get pod $pod -n $ns -o jsonpath="{.status.containerStatuses[?(@.name=='$container')].restartCount}"

"UID=$uid"
"NODE=$node"
"CONTAINER_ID=$containerID"
"RESTART_COUNT=$restartCount"
```

然后定向看：

```powershell
kubectl get pod $pod -n $ns -o jsonpath='{.status.phase}{"\n"}{range .status.conditions[*]}{.type}{"="}{.status}{" reason="}{.reason}{" message="}{.message}{"\n"}{end}{range .status.containerStatuses[*]}{.name}{" ready="}{.ready}{" started="}{.started}{" restart="}{.restartCount}{" id="}{.containerID}{" state="}{.state}{" lastState="}{.lastState}{"\n"}{end}'

kubectl get events -n $ns `
  --field-selector "involvedObject.uid=$uid" `
  --sort-by=.metadata.creationTimestamp
```

完整 Event message、Pod spec、应用日志可能含 URL、租户、内部地址、环境变量或业务数据；外发前脱敏。探针命令输出也可能泄露应用返回内容。

### 17.2 EndpointSlice 按 Pod UID 对齐

```powershell
$service = 'game-api'
$sliceJson = kubectl get endpointslice -n $ns `
  -l "kubernetes.io/service-name=$service" -o json |
  ConvertFrom-Json

$sliceJson.items | ForEach-Object { $_.endpoints } |
  Where-Object { $_.targetRef.uid -eq $uid } |
  Select-Object addresses,conditions,targetRef,nodeName
```

不要仅按 IP 猜：Pod重建可能复用/更换 IP，UID 才是当前身份。

### 17.3 日志与 previous

```powershell
kubectl logs $pod -n $ns -c $container --since=15m --timestamps
kubectl logs $pod -n $ns -c $container --previous --timestamps
```

- 当前日志看 container B；
- `--previous` 看同一 Pod、同一 container name 的上一实例 A；
- Pod被 Deployment 删除重建后，旧 Pod日志不再通过新 UID 的 `--previous` 自动继承。

### 17.4 证据矩阵

| 现场 | 最可能机制 | 不要误判 |
|---|---|---|
| Running、Ready=False、container ID/restartCount不变 | readiness状态链或 readiness gate | 不是 liveness重启 |
| 第一条 Unhealthy，Ready暂未变 | 未达threshold、Event聚合/传播，或probe error未计数 | Event不等于稳定结果已变 |
| Unhealthy反复、UID不变、container ID变、restartCount+1 | kubelet在原 Pod内重启 container | 不是 Deployment新建 Pod |
| Pod UID改变、restartCount又从0开始 | controller删除/新建 Pod | 不是同一 Pod内 restart |
| 无probe Event，lastState=OOMKilled/exit code非0 | 进程/OOM/runtime先变，PLEG发现 | 不要求liveness参与 |
| 多 Pod一起卡、PLEG last seen超阈值、Skipping pod synchronization | Node runtime/PLEG级故障 | 不是单个 Java health endpoint |
| Ready=False，但 Service `publishNotReadyAddresses=true` | Endpoint ready可能仍被强制True | 不能用常规Service假设 |

---

## 18. 可复现实验：同一 Pod 先 NotReady，再 liveness restart

条件：

- 只在测试集群执行；
- 使用唯一 namespace；
- 不停 kubelet/containerd，不制造 PLEG unhealthy；
- 只删除容器自己的 `/health` 文件；
- 镜像使用当前源码 e2e 也在使用的 `registry.k8s.io/e2e-test-images/busybox:1.36.1-1`。

完整 PowerShell 实验：

```powershell
$context = kubectl config current-context
Write-Host "current-context = $context"
if ((Read-Host '确认是可销毁测试集群；输入 LAB13 继续') -ne 'LAB13') {
  throw '用户取消实验'
}

$stamp = Get-Date -Format 'yyyyMMddHHmmss'
$ns = "probe-lab-13-$stamp"
$owner = "chapter13-$stamp"
$createdNamespace = $false

try {
  kubectl create namespace $ns
  if ($LASTEXITCODE -ne 0) { throw '创建 namespace 失败' }
  $createdNamespace = $true
  kubectl label namespace $ns "studyowner=$owner"
  if ($LASTEXITCODE -ne 0) { throw '设置 owner label 失败' }

  $manifest = @'
apiVersion: v1
kind: Service
metadata:
  name: probe-lab
  namespace: __NAMESPACE__
spec:
  selector:
    app: probe-lab
  ports:
  - name: http
    port: 80
    targetPort: 8080
---
apiVersion: v1
kind: Pod
metadata:
  name: probe-lab
  namespace: __NAMESPACE__
  labels:
    app: probe-lab
spec:
  terminationGracePeriodSeconds: 1
  containers:
  - name: game-api
    image: registry.k8s.io/e2e-test-images/busybox:1.36.1-1
    command:
    - sh
    - -c
    - |
      mkdir -p /health
      echo "booting $(date)"
      sleep 15
      touch /health/startup /health/ready /health/live
      echo "health files ready $(date)"
      while true; do
        echo "game-api alive $(date)"
        sleep 5
      done
    resources:
      requests:
        cpu: 10m
        memory: 16Mi
    startupProbe:
      exec:
        command: ["sh", "-c", "test -f /health/startup"]
      periodSeconds: 2
      timeoutSeconds: 1
      failureThreshold: 15
    readinessProbe:
      exec:
        command: ["sh", "-c", "test -f /health/ready"]
      periodSeconds: 2
      timeoutSeconds: 1
      failureThreshold: 2
    livenessProbe:
      exec:
        command: ["sh", "-c", "test -f /health/live"]
      periodSeconds: 2
      timeoutSeconds: 1
      failureThreshold: 3
'@

  $manifest.Replace('__NAMESPACE__', $ns) | kubectl create -f -
  if ($LASTEXITCODE -ne 0) { throw '创建实验对象失败' }

  $uid = kubectl get pod probe-lab -n $ns -o jsonpath='{.metadata.uid}'
  $nodeDeadline = (Get-Date).AddSeconds(90)
  do {
    $node = kubectl get pod probe-lab -n $ns -o jsonpath='{.spec.nodeName}'
    if ($node) { break }
    Start-Sleep -Seconds 2
  } while ((Get-Date) -lt $nodeDeadline)
  if (-not $node) { throw 'Pod 未调度，先排除测试集群容量/taint问题' }

  Write-Host '观察启动前约24秒：startup尚未成功时，Started/Ready如何变化'
  $sawStartedFalse = $false
  for ($i = 0; $i -lt 12; $i++) {
    kubectl get pod probe-lab -n $ns `
      -o jsonpath='{.status.phase}{range .status.containerStatuses[*]}{" started="}{.started}{" ready="}{.ready}{" restart="}{.restartCount}{end}{"\n"}'
    if ($LASTEXITCODE -ne 0) { throw '读取启动窗口Pod状态失败' }
    $startedNow = kubectl get pod probe-lab -n $ns `
      -o jsonpath='{.status.containerStatuses[0].started}'
    if ($LASTEXITCODE -ne 0) { throw '读取container started字段失败' }
    if ($startedNow -eq 'false') { $sawStartedFalse = $true }
    Start-Sleep -Seconds 2
  }

  kubectl wait --for=condition=Ready pod/probe-lab -n $ns --timeout=120s
  if ($LASTEXITCODE -ne 0) { throw '基线 Pod 未 Ready' }

  $startupWindowEvents = kubectl get events -n $ns `
    --field-selector "involvedObject.uid=$uid,reason=Unhealthy" `
    -o json | ConvertFrom-Json
  if ($LASTEXITCODE -ne 0 -or $null -eq $startupWindowEvents) {
    throw '读取startup观察窗Event失败，不能把空结果当成门控证据'
  }
  $startupProbeEvents = @($startupWindowEvents.items | Where-Object {
    $_.message -match '(?i)startup'
  })
  $wrongProbeEvents = @($startupWindowEvents.items | Where-Object {
    $_.message -match '(?i)readiness|liveness'
  })
  if (-not $sawStartedFalse) {
    throw '观察窗内未看到Started=false，缺少startup门控的正向状态锚点'
  }
  if ($startupProbeEvents.Count -lt 1) {
    throw '观察窗内未看到startup probe失败Event，实验缺少正向失败锚点'
  }
  if ($wrongProbeEvents.Count -gt 0) {
    throw 'startup完成前观察窗出现readiness/liveness失败，门控证据被其他因素干扰'
  }

  $uidBefore = kubectl get pod probe-lab -n $ns -o jsonpath='{.metadata.uid}'
  $idBefore = kubectl get pod probe-lab -n $ns -o jsonpath='{.status.containerStatuses[0].containerID}'
  [int]$restartBefore = kubectl get pod probe-lab -n $ns -o jsonpath='{.status.containerStatuses[0].restartCount}'

  Read-Host '基线已Ready；按Enter删除readiness文件'
  kubectl exec probe-lab -n $ns -c game-api -- sh -c 'rm -f /health/ready'
  if ($LASTEXITCODE -ne 0) { throw '删除readiness文件失败' }

  kubectl wait --for=condition=Ready=false pod/probe-lab -n $ns --timeout=60s
  if ($LASTEXITCODE -ne 0) { throw 'Ready未按预期变False' }

  $phaseWhileUnready = kubectl get pod probe-lab -n $ns -o jsonpath='{.status.phase}'
  $idWhileUnready = kubectl get pod probe-lab -n $ns -o jsonpath='{.status.containerStatuses[0].containerID}'
  [int]$restartWhileUnready = kubectl get pod probe-lab -n $ns -o jsonpath='{.status.containerStatuses[0].restartCount}'
  if ($phaseWhileUnready -ne 'Running') { throw "NotReady时phase=$phaseWhileUnready，不符合本实验基线" }
  if ($idWhileUnready -ne $idBefore) { throw 'readiness失败期间container ID变化，不算纯readiness实验' }
  if ($restartWhileUnready -ne $restartBefore) { throw 'readiness失败触发了其他重启干扰' }

  $sliceDeadline = (Get-Date).AddSeconds(60)
  $endpoint = @()
  $allEndpointsNotReady = $false
  do {
    $sliceJson = kubectl get endpointslice -n $ns `
      -l 'kubernetes.io/service-name=probe-lab' -o json
    if ($LASTEXITCODE -ne 0 -or -not $sliceJson) {
      throw '读取EndpointSlice失败，不能把空结果当成readiness证据'
    }
    $sliceList = $sliceJson | ConvertFrom-Json
    $endpoint = @(
      $sliceList.items | ForEach-Object { $_.endpoints } |
      Where-Object { $_.targetRef.uid -eq $uidBefore }
    )
    $notReadyEndpoints = @(
      $endpoint | Where-Object {
        $_.conditions.ready -eq $false
      }
    )
    $allEndpointsNotReady = (
      $endpoint.Count -gt 0 -and
      $notReadyEndpoints.Count -eq $endpoint.Count
    )
    if ($allEndpointsNotReady) { break }
    Start-Sleep -Seconds 2
  } while ((Get-Date) -lt $sliceDeadline)
  if (-not $allEndpointsNotReady) {
    throw '该UID在全部EndpointSlice中的endpoint尚未全部变成ready=false'
  }

  Write-Host "PASS readiness: phase=$phaseWhileUnready restart=$restartWhileUnready endpoints=$($endpoint.Count) allReadyFalse=$allEndpointsNotReady"

  kubectl exec probe-lab -n $ns -c game-api -- sh -c 'touch /health/ready'
  kubectl wait --for=condition=Ready pod/probe-lab -n $ns --timeout=60s
  if ($LASTEXITCODE -ne 0) { throw '恢复readiness后未Ready' }

  Read-Host 'readiness已恢复；按Enter删除liveness文件'
  $livenessRemovedAt = (Get-Date).ToUniversalTime()
  kubectl exec probe-lab -n $ns -c game-api -- sh -c 'rm -f /health/live'
  if ($LASTEXITCODE -ne 0) { throw '删除liveness文件失败' }

  $unhealthyEvents = @()
  $eventDeadline = (Get-Date).AddSeconds(60)
  do {
    $eventJson = kubectl get events -n $ns `
      --field-selector "involvedObject.uid=$uidBefore,reason=Unhealthy" `
      -o json 2>$null
    if ($LASTEXITCODE -eq 0 -and $eventJson) {
      $eventList = $eventJson | ConvertFrom-Json
      $unhealthyEvents = @($eventList.items | Where-Object {
        $observedText = @(
          $_.series.lastObservedTime
          $_.lastTimestamp
          $_.eventTime
          $_.metadata.creationTimestamp
        ) | Where-Object { $_ } | Select-Object -First 1
        $observedAt = [datetime]::MinValue
        if ($observedText) {
          [datetime]::TryParse(
            $observedText,
            [ref]$observedAt
          ) | Out-Null
          $observedAt = $observedAt.ToUniversalTime()
        }
        $_.message -match '(?i)liveness' -and
        $observedAt -ge $livenessRemovedAt.AddSeconds(-2) -and
        (
          $_.source.host -eq $node -or
          $_.reportingInstance -eq $node
        )
      })
    }
    if ($unhealthyEvents.Count -gt 0) { break }
    Start-Sleep -Seconds 2
  } while ((Get-Date) -lt $eventDeadline)
  if ($unhealthyEvents.Count -lt 1) {
    throw '没有该UID且来自目标Node的liveness Unhealthy Event'
  }

  $restartDeadline = (Get-Date).AddSeconds(90)
  do {
    [int]$restartAfter = kubectl get pod probe-lab -n $ns `
      -o jsonpath='{.status.containerStatuses[0].restartCount}'
    if ($restartAfter -gt $restartBefore) { break }
    Start-Sleep -Seconds 2
  } while ((Get-Date) -lt $restartDeadline)
  if ($restartAfter -le $restartBefore) { throw 'liveness失败后restartCount未增加' }
  if ($restartAfter -ne ($restartBefore + 1)) {
    throw "restartCount从$restartBefore跳到$restartAfter，存在额外重启干扰"
  }

  kubectl wait --for=condition=Ready pod/probe-lab -n $ns --timeout=120s
  if ($LASTEXITCODE -ne 0) { throw '重启后新container未恢复Ready' }

  $uidAfter = kubectl get pod probe-lab -n $ns -o jsonpath='{.metadata.uid}'
  $idAfter = kubectl get pod probe-lab -n $ns -o jsonpath='{.status.containerStatuses[0].containerID}'
  if ($uidAfter -ne $uidBefore) { throw 'Pod UID变化，说明被重建，不是原Pod内container restart' }
  if ($idAfter -eq $idBefore) { throw 'container ID未变化，重启证据不完整' }

  kubectl get pod probe-lab -n $ns -o jsonpath='{.metadata.uid}{"\n"}{.spec.nodeName}{"\n"}{.status.phase}{"\n"}{range .status.containerStatuses[*]}{.name}{" id="}{.containerID}{" restart="}{.restartCount}{" state="}{.state}{" lastState="}{.lastState}{"\n"}{end}'
  kubectl get events -n $ns --field-selector "involvedObject.uid=$uidBefore" `
    -o custom-columns='FIRST:.firstTimestamp,LAST:.lastTimestamp,COUNT:.count,TYPE:.type,REASON:.reason,MESSAGE:.message'
  kubectl logs probe-lab -n $ns -c game-api --previous --timestamps

  Write-Host "PASS liveness: UID保持=$uidAfter，oldID=$idBefore，newID=$idAfter，restart=$restartAfter，removedAt=$livenessRemovedAt"
  Read-Host '完成观察后按Enter清理'
}
finally {
  if ($createdNamespace) {
    $actualOwner = kubectl get namespace $ns -o jsonpath='{.metadata.labels.studyowner}' 2>$null
    if ($actualOwner -eq $owner) {
      kubectl delete namespace $ns --wait=false
    } else {
      Write-Warning "owner label不匹配，未自动删除 $ns"
    }
  }
}
```

本实验用 `Started=false` 和 startup Failure Event 作为正向锚点，同时检查没有 readiness/liveness Failure；在这份受控配置与观察窗内，它支持下面的门控判断。其余断言则分别用 UID、container ID、restartCount 和 EndpointSlice做硬校验：

- startup未成功的观察窗内，readiness/liveness没有执行出失败结果，符合 worker门控源码；
- readiness Failure 让 `Running + Ready=False`，不改变 container ID/restartCount；
- EndpointSlice保留当前 UID endpoint，但 `ready=false`；
- liveness达到threshold后，Pod UID不变、container ID改变、restartCount增加；
- 新 container重新经过 startup/readiness 后恢复 Ready。

它不制造 PLEG unhealthy，也不能用单条 Event 的 count 精确还原每次 probe时间；Event可能聚合。

---

## 19. 指标与告警

### 19.1 probe

```promql
sum by (namespace, pod, container, probe_type, result) (
  rate(prober_probe_total{
    namespace="game",
    pod=~"game-api-.*"
  }[5m])
)

histogram_quantile(
  0.99,
  sum by (le, namespace, pod, container, probe_type) (
    rate(prober_probe_duration_seconds_bucket{
      namespace="game",
      pod=~"game-api-.*"
    }[5m])
  )
)
```

当前 `prober_probe_total` 还带 `pod_uid` label，适合单 Pod取证但可能有较高时序 churn；聚合告警应控制维度。`probe_duration_seconds` 当前只对源码记录的成功/Unknown路径观测，不能把它当所有失败请求耗时的完整直方图。

### 19.2 PLEG

```promql
time() - kubelet_pleg_last_seen_seconds

histogram_quantile(
  0.99,
  sum by (instance, le) (
    rate(kubelet_pleg_relist_duration_seconds_bucket[5m])
  )
)

rate(kubelet_pleg_discard_events[5m])
```

应按目标版本的 relist threshold、节点规模和历史基线定告警，不在讲义里硬编码一个适合所有集群的秒数。

### 19.3 status

```promql
histogram_quantile(
  0.99,
  sum by (instance, le) (
    rate(kubelet_pod_status_sync_duration_seconds_bucket[5m])
  )
)
```

该指标是 Node聚合，能提示 status传播变慢，不能单独说明某个 readiness探针逻辑错。

---

## 20. 节点资源管理边界：只画责任表，不塞进 PLEG

冻结标题保留“节点资源管理边界”，但本课不把 CPUManager、DeviceManager等误讲成 probe/PLEG内部步骤。

| 事实/动作 | 首要组件 | 是否属于 PLEG |
|---|---|---|
| Java HTTP/exec/gRPC健康 | prober | 否 |
| container Running/Exited/OOM事实 | CRI runtime + PLEG observation | PLEG负责观察 |
| Pod API status/Ready/restartCount上报 | Kubelet状态生成 + statusManager | 否 |
| Node RuntimeReady/NetworkReady | runtimeState + NodeStatus | 否 |
| CPU独占分配 | CPUManager | 否 |
| memory分配/NUMA hint | MemoryManager/TopologyManager | 否 |
| GPU资源分配与容器注入 | Device Plugin/DeviceManager/DRA | 否 |
| GPU Xid/ECC/温度/显存健康 | Driver/DCGM/设备插件策略 | 否 |

看到 `PLEG is not healthy` 不要跳到 GPU UUID；看到 `nvidia-smi` Xid 也不能说是 PLEG探测出来的。

---

## 21. GPU 短映射：推理容器更怕错误的 probe 策略

### 21.1 startup 要覆盖真实冷启动

GPU 推理服务可能经历：

```text
加载几十/几百GB模型权重
  -> host memory / page cache
  -> GPU显存分配
  -> CUDA context
  -> kernel/JIT编译
  -> graph capture
  -> warmup请求
  -> 才能稳定服务
```

若 liveness 在 startup完成前运行，可能形成：

```text
模型快加载完
  -> liveness误杀
  -> 重新加载
  -> 再误杀
  -> GPU冷启动风暴
```

因此 startup probe 的总窗口要来自模型大小、存储带宽、GPU型号和真实历史，而不是照抄 Java应用的30秒。

### 21.2 readiness失败不会释放 GPU

```text
readiness Failure
  -> Pod Ready=False
  -> 通常停止接普通Service流量
  -> container仍Running
  -> Pod仍在原Node
  -> GPU分配仍属于这个Pod
```

它可以用于“保留已加载模型、暂时摘流量”，但不能当作自动归还GPU给其他队列。

### 21.3 liveness restart 的成本更高

liveness kill/restart会重新进入第12课 container流程和应用冷启动。即使 Pod UID不变：

- CUDA context会重建；
- 模型通常重新加载；
- 显存重新分配；
- readiness要重新恢复；
- 设备注入/checkpoint怎样保证同一分配留到第15～17课。

因此 GPU服务 liveness 应检测“不可自愈的进程坏死”，不要把短时模型队列拥塞、单次推理超时直接等价成必须重启。

### 21.4 PLEG 不懂 Xid/ECC

PLEG只观察 runtime container state。它不知道：

- GPU Xid；
- ECC错误；
- 温度/功耗；
- 显存碎片；
- NVLink/NCCL状态；
- token吞吐与P99延迟。

这些由 driver、DCGM、device plugin策略与应用指标补齐。DCGM告警是否联动 Pod迁移/Node隔离，不是 PLEG内置行为。

---

## 22. 本章 Go 语法复习索引

会挡住主链的语法已放在首次源码旁；这里集中复习。

| 写法 | 本章大白话 | 首遍优先级 |
|---|---|---|
| `go w.run(ctx)` | 并发启动长期 probe loop | 必须 |
| `switch probeType` | 按三种 probe选择配置 | 必须 |
| `result, ok := m.Get(id)` | 同时取缓存值与是否存在 | 必须 |
| `resultRun++` | 连续同结果次数加一 | 必须 |
| `status := *old.DeepCopy()` | 深拷贝后解引用为值，避免改共享对象 | 必须 |
| `case update := <-ch` | 从某个结果channel收到update | 第11课复用 |
| `select { case ch <- x: default: }` | 通知已挂起时不阻塞重复发送 | 二读 |
| `defer m.Unlock()` | 函数退出前一定解锁 | 二读 |
| `val.(time.Time)` | type assertion，把interface值断言为time.Time | 二读 |

### 22.1 threshold 条件怎样读

```go
if (result == Failure &&
    resultRun < FailureThreshold) ||
   (result == Success &&
    resultRun < SuccessThreshold) {
    return true
}
```

先分两组：

```text
Failure 且还没达到 FailureThreshold
或
Success 且还没达到 SuccessThreshold
```

满足任一组都只继续probe，不更新稳定缓存。

这段 worker代码为了复用而同时写了 Success/Failure 两边；配置层还要再套 API validation：

```text
liveness/startup successThreshold只能是1
readiness successThreshold才允许大于1
```

所以首遍读源码时不要误推导出“liveness可以配置连续3次成功才算恢复”。

### 22.2 `select` 通知为什么允许丢“次数”

statusManager：

```go
select {
case podStatusChannel <- struct{}{}:
default:
}
```

channel只表达“cache里有新状态待处理”，最新状态保存在 `podStatuses` map。若已有一个通知待消费，再发十个相同唤醒没有意义；worker被唤醒后会扫描版本。

这与“丢失状态数据”不同：

```text
payload在cache
channel只是doorbell
```

---

## 23. 本章必须掌握、二读与可以略过

### 23.1 首遍必须掌握

- probe manager按 Pod UID、container name、probe type 建 worker；
- readiness/liveness/startup初始值为什么不同；
- startup怎样门控另外两种 probe；
- `Unhealthy` Event 与 threshold达成不是同一事实；
- probe error可能记录Event但不计入threshold；
- result manager只有稳定结果变化才通知syncLoop；
- readiness Failure改Ready状态，不直接kill；
- phase Running不读取readiness；
- EndpointSlice通常保留endpoint并设`ready=false`；
- liveness/startup worker不直接调CRI kill；
- Failure怎样经SyncPod进入ContainersToKill/ToStart；
- restartPolicy仍参与重启决定；
- 一次restart不等于CrashLoopBackOff；
- PLEG先刷新runtime podCache，再发lifecycle Event；
- probe驱动与runtime先变两条方向；
- PLEG unhealthy为何是Node级同步问题；
- statusManager本地cache与API状态存在传播时间；
- Pod UID、container ID、restartCount、lastState的关系。

### 23.2 二读再掌握

- kubelet重启时probe初始状态兼容；
- restartable init container probe；
- worker manual trigger/onHold；
- probe exec/HTTP/TCP/gRPC具体执行器；
- PostStop/ContainerRestartRules等特殊restart分支；
- GenericPLEG reinspection、event discard、timestamp；
- EventedPLEG stream/fallback；
- statusManager static/mirror Pod UID翻译；
- status三方merge、删除安全、API版本账；
- EndpointSlice完整reconcile与terminating/serving。

### 23.3 可以一笔带过

- HTTP prober transport所有参数；
- probe cache锁的每个实现；
- 每个PLEG metric bucket；
- statusManager全部边缘condition；
- NodeLease/完整NodeStatus；
- CPUManager/MemoryManager/TopologyManager内部；
- DeviceManager、DRA和GPU健康策略。

---

## 24. 本章验收题

### 24.1 首遍必须答出的 12 题

1. 为什么 `Running` 与 `Ready=False` 可以同时成立？对应哪个phase判断？
2. 第一条 `Readiness probe failed` 为什么不保证Ready已经False？
3. 第一条 `Liveness probe failed` 为什么不保证restartCount已经增加？
4. `probe failed` 与 `probe errored` 对threshold计数有什么差别？
5. startup尚未成功时，readiness/liveness worker在哪里被门控？
6. readiness Failure从result cache到EndpointSlice要经过哪些组件？
7. liveness worker是否直接调用CRI kill？真正kill决定在哪里做？
8. Deployment Pod为什么通常会在liveness Failure后重启？
9. 一次container restart为什么不等于CrashLoopBackOff？
10. PLEG在probe驱动重启中发生在kill决定前还是runtime状态变化后？
11. Java进程主动退出时，为什么没有probe也能被发现？
12. probe cache、PLEG podCache、statusManager cache分别保存什么？

### 24.2 二读源码反查 10 题

1. readiness/liveness/startup三个initialValue分别是什么？
2. worker为什么用container ID做result key？
3. result manager为什么只在`prev != result`时发Update？
4. `SetContainerReadiness`为什么先DeepCopy？
5. EndpointSlice的`ready/serving/terminating`怎样计算？
6. runtime manager怎样同时填`ContainersToKill`和`ContainersToStart`？
7. `doBackOff`用什么时间点和key判断CrashLoopBackOff？
8. GenericPLEG为什么必须先updateCache再发Event？
9. pod worker为什么用`GetNewerThan(lastSyncTime)`？
10. statusManager怎样防止旧UID status写到同名新Pod？

### 24.3 现场辨析题

现场A：

```text
UID不变
phase=Running
Ready=False
containerID不变
restartCount=0
readiness Unhealthy
```

结论：

```text
readiness状态链；
先查依赖/探针与Endpoint ready，
不能说container已重启。
```

现场B：

```text
UID不变
old containerID=A
new containerID=B
restartCount 3 -> 4
lastState.terminated.reason=Error
liveness Unhealthy
```

结论：

```text
原Pod内container重启；
用previous日志查A；
继续判断是否进入CrashLoopBackOff。
```

现场C：

```text
多个Pod状态长期不刷新
time()-pleg_last_seen_seconds持续增长
kubelet: Skipping pod synchronization
```

结论：

```text
Node runtime/PLEG健康链；
不是逐个修改Java readiness URL。
```

---

## 25. 当前源码断点

```text
本章已讲：

pkg/kubelet/kubelet.go
  -> 三个probe result manager、podCache、statusManager创建:687-696
  -> PLEG装配与health check:859-892
  -> statusManager/PLEG启动:1947-1961
  -> Kubelet.SyncPod注册probe:2213-2214
  -> syncLoop PLEG/probe分支:2733-2779
  -> handleProbeSync:2817-2828
  -> HandlePodSyncs:3186-3210

pkg/kubelet/prober/prober_manager.go
  -> AddPod创建workers:185-230
  -> UpdatePodStatus读probe cache:332-374

pkg/kubelet/prober/worker.go
  -> 三种initialValue:100-154
  -> run loop:157-202
  -> 新container ID/初始cache:250-287
  -> startup/initial delay门控:330-346
  -> 执行probe、threshold、Set:348-393

pkg/kubelet/prober/prober.go
  -> probe Event在threshold之前:102-128

pkg/kubelet/prober/results/results_manager.go
  -> Get/Set/只在变化时Update:106-138

pkg/kubelet/status/status_manager.go
  -> Start即时/周期sync:267-294
  -> SetContainerReadiness:490-557
  -> 本地version/cache/channel:969-1011
  -> syncBatch选择版本:1073-1148
  -> syncPod GET/UID保护/PatchPodStatus:1150-1207

pkg/kubelet/kubelet_pods.go
  -> generateAPIPodStatus与probe cache:1888-约2000
  -> getPhase Running判断:1809-1824

pkg/kubelet/kuberuntime/kuberuntime_manager.go
  -> 读取liveness/startup并计划kill/start:1327-1374
  -> 执行ContainersToKill:1486-1496
  -> start helper/startContainer:1687-1712,1790-1793
  -> doBackOff/CrashLoopBackOff:1906-1939

pkg/kubelet/pleg/generic.go
  -> Healthy:236-249
  -> Relist/GetPods:290-330
  -> updateCache后发Event:332-417
  -> computeEvents:480-489
  -> GetPodStatus/podCache.Set:516-566

pkg/kubelet/pod_workers.go
  -> podCache.GetNewerThan:1259-1280

staging/src/k8s.io/endpointslice/utils.go
  -> Pod Ready到Endpoint conditions:37-50
```

---

## 26. 下一章前的收口

到这里，平台 Java 主线已经从：

```text
Deployment
  -> scheduler
  -> kubelet接单
  -> PodSandbox/CRI/container
  -> probe/PLEG/status
```

完整走通。

下一章开始正式把主案例切到 GPU Node，但不是立刻读 Device Plugin：

```text
第14章 NVIDIA节点栈
  Driver
  CUDA用户态
  NVIDIA Container Toolkit
  containerd/OCI/CDI

第15～17章
  Device Plugin注册与ListAndWatch
  DeviceManager Allocate/容器注入
  checkpoint/PodResources/CDI
```

这样安排是为了先回答：

```text
“没有Kubernetes，宿主机和普通容器能不能正确使用GPU？”
```

只有节点栈基线正常，后面 `nvidia.com/gpu` 上报、Allocate与K8s注入才有可解释的底座。
