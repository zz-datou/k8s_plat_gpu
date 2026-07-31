# 企业实战案例库：把真实故障变成可复用的排障方法

这个案例库不是新的第 22 课，也不改变 07～21 的冻结顺序。

它解决的是另一个问题：学完某条 Kubernetes / GPU 主线后，怎样回到企业现场，把“我见过这个现象”升级成“我能用证据定位责任层、控制变更风险并留下可复盘记录”。

## 1. 案例从哪里来

案例来自既有运维讨论中反复出现的问题模式，并做了合并、校正和脱敏。它们不是聊天记录的原样搬运。

| 标记 | 含义 | 使用方式 |
|---|---|---|
| A：真实故障抽象 | 确实处理过同类现象，但名称、数量、地域和时间已改写 | 学证据链和处置边界 |
| B：复合案例 | 把多次相近事件合成一个更完整的现场 | 学系统化故障树，不把细节当成单次事故原貌 |
| C：迁移演练 | 尚未在现有环境真实发生，依据同一机制设计 | 用于 GPU 转型和预案演练，不能写进真实事故统计 |

> 重要边界：当前历史问题主要来自 Java 业务、多云 Kubernetes、GitOps、CI 和入口流量。GPU 章节中的生产故障均明确标为 C 类演练，不冒充公司已经发生过的 GPU 事故。

## 2. 抽象后的公司技术背景

本案例库按下面这类平台来设计：

~~~text
多地域、多环境
  -> AWS EKS / 阿里云 ACK 等多个 Kubernetes 集群
  -> Dev / FAT / UAT / Prod 不同发布节奏
  -> GitLab CI 构建镜像
  -> Kustomize 管理环境差异
  -> Argo CD / ApplicationSet 执行 GitOps
  -> Ingress / ALB / MSE / Nginx / CDN 承接入口流量
  -> Java 服务依赖配置中心、可观测性探针和数据库
  -> 后续扩展 GPU 节点、Device Plugin、DCGM 与 vLLM
~~~

示例统一使用 `cluster-a`、`cluster-b`、`app.example.com`、`<POD-CIDR>` 和 `<PLACEHOLDER>`。不要把示例值直接用于生产。

## 3. 案例索引

