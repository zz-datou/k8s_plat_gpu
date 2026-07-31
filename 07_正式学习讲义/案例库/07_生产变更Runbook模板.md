# 生产变更 Runbook 模板

这个模板用于把“我知道怎么修”变成“另一个值班人员也能安全执行、停止、验证和回滚”。

## 1. 变更摘要

~~~text
标题：
案例类型：真实事故脱敏 / 多事故合成 / 假设演练
变更目的：
业务影响：
目标环境：
目标集群：
目标 namespace：
目标对象：
计划窗口：
负责人 / 复核人：
关联工单 / commit：
~~~

## 2. 事实、假设与未知

| 类别 | 内容 | 证据 |
|---|---|---|
| 已确认事实 | 例如 EndpointSlice 为空 | 命令输出 / 指标 / 日志 |
| 当前假设 | 例如 readiness 指向错误端口 | 尚待反事实验证 |
| 未知 | 例如云网关是否缓存旧后端 | 需要查询云控制面 |

不要把“最像的原因”直接写进“已确认根因”。

## 3. 目标身份门禁

任何写操作前，至少核对：

~~~bash
kubectl config current-context
kubectl --context <EXPECTED_CONTEXT> get ns <EXPECTED_NAMESPACE> \
  -o jsonpath='{.metadata.uid}{"\t"}{.metadata.labels}{"\n"}'
kubectl --context <EXPECTED_CONTEXT> get node -o wide
~~~

记录并人工比对：

~~~text
kubeconfig context
API endpoint / cluster UID
云账号与地域
namespace UID
Argo Application destination
Git overlay 路径
~~~

命令里始终显式写 `--context` 和 `-n`。不要依赖当前终端提示符或默认 namespace。

## 4. 变更范围

### 4.1 会修改

~~~text
- 精确文件 / 对象 / 字段
- 预计触发的 ReplicaSet / Pod / listener / route 变化
- 预计重启、扩缩容或短暂不可用范围
~~~

### 4.2 明确不修改

~~~text
- 其他环境
- 其他 namespace
- 数据库 schema / 配置中心 / DNS（若不在本次范围）
- 集群级资源
- Secret 值
~~~

范围外出现 diff 就停止，不把“顺手修一下”混进同一变更。

## 5. 依赖与容量

~~~text
[ ] 镜像 digest 可用
[ ] 配置中心 data ID / group / namespace 存在
[ ] 数据库、缓存、消息、对象存储可达
[ ] 证书、DNS、LB、网关引用属于正确地域和账号
[ ] requests + rollout surge 不超过目标容量
[ ] Quota / LimitRange / PDB / HPA / KEDA 已核对
[ ] nodeSelector / taint / zone / storage 约束有可落脚节点
[ ] 日志、指标和告警能覆盖变更窗口
~~~

GPU 还要加：Driver/Toolkit/containerd/Device Plugin 版本、实际 GPU/MIG 库存、模型 cache、额外 surge GPU 和训练 checkpoint。

## 6. 变更前证据与备份

至少保存：

~~~text
Git HEAD 和工作范围 diff
Kustomize/Helm 渲染结果
目标对象 YAML 与 UID
Deployment/RS/Pod/Service/EndpointSlice 状态
最近 Event、日志和关键指标
Argo Application diff、sync/health、prune 候选
云 LB / MSE route / backend health
真实请求基线
~~~

备份不是只有一个目录名。还要记录采集时间、校验和、权限、保留期限和恢复方式。Secret 默认不导出明文；必须备份时使用经过批准的加密存储。

## 7. 静态与服务端校验

~~~bash
kustomize build <OVERLAY> > /tmp/rendered.yaml
kubectl --context <EXPECTED_CONTEXT> apply --dry-run=server -f /tmp/rendered.yaml
~~~

根据项目再执行 schema、策略、YAML、Helm、Prometheus rule 或应用测试。只有实际运行过的校验才能写 PASS。

检查 diff 时重点看：

- image、replicas、selector、port/targetPort；
- probe、resources、lifecycle、securityContext；
- ConfigMap / Secret 引用；
- namespace、cluster-scoped 资源和 RBAC；
- prune 候选；
- 云厂商 annotation；
- 与本次无关的格式化或批量变动。

