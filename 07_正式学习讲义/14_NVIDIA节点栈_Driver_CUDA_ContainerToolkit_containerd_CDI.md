# 第 14 课：NVIDIA 节点栈——Driver、CUDA 用户态、Container Toolkit、containerd 与 CDI

> 主案例：GPU Node `Ready=True`，业务 Pod 已经调度到该 Node，但容器看不到 GPU，或 `CreateContainerError`  
> 本课主线：PCI 设备 -> NVIDIA kernel module/device node -> Driver 用户态库 -> CUDA 应用镜像 -> NVIDIA Container Toolkit -> containerd/OCI/CDI  
> Kubernetes 源码锚点：`RunContainerOptions -> runtimeapi.ContainerConfig.CDIDevices -> CRI CreateContainer`  
> 源码基线：`301946d15e67a4a2e8a5fb8292eb836acd366d78`（`v1.37.0-alpha.0-280-g301946d15e6`）  
> 本课深度：**运维 O3，NVIDIA/Container Toolkit/Kubernetes 源码 S0，只读边界证据**  
> 前置断点：第 12 课已经读到 CRI `CreateContainer`，第 13 课已经解释容器运行后的 probe/status/PLEG

---

## 0. 从这一课开始，案例主角正式换成 GPU

前 08～13 课用 Java Pod，是为了把 scheduler、kubelet、CRI 的通用骨架读透。从本课开始，主案例翻转：

```text
Java平台运维经验
  -> 保留：Node、Pod、CRI、Event、日志、变更窗口、回滚

新增GPU专项
  -> Driver、CUDA兼容、设备注入、GPU健康、推理服务
```

这不是重新学一遍 Kubernetes，也不是立刻去读 NVIDIA Driver 源码。本课要建立的是一张**责任栈**：

```text
哪一层坏了
  -> 该看什么证据
  -> 上一层为什么必然失败
  -> 哪些现象不能反向证明
```

本课不会提前把第 15～17 课的 Device Plugin/DeviceManager 读完。边界先定清：

- 本课回答“这台机器和这个容器是否具备使用 GPU 的底座”；
- 第 15 课回答“GPU 怎样注册并变成 Node Capacity/Allocatable”；
- 第 16 课回答“kubelet 怎样选择 device ID、调用 Allocate并组装注入参数”；
- 第 17 课回答“checkpoint、健康变化、PodResources 与 CDI怎样形成可恢复账本”。

---

## 1. 先看一个最容易被误诊的生产现场

GPU 推理 Pod：

```text
PodScheduled=True
spec.nodeName=gpu-node-07
Node Ready=True
容器状态=CreateContainerError
```

值班同学先执行：

```bash
nvidia-smi
```

宿主机能看到 GPU，于是判断“驱动没问题，应该是 Kubernetes 问题”。

这个结论过早。宿主机 `nvidia-smi` 成功只证明了责任栈中的一部分：

```text
PCI设备可见
  + 当前内核能使用匹配的NVIDIA驱动
  + nvidia-smi能装载NVML并查询设备
```

它没有证明：

- containerd 已启用正确的 NVIDIA 注入路径；
- CDI spec 存在且包含目标 device；
- Device Plugin 已把 `nvidia.com/gpu` 上报给 kubelet；
- kubelet 已给该 container 分配目标 device ID；
- CRI/OCI runtime 已把设备节点、库、环境变量或 hook注入容器；
- 镜像中的 CUDA runtime/framework 与宿主机 Driver兼容；
- 应用真的成功执行过 CUDA kernel。

反过来，Node `Ready=True` 只说明 kubelet的通用节点健康条件已满足，也不证明 GPU 栈健康。

---

## 2. 全栈责任图：从一块卡到一次 CUDA kernel

```text
物理卡 / 云平台PCI透传 / vGPU
  |
  | lspci、云平台设备挂载
  v
Linux内核 + NVIDIA kernel modules
  |
  | nvidia、nvidia_uvm、按平台需要的其他模块
  | /dev/nvidia0、/dev/nvidiactl、/dev/nvidia-uvm...
  v
NVIDIA Driver 用户态组件
  |
  | libcuda.so：CUDA Driver API
  | libnvidia-ml.so：NVML，nvidia-smi/DCGM常用
  v
容器镜像中的 CUDA 用户态
  |
  | libcudart.so、CUDA libraries、framework、业务应用
  v
NVIDIA Container Toolkit
  |
  | 发现设备和驱动文件
  | 标准workload走legacy hook或runtime原生CDI
  | 特定GPU管理容器可能另由NRI调整
  v
containerd CRI plugin -> container runtime -> OCI runtime
  |
  | 消费CRI ContainerConfig中的device/mount/env/CDI信息
  v
容器内进程
  |
  | CUDA Driver API -> kernel driver -> GPU
  v
CUDA kernel真正执行
```

Kubernetes 位于其中的上层编排位置，但要先分清两本资源账：

```text
传统Device Plugin：
  Pod limits: nvidia.com/gpu
    -> Node Capacity/Allocatable扩展资源
    -> scheduler按扩展资源账本选Node
    -> kubelet DeviceManager选具体device并取得注入参数

原生DRA：
  DeviceClass / ResourceSlice / ResourceClaim
    -> scheduler与DRA controller/driver完成claim allocation
    -> kubelet DRA manager取得CDI等注入信息

两条路径最终都可能：
  -> CRI把容器配置交给runtime
```

所以本章出现的`nvidia.com/gpu`、Node Capacity/Allocatable和第15～16章 DeviceManager，特指**传统 Device Plugin扩展资源路径**。原生 DRA 不要求Node上一定存在`nvidia.com/gpu`；它的可调度性与分配事实要看DeviceClass、ResourceSlice、ResourceClaim及其allocation，不能拿扩展资源字段代替。

还要加一个当前源码例外：本仓库`DRAExtendedResource`已是Beta且默认开启。若某个`DeviceClass.spec.extendedResourceName`匹配`nvidia.com/gpu`，看起来仍是传统extended resource的container limit，也可能被scheduler转换成DRA ResourceClaim。因此“YAML写了`nvidia.com/gpu: 1`”本身不再足以证明走传统DeviceManager；还要查匹配的DeviceClass和Pod `status.extendedResourceClaimStatus`。

Kubernetes 不会替你：

- 把一块未透传的 PCI 设备“变出来”；
- 编译一个不匹配当前内核的 NVIDIA kernel module；
- 自动修复镜像里的 CUDA library ABI；
- 仅凭 `limits: nvidia.com/gpu: 1` 安装 Driver；
- 仅凭 RuntimeClass 创建 `nvidia.com/gpu` 资源；
- 仅凭 CDI spec 判断一块 GPU 是否应该被 scheduler分配。

---

## 3. 用你熟悉的 Java 容器栈做类比

| Java平台层 | GPU平台对应层 | 共同排障思想 |
|---|---|---|
| 物理机/VM有CPU | 物理机/VM透传出GPU | 先证明硬件在宿主机责任域存在 |
| Linux kernel/cgroup | NVIDIA kernel module/device node | 先证明内核能管理资源 |
| glibc/JDK native库 | `libcuda.so`、NVML | 用户态入口必须能装载 |
| JRE/JDK与应用bytecode | CUDA runtime/framework与模型程序 | 应用依赖与底层兼容 |
| containerd/runc mount/ns | Toolkit + containerd/OCI/CDI | runtime负责把宿主机能力交给容器 |
| Deployment request/limit | `nvidia.com/gpu` limit | Kubernetes只消费资源账本 |

最重要的迁移不是记新命令，而是保持原来的证据纪律：

```text
不要因为Java进程存在就断言readiness健康
同样，不要因为nvidia-smi成功就断言CUDA业务健康

不要因为Node Ready就断言所有节点插件健康
同样，不要因为Pod Scheduled就断言GPU已经注入容器
```

---

## 4. 第一层：GPU 是否真的出现在这台 Node

### 4.1 裸金属与云主机先看 PCI/平台分配

查询意图的证据（通常不改变管理配置）：

```bash
lspci -nn | grep -i -E 'nvidia|3d controller|vga'
```

这一步回答：

```text
Linux PCI层是否看到了设备
```

它还没有回答：

```text
NVIDIA驱动是否绑定成功
设备是否健康
是否能执行CUDA
```

在云平台、VM、vGPU或裸金属直通环境，还要保留控制面证据：

- 实例规格/虚机型号；
- PCI passthrough、vGPU profile 或 SR-IOV分配；
- 宿主机与 guest 的 IOMMU/直通状态；
- 云厂商维护、热迁移、宿主机故障记录。

如果 `lspci` 根本没有 NVIDIA设备，优先回到硬件、BIOS、PCIe、云平台分配或虚拟化层；此时修改 Kubernetes YAML没有意义。

### 4.2 设备存在不等于驱动绑定

可补充查看：

```bash
lspci -nnk -d 10de:
```

重点区分：

```text
Kernel driver in use
Kernel modules
```

前者才是当前绑定使用的 driver。设备可能被 `vfio-pci` 占用、驱动未加载，或绑定状态与预期不一致。

---

## 5. 第二层：kernel module 与 device node

### 5.1 模块证据

```bash
uname -r
lsmod | grep -E '^nvidia'
modinfo nvidia 2>/dev/null | head
cat /proc/driver/nvidia/version 2>/dev/null
```

这些证据分别回答：

| 证据 | 回答什么 | 不能证明什么 |
|---|---|---|
| `uname -r` | 当前正在运行的kernel | NVIDIA模块已为该kernel构建 |
| `lsmod` | 模块当前已加载 | 每块卡都健康 |
| `modinfo` | 系统上模块文件及元数据 | 此模块就是当前加载版本 |
| `/proc/driver/nvidia/version` | 当前driver版本/构建信息 | 容器能看到driver库 |

生产中常见的升级事故：

```text
OS更新安装了新kernel
  -> Node尚未重启，当前kernel仍旧
  -> 或Node重启进入新kernel
  -> DKMS/precompiled module没有为新kernel准备好
  -> nvidia module加载失败
  -> /dev/nvidia*消失
  -> Device Plugin上报容量下降或启动失败
```

所以 Driver升级不是单独升级一个用户态命令。要把下面内容作为一个变更单元：

- kernel版本；
- 对应 kernel headers/devel包；
- NVIDIA driver branch/package；
- open/proprietary kernel module flavor；
- Secure Boot/module signing；
- NVSwitch机器上的 Fabric Manager等平台组件；
- 重启与回退路径。

