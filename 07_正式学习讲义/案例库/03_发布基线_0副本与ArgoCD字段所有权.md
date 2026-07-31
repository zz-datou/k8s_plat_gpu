# 发布基线、0 副本与 Argo CD 字段所有权

本篇补充 A02、A10。重点不是再讲一次 Deployment 原理，而是把发布、GitOps、镜像、配置和业务就绪分开。

## 1. 一次发布至少有六本账

| 账本 | 主要写入者 | 典型证据 |
|---|---|---|
| Git 期望 | 人、CI、发布系统 | commit、render、Argo diff |
| 镜像内容 | CI / registry | immutable digest、SBOM、签名 |
| Kubernetes 对象 | API Server / controller | generation、ReplicaSet、Pod spec |
| 运行时事实 | kubelet / container runtime | imageID、containerID、restart、exit reason |
| 配置与依赖 | 配置中心、Secret、数据库、DNS | 配置版本、连接结果、应用日志 |
| 业务可用性 | 应用、网关、监控 | readiness、EndpointSlice、真实请求、SLI |

`kubectl apply` 成功只更新了其中一部分。

## 2. A10：同一个 tag 已变，重建后仍然不可用

### 2.1 现场

开发重新推送了相同 tag 的镜像。目标环境未被 Argo 管理，于是删除并重新 Apply Deployment，并将 `imagePullPolicy` 设为 `Always`，新 Pod 确实拉到了新的 digest。

但应用仍未就绪。日志显示数据源初始化失败；继续核对后发现配置中心中的目标 data ID / group 存在，但内容为空或缺少必需的数据库字段。

这说明两个独立问题先后出现：

~~~text
问题 1：mutable tag 导致“YAML没变，但镜像内容变了”
问题 2：新镜像启动后暴露目标环境配置不完整
~~~

修复问题 1 不能证明问题 2 已经解决。

### 2.2 证据链

~~~bash
kubectl --context <CTX> -n <NS> get deploy <APP> -o jsonpath='{.spec.template.spec.containers[0].image}{"\n"}'
kubectl --context <CTX> -n <NS> get pod -l app=<APP> \
  -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.status.containerStatuses[0].imageID}{"\t"}{.status.containerStatuses[0].ready}{"\n"}{end}'
kubectl --context <CTX> -n <NS> describe pod <POD>
kubectl --context <CTX> -n <NS> logs <POD> --all-containers --tail=200
~~~

配置中心验证只记录：实例、namespace、data ID、group、配置版本与必需 key 是否存在。不要输出 AccessKey、Secret 或完整敏感配置。

### 2.3 长期修复

- 生产发布使用不可变 tag 或直接记录 digest；
- 禁止同 tag 覆盖，或把例外限定在明确的开发环境；
- 在启动前做配置契约检查，而不是等 Spring 初始化失败；
- readiness 只在关键依赖可用且应用能服务时成功；
- CI 输出 image digest，GitOps 记录实际批准的 digest；
- 同 tag 应急处理结束后恢复正常版本治理，不能长期依赖 `Always`。

## 3. 0 副本为什么是一种迁移状态，不是健康证明

### 3.1 它能验证什么

- namespace、RBAC 和对象 schema；
- Kustomize 渲染和 API Server dry-run；
- Argo Application 生成、目标集群和目标 namespace；
- Deployment、Service、ConfigMap 的声明是否可创建；
- 是否有意外 prune、集群级资源或越权。

### 3.2 它不能验证什么

- 镜像能否拉取和启动；
- 配置中心、数据库、DNS、证书和第三方依赖；
- scheduler 是否有容量；
- 探针是否正确；
- Service 是否产生 Ready Endpoint；
- 应用在真实流量下的性能和错误率。

所以 0 副本 Application 即使 `Synced/Healthy`，发布状态仍应写成 `CONFIG_ONBOARDED`，而不是 `PRODUCTION_READY`。

## 4. 基线分成“可移植”和“环境所有”

### 4.1 可移植基线

通常包括：

- RollingUpdate 策略；
- startup/readiness/liveness 的角色划分；
- requests/limits 与 JVM RAM 百分比原则；
- 优雅终止、preStop 是否使用的组织规则；
- securityContext、日志目录和监控接入方式；
- 统一 label / annotation 契约。

### 4.2 环境所有

通常包括：

