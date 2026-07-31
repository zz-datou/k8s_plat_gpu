# 多云集群迁移与 GitOps 接管

本篇包含 A01、B01、A02。它们来自多次 EKS、ACK、FAT、UAT、Prod 服务纳管经历的脱敏抽象。

## 1. 现场不是“复制 YAML”

源集群中的对象能运行，不代表把 `kubectl get -o yaml` 复制到目标集群就能交付。迁移至少有四份事实：

| 事实 | 回答的问题 | 不能替代什么 |
|---|---|---|
| 源集群 live | 现在实际运行了什么 | 不能证明 Git 是最新的 |
| Git 渲染结果 | 平台准备长期维护什么 | 不能证明目标云依赖已就绪 |
| 目标集群 live | 已有哪些 namespace、准入策略、节点和依赖 | 不能替代期望配置 |
| 环境例外清单 | 哪些差异是故意的 | 不能靠口头记忆长期保存 |

迁移前的第一条原则：先把四份事实对齐，再讨论 Apply 或 Sync。

## 2. A01：从 EKS 向 ACK 迁移一组 Java 服务

### 2.1 现象与目标

源环境已有一组 Java Deployment、Service 和 ConfigMap。目标 ACK 集群刚纳入平台，希望：

- 先把资源交给 Argo CD 管理；
- 不立即承接流量；
- 不把源环境的配置中心地址、凭据和地域参数带过去；
- 不因一次全量同步把目标集群容量打满；
- 源集群在验收前继续提供服务。

这不是“发布”，而是“先建立可控的第二份期望状态”。

### 2.2 最危险的三个捷径

1. 直接导出 live YAML 当模板。它会带上 `status`、UID、resourceVersion、云厂商 annotation 和现场漂移。
2. 看到源集群健康，就认为 Git 与 live 一致。历史热修、手工扩容和镜像替换可能从未回写 Git。
3. 第一轮就恢复源环境全部副本。目标集群的 Allocatable、配额、节点池和外部依赖可能尚未满足。

### 2.3 先做服务清单去重

业务给出的列表常出现重复、旧名称和管理端/业务端混用。先形成唯一清单：

~~~text
原始列表
  -> 去重
  -> 确认规范名称
  -> Deployment / Service / ConfigMap / namespace 一一对应
  -> 标记“已有、缺部分资源、完全缺失”
~~~

不要在生成几十个目录之后才发现同一服务出现两次，或 `foo-mng` 在另一个 namespace 叫 `mng-foo`。

### 2.4 四层差异表

建议每个服务至少核对这些字段：

| 层 | 可迁移基线 | 必须重新确认的环境值 |
|---|---|---|
| Deployment | strategy、探针、resources、terminationGracePeriodSeconds、securityContext | image registry、replicas、nodeSelector、toleration、serviceAccount、注入标签 |
| Service | port、targetPort、selector 规范 | 云 LB annotation、内部/公网类型、旧 DNS 兼容名 |
| ConfigMap/Secret | key 的结构和配置契约 | 配置中心地址、namespace、数据库、域名、地域和凭据 |
| 平台 | Kustomize 目录结构、公共 patch | Admission、LimitRange、Quota、RuntimeClass、CNI、存储类 |

Secret 只比较 key、来源和引用关系，不把 value 打进终端记录或讲义。

### 2.5 目标集群第一轮保持 0 副本

在外部依赖和容量没有验证前，推荐先提交完整资源但让业务 Deployment 为 0：

~~~yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: api-a
  namespace: platform
spec:
  replicas: 0
~~~

0 副本的意义不是“已经上线”，而是把以下问题分开：

- YAML 能否渲染；
- API Server 是否接受；
- Argo 是否能生成并管理 Application；
- namespace 与 RBAC 是否正确；
- 真正启动 Pod 后的镜像、配置、调度和依赖是否可用。

### 2.6 Argo 只授予需要的边界

新集群纳管时，不要因为省事直接给 Argo cluster-admin。可行时建立专用 ServiceAccount，只授权目标 namespace 和本次所需资源。

最小验收至少包含：

~~~text
允许：目标 namespace 内 Deployment / Service / ConfigMap 等
拒绝：kube-system、其他业务 namespace、未知集群级资源
确认：Argo 保存的是受限凭据，而不是意外选中的管理员凭据
~~~

若 Operator、CRD 或 ClusterRole 确实需要集群级权限，应拆成独立的平台变更，不与普通业务迁移混在同一次 Sync。

### 2.7 云端变更前的只读门禁

下面命令只展示模板。真正执行时必须把占位符替换成经过确认的值：

~~~bash
kubectl config current-context
kubectl --context <TARGET_CONTEXT> get ns <TARGET_NAMESPACE> -o jsonpath='{.metadata.uid}{"\n"}'
kubectl --context <TARGET_CONTEXT> get nodes -o wide
kubectl --context <TARGET_CONTEXT> get resourcequota,limitrange -n <TARGET_NAMESPACE>
kubectl --context <TARGET_CONTEXT> get deploy,svc,configmap -n <TARGET_NAMESPACE>

kustomize build <OVERLAY> > /tmp/rendered.yaml
kubectl --context <TARGET_CONTEXT> apply --dry-run=server -f /tmp/rendered.yaml
~~~

在真实流程里还要保存：当前 Git commit、目标集群 UID、namespace UID、Argo Application 目标 server/namespace 以及渲染摘要。

### 2.8 分批接管顺序