### 5.2 open 与 proprietary kernel modules

当前 NVIDIA 官方安装文档同时提供 open 与 proprietary kernel module路径；支持范围和推荐会随 GPU代际、driver branch、操作系统变化。

本课不要求你背“某个版本以后永远用哪一种”。生产做法是：

1. 记录 GPU型号/架构；
2. 记录目标 driver branch；
3. 查该 branch 的官方支持矩阵；
4. 使用发行版包管理器或平台统一交付方式；
5. 在测试节点完成 reboot、CUDA workload、监控和回滚验证。

不要混装 runfile、发行版包、Operator容器化driver 等多种安装方式。文件来自不同包时，kernel module和用户态library很容易出现版本漂移。

### 5.3 device node 是内核能力交给进程的门

常见只读检查：

```bash
ls -l /dev/nvidia* 2>/dev/null
```

常见但不是每台机器都完全相同的节点：

```text
/dev/nvidia0、/dev/nvidia1 ... 具体GPU
/dev/nvidiactl                  控制设备
/dev/nvidia-uvm                 Unified Virtual Memory
/dev/nvidia-uvm-tools           UVM工具接口
/dev/nvidia-modeset             显示/模式相关，compute-only场景未必是主证据
/dev/nvidia-caps/*              某些能力/MIG相关节点
```

不要写死“必须正好有这五个”。设备代际、driver、MIG和工作负载能力不同，所需节点也不同。

容器里只有 `nvidia-smi` 二进制但没有正确 device node/driver library，仍然不能访问 GPU。

---

## 6. 第三层：Driver 并不只是一个 kernel module

在 Linux 上，NVIDIA driver package通常同时交付：

```text
kernel-mode components
  +
CUDA user-mode driver（典型为 libcuda.so）
  +
管理库（典型为 libnvidia-ml.so / NVML）
  +
相关工具和辅助组件
```

### 6.1 三个库不要混

| 名称 | 大白话职责 | 通常来自哪里 |
|---|---|---|
| `libnvidia-ml.so` | 管理和监控GPU；`nvidia-smi`大量能力基于NVML | 宿主机Driver |
| `libcuda.so` | 应用通过CUDA Driver API进入宿主机Driver | 宿主机Driver |
| `libcudart.so` | CUDA Runtime API实现，应用常直接链接 | 通常在应用/CUDA镜像中 |

这解释了为什么：

```text
容器内 nvidia-smi 成功
  -> device + NVML utility 路径大体可用

但应用仍报：
  libcudart.so not found
  CUDA driver version is insufficient
  no kernel image is available
  undefined symbol
```

因为监控路径与实际应用的 CUDA runtime、framework、编译架构路径并不完全相同。

### 6.2 host 通常不需要完整 CUDA Toolkit

GPU Node 只负责**运行**已经构建好的容器化应用时，宿主机通常需要：

```text
兼容的 NVIDIA Driver
NVIDIA Container Toolkit / runtime集成
```

而不必安装完整的 CUDA Toolkit、编译器 `nvcc`、cuDNN、PyTorch等开发依赖。这些通常随应用镜像交付。

官方 NVIDIA Data Center Driver工作流也明确区分：

- CUDA Toolkit：用于构建应用的用户态 SDK、runtime、libraries和工具；
- CUDA driver：用户态 `libcuda.so`；
- GPU device driver：kernel-mode component。

如果应用镜像动态链接某个 CUDA library，它仍必须在镜像或明确的兼容注入路径中存在；“宿主机装了 Toolkit”不是合理的镜像依赖管理方案。

---

## 7. 第四层：CUDA Toolkit、runtime、Driver 兼容关系

### 7.1 最稳定的心智模型

```text
应用/框架由某个CUDA Toolkit构建
  -> 镜像带相应CUDA runtime/libraries
  -> 运行时调用宿主机提供的CUDA Driver
  -> Driver必须满足该应用路径的兼容要求
```

常见兼容方向：

```text
较新的Driver运行较旧CUDA应用
  -> 通常走backward compatibility

同一major内较旧Driver运行较新minor Toolkit应用
  -> 可能走minor version compatibility
  -> 有最低Driver和特性限制

跨major让较旧Driver运行较新Toolkit
  -> 只有受支持平台/GPU下的forward compatibility路径
  -> 需要cuda-compat包等额外条件
```

不能简化成“Driver版本比CUDA数字大就一定行”。还要核对：

- 官方 Toolkit release notes 的最低 Driver；
- GPU architecture/compute capability；
- 应用是否带 PTX 或只带对应 SASS；
- framework/cuDNN/TensorRT/NCCL版本；
- 容器中实际装载的 library，而不是镜像标签想象值。

### 7.2 真正验证应用路径要执行 CUDA workload

证据强度从弱到强：

```text
lspci发现GPU
  <
nvidia-smi -L
  <
容器内nvidia-smi
  <
deviceQuery识别设备并PASS
  <
vectorAdd/受控CUDA sample执行成功
  <
目标framework能分配显存并完成一次真实推理/训练
```

任何一级成功都不能自动替代更上一级。

---

## 8. `nvidia-smi` 到底证明什么

NVIDIA 官方文档说明，`nvidia-smi` 的大量能力由 NVML 提供；其输出中的 `CUDA Version` 是**当前 Driver支持的最新 CUDA 版本**，不是“宿主机已安装 CUDA Toolkit版本”的可靠证明。

### 8.1 宿主机成功

下面两条是**查询意图**，通常不会修改 persistence mode、MIG、clock、power 等管理配置。这里仍不把它们承诺成“严格零状态变化”：NVIDIA 官方说明，以 root 运行 `nvidia-smi` 时可能调整 NVIDIA device file。生产取证优先使用有读取权限的非 root 身份，并记录执行身份、时间和命令。

```bash
nvidia-smi -L
nvidia-smi --query-gpu=index,uuid,name,pci.bus_id,driver_version --format=csv,noheader
```

可以支持：

- driver/NVML能枚举GPU；
- UUID、PCI bus ID、Driver版本等管理信息可查询；
- 当前调用时设备没有完全从Driver视角消失。

不能单独支持：

- Toolkit安装版本；
- 某个容器已拿到GPU；
- 某个CUDA应用兼容；
- Device Plugin资源账本正确；
- GPU没有Xid/ECC/链路等历史或间歇性故障；
- 一次多卡通信/NCCL拓扑健康。

### 8.2 容器内成功

容器内 `nvidia-smi` 成功，比宿主机成功多证明了：

```text
该容器获得了足够的device/NVML utility注入
```

它仍不等于：

```text
libcudart/framework版本正确
应用能执行目标kernel
显存足够
NCCL跨卡通信健康
```

### 8.3 不把人类表格输出当稳定API

NVIDIA 文档提示 `nvidia-smi` 人类可读输出不保证跨版本向后兼容。自动采集时：

- 优先 `--query-gpu=... --format=csv,noheader,nounits`；
- 长期维护程序优先使用NVML或官方稳定binding；
- 不用 `awk '{print $9}'` 解析整张动态表格；
- 保留命令版本、字段名、时间与Node身份。

---

## 9. 第五层：NVIDIA Container Toolkit 做了什么

NVIDIA Container Toolkit位于“宿主机Driver已经可用”和“容器真正获得GPU能力”之间。

它的工作不是替代Driver，也不是替代Kubernetes调度。大白话说：

```text
容器runtime已经会创建普通Linux容器
Toolkit告诉runtime：
  这次还要把哪些GPU设备节点
  哪些Driver用户态library/工具
  哪些环境变量、mount、hook或OCI edit
  交给这个容器
```

当前 Toolkit文档中的主要组件包括：

- `libnvidia-container` / `nvidia-container-cli`；
- `nvidia-ctk`；
- NVIDIA container runtime hook；
- 兼容场景中的 `nvidia-container-runtime`包装层；
- CDI以及NRI等生态集成能力（适用对象不同，见下文）。

历史文章经常让你安装独立 `nvidia-docker2`、把 `nvidia`设成所有容器默认 runtime。当前文档已经把一些旧包标为 deprecated/兼容用途；不能照抄旧博客。

### 9.1 三类路径要按现场识别

#### 路径 A：legacy hook / `NVIDIA_VISIBLE_DEVICES`

```text
容器配置带 NVIDIA_VISIBLE_DEVICES 等信息
  -> NVIDIA runtime/hook在OCI prestart阶段修改容器
  -> 注入driver能力
```

它在大量存量集群仍存在，但可能要求 named runtime或特定runtime配置。

#### 路径 B：CDI

```text
上游组件给runtime一个完全限定CDI device name
  -> runtime读取CDI spec
  -> 把spec中的OCI edits应用到容器
```

这减少了厂商专属环境变量与hook耦合，且是当前 NVIDIA栈的重要方向。

#### 路径 C：NRI（不要当成标准 GPU workload 的默认分配链）

```text
containerd在容器生命周期调用NRI plugin
  -> NVIDIA NRI插件可按环境变量等信息调整容器配置
```

在当前 GPU Operator v25.10+ 文档描述的常规模式里，标准 Device Plugin 或 DRA workload 主要让 runtime 原生消费 CDI；NRI 的明确用途更偏向 GPU 管理容器：这些容器使用 `NVIDIA_VISIBLE_DEVICES`，但不经过 Kubernetes 的 GPU 资源分配。自研集成或旧版本可能不同，必须按锁定版本确认。

所以不能把 NRI 画成与 Device Plugin、DRA 等价的第三套资源分配账本。它可以改变容器配置，却不替 scheduler/kubelet 决定“这个租户应拿哪块 GPU”。本章只要求识别责任边界，不把 NRI 实现读到源码深度。

这三条不是“看到其中一个名词就能推断全栈配置”。必须用锁定版本、配置输入、解析结果和runtime运行证据确认现场究竟走哪一条。

---

## 10. containerd、CRI、OCI runtime：谁负责哪一步

第 12 课已经建立通用链：

```text
kubelet
  -> CRI RuntimeService.CreateContainer
  -> containerd CRI plugin
  -> containerd task/runtime
  -> OCI runtime
  -> Linux namespaces/cgroups/mount/device
```

加上 GPU 后，主链变成：

