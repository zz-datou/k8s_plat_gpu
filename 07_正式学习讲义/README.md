# 源码专项主线与生产速查

这不是 Kubernetes 入门课。

```text
你的 Kubernetes 生产运维经验：直接复用
你的 Go / Kubernetes 源码能力：从生产现象开始系统建立
你的 GPU 平台能力：沿 scheduler、kubelet、NVIDIA 节点栈、Device Plugin 逐步接入
```

## 现在从哪里开始

第一次可以花 3 分钟看：

- [课程使用约定](00_怎么使用这套讲义.md)

然后直接进入真正的第一篇：

- [第 07 课：Java 应用滚动发布停在“3 个旧 Pod + 1 个新 Pod”——Deployment 在等什么](07_源码热身_Deployment_rollout卡住如何沿控制器找原因.md)

不要求先按 `01~06` 重学基础。

## 企业实战案例库

07～21 负责讲机制和源码；[企业实战案例库](案例库/README.md)负责把历史运维问题转成证据链、变更门禁、验收和回滚。

案例按来源明确分级：

- `A`：真实故障抽象，保留故障模式并脱敏全部公司标识；
- `B`：多次相近事件合成，训练完整故障树；
- `C`：GPU 迁移演练，明确不是公司已经发生过的事故。

当前案例覆盖多云 EKS/ACK 迁移、Argo CD 字段所有权、Ingress/MSE/ALB、404/405/413/503/504/CORS、CI BuildKit DNS、证据保全，以及 GPU Pending、CDI、Xid、vLLM 和 time-slicing 演练。它是旁路练习，不新增第 22 课，也不改变冻结主线。

## 独立专题讲义（不改变冻结主线）

以下专题用于把多个正式课程一次性串成完整生产因果链，不新增第 22 课，不改变 07～21 的冻结顺序，也不自动推进个人学习进度。

- [Kubernetes Scheduler 全景：从生产实战到业务平台、GPU 与源码](专题/01_Kubernetes_Scheduler从生产实战到业务平台_GPU与源码全景_新手独立讲义.md)
  - 生产问题：从一个 `Pending` Pod 出发，贯通 Filter/Score、Assume/Bind、失败重排、抢占、平台治理、GPU 设备供给、Kueue/Volcano 与生产排障。
  - 必要前置：认识 Pod、Node、Deployment、resources request、label/affinity 和 taint/toleration；不要求先掌握 Go。
  - 插入理由：作为面向新手的独立全景复盘，横向串联第 08～10 课 scheduler 主线和第 15～21 课 GPU 平台主线，适合值班前查阅或学完分章后回看。
  - 验证边界：固定 Kubernetes commit `301946d15e67a4a2e8a5fb8292eb836acd366d78`；静态源码与教学审校已完成，Go 单测受本机工具链版本阻塞，真实集群/GPU 实验未执行。

## 唯一大纲与冻结规则

```text
课程版本：v1.0
冻结日期：2026-07-12
唯一课程顺序来源：本 README
```

从现在起：

- 本文件只负责定义课程编号和唯一学习顺序；
- `06_接下来12周学习计划.md` 只把课程映射到周次，不能另定义一套先后关系；
- [`../PROGRESS.md`](../PROGRESS.md) 只记录当前课、已讲断点和下一课，不维护第二份未来课表；
- 不再静默调整顺序。只有目标岗位、实验环境或必要前置发生实质变化时才允许修改，并且必须先在这里登记原因，再同步其他导航。

### v1.0 冻结时的唯一顺序修正

旧稿曾把 Device Plugin 放在 NVIDIA Driver/CUDA/Container Toolkit 前面，而 12 周计划采用相反顺序。现统一为：

```text
scheduler
  -> kubelet / CRI
  -> NVIDIA 节点栈
  -> Device Plugin / DeviceManager
  -> GPU Operator / DCGM / vLLM / 多租户
```

原因：先知道宿主机怎样识别 GPU、容器怎样获得设备，再读 GPU 怎样上报和分配给 Kubernetes，调用链才不会变成纯名词记忆。

## 真正必修的源码与 GPU 主线

