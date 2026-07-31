# GPU 平台迁移演练

本篇包含 C01～C05。它们是依据现有 Java / Kubernetes 平台故障机制设计的迁移演练，不是公司已经发生过的 GPU 事故。

## 1. 先建立版本与证据账本

进入任何 GPU 实验前先填：

| 字段 | 示例写法 |
|---|---|
| 源码学习基线 | Kubernetes commit / tag |
| 实验运行基线 | Kubernetes、OS、kernel、containerd |
| GPU 栈 | GPU SKU、Driver、Toolkit、Device Plugin / Operator |
| 工作负载 | image digest、CUDA、framework、model revision |
| 实验状态 | `STATIC_REVIEWED` / `GPU_LAB_EXECUTED` 等 |
| 最后验证日期 | ISO 8601 时间 |

源码课使用的新版本或 alpha commit 只是一份学习快照，不等于当前生产集群版本，也不等于 GPU Operator 的支持矩阵。

## 2. C01：GPU Pod 一直 Pending

### 2.1 现场

Pod 声明：

~~~yaml
resources:
  limits:
    nvidia.com/gpu: 1
~~~

所有 GPU Node 都是 `Ready=True`，Device Plugin DaemonSet 也显示 Running，但 Pod Event 持续：

~~~text
0/N nodes are available: Insufficient nvidia.com/gpu
~~~

### 2.2 五层事实

| 层 | 要确认什么 |
|---|---|
| Pod | 最终 admission 后的 resource request、nodeSelector、toleration、RuntimeClass |
| Node | `status.capacity/allocatable` 是否真的有 `nvidia.com/gpu` |
| Device Plugin | 是否注册成功并发送首个 ListAndWatch 快照 |
| scheduler | Filter 失败原因，是资源不足还是标签/污点等其他约束 |
| 物理节点 | PCI、Driver 和设备是否正常 |

Device Plugin Pod Running 只证明进程存在，不证明资源已经进入 Node Allocatable。

### 2.3 只读证据

~~~bash
kubectl --context <CTX> -n <NS> get pod <POD> -o yaml
kubectl --context <CTX> -n <NS> describe pod <POD>
kubectl --context <CTX> get node <NODE> \
  -o jsonpath='{.status.capacity.nvidia\.com/gpu}{"\t"}{.status.allocatable.nvidia\.com/gpu}{"\n"}'
kubectl --context <CTX> -n <PLUGIN_NS> get ds,pod -o wide
kubectl --context <CTX> -n <PLUGIN_NS> logs <PLUGIN_POD> --since=30m
~~~

还要检查是否请求了 MIG 或 time-slicing 重命名后的资源名，而 Node 只上报另一种资源名。

### 2.4 反事实

- 去掉业务 nodeSelector 后仍 Pending：说明不仅是标签约束。
- 同节点上另一个已分配 GPU Pod 正常：说明资源链曾经工作，但不证明现在还有空闲。
- `Capacity=8, Allocatable=0` 与 `Capacity/Allocatable` 都无该 key 是不同故障。

### 2.5 无 GPU 实验

在隔离的无 GPU 集群中，可用 fake Device Plugin 上报 `example.com/fake-gpu`，练习：

~~~text
未注册 -> Node 无资源
注册但首包未到 -> 仍无资源
首包上报 2 -> Pod 可调度
插件断开/健康变化 -> 观察 Capacity/Allocatable 与重入队
~~~

不要为了制造实验在生产节点删除真实 Device Plugin。

## 3. C02：host `nvidia-smi` 正常，容器仍启动失败

### 3.1 现场

Pod 已绑定 GPU Node，Sandbox 已 Ready，但业务 container 没有 container ID，状态为 `CreateContainerError`。宿主机运行 `nvidia-smi` 正常。

### 3.2 分账

~~~text
PCI / 云主机分配
  -> kernel module / device node
  -> user-space Driver library
  -> NVIDIA Container Toolkit
  -> containerd runtime handler / CDI spec
  -> DeviceManager Allocate 结果
  -> CRI CreateContainer
  -> 容器内 CUDA workload
~~~

