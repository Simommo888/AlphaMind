# AlphaMind Phase3 Precision Runbook

本文对应《金融系统报价单(终版)》的“方案三：终极高精版”。Phase3 含 Phase2 全部能力，并增加复杂金融 PDF 预处理、金融证据图谱、多跳查询、金融词典混合融合和准生产质量/压测门禁。

## 1. 交付边界

已落地：

- `scripts/alphamind_phase3_pdf.py`：PDF 版面文本、表格、脚注提取；相邻页重复表头跨页拼接；Markdown 表格；文档/页/表/脚注 lineage；数值保留率。
- `scripts/alphamind_phase3_retrieval.py`：金融词典扩展、最长金融短语优先、Dense/Sparse/Graph 加权 RRF 去重融合。
- `scripts/alphamind_phase3_graph.py`：供应链交叉、机构-高管网络、指标跨期三类受控参数化多跳查询。
- `scripts/alphamind_graph_online_acceptance.py`：对真实 Neo4j 执行供应链多跳，并以非空、双边证据完整和受控语义来源作为 fail-closed 验收条件。
- `scripts/alphamind_phase3_acceptance.py`：真实 PDF → Phase3 KB → 解析 → chunk → hybrid search → 带引用 chat 的端到端验收。
- `scripts/alphamind_phase3_quality_gate.py`：配置、单测、真实 PDF、live health/load 一键门禁。
- `scripts/alphamind_phase3_load_test.py`：有界并发、p50/p95/p99、吞吐、错误率。

边界：PyMuPDF 版面管线是本机可运行默认实现；若客户要求 OCR/视觉模型对扫描件、无文本层或极端无框表格进行识别，可配置现有 DocReader ODL hybrid URL（MinerU/OpenDataLab 类服务）。未配置外部视觉解析服务时不会伪称完成 OCR。

## 2. 配置

从当前可用 Phase2 私有配置复制，保留原 AES/API key：

```powershell
copy .env.phase2-advanced .env.phase3-precision
```

再对照 `.env.phase3-precision.example` 设置：

```env
PHASE3_ENV_FILE=.env.phase3-precision
QDRANT_COLLECTION=alphamind_phase3_precision_embeddings
REDIS_PREFIX=alphamind:phase3:stream:
PHASE3_PDF_ENGINE=pymupdf-layout
PHASE3_PDF_MIN_NUMERIC_PRESERVATION=0.98
EMBEDDING_BASE_URL=http://host.docker.internal:8010/v1
RERANK_BASE_URL=http://host.docker.internal:8010
BATCH_EMBED_SIZE=1
CONCURRENCY_POOL_SIZE=1
```

共享 Qwen3 embedding/reranker 服务位于 `D:\AI-Models\qwen3-service`，启动并验证：

```powershell
curl http://127.0.0.1:8000/v1/models
D:\AI-Models\qwen3-service\start_qwen3_service.bat
curl http://127.0.0.1:8010/health
```

聊天模型按客户说明切换为客户机 vLLM 暴露的 `qwen3-14b`。推荐 vLLM 启动时固定 `--served-model-name qwen3-14b`，详见 `ALPHAMIND_QWEN3_14B_VLLM.md`。Phase3 默认配置如下：

```env
LLM_MODEL_NAME=qwen3-14b
LLM_BASE_URL=http://host.docker.internal:8000/v1
SSRF_WHITELIST=host.docker.internal
PHASE3_DIRECT_CHAT_RUNTIME=vllm
PHASE3_DISABLE_THINKING=true
PHASE3_DIRECT_CHAT_URL=http://localhost:8000/v1
PHASE3_DIRECT_CHAT_MODEL=qwen3-14b
PHASE3_EMBEDDING_UNLOAD_URL=http://localhost:8010
```

容器内 WeKnora 访问 `host.docker.internal:8000/v1`，宿主验收脚本访问 `localhost:8000/v1`。若客户实际 `/v1/models` 返回的模型 id 不是 `qwen3-14b`，必须同步修改 `LLM_MODEL_NAME` 与 `PHASE3_DIRECT_CHAT_MODEL`。引用门禁不变：无有效 `[S1..Sn]` 标记仍判定失败。

