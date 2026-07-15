# 第 20 课：vLLM 在线推理——模型加载、显存、探针、吞吐/延迟与 SLO

> 主案例：一个固定模型版本的 vLLM OpenAI-compatible 服务部署到 Kubernetes 后，Pod 长时间 `Running` 但不 `Ready`；调大探针后终于接流量，却出现排队、TTFT 尾延迟、CUDA OOM 和发布时 GPU 不够用  
> 主线源码：vLLM `v0.25.0` 的 `vllm/entrypoints/openai/api_server.py`、`vllm/v1/engine/async_llm.py`、`vllm/v1/engine/core.py`、`vllm/v1/core/sched/scheduler.py`、`vllm/v1/worker/gpu_worker.py`、`vllm/v1/worker/gpu/model_runner.py`、`vllm/v1/metrics/loggers.py`  
> 版本基线：vLLM `v0.25.0`，GitHub release 于 `2026-07-11` 发布，tag 指向提交 `702f481`；事实核对日期 `2026-07-14`  
> 本课深度：S1。必须能把请求、队列、调度、KV cache、GPU worker、探针、指标和发布连成一条生产证据链；不要求逐个读懂 CUDA kernel 或注意力后端  
> 前置断点：第 13 课已经完成 kubelet probe、statusManager、PLEG 和重启链；第 14～19 课已经完成 GPU 节点软件栈、Device Plugin、DeviceManager、恢复账本、GPU Operator、DCGM/Xid/ECC。本课只在需要处回链，不重复整章源码

---


## 0. 生产现场：`Running`、`Ready`、`/health 200` 为什么仍可能无法交付

先看一个在线推理服务常见的时间线：

```text
10:00:00  Pod 被调度到 GPU Node
10:00:08  容器进程启动，Pod phase=Running
10:00:12  开始读取 tokenizer 和模型配置
10:01:10  从模型仓库或 PVC 读取权重
10:04:20  权重装入 GPU
10:04:55  profile 可用显存，规划 KV cache
10:05:25  分配 KV cache
10:06:40  编译、warm-up、CUDA Graph capture
10:06:47  API server 开始响应 /health
10:06:48  readinessProbe 成功，Service 开始送流量
10:07:20  并发快速上升，waiting queue 增长
10:07:35  /health 仍是 200，但业务 TTFT p99 已经超出 SLO
10:08:02  一个超长 prompt 触发 preemption；尾延迟继续变坏
10:08:44  新发布副本 Pending，因为滚动发布没有额外 GPU
```

这个时间线至少有四种不同的“健康”：

| 层次 | 要回答的问题 | 典型证据 | 不能证明什么 |
|---|---|---|---|
| 进程存活 | 进程是否还活着 | 容器状态、退出码、liveness | 模型已经可服务 |
| 引擎就绪 | 引擎是否初始化完成且未进入已知错误状态 | startup/readiness、启动日志、`/health` | 当前负载下能满足 TTFT/ITL |
| 流量可用 | 网关能否把合格请求交给至少一个 Ready endpoint | EndpointSlice、网关成功率 | 单个请求的生成质量或尾延迟 |
| 产品 SLO | 指定模型、请求分布和租户等级能否满足可用性与延迟目标 | 客户端观测、服务端直方图、请求分桶 | 下一个版本或不同流量分布也会满足 |

因此，本课的第一条纪律是：

```text
Pod Running
  != 模型加载完成
  != 引擎可接流量
  != /health 200
  != 推理请求成功
  != TTFT、ITL、E2E满足SLO
```

Java 经验可以帮助建立类比，但不能替换 GPU 推理事实：

| Java 平台经验 | vLLM 可借用的部分 | 不能直接照搬的部分 |
|---|---|---|
| JVM 启动、类加载、JIT/warm-up | 启动有多个阶段；探针窗口应来自实测分位数 | 权重、KV cache、CUDA Graph 和 GPU collective 有独立显存与拓扑约束 |
| 线程池 active/queue/reject | running/waiting、排队、饱和、背压 | vLLM 每一步动态重组批次，不是固定线程拿一个请求跑到底 |
| Deployment 滚动发布 | readiness、maxSurge、maxUnavailable、回滚证据 | 每个副本需要稀缺 GPU；新旧版本可能无法同时放置 |
| HTTP p95/p99 SLO | 可用性、错误率、端到端延迟、分桶 | 生成请求还必须拆 TTFT、ITL/TPOT、输出长度和 token 吞吐 |

---

## 1. 本课先钉死二十四个结论

1. vLLM `v0.25.0` 是本课的冻结版本；不同 tag 的默认 runner、指标、参数和安全边界不得混讲。
2. `v0.25.0` 对所有 dense models 默认使用 Model Runner V2；旧文章让读者直接追“PagedAttention 实现类”的源码路线已经不适合作为当前入口。
3. “paged KV cache”作为块化管理概念仍然存在；release 中删除的是旧 PagedAttention 实现，不能误讲成 KV cache 不再分页管理。
4. API server 负责 HTTP/OpenAI-compatible 协议、输入处理、tokenization、流式输出等；真正的调度与执行主循环在 Engine Core。
5. 一个请求进入 Engine Core 后先成为等待态；`Scheduler.schedule()` 每个 engine step 重新决定本步运行哪些请求和多少 token。
6. 当前 scheduler 源码明确不把内部调度简单分成两个互斥的“prefill 阶段”和“decode 阶段”；它按已计算 token 与目标 token 的差额推进。运维上仍需要区分 prompt prefill 与自回归 decode 的成本。
7. continuous batching 是“每个 step 动态重组工作集合”，不是启动时固定一个 batch，直到所有请求同时结束。
8. `gpu_memory_utilization` 默认值在 `v0.25.0` 为 `0.92`；它是单个 vLLM 实例的目标显存预算比例，不是 KV cache 比例、不是硬隔离，也不会协调同卡上的另一个实例。
9. 显式设置 `kv_cache_memory_bytes` 时，KV cache 大小不再由 `gpu_memory_utilization` 推导；二者不能当成两个同时生效的上限。
10. `max_model_len=-1` 或 `auto` 可以走自动适配；生产首发仍应把允许的上下文、请求大小和压测分布明确化，不能把自动适配当容量规划。
11. 模型权重只是显存的一部分；activation/workspace、KV cache、CUDA Graph、NCCL buffer、allocator reserve/fragmentation 和安全余量都要入账。
12. CUDA OOM 通常是 GPU allocator/driver 路径中的错误；它可能让进程异常退出，但不等于 Kubernetes `OOMKilled`。后者首先指向容器 cgroup/宿主内存被内核 OOM killer 终止。
13. startup probe 的职责是给冷启动足够但有限的时间；readiness 决定是否接流量；liveness 只应用于确实需要重启才能恢复的失活。
14. `/health` 当前主要检查 Engine Client 是否进入已知死亡/错误状态，不执行合成推理，也不验证 queue、TTFT、模型输出质量或下游依赖。
15. `failureThreshold × periodSeconds` 只是探针失败窗口的粗略下界；还要考虑 initial delay、timeout、探测调度、进程退出和 kubelet 同步时序。
16. 启动预算必须按模型 revision、缓存命中/未命中、GPU/节点类别、并行模式分别测 p99 或更高分位；不能从一次热缓存启动拍脑袋。
17. `vllm:num_requests_waiting`、`vllm:num_requests_running` 和 `vllm:kv_cache_usage_perc` 是容量线索，不是单独的 SLO。
18. Counter 必须用 `rate()` 或 `increase()` 看区间变化；Histogram 分位数必须对 `_bucket` 做 `rate()` 后再 `histogram_quantile()`。
19. vLLM 文档中的 Counter 基名与 Python Prometheus client 的 exposition 名可能不同；例如文档写 `vllm:num_preemptions`，实际抓取通常带 `_total`。查询前必须看本实例 `/metrics`。
20. “没有时间序列”不等于数值为零；先排 scrape、RBAC、标签、版本和 metric rename，再谈告警正常。
21. server aggregate metrics 与 opt-in per-request metrics 的边界不同；后者会增加 CPU 成本，`n>1` 等场景还可能返回 `metrics: null`。
22. 吞吐、TTFT、ITL、TPOT 和 E2E 有天然权衡；只提高总 token/s 可能牺牲单请求尾延迟。
23. 单 GPU 能放下模型时先用单 GPU；TP、PP、DP 分别解决不同问题。跨 Pod 的普通 Deployment 不会自动组成 vLLM 分布式集群。
24. 生产发布、扩缩容和压测都必须先算 GPU 放置、冷启动和容量余量；任何对生产有负载或状态影响的命令都要显式审批。

---

## 2. 冻结版本、证据层级与当前默认实现

### 2.1 本课版本账本

| 项目 | 冻结值 | 运维意义 |
|---|---|---|
| vLLM release | `v0.25.0` | 参数、指标、源码函数以该 tag 为准 |
| 发布时间 | `2026-07-11` | 本课核对日只晚三天，仍不得把 main 分支混入 |
| tag commit | `702f481` | 源码链接优先用 tag；审计时记录 commit |
| 核对日期 | `2026-07-14` | 后续读者必须主动检查是否已有行为变化 |
| dense 默认 runner | Model Runner V2 | 源码入口是 `vllm/v1/worker/gpu/model_runner.py` |
| `gpu_memory_utilization` 默认 | `0.92` | 只是默认，不是所有生产模型的推荐值 |
| 示例服务镜像 | `vllm/vllm-openai:v0.25.0` 的 amd64 manifest digest | 固定示例工件；落地前仍验证架构、driver、CUDA 和 SBOM |
| 示例模型 | `Qwen/Qwen3-0.6B`，revision `9d4bfd9a94aa5f2ab18d77fa457c306da0b8e439` | 用于讲部署结构，不代表生产容量或质量基线 |

release 入口：

