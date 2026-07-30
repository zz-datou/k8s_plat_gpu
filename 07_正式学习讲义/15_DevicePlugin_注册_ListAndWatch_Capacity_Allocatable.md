# 第 15 课：Device Plugin 已经注册，为什么 Java 推理 Pod 仍然 FailedScheduling——从 ListAndWatch 读懂 Capacity 与 Allocatable

> 本课源码基线：Kubernetes commit `301946d15e67a4a2e8a5fb8292eb836acd366d78`（本地 `git describe` 为 `v1.37.0-alpha.0-280-g301946d15e6`）。
>
> 本课面向已经做过多年 Kubernetes 平台运维、但 Go 语法还不熟的学习者。重点不是背 Device Plugin 部署命令，而是弄明白：**一块宿主机看得见的 GPU，经过哪些状态账本，才会变成 scheduler 可以使用的 `nvidia.com/gpu`。**

---

## 0. 先把这一课的生产问题钉死

### 0.1 固定案例：只讨论这一条主线

游戏平台有一个 Java 推理服务：

```text
namespace: prod
Deployment: game-infer
container: infer-server
用途: Java 服务通过 JNI 调用 CUDA 推理
GPU request/limit: nvidia.com/gpu=1
```

新扩容了一台 GPU Node：

```text
Node: gpu-node-07
物理 GPU: 8 张
MIG: 关闭
time-slicing: 关闭
MPS: 关闭
DRA Extended Resource 映射: 未配置
历史 kubelet_internal_checkpoint: 不存在
```

为了让“8 张物理卡”和“8 个 Kubernetes 资源单位”在本课里一一对应，我们特意关闭 MIG 和共享策略。生产环境若开启 MIG 或 time-slicing，`nvidia.com/gpu` 的数量可能不再等于物理卡数，不能照抄这个等式。

目标节点上的 NVIDIA Device Plugin 固定为：

```text
namespace: gpu-operator
Pod: nvidia-device-plugin-daemonset-k7m2p
UID: 1d19c8c0-1111-2222-3333-777777777777
container: nvidia-device-plugin-ctr
image: nvcr.io/nvidia/k8s-device-plugin@sha256:TEACHING_DIGEST
containerID: containerd://9e8f7a6b5c4d
registration path: direct Registration.Register
kubelet socket: /var/lib/kubelet/device-plugins/kubelet.sock
plugin endpoint: nvidia-gpu.sock
resourceName: nvidia.com/gpu
```

`TEACHING_DIGEST`只是讲义里的脱敏占位值，不代表真实镜像；现场必须记录完整镜像 digest。

### 0.2 故障时已经确认的事实

```text
1. host 上 nvidia-smi -L 能看到 8 张 GPU。
2. Device Plugin Pod 是 Running，restartCount=0。
3. nvidia-gpu.sock 已存在，进程正在监听。
4. 插件日志显示已向 kubelet 发起 Register。
5. kubelet 日志出现：
   Got registration request...
   Connected to new client...
   Device plugin connected...
6. 已确认同一 boot ID、同一注册时间窗的 kubelet V(2) 日志采集完整，但没有出现：
   State pushed for device plugin...
   Processed device updates for resource...
7. Node status 中 nvidia.com/gpu 字段完全 absent，不是显式 0。
8. prod/game-infer 新 Pod 持续 FailedScheduling：
   Insufficient nvidia.com/gpu
```

因为这是新节点且没有历史 checkpoint，所以“字段 absent”与本案一致：kubelet 还没有形成第一份可发布的设备清单。

### 0.3 本课要验证的假设

```text
注册入口已经通过
  -> kubelet 已经反向连接插件
  -> GetDevicePluginOptions 已经成功
  -> 但 ListAndWatch 的第一份完整清单尚未被 kubelet 收到
  -> DeviceManager 里的 healthyDevices/unhealthyDevices 仍没有 nvidia.com/gpu
  -> GetCapacity 无法产出该资源
  -> Node Status 没有该字段
  -> scheduler 只能判定 Insufficient nvidia.com/gpu
```

什么证据可以推翻这个假设？

```text
若 kubelet 已经记录：
  State pushed ... resourceCapacity=8
  Processed device updates ... totalCount=8 healthyCount=8

但 Node status 仍长期 absent，
那么断点已经从 ListAndWatch 前移到了：
  GetCapacity -> Node status setter -> API Server patch
```

这就是源码排障的基本纪律：**先提出可证伪的链路假设，再找能把断点向前或向后移动的证据。**

---

## 1. 为什么 Kubernetes 要设计这么多层，而不是让插件直接改 Node

如果只看表面，这套链路很绕：

```text
GPU
  -> NVIDIA Device Plugin
  -> gRPC registration
  -> ListAndWatch
  -> kubelet DeviceManager 内存表
  -> Node Status
  -> API Server
  -> scheduler cache
```

但它不是为了“把简单事情复杂化”，而是在解决五个真实矛盾。

### 1.1 Kubernetes 核心不可能内置所有厂商驱动逻辑

如果 kubelet 直接理解 NVIDIA、AMD、Intel、FPGA、RDMA 和各种专用加速卡：

```text
每增加一种硬件
  -> kubelet 都要加厂商代码
  -> Kubernetes 发版节奏被厂商驱动节奏绑住
  -> 核心组件权限和依赖不断膨胀
```

Device Plugin 把边界切成：

```text
厂商插件负责：
  我能发现哪些设备
  每个设备 ID 是什么
  当前健康状态是什么
  分配时要注入什么

kubelet 负责：
  验证资源名与协议版本
  保存节点本地设备账
  保护分配过程
  把数量写进 Node Status
```

这叫**机制与厂商知识分离**。

### 1.2 为什么不能让插件自己 patch Node Status

假设每个厂商插件都能直接写 Node：

```text
插件 A 写 Capacity
插件 B 写 Allocatable
kubelet 同时更新 CPU/内存/临时盘
多个写者并发覆盖同一个 status
权限、重试、冲突和审计边界全部混在一起
```

现在的设计把 Node Status 的写权限收口给 kubelet：

```text
插件只报告设备事实
kubelet统一组装节点事实
API Server只看到一个节点代理的状态写入
```

代价是状态传播多了一跳，收益是**单写者、统一校验、统一重试和更清楚的责任边界**。

### 1.3 为什么 ListAndWatch 发完整清单，而不是只发增量事件

如果只发送：

```text
GPU-1 added
GPU-3 unhealthy
GPU-5 removed
```

一旦 kubelet 重连、插件重启或中间丢过事件，双方就可能永远对不上账。

完整快照的思路是：

```text
每次 response 都表达“我现在认可的全部设备”
旧快照整体被新快照替换
重连后再发一份完整清单即可重新收敛
```

它牺牲了一点网络和重建 map 的成本，换来了**幂等和最终收敛**。对于每个节点几十到几百个设备 entry，这个取舍通常很划算。

### 1.4 为什么 Capacity 包含 Unhealthy，而 Allocatable 只包含 Healthy

Kubernetes 希望同时表达两个事实：

```text
Capacity:
  这个插件当前登记过多少个设备单位

Allocatable:
  当前允许继续分配多少个健康设备单位
```

所以 8 个 entry 中 1 个 Unhealthy 时：

```text
Capacity = 8
Allocatable = 7
```

如果把坏设备直接从 Capacity 抹掉，平台会失去“硬件仍存在但不可分配”的差异；如果 Allocatable 也保留 8，scheduler 又可能继续把新 Pod 放到坏设备上。

### 1.5 为什么插件断开后不立刻把 Capacity 清零

插件短暂重启、socket 切换或节点负载抖动，不代表 8 张 GPU 在同一瞬间物理消失。当前源码保留 5 分钟停止宽限期：

```text
刚断开：Capacity 保留，Allocatable 立即变 0
宽限期后仍未恢复：Capacity 和 Allocatable 都变 0
```

这个设计的目标不是“保证业务不断”，而是避免一次瞬时插件抖动立刻把节点硬件身份从集群里抹掉，同时阻止新分配继续发生。

---

## 2. 先建立六本账，后面所有源码都往账上放

| 账本 | 谁维护 | 记录什么 | 本案故障时的状态 |
|---|---|---|---|
| 宿主机硬件账 | Driver/NVML | host 能看到哪些 GPU | 8 张 |
| 插件进程账 | kubelet/CRI | Plugin Pod、进程、socket 是否存在 | Running，socket 存在 |
| 注册关系账 | Device Plugin server | resourceName 对应哪个 gRPC client | 已 connected |
| 设备快照账 | DeviceManager | all/healthy/unhealthy device IDs | 尚未形成第一份快照 |
| Node 广告账 | kubelet Node status | Capacity/Allocatable | 字段 absent |
| 调度占用账 | scheduler | Node 广告量减去已绑定 Pod requests | 可用量 0 |

本章最关键的认识是：

```text
前一本账正确
  != 后一本账已经正确
```

例如：

```text
nvidia-smi 有 8 张卡
  != 插件已注册

插件已注册
  != 第一份 ListAndWatch 已处理

Node Allocatable=8
  != 当前还剩 8 个可调度单位
```

### 2.1 五条必须保持的系统不变量

1. 一个 resourceName 在 kubelet 内应对应一个当前有效 endpoint。
2. `allDevices`、`healthyDevices`、`unhealthyDevices`应来自同一份快照。
3. 同一个 device ID 在同一份插件清单中应唯一且健康状态一致。
4. `Capacity = healthy set 数量 + unhealthy set 数量`。
5. scheduler 的实时剩余量不写回 Node Allocatable，而由 scheduler 自己减去 Pod requests。

如果现场违反这些不变量，就不能只靠“重启插件试试”掩盖问题，要继续追查版本、配置或插件实现。

---

## 3. 本课怎么读：哪些读深，哪些只知道边界

### 第一遍：必须读到 S3

```text
direct Register
  -> connectClient
  -> GetDevicePluginOptions
  -> ListAndWatch first snapshot
  -> 重建三张设备表
  -> GetCapacity
  -> Node Capacity/Allocatable
  -> disconnect 两阶段收敛
```

这是 NVIDIA Device Plugin “资源为什么没出现在 Node 上”的主排障链。

### 第二遍：读到 S2 即可

- 通用 plugin watcher 的 `GetInfo -> Validate -> Register -> Notify`；
- checkpoint 的恢复与失败边界；
- `ResourceHealthStatus`怎样把已分配设备健康写进 Pod status；
- Device Plugin 注册指标与 generic plugin manager 指标。

### 本课一笔带过

- gRPC/HTTP2 底层帧；
- NVIDIA 插件内部 NVML 枚举实现；
- MIG Manager、time-slicing 和 MPS 的完整实现；
- DRA driver 的 claim 分配链；
- `Allocate`、Topology Manager 和 CDI 注入，留到第 16 课。

### 源码阅读约定

1. 源码路径都相对本地 `kubernetes/` 目录。
2. 代码以固定 commit `301946d...` 为准，不用滚动的 GitHub `master`代替。
3. 代码块中的中文 `//`是教学注释；删除这些中文注释后，业务语句与当前源码一致。
4. 为了让你先看懂状态变化，少数长函数只摘连续的关键区间；不会用省略号伪造不存在的控制流。
5. 每个源码块后都有“大白话总结”和“顺手学 Go”，不用先系统学完整本 Go 教材。

---

## 4. 入口之前：kubelet 先恢复旧账，再启动注册服务

源码：`pkg/kubelet/cm/devicemanager/manager.go::Start`

```go
// Start 接收 kubelet 已知的 Pod、容器和 source 就绪信息，然后启动 DeviceManager。
func (m *ManagerImpl) Start(logger klog.Logger, activePods ActivePodsFunc, sourcesReady config.SourcesReady, initialContainers containermap.ContainerMap, initialContainerRunningSet sets.Set[string]) error {
	// 记录 DeviceManager 开始启动；V(2) 表示需要相应日志级别才能看到。
	logger.V(2).Info("Starting Device Plugin manager")

	// 保存一个函数，后面需要时可读取当前活跃 Pod。
	m.activePods = activePods
	// 保存 kubelet 配置源是否已经就绪的判断器。
	m.sourcesReady = sourcesReady
	// 保存 kubelet 启动时已经知道的 container ID 映射。
	m.containerMap = initialContainers
	// 保存启动时仍在运行的 container ID 集合。
	m.containerRunningSet = initialContainerRunningSet

	// 先尝试从磁盘 checkpoint 恢复历史分配账。
	err := m.readCheckpoint(logger)
	// checkpoint 读取失败不会直接阻止 DeviceManager 启动。
	if err != nil {
		// kubelet 明确记录“分配信息可能不是最新”，然后继续启动。
		logger.Error(err, "Continue after failing to read checkpoint file. Device allocation info may NOT be up-to-date")
	}

	// 最后启动 Device Plugin registration gRPC server。
	return m.server.Start(logger)
}
```

**大白话总结：** kubelet 重启后不是直接等插件重新报卡，而是先读旧的分配账，再监听插件注册。checkpoint 失败属于“带风险继续”，不是“自动回到完全正确的空状态”。本案明确是新节点且没有历史 checkpoint，所以第一份 Node GPU 数量只能等插件新快照形成。

**顺手学 Go：**

- `func (m *ManagerImpl) Start`：`m`叫接收者，表示这是 `ManagerImpl`对象的方法，类似 Java 的实例方法。
- `err :=`：短变量声明，Go 根据右侧返回值推断 `err`类型。
- `if err != nil`：Go 用 `nil`表示“没有对象/没有错误值”；错误不为 `nil`就进入分支。
- `return m.server.Start(logger)`：直接把下层函数返回的 `error`原样返回。