扫描件/视觉解析可选项：

```env
DOCREADER_ODL_HYBRID=on
DOCREADER_ODL_HYBRID_MODE=auto
DOCREADER_ODL_HYBRID_FALLBACK=true
DOCREADER_ODL_HYBRID_URL=<private MinerU/OpenDataLab compatible endpoint>
```

## 3. 启动

交付环境优先使用统一控制器；它会先校验 Docker、Compose、环境文件和占位密钥：

```powershell
python scripts\alphamind_delivery.py preflight
python scripts\alphamind_delivery.py start
python scripts\alphamind_delivery.py status
```

Phase3 依赖两类模型端点：vLLM `qwen3-14b`（默认 `http://127.0.0.1:8000/v1`）与共享 embedding/reranker（默认 `http://127.0.0.1:8010`）。若任一端点不可达，WeKnora 的 `/health` 仍可能正常，但真实 hybrid search 或引用回答会失败：

```powershell
curl http://127.0.0.1:8000/v1/models
D:\AI-Models\qwen3-service\start_qwen3_service.bat
curl http://127.0.0.1:8010/health
```

底层 Compose 等价命令：

```powershell
docker compose --env-file .env.phase3-precision `
  -f docker-compose.yml `
  -f docker-compose.phase2-advanced.yml `
  -f docker-compose.phase3-precision.yml `
  --profile qdrant --profile neo4j --profile minio up -d
```

只检查 Compose：

```powershell
$env:PHASE3_ENV_FILE='.env.phase3-precision'
docker compose --env-file .env.phase3-precision -f docker-compose.yml -f docker-compose.phase2-advanced.yml -f docker-compose.phase3-precision.yml --profile qdrant --profile neo4j --profile minio config --quiet
```

## 4. 复杂 PDF 预处理

```powershell
python scripts\alphamind_phase3_pdf.py `
  "D:\AlphaMind\数据\0710\半导体券商研究报告\产业链\A股\卓胜微_300782\卓胜微_300782_2024年报.pdf" `
  --out-dir dataset\phase3_runs\pdf `
  --max-pages 30 `
  --strict --require-table
```

输出：

- `*.phase3.md`：可直接入 WeKnora 的 Markdown。
- `*.phase3.manifest.json`：SHA-256、页数、表格、跨页表、脚注、解析错误、数值保留率。

严格门禁要求：有可读页、有表格、数值保留率 >= 0.98。完整交付应去掉 `--max-pages` 处理全篇；30 页用于快速回归。

## 5. Phase3 入库

Dry-run：

```powershell
python scripts\alphamind_bulk_ingest.py `
  --phase phase3-precision `
  --root dataset\phase3_runs\pdf `
  --kb-id <phase3-kb-id> `
  --limit 1 --dry-run
```

正式上传（密钥从 `.env.phase3-precision` 或当前进程的 `WEKNORA_API_KEY` 读取，不放入命令行历史）：

```powershell
$env:WEKNORA_API_KEY='<private>'
python scripts\alphamind_bulk_ingest.py `
  --phase phase3-precision `
  --root dataset\phase3_runs\pdf `
  --kb-id <phase3-kb-id> `
  --limit 1
```

## 6. 端到端验收

```powershell
python scripts\alphamind_phase3_acceptance.py `
  --pdf "D:\AlphaMind\数据\0710\半导体券商研究报告\产业链\A股\卓胜微_300782\卓胜微_300782_2024年报.pdf" `
  --max-pages 10 `
  --query "卓胜微2024年利润分配预案中，每10股派发多少现金红利？请仅根据年报原文回答并引用来源。" `
  --answer-must-contain "1.02" `
  --answer-must-contain "现金红利"
