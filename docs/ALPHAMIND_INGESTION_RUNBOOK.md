# AlphaMind 研报批量入库实测 Runbook

本文用于从 0 到 1 跑通 AlphaMind 研报入库：启动 WeKnora、创建知识库、获取 API Key、dry-run、真实上传、日志检查、失败重试和验收。

## 1. 启动服务

在 `D:\AlphaMind` 执行：

```powershell
cd D:\AlphaMind

docker compose -f docker-compose.yml -f docker-compose.alphamind.yml `
  --profile qdrant --profile neo4j --profile minio --profile langfuse up -d
```

检查容器：

```powershell
docker compose -f docker-compose.yml -f docker-compose.alphamind.yml ps
```

检查关键日志：

```powershell
docker compose -f docker-compose.yml -f docker-compose.alphamind.yml logs app --tail=100
docker compose -f docker-compose.yml -f docker-compose.alphamind.yml logs docreader --tail=100
```

服务地址：

| 服务 | 地址 |
|---|---|
| WeKnora Web UI | http://localhost:8088 |
| Backend API | http://localhost:8080 |
| Qdrant REST | http://localhost:6333 |
| Neo4j Browser | http://localhost:7474 |
| MinIO Console | http://localhost:9001 |
| Langfuse | http://localhost:3000 |

## 2. 创建知识库与 API Key

1. 打开 http://localhost:8088。
2. 注册/登录管理员。
3. 配置 LLM、Embedding、Rerank 模型。
4. 创建知识库：`AlphaMind 投研研报库`。
5. 参考 `docs/ALPHAMIND_KB_CONFIG.json` 配置：
   - `vector_enabled=true`
   - `keyword_enabled=true`
   - `graph_enabled=true`
   - `chunk_size=1000`
   - `chunk_overlap=150`
   - `enable_parent_child=true`
6. 在设置里创建 API Key。
7. 记录知识库 ID（KB ID）。

不要把 API Key 写入仓库。PowerShell 临时设置：

```powershell
$env:WEKNORA_API_KEY="sk-xxxxx"
$env:WEKNORA_BASE_URL="http://localhost:8080"
```

## 2.5 真实上传前健康检查

真实上传前先跑健康检查脚本：

```powershell
python D:\AlphaMind\scripts\alphamind_healthcheck.py
```

如果你已经设置 API Key，会额外检查 WeKnora 认证接口、系统信息、docreader/parser engine 和 storage engine：

```powershell
$env:WEKNORA_API_KEY="sk-xxxxx"
python D:\AlphaMind\scripts\alphamind_healthcheck.py
```

如果本轮准备强依赖 Qdrant / Neo4j / MinIO / Langfuse，可把它们设为 required，脚本会用非 0 退出码阻止继续：

```powershell
python D:\AlphaMind\scripts\alphamind_healthcheck.py `
  --require-qdrant `
  --require-neo4j `
  --require-minio `
  --require-langfuse
```

输出含义：

| 状态 | 含义 |
|---|---|
| `PASS` | 检查通过 |
| `WARN` | 可选依赖不可达或配置缺失，不阻塞脚本退出 |
| `FAIL` | required 检查失败，先修复再上传 |
| `SKIP` | 缺 API Key 等前置条件，跳过认证检查 |

常见处理：

- `weknora_health FAIL`：先启动/重启 app，检查 `docker compose ... logs app --tail=100`。
- `weknora_system_info SKIP`：设置 `WEKNORA_API_KEY` 后重跑。
- `qdrant_collections WARN/FAIL`：确认是否真的使用 `RETRIEVE_DRIVER=qdrant`，以及 profile 是否启动。
- `neo4j_http WARN/FAIL`：确认 `NEO4J_ENABLE=true`、Neo4j profile 启动、密码正确。
- `minio_live WARN/FAIL`：确认 `STORAGE_TYPE=minio`、MinIO profile 启动。
- `langfuse_http WARN/FAIL`：如果暂不做观测可先忽略；生产建议启用。

如果健康检查显示服务超时或不可达，继续跑 Docker 栈诊断脚本：

```powershell
python D:\AlphaMind\scripts\alphamind_stack_diagnose.py --start-command
```

带短日志：

```powershell
python D:\AlphaMind\scripts\alphamind_stack_diagnose.py --include-logs --log-lines 80
```

它会检查：

- Docker CLI / daemon 是否可用。
- `docker compose config` 是否能正常渲染。
- WeKnora 核心容器是否存在且 running。
- app/frontend/Qdrant/Neo4j/MinIO/Langfuse 端口是否可连。
- app/docreader/postgres/redis/qdrant/neo4j/minio/langfuse-web 最近日志是否包含常见错误关键词。

排障顺序建议：

1. `docker_daemon FAIL`：先启动 Docker Desktop。
2. `compose_config FAIL`：检查 `.env`、compose 文件和变量格式。
3. `service_app/service_docreader/service_postgres/service_redis FAIL`：先看对应日志，不要急着上传。
4. `port_app WARN` 但 `service_app PASS`：确认 `APP_PORT` 和端口占用。
5. `qdrant/neo4j/minio` 服务缺失：确认启动命令带了对应 profile。
6. 端口都 PASS 后，再重跑 `alphamind_healthcheck.py`。

推荐启动命令：

```powershell
cd D:\AlphaMind

