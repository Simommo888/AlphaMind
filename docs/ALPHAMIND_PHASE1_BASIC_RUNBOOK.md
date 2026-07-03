# AlphaMind 第一阶段基础部署与打通版 Runbook

本文对应 `C:\Users\59521\Desktop\金融系统报价单(终版).pdf` 中的“方案一：基础部署与打通版”。目标是跑通本地模型 API + WeKnora + 基础文本 RAG 闭环。

## 1. 阶段边界

### 本阶段做

- Ubuntu / CUDA / GPU 条件确认。
- 本地 Qwen 35B 对话模型 API 连通性验证。
- 本地 Embedding API 连通性与向量维度验证。
- 本地 Rerank API 连通性验证。
- WeKnora 基础服务部署。
- Markdown / TXT / HTML / MHTML 小样本上传。
- 基础分块、向量写入、检索、问答验证。

### 本阶段不做

- 不攻坚 PDF 解析失败。
- 不上传 PDF / DOCX / PPTX / XLSX 作为验收输入。
- 不启用 GraphRAG / Neo4j 图谱抽取。
- 不做金融专属 schema。
- 不做跨文档多跳推理。
- 不做金融词典、召回率压测或准生产调优。

当前已有 PDF 知识库的 `DOCREADER_PARSE_FAILED` 不纳入第一阶段验收；该问题归入第二阶段“金融 PDF 解析接入”。

## 2. 准备 phase1 环境变量

复制示例文件：

```powershell
cd D:\AlphaMind
copy .env.phase1-basic.example .env.phase1-basic
```

编辑 `.env.phase1-basic`，至少确认：

```env
LLM_MODEL_NAME=qwen-35b-local
LLM_BASE_URL=http://host.docker.internal:8000/v1
LLM_API_KEY=local-no-auth
LLM_PROVIDER=openai

EMBEDDING_MODEL_NAME=qwen3-embedding-local
EMBEDDING_BASE_URL=http://host.docker.internal:8001/v1
EMBEDDING_API_KEY=local-no-auth
EMBEDDING_PROVIDER=openai
EMBEDDING_DIMENSION=1024

RERANK_MODEL_NAME=qwen3-reranker-local
RERANK_BASE_URL=http://host.docker.internal:8002
RERANK_API_KEY=local-no-auth
RERANK_PROVIDER=generic
```

要求：

- `LLM_BASE_URL` 必须提供 `/v1/chat/completions`。
- `EMBEDDING_BASE_URL` 必须提供 `/v1/embeddings`。
- `RERANK_BASE_URL` 必须提供 WeKnora 兼容 rerank 接口，通常是 `/rerank`。
- 不要使用远程网关或 `alphamind_local_embedding_proxy.py` 的 hash embedding 作为正式验收。

## 3. 验证本地模型 API

```powershell
python scripts\alphamind_phase1_model_check.py --env-file .env.phase1-basic
```

通过标准：

- `chat_completion PASS`
- `embedding PASS`，且返回维度等于 `EMBEDDING_DIMENSION`
- `rerank PASS`

如果模型还没启动，此步骤会失败；这是第一阶段的前置阻塞，不应绕过。

## 4. 启动 phase1 WeKnora 栈

```powershell
docker compose --env-file .env.phase1-basic `
  -f docker-compose.yml -f docker-compose.phase1-basic.yml `
  --profile qdrant up -d
```

第一阶段只要求：

- frontend
- app
- docreader
- postgres
- redis
- qdrant

不要求：

- neo4j
- minio
- langfuse

## 5. 健康检查

```powershell
python scripts\alphamind_stack_diagnose.py --phase phase1-basic --start-command
python scripts\alphamind_healthcheck.py --phase phase1-basic --check-models
```

说明：

- `phase1-basic` 模式会跳过 Neo4j / MinIO / Langfuse required 检查。
- Qdrant 优先检查 app 容器内访问；宿主机端口只用于人工排障。
- `--check-models` 会调用 `alphamind_phase1_model_check.py`。

## 6. Phase1 样本

样本目录：

```text
dataset\phase1_samples\sample_facts.md
dataset\phase1_samples\sample_policy.txt
dataset\phase1_samples\sample_web_archive.mhtml
```

验收短语：

| 文件 | 验收短语 |
|---|---|
| `sample_facts.md` | `PHASE1-RAG-BASIC-2026` |
| `sample_policy.txt` | `BASIC-TXT-CHECKPOINT-7319` |
| `sample_web_archive.mhtml` | `MHTML-ARCHIVE-CHECK-9527` |

## 7. 可选：用 bulk ingest dry-run 预览

```powershell
python scripts\alphamind_bulk_ingest.py `
  --phase phase1-basic `
  --root dataset\phase1_samples `
  --kb-id dry-run-kb `
  --dry-run
```

`--phase phase1-basic` 会自动：

- 限制扩展名为 `.md .markdown .txt .html .mhtml`
- 使用 `dataset\alphamind_process_config_phase1_basic.json`
- 拒绝 PDF / Office / 表格文件

## 8. 端到端验收

确保 `.env.phase1-basic` 中填好了 `WEKNORA_API_KEY`，然后运行：

```powershell
python scripts\alphamind_phase1_acceptance.py --env-file .env.phase1-basic
```

脚本会：

1. 检查 WeKnora `/health`。
2. 创建或复用 `AlphaMind Phase1 Basic RAG` KB。
3. 上传 Markdown / TXT / MHTML 样本。
4. 轮询解析完成。
5. 检查 chunk。
6. 调用 hybrid search。
7. 调用 knowledge chat，验证引用和答案。

输出报告位置：

```text
dataset\phase1_runs\phase1_acceptance_<timestamp>.json
dataset\phase1_runs\phase1_uploads_<timestamp>.csv
```

## 9. 第一阶段验收标准

必须全部通过：

- 本地 chat / embedding / rerank API 检查通过。
- phase1 compose config 可渲染。
- WeKnora app health 正常。
- app 容器内 Qdrant 可访问。
- phase1 KB 创建或复用成功。
- 三个样本全部 `parse_status=completed`。
- 每个样本至少有一个 text chunk。
- hybrid search 能命中对应验收短语。
- knowledge chat 返回答案和 references。
- 不启用 Neo4j / GraphRAG / PDF 解析作为验收条件。

## 10. 常见问题

### 模型 API 检查失败

先确认本地模型服务是否已经启动，并且 endpoint 与 `.env.phase1-basic` 一致。当前报价单要求本地模型；远程模型或 hash embedding 只能用于排障，不能作为正式验收。

### Qdrant 宿主机端口不可达

第一阶段 compose 默认暴露 `127.0.0.1:6333`，但正式判断以 app 容器内访问 `qdrant:6333` 为准。

### PDF 解析失败

第一阶段不处理 PDF。继续上传 Markdown/TXT/MHTML 样本验收基础链路；PDF 解析归入第二阶段。

### Chat 没有 references

先检查 hybrid search 是否能命中样本短语；如果 search 命中但 chat 没有 references，查看 app 日志和模型回答链路。

## 11. 进入第二阶段前的交付物

第一阶段完成后应保留：

- `.env.phase1-basic`（本地私有，不提交真实密钥）
- `dataset\alphamind_phase1_basic_kb_id.txt`
- `dataset\phase1_runs\phase1_acceptance_<timestamp>.json`
- `dataset\phase1_runs\phase1_uploads_<timestamp>.csv`
- healthcheck 输出
- 模型 API 检查输出

第二阶段再处理金融 PDF、表格、GraphRAG、知识图谱和投研检索调优。
