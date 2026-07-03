# AlphaMind 投研 RAG 与知识图谱落地执行手册

本文把当前 `D:\AlphaMind` 的 WeKnora 代码、AlphaMind 配置与上一轮基线评测结果，整理成可执行的部署、入库、检索调优、知识图谱和评测闭环清单。

> 模型价格、可用性、上下文长度、embedding 维度都可能变化。上线前必须以模型/向量库供应商实时页面为准，本文只给模型层级与配置位置，不把价格写成事实。

## 0. 本轮结论

当前最重要的排序是：

1. **先把解析质量打稳**：已有 txt 样本明显有页眉、页脚、联系方式、版面噪声，chunking 工具推荐 fixed-size 不能直接说明最佳策略，而是在提醒“解析后的文本结构不够干净”。
2. **20GB 不要一次全量灌入**：先 5-10 篇代表性研报小样本，再 100 篇灰度批，再全量。
3. **检索必须 metadata-first + hybrid + rerank**：基线 `Recall@5=1.0` 但 `Precision@1=0.333`，说明“能找到但排不准”。
4. **知识图谱先做投研 MVP schema**：Company / Report / Broker / Analyst / FinancialMetric / Event / Industry 足够先跑通，不要一开始就做大而全本体。
5. **财务表单独结构化**：年报、季报、盈利预测表不要只 embedding；同时保留表格 JSON/CSV、结构化表、图谱三元组。

## 1. 推荐部署拓扑

本地/单机 MVP：

```text
WeKnora frontend
  ↓
WeKnora app ── Postgres/ParadeDB
  │              ├─ 元数据 / 租户 / 文档 / 可选向量索引
  │              └─ Langfuse 独立 DB（可选）
  ├─ docreader   ── PDF / Office / OCR / 表格解析
  ├─ Redis       ── 异步任务 / stream
  ├─ MinIO       ── 原始文件 / 图片 / 解析产物
  ├─ Qdrant      ── 可选专用向量库
  ├─ Neo4j       ── 投研知识图谱
  └─ Langfuse    ── trace / token / 失败分析
```

启动命令沿用现有覆盖文件：

```powershell
cd D:\AlphaMind

docker compose -f docker-compose.yml -f docker-compose.alphamind.yml `
  --profile qdrant --profile neo4j --profile minio --profile langfuse up -d
```

启动后检查：

```powershell
docker compose -f docker-compose.yml -f docker-compose.alphamind.yml ps
docker compose -f docker-compose.yml -f docker-compose.alphamind.yml logs app --tail=100
docker compose -f docker-compose.yml -f docker-compose.alphamind.yml logs docreader --tail=100
curl http://localhost:6333/collections
```

Neo4j Browser：

```cypher
MATCH (n) RETURN n LIMIT 20;
CALL db.schema.visualization();
```

## 2. `.env` 必查项

不要提交 `.env`。生产前必须替换所有密码、JWT、AES、Langfuse、MinIO、Redis、数据库等 secret。

### 2.1 图谱与检索

```env
# 向量后端：MVP 可先 postgres，若要专用向量服务再切 qdrant
RETRIEVE_DRIVER=postgres
# RETRIEVE_DRIVER=qdrant

# GraphRAG / Neo4j
ENABLE_GRAPH_RAG=true
NEO4J_ENABLE=true
NEO4J_URI=bolt://neo4j:7687
NEO4J_USERNAME=neo4j
NEO4J_PASSWORD=

# 存储
STORAGE_TYPE=minio
MINIO_ENDPOINT=minio:9000
MINIO_BUCKET_NAME=alphamind
```

### 2.2 模型声明

`config/builtin_models.yaml` 从 `.env` 注入模型信息。建议按层级评测，不要盲选：

| 用途 | MVP 层级 | 说明 |
|---|---|---|
| LLM | OpenAI-compatible / 私有网关 | 用于问答、抽取、重排解释；供应商和价格需上线前核对 |
| Embedding | 中文/多语 balanced open 或 quality API | 先固定维度，避免索引重建成本 |
| Rerank | 中文/多语 rerank | 投研问答默认启用 |

必填变量示例：

```env
LLM_MODEL_NAME=<model>
LLM_BASE_URL=<base-url>
LLM_API_KEY=
LLM_PROVIDER=openai

EMBEDDING_MODEL_NAME=<embedding-model>
EMBEDDING_BASE_URL=<base-url>
EMBEDDING_API_KEY=
EMBEDDING_PROVIDER=openai
EMBEDDING_DIMENSION=<dimension>