### 4.1 这里体现了什么设计思想

checkpoint 是**恢复优化**，不是插件存活证明：

```text
checkpoint 告诉 kubelet：过去哪些设备分给过哪些容器
ListAndWatch 告诉 kubelet：插件现在认可哪些设备以及当前健康状态
```

把两者混成一本账会产生危险结论：旧 checkpoint 里有 8 个 ID，不代表当前插件和硬件已经健康。

---

## 5. 主入口：NVIDIA 插件怎样通过 direct Register 报到

本案走传统 direct registration：插件主动连接固定的 kubelet socket，然后发送 `RegisterRequest`。

```text
插件连接：/var/lib/kubelet/device-plugins/kubelet.sock
请求里携带：Version、Endpoint、ResourceName、Options
kubelet 回连：/var/lib/kubelet/device-plugins/nvidia-gpu.sock
```

当前 `server.Register`真正读取的是 `Version`、`Endpoint`和 `ResourceName`；它没有用 `RegisterRequest.Options`决定插件能力。能力协商发生在回连后的独立 `GetDevicePluginOptions` RPC，不能把这两份 Options 混成一步。

源码：`pkg/kubelet/cm/devicemanager/plugin/v1beta1/server.go::Register`

```go
// Register 是插件调用的注册 RPC；ctx 带着这次请求的上下文，r 是请求对象。
func (s *server) Register(ctx context.Context, r *api.RegisterRequest) (*api.Empty, error) {
	// 从 RPC context 中取出结构化 logger。
	logger := klog.FromContext(ctx)
	// 只说明 kubelet 收到注册请求，还不说明设备清单已经到达。
	logger.Info("Got registration request from device plugin with resource", "resourceName", r.ResourceName)
	// direct 注册计数器在校验之前就加一，因此“计数增加”不等于注册成功。
	metrics.DevicePluginRegistrationCount.WithLabelValues(r.ResourceName).Inc()

	// 先检查插件声明的 API 版本是否与 kubelet 兼容。
	if !s.isVersionCompatibleWithPlugin(r.Version) {
		// 构造“不支持版本”的错误。
		err := fmt.Errorf(errUnsupportedVersion, r.Version, api.SupportedVersions)
		// 日志保留 resourceName，方便区分不同插件。
		logger.Error(err, "Bad registration request from device plugin with resource", "resourceName", r.ResourceName)
		// 返回空响应和错误，后续不会建立 client。
		return &api.Empty{}, err
	}

	// 再检查 resourceName 是否符合扩展资源命名规则，例如 nvidia.com/gpu。
	if !v1helper.IsExtendedResourceName(core.ResourceName(r.ResourceName)) {
		// 构造非法资源名错误。
		err := fmt.Errorf(errInvalidResourceName, r.ResourceName)
		// 记录错误；此时依然没有连接插件 endpoint。
		logger.Error(err, "Bad registration request from device plugin")
		// 拒绝这次注册。
		return &api.Empty{}, err
	}

	// Endpoint 是插件报来的文件名；kubelet把它拼到固定 socket 目录后再反向连接。
	if err := s.connectClient(ctx, r.ResourceName, filepath.Join(s.socketDir, r.Endpoint)); err != nil {
		// 反向连接、GetDevicePluginOptions 等任一步失败都会到这里。
		logger.Error(err, "Error connecting to device plugin client")
		// 注册 RPC 返回失败。
		return &api.Empty{}, err
	}

	// 能走到这里，只能证明客户端连接阶段成功；第一份 ListAndWatch 仍可能没到。
	return &api.Empty{}, nil
}
```

**大白话总结：** `Register`不是“把 8 张 GPU 写进 Node”的 RPC。它只做报到、校验和建立回连。最容易误判的地方有两个：计数器在校验前就增加；`Register`返回成功时，后台 `ListAndWatch`才刚准备启动。

**顺手学 Go：**

- `r *api.RegisterRequest`：`r`是指向请求结构体的指针，避免复制整个对象。
- `!condition`：逻辑取反，相当于 Java 的 `!condition`。
- `if err := call(); err != nil`：把变量声明和判断写在同一个 `if`里，`err`只在这个 `if`作用域可见。
- `&api.Empty{}`：创建一个 `api.Empty`结构体并取得它的地址。
- `filepath.Join`：按操作系统路径规则安全拼接，不是普通字符串相加。

### 5.1 为什么 resourceName 必须是扩展资源名

插件不能注册一个裸名字 `gpu`去冒充 Kubernetes 内置资源。`nvidia.com/gpu`中的域名前缀形成了所有权边界：

```text
nvidia.com/gpu
└─ 厂商/组织域名 ─┘ └资源名┘
```

这避免第三方插件覆盖 `cpu`、`memory`等核心资源语义。

### 5.2 为什么 Endpoint 只报文件名

direct 路径由 kubelet掌握 socket 根目录，插件只报告 endpoint：

```text
s.socketDir = /var/lib/kubelet/device-plugins/
r.Endpoint  = nvidia-gpu.sock
最终路径    = /var/lib/kubelet/device-plugins/nvidia-gpu.sock
```

如果插件容器的 hostPath、mountPath 或 endpoint 参数对不上，Plugin Pod 可以是 Running，但 kubelet 回连仍然失败。

---

## 6. Register 成功之前，connectClient 实际完成了什么

源码：`pkg/kubelet/cm/devicemanager/plugin/v1beta1/handler.go::connectClient`

```go
// connectClient 为一个 resourceName 创建并连接 Device Plugin client。
func (s *server) connectClient(ctx context.Context, name string, socketPath string) error {
	// 获取本次注册链路使用的 logger。
	logger := klog.FromContext(ctx)
	// 先创建只保存 resourceName、socketPath 和回调接口的 client 对象。
	c := NewPluginClient(name, socketPath, s.chandler)

	// 先把 client 放入 server.clients；连接失败时下面会删掉。
	s.registerClient(logger, name, c)
	// Connect 会拨号，并同步调用 DeviceManager.PluginConnected。
	if err := c.Connect(ctx); err != nil {
		// 连接失败就回滚刚才登记的 client。
		s.deregisterClient(logger, name)
		// 记录具体 resourceName。
		logger.Error(err, "Failed to connect to new client", "resource", name)
		// 把错误传回 direct Register 或 generic handler。
		return err
	}

	// 到这里说明连接阶段成功，但 ListAndWatch 的第一份 response 还没处理。
	logger.V(2).Info("Connected to new client", "resource", name)
	// 启动一个新的 goroutine，在后台长期运行 ListAndWatch。
	go func() {
		// runClient 会一直等到 stream 结束，再执行 disconnect。
		s.runClient(ctx, name, c)
	}()

	// 主协程立即返回，因此注册完成与 first snapshot 之间天然存在时间差。
	return nil
}
```

**大白话总结：** `connectClient`先同步完成连接，再异步启动长流。也就是说：注册 RPC 可以先返回成功，而真正的设备清单仍在后台等待。这正是本案“已经 connected，但 Node 资源 absent”的源码基础。

**顺手学 Go：**

- `c := NewPluginClient(...)`：`:=`同时声明并赋值。
- `go func() { ... }()`：定义一个匿名函数并马上放到新的 goroutine 执行，可类比 Java 里把任务提交给线程执行，但 goroutine 更轻量。
- goroutine 启动后，当前函数不会等待它完成，所以后面的 `return nil`会先返回。
- 闭包会捕获外层的 `ctx`、`name`和 `c`。

### 6.1 Connect 为什么还会调用 PluginConnected

`client.Connect`完成 Unix socket gRPC 拨号后，会调用 handler：

```go
// Connect 建立 kubelet 到插件 endpoint 的 gRPC 连接。
func (c *client) Connect(ctx context.Context) error {
	// 获取这条连接链的 logger。
	logger := klog.FromContext(ctx)
	// dial 返回 DevicePlugin API client、底层连接和错误。
	client, conn, err := dial(ctx, c.socket)
	// 拨号失败就记录路径并返回。
	if err != nil {
		// socket 路径是关键证据，但外发日志时要脱敏节点目录信息。
		logger.Error(err, "Unable to connect to device plugin client with socket path", "path", c.socket)
		// 上层 connectClient 会删除刚登记的 client。
		return err
	}
	// 写共享字段前加锁，防止与 Disconnect 并发。
	c.mutex.Lock()
	// 保存底层 gRPC connection。
	c.grpc = conn
	// 保存生成的 DevicePlugin API client。
	c.client = client
	// 写完共享字段后解锁。
	c.mutex.Unlock()
	// 最后同步通知 DeviceManager；它还会调用 GetDevicePluginOptions。
	return c.handler.PluginConnected(ctx, c.resource, c)
}
```

**大白话总结：** “socket 能 dial 通”仍然不是完整成功。`PluginConnected`如果读取插件选项失败，`Connect`就返回错误，direct Register 也会失败。

**顺手学 Go：**

- `client, conn, err := dial(...)`：Go 函数可以返回多个值，这里一次接三个。
- `c.mutex.Lock()` / `Unlock()`：保护 `c.grpc`和 `c.client`这两个会被并发访问的字段。
- `return c.handler.PluginConnected(...)`：把下层的错误直接交给调用者，不额外包装。

### 6.2 PluginConnected 先读取插件能力，再保存 endpoint

源码：`pkg/kubelet/cm/devicemanager/manager.go::PluginConnected`

```go
// PluginConnected 在连接建立阶段把插件能力和 endpoint 放进 DeviceManager。
func (m *ManagerImpl) PluginConnected(ctx context.Context, resourceName string, p plugin.DevicePlugin) error {
	// 从调用链 context 中取得 logger。
	logger := klog.FromContext(ctx)
	// 同步调用插件的 GetDevicePluginOptions；当前函数自己没有另加独立超时。
	options, err := p.API().GetDevicePluginOptions(ctx, &pluginapi.Empty{})
	// 读取能力失败就拒绝进入 endpoints 账本。
	if err != nil {
		// 包装错误，告诉上层失败发生在 options 阶段。
		return fmt.Errorf("failed to get device plugin options: %v", err)
	}

	// 用已经连接的 plugin client 创建 endpoint 实现。
	e := newEndpointImpl(p)

	// 修改共享 endpoints map 前加锁。
	m.mutex.Lock()
	// defer 表示函数退出时一定执行 Unlock。
	defer m.mutex.Unlock()
	// resourceName 作为 key，保存 endpoint 和插件选项。
	m.endpoints[resourceName] = endpointInfo{e, options}

	// 这条日志证明 endpoint 已保存，仍不证明 ListAndWatch 已经产生设备快照。
	logger.V(2).Info("Device plugin connected", "resourceName", resourceName)
	// 连接阶段成功。
	return nil
}
```

**大白话总结：** 本案已经看到 `Device plugin connected`，因此可以把断点放到 `GetDevicePluginOptions`之后。但这条日志之前还没启动 `ListAndWatch`接收循环，所以不能据此宣称 8 个 device ID 已进入 kubelet。

**顺手学 Go：**

- `defer m.mutex.Unlock()`：无论后面正常返回还是提前返回，函数结束前都解锁。
- `endpointInfo{e, options}`：按字段声明顺序构造结构体；阅读时要回到类型定义确认两个位置各代表什么。
- `fmt.Errorf`：构造带上下文的错误字符串；这里使用 `%v`嵌入原错误。

### 6.3 一个很容易抄错的超时结论

通用 plugin watcher 在调用 `GetInfo`时显式使用 1 秒 timeout；但上面的 `GetDevicePluginOptions`没有在该函数内部再创建一个 1 秒 timeout。

```text
generic GetInfo 有 1 秒 timeout
  != DevicePlugin GetDevicePluginOptions 也有同一个 timeout
```

direct 路径沿用传入的 RPC context；generic handler 当前又用背景 context 调用 `connectClient`。排障时必须按实际入口分别看，不能把一个函数的 timeout 记到另一个函数头上。

---

## 7. 本案真正断住的地方：ListAndWatch 第一份快照

Device Plugin API 把 `ListAndWatch`定义为 server-streaming：kubelet发一个空请求，插件可以持续返回多份完整设备清单。

当前源码在这里使用了 `context.TODO()`。下面仍是 Go 源码，使用 `text`围栏只是为了把这个上游待改进点保留下来，不把它当成推荐写法。

源码：`pkg/kubelet/cm/devicemanager/plugin/v1beta1/client.go::Run`

```text
// Run 在一个后台 goroutine 里维护 ListAndWatch 接收循环。
func (c *client) Run(ctx context.Context) {
    // 取出 logger；ctx 主要用于日志上下文。
    logger := klog.FromContext(ctx)
    // 当前源码没有把传入 ctx 直接交给长流，而是使用 context.TODO()。
    stream, err := c.client.ListAndWatch(context.TODO(), &api.Empty{})
    // 建流失败就记录错误并退出，不在这个函数内部自动重连。
    if err != nil {
        logger.Error(err, "ListAndWatch ended unexpectedly for device plugin", "resource", c.resource)
        return
    }

    // 建流成功后进入无限接收循环。
    for {
        // Recv 会阻塞，直到插件推来下一份 response 或 stream 出错。
        response, err := stream.Recv()
        // stream 结束或接收失败就退出循环。
        if err != nil {
            logger.Error(err, "ListAndWatch ended unexpectedly for device plugin", "resource", c.resource)
            return
        }
        // resourceCapacity 只是本次 response.Devices 的长度。
        logger.V(2).Info("State pushed for device plugin", "resource", c.resource, "resourceCapacity", len(response.Devices))
        // 把整份 response 交给 DeviceManager 重建设备账。
        c.handler.PluginListAndWatchReceiver(logger, c.resource, response)
    }
}
```