```text
传统DeviceManager或DRA manager取得GPU注入信息
  -> kubelet生成CRI ContainerConfig
  -> Devices / Mounts / Envs / Annotations / CDIDevices
  -> containerd CRI plugin消费
  -> runtime原生CDI、legacy NVIDIA hook，或管理容器专用NRI等按现场路径处理
  -> OCI spec获得device node、mount、env、hook等edits
  -> OCI runtime创建容器
```

### 10.1 containerd 1.x 与 2.x 配置不要混抄

containerd官方当前文档明确区分：

```text
containerd 1.x：配置version 2，CRI配置主要位于 io.containerd.grpc.v1.cri
containerd 2.x：推荐配置version 3；runtime配置拆到 io.containerd.cri.v1.runtime，
               image配置拆到 io.containerd.cri.v1.images；
               io.containerd.grpc.v1.cri 仍承载部分gRPC/streaming配置
```

这不是“整个 CRI plugin 统一改了一个新 ID”，而是配置职责被拆分；具体字段、imports和默认值也可能不同。

生产排障先采：

```bash
systemctl show containerd -p ExecStart -p FragmentPath -p DropInPaths
systemctl cat containerd
ps -ef | grep '[c]ontainerd'
# 确认实际binary和--config后，再用同一binary、同一config执行config dump
containerd --version
containerd --config /path/from/ExecStart/config.toml config dump
```

`/etc/containerd/config.toml` 只是可能的输入之一：

- service参数可能指定其他路径；
- `imports`可能加载drop-in；
- GPU Operator/Toolkit可能管理额外配置；
- 文件已修改但daemon尚未reload/restart；
- `config dump` 只是解析命令行所选配置文件及imports；它不会向正在运行的daemon查询“内存中的真实配置”。

因此应先从 service `ExecStart`/实际进程确认 binary 和 `--config`，再用相同输入执行 `config dump`，并把结果称为“解析后的配置证据”。最后还要与 `ctr plugins ls`、`crictl info`、containerd日志及受控容器创建结果交叉验证。不要只 `grep config.toml`，也不要只凭一次 `config dump` 就宣布运行路径已经生效。

### 10.2 `nvidia-ctk runtime configure` 是变更命令

当前 NVIDIA Toolkit安装文档给出类似：

```bash
sudo nvidia-ctk runtime configure --runtime=containerd
```

该命令会修改或创建 containerd配置/drop-in，随后通常还需要重启containerd。

因此本课只把它放进**变更窗口章节**，不放进日常只读取证脚本。在线节点不能为了“看看会发生什么”直接执行。

---

## 11. CDI：不是设备，而是一份标准化“容器修改说明”

### 11.1 大白话定义

CDI（Container Device Interface）是一份开放规范。厂商用 spec描述：

```text
当容器请求 vendor.com/class=device-name 时
runtime 应给OCI spec加哪些：
  device nodes
  mounts
  environment variables
  hooks
  annotations
  其他标准化edits
```

CDI spec不是：

- 一块真实GPU；
- NVIDIA Driver；
- kubelet Device Plugin；
- scheduler资源模型；
- `nvidia.com/gpu` Capacity/Allocatable；
- GPU健康检查器；
- 自动选择“哪块GPU”的算法。

### 11.2 fully qualified device name

形式：

```text
vendor.com/class=device-name
```

NVIDIA场景常见示意：

```text
nvidia.com/gpu=all
nvidia.com/gpu=0
nvidia.com/gpu=GPU-...
```

具体可用名字必须以本机生成的 CDI spec和 `nvidia-ctk cdi list`为准。不要假设一定是index或UUID，也不要把示意值直接写进生产Pod。

### 11.3 一个缩小后的 spec

```yaml
cdiVersion: "0.8.0"
kind: "vendor.example/gpu"
devices:
  - name: "gpu0"
    containerEdits:
      deviceNodes:
        - path: /dev/example-gpu0
      mounts:
        - hostPath: /opt/vendor/lib/libexample.so
          containerPath: /usr/lib/libexample.so
      env:
        - EXAMPLE_VISIBLE_DEVICE=gpu0
```

容器请求：

```text
vendor.example/gpu=gpu0
```

runtime解析后才把 edits 合并进 OCI spec。

这也解释一个重要失败：

```text
kubelet/CRI给出一个CDI名字
  -> runtime本地spec里不存在
  -> CreateContainer失败
```

这不一定是scheduler错，也不一定是Driver不可见；它可以是“分配账本与runtime本地CDI目录漂移”。

---

## 12. 当前 NVIDIA CDI 的版本事实

以下是截至 **2026-07-13** 查到的官方文档事实，生产使用时仍应按你们锁定版本复核：

- NVIDIA Container Toolkit从较早版本已支持生成CDI spec；
- 当前文档描述 `nvidia-cdi-refresh` 自动生成/刷新 `/var/run/cdi/nvidia.yaml`；
- `nvidia-ctk cdi list` 用于列出可用CDI device；
- driver卸载、MIG重新配置等场景可能需要显式刷新或重启refresh服务；
- GPU Operator v25.10文档把CDI作为标准workload的重要默认路径；
- 标准 Device Plugin/DRA workload在该模式下通常不要求每个Pod写 `runtimeClassName`；
- 某些管理容器、legacy `NVIDIA_VISIBLE_DEVICES`或特定集成模式仍可能需要RuntimeClass。

因此不能用两个绝对句：

```text
错误1：GPU Pod 永远必须 runtimeClassName: nvidia
错误2：GPU Pod 永远不需要 RuntimeClass
```

正确问法：

```text
你们锁定的GPU Operator/Toolkit/containerd版本是什么？
标准workload走legacy hook还是runtime原生CDI？管理容器是否另用NRI？
Device Plugin采用什么deviceListStrategy？
containerd有效runtime/CDI配置是什么？
这个Pod的CRI ContainerConfig实际带了什么？
```

---

## 13. 回到当前 Kubernetes 源码：它只把“注入意图”交给 CRI

本课源码深度是 S0，但仍要用三段当前仓库代码钉住边界。

### 13.1 DeviceManager/DRA 的结果汇合到 `RunContainerOptions`

```text
pkg/kubelet/cm/container_manager_linux.go:753-779
```

```go
func (cm *containerManagerImpl) GetResources(
    ctx context.Context,
    pod *v1.Pod,
    container *v1.Container,
) (*kubecontainer.RunContainerOptions, error) {
    opts := &kubecontainer.RunContainerOptions{}

    if utilfeature.DefaultFeatureGate.Enabled(
        kubefeatures.DynamicResourceAllocation,
    ) {
        resOpts, err := cm.draManager.GetResources(pod, container)
        if err != nil {
            return nil, err
        }
        opts.CDIDevices = append(
            opts.CDIDevices,
            resOpts.CDIDevices...,
        )
    }

    devOpts, err :=
        cm.deviceManager.GetDeviceRunContainerOptions(
            ctx,
            pod,
            container,
        )
    // ...
    opts.Devices = append(opts.Devices, devOpts.Devices...)
    opts.Mounts = append(opts.Mounts, devOpts.Mounts...)
    opts.Envs = append(opts.Envs, devOpts.Envs...)
    opts.Annotations =
        append(opts.Annotations, devOpts.Annotations...)
    opts.CDIDevices =
        append(opts.CDIDevices, devOpts.CDIDevices...)
    return opts, nil
}
```

本课只取结论：

```text
传统Device Plugin路径
  -> 可以给出device/mount/env/annotation/CDI

DRA路径
  -> 主要给出CDI devices

两条路径的结果
  -> 汇合到RunContainerOptions
```

“谁选中 device、为什么有这些结果”留到第 15～17 课。

#### 13.1.1 两本账不能互相替代

| 对比 | 传统 Device Plugin | 原生 DRA |
|---|---|---|
| workload声明 | container `limits: nvidia.com/gpu: 1`等扩展资源 | ResourceClaim/claim template等DRA声明 |
| 集群资源发现 | Node `status.capacity/allocatable`中的扩展资源 | DeviceClass、ResourceSlice等DRA对象 |
| 分配事实 | kubelet DeviceManager内部设备账与checkpoint | ResourceClaim `status.allocation`及DRA组件账本 |
| kubelet执行者 | DeviceManager | DRA manager |
| 常见注入输出 | devices、mounts、env、annotations或CDI | 主要是CDI device names |
| 是否必有`nvidia.com/gpu` | 这条路径需要对应扩展资源 | 不一定，也不应据此判断DRA健康 |

`DRAExtendedResource`是两栏之间的桥：它允许相同extended resource请求被匹配的DeviceClass转换为DRA claim。本章第19节对照实验只有在明确排除这个映射后才验证左栏；它既不能只凭`nvidia.com/gpu: 1`断言传统路径，也不能拿该Pod宣称“DRA已通过”。

### 13.2 kubelet只复制 CDI name 到 CRI

```text
pkg/kubelet/kuberuntime/kuberuntime_container.go:342-391
pkg/kubelet/kuberuntime/kuberuntime_container.go:470-480
```

```go
config := &runtimeapi.ContainerConfig{
    // ...
    Devices:    makeDevices(opts),
    CDIDevices: makeCDIDevices(opts),
    Mounts:     m.makeMounts(opts, container),
}

func makeCDIDevices(
    opts *kubecontainer.RunContainerOptions,
) []*runtimeapi.CDIDevice {
    devices := make(
        []*runtimeapi.CDIDevice,
        len(opts.CDIDevices),
    )

    for i, device := range opts.CDIDevices {
        devices[i] = &runtimeapi.CDIDevice{
            Name: device.Name,
        }
    }
    return devices
}
```

这里没有：

```text
打开 /var/run/cdi/nvidia.yaml
解析spec
检查hostPath
把/dev/nvidia0 mount进容器
```

这些工作属于 CRI/runtime/CDI实现边界。kubelet在这里主要完成数据结构转换。

### 13.3 CRI 协议只要求 fully qualified name

```text
staging/src/k8s.io/cri-api/pkg/apis/runtime/v1/api.proto:1224-1292
```

```protobuf
message CDIDevice {
    // Fully qualified CDI device name
    // for example: vendor.com/gpu=gpudevice1
    string name = 1;
}

message ContainerConfig {
    // ...
    repeated Device devices = 8;
    repeated CDIDevice CDI_devices = 17;
}
```

由此可反推：

```text
CreateContainer报 unknown CDI device
  -> 先核对CRI传入的name
  -> 再核对目标Node runtime的CDI cache/spec
  -> 不是先去改scheduler
```

### 13.4 RuntimeClass是另一条选择 runtime handler 的链

第 12 课读过：