RERANK_MODEL_NAME=<rerank-model>
RERANK_BASE_URL=<base-url>
RERANK_API_KEY=
RERANK_PROVIDER=generic
```

## 3. 20GB 研报入库节奏

### 3.1 数据分层

```text
data/
  raw/
    broker_reports/          # 券商研报 PDF/DOCX/PPT/HTML
    annual_reports/          # 年报、半年报、季报
    announcements/           # 公告
    financial_tables/        # 财报 CSV/Excel/Parquet
    market_data/             # 行情、估值、行业分类
  processed/
    documents/               # 解析后正文
    tables/                  # 表格抽取结果
    metadata/                # 每篇文档 metadata sidecar
    entities/                # 图谱实体
    relations/               # 图谱关系
  eval/
    queries.json
    ground_truth.json
```

### 3.2 入库批次

| 阶段 | 范围 | 目标 | 放行条件 |
|---|---:|---|---|
| Smoke | 5-10 篇 | 验证解析、上传、引用、图谱开关 | app/docreader 无错误，引用可点回原文 |
| Pilot | 100 篇 | 验证批量任务、检索调优、rerank | Precision@5 >= 0.75，Recall@10 >= 0.85 |
| Scale-1 | 1000 篇 | 验证磁盘、队列、并发、Neo4j 增长 | 无明显任务堆积，P95 查询可接受 |
| Full | 20GB | 全量入库 | 有失败重试和 reparse 清单 |

### 3.3 文件上传 API

文件上传入口：

```http
POST /api/v1/knowledge-bases/{kbId}/knowledge/file
Content-Type: multipart/form-data
```

关键字段：

- `file`
- `fileName`
- `metadata`
- `tag_ids`
- `channel`
- `process_config`：JSON 字符串

失败或策略调整后，使用批量重新解析：

```http
POST /api/v1/knowledge/batch-reparse
Content-Type: application/json
```

单批最多 200 条：

```json
{
  "kb_id": "<kb-id>",
  "ids": ["<knowledge-id-1>", "<knowledge-id-2>"],
  "process_config": {}
}
```

## 4. 推荐 `process_config`

已落盘模板：`dataset/alphamind_process_config_research_report.json`。

核心参数：

```json
{
  "chunking_config": {
    "strategy": "auto",
    "chunk_size": 1000,
    "chunk_overlap": 150,
    "separators": ["\n\n", "\n", "。", "！", "？", ";", "；"],
    "languages": ["zh"],
    "enable_parent_child": true,
    "parent_chunk_size": 4096,
    "child_chunk_size": 512
  },
  "graph_enabled": true,
  "extract_config": {
    "enabled": true
  },
  "question_generation_config": {
    "enabled": true,
    "question_count": 3
  }
}
```

说明：

- PDF 研报正文：先 `auto + parent-child`。
- 表格：解析为 Markdown + JSON 双形态；结构化表另入库。
- 图表：OCR 文本、图表标题、上下文段落要绑定同一 `doc_id/page`。
- 每次只改一个变量：chunk_size / overlap / parent-child / embedding / hybrid / rerank / GraphRAG / docreader OCR。

## 5. 推荐租户级检索配置

已落盘模板：`dataset/alphamind_retrieval_config_recommended.json`。

```json
{
  "embedding_top_k": 50,
  "vector_threshold": 0.15,
  "keyword_threshold": 0.3,
  "rerank_model_id": "<fill-rerank-model-id>",
  "rerank_top_k": 15,
  "rerank_threshold": 0.2,
  "rrf_k": 60,
  "rrf_vector_weight": 0.65,
  "rrf_keyword_weight": 0.35
}
```

更新入口：

```http
PUT /api/v1/tenants/kv/retrieval-config
Content-Type: application/json
X-API-Key: <api-key>
```

调参顺序：

1. 固定解析与 chunk。
2. 固定 embedding。
3. 先调 `embedding_top_k` 和 keyword/vector threshold。
4. 再调 RRF 权重。
5. 最后调 rerank_top_k / threshold。

## 6. 知识图谱落地路线

### 6.1 MVP 节点

```text
Company(name, ticker, exchange, aliases)
Industry(name, level, taxonomy_source)
Report(doc_id, title, broker, publish_date, report_type)
Broker(name)
Analyst(name, broker)
FinancialMetric(name, period, value, unit, source_doc_id)
Event(type, event_date, description)
InvestmentView(thesis, rating, target_price, direction)
RiskFactor(name, description)
Product(name, category)
```

### 6.2 MVP 关系

```text
(:Report)-[:COVERS]->(:Company)
(:Report)-[:PUBLISHED_BY]->(:Broker)
(:Report)-[:WRITTEN_BY]->(:Analyst)
(:Company)-[:BELONGS_TO]->(:Industry)
(:Report)-[:HAS_METRIC]->(:FinancialMetric)
(:FinancialMetric)-[:ABOUT_COMPANY]->(:Company)
(:Report)-[:MENTIONS]->(:Event)
(:Event)-[:AFFECTS]->(:Company)
(:Report)-[:HAS_VIEW]->(:InvestmentView)
(:InvestmentView)-[:ABOUT_COMPANY]->(:Company)
(:Report)-[:HAS_RISK]->(:RiskFactor)
(:Company)-[:HAS_PRODUCT]->(:Product)
(:Company)-[:COMPETES_WITH]->(:Company)
(:Company)-[:SUPPLIES_TO]->(:Company)
```

### 6.3 实体规范化

必须做 alias/代码映射，否则 Neo4j 当前按 `name CONTAINS` 查询会比较脆弱。

建议规范：

```text
公司主键：ticker 优先，其次统一公司全称
显示名：公司简称
aliases：简称 / 全称 / 股票代码 / 港股代码 / 英文名
报告主键：doc_id
指标主键：company + metric + period + source_doc_id
```

### 6.4 GraphRAG 使用方式

图谱不要替代原文检索，而是产生 filter / boost：

```text
用户问题
  ↓