host `nvidia-smi` 只覆盖前几层，不能证明 containerd 读取了正确配置、CDI spec 可解析或业务容器拿到了设备。

### 3.3 证据

- Pod Event、status、sandbox/container ID；
- Node 的 OS、kernel、runtimeVersion；
- 实际运行的 containerd binary、启动参数和 config dump；
- Toolkit 版本、runtime handler、CDI spec 的 kind/owner/更新时间；
- Device Plugin / kubelet / containerd 同一时间窗口日志；
- 一个经过批准、固定 digest 的最小 CUDA canary。

不要一看到失败就同时重启 kubelet、containerd、Device Plugin 和 Driver。那会破坏因果链，也可能影响节点上其他 GPU workload。

### 3.4 云节点池漂移案例

一种常见演练是：旧 GPU NodePool 正常，新扩容 NodePool 的 OS image、kernel 或 containerd 配置不同；`nvidia-smi` 都正常，但只有新池容器失败。此时应做节点规范化 diff，而不是把全部 Pod 都当作镜像问题。

长期方案应把 Driver / Toolkit 的所有者写清：云节点镜像、启动脚本还是 GPU Operator，只能有一个正常控制入口。

## 4. C03：Xid / ECC 告警后要不要自动驱逐

### 4.1 先分四件事

~~~text
exporter 存活
GPU 指标 series 存在
DCGM / Driver 报告设备事件
业务实际受影响
~~~

`/metrics` 200 不代表 GPU 健康；单个 Xid 数字也不能在没有 GPU 型号、Driver 版本、上下文和复发信息时直接触发高风险自动化。

### 4.2 建议状态机

~~~text
告警
  -> 保存 Xid/ECC、Node、GPU UUID、Pod、Driver、时间
  -> 判断是否 sticky / 重复 / 伴随 workload error
  -> cordon 或给 GPU 节点加维护状态
  -> 评估训练 checkpoint、PDB、替代容量
  -> 经批准 drain / 重启 / 节点替换
  -> 固定 canary 验证
  -> 观察窗口
  -> 恢复调度或继续隔离
~~~

“告警即自动 drain”会把设备异常转换成全节点业务中断。尤其是长训练、单副本推理或没有可用 GPU 容量时，必须先评估迁移后果。

### 4.3 恢复门禁

- Driver 和设备枚举稳定；
- 关键 Xid/ECC counter 在观察窗口没有继续增长；
- DCGM series 正常且身份标签不漂移；
- 最小 CUDA canary 通过；
- 真实模型 smoke test 通过；
- 节点重新加入后先跑低风险 canary，再恢复普通调度。

## 5. C04：vLLM OOM 或长时间不 Ready

### 5.1 先判断是哪种失败

| 证据 | 可能类型 |
|---|---|
| 应用日志 `CUDA out of memory` | GPU 显存 |
| container `OOMKilled`、exit 137 | cgroup / 主机内存 |
| Pod Running，startup 仍失败 | 模型加载、下载、编译或依赖 |
| `/health` 200，但 TTFT/queue 恶化 | 服务存活但容量/SLO不足 |
| Node Xid 同时出现 | 先排硬件/Driver，不只调显存参数 |

### 5.2 探针分工

~~~text
startupProbe
  -> 给模型下载、加载、图编译和 cache 预热预算

readinessProbe
  -> 当前实例是否应该接收新请求

livenessProbe
  -> 进程是否进入无法自行恢复的状态
~~~

liveness 过早会在大模型冷启动时形成重启风暴。

### 5.3 GPU 发布预算

普通 Deployment 默认 surge 可能需要额外 GPU：

~~~text
当前副本 GPU
  + maxSurge 所需 GPU
  + canary / warmup GPU
  <= 集群当前可用且满足同类约束的 GPU
~~~

如果没有额外 GPU，新 Pod 会 Pending，旧 Pod 又不能按可用性约束先删除。可选方案包括经过容量计算的 Recreate、独立 canary、blue/green 节点池或临时扩容；不存在对所有模型通用的答案。

### 5.4 验收不止 `/health`

