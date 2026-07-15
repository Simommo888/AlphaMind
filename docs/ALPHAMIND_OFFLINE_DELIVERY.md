# AlphaMind 离线镜像交付

交付镜像全部固定为 registry digest，定义在 `docker-compose.delivery.yml` 和 `scripts/alphamind_offline_images.py`。

## 导出

在有网络且已完成在线验收的机器执行：

```bash
python scripts/alphamind_offline_images.py export D:/AlphaMind-offline-images
python scripts/alphamind_offline_images.py verify D:/AlphaMind-offline-images
```

本次真实产物：

- 目录：`D:/AlphaMind-offline-images`
- 镜像数：8
- `images.tar`：3,268,902,400 bytes
- SHA-256：`556e4c339f23ac15783c294a0d961ac922e7e4140ad8faea3a07579066e0c8ee`

必须连同 `manifest.json` 一起传输。建议使用加密移动介质或客户批准的文件传输系统。

## 目标机校验和导入

```bash
python3 scripts/alphamind_offline_images.py verify /media/secure/AlphaMind-offline-images
python3 scripts/alphamind_offline_images.py load /media/secure/AlphaMind-offline-images
```

脚本在以下情况失败关闭：

- 镜像列表不是交付定义的 8 个 digest；
- `images.tar` 缺失；
- 文件大小不一致；
- SHA-256 不一致；
- `docker load` 返回非零。

模型权重不包含在 Docker 镜像包中。Qwen 模型目录必须单独生成 SHA-256 清单并通过加密介质交付；聊天模型按客户说明为 vLLM `qwen3-14b`，Embedding 8B 与 Reranker 0.6B 等权重仍应在目标 GPU 上单独验收。