docker compose -f docker-compose.yml -f docker-compose.alphamind.yml `
  --profile qdrant --profile neo4j --profile minio --profile langfuse up -d
```

## 3. 先 dry-run 生成 manifest

建议优先只处理 PDF/Office 原件，避免 `.pdf` 和 `.txt` 同时入库造成重复。使用 `--prefer-originals` 可自动跳过与原件同 stem 的 txt/md/html。

```powershell
python D:\AlphaMind\scripts\alphamind_bulk_ingest.py `
  --root D:\AlphaMind\数据\数据库\Documents `
  --kb-id "dry-run-kb" `
  --dry-run `
  --prefer-originals `
  --limit 20
```

输出：

```text
D:\AlphaMind\dataset\ingest_runs\alphamind_manifest_<timestamp>.jsonl
D:\AlphaMind\dataset\ingest_runs\alphamind_ingest_<timestamp>.csv
```

检查点：

- `doc_id` 是否唯一。
- `broker` 是否能从文件名识别。
- `company` / `ticker` 是否能从 `公司(代码)` 识别。
- `publish_date` 是否正确。
- `.txt` 是否被 `--prefer-originals` 跳过。

## 4. 小样本真实上传

先上传 5-10 篇：

```powershell
python D:\AlphaMind\scripts\alphamind_bulk_ingest.py `
  --root D:\AlphaMind\数据\数据库\Documents `
  --kb-id "<你的 KB ID>" `
  --base-url http://localhost:8080 `
  --prefer-originals `
  --limit 10 `
  --sleep 0.5
```

说明：

- 默认读取 `dataset/alphamind_process_config_research_report.json`。
- 默认写 CSV 与 JSONL manifest 到 `dataset/ingest_runs/`。
- 默认 `doc_id_mode=stem-ext`，会加入扩展名和相对路径 hash，避免 PDF/TXT 同 stem 冲突。

## 5. 日志观察

上传后观察：

```powershell
docker compose -f docker-compose.yml -f docker-compose.alphamind.yml logs app --tail=200
docker compose -f docker-compose.yml -f docker-compose.alphamind.yml logs docreader --tail=200
```

重点看：

- 文件上传是否 2xx。
- docreader 是否解析成功。
- 是否出现 PDF render/OCR/timeout 错误。
- graph extraction 是否触发。
- Redis/任务队列是否积压。

如果 docreader 压力大：

- 降低脚本 `--sleep` 上传速度。
- 降低 `.env` / overlay 中 docreader worker。
- 先关 `graph_enabled` 跑纯 RAG，再逐批 reparse 图谱。

## 6. 失败重试

CSV 报告包含：

- `file_path`
- `relative_path`
- `doc_id`
- `status`
- `http_status`
- `knowledge_id`
- `message`
- `metadata_json`

如果上一轮部分成功，下一轮跳过成功件：

```powershell
python D:\AlphaMind\scripts\alphamind_bulk_ingest.py `
  --root D:\AlphaMind\数据\数据库\Documents `
  --kb-id "<你的 KB ID>" `
  --prefer-originals `
  --skip-existing-report D:\AlphaMind\dataset\ingest_runs\alphamind_ingest_<timestamp>.csv
