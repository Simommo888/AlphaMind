# AlphaMind 第二阶段进阶调优与图谱接入版 Runbook

本文对应 `C:\Users\59521\Desktop\金融系统报价单(终版).pdf` 中的“方案二：进阶调优与图谱接入版”。目标是在第一阶段本地模型 + WeKnora + 基础 RAG 打通的基础上，接入金融 PDF / 简单表格解析、金融 chunking、基础 Graph RAG 与初步检索问答调优。

## 1. 阶段边界

### 本阶段做

- 继续使用本地 Qwen 35B / Embedding / Rerank API。
- 启动 WeKnora + Qdrant + MinIO + Neo4j。
- 上传小批量金融研报 PDF、年报/招股书 PDF 或简单 CSV/XLSX 表格。
- 使用金融投研 `process_config` 进行 chunking、实体关系抽取和问题生成。
- 构建基础实体关系图谱：Company / Report / Broker / FinancialMetric / RiskFactor 等。
- 通过 hybrid search 和 knowledge chat 验证 PDF/表格召回、引用问答与基础跨文档关联。
- 输出结构化 JSON / CSV 验收报告。

### 本阶段不做

- 不做全量 20GB 语料一次性导入。
- 不承诺复杂跨页无框表格、密集脚注财务表的高精度结构化解析；这属于后续高精度阶段。
- 不训练或微调模型 / reranker。
- 不做 UI 改造、生产权限治理或公网暴露。
- 不把 Langfuse 作为硬验收项；如需链路观测可另行启用。

## 2. 准备 phase2 环境变量

复制示例文件：

```powershell
cd D:\AlphaMind
copy .env.phase2-advanced.example .env.phase2-advanced
```

编辑 `.env.phase2-advanced`，至少确认：

```env
WEKNORA_BASE_URL=http://localhost:8080
WEKNORA_API_KEY=

STORAGE_TYPE=minio
RETRIEVE_DRIVER=qdrant
QDRANT_COLLECTION=alphamind_phase2_advanced_embeddings

ENABLE_GRAPH_RAG=true
NEO4J_ENABLE=true
NEO4J_URI=bolt://neo4j:7687
NEO4J_USERNAME=neo4j
NEO4J_PASSWORD=alphamind_phase2_neo4j_change_me

LLM_MODEL_NAME=qwen3.7-plus
LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
LLM_API_KEY=<private>
LLM_PROVIDER=aliyun

EMBEDDING_MODEL_NAME=text-embedding-v4
EMBEDDING_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
EMBEDDING_API_KEY=<private>
EMBEDDING_PROVIDER=aliyun
EMBEDDING_DIMENSION=1024

RERANK_MODEL_NAME=qwen3-rerank
RERANK_BASE_URL=https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank
RERANK_API_KEY=<private>
RERANK_PROVIDER=aliyun
```

要求：

- `LLM_BASE_URL` 必须提供 `/v1/chat/completions`。
- `EMBEDDING_BASE_URL` 必须提供 `/v1/embeddings`。
- `RERANK_BASE_URL` 必须提供 WeKnora 兼容 rerank 接口，DashScope 可使用完整 rerank service URL。
- `config/builtin_models.phase2-advanced.yaml` 中的模型 `source` 必须与服务类型一致；使用 DashScope / OpenAI-compatible HTTP API 时应为 `remote`，避免后端按 `local` 走 Ollama 拉模型路径。
- `TENANT_AES_KEY` 必须与既有数据库租户 API key 生成时的值一致，否则已有 `WEKNORA_API_KEY` 会认证失败。
- `NEO4J_PASSWORD`、`MINIO_SECRET_ACCESS_KEY`、`DB_PASSWORD`、`REDIS_PASSWORD`、`SYSTEM_AES_KEY`、`TENANT_AES_KEY` 应在真实环境中改成私有值。

## 3. 启动 phase2 栈

```powershell
docker compose --env-file .env.phase2-advanced `
  -f docker-compose.yml -f docker-compose.phase2-advanced.yml `
  --profile qdrant --profile neo4j --profile minio up -d
```

第二阶段要求：

- frontend
- app
- docreader
- postgres
- redis
- qdrant
- neo4j
- minio

不强制要求：

- langfuse
- searxng
- milvus / weaviate / doris

## 4. 诊断与健康检查

只打印启动命令和诊断：

```powershell
python scripts\alphamind_stack_diagnose.py --phase phase2-advanced --start-command
```

检查核心依赖：

```powershell
python scripts\alphamind_healthcheck.py `
  --phase phase2-advanced `
  --env-file .env.phase2-advanced `
  --require-qdrant `
  --require-neo4j `
  --require-minio
