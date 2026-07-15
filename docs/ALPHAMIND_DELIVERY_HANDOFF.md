# AlphaMind Phase3 客户交付与运维移交

## 1. 交付范围

本交付对应《金融系统报价单(终版)》方案三，并包含：

- WeKnora 私有化部署；
- Qdrant + Neo4j + MinIO + PostgreSQL；
- 复杂金融 PDF 结构化预处理；
- Dense/Sparse/Graph 混合检索与金融词典；
- 金融证据图谱和受控多跳查询；
- 板块—行业—产业链—企业 Neo4j 关系图；
- 内容硬断言、引用校验、质量门禁和负载探针；
- 启停、状态、日志、备份、恢复和验收操作。

条件项见 `ALPHAMIND_DELIVERY_ACCEPTANCE_MATRIX.md`。聊天模型已按客户说明改为 vLLM `qwen3-14b`；旧 8GB/Ollama 基线不等于客户目标机终验。

## 2. 目录说明

| 路径 | 用途 |
|---|---|
| `.env.phase3-precision` | 私有运行配置，不可提交或外发 |
| `.env.phase3-precision.example` | 客户配置模板 |
| `docker-compose*.yml` | WeKnora 基础与 Phase2/Phase3 覆盖层 |
| `scripts/alphamind_delivery.py` | 统一交付控制器 |
| `scripts/alphamind_phase3_*.py` | PDF、检索、图谱、验收、压测 |
| `scripts/alphamind_relation_graph.py` | 板块产业链企业关系图构建/导入 |
| `dataset/` | 配置、词典、图谱与评测集 |
| `dataset/phase3_runs/` | 本机验收产物，默认不纳入 Git |
| `delivery/` | 交付清单与入口文档 |

## 3. 首次交付

### 3.1 前置条件

- Docker Engine / Docker Desktop 可用；
- Docker Compose v2；
- Python 3.11+；
- 客户目标机需要 vLLM `qwen3-14b` OpenAI-compatible API；Ubuntu 目标机需按客户 GPU 安装匹配 CUDA；
- `.env.phase3-precision` 已从 example 创建并替换所有 `change_me`；
- 本地 embedding/reranker 服务可访问。
- `curl http://127.0.0.1:8000/v1/models` 能看到 `qwen3-14b`（或已同步配置的 served model name）。

当前共享模型服务：

```bash
cd D:/AI-Models/qwen3-service
bash start_qwen3_service.sh
curl http://127.0.0.1:8010/health
```

### 3.2 预检和启动

```bash
cd D:/AlphaMind
python scripts/alphamind_delivery.py preflight
python scripts/alphamind_delivery.py start
python scripts/alphamind_delivery.py status
```

健康检查：

```bash
python scripts/alphamind_healthcheck.py \
  --phase phase3-precision \
  --env-file .env.phase3-precision \
  --require-qdrant --require-neo4j --require-minio
```

## 4. 客户日常操作

### 启动

```bash
python scripts/alphamind_delivery.py start
```

### 状态和日志

```bash
python scripts/alphamind_delivery.py status
python scripts/alphamind_delivery.py logs --tail 200
```

### 停止

```bash
python scripts/alphamind_delivery.py stop
```

停止不删除数据卷。任何时候都不要执行：

```text
docker compose down -v
```

## 5. 文档上传和查询

1. 打开 `http://localhost:18088`。
2. 进入 Phase3 Precision 知识库。
3. Markdown/MHTML 可直接上传。
4. 年报、招股书等复杂 PDF 建议先运行：

```bash
python scripts/alphamind_phase3_pdf.py <input.pdf> \
  --out-dir dataset/phase3_runs/pdf \
  --strict --require-table
```

5. 上传生成的 `.phase3.md`。
6. 等待状态为 `completed` 后再查询。
7. 金融数值回答必须检查来源引用，不接受无来源结论。

## 6. Neo4j 关系图

- Browser：`http://localhost:17474`
- Bolt：`neo4j://localhost:17687`
- 查询手册：`ALPHAMIND_RELATION_GRAPH_NEO4J.md`

关系图只替换带 `AlphaMindEntity` 标签的确定性图层，不删除 WeKnora 原生图谱。

## 7. 当前真实验收

2026-07-12 最新在线门禁：

- config validation：PASS；
- Phase3 单测：PASS；
- 30 页真实年报解析：PASS；
- WeKnora/Qdrant/Neo4j/MinIO 健康：PASS；
- 真实 hybrid search：PASS；
- 引用答案：PASS；
- 健康端点 40 请求、并发 8：PASS；
- 真实 hybrid-search POST 40 请求、并发 4：PASS，错误率 0%，P95 2388.579 ms；
- 总质量门禁：PASS。

报告：

- `dataset/phase3_runs/phase3_quality_gate_latest.json`
- `dataset/phase3_runs/phase3_acceptance_latest.json`
- `dataset/alphamind_relation_graph_report.json`

## 8. 备份恢复

详见 `ALPHAMIND_BACKUP_RESTORE.md`。恢复必须先通过备份哈希校验和 dry-run，并在隔离环境演练。

## 9. 常见问题

### hybrid search 返回 embedding HTTP 500

先检查共享模型服务：

```bash
curl http://127.0.0.1:8010/health
cd D:/AI-Models/qwen3-service
bash start_qwen3_service.sh
```

模型服务停机时，WeKnora 健康端点仍可能正常，但在线检索门禁必须失败，不能把系统标为可交付。

### 8GB GPU 显存不足

当前 embedding/reranker 使用互斥懒加载。聊天模型按客户说明由 vLLM `qwen3-14b` 长驻提供；若恢复报价单原 35B 或调整模型规格，需要更高显存硬件并重新跑完整验收。

### 扫描 PDF 没有文本

未配置 `DOCREADER_ODL_HYBRID_URL` 时，不承诺 OCR。接入 MinerU/OpenDataLab 兼容端点后再复验。

## 10. 一个月咨询服务移交项

商务交付单需另行记录：

- 服务开始/结束日期；
- 问题提交渠道；
- 工作时间和响应边界；
- 包含的配置/故障咨询范围；
- 不包含的新增功能和硬件采购范围。
