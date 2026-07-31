# SRE平台与GPU运维双路线

这个目录专门整理两条路线：

```text
当前主线：SRE平台 / Kubernetes运维 / Java业务容器平台
转型主线：GPU运维 / Kubernetes GPU调度 / AI推理与算力平台
```

核心判断：

```text
你不是从零转行。
你是在已有 Kubernetes 运维能力上，把工作负载从 Java Web / SDK 服务，扩展到 GPU 推理 / 训练 / 算力资源平台。
```

当前执行边界：

```text
第一阶段主线：平台 Kubernetes + GPU 基础设施 + 在线推理平台 SRE
第二阶段再扩展：训练队列、NCCL/RDMA、多机多卡与 HPC
```

你目前只读过少量源码讲义。原工作区中的 `01~1267` 旧资料库不随本仓库发布，也不是待打卡课程；本仓库的源码学习只按 `04` 的深度矩阵和正式讲义顺序推进。

两条路线共同底座都是 Kubernetes，但看问题的角度不同：

- Java业务SRE：关注应用发布、稳定性、流量、配置、容量、故障恢复。
- GPU运维：关注算力资源、设备插件、GPU调度、模型服务、显存、利用率、成本。

## 现在从这里开始

直接进入：

- [正式学习讲义入口](07_正式学习讲义/README.md)
- [企业实战案例库](07_正式学习讲义/案例库/README.md)
- [当前学习进度](PROGRESS.md)

这是唯一学习入口。它默认你已有多年 K8s 运维经验，重点把熟悉的生产现象映射到源码，再逐步进入 GPU 专项；你不需要自己去 `90_主线复盘与进阶` 拼阅读顺序。

第一次这样开始：

1. 可用 3 分钟看 `07_正式学习讲义/00_怎么使用这套讲义.md`。
2. 直接学习 `07_正式学习讲义/07_源码热身_Deployment_rollout卡住如何沿控制器找原因.md`。
3. `01~06` 是生产速查，已经熟悉的内容全部跳过。

后续只按正式讲义 README 已冻结的 scheduler → kubelet/CRI → NVIDIA 节点栈 → Device Plugin/DeviceManager → GPU 平台主线继续。

## 仓库边界

本仓库只保存课程、路线说明、学习进度和维护规则，不复制完整的 Kubernetes 上游源码。

源码课当前主要以 `kubernetes/kubernetes` commit `301946d15e67a4a2e8a5fb8292eb836acd366d78` 为固定阅读基线；实际用于生产版本排障时，必须重新核对目标集群对应版本。源码引用和第三方项目说明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

课程里的业务名、Pod IP、节点名和故障现场均按教学案例理解，不应直接当作生产配置使用。

## 本目录其他 01~06 是什么

它们是课程设计和职业路线说明，不是另一套学习顺序：

| 文件 | 用途 | 现在是否必读 |
|---|---|---|
| [01 总览：两条路线的关系](01_总览_两条路线的关系.md) | 解释平台 SRE 和 GPU 运维怎样衔接 | 可选 |
| [02 路线 A：Java 游戏业务 K8s/SRE 平台运维](02_路线A_Java游戏业务K8s_SRE平台运维.md) | 当前岗位能力地图 | 按需 |
| [03 路线 B：GPU 算力平台运维](03_路线B_GPU算力平台运维.md) | GPU 方向能力地图 | 按需 |
| [04 K8s 交叉能力矩阵](04_K8s交叉能力矩阵_哪些深入哪些一笔带过.md) | 控制源码学习深度 | 我备课时使用，你可查 |
| [05 企业真实场景](05_企业真实场景_实际怎么用.md) | 企业场景与责任边界 | 按需 |
| [06 接下来 12 周学习计划](06_接下来12周学习计划.md) | 周期和阶段产出 | 每阶段开始时看 |
| [07 正式学习讲义](07_正式学习讲义/README.md) | 一课一课真正学习 | **当前必读** |

## 你要形成的最终能力

### 已有岗位经验升级

- 把 Java / SDK / 官网类容器应用的多年运维经验沉淀为标准模板、SOP、告警和变更护栏。
- 能从 Deployment/Pod/Node 的生产证据继续定位到 controller、scheduler、kubelet 等源码判断。
- 不只会恢复故障，还能解释机制、识别平台共性问题，并推动自动化和治理改进。

### GPU方向转型

- 能搭建和维护 GPU Kubernetes 节点池。
- 能解释 `nvidia.com/gpu` 从声明、调度、分配到容器可见的完整链路。
- 能排查 GPU Pod Pending、GPU不可见、device plugin异常、driver/runtime问题、GPU OOM、利用率低。
- 第一阶段能部署和运维 vLLM；后续再按岗位需要扩展 Triton / KServe。
- 能做 GPU 资源治理：队列、配额、租户隔离、共享、成本优化。

## 官方资料入口

- Kubernetes Device Plugins: <https://kubernetes.io/docs/concepts/extend-kubernetes/compute-storage-net/device-plugins/>
- Kubernetes Resource Management: <https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/>
- Kubernetes Probes: <https://kubernetes.io/docs/concepts/workloads/pods/probes/>
- Kubernetes HPA: <https://kubernetes.io/docs/concepts/workloads/autoscaling/horizontal-pod-autoscale/>
- NVIDIA GPU Operator: <https://docs.nvidia.com/datacenter/cloud-native/gpu-operator/latest/getting-started.html>
- NVIDIA DCGM Exporter: <https://docs.nvidia.com/datacenter/dcgm/latest/gpu-telemetry/dcgm-exporter.html>
- Kueue: <https://kueue.sigs.k8s.io/>

## 非官方声明

本仓库是个人独立学习项目，并非 Kubernetes、CNCF、NVIDIA、vLLM 或 Kueue 官方教程。项目名称和商标仅用于说明技术对象与源码来源，相关权利归各自权利人所有。