```

如果策略改了，使用 WeKnora 批量 reparse：

```http
POST /api/v1/knowledge/batch-reparse
Content-Type: application/json
X-API-Key: <api-key>

{
  "kb_id": "<kb-id>",
  "ids": ["<knowledge-id-1>", "<knowledge-id-2>"],
  "process_config": { }
}
```

单批最多 200 条。

## 7. 图谱验收

Neo4j Browser 执行：

```cypher
MATCH (n) RETURN labels(n) AS labels, count(*) AS count ORDER BY count DESC;
MATCH ()-[r]->() RETURN type(r) AS relation, count(*) AS count ORDER BY count DESC;
```

公司覆盖：

```cypher
MATCH (r:Report)-[:COVERS]->(c:Company)
RETURN c.name, c.ticker, count(r) AS report_count
ORDER BY report_count DESC
LIMIT 20;
```

指标抽取：

```cypher
MATCH (r:Report)-[:HAS_METRIC]->(m:FinancialMetric)-[:ABOUT_COMPANY]->(c:Company)
RETURN c.name, m.name, m.period, m.value, m.unit, r.title
ORDER BY c.name, m.period
LIMIT 50;
```

如果图谱为空，检查：

- `.env`: `NEO4J_ENABLE=true`
- `.env`: `ENABLE_GRAPH_RAG=true`
- KB: `graph_enabled=true`
- KB: `extract_config.enabled=true`
- app 日志中是否有 graph extraction 错误

## 8. 检索验收

先用人工问题：

1. “这篇报告对公司的核心投资逻辑是什么？请引用原文。”
2. “某公司 2026E EPS 是多少？来自哪家券商哪份报告？”
3. “最近哪些报告上调目标价，理由是什么？”
4. “风险因素有哪些？按业务/行业/财务拆分。”

再维护正式评测集：

- `dataset/alphamind_eval_queries.json`
- `dataset/alphamind_ground_truth_template.json`

目标：

| 指标 | MVP 目标 |
|---|---:|
| Precision@5 | >= 0.75 |
| Recall@10 | >= 0.85 |
| Citation accuracy | >= 0.90 |
| Hallucination rate | <= 0.05 |

## 9. 扩大批次

推荐节奏：

```text
10 篇 smoke → 100 篇 pilot → 1000 篇 scale-1 → 全量 20GB
```

每轮记录：

- 上传文件数 / 成功数 / 失败数。
- 平均解析耗时。
- app/docreader 错误。
- Qdrant/Postgres/Neo4j 数据增长。
- 检索评测指标。

## 10. 财务结构化数据

财务报表和盈利预测表不要只上传为普通文档。先参考：

- `docs/ALPHAMIND_FINANCIAL_DATA_PIPELINE.md`
- `dataset/alphamind_financial_schema.sql`

本地 SQLite MVP 初始化示例：

```powershell
sqlite3 D:\AlphaMind\数据\数据库\financial_data.db ".read D:\AlphaMind\dataset\alphamind_financial_schema.sql"
```

如果没有 `sqlite3` CLI，也可以后续用 Python importer 初始化。结构化指标应保留 `source_doc_id/source_chunk_id/page/raw_cell`，最终回答仍需回到 WeKnora 原文 chunk 做引用。

## 11. 常见问题

| 问题 | 处理 |
|---|---|
| PDF/TXT 重复入库 | 使用 `--prefer-originals` |
| doc_id 冲突 | 默认 `--doc-id-mode stem-ext`；如仍冲突，使用 `--doc-id-mode hash` |
| 上传太快 | 增大 `--sleep`，如 `--sleep 1` |
| API Key 缺失 | 设置 `$env:WEKNORA_API_KEY` 或传 `--api-key` |
| 解析慢 | 先小批量，降低 docreader worker 或关闭图谱抽取 |
| 图谱为空 | 检查 Neo4j/env/KB graph/extract_config 四个开关，并运行 `dataset/alphamind_neo4j_constraints.cypher` 中的 smoke 查询 |
| 引用不准 | 降低 chunk_size、开启 parent-child、提高 rerank_top_k、清理解析噪声 |