- [vLLM v0.25.0 release](https://github.com/vllm-project/vllm/releases/tag/v0.25.0)
- [vLLM v0.25.0 固定 tag 源码树](https://github.com/vllm-project/vllm/tree/v0.25.0)

### 2.2 证据优先级

本课遇到冲突时按以下顺序裁决：

```text
固定tag源码
  > 同版本官方API/source文档
  > 同版本官方使用文档
  > release notes
  > 实际镜像/Pod运行证据
  > 未固定版本的博客、示例和经验
```

“实际运行证据”并不是永远高于源码。它可能来自不同镜像、不同 architecture manifest、额外 patch 或错误参数，所以必须先确认：

```text
镜像digest
模型ID与revision
tokenizer revision
vLLM版本输出
启动参数
GPU产品与driver
并行度
指标抓取样本
```

### 2.3 Model Runner V2 与旧资料的断点

`v0.25.0` release 明确：

- Model Runner V2 成为所有 dense model 的默认 runner。
- legacy PagedAttention 实现已移除。

这两句话要精确理解：

| 正确说法 | 错误说法 |
|---|---|
| 当前 dense 默认执行源码从 `vllm/v1/worker/gpu/model_runner.py` 进入 | vLLM 再也没有 KV block 或 paged KV cache |
| 调度器/KV cache manager 仍做块化容量与映射管理 | 文档里出现“Paged Attention”就说明旧实现仍是默认 |
| 旧 `gpu_model_runner.py` 路径仍可能服务 legacy/兼容代码，不能据此判断默认 | 找到旧文件就证明 v0.25.0 没切换 MRv2 |

阅读旧博客时，先问三个问题：

1. 它对应哪个 tag？
2. 它讲的是概念、公共 API，还是某个已删除的内部类？
3. 当前 default path 的调用链是否仍会到达它？

---

## 3. 从 HTTP 请求到 GPU token：一条完整生产链

### 3.1 组件分层

官方架构文档把在线服务拆成 API server 与 Engine Core。结合 `v0.25.0` 源码，可以画成：

```text
Client / Gateway
  |
  | HTTP, auth, request validation, streaming
  v
OpenAI-compatible API server
  |
  | renderer / tokenizer / input processor
  | AsyncLLM.add_request()
  v
Engine Core client / ZMQ transport
  |
  v
EngineCore.add_request()
  |
  v
Scheduler waiting queue
  |
  | 每个step: schedule()
  v
Executor -> one or more GPU Workers
  |
  v
MRv2 GPUModelRunner.execute_model()
  |
  | logits / sampled tokens
  v
Scheduler.update_from_output()
  |
  v
Async output processor / detokenizer / stream
  |
  v
Client receives first token ... final token
```

责任边界：

| 层 | 主要职责 | 常见瓶颈/故障 |
|---|---|---|
| Gateway/Ingress | TLS、租户认证、配额、限流、请求大小、客户端连接 | 5xx、限流、连接排队、流式缓冲 |
| API server | 协议、参数校验、模板、tokenization、多模态预处理、stream | CPU 饥饿、事件循环阻塞、模板错误 |
| AsyncLLM/transport | 请求生命周期、异步输出、Engine Core 通信 | core death、队列/IPC、输出消费者慢 |
| Scheduler/KV manager | admission、token budget、KV block、preemption | waiting 增长、capacity/deferred、KV 压力 |
| Executor/Worker | 分布式执行、设备初始化、模型加载、KV 分配 | CUDA/NCCL、设备映射、worker crash |
| MRv2 model runner | forward、sampling、compile/warm-up/graph | kernel、activation 峰值、graph capture |
| GPU/driver/fabric | 计算、HBM、PCIe/NVLink、collective | Xid/ECC、带宽、拓扑、reset |

### 3.2 关键源码入口

| 调用点 | 固定源码 | 阅读时要回答的问题 |
|---|---|---|
| `run_server()` / `build_and_serve()` | `vllm/entrypoints/openai/api_server.py` | server 如何构建 app 和 engine client |
| `build_async_engine_client_from_engine_args()` | 同上 | 配置如何进入 `AsyncLLM`，退出时如何 shutdown |
| `AsyncLLM.add_request()` | `vllm/v1/engine/async_llm.py` | 输入何时被处理、何时提交到 Engine Core |
| `AsyncLLM.generate()` | 同上 | 为什么它是 async generator，输出如何逐步返回 |
| `EngineCore.__init__()` | `vllm/v1/engine/core.py` | executor、KV cache、scheduler 的初始化顺序 |
| `EngineCore.add_request()` | 同上 | request 如何交给 scheduler |
| `EngineCore.step()` | 同上 | `schedule -> execute_model -> update_from_output` 主循环 |
| `Scheduler.add_request()` / `schedule()` | `vllm/v1/core/sched/scheduler.py` | waiting/running 如何变化，token budget 如何使用 |
| `Worker.init_device()` / `load_model()` | `vllm/v1/worker/gpu_worker.py` | device、权重和 memory profiler 边界 |
| `determine_available_memory()` | 同上 | 可给 KV cache 的预算如何得出 |
| `initialize_from_config()` / `compile_or_warm_up_model()` | 同上 | KV cache 何时分配，warm-up/graph 何时发生 |
| `GPUModelRunner.load_model()` | `vllm/v1/worker/gpu/model_runner.py` | MRv2 如何选择 loader 并记录权重显存 |

固定源码：

- [api_server.py @ v0.25.0](https://github.com/vllm-project/vllm/blob/v0.25.0/vllm/entrypoints/openai/api_server.py)
- [async_llm.py @ v0.25.0](https://github.com/vllm-project/vllm/blob/v0.25.0/vllm/v1/engine/async_llm.py)
- [core.py @ v0.25.0](https://github.com/vllm-project/vllm/blob/v0.25.0/vllm/v1/engine/core.py)
- [scheduler.py @ v0.25.0](https://github.com/vllm-project/vllm/blob/v0.25.0/vllm/v1/core/sched/scheduler.py)
- [gpu_worker.py @ v0.25.0](https://github.com/vllm-project/vllm/blob/v0.25.0/vllm/v1/worker/gpu_worker.py)
- [MRv2 gpu/model_runner.py @ v0.25.0](https://github.com/vllm-project/vllm/blob/v0.25.0/vllm/v1/worker/gpu/model_runner.py)

### 3.3 源码主循环：运维上最值得记住的五行

把 `EngineCore.step()` 压缩成伪代码：

```python
def step():
    scheduler_output = scheduler.schedule()
    future = model_executor.execute_model(scheduler_output, non_block=True)
    model_output = future.result()
    engine_core_outputs = scheduler.update_from_output(
        scheduler_output, model_output
    )
    return engine_core_outputs
```

这段伪代码解释了几个现象：

- queue 增长可能发生在 GPU forward 之前。
- 一次 `execute_model` 不是“一个 HTTP 请求从头跑到尾”。
- scheduler 的选择会影响 TTFT 与 decode 流畅度。
- GPU utilization 只是 execute 部分的观测，不能覆盖 tokenizer、queue、transport 和输出消费。
- worker 或 collective 失效会向上破坏 Engine Core，但 `/health` 是否及时反映取决于错误是否已被 client/core 识别。

### 3.4 prefill、decode 与 continuous batching

对产品和容量工程，仍要区分：

| 工作 | 输入 | 主要产出 | 常见敏感项 |
|---|---|---|---|
| prefill | 整段 prompt tokens | 首次可用于生成的上下文/KV | prompt 长度、attention 计算、TTFT |
| decode | 已有上下文 + 新生成 token | 下一 token | 并发序列、KV 访问、ITL/TPOT |

但当前 scheduler 内部不是两个互斥大阶段。源码注释的核心含义是：

```text
每个请求记录num_computed_tokens
目标包含prompt token、output token和可能的spec token
每一步计算“还差多少token”
调度器在本步预算内分配token
因此同一步可以包含多个请求、不同工作形态和chunked prefill
```

continuous batching 不是：

```text
凑齐8个请求
  -> 8个请求形成永久batch
  -> 等最慢请求完成才换下一批
```

而更接近：

```text
step N:
  running A/B/C + admit D的一部分prompt

step N+1:
  A完成，B/C继续decode，D继续prefill，admit E

step N+2:
  B/C/D/E按token budget和KV可用性重组
```

这也是吞吐与延迟调优不能只看 `max_num_seqs` 或一个 batch size 的原因。

---

## 4. Kubernetes 如何把一个 GPU 交给 vLLM

### 4.1 从 Pod 资源声明到进程可见设备

最简链路：

```text
Pod limits: nvidia.com/gpu: 1
  -> scheduler只选有可分配扩展资源的Node
  -> kubelet DeviceManager调用Device Plugin Allocate
  -> runtime/CDI注入设备、库和环境
  -> 容器内vLLM看到一个逻辑CUDA设备
  -> Worker.init_device()选择该可见设备
```

关键边界：

1. `nvidia.com/gpu` 是扩展资源；通常写 `limits` 即可，Kubernetes 会使 request 与 limit 一致。
2. 容器内 `cuda:0` 是“此容器可见设备集合中的第 0 个”，不保证等于宿主机物理 index 0。
3. 不要在普通工作负载中手工写死 `CUDA_VISIBLE_DEVICES=0` 来绕过 kubelet 分配。
4. Pod 内所有普通容器位于同一 Node；一个 Pod 申请多个 GPU，也只能拿该 Node 上可分配的设备。
5. Device Plugin 分配成功只证明设备注入链通过，不证明模型能装下、NCCL 能通信或 SLO 能满足。

### 4.2 Pod、进程、GPU 与 parallel rank

官方架构对默认进程拓扑给出一个重要关系：每个 Engine Core 的 worker 数通常与 `TP × PP` 对应，DP 则有多个 Engine Core/rank。

生产上可用下面的近似映射理解：

| 模式 | 主要目标 | GPU/进程关系 | Kubernetes 常见承载 |
|---|---|---|---|
| 单 GPU | 最简单、模型能放下 | 1 worker / 1 GPU | 1 Pod 请求 1 GPU |
| TP | 一层张量切到多卡，解决单卡放不下或提速 | 同一请求频繁 collective | 常见为 1 Pod 请求同节点多 GPU |
| PP | 模型层分到多个 stage | stage 间传 activation | 可跨节点，但启动、网络和调度更复杂 |
| DP | 多个模型副本处理不同请求 | 每 rank 有自己的 Engine Core/副本语义 | 多 Pod 或受控分布式拓扑 |

普通 `Deployment replicas: 4` 只会产生四个独立 Pod。它不会自动：

- 选出 Ray head；
- 建立 TP/PP process group；
- 分配 rank；
- 保证 gang scheduling；
- 等待全部成员后一起 Ready；
- 建立安全的跨节点内部通信。

跨节点 vLLM 必须显式设计 launcher/cluster runtime、head/worker 生命周期、服务发现、端口、RBAC、NetworkPolicy、failure domain 和整体回滚。本课部署模板故意采用独立单 GPU 副本。

### 4.3 GPU 之外的资源也能卡死在线推理

| 资源 | 不足时的现象 | 证据 |
|---|---|---|
| CPU | tokenizer/JSON/stream 慢，GPU util 低，TTFT 高 | CPU throttling、run queue、API server profile |
| 容器内存 | tokenizer/cache/pinned memory 触发 cgroup OOM | `lastState.reason=OOMKilled`、memory events |
| ephemeral storage/inode | 模型下载、编译 cache、日志写失败 | eviction、`df`/inode、容器日志 |
| PVC/对象存储 | 冷启动慢、I/O 抖动、校验失败 | volume events、I/O latency、下载日志 |
| `/dev/shm` | multiprocessing/NCCL/IPC 异常或性能差 | mount size、worker/collective error |
| 网络 | 模型下载慢、跨节点 NCCL 慢、stream 阻塞 | flow、retransmit、NCCL logs |
| GPU fabric | TP collective 慢 | topology、NVLink/PCIe、DCGM/NCCL tests |

`emptyDir.medium: Memory` 提供的 `/dev/shm` 会计入 Pod/容器的内存使用，不能把它当免费 GPU 显存。

---

## 5. 模型启动：从进程创建到真正 Ready

### 5.1 启动阶段账本

一个可用于探针与故障定位的阶段表：

| 阶段 | 主要动作 | 典型资源 | 常见失败 |
|---|---|---|---|
| S0 容器准备 | image pull、volume mount、Secret/config | registry、PVC、runtime | ImagePull、Mount、权限 |
| S1 CLI/config | 解析参数、加载 model/tokenizer config | CPU、网络/cache | 参数不兼容、revision 不存在 |
| S2 device init | CUDA device、distributed/NCCL 初始化 | driver、GPU、网络 | driver/CUDA/NCCL、设备不可见 |
| S3 权重读取 | 下载/读取 shards、反序列化 | 网络、磁盘、CPU memory | 401、超时、磁盘满、损坏 |
| S4 权重装载 | 将 shard 放到目标 device | HBM、PCIe | CUDA OOM、dtype/quant 不支持 |
| S5 memory profile | profile activation/non-KV/graph 峰值 | GPU、CPU | profile OOM、配置过激 |
| S6 KV 规划/分配 | 计算可用 KV，分配 block | HBM | max len/并发不成立 |
| S7 compile/warm-up | 编译、kernel warm-up、graph capture | GPU、CPU、cache | compile 慢、graph OOM、cache 权限 |
| S8 server ready | Engine Core handshake 完成，路由可响应 | IPC/ZMQ、HTTP | core dead、port/bind |
| S9 首个真实请求 | tokenization、schedule、prefill、decode | 全链路 | SLO/模型模板/输出异常 |

源码中的 `EngineCore.__init__()` 先构造 executor，再通过 `_initialize_kv_caches()`：

```text
worker报告KV cache规格
  -> determine_available_memory()
  -> get_kv_cache_configs()
  -> 计算KV token capacity / max concurrency
  -> initialize_from_config()
  -> profile、创建KV cache、warm-up/compile完成
  -> scheduler与引擎进入可工作状态
```

启动日志必须和阶段对齐。只搜最后一个 `Ready` 字样，会丢失“下载慢”还是“graph capture 慢”的主要矛盾。

### 5.2 冷缓存、热缓存与发布预算

至少建立四类启动基线：

| 维度 | 取值示例 |
|---|---|
| 模型缓存 | miss / hit |
| 编译缓存 | miss / hit |
| GPU/Node 类别 | A10 / L40S / A100；不同 CPU/磁盘 |
| 并行模式 | single / TP2 / TP4 / PP |

每次记录：

```text
T_schedule
T_image_ready
T_process_start
T_model_config
T_weights_loaded
T_profile_done
T_kv_ready
T_compile_warmup_done
T_health_200
T_readiness_true
T_first_real_request_first_token
```

探针不是隐藏不稳定的工具。合理预算应类似：

```text
startup budget
  = 对应模型+revision+节点类别+缓存状态的启动高分位
  + 可解释的抖动余量
  + 观测和发布决策余量
```

不要把“PVC 热缓存 p50 40 秒”直接设成所有节点的 60 秒 startup window。首次调度到新节点时可能需要下载数十或数百 GB，并重新编译。

### 5.3 `/health` 的真实边界

固定版本的 health handler 调用 engine client 的 `check_health()`。`AsyncLLM.check_health()` 的核心语义是：如果 engine 已进入 errored 状态则抛错，否则返回。

所以 `/health 200` 可以支持：

- server 路由能响应；
- engine client 尚未报告已知死亡状态；
- 初始化通常已经走到 server 可接受 health 的阶段。

它不能支持：

- 发送一个固定 prompt 后能产出正确 token；
- 当前 waiting queue 为零；
- TTFT、ITL、TPOT 或 E2E 达标；
- 所有 DP/TP/PP rank 的性能都正常；
- 模型 revision、chat template 和业务语义正确；
- 网关、鉴权、配额和流式客户端路径正常；
- GPU 没有潜在 Xid/ECC 或即将发生的容量故障。

固定源码：

- [health.py @ v0.25.0](https://github.com/vllm-project/vllm/blob/v0.25.0/vllm/entrypoints/serve/instrumentator/health.py)
- [AsyncLLM.check_health() @ v0.25.0](https://github.com/vllm-project/vllm/blob/v0.25.0/vllm/v1/engine/async_llm.py)

### 5.4 startup、readiness、liveness 的正确分工

| Probe | 失败动作 | 本课建议语义 | 错用后果 |
|---|---|---|---|
| startup | 达阈值后重启容器 | 冷启动是否在预算内完成 | 下载/编译未完成就循环重启 |
| readiness | 从 Service endpoint 摘流量 | 当前实例是否允许接新请求 | 把容量问题变成流量抖动或雪崩 |
| liveness | 达阈值后重启容器 | 引擎失活且重启有恢复价值 | 队列高时杀进程，放大失败 |

startup probe 成功前，Kubernetes 会抑制 liveness/readiness；因此它适合保护长启动。

probe 窗口的粗略估算：

```text
最短连续失败窗口 ≈ failureThreshold × periodSeconds
```

但实际动作还受这些因素影响：

- `initialDelaySeconds`；
- 单次 `timeoutSeconds`；
- probe 执行和 kubelet worker 调度；
- success/failure 阈值；
- kubelet sync 与 runtime 重启；
- termination grace；
- API server/网络路径；
- 容器是否在 probe 开始前就退出。

完整 kubelet 源码链已经在[第 13 课](./13_kubelet_JavaPod_Running不Ready与重启_probe_statusManager_PLEG.md)讲过。本课只记：

```text
prober worker产生结果
  -> startup/liveness/readiness manager保存
  -> readiness影响Pod Ready
  -> startup/liveness失败进入sync决策
  -> kubelet runtime manager计算容器动作
  -> 需要时重启容器
```

当前本地 Kubernetes 快照的关键入口：

- `pkg/kubelet/prober/worker.go`：`(*worker).doProbe`
- `pkg/kubelet/kubelet.go`：probe manager updates、`handleProbeSync`
- `pkg/kubelet/kuberuntime/kuberuntime_manager.go`：`computePodActions`

不要误讲成“probe worker 直接调用 CRI kill”。

### 5.5 readiness 不是容量自动控制器

以下条件不应默认让 readiness 失败：

- waiting queue 短暂大于零；
- GPU utilization 高；
- KV usage 暂时高；
- 单个请求超时；
- 业务 SLO 在 1 分钟窗口轻微波动。

否则会形成：

```text
流量升高
  -> readiness失败
  -> endpoint减少
  -> 剩余Pod负载更高
  -> 更多readiness失败
  -> 服务雪崩
```

readiness 更适合表达“实例不能安全接新流量”的离散状态，例如：

- engine 已死或 worker 集合不完整；
- 必需模型没有加载；
- 发布/排空控制明确把实例置为不接新流量；
- 关键内部连接不可恢复。

容量过载主要应由网关限流/排队、扩缩容和 SLO 告警处理。

---

## 6. 显存账本：`gpu_memory_utilization` 绝不是“KV cache 百分比”

### 6.1 一张卡上的主要显存科目

先用预算式建立边界：

```text
M_GPU_total
  =
    M_driver_and_context
  + M_weight_shard
  + M_peak_activation_and_workspace
  + M_KV_cache
  + M_CUDA_graph_pools
  + M_collective_buffers
  + M_allocator_reserved_and_fragmentation
  + M_multimodal_or_other_features
  + M_safety_headroom
```

这不是 vLLM 内部的一个精确等式，而是运维容量账本。每项的意义：

| 科目 | 受什么影响 | 为什么常被漏掉 |
|---|---|---|
| driver/context | CUDA context、库、进程数 | 不在模型参数量里 |
| weight shard | 参数量、dtype、quant、TP/PP | “参数量 × 2 bytes”只是粗估 |
| activation/workspace | batch/token budget、kernel、模型结构 | profile 或首个大请求才出现峰值 |
| KV cache | 活跃 cached tokens、层数、KV heads、head dim、KV dtype、sharding | 随并发和上下文增长 |
| CUDA Graph | capture shape、runner/config | warm-up 时才分配 |
| collective buffer | TP/PP/DP、NCCL | 单卡实验里没有 |
| allocator reserve/fragmentation | 分配历史、size class、并发 | `reserved != allocated` |
| multimodal/feature cache | 输入类型、encoder、LoRA 等 | 纯文本基线无法覆盖 |
| safety headroom | 驱动抖动、版本、异常输入 | 被“榨满显存”目标吃掉 |

权重的第一阶估算：

```text
M_weights_rough
  ≈ parameter_count
    × bytes_per_stored_weight
    ÷ effective_sharding_factor
```

但这些会让估算偏离：

- quantization scale、zero point 和 metadata；
- 未被切分或被复制的层；
- vocabulary、embedding、lm head 的特殊处理；
- padding、alignment、tied weights；
- offload 与 host staging；
- loader 临时峰值；
- PP 的不均匀层切分；
- MoE expert placement。

KV cache 的一阶关系：

```text
M_KV_rough
  ∝ live_cached_tokens
    × layers
    × key_and_value
    × kv_heads
    × head_dim
    × kv_dtype_bytes
    ÷ effective_KV_sharding
```

它适合回答“什么变量会增大显存”，不适合替代固定模型在固定版本上的 profile 和压测。

### 6.2 `gpu_memory_utilization` 的准确语义

`v0.25.0` `CacheConfig` 中：

```python
gpu_memory_utilization: float = Field(default=0.92, gt=0, le=1)
```

官方字段说明强调：

- 这是当前 vLLM instance 的 GPU memory utilization 比例；
- 它不与同一 GPU 上的另一个 vLLM instance 协调；
- 如果明确要同卡运行两个 instance，可以分别配置类似 `0.5`，但这仍只是配置，不是硬隔离保证。

因此：

```text
--gpu-memory-utilization 0.80
```

不能翻译为：

- “KV cache 占卡的 80%”；
- “容器最多只能用 80%”；
- “另一个进程一定能安全使用剩余 20%”；
- “运行时无论什么请求都不会 OOM”；
- “MIG/time-slicing 已提供资源隔离”。

更准确的理解：

```text
目标executor预算
  -> 扣除profile得到的非KV峰值等部分
  -> 剩余预算用于规划KV cache
  -> 运行时仍可能受到未覆盖峰值、fragmentation和其他进程影响
```

固定官方 API：

- [CacheConfig @ v0.25.0](https://docs.vllm.ai/en/v0.25.0/api/vllm/config/cache/)
- [cache.py @ v0.25.0](https://github.com/vllm-project/vllm/blob/v0.25.0/vllm/config/cache.py)

### 6.3 `kv_cache_memory_bytes` 的覆盖关系

当 `kv_cache_memory_bytes` 非空时，官方说明是它覆盖自动推导的 KV cache 大小，`gpu_memory_utilization` 对 KV cache 推导被忽略。

这意味着：

| 配置 | 行为 |
|---|---|
| 只给 `gpu_memory_utilization` | worker profile 非 KV 使用后推导可分配 KV |
| 显式给 `kv_cache_memory_bytes` | 使用明确的 KV 字节预算 |
| 两者都给 | 不能把二者解释成“取更小者”；KV 配置走显式字节值 |

显式字节值适合可重复实验，但风险也更直接：

- 换 GPU 型号后总显存不同；
- 换模型/dtype/runner 后非 KV 峰值不同；
- CUDA Graph 配置变化；
- TP/PP shard 变化；
- 同卡出现额外进程；
- 数值可分配但运行峰值仍 OOM。

每次版本、模型或硬件变化都要重新 profile。

### 6.4 `max_model_len`、上下文与容量

`max_model_len` 限制模型处理的上下文长度。`v0.25.0` 支持 `-1` 或 `auto` 让系统在 GPU memory 约束下自动选择可容纳的最大值：如果完整模型上下文能放下则使用完整值，否则选择能够容纳的较大值。

自动适配不等于：

- 自动得到业务所需上下文；
- 自动保证指定并发；
- 自动限制用户 prompt；
- 自动避免单请求耗尽队列；
- 自动得到最佳 TTFT/吞吐。

生产更稳妥的顺序：

1. 产品定义真正需要的 prompt + output 上限。
2. 网关限制 body、prompt、`max_tokens`、`n` 等请求放大项。
3. vLLM 用明确 `max_model_len` 建立硬边界。
4. 按 prompt/output 分布压测并发与尾延迟。
5. 只有在接受自动选择及其版本变化时才用 `auto`。

固定源码：

- [ModelConfig @ v0.25.0](https://docs.vllm.ai/en/v0.25.0/api/vllm/config/model/)
- [model.py @ v0.25.0](https://github.com/vllm-project/vllm/blob/v0.25.0/vllm/config/model.py)
- [KV cache auto-fit implementation @ v0.25.0](https://github.com/vllm-project/vllm/blob/v0.25.0/vllm/v1/core/kv_cache_utils.py)

### 6.5 为什么两实例各 `0.5` 仍不是隔离方案

假设同一完整 GPU 上运行 A/B 两个 Pod，每个 `gpu_memory_utilization=0.5`：

```text
静态规划:
  A目标50%
  B目标50%

现实:
  driver/context各有一份
  warm-up峰值时间可能重叠
  graph/collective/allocator保留量不同
  GPU compute和memory bandwidth仍竞争
  kubelet扩展资源默认不会把一张整卡同时分给两个普通Pod
```

只有在平台显式配置 time-slicing、MPS、MIG 或其他共享机制时，多个工作负载才可能看到同一物理 GPU。此时还必须分别确认：

- 显存隔离是否存在；
- fault domain 是否隔离；
- compute fairness；
- 监控能否按实例归因；
- Device Plugin 暴露的是整卡、MIG profile 还是共享 replica；
- vLLM 参数是否按逻辑切片容量重测。

不要把一个 vLLM 参数当作硬件多租户隔离。

---

## 7. OOM 故障树：先分 GPU、主机内存与硬件故障

### 7.1 第一刀：容器为什么结束

```text
Pod重启或请求失败
  |
  +-- container lastState.reason=OOMKilled ?
  |     |
  |     +-- 是 -> 主机/容器cgroup内存方向
  |
  +-- 日志有torch.cuda.OutOfMemoryError / CUDA out of memory ?
  |     |
  |     +-- 是 -> GPU HBM预算/峰值/碎片/竞争方向
  |
  +-- 日志有NCCL/CUDA error，Node有Xid/ECC ?
  |     |
  |     +-- 是 -> driver/GPU/fabric健康方向
  |
  +-- exit code、signal、probe失败、应用异常？
        |
        +-- 按退出与事件证据继续
```

三类不能混写：

| 类别 | 典型证据 | Kubernetes 表现 | 第一责任域 |
|---|---|---|---|
| GPU OOM | `torch.cuda.OutOfMemoryError`、CUDA allocation failure | 进程可捕获、请求失败，也可能退出/CrashLoop | 模型/runner/显存配置/同卡竞争 |
| cgroup OOM | `lastState.reason=OOMKilled`、exit 137、memory events | kubelet 看到容器被内核杀死 | Pod memory limit、CPU-side cache/pinned memory |
| GPU/driver fault | Xid、ECC、device lost、NCCL async error | Pod 可能挂死、失败或重启 | GPU Node/driver/fabric，衔接第 19 课 |

CUDA OOM 最终导致进程退出时，Pod 可能进入 `CrashLoopBackOff`。这仍不把根因改成 Kubernetes `OOMKilled`。

### 7.2 CUDA OOM 出现在不同阶段，含义不同

| 阶段 | 更可能的原因 | 优先证据 |
|---|---|---|
| 权重加载 | 模型/quant/dtype/TP shard 放不下；同卡已有进程 | loader memory log、GPU process inventory、模型配置 |
| memory profile | activation/workspace 峰值；配置太激进 | `determine_available_memory` 前后日志 |
| KV 分配 | KV bytes/context/token capacity 不成立 | CacheConfig、KV capacity log |
| graph capture/warm-up | capture shape/graph pool 额外显存 | compile/capture 阶段日志 |
| 运行 prefill | 超长 prompt、大 token budget、activation 峰值 | prompt bucket、running/waiting、request params |
| 运行 decode | 活跃 cached token 过多、KV 压力、并发 | KV usage、preemption、running/waiting |
| TP/PP | rank 不均、collective buffer、某卡有额外进程 | 每 rank 日志与每 GPU process/memory |

### 7.3 CUDA OOM 的处置顺序

不要只做“把 utilization 从 `0.92` 改成 `0.99`”。建议顺序：

1. 固定镜像、模型 revision、启动参数、GPU UUID/逻辑设备和并行 rank。
2. 确认是否有额外 GPU 进程或共享机制。
3. 定位 OOM 阶段。
4. 记录权重、非 KV profile、KV、graph capture 的日志数值。
5. 降低允许上下文或请求放大项。
6. 降低并发/token budget，观察是否从运行峰值恢复。
7. 留出更大 headroom，而不是榨满预算。
8. 评估 dtype/quant，但必须重新做质量和 kernel 支持验收。
9. 模型单卡确实放不下时，再评估 TP/PP。
10. 固定工作负载重新跑吞吐与尾延迟，防止“无 OOM 但 SLO 更差”。

### 7.4 cgroup OOM 的常见来源

即使权重主要在 GPU，CPU memory 仍可能被这些占用：

- model shard 下载与反序列化 staging；
- tokenizer、chat template 和输入对象；
- pinned host memory；
- Ray/worker/IPC；
- Python heap 与输出队列；
- 多模态 decode；
- page cache 与 memory-backed `emptyDir`；
- profiler trace；
- 并发请求 body 和流式缓冲。

证据必须包含：

```text
Pod resources.requests/limits.memory
containerStatuses.lastState
Node memory pressure
cgroup memory.current / memory.events（若平台允许）
working set与RSS时间线
/dev/shm类型和sizeLimit
请求并发、body与多模态大小
```

### 7.5 allocator 碎片的判断纪律

看到 “reserved but unallocated” 不应立刻把所有 OOM 归为碎片。先比较：

- total capacity；
- 当前进程 allocated/reserved；
- 其他进程使用；
- OOM 申请大小；
- 发生阶段；
- 请求/shape 是否突然变化；
- 重启后相同固定负载能否复现。

碎片可能是放大因素，但模型与 KV 预算本身超限时，调 allocator 配置不会创造显存。

---

## 8. 一个可审计的 Kubernetes 基线模板

### 8.1 模板目标与非目标

下面模板用于讲清：

- 镜像 digest 与模型 revision 双固定；
- 独立单 GPU 副本；
- 两副本、`maxSurge: 0` 的稀缺 GPU 发布；
- startup/readiness 分工，以及 liveness 只在固定负载验证后的 opt-in 边界；
- API key 从 Secret 注入；
- 每个 Pod 使用独立可写 cache；共享只读模型工件与持久 cache 另行验收；
- 只调度到已验收 GPU 产品/driver/节点池；
- `/dev/shm` 与容器内存的边界；
- 默认只允许 render 或 dry-run，不授权实际变更。

它不是：

- 任何 GPU 上都能直接运行的“黄金参数”；
- 性能基线；
- 完整租户鉴权；
- 跨节点 vLLM 集群；
- 自动创建 Secret 或持久模型 cache 的脚本；
- 对生产执行 apply 的授权。

示例镜像 digest 是 Docker Hub 上 `v0.25.0` 的 amd64 manifest：

```text
sha256:e1c1ff1af9a15921bfa11d1d95047258c1797392cdbfa296e7639da446b23f97
```

部署前必须再次验证目标 Node 是 amd64，镜像标签/label/SBOM、CUDA 用户态与 host driver 兼容。digest 固定镜像工件，不证明其 build commit 就等于 GitHub release tag commit。

### 8.2 基线 YAML

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: vllm-qwen3-06b
  namespace: inference
  labels:
    app.kubernetes.io/name: vllm
    app.kubernetes.io/instance: qwen3-06b
    app.kubernetes.io/version: "0.25.0"
    platform.example.com/change-mode: dry-run-only
spec:
  replicas: 2
  revisionHistoryLimit: 3
  progressDeadlineSeconds: 1800
  strategy:
    type: RollingUpdate
    rollingUpdate:
      maxSurge: 0
      maxUnavailable: 1
  selector:
    matchLabels:
      app.kubernetes.io/name: vllm
      app.kubernetes.io/instance: qwen3-06b
  template:
    metadata:
      labels:
        app.kubernetes.io/name: vllm
        app.kubernetes.io/instance: qwen3-06b
        app.kubernetes.io/version: "0.25.0"
        platform.example.com/model-revision: r9d4bfd9
    spec:
      automountServiceAccountToken: false
      terminationGracePeriodSeconds: 120
      nodeSelector:
        nvidia.com/gpu.product: NVIDIA-A10
        platform.example.com/vllm-baseline: qwen3-06b-v0250
      securityContext:
        seccompProfile:
          type: RuntimeDefault
      topologySpreadConstraints:
        - maxSkew: 1
          minDomains: 2
          topologyKey: kubernetes.io/hostname
          whenUnsatisfiable: DoNotSchedule
          labelSelector:
            matchLabels:
              app.kubernetes.io/name: vllm
              app.kubernetes.io/instance: qwen3-06b
      containers:
        - name: server
          image: vllm/vllm-openai@sha256:e1c1ff1af9a15921bfa11d1d95047258c1797392cdbfa296e7639da446b23f97
          imagePullPolicy: IfNotPresent
          args:
            - Qwen/Qwen3-0.6B
            - --revision
            - 9d4bfd9a94aa5f2ab18d77fa457c306da0b8e439
            - --tokenizer-revision
            - 9d4bfd9a94aa5f2ab18d77fa457c306da0b8e439
            - --served-model-name
            - qwen3-0.6b-r9d4bfd9
            - --host
            - 0.0.0.0
            - --port
            - "8000"
            - --max-model-len
            - "4096"
            - --max-num-seqs
            - "32"
            - --gpu-memory-utilization
            - "0.80"
          env:
            - name: VLLM_API_KEY
              valueFrom:
                secretKeyRef:
                  name: vllm-api-auth
                  key: api-key
            - name: HF_HOME
              value: /root/.cache/huggingface
            - name: VLLM_CACHE_ROOT
              value: /root/.cache/vllm
          ports:
            - name: http
              containerPort: 8000
              protocol: TCP
          startupProbe:
            httpGet:
              path: /health
              port: http
            timeoutSeconds: 2
            periodSeconds: 10
            failureThreshold: 90
          readinessProbe:
            httpGet:
              path: /health
              port: http
            timeoutSeconds: 2
            periodSeconds: 5
            failureThreshold: 3
            successThreshold: 1
          resources:
            requests:
              cpu: "4"
              memory: 12Gi
              ephemeral-storage: 10Gi
            limits:
              cpu: "8"
              memory: 16Gi
              ephemeral-storage: 20Gi
              nvidia.com/gpu: "1"
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            capabilities:
              drop:
                - ALL
          volumeMounts:
            - name: cache
              mountPath: /root/.cache
            - name: tmp
              mountPath: /tmp
            - name: dshm
              mountPath: /dev/shm
      volumes:
        - name: cache
          emptyDir:
            sizeLimit: 10Gi
        - name: tmp
          emptyDir:
            sizeLimit: 2Gi
        - name: dshm
          emptyDir:
            medium: Memory
            sizeLimit: 1Gi
---
apiVersion: v1
kind: Service
metadata:
  name: vllm-qwen3-06b
  namespace: inference
spec:
  selector:
    app.kubernetes.io/name: vllm
    app.kubernetes.io/instance: qwen3-06b
  ports:
    - name: http
      port: 8000
      targetPort: http
---
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: vllm-qwen3-06b
  namespace: inference
spec:
  minAvailable: 1
  selector:
    matchLabels:
      app.kubernetes.io/name: vllm
      app.kubernetes.io/instance: qwen3-06b
```

### 8.3 模板逐项解释

#### 镜像和模型是两个供应链对象

固定镜像 digest 只固定 server 工件。模型与 tokenizer 还要分别固定：

```text
image digest
model ID + model revision
tokenizer revision
可选 code revision
chat template / generation config
```

模板没有 `--trust-remote-code`。公开模型不需要 HF token，因此也没有无意义地挂载一个长效仓库凭证。

#### Service 只能作为内部示例

模板中的 ClusterIP Service 和 `VLLM_API_KEY` 不能构成生产暴露方案。`VLLM_API_KEY` 只保护固定版本中的一部分路由，同一 server 仍有未受它保护的 inference、operational 和 health 类路由。

片段没有创建 NetworkPolicy 或 gateway policy，是因为 namespace label、gateway identity 和允许路由属于现场配置，而不是因为它们可选。上线前置条件：

```text
vLLM Service不直接对外
  + gateway只转发批准的endpoint allowlist
  + NetworkPolicy只允许gateway/monitoring/必要管理来源
  + 租户认证、授权、配额和限流在gateway
```

任一项未验证，模板只能留在隔离测试环境。

#### GPU 产品与已验收节点池不能漂移

模板的两个 `nodeSelector` 是有意设置的“默认不通用”护栏：

- `nvidia.com/gpu.product: NVIDIA-A10` 固定本示例的 GPU 产品；
- `platform.example.com/vllm-baseline: qwen3-06b-v0250` 表示平台已验收池，该组织标签默认不会天然存在。

实际集群必须先只读查看 GFD/平台标签、GPU 显存、driver、taint 和 NodePool，再把 selector 改成自己的已验收值。不可照抄示例标签，也不可删除约束后让同一性能基线漂移到任意 GPU。

如果换 GPU 产品/driver/池，显存 profile、启动分位数、吞吐和 SLO 全部重测。

#### 为什么 writable cache 不共享 PVC

模板改用每 Pod 独立 `emptyDir`：

- 不会让两个副本并发写同一 Hugging Face/compile cache；
- 不会让跨 Node Pod 争用单个 RWO volume；
- 代价是 Pod 重建后 cache miss，startup budget 必须覆盖下载和编译；
- 10 GiB 只适合本示例小模型，仍受 20 GiB ephemeral-storage limit 约束。

生产大模型可选：

| 方案 | 模型工件 | compile/runtime cache | 前置验收 |
|---|---|---|---|
| 预制只读 snapshot/镜像/CSI | 多 Pod 只读共享、revision 固定 | 每 Pod 独立可写 | 工件完整性、mount topology |
| 每 Pod RWO PVC | 每 Pod 独立 | 同一 Pod 独立 | StorageClass、回收、容量、冷启动 |
| RWX cache | 可共享 | 最好仍把 compile/tmp 分离 | CSI 一致性、锁、并发 writer、性能 |

把同一个 RWO claim 挂给跨 Node 两副本可能出现 Multi-Attach；换成 RWX 也不会自动解决 Hugging Face lock、半写 shard 或 compile cache 并发风险。共享模型工件应优先只读，per-Pod compile/tmp 应独立可写。

#### 为什么示例显存比例是 `0.80`

`0.80` 只是在小模型基线中显式展示“留 headroom 并测量”的原则，不是推荐默认。真正上线要由：

- 目标 GPU；
- 模型和 dtype/quant；
- max model len；
- max sequences/token budget；
- graph/compile 配置；
- workload 分布；
- 同卡进程；
- 容错余量

共同决定。读者不得把 `0.80` 复制成大模型生产标准。

#### 为什么不默认启用 per-request metrics

`--enable-per-request-metrics` 会增加请求级 timing 处理，官方文档明确提示高并发下 CPU overhead 可能不可忽略。基线先保留 server aggregate metrics；在受控压测证明开销可接受后，再通过独立 overlay 开启。

#### 为什么 `maxSurge: 0`

两副本每个申请一张 GPU：

```text
maxSurge=1
  -> rollout峰值需要3张可放置GPU

maxSurge=0,maxUnavailable=1
  -> controller先减少1个旧副本
  -> 旧Pod真正终止并释放GPU后，新Pod才能稳定拿卡
  -> 终止未完成时，新Pod仍可能Pending等待GPU
  -> 发布期间容量降到1副本
```

这不是免费选择。必须在发布前确认单副本能承载发布窗口流量，必要时先预留额外 GPU 或调低入口配额。PDB 主要约束 eviction，不替代 Deployment 的 rollingUpdate 策略。

#### `topologySpreadConstraints` 可能让第二副本 Pending

示例用 hostname topology、`maxSkew: 1`、`minDomains: 2` 和 `DoNotSchedule`，目的是让两个副本避免落在同一 Node 故障域。`minDomains: 2` 才把“至少两个合格 hostname 域”变成明确约束；只写 `maxSkew` 和 `DoNotSchedule` 不能等价地保证跨 Node。目标集群还必须支持该字段。

它要求 scheduler 能找到至少两个满足 GPU 资源、label/affinity、taint/toleration、storage 等全部条件的可用 hostname 拓扑域。

如果集群只有一个合格 GPU Node/单一可用域，第二副本可能长期 `Pending`。这不是 vLLM 故障，而是反亲和式可用性约束在生效。

现场选择：

| 选择 | 调度结果 | 接受的风险 |
|---|---|---|
| 保持 `DoNotSchedule` | 不满足跨 Node 分布就 Pending | 容量暂时不足，但不伪造跨故障域 HA |
| 改 `ScheduleAnyway` | 尽量 spread，必要时同 Node | Node 故障可能同时损失两个副本 |
| 去除约束 | scheduler 自由放置 | 不保证副本故障域 |

必须由 SLO 和 GPU Node 供给决定，不能把示例约束当通用模板。

#### 为什么基线不默认配置 liveness

startup 成功后，`/health` 请求仍经过 server/event loop。若 CPU 瞬时饥饿、GC/调度停顿或高负载让连续 2 秒 timeout，激进 liveness 会重启一个仍可恢复的实例，随后付出完整模型冷启动并把流量压到剩余副本。

因此模板默认只有 startup/readiness。只有在固定模型、CPU limit、目标/过载 workload 下证明：

- health failure 能可靠代表“必须重启才能恢复”；
- timeout/threshold 不会被合法尾延迟触发；
- 重启比等待恢复更快；
- 冷启动期间剩余容量足够

后，才通过受审 overlay 添加 liveness。不能直接复制 `timeoutSeconds: 2, failureThreshold: 3`。

#### `progressDeadlineSeconds` 必须覆盖合法冷启动

模板的 startup probe 粗略失败窗口是：

```text
90 × 10 seconds ≈ 900 seconds
```

Deployment 默认 `progressDeadlineSeconds` 通常是 600 秒。如果保留默认值，controller 可能在合法 startup budget 尚未结束时就把 rollout 标为 `ProgressDeadlineExceeded`。因此模板显式给出 `1800` 作为演示值；它不是通用推荐，必须由下列实测阶段派生：

```text
调度与GPU等待
+ image pull
+ storage/cache准备
+ 模型cache miss高分位
+ profile/KV/compile/warm-up
+ minReadySeconds（若配置）
+ controller观测与合理抖动
```

这些阶段可能重叠，实际应从 rollout 时间线取保守高分位，而不是机械相加。还要确保 deadline 大于 `minReadySeconds`。

Deployment 超过期限只会在 status condition 写入 `Progressing=False, reason=ProgressDeadlineExceeded`；它不会自动回滚。GitOps/企业发布平台（或具体现场控制器）可能读取该 condition、判定失败并执行自己的中止/回退策略，所以必须按现场控制器的真实行为，把 Pod probe、Deployment deadline 与上层发布超时一起设计。

#### 为什么没有 `preStop: sleep`

固定睡眠不等于排空。安全发布需要：

1. endpoint 摘除/网关停止送新请求；
2. 允许已接收流式请求在限定时间完成；
3. 超过产品上限的请求被取消并可观察；
4. SIGTERM 与 server shutdown 行为在固定版本验证；
5. `terminationGracePeriodSeconds` 覆盖允许的最长排空时间；
6. 客户端重试有幂等/重复 token 语义。

未验证这些之前，加入 `sleep 20` 只是在隐藏竞态。

#### `readOnlyRootFilesystem` 需要验证

模板把已知写目录挂为 volume，但第三方库、compile backend 或驱动工具仍可能尝试写其他路径。server 必须在同一 digest 上先做只读根文件系统 smoke；若失败，应该定位并显式挂载最小写目录，而不是直接把整个 rootfs 改回可写。

### 8.4 只允许 render/dry-run 的操作边界

本课不授权执行以下真实变更：

```text
kubectl apply
kubectl patch
kubectl rollout restart
kubectl scale
helm upgrade
创建或修改Secret/PVC/NetworkPolicy
对生产发送压测流量
```

安全的默认验证顺序：

```text
1. 保存模板到变更分支
2. kubeconform/kubeval或API schema校验
3. kubectl apply --dry-run=client -o yaml
4. 在已授权测试集群执行--dry-run=server
5. 检查diff、GPU峰值需求、Secret/PVC前置条件
6. 变更审批
7. 才允许真实apply
```

示意命令故意放在 `text` 而不是可直接执行的 shell fence：

```text
只读/本地渲染：
kubectl --context vllm-lab-shanghai apply --dry-run=client -f .\vllm-qwen3-06b.yaml -o yaml

需要测试集群API访问但不持久化：
kubectl --context vllm-lab-shanghai apply --dry-run=server -f .\vllm-qwen3-06b.yaml -o yaml

真实apply：
必须有明确变更审批、目标context/namespace确认、容量与回滚检查，本课不授权。
```

---

## 9. 生产指标：先分 Counter、Gauge、Histogram

### 9.1 固定版本的核心指标清单

`v0.25.0` 官方 Production Metrics 与 `vllm/v1/metrics/loggers.py` 中，本课必须掌握：

| 文档基名 | 类型 | 主要含义 | 正确问题 |
|---|---|---|---|
| `vllm:num_preemptions` | Counter | scheduler preemption 累计 | 最近 5 分钟每秒/每分钟增加多少 |
| `vllm:request_success` | Counter | 按 `finished_reason` 记录完成事件；历史名字容易误导 | 各结束原因速率，不是天然“成功数”或全部 HTTP 请求 |
| `vllm:prompt_tokens` | Counter | 已处理 prompt token 累计 | 输入 token/s |
| `vllm:generation_tokens` | Counter | 已生成 token 累计 | 输出 token/s |
| `vllm:kv_cache_usage_perc` | Gauge | KV cache 使用比例；`1` 代表 100% | 当前/窗口最大压力 |
| `vllm:num_requests_running` | Gauge | 当前 running 请求数 | 当前并发执行水平 |
| `vllm:num_requests_waiting` | Gauge | 当前 waiting 请求数 | 排队是否形成 |
| `vllm:num_requests_waiting_by_reason` | Gauge | waiting 按 `capacity` / `deferred` 拆分 | 是容量不足还是暂时约束 |
| `vllm:e2e_request_latency_seconds` | Histogram | 服务端请求 E2E 分布 | p50/p95/p99，必须认清测量边界 |
| `vllm:time_to_first_token_seconds` | Histogram | 首 token 时间分布 | 服务端 TTFT 尾部 |
| `vllm:inter_token_latency_seconds` | Histogram | token 间隔分布 | 生成流畅度 |
| `vllm:request_queue_time_seconds` | Histogram | queue time 分布 | 排队尾部 |
| `vllm:request_prefill_time_seconds` | Histogram | prefill time 分布 | prompt 计算成本 |
| `vllm:request_decode_time_seconds` | Histogram | decode time 分布 | 生成阶段总耗时 |
| `vllm:request_inference_time_seconds` | Histogram | inference 时间分布 | 引擎计算时间 |
| `vllm:request_time_per_output_token_seconds` | Histogram | 每输出 token 的请求级时间 | 不应不加定义地叫 ITL |
| `vllm:request_prompt_tokens` | Histogram | 每请求 prompt token 分布 | 流量输入长度画像 |
| `vllm:request_generation_tokens` | Histogram | 每请求生成 token 分布 | 输出长度画像 |

固定入口：

- [Production Metrics v0.25.0](https://docs.vllm.ai/en/v0.25.0/usage/metrics/)
- [loggers.py @ v0.25.0](https://github.com/vllm-project/vllm/blob/v0.25.0/vllm/v1/metrics/loggers.py)

### 9.2 文档基名与实际 exposition 名

Python Prometheus client 的 `Counter` 通常会在 exposition 中增加 `_total`。因此：

| 官方文档/构造器基名 | 实际 `/metrics` 常见名字 |
|---|---|
| `vllm:num_preemptions` | `vllm:num_preemptions_total` |
| `vllm:request_success` | `vllm:request_success_total` |
| `vllm:prompt_tokens` | `vllm:prompt_tokens_total` |
| `vllm:generation_tokens` | `vllm:generation_tokens_total` |

不要凭表直接写告警。上线时必须保存一份固定实例的原始 `/metrics` 样本并核对：

```text
metric name
type/help
labels
unit
是否有_total/_bucket/_sum/_count
实例启动后何时出现
无流量时是否存在
```

Prometheus metric name 允许冒号，所以 `vllm:...` 可以直接用于 PromQL。adapter/KEDA/外部指标系统若不支持冒号，应在 recording rule 或适配层提供稳定的低基数名字，而不是随手改 dashboard。

### 9.3 Counter：只能看区间增量

错误：

```promql
vllm:generation_tokens_total
```

上式只显示进程自启动以来累计值；Pod 重启会归零。正确的输出 token/s：

```promql
sum by (model_name) (
  rate(vllm:generation_tokens_total[5m])
)
```

最近 15 分钟 preemption 次数：

```promql
sum by (model_name) (
  increase(vllm:num_preemptions_total[15m])
)
```

所有完成原因速率：

```promql
sum by (model_name, finished_reason) (
  rate(vllm:request_success_total[5m])
)
```

`request_success` 这个历史名字并不只描述成功完成，也不能单独构造：

```text
错误率 = 1 - request_success
```

上面的公式错误有两层：

1. `request_success` 源码会对 `stop`、`length`、`abort`、`error`、`repetition` 等 FinishReason 计数；名字不能解释成成功。
2. 它缺少所有 eligible 请求、网关拒绝、认证失败、连接中断、server 5xx 等统一分母。

如果产品明确把 `stop` 和 `length` 都视为正常完成，才可以建立一个内部“正常结束速率”辅助查询：

```promql
sum by (model_name) (
  rate(vllm:request_success_total{
    finished_reason=~"stop|length"
  }[5m])
)
```

`abort`、`error`、`repetition` 必须单列，`length` 是否算产品成功也要由 API 契约决定。可用性总分母仍来自 gateway/client；这个 Counter 只做 engine completion breakdown。

### 9.4 Gauge：看当前值，也看窗口持续性

当前 waiting：

```promql
sum by (model_name) (
  vllm:num_requests_waiting
)
```

按原因拆分：

```promql
sum by (model_name, reason) (
  vllm:num_requests_waiting_by_reason
)
```

`v0.25.0` 中：

- `reason="capacity"`：受 scheduler capacity 限制；
- `reason="deferred"`：LoRA budget、KV transfer 或其他瞬态约束导致推迟；
- 两个 reason 的和应与总 waiting 对齐。

只看总 queue 会把两种处置混在一起。capacity 持续升高更像需要降载/扩容/调优；deferred 持续升高要先查对应特性和传输/预算，未必增加副本就能解决。

KV 使用的 5 分钟最大值：

```promql
max by (model_name, pod) (
  max_over_time(vllm:kv_cache_usage_perc[5m])
)
```

KV ratio 高不是故障本身。只有与 queue、preemption、TTFT/ITL 和请求长度一起，才能判断是有效高利用还是容量风险。

### 9.5 Histogram：分位数必须从 bucket 重建

TTFT p99：

```promql
histogram_quantile(
  0.99,
  sum by (le, model_name) (
    rate(vllm:time_to_first_token_seconds_bucket[5m])
  )
)
```

E2E p95：

```promql
histogram_quantile(
  0.95,
  sum by (le, model_name) (
    rate(vllm:e2e_request_latency_seconds_bucket[5m])
  )
)
```

queue p99：

```promql
histogram_quantile(
  0.99,
  sum by (le, model_name) (
    rate(vllm:request_queue_time_seconds_bucket[5m])
  )
)
```

三条纪律：

1. 聚合时保留 `le`。
2. 不同 bucket layout 不得盲目合并；版本升级先比 schema。
3. `histogram_quantile` 是桶内插值，尾部精度受 bucket 边界影响；SLO 阈值附近必须确认有合适 bucket。

平均值：

```promql
sum(rate(vllm:e2e_request_latency_seconds_sum[5m]))
/
sum(rate(vllm:e2e_request_latency_seconds_count[5m]))
```

只能回答平均耗时，不能替代 p99。少量极慢请求可能被平均数完全掩盖。

### 9.6 “没有指标”不等于零

缺 series 的可能原因：

- Prometheus 没 scrape 到 Pod；
- `/metrics` 被 NetworkPolicy、ServiceMonitor、TLS 或 RBAC 挡住；
- label selector 选错；
- Pod 尚未 Ready/尚未产生该指标；
- fixed version 与 dashboard name 不一致；
- Counter 实际带 `_total`；
- 进程重启；
- metric 被版本弃用/重命名；
- 查询聚合丢掉了 label。

显式检查缺失：

```promql
absent(vllm:num_requests_waiting)
```

清点实际目标：

```promql
count by (namespace, pod) (
  up{job="vllm"}
)
```

不要无条件在所有告警后加 `or vector(0)`。它会把“采集坏了”伪装成“业务为零”，还可能丢失 model/pod 标签。

---

## 10. 从指标到 SLO：TTFT、ITL、TPOT、E2E 与吞吐

### 10.1 五个时间边界不能混

以 streaming request 为例：

```text
client send
  -> gateway receive/auth/rate-limit
  -> API server parse/render/tokenize
  -> request queued
  -> scheduled
  -> prefill
  -> first output token
  -> decode token gaps
  -> final token
  -> stream flush/client receive
```

| 指标 | 推荐边界 | 主要体验 |
|---|---|---|
| client-observed TTFT | client 发出到收到首 token | 用户“多久看到第一字” |
| queue time | engine 接收/入队到被调度 | 容量等待 |
| server TTFT | 必须按固定版本定义核对 | server 内部首 token 时间 |
| ITL | 相邻输出 token 之间 | 流式是否卡顿 |
| TPOT | 请求级每输出 token 时间的约定公式 | decode 速度概括 |
| E2E | 请求开始到最终 token/响应完成 | 总体验 |

常用请求级 TPOT 近似：

```text
TPOT
  = (E2E - TTFT) / (output_tokens - 1)
  仅对output_tokens >= 2有定义
```

但它不是每一个 token 间隔的分布。一个请求可能平均 TPOT 正常，却在中间卡住数秒；ITL 才能暴露这种不平滑。

不要把不同观测点的数值直接相加。client TTFT 可能包含网络、gateway、tokenization、queue、prefill 和 stream flush；server 某个 timing 可能只从 scheduled 开始。

### 10.2 v0.25.0 per-request metrics

`v0.25.0` 可通过：

```text
--enable-per-request-metrics
```

在 OpenAI-compatible response 的顶层扩展字段 `metrics` 中返回请求级 timing；它与 `usage` 同级，Python client 通常从 `response.model_extra.get("metrics")` 读取。字段：

| 字段 | v0.25.0 语义 |
|---|---|
| `time_to_first_token_ms` | 从 scheduled 到 first output；不含 queue |
| `generation_time_ms` | first output 到 last output；不含 queue，也不含 prefill/TTFT |
| `queue_time_ms` | 请求等待被调度的时间 |
| `mean_itl_ms` | 平均 inter-token latency；只生成 1 token 时为 null |
| `tokens_per_second` | 从 scheduled 到 last output 的生成 token/s；包含 prefill 时间 |

这些字段在 timing 不可用时都可能为 null。

固定官方页：

- [Per-Request Metrics v0.25.0](https://docs.vllm.ai/en/v0.25.0/features/per_request_metrics/)

#### 重要限制

1. 高并发下 CPU overhead 可能不可忽略，必须 A/B benchmark。
2. `n > 1` 时 request-level metrics object 被抑制为 null，但 token usage 仍准确。
3. Completions API 一次包含多个 prompts 时也不会提供单一请求 timing。
4. streaming 只在最终 usage chunk 同一响应对象的顶层扩展字段附带 metrics，client 通常从 `chunk.model_extra.get("metrics")` 读取。
5. client 要设置 `stream_options.include_usage: true`，或 server 显式启用强制 include usage。
6. 它依赖 server log stats，不能与 `--disable-log-stats` 组合。

示例响应结构仅展示字段，不承诺数值：

```json
{
  "usage": {
    "prompt_tokens": 128,
    "completion_tokens": 64,
    "total_tokens": 192
  },
  "metrics": {
    "time_to_first_token_ms": 84.2,
    "generation_time_ms": 511.7,
    "queue_time_ms": 12.1,
    "mean_itl_ms": 8.1,
    "tokens_per_second": 107.4
  }
}
```

请求级指标适合：

- 受控压测输出；
- 客户端/网关按安全维度分桶；
- 单次慢请求调查；
- 对比新旧版本。

它不应被直接打上 request ID、user ID、prompt hash 等高基数标签送入 Prometheus。

### 10.3 server aggregate 与 client SLI 各自回答什么

| 数据源 | 优点 | 盲区 |
|---|---|---|
| vLLM aggregate histograms | 低基数、直接看 engine 行为 | 不含完整 gateway/client 网络；长度分桶有限 |
| per-request metrics | 可与该响应 token usage 关联 | opt-in/CPU 成本/部分场景为 null |
| Gateway metrics | 完整请求分母、HTTP/租户级策略 | 不知道内部 queue/KV/preemption |
| Client telemetry | 最接近用户体验 | 采样、SDK、网络环境差异 |
| Offline benchmark | workload 可重复、可做长度分桶 | 不等于生产真实分布 |

最佳实践不是选一个，而是关联：

```text
client/gateway SLI发现违约
  -> server histogram确认TTFT/ITL/E2E阶段
  -> queue/running/KV/preemption解释容量
  -> CPU/GPU/DCGM/NCCL/Node证据定位资源
```

### 10.4 在线推理的 SLI 定义

#### 可用性

```text
Availability
  = eligible requests that complete successfully
    / all eligible requests
```

必须先定义：

- 认证失败是否 eligible；
- 用户参数非法是否排除；
- 网关限流是否算失败；
- client cancel 是否算失败；
- streaming 收到首 token 但中途断开是否成功；
- server stop/length 完成原因怎样计；
- 重试后的成功是否掩盖首请求失败。

#### TTFT SLI

```text
good_TTFT
  = successful eligible requests
    whose client-observed TTFT <= class budget
```

按这些维度分层：

- model/revision；
- endpoint/task；
- streaming/non-streaming；
- prompt length bucket；
- priority/tenant tier；
- cache hit/miss（若可靠可观测）；
- region/cluster。

#### decode 流畅度

可以同时定义：

- ITL p95/p99；
- “任一 ITL 超阈值的请求比例”；
- request-level TPOT p99；
- streaming 中断率。

只报 aggregate output token/s 无法证明交互体验。

#### E2E

E2E 强依赖输出长度，所以至少按 output token bucket 分层。把 1 token 分类请求与 2000 token 生成请求混成一个 p99，没有可行动意义。

### 10.5 不给所有模型一个万能阈值

本课不写“TTFT p99 必须 1 秒”之类的万能数值。正确过程：

1. 产品定义交互/批处理等级和容忍度。
2. 固定模型、硬件、请求长度分布。
3. 测空载基线、目标负载和过载拐点。
4. 选择用户可感知且系统可守的 objective。
5. 留出发布、节点故障、流量增长和模型变化余量。
6. 把阈值写入版本化 SLO 文档与 recording rules。

形式化表达：

```text
For each (model_revision, traffic_class, prompt_bucket, output_bucket):
  availability >= approved target
  client_TTFT_p99 <= approved budget
  ITL_p99 <= approved budget
  E2E_p99 <= approved budget
```

### 10.6 Error budget 与 burn rate

```text
error_budget_fraction = 1 - SLO_target

burn_rate
  = observed_bad_event_fraction
    / error_budget_fraction
```

例如 target、窗口和告警阈值都必须由产品/平台审批。本课只规定：

- 快窗口发现剧烈故障；
- 慢窗口发现持续退化；
- availability 与 latency bad events 分开；
- 发布标签进入告警上下文；
- 低流量时设置最小事件数，避免一个请求制造假 p99。

### 10.7 throughput 的四种口径

| 口径 | 公式 | 容易被什么误导 |
|---|---|---|
| request/s | completed requests / second | 请求长度差异 |
| prompt token/s | prompt tokens / second | 不代表生成能力 |
| output token/s | generated tokens / second | 可能牺牲单请求延迟 |
| per-request token/s | 某请求 tokens / elapsed | 并发与调度公平性 |

一次调优后：

```text
aggregate output token/s +25%
TTFT p99 +80%
ITL p99 +40%
```

对离线批处理可能是胜利，对交互聊天可能是事故。优化目标必须由服务等级决定。

### 10.8 “GPU util 低但 TTFT 高”的故障矩阵

| 组合 | 可能解释 | 先查 |
|---|---|---|
| GPU util 低、queue 高、CPU 高 | tokenizer/API CPU 饥饿 | CPU throttle、event loop、prompt 长度 |
| GPU util 低、queue 高、preemption 高 | KV/admission 约束，GPU 采样未覆盖 burst | KV、waiting reason、采样粒度 |
| GPU util 低、queue 低、TTFT 高 | 网络/stream flush/小而稀疏流量 | client/gateway 分段 timing |
| GPU util 低、TP 慢 | NCCL/fabric/同步等待 | rank logs、NCCL、topology |
| GPU util 低、Pod 频繁重启 | probe/oom/hardware | restart/lastState/events |
| GPU util 高、queue 低、SLO 正常 | 有效高利用 | 保持 headroom 与故障余量 |
| GPU util 高、queue/TTFT 同升 | 到达容量拐点 | admission、扩容、length buckets |

GPU utilization 是采样后的忙碌比例，不是“模型效率”的完整定义。短 kernel、同步等待、CPU gap 和采样窗口都能让它失真。

---

## 11. 扩缩容与发布：GPU 稀缺资源下不能照搬普通 Deployment

### 11.1 先测“单个 Ready 副本能做多少”

扩容公式需要一个固定容量单位。至少按下列 workload class 建基线：

| 类别 | prompt tokens | output tokens | arrival pattern | 必测结果 |
|---|---:|---:|---|---|
| short interactive | 固定分布 | 固定分布 | Poisson/生产回放 | TTFT/ITL/E2E、token/s |
| long prompt | 固定长分布 | 中等 | 同上 | prefill、TTFT、KV |
| long generation | 中等 | 固定长分布 | 同上 | ITL/TPOT、KV |
| burst | 生产 burst envelope | 生产分布 | 突发 | queue 恢复时间 |

每个 capacity point 必须记录：

```text
model image/revision
GPU/driver/node class
CPU/memory/storage
max_model_len/max_num_seqs/token budget
parallel config
request distribution
arrival rate
output token/s
TTFT/ITL/E2E p50/p95/p99
waiting/running/KV/preemption
error rate
```

容量不是“GPU util 到 90%”一个数字。真正的拐点是：

```text
在目标workload下
  随arrival rate上升
  queue开始持续增长
  或任一SLO先违约
```

### 11.2 queue 为什么适合作为扩容前导信号

Little's Law 的稳态近似：

```text
L ≈ λ × W

L: 系统内平均请求数
λ: 到达/完成速率
W: 平均停留时间
```

当 arrival 超过可服务速率，waiting 会在 latency 全面爆炸前增长。因此 queue 可以作为前导信号。但它有局限：

- 瞬时 burst 不一定需要新 GPU；
- 模型冷启动可能比 burst 更长；
- deferred queue 未必是副本不足；
- 客户端重试会放大 queue；
- queue 为零可能是 server 已死或 scrape 缺失；
- 不同 prompt/output 长度对应不同工作量。

正确设计是：

```text
capacity waiting持续
  + arrival/token rate
  + Ready副本数
  + SLO guardrail
  + GPU Node可供给性
```

### 11.3 一个低基数的 scaler 指标

scaler 的 numerator 和 denominator 必须精确指向同一组 `qwen3-06b` Pod。示例查询：

```promql
sum(
  vllm:num_requests_waiting_by_reason{
    namespace="inference",
    pod=~"vllm-qwen3-06b-.*",
    model_name="qwen3-0.6b-r9d4bfd9",
    reason="capacity"
  }
)
/
clamp_min(
  sum(
    kube_pod_status_ready{
      namespace="inference",
      condition="true",
      pod=~"vllm-qwen3-06b-.*"
    } == 1
  ),
  1
)
```

不能用未过滤的全局 waiting 作 numerator，也不能用裸 `count(kube_pod_status_ready{...})` 作 denominator：kube-state-metrics 通常同时暴露值为 `0` 和 `1` 的 condition series，`count` 会把 `Ready=0` 也计入；`sum(... == 1)` 才计 Ready Pod。

`model_name` 是 vLLM logger 的业务标签；`namespace`、`pod` 通常是 Prometheus Kubernetes service discovery/relabel 添加的 scrape target labels，并非 vLLM 原生 metric 永远保证。如果 raw series 没有它们，不能为了让查询“有数”而删掉 scope。应先：

1. 在 scrape/relabel 层稳定附加 namespace 与 Pod identity；
2. 核对 `model_name=qwen3-0.6b-r9d4bfd9` 与 Deployment 的 served model；
3. 再用 recording rule 映射成固定 workload series；
4. 最后把低基数结果交给 HPA adapter/KEDA。

将其发布为不含冒号的稳定 external metric，例如：

```text
vllm_capacity_waiting_per_ready_replica
```

零 Ready 告警必须限定 Deployment 期望副本大于 0，避免计划 scale-to-zero 时误报：

```promql
(
  sum(
    kube_pod_status_ready{
      namespace="inference",
      condition="true",
      pod=~"vllm-qwen3-06b-.*"
    } == 1
  ) == 0
)
and on()
(
  max(
    kube_deployment_spec_replicas{
      namespace="inference",
      deployment="vllm-qwen3-06b"
    }
  ) > 0
)
```

`clamp_min(...,1)` 只防止 scaler 除零，不得把零 Ready 解释成健康。

missing 也不能解释成零。至少单独建立 target 与 metric 两类采集健康告警；`job` 名称按本集群 scrape 配置固定：

```promql
absent(
  up{
    job="vllm",
    namespace="inference",
    pod=~"vllm-qwen3-06b-.*"
  }
)
and on()
(
  max(
    kube_deployment_spec_replicas{
      namespace="inference",
      deployment="vllm-qwen3-06b"
    }
  ) > 0
)
```

```promql
absent(
  vllm:num_requests_waiting_by_reason{
    namespace="inference",
    pod=~"vllm-qwen3-06b-.*",
    model_name="qwen3-0.6b-r9d4bfd9",
    reason="capacity"
  }
)
and on()
(
  max(
    kube_deployment_spec_replicas{
      namespace="inference",
      deployment="vllm-qwen3-06b"
    }
  ) > 0
)
```

还要告警 `up == 0` 的现存 target。`absent`、`up` 与业务 gauge 分开，才能区分“真的没有排队”和“采集链不存在/失败”。

标准 HPA 使用 Prometheus 指标通常需要 custom/external metrics adapter；KEDA Prometheus scaler 则执行查询并把标量用于触发。无论用哪一个，先验证：

- 查询在 no traffic/no series/Pod restart 时的值；
- label 不会跨模型聚合；
- adapter 对冒号和单位的处理；
- metrics lag；
- 多个 scaler 的合并规则；
- scale-up/scale-down 边界；
- 控制器失效时的 fallback。

### 11.4 扩容与缩容要用不同节奏

#### Scale up

扩容需要：

```text
Node已有空闲GPU:
  schedule + image/cache + model load + profile + warm-up + readiness

Node不存在:
  node provision + driver/operator/device plugin ready
  + 上述全部冷启动
```

所以 scale-up 不是即时反馈。配置：

- 快速但有上限的 scale-up；
- 合理 `maxReplicas`，不超过 GPU 配额/节点池能力；
- 提前量而不是等 p99 已烧穿；
- min replicas 覆盖稳定基础流量；
- GPU 节点池 autoscaler 的额外分钟级延迟；
- admission/backpressure 保护冷启动窗口。

#### Scale down

缩容更危险：

- 流式请求可能持续很久；
- endpoint 摘除不保证已建立连接立即迁移；
- 删 Pod 会丢失其 KV cache；
- scale-in 后剩余副本 queue 可能瞬间上升；
- 频繁缩放会反复加载权重和编译。

需要：

- scale-down stabilization window；
- cooldown 大于短 burst；
- 明确 drain timeout；
- readiness 与连接排空协作；
- 最小副本；
- 把长请求最大时长纳入 termination grace；
- 对取消请求有可观察结果。

### 11.5 为什么不建议交互服务直接 scale-to-zero

scale-to-zero 的首请求可能承担：

```text
GPU Node启动
+ driver/operand就绪
+ image pull
+ 模型下载
+ 权重加载
+ profile/KV
+ compile/warm-up
+ readiness
```

这通常不是交互式 TTFT 能接受的“冷启动”。只有当产品明确接受异步排队或分钟级等待、并有 durable queue/超时语义时，才评估 scale-to-zero。

### 11.6 RollingUpdate 的 GPU 峰值公式

设：

- `R`：期望副本；
- `G`：每 Pod GPU 数；
- `S`：`maxSurge` 换算后的 Pod 数；
- `U`：`maxUnavailable` 换算后的 Pod 数；
- `T`：已经有 `deletionTimestamp`、但尚未真正释放 GPU 的 terminating Pod 数。

`R + S` 是 Deployment controller 对非 terminating 活动副本的计划上界/基础，不等于任意瞬间实际占卡 Pod 的严格上界：

```text
non_terminating_active <= R + S

GPU_controller_plan_upper ≈ (R + S) × G
```

终止中的旧 Pod 可能仍在执行 preStop、排空长 stream、等待 grace period 或等待 runtime 完成 teardown；只要设备尚未释放，它仍占 GPU。实际瞬时占用上界应把这部分加入：

```text
GPU_instantaneous_held ≈ (R + S + T) × G

T = terminating Pods that still hold their assigned GPU
```

可用容量下界近似：

```text
ready_old_and_new >= R - U
```

Kubernetes 对百分比有取整规则；变更评审必须看 controller 最终换算值，不凭心算。取证时检查 Pod `deletionTimestamp`、container/runtime 终止完成时间、Node 上设备是否真正释放；在当前 API/feature gate 暴露时，也查看 Deployment `status.terminatingReplicas`，但不能只凭 ReplicaSet desired 数认为 GPU 已空闲。

即使 `maxSurge=0`，controller 也可能先标记旧 Pod 删除并创建/推进新 Pod，而旧 Pod 尚在 terminating 且仍持卡；新 Pod 会因 `Insufficient nvidia.com/gpu` 暂时 Pending，直到旧设备真正释放。这是 GPU 生命周期延迟，不等于 scheduler 错误。

#### 典型死锁

```text
2 replicas × 1 GPU
集群恰好2张可放置GPU
maxSurge=1
maxUnavailable=0

新Pod:
  Pending: Insufficient nvidia.com/gpu

旧Pod:
  不允许删除，因为maxUnavailable=0

结果:
  rollout永久卡住
```

#### 两种选择

| 策略 | GPU 峰值 | 发布容量 | 风险 |
|---|---:|---:|---|
| 预留 surge GPU，`maxSurge>0` | 高 | 可保持 | 成本/配额/放置 |
| `maxSurge=0,maxUnavailable=1` | 不增加 | 暂时下降 | 单副本过载、可用性余量下降 |

没有第三种魔法能同时做到“零额外 GPU、零容量下降、零风险”。

### 11.7 Canary、blue/green 与模型版本

推理发布的变更单元应是：

```text
server image digest
+ model revision
+ tokenizer revision
+ chat template/generation config
+ engine args
+ driver/CUDA/GPU class
+ traffic policy
```

只写“vLLM 从 0.24 升到 0.25”不够。

#### Canary

一个 GPU canary 适合验证：

- 启动阶段；
- memory profile/KV capacity；
- 固定 smoke；
- 低比例真实 workload；
- TTFT/ITL/E2E；
- 输出质量/格式；
- error/oom/preemption；
- API compatibility。

但单 canary 的 cache、流量和并发与全量不同，不能仅凭“canary 低流量很快”证明全量容量。

#### Blue/green

blue/green 可以让新旧模型完整并存并快速切流，但 GPU 峰值接近双倍，模型 cache、PVC、网关路由和长连接切换也要双份规划。

### 11.8 发布门禁

```text
Gate 0 供应链:
  digest/revision/SBOM/signature/漏洞与许可证

Gate 1 render:
  schema、dry-run、diff、Secret/PVC、GPU峰值

Gate 2 cold start:
  cache miss启动、probe窗口、只读rootfs、权限

Gate 3 engine:
  /health、固定模型清单、固定smoke

Gate 4 performance:
  workload buckets、TTFT/ITL/E2E、token/s、queue/KV

Gate 5 canary:
  真实小流量、质量与SLO、无新错误

Gate 6 rollout:
  GPU放置、容量余量、drain、progress

Gate 7 rollback:
  旧digest/revision/cache仍可用，回退时间已测
```

`/health` 只属于 Gate 3 的一小部分，不是所有门禁。

### 11.9 回滚也需要 GPU 与 cache

回滚失败的常见原因：

- 旧镜像已被 registry policy 清理；
- 旧模型 revision 没有本地 cache，重新下载超时；
- schema/chat template 已改变；
- rollback 仍被 `maxSurge` 卡住；
- GPU driver/CUDA 已变化，旧工件不再兼容；
- autoscaler 正在同时改变副本；
- 新旧 model served name 让路由/指标混淆；
- PVC 被新版本写入不兼容 cache。

回滚演练必须测“从当前真实状态回去”，不能只保留一个 Deployment revision 数字。

---

## 12. TP、PP、DP：先问要解决哪一个问题

### 12.1 决策表

| 问题 | 首选起点 | 原因 |
|---|---|---|
| 模型单 GPU 能放下，目标是简单可靠 | 单 GPU + 多独立副本 | 最少 collective 和故障域 |
| 单 GPU 放不下，单节点多 GPU | TP | shard tensor/权重，常利用 NVLink |
| 跨节点或模型层适合分 stage | PP，可能与 TP 组合 | layer stages 跨设备/节点 |
| 模型能放下，要提高多请求吞吐 | DP/独立副本 | 请求分散到多个副本 |
| 大模型跨多节点，且需多副本 | TP × PP × DP | 最复杂，必须专门平台化 |

官方 scaling 指南给出的实用起点：

1. 模型能放单 GPU，先单 GPU。
2. 单节点单卡放不下，使用该节点内 TP。
3. 多节点时，常把 TP 设为每节点 GPU 数、PP 设为节点数。
4. 没有 NVLink 或模型不能均匀切分时，PP 可能比 TP 更合适。

固定官方页：

- [Parallelism and Scaling v0.25.0](https://docs.vllm.ai/en/v0.25.0/serving/parallelism_scaling/)
- [Architecture Overview v0.25.0](https://docs.vllm.ai/en/v0.25.0/design/arch_overview/)

### 12.2 Tensor Parallelism

TP 把同一层的 tensor/计算分到多 GPU。优点：

- 单卡放不下的权重可分片；
- 合适模型/硬件上可提高计算能力；
- 同节点 NVLink/NVSwitch 可降低 collective 成本。

代价：

- 每层或频繁 collective；
- 对 GPU 拓扑、NCCL 和同步最敏感；
- 最慢 rank 拖住所有 rank；
- 每卡仍有复制项和 buffer；
- 小 batch/小模型可能通信大于收益；
- 一个 rank 失败常让整个实例失败。

Kubernetes 单节点常见结构：

```yaml
resources:
  limits:
    nvidia.com/gpu: "4"
```

这保证一个 Pod 的四张卡来自同一 Node，但不自动保证 Device Plugin 选择的 GPU 拓扑是最优 NVLink clique。还要核对：

- Node GPU topology；
- NUMA/CPU pinning；
- Topology Manager policy；
- Device Plugin allocation；
- `NCCL_TOPO_FILE` 等平台约束；
- 每 rank 逻辑设备映射。

### 12.3 Pipeline Parallelism

PP 把模型层分成 stage。优点：

- 可跨节点；
- stage 间通信频度/形态与 TP 不同；
- 在无 NVLink 或某些不均匀切分场景可能更合适。

代价：

- pipeline bubble；
- stage 不均衡；
- activation 传输；
- batch/并发不足时利用率差；
- 任一 stage 故障影响整体；
- 启动/就绪必须等待完整 world。

“两台机器，每台四卡”不等于随便启动两个 Pod 就得到 `TP=4, PP=2`。需要同一个分布式作业定义 rank、master address/port、world size、成员发现和整体失败语义。

### 12.4 Data Parallelism

DP 为不同请求提供多个模型副本/Engine Core rank。它主要增加吞吐和故障隔离，但：

- 每个 DP rank 需要权重与自己的 KV capacity；
- 总 GPU 消耗按副本增长；
- load balancing 必须理解 queue；
- cache 命中可能因请求散列变化；
- 每 rank 指标需可区分又不能高基数；
- 内部、混合或外部 LB 模式的语义不同。

独立 Deployment replicas 与 vLLM 内部 DP 可以都实现“多个服务单元”，但生命周期和路由不同：

| 独立 Pod 副本 | 内部 DP |
|---|---|
| Kubernetes 单独调度/重启 | 一个分布式配置管理 rank |
| Service/Gateway 负载均衡 | vLLM coordinator/internal LB 可参与 |
| 故障域较清晰 | rank/coordinator 故障语义更耦合 |
| 容易滚动发布 | 需整体 world 与内部端口 |

没有内部 DP 需求时，先用独立 Pod 副本通常更易运维。

### 12.5 进程数量与 GPU 数

官方架构给出的概念关系：

```text
per Engine Core:
  worker count ≈ TP_size × PP_size

DP:
  one Engine Core per DP rank
```

所以总 worker 近似：

```text
workers_total ≈ DP × TP × PP
```

这有助于估算：

- GPU 数；
- process/context overhead；
- log 数；
- health handshake；
- NCCL group；
- CPU core；
- shared memory；
- startup 并发 I/O。

不要只按 GPU 数给 CPU。多进程 tokenization、IPC、Ray 和 log/metrics 都需要主机资源。

### 12.6 跨节点为什么不只是“网络通”

至少核对：

| 维度 | 证据 |
|---|---|
| world/rank | 每 rank 启动日志、master、world size |
| GPU mapping | Pod UID、Node UID、rank、logical device、GPU UUID |
| fabric | NIC、RDMA、NVLink、PCIe topology |
| NCCL transport | NCCL debug summary、是否错误回落到 Socket |
| MTU/routing | 节点间接口、丢包、重传 |
| ports | Ray/PyTorch/KV/DP 内部端口清单 |
| security | 专用可信网络、NetworkPolicy/防火墙 |
| scheduling | gang/placement group、反亲和/拓扑 |
| failure | 少一个成员时如何退出与重建 |
| storage/cache | 所有成员看到相同 revision 与工件 |

官方文档特别提醒，`NET/Socket` 可能是低效路径。能建立 TCP 不代表 collective 达到预期带宽。

### 12.7 更多 GPU 不保证线性加速

理想：

```text
4 GPUs -> 4× throughput
```

现实受限于：

```text
serial frontend/tokenization
+ collective communication
+ pipeline bubble
+ load imbalance
+ memory bandwidth
+ kernel shape/occupancy
+ batch不足
+ queue与输出消费
```

扩卡后至少比较：

```text
speedup(N) = throughput(N GPUs) / throughput(1 GPU)
efficiency(N) = speedup(N) / N
```

同时比较 TTFT/ITL/E2E；TP 可能让模型放下，却让交互延迟因 collective 变坏。

### 12.8 分布式配置的停损线

出现以下任一条件，先停止扩大规模：

- rank 数与可见 GPU 不一致；
- 任一 rank 使用错误 Node/GPU；
- NCCL 退化路径未解释；
- 冷启动不能稳定完成；
- 一个 worker 死亡后整体不收敛；
- metrics 无法区分 rank/模型；
- 内部端口暴露到不可信网络；
- 单 GPU/单节点基线尚未建立；
- 扩卡 efficiency 低且 SLO 无改善。

---

## 13. 安全边界：模型服务不是“加一个 API key”就结束

### 13.1 先画攻击面

```text
Untrusted client
  -> Gateway / Ingress
      -> vLLM HTTP API
      -> optional gRPC
      -> operational/dev/profiler endpoints
      -> remote media fetch
      -> tool server / dynamic LoRA
      -> model/tokenizer/cache
      -> distributed TP/PP/DP network
      -> GPU/driver/host filesystem
```

每条边都可能同时消耗 GPU、CPU、内存、磁盘和网络。推理 API 的安全问题不只是不当输出，还包括资源耗尽、SSRF、供应链代码执行、内部控制面暴露和模型/提示词泄漏。

固定官方页：

- [Security v0.25.0](https://docs.vllm.ai/en/v0.25.0/usage/security/)

### 13.2 `--api-key` 的保护范围有限

`--api-key` 或 `VLLM_API_KEY` 为部分 HTTP API 提供 Bearer authentication，但官方安全页明确：主要保护 `/v1`、`/v2`、`/inference` 等指定前缀的 endpoints；同一 server 上仍有许多不受它保护的 endpoint。

`v0.25.0` 官方列出的重要未保护面包括：

- `/invocations` 等替代 inference endpoint；
- `/pause`、`/resume`、`/scale_elastic_ep` 等操作 endpoint；
- `/health`、`/ping`、`/version`、`/load`；
- dev mode 下的 cache reset、sleep/wake、collective RPC；
- profiler start/stop；
- 某些 tokenize/detokenize、score/rerank 变体。

所以：

```text
VLLM_API_KEY configured
  != 整个HTTP server都受认证
  != 租户授权
  != rate limit
  != request cost limit
  != TLS
```

生产控制：

1. 只通过 gateway/reverse proxy 暴露批准的 endpoint allowlist。
2. vLLM Pod 不直接暴露 LoadBalancer/NodePort。
3. NetworkPolicy 只允许 gateway、monitoring 和必要管理来源。
4. gateway 承担租户身份、授权、配额、限流和审计。
5. API key 仍从 Secret 注入并轮换，但作为纵深防御。
6. 对 `/health`、`/metrics` 等运维接口使用独立路径和网络策略。

### 13.3 gRPC 默认不安全

只有传 `--grpc-port` 才启动 gRPC listener。`v0.25.0` 官方说明它默认没有：

- authentication；
- authorization；
- encryption。

它只能位于受信私网，并由防火墙/NetworkPolicy/segmentation 保护。不要把 gRPC port 加进面向互联网的 Service。

### 13.4 多节点内部通信默认也不可信

PyTorch Distributed、KV transfer、TP/PP/DP 通信默认缺少面向不可信网络的认证和加密。官方安全页强调：

- PyTorch Distributed 是内部通信；
- 没有 authorization protocol；
- 消息不加密；
- 某些连接会监听可达接口。

控制要求：

- 专用 GPU backend network；
- 精确 `VLLM_HOST_IP`/接口选择；
- 只开放固定成员与端口；
- 不把内部 port 复用到 public Service；
- 需要加密合规时由 mTLS sidecar、IPsec 或合规网络层提供；
- Network isolation 不是 cryptographic encryption。

### 13.5 `trust_remote_code` 是代码执行决策

有些 Hugging Face 模型要求：

```text
--trust-remote-code
```

其含义不是“信任权重数值”，而是允许加载模型仓库提供的 Python code。风险包括：

- 读取挂载的 Secret；
- 访问 service account token；
- 发起网络请求；
- 写 cache/PVC；
- 影响 host/GPU runtime；
- 通过依赖安装或 import 扩大供应链。

默认策略：

1. 不启用。
2. 优先使用 vLLM/Transformers 原生支持模型。
3. 必须启用时做 code review、恶意代码扫描和隔离评审。
4. 同时固定 model `revision`、tokenizer revision 与 `code_revision`。
5. 禁止默认 service account token。
6. 最小 egress、只读 root、独立 cache、最小 Secret。
7. 在隔离构建环境预取并产出受信模型工件。

固定 revision 只减少漂移，不自动证明代码安全。

### 13.6 模型、tokenizer 与 cache 是供应链

cache 不是“纯性能数据”。`v0.25.0` 安全文档明确指出：vLLM 假定 cache directory 私有且可信；其中某些内容加载时没有 cryptographic integrity verification，并可能使用支持代码执行的格式。

因此：

- cache PVC 不与不可信租户共享写权限；
- 不从未知快照恢复 cache；
- 构建流水线记录来源、digest/hash、revision；
- model/tokenizer/config/chat template 一起审计；
- 容器只访问其模型 cache；
- 权限只给运行 UID；
- 清 cache 是状态变更，需审批且先评估冷启动；
- model weight 使用 safetensors 可降低某些反序列化风险，但不能替代整体供应链审查。

`VLLM_CACHE_ROOT`、Hugging Face cache、Triton/compile cache 需要分别清点；不要只保护一个目录。

### 13.7 HF token 与 API key

| Secret | 用途 | 最小权限 |
|---|---|---|
| `HF_TOKEN` | 读取 private/gated model | read-only、限定组织/仓库、短期轮换 |
| `VLLM_API_KEY` | vLLM 指定 HTTP endpoints Bearer auth | 独立服务 key、定期轮换 |
| gateway credentials | 租户认证/授权 | 不应下发给 vLLM worker |

禁止：

- 把 token 写进 image layer；
- 放在 CLI args 里让进程列表/日志暴露；
- 写入 ConfigMap；
- 在故障包中导出完整 env；
- 多环境共用长效 token；
- 为公开模型无意义地挂私有仓库高权限 token。

Secret 挂载/注入仍可能被进程读取；需要配合最小 code trust、egress 和 Pod isolation。

### 13.8 远程多模态 URL：SSRF 与解压炸弹

如果允许 client 提供 image/audio/video URL，server 会成为网络 fetcher。官方建议：

- 用 `--allowed-media-domains` 限定域；
- `VLLM_MEDIA_URL_ALLOW_REDIRECTS=0` 阻止 redirect 绕过 allowlist；
- 保持 image pixels、audio filesize、decode duration 上限；
- egress firewall 禁止 cloud metadata 与内网管理面；
- gateway 限 body、URL 数、超时和并发；
- 下载 cache 有空间和租户边界。

allowlist 不能只检查字符串后缀；还要考虑 DNS rebinding、redirect、解析后的私网地址和压缩内容膨胀。

### 13.9 请求本身就是资源分配请求

攻击者或误用客户端可以放大：

- prompt tokens；
- `max_tokens`；
- `n`/best-of 类输出数；
- logprobs；
- 多模态数量/尺寸；
- structured output grammar；
- 并发与连接时长；
- tools/LoRA；
- stream 消费速度。

控制层：

```text
gateway hard limits
  -> tenant quota/rate
  -> vLLM server/engine limits
  -> queue/admission
  -> SLO overload policy
```

仅在 engine 最深处拒绝，已经消耗了网络、JSON/tokenization 和 queue 资源。

### 13.10 dev、profiler、动态 LoRA 与 tool server

| 功能 | 风险 | 生产策略 |
|---|---|---|
| `VLLM_SERVER_DEV_MODE=1` | 暴露 cache reset、sleep、collective RPC 等 | 禁止 |
| profiler endpoint | 高开销、trace 含敏感信息、可控服务状态 | 默认关闭；隔离环境、审批、限时 |
| dynamic LoRA | 运行时改变模型行为和加载内容 | 不对不可信 client；管理面隔离 |
| `--tool-server demo` | 模型驱动 Python/browser 等外部能力 | 生产禁用 demo；专门 sandbox |
| tokenizer info | 暴露 template/config | 默认关闭或严格保护 |

“endpoint 没写进文档导航”也不能当作不存在；以固定 tag route 源码和实际 OpenAPI/路由清单为准。

### 13.11 日志、指标与 prompt 隐私

故障排查常想记录完整 prompt，但这可能含：

- 用户 PII；
- 游戏账号/支付信息；
- 商业策略；
- system prompt；
- API key 或工具返回；
- 安全攻击 payload。

生产策略：

- 默认不记录 prompt/body；
- request ID 使用无业务含义随机值；
- 只记录 token length bucket、status、timing 等必要字段；
- debug log 限时、审批、脱敏；
- profiler/trace 单独保管；
- metrics endpoint 不公开；model name、queue 与拓扑本身也可能敏感；
- 故障包先做 Secret/URL/query/header 清洗。

### 13.12 安全基线验收

```text
[ ] image digest和SBOM已固定
[ ] model/tokenizer/code revision已固定
[ ] trust_remote_code默认关闭
[ ] API只经gateway暴露
[ ] endpoint allowlist已验证
[ ] vLLM API key不被当成完整授权
[ ] gRPC未启用或仅可信私网可达
[ ] internal distributed ports隔离
[ ] serviceAccount token未挂载
[ ] Secret最小权限且可轮换
[ ] cache来源可信且写权限隔离
[ ] remote media domain/redirect/decode/egress受限
[ ] dev/profiler/dynamic LoRA/demo tools关闭
[ ] request cost和租户quota受限
[ ] logs/metrics/profiles有隐私策略
```

---

## 14. 只读取证：固定 Pod UID 与 Node UID 后再看证据

### 14.1 为什么 Pod 名不够

Deployment 重新创建后，Pod name 可能相似但 UID 已变；Node 重装后，Node name 也可能相同但 UID 已变。故障时间线必须绑定：

```text
cluster context
+ namespace
+ Pod name
+ Pod UID
+ Node name
+ Node UID
+ container name
+ image ID/digest
+ restart attempt
```

否则容易把：

- 新 Pod 的健康状态；
- 旧 Pod 的 previous log；
- 同名重建 Node 的 event；
- 不同 replica 的 metrics

拼成一条不存在的故事。

### 14.2 只读脚本的边界

下面脚本只执行：

- `kubectl config current-context`；
- `kubectl get`；
- `kubectl logs`；
- Kubernetes API Pod proxy 的 `GET /metrics`。

明确不执行：

- `kubectl exec`；
- `port-forward`；
- `apply/patch/delete/scale/rollout`；
- Node shell；
- `nvidia-smi`；
- profiler；
- 压测请求；
- 文件写入。

事件单必须预先提供精确的五个输入。脚本会把它们 trim 后冻结为只读变量，并在输出业务证据前校验 Pod UID 与 Node UID。

```powershell
param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$Context,

    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[a-z0-9]([-a-z0-9_.]*[a-z0-9])?$')]
    [string]$Namespace,

    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$')]
    [string]$Pod,

    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$')]
    [string]$NodeUID,

    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$')]
    [string]$PodUID
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if (-not (Get-Command kubectl -ErrorAction SilentlyContinue)) {
    throw 'kubectl is not available in PATH.'
}

$Context = $Context.Trim()
$Namespace = $Namespace.Trim()
$Pod = $Pod.Trim()
$NodeUID = $NodeUID.Trim().ToLowerInvariant()
$PodUID = $PodUID.Trim().ToLowerInvariant()

foreach ($Name in @('Context', 'Namespace', 'Pod', 'NodeUID', 'PodUID')) {
    Set-Variable -Name $Name -Scope Script -Option ReadOnly -Force
}

function Invoke-KubectlReadOnly {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments
    )

    $Output = & kubectl --context $Context @Arguments 2>&1
    if ($LASTEXITCODE -ne 0) {
        $Rendered = $Arguments -join ' '
        $Message = 'Read-only kubectl call failed: kubectl --context {0} {1}{2}{3}' -f
            $Context, $Rendered, [Environment]::NewLine,
            ($Output -join [Environment]::NewLine)
        throw $Message
    }

    return ($Output -join [Environment]::NewLine)
}

$CurrentContext = (& kubectl config current-context 2>&1 | Out-String).Trim()
if ($LASTEXITCODE -ne 0) {
    throw 'Unable to read the current kubectl context.'
}
if ($CurrentContext -cne $Context) {
    throw "Context mismatch. Current='$CurrentContext'; expected='$Context'."
}

$PodRaw = Invoke-KubectlReadOnly -Arguments @(
    '--namespace', $Namespace,
    'get', 'pod', $Pod,
    '--output', 'json'
)
$PodObject = $PodRaw | ConvertFrom-Json

if ([string]$PodObject.metadata.namespace -cne $Namespace) {
    throw 'Namespace identity validation failed.'
}
if ([string]$PodObject.metadata.name -cne $Pod) {
    throw 'Pod name identity validation failed.'
}
if ([string]$PodObject.metadata.uid -ine $PodUID) {
    throw "Pod UID mismatch. Live='$($PodObject.metadata.uid)'; expected='$PodUID'."
}

$NodeName = [string]$PodObject.spec.nodeName
if ([string]::IsNullOrWhiteSpace($NodeName)) {
    throw 'The fixed Pod is not assigned to a Node.'
}

$NodeRaw = Invoke-KubectlReadOnly -Arguments @(
    'get', 'node', $NodeName,
    '--output', 'json'
)
$NodeObject = $NodeRaw | ConvertFrom-Json

if ([string]$NodeObject.metadata.uid -ine $NodeUID) {
    throw "Node UID mismatch. Node='$NodeName'; live='$($NodeObject.metadata.uid)'; expected='$NodeUID'."
}

$ServerContainer = @($PodObject.spec.containers) |
    Where-Object { $_.name -eq 'server' } |
    Select-Object -First 1
if ($null -eq $ServerContainer) {
    throw "Container 'server' is absent from the fixed Pod."
}

$ServerStatus = @($PodObject.status.containerStatuses) |
    Where-Object { $_.name -eq 'server' } |
    Select-Object -First 1
if ($null -eq $ServerStatus) {
    throw "ContainerStatus for 'server' is absent."
}

$Conditions = [ordered]@{}
foreach ($Condition in @($PodObject.status.conditions)) {
    $Conditions[[string]$Condition.type] = [ordered]@{
        status             = [string]$Condition.status
        reason             = [string]$Condition.reason
        message            = [string]$Condition.message
        lastTransitionTime = [string]$Condition.lastTransitionTime
    }
}

$RestartState = $null
if ($null -ne $ServerStatus.lastState.terminated) {
    $RestartState = [ordered]@{
        reason     = [string]$ServerStatus.lastState.terminated.reason
        exitCode   = [int]$ServerStatus.lastState.terminated.exitCode
        signal     = [int]$ServerStatus.lastState.terminated.signal
        startedAt  = [string]$ServerStatus.lastState.terminated.startedAt
        finishedAt = [string]$ServerStatus.lastState.terminated.finishedAt
    }
}

$Summary = [ordered]@{
    identity = [ordered]@{
        context   = $Context
        namespace = $Namespace
        pod       = $Pod
        podUID    = $PodUID
        node      = $NodeName
        nodeUID   = $NodeUID
    }
    pod = [ordered]@{
        phase             = [string]$PodObject.status.phase
        qosClass          = [string]$PodObject.status.qosClass
        podIP             = [string]$PodObject.status.podIP
        startTime         = [string]$PodObject.status.startTime
        deletionTimestamp = [string]$PodObject.metadata.deletionTimestamp
        conditions        = $Conditions
        owners            = @($PodObject.metadata.ownerReferences)
    }
    server = [ordered]@{
        image        = [string]$ServerContainer.image
        imageID      = [string]$ServerStatus.imageID
        ready        = [bool]$ServerStatus.ready
        started      = [bool]$ServerStatus.started
        restartCount = [int]$ServerStatus.restartCount
        currentState = $ServerStatus.state
        lastExit     = $RestartState
        resources    = $ServerContainer.resources
        probes       = [ordered]@{
            startup   = $ServerContainer.startupProbe
            readiness = $ServerContainer.readinessProbe
            liveness  = $ServerContainer.livenessProbe
        }
    }
    node = [ordered]@{
        allocatable = [ordered]@{
            cpu       = [string]$NodeObject.status.allocatable.cpu
            memory    = [string]$NodeObject.status.allocatable.memory
            nvidiaGpu = [string]$NodeObject.status.allocatable.'nvidia.com/gpu'
        }
        conditions = @($NodeObject.status.conditions)
    }
}

'=== IDENTITY-VALIDATED SNAPSHOT ==='
$Summary | ConvertTo-Json -Depth 15

'=== POD EVENTS BY FIXED UID ==='
Invoke-KubectlReadOnly -Arguments @(
    '--namespace', $Namespace,
    'get', 'events',
    '--field-selector', "involvedObject.uid=$PodUID",
    '--sort-by=.metadata.creationTimestamp',
    '--output', 'wide'
)

'=== SERVER LOGS: CURRENT, FILTERED ==='
$CurrentLogs = Invoke-KubectlReadOnly -Arguments @(
    '--namespace', $Namespace,
    'logs', $Pod,
    '--container', 'server',
    '--timestamps',
    '--tail', '600'
)
$CurrentLogs -split [Environment]::NewLine |
    Where-Object {
        $_ -match '(?i)(error|exception|traceback|oom|out of memory|cuda|nccl|loading|loaded|memory|kv cache|warm|compile|capture|health|ready|preempt)'
    } |
    ForEach-Object {
        $_ -replace '(?i)(authorization:\s*bearer\s+)\S+', '$1[REDACTED]'
    }

if ([int]$ServerStatus.restartCount -gt 0) {
    '=== SERVER LOGS: PREVIOUS, FILTERED ==='
    try {
        $PreviousLogs = Invoke-KubectlReadOnly -Arguments @(
            '--namespace', $Namespace,
            'logs', $Pod,
            '--container', 'server',
            '--previous',
            '--timestamps',
            '--tail', '600'
        )
        $PreviousLogs -split [Environment]::NewLine |
            Where-Object {
                $_ -match '(?i)(error|exception|traceback|oom|out of memory|cuda|nccl|loading|loaded|memory|kv cache|warm|compile|capture|health|ready|preempt)'
            } |
            ForEach-Object {
                $_ -replace '(?i)(authorization:\s*bearer\s+)\S+', '$1[REDACTED]'
            }
    }
    catch {
        "Previous logs unavailable: $($_.Exception.Message)"
    }
}

'=== VLLM METRICS: SELECTED RAW SERIES ==='
$MetricsPath = "/api/v1/namespaces/$Namespace/pods/$($Pod):8000/proxy/metrics"
try {
    $Metrics = Invoke-KubectlReadOnly -Arguments @(
        'get', '--raw', $MetricsPath
    )
    $Metrics -split [Environment]::NewLine |
        Where-Object {
            $_ -match '^# (HELP|TYPE) vllm:' -or
            $_ -match '^vllm:(num_requests_running|num_requests_waiting|num_requests_waiting_by_reason|kv_cache_usage_perc|num_preemptions|request_success|prompt_tokens|generation_tokens)'
        }
}
catch {
    "Pod proxy metrics unavailable: $($_.Exception.Message)"
}
```

### 14.3 如何读脚本输出

按顺序，而不是按感觉：

1. identity block 五个输入全部匹配。
2. image 是声明值，imageID 才是 runtime 实际工件标识。
3. Pod conditions 区分 Scheduled、Initialized、ContainersReady、Ready。
4. `lastExit.reason/exitCode` 先分 OOMKilled、Error、signal。
5. event 按固定 Pod UID，不把同名历史对象混入。
6. current 与 previous logs 分开；previous 只对应上一次容器实例。
7. metrics 是 raw series，先看实际名字和 label，再写 PromQL。
8. proxy 失败只说明这条读取路径/RBAC/endpoint 有问题，不证明 vLLM 没指标。

日志过滤只是降低噪声，不是可靠脱敏。授权终端仍不得复制未审查日志到工单或聊天。

### 14.4 这份脚本仍不能证明什么

- GPU UUID 与 rank 映射；
- host 上其他 GPU 进程；
- Xid/ECC；
- NCCL 实际 transport；
- cgroup memory.events；
- client-observed TTFT；
- 模型输出质量；
- PVC/storage latency；
- 网关请求分母。

这些需要各责任域的只读 connector/telemetry。不要为补证据直接在生产 Pod `exec` 或到 Node 开 shell。

---

## 15. 受控实验：不在生产上“边试边调”

### 15.1 任何压测都是有影响操作

即使只发 HTTP request，压测也会改变：

- queue；
- KV cache；
- GPU/CPU/memory；
- prefix cache；
- autoscaler；
-日志与指标；
- 成本；
- 其他租户的 SLO。

因此以下动作全部需要明确审批：

```text
发送benchmark流量
改变并发/到达率
清理模型或编译cache
删除/重建Pod
调整gpu_memory_utilization/max_model_len/max_num_seqs
开关per-request metrics
启用profiler
改变TP/PP/DP
改变副本或滚动发布
制造OOM/NCCL/Node故障
```

生产集群默认不允许。实验目标应是隔离测试集群或专用 namespace/NodePool，且测试流量不得经生产入口。

### 15.2 实验契约

每次实验先填满：

| 项目 | 必填内容 |
|---|---|
| approval | ticket、owner、开始/结束时间 |
| environment | context、namespace、NodePool、是否与生产共享 |
| identity | image digest、model/tokenizer revision |
| hardware | GPU product/count/topology、driver、CPU、memory、disk/network |
| engine | 完整 args、env name、parallel config |
| workload | dataset hash、prompt/output 分布、streaming、arrival model |
| variable | 本轮唯一主变量 |
| control | 对照组配置 |
| duration/repeat | warm-up、steady、重复次数、随机顺序 |
| stop conditions | error/OOM/Xid/Node pressure/SLO/queue 上限 |
| evidence | raw metrics、client results、events/logs、timestamps |
| rollback | 恢复对象、cache 处理、验证 |

一次只改一个主变量；否则即使更快，也不知道是哪个因素。

### 15.3 固定 workload 而不是固定一句 prompt

最小 workload corpus 应有：

| Bucket | Prompt tokens | Output cap | 比例 | 目的 |
|---|---:|---:|---:|---|
| P1/O1 | 128 | 64 | 30% | 短交互 |
| P2/O1 | 512 | 64 | 25% | 中等 prefill |
| P2/O2 | 512 | 256 | 25% | 常规生成 |
| P3/O2 | 2048 | 256 | 15% | 长 prompt |
| P3/O3 | 2048 | 1024 | 5% | 长尾生成 |

这些只是实验示例分布，不是生产假设。真实基线应来自脱敏 token length histogram，并固定：

- tokenizer revision；
- random seed；
- temperature/top-p；
- stop conditions；
- streaming；
- output cap；
- 请求体功能；
- 是否 prefix 重复。

不能复制真实 prompt 到测试集。合成 prompt 也要避免所有请求共享同一 prefix，否则 prefix cache 会把结果“优化”成不真实。

### 15.4 两种负载模型

#### Closed loop concurrency

```text
固定N个并发client
每个请求完成后立刻发下一个
```

适合测不同 concurrency 下的最大吞吐，但系统变慢时发包率会自动降低，可能掩盖 overload。

#### Open loop arrival

```text
按独立arrival process发送
不等待上一请求完成
```

更接近生产到达率，能看 queue 发散与恢复；必须设 client 连接/内存上限，避免 load generator 先崩。

两者都做，不能互相替代。

### 15.5 实验 A：冷启动与探针

目标：为 startup probe 和 rollout timeout 建证据。

分组：

| 组 | Model cache | Compile cache | Node | 说明 |
|---|---|---|---|---|
| A1 | hit | hit | warm | 最快路径 |
| A2 | hit | miss | warm | compile/capture 成本 |
| A3 | miss | miss | existing | 下载 + compile |
| A4 | miss | miss | new Node | 最坏冷启动 |

记录第 5.1 节的全部时间戳，至少重复到能估计高分位。不要在生产清 cache 来制造 A3/A4；使用审批的隔离 PVC/Node。

验收：

- startup window 高于已批准的高分位与余量；
- 各阶段日志可区分；
- 超时能收敛为明确失败，不无限挂起；
- readiness 不提前；
- cache miss 不循环重启。

### 15.6 实验 B：并发/到达率扫描

固定 model/hardware/config，按预先定义阶梯增加 load：

```text
warm-up
  -> low steady
  -> medium steady
  -> target steady
  -> controlled overload
  -> recovery
```

每级采集：

```text
arrival/completion request rate
prompt/output/total token rate
client TTFT/ITL/E2E
server queue/prefill/decode/E2E
running/waiting by reason
KV usage/preemption
GPU util/memory/power
CPU/throttle/memory/network
error/cancel/retry
```

得到三点：

1. SLO-safe capacity；
2. saturation knee；
3. overload recovery time。

不要以 GPU 100% 作为成功标准。

### 15.7 实验 C：显存与上下文矩阵

可控变量：

- `gpu_memory_utilization`；
- `max_model_len`；
- `max_num_seqs`；
- prompt/output length；
- KV dtype/quant（若模型/硬件支持）；
- eager/graph/compile 配置。

每个 cell 先测启动，再测固定 workload。结果表至少有：

| Cell | Start | Weight | Non-KV/Profile | KV capacity | Graph | OOM stage | SLO | Throughput |
|---|---:|---:|---:|---:|---:|---|---|---:|

不要故意把生产 GPU 推到 OOM。OOM 边界实验只允许在可隔离的测试实例，并设置 stop condition。

### 15.8 实验 D：per-request metrics 开销

对照：

```text
Control:
  per-request metrics disabled

Treatment:
  --enable-per-request-metrics
```

其余 digest、revision、args、workload 完全相同。比较：

- API server CPU；
- event loop/queue；
- TTFT/ITL/E2E；
- request/output token throughput；
- response bytes；
- metrics null 比例；
- client parsing overhead。

同时覆盖：

- non-streaming；
- streaming with include_usage；
- `n=1`；
- `n>1` 返回 metrics null；
- multiple prompts 返回 metrics null；
- single output token 的 `mean_itl_ms=null`。

### 15.9 实验 E：TP/PP/DP scaling

顺序：

```text
1 GPU baseline
  -> same-node TP2
  -> same-node TP4
  -> approved PP/multi-node
  -> DP replicas
```

每一步计算 speedup 与 efficiency，并记录：

- 每 rank GPU memory；
- NCCL transport/bandwidth；
- worker startup；
- 最慢 rank；
- TTFT/ITL；
- failure recovery；
- total cost/token。

如果 single GPU 放不下，可用最小能运行配置作为 baseline，但仍要明确它不是 `N=1`。

### 15.10 实验 F：发布与排空

必须用可取消的合成流式请求验证：

1. 旧 Pod 正在生成。
2. 新 Pod 启动且尚未 Ready。
3. 新 Pod Ready 后接收新请求。
4. 旧 Pod endpoint 摘除。
5. 已有 stream 是完成、被取消还是中断。
6. grace 到期后进程如何退出。
7. rollback 是否能重新拉起旧 digest/revision。

记录 client 看到的重复、缺 token、HTTP status、终止原因。只看 Deployment `Successfully rolled out` 不足。

### 15.11 停损条件

实验开始前把数值写入审批单。至少包含：

- 任一 Xid/ECC critical；
- CUDA OOM；
- container OOMKilled；
- Node Memory/Disk/PID pressure；
- error rate 超批准值；
- queue 超批准上限或持续发散；
- client TTFT/ITL/E2E 超安全上限；
- GPU temperature/power/fabric 告警；
- load generator 自身饱和；
- 指标缺失或时钟不同步；
- 非测试流量进入目标。

命中后：

```text
停止新增负载
  -> 保留现状证据
  -> 等待已接受请求按计划收敛
  -> 只读收集
  -> 按审批回滚
```

不要为了“把曲线跑完”越过停损线。

### 15.12 结果可复现清单

```text
[ ] image digest
[ ] vLLM tag/commit
[ ] model/tokenizer/code revision
[ ] full effective args
[ ] GPU/driver/topology
[ ] Kubernetes resources/probes
[ ] dataset hash/seed/distribution
[ ] client version and clocks
[ ] warm-up/steady duration
[ ] concurrency/arrival model
[ ] raw result and metric schema
[ ] null/missing/retry policy
[ ] experiment start/end
[ ] control/treatment order
[ ] stop condition outcome
```

---

## 16. 定向 Python 语法：只学读这条源码链需要的部分

本节目标不是把 Kubernetes/SRE 工程师变成 Python 开发者，而是让你能回答：

```text
这个函数何时阻塞？
资源何时创建/释放？
None代表什么？
装饰器改了什么？
类型标注有没有运行时校验？
异常会怎样穿过请求链？
```

### 16.1 `async def`、`await` 与事件循环

化简示意：

```python
async def run_server(args) -> None:
    await run_server_worker(args)
```

- `async def` 定义 coroutine function。
- 调用它先得到 coroutine object，不会自动跑完。
- `await` 把控制权交回 event loop，等待可 await 的结果。
- 等待 socket/ZMQ/queue 时，event loop 可以处理其他请求。

它不等于：

- GPU kernel 自动并行；
- CPU-bound tokenization 不占 CPU；
- Python GIL 消失；
- 一个 event loop 可以承受无限请求；
- `await` 之后一定切换线程。

运维映射：

| 现象 | 可能层 |
|---|---|
| GPU 空闲但 API TTFT 高 | event loop/CPU/tokenizer/queue |
| 一个慢 client 拖累输出 | stream backpressure/output consumer |
| engine 正常但 API 卡住 | frontend async task/IPC |

### 16.2 async generator 与 `yield`

在线生成不是等所有 token 完成才返回。等价化简：

```python
async def generate(request) -> AsyncIterator[RequestOutput]:
    stream = await add_request(request)
    async for output in stream:
        yield output
```

`yield` 让函数成为 generator；`async def + yield` 是 async generator。client 可以：

```python
async for output in engine.generate(...):
    send_to_client(output)
```

运维意义：

- first yield 对应首输出时机；
- generator 尚未结束表示请求仍占生命周期资源；
- client cancel 必须向下游传播，释放 scheduler/KV 状态；
- output consumer 慢可能制造 backpressure；
- E2E 结束点是 generator 完成，不是 first yield。

固定入口：

- [AsyncLLM API/source v0.25.0](https://docs.vllm.ai/en/v0.25.0/api/vllm/v1/engine/async_llm/)

### 16.3 `@asynccontextmanager` 与 `async with`

`api_server.py` 使用 `@asynccontextmanager` 管理 engine client 生命周期。等价化简：

```python
from contextlib import asynccontextmanager

@asynccontextmanager
async def build_engine(config):
    engine = create_engine(config)
    try:
        yield engine
    finally:
        engine.shutdown()

async with build_engine(config) as engine:
    await serve(engine)
```

控制流：

```text
进入async with
  -> 运行yield之前：创建/握手
  -> yield engine：server使用
  -> 正常退出或异常
  -> finally：shutdown/清理
```

运维上要看：

- 初始化异常发生在 yield 前还是后；
- SIGTERM 时是否走 finally；
- shutdown 是否等待 worker；
- engine core/ZMQ/GPU memory 是否清理；
- server 先停流量还是先销毁 engine。

固定源码：

- [api_server.py @ v0.25.0](https://github.com/vllm-project/vllm/blob/v0.25.0/vllm/entrypoints/openai/api_server.py)

### 16.4 普通 context manager：`with`

`gpu_worker.py` 在模型加载和 memory profiling 中使用 context manager。等价化简：

```python
with memory_profiling(snapshot, weights_memory=weight_bytes) as result:
    model_runner.profile_run()
```

`with` 协议保证：

```text
__enter__()
  -> 被测代码
  -> __exit__()，即使发生异常也会调用
```

这允许 profiler 在前后读取 snapshot、计算 peak/non-KV。不要只看 with block 里面一行而忽略上下文管理器的 enter/exit 逻辑。

模型加载还组合多个 context manager：

```python
with (
    weight_memory_pool,
    current_vllm_config,
    allocator_split_policy,
):
    model_runner.load_model()
```

其中一个 enter 失败，后续未必执行；已进入的 manager 会按逆序退出。阶段日志因此很重要。

### 16.5 decorator：函数外面的行为

固定源码中可见：

```python
@instrument(span_name="Init device")
def init_device(...):
    ...

@torch.inference_mode()
def determine_available_memory(...):
    ...
```

语法：

```python
@decorator
def f():
    ...
```

大致等价于：

```python
f = decorator(f)
```

所以读函数不能忽略上方装饰器：

- `@instrument` 可能创建 trace/span、记录异常；
- `@torch.inference_mode()` 关闭 autograd 相关状态，适合 inference/profile；
- 其他 decorator 可能加锁、重试、同步或改参数。

decorator 名称只是入口；性能与异常语义要继续读它的实现。

### 16.6 union type：`A | None`

固定源码示例：

```python
async_llm: AsyncLLM | None = None
client_config: dict[str, Any] | None = None
```

`A | None` 表示值可以是 A 或 None。它主要帮助：

- IDE/static checker；
- 读者理解状态；
- 文档生成。

普通 Python type hint 本身通常不做运行时校验。下面仍可能在运行时传错：

```python
def f(x: int) -> None:
    ...

f("not an int")
```

只有函数自己、Pydantic 或其他框架验证，才会拒绝。

这解释了为什么 `CacheConfig` 的：

```python
Field(default=0.92, gt=0, le=1)
```

不只是 type hint；`gt/le` 提供运行时配置约束。

### 16.7 generics：`list[T]`、`dict[K, V]`、`tuple[...]`

示例：

```python
comm_handles: list[Handle] | None
tensors: dict[str, torch.Tensor]
def step() -> tuple[dict[int, EngineCoreOutputs], bool]:
    ...
```

读法：

- `list[Handle]`：Handle 列表；
- `dict[str, Tensor]`：字符串到 Tensor；
- `tuple[X, bool]`：返回两个位置固定的值；
- `dict[int, EngineCoreOutputs]`：按 engine/rank key 的输出。

它能帮助判断“这是单对象还是每 rank 一份”，但仍不是运行时容量限制。

### 16.8 walrus operator：`:=`

`EngineCore.add_request()` 与 `Worker.determine_available_memory()` 有类似写法：

```python
if kv_cache_memory_bytes := cache_config.kv_cache_memory_bytes:
    ...
```

它同时赋值并判断 truthiness，近似：

```python
kv_cache_memory_bytes = cache_config.kv_cache_memory_bytes
if kv_cache_memory_bytes:
    ...
```

读代码时要注意它判断的不是严格 `is not None`，而是真值。配置字段自身的验证通常排除不合理的 0，但仍应读 schema。

### 16.9 comprehension、`any` 与短路

`EngineCore._initialize_kv_caches()` 类似：

```python
has_kv_cache = any(
    kv_cache_spec
    for worker_specs in kv_cache_specs
    for kv_cache_spec in worker_specs
)
```

`any` 找到第一个 truthy 值就短路。嵌套 generator 顺序：

```text
for each worker_specs
  for each kv_cache_spec
    test truthiness
```

它不是把所有对象先复制成 list，因此通常更省临时内存。

### 16.10 `try/finally`、`raise` 与异常边界

```python
try:
    yield engine
finally:
    engine.shutdown()
```

`finally` 无论正常或异常都会尝试执行。另一个模式：

```python
try:
    execute_model()
except Exception as error:
    dump_evidence()
    raise error
```

`raise` 把异常继续向上传播。运维要问：

- 哪一层首次产生异常；
- 哪一层只记录后 re-raise；
- 哪一层把异常转换成 HTTP status；
- 哪一层把 engine 标记 errored；
- liveness 何时能看到；
- 是否存在 catch 后继续运行的降级。

重复 traceback 不代表有多个根因，可能是同一异常跨层记录。

### 16.11 class、state 与进程边界

`EngineCore`、`Scheduler`、`Worker`、`GPUModelRunner` 是不同对象，也可能位于不同进程。看到：

```python
self.scheduler.add_request(request)
```

只说明当前对象调用成员，不证明它是本地函数还是跨进程 RPC 的最终落点。架构文档说明 API server 与 Engine Core 之间可经 ZMQ；executor 又协调 workers。

读源码时建立表：

| Object | Process | Owns state | Communication |
|---|---|---|---|
| API server | frontend | HTTP/request/output | HTTP/ZMQ |
| AsyncLLM/client | frontend | streams/errors | ZMQ/queue |
| EngineCore | core process | scheduler/KV logical state | executor RPC |
| Worker | worker process | device/model runner | distributed/IPC |
| GPUModelRunner | worker process | model/KV physical execution | CUDA |

不要把 Python `self` 当成整个系统都在一个进程。

### 16.12 本课 Python 停止线

必须会：

- 找固定 tag 文件和函数；
- 看 async/await/yield；
- 看 context manager/decorator；
- 看类型与 None；
- 看异常传播；
- 把对象映射到进程和资源。

本课不要求：

- 实现新的 attention backend；
- 修改 CUDA/Triton kernel；
- 证明 scheduler 算法；
- 重写 Model Runner；
- 深入 CPython event loop internals。

---

## 17. 十个完整故障复盘

### 17.1 场景一：第二个副本一直 Pending

**现象**

```text
Deployment desired=2
ready=1
另一个Pod Pending
event: 0/N nodes are available
```

**证据链**

1. 固定 Pending Pod UID。
2. event 拆 `Insufficient nvidia.com/gpu`、taint、affinity、PVC。
3. 看 Node `allocatable/allocated` GPU，不只看物理卡数。
4. 检查 hostname topology spread `DoNotSchedule`。
5. 核对第一个 Pod 所在 Node 和其余合格拓扑域。

**错误结论**

“vLLM 起不来。”Pod 还没有容器，vLLM 尚未执行。

**根因示例**

集群只有一个合格 GPU Node；第二个副本被 `DoNotSchedule` 拒绝放到同 Node。

**止血与修复**

按 SLO 选择增加合格 GPU Node、等待节点池扩容，或审批后改 `ScheduleAnyway`/移除约束并接受同 Node 故障域。不能通过删除第一个健康 Pod 反复碰运气。

**预防**

发布前模拟 `replicas + maxSurge` 的 GPU 与 topology domain 数。

### 17.2 场景二：ContainerCreating，不是模型加载慢

**现象**

```text
Pod已Scheduled
container state=Waiting
reason=ContainerCreating
没有vLLM日志
```

**证据链**

- Pod UID event；
- image pull status；
- PVC attach/mount；
- sandbox/runtime event；
- Device Plugin Allocate/runtime/CDI；
- Node driver/operator 健康。

**错误结论**

“模型太大，所以五分钟没 Ready。”进程还没创建，模型阶段尚未开始。

**根因示例**

cache PVC 未能跨可用域挂载，或者 runtime 无法完成 GPU device injection。

**止血与修复**

在 storage/runtime/GPU 注入责任域修复；不要盲目增大 startup probe，它尚未运行。

**预防**

Gate 1 验证 PVC topology、image pull 权限和真实 GPU smoke。

### 17.3 场景三：cache miss 被 startup probe 反复杀死

**现象**

```text
每次都从下载权重开始
接近固定时间后容器重启
restartCount递增
previous log停在download/load阶段
```

**证据链**

1. container `lastState` 显示 probe-driven restart 时间。
2. event 有 startup probe failed。
3. current/previous log 阶段时间每次相似。
4. cache PVC 是否持久化、是否有写权限。
5. `failureThreshold × periodSeconds` 与 cold-start 分位数比较。

**错误结论**

“模型仓库不稳定。”固定时刻被终止更像 probe budget。

**根因**

探针按热 cache 的 90 秒设置，cache miss 实际需要 7 分钟；重启又丢弃 emptyDir 下载进度。

**止血与修复**

经变更审批扩大 startup budget，使用受信持久 cache，并先验证下载最终能完成。不得简单关闭所有探针。

**预防**

分开测 cache hit/miss、新 Node 和 compile miss 的 p99。

### 17.4 场景四：权重加载 CUDA OOM，却被写成 OOMKilled

**现象**

```text
log: torch.cuda.OutOfMemoryError
container exit reason=Error
lastState.reason不是OOMKilled
Pod CrashLoopBackOff
```

**证据链**

- OOM 发生在 `load_model`；
- GPU total/free 与其他 process；
- model/dtype/quant/TP；
- vLLM requested memory 与权重加载日志；
- Pod cgroup memory 没有 OOMKilled 证据。

**错误结论**

“把 Kubernetes memory limit 从 16Gi 调到 32Gi。”这不增加 HBM。

**根因示例**

模型 BF16 权重单卡放不下，或共享 GPU 上已有另一个进程。

**止血与修复**

移除未授权同卡进程；选择已验证 quant/dtype；模型确实放不下时评估 TP/PP。`gpu_memory_utilization` 主要影响 executor/KV 预算，不能让过大的权重凭空装下。

**预防**

模型工件进入发布前先做固定 GPU 的 load-only gate。

### 17.5 场景五：真正的 cgroup OOMKilled

**现象**

```text
lastState.reason=OOMKilled
exitCode=137
GPU memory尚有余量
Node可能没有MemoryPressure
```

**证据链**

- container memory limit/working set；
- `memory.events`（通过批准的节点 telemetry）；
- `/dev/shm` 是否 memory-backed；
- tokenizer/input/body/output buffer；
- model download staging；
- profiler/多模态 decode；
- 同 Pod sidecar。

**错误结论**

“CUDA OOM。”GPU 剩余显存与 host cgroup 是两套资源。

**根因示例**

`/dev/shm` 1 GiB、模型 staging 和高并发 request body 一起越过 16 GiB container limit。

**止血与修复**

入口限 body/并发；减少 host cache/staging；按证据调整 memory request/limit 与 `/dev/shm`。变更前验证 Node 可承载，防止把 Pod OOM 转成 Node pressure。

**预防**

压测同时采 CPU memory，不只采 HBM。

### 17.6 场景六：`/health 200`，TTFT p99 仍爆炸

**现象**

```text
readiness=Ready
/health=200
HTTP成功率尚可
client TTFT p99持续升高
```

**证据链**

- client/gateway TTFT；
- server queue time/TTFT；
- waiting capacity/deferred；
- running/KV/preemption；
- prompt length bucket；
- arrival/completion token rate；
- Ready replicas。

**错误结论**

“探针坏了。”health 本来就不验证 SLO。

**根因示例**

arrival token rate 超过 SLO-safe capacity，capacity waiting 持续增长。

**止血与修复**

入口背压/限流；使用已准备好的副本扩容；保护高优流量。不要让 readiness 因 queue 短暂升高而失败，否则 endpoint 减少会放大过载。

**预防**

queue leading signal、SLO burn alert 和容量余量联动。

### 17.7 场景七：GPU utilization 低，TTFT 却高

**现象**

```text
GPU util 20%~35%
queue和TTFT高
CPU throttling明显
```

**证据链**

- API server/Pod CPU usage 与 CFS throttle；
- prompt token histogram；
- tokenizer/renderer timing；
- GPU kernel timeline 的批准采样；
- queue time 与 prefill time；
- Node CPU NUMA/oversubscription。

**错误结论**

“再增加更多 GPU。”frontend 供不上 GPU，可能让更多 GPU 一起空闲。

**根因示例**

CPU limit 太低，高并发 JSON/tokenization 阻塞；GPU 在等 input。

**止血与修复**

按 benchmark 提高 CPU request/limit 或拆分 frontend；限制异常长 prompt；优化入口处理。变更后同时看 GPU util、TTFT 和 token/s，避免只把 CPU 问题推到 queue 后面。

**预防**

容量基线必须包含 CPU 配置与 throttle。

### 17.8 场景八：RollingUpdate 卡在新 Pod Pending

**现象**

```text
old replicas=2 Ready
new replicas=1 Pending
rollout不前进
event=Insufficient nvidia.com/gpu
```

**证据链**

- effective `maxSurge/maxUnavailable`；
- 当前/峰值 GPU 需求；
- Node allocatable/allocated；
- quota；
- topology/PVC；
- rollout revision 与 progress。

**错误结论**

“scheduler 有 bug。”`2 + surge 1` 本来就需要第三张可放置 GPU。

**止血与修复**

选择预留 surge GPU，或在确认单副本容量后审批 `maxSurge=0,maxUnavailable=1`。不要同时手工删 Pod、scale 和让 autoscaler 竞态。

**预防**

发布单同时记录 `GPU_controller_plan_upper`、含 terminating 持卡数 `T` 的 `GPU_instantaneous_held`，以及最小可用容量。

### 17.9 场景九：共享 GPU 上两个实例都配 `0.92`

**现象**

```text
单独启动A正常
单独启动B正常
同时启动时profile或运行随机OOM
```

**证据链**

- 平台是否启用 time-slicing/MPS/其他共享；
- 同一 GPU UUID 的 process/memory inventory；
- A/B `gpu_memory_utilization`；
- 启动重叠时间；
- 每实例 weight/KV/graph；
- Device Plugin 逻辑资源语义。

**错误结论**

“0.92 是默认，所以两个都安全。”参数不会跨实例协调。

**根因**

A/B 各自把同卡 92% 当成自己的预算。

**止血与修复**

优先恢复整卡独占或 MIG 隔离；若业务明确接受共享，按总显存、峰值与干扰重新分配预算并压测，不承诺硬隔离。

**预防**

把共享策略与 vLLM 配置放进同一个 capacity contract。下一课会继续展开。

### 17.10 场景十：TP4 能启动，但比 TP2 更慢

**现象**

```text
所有rank Ready
无CUDA OOM
TP4 output token/s低于预期
TTFT/ITL更差
```

**证据链**

- rank/device/GPU UUID 映射；
- NVLink/PCIe topology；
- NCCL transport；
- 是否回落 `NET/Socket`；
- 每 rank 时间与最慢 rank；
- CPU/NUMA/NIC affinity；
- 同一 workload 的 TP2 基线。

**错误结论**

“四张卡必然是两张卡的两倍。”

**根因示例**

四卡跨两个弱互联 topology group，collective 成本和同步等待吞掉计算收益。

**止血与修复**

恢复 TP2 容量；调整 GPU placement/topology；评估 PP 或独立 DP。不要仅以模型能启动验收 TP。

**预防**

发布门禁包含 scaling efficiency、NCCL transport 与 tail latency。

---

## 18. 值班一页纸

### 18.1 前五分钟：冻结身份，不做变更

```text
1. 固定Context/Namespace/Pod/PodUID/Node/NodeUID
2. 固定imageID、model/tokenizer revision、effective args
3. 标记故障起止、发布/扩缩容/Node事件
4. 停止并行手工操作
5. 运行第14节只读脚本
```

### 18.2 按最早失败层分流

| 最早失败 | 先查 | 不要先做 |
|---|---|---|
| Pending | scheduler event、GPU allocatable、topology/quota/PVC | 查 vLLM log |
| ContainerCreating | image、mount、sandbox、Device Plugin/runtime | 加大 probe |
| Running 不 Ready、无重启 | 启动阶段、`/health`、startup budget | scale traffic |
| 反复重启 | lastState、previous log、probe event | 只看 current log |
| CUDA OOM | 阶段、HBM、其他进程、配置 | 调 Pod memory |
| OOMKilled | cgroup/host memory、`/dev/shm` | 调 GPU utilization |
| Ready 但 SLO 差 | client/gateway、queue、KV、CPU/GPU | 让 readiness 跟 queue 抖 |
| TP/PP 卡住 | rank/world/device/NCCL/topology | 逐个重启 rank |

### 18.3 Ready 但慢：固定诊断顺序

```text
client/gateway可用性与TTFT
  -> request length/arrival分布
  -> queue time + waiting reason
  -> running/KV/preemption
  -> prefill/decode/ITL/E2E
  -> CPU/throttle/memory/network
  -> GPU/DCGM/NCCL/topology
```

### 18.4 四个停手条件

立即停止新增负载或发布动作，并走事件升级：

- Xid/ECC/device lost；
- queue 持续发散且 overload policy 失效；
- CUDA OOM/OOMKilled 重复；
- 身份、指标或时钟无法确认。

### 18.5 交接最小字段

```text
incident window
fixed five-part identity
image/model/tokenizer revision
GPU/driver/node class
first failing layer
lastState + restart count
startup stage
client and server SLI
waiting reason/KV/preemption
approved actions already taken
current risk and next evidence
```

没有证据的判断写“假设”，不能写“根因”。

---

## 19. 学习深度与源码停损线

### 19.1 S1：必须能独立值班

必须掌握：

- `v0.25.0` 版本、MRv2 默认和旧 PagedAttention 资料断点；
- request → waiting → schedule → worker → output；
- prefill/decode/continuous batching 的运维语义；
- 显存科目、`gpu_memory_utilization`、`kv_cache_memory_bytes`；
- CUDA OOM 与 OOMKilled 分流；
- startup/readiness/liveness 边界；
- Counter/Gauge/Histogram 与 raw series；
- TTFT/ITL/TPOT/E2E/throughput；
- queue scaler、GPU rollout 峰值；
- 单 GPU、TP、PP、DP 的选择；
- API、cache、remote code、内部网络安全。

验收标准：能对第 17 节任一场景给出“证据、非证据、止血、根修、预防”。

### 19.2 S2：定向读源码

必须亲自打开并追：

```text
api_server.build_async_engine_client*
  -> AsyncLLM.add_request/generate/check_health
  -> EngineCore.add_request/step/_initialize_kv_caches
  -> Scheduler.add_request/schedule/update_from_output
  -> Worker.init_device/load_model/determine_available_memory
  -> GPUModelRunner.load_model/profile/capture
  -> PrometheusStatLogger
```

读完要能解释：

1. request 何时进入 waiting；
2. 一个 engine step 做哪三件事；
3. 可用 KV memory 从哪几项扣出；
4. health 为什么不等于 SLO；
5. metric name/type/label 从哪里定义。

### 19.3 S3：本课允许略读

可以后续再深挖：

- CUDA/Triton kernel；
- attention backend；
- MRv2 内部 batch descriptor；
- speculative decoding 算法；
- disaggregated prefill/KV connector；
- expert parallel/MoE；
- Ray 内部调度。

它们只有在生产配置实际启用或证据指向时才升级为 S1/S2。

### 19.4 Java 经验的使用边界

可以迁移：

- 线程池 queue/active/reject 的饱和思路；
- JVM warm-up 对应“探针来自实测”；
- Deployment rollout、drain、error budget；
- 指标类型与尾延迟。

不可迁移：

- 把 vLLM scheduler 当固定线程池；
- 把 GPU HBM 当 Java heap；
- 把 `/health` 当业务 transaction；
- 假设加副本不受 GPU/模型冷启动限制；
- 假设更多 GPU 线性加速。

---

## 20. 自测题

### 20.1 问题

1. 本课冻结的 vLLM tag、release commit 和事实核对日期是什么？
2. `v0.25.0` dense model 默认 runner 有什么变化？“PagedAttention removed”应怎样解释？
3. 写出 `EngineCore.step()` 的三个核心动作。
4. 为什么不能把当前 scheduler 讲成两个互斥的大阶段？
5. continuous batching 与固定 batch 的根本区别是什么？
6. `gpu_memory_utilization=0.8` 能否解释成“KV cache 占 80%”？为什么？
7. 同时设置 `kv_cache_memory_bytes` 后，`gpu_memory_utilization` 怎样影响 KV cache？
8. `max_model_len=auto` 能否替代产品请求上限与容量规划？
9. `/health 200` 能证明和不能证明什么？
10. startup、readiness、liveness 各自的失败动作与适用语义是什么？
11. CUDA OOM 与 `lastState.reason=OOMKilled` 的第一证据分别是什么？
12. 文档写 `vllm:num_preemptions`，raw series 为什么可能是 `vllm:num_preemptions_total`？
13. 为什么 `vllm:request_success_total` 不能直接叫成功请求数？
14. `waiting_by_reason` 的两个固定 reason 是什么？它们与总 waiting 什么关系？
15. TTFT p99 的 PromQL 为什么必须保留 `le`？
16. 一个 metric series 不存在时，为什么不能当作零？
17. per-request `metrics` 位于响应哪里？五个字段是什么？
18. `n>1`、multiple prompts 和 streaming 对 per-request metrics 有什么限制？
19. client TTFT 与 `time_to_first_token_ms` 的边界有什么不同？
20. 为什么 aggregate output token/s 上升不一定是交互服务优化成功？
21. queue scaler 为什么要除以 Ready replica？零 Ready 怎样处理？
22. 两副本、两张 GPU、`maxSurge=1,maxUnavailable=0` 为什么可能死锁？
23. hostname topology spread 同时配置 `maxSkew: 1`、`minDomains: 2`、`DoNotSchedule` 时，为什么可能让第二副本 Pending？
24. TP、PP、DP 分别优先解决什么问题？
25. `VLLM_API_KEY`、`trust_remote_code` 和共享 cache 各有什么关键安全边界？
26. 只读取证为什么必须固定 Pod UID 与 Node UID，而不只固定名字？

### 20.2 答案

1. `v0.25.0`，commit `702f481`，核对日 `2026-07-14`；release 日 `2026-07-11`。
2. dense 默认 MRv2，主入口为 `vllm/v1/worker/gpu/model_runner.py`。旧 PagedAttention 实现被移除，不等于块化/paged KV cache 概念消失。
3. `scheduler.schedule() -> model_executor.execute_model() -> scheduler.update_from_output()`。
4. scheduler 按每请求已计算 token 与目标 token 的差额，在每个 step 分配 token；同一步可混合 decode、chunked prefill 等工作。
5. continuous batching 每个 engine step 重新组合运行/新接纳请求；固定 batch 则让一组请求长期绑定。
6. 不能。它是单 instance 的目标 GPU memory budget 比例，权重、activation、graph 等先占用，剩余才规划 KV；也不是硬隔离。
7. 显式 KV bytes 覆盖 KV 自动推导，worker 仍可能做 profile/compile，但不会把二者解释成“取最小”。
8. 不能。auto 只在显存约束下适配长度；产品仍要限制 prompt/output、并发和请求放大项并压测。
9. 能证明 health route 可响应且 engine client 未报告已知死亡；不能证明合成推理、queue、TTFT/ITL、模型质量、网关或产品 SLO。
10. startup 达阈值重启，保护但限制冷启动；readiness 摘流量；liveness 重启失活实例，只应在“重启有恢复价值”且阈值经验证时启用。
11. CUDA OOM 看 PyTorch/CUDA error 与 GPU memory/阶段；OOMKilled 看 container lastState、exit/cgroup memory evidence。
12. Python Prometheus Counter exposition 通常自动加 `_total`；查询必须以该实例 `/metrics` 为准。
13. 源码按 FinishReason 计 `stop/length/abort/error/repetition` 等完成事件；名字有误导性，且缺 gateway/client 全请求分母。
14. `capacity` 与 `deferred`；二者之和应与总 waiting 对齐。
15. `histogram_quantile` 需要按 bucket 上界重建累计分布；聚合丢 `le` 就没有桶结构。
16. 可能是 scrape/RBAC/label/name/version/重启问题；零是观测值，missing 是观测链不完整。
17. 顶层扩展字段 `metrics`，与 `usage` 同级；字段是 `time_to_first_token_ms`、`generation_time_ms`、`queue_time_ms`、`mean_itl_ms`、`tokens_per_second`。
18. `n>1` 和 multiple prompts 时 metrics object 为 null；stream 只在最终 usage chunk 同一响应对象附带，client 需 include usage 或 server force flag。
19. per-request TTFT 从 scheduled 到 first output，不含 queue；client TTFT 还可能含网络、gateway、tokenization、queue 和 flush。
20. scheduler 可能用更大批次换吞吐，导致 TTFT/ITL/E2E 尾部恶化；目标由服务等级决定。
21. 总 queue 要按可服务单元归一；零 Ready 必须独立高优告警，`clamp_min` 只防除零。
22. 新 Pod 需要第三张 GPU，旧 Pod 又不允许减少，双方都不能推进。
23. `minDomains: 2` 要求至少两个合格 hostname topology domain；单 GPU Node/单域下第二个副本会因 skew 约束 Pending。没有 `minDomains: 2` 时，不能只凭 `DoNotSchedule` 宣称已强制跨 Node。
24. TP 切同层 tensor、常用于单卡放不下；PP 切 layer stage、可跨节点；DP 用多个副本/rank 处理不同请求以扩吞吐。
25. API key 不保护全部路由；remote code 是模型仓库 Python 执行决策；cache 被假定可信且可能缺完整性校验，不能让不可信 writer 共享。
26. Pod/Node 可同名重建，UID 才绑定对象 incarnation；否则会把不同实例的状态、event、log 和 metrics 混在一起。

---

## 21. 固定版本官方资料索引

### 21.1 Release、架构与配置

- [vLLM v0.25.0 release](https://github.com/vllm-project/vllm/releases/tag/v0.25.0)
- [vLLM v0.25.0 source tree](https://github.com/vllm-project/vllm/tree/v0.25.0)
- [Architecture Overview v0.25.0](https://docs.vllm.ai/en/v0.25.0/design/arch_overview/)
- [Optimization and Tuning v0.25.0](https://docs.vllm.ai/en/v0.25.0/configuration/optimization/)
- [Conserving Memory v0.25.0](https://docs.vllm.ai/en/v0.25.0/configuration/conserving_memory/)
- [CacheConfig API v0.25.0](https://docs.vllm.ai/en/v0.25.0/api/vllm/config/cache/)
- [ModelConfig API v0.25.0](https://docs.vllm.ai/en/v0.25.0/api/vllm/config/model/)
- [Parallelism and Scaling v0.25.0](https://docs.vllm.ai/en/v0.25.0/serving/parallelism_scaling/)

### 21.2 指标、请求与安全

- [Production Metrics v0.25.0](https://docs.vllm.ai/en/v0.25.0/usage/metrics/)
- [Per-Request Metrics v0.25.0](https://docs.vllm.ai/en/v0.25.0/features/per_request_metrics/)
- [Security v0.25.0](https://docs.vllm.ai/en/v0.25.0/usage/security/)
- [AsyncLLM API/source v0.25.0](https://docs.vllm.ai/en/v0.25.0/api/vllm/v1/engine/async_llm/)
- [Scheduler API/source v0.25.0](https://docs.vllm.ai/en/v0.25.0/api/vllm/v1/core/sched/scheduler/)
- [GPU Worker API/source v0.25.0](https://docs.vllm.ai/en/v0.25.0/api/vllm/v1/worker/gpu_worker/)
- [MRv2 GPU Model Runner API/source v0.25.0](https://docs.vllm.ai/en/v0.25.0/api/vllm/v1/worker/gpu/model_runner/)

### 21.3 本课主线固定源码

- [api_server.py @ v0.25.0](https://github.com/vllm-project/vllm/blob/v0.25.0/vllm/entrypoints/openai/api_server.py)
- [health.py @ v0.25.0](https://github.com/vllm-project/vllm/blob/v0.25.0/vllm/entrypoints/serve/instrumentator/health.py)
- [async_llm.py @ v0.25.0](https://github.com/vllm-project/vllm/blob/v0.25.0/vllm/v1/engine/async_llm.py)
- [core.py @ v0.25.0](https://github.com/vllm-project/vllm/blob/v0.25.0/vllm/v1/engine/core.py)
- [scheduler.py @ v0.25.0](https://github.com/vllm-project/vllm/blob/v0.25.0/vllm/v1/core/sched/scheduler.py)
- [gpu_worker.py @ v0.25.0](https://github.com/vllm-project/vllm/blob/v0.25.0/vllm/v1/worker/gpu_worker.py)
- [MRv2 gpu/model_runner.py @ v0.25.0](https://github.com/vllm-project/vllm/blob/v0.25.0/vllm/v1/worker/gpu/model_runner.py)
- [metrics/loggers.py @ v0.25.0](https://github.com/vllm-project/vllm/blob/v0.25.0/vllm/v1/metrics/loggers.py)
- [config/cache.py @ v0.25.0](https://github.com/vllm-project/vllm/blob/v0.25.0/vllm/config/cache.py)
- [config/model.py @ v0.25.0](https://github.com/vllm-project/vllm/blob/v0.25.0/vllm/config/model.py)
- [kv_cache_utils.py @ v0.25.0](https://github.com/vllm-project/vllm/blob/v0.25.0/vllm/v1/core/kv_cache_utils.py)

### 21.4 示例工件与模型

- [vllm/vllm-openai v0.25.0 Docker Hub artifact page](https://hub.docker.com/layers/vllm/vllm-openai/v0.25.0)
- [Qwen3-0.6B fixed model revision](https://huggingface.co/Qwen/Qwen3-0.6B/tree/9d4bfd9a94aa5f2ab18d77fa457c306da0b8e439)
- [Qwen3-0.6B fixed safetensors object](https://huggingface.co/Qwen/Qwen3-0.6B/blob/9d4bfd9a94aa5f2ab18d77fa457c306da0b8e439/model.safetensors)

示例 artifact 只服务于本章结构演示。后续复现必须重新核对 registry manifest、architecture、SBOM、driver/CUDA compatibility 和模型许可。

### 21.5 本地 Kubernetes probe 链固定快照

本地 `kubernetes` 源码快照：

```text
301946d15e67a4a2e8a5fb8292eb836acd366d78
```

- [prober/worker.go @ 301946d](https://github.com/kubernetes/kubernetes/blob/301946d15e67a4a2e8a5fb8292eb836acd366d78/pkg/kubelet/prober/worker.go)
- [kubelet.go @ 301946d](https://github.com/kubernetes/kubernetes/blob/301946d15e67a4a2e8a5fb8292eb836acd366d78/pkg/kubelet/kubelet.go)
- [kuberuntime_manager.go @ 301946d](https://github.com/kubernetes/kubernetes/blob/301946d15e67a4a2e8a5fb8292eb836acd366d78/pkg/kubelet/kuberuntime/kuberuntime_manager.go)

完整 probe 讲义见[第 13 课](./13_kubelet_JavaPod_Running不Ready与重启_probe_statusManager_PLEG.md)。

---

## 22. 章末能力与下一课

学完本课，应该能独立完成：

```text
[ ] 固定vLLM image、tag、model和tokenizer revision
[ ] 从HTTP请求追到EngineCore、Scheduler、Worker和MRv2
[ ] 解释prefill/decode/continuous batching
[ ] 建立权重、activation、KV、graph、buffer与headroom账本
[ ] 分流CUDA OOM、OOMKilled与GPU硬件故障
[ ] 用实测冷启动设计startup/readiness，谨慎opt-in liveness
[ ] 正确查询Counter/Gauge/Histogram和missing series
[ ] 定义client TTFT、ITL/TPOT、E2E、吞吐与error budget
[ ] 用queue/SLO/GPU供给设计扩缩容
[ ] 算出rolling update GPU峰值与容量下界
[ ] 选择单GPU、TP、PP或DP并验证scaling efficiency
[ ] 识别API key、remote code、cache、media与内部网络风险
[ ] 用固定PodUID/NodeUID做只读取证
[ ] 在审批的隔离环境做可复现实验
```

本课最终证据链：

```text
固定工件与身份
  -> GPU被正确分配
  -> 模型按阶段加载
  -> 显存profile/KV/graph成立
  -> startup后readiness接流量
  -> request进入waiting并被逐step调度
  -> worker/MRv2执行并流式输出
  -> client与server指标共同证明SLO
  -> queue、GPU供给和冷启动约束扩缩容/发布
```

下一课进入资源共享与经济性：同一物理 GPU 如何通过 MIG 或 time-slicing 暴露不同逻辑资源，queue、多租户 quota、隔离、SLO 和成本怎样重新计算。继续阅读[第 21 课：MIG、time-slicing、队列、多租户、配额与成本](./21_MIG_time-slicing_队列_多租户_配额与成本.md)。
