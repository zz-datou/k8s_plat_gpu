# 第三方资料与源码说明

本仓库包含面向学习的中文讲解、示意配置、命令片段，以及少量第三方项目源码摘录。第三方项目的名称、商标、源码和文档仍归各自权利人所有。

## Kubernetes

- 上游项目：<https://github.com/kubernetes/kubernetes>
- 上游许可证：Apache License 2.0
- 许可证文件：<https://github.com/kubernetes/kubernetes/blob/master/LICENSE>
- 当前讲义主要阅读基线：commit `301946d15e67a4a2e8a5fb8292eb836acd366d78`

讲义中的 Kubernetes 源码片段用于解释调用链、状态流和设计取舍。上游源码及对应版本始终是判断实现行为的权威依据。

## NVIDIA、vLLM 与其他项目

讲义引用以下项目时，正文会尽量链接固定版本源码、官方文档或上游仓库：

- NVIDIA GPU Operator：<https://github.com/NVIDIA/gpu-operator>
- NVIDIA Kubernetes Device Plugin：<https://github.com/NVIDIA/k8s-device-plugin>
- NVIDIA DCGM Exporter：<https://github.com/NVIDIA/dcgm-exporter>
- NVIDIA Container Toolkit：<https://github.com/NVIDIA/nvidia-container-toolkit>
- NVIDIA MIG Parted：<https://github.com/NVIDIA/mig-parted>
- vLLM：<https://github.com/vllm-project/vllm>
- Kueue：<https://github.com/kubernetes-sigs/kueue>

相关软件和文档分别适用各自项目所声明的许可证与使用条款。讲义中的片段可能为了教学进行连续摘选、删减、重排或增加中文注释；这种教学加工不改变上游项目的归属和许可证。

## 本仓库原创内容

当前尚未为原创中文讲解选择开放许可证。仓库保持私有时不影响个人学习；若以后改为公开仓库，应由仓库所有者明确选择文档许可证，再补充对应 `LICENSE` 文件。