| 编号 | 课程 | 深度 | 状态 |
|---:|---|---|---|
| 07 | [Java 发布停在“3 个旧 Pod + 1 个新 Pod”：Deployment 控制循环与安全边界](07_源码热身_Deployment_rollout卡住如何沿控制器找原因.md) | S2 | 已按设计思想重构；固定提交源码、教学与运维终审通过，Go 测试受本机工具链版本阻塞，待个人自测 |
| 08 | [scheduler：Java Pod 为什么宁愿 Pending——Filter/Score、Assume/Bind](08_scheduler源码主线_Pending到ScheduleOne与Filter_Score.md) | S3 | 已按设计思想重构，源码与教学已校验 |
| 09 | [scheduler：NodeResourcesFit 与 Java Pod CPU/memory request 计算](09_NodeResourcesFit_JavaPod_request计算与Insufficient资源.md) | S3 | 已按设计思想重构，源码与教学已校验 |
| 10 | [scheduler：Java Pod Unschedulable、重入队、抢占与稀缺资源](10_scheduler_JavaPod_Unschedulable重入队与抢占.md) | 队列 S3 / 抢占 S3（二遍进阶） | 已按设计思想重构，静态源码与教学已校验 |
| 11 | [kubelet：Java Pod 已绑定却 FailedMount——持久交接、podWorkers 与 SyncPod](11_kubelet_JavaPod已绑定到syncLoop_podWorkers与SyncPod.md) | S3 | 已按设计思想重构，静态源码与教学已校验 |
| 12 | [kubelet：Java Pod 从 runtime manager 到 PodSandbox、CRI 与容器启动](12_kubelet_JavaPod从runtimeManager到PodSandbox_CRI与容器启动.md) | S3 | 已按设计思想重构；静态源码、教学与运维终审通过，Go 测试受本机工具链版本阻塞，待个人自测 |
| 13 | [kubelet：Java Pod Running 不 Ready 与重启——probe、statusManager、PLEG](13_kubelet_JavaPod_Running不Ready与重启_probe_statusManager_PLEG.md) | S2；liveness/PLEG窄链 S3 | 已按设计思想重构；静态源码、教学与运维终审通过，Go 测试受本机工具链版本阻塞，待个人自测 |
| 14 | [NVIDIA 节点栈已就绪，为什么容器仍 CreateContainerError——Driver、Toolkit、containerd 与 CDI 分账](14_NVIDIA节点栈_Driver_CUDA_ContainerToolkit_containerd_CDI.md) | 运维 S3 / 源码 S1 | 已按设计思想重构；技术、源码与运维终审通过，Go 测试受本机工具链版本阻塞，待个人自测 |
| 15 | [Device Plugin 已注册，为什么 Java 推理 Pod 仍 FailedScheduling——ListAndWatch、Capacity/Allocatable](15_DevicePlugin_注册_ListAndWatch_Capacity_Allocatable.md) | 主链 S3 / generic 注册 S2 | 已按设计思想重构；静态源码、教学与运维终审通过，Go 测试受本机工具链版本阻塞，待个人自测 |
| 16 | [DeviceManager：device ID、Allocate 与容器注入](16_DeviceManager_deviceID_Allocate与容器注入.md) | S3 | 正文与源码/实验安全终审完成，待自测 |
| 17 | [checkpoint、健康变化、PodResources 与 CDI](17_checkpoint_健康状态_PodResources与CDI恢复账本.md) | S3 | 正文与源码/实验安全终审完成，待自测 |
| 18 | [GPU Operator：组件、安装、升级与故障定位](18_GPU_Operator_组件安装升级与故障定位.md) | S1 | 正文与技术/实验安全终审完成，待自测 |
| 19 | [DCGM：指标、告警、Xid/ECC 与 GPU 健康](19_DCGM_指标告警_Xid_ECC与GPU健康.md) | S1 | 正文与技术/实验安全终审完成，待自测 |
| 20 | [vLLM：模型加载、显存、探针、吞吐/延迟与 SLO](20_vLLM_模型加载_显存_探针_吞吐延迟与SLO.md) | S1 | 正文与技术/实验安全终审完成，待自测 |
| 21 | [MIG、time-slicing、队列、多租户、配额与成本](21_MIG_time-slicing_队列_多租户_配额与成本.md) | S1；二开时 S2 | 正文与技术/实验安全终审完成，待自测 |

07～21 已按冻结顺序形成正式讲义并完成终审。这里的“正文完成”只表示课程材料建设完成，不表示个人已经阅读、自测通过，也不表示 GPU 实验已经执行。

## 00~06 是生产速查，不是前置课

这些内容保留，因为以后排障和 GPU 场景仍会用到；但你已经熟悉的部分可以跳过。

| 文件 | 用途 | 什么时候回看 |
|---|---|---|
| [01 平台 K8s 总图](01_平台K8s总图_一次发布到接流量.md) | 一次发布到流量的全链路图 | 组件边界混淆时 |
| [02 Deployment、RS、Pod](02_Deployment_ReplicaSet_Pod_发布与副本.md) | RollingUpdate 参数与实验 | 需要重做 rollout 故障实验时 |
| [03 Pod 状态与探针](03_Pod状态与探针_Running为什么不等于Ready.md) | probe 与状态排障手册 | readiness/liveness 现场 |
| [04 Service 与 EndpointSlice](04_Service与EndpointSlice_流量怎么找到Pod.md) | Service 流量排障手册 | 服务无后端、转发异常时 |
| [05 资源与弹性](05_资源与弹性_requests_limits_QoS_HPA.md) | request、HPA、quota、GPU 扩展资源前置 | scheduler/GPU 资源课前选择性读 |
| [06 节点维护](06_节点维护与排障_cordon_drain_PDB_NodeReady.md) | cordon/drain/PDB/Node 状态手册 | GPU 节点升级维护时 |