```

## 5. 二阶段配置文件

| 文件 | 用途 |
|---|---|
| `.env.phase2-advanced.example` | 二阶段环境变量模板 |
| `docker-compose.phase2-advanced.yml` | 二阶段 compose overlay |
| `config/builtin_models.phase2-advanced.yaml` | 二阶段内置模型配置（保持 `alphamind-*-default` ID，避免已有 KB/模型引用迁移） |
| `docs/ALPHAMIND_KB_CONFIG.phase2-advanced.json` | 二阶段 KB 创建配置 |
| `dataset/alphamind_process_config_phase2_advanced.json` | PDF/表格/GraphRAG 解析配置 |
| `dataset/alphamind_retrieval_config_phase2_advanced.json` | 初始检索与防幻觉策略 |
| `dataset/phase2_eval_queries.json` | 二阶段评测问题种子 |
| `dataset/phase2_ground_truth.json` | ground truth 占位模板 |

## 6. 小批量 dry-run 入库计划

先检查 metadata 和样本选择，不上传：

```powershell
python scripts\alphamind_bulk_ingest.py `
  --phase phase2-advanced `
  --root "D:\AlphaMind\数据\Documents(1)" `
  --kb-id dry-run-kb `
  --limit 5 `
  --dry-run
```

重点人工检查 CSV/JSONL 输出中的：

- `doc_id`
- `company`
- `ticker`
- `broker`
- `publish_date`
- `report_type`
- `source_type`
- `file_suffix`

## 7. 端到端验收

确保 `.env.phase2-advanced` 中填好了 `WEKNORA_API_KEY`，然后运行：

```powershell
python scripts\alphamind_phase2_acceptance.py `
  --env-file .env.phase2-advanced `
  --sample-root "D:\AlphaMind\数据\Documents(1)" `
  --table-root "D:\AlphaMind\数据\数据库\Documents" `
  --limit 5 `
  --table-limit 1
```

脚本会：

1. 检查 phase2 本地配置文件。
2. 检查 WeKnora `/health`。
3. 创建或复用 `AlphaMind Phase2 Advanced Graph RAG` KB。
4. 上传小批量 PDF / 简单表格样本。
5. 轮询解析完成。
6. 检查 chunks。
7. 调用 hybrid search。
8. 调用 knowledge chat，验证 references。
9. 查询 Neo4j，验证基础节点与关系。
10. 输出 JSON / CSV 报告。

输出报告位置：

```text
dataset\phase2_runs\phase2_acceptance_<timestamp>.json
dataset\phase2_runs\phase2_uploads_<timestamp>.csv
```

## 8. 第二阶段验收标准

必须全部通过或明确记录原因：

- phase2 compose config 可渲染。
- WeKnora app health 正常。
- Qdrant、Neo4j、MinIO 可访问。
- phase2 KB 创建或复用成功，且 `embedding_model_id` / `summary_model_id` 指向当前内置模型 ID。
- 小批量 PDF / 表格上传成功。
- 上传样本 `parse_status=completed`。
- 每个成功解析样本至少有一个 chunk。
- hybrid search 能召回公司、券商、报告或表格字段。
- knowledge chat 返回 references。
- Neo4j 中至少能看到实体节点与关系；若 WeKnora 使用 KB-specific `ENTITY` 标签，则以实体节点总数、关系总数，以及 `COVERS` / `PUBLISHED_BY` 关系作为 graph smoke 验收依据，不硬要求 `Company` / `Report` / `Broker` 标签计数非零。
- 基础跨文档问题至少能返回多个来源引用；如果 references 不足，必须在报告中记录为 warn/fail。

## 9. 调优顺序

1. 固定样本和 `dataset/phase2_eval_queries.json`。
2. 先跑 baseline acceptance。
3. 只调整 chunk size / overlap。
4. 再调整 vector / keyword threshold。
5. 再调整 rerank top_k / threshold。
6. 最后调整 Prompt / extract_config。

防幻觉要求：

- 没有引用时不能回答精确财务数据。
- 财务指标必须带期间、单位和来源。
- 跨文档比较必须按文档列出依据。
- 图谱只作为召回线索，最终答案必须回到原文引用。

## 10. 常见问题

### PDF 解析失败

先确认文件大小没有超过 `MAX_FILE_SIZE_MB`，再查看 docreader 日志。默认 `DOCREADER_PDF_FORCE_SCANNED=false`；如文本层质量差，可对失败样本单独开启扫描解析验证，但不要一开始全量开启。

### Neo4j 没有节点或关系

确认：

- `.env.phase2-advanced` 中 `ENABLE_GRAPH_RAG=true` 且 `NEO4J_ENABLE=true`。
- KB config 与 process config 均启用了 `graph_enabled` / `extract_config.enabled`。
- LLM 模型 API 可用于抽取。
- 文档解析已完成，而不是仍在 processing。

如果节点存在但 `Company` / `Report` / `Broker` 标签计数为 0，先检查是否为 WeKnora 的 KB-specific `ENTITY` 标签模式；只要实体节点、关系总数以及 `COVERS` / `PUBLISHED_BY` 等核心关系存在，Phase2 graph smoke 可判定通过。

### Chat 有答案但无 references

先检查 hybrid search 是否能命中样本内容；如果 search 命中但 chat 没 references，查看 app 日志和 knowledge-chat SSE 事件。

## 11. 第二阶段交付物

- `.env.phase2-advanced`（本地私有，不提交真实密钥）
- `dataset\alphamind_phase2_advanced_kb_id.txt`
- `dataset\phase2_runs\phase2_acceptance_<timestamp>.json`
- `dataset\phase2_runs\phase2_uploads_<timestamp>.csv`
- healthcheck 输出
- Neo4j graph smoke 输出
- dry-run manifest / CSV