```text
pkg/kubelet/kuberuntime/kuberuntime_sandbox.go:36-72
```

```go
runtimeHandler := ""
if m.runtimeClassManager != nil {
    runtimeHandler, err =
        m.runtimeClassManager.LookupRuntimeHandler(
            pod.Spec.RuntimeClassName,
        )
    if err != nil {
        return "", message, err
    }
}

podSandBoxID, err :=
    m.runtimeService.RunPodSandbox(
        ctx,
        podSandboxConfig,
        runtimeHandler,
    )
```

两条链不要混成一条：

```text
RuntimeClass
  -> 为PodSandbox选择CRI runtime handler

CDIDevice
  -> 为具体container给出标准化device selector
```

某种部署可以把二者组合使用；另一个部署可以用默认handler原生消费CDI。`runtimeClassName: nvidia` 不是创建 GPU扩展资源的开关。

---

## 14. 本章唯一需要现场补的 Go 语法

本章不展开Go课程，只解释刚才会挡住读源码的三点。

### 14.1 `append(dst, src...)`

```go
opts.CDIDevices =
    append(opts.CDIDevices, devOpts.CDIDevices...)
```

大白话：

```text
把devOpts.CDIDevices这整个slice逐个追加到opts.CDIDevices
```

末尾的 `...` 是“把 slice展开为多个参数”，不是“省略代码”。

例子：

```go
a := []string{"gpu-a"}
b := []string{"gpu-b", "gpu-c"}
a = append(a, b...)
// a = ["gpu-a", "gpu-b", "gpu-c"]
```

### 14.2 `make([]*T, len(...))`

```go
devices := make(
    []*runtimeapi.CDIDevice,
    len(opts.CDIDevices),
)
```

创建一个长度已经确定的 slice。后面 `devices[i] = ...` 按下标填充。

它与下面写法不同：

```go
devices := []*runtimeapi.CDIDevice{}
devices = append(devices, oneDevice)
```

前者已分配长度、按位置写；后者长度从0开始、逐个追加。

### 14.3 `&runtimeapi.CDIDevice{...}`

```go
devices[i] = &runtimeapi.CDIDevice{
    Name: device.Name,
}
```

- `{Name: ...}` 构造 struct；
- `&` 取得这个struct的指针；
- 目标slice类型是 `[]*runtimeapi.CDIDevice`，元素必须是指针。

这段转换没有偷偷解析 CDI。它只把同一个字符串放进另一个协议struct。

---

## 15. 六层证据矩阵：先定位层，再决定命令

| 层 | 最小正向证据 | 典型失败 | 首要责任域 |
|---|---|---|---|
| 物理/虚拟化 | `lspci`或云平台清单有目标GPU | 完全没有PCI设备 | 硬件/云平台/虚拟化 |
| kernel/Driver | 模块加载、device node存在、host `nvidia-smi -L`成功 | module load失败、driver/library mismatch | Node OS/Driver |
| Toolkit | 版本可查，能发现driver/device，CDI可列出 | hook/toolkit缺失、spec生成失败 | Node镜像/Toolkit |
| containerd/OCI | 配置与plugin/CRI运行证据一致，受控创建能解析CDI | unknown runtime、unknown CDI device、hook错误 | container runtime |
| Kubernetes设备账本 | Node有`nvidia.com/gpu` Capacity/Allocatable | Device Plugin未上报或资源为0 | kubelet/Device Plugin |
| 应用 | CUDA sample/framework真实执行 | driver insufficient、library/arch/OOM/NCCL错误 | 镜像/框架/模型/应用 |

### 15.1 一个更实用的“从现象反查”

| 现象 | 先查 | 不要先做 |
|---|---|---|
| Node无`nvidia.com/gpu` | host Driver -> Device Plugin日志/ListAndWatch -> Node status | 重建业务Deployment |
| Pod `NODE=<none>`、Insufficient GPU | Capacity/Allocatable、已有requests、taint/affinity | 查容器内`nvidia-smi` |
| Pod已绑定、CreateContainerError | Event完整message、kubelet/runtime日志、CDI name/spec | 扩GPU节点数 |
| 容器Running但`nvidia-smi`不存在 | 镜像是否含工具、utility capability、注入策略 | 直接判定GPU不可见 |
| 容器`nvidia-smi`成功，framework不可用 | 镜像CUDA库、Driver兼容、framework实际错误 | 重装Device Plugin |
| 一段时间后GPU掉卡 | Xid/内核日志/DCGM/PCIe/硬件 | 只重启Pod后关闭工单 |

---

## 16. 生产证据采集：四个快照必须同一时刻、同一 Node

### 16.1 快照 A：Kubernetes对象

从管理端：

```powershell
$ns = '__NAMESPACE__'
$pod = '__POD__'

$uid = kubectl get pod $pod -n $ns -o jsonpath='{.metadata.uid}'
if ($LASTEXITCODE -ne 0 -or -not $uid) {
  throw '读取Pod失败'
}

$node = kubectl get pod $pod -n $ns -o jsonpath='{.spec.nodeName}'
if ($LASTEXITCODE -ne 0) {
  throw '读取spec.nodeName失败'
}

kubectl get pod $pod -n $ns -o yaml
kubectl get events -n $ns `
  --field-selector "involvedObject.uid=$uid" `
  -o custom-columns='FIRST:.firstTimestamp,LAST:.lastTimestamp,COUNT:.count,TYPE:.type,REASON:.reason,MESSAGE:.message'

if ($node) {
  kubectl get node $node `
    -o custom-columns='NAME:.metadata.name,READY:.status.conditions[?(@.type=="Ready")].status,GPU_CAP:.status.capacity.nvidia\.com/gpu,GPU_ALLOC:.status.allocatable.nvidia\.com/gpu'
  kubectl describe node $node
} else {
  Write-Host 'Pod尚未绑定Node；先回第08～10课调度责任域'
}
```

生产输出可能含：

- image地址；
- annotation；
-环境变量引用；
- Node label/taint；
- 内部域名与IP。

保存到受控工单前按制度脱敏；不要把Secret值、registry credential或完整业务配置粘进公开聊天。

### 16.2 快照 B：宿主机 Driver

在**上一步确认的目标 Node**上执行查询意图命令。优先使用有读取权限的非 root 身份；若制度要求提权，记录审批、身份与时间。它们通常不改变 GPU 管理配置，但不能被描述成绝对“零状态变化”：

```bash
date -Is
hostname -f
uname -r
cat /etc/os-release

lspci -nn | grep -i -E 'nvidia|3d controller|vga' || true
lspci -nnk -d 10de: || true

lsmod | grep -E '^nvidia' || true
cat /proc/driver/nvidia/version 2>/dev/null || true
find /dev -maxdepth 2 -name 'nvidia*' -ls 2>/dev/null

nvidia-smi -L
nvidia-smi \
  --query-gpu=index,uuid,name,pci.bus_id,driver_version,pstate,temperature.gpu,memory.total,memory.used \
  --format=csv,noheader
```

这里故意不用：

```bash
nvidia-smi --gpu-reset
nvidia-smi -pm 1
nvidia-smi -mig 1
modprobe -r nvidia
```

它们会改状态、影响在跑任务，不能混进取证脚本。

### 16.3 快照 C：Toolkit、CDI、containerd

```bash
containerd --version 2>&1 || true
runc --version 2>&1 | head -n 3 || true
nvidia-ctk --version 2>&1 || true
nvidia-container-cli --version 2>&1 || true

systemctl show containerd \
  -p FragmentPath -p DropInPaths -p ExecStart -p ActiveState -p SubState
systemctl cat containerd
ps -ef | grep '[c]ontainerd'

# 先从上面三份证据确认实际binary与--config。
# 然后人工替换占位符，使用同一binary、同一配置输入：
__ACTUAL_CONTAINERD_BINARY__ \
  --config __ACTUAL_CONTAINERD_CONFIG__ config dump

ctr plugins ls 2>&1 | grep -E 'cri|runtime|images' || true
crictl info 2>&1

nvidia-ctk cdi list 2>&1 || true
nvidia-ctk cdi generate 2>&1 | sed -n '1,160p'

systemctl status nvidia-cdi-refresh.path \
  nvidia-cdi-refresh.service --no-pager 2>&1 || true
journalctl -u nvidia-cdi-refresh.service \
  --since '-2 hours' --no-pager 2>&1 || true

find /etc/cdi /var/run/cdi \
  -maxdepth 2 -type f -printf '%p %TY-%Tm-%TdT%TH:%TM:%TS %s bytes\n' \
  2>/dev/null
```

说明：

- `nvidia-ctk cdi generate` 省略 `--output` 时把预期spec写到stdout，不直接覆盖文件；
- 某些版本/发行包没有 `nvidia-cdi-refresh` unit，不能仅凭unit不存在判Toolkit坏；
- `config dump`解析的是命令所选配置，不是向daemon读取内存状态；必须先确认实际binary与`--config`，再和plugin清单、`crictl info`、日志及受控创建交叉验证；
- `config dump`、`crictl info`和`systemctl cat`可能暴露私有registry、代理、路径或环境变量，外发前必须脱敏；
- 不要为了看日志临时重启containerd；
- 命令是否存在、参数是否支持，要以目标版本 `--help`为准。

### 16.4 快照 D：同一个 Pod 的 sandbox/container runtime事实

```bash
crictl pods --name '__POD__'
crictl ps -a --label io.kubernetes.pod.uid='__POD_UID__'
```

先分清两种对象：

```bash
# 已创建成功的sandbox：handler属于PodSandboxStatus这一侧
crictl inspectp '__SANDBOX_ID__'