| ID | 类型 | 现场 | 主要训练目标 | 对应课程 |
|---|---|---|---|---|
| A01 | 真实抽象 | 从 EKS 向 ACK 迁移一组 Java 服务 | 四份事实对账、0 副本落地、分批接管 | [01](../01_平台K8s总图_一次发布到接流量.md)、[07](../07_源码热身_Deployment_rollout卡住如何沿控制器找原因.md) |
| B01 | 复合案例 | FAT 基线同时参考国内和海外环境 | 区分通用基线与环境例外，避免复制漂移 | [02](../02_Deployment_ReplicaSet_Pod_发布与副本.md)、[03](../03_Pod状态与探针_Running为什么不等于Ready.md) |
| A02 | 真实抽象 | 各环境 Argo 同步策略和字段所有权不同 | 自动/手工 Sync、镜像和副本归属、忽略规则风险 | [05](../05_资源与弹性_requests_limits_QoS_HPA.md)、[21](../21_MIG_time-slicing_队列_多租户_配额与成本.md) |
| A03 | 真实抽象 | MSE 路由已发布，但部分路径仍返回 503 | Route、Service、EndpointSlice、Ready Pod 分层 | [04](../04_Service与EndpointSlice_流量怎么找到Pod.md)、[13](../13_kubelet_JavaPod_Running不Ready与重启_probe_statusManager_PLEG.md) |
| A04 | 真实抽象 | Ingress 外网 404，最初怀疑 Service 名称 | 用 Event 发现证书地域错误阻断 ALB listener | [01](../01_平台K8s总图_一次发布到接流量.md)、[04](../04_Service与EndpointSlice_流量怎么找到Pod.md) |
| A05 | 真实抽象 | 页面 504，已有 ALB 却突然失去更新能力 | controller 重启、元数据依赖、显式 VPC 配置 | [06](../06_节点维护与排障_cordon_drain_PDB_NodeReady.md)、[13](../13_kubelet_JavaPod_Running不Ready与重启_probe_statusManager_PLEG.md) |
| A06 | 真实抽象 | HTTP POST 经 301 跳 HTTPS 后变成 405 | 区分重定向、方法变化、StripPrefix 和 CORS | [04](../04_Service与EndpointSlice_流量怎么找到Pod.md) |
| A07 | 真实抽象 | 上传接口返回 413 | 从公网 LB、网关到应用逐层定位有效限制 | [04](../04_Service与EndpointSlice_流量怎么找到Pod.md) |
| A08 | 真实抽象 | OSS 已加 CORS，浏览器仍拒绝 CSS | CDN 旧缓存、Origin 变体与响应头验证 | [04](../04_Service与EndpointSlice_流量怎么找到Pod.md) |
| A09 | 真实抽象 | Docker BuildKit 报 `/etc/resolv.conf` 只读 | 区分 Dockerfile 硬错误、宿主 DNS 和镜像源慢 | [12](../12_kubelet_JavaPod从runtimeManager到PodSandbox_CRI与容器启动.md) |
| A10 | 真实抽象 | 同 tag 重建后镜像更新，但应用仍未就绪 | 镜像拉取、配置中心、探针和业务依赖分账 | [03](../03_Pod状态与探针_Running为什么不等于Ready.md)、[12](../12_kubelet_JavaPod从runtimeManager到PodSandbox_CRI与容器启动.md) |
| B02 | 复合案例 | 变更已恢复，但原始容器日志已经丢失 | 证据保全、控制面与容器日志边界、复盘材料 | [13](../13_kubelet_JavaPod_Running不Ready与重启_probe_statusManager_PLEG.md)、[19](../19_DCGM_指标告警_Xid_ECC与GPU健康.md) |
| A11 | 真实抽象 | HTTP 200，但后端身份切错 | 用 UID、selector、EndpointSlice 验证“是谁” | [01](../01_平台K8s总图_一次发布到接流量.md)、[04](../04_Service与EndpointSlice_流量怎么找到Pod.md) |
| A12 | 真实抽象 | Argo self-heal 覆盖 Rancher 手改 | Git 所有权、自动同步与 break-glass | [07](../07_源码热身_Deployment_rollout卡住如何沿控制器找原因.md) |
| A13 | 真实抽象 | DaemonSet 因强反亲和和 request 饱和 Pending | Filter、多原因 Event、request 与 usage | [08](../08_scheduler源码主线_Pending到ScheduleOne与Filter_Score.md)、[09](../09_NodeResourcesFit_JavaPod_request计算与Insufficient资源.md) |
| A14 | 真实抽象 | 删除旧 Pod 后 Job 再补建 | owner/controller 与告警时间窗 | [10](../10_scheduler_JavaPod_Unschedulable重入队与抢占.md)、[12](../12_kubelet_JavaPod从runtimeManager到PodSandbox_CRI与容器启动.md) |
| A15 | 真实抽象 | 上传机有文件，分发机 404 | 多机本地盘、Nginx alias 与资产同步 | [04](../04_Service与EndpointSlice_流量怎么找到Pod.md) |
| A16 | 真实抽象 | 去掉 CDN 后 WAF HTTPS 502 | 每一跳协议、SNI 与回源契约 | [01](../01_平台K8s总图_一次发布到接流量.md) |
| A17 | 真实抽象 | TTL 已过旧入口仍有流量 | 长连接、HTTPDNS、XFF 与退役门禁 | [01](../01_平台K8s总图_一次发布到接流量.md) |
| A18 | 真实抽象 | Header 小写导致旧 SDK 验签失败 | HTTP 语义与兼容边界 | [01](../01_平台K8s总图_一次发布到接流量.md) |
| A19 | 真实抽象 | 镜像已推送但 Pod 无权拉取 | CI、registry、ServiceAccount、RWO 分账 | [11](../11_kubelet_JavaPod已绑定到syncLoop_podWorkers与SyncPod.md)、[12](../12_kubelet_JavaPod从runtimeManager到PodSandbox_CRI与容器启动.md) |
| A20 | 真实抽象 | 切图慢是否应该加 GPU | 先证明软件有 GPU 执行路径 | [14](../14_NVIDIA节点栈_Driver_CUDA_ContainerToolkit_containerd_CDI.md) |
| A21 | 真实抽象 | internal 宽路由暴露 `/inner/**` | Private DNS、网络入口与 API 授权分层 | [04](../04_Service与EndpointSlice_流量怎么找到Pod.md) |
| A22 | 真实抽象 | 503 修成 404，根路径仍 `Cannot GET /` | 后端端口、rewrite 与应用 handler 分层 | [04](../04_Service与EndpointSlice_流量怎么找到Pod.md) |
| C01 | 迁移演练 | GPU Pod 一直 Pending | scheduler 与 `nvidia.com/gpu` 资源账 | [08](../08_scheduler源码主线_Pending到ScheduleOne与Filter_Score.md)、[15](../15_DevicePlugin_注册_ListAndWatch_Capacity_Allocatable.md) |
| C02 | 迁移演练 | host `nvidia-smi` 正常，容器仍启动失败 | Driver、Toolkit、containerd、CDI 分账 | [14](../14_NVIDIA节点栈_Driver_CUDA_ContainerToolkit_containerd_CDI.md)、[16](../16_DeviceManager_deviceID_Allocate与容器注入.md) |
| C03 | 迁移演练 | Xid / ECC 告警后是否自动驱逐 | 监控事实、设备健康、节点隔离与恢复门禁 | [17](../17_checkpoint_健康状态_PodResources与CDI恢复账本.md)、[19](../19_DCGM_指标告警_Xid_ECC与GPU健康.md) |
| C04 | 迁移演练 | vLLM OOM 或长时间不 Ready | 显存账、startup/readiness、GPU 发布预算 | [20](../20_vLLM_模型加载_显存_探针_吞吐延迟与SLO.md) |
| C05 | 迁移演练 | time-slicing 后“GPU 数量暴涨” | 逻辑槽位、物理库存、Quota 与队列四本账 | [21](../21_MIG_time-slicing_队列_多租户_配额与成本.md) |