**大白话总结：** `ListAndWatch`不是 kubelet 每隔几秒去轮询一次，也不是注册 RPC 顺带带回设备列表。它是一条独立长流。`Recv()`在第一份 response 到来之前可以一直阻塞，所以 Plugin Pod Running、Register 成功、connected 成功都可能与 Node 资源 absent 同时成立。

**顺手学 Go：**

- `for {}`：没有条件的无限循环，相当于 Java 的 `while (true)`。
- `stream.Recv()`：阻塞式调用；没有消息时 goroutine 会等待，不会自动返回空列表。
- `len(response.Devices)`：读取 slice 长度；它是输入条目数，不等于去重后的 Capacity。
- `context.TODO()`：表示调用者知道这里应该有更合适的 context，但当前还没有整理好；它不是 timeout。

### 7.1 为什么 first snapshot 是独立里程碑

把时间线精确拆开：

```text
T0 Plugin 进程启动并监听 nvidia-gpu.sock
T1 Plugin 调用 direct Register
T2 kubelet 校验版本和 resourceName
T3 kubelet dial 插件 endpoint
T4 GetDevicePluginOptions 成功
T5 Device plugin connected 日志出现
T6 后台 goroutine 调用 ListAndWatch
T7 第一份 response 到达
T8 DeviceManager 重建三张设备表
T9 下次 Node Status 组装时读取 Capacity/Allocatable
T10 API Server 中 Node status 更新
T11 scheduler cache 观察到新 Node 资源
```

本案证据只到 T5，所以最小断点是 T6～T7，不应该直接跳到 scheduler。

### 7.2 当前 client.Run 自己没有重连循环

`Recv()`报错后 `Run`直接返回；`runClient`随后 disconnect。重新注册依赖插件或通用 watcher 再次触发，不是这个 `for`循环悄悄重拨。

因此生产日志里的这条信息很重要：

```text
ListAndWatch ended unexpectedly
```

它不是普通心跳丢一拍，而是当前这条 stream 已经结束。

---

## 8. 一份 response 怎样替换成三张设备表

三张表的职责：

```text
allDevices[resourceName][deviceID] = 完整 Device 对象
healthyDevices[resourceName]       = 可继续分配的 ID 集合
unhealthyDevices[resourceName]     = 已登记但不可继续分配的 ID 集合
```

源码：`pkg/kubelet/cm/devicemanager/manager.go::genericDeviceUpdateCallback`

```go
// genericDeviceUpdateCallback 接收某个 resourceName 的一整份设备快照。
func (m *ManagerImpl) genericDeviceUpdateCallback(logger klog.Logger, resourceName string, devices []*pluginapi.Device) {
	// healthyCount 只用于最后的日志；它按输入条目递增。
	healthyCount := 0
	// 重建共享设备表前加锁。
	m.mutex.Lock()
	// 为本资源创建一个全新的 healthy ID 集合，旧集合被替换。
	m.healthyDevices[resourceName] = sets.New[string]()
	// 为本资源创建一个全新的 unhealthy ID 集合，旧集合被替换。
	m.unhealthyDevices[resourceName] = sets.New[string]()
	// 暂存上一次完整设备 map，后面用于比较健康变化。
	oldDevices := m.allDevices[resourceName]
	// 收集需要刷新 Pod status 的 Pod UID。
	podsToUpdate := sets.New[string]()
	// 为本资源创建一个全新的 allDevices map，表示 full snapshot 替换。
	m.allDevices[resourceName] = make(map[string]*pluginapi.Device)
	// 逐个处理插件本次上报的设备 entry。
	for _, dev := range devices {

		// 只有启用 ResourceHealthStatus 时才计算哪些已分配 Pod 需要刷新健康状态。
		if utilfeature.DefaultFeatureGate.Enabled(features.ResourceHealthStatus) {
			// 定义一个局部函数：device 已分配给 Pod 时，把 Pod UID 放入待更新集合。
			updatePodUIDFn := func(deviceID string) {
				// 通过分配账反查这个 device ID 对应的 Pod 和 container。
				podUID, _ := m.podDevices.getPodAndContainerForDevice(deviceID)
				// 空字符串表示没有找到已分配 Pod。
				if podUID != "" {
					// 集合 Insert 会去重，同一个 Pod 只需通知一次。
					podsToUpdate.Insert(podUID)
				}
			}
			// 如果旧快照里也有这个 ID，就比较健康状态是否变化。
			if oldDev, ok := oldDevices[dev.ID]; ok {
				// 只有健康字符串变化时才通知已分配 Pod。
				if oldDev.Health != dev.Health {
					// 记录需要重算 status 的 Pod。
					updatePodUIDFn(dev.ID)
				}
			} else {
				// 本次新出现的 ID 也可能是“消失后重新回来”的已分配设备。
				updatePodUIDFn(dev.ID)
			}
		}

		// 以 device ID 为 key 写入新的完整设备 map。
		m.allDevices[resourceName][dev.ID] = dev
		// 只有健康字符串精确等于协议常量 Healthy 才进入 healthy 集合。
		if dev.Health == pluginapi.Healthy {
			// 插入 healthy set；同一 ID 重复插入不会增加 set 长度。
			m.healthyDevices[resourceName].Insert(dev.ID)
			// 日志计数按输入条目加一，因此异常重复 ID 可能让日志值与 set 长度不同。
			healthyCount++
		} else {
			// 其他任何健康字符串都进入 unhealthy set。
			m.unhealthyDevices[resourceName].Insert(dev.ID)
		}
	}
	// 三张内存表已经替换完成，先释放主锁。
	m.mutex.Unlock()

	// 只有启用 ResourceHealthStatus 才发送 Pod status 刷新通知。
	if utilfeature.DefaultFeatureGate.Enabled(features.ResourceHealthStatus) {
		// 有受影响 Pod 时才尝试写 channel。
		if len(podsToUpdate) > 0 {
			// select 加 default 表示 channel 满时不阻塞这条设备更新链。
			select {
			// 把去重后的 Pod UID 列表发给资源更新消费者。
			case m.update <- resourceupdates.Update{PodUIDs: podsToUpdate.UnsortedList()}:
			// channel 满时丢弃本次通知并记录错误。
			default:
				// 设备内存账已经更新；这里失败不会回滚刚才的三张表。
				logger.Error(goerrors.New("device update channel is full"), "discard pods info", "podsToUpdate", podsToUpdate.UnsortedList())
			}
		}
	}

	// 把当前分配账和已注册 healthy IDs 写入 checkpoint。
	if err := m.writeCheckpoint(logger); err != nil {
		// checkpoint 失败只记录日志，不回滚已经生效的内存快照。
		logger.Error(err, "Writing checkpoint encountered")
	}
	// 最后记录输入条目数和按输入统计的 healthyCount。
	logger.V(2).Info("Processed device updates for resource", "resourceName", resourceName, "totalCount", len(devices), "healthyCount", healthyCount)
}
```

**大白话总结：** 每一份 response 都不是在旧表上“加几个、减几个”，而是先创建新的集合和 map，再用本次清单重新填满。本次没有出现的旧 ID 会从新快照中消失。内存表先成功，Pod 更新通知和 checkpoint 后做；后两者失败都不会把设备表回滚。

**顺手学 Go：**

- `make(map[string]*pluginapi.Device)`：创建一个可写 map；只声明不 `make`的 nil map 不能直接写 key。
- `for _, dev := range devices`：遍历 slice，`_`表示丢弃下标，只保留元素。
- `oldDev, ok := oldDevices[dev.ID]`：map 的 comma-ok 写法；`ok=false`表示 key 不存在。
- `func(deviceID string) { ... }`：局部匿名函数，可捕获外层的 `podsToUpdate`和 `m`。
- `select { case ch <- value: default: }`：尝试非阻塞发送；channel 满时走 `default`。

### 8.1 full snapshot 的四个精确后果

#### 后果一：旧 ID 本次不出现，就从三张新表中消失

这不是显式执行 `delete(oldID)`，而是因为新 map 从空开始重建。

#### 后果二：旧 ID 消失不会在本次循环里触发 Pod 更新通知

循环只遍历**新清单中的 device**。如果 `GPU-bbbb`存在于旧快照、却完全不在新快照中，这一轮不会进入它的比较分支，也就不会因为“消失”直接把已分配 Pod UID 放入 `podsToUpdate`。

如果该 ID 以后重新出现，`else`分支才会尝试通知对应 Pod。

#### 后果三：checkpoint 失败不等于 Node 数量一定没有更新

顺序是：

```text
先改内存表
  -> 解锁
  -> 尝试通知 Pod
  -> 尝试写 checkpoint
  -> 失败只记日志
```

因此 checkpoint 写失败时，当前进程中的 `GetCapacity`仍可能读到新数量；真正增加的是 kubelet 重启后的恢复风险。

#### 后果四：插件必须保证 device ID 唯一且健康状态一致

同一 ID 重复且都是 Healthy：set 会去重，但日志里的 `healthyCount`可能按重复条目增长。

同一 ID 一条 Healthy、一条 Unhealthy：它可能同时进入两个 set，Capacity 求和时出现双计。kubelet不会替插件猜哪一条才是真相，正确修复应在插件端保证快照合法。

---

## 9. 从三张表算出 Capacity 与 Allocatable

先记住公式：

```text
Capacity(resource)    = len(healthyDevices) + len(unhealthyDevices)
Allocatable(resource) = len(healthyDevices)
```

源码：`pkg/kubelet/cm/devicemanager/manager.go::GetCapacity`

当前函数为了打日志使用了 `klog.TODO()`。下面用 `text`围栏保留这个上游标记，避免把它误当成推荐的 context 写法。

```text
// GetCapacity 在 kubelet 组装 Node Status 时读取 DeviceManager 的资源数量。
func (m *ManagerImpl) GetCapacity() (v1.ResourceList, v1.ResourceList, []string) {
	// 当前函数没有 logger 参数，源码临时取得一个背景 logger。
	logger := klog.TODO()
	// 创建返回用的 Capacity map。
	var capacity = v1.ResourceList{}
	// 创建返回用的 Allocatable map。
	var allocatable = v1.ResourceList{}
	// 记录已经超过停止宽限期、需要从内部清理的 resourceName。
	deletedResources := sets.New[string]()
	// 读取和清理共享设备表前加锁。
	m.mutex.Lock()
	// 先遍历每种资源的 healthy ID 集合。
	for resourceName, devices := range m.healthyDevices {
		// 查找该 resourceName 当前的 endpoint。
		eI, ok := m.endpoints[resourceName]
		// endpoint 不存在，或已停止且宽限期到期，就把资源标为待删除。
		if (ok && eI.e.stopGracePeriodExpired()) || !ok {
			// endpoint 不存在说明内部表出现了不一致。
			if !ok {
				// 记录异常，但仍继续收敛清理。
				logger.Info("Unexpected: healthyDevices and endpoints are out of sync")
			}
			// 先记入集合，后面统一删除。
			deletedResources.Insert(resourceName)
		} else {
			// Capacity 先写 healthy 数量。
			capacity[v1.ResourceName(resourceName)] = *resource.NewQuantity(int64(devices.Len()), resource.DecimalSI)
			// Allocatable 只写 healthy 数量。
			allocatable[v1.ResourceName(resourceName)] = *resource.NewQuantity(int64(devices.Len()), resource.DecimalSI)
		}
	}
	// 再遍历每种资源的 unhealthy ID 集合。
	for resourceName, devices := range m.unhealthyDevices {
		// 同样检查 endpoint 是否还在宽限期内。
		eI, ok := m.endpoints[resourceName]
		// 已到期或 endpoint 丢失时标记待删除。
		if (ok && eI.e.stopGracePeriodExpired()) || !ok {
			// endpoint 丢失说明内部账不一致。
			if !ok {
				// 记录异常但继续清理。
				logger.Info("Unexpected: unhealthyDevices and endpoints became out of sync")
			}
			// 资源进入待删除集合。
			deletedResources.Insert(resourceName)
		} else {
			// 取出前面已经写入的 healthy Capacity；不存在时得到零值 Quantity。
			capacityCount := capacity[v1.ResourceName(resourceName)]
			// 把 unhealthy set 的长度构造成 Quantity。
			unhealthyCount := *resource.NewQuantity(int64(devices.Len()), resource.DecimalSI)
			// Capacity 加上 unhealthy 数量；Allocatable 不加。
			capacityCount.Add(unhealthyCount)
			// 写回最终 Capacity。
			capacity[v1.ResourceName(resourceName)] = capacityCount
		}
	}

	// 对超过宽限期的资源统一清理 endpoint 和两张健康集合。
	for resourceName := range deletedResources {
		// 删除 endpoint 关系。
		delete(m.endpoints, resourceName)
		// 删除 healthy set。
		delete(m.healthyDevices, resourceName)
		// 删除 unhealthy set。
		delete(m.unhealthyDevices, resourceName)
	}

	// 内存读取和清理完成，释放主锁。
	m.mutex.Unlock()

	// 只有实际清理了资源，才需要重写 checkpoint。
	if deletedResources.Len() > 0 {
		// checkpoint 失败只记录错误，不回滚刚才的内存删除。
		if err := m.writeCheckpoint(logger); err != nil {
			// 这意味着当前状态可能已清理，但重启恢复文件没有同步成功。
			logger.Error(err, "Failed to write checkpoint file")
		}
	}
	// 返回 Capacity、Allocatable 和需要在 Node status 写 0 的已删除资源名。
	return capacity, allocatable, deletedResources.UnsortedList()
}
```

