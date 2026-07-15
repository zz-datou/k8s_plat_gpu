# 03_路线B_GPU算力平台运维

## 1. 这条路线的定位

GPU运维不是单纯装驱动，也不是训练模型。

更准确的定位是：

```text
基于 Kubernetes 管理昂贵 GPU 算力资源，
让训练、推理、AI应用能够稳定、高效、可观测、可治理地运行。
```

它的核心问题是：

```text
谁在什么时候使用哪张GPU？
用得是否稳定？
用得是否高效？
用得是否值得？
```

## 2. 企业里真实在用什么

### 2.1 GPU节点池

必须深入：

- GPU节点规格和型号：T4 / L4 / A10 / A100 / H100 等。
- NVIDIA Driver。
- CUDA兼容关系。
- NVIDIA Container Toolkit。
- containerd runtime配置。
- GPU节点标签、污点、亲和性。
- GPU Node Pool自动扩缩容。

企业实际用法：

```text
普通业务节点池和 GPU 节点池分开。
GPU 节点通常更贵，会设置 taint；只有带匹配 toleration 的 Pod 才能通过这道门。
`limits: nvidia.com/gpu: 1` 负责申请 GPU 数量，toleration 负责容忍 GPU 节点污点，两者是两套独立条件，不能混为一谈。企业通常通过工作负载模板、准入策略或队列系统同时补齐资源声明、节点选择和 toleration。
不同型号 GPU 用 label 或资源队列的 flavor 区分，比如推理用 L4，训练用 A100/H100。
```

一笔带过：

- CUDA编程不需要深入。
- 深度学习算法不需要深入。
- 模型训练代码不需要成为主线。

你要掌握的是：

```text
驱动、CUDA、容器运行时、K8s调度之间的运维关系。
```

### 2.2 GPU在Kubernetes里的资源模型

必须深入：

- Extended Resource。
- `nvidia.com/gpu`。
- Device Plugin。
- kubelet device manager。
- PodResources API。
- Node allocatable。
- scheduler NodeResourcesFit。

核心链路：

```text
NVIDIA device plugin 启动
  -> 向 kubelet 注册
  -> ListAndWatch 上报GPU设备和健康状态
  -> kubelet 更新 Node capacity / allocatable
  -> Pod 声明 limits: nvidia.com/gpu: 1
  -> scheduler 过滤有可用GPU的节点
  -> kubelet 启动Pod前调用 Allocate
  -> device plugin 返回设备、env、mount、CDI等信息
  -> runtime 启动容器
  -> 容器内可见GPU
```

学到什么程度：

```text
看到 GPU Pod Pending，
你能判断是资源不足、节点标签不匹配、taint不容忍、device plugin异常，
还是Node没有上报nvidia.com/gpu。
```

### 2.3 NVIDIA GPU Operator

必须深入：

- GPU Operator负责哪些组件。
- Driver是否由Operator管理。
- NVIDIA Container Toolkit。
- Device Plugin。
- DCGM Exporter。
- MIG Manager。
- Node Feature Discovery。

企业实际用法：

```text
云上或自建GPU集群通常会用GPU Operator统一安装和维护GPU相关组件。
它减少手工装驱动和插件的复杂度，但故障时仍然要知道每个组件负责什么。
```

一笔带过：

- Helm安装命令不用死记。
- 每个CRD字段不用一次性背。

必须知道：

```text
GPU Operator不是调度器。
它主要负责把GPU节点变成Kubernetes可识别、可监控、可管理的GPU节点。
```

### 2.4 GPU监控

必须深入：

- DCGM。
- DCGM Exporter。
- Prometheus指标。
- Grafana看板。
- GPU利用率。
- 显存使用。
- 温度、功耗。
- Xid错误。
- 每Pod GPU指标归属。

企业实际用法：

GPU看板至少要有：

```text
资源层：GPU数量、型号、节点、健康状态
利用率层：GPU util、显存使用、显存利用率
故障层：Xid、温度、driver错误、device plugin状态
业务层：模型QPS、延迟、token/s、batch size、队列长度
成本层：空闲GPU、低利用GPU、Spot使用、每团队用量
```

注意：

```text
GPU util 高不等于服务好。
GPU util 低也不一定是浪费，可能是低延迟推理场景需要预留。
```

### 2.5 模型服务

第一阶段只深入一个：vLLM。

后续再扩展：

- Triton
- KServe
- Ray Serve
- Seldon

为什么先 vLLM：

```text
更贴近大模型推理。
容易观察显存、并发、吞吐、延迟、KV cache。
能帮助你理解GPU运维不是只看Pod Running，而是要看模型服务是否真正可用。
```

学到什么程度：

```text
能部署一个模型推理服务。
能看显存占用和GPU利用率。
能压测并发。
能解释OOM、延迟抖动、吞吐不足。
能知道什么时候该扩副本，什么时候该换更大GPU。
```

### 2.6 GPU共享和多租户

必须理解：

- MIG
- time-slicing
- MPS
- namespace quota
- ResourceQuota
- Kueue
- Volcano
- PriorityClass
- 抢占和公平性

学习深度建议：

```text
先理解场景和风险，再做实验。
不要一开始钻实现细节。
```

企业实际用法：

```text
训练任务通常偏批处理和队列。
推理服务通常偏在线服务和SLA。
多团队共用GPU时，必须做配额、队列、优先级、成本归属。
```

MIG和time-slicing的区别先记：

```text
MIG偏硬隔离，适合把一张大卡切成多个相对独立的GPU实例。
time-slicing偏时间复用，隔离弱，适合轻量或低利用率场景。
```

## 3. GPU路线需要读源码到什么程度

### Kubernetes 源码真正需要深读

- scheduler `ScheduleOne`、Filter/Score、NodeResourcesFit、失败和重入队主链。
- kubelet `syncLoop`、podWorkers、`SyncPod` 与 CRI 边界。
- plugin manager、Device Plugin 注册、ListAndWatch、DeviceManager Allocate。
- podDevices/checkpoint、PodResources API 与容器设备注入。

### 机制和关键入口即可

- TopologyManager。
- CPUManager / MemoryManager 基本概念；需要独占 CPU、NUMA 优化时再深入。
- DRA / ResourceClaim 基本模型；只有目标集群实际采用 DRA 时再源码深读。
- controller / informer / workqueue；只有开发 GPU 平台控制器时再升到源码深读。

### 不必深入

- CUDA kernel。
- 深度学习框架内部算子。
- PyTorch训练代码实现。
- NVIDIA驱动源码。
- 所有GPU Operator CRD源码。

### 学到企业可用的程度

你能做到这些，就算进入GPU运维主线：

```text
1. 能搭一个可用GPU K8s节点池。
2. 能让GPU Pod稳定调度和启动。
3. 能解释nvidia.com/gpu从上报到分配的链路。
4. 能排查容器内看不到GPU。
5. 能部署一个推理服务并接入监控。
6. 能分析GPU利用率低、显存不足、服务延迟高。
7. 能给不同团队设计GPU配额和队列。
8. 能从成本角度识别空闲和低效GPU。
```
