# 第 15 课：Device Plugin——注册、ListAndWatch 与 Node Capacity/Allocatable

> 主案例：GPU Node 的 Driver、Toolkit、containerd/CDI都正常，NVIDIA Device Plugin Pod也是 Running，但 Node 没有 `nvidia.com/gpu`  
> 主线源码：`pkg/kubelet/cm/devicemanager/plugin/v1beta1/*`、`pkg/kubelet/pluginmanager/*`、`pkg/kubelet/cm/devicemanager/manager.go`、`pkg/kubelet/nodestatus/setters.go`  
> 源码基线：`301946d15e67a4a2e8a5fb8292eb836acd366d78`（`v1.37.0-alpha.0-280-g301946d15e6`）  
> 本课深度：S3，读穿注册成功、第一份设备清单、健康账本、Node status异步传播和断连收敛  
> 前置断点：第 14 课已经证明宿主机GPU底座与容器runtime责任边界，本课从“设备插件进程怎样把GPU变成Kubernetes资源”继续

---

## 0. 这一课不再把“Device Plugin Pod Running”当作结论

生产现场：

```text
gpu-node-07:
  Node Ready=True
  host nvidia-smi -L成功
  nvidia-ctk cdi list有设备
  nvidia-device-plugin Pod Running/Ready

但：
  Node.status.capacity["nvidia.com/gpu"] 不存在或为0
  Node.status.allocatable["nvidia.com/gpu"] 不存在或为0
  GPU业务Pod FailedScheduling: Insufficient nvidia.com/gpu
```

很多排障到这里会说：

> 插件Pod明明是Running，应该是scheduler缓存有问题。

这仍然跳得太快。一个 Device Plugin Pod至少要经过：

```text
进程启动
  -> 发现宿主机设备
  -> 监听自己的Unix socket
  -> 向kubelet注册
  -> kubelet反向连接插件
  -> GetDevicePluginOptions成功
  -> ListAndWatch建流成功
  -> 第一份完整设备清单到达
  -> DeviceManager重建healthy/unhealthy账本
  -> kubelet下一轮生成Node Status
  -> Patch到apiserver
  -> scheduler informer看到新Node对象
```

Pod `phase=Running` 最多证明容器进程仍在。它不证明后面十步都成功。

生产取证按同一个Node、同一个时间窗过六道闸门：

| 闸门 | 最小证据 | 还不能证明 |
|---|---|---|
| Plugin Pod身份 | namespace/name/UID/image/containerID/`spec.nodeName` | socket已监听 |
| 入口判定 | direct `Register`日志，或generic watcher/GetInfo日志与socket路径 | 两套入口都必须串行经过 |
| socket/listener | 目标Node实际socket、owner/mode、监听进程 | kubelet已连接 |
| registration/connect | 字段校验、dial、GetOptions成功日志 | first list已处理 |
| first ListAndWatch | 同resource的首份完整清单、total/healthy与stream错误 | Node API已patch |
| Node/scheduler | Capacity/Allocatable、resourceVersion、FailedScheduling时间 | 具体device ID已Allocate给container |

任何一步使用了另一个Node的Pod或日志，整条因果链都无效。

本课还要先过一条版本闸门：它讲的是**传统 Device Plugin扩展资源账本**。当前commit的`DRAExtendedResource`可以让`DeviceClass.spec.extendedResourceName`接管同名extended resource；此时相同的`limits: nvidia.com/gpu: 1`可能转成DRA claim。进入本课主链前先确认没有匹配的DeviceClass，并检查Pod `status.extendedResourceClaimStatus`；若由DRA接管，应改查DeviceClass/ResourceSlice/ResourceClaim，而不是要求DeviceManager一定生成同名Node Capacity。

---

## 1. 本课先钉死六个结论

1. **当前源码同时存在两套 Device Plugin注册入口**，不能把其中一套说成所有插件唯一真相。
2. **注册成功不等于资源已经出现**；至少还要等第一份 `ListAndWatchResponse`。
3. `ListAndWatch` 是 gRPC server-stream，不是 Kubernetes Watch，也不是固定周期心跳。
4. 每份 response是该resource的**完整设备清单**，DeviceManager据此重建三张表。
5. `Capacity=healthy+unhealthy`，`Allocatable=healthy`；计数单位是插件逻辑device entry，且Allocatable不是“当前剩余未分配GPU”。
6. Device Plugin容量变化走 Node Status，不走 Node Lease，也不是直接同步通知scheduler。

把这六句掌握后，你才能解释：

```text
为什么在“无MIG/无sharing、一entry一整卡”的例子里，物理8卡、Node Allocatable仍是8，但只剩2个资源单位可继续调度；
为什么一个entry变Unhealthy时Capacity可能仍是8、Allocatable变7；
为什么插件刚断连时Allocatable先归0，Capacity过一段时间才归0；
为什么kubectl Events里没有“Device Plugin注册失败”也不能证明注册正常。
```

---

## 2. 先分清四个角色和两组 gRPC API

| 角色 | 运行位置 | 主要职责 |
|---|---|---|
| NVIDIA Device Plugin | 每个GPU Node上的DaemonSet Pod/进程 | 发现GPU、报告device ID/health、响应Allocate等RPC |
| kubelet plugin manager | kubelet进程 | 通用发现registration socket并按plugin type分发 |
| kubelet DeviceManager | kubelet进程 | 保存设备账、形成capacity/allocatable、后续分配具体device |
| Node status/scheduler | apiserver对象与scheduler缓存 | 暴露数量并按requests做调度 |

两组API：

### 2.1 Device Plugin专用 API

```text
staging/src/k8s.io/kubelet/pkg/apis/deviceplugin/v1beta1/api.proto
```

方向：

```text
插件 -> kubelet Registration.Register
kubelet -> 插件 GetDevicePluginOptions
kubelet -> 插件 ListAndWatch
kubelet -> 插件 Allocate / PreStartContainer / GetPreferredAllocation
```

本课读前两段和 `ListAndWatch`；Allocate留到第 16 课。

### 2.2 通用 plugin registration API

```text
staging/src/k8s.io/kubelet/pkg/apis/pluginregistration/v1/api.proto
```

方向：

```text
kubelet -> registration socket GetInfo
kubelet -> registration socket NotifyRegistrationStatus
```

它还服务CSI、DRA等插件。看到 `plugins_registry` socket时，不能只凭路径就认定是GPU Device Plugin。

---

## 3. 当前 kubelet为什么会同时有两套入口

### 3.1 通用 plugin manager在构造时就建立

```text
pkg/kubelet/kubelet.go:1038-1042
```

```go
klet.pluginManager = pluginmanager.NewPluginManager(
    klet.getPluginsRegistrationDir(),
    kubeDeps.Recorder,
)
```

registration目录：

```text
pkg/kubelet/kubelet_getters.go:76-84

<kubelet root>/plugins_registry
```

默认root通常是 `/var/lib/kubelet`，但发行版或托管平台可能改 `--root-dir`；不能永远硬编码。

### 3.2 DeviceManager自己仍启动专用registration server

kubelet启动container manager：

```text
pkg/kubelet/kubelet.go:1817-1825
```

DeviceManager：

```text
pkg/kubelet/cm/devicemanager/manager.go:340-355
```

```go
func (m *ManagerImpl) Start(...) error {
    logger.V(2).Info("Starting Device Plugin manager")

    err := m.readCheckpoint(logger)
    if err != nil {
        logger.Error(
            err,
            "Continue after failing to read checkpoint file...",
        )
    }

    return m.server.Start(logger)
}
```

`m.server.Start`监听传统socket：

```text
/var/lib/kubelet/device-plugins/kubelet.sock
```

### 3.3 同时又把DeviceManager挂到通用plugin manager

```text
pkg/kubelet/cm/container_manager_linux.go:736-744
```

```go
func (cm *containerManagerImpl) GetPluginRegistrationHandlers() map[string]cache.PluginHandler {

    res := map[string]cache.PluginHandler{
        pluginwatcherapi.DevicePlugin:
            cm.deviceManager.GetWatcherHandler(),
    }
    // DRA feature gate开启时还会加入DRAPlugin
    return res
}
```

kubelet随后：

```text
pkg/kubelet/kubelet.go:1830-1840
```

```go
for name, handler :=
    range kl.containerManager.
        GetPluginRegistrationHandlers() {
    kl.pluginManager.AddHandler(name, handler)
}

go kl.pluginManager.Run(
    ctx,
    kl.sourcesReady,
    wait.NeverStop,
)
```

所以当前commit的真实图是：

```text
入口A：device-plugins/kubelet.sock
  -> Device Plugin专用Registration.Register

入口B：plugins_registry/<plugin>.sock
  -> 通用GetInfo/NotifyRegistrationStatus

两条入口
  -> 最终都交给DeviceManager server/connectClient
```

不能写：

```text
“支持plugin watcher后，direct Register已经完全删除”
```

也不能反过来写：

```text
“Device Plugin永远只会调用kubelet.sock”
```

实际 NVIDIA Device Plugin、Operator打包版本或其他厂商插件走哪条，要看它的实现、挂载、socket和日志。NVIDIA官方插件的常见部署仍会挂载 `/var/lib/kubelet/device-plugins`并向专用socket注册，但本课不把这个实现习惯升级成Kubernetes协议唯一入口。

---

## 4. 入口 A：传统 direct `Registration.Register`

### 4.1 kubelet先监听 `kubelet.sock`

```text
pkg/kubelet/cm/devicemanager/plugin/v1beta1/server.go:86-133
```