**大白话总结：** `GetCapacity`不看 `nvidia-smi`，也不直接调用插件。它只读 DeviceManager 已经收敛好的集合。**在本案这种新节点、没有可读 checkpoint 的前提下**，没有 first snapshot 就没有对应 resource key；若旧 checkpoint 成功恢复，则 first snapshot 前也可能已经存在 stopped endpoint 和空集合，暂时呈现 0/0。8 healthy + 0 unhealthy 得到 8/8；7 healthy + 1 unhealthy 得到 8/7；endpoint 断开并超过宽限期后，资源进入 deletedResources 并从内存表清掉。

**顺手学 Go：**

- `v1.ResourceList{}`：创建一个空 map，key 是资源名，value 是 `resource.Quantity`。
- `sets.New[string]()`：Go 泛型函数，创建元素类型为 `string`的集合。
- `for resourceName, devices := range map`：同时取得 map 的 key 和 value；遍历顺序不保证固定。
- `delete(m.endpoints, resourceName)`：删除 map key；key 不存在也不会 panic。
- `*resource.NewQuantity(...)`：函数返回指针，前面的 `*`取出指针指向的 Quantity 值，因为 ResourceList 保存的是值。

### 9.1 五个场景必须会手算

| 场景 | healthy set | unhealthy set | Capacity | Allocatable |
|---|---:|---:|---:|---:|
| 第一份 8 个都 Healthy | 8 | 0 | 8 | 8 |
| 其中 1 个变 Unhealthy | 7 | 1 | 8 | 7 |
| 插件发空的完整清单 | 0 | 0 | 0 | 0 |
| 插件刚断连，仍在宽限期 | 0 | 8 | 8 | 0 |
| 断连超过宽限期 | 资源被清理 | 资源被清理 | Node 后续写 0 | Node 后续写 0 |

### 9.2 Allocatable 不是“当前剩余未使用 GPU”

假设 Node Status：

```text
Capacity nvidia.com/gpu = 8
Allocatable nvidia.com/gpu = 8
```

已有 6 个已绑定 Pod 各 request 1：

```text
scheduler 账本剩余 = Node Allocatable 8 - 已有 Pod requests 6 = 2
```

此时一个 request 3 GPU 的新 Pod 会 FailedScheduling，但 Node 的 Allocatable 字段仍然可以是 8。scheduler 不会把每次绑定后的剩余量反写到 Node Status。

这和你熟悉的 CPU 完全一样：

```text
Node Allocatable CPU=64
  != 现在还空闲 64 核

Node Allocatable GPU=8
  != 现在还剩 8 个 GPU 资源单位
```

`nvidia-smi`显示利用率 0 也不能改变 scheduler 的 request 账本。

---

## 10. 数量怎样写进 Node Status

DeviceManager 返回三样东西：

```text
devicePluginCapacity
devicePluginAllocatable
removedDevicePlugins
```

kubelet 的 Node status setter 再把它们合并进节点状态。

### 10.1 先写 Capacity；被移除的旧资源写显式 0

源码：`pkg/kubelet/nodestatus/setters.go`

```go
// 调用 ContainerManager，最终会进入 DeviceManager.GetCapacity。
devicePluginCapacity, devicePluginAllocatable, removedDevicePlugins = devicePluginResourceCapacityFunc()
// 把当前仍活跃的 Device Plugin Capacity 逐项写入 Node status。
for k, v := range devicePluginCapacity {
	// 数量新出现或发生变化时记录日志。
	if old, ok := node.Status.Capacity[k]; !ok || old.Value() != v.Value() {
		// 这条日志只说明 setter 看到了变化，还要继续观察 API 更新是否成功。
		logger.V(2).Info("Updated capacity for device plugin", "plugin", k, "capacity", v.Value())
	}
	// 覆盖 Node status 中对应资源的 Capacity。
	node.Status.Capacity[k] = v
}

// 遍历已经超过宽限期并从 DeviceManager 内存表移除的资源名。
for _, removedResource := range removedDevicePlugins {
	// 记录将旧 Device Plugin 资源设为 0。
	logger.V(2).Info("Set capacity for removed resource to 0 on device removal", "device", removedResource)
	// 不删除字段，而是写显式 0，保留“它曾由 Device Plugin 管理”的区别。
	node.Status.Capacity[v1.ResourceName(removedResource)] = *resource.NewQuantity(int64(0), resource.DecimalSI)
}
```

**大白话总结：** 新节点从未形成过资源时，字段可以 absent；一个曾经注册过、后来超过宽限期被清理的资源，则会被写成显式 `0`。所以 absent 和 `0/0`不是完全相同的历史状态。

**顺手学 Go：**

- `for k, v := range devicePluginCapacity`：遍历 ResourceList map。
- `old, ok := node.Status.Capacity[k]`：同时拿旧值和“是否存在”。
- `!ok || old.Value() != v.Value()`：先判断 key 不存在，或数量发生变化。
- `int64(0)`：把整数常量显式转换成 `int64`。

### 10.2 再用 Device Plugin 的 healthy 数量覆盖 Allocatable

```go
// 遍历 DeviceManager 算出的、只含 healthy 数量的 Allocatable。
for k, v := range devicePluginAllocatable {
	// 新出现或数量变化时记录日志。
	if old, ok := node.Status.Allocatable[k]; !ok || old.Value() != v.Value() {
		// 日志中的 device 是 resourceName，不是具体 GPU UUID。
		logger.V(2).Info("Updated allocatable", "device", k, "allocatable", v.Value())
	}
	// 覆盖对应扩展资源的 Node Allocatable。
	node.Status.Allocatable[k] = v
}
```

**大白话总结：** CPU/内存 Allocatable 会考虑节点保留；Device Plugin 扩展资源则由它自己的 healthy set 数量覆盖。这个值仍是节点可分配上限，不是 scheduler 的实时余额。

**顺手学 Go：**

- 这里的 `k`类型是 `v1.ResourceName`，`v`类型是 `resource.Quantity`。
- `node.Status.Allocatable[k] = v`是 map 赋值；同 key 的旧值会被覆盖。
- `Value()`把 Quantity 读成整数值，适合 Device Plugin 这种不可超卖的小整数资源单位。

### 10.3 Node Lease 为什么帮不上这个问题

Node Lease 主要是轻量心跳，不携带完整 Capacity/Allocatable。看到 Node Ready、Lease 持续更新，只说明 kubelet 心跳链大体还活着，不证明 GPU 数量已经写入 Node Status。

### 10.4 resourceVersion 和 managedFields 怎么用

```text
resourceVersion：
  可以判断对象更新的先后顺序
  不能直接当成墙上时钟

managedFields.time：
  不能可靠代表“GPU Capacity 写入的精确时刻”
```

需要精确时间线时，用持续 watch 的本地采样时间、API audit、kubelet 原始日志和插件日志对齐；不要从 managedFields 猜一个不存在的精确写入时刻。

---

## 11. 回到主案例：断点现在已经可以精确定位

把现有证据放进源码链：

| 里程碑 | 需要的证据 | 本案状态 | 能说明什么 |
|---|---|---|---|
| host 枚举 | `nvidia-smi -L` 8 张 | 已通过 | Driver/NVML 能看见硬件 |
| plugin 进程 | Pod UID、image digest、containerID、socket listener | 已通过 | 插件进程和 socket 存在 |
| Register 请求 | direct registration 日志/计数 | 已通过 | kubelet 收到请求，不代表校验后成功 |
| endpoint connected | `Device plugin connected` | 已通过 | dial 和 GetDevicePluginOptions 成功 |
| first snapshot | `State pushed` + `Processed device updates` | **缺失** | 三张设备表尚无形成证据 |
| Node 广告 | Capacity/Allocatable | absent | GetCapacity 尚未产出或未传播 |
| scheduler | FailedScheduling | 失败 | scheduler 当前账本没有可用资源 |

因此本案结论不是“scheduler 有 bug”，而是：

```text
断点位于 ListAndWatch 建流/首包阶段；
在 first snapshot 被证明到达之前，
没有必要先追 scheduler cache。
```

### 11.1 修复后应看到的闭环

假设最终发现插件初始化 goroutine 卡在设备发现，修复配置并由批准的 canary 变更重新发布后：

```text
T1 新 Plugin Pod 启动并使用正确 image/config
T2 direct Register 成功
T3 Device plugin connected
T4 first ListAndWatch 返回 8 个唯一、Healthy 的 device IDs
T5 Processed device updates: totalCount=8 healthyCount=8
T6 Node Capacity=8 Allocatable=8
T7 scheduler cache 观察到新 Node 资源
T8 prod/game-infer Pod 被调度到 gpu-node-07
```

到 T6 只能说明数量链恢复；T8 之后还要在第 16 课继续验证具体 device ID 的选择、Allocate、CDI/runtime 注入和容器内 CUDA 可见性。

---

## 12. 对照分支：当前 kubelet 为什么还有 generic plugin watcher

当前源码同时保留两套 Device Plugin 注册入口：

| 入口 | 谁先发起 | 发现目录 | 身份信息从哪里来 | 典型用途 |
|---|---|---|---|---|
| direct registration | 插件主动调用 kubelet | 固定 `/var/lib/kubelet/device-plugins/kubelet.sock` | `RegisterRequest` | 主流传统 Device Plugin，包括本课 NVIDIA 主案 |
| generic watcher | kubelet 发现插件 socket 后反向询问 | kubelet root 下的 `plugins_registry` | `GetInfo` | 统一 plugin manager 管理的插件注册框架 |

两条入口不是两套设备账。它们最后都会进入：

```text
DeviceManager server.connectClient
  -> client.Connect
  -> DeviceManager.PluginConnected
  -> client.Run/ListAndWatch
```

因此排障时要先确认入口，再从汇合点继续追。

### 12.1 generic 注册事务为什么更长

generic watcher 只知道“某个 Unix socket 出现了”，还不知道它是什么插件，所以必须反向问：

```text
watch socket
  -> desired state
  -> reconciler
  -> dial registration API
  -> GetInfo
  -> 按 Type 找 handler
  -> ValidatePlugin
  -> 写 actual state
  -> handler.RegisterPlugin
  -> NotifyRegistrationStatus
```

核心源码：`pkg/kubelet/pluginmanager/operationexecutor/operation_generator.go`

```go
// 为 GetInfo 单独创建 1 秒截止时间；它只属于 generic registration API。
ctxWithTimeout, cancel := context.WithTimeout(ctx, time.Second)
// 函数退出时释放 timer 相关资源。
defer cancel()

// 反向调用插件的 GetInfo，取得 Name、Type、Endpoint 和支持版本。
infoResp, err := client.GetInfo(ctxWithTimeout, &registerapi.InfoRequest{})
// GetInfo 超时或失败就终止这一轮 generic 注册。
if err != nil {
	// 返回的错误保留 socketPath，方便定位是哪一个注册 socket。
	return fmt.Errorf("RegisterPlugin error -- failed to get plugin info using RPC GetInfo at socket %s, err: %v", socketPath, err)
}

// 用插件声明的 Type 在 handler map 中查找消费者。
handler, ok := pluginHandlers[infoResp.Type]
// 没有对应 handler 时不能继续把它当成 Device Plugin。
if !ok {
	// 先通知插件注册失败；通知本身也可能失败。
	if err := og.notifyPlugin(ctx, client, false, fmt.Sprintf("RegisterPlugin error -- no handler registered for plugin type: %s at socket %s", infoResp.Type, socketPath)); err != nil {
		// 若失败通知也发不出去，返回更具体的错误。
		return fmt.Errorf("RegisterPlugin error -- failed to send error at socket %s, err: %v", socketPath, err)
	}
	// 通知成功后仍然返回“没有 handler”的原始注册错误。
	return fmt.Errorf("RegisterPlugin error -- no handler registered for plugin type: %s at socket %s", infoResp.Type, socketPath)
}

// 插件没有另报业务 endpoint 时，就直接使用 watcher 发现的 socketPath。
if infoResp.Endpoint == "" {
	// 空字符串在这里有明确语义，不等于随便补默认值。
	infoResp.Endpoint = socketPath
}
// 交给 Type 对应的 handler 校验名字、endpoint 和协议版本。
if err := handler.ValidatePlugin(infoResp.Name, infoResp.Endpoint, infoResp.SupportedVersions); err != nil {
	// 校验失败时通知插件失败状态。
	if err = og.notifyPlugin(ctx, client, false, fmt.Sprintf("RegisterPlugin error -- plugin validation failed with err: %v", err)); err != nil {
		// 失败通知也失败时，返回通知错误。
		return fmt.Errorf("RegisterPlugin error -- failed to send error at socket %s, err: %v", socketPath, err)
	}
	// 校验失败结束本轮事务。
	return fmt.Errorf("RegisterPlugin error -- pluginHandler.ValidatePluginFunc failed")
}
```

**大白话总结：** generic watcher 发现 socket 只是“发现一个门牌”，`GetInfo`才问清它叫什么、是什么类型。这里的 1 秒 timeout 只保护 `GetInfo`，不能拿来解释 direct 路径里的 `GetDevicePluginOptions`。