# 只有container已经创建出ID，才有对象可inspect
crictl inspect '__CONTAINER_ID__'
```

标准 CRI 边界不要混写：

- `PodSandboxStatus`一侧可核对sandbox状态和runtime handler；
- `ContainerStatus`一侧可核对container状态、reason/message、mounts、annotations、log path等；
- 标准`ContainerStatus`没有承诺返回handler、完整devices/env/CDI输入；某些runtime的verbose `info`可能暴露额外内部字段，但字段不稳定，不能写成CRI通用事实；
- `CreateContainer`若在创建对象前失败，通常根本没有container ID，这是正常边界，不要反复找一个不存在的inspect对象；
- 对`CreateContainerError`，以Pod UID、sandbox ID和严格时间窗关联Event、kubelet日志、containerd日志；只对确实创建成功的container做inspect。

#### 原始 inspect 可能泄露 Secret，默认不能外发

runtime-specific verbose输出可能含已展开的环境变量、参数、registry/auth信息或敏感annotation。**禁止把原始`crictl inspect/inspectp`直接上传工单、群聊或公开聊天。**

默认只提取故障所需字段，例如：对象ID、状态、reason/message、handler、必要的mount/CDI线索；删除环境变量值、args、registry/auth和无关annotation。只有在目标Node已获授权时，才可把原始JSON临时写进受控目录，权限设为`0600`，在Node本地完成复核与脱敏，并按取证制度及时销毁原件。字段随CRI/runtime版本变化，提取规则也必须按现场JSON复核。

---

## 17. 四个最常见的错误推理

### 17.1 “Node Ready，所以 GPU Ready”

错。Node Ready是通用kubelet/runtime/node status边界；GPU资源可能因为 Device Plugin未注册而是0，Driver也可能在Node Ready之后异常。

### 17.2 “host `nvidia-smi` 成功，所以容器runtime没问题”

错。host路径不经过 containerd、CDI/hook、OCI spec和容器namespace。

### 17.3 “容器内`nvidia-smi`成功，所以模型能跑”

错。它主要验证device + NVML utility路径，不覆盖所有CUDA runtime/framework/library/显存/算子架构路径。

### 17.4 “Pod写了`nvidia.com/gpu: 1`，Kubernetes就会注入GPU”

不完整。这个声明先参与资源账本；真正注入还依赖：

```text
Device Plugin注册/健康
  -> DeviceManager分配device
  -> AllocateResponse或CDI selector
  -> CRI ContainerConfig
  -> runtime应用注入
```

第 15～17 课会把这条链读穿。

---

## 18. 安全实验 1：查询取证，不做 GPU 管理变更

### 18.1 前置条件

这份仓库当前没有连接真实 GPU实验环境，因此下面是**可执行实验设计**，不是已在你的集群跑出的 PASS。

执行前必须满足：

- 目标是实验节点或经过批准的生产查询取证；
- 查询优先用非 root 身份；若必须提权，记录审批与执行身份；
- 已由 Pod的 `spec.nodeName`确认目标Node；
- 不运行reset、MIG切换、module unload、runtime restart；
- 输出进入受控目录，外发前脱敏；
- 命令缺失时记录“工具未安装”，不临时在线安装。

### 18.2 采集并建立因果表

把第 16.2、16.3 的命令输出按下面格式保存：

| 时间 | Node | 检查 | 结果 | 能证明 | 不能证明 |
|---|---|---|---|---|---|
| T0 | gpu-node-07 | `lspci` | 有2块NVIDIA设备 | PCI可见 | driver健康 |
| T0 | gpu-node-07 | `/proc/driver/nvidia/version` | 记录实际值 | 当前加载driver信息 | CUDA app兼容 |
| T0 | gpu-node-07 | `nvidia-smi -L` | 记录实际值 | NVML枚举 | 容器注入 |
| T0 | gpu-node-07 | `nvidia-ctk cdi list` | 记录实际值 | Toolkit看到的CDI names | K8s已分配 |
| T0 | gpu-node-07 | 同binary/同`--config`的`config dump` | 记录/脱敏 | 配置文件被解析后的结果 | daemon已应用、当前Pod必然走该路径 |

实验验收不是“所有命令都输出固定字符串”，而是你能回答：

1. 失败从哪一层首次出现？
2. 上一层有哪些硬证据仍正常？
3. 下一层为什么必然受影响？
4. 修复动作属于哪支团队/哪个变更窗口？

---

## 19. 安全实验 2：普通容器与 GPU 容器做对照

### 19.1 为什么要对照

只跑一个GPU Pod时，看到 `nvidia-smi`成功，你无法确认：

```text
是Kubernetes按资源请求精确注入
还是该Node把GPU默认暴露给所有容器
```

所以在**隔离实验GPU节点**上，用同一个镜像、同一个Node做：

```text
negative control：不请求GPU
positive control：limits请求1个GPU
```

预期：

```text
negative control不能枚举GPU
positive control能枚举恰当范围的GPU并执行CUDA smoke
```

如果平台明确设计为给所有容器暴露GPU，这个预期不成立；但那必须是有文档、有安全评审的显式设计，不能当作默认合理。

### 19.2 先限定实验适用范围

本实验只验证这一条传统路径：

```text
NVIDIA Device Plugin
  -> 扩展资源 nvidia.com/gpu: 1
  -> 一次独占、完整GPU分配
  -> runtime注入
```

它**不适用于**原生 DRA、MIG 资源、time-slicing、MPS或“默认向所有容器暴露GPU”的平台。实验Node必须由平台预先打上“允许GPU实验”和“exclusive-full-gpu”标签；脚本只读这些标签，绝不临时给Node补标签来绕过治理。此外必须确认没有`DeviceClass.spec.extendedResourceName=nvidia.com/gpu`的DRA映射，并在Pod创建后复核`status.extendedResourceClaimStatus`为空。

运行前替换：

```powershell
$approvedContext = '__APPROVED_KUBE_CONTEXT__'
$approvedNode = '__APPROVED_GPU_LAB_NODE__'
$gpuImage = '__INTERNAL_VALIDATED_GPU_IMAGE_BY_DIGEST__'
$runtimeClass = '' # 只有锁定版本和平台设计明确要求时才填写
$keepArtifacts = $false
```

镜像必须：

- 使用digest锁定，不能写漂移的`latest`；
- 已由安全扫描和镜像治理批准；
- 固定包含`/opt/gpu-lab/cuda-smoke`，不允许把任意shell命令从变量拼入YAML；
- 该脚本已经代码评审，至少申请device memory并真实launch、同步一个CUDA kernel；不能只打印版本；
- 成功返回0，失败返回非0；
- 不下载模型、不访问外网、不长时间占用GPU。

示例治理标签名是：

```text
ops.example.com/gpu-lab-approved=true
ops.example.com/gpu-allocation-mode=exclusive-full-gpu
```

请由平台团队把示例域名改成内部正式标签键；**先完成Node准入流程，再运行实验**。

### 19.3 受控实验脚本

> 只在有空闲GPU的测试池运行。若Node有专用taint，先由平台方把经评审的精确toleration固化进实验模板；不要为实验删除Node taint。下面脚本故意不自动猜测或放宽toleration。

```powershell
$ErrorActionPreference = 'Stop'

$approvedContext = '__APPROVED_KUBE_CONTEXT__'
$approvedNode = '__APPROVED_GPU_LAB_NODE__'
$gpuImage = '__INTERNAL_VALIDATED_GPU_IMAGE_BY_DIGEST__'
$runtimeClass = ''
$keepArtifacts = $false

$approvalLabelKey = 'ops.example.com/gpu-lab-approved'
$modeLabelKey = 'ops.example.com/gpu-allocation-mode'
$dnsSubdomain = '\A(?=.{1,253}\z)(?:[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?\.)*[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?\z'

function Invoke-KubectlText {
  param([Parameter(Mandatory=$true)][string[]]$KubectlArgs)

  $output = & kubectl --context $approvedContext @KubectlArgs 2>&1
  if ($LASTEXITCODE -ne 0) {
    throw ('kubectl失败：kubectl --context <approved> ' + ($KubectlArgs -join ' '))
  }
  return (($output | Out-String).TrimEnd())
}

if (
  $approvedContext -like '__*' -or
  $approvedNode -like '__*' -or
  $gpuImage -like '__*'
) {
  throw '先替换集群context、Node和镜像digest'
}
if ($approvedContext -notmatch '\A\S+\z') {
  throw 'approvedContext必须是无空白、无换行的精确context名称'
}
if ($approvedNode -notmatch $dnsSubdomain) {
  throw 'approvedNode必须是合法DNS subdomain，且不能含空白或换行'
}
if ($gpuImage -notmatch '\A[^@\s]+@sha256:[0-9a-fA-F]{64}\z') {
  throw 'GPU实验镜像必须是无空白、单个@、sha256 digest锁定的引用'
}
if ($gpuImage -notmatch '\A[A-Za-z0-9._:/-]+@sha256:[0-9a-fA-F]{64}\z') {
  throw '镜像引用含模板不接受的字符；只允许常见registry/repository字符'
}
if ($runtimeClass) {
  if ($runtimeClass -notmatch $dnsSubdomain) {
    throw 'runtimeClass必须是合法DNS subdomain，且不能含空白或换行'
  }
}

$actualContextOutput = & kubectl config current-context 2>&1
if ($LASTEXITCODE -ne 0 -or -not $actualContextOutput) {
  throw '读取current-context失败或返回空值'
}
$actualContext = (($actualContextOutput | Out-String).TrimEnd())
if ($actualContext -cne $approvedContext) {
  throw "当前context=$actualContext，不是批准的context=$approvedContext"
}

if ($runtimeClass) {
  $actualRuntimeClass = Invoke-KubectlText -KubectlArgs @(
    'get','runtimeclass',$runtimeClass,
    '-o','jsonpath={.metadata.name}'
  )
  if (-not $actualRuntimeClass -or $actualRuntimeClass -cne $runtimeClass) {
    throw 'RuntimeClass不存在、读取失败或返回名称不一致'
  }
}

$nodeJsonText = Invoke-KubectlText -KubectlArgs @(
  'get','node',$approvedNode,'-o','json'
)
if (-not $nodeJsonText) { throw 'Node JSON为空' }
$nodeObject = $nodeJsonText | ConvertFrom-Json
if ($nodeObject.metadata.name -cne $approvedNode) {
  throw 'API返回的Node名称与批准名称不一致'
}
$approvalValue = $nodeObject.metadata.labels.PSObject.Properties[$approvalLabelKey].Value
$modeValue = $nodeObject.metadata.labels.PSObject.Properties[$modeLabelKey].Value
if ($approvalValue -cne 'true') {
  throw "Node缺少预先审批标签：$approvalLabelKey=true"
}
if ($modeValue -cne 'exclusive-full-gpu') {
  throw "Node不是exclusive-full-gpu实验池：$modeLabelKey"
}

$gpuAlloc = Invoke-KubectlText -KubectlArgs @(
  'get','node',$approvedNode,
  '-o','jsonpath={.status.allocatable.nvidia\.com/gpu}'
)
if (-not $gpuAlloc -or $gpuAlloc -notmatch '\A[1-9][0-9]*\z') {
  throw '目标Node没有正整数nvidia.com/gpu allocatable；先进入第15课责任域排障'
}

