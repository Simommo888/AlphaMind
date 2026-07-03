# AlphaMind 投研 RAG 本地部署说明

本文件记录 AlphaMind 在 WeKnora 上的本地部署约定。目标是先跑通股票投研 RAG MVP，再逐步扩展到大量券商研报、财务报表结构化数据和知识图谱。

## 1. 已添加的本地配置

- `.env`：AlphaMind 本地环境变量，已配置 Qdrant、Neo4j、MinIO、Langfuse 相关开关。
- `docker-compose.alphamind.yml`：不修改上游 compose 的覆盖文件，追加内置模型配置挂载、本地端口绑定和 docreader 并发建议。
- `config/builtin_models.yaml`：默认空模型列表，避免 `.env` 未填模型 key 时写入无效模型。
- `docs/ALPHAMIND_RAG_ARCHITECTURE.md`：投研 RAG 架构、图谱 schema 和检索策略。
- `docs/ALPHAMIND_KB_CONFIG.json`：创建投研知识库时可参考的配置草案。
- `dataset/alphamind_eval_queries.json`：第一批评测问题模板。
- `dataset/alphamind_ground_truth_template.json`：人工标注 ground truth 模板。

## 2. 推荐启动命令

在 `D:\AlphaMind` 执行：

```powershell
docker compose -f docker-compose.yml -f docker-compose.alphamind.yml `
  --profile qdrant --profile neo4j --profile minio --profile langfuse up -d
```

访问地址：

| 服务 | 地址 |
|---|---|
| WeKnora Web UI | http://localhost:8088 |
| Backend API | http://localhost:8080 |
| Qdrant REST | http://localhost:6333 |
| Neo4j Browser | http://localhost:7474 |
| MinIO Console | http://localhost:9001 |
| Langfuse | http://localhost:3000 |

## 3. 启动后第一轮检查

```powershell
docker compose -f docker-compose.yml -f docker-compose.alphamind.yml ps
docker compose -f docker-compose.yml -f docker-compose.alphamind.yml logs app --tail=100
docker compose -f docker-compose.yml -f docker-compose.alphamind.yml logs docreader --tail=100
```

Neo4j 验证：

```cypher
MATCH (n) RETURN n LIMIT 20;
CALL db.schema.visualization();
```

Qdrant 验证：

```powershell
curl http://localhost:6333/collections
```

## 4. 首次配置顺序

1. 打开 http://localhost:8088 注册管理员账号。
2. 在 UI 中配置 LLM、Embedding、Rerank 模型。
3. 创建“AlphaMind 投研研报库”。
4. Chunking 选择 `auto`，PDF 研报建议 chunk size 800-1200、overlap 100-150、开启 parent-child。
5. 启用关键词检索、向量检索、图谱抽取。
6. 上传 5-10 篇代表性研报，先跑小样本。
7. 用 `dataset/alphamind_eval_queries.json` 中的问题手动验证召回和引用。

## 5. 注意事项

- `.env` 包含本地密码和密钥，已被 `.gitignore` 忽略，不要提交。
- 当前 `.env` 中的密码/密钥是本地开发占位值，生产必须替换。
- `LANGFUSE_ENABLED=false`：先启动 Langfuse 后，在 UI 生成项目 key，再填写 `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` 并改为 true。
- 模型价格与可用性会变化；上线前请核对供应商最新价格和上下文/维度限制。