**顺手学 Go：**

- `context.WithTimeout`返回一个子 context 和 `cancel`函数。
- `handler, ok := pluginHandlers[key]`是 map 的存在性判断，不能只看 `handler`是否为 nil。
- `err = og.notifyPlugin(...)`使用已有的 `err`变量，所以是 `=`，不是重新声明用的 `:=`。
- `infoResp.Endpoint == ""`用空字符串表达“插件没有提供另一个 endpoint”。

### 12.2 generic 路径的三个 timeout 不要混

当前源码边界：

| 动作 | timeout |
|---|---:|
| registration socket dial | 10 秒 |
| `GetInfo` | 1 秒 |
| `NotifyRegistrationStatus` | 5 秒 |

这些 timeout 保护的是 generic registration 事务。进入 Device Plugin handler 后，当前 `RegisterPlugin`使用背景 context 调用 `connectClient`，`GetDevicePluginOptions`本身没有再加固定 timeout。

### 12.3 Notify 失败为什么还要做补偿

generic 路径的顺序特意设计为：

```text
Validate
  -> actual state AddPlugin
  -> handler.RegisterPlugin
  -> Notify success
```

若最后的 Notify 失败，源码会移除 actual state 并调用 DeRegister。因为插件没有收到明确成功反馈，kubelet不能一边对外说失败、一边把它长期保留为已注册。

---

## 13. ResourceHealthStatus：它不参与 Capacity 公式，但有一个危险边界

在当前源码基线中：

```text
ResourceHealthStatus: Beta，默认开启
ResourceHealthStatusMessage: Beta，默认开启
DRAExtendedResource: Beta，默认开启
```

依赖关系也要一起看：`ResourceHealthStatus`和 `DRAExtendedResource`依赖 `DynamicResourceAllocation`，`ResourceHealthStatusMessage`又依赖 `ResourceHealthStatus`；当前 commit 中 Dynamic Resource Allocation 已默认开启并锁定。生产仍必须按实际 Kubernetes 版本和 feature-gate 配置校准。本案明确没有 DRA Extended Resource 映射，仍走普通 Device Plugin。

### 13.1 它做的是“已分配设备健康写进 Pod status”

Capacity/Allocatable 始终来自 healthy/unhealthy set；`ResourceHealthStatus`只是让 kubelet把已分配 device ID 的健康状态放进 container status。

源码：`pkg/kubelet/cm/devicemanager/manager.go::UpdateAllocatedResourcesStatus`

```go
// UpdateAllocatedResourcesStatus 把 DeviceManager 的已分配设备健康写进 PodStatus。
func (m *ManagerImpl) UpdateAllocatedResourcesStatus(pod *v1.Pod, status *v1.PodStatus) {
	// 读取 allDevices 与 podDevices 前持有同一把主锁。
	m.mutex.Lock()
	// 函数退出时保证解锁。
	defer m.mutex.Unlock()

	// 当前源码只遍历普通 ContainerStatuses，不遍历 initContainerStatuses。
	for i, containerStatus := range status.ContainerStatuses {
		// 从分配账查出该 Pod/Container 已拿到的设备。
		devices := m.podDevices.getContainerDevices(string(pod.UID), containerStatus.Name)

		// 第一轮按 resourceName 更新每个已分配 device instance 的内部 Health。
		for resourceName, deviceInstances := range devices {
			// 遍历这个 container 已分配的每个 device ID。
			for id, d := range deviceInstances {
				// 先把默认值设为协议 Healthy。
				health := pluginapi.Healthy
				// 只有 allDevices 里还存在这个 resourceName 才继续查。
				if r, ok := m.allDevices[resourceName]; ok {
					// 只有具体 device ID 也存在时才覆盖默认健康值。
					if _, ok := r[id]; ok {
						// 读取最新 ListAndWatch 快照中的 Health。
						health = m.allDevices[resourceName][id].Health
					}
				}

				// 把最终结果写进这个 device instance。
				d.Health = health

				// map 保存的是结构体值，所以修改后要按 ID 显式写回。
				deviceInstances[id] = d
			}
		}

		// 第二轮把内部 device instance 转成 Pod API 的 ResourceStatus。
		for resourceName, dI := range devices {
			// 为当前扩展资源创建一条状态对象。
			resourceStatus := v1.ResourceStatus{
				// Name 例如 nvidia.com/gpu。
				Name: v1.ResourceName(resourceName),
				// 先创建空的 device 健康列表。
				Resources: []v1.ResourceHealth{},
			}

			// 把每个 device ID 转成 API 层的健康记录。
			for id, d := range dI {
				// API 层同样先默认 Healthy。
				health := v1.ResourceHealthStatusHealthy
				// Device Plugin 只要不是精确 Healthy，就映射为 Unhealthy。
				if d.Health != pluginapi.Healthy {
					// 当前 Device Plugin 路径不会在这里生成 Unknown。
					health = v1.ResourceHealthStatusUnhealthy
				}
				// 把 device ID 和健康状态追加到资源状态列表。
				resourceStatus.Resources = append(resourceStatus.Resources, v1.ResourceHealth{
					// ResourceID 保存 opaque device ID。
					ResourceID: v1.ResourceID(id),
					// Health 保存刚才完成的 API 状态映射。
					Health: health,
				})
			}

			// 第一次写该 container 状态时，先初始化 slice。
			if status.ContainerStatuses[i].AllocatedResourcesStatus == nil {
				// 创建空的 ResourceStatus 列表。
				status.ContainerStatuses[i].AllocatedResourcesStatus = []v1.ResourceStatus{}
			}

			// found 记录同名 resource 状态是否已经存在。
			found := false
			// 在当前 container 的已有状态中按资源名查找。
			for j, rs := range status.ContainerStatuses[i].AllocatedResourcesStatus {
				// 找到同名资源时覆盖旧状态。
				if rs.Name == resourceStatus.Name {
					// 用本次完整计算结果替换旧 ResourceStatus。
					status.ContainerStatuses[i].AllocatedResourcesStatus[j] = resourceStatus
					// 标记已经完成覆盖。
					found = true
					// 同一个 resourceName 不需要继续向后查。
					break
				}
			}

			// 原列表中没有同名资源时，追加一条新状态。
			if !found {
				// append 返回新的 slice，必须赋值回原字段。
				status.ContainerStatuses[i].AllocatedResourcesStatus = append(status.ContainerStatuses[i].AllocatedResourcesStatus, resourceStatus)
			}
		}
	}
}
```

**大白话总结：** 这段代码最反直觉的地方是“先默认 Healthy”。如果一个已分配 device ID 从新 `ListAndWatch`快照中直接消失，`allDevices`找不到它时不会自动写 Unknown 或 Unhealthy，最后仍可能保留 Healthy。这是源码事实，不是理想化语义。

**顺手学 Go：**

- `for i, containerStatus := range ...`：`i`是下标，`containerStatus`是当前元素的副本。
- `string(pod.UID)`：把强类型 UID 转成普通字符串作为 map key。
- `for id, d := range deviceInstances`：若 map value 是结构体值，修改 `d`后要再写回 `deviceInstances[id]`。
- `v1.ResourceStatus{Name: ..., Resources: ...}`：按字段名构造结构体，比按位置构造更容易读。
- `append(slice, value)`：可能返回新的底层 slice，所以必须把返回值赋回原字段。
- `break`：只跳出当前最内层循环，这里表示同名 resource 找到后停止继续查找。

### 13.2 三个生产上必须知道的限制

1. **旧 ID 直接消失，不会在更新 callback 中触发 Pod 刷新。** 因为 callback 只遍历新快照中的 entry。
2. **PluginDisconnected 只移动 healthy/unhealthy set。** 它不会同步改写 `allDevices[ID].Health`，也不会向 Pod 更新 channel 发送通知。
3. **Device Plugin 路径最终只映射 Healthy/Unhealthy。** 这段逻辑不会为“查不到的 ID”生成 API 层 Unknown，也不填充 Device Plugin 的健康消息。

于是可能出现：

```text
Node Allocatable 已经从 8 降到 0
但某个正在运行 Pod 的 allocatedResourcesStatus 仍显示旧 Healthy
```

这不是在说 GPU 一定健康，而是说明两个状态面更新机制不同。硬件事故仍要结合 DCGM、Xid/ECC、插件日志和应用 CUDA 行为。

---

## 14. 插件断连：为什么先 8/0，再 0/0

### 14.1 第一阶段：立即把 healthy set 移入 unhealthy set

源码：`pkg/kubelet/cm/devicemanager/manager.go::PluginDisconnected`

```go
// PluginDisconnected 按 resourceName 处理插件断开。
func (m *ManagerImpl) PluginDisconnected(logger klog.Logger, resourceName string) {
	// 修改 endpoints 和健康集合前加主锁。
	m.mutex.Lock()
	// 函数退出时解锁。
	defer m.mutex.Unlock()

	// 只有 endpoints 中仍存在这个 resourceName 才继续。
	if ep, exists := m.endpoints[resourceName]; exists {
		// 把该资源当前所有 healthy IDs 合并到 unhealthy set。
		m.markResourceUnhealthy(logger, resourceName)
		// 记录 endpoint 进入不健康状态。
		logger.V(2).Info("Endpoint became unhealthy", "resourceName", resourceName)

		// 保存停止时间，后面 GetCapacity 用它判断 5 分钟宽限期。
		ep.e.setStopTime(time.Now())
	}
}
```

同一个动作的集合变化：

```go
// 先准备一个空集合，用来接住原 healthy IDs。
healthyDevices := sets.New[string]()
// healthyDevices map 中存在这个 resourceName 时才读取。
if _, ok := m.healthyDevices[resourceName]; ok {
	// 保存原 healthy set。
	healthyDevices = m.healthyDevices[resourceName]
	// 把当前 healthy set 替换为空集合，所以 Allocatable 将变成 0。
	m.healthyDevices[resourceName] = sets.New[string]()
}
// 如果原来没有 unhealthy set，就先创建。
if _, ok := m.unhealthyDevices[resourceName]; !ok {
	// 初始化可写的空集合。
	m.unhealthyDevices[resourceName] = sets.New[string]()
}
// 把原 healthy IDs 与已有 unhealthy IDs 做并集，所以 Capacity 暂时保留。
m.unhealthyDevices[resourceName] = m.unhealthyDevices[resourceName].Union(healthyDevices)
```

**大白话总结：** 断连第一阶段不是删除设备，而是把“还能继续分配”的集合清空，同时把原来的 ID 全部保留为 unhealthy。因此 8 张已登记设备会先表现为 Capacity=8、Allocatable=0。

**顺手学 Go：**

- `if ep, exists := map[key]; exists`：同时取得 value 和存在标记，`ep`只在 `if`作用域可见。
- `sets.Set.Union`返回并集集合，不是简单拼接 slice。
- `time.Now()`取得当前时间，保存到 endpoint 自己的 stopTime。
- 两段代码都在调用者已经持有的主锁保护下修改共享 map。

### 14.2 第二阶段：5 分钟不是定时器主动清理

源码常量：`pkg/kubelet/cm/devicemanager/types.go`

```go
// endpoint 停止后允许它在缓存中保留的固定宽限期。
const endpointStopGracePeriod = time.Duration(5) * time.Minute
```

**大白话总结：** 这不是启动一个“5 分钟后必定准点执行”的 timer。真正删除发生在后续某次 `GetCapacity`调用时，它发现 `time.Since(stopTime)`已经超过宽限期，才把资源放进 `deletedResources`。

**顺手学 Go：**

- `time.Duration(5)`把数字转换成 Duration。
- `5 * time.Minute`得到五分钟时长；它不是时间戳。

完整传播仍要走：

```text
超过 5 分钟
  -> 某次 GetCapacity 执行
  -> 删除 endpoint/healthy/unhealthy 内存项
  -> 返回 removedResources
  -> Node setter 把 Capacity 写显式 0
  -> Node status patch 成功
  -> scheduler cache 观察到更新
```

所以 `kubectl get node`不保证在第五分钟整恰好变化。

### 14.3 重注册竞争时，断开的不一定只是“那个旧对象”

当前 client map 以 resourceName 为 key。`runClient`结束后会再次按 resourceName 从 map 读取 client；新注册也可能覆盖同一个 key。

这意味着有旧 stream 结束和新注册并发时，源码行为是“按资源名处理当前映射”，不能简单描述成：

```text
哪个旧 connection 断了
  -> 只标记那个旧 endpoint
```

生产排障要把 Pod UID、socket inode/路径、注册时间和 kubelet日志放到同一条时间线，避免把新旧实例混成一个对象。

### 14.4 四种相近现象的对照

| 现象 | Capacity | Allocatable | 关键原因 |
|---|---:|---:|---|
| 新节点从未收到 first snapshot | absent | absent | 没形成资源账 |
| 插件主动发送空完整清单 | 0 | 0 | endpoint 活着，但快照为空 |
| 插件刚断连 | 8 | 0 | 全部 healthy 转 unhealthy，仍在宽限期 |
| 断连超宽限且已执行 GetCapacity | 0 | 0 | 资源被清理，Node setter 写显式 0 |

---

## 15. checkpoint：它保存恢复账，但没有事务回滚

源码：`pkg/kubelet/cm/devicemanager/manager.go::writeCheckpoint`