快速跳过标准见 [课程使用约定](00_怎么使用这套讲义.md)。

## 源码深度定义

| 深度 | 学到什么程度 |
|---|---|
| S0 | 不读源码，生产配置、兼容、验证和故障处理学深 |
| S1 | 理解职责边界，认识关键入口 |
| S2 | 读一条精选调用链，能解释关键状态和判断 |
| S3 | 读穿核心状态流、失败流和重试流，能从生产证据反查函数 |

源码不是平均用力：

```text
scheduler、kubelet、Device Plugin/DeviceManager：S3
Deployment：只用 S2 训练源码阅读方法
通用 apiserver、kube-proxy、CSI、鉴权：按故障需要 S1~S2
NVIDIA Driver/CUDA：运维学深，不读实现源码
```

## 每篇源码课的固定方法

```text
Java 平台生产现象（08～13 的主案例）
  -> kubectl / status / Event / 日志
  -> 源码入口
  -> 关键调用链
  -> 决定行为的 if / 公式
  -> Go 语法现场补
  -> 回到 Java 应用排障
  -> 文末做一次简短 GPU 映射
```

### 现阶段案例主次已经确定

现阶段真正要掌握的是 Kubernetes 底层源码怎样运行，Java 应用只是最熟悉、最容易核对的生产入口；GPU 不是 08～13 的第二套并行课程。

| 课程 | 主案例 | GPU 处理方式 |
|---|---|---|
| 07～13 | Java 平台发布、Pending、ContainerCreating、探针和节点故障 | 每章只留一个短映射，不重复完整调用链 |
| 14 | NVIDIA Driver、Toolkit、containerd/CDI | 从这里开始自然翻转为 GPU 主案例 |
| 15～21 | Device Plugin、DeviceManager、Operator、DCGM、vLLM、多租户 | GPU 主案例；Java 平台经验只用来类比发布、监控和 SLO |

08～13 的篇幅优先级：

```text
Kubernetes 源码运行逻辑
  > Java 平台现场与证据
  > GPU 简短迁移
```

不为了增加 GPU 含量而复制一套完整 GPU 故障；只说明“同一源码机制迁移到 GPU 后，多了哪个资源或设备边界”。

每篇必须包含：

1. 本地源码 commit 和目标版本提醒。
2. 精确文件、函数、当前行号和关键源码块。
3. 每个关键判断对应的生产证据。
4. 本篇明确不展开的低收益细节。
5. 可复现观察或故障实验。
6. 验收题；08～13 只附一个简短 GPU 迁移小节。
7. Go 语法必须在第一次影响理解时现场解释；只在人话翻译仍不足时给最小例子。

### Go 不熟时，固定怎样讲

每个源码片段最多按四步推进：

```text
1. 源码摘录：只取当前真正要读的 5~15 行
2. 行为翻译：读取什么、判断什么、修改什么、返回什么
3. Go 现场补：只解释 1~2 个会挡住当前理解的写法
4. 必要时的最小例子：单靠解释仍不直观时，再用脱离 Kubernetes 的小代码演示
```

最小例子可以省略 `package main` 和 `import`，但必须明确它是阅读片段；需要验证行为时，再提供可直接运行的完整程序。

必须现场解释的典型写法：

```text
:= 与 =
多返回值、err 和 nil
指针的 * / &
slice、append、for/range 和 _
结构体字段初始化
receiver、early return、defer
```

不会先要求学完整 Go 课程。interface 内部机制、goroutine、channel、反射、泛型和内存模型，只在后面的 scheduler、kubelet 或 DeviceManager 源码真正需要时再补。

## 旧资料怎样使用

`study/90_主线复盘与进阶/01~1267` 不再是你的阅读清单。

它的定位是：

```text
我编写正式课时的素材库
真实故障需要某个分支时的深挖索引
以后做控制器二开时的专题资料
```

正式正文必须自洽，不能把“再读十篇旧文”当作前置任务。

## 当前断点

```text
课程材料建设断点：
07～21 已按 v1.0 冻结大纲全部完成正文与终审

个人掌握断点：
仍以实际阅读、自测和实验记录为准
资料写完不会自动把个人进度推进到第21课

下一步学习：
  -> 第一次进入或重新梳理主线：从第07课开始
  -> 已经完成某课自测：按 ../PROGRESS.md 的真实记录续学
  -> 第14～21课的GPU实验必须在真实环境执行后单独登记

大纲边界：
当前冻结大纲到第21课收束，不静默新增第22课
后续若增加专题，必须先说明生产问题、必要前置和插入理由
```