# 当前commit的DRAExtendedResource可能把同名extended resource转换成DRA claim。
# 查询失败（API不存在、RBAC不足、网络错误）时一律停止，不能猜“应该没有”。
$deviceClassesText = Invoke-KubectlText -KubectlArgs @(
  'get','deviceclasses.resource.k8s.io','-o','json'
)
if (-not $deviceClassesText) { throw 'DeviceClass JSON为空，无法排除DRA映射' }
$deviceClasses = @(
  ($deviceClassesText | ConvertFrom-Json).items
)
$mappedClasses = @(
  $deviceClasses | Where-Object {
    $_.spec.extendedResourceName -ceq 'nvidia.com/gpu'
  }
)
if ($mappedClasses.Count -gt 0) {
  $mappedNames = ($mappedClasses.metadata.name -join ',')
  throw "存在把nvidia.com/gpu映射到DRA的DeviceClass：$mappedNames"
}

$owner = [guid]::NewGuid().ToString()
$suffix = [guid]::NewGuid().ToString('N').Substring(0,8)
$ns = 'gpu-stack-lab-' + $suffix
$expiresAt = (Get-Date).ToUniversalTime().AddMinutes(20).ToString('o')
$createdNamespace = $false

try {
  $createNamespace = Invoke-KubectlText -KubectlArgs @(
    'create','namespace',$ns
  )
  if (-not $createNamespace) { throw '创建namespace返回空输出' }
  $createdNamespace = $true

  $labelResult = Invoke-KubectlText -KubectlArgs @(
    'label','namespace',$ns,"studyowner=$owner",'--overwrite'
  )
  if (-not $labelResult) { throw '写namespace owner label返回空输出' }
  $annotationResult = Invoke-KubectlText -KubectlArgs @(
    'annotate','namespace',$ns,
    "study.example.com/expires-at=$expiresAt",'--overwrite'
  )
  if (-not $annotationResult) { throw '写namespace过期时间返回空输出' }

  $runtimeClassBlock = ''
  if ($runtimeClass) {
    $runtimeClassBlock = "  runtimeClassName: $runtimeClass`n"
  }

  $manifest = @'
apiVersion: v1
kind: Pod
metadata:
  name: gpu-negative
  namespace: __NAMESPACE__
  labels:
    app: gpu-stack-lab
    control: negative
spec:
__RUNTIME_CLASS_BLOCK__  restartPolicy: Never
  activeDeadlineSeconds: 120
  affinity:
    nodeAffinity:
      requiredDuringSchedulingIgnoredDuringExecution:
        nodeSelectorTerms:
        - matchFields:
          - key: metadata.name
            operator: In
            values:
            - __NODE__
  containers:
  - name: check
    image: __IMAGE__
    imagePullPolicy: IfNotPresent
    command: ["/bin/sh", "-c"]
    args:
    - |
      set -eu
      if find /dev -maxdepth 1 -type c -name 'nvidia[0-9]*' -print -quit | grep -q .; then
        echo "unexpected_gpu_device_node"
        exit 41
      fi
      if nvidia-smi --query-gpu=uuid --format=csv,noheader; then
        echo "unexpected_nvidia_smi_success"
        exit 42
      fi
      if /opt/gpu-lab/cuda-smoke; then
        echo "unexpected_cuda_smoke_success"
        exit 43
      fi
      echo "GPU_NEGATIVE_PASS"
---
apiVersion: v1
kind: Pod
metadata:
  name: gpu-positive
  namespace: __NAMESPACE__
  labels:
    app: gpu-stack-lab
    control: positive
spec:
__RUNTIME_CLASS_BLOCK__  restartPolicy: Never
  activeDeadlineSeconds: 180
  affinity:
    nodeAffinity:
      requiredDuringSchedulingIgnoredDuringExecution:
        nodeSelectorTerms:
        - matchFields:
          - key: metadata.name
            operator: In
            values:
            - __NODE__
  containers:
  - name: check
    image: __IMAGE__
    imagePullPolicy: IfNotPresent
    command: ["/bin/sh", "-c"]
    args:
    - |
      set -eu
      uuid_count="$(nvidia-smi --query-gpu=uuid --format=csv,noheader | awk 'NF {n++} END {print n+0}')"
      echo "gpu_uuid_count=$uuid_count"
      test "$uuid_count" -eq 1
      /opt/gpu-lab/cuda-smoke
      echo "CUDA_SMOKE_PASS"
    resources:
      limits:
        nvidia.com/gpu: 1
'@

  $rendered = $manifest.
    Replace('__NAMESPACE__', $ns).
    Replace('__NODE__', $approvedNode).
    Replace('__IMAGE__', $gpuImage).
    Replace('__RUNTIME_CLASS_BLOCK__', $runtimeClassBlock)

  $createPodsOutput = $rendered |
    & kubectl --context $approvedContext create -f - 2>&1
  if ($LASTEXITCODE -ne 0) { throw '创建对照Pod失败' }
  if (-not (($createPodsOutput | Out-String).Trim())) {
    throw '创建对照Pod返回空输出'
  }

  $deadline = (Get-Date).AddMinutes(4)
  do {
    $negativePhase = Invoke-KubectlText -KubectlArgs @(
      'get','pod','gpu-negative','-n',$ns,
      '-o','jsonpath={.status.phase}'
    )
    $positivePhase = Invoke-KubectlText -KubectlArgs @(
      'get','pod','gpu-positive','-n',$ns,
      '-o','jsonpath={.status.phase}'
    )
    if (-not $negativePhase -or -not $positivePhase) {
      throw '读取Pod phase返回空值'
    }
    if (
      $negativePhase -in @('Succeeded','Failed') -and
      $positivePhase -in @('Succeeded','Failed')
    ) {
      break
    }
    Start-Sleep -Seconds 3
  } while ((Get-Date) -lt $deadline)

  $podTable = Invoke-KubectlText -KubectlArgs @(
    'get','pod','-n',$ns,'-o','wide'
  )
  if (-not $podTable) { throw 'Pod列表输出为空' }
  Write-Host $podTable

  $negativeNode = Invoke-KubectlText -KubectlArgs @(
    'get','pod','gpu-negative','-n',$ns,
    '-o','jsonpath={.spec.nodeName}'
  )
  $positiveNode = Invoke-KubectlText -KubectlArgs @(
    'get','pod','gpu-positive','-n',$ns,
    '-o','jsonpath={.spec.nodeName}'
  )
  if (
    $negativeNode -cne $approvedNode -or
    $positiveNode -cne $approvedNode
  ) {
    throw "Pod实际落点不符合批准Node：negative=$negativeNode positive=$positiveNode"
  }

  foreach ($name in @('gpu-negative','gpu-positive')) {
    $podStatusText = Invoke-KubectlText -KubectlArgs @(
      'get','pod',$name,'-n',$ns,'-o','json'
    )
    if (-not $podStatusText) { throw "读取$name JSON返回空值" }
    $podStatusObject = $podStatusText | ConvertFrom-Json
    if ($null -ne $podStatusObject.status.extendedResourceClaimStatus) {
      throw "$name出现extendedResourceClaimStatus；请求已走DRA映射，不能验收传统Device Plugin路径"
    }
  }

  $negativeLog = Invoke-KubectlText -KubectlArgs @(
    'logs','gpu-negative','-n',$ns,'-c','check'
  )
  $positiveLog = Invoke-KubectlText -KubectlArgs @(
    'logs','gpu-positive','-n',$ns,'-c','check'
  )
  if (-not $negativeLog -or -not $positiveLog) {
    throw 'kubectl logs成功但返回空内容，不能验收'
  }
  Write-Host $negativeLog
  Write-Host $positiveLog

  foreach ($name in @('gpu-negative','gpu-positive')) {
    $uid = Invoke-KubectlText -KubectlArgs @(
      'get','pod',$name,'-n',$ns,
      '-o','jsonpath={.metadata.uid}'
    )
    if (-not $uid) { throw "读取$name UID返回空值" }
    $events = Invoke-KubectlText -KubectlArgs @(
      'get','events','-n',$ns,
      '--field-selector',"involvedObject.uid=$uid",
      '-o','custom-columns=FIRST:.firstTimestamp,LAST:.lastTimestamp,COUNT:.count,TYPE:.type,REASON:.reason,MESSAGE:.message'
    )
    Write-Host $events
  }

  if ($negativePhase -ne 'Succeeded') {
    throw "negative control phase=$negativePhase；先看完整状态与Event"
  }
  if ($negativeLog -notmatch '(?m)^GPU_NEGATIVE_PASS\r?$') {
    throw 'negative没有同时证明：无GPU编号设备、nvidia-smi失败、固定CUDA smoke失败'
  }
  if ($positivePhase -ne 'Succeeded') {
    throw "positive control phase=$positivePhase；不能宣布GPU栈通过"
  }
  if ($positiveLog -notmatch '(?m)^gpu_uuid_count=1\r?$') {
    throw 'positive可见GPU UUID数量不是恰好1'
  }
  if ($positiveLog -notmatch '(?m)^CUDA_SMOKE_PASS\r?$') {
    throw 'positive缺少固定CUDA kernel smoke的机器可判定PASS行'
  }

  Write-Host 'PASS：两个Pod实际落在批准Node；negative未获GPU；positive仅见1个UUID并完成固定CUDA kernel smoke'
}
finally {
  if ($createdNamespace) {
    $ownerOutput = & kubectl --context $approvedContext get namespace $ns `
      -o jsonpath='{.metadata.labels.studyowner}' 2>$null
    $ownerReadExit = $LASTEXITCODE
    $actualOwner = (($ownerOutput | Out-String).TrimEnd())

    if ($ownerReadExit -ne 0 -or $actualOwner -cne $owner) {
      throw "清理保护失败：无法证明namespace owner；请立即人工核对残留 $ns"
    }

    if ($keepArtifacts) {
      Write-Warning "按显式keepArtifacts保留 $ns；到期时间UTC：$expiresAt"
      Write-Warning "人工清理前先核对：kubectl --context $approvedContext get ns $ns -o jsonpath='{.metadata.labels.studyowner}' 应等于 $owner"
      Write-Warning "核对后执行：kubectl --context $approvedContext delete ns $ns --wait=true --timeout=90s"
    } else {
      $deleteOutput = & kubectl --context $approvedContext delete namespace $ns `
        --wait=true --timeout=90s 2>&1
      if ($LASTEXITCODE -ne 0) {
        throw "namespace删除失败或超时，存在残留风险：$ns"
      }

      $residualOutput = & kubectl --context $approvedContext get namespace $ns `
        --ignore-not-found -o name 2>&1
      if ($LASTEXITCODE -ne 0) {
        throw "无法确认namespace是否残留（API查询失败）：$ns"
      }
      if (($residualOutput | Out-String).Trim()) {
        throw "namespace删除命令返回成功但对象仍存在：$ns"
      }
      Write-Host "CLEANUP_PASS：namespace已确认不存在：$ns"
    }
  }
}
```

### 19.4 这个实验能证明与不能证明的边界

能支持：

- 当前kubectl context与人工批准值精确一致；
- Node已经由平台预授权为传统 Device Plugin、exclusive full-GPU实验池；
- 两个Pod通过scheduler落到同一个、精确批准的Node，且创建后再次核对了`spec.nodeName`；
- 未请求GPU的container既没有`/dev/nvidia[0-9]*`，也无法通过`nvidia-smi`和同一固定CUDA smoke；
- 请求`nvidia.com/gpu: 1`的container恰好枚举1个UUID；
- 固定、已评审的smoke真实launch并同步CUDA kernel，返回机器可判定的`CUDA_SMOKE_PASS`；
- 所有验收用的`kubectl get/logs`均检查了退出码和必要的非空输出；
- 默认会立即清理；显式保留时有owner与20分钟到期证据。

不能支持：

- 原生DRA路径正确；
- MIG、time-slicing、MPS或多租户隔离正确；
- 所有GPU型号都兼容；
- 多卡/NVLink/NCCL健康；
- 长时间负载稳定；
- Driver升级后仍必然正常；
- 推理模型的吞吐、延迟、显存峰值满足SLO。

这些在第 17～21 课逐步补齐。

---

## 20. 安全实验 3：不存在的 CDI name 应在 runtime 边界失败

### 20.1 实验目的

验证：

```text
CDI selector不是一个随便写的字符串
runtime必须能在本地CDI cache/spec中解析它
```

这不是 Kubernetes Pod实验。只在已经安装并批准使用 `podman`或`nerdctl`、且该客户端明确支持 CDI 的实验节点执行。

先记录真实清单：

```bash
nvidia-ctk cdi list
```

再构造一个清单中确定不存在的名字：

```text
nvidia.com/gpu=study-definitely-not-present
```

使用你们批准的本地容器客户端，示意：

```bash
podman run --rm \
  --device 'nvidia.com/gpu=study-definitely-not-present' \
  '__APPROVED_MINIMAL_IMAGE_BY_DIGEST__' \
  true
```

或：

```bash
nerdctl run --rm \
  --device 'nvidia.com/gpu=study-definitely-not-present' \
  '__APPROVED_MINIMAL_IMAGE_BY_DIGEST__' \
  true
```

验收：

```text
exit code != 0
错误明确落在CDI device无法解析/不存在
没有遗留container
没有修改spec
没有重启runtime
```

错误文本随客户端/runtime版本变化，不把某一句英文写成唯一签名；保存完整命令、版本、stderr和exit code。

如果客户端根本不支持该参数，实验结论只能是“客户端能力不满足”，不能当成 CDI解析失败。

### 20.2 为什么不用删除真实 CDI spec 来制造故障

删除或移动 `/var/run/cdi/nvidia.yaml` 会影响同Node其他GPU workload，而且自动refresh可能又把它生成回来，实验不可控。

不存在的selector：

- 不改配置；
- 不碰真实device；
- 不重启daemon；
- 失败范围只在新建的测试container；
- 更符合生产安全。

---

## 21. 故障推演 A：host `nvidia-smi` 就失败

### 21.1 分层

```text
lspci无设备
  -> 硬件/云平台/虚拟化

lspci有设备，但nvidia module未加载
  -> kernel headers、module build/signing、冲突driver、版本/安装方式

module加载，但nvidia-smi找不到NVML
  -> 用户态library/package/loader路径

nvidia-smi报告driver/library version mismatch
  -> kernel module与用户态driver组件漂移

GPU fell off bus / Xid
  -> PCIe、电源、硬件、driver、平台健康事件
```

### 21.2 NVIDIA SMI return code是线索，不是完整根因

当前官方文档列出的部分返回码包括：

```text
9   NVIDIA driver未加载
12  NVML shared library无法找到或加载
15  GPU已从总线掉线或不可访问
255  其他内部错误
```

自动化可记录return code，但最终仍要关联：

```bash
journalctl -k --since '-2 hours'
dmesg -T
journalctl -u nvidia-persistenced --since '-2 hours'
```

生产中内核日志可能含硬件序列、拓扑和内部信息，外发前脱敏。

---

## 22. 故障推演 B：host 正常，CDI list为空或过期

调用链：

```text
host Driver/NVML可枚举
  -> Toolkit发现device
  -> 生成CDI spec
  -> runtime加载spec
```

逐项核对：

1. `nvidia-ctk --version`；
2. `nvidia-ctk cdi generate`（省略`--output`，由stdout预览）是否能生成预期内容；
3. `nvidia-ctk cdi list`；
4. `/etc/cdi`、`/var/run/cdi`真实文件、mtime、owner、mode；
5. `nvidia-cdi-refresh.path/service`状态与日志；
6. 最近是否卸载/re装Driver、切换MIG、改变device topology；
7. containerd是否加载目标spec目录；
8. runtime cache是否观察到刷新。

不能因为 `/var/run/cdi/nvidia.yaml` 文件存在就宣布正常：

- YAML可能无目标device；
- name与上游分配值不一致；
- hostPath已失效；
- runtime没有扫描这个目录；
- spec生成时间早于最近Driver/MIG变化；
- spec语法版本超出runtime支持范围。

---

## 23. 故障推演 C：`RuntimeClass not found` 与 `unknown runtime handler`

区分两层：

```text
API admission/RuntimeClass lookup失败
  -> 集群里没有对应RuntimeClass对象

RuntimeClass对象存在，handler=nvidia
但目标Node containerd没有这个handler
  -> RunPodSandbox在CRI/runtime边界失败
```

取证：

```bash
kubectl get runtimeclass
kubectl get runtimeclass '__NAME__' -o yaml
```

目标Node：

```bash
systemctl show containerd -p ExecStart -p FragmentPath -p DropInPaths
ps -ef | grep '[c]ontainerd'
# 确认实际binary与--config后，再用相同输入执行config dump
ctr plugins ls | grep -E 'cri|runtime|images'
crictl info
systemctl cat containerd
journalctl -u containerd --since '-30 minutes'
journalctl -u kubelet --since '-30 minutes'
```

修复前先问：

```text
这个workload按当前部署模式真的需要named NVIDIA runtime吗？
还是应该删除从旧模板继承的runtimeClassName并走默认handler + CDI？
```

不要为了让一个旧YAML跑起来，未经评审把 `nvidia`设为所有容器的默认runtime。

---

## 24. 故障推演 D：`unknown CDI device`

硬证据链：

```text
Pod UID/container attempt
  -> kubelet日志/CRI CreateContainer错误
  -> 请求的完全限定CDI name
  -> 目标Node nvidia-ctk cdi list
  -> 目标Nodespec内容/mtime
  -> runtime版本和CDI目录
```

最常见方向：

- Device Plugin/上游策略发出UUID，而spec只生成index，或反过来；
- Driver/MIG变化后spec未刷新；
- `/var/run/cdi`被临时文件系统清空但refresh未恢复；
- containerd版本/配置未启用或未扫描正确目录；
- 节点镜像不一致，只有部分GPU Node缺配置；
- kubelet/Device Plugin账本恢复值与当前物理device集合漂移。

不能做的跳跃：

```text
unknown CDI device
  -> 直接结论“GPU坏了”
```

它首先是一个**名字到OCI edits无法解析**的问题。

---

## 25. 故障推演 E：容器内 `nvidia-smi` 成功，CUDA应用失败

### 25.1 `CUDA driver version is insufficient`

核对：

- 宿主机实际Driver；
- 容器实际CUDA runtime/library；
- framework构建版本；
- 官方最低Driver要求；
- 是否误装载镜像内/compat目录中的另一份 `libcuda.so`；
- `LD_LIBRARY_PATH`和loader实际选择。

不要只看镜像名中的 `cuda12.x`。

### 25.2 `libcudart.so` / cuDNN / TensorRT找不到

更偏镜像依赖：

```text
Driver注入解决的是host Driver能力
不自动为任意应用镜像补齐所有CUDA开发/运行库
```

用受控方式检查：

```bash
ldconfig -p | grep -E 'libcuda|libcudart|libnvidia-ml'
```

对具体二进制：

```bash
ldd /path/to/application
```

注意不要把 `ldd` 用在不可信二进制上；生产安全流程可改用 `readelf -d`等静态手段。

### 25.3 `no kernel image is available for execution`

常见方向是应用binary没有为当前GPU compute capability构建合适SASS/PTX，而不是 Device Plugin分配失败。

### 25.4 OOM

区分：

```text
Linux cgroup memory OOM
CUDA out of memory / GPU显存不足
模型框架allocator碎片/缓存
GPU共享策略造成的争用
```

`kubectl describe pod`中的 `OOMKilled` 不能覆盖所有GPU显存OOM；应用日志和第 19～20 课的GPU指标也要关联。

---

## 26. Driver/Toolkit/containerd 升级必须当节点变更

### 26.1 标准变化顺序

```text
确认兼容矩阵和回退包
  -> 选择canary GPU Node
  -> cordon
  -> 按PDB/任务语义安全drain或迁移GPU workload
  -> 保存基线：kernel/driver/toolkit/containerd/CDI/allocatable
  -> 变更Driver/kernel（如需要）
  -> reboot并验证host
  -> 变更Toolkit/containerd集成
  -> 验证CDI/runtime
  -> Device Plugin重新上报
  -> negative/positive/CUDA smoke
  -> 监控观察窗
  -> uncordon
  -> 小批量扩展
```

GPU训练Job、在线推理Deployment、daemon型监控组件的迁移语义不同：

- 无checkpoint训练任务可能不能随便evict；
- 推理服务要先确认剩余副本、PDB和流量摘除；
- Device Plugin/DCGM等DaemonSet会随Node生命周期恢复；
- MIG/NVSwitch节点还可能有额外平台依赖。

### 26.2 回退不是只降一个RPM

回退包至少关联：

| 层 | 要记录 |
|---|---|
| kernel | 旧kernel是否仍可启动、boot entry |
| Driver | branch、package集合、module flavor、签名 |
| userspace | `libcuda`/NVML实际版本 |
| Toolkit | package版本、配置格式 |
| containerd | version、config version、drop-in |
| CDI | spec版本、生成方式、refresh unit |
| K8s | Device Plugin/Operator版本和策略 |

如果同binary/同配置输入的解析结果、plugin运行状态、Driver包和节点镜像都没有基线，事后很难区分是升级引入还是原有漂移。

### 26.3 禁止把这些命令放进普通巡检

```text
systemctl restart containerd
systemctl restart kubelet
modprobe -r nvidia*
nvidia-smi --gpu-reset
nvidia-smi -mig ...
删除 /var/run/cdi/nvidia.yaml
修改默认runtime
直接执行 nvidia-ctk runtime configure
```

它们不是“多看一眼”，而是变更动作。

---

## 27. 安全与多租户边界

### 27.1 CDI spec目录是信任边界

runtime会按spec把host device、mount、env、hook等加入容器。因此：

- spec目录必须由受信任组件写；
- owner/mode不能允许普通租户篡改；
- refresh service身份和产物要审计；
- 不把任意租户输入直接拼成hostPath/hook；
- 节点镜像与配置需要完整性管理。

### 27.2 不请求GPU的Pod不应意外获得所有GPU

若默认runtime/hook配置把全部GPU暴露给任意Pod，会造成：

- 绕过scheduler资源账本；
- 多租户隔离失效；
- 利用率/成本归属错误；
- 一个容器可以干扰其他GPU workload；
- PodResources与实际可见设备不一致。

这就是第 19 课对照实验必须有negative control的原因。

### 27.3 `nvidia-smi` 有查询也有变更能力

同一个工具包含reset、compute mode、MIG、clock、power等管理动作。RBAC只能控制Kubernetes API，不能自动限制容器内对device node的每个ioctl。

因此还要考虑：

- 容器securityContext；
- privileged/hostPID/hostPath；
- 注入哪些 driver capability；
- device cgroup/runtime策略；
- Node访问权限；
- GPU硬件与driver支持的隔离能力。

MIG和time-slicing不是同等强度的隔离方案，第 21 课单独讲。

---

## 28. 这章哪些要学深，哪些一笔带过

### 必须运维学深（O3）

- 六层责任栈和证据强弱；
- Driver/kernel/device node/NVML边界；
- host Toolkit通常不是应用CUDA依赖仓库；
- `nvidia-smi`能与不能证明什么；
- CUDA Driver与镜像runtime兼容模型；
- Toolkit legacy hook与runtime原生CDI路径；以及NRI主要面向绕过Kubernetes GPU分配的管理容器这一边界；
- containerd配置输入、解析结果、运行证据与1.x/2.x差异；
- RuntimeClass与CDI的不同职责；
- 安全取证、canary升级、回退；
- 容器内真实CUDA smoke。

### 只读边界（S0）

- NVIDIA Driver内部源码；
- CUDA kernel实现；
- `libnvidia-container`完整代码；
- containerd CDI cache内部实现；
- OCI runtime全部namespace/cgroup源码。

你只需能从错误定位到这些组件的责任边界，必要时再按真实事故窄读。

### 留到后章

- Device Plugin注册、ListAndWatch：第 15 课 S3；
- DeviceManager device选择与Allocate：第 16 课 S3；
- checkpoint、PodResources、健康变化/CDI账本：第 17 课 S3；
- Operator自动管理这些组件：第 18 课；
- Xid/ECC/DCGM：第 19 课；
- vLLM应用SLO：第 20 课；
- MIG/time-slicing/队列/成本：第 21 课。

---

## 29. 生产排障口述模板

面试或事故会上，不要说：

> GPU不可用，我先重装驱动。

应该说：

```text
1. 先用Pod UID、nodeName和Event确认故障在调度前、Sandbox、CreateContainer还是应用运行期。
2. 在同一个目标Node验证PCI -> module/device node -> host NVML。
3. 再验证Toolkit版本、CDI清单、refresh；对containerd同时核对配置输入、解析结果、plugin/CRI运行证据。
4. 对照CRI/kubelet错误里的runtime handler或CDI name，确认runtime能否解析。
5. Node扩展资源异常时转到Device Plugin/ListAndWatch账本；容器已注入但应用失败时转到CUDA/framework兼容。
6. 修复走cordon/drain/canary/真实CUDA smoke/观察窗/回退，不在线盲改runtime。
```

这套表达能把“会跑命令”提升为“能划分责任并控制变更风险”。

---

## 30. 本章自测

### 30.1 必须能不看答案解释

1. 宿主机 `nvidia-smi` 成功，能证明哪几层？不能证明哪几层？
2. `nvidia-smi`显示的 `CUDA Version` 为什么不是宿主机Toolkit版本？
3. `libcuda.so`、`libnvidia-ml.so`、`libcudart.so`分别负责什么？
4. 只运行容器化CUDA应用的Node，为什么通常不需要完整host CUDA Toolkit？
5. containerd 1.x与2.x配置为什么不能互抄？
6. 为什么`containerd config dump`只能证明“指定输入如何被解析”，不能单独证明daemon与当前Pod实际使用了该路径？
7. CDI spec、CDI fully qualified name和真实GPU是什么关系？
8. RuntimeClass与CDIDevice分别进入哪条链？
9. `unknown CDI device`为什么不应先归因成scheduler故障？
10. 容器内 `nvidia-smi`成功，为什么还要跑 `deviceQuery`或真实framework smoke？
11. 为什么不能通过删除真实CDI spec制造实验故障？
12. Driver/Toolkit升级前为什么要cordon/drain/canary？

### 30.2 现场题

给你以下证据：

```text
Node Ready=True
Node allocatable nvidia.com/gpu=4
Pod Scheduled=True
FailedCreatePodSandBox:
  no runtime for "nvidia" is configured
host nvidia-smi -L 成功
```

回答：

- 失败发生在Sandbox还是业务container？
- `nvidia.com/gpu=4`能否证明named handler存在？
- 是修RuntimeClass模板、还是修containerd handler，要先核对什么？
- 为什么此时还没必要检查应用的`libcudart.so`？

再给一组：

```text
PodSandbox已Ready
CreateContainer失败：
  unresolvable CDI device nvidia.com/gpu=GPU-abc
host nvidia-smi -L 有GPU-abc
nvidia-ctk cdi list没有GPU-abc
```

回答：

- 哪两本账发生了漂移？
- 下一步应查refresh、MIG/Driver变更还是scheduler score？
- 为什么host NVML成功不能替代runtime CDI解析？

### 30.3 通过标准

你能：

- 画出六层责任栈；
- 对任一失败指出首个坏层和相邻层证据；
- 不把`nvidia-smi`当最终CUDA验收；
- 不把RuntimeClass、CDI、Device Plugin混成一个组件；
- 设计不重启runtime、不删除spec的安全实验；
- 说清变更窗口和回退；

才算通过第 14 课。暂时没有GPU环境时，可以先通过口述与源码边界题；实验状态必须明确记为“未执行”，不能写假PASS。

---

## 31. 官方资料与版本校准

> 本节链接于 2026-07-13核对。`latest`页面会随版本更新；生产变更必须改查你们锁定版本的文档与release notes。

### NVIDIA Driver与CUDA

- [NVIDIA Driver Installation Guide](https://docs.nvidia.com/datacenter/tesla/driver-installation-guide/latest/): kernel headers、安装方法、module与发行版支持。
- [Kernel Modules](https://docs.nvidia.com/datacenter/tesla/driver-installation-guide/kernel-modules.html): open/proprietary module及当前支持说明。
- [Software Deployment Workflow](https://docs.nvidia.com/datacenter/tesla/drivers/latest/software-deployment-workflow.html): Toolkit、CUDA user-mode driver、kernel-mode driver三层；运行节点通常无需完整Toolkit。
- [CUDA Compatibility](https://docs.nvidia.com/deploy/cuda-compatibility/latest/): backward、minor version、forward compatibility边界。
- [NVIDIA SMI](https://docs.nvidia.com/deploy/nvidia-smi/): NVML关系、字段语义、return code；`CUDA Version`是Driver支持上限。

### NVIDIA Container Toolkit与CDI

- [Container Toolkit Architecture Overview](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/arch-overview.html): Toolkit组件与legacy runtime/hook架构。
- [Installing NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html): 当前安装与runtime configure方法；其中配置命令属于变更动作。
- [Container Toolkit CDI Support](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/cdi-support.html): spec生成、`nvidia-cdi-refresh`、list/debug及已知刷新边界。
- [GPU Operator CDI](https://docs.nvidia.com/datacenter/cloud-native/gpu-operator/latest/cdi.html): Operator当前CDI模式、RuntimeClass适用边界。
- [CNCF CDI Specification](https://github.com/cncf-tags/container-device-interface/blob/main/SPEC.md): fully qualified device与OCI edits标准。

### containerd

- [containerd CRI Configuration](https://github.com/containerd/containerd/blob/main/docs/cri/config.md): containerd 1.x/2.x配置version和CRI plugin ID差异。

---

## 32. 一页收口

```text
GPU物理/透传
  -> kernel module
  -> /dev/nvidia*
  -> Driver用户态：libcuda/NVML
  -> 镜像CUDA runtime/framework
  -> NVIDIA Container Toolkit
  -> 标准workload：legacy hook或runtime原生CDI
  -> 特定GPU管理容器：可能另走NRI
  -> containerd CRI / OCI runtime
  -> 容器真正执行CUDA
```

五个必须记住的否定句：

```text
Node Ready != GPU Ready
host nvidia-smi成功 != 容器注入成功
容器nvidia-smi成功 != CUDA业务成功
CDI spec存在 != nvidia.com/gpu已上报
runtimeClassName=nvidia != GPU资源已分配
```

下一课从本章的这个断点继续：

```text
宿主机GPU栈已经可用
  -> NVIDIA Device Plugin进程怎样被kubelet发现
  -> Register/GetInfo/PluginConnected
  -> ListAndWatch上报opaque device ID与Healthy
  -> DeviceManager如何形成Capacity/Allocatable
  -> Node status为什么出现或失去nvidia.com/gpu
```

这会第一次进入 GPU 专项的 Kubernetes S3源码深读。
