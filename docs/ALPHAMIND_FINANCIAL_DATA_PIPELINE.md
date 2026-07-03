# AlphaMind 财务结构化数据接入方案

AlphaMind 后续会接入年报、季报、财务报表、盈利预测表等结构化数据。它们不能只作为普通 PDF/Excel 文本 embedding；正确路线是“结构化表 + 图谱三元组 + 原文 chunk 引用”三线并行。

## 1. 现状

- 当前发现 `数据/数据库/financial_data.db` 存在但大小为 0，尚无可用表结构。
- 现有 RAG 文档主要是 `数据/**/Documents` 下的 PDF 研报；已有 `alphamind_bulk_ingest.py` 可生成研报 metadata 并上传 WeKnora。
- 知识图谱 MVP 已定义在 `docs/ALPHAMIND_KNOWLEDGE_GRAPH_SCHEMA.md`，其中 `FinancialMetric` 是财务数据进入 GraphRAG 的核心节点。

## 2. 推荐数据分层

```text
数据/数据库/
  financial_data.db              # 本地 SQLite MVP，可迁移到 Postgres/DuckDB
  raw_financial_tables/           # 原始 Excel/CSV/Parquet
  normalized_financial_tables/    # 规范化 CSV/Parquet
  manifests/                      # 导入批次、校验结果、异常行
```

生产阶段建议把结构化财务数据迁到 Postgres/DuckDB/ClickHouse 之一；SQLite 只适合本地 PoC。

## 3. Canonical schema

### 3.1 companies

```sql
CREATE TABLE IF NOT EXISTS companies (
  company_id TEXT PRIMARY KEY,          -- ticker 优先，如 600519.SH；无 ticker 用 normalized_name
  name TEXT NOT NULL,
  full_name TEXT,
  ticker TEXT,
  exchange TEXT,
  aliases_json TEXT DEFAULT '[]',
  source TEXT,
  updated_at TEXT
);
```

### 3.2 reports

```sql
CREATE TABLE IF NOT EXISTS reports (
  doc_id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  source_type TEXT,                     -- broker_report / financial_report / announcement / table_data
  broker TEXT,
  publish_date TEXT,
  report_type TEXT,
  file_path TEXT,
  pages INTEGER,
  language TEXT DEFAULT 'zh-CN',
  created_at TEXT
);
```

### 3.3 financial_metrics

```sql
CREATE TABLE IF NOT EXISTS financial_metrics (
  metric_id TEXT PRIMARY KEY,           -- company_id + metric_name + period + source_doc_id
  company_id TEXT NOT NULL,
  metric_name TEXT NOT NULL,            -- EPS / revenue / net_profit_attributable / gross_margin / ROE / PE / PB / target_price
  period TEXT NOT NULL,                 -- 2024A / 2025E / 2026Q1 / 2026H1
  value REAL,
  unit TEXT,
  currency TEXT,
  source_doc_id TEXT,
  source_chunk_id TEXT,
  page INTEGER,
  broker TEXT,
  analyst TEXT,
  confidence REAL,
  created_at TEXT,
  FOREIGN KEY(company_id) REFERENCES companies(company_id),
  FOREIGN KEY(source_doc_id) REFERENCES reports(doc_id)
);

CREATE INDEX IF NOT EXISTS idx_financial_metrics_company_period
ON financial_metrics(company_id, period);

CREATE INDEX IF NOT EXISTS idx_financial_metrics_name_period
ON financial_metrics(metric_name, period);
```

### 3.4 metric_lineage

```sql
CREATE TABLE IF NOT EXISTS metric_lineage (
  lineage_id TEXT PRIMARY KEY,
  metric_id TEXT NOT NULL,
  raw_file_path TEXT,
  raw_sheet TEXT,
  raw_cell TEXT,
  raw_text TEXT,
  parser TEXT,
  imported_at TEXT,
  FOREIGN KEY(metric_id) REFERENCES financial_metrics(metric_id)
);
```

## 4. 指标规范化

| 原文 | 规范名 |
|---|---|
| EPS / 每股收益 | `EPS` |
| 营业收入 / 收入 / revenue | `revenue` |
| 归母净利润 / 净利润 | `net_profit_attributable` |
| 毛利率 | `gross_margin` |
| ROE / 净资产收益率 | `ROE` |
| 目标价 | `target_price` |
| PE / P/E | `PE` |
| PB / P/B | `PB` |

期间规范：

- 历史年度：`2024A`
- 预测年度：`2025E`
- 季度：`2026Q1`
- 半年：`2026H1`

## 5. 入库流程

```text
Excel/CSV/PDF 表格
  ↓
解析：保留 sheet/cell/page/raw_text
  ↓
字段映射：company/ticker/metric/period/value/unit/source_doc_id
  ↓
质量校验：单位、期间、数值范围、重复键
  ↓
写入 financial_metrics
  ↓
生成 FinancialMetric 图谱节点与 ABOUT_COMPANY / HAS_METRIC 关系
  ↓
RAG 回答时回填 source_doc_id/source_chunk_id 引用原文
```

## 6. 质量校验规则

1. 同一 `company_id + metric_name + period + source_doc_id` 不应重复；重复时保留 lineage 并标记冲突。
2. `EPS/PE/PB/ROE/gross_margin` 必须有单位或可推断单位。
3. `target_price` 必须有 currency；A 股默认 CNY，港股默认 HKD，但需要在 lineage 中记录默认推断。
4. 预测值必须带 `E`，历史值必须带 `A`；无法判断时进入人工校验队列。
5. 所有指标必须能追溯到 `source_doc_id`，最好到 `source_chunk_id/page/raw_cell`。

## 7. GraphRAG 回填方式

结构化数据不直接替代 RAG。推荐查询链路：

```text
用户问：德龙激光 2026E EPS 预测是多少？
  ↓
结构化表按 company_id + EPS + 2026E 找候选值
  ↓
Neo4j 查 FinancialMetric -[:ABOUT_COMPANY]-> Company 与 Report
  ↓
用 source_doc_id/source_chunk_id 对 WeKnora chunk 做 filter/boost
  ↓
rerrank 后生成带引用答案
```

## 8. 下一步落地

1. 往 `financial_data.db` 写入上面的 schema 初始化脚本。
2. 新增 `scripts/alphamind_financial_import.py`：读取 Excel/CSV，输出 dry-run manifest，再写入 SQLite。
3. 对 3-5 份盈利预测表做人工映射，形成字段映射模板。
4. 将导入结果转成 Neo4j Cypher/WeKnora graph extraction 可消费的 `FinancialMetric` 节点。
5. 把结构化 metric lookup 加入检索前的 query understanding/metadata filter 阶段。
