# AlphaMind Ubuntu 24.04 目标机部署手册

> 本文提供可执行交付步骤；当前仓库的最终 PASS 仍必须在客户 Ubuntu 24.04 + 目标 GPU 上重新产生，不能用 Windows 结果替代。

## 1. 最低资源

- Ubuntu 24.04 LTS x86_64
- Docker Engine 27+ 与 Compose plugin 2.30+
- NVIDIA Driver/CUDA 版本按目标模型和 PyTorch 官方矩阵选择
- 聊天模型按客户说明使用 vLLM `qwen3-14b`；显存、量化和 max-model-len 需按客户 GPU 实测核定
- 内存 >= 32GB，交付数据盘 >= 500GB

## 2. Docker 与 NVIDIA 前置检查

```bash
docker version
docker compose version
nvidia-smi
```

若模型服务在宿主机运行，不要求容器使用 GPU；WeKnora 容器通过交付 overlay 中的：

```yaml
extra_hosts:
  - "host.docker.internal:host-gateway"
```

访问宿主机模型 API。

## 3. 安装目录

```bash
sudo useradd --system --create-home --shell /usr/sbin/nologin alphamind || true
sudo mkdir -p /opt/alphamind /opt/alphamind/models /etc/alphamind /var/cache/alphamind /var/log/alphamind
sudo chown -R alphamind:alphamind /opt/alphamind /var/cache/alphamind /var/log/alphamind
sudo chmod 750 /etc/alphamind
```

把仓库放到 `/opt/alphamind/app`，共享模型服务放到 `/opt/alphamind/qwen3-service`，模型权重放到 `/opt/alphamind/models`。

## 4. 模型服务

### 4.1 qwen3-14b vLLM 聊天服务

客户机按实际模型目录启动 vLLM，推荐固定 served model name：

```bash
python -m vllm.entrypoints.openai.api_server \
  --host 0.0.0.0 \
  --port 8000 \
  --model Qwen/Qwen3-14B \
  --served-model-name qwen3-14b \
  --trust-remote-code \
  --max-model-len 32768 \
  --gpu-memory-utilization 0.90
curl --fail http://127.0.0.1:8000/v1/models
```

如使用 `--api-key`，同步写入 `.env.phase3-precision` 的 `LLM_API_KEY`。详见 `docs/ALPHAMIND_QWEN3_14B_VLLM.md`。

### 4.2 Qwen3 embedding/reranker 共享服务

```bash
cd /opt/alphamind/qwen3-service
sudo -u alphamind python3 -m venv .venv
sudo -u alphamind .venv/bin/pip install -r requirements.txt
sudo cp /opt/alphamind/app/deploy/systemd/qwen3.env.example /etc/alphamind/qwen3.env
sudo chmod 600 /etc/alphamind/qwen3.env
sudo cp /opt/alphamind/app/deploy/systemd/alphamind-qwen3.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now alphamind-qwen3
curl --fail http://127.0.0.1:8010/health
```

先修改 `/etc/alphamind/qwen3.env` 中的 API key 和模型路径。模型权重必须提前下载并做 SHA-256 清单；生产机建议启用 `HF_HUB_OFFLINE=1`、`TRANSFORMERS_OFFLINE=1`。

## 5. AlphaMind 配置

```bash
cd /opt/alphamind/app
cp .env.phase3-precision.example .env.phase3-precision
chmod 600 .env.phase3-precision
```

必须替换全部密码、AES key、JWT、MinIO、Neo4j 和 API key。以下命令会拒绝空值、占位符和过短密钥：

```bash
python3 scripts/alphamind_delivery.py preflight
```

## 6. 启动和验收

```bash
python3 scripts/alphamind_delivery.py start
python3 scripts/alphamind_delivery.py status
python3 scripts/alphamind_delivery.py acceptance
```

交付控制器使用：

1. `docker-compose.yml`
2. `docker-compose.phase2-advanced.yml`
3. `docker-compose.phase3-precision.yml`
4. `docker-compose.delivery.yml`

第四层固定镜像 digest、日志轮转、资源上限及 Linux `host-gateway`。

## 7. 防火墙

只向受信网络开放前端端口。Qdrant、Neo4j、MinIO、PostgreSQL、Redis 和模型端点默认仅本机或 Compose 网络可见。

```bash
sudo ufw default deny incoming
sudo ufw allow from <管理网段> to any port 18088 proto tcp
sudo ufw enable
```

## 8. 备份恢复

默认备份写到仓库外：

```bash
python3 scripts/alphamind_delivery.py backup
python3 scripts/alphamind_delivery.py verify-backup ../AlphaMind-backups/alphamind-<UTC时间戳>
python3 scripts/alphamind_delivery.py restore ../AlphaMind-backups/alphamind-<UTC时间戳> \
  --confirm-restore alphamind-<UTC时间戳> --execute-staging
python3 scripts/alphamind_delivery.py validate-restore alphamind-<UTC时间戳>
```

详见 `docs/ALPHAMIND_BACKUP_RESTORE.md`。

## 9. Ubuntu 终验记录

目标机必须保存：

- `uname -a`、`docker version`、`docker compose version`、`nvidia-smi`
- `delivery/release-manifest.json`
- `dataset/phase3_runs/phase3_quality_gate_latest.json`
- 全量测试输出
- 真实检索负载报告
- 多跳非空报告
- 备份及隔离恢复报告

以上材料未在目标机产生前，Ubuntu 24.04 条件项保持未完成。
