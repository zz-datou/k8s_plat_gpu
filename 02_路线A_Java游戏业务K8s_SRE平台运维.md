# 02_路线A_Java游戏业务K8s_SRE平台运维

## 1. 这条路线的定位

这条路线对应你当前本职工作：

```text
公司官网 / 主页 / 游戏SDK / Java后端服务
  -> 容器化部署
  -> Kubernetes发布和运维
  -> 稳定性保障
  -> 平台规范沉淀
```

它的核心目标不是“把K8s每个组件都读完”，而是：

```text
让业务服务稳定、可观测、可发布、可回滚、可扩缩、可治理。
```

## 2. 企业里真实在用什么

### 2.1 工作负载

必须深入：

- Deployment
- ReplicaSet
- Pod
- Job / CronJob
- ConfigMap
- Secret

学到什么程度：

```text
能从一次发布解释到：
Deployment如何创建ReplicaSet
ReplicaSet如何创建Pod
Pod如何被调度
kubelet如何启动容器
readiness如何影响Service后端
发布失败如何暂停和回滚
```

一笔带过：

- StatefulSet：除非你们把中间件跑在K8s里，否则先知道它适合有状态服务即可。
- DaemonSet：知道日志Agent、监控Agent、网络插件、GPU插件会用它。

### 2.2 发布系统

必须深入：

- 滚动发布
- 回滚
- `maxSurge` / `maxUnavailable`
- `progressDeadlineSeconds`
- readiness gate
- 镜像版本和配置版本

企业实际用法：

```text
研发提交代码
  -> CI构建镜像
  -> CD更新Deployment镜像Tag或Helm values
  -> Kubernetes滚动替换Pod
  -> readiness通过后接流量
  -> 异常时回滚ReplicaSet
```

你要能处理：

```text
发布卡住
新Pod不Ready
老Pod被过早下线
镜像拉取失败
配置未生效
回滚后仍异常
```

### 2.3 流量入口

必须深入：

- Service
- EndpointSlice
- kube-proxy
- Ingress / Gateway
- TLS证书
- 域名解析
- CDN / WAF / SLB 到集群入口的边界

学到什么程度：

```text
用户访问域名
  -> DNS
  -> CDN/WAF/SLB
  -> Ingress Controller
  -> Service
  -> EndpointSlice
  -> Pod
```

能按这条链路排查：

```text
域名不通
证书异常
Ingress 404
网关 502/504
Service无Endpoint
Pod Ready但业务不通
```

一笔带过：

- CNI源码不用深挖到实现细节，先掌握 Pod IP、Service转发、NetworkPolicy、DNS、NodePort/LoadBalancer边界。

### 2.4 Java / JVM 容器化

必须深入：

- JVM识别容器CPU和内存。
- `Xmx`、Metaspace、Direct Memory、线程栈。
- OOMKilled 和 Java OOM 的区别。
- CPU limit 导致 throttling 对延迟的影响。
- Spring Boot actuator readiness/liveness。

企业实际用法：

```text
Java服务不是只配一个 memory limit 就完事。
要结合堆内存、非堆内存、线程、DirectBuffer、容器limit和业务峰值。
```

你要能判断：

```text
容器被K8s杀了，还是JVM自己OOM？
CPU使用率不高但接口慢，是不是被CPU limit throttling？
readiness探针失败，是应用没启动完，还是依赖数据库/Redis异常？
```

### 2.5 资源治理

必须深入：

- requests / limits
- QoS
- ResourceQuota
- LimitRange
- HPA
- VPA建议模式
- Cluster Autoscaler边界

学到什么程度：

```text
知道scheduler按request调度。
知道limit是运行时约束。
知道HPA按指标调副本。
知道request设置会影响HPA利用率计算和节点扩容。
```

企业实际用法：

```text
线上服务设置合理requests保障调度和容量规划。
核心服务谨慎设置CPU limit，避免延迟被throttling放大。
内存limit必须谨慎，因为超过后会OOMKilled。
```

### 2.6 可观测性

必须深入：

- Prometheus
- Grafana
- Alertmanager
- 日志采集
- traces/APM
- Kubernetes events
- kube-state-metrics
- node-exporter
- 应用业务指标

企业实际用法：

Java业务至少要有四类看板：

```text
应用看板：QPS、错误率、延迟、线程池、JVM、GC
Pod看板：CPU、内存、重启、Ready、OOMKilled
集群看板：节点、磁盘、网络、组件健康
发布看板：版本、实例数、发布耗时、失败事件
```

告警要分层：

```text
用户影响类：错误率、延迟、可用性
应用异常类：重启、OOM、探针失败
资源风险类：CPU/内存/磁盘/连接数
平台异常类：节点NotReady、组件错误、证书过期
```

## 3. 这条路线需要读源码到什么程度

### 必须读深

- Deployment控制器
- ReplicaSet控制器
- scheduler `ScheduleOne`
- NodeResourcesFit
- kubelet `syncLoop` / `podWorkers` / `SyncPod`
- probe / statusManager
- Service / EndpointSlice / kube-proxy
- HPA控制器
- ResourceQuota / LimitRange

### 不必深读

- apiserver底层每个RESTStorage细节。
- etcd MVCC内部实现。
- CNI插件具体实现源码。
- kube-proxy每一种后端的全部规则细节。
- 所有内置controller。

### 学到企业可用的程度

你能做到这些，就够硬：

```text
1. 线上Pod异常，能从event/log/status/source chain定位原因。
2. 发布异常，能解释Deployment/ReplicaSet/Pod状态为什么卡住。
3. 服务不通，能从Ingress到EndpointSlice到Pod逐层排查。
4. 资源不足，能解释request、limit、QoS、HPA、调度失败之间的关系。
5. 节点异常，能判断是kubelet、runtime、网络、磁盘还是业务容器问题。
6. 能把反复出现的问题沉淀成平台规则和发布模板。
```