至少包含：模型 revision、GPU memory、queue、TTFT、ITL/TPOT、E2E、错误率、吞吐、重试和真实业务 smoke test。回滚还要估算旧模型重新下载和冷 cache 的时间。

## 6. C05：time-slicing 后“集群突然多了很多 GPU”

### 6.1 现场

一台物理 GPU 节点配置 time-slicing replicas 后，Node Allocatable 显示大量 `nvidia.com/gpu`。容量报表若直接求和，会把逻辑访问槽位当成物理 GPU 库存。

### 6.2 四本账

| 账 | 表示什么 | 不能表示什么 |
|---|---|---|
| 物理库存 | 真实 GPU / MIG instance | 不能从逻辑 slot 反推 |
| Node Allocatable | scheduler 可分配的资源名与数量 | 不等于剩余，也不等于物理卡 |
| ResourceQuota | namespace 可请求上限 | 不保证集群存在对应容量 |
| Kueue quota | 谁先获得预算和是否可借用 | 不替代 scheduler placement |

time-slicing 提供共享访问，不自动提供显存硬隔离、性能保证或故障域隔离。成本报表必须以物理库存守恒，再把逻辑使用按经过定义的分摊规则归属。

### 6.3 反事实

即使 Kueue 已 `Admitted`，Pod 仍可能因目标 zone 没有对应 GPU、nodeSelector、taint 或资源已经被占用而 Pending。Kueue 决定预算，scheduler 决定放哪台 Node。

## 7. GPU GitOps 变更为什么要分阶段

推荐同步顺序：

~~~text
1. CRD / cluster-scoped RBAC
2. namespace / PSA / AppProject 边界
3. GPU Operator controller
4. ClusterPolicy / NodePool canary
5. Device Plugin / DCGM 资源与指标验收
6. 0副本 vLLM workload
7. 单副本模型 canary
8. 正式流量
~~~

Driver、MIG、CRD 等变更进入专用同步窗口，不与普通业务自动 Sync 混在一起。紧急现场动作前暂停可能覆盖它的自动同步；事故后把有效状态重新归并回 Git，再恢复 reconcile。

回滚也要分对象：Git revert、Helm rollback、CRD 兼容、Driver 节点替换、MIG 几何、模型和 cache 不是同一件事。

## 8. 公司版本矩阵模板

| 维度 | Cloud-A UAT | Cloud-B Prod |
|---|---|---|
| Kubernetes | `<VERSION>` | `<VERSION>` |
| Node OS/kernel | `<VERSION>` | `<VERSION>` |
| containerd | `<VERSION>` | `<VERSION>` |
| GPU SKU | `<SKU>` | `<SKU>` |
| Driver 所有者/版本 | `<OWNER>/<VERSION>` | `<OWNER>/<VERSION>` |
| Toolkit 所有者/版本 | `<OWNER>/<VERSION>` | `<OWNER>/<VERSION>` |
| Operator / Device Plugin | `<DIGEST>` | `<DIGEST>` |
| MIG / sharing | `<POLICY>` | `<POLICY>` |
| 回滚方式 | `<NODE_REPLACE_OR_INPLACE>` | `<...>` |
| 实验状态 | `<STATUS>` | `<STATUS>` |

业务应依赖平台统一标签，例如 `platform.example.com/gpu-class`，不要直接绑定各云厂商互不一致的底层标签。

## 9. 演练交付物

每个 C 类案例至少提交：

- 版本矩阵与实验状态；
- 责任链和故障假设；
- 只读证据包；
- 允许执行的实验步骤与隔离环境；
- 停止条件、恢复步骤和残留检查；
- PASS/FAIL/NOT RUN 结果；
- 未验证的生产假设。

## 10. 验收题

1. Device Plugin Pod Running，为什么 Node 仍可能没有 GPU Allocatable？
2. host `nvidia-smi` 成功覆盖了哪几层，没覆盖哪几层？
3. 单个 Xid 告警为什么不适合直接自动 drain？
4. vLLM `/health` 200 为什么仍可能烧穿 SLO？
5. time-slicing 后 Allocatable 增加，为什么财务库存不能增加？
6. Kueue Admitted 后 Pod 为什么仍会 scheduler Pending？
