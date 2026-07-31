# CI 构建：DNS、BuildKit 与只读 resolv.conf

本篇是 A09。它来自一次 GitLab Runner 上的真实构建故障抽象。

## 1. 现场

流水线执行：

~~~bash
docker build --network=host -t "<IMAGE>:<PIPELINE_ID>" .
~~~

构建在安装 Alpine 依赖前失败：

~~~text
/bin/sh: can't create /etc/resolv.conf: Read-only file system
~~~

Dockerfile 中有这样的“DNS 兜底”：

~~~dockerfile
RUN printf 'nameserver <PUBLIC_DNS>\n' > /etc/resolv.conf \
    && apk add --no-cache python3 make g++
~~~

第一反应很容易变成“修打包机 DNS”。但当前直接失败点不是解析超时，而是写一个只读挂载文件。

## 2. 先按错误文本定第一责任层

| 错误 | 第一判断 | 下一步 |
|---|---|---|
| `Read-only file system` | 写入动作非法 | 删除 Dockerfile 中覆写 resolv.conf 的命令 |
| `temporary error` / `no such host` | DNS 解析链 | 测宿主、Docker 网络和 daemon DNS |
| TLS timeout / certificate error | 镜像源、代理、证书或网络 | 分别测 registry 与软件源 |
| 401/403 | 鉴权或镜像源策略 | 查 credential / token / mirror |
| 拉取很慢但最终成功 | mirror 逐个失败后回退 | 查 daemon mirror 健康 |

不要把“构建步骤是安装依赖”直接等同于“根因是 DNS”。

## 3. 为什么 `/etc/resolv.conf` 不能这样写

构建引擎会为构建容器提供 DNS 配置。BuildKit 环境中 `/etc/resolv.conf` 可能作为受管理的挂载呈现，Dockerfile 的普通 `RUN` 不能把它当成镜像层里的常规文件覆盖。

而且即使某个版本恰好允许写入，把公共 DNS 烧进镜像层也不是可靠设计：

- 企业内网域名可能只能由内部 DNS 解析；
- 构建网络和运行时网络是两套上下文；
- 代理、VPN、私有 registry 和 split-horizon DNS 会被绕开；
- 明文写死的 DNS 让问题难以集中治理。

DNS 应由 Runner 宿主、Docker daemon / BuildKit builder、CI 网络或平台配置提供。

## 4. 最小修复

原来两个 `RUN` 都以写 resolv.conf 开头。删除时不能留下一个以 `&&` 开头的无效 shell 片段。

修改前：

~~~dockerfile
RUN printf 'nameserver <PUBLIC_DNS>\n' > /etc/resolv.conf \
    && sed -i 's#<UPSTREAM_MIRROR>#<APPROVED_MIRROR>#g' /etc/apk/repositories \
    && apk add --no-cache python3 make g++

COPY package.json ./
RUN printf 'nameserver <PUBLIC_DNS>\n' > /etc/resolv.conf \
    && npm config set registry <APPROVED_NPM_REGISTRY> \
    && npm install --production
~~~

修改后：

~~~dockerfile
RUN sed -i 's#<UPSTREAM_MIRROR>#<APPROVED_MIRROR>#g' /etc/apk/repositories \
    && apk add --no-cache python3 make g++

COPY package.json ./
RUN npm config set registry <APPROVED_NPM_REGISTRY> \
    && npm install --production
~~~

这一步只修复确定的只读文件错误。是否保留 `--network=host` 是另一个风险决策，要按 Runner 隔离、网络策略和实测结果处理。

## 5. 怎样证明宿主机不是本次直接根因

在允许的诊断窗口中，分四层测试。下面是模板，不要把公共地址和临时凭据直接写进共享日志。

### 5.1 宿主机

~~~bash
getent hosts <PACKAGE_HOST>
curl -fsSIL --connect-timeout 5 https://<PACKAGE_HOST>/
ip -s link
conntrack -C 2>/dev/null || true
~~~

### 5.2 Docker 默认网络与 host 网络

~~~bash
docker run --rm <KNOWN_LOCAL_IMAGE> getent hosts <PACKAGE_HOST>
docker run --rm --network=host <KNOWN_LOCAL_IMAGE> getent hosts <PACKAGE_HOST>
~~~

若命令会拉取新镜像，应先获得许可，或使用本机已有的可信诊断镜像。

### 5.3 BuildKit

使用最小、自动清理的测试构建，分别验证解析和 HTTPS，不要直接在业务 Dockerfile 里反复试错。保存 builder 类型、Docker/BuildKit 版本、网络模式和时间。

### 5.4 daemon 与镜像加速器

检查配置了哪些 registry mirror、每个 mirror 的证书、连通性和鉴权。镜像元数据获取慢可能来自多个坏 mirror 逐个超时后回退，它与 resolv.conf 只读可以同时存在，但不是同一个根因。

## 6. 为什么 `--network=host` 不是万能修复

它可能让构建步骤复用宿主网络，但不能：

- 让只读文件变成可写；
- 修复失效的 registry mirror；
- 修复 TLS 或鉴权；
- 保证所有 BuildKit driver 对 host 网络的支持和隔离方式一致；
- 替代可审计的 Runner 网络配置。

将它作为临时缓解措施时，应记录启用原因、适用 Runner、退出条件和安全评估。

## 7. 这次故障里最有价值的分账

~~~text
确定根因：Dockerfile 写只读 /etc/resolv.conf
  -> 删除两处写入并修复 RUN 链

独立发现：部分 registry mirror 证书、端口或鉴权异常
  -> 另开维护项处理 daemon 配置

当前正常：宿主 DNS、Docker bridge/host 对软件源可达
  -> 不因为“曾经怀疑 DNS”就修改生产网络
~~~

一个事故可以暴露多个问题，但复盘必须区分“本次失败的必要原因”和“顺手发现的隐患”。

## 8. 防复发检查

- 对 Dockerfile 做 lint，禁止写 `/etc/resolv.conf`、在镜像中写凭据和使用未经批准的公共 DNS；
- CI 中记录 Docker、BuildKit、base image digest 和 registry；
- 监控 Runner 的 DNS、registry mirror、磁盘、inode 和连接跟踪；
- 定期验证镜像加速器证书与鉴权；
- 使用 lockfile 和可重复安装命令；
- 将 frontend/backend CI 模板按项目类型复用，但保留构建目录、产物路径和运行时差异；
- Runner 临时检出目录不能当正式修复位置，修改必须进入代码仓库。

## 9. 与 Kubernetes / GPU 的连接

CI 构建成功只证明镜像产生，不证明 Kubernetes 节点能拉取它。运行时还要经过：

~~~text
kubelet
  -> CRI / containerd
  -> registry DNS / TLS / auth
  -> unpack snapshot
  -> Create / Start container
~~~

GPU 镜像通常更大，还包含 CUDA / framework 兼容约束。以后要额外记录 base image digest、Driver 最低版本、架构、镜像大小和节点缓存策略。不要用“构建机能跑 `nvidia-smi`”推断目标节点运行时一定兼容。

## 10. 验收题

1. 为什么看到 apk/npm 失败不能立刻把根因写成 DNS？
2. 删除写 resolv.conf 的一行时，为什么还要重写后续 `&&`？
3. registry mirror 慢与本次只读错误怎样分账？
4. `--network=host` 能解决和不能解决什么？
5. 为什么不能直接修改 Runner 的临时检出目录？