核心动作：

```go
os.MkdirAll(s.socketDir, 0750)
s.rhandler.CleanupPluginDirectory(
    logger,
    s.socketDir,
)

ln, err := net.Listen(
    "unix",
    s.SocketPath(),
)

s.grpc = grpc.NewServer()
api.RegisterRegistrationServer(
    s.grpc,
    s,
)
go s.grpc.Serve(ln)
```

路径常量：

```text
staging/src/k8s.io/kubelet/pkg/apis/deviceplugin/v1beta1/constants.go

DevicePluginPath = /var/lib/kubelet/device-plugins/
KubeletSocket    = /var/lib/kubelet/device-plugins/kubelet.sock
```

这里要和通用目录严格分开：当前 upstream Linux 把 direct Device Plugin目录和`kubelet.sock`写成上述绝对常量，普通`--root-dir`**不会**把它们移动到新root。发行版源码补丁或容器化kubelet的mount namespace可能让“宿主机看到的路径”不同，但那不是`--root-dir`本身的效果；要用源码版本、进程mount和DaemonSet hostPath交叉确认。

### 4.2 插件调用Register

```text
pkg/kubelet/cm/devicemanager/plugin/v1beta1/server.go:159-185
```

```go
func (s *server) Register(
    ctx context.Context,
    r *api.RegisterRequest,
) (*api.Empty, error) {
    logger := klog.FromContext(ctx)
    logger.Info(
        "Got registration request from device plugin with resource",
        "resourceName",
        r.ResourceName,
    )

    metrics.DevicePluginRegistrationCount.
        WithLabelValues(r.ResourceName).Inc()

    if !s.isVersionCompatibleWithPlugin(r.Version) {
        err := fmt.Errorf(
            errUnsupportedVersion,
            r.Version,
            api.SupportedVersions,
        )
        return &api.Empty{}, err
    }

    if !v1helper.IsExtendedResourceName(
        core.ResourceName(r.ResourceName),
    ) {
        err := fmt.Errorf(
            errInvalidResourceName,
            r.ResourceName,
        )
        return &api.Empty{}, err
    }

    if err := s.connectClient(
        ctx,
        r.ResourceName,
        filepath.Join(s.socketDir, r.Endpoint),
    ); err != nil {
        return &api.Empty{}, err
    }
    return &api.Empty{}, nil
}
```

Go现场补——第一个方法签名现在就读懂：

- `(s *server)`是receiver：这不是独立函数，而是`*server`对象的方法；`s`让方法访问socket目录、client表等状态；
- `ctx context.Context`按值传递取消、超时和日志上下文；
- `r *api.RegisterRequest`是请求指针，读取`Version/Endpoint/ResourceName`；
- `(*api.Empty, error)`表示两个返回值：空响应指针和错误；gRPC调用成功是`&api.Empty{}, nil`，失败返回非`nil error`；
- `if err := ...; err != nil`把`err`限制在这个`if`作用域，不能拿作用域外不存在的`err`凑一个删节摘录。

这条入口的关键字段：

| 字段 | 示例 | 作用 |
|---|---|---|
| `Version` | `v1beta1` | 必须与kubelet支持版本兼容 |
| `Endpoint` | `nvidia-gpu.sock` | 插件自己的DevicePlugin socket文件名 |
| `ResourceName` | `nvidia.com/gpu` | Node status里的extended resource name |
| `Options` | DevicePluginOptions | 当前 `server.Register`没有直接消费 |

容易误讲的点：

> `RegisterRequest.Options` 并不是当前DeviceManager最终选项来源。

`connectClient`成功后，kubelet还会主动调用插件的 `GetDevicePluginOptions`。本commit最终保存的是那个RPC返回值。

### 4.3 direct路径会拼接socket目录

```go
filepath.Join(s.socketDir, r.Endpoint)
```

这意味着插件传的是endpoint名，由kubelet拼成专用目录下路径。

若插件把绝对路径、跨目录路径或错误文件名当作普通endpoint，实际行为和安全边界必须按 `filepath.Join`及平台路径规则重新核对，不能只看Register请求打印值。

---

## 5. 入口 B：通用 plugin watcher

### 5.1 watcher看到Unix socket只先写desired state

```text
pkg/kubelet/pluginmanager/pluginwatcher/plugin_watcher.go:163-210
```

```go
func (w *Watcher) handleCreateEvent(
    ctx context.Context,
    event fsnotify.Event,
) error {
    fi, err := getStat(event)
    // ...
    if !fi.IsDir() {
        isSocket, err :=
            util.IsUnixDomainSocket(
                util.NormalizePath(event.Name),
            )
        if !isSocket {
            return nil
        }
        return w.handlePluginRegistration(
            ctx,
            event.Name,
        )
    }
    return w.traversePluginDir(
        ctx,
        event.Name,
    )
}

func (w *Watcher) handlePluginRegistration(
    ctx context.Context,
    socketPath string,
) error {
    return w.desiredStateOfWorld.
        AddOrUpdatePlugin(
            ctx,
            getSocketPath(socketPath),
        )
}
```

这还是：

```text
“期望存在这个plugin”
```

不是：

```text
“plugin已经注册成功”
```

后续由 reconciler把 desired state和actual state调到一致。

### 5.2 operation executor反向问插件 `GetInfo`

```text
pkg/kubelet/pluginmanager/operationexecutor/operation_generator.go:78-156
```

主线：

```go
client, conn, err :=
    dial(ctx, socketPath, dialTimeoutDuration)
if err != nil {
    return fmt.Errorf(
        "RegisterPlugin error -- dial failed at " +
            "socket %s, err: %v",
        socketPath,
        err,
    )
}
defer conn.Close()

ctxWithTimeout, cancel :=
    context.WithTimeout(ctx, time.Second)
defer cancel()

infoResp, err :=
    client.GetInfo(
        ctxWithTimeout,
        &registerapi.InfoRequest{},
    )
if err != nil {
    return fmt.Errorf(
        "RegisterPlugin error -- failed to get plugin " +
            "info using RPC GetInfo at socket %s, err: %v",
        socketPath,
        err,
    )
}

handler, ok :=
    pluginHandlers[infoResp.Type]
if !ok {
    if err := og.notifyPlugin(ctx, client, false,
        fmt.Sprintf(
            "RegisterPlugin error -- no handler registered " +
                "for plugin type: %s at socket %s",
            infoResp.Type,
            socketPath,
        )); err != nil {
        return fmt.Errorf(
            "RegisterPlugin error -- failed to send error at " +
                "socket %s, err: %v",
            socketPath, err,
        )
    }
    return fmt.Errorf(
        "RegisterPlugin error -- no handler registered for " +
            "plugin type: %s at socket %s",
        infoResp.Type, socketPath,
    )
}

if infoResp.Endpoint == "" {
    infoResp.Endpoint = socketPath
}

err = handler.ValidatePlugin(
    infoResp.Name,
    infoResp.Endpoint,
    infoResp.SupportedVersions,
)
```

Go现场补——`context.WithTimeout`与 `defer cancel()`：

- dial失败先early return；成功后`defer conn.Close()`保证这次generic registration操作结束时关闭临时GetInfo/Notify连接；
- 派生一个最多活1秒的context；
- `cancel`释放timer等资源；
- `defer`保证当前函数返回时执行；
- GetInfo卡死因此不会无限阻塞registration operation。

Go现场补——`handler, ok := pluginHandlers[key]`：

- Go map读取可以同时返回value和 `ok bool`；
- `ok=false`表示key不存在；
- 这里不是handler执行业务后报错，而是kubelet根本没为这个plugin type注册消费者；
- map的value是interface，CSI、DevicePlugin、DRA都可用同一组 `Validate/Register/DeRegister`方法被通用manager调度。

时间边界：

```text
dial registration socket：10秒
GetInfo：1秒
NotifyRegistrationStatus：5秒
```

这些是当前源码常量，不是所有历史版本固定值。

### 5.3 `Type`决定由谁处理

`GetInfoResponse`返回：

```text
Type
Name
Endpoint
SupportedVersions
```

`Type=DevicePlugin`才会找到本章的DeviceManager handler。

如果：

- `Type`拼错；
- kubelet没有该type handler；
- resource name不合法；
- API version不兼容；
- endpoint连不上；

registration会失败，并通过 `NotifyRegistrationStatus(false, error)`回告插件。

### 5.4 endpoint为空与非空不是一回事

```go
if infoResp.Endpoint == "" {
    infoResp.Endpoint = socketPath
}
```

- 为空：registration socket本身也充当DevicePlugin业务socket；
- 非空：按插件返回值传给handler。

与direct路径不同，这里不会自动执行：

```go
filepath.Join(
    /var/lib/kubelet/device-plugins,
    endpoint,
)
```

所以不要把两条入口的endpoint解析规则画成一模一样。

### 5.5 validate、actual state、register、notify的顺序

```text
GetInfo
  -> 找handler
  -> ValidatePlugin
  -> actualStateOfWorld.AddPlugin
  -> handler.RegisterPlugin
  -> NotifyRegistrationStatus(success)
```

若 `RegisterPlugin`失败，会从actual state移除。若成功后通知失败，也会移除并调用 `DeRegisterPlugin`。

这里体现通用plugin manager的reconcile思想：

```text
socket存在
  != actual state已经稳定
```

---

## 6. `DEPRECATION` 文件：按当前调用链讲，不按函数名猜

