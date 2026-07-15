# AlphaMind Phase3 Precision 交付包

本目录对应《金融系统报价单(终版)》的“方案三：终极高精版”。

## 交付状态

当前为交付加固阶段。准确范围与条件项见：

- `../docs/ALPHAMIND_DELIVERY_ACCEPTANCE_MATRIX.md`
- `../docs/ALPHAMIND_PHASE3_ACCEPTANCE_REPORT.md`
- `../docs/ALPHAMIND_PHASE3_PRECISION_RUNBOOK.md`

不得把以下条件项表述为当前已完成：

- 客户 Ubuntu 24.04 主机实机验收；
- 客户机 vLLM `qwen3-14b` 尚未在本机 8000 端口完成终验；
- 报价单原 35B 聊天模型已按客户说明替换为 `qwen3-14b`，Embedding 8B 仍需目标硬件复验（Reranker 0.6B 已按报价型号复验）；
- 未配置 MinerU/OpenDataLab 视觉端点时的扫描件 OCR；
- 尚未导入的客户最终私有语料 Recall/MRR；
- 客户 Ubuntu 目标机上的驱动/CUDA 参数调优（当前仅验证 Windows 10 + Docker Desktop 的 8GB GPU 基线）；
- 客户企业微信/内部文档尚未提供，因此不能把“客户数据已脱敏”标为完成。

## 可复现发布清单

`manifest.json` 不是手工维护的文件列表，必须由脚本生成并校验：

```bash
python scripts/alphamind_release_manifest.py generate
python scripts/alphamind_release_manifest.py verify
```

校验为 fail-closed，覆盖：

- `.env.phase3-precision.example`、三层 Compose、builtin models、process/retrieval/KB 配置；
- 金融词典、固定评测集、验收报告、bulk ingest、healthcheck、全部 `test_alphamind*.py`；
- 每个交付文件的原始字节大小与 SHA-256；
- Git commit SHA、dirty 状态和 dirty 条目摘要；
- 配置、数据集、验收证据的聚合 SHA-256；
- 验收 run ID（旧报告无 `run_id` 时由报告 SHA-256 确定性派生）；
- Python/Go/前端锁文件以及 Docker、模型服务依赖。

只要文件、Git 状态或验收证据发生变化，旧清单校验就会失败；应重新审查变更后再生成，禁止手改 hash。Phase3 Python 依赖安装命令：

```bash
python -m pip install -r delivery/requirements-phase3.txt
```

## 最短操作路径

在项目根目录执行：

```bash
python scripts/alphamind_delivery.py preflight
python scripts/alphamind_delivery.py start
python scripts/alphamind_delivery.py status
python scripts/alphamind_delivery.py acceptance
```

当前访问地址：

- WeKnora：`http://localhost:18088`
- API：`http://localhost:18080`
- Neo4j Browser：`http://localhost:17474`
- Neo4j Bolt：`neo4j://localhost:17687`
- MinIO Console：`http://localhost:19001`

## 日常启停

```bash
python scripts/alphamind_delivery.py start
python scripts/alphamind_delivery.py status
python scripts/alphamind_delivery.py logs --tail 200
python scripts/alphamind_delivery.py stop
```

`stop` 只停止容器，不删除卷。禁止使用 `docker compose down -v`。

## 备份

```bash
python scripts/alphamind_delivery.py backup
python scripts/alphamind_delivery.py verify-backup ../AlphaMind-backups/alphamind-<timestamp>
```

恢复属于危险操作，必须先阅读：

`../docs/ALPHAMIND_BACKUP_RESTORE.md`

## 客户日常操作

1. 打开 WeKnora Web UI。
2. 选择 `AlphaMind Phase3 Precision Financial Graph RAG` 知识库。
3. 上传 Markdown/MHTML；复杂 PDF 建议先通过 `alphamind_phase3_pdf.py` 预处理。
4. 等待文档状态变成 `completed`。
5. 发起问题并检查回答中的来源引用。
6. 板块、产业链和企业关系直接在 Neo4j Browser 查看，查询模板见 `../docs/ALPHAMIND_RELATION_GRAPH_NEO4J.md`。

## 交付验收命令

```bash
python scripts/alphamind_release_manifest.py verify
python scripts/alphamind_phase3_quality_gate.py \
  --sample-pdf 'D:/AlphaMind/数据/0710/半导体券商研究报告/产业链/A股/卓胜微_300782/卓胜微_300782_2024年报.pdf' \
  --max-pages 30 --live --concurrency 4 --requests 40 --keep-going
```

硬门禁：

- 数值保留率 >= 0.98；
- Recall@10 >= 0.90；
- MRR >= 0.90；
- citation doc recall >= 0.95；
- 无证据拒答率 = 1.00；
- 错误率 <= 1%；
- p95 <= 3000ms；
- 所有 Graph 结论均有证据字段。

## 凭据安全

- 私有配置只放 `.env.phase3-precision`，不得提交 Git。
- 交付包只包含 `.env.phase3-precision.example`。
- API key 优先通过进程环境变量传入，避免出现在命令历史和进程列表。
- 正式交付前必须替换示例密码和 JWT/AES 密钥。