```

必须全部 PASS：复杂 PDF、KB 创建/复用、上传/复用、解析完成、chunk、hybrid search、带引用答案。`--answer-must-contain` 是可重复的内容级硬断言；检索结果中找不到同时包含全部断言的原文、引用越界或模型答错数值均按严格失败处理。

## 7. 质量门禁与压测

离线门禁（配置 + Phase3 单测 + 真实 PDF）：

```powershell
python scripts\alphamind_phase3_quality_gate.py --sample-pdf <pdf> --max-pages 30
```

在线门禁（额外检查 WeKnora/Qdrant/Neo4j/MinIO 与并发）：

```powershell
python scripts\alphamind_phase3_quality_gate.py --sample-pdf <pdf> --max-pages 30 --live
```

独立负载探针：

```powershell
python scripts\alphamind_phase3_load_test.py `
  --url http://localhost:18080/health `
  --requests 40 --concurrency 8 `
  --max-p95-ms 3000 --max-error-rate 0.01
```

实际检索压测时，把 URL 改为 KB hybrid-search endpoint，通过 `--method POST --payload @request.json` 提交固定查询；认证密钥优先放在进程环境变量 `WEKNORA_API_KEY`，不要作为命令行参数暴露在进程列表中。

## 8. 多跳查询

```powershell
# 默认只输出参数化执行计划
python scripts\alphamind_phase3_graph.py supply_chain_overlap --company-a 卓胜微 --company-b 中芯国际
python scripts\alphamind_phase3_graph.py institution_executive_network --company-a 卓胜微

# 加 --execute 后读取 .env.phase3-precision 并通过 Neo4j transaction API 执行
python scripts\alphamind_phase3_graph.py metric_period_compare --company-a 300782.SZ --metric ROE --period 2023A --period 2024A --execute

# 在线 P1 验收：默认验证 中芯国际 -> 兆易创新 -> 乐鑫科技，并写入 JSON 报告
python scripts\alphamind_graph_online_acceptance.py `
  --env-file .env.phase3-precision `
  --report dataset\alphamind_graph_online_acceptance.json
```

`supply_chain_overlap` 保留原生 `HAS_CUSTOMER/HAS_SUPPLIER` 分支，并把独立 `AlphaMindEntity` 图层的 `SUPPLIES_TO` 供应商→客户方向桥接为相同返回契约；不会改写或删除任一现有图层。脚本只允许执行三种固定意图的参数化 Cypher；用户值和图层来源值仅进入 `parameters`，不能注入任意语句。结果保留两段关系各自的 `source_doc_id/source_chunk_id/page/evidence_quote` 与 `semantic_source`。

## 9. 验收文件

- `dataset/phase3_runs/phase3_quality_gate_latest.json`
- `dataset/phase3_runs/phase3_acceptance_latest.json`
- `dataset/phase3_runs/phase3_load_latest.json`
- `dataset/phase3_runs/pdf/*.phase3.manifest.json`
- `dataset/phase3_eval_queries.json`
- `dataset/alphamind_graph_online_acceptance.json`（真实 Neo4j 供应链多跳非空与双边证据报告）

## 10. 准生产阈值

- 配置和单元测试：100% PASS。
- PDF 数值保留率：>= 0.98。
- Recall@10：>= 0.90；MRR：>= 0.90。
- citation doc recall：>= 0.95。
- no-evidence refusal：1.00。
- 健康探针 p95：<= 3000ms；错误率：<= 1%。
- Graph 结论：100% 带 source doc/chunk/page 证据。

## 11. 备份与恢复

```powershell
python scripts\alphamind_delivery.py backup
python scripts\alphamind_delivery.py verify-backup ..\AlphaMind-backups\alphamind-<timestamp>
```

恢复前必须阅读 `docs/ALPHAMIND_BACKUP_RESTORE.md`。禁止使用 `docker compose down -v`，禁止未校验哈希就覆盖当前持久卷。

## 12. 交付边界与客户手册

- 报价单逐项验收矩阵：`docs/ALPHAMIND_DELIVERY_ACCEPTANCE_MATRIX.md`
- 客户交付与运维：`docs/ALPHAMIND_DELIVERY_HANDOFF.md`
- 最短入口：`delivery/README.md`
- 板块产业链企业图：`docs/ALPHAMIND_RELATION_GRAPH_NEO4J.md`

当前 Windows 历史验收不能替代客户 Ubuntu 24.04 + vLLM `qwen3-14b` 目标机终验；扫描件 OCR 也必须在配置视觉解析端点后单独验收。