> **二读内容**：这是训练“不能按函数名猜调用链”的好例子，但首遍生产排障先掌握两套入口、connect、first list与Node Status；不要求第一次就背这个兼容死角。

源码里有：

```text
pkg/kubelet/cm/devicemanager/plugin/v1beta1/handler.go:32-44
```

```go
func (s *server) GetPluginHandler() cache.PluginHandler {

    os.Create(
        s.socketDir + "DEPRECATION",
    )
    return s
}
```

测试插件可能据此跳过direct Register。仅看函数名，很容易写出：

> kubelet启用plugin watcher后一定创建DEPRECATION文件，传统入口自动关闭。

但当前commit全仓调用链中：

```text
ManagerImpl.GetWatcherHandler()
  -> 直接 return m.server
```

没有经过 `server.GetPluginHandler()`。

因此本课只得出：

```text
源码保留这个能力和兼容信号
但当前静态主链不能证明它会被必然调用
```

生产看到或没看到 `DEPRECATION`文件，都不能脱离版本、调用链和插件实现直接推断注册模式。

---

## 7. 两条入口最终怎样汇合

direct：

```text
Registration.Register
  -> server.connectClient
```

generic：

```text
GetInfo/Validate
  -> server.RegisterPlugin
  -> server.connectClient
```

共同后半段：

```text
connectClient
  -> NewPluginClient(resourceName, endpoint)
  -> client.Connect
  -> ManagerImpl.PluginConnected
  -> GetDevicePluginOptions
  -> endpoints[resourceName] = endpointInfo
  -> go client.Run
  -> ListAndWatch
```

对应：

```text
pkg/kubelet/cm/devicemanager/plugin/v1beta1/handler.go:83-105
```

```go
func (s *server) connectClient(
    ctx context.Context,
    name string,
    socketPath string,
) error {
    logger := klog.FromContext(ctx)
    c := NewPluginClient(
        name,
        socketPath,
        s.chandler,
    )

    s.registerClient(logger, name, c)
    if err := c.Connect(ctx); err != nil {
        s.deregisterClient(logger, name)
        return err
    }

    go s.runClient(ctx, name, c)
    return nil
}
```

Go现场补——`go s.runClient(...)`：

- `go`启动新goroutine；
- `connectClient`不用等ListAndWatch结束才返回；
- registration可以完成，而设备清单流在后台长期运行；
- 所以“Register返回成功”和“第一份response已经处理”天然有时间差。

注意顺序：

```text
client先放入server.clients
  -> Connect失败再移除
  -> Connect成功后另起goroutine跑ListAndWatch
```

这也是为什么“有过registration请求”不能证明ListAndWatch仍活着。

---

## 8. `PluginConnected`主动读取真正的插件选项

```text
pkg/kubelet/cm/devicemanager/manager.go:228-245
```

```go
func (m *ManagerImpl) PluginConnected(
    ctx context.Context,
    resourceName string,
    p plugin.DevicePlugin,
) error {
    options, err :=
        p.API().
            GetDevicePluginOptions(
                ctx,
                &pluginapi.Empty{},
            )
    if err != nil {
        return fmt.Errorf(
            "failed to get device plugin options: %v",
            err,
        )
    }

    e := newEndpointImpl(p)

    m.mutex.Lock()
    defer m.mutex.Unlock()
    m.endpoints[resourceName] =
        endpointInfo{e, options}
    return nil
}
```

Go现场补——这里第一次真正加锁：

- `m.mutex.Lock()`只是在**当前kubelet进程内**保护`endpoints`共享map，避免连接、断连、Node status读取等goroutine并发读写；
- `defer m.mutex.Unlock()`保证函数返回前释放锁；
- 它不等于分布式锁，也不会同步等待apiserver或scheduler完成更新；
- 所以“锁内写入endpoint成功”和“Node API已出现Capacity”仍是两个完成点。

`DevicePluginOptions`主要告诉kubelet：

- 是否需要 `PreStartContainer`；
- 是否支持 `GetPreferredAllocation`；
- 当前API版本的其他能力。

本课只记“何时读取和保存”。具体怎样影响第 16 课的分配链，后面再展开。

注册到这里仍只说明：

```text
kubelet能连上插件
并获取能力选项
```

Node resource尚未必出现。

---

## 9. `ListAndWatch`：不是心跳，是完整设备清单流

```text
pkg/kubelet/cm/devicemanager/plugin/v1beta1/client.go:81-107
```

```go
func (c *client) Run(ctx context.Context) {
    logger := klog.FromContext(ctx)
    stream, err :=
        c.client.ListAndWatch(
            context.TODO(),
            &api.Empty{},
        )
    if err != nil {
        logger.Error(
            err,
            "ListAndWatch ended unexpectedly...",
        )
        return
    }

    for {
        response, err := stream.Recv()
        if err != nil {
            logger.Error(
                err,
                "ListAndWatch ended unexpectedly...",
            )
            return
        }
        c.handler.
            PluginListAndWatchReceiver(
                logger,
                c.resource,
                response,
            )
    }
}
```

Go现场补——`for { stream.Recv() }`：

- 无条件循环持续收server-stream消息；
- `Recv`阻塞等待下一份response；
- `err != nil`立即return，本层没有自动backoff/reconnect；
- 它与遍历slice的 `for _, x := range xs`不是同一种循环。

### 9.1 server-streaming怎么理解

```text
kubelet发一次ListAndWatch请求
  -> 插件持续往同一条stream发response
  -> 每次设备集合或health变化时再发新清单
```

它不是：

```text
每10秒一次固定心跳
每个device一个增量事件
Kubernetes watch resourceVersion
```

### 9.2 第一份response为什么关键

注册完成时：

```text
endpoints[resourceName]已经有连接
```

但只有第一份response到达，才有：

```text
allDevices
healthyDevices
unhealthyDevices
```

所以完整时间线是：

```text
registered
  < first ListAndWatch response
  < Node status updated
  < scheduler cache updated
```

### 9.3 当前client没有内部自动重连循环

`ListAndWatch`建流失败或 `Recv`返回错误时，`Run`直接return。恢复通常依赖：

- generic入口：registration socket删除/重建被通用watcher发现，再走desired/actual reconcile；
- direct入口：插件监测kubelet重启/`kubelet.sock`被重建等信号，重新监听自己的socket并主动调用`Register`；
- 插件进程自身restart/reconcile，或DaemonSet/Operator按既定策略重新拉起。

删除direct插件自己的socket，不能笼统描述成“通用plugin manager一定会发现并恢复”；只有它确实也作为generic registration socket被watch时才属于那条入口。两套入口是替代入口，各按自己的协议恢复。

不能只等“这个goroutine自己无限重试”。

---

## 10. 每份response怎样重建三张设备表

接收：

```text
pkg/kubelet/cm/devicemanager/manager.go:263-322
```

```go
func (m *ManagerImpl) PluginListAndWatchReceiver(
        logger klog.Logger,
        resourceName string,
        resp *pluginapi.ListAndWatchResponse,
    ) {
    m.genericDeviceUpdateCallback(
        logger,
        resourceName,
        resp.Devices,
    )
}
```

核心更新：

```go
m.mutex.Lock()

m.healthyDevices[resourceName] =
    sets.New[string]()
m.unhealthyDevices[resourceName] =
    sets.New[string]()

oldDevices := m.allDevices[resourceName]
m.allDevices[resourceName] =
    make(map[string]*pluginapi.Device)

for _, dev := range devices {
    m.allDevices[resourceName][dev.ID] = dev

    if dev.Health == pluginapi.Healthy {
        m.healthyDevices[resourceName].
            Insert(dev.ID)
    } else {
        m.unhealthyDevices[resourceName].
            Insert(dev.ID)
    }
}

m.mutex.Unlock()

m.writeCheckpoint(logger)
```

Go现场补——map与set重建：

- `make(map[...])`创建新map；
- `sets.New[string]()`创建空集合；
- 把它们赋回 `m.*Devices[resourceName]`会替换该resource上一版账；
- 因而这里是full snapshot replacement，不是incremental merge。

### 10.1 为什么说response是完整清单

每次先：

```go
healthyDevices[resource] = empty
unhealthyDevices[resource] = empty
allDevices[resource] = new map
```

再按当前response全部重建。

因此：

```text
上次有 device-a、device-b
这次只发 device-a
  -> device-b不是“保持旧状态”
  -> 它从当前设备集合消失
```

插件不能只发送“变化的那一个逻辑device entry”而期待kubelet自动与旧列表合并。

### 10.2 只有精确 `Healthy` 才算健康

```go
if dev.Health == pluginapi.Healthy
```

其他字符串进入unhealthy集合。设备插件协议定义：

```text
Healthy
Unhealthy
```

不要让厂商插件自创 `OK`、`healthy`、`Degraded`并假设kubelet能理解。

### 10.3 device ID是opaque string

`dev.ID`可能是：

- GPU UUID；
- index；
- MIG device ID；
- vGPU ID；
- time-slicing插件构造的share ID；
- 其他厂商插件自定义标识。

kubelet主要按字符串唯一性消费。Node Status只暴露数量，不把这些ID写进 `capacity`。

所以：

```text
Node capacity nvidia.com/gpu=8
  != scheduler知道8个GPU UUID
```

具体哪一个ID给哪个container，留到第 16 课。

### 10.4 重复ID不能用response长度掩盖

`allDevices`是map、healthy/unhealthy是set。若错误插件在一份response里重复同一ID：