抽取 Company / Industry / Metric / Period / Broker
  ↓
Neo4j 找相关 Report / Metric / Event / View
  ↓
用 doc_id、ticker、publish_date 作为检索 filter 或 boost
  ↓
Dense + BM25 召回原文 chunk
  ↓
RRF merge + rerank
  ↓
带引用回答
```

## 7. 评测闭环

上一轮基线文件：

- `dataset/alphamind_chunking_eval_latest.json`
- `dataset/alphamind_design_latest.json`
- `dataset/alphamind_retrieval_eval_baseline.json`

当前基线：

| 指标 | 当前值 | 目标 |
|---|---:|---:|
| Precision@1 | 0.333 | 先提升到 0.60+ |
| Precision@5 | 0.611 | >= 0.75 |
| Recall@5 | 1.000 | 保持 |
| MRR | 0.667 | >= 0.80 |

注意：当前 retrieval evaluator 只在 4 篇 txt 样本上跑了 TF-IDF baseline，不代表生产效果；它的价值是暴露“首位排序不足”和“解析文本噪声”。

正式评测集至少覆盖：

1. 公司观点：最近 90 天券商核心分歧。
2. 指标查询：EPS / 营收 / 毛利率 / ROE。
3. 目标价变化：上调/下调与理由。
4. 行业需求：国内/海外拆分。
5. 券商对比：不同假设差异。
6. 风险因素：按行业/公司聚合。
7. 表格计算：CAGR、同比、环比。
8. 多文档推理：观点不一致的来源。

## 8. 验收清单

### 部署

- [ ] `.env` secret 已替换，未提交。
- [ ] `docker compose ... ps` 全部健康。
- [ ] Qdrant/Neo4j/MinIO/Langfuse 仅本地绑定或受控暴露。
- [ ] docreader 并发与机器 CPU/内存匹配。
- [ ] embedding 维度与向量索引一致。

### 入库

- [ ] 5-10 篇 smoke 文档成功上传。
- [ ] 引用能回到原文件/页码/段落。
- [ ] 表格能被单独抽取。
- [ ] 批量失败件有 reparse 清单。
- [ ] 100 篇 pilot 后再全量。

### 检索

- [ ] metadata 字段完整：company/ticker/broker/date/industry/report_type。
- [ ] hybrid 检索开启。
- [ ] rerank_model_id 已配置。
- [ ] `Precision@5 >= 0.75`。
- [ ] `Recall@10 >= 0.85`。

### 图谱

- [ ] `NEO4J_ENABLE=true`。
- [ ] KB `graph_enabled=true` 且 `extract_config.enabled=true`。
- [ ] Company alias/ticker 规范化。
- [ ] Report/Metric/Event 至少能入图。
- [ ] GraphRAG 结果能回填到原文 chunk。

## 9. 本轮新增落地资产

- `dataset/alphamind_neo4j_constraints.cypher`：Neo4j 约束、索引和 smoke 验收查询。
- `docs/ALPHAMIND_FINANCIAL_DATA_PIPELINE.md`：财务结构化数据接入设计，明确结构化表 + 图谱 + 原文引用三线并行。
- `dataset/alphamind_financial_schema.sql`：本地 SQLite MVP schema；生产可迁移到 Postgres/DuckDB/ClickHouse。
- `dataset/alphamind_eval_plan.md`：smoke/pilot/full 评测计划、当前基线和放行标准。
- `docs/ALPHAMIND_KB_CONFIG.json`：已与 `dataset/alphamind_process_config_research_report.json` 对齐，补齐 InvestmentView/RiskFactor/Product 等投研图谱实体。

## 10. 下一步建议

下一轮建议优先做三件事：

1. 运行 `scripts/alphamind_healthcheck.py --require-qdrant --require-neo4j --require-minio`，确认 Docker 栈、模型配置和可选依赖可用。
2. 真实创建 KB 后，用 `scripts/alphamind_bulk_ingest.py --dry-run --prefer-originals --limit 20` 检查 metadata，再上传 5-10 篇 smoke 文档。
3. 初始化 `financial_data.db` schema，并为第一批盈利预测表写 dry-run importer；结构化指标写表后再同步成 `FinancialMetric` 图谱节点。