- 配置中心地址和 namespace；
- 数据库、缓存、消息和对象存储地址；
- 地域、域名、证书和云资源引用；
- 副本、HPA、PDB、节点池和调度约束；
- 监控应用名、租户和告警路由；
- Secret 的实际值。

从另一个环境复制时，只能复用结构，不能默认复用值。

## 5. 谁拥有 image 和 replicas

一个字段同时被多个控制器写，最终一定会产生抖动或隐性覆盖。

### 5.1 image 的可能所有者

~~~text
Git commit
发布平台回写 Git
外部镜像发布器直接 patch live
人工 kubectl set image
~~~

必须选定正常入口。人工命令只能是有记录、有回归动作的 break-glass。

### 5.2 replicas 的可能所有者

~~~text
Git
HPA
KEDA
外部容量平台
人工临时扩缩容
~~~

如果 HPA 拥有副本，就不应让 Argo 每次 Sync 把 replicas 拉回固定值；如果 Git 拥有副本，也不能把现场手工扩容当永久状态。

### 5.3 忽略规则不是“万能不冲突”

使用 Argo `ignoreDifferences` 时，应同时保存：

| 字段 | 正常写入者 | 审计来源 | 故障时查看哪里 | 何时取消忽略 |
|---|---|---|---|---|
| image | 示例：发布平台 | 发布记录 + registry digest | live Pod imageID | 发布平台停用时 |
| replicas | 示例：HPA | HPA status + metrics | desired/current replicas | HPA 移除时 |

否则只是让 OutOfSync 消失，并没有建立治理。

## 6. Argo 的三个状态不要混

~~~text
Sync Status
  -> live 与 Git 在比较规则下是否一致

Health Status
  -> Argo 对 Kubernetes 对象状态的聚合判断

业务 SLO
  -> 用户请求是否成功、延迟和错误预算是否正常
~~~

典型现场：Application 已 `Synced`，Deployment 仍 `Progressing`；新 ReplicaSet 的 Pod 因 readiness 失败没有 Available，旧 Pod 继续接流量。GitOps 没有失败，业务发布还没有完成。

如果直接 `kubectl rollout undo`，而 Git 仍指向新版本，下一次 selfHeal 或人工 Sync 可能把现场再次改回去。持久回滚应恢复期望源，并跟踪 Application、Deployment、EndpointSlice 和业务指标共同收敛。

## 7. HPA 与 rollout 的组合风险

另一个常见复合场景：

~~~text
HPA 计算需要更多副本
  -> 新 Pod 因 Insufficient cpu / taint / quota Pending
  -> Ready 副本继续承受流量
  -> 利用率更高
  -> HPA desired 继续升高
~~~

这时只看平均 CPU 会误以为 HPA 没扩容。要同时查看 HPA Condition、Pending Event、Quota、Node Pool 和可调度约束。Cluster Autoscaler 也不一定能解决标签、污点、PVC zone 或没有对应实例规格的问题。

## 8. 发布验收单

~~~text
[ ] Git commit、render 和目标环境一致
[ ] image digest 已固定并可追溯
[ ] Argo 目标集群/namespace 正确，无意外 prune
[ ] Deployment observedGeneration 已追上
[ ] 新 ReplicaSet Available 达标
[ ] Pod imageID、配置版本和依赖正确
[ ] EndpointSlice 只有预期 Ready 后端
[ ] 入口真实请求、错误率、P95/P99 正常
[ ] 回滚 commit、旧镜像和容量仍可用
[ ] break-glass 现场改动已归并回 Git 或明确撤销
~~~

## 9. 与 GPU 发布的连接

vLLM 或其他 GPU 推理服务会把这些问题放大：

- 镜像 digest 之外还要固定模型版本、权重来源和运行参数；
- 0 副本不能验证 GPU 分配、模型加载、显存和 CUDA 路径；
- RollingUpdate 的 surge 需要额外 GPU，普通 Java 服务的默认策略可能永远 Pending；
- 回滚时旧模型 cache 可能已被清理，恢复时间不能只按 Pod 启动时间估算；
- readiness 通过不代表 TTFT、队列长度和吞吐满足 SLO。

## 10. 验收题

1. 新 Pod 已拉到新 digest，为什么仍不能宣布发布成功？
2. 0 副本 `Healthy` 与业务健康有什么差别？
3. image/replicas 被 Argo 忽略后，什么信息必须补充？
4. 为什么 `rollout undo` 在 selfHeal 环境可能只是临时动作？
5. HPA desired 上升但 Ready 副本不变，应看哪四类证据？