## 4. 按专题阅读

- [多云集群迁移与 GitOps 接管](01_多云集群迁移与GitOps接管.md)：A01、B01、A02。
- [入口流量故障树：404、405、413、503、504 与 CORS](02_入口流量故障树_404_405_413_503_504_CORS.md)：A03～A08。
- [发布基线、0 副本与 Argo CD 字段所有权](03_发布基线_0副本与ArgoCD字段所有权.md)：A02、A10。
- [CI 构建：DNS、BuildKit 与只读 resolv.conf](04_CI构建_DNS_BuildKit与只读resolvconf.md)：A09。
- [证据保全与可观测性闭环](05_证据保全与可观测性闭环.md)：B02，以及 ARMS / controller / GPU 指标的共同方法。
- [GPU 平台迁移演练](06_GPU平台迁移演练.md)：C01～C05。
- [生产变更 Runbook 模板](07_生产变更Runbook模板.md)：把任一案例改写成可执行、可停止、可回滚的变更单。
- [补充真实案例卡：身份、调度、入口与供应链](08_补充真实案例卡_身份调度入口与供应链.md)：A11～A22。

## 5. 每个案例固定回答七个问题

1. 用户看到的现象是什么，影响面是什么？
2. 哪些只是猜测，哪些已经有证据？
3. 请求或状态跨过了哪些责任层？
4. 最小只读证据集是什么？
5. 哪一个反事实测试能最快推翻错误假设？
6. 修复动作的生效范围、停止条件和回滚点是什么？
7. 修复后怎样证明业务恢复，而不只是对象变成绿色？

## 6. 推荐练法

每学完一课，挑一个关联案例，只输出下面五样东西：

~~~text
1. 一张责任链
2. 一张“现象 -> 证据 -> 结论”表
3. 一组只读命令
4. 一个带停止条件的变更方案
5. 一份修复后验收记录
~~~

能稳定产出这五样，才算把源码知识转成了生产能力。

## 7. 脱敏与安全规则

- 不写真实域名、IP、集群 ID、账号、仓库地址、镜像 tag、证书 ARN、AccessKey、Secret 或 token。
- 即使仓库是私有的，也把讲义当成将来可能公开处理。
- 聊天、终端或日志曾出现凭据时，正确动作是轮换凭据并清理传播面，不是把值写入案例。
- 所有写操作先有备份、dry-run、停止条件和回滚路径；没有这些内容的命令只作为观察命令。
- GPU 演练不允许在生产节点随意重启 runtime、重载 Driver、重配 MIG 或制造 Xid。