- `len(response.Devices)`可能包含重复项；
- 同一ID、同一Health重复时，会在同一个set里去重；
- 同一ID若一条`Healthy`、另一条非`Healthy`，可能同时进入healthy与unhealthy两个set；
- `allDevices` map对同一key执行最后一次写覆盖，但两个health set不会替插件做跨集合冲突消解；
- 因此冲突Health的同一ID可能在`Capacity = healthy.Len + unhealthy.Len`里被双计；
- 日志里的`len(response.Devices)`和循环内`healthyCount`也可能统计原始重复entry；
- `State pushed ... resourceCapacity=<len>`日志不等于最终Node Capacity。

Device Plugin协议要求device ID唯一。当前kubelet不会替错误插件修复所有重复/冲突组合；排障必须比较原始entry、`allDevices`最后值和两个health集合，不能概括成“set会自动全部去重”。

### 10.5 topology不是Capacity字段

一个 `pluginapi.Device`还可以带 `TopologyInfo`，描述设备NUMA node。当前 `GetCapacity`只数healthy/unhealthy集合，不把具体topology写进Node Capacity。

Topology Manager怎样消费设备拓扑、具体ID怎样受hint影响，属于第 16 课。

---

## 11. `ResourceHealthStatus`不控制容量计算

当前代码在feature gate开启时，还比较新旧device health，找出占用这些device的Pod UID，并发resource update。

但三张表的重建和checkpoint不依赖这个feature gate：

```text
ResourceHealthStatus关闭
  -> 仍更新healthy/unhealthy
  -> 仍影响Node Capacity/Allocatable

ResourceHealthStatus开启
  -> 额外通知相关Pod刷新allocatedResourcesStatus
```

不能误解成：

> 没开ResourceHealthStatus，kubelet就不会感知GPU Unhealthy。

它主要控制已分配设备健康怎样进一步暴露到Pod status；这部分在第 17 课展开。

---

## 12. Capacity与Allocatable的源码公式

```text
pkg/kubelet/cm/devicemanager/manager.go:444-493
```

先遍历healthy：

```go
capacity[resourceName] =
    quantity(healthyDevices.Len())

allocatable[resourceName] =
    quantity(healthyDevices.Len())
```

再遍历unhealthy：

```go
capacityCount :=
    capacity[resourceName]

capacityCount.Add(
    quantity(unhealthyDevices.Len()),
)

capacity[resourceName] =
    capacityCount
```

所以：

```text
Capacity    = healthy + unhealthy
Allocatable = healthy
```

这里的`healthy/unhealthy`单位是**插件上报的逻辑 Device entry**，不是PCI/NVML物理卡：

```text
Capacity
  = 被DeviceManager两个health集合计数的逻辑entry数
  != 必然等于物理GPU张数
```

例如 NVIDIA time-slicing把8张物理GPU配置为每张10个replica时，插件可以上报80个可调度逻辑entry，Node的`nvidia.com/gpu` Capacity可能是80。MIG也可能按策略暴露不同resource name及逻辑实例。第21课再讲共享与隔离；本课先形成纪律：**每次看到数量，都先问resource name、插件策略和一个entry代表什么。**

### 12.1 三个手算场景

以下三个场景为方便手算，明确假设：无MIG、无sharing/time-slicing，每个逻辑entry恰好对应一张完整物理GPU。公式本身不依赖这个假设。

#### 场景 A：8个逻辑entry都健康

```text
healthy=8
unhealthy=0

Capacity=8
Allocatable=8
```

#### 场景 B：1个逻辑entry被插件标记Unhealthy

```text
healthy=7
unhealthy=1

Capacity=8
Allocatable=7
```

含义：

```text
物理/逻辑设备仍在插件清单
但1个暂不允许新分配
```

#### 场景 C：1个逻辑entry从新清单完全消失

```text
healthy=7
unhealthy=0

Capacity=7
Allocatable=7
```

含义：

```text
插件当前不再承认第8个device存在
```

“Unhealthy”和“消失”对Capacity影响不同。

### 12.2 最重要纠偏：Allocatable不是“剩余未使用”

节点有8个健康、可分配逻辑GPU资源单位，已有6个Pod各请求1个单位：

```text
Node.status.capacity[nvidia.com/gpu]    = 8
Node.status.allocatable[nvidia.com/gpu] = 8
```

通常仍是8，不会自动变2。

scheduler计算：

```text
可继续调度
  = Node Allocatable
  - NodeInfo中现有Pod requests
  - 调度周期内assumed Pod requests
```

这与第 09 课 CPU/memory账本完全一致。Node Allocatable表达“节点可供Pod使用的总预算”，不是动态空闲量。

因此看到：

```text
Allocatable GPU=8
FailedScheduling: Insufficient nvidia.com/gpu
```

并不矛盾。还要看该Node已有Pod的GPU requests。

---

## 13. 数量怎样进入 Node Status

调用链：

```text
ListAndWatchResponse
  -> genericDeviceUpdateCallback
  -> healthyDevices/unhealthyDevices
  -> kubelet syncNodeStatus
  -> nodestatus.MachineInfo setter
  -> containerManager.GetDevicePluginResourceCapacity
  -> deviceManager.GetCapacity
  -> node.Status.Capacity/Allocatable
  -> PatchNodeStatus
  -> apiserver
  -> scheduler Node informer
```

### 13.1 Node setter写Capacity

```text
pkg/kubelet/nodestatus/setters.go:260-286
```

```go
devicePluginCapacity,
devicePluginAllocatable,
removedDevicePlugins =
    devicePluginResourceCapacityFunc()

for k, v := range devicePluginCapacity {
    node.Status.Capacity[k] = v
}

for _, removedResource :=
    range removedDevicePlugins {

    node.Status.Capacity[
        v1.ResourceName(removedResource)
    ] = *resource.NewQuantity(
        0,
        resource.DecimalSI,
    )
}
```

### 13.2 再覆盖Device Plugin Allocatable

普通资源先按capacity减Node reservation形成allocatable；随后：

```text
pkg/kubelet/nodestatus/setters.go:306-337
```

```go
for k, v :=
    range devicePluginAllocatable {

    node.Status.Allocatable[k] = v
}
```

Device Plugin extended resource通常没有像CPU/memory那样的kube/system reserved减法；它由DeviceManager健康集合给出。

### 13.3 removed resource为什么写0而不是删字段

源码注释明确：

```text
写0用于表示：
  这个extended resource以前由Device Plugin管理过
```

直接从Node Status删除，会难以区分：

- 从未注册过；
- 曾注册但现在已移除；
- 其他集群级opaque resource。

#### 13.3.1 absent与显式`0/0`状态表

| 阶段 | Node API常见最终状态 | 解释边界 |
|---|---|---|
| 从未注册，或首次清单尚未进入本地账 | resource absent | 也可能仍在异步传播，需对时 |
| 活跃插件发送空的完整清单 | Capacity=0，Allocatable=0 | 插件明确上报当前没有entry |
| kubelet从checkpoint恢复、等待插件重注册/首包 | Capacity=0，Allocatable=0 | 记得“以前有此resource”，不复用旧Health |
| stream断连，下一次`GetCapacity`和Node patch已完成，仍在宽限期 | Capacity=N，Allocatable=0 | 总量暂保留，禁止新分配 |
| 超过宽限且Node Status传播完成 | Capacity=0，Allocatable=0 | removed resource被显式写0，不是长期删除字段 |

表中是各阶段完成异步传播后的典型状态。任一瞬间都可能暂时看到上一次Node值，所以要同时记录kubelet本地日志、Node `resourceVersion`和时间。

### 13.4 Node Lease不带GPU容量

Node Lease是轻量心跳，不保存 `status.capacity/allocatable`。GPU数量变化必须通过Node Status patch传播。

所以可能有短窗口：

```text
插件本地账已经变
  -> kubectl get node仍是旧值
  -> scheduler cache也可能仍是旧值
```

这不是要求你等待无限久，而是要按链逐层验证时间：

```text
kubelet日志更新时间
Node metadata.resourceVersion / managedFields时间
scheduler看到的Node更新
FailedScheduling Event新旧时间
```

---

## 14. 注册成功到可调度资源出现的完整时间线

```text
t0  插件进程启动，发现GPU
t1  插件监听自己的DevicePlugin socket
t2  direct Register请求到达并完成字段校验
    或generic GetInfo/ValidatePlugin完成
t3  connectClient完成dial
t4  PluginConnected/GetDevicePluginOptions成功
t5  ListAndWatch goroutine已启动；
    direct Register现在可以返回成功，
    或generic RegisterPlugin完成后NotifyRegistrationStatus(true)
t6  第一份完整设备清单到达
t7  DeviceManager重建三张表并写checkpoint
t8  kubelet下一轮syncNodeStatus
t9  PatchNodeStatus成功
t10 scheduler informer拿到新Node
t11 之前Pending的GPU Pod因Node资源变化重新排队
t12 scheduler重新Filter并可能绑定
```

这是一组**因果完成点**，不是要求日志时间戳绝对串行。`connectClient`在启动`ListAndWatch` goroutine后返回，goroutine调度与direct RPC响应/generic Notify可能发生细小交错；但“注册调用可以成功返回”仍不能替代“kubelet已经处理first response”这一独立证据。

因此：

| 截止证据 | 最多能证明 |
|---|---|
| 插件Pod Running | 进程容器还活着 |
| 插件日志“Starting to serve” | 自己的gRPC socket开始监听 |
| kubelet“Device plugin connected” | 连接和GetOptions成功 |
| kubelet“Processed device updates” | 至少处理了一份设备清单 |
| Node Status出现resource | 数量已上报到apiserver |
| GPU Pod重新被调度 | scheduler缓存和队列也已消费变化 |

