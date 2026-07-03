# AlphaMind 股票投研 RAG 与知识图谱架构

## 目标

基于 WeKnora 构建面向股票投研的 RAG 系统。当前数据规模约 20GB，主要为研究报告；后续会接入大量券商研报、上市公司公告、年报/季报和财务报表等结构化数据。

## 核心原则

1. **metadata-first**：股票投研查询天然带公司、行业、券商、日期、指标等过滤条件，不能只依赖向量相似度。
2. **混合召回**：Dense vector + BM25/keyword + metadata filter + graph expansion。
3. **默认 rerank**：研报问答必须重排，尤其是目标价、EPS、营收预测、风险因素等精确证据问题。
4. **结构化数据单独入库**：财务报表不要只 embedding，应同时进入结构化表和知识图谱。
5. **评测先行**：每次 chunking、embedding、rerank、GraphRAG 调整都要跑固定评测集。

## 推荐数据分层

```text
data/
  raw/
    broker_reports/          # 券商研报 PDF/DOCX/PPT/HTML
    annual_reports/          # 年报、半年报、季报
    announcements/           # 公告
    financial_tables/        # 财报 CSV/Excel/Parquet
    market_data/             # 行情、估值、行业分类
  processed/
    documents/               # 解析后的正文
    chunks/                  # 切片
    tables/                  # 表格抽取结果
    entities/                # 实体抽取结果
    relations/               # 关系抽取结果
  eval/
    queries.json
    ground_truth.json
```

## 文档 metadata schema

每篇文档建议至少维护以下字段：

```json
{
  "doc_id": "broker_report_2026_xxx",
  "source_type": "broker_report",
  "title": "公司深度报告",
  "company": "贵州茅台",
  "ticker": "600519.SH",
  "industry": "食品饮料",
  "broker": "中信证券",
  "analyst": ["张三"],
  "publish_date": "2026-06-30",
  "report_type": "深度报告",
  "rating": "买入",
  "target_price": 2000,
  "pages": 42,
  "language": "zh-CN",
  "file_path": "...",
  "ingested_at": "..."
}
```

## 检索链路

```text
User Query
  ↓
Query Understanding
  - 公司/股票代码识别
  - 行业识别
  - 时间范围识别
  - 指标识别：营收、毛利率、ROE、EPS、目标价
  - 报告类型识别
  ↓
Retrieval Fan-out
  1. Dense vector search
  2. BM25 / keyword search
  3. Metadata filtered retrieval
  4. Graph expansion retrieval
  ↓
Merge + Dedup
  ↓
Rerank top 50-100 → top 8-15
  ↓
Evidence pack with citations
  ↓
LLM answer
```

## Chunking 建议

WeKnora 自带 adaptive 3-tier chunking。投研研报第一版建议：

| 内容类型 | 策略 |
|---|---|
| PDF 研报正文 | `auto`，通常会走 heuristic；chunk_size 800-1200，overlap 100-150 |
| Markdown/HTML | `auto`，优先 heading |
| 摘要/投资要点 | 单独高权重 chunk |
| 盈利预测表 | 表格转 Markdown + JSON 双形态 |
| 财务报表 | 结构化入库，不只 embedding |
| 图表说明 | OCR/标题/周边文字绑定 |

当前建议配置见 `docs/ALPHAMIND_KB_CONFIG.json`。

## Embedding / Rerank 选择

不要把模型价格写死为当前事实。上线前用供应商最新价格页核对。

| 层级 | 适用场景 |
|---|---|
| Fast/self-hosted | 成本敏感、小规模实时查询、本地部署 |
| Balanced open | 质量与私有化平衡，适合先做 PoC |
| Quality API | 准确率优先，适合核心投研问答 |

建议第一阶段：选择一个中文/多语质量较强的 embedding，配合 rerank；保留固定 eval set 比较召回效果。

## 知识图谱 MVP

### 节点

```text
Company(name, ticker, exchange, aliases)
Industry(name, level, taxonomy_source)
Report(doc_id, title, broker, publish_date, report_type)
Broker(name)
Analyst(name, broker)
FinancialMetric(name, period, value, unit, source_doc_id)
Event(type, event_date, description)
```

### 关系

```text
(:Report)-[:COVERS]->(:Company)
(:Report)-[:PUBLISHED_BY]->(:Broker)
(:Report)-[:WRITTEN_BY]->(:Analyst)
(:Company)-[:BELONGS_TO]->(:Industry)
(:Report)-[:HAS_METRIC]->(:FinancialMetric)
(:FinancialMetric)-[:ABOUT_COMPANY]->(:Company)
(:Report)-[:MENTIONS]->(:Event)
(:Event)-[:AFFECTS]->(:Company)
(:Company)-[:COMPETES_WITH]->(:Company)
(:Company)-[:SUPPLIES_TO]->(:Company)
```

## GraphRAG 用法示例

问题：最近券商为什么上调宁德时代目标价？

```text
识别 Company=宁德时代
  ↓
图谱找最近 90 天 Report -[:COVERS]-> Company
  ↓
找 HAS_METRIC target_price 变化
  ↓
找 MENTIONS Event：销量、毛利率、海外储能、政策等
  ↓
用 doc_id/chunk_id 作为检索过滤/boost
  ↓
向量 + BM25 召回原文证据
  ↓
rerank
  ↓
带引用回答
```

## 评测目标

第一阶段建议：

```text
precision@5 >= 0.75
recall@10 >= 0.85
citation accuracy >= 0.90
hallucination rate <= 0.05
```

评测模板见：

- `dataset/alphamind_eval_queries.json`
- `dataset/alphamind_ground_truth_template.json`
- `dataset/alphamind_eval_plan.md`

当前基线见 `dataset/alphamind_retrieval_eval_baseline.json`：4 篇文本样本上 `Precision@5=0.611`、`Recall@10=1.000`、`MRR=0.667`，说明能召回但首位排序不足；后续重点验证 metadata filter、hybrid、rerank 和 GraphRAG boost。

## 结构化财务数据

财务报表、盈利预测表和估值表应按 `docs/ALPHAMIND_FINANCIAL_DATA_PIPELINE.md` 单独入结构化表，再同步为 `FinancialMetric` 图谱节点。RAG 回答时必须回填 `source_doc_id/source_chunk_id/page/raw_cell`，不要只根据表值或图谱三元组生成。

## 迭代变量

每轮只改一个变量：

1. chunk_size / overlap
2. parent-child 开关
3. embedding 模型层级
4. dense only vs hybrid
5. rerank top_k / threshold
6. metadata filter 是否强制
7. GraphRAG expansion 是否开启
8. docreader OCR/解析参数