```go
// writeCheckpoint 把当前分配账和 registered healthy IDs 写到磁盘。
func (m *ManagerImpl) writeCheckpoint(logger klog.Logger) error {
	// 复制共享内存状态前加锁。
	m.mutex.Lock()
	// 创建 resourceName 到 device ID slice 的新 map。
	registeredDevs := make(map[string][]string)
	// 遍历当前 healthy set。
	for resource, devices := range m.healthyDevices {
		// 把集合复制为无固定顺序的字符串 slice。
		registeredDevs[resource] = devices.UnsortedList()
	}
	// 同时复制 Pod/Container 的设备分配账，构造 checkpoint 数据对象。
	data := checkpoint.New(m.podDevices.toCheckpointData(logger), registeredDevs)
	// 内存快照复制完成就解锁，不在主锁内做磁盘 I/O。
	m.mutex.Unlock()
	// 在锁外尝试创建或覆盖 checkpoint 文件。
	err := m.checkpointManager.CreateCheckpoint(kubeletDeviceManagerCheckpoint, data)
	// 磁盘写入失败时返回 error。
	if err != nil {
		// 增加 checkpoint 文件名上下文。
		err2 := fmt.Errorf("failed to write checkpoint file %q: %v", kubeletDeviceManagerCheckpoint, err)
		// 同时记录底层错误。
		logger.Error(err, "Failed to write checkpoint file")
		// 调用者决定是继续、记录还是返回；本函数本身不回滚内存。
		return err2
	}
	// 成功时只在更高日志级别记录。
	logger.V(4).Info("Checkpoint file written", "checkpoint", kubeletDeviceManagerCheckpoint)
	// 返回 nil 表示这次磁盘持久化成功。
	return nil
}
```

**大白话总结：** 源码先在锁内复制一份内存快照，再在锁外写磁盘，避免慢 I/O 长时间堵住 DeviceManager。但这不是数据库事务：磁盘写失败时，前面已经更新的内存表、插件 Allocate 副作用或已清理的 endpoint 不会自动恢复。

**顺手学 Go：**

- `make(map[string][]string)`：value 类型是字符串 slice。
- `UnsortedList()`明确告诉你返回顺序不稳定，测试不能依赖自然排序。
- `err2 := fmt.Errorf(...)`创建一个更有上下文的新 error。
- 锁内复制、锁外 I/O 是常见并发设计：缩短临界区，但需要接受快照与后续内存变化之间存在时间差。

### 15.1 读失败也不会阻止 registration server 启动

第 4 节已经看到：`Start`记录 checkpoint 读取错误后，仍执行 `m.server.Start(logger)`。

所以真实状态可能是：

```text
checkpoint 文件存在但损坏
  -> readCheckpoint 报错
  -> DeviceManager 继续启动
  -> 历史分配账可能不完整
  -> 等插件重新注册和新快照逐步收敛
```

更细的边界是：启动时用于判断“是否需要 reset 扩展资源”的保护逻辑关注目录状态，不等价于完整校验这个 checkpoint 可读。看到文件存在，不能当成恢复成功证据。

### 15.2 正确告警语义

```text
checkpoint write failed
  -> 当前内存 Capacity 仍可能已经更新
  -> 当前 Node status 仍可能继续变化
  -> kubelet 下次重启恢复风险上升
```

不要写成“checkpoint 写失败，所以本次 ListAndWatch 没生效”；这与源码顺序相反。

---

## 16. 一个隐藏断点：cAdvisor MachineInfo 失败时，GetCapacity 可能根本没被调用

Node status setter 先调用 `machineInfoFunc()`。当前源码把 `devicePluginResourceCapacityFunc()`放在 MachineInfo 成功的 `else`分支里。

因此：

```text
DeviceManager 内已经有 8 healthy IDs
  + 本轮 cAdvisor MachineInfo 获取失败
  -> 这一轮不会调用 DeviceManager.GetCapacity
  -> Node 上旧 GPU Capacity 可能继续保留
```

这给“Processed device updates 已经是 8/8，但 Node 仍没变化”增加了一个真实分支：

```text
不要只查 PatchNodeStatus
还要查同一轮 Node status 组装前是否有 Error getting machine info
```

这是 Kubernetes 组合 Node 状态时的历史耦合：Device Plugin 数量本身不依赖 cAdvisor 枚举 GPU，但当前 setter 的控制流仍受 MachineInfo 分支影响。

---

## 17. 从生产现象反查源码断点

| 生产现象 | 最小可证断点 | 下一份证据 | 先不要做什么 |
|---|---|---|---|
| Plugin Pod Running，Node 字段 absent | 进程账之后 | exact Pod UID/image/config、direct Register 日志 | 不先怪 scheduler |
| direct registration counter 增加 | 收到请求 | 校验错误、`Connected to new client` | 不把 counter 当成功率 |
| `Device plugin connected`，无 first snapshot | `PluginConnected`之后 | `State pushed`、stream error、插件初始化日志 | 不改 Node label |
| `State pushed=8`，无 `Processed` | callback 前后 | kubelet panic/error、同一 boot 日志完整性 | 不重启整台 Node 掩盖证据 |
| `Processed 8/8`，Node 仍旧 | GetCapacity/Node setter | MachineInfo 错误、capacity 日志、status patch 错误 | 不直接清 scheduler cache |
| Node 8/8，新 Pod仍 Insufficient | scheduler 传播/占用账 | scheduler cache、现有 Pod requests、queue event | 不把 Allocatable 当余额 |
| stream 结束后 8/0 | disconnect 第一阶段 | 重注册、stopTime 后续变化 | 不删 socket 做实验 |
| 约 5 分钟后仍 8/0 | 还没执行/传播清理 | 后续 GetCapacity、Node status patch | 不把 5 分钟当准点 timer |
| Node 数量减少，运行 Pod status仍 Healthy | ResourceHealthStatus 边界 | ID 是否直接消失、allDevices、应用/DCGM | 不用 Pod status反证硬件健康 |

### 17.1 主案需要的证据优先级

```text
第一优先：对象身份和同一时间线
  Node name/systemUUID/bootID
  Plugin Pod UID/image digest/containerID
  kubelet version/commit或发行版版本

第二优先：链路里程碑
  Register
  Connected
  State pushed
  Processed device updates
  Node status values

第三优先：根因组件
  NVIDIA plugin init/NVML日志
  socket/mount/permission
  Node status MachineInfo/patch日志
  scheduler cache与Pod request账
```

日志里“没有某行”只能在以下前提下当证据：

- kubelet `V(2)`日志确实被采集；
- 时间窗覆盖插件启动与注册；
- boot ID 没换；
- 没有日志采集丢弃、限流或保留期缺口。

否则“没看到”只是证据缺口，不等于代码路径一定没执行。

---

## 18. 日志、Event 与指标分别能证明什么

### 18.1 direct 路径关键日志

```text
Got registration request from device plugin with resource
Connected to new client
Device plugin connected
State pushed for device plugin
Processed device updates for resource
ListAndWatch ended unexpectedly for device plugin
Endpoint became unhealthy
Updated capacity for device plugin
Updated allocatable
```

不要只搜一条。至少按同一 resourceName 和时间窗组成序列。

### 18.2 这条链没有专用 Kubernetes Event 逐步记录

`kubectl get events`可以看到 Pod FailedScheduling，但 direct Register、first snapshot 和三张表重建主要靠组件日志与 Node status。Event 还可能被聚合、限流或过期，不能作为完整审计日志。

### 18.3 两个容易被名字骗到的指标

#### `kubelet_device_plugin_registration_total`

- Alpha 指标；
- direct `Register`一进函数、校验前就递增；
- kubelet 重启后进程内计数重新开始；
- 能证明收到过请求，不能证明版本、资源名、回连和 first snapshot 成功。

#### `plugin_manager_total_plugins`

- Alpha 指标；
- 标签是 `socket_path`和 `state`；
- 描述 generic plugin manager 的 desired/actual state；
- 某 socket 进入 actual state，不等于它一定是 `nvidia.com/gpu`，也不等于清单健康。

当前核心 DeviceManager没有直接暴露下面这种可依赖指标：

```text
kubelet_device_plugin_healthy_devices{resource="nvidia.com/gpu"}
```

数量应以 Node Status 和 kubelet处理日志为准；硬件健康应以厂商插件、DCGM 与应用证据补充。

---

## 19. NVIDIA 场景的六个常见分支

### 19.1 `FAIL_ON_INIT_ERROR`不要只背二进制默认值

NVIDIA Device Plugin二进制的默认行为与 Operator/Helm 实际传入配置可能不同。若现场配置让初始化失败时进程继续等待，就可能看到：

```text
Plugin Pod Running
  + socket/进程存在
  + 没有可用设备快照
```

要同时记录：

- 镜像 digest；
- container args；
- env；
- 挂载的配置文件内容与其来源；
- 实际启动日志。

不能拿“官方默认”覆盖现场 manifest。

### 19.2 DaemonSet 不一定覆盖目标 Node

检查目标 Pod 的 `spec.nodeName`，不要看集群里任意一个 Device Plugin Pod。常见原因有 nodeSelector/affinity、taint/toleration、Operator 节点标签、canary 策略或节点排除配置。

### 19.3 hostPath、mountPath 与 endpoint 可能对不上

direct registration 根目录固定为 `/var/lib/kubelet/device-plugins`。插件容器必须看到正确的 hostPath；容器化 kubelet、SELinux、权限或发行版补丁都可能改变现场可见性。

### 19.4 MIG 与 sharing 会改变“资源单位”的含义

```text
8 张物理卡
  + MIG profile
  -> 可能暴露 nvidia.com/mig-<profile>

8 张物理卡
  + time-slicing 每卡 10 replicas
  -> nvidia.com/gpu Capacity 可能是 80
```

本课关闭这些策略只是为了学习公式，不代表生产应该关闭。

### 19.5 `deviceListStrategy`主要影响 Allocate 后的交付方式

```text
envvar
volume-mounts
cdi-annotations
cdi-cri
```

它们是第 16 课“已选 device ID 怎样交给 runtime”的重点。Node Capacity absent 时，第一优先仍是 registration/ListAndWatch 快照，不是先改 `deviceListStrategy`。

### 19.6 `Health=Healthy`不是完整硬件体检

它只是 Kubernetes Device Plugin 分配层认可的状态，不替代：

- Xid/ECC；
- 温度、功耗和降频；
- NVLink/NVSwitch/Fabric Manager；
- CUDA/NCCL 真实测试；
- 长时间趋势。

---

## 20. 反事实：如果设计成别的样子，会发生什么

| 假设计法 | 表面上更简单 | 实际问题 | 当前设计的取舍 |
|---|---|---|---|
| 插件直接 patch Node | 少一层 kubelet | 多写者冲突、权限膨胀、校验分散 | kubelet统一写 Node Status |
| ListAndWatch只发 delta | 每次消息小 | 重连或丢事件后账永久漂移 | full snapshot 重建 |
| Unhealthy直接从 Capacity删除 | 数字看着干净 | 看不出“存在但不可分配” | Capacity保留，Allocatable下降 |
| 断连立刻 0/0 | 收敛很快 | 插件瞬时重启导致节点硬件身份抖动 | 先8/0，5分钟后0/0 |
| Node Allocatable实时减已分配 | 一眼看“剩余” | kubelet与scheduler成为双写占用账 | scheduler独立维护 request 余额 |
| checkpoint失败回滚全部 | 状态更像事务 | 插件外部副作用和内存并非总可逆 | 记录风险，依赖后续收敛与重启恢复治理 |

这张表就是本章真正要掌握的 Kubernetes 设计思想：它优先保证**职责边界、幂等收敛和故障隔离**，而不是让每一个瞬时状态都只靠一个数字表达。

---

## 21. 只读取证：先看证据模型，再看命令

> 当前工作区没有连接 GPU 生产集群。以下命令未执行，不写假 PASS；它们只用于已经批准的 context、节点和日志访问链。

### 21.1 Node 数量观察