任何一格都不能自动替代下一格。

---

## 15. 插件断连怎样分两阶段收敛

### 15.1 stream结束会走disconnect

`client.Run`返回后：

```text
pkg/kubelet/cm/devicemanager/plugin/v1beta1/handler.go:122-133
```

```go
func (s *server) runClient(
    ctx context.Context,
    name string,
    c Client,
) {
    c.Run(ctx)

    c = s.getClient(name)
    if c == nil {
        return
    }
    s.disconnectClient(
        logger,
        name,
        c,
    )
}
```

`Client.Disconnect`：

```text
关闭gRPC连接
  -> ManagerImpl.PluginDisconnected
```

### 15.2 第一阶段：立即把healthy移到unhealthy

```text
pkg/kubelet/cm/devicemanager/manager.go:247-261
```

```go
if ep, exists :=
    m.endpoints[resourceName]; exists {

    m.markResourceUnhealthy(
        logger,
        resourceName,
    )
    ep.e.setStopTime(time.Now())
}
```

`markResourceUnhealthy`：

```text
healthy集合清空
unhealthy = unhealthy ∪ 原healthy
```

若原来8个逻辑entry健康：

```text
插件刚断连
Capacity=8
Allocatable=0
```

这是DeviceManager下一次`GetCapacity`根据本地集合会算出的结果；Node API还要等`syncNodeStatus/PatchNodeStatus`，scheduler又要等Node informer更新。刚看见断连日志的同一瞬间，`kubectl get node`仍可能短暂显示旧的`8/8`。

含义：

```text
暂时保留已知设备总量
但禁止新Pod继续分配
给插件短暂重连留宽限
```

### 15.3 第二阶段：5分钟宽限期后移除内部resource

当前常量：

```text
pkg/kubelet/cm/devicemanager/types.go

endpointStopGracePeriod = 5 * time.Minute
```

`GetCapacity`发现endpoint停用超过宽限期后：

```text
delete endpoints[resource]
delete healthyDevices[resource]
delete unhealthyDevices[resource]
return removed resource name
```

Node setter再把Capacity置0，Allocatable随之归0。

时间线：

```text
刚断连：
  Capacity=原总量
  Allocatable=0

宽限期后且Node Status完成传播：
  Capacity=0
  Allocatable=0
```

五分钟是当前源码实现值，不应写进平台永恒SLA；升级版本后重新校准。

### 15.4 重连会发生什么

插件在宽限期内重新注册：

```text
新endpoint覆盖/恢复
  -> 新ListAndWatch清单
  -> 三张表按新事实重建
  -> Node Status再收敛
```

不能只看到Pod重启完成就宣布恢复；必须等新清单和Node Status。

---

## 16. checkpoint在本课只读一条窄边界

每次设备清单更新后：

```go
m.writeCheckpoint(logger)
```

checkpoint里的 `registeredDevs`当前取自healthy设备集合。kubelet重启读取checkpoint时，会：

```text
恢复已知resource键
  -> healthy/unhealthy先建空集合
  -> endpoint先设为stopped
  -> 等插件重新注册和第一份ListAndWatch
```

这能避免把旧健康设备盲目当成当前可分配资源。

本课只记：

```text
checkpoint帮助知道“以前有这个resource”
但不会把旧health直接当新事实
```

具体已分配device ID、Pod/container账本、重启恢复和PodResources留到第 17 课。

---

## 17. 四种容量变化必须会区分

| 变化 | healthy | unhealthy | Capacity | Allocatable |
|---|---:|---:|---:|---:|
| 8个逻辑entry首次健康上报 | 8 | 0 | 8 | 8 |
| 1个entry的Health变Unhealthy | 7 | 1 | 8 | 7 |
| 1个entry从ListAndWatch清单消失 | 7 | 0 | 7 | 7 |
| 插件刚断连 | 0 | 8 | 8 | 0 |
| 断连超宽限并完成Node更新 | 0 | 0 | 0 | 0 |

这张表是本课最重要的手算题。

### 17.1 已分配中的设备变Unhealthy

Node Allocatable下降，阻止新分配只是第一步。已经在跑的Pod是否：

- 继续运行；
- Pod status显示allocated resource health；
- 被平台隔离/迁移；
- 由Operator/DCGM策略处理；

不是DeviceManager自动统一杀Pod。第 17、19、21 课再讲健康联动。

### 17.2 设备从清单消失

可能方向：

- Driver/NVML不再枚举；
- MIG重配；
- vGPU/profile变化；
- 插件过滤策略变化；
- 插件bug漏报；
- 设备ID策略变化。

若该ID仍被Pod占用，会进入更复杂的已分配账本一致性问题，不能只把Capacity数字改回去。

---

## 18. 从生产现象反查源码断点

| 现象 | 首个源码断点 | 下一证据 |
|---|---|---|
| Plugin Pod不在目标Node | DaemonSet调度/Node selector/taint | Pod spec、owner、nodeName |
| Pod Running但没有plugin socket | 插件初始化/hostPath/runtime | plugin日志、mount、目标Node socket |
| direct Register反复失败 | `server.Register` | Version、ResourceName、Endpoint |
| generic watcher发现socket但注册失败 | `GenerateRegisterPluginFunc` | GetInfo Type/Name/Endpoint/Versions、Notify错误 |
| “Device plugin connected”后资源仍无 | `client.Run/ListAndWatch` | 第一份response、stream错误 |
| response有8个entry，Node只有7 | `genericDeviceUpdateCallback` | 唯一ID、Health、重复/冲突ID |
| Capacity=8/Allocatable=0 | 全部Unhealthy或刚断连 | endpoint日志、health清单、stopTime时间线 |
| Capacity/Allocatable本地已算新值，API仍旧 | Node status链 | syncNodeStatus/PatchNodeStatus错误 |
| Node显示8但仍Insufficient | scheduler request账本 | 已有Pod requests，不是看实时利用率 |

---

## 19. 日志、Event与指标：哪些证据真的存在

### 19.1 这条链没有专用 Kubernetes Event

当前注册、ListAndWatch、三张表和Capacity更新主链没有发专用Event。

因此：

```text
kubectl get events没有Device Plugin错误
  != Device Plugin注册正常
```

你可能看到：

```text
FailedScheduling: Insufficient nvidia.com/gpu
UnexpectedAdmissionError: Allocate failed ...
```

它们分别是：

- scheduler看到资源不足的下游结果；
- 第 16 课Allocate/本机准入失败的下游结果。

都不是“注册成功”的直接证据。

### 19.2 direct入口关键kubelet日志

```text
Got registration request from device plugin with resource
Bad registration request from device plugin with resource
Error connecting to device plugin client
```

### 19.3 generic watcher关键日志

```text
Plugin Watcher Start
Adding socket path or updating timestamp to desired state cache
Registering plugin at endpoint
Device plugin validated
OperationExecutor.RegisterPlugin failed
```

### 19.4 共同后半段日志

```text
Device plugin connected
State pushed for device plugin
Processed device updates for resource
ListAndWatch ended unexpectedly for device plugin
Device plugin disconnected
Endpoint became unhealthy
Mark all resources Unhealthy for resource
Updated capacity for device plugin
Updated allocatable
Set capacity for removed resource to 0 on device removal
```

许多成功日志是 `V(2)`或更高。默认日志级别看不到，不等于代码没走；也不能为了临时取证就在生产全量Node长期打开高verbosity。

### 19.5 NVIDIA插件日志要与kubelet日志对时

不同版本文本会变化，常见阶段包括：

```text
发现NVML/设备
Starting to serve '<resource>' on <socket>
Registered device plugin for '<resource>' with Kubelet
health check / Xid状态
配置重载或插件restart
```

必须记录：

- plugin image digest/version；
- Pod UID、restartCount、containerID；
- Node name；
- 日志时间；
- kubelet同一时间窗。

只看插件一边会漏掉kubelet的版本校验、endpoint连接和stream结束。

---

## 20. 两个容易被指标名字骗到的地方

### 20.1 `kubelet_device_plugin_registration_total`

定义：

```text
pkg/kubelet/metrics/metrics.go
```

labels：

```text
resource_name
```

但当前只在direct `server.Register`里递增，而且位置在：

```text
Version校验之前
ResourceName校验之前
connectClient之前
```

所以它统计：

```text
direct registration请求次数
```

不是：

```text
成功注册数
当前已连接插件数
第一份ListAndWatch已收到数
```

generic watcher路径也不会递增这个counter。

### 20.2 `plugin_manager_total_plugins`

当前descriptor：

```text
labels:
  socket_path
  state
```

它反映通用plugin manager desired/actual state，覆盖CSI、DRA、DevicePlugin；没有plugin type label。

所以：

```text
state="actual_state_of_world"且值为1
  -> 某socket进入actual state
  != 它一定是nvidia.com/gpu
  != ListAndWatch设备清单健康
```

socket path本身可能暴露节点内部目录，外发前脱敏。

### 20.3 核心DeviceManager没有GPU健康数量指标

当前核心代码没有直接提供：

```text
kubelet_device_plugin_healthy_devices{resource=...}
kubelet_device_plugin_unhealthy_devices{resource=...}
```

数量最终看Node Status；硬件健康和Xid/ECC看厂商插件/DCGM。不要编造不存在的Prometheus指标。

---

## 21. 只读取证实验 1：持续观察 Node 数量