推荐把“资源纳管”和“业务启动”拆成两个发布窗口：

~~~text
第一阶段：Application / namespace / RBAC / 0副本资源
  -> Argo Synced/Healthy
  -> 无意外 prune
  -> live 与 render 的非预期 diff 为 0

第二阶段：外部依赖和容量通过后
  -> 先选低风险服务 1 副本
  -> 验证配置中心、数据库、DNS、探针和指标
  -> 分批扩到目标副本
  -> 最后切入口流量
~~~

每批都要有停止条件，例如：

- 新 Pod 连续 NotReady；
- 出现镜像或配置凭据错误；
- Pending 原因是容量或约束，且没有经过批准的扩容方案；
- Error rate、P95 或外部依赖异常；
- Argo 出现本批之外的 prune 或字段变化。

### 2.9 回滚不是“再 Sync 一次”

迁移期至少保留三层回滚：

1. Git：恢复到变更前 commit 或 revert 本次提交。
2. 目标集群：副本降回 0，入口仍不切入或切回源集群。
3. 源环境：在业务验收结束前保持可用，不提前删除。

如果配置中心已经发生写入、数据库 schema 已升级或两边同时消费同一队列，回滚就不再只是 Kubernetes 对象回滚，必须单独设计数据和消息语义。

## 3. B01：FAT 基线为什么不能照抄某一个 UAT

一次基线补齐常同时遇到三类来源：

~~~text
国内 UAT：环境最接近，但模板较旧
海外 UAT：平台基线最新，但有地域差异
FAT live：保留了真实环境参数，但资源规格不规范
~~~

正确做法不是选一个“最像的目录”整份复制，而是建立决策表：

| 字段 | 来源 | 理由 |
|---|---|---|
| probes / resources / rolling strategy | 最新平台基线 | 属于跨环境通用能力 |
| 国内环境不使用的 lifecycle 钩子 | 国内例外清单 | 是明确差异，不是漏配 |
| ConfigMap 环境值 | FAT 当前值或已确认目标值 | 不能从 UAT 带入 |
| 新服务初始镜像 | 已确认的同环境版本 | 不凭目录相似度猜 |
| 副本 | 0 | 先纳管，后启动 |
| 历史 Service 名 | 兼容保留并补规范别名 | 避免一次改断旧调用方 |

“只有一个例外”也要写进 overlay 或说明文件，不能只靠操作者记住。

## 4. A02：Argo CD 的 Sync 策略和字段所有权

同一公司里的 Dev、UAT、FAT、Prod 往往不是一套策略。必须分别回答两个问题：

1. 什么时候允许 Argo 自动执行？
2. 哪些字段由 Git 拥有？

### 4.1 策略矩阵

| 环境示例 | Sync | image 所有者 | replicas 所有者 | 常见理由 |
|---|---|---|---|---|
| Dev | 自动或时间窗 | 发布系统或 Git | HPA / 平台 | 快速反馈 |
| UAT | 部分自动、部分手工 | 发布系统 | Git 或 HPA | 跨团队联调 |
| FAT | 手工 | 外部发布系统 | 现场容量计划 | 环境不常驻 |
| Prod | 手工、分批 | 明确指定一种 | HPA 或 Git，只能选清楚 | 降低无意变更 |

这张表只是示例，不能直接当公司策略。真正重要的是每个环境都写清字段所有权。

### 4.2 `ignoreDifferences` 的真实代价

忽略 Deployment 的 image 或 replicas 可以避免 Argo 覆盖外部发布系统、HPA 或现场扩容，但同时意味着：

- 这些字段的漂移不再通过普通 OutOfSync 明确暴露；
- Git 不能单独还原完整 live 状态；
- 故障时必须知道真正的写入者是谁；
- `RespectIgnoreDifferences=true` 是否生效要通过渲染后的 Application 验证。

因此每条忽略规则旁边应记录：所有者、更新入口、审计来源和回收条件。

### 4.3 `Synced` 不等于业务发布成功

~~~text
Application Synced
  只说明 live 与期望在 Argo 比较规则下相符

Deployment Available
  说明控制器观察到足够可用副本

EndpointSlice ready
  说明 Service 有可接新流量的后端

业务验收通过
  还需要真实请求、依赖、指标和日志
~~~

这四层不能互相替代。

## 5. 与 GPU 平台的迁移

以后纳管 GPU 节点和推理服务时，同一方法仍然成立，但多出三类高风险对象：

- GPU Operator / Device Plugin / DCGM 等集群级组件；
- Driver、Toolkit、containerd 配置等节点状态；
- `nvidia.com/gpu`、MIG profile、time-slicing 和队列配额等资源契约。

建议拆成：

~~~text
平台 CRD/RBAC
  -> 节点 GPU 栈
  -> 资源上报
  -> 0副本推理 Deployment
  -> 单副本模型加载
  -> 业务流量
~~~

不要把 GPU Operator 升级、MIG 重配和业务模型发布塞进一个 Argo Sync。

## 6. 验收题

1. 为什么源集群 live 与 Git 一致仍不能直接全量启动目标副本？
2. 0 副本资源 `Synced/Healthy` 能证明什么，不能证明什么？
3. 忽略 image 和 replicas 后，平台必须新增哪四项治理信息？
4. 为什么保留旧 Service 并增加规范别名有时比一次改名更安全？
5. GPU 集群纳管为什么要把 CRD、节点栈和业务发布拆开？