```powershell
$ErrorActionPreference = 'Stop'
$Kubeconfig = '__APPROVED_KUBECONFIG_PATH__'
$ExpectedKubeconfigSha256 = '__APPROVED_KUBECONFIG_SHA256__'
$Context = '__APPROVED_CONTEXT__'
$ExpectedCluster = '__APPROVED_CLUSTER_NAME__'
$ExpectedServer = '__APPROVED_APISERVER_URL__'
$ExpectedUser = '__APPROVED_CONTEXT_USER__'
$Node = '__APPROVED_GPU_NODE__'
$ExpectedNodeUID = '__APPROVED_NODE_UID__'
$Resource = 'nvidia.com/gpu'
$RequestTimeout = '10s'

$Required = @(
  $Kubeconfig,
  $ExpectedKubeconfigSha256,
  $Context,
  $ExpectedCluster,
  $ExpectedServer,
  $ExpectedUser,
  $Node,
  $ExpectedNodeUID
)
if ($Required | Where-Object {
  [string]::IsNullOrWhiteSpace($_) -or $_ -like '__*'
}) {
  throw '先填写完整的批准身份：kubeconfig、hash、context、cluster/server/user、Node和Node UID'
}

$ResolvedKubeconfig = (Resolve-Path -LiteralPath $Kubeconfig).Path
$ActualKubeconfigSha256 = (Get-FileHash -LiteralPath $ResolvedKubeconfig -Algorithm SHA256).Hash
if ($ActualKubeconfigSha256 -cne $ExpectedKubeconfigSha256) {
  throw 'kubeconfig hash与批准值不一致，拒绝继续取证'
}

$ConfigRaw = & kubectl --kubeconfig $ResolvedKubeconfig config view -o json
if ($LASTEXITCODE -ne 0 -or -not $ConfigRaw) {
  throw '读取指定kubeconfig失败'
}
$Config = $ConfigRaw | ConvertFrom-Json
$Current = (& kubectl --kubeconfig $ResolvedKubeconfig config current-context | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or $Current -cne $Context) {
  throw "当前 context [$Current] 与批准值 [$Context] 不一致"
}

$ContextObject = @($Config.contexts) |
  Where-Object { $_.name -ceq $Context } |
  Select-Object -First 1
if (-not $ContextObject) {
  throw '批准context不存在于指定kubeconfig'
}
if (
  $ContextObject.context.cluster -cne $ExpectedCluster -or
  $ContextObject.context.user -cne $ExpectedUser
) {
  throw 'context绑定的cluster或user与批准值不一致'
}
$ClusterObject = @($Config.clusters) |
  Where-Object { $_.name -ceq $ExpectedCluster } |
  Select-Object -First 1
if (-not $ClusterObject -or $ClusterObject.cluster.server -cne $ExpectedServer) {
  throw 'API Server地址与批准值不一致'
}

$KubectlBase = @(
  '--kubeconfig', $ResolvedKubeconfig,
  '--context', $Context,
  '--request-timeout', $RequestTimeout
)

1..20 | ForEach-Object {
  $Raw = & kubectl @KubectlBase get node $Node -o json
  $NodeExit = $LASTEXITCODE
  if ($NodeExit -ne 0 -or -not $Raw) {
    throw "第 $_ 次读取Node失败，exit=$NodeExit，node=$Node，uid=$ExpectedNodeUID"
  }
  $Object = $Raw | ConvertFrom-Json
  if (
    $Object.kind -cne 'Node' -or
    $Object.metadata.name -cne $Node -or
    $Object.metadata.uid -cne $ExpectedNodeUID
  ) {
    throw "第 $_ 次返回的Node身份与批准对象不一致"
  }
  $Capacity = $Object.status.capacity.PSObject.Properties[$Resource]
  $Allocatable = $Object.status.allocatable.PSObject.Properties[$Resource]
  [pscustomobject]@{
    Time = Get-Date -Format 'yyyy-MM-dd HH:mm:ss.fff'
    NodeUID = $Object.metadata.uid
    ResourceVersion = $Object.metadata.resourceVersion
    Capacity = if ($Capacity) { $Capacity.Value } else { '<absent>' }
    Allocatable = if ($Allocatable) { $Allocatable.Value } else { '<absent>' }
  }
  if ($_ -lt 20) {
    Start-Sleep -Seconds 2
  }
}
```

脚本不只比较 context 名称：还固定 kubeconfig 文件 hash，并核对 context 绑定的 cluster、server 和 user；这样可以防止另一个同名 context 指向错误集群。它只能证明**这 20 个采样点中观测到的快照顺序**；2 秒间隔内的中间状态仍可能被漏掉。它也不能单独证明是哪个 device ID、哪个插件实例或哪次 Patch 导致。

### 21.2 精确对到目标 Node 上的 Plugin Pod

```powershell
$Namespace = 'gpu-operator'
$Selector = '__ACTUAL_DEVICE_PLUGIN_SELECTOR__'
$ExpectedDaemonSetUID = '__APPROVED_DAEMONSET_UID__'
$ExpectedPodUID = '__APPROVED_PLUGIN_POD_UID__'
$ExpectedContainer = 'nvidia-device-plugin-ctr'
$ExpectedImageID = '__APPROVED_IMAGE_DIGEST__'
$PluginRequired = @(
  $Selector,
  $ExpectedDaemonSetUID,
  $ExpectedPodUID,
  $ExpectedContainer,
  $ExpectedImageID
)
if (-not $KubectlBase -or ($PluginRequired | Where-Object {
  [string]::IsNullOrWhiteSpace($_) -or $_ -like '__*'
})) {
  throw '先完成21.1身份校验，并填写真实selector、DaemonSet UID、Pod UID和image digest'
}

$PodArgs = @(
  'get', 'pods',
  '-n', $Namespace,
  '-l', $Selector,
  '--field-selector', "spec.nodeName=$Node",
  '-o', 'json'
)
$PodRaw = & kubectl @KubectlBase @PodArgs
$PodListExit = $LASTEXITCODE
if ($PodListExit -ne 0 -or -not $PodRaw) {
  throw "读取目标Node的Device Plugin Pod失败，exit=$PodListExit，node=$Node"
}

$PodList = $PodRaw | ConvertFrom-Json
if ($PodList.kind -cne 'PodList') {
  throw 'API返回对象不是PodList'
}
$Pods = @($PodList.items)
if ($Pods.Count -ne 1) {
  throw "预期恰好一个匹配 Pod，实际为 $($Pods.Count)"
}

$Pod = $Pods[0]
$PodName = $Pod.metadata.name
$PodNamespace = $Pod.metadata.namespace
$Owner = @($Pod.metadata.ownerReferences) |
  Where-Object {
    $_.controller -eq $true -and
    $_.kind -ceq 'DaemonSet' -and
    $_.uid -ceq $ExpectedDaemonSetUID
  } |
  Select-Object -First 1
$ContainerSpec = @($Pod.spec.containers) |
  Where-Object { $_.name -ceq $ExpectedContainer } |
  Select-Object -First 1
$ContainerStatus = @($Pod.status.containerStatuses) |
  Where-Object { $_.name -ceq $ExpectedContainer } |
  Select-Object -First 1

if (
  $Pod.metadata.uid -cne $ExpectedPodUID -or
  $Pod.spec.nodeName -cne $Node -or
  $Pod.status.phase -cne 'Running' -or
  $Pod.metadata.deletionTimestamp -or
  -not $Owner -or
  -not $ContainerSpec -or
  -not $ContainerStatus -or
  $ContainerStatus.imageID -cne $ExpectedImageID -or
  [string]::IsNullOrWhiteSpace($ContainerStatus.containerID)
) {
  throw 'Pod UID、owner、Node、phase、删除状态、目标container、image digest或containerID与批准对象不一致'
}

$Pod | Select-Object `
  @{n='Namespace';e={$_.metadata.namespace}}, `
  @{n='Name';e={$_.metadata.name}}, `
  @{n='UID';e={$_.metadata.uid}}, `
  @{n='Node';e={$_.spec.nodeName}}, `
  @{n='OwnerUID';e={$Owner.uid}}, `
  @{n='TargetContainer';e={$ExpectedContainer}}, `
  @{n='ImageID';e={$ContainerStatus.imageID}}, `
  @{n='ContainerID';e={$ContainerStatus.containerID}}, `
  @{n='RestartCount';e={$ContainerStatus.restartCount}} |
  Format-List

$LogArgs = @(
  'logs', $PodName,
  '-n', $PodNamespace,
  '-c', $ExpectedContainer,
  '--prefix=true',
  '--timestamps',
  '--tail=1000'
)
$LogIdentity = "$PodNamespace/$PodName uid=$ExpectedPodUID container=$ExpectedContainer"
"[LOG IDENTITY] $LogIdentity"
$Logs = & kubectl @KubectlBase @LogArgs
$LogExit = $LASTEXITCODE
if ($LogExit -ne 0) {
  throw "目标container当前日志命令失败，exit=$LogExit，$LogIdentity"
}
if (-not $Logs) {
  "[EVIDENCE GAP] 目标container日志命令成功但stdout为空；$LogIdentity；继续检查日志入口和时间窗"
} else {
  $Logs
}

if ($ContainerStatus.restartCount -gt 0) {
  $PreviousLogs = & kubectl @KubectlBase @LogArgs --previous
  $PreviousExit = $LASTEXITCODE
  if ($PreviousExit -ne 0) {
    "[EVIDENCE GAP] restartCount>0但previous日志不可读，exit=$PreviousExit，$LogIdentity"
  } elseif (-not $PreviousLogs) {
    "[EVIDENCE GAP] previous日志命令成功但stdout为空；$LogIdentity"
  } else {
    $PreviousLogs
  }
}

$AfterRaw = & kubectl @KubectlBase get pod $PodName -n $PodNamespace -o json
$AfterExit = $LASTEXITCODE
if ($AfterExit -ne 0 -or -not $AfterRaw) {
  throw "日志读取后复核Pod身份失败，exit=$AfterExit，pod=$PodNamespace/$PodName"
}
$AfterPod = $AfterRaw | ConvertFrom-Json
if ($AfterPod.kind -cne 'Pod' -or $AfterPod.metadata.uid -cne $ExpectedPodUID) {
  throw '日志读取期间Pod已被同名重建，拒绝把日志归入原实例'
}
```

“目标插件 container 日志为空”与“命令失败”要分开。空日志可能是进程没输出、日志轮转、采集入口不对或时间窗不对；如果 Pod 在读取期间被同名重建，脚本会因 UID 变化拒绝把新旧日志拼在一起。

### 21.3 目标 Node 上的 socket 与 kubelet日志

先从批准的 Node 对象和资产系统记录 `.status.nodeInfo.systemUUID`、`.status.nodeInfo.bootID`，填入脚本；脚本会在第一次 `sudo`前与宿主机 product UUID、当前 boot ID 做 fail-closed 比较。只有完全一致才继续：

```bash
set -o pipefail

approved_system_uuid='__APPROVED_NODE_SYSTEM_UUID__'
approved_boot_id='__APPROVED_NODE_BOOT_ID__'
direct_dir='/var/lib/kubelet/device-plugins'

if test -z "$approved_system_uuid" ||
   test -z "$approved_boot_id" ||
   test "$approved_system_uuid" = '__APPROVED_NODE_SYSTEM_UUID__' ||
   test "$approved_boot_id" = '__APPROVED_NODE_BOOT_ID__'; then
  echo '[IDENTITY FAILED] 先从批准的Node对象记录systemUUID和bootID' >&2
  exit 2
fi

if ! observed_at=$(date -Is); then
  echo '[COMMAND FAILED] date -Is' >&2
  exit 3
fi
if ! host_fqdn=$(hostname -f); then
  echo '[COMMAND FAILED] hostname -f' >&2
  exit 4
fi
if ! test -r /sys/class/dmi/id/product_uuid; then
  echo '[IDENTITY FAILED] product_uuid不可读' >&2
  exit 5
fi
if ! test -r /proc/sys/kernel/random/boot_id; then
  echo '[IDENTITY FAILED] boot_id不可读' >&2
  exit 6
fi

actual_system_uuid=$(tr -d '-' < /sys/class/dmi/id/product_uuid | tr '[:upper:]' '[:lower:]')
actual_boot_id=$(tr '[:upper:]' '[:lower:]' < /proc/sys/kernel/random/boot_id)
expected_system_uuid=$(printf '%s' "$approved_system_uuid" | tr -d '-' | tr '[:upper:]' '[:lower:]')
expected_boot_id=$(printf '%s' "$approved_boot_id" | tr '[:upper:]' '[:lower:]')

if test -z "$actual_system_uuid" ||
   test -z "$actual_boot_id" ||
   test "$actual_system_uuid" != "$expected_system_uuid" ||
   test "$actual_boot_id" != "$expected_boot_id"; then
  echo '[IDENTITY FAILED] 当前宿主机systemUUID/bootID与批准Node不一致' >&2
  exit 7
fi

printf 'observed_at=%s\nhost_fqdn=%s\nsystem_uuid=%s\nboot_id=%s\n' \
  "$observed_at" "$host_fqdn" "$actual_system_uuid" "$actual_boot_id"

if ! sudo -n true; then
  echo '[COMMAND FAILED] 当前批准会话没有非交互sudo读取权限' >&2
  exit 8
fi

if ! sudo -n find "$direct_dir" \
  -maxdepth 1 \
  -type s \
  -printf '%p %u:%g %m %TY-%Tm-%TdT%TH:%TM:%TS\n'; then
  echo '[COMMAND FAILED] 读取Device Plugin socket目录失败' >&2
  exit 9
fi

sudo -n ss -xlpn | grep -F "$direct_dir/"
ss_rc=( "${PIPESTATUS[@]}" )
if test "${ss_rc[0]}" -ne 0; then
  echo '[COMMAND FAILED] ss读取失败' >&2
  exit 10
elif test "${ss_rc[1]}" -eq 1; then
  echo '[EVIDENCE GAP] 没有匹配Device Plugin目录的监听socket' >&2
elif test "${ss_rc[1]}" -ne 0; then
  echo '[COMMAND FAILED] socket过滤失败' >&2
  exit 11
fi

sudo -n journalctl -b -u kubelet --since '-30 minutes' --no-pager |
  grep -E 'Got registration request|Connected to new client|Device plugin connected|State pushed for device plugin|Processed device updates|ListAndWatch ended unexpectedly|Endpoint became unhealthy|Updated capacity|Updated allocatable|Error getting machine info'
log_rc=( "${PIPESTATUS[@]}" )
if test "${log_rc[0]}" -ne 0; then
  echo '[COMMAND FAILED] 当前boot的kubelet日志读取失败' >&2
  exit 12
elif test "${log_rc[1]}" -eq 1; then
  echo '[EVIDENCE GAP] 当前boot和时间窗内没有匹配日志；核对verbosity与日志入口' >&2
elif test "${log_rc[1]}" -ne 0; then
  echo '[COMMAND FAILED] kubelet日志过滤失败' >&2
  exit 13