> 仓库当前未连接GPU实验集群，以下脚本未执行，不写假PASS。

```powershell
$ErrorActionPreference = 'Stop'

$ApprovedContext = '__APPROVED_KUBE_CONTEXT__'
$Node = '__GPU_NODE__'
$Resource = 'nvidia.com/gpu'
$Samples = 30
$IntervalSeconds = 2

if ($ApprovedContext -like '__*' -or $Node -like '__*') {
  throw '先填写批准的context和经过确认的GPU Node'
}
$actualContext = kubectl config current-context
if ($LASTEXITCODE -ne 0 -or -not $actualContext) {
  throw '读取current-context失败或为空'
}
if (($actualContext | Out-String).TrimEnd() -cne $ApprovedContext) {
  throw '当前kubectl context不是批准值'
}

$rows = for ($i = 0; $i -lt $Samples; $i++) {
  $nodeJson = kubectl --context $ApprovedContext get node $Node -o json
  if ($LASTEXITCODE -ne 0 -or -not $nodeJson) {
    throw "第$i次读取Node失败"
  }

  $nodeObject = $nodeJson | ConvertFrom-Json
  if ($nodeObject.metadata.name -cne $Node) {
    throw "第$i次API返回Node名称与批准值不一致"
  }
  $capProperty = $nodeObject.status.capacity.
    PSObject.Properties[$Resource]
  $allocProperty = $nodeObject.status.allocatable.
    PSObject.Properties[$Resource]

  [pscustomobject]@{
    Time = Get-Date -Format 'yyyy-MM-dd HH:mm:ss.fff'
    NodeResourceVersion = $nodeObject.metadata.resourceVersion
    Capacity = if ($capProperty) {
      $capProperty.Value
    } else {
      '<absent>'
    }
    Allocatable = if ($allocProperty) {
      $allocProperty.Value
    } else {
      '<absent>'
    }
  }

  if ($i -lt ($Samples - 1)) {
    Start-Sleep -Seconds $IntervalSeconds
  }
}

$rows | Format-Table -AutoSize
```

它能观察：

- 字段absent、首次出现或归0；当前DeviceManager的removed resource路径长期写显式0，不把它描述成“正常删除后字段消失”；
- Capacity与Allocatable分叉；
- Node `resourceVersion`是否变化。

它不能单独说明：

- 哪个device ID变了；
- 是Health变化、清单消失还是插件断连；
- scheduler cache已在同一毫秒更新；
- GPU硬件根因。

要与插件/kubelet日志同时间线关联。

---

## 22. 只读取证实验 2：先把Plugin Pod精确对到Node

不要直接看“集群里任意一个nvidia-device-plugin Pod”的日志。DaemonSet每个Node一份，必须对到目标Node。

```powershell
$ErrorActionPreference = 'Stop'

$ApprovedContext = '__APPROVED_KUBE_CONTEXT__'
$Node = '__GPU_NODE__'
$Selector = '__YOUR_PLUGIN_LABEL_SELECTOR__'

if (
  $ApprovedContext -like '__*' -or
  $Node -like '__*' -or
  $Selector -like '__*'
) {
  throw '先填写批准的context、目标Node和真实label selector'
}
$actualContext = kubectl config current-context
if ($LASTEXITCODE -ne 0 -or -not $actualContext) {
  throw '读取current-context失败或为空'
}
if (($actualContext | Out-String).TrimEnd() -cne $ApprovedContext) {
  throw '当前kubectl context不是批准值'
}

$podsJson = kubectl --context $ApprovedContext get pods -A `
  -l $Selector `
  --field-selector "spec.nodeName=$Node" `
  -o json
if ($LASTEXITCODE -ne 0 -or -not $podsJson) {
  throw '读取目标Node的Device Plugin Pod失败'
}

$pods = @(
  ($podsJson | ConvertFrom-Json).items
)
if ($pods.Count -lt 1) {
  throw '目标Node没有匹配的Device Plugin Pod'
}