## 8. 执行计划

用最小批次写，不用“执行发布”四个字代替步骤。

~~~text
阶段 1：提交期望状态
  动作：
  预计现象：
  验证：
  停止条件：

阶段 2：同步 0 副本或 canary
  动作：
  预计现象：
  验证：
  停止条件：

阶段 3：分批扩容 / 切流
  动作：
  每批大小与等待时间：
  验证：
  停止条件：

阶段 4：最终收敛
  动作：
  验证：
~~~

如果 Argo 为手工同步，记录每次实际选中的资源；如果自动同步，记录 sync window、selfHeal、prune 和暂停方式。

## 9. 停止条件

在执行前写清，不能出问题后临时决定：

~~~text
- 出现未计划的 prune 或 cluster-scoped 变更
- 目标 context / namespace / Application 与计划不一致
- 新 Pod Pending / CrashLoop / NotReady 超过阈值
- 错误率、延迟、队列或资源超过阈值
- 配置中心、数据库或外部依赖失败
- 容量不足，且替代节点/配额不能在窗口内到位
- controller、LB 或网关无法收敛
- 原始证据与当前假设矛盾
~~~

“停止”表示不再扩大影响面；是否立即回滚取决于当前阶段和回滚风险。

## 10. 验收矩阵

| 层 | 验收 |
|---|---|
| GitOps | commit 正确、Application 目标正确、无意外 diff/prune |
| Deployment | observedGeneration、updated/available、旧 RS 收敛 |
| Pod | Ready、restart、imageID、配置版本、节点分布 |
| Service | EndpointSlice ready/serving/terminating 符合预期 |
| 入口 | DNS、TLS、route、backend health、真实 Host/path |
| 业务 | smoke、错误率、P95/P99、关键依赖 |
| 可观测性 | 日志、指标、trace、告警和 dashboard 数据新鲜 |
| 安全 | 无凭据输出、无临时高权、无绕过策略残留 |

GPU 增加：Allocatable/resource name、Allocate/CDI、CUDA canary、DCGM series、Xid/ECC、显存、模型 revision、TTFT/queue/throughput。

## 11. 回滚计划

### 11.1 触发条件

写可观察阈值，不写“情况不对就回滚”。

### 11.2 回滚动作

~~~text
Git revert 的 commit：
旧镜像 / digest：
副本或流量恢复值：
Argo 自动同步是否需先暂停：
配置中心 / DNS / 网关是否有独立回滚：
数据写入是否向后兼容：
预计恢复时间：
~~~

### 11.3 回滚验收

回滚后重新跑第 10 节，不把“命令返回成功”当恢复。

GPU 回滚要额外说明：旧 Driver 是否仍在节点、CRD 是否向后兼容、旧 MIG 几何能否恢复、旧模型是否仍可下载、cache 是否需要重建。

## 12. 变更后清理

~~~text
[ ] 临时 canary、debug Pod、端口转发已结束
[ ] 临时扩容、宽松网络策略和 break-glass 权限已回收
[ ] 自动同步 / 告警已恢复
[ ] 现场热修已归并回 Git
[ ] 凭据未进入 commit、日志、聊天或附件
[ ] 证据包和复盘链接已登记
[ ] 技术债和独立隐患已拆成后续事项
~~~

## 13. 填写示例：MSE route 返回 503

~~~text
已确认事实：route 已发布；多数同类路径 200；目标 Service EndpointSlice 为空。
当前假设：Deployment 为 0 副本或 readiness 失败。
本次范围：只恢复目标 workload，不修改其他 route。
停止条件：出现非目标 Application diff；Pod 无法通过配置依赖；容量不足。
验证：Pod Ready -> EndpointSlice ready -> MSE backend healthy -> 指定 Host/path 真实请求 200。
回滚：副本恢复 0；route 保持原状；不影响其他已工作的后端。
~~~

这个示例的关键是：503 证据已经指向后端事实，所以不再盲目修改路由。

## 14. 复盘输出

~~~text
影响：
时间线：
根因状态：已确认 / 推定 / 未确认
关键证据：
最初误判：
恢复动作：
为什么有效：
未解决隐患：
防复发：
负责人和期限：
~~~