fi
```

这些命令不修改 kubelet、Device Plugin 或 GPU 状态，但 `sudo`仍可能写入系统认证/审计日志并影响 sudo timestamp cache，所以只能在批准的节点会话中使用。不要为了观察主动执行任何同义变更：

```text
删除、移动、chmod/chown Device Plugin socket
删除或重建 Device Plugin Pod，修改 DaemonSet/Operator/ConfigMap
kill 插件进程，重启 kubelet、containerd、节点或 NVIDIA Driver
删除或改写 kubelet_internal_checkpoint
cordon/drain Node，修改 Node label/taint
切换 MIG、执行 GPU reset、伪造或修改 health
```

### 21.4 证据外发前脱敏

至少处理：

- Node name、hostname/FQDN、systemUUID、boot ID；
- Plugin Pod UID、containerID、镜像仓库凭据；
- GPU/MIG UUID；
- socket path、宿主机本地用户/组、进程名、PID/FD；
- Pod YAML 中的 env、secret/config 路径和内部 registry；
- kubelet日志整行中的内部 resourceName、路径和错误上下文；
- 游戏业务 namespace、镜像名和服务名。

---

## 22. 受控 canary 观察表

只有平台本来就安排 Device Plugin/Operator canary 变更时，才跟随观察；不要为了学习主动制造生产故障。

| 时刻 | Plugin Pod UID/image | direct Register | connected | first snapshot | Node Capacity/Allocatable | Java canary |
|---|---|---|---|---|---|---|
| T0 | 旧实例 | 基线 | 基线 | 基线 | 基线 | 基线 |
| T1 | 新实例启动 | 未知 | 否 | 否 | 只观察 | 不启动 |
| T2 | 新实例 | 是 | 未知 | 否 | 只观察 | 不启动 |
| T3 | 新实例 | 是 | 是 | 否 | 只观察 | 不启动 |
| T4 | 新实例 | 是 | 是 | 8/8 | 等 Node 更新 | 不启动 |
| T5 | 新实例 | 是 | 是 | 8/8 | 8/8 | request 1 GPU |
| T6 | 新实例 | 是 | 是 | 8/8 | 8/8 | 容器内 CUDA smoke 与应用探针通过 |

验收不能停在 Plugin Pod Running，也不能停在 Node 8/8。Java canary 最终仍要验证第 16 课的设备注入链。

---

## 23. Go 语法回看：这章真正需要掌握的只有这些

| Go 写法 | 大白话 | 本章为什么重要 |
|---|---|---|
| `func (m *ManagerImpl)` | 给对象定义实例方法 | 看清状态属于哪个 manager |
| `*Type` / `&Type{}` | 指针类型 / 创建对象并取地址 | gRPC 请求和共享对象常用 |
| `:=` | 首次声明并赋值 | 源码局部变量大量使用 |
| `value, ok := map[key]` | 查值并判断 key 是否存在 | 区分 absent 与零值 |
| `defer Unlock()` | 函数结束前保证解锁 | 避免 early return 漏锁 |
| `go func(){}` | 后台启动 goroutine | 注册早于 first snapshot 的根因 |
| `for { Recv() }` | 无限阻塞接收长流 | ListAndWatch 没有固定轮询周期 |
| `make(map...)` | 创建可写 map | full snapshot 重建新表 |
| `sets.New[string]()` | 创建字符串集合 | device ID 去重与计数 |
| `select/default` | 非阻塞尝试 channel 操作 | Pod 更新通知满时可被丢弃 |
| 多返回值 | 一个函数返回多份结果 | Capacity/Allocatable/removed 一次返回 |
| `resource.Quantity` | Kubernetes 资源数量类型 | GPU 数量不是普通 YAML 字符串 |

你暂时不需要先学反射、泛型实现原理、gRPC 生成代码和 channel 调度器内部。先能顺着 receiver、map、锁、goroutine、error 返回把状态链读通，就已经够支撑这一阶段的源码排障。

---

## 24. 本章自测

### 24.1 必须能口述的十二个问题

1. 为什么 Device Plugin Pod Running 不能证明 Node 已有 `nvidia.com/gpu`？
2. direct Register成功时，为什么 first snapshot 仍可能没到？
3. `GetDevicePluginOptions`与 generic `GetInfo`的 timeout 为什么不能混？
4. `ListAndWatch`为什么是完整快照而不是增量日志？
5. 7 Healthy + 1 Unhealthy 时 Capacity/Allocatable 各是多少？
6. Node Allocatable=8、已有 6 个 Pod request GPU 时，scheduler 为什么只剩 2？
7. 新节点字段 absent 与历史资源显式 0 有什么差别？
8. 插件断连为什么先 8/0，再 0/0？
9. checkpoint 写失败为什么不代表本次内存快照被回滚？
10. device ID 从新快照消失时，为什么运行 Pod status 仍可能显示 Healthy？
11. `Processed device updates=8/8`后 Node 不变，为什么要检查 cAdvisor MachineInfo？
12. 两个 registration/plugin manager 指标分别不能证明什么？

### 24.2 现场题一：本课主案

```text
host 8 cards
Plugin Running
Register success
Device plugin connected
no State pushed
Node resource absent
Pod Insufficient nvidia.com/gpu
```

请回答：

- 最小断点在哪两个源码函数之间？
- 还缺哪两条 kubelet里程碑日志？
- 为什么此时不先排 scheduler？
- 什么证据能把断点移动到 Node status 链？

### 24.3 现场题二：健康变化

```text
Processed device updates:
  totalCount=8
  healthyCount=7
Node:
  Capacity=8
  Allocatable=7
```

请回答：

- unhealthy set 中有几个 ID？
- scheduler 能否给新 Pod 分配 8 个单位？
- 这是否证明一张物理 GPU 已经消失？
- 已占用该 ID 的 Pod 会不会被 kubelet自动删除？

### 24.4 现场题三：断连

```text
10:00 stream ended
10:00 Node 8/0
10:03 Node 8/0
10:06 Node 0/0
```

请回答：

- 10:00 内部哪两个集合发生了什么变化？
- 10:03 为什么 Capacity 还能保留？
- 10:06 为什么不保证精确等于第五分钟整？
- 哪条源码链最终把 Node 字段写成 0？

### 24.5 参考答案

<details>
<summary>展开查看；建议先自己口述</summary>

1. Running只证明进程/容器状态；注册、回连、选项读取、first snapshot和Node传播都是后续独立阶段。
2. `connectClient`同步连接成功后，用 goroutine异步启动 `runClient`；注册 RPC 可以先返回。
3. generic `GetInfo`显式 1 秒；`GetDevicePluginOptions`当前没有在自身函数内创建同样的 timeout。
4. full snapshot在重连和丢事件后可以整体替换旧状态，保证幂等收敛。
5. Capacity=8，Allocatable=7。
6. scheduler 自己用 8 减去已绑定 Pod requests 6，剩余 2；Node字段不反写实时余额。
7. absent可表示从未形成过该资源账；显式 0保留“它曾由 Device Plugin 管理、后来被清理”的历史语义。
8. 断连先把 healthy并入 unhealthy，保留 Capacity并阻止新分配；宽限期后清理资源并由Node setter写0。
9. 内存先更新，checkpoint在锁外尝试写；失败只返回/记录error，没有事务回滚。
10. callback只遍历新快照，missing ID不触发更新；status读取时找不到 ID 又默认 Healthy。
11. 当前 Node setter只在 MachineInfo成功分支调用 DeviceManager.GetCapacity。
12. direct counter只证明收到请求；generic total plugins只证明某socket处于desired/actual状态，不证明first snapshot和GPU健康数。

主案最小断点是 `client.Run`调用 `ListAndWatch`到第一次 `stream.Recv()`返回之间。缺少 `State pushed`与 `Processed device updates`。若两条日志已经显示8/8，才把断点移向 GetCapacity、MachineInfo、Node setter和status patch。

健康题中 unhealthy set有1个ID，新Pod不能申请8个健康单位；这只能说明插件把一个逻辑entry标为不可分配，不足以证明物理卡消失；kubelet也不会因这一变化自动删除运行Pod。

断连题中 healthy set变空，原ID并入unhealthy set，并记录stopTime。5分钟只是判断阈值，仍要等后续GetCapacity与Node status传播。

</details>

### 24.6 通过标准

你能做到下面五件事，才算真正学完：

- 从 NVIDIA Plugin Pod 画到 scheduler cache，而不是只画到 Register；
- 区分 registered、connected、first snapshot、Node published；
- 手算 absent、8/8、8/7、8/0、0/0；
- 从生产证据判断断在 stream、内存账、MachineInfo、Node status还是scheduler账；
- 不靠删 socket、删 checkpoint或重启生产组件制造实验。

---

## 25. 源码锚点与本地测试边界

### 25.1 源码锚点

| 问题 | 当前 commit 的文件/函数 |
|---|---|
| DeviceManager 启动 | `pkg/kubelet/cm/devicemanager/manager.go::Start` |
| direct Register | `pkg/kubelet/cm/devicemanager/plugin/v1beta1/server.go::Register` |
| 建立 client | `handler.go::connectClient`、`client.go::Connect` |
| 读取插件选项 | `manager.go::PluginConnected` |
| ListAndWatch | `client.go::Run` |
| 重建设备表 | `manager.go::genericDeviceUpdateCallback` |
| Capacity/Allocatable | `manager.go::GetCapacity` |
| Node status | `pkg/kubelet/nodestatus/setters.go` |
| disconnect | `client.go::Disconnect`、`manager.go::PluginDisconnected` |
| 5分钟宽限 | `types.go::endpointStopGracePeriod` |
| checkpoint | `manager.go::readCheckpoint/writeCheckpoint` |
| 已分配设备健康 | `manager.go::UpdateAllocatedResourcesStatus` |

### 25.2 与本章结论最贴近的现有测试

```text
pkg/kubelet/cm/devicemanager/manager_test.go
  TestDevicePluginReRegistration
  TestUpdateCapacityAllocatable
  TestEndpointSyncOnDisconnect
  TestUpdateAllocatedResourcesStatus

pkg/kubelet/pluginmanager/pluginwatcher/plugin_watcher_test.go
  TestPluginRegistration
  TestPluginReRegistration
  TestPluginRegistrationAtKubeletStart
```

### 25.3 当前机器的真实执行结果

本地源码要求 Go `1.26.0`，当前机器是 Go `1.19.4`。2026-07-18 已实际尝试下面两组目标测试，两组都在读取 `go.work`时终止，尚未进入包加载、编译和测试执行：

```text
invalid go version '1.26.0': must match format 1.23
unknown directive: godebug
```

因此本课当前测试状态是：**工具链不满足，目标测试未运行**。这既不是源码测试 FAIL，也不是 PASS；需要换成匹配 `go.mod/go.work`的 Go 工具链后重新执行。

可在匹配工具链环境执行：

```powershell
go test ./pkg/kubelet/cm/devicemanager `
  -run 'Test(DevicePluginReRegistration|UpdateCapacityAllocatable|EndpointSyncOnDisconnect|UpdateAllocatedResourcesStatus)$' `
  -count=1

go test ./pkg/kubelet/pluginmanager/pluginwatcher `
  -run 'Test(PluginRegistration|PluginReRegistration|PluginRegistrationAtKubeletStart)$' `
  -count=1
```

---

## 26. 官方资料与版本校准

> 链接于 2026-07-18核对。生产必须固定实际 Kubernetes、NVIDIA Device Plugin、GPU Operator 和配置版本。

- [Kubernetes Device Plugins](https://kubernetes.io/docs/concepts/extend-kubernetes/compute-storage-net/device-plugins/)：注册、ListAndWatch、扩展资源和设备健康的官方概念边界。
- [Kubernetes Device Plugin v1beta1 API](https://github.com/kubernetes/kubernetes/tree/master/staging/src/k8s.io/kubelet/pkg/apis/deviceplugin/v1beta1)：协议定义；滚动分支只用于导航，源码结论仍以本地 commit 为准。
- [NVIDIA k8s-device-plugin](https://github.com/NVIDIA/k8s-device-plugin)：官方部署、配置、MIG、sharing 和 device list strategy。
- [Kubernetes plugin manager](https://github.com/kubernetes/kubernetes/tree/master/pkg/kubelet/pluginmanager)：generic watcher、desired/actual state和注册事务。

---

## 27. 一页收口

```text
host 有 8 张 GPU
  != Kubernetes 已有 8 个 GPU resource

NVIDIA Device Plugin Running
  -> direct Register
  -> kubelet 回连 endpoint
  -> GetDevicePluginOptions
  -> 后台 ListAndWatch
  -> first full snapshot
  -> all/healthy/unhealthy 三张表
  -> Capacity = healthy + unhealthy
  -> Allocatable = healthy
  -> Node Status
  -> API Server
  -> scheduler cache - 已绑定 Pod requests
```

本案最终结论：

```text
Device plugin connected
  + 没有 first snapshot 证据
  + 新节点 Node resource absent
  -> 先排 ListAndWatch 建流、首包与插件初始化
  -> 不先排 scheduler
```

四个最容易记错的边界：

```text
registration counter 增加 != 注册成功
registered/connected != first snapshot
Node Allocatable != 实时剩余
checkpoint 写失败 != 内存状态已回滚
```

下一课继续顺着成功路径读：

```text
Node 已经 8/8
Java 推理 Pod 已调度到 gpu-node-07
  -> kubelet 怎样识别 container limit
  -> 怎样选择具体 opaque device ID
  -> Topology/GetPreferredAllocation 怎样参与
  -> 何时调用 Allocate
  -> env/mount/device/CDI 怎样进入 runtime ContainerConfig
```