foreach ($pod in $pods) {
  if ($pod.spec.nodeName -cne $Node) {
    throw "API返回了不在目标Node上的Pod：$($pod.metadata.namespace)/$($pod.metadata.name)"
  }

  [pscustomobject]@{
    Namespace = $pod.metadata.namespace
    Name = $pod.metadata.name
    UID = $pod.metadata.uid
    Phase = $pod.status.phase
    Node = $pod.spec.nodeName
    Containers = (
      $pod.spec.containers.name -join ','
    )
    Restarts = (
      $pod.status.containerStatuses.restartCount |
        Measure-Object -Sum
    ).Sum
  } | Format-List

  $podYaml = kubectl --context $ApprovedContext get pod $pod.metadata.name `
    -n $pod.metadata.namespace `
    -o yaml
  if ($LASTEXITCODE -ne 0 -or -not $podYaml) {
    throw "读取Pod YAML失败：$($pod.metadata.namespace)/$($pod.metadata.name)"
  }
  $podYaml

  foreach ($container in $pod.spec.containers.name) {
    $currentLog = kubectl --context $ApprovedContext logs $pod.metadata.name `
      -n $pod.metadata.namespace `
      -c $container `
      --timestamps `
      --tail=500
    if ($LASTEXITCODE -ne 0 -or -not $currentLog) {
      throw "读取当前日志失败或为空：$($pod.metadata.name)/$container"
    }
    $currentLog

    $containerStatus = @($pod.status.containerStatuses) |
      Where-Object { $_.name -ceq $container } |
      Select-Object -First 1
    if ($containerStatus -and $containerStatus.restartCount -gt 0) {
      $previousLog = kubectl --context $ApprovedContext logs $pod.metadata.name `
        -n $pod.metadata.namespace `
        -c $container `
        --previous `
        --timestamps `
        --tail=500
      if ($LASTEXITCODE -ne 0 -or -not $previousLog) {
        throw "container有重启但previous日志读取失败或为空：$($pod.metadata.name)/$container"
      }
      $previousLog
    }
  }
}
```

安全边界：

- selector来自实际DaemonSet/Operator对象，不猜通用label；
- 输出含image、env、hostPath、Node名，外发需脱敏；
- `--tail`限制日志量；
- 不执行delete、rollout restart或改ConfigMap；
- 多套Device Plugin并存时全部记录，不能随便挑一个。

---

## 23. 只读取证实验 3：检查两类socket

先通过平台资产/堡垒机审批链，把登录目标与Kubernetes Node对象对应起来；推荐同时记录Node `.status.nodeInfo.systemUUID`、当前boot ID及资产ID。`hostname -f`只是本机线索，**不保证等于Kubernetes Node name**。

在身份已经确认的目标GPU Node上，先把`__ACTUAL_KUBELET_ROOT__`替换为该进程实际`--root-dir`：

```bash
date -Is
hostname -f
cat /sys/class/dmi/id/product_uuid 2>/dev/null || true
cat /proc/sys/kernel/random/boot_id

direct_dir=/var/lib/kubelet/device-plugins
generic_dir=__ACTUAL_KUBELET_ROOT__/plugins_registry
test "$generic_dir" != '__ACTUAL_KUBELET_ROOT__/plugins_registry' || {
  echo '先填写实际kubelet root' >&2
  exit 2
}

sudo find \
  "$direct_dir" \
  "$generic_dir" \
  -maxdepth 3 \
  -type s \
  -printf '%p %u:%g %m %TY-%Tm-%TdT%TH:%TM:%TS\n' \
  2>/dev/null

sudo ss -xlpn |
  grep -E 'device-plugins|plugins_registry' ||
  true

sudo stat \
  /var/lib/kubelet/device-plugins/kubelet.sock \
  /var/lib/kubelet/device-plugins/DEPRECATION \
  2>/dev/null ||
  true
```

解释纪律：

```text
看到kubelet.sock
  -> direct入口监听条件存在
  != NVIDIA插件已注册

看到nvidia-gpu.sock
  -> 某进程创建了socket文件
  != kubelet已连接

看到plugins_registry中的socket
  -> 通用watcher可发现
  != Type一定是DevicePlugin

看到DEPRECATION
  -> 有兼容信号文件
  != 当前commit必然由主链创建
```

当前upstream边界要分别记：

- direct目录固定为`/var/lib/kubelet/device-plugins`，不随普通`--root-dir`变化；
- generic `plugins_registry`跟随kubelet root；
- 容器化kubelet的mount namespace或发行版源码补丁可能改变宿主机可见路径，需额外核对进程mount与DaemonSet hostPath。

不要：

```text
删除socket试试能否重建
touch DEPRECATION
改owner/mode
停止监听进程
```

---

## 24. 只读取证实验 4：抓同一时间窗kubelet日志

目标Node：

```bash
sudo journalctl -u kubelet \
  --since '-30 minutes' \
  --no-pager |
grep -E \
  'registration request from device plugin|Registering plugin at endpoint|Device plugin validated|Device plugin connected|State pushed for device plugin|Processed device updates|ListAndWatch ended unexpectedly|Device plugin disconnected|Endpoint became unhealthy|Updated capacity|Updated allocatable|removed resource'
```

注意：

- 非systemd/托管发行版要改用平台日志入口；
- kubelet日志可能进入journald、文件或集中日志；
- `grep`无输出可能只是verbosity不足；
- 记录原始完整行，不只截图一个关键字；
- Node、时间、boot ID、kubelet版本必须同一份证据。

如果需要临时提高verbosity，属于Node变更，另走审批和回退；本实验不自动改。

---

## 25. 只读取证实验 5：检查两个指标

需要Node proxy RBAC：

```powershell
$ErrorActionPreference = 'Stop'

$ApprovedContext = '__APPROVED_KUBE_CONTEXT__'
$Node = '__GPU_NODE__'
if ($ApprovedContext -like '__*' -or $Node -like '__*') {
  throw '先填写批准的context和目标Node'
}
$actualContext = kubectl config current-context
if ($LASTEXITCODE -ne 0 -or -not $actualContext) {
  throw '读取current-context失败或为空'
}
if (($actualContext | Out-String).TrimEnd() -cne $ApprovedContext) {
  throw '当前kubectl context不是批准值'
}

$metrics = kubectl --context $ApprovedContext get --raw `
  "/api/v1/nodes/$Node/proxy/metrics"
if ($LASTEXITCODE -ne 0 -or -not $metrics) {
  throw '读取kubelet metrics失败；核对RBAC与Node proxy'
}

$metrics -split "`n" |
  Select-String `
    'device_plugin_registration_total|plugin_manager_total_plugins'
```

解读：

```text
direct counter增加
  -> 有direct Register请求
  != 注册成功

generic actual state出现
  -> socket进入通用plugin manager实际状态
  != 第一份设备清单已到
```

不要把Node proxy权限授给不需要的普通业务账号；metrics可能暴露大量节点内部标签。

---

## 26. 受控观察实验：只跟随已批准的canary变更

若平台本来就安排了Device Plugin/Operator canary升级，可在变更窗口观察：

```text
T0  旧Plugin Pod UID/image/restart
T1  socket/listener变化
T2  registration日志
T3  Device plugin connected
T4  first ListAndWatch processed
T5  Node Capacity/Allocatable变化
T6  canary CUDA workload通过
```

验收表：

| 时刻 | Plugin Pod UID | connected | first list | Capacity | Allocatable | 结论 |
|---|---|---|---|---:|---:|---|
| T0 | 旧UID | 是 | 是 | 实测 | 实测 | 基线 |
| T1 | 新旧切换 | 未知 | 否 | 实测 | 实测 | 只记录 |
| T2 | 新UID | 是/否 | 否 | 实测 | 实测 | registration证据，不能宣布恢复 |
| T3 | 新UID | 是 | 否/未知 | 实测 | 实测 | connected，仍等first list |
| T4 | 新UID | 是 | 是 | 实测 | 实测 | first list，仍等Node Status |
| T5 | 新UID | 是 | 是 | 预期 | 预期 | Node Status已传播 |
| T6 | 新UID | 是 | 是 | 预期 | 预期 | CUDA smoke通过才验收 |

本章**禁止为了实验主动制造**：

- 删除生产Device Plugin socket；
- 重启生产DaemonSet；
- 停kubelet/containerd；
- 修改GPU health；
- 删除 `kubelet_internal_checkpoint`；
- 切换MIG；
- 改Device Plugin resource name。

源码理解不值得拿生产GPU任务做破坏性演示。

---

## 27. 本地源码测试边界

当前仓库 `go.mod`要求 Go 1.26，本机已知Go 1.19.4，因此不声称已运行下列测试。

具备匹配工具链后可选择：

```powershell
$ErrorActionPreference = 'Stop'

$SourceRoot = '__KUBERNETES_SOURCE_ROOT__'
if ($SourceRoot -like '__*') {
  throw '先填写当前commit对应的Kubernetes源码根目录'
}
$resolvedRoot = (Resolve-Path -LiteralPath $SourceRoot).Path
if (-not (Test-Path -LiteralPath (Join-Path $resolvedRoot 'go.mod'))) {
  throw 'SourceRoot不是Kubernetes源码根：缺少go.mod'
}

$deviceRegex = 'Test(UpdateCapacityAllocatable|EndpointSyncOnDisconnect|DevicePluginReRegistration)$'
$watcherRegex = 'Test(PluginRegistration|PluginRegistrationAtKubeletStart|PluginReRegistration)$'

Push-Location -LiteralPath $resolvedRoot
try {
  $deviceList = & go test ./pkg/kubelet/cm/devicemanager `
    -list $deviceRegex
  if ($LASTEXITCODE -ne 0) {
    throw 'devicemanager go test -list失败'
  }
  if (-not (@($deviceList | Where-Object { $_ -match '^Test' }).Count)) {
    throw 'devicemanager正则没有匹配任何测试，拒绝假通过'
  }

  & go test ./pkg/kubelet/cm/devicemanager `
    -run $deviceRegex `
    -count=1
  if ($LASTEXITCODE -ne 0) {
    throw 'devicemanager目标测试失败'
  }

  $watcherList = & go test ./pkg/kubelet/pluginmanager/pluginwatcher `
    -list $watcherRegex
  if ($LASTEXITCODE -ne 0) {
    throw 'pluginwatcher go test -list失败'
  }
  if (-not (@($watcherList | Where-Object { $_ -match '^Test' }).Count)) {
    throw 'pluginwatcher正则没有匹配任何测试，拒绝假通过'
  }

  & go test ./pkg/kubelet/pluginmanager/pluginwatcher `
    -run $watcherRegex `
    -count=1
  if ($LASTEXITCODE -ne 0) {
    throw 'pluginwatcher目标测试失败'
  }
}
finally {
  Pop-Location
}
```

脚本先固定源码根、用`go test -list`确认每组正则至少匹配一个测试，再逐条检查原生命令退出码，并在`finally`恢复原目录。即便如此，仍要先提供与`go.mod`匹配的Go工具链；本机当前版本不满足，所以这里没有实际PASS记录。

本课当前验证方式是：

- 静态读当前源码；
- 代码块/PowerShell语法检查；
- 生产实验设计安全审查；
- 不伪造运行结果。

---

## 28. NVIDIA场景最常见的六个具体分支

### 28.1 `FAIL_ON_INIT_ERROR=false`可能让Pod活着但资源不出现

NVIDIA Device Plugin有“初始化失败是否退出”的配置。某些版本/模式下，设为false会让插件在找不到GPU/Driver时阻塞等待，而不是CrashLoop。

于是：

```text
Plugin Pod phase=Running
  -> 进程仍活着
  -> 但可能没有为nvidia.com/gpu成功serve/register/list
```

所以要查实际配置、镜像版本和日志，不能用Pod Running反证NVML初始化成功。

### 28.2 插件DaemonSet没覆盖目标Node

常见原因：

- nodeSelector/affinity不匹配；
- GPU Node taint没有被插件tolerate；
- Operator节点标签还没形成；
- DaemonSet update strategy/canary选择；
- Node被配置排除；
- 插件Pod调度到了同名但不是故障Node。

先用 `spec.nodeName`做精确关联。

### 28.3 hostPath/socket mount不一致

插件容器通常要访问kubelet Device Plugin目录。若：

```text
upstream direct固定目录没有按正确hostPath挂进插件容器
容器内mountPath与插件参数不一致
SELinux/permission阻止socket访问
发行版源码补丁或容器化kubelet的mount视图与默认假设不同
```

就可能出现“插件进程在跑，但注册socket永远对不上”。

### 28.4 MIG策略改变resource name与数量

不同MIG策略可能暴露：

```text
nvidia.com/gpu
nvidia.com/mig-<profile>
```

不要固定只grep `nvidia.com/gpu`后就说“插件没上报任何资源”。先查看Node完整extended resources和插件实际配置。

MIG实例如何创建、重配和隔离留到第 21 课，本课只看它对ListAndWatch资源名/设备清单的影响。

### 28.5 device ID策略不等于resource name策略

NVIDIA插件可以用UUID或index等方式表达device ID；这改变后续分配标识，不会把 `nvidia.com/gpu`自动改成另一个resource name。

同理，`deviceListStrategy`主要影响第 16 课Allocate后怎样把已选device交给runtime：

```text
envvar
volume-mounts
cdi-annotations
cdi-cri
```

它不是“Node没有Capacity”时第一个要改的开关。

### 28.6 插件健康检测不等于完整硬件诊断

官方NVIDIA插件能报告部分GPU health，但不能替代：

- DCGM/Xid/ECC完整观测；
- NVLink/NVSwitch/Fabric Manager健康；
- 应用真实CUDA/NCCL测试；
- 长时间温度、功耗、错误趋势。

所以 `ListAndWatch Health=Healthy`是Kubernetes分配层事实，不是硬件全量体检报告。第 19 课再补。

---

## 29. 一次完整故障推演

### 29.1 证据

```text
10:00:00 host nvidia-smi -L有8卡
10:00:05 Plugin Pod Running，restartCount=0
10:00:06 插件日志：Starting to serve nvidia.com/gpu
10:00:07 插件日志：Registered ... with Kubelet
10:00:10 Node Capacity无nvidia.com/gpu
10:00:20 GPU Pod FailedScheduling
```

### 29.2 不能直接得出的结论

```text
不能说scheduler缓存坏
不能说DeviceManager一定收到8卡
不能说Registered日志就是first ListAndWatch
不能说host 8卡等于Kubernetes 8个healthy device
```

### 29.3 按源码补证据

1. 目标Node kubelet是否有 `Device plugin connected`；
2. 是否有 `State pushed`和 `Processed device updates`；
3. `totalCount/healthyCount`是多少；
4. 是否马上出现 `ListAndWatch ended unexpectedly`；
5. direct还是generic入口；
6. plugin endpoint是否同一socket；
7. Node Status patch有没有失败；
8. Node `resourceVersion`和Capacity何时变化。

### 29.4 两种可能分叉

#### 分叉 A：connected后没有first list

```text
断点：
  client.Run -> ListAndWatch建流/Recv

方向：
  插件server实现、初始化阻塞、stream错误、socket被替换
```

#### 分叉 B：processed healthyCount=8，但Node仍无

```text
断点：
  GetCapacity -> nodestatus.MachineInfo -> PatchNodeStatus

方向：
  node status sync/Patch错误、对象身份/Node名、传播时间
```

只有第二种才进入Node Status传播，而不是一开始就怪scheduler。

---

## 30. Java平台经验在这里怎样继续复用

篇幅只留一小段，因为本课已是GPU主案例。

你熟悉的：

```text
Java进程Running
  != Service endpoint ready
```

现在对应：

```text
Device Plugin进程Running
  != gRPC registered
  != first ListAndWatch processed
  != Node resource ready
```

你熟悉的：

```text
Node Allocatable CPU=64核
  != 还有64核可调度
```

现在对应：

```text
Node Allocatable GPU=8
  != 还有8个资源单位可调度
```

底层都是：

```text
总预算 - 已有Pod request账本
```

新知识只是“这个总预算怎样由Device Plugin健康逻辑entry形成”；它是否等于物理卡数取决于MIG/sharing等插件策略。

---

## 31. Go语法复习索引

| 写法 | 大白话 | 为什么影响源码结论 |
|---|---|---|
| `handler, ok := m[key]` | 查map并判断key是否存在 | 区分“没注册handler”和“handler执行失败” |
| `context.WithTimeout` | 为RPC加截止时间 | GetInfo不会无限挂住 |
| `defer cancel()` | 函数返回时释放context资源 | 常与timeout成对 |
| `go s.runClient(...)` | 后台启动长流 | 注册完成早于first list |
| `for { stream.Recv() }` | 持续阻塞收server stream | error后直接退出 |
| `make(map[...])` | 创建新map | 每份清单替换旧snapshot |
| `sets.New[string]()` | 创建单个集合内去重的集合 | 同一ID仍可能因冲突Health同时进入两个集合并被Capacity双计 |
| `m.mutex.Lock()` | 保护共享设备账 | ListAndWatch与Node status可能并发 |
| `defer m.mutex.Unlock()` | 退出前必解锁 | 避免early return漏解锁 |
| `*resource.NewQuantity(...)` | 构造Quantity指针后取值 | 写入ResourceList需要Quantity值 |

### 31.1 为什么锁内不做所有事情

`genericDeviceUpdateCallback`：

```text
锁内：
  重建共享map/set

解锁后：
  通知Pod更新
  写checkpoint
  打日志
```

大白话：

```text
只在修改共享账时占锁
慢I/O和channel通知尽量不长期堵住其他读写
```

但 `writeCheckpoint`内部还会重新加锁复制数据；读源码不能只看函数名猜“完全无锁”。

### 31.2 `sets.Set`为什么适合device ID

需要：

- 去重；
- `Insert`；
- `Len`；
- `Union`；
- 转list。

这比手写 `map[string]bool`更直接。要注意这里有healthy和unhealthy**两个**set：同ID同Health重复不会增加同一set的`Len()`；同ID冲突Health却可能各占一个set，求和时出现双计。正确修复在插件端保证ID唯一、Health一致，而不是依赖kubelet猜测哪条为真。

---

## 32. 哪些必须读深，哪些留到后面

### 本课必须S3

- 两套registration入口的真实并存；
- direct Register字段校验与endpoint拼接；
- generic watcher desired/actual/reconcile/GetInfo/Notify；
- 两条入口汇合到connectClient；
- GetDevicePluginOptions与ListAndWatch；
- full snapshot重建三张表；
- Health精确判断；
- Capacity/Allocatable公式；
- Node Status异步传播；
- disconnect立即unhealthy与5分钟移除；
- Event与指标证据边界。

### 本课只做边界

- NVIDIA插件内部NVML枚举/health实现；
- gRPC底层HTTP/2；
- plugin manager所有CSI/DRA分支；
- `DEPRECATION`兼容函数的静态死角与非法重复ID的全部异常组合（二读即可）；
- Topology Manager算法；
- checkpoint具体schema；
- scheduler NodeResourcesFit已在08～10学过，不重复读。

### 第 16 课再读

- requests里怎么识别device plugin resource；
- `devicesToAllocate`怎样选opaque ID；
- Topology hint；
- `GetPreferredAllocation`；
- `Allocate`请求与响应；
- env/mount/device/annotation/CDI怎样进入container。

### 第 17 课再读

- `kubelet_internal_checkpoint`；
- kubelet重启恢复；
- device health怎样暴露到Pod status；
- PodResources API；
- CDI device与已分配账本。

---

## 33. 本章自测

### 33.1 十四个必须口述的问题

1. 为什么Device Plugin Pod Running不能证明Node有GPU resource？
2. 当前commit的两套注册入口分别是什么？
3. direct Register和generic GetInfo的endpoint处理有什么差别？
4. direct与generic入口在socket消失后分别依靠什么机制重新注册？
5. `PluginConnected`后为什么还不能说资源已上报？
6. `ListAndWatch`为什么不是固定周期心跳？
7. 新response为什么会让上次有、本次没有的device消失？
8. 只有什么Health字符串会进入healthy集合？
9. 7 healthy + 1 unhealthy时Capacity/Allocatable各是多少？
10. Node Allocatable GPU=8、已有6块被请求时，为什么Allocatable字段仍可能是8？
11. 插件刚断连和断连超过宽限期的字段分别怎样变化？
12. 哪两个指标名字最容易被误读，它们实际证明什么？
13. 8张物理GPU、time-slicing每卡10个replica时，为什么`nvidia.com/gpu` Capacity可能是80？
14. resource从未注册时的absent，与活跃插件空清单/断连超宽限后的显式`0/0`，分别说明什么？

### 33.2 现场题一

```text
host有8卡
Processed device updates:
  totalCount=8
  healthyCount=7
Node:
  capacity=8
  allocatable=7
```

回答：

- 哪张表里有1个ID？
- scheduler能否给新Pod分配8个资源单位？
- 这是否证明对应的物理GPU消失？
- 已占用异常entry的Pod会不会被kubelet立刻自动删除？

### 33.3 现场题二

```text
10:00 ListAndWatch ended unexpectedly
10:00 Capacity=8 Allocatable=0
10:03 Capacity=8 Allocatable=0
10:06 Capacity=0 Allocatable=0
```

回答：

- 10:00发生了哪两个内部动作？
- 10:03为什么Capacity仍保留？
- 10:06是哪条宽限逻辑？
- 状态为什么不一定精确在第五分钟整出现在kubectl？

### 33.4 现场题三

```text
Node Capacity=8
Node Allocatable=8
已有6个Pod各request 1 GPU
新Pod request 3 GPU
```

回答：

- Node status字段是否自相矛盾？
- scheduler剩余账是多少？
- 为什么会FailedScheduling？
- `nvidia-smi`实时利用率为0能否改变结论？

### 33.5 通过标准

你能：

- 从插件Pod一路画到scheduler informer；
- 区分registered、connected、first list、Node patched；
- 手算五种容量变化；
- 从日志判断断在入口、stream、账本还是Node Status；
- 不把Allocatable当实时剩余；
- 不靠删除socket或重启生产组件做实验；

才算通过第 15 课。

---

## 34. 官方资料与版本校准

> 链接于 2026-07-13核对。生产必须锁定实际Kubernetes、NVIDIA插件和GPU Operator版本。

- [Kubernetes Device Plugins](https://kubernetes.io/docs/concepts/extend-kubernetes/compute-storage-net/device-plugins/): 协议角色、注册、ListAndWatch、扩展资源使用。
- [NVIDIA k8s-device-plugin](https://github.com/NVIDIA/k8s-device-plugin): 官方实现、部署、配置、MIG、device ID/list strategy。
- [Kubernetes当前Device Plugin API](https://github.com/kubernetes/kubernetes/tree/master/staging/src/k8s.io/kubelet/pkg/apis/deviceplugin/v1beta1): `Register`、`ListAndWatch`、`Allocate`等协议定义。
- [Kubernetes当前plugin manager](https://github.com/kubernetes/kubernetes/tree/master/pkg/kubelet/pluginmanager): 通用socket watcher、desired/actual state与registration operation。

本课源码结论以本地commit为准；GitHub `master`会继续变化，链接用于找项目，不替代commit校准。

---

## 35. 一页收口

```text
Device Plugin进程
  -> 监听自己的socket
  -> direct Register
     或 generic GetInfo/Notify
  -> connectClient
  -> GetDevicePluginOptions
  -> ListAndWatch
  -> full device snapshot
  -> all/healthy/unhealthy
  -> Capacity=healthy+unhealthy
  -> Allocatable=healthy
  -> Node Status patch
  -> scheduler informer/queue
```

三个“不等于”：

```text
Plugin Pod Running != registered
registered != first ListAndWatch processed
Node Allocatable != 当前未使用剩余量
```

下一课从这里接：

```text
Node已经上报 nvidia.com/gpu=8
GPU Pod已经调度到该Node
  -> kubelet怎样从container limits识别请求
  -> 怎样从healthy集合选具体opaque device ID
  -> Topology hint/GetPreferredAllocation怎样参与
  -> 何时调用Allocate
  -> 返回的env/mount/device/annotation/CDI
  -> 怎样进入RunContainerOptions与CRI ContainerConfig
```

第 16 课会把“数量资源”继续读成“某个container真正拿到哪块device”。
