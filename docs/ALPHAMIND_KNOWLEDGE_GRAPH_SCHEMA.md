# AlphaMind 股票投研知识图谱 Schema

本文定义 AlphaMind 在 WeKnora + Neo4j 上的股票投研知识图谱 MVP。目标不是一次性建成金融全域知识图谱，而是服务 RAG：用图谱识别公司、研报、指标、事件和观点，再回填到原文 chunk 做引用回答。

## 1. 设计原则

1. **证据优先**：所有节点、关系必须能追溯到 `source_doc_id` / `chunk_id` / 页码或原文片段。
2. **ticker 优先**：上市公司实体优先用股票代码归一；没有代码时用公司全称。
3. **Report 是证据中心**：研报覆盖了券商、分析师、日期、评级、目标价、盈利预测和风险因素，必须作为一级节点。
4. **财务指标结构化**：EPS、营收、净利润、毛利率、ROE、PE、PB、目标价等进入 `FinancialMetric`，不要只存自然语言。
5. **图谱只做召回增强**：最终答案仍必须回到原文 chunk，不能只根据图谱三元组生成。

## 2. 节点类型

### 2.1 Company

上市公司、港股公司、产业链相关非上市主体。

```text
Company(
  id,              # ticker 优先，如 600519.SH；无 ticker 时用 normalized_name
  name,            # 显示名/简称
  full_name,
  ticker,
  exchange,
  aliases[],
  source_doc_ids[]
)
```

### 2.2 Industry

行业、子行业、主题赛道。

```text
Industry(
  id,
  name,
  level,
  taxonomy_source, # 申万/中信/自定义
  aliases[]
)
```

### 2.3 Report

券商研报、行业报告、公告、年报等文档级证据。

```text
Report(
  doc_id,
  title,
  source_type,     # broker_report / financial_report / announcement / table_data
  broker,
  publish_date,
  report_type,
  file_path,
  pages,
  language
)
```

### 2.4 Broker / Analyst

```text
Broker(name, aliases[])
Analyst(name, broker, license_no, email)
```

### 2.5 FinancialMetric

财务指标、估值指标、预测值和目标价。

```text
FinancialMetric(
  id,              # company_id + metric + period + source_doc_id
  name,            # EPS / revenue / net_profit / gross_margin / ROE / PE / target_price
  period,          # 2024A / 2025E / 2026E / 2026Q1
  value,
  unit,
  currency,
  source_doc_id,
  source_chunk_id,
  page
)
```

### 2.6 InvestmentView

投资逻辑、评级、目标价方向、核心分歧。

```text
InvestmentView(
  id,
  thesis,
  rating,
  target_price,
  direction,       # initiate / maintain / upgrade / downgrade
  confidence,
  source_doc_id,
  source_chunk_id
)
```

### 2.7 Event

业务、政策、订单、产能、价格、行业催化、风险事件。

```text
Event(
  id,
  type,
  event_date,
  description,
  source_doc_id,
  source_chunk_id
)
```

### 2.8 RiskFactor

```text
RiskFactor(
  id,
  name,
  description,
  severity,
  source_doc_id,
  source_chunk_id
)
```

### 2.9 Product

```text
Product(
  id,
  name,
  category,
  aliases[]
)
```

## 3. 关系类型

```text
(:Report)-[:COVERS]->(:Company)
(:Report)-[:PUBLISHED_BY]->(:Broker)
(:Report)-[:WRITTEN_BY]->(:Analyst)
(:Analyst)-[:WORKS_FOR]->(:Broker)

(:Company)-[:BELONGS_TO]->(:Industry)
(:Company)-[:HAS_PRODUCT]->(:Product)
(:Company)-[:COMPETES_WITH]->(:Company)
(:Company)-[:SUPPLIES_TO]->(:Company)
(:Product)-[:BELONGS_TO]->(:Industry)

(:Report)-[:HAS_METRIC]->(:FinancialMetric)
(:FinancialMetric)-[:ABOUT_COMPANY]->(:Company)
(:FinancialMetric)-[:ABOUT_PRODUCT]->(:Product)

(:Report)-[:HAS_VIEW]->(:InvestmentView)
(:InvestmentView)-[:ABOUT_COMPANY]->(:Company)
(:InvestmentView)-[:SUPPORTED_BY]->(:FinancialMetric)
(:InvestmentView)-[:SUPPORTED_BY]->(:Event)

(:Report)-[:MENTIONS]->(:Event)
(:Event)-[:AFFECTS]->(:Company)
(:Event)-[:AFFECTS]->(:Industry)

(:Report)-[:HAS_RISK]->(:RiskFactor)
(:RiskFactor)-[:AFFECTS]->(:Company)
(:RiskFactor)-[:AFFECTS]->(:Industry)
```

关系属性建议：

```text
source_doc_id
source_chunk_id
page
confidence
extracted_at
```

## 4. 规范化规则

### 4.1 公司

| 输入 | 规范化 |
|---|---|
| 宁德时代 / CATL / 300750.SZ | `Company.id=300750.SZ` |
| 东岳集团 / 0189.HK / 00189.HK | `Company.id=00189.HK` |
| 人福医药(600079) | `Company.id=600079.SH` 或先保留 `600079`，后续补交易所 |

规则：

1. 能从文件名或正文提取 ticker 时，ticker 是主键。
2. A 股 6 位代码后续补 `.SH/.SZ/.BJ`，如果无法判断，先保留原始代码并记录 `exchange_unknown=true`。
3. 港股统一 5 位 `.HK`，如 `0189.HK` 规范到 `00189.HK`。
4. `aliases` 保留简称、全称、英文名、股票代码。

### 4.2 研报

`doc_id` 应由批量入库脚本生成并写入 metadata。建议保持稳定，不随文件移动变化。

```text
alphamind_<sanitized_filename>
```

### 4.3 指标

指标名统一映射：

| 原文 | 规范名 |
|---|---|
| EPS / 每股收益 | EPS |
| 营业收入 / 收入 / revenue | revenue |
| 归母净利润 / 净利润 | net_profit_attributable |
| 毛利率 | gross_margin |
| ROE / 净资产收益率 | ROE |
| 目标价 | target_price |
| PE / P/E | PE |
| PB / P/B | PB |

期间规则：

- 历史值：`2024A`
- 预测值：`2025E`
- 季度：`2026Q1`
- 半年：`2026H1`

## 5. WeKnora `extract_config` 模板

完整可用模板见：`dataset/alphamind_process_config_research_report.json`。

核心 schema：

```json
{
  "enabled": true,
  "text": "抽取股票投研领域实体与关系。重点识别公司、股票代码、行业、券商、分析师、报告、财务指标、预测期、目标价、评级、投资逻辑、风险因素、产品、产业链上下游和关键事件。实体名称必须规范化：公司优先保留股票代码和简称，报告保留 doc_id，财务指标保留 period/value/unit/source_doc_id。不要抽取没有原文证据的关系。",
  "tags": ["股票投研", "券商研报", "财务指标", "行业研究", "GraphRAG"],
  "nodes": [
    {"name": "Company", "attributes": ["name", "ticker", "exchange", "aliases"]},
    {"name": "Industry", "attributes": ["name", "level", "taxonomy_source"]},
    {"name": "Report", "attributes": ["doc_id", "title", "broker", "publish_date", "report_type"]},
    {"name": "Broker", "attributes": ["name"]},
    {"name": "Analyst", "attributes": ["name", "broker"]},
    {"name": "FinancialMetric", "attributes": ["name", "period", "value", "unit", "source_doc_id"]},
    {"name": "InvestmentView", "attributes": ["thesis", "rating", "target_price", "direction"]},
    {"name": "RiskFactor", "attributes": ["name", "description"]},
    {"name": "Event", "attributes": ["type", "event_date", "description"]},
    {"name": "Product", "attributes": ["name", "category"]}
  ],
  "relations": [
    "Report-COVERS-Company",
    "Report-PUBLISHED_BY-Broker",
    "Report-WRITTEN_BY-Analyst",
    "Company-BELONGS_TO-Industry",
    "Report-HAS_METRIC-FinancialMetric",
    "FinancialMetric-ABOUT_COMPANY-Company",
    "Report-HAS_VIEW-InvestmentView",
    "InvestmentView-ABOUT_COMPANY-Company",
    "Report-HAS_RISK-RiskFactor",
    "Report-MENTIONS-Event",
    "Event-AFFECTS-Company",
    "Company-HAS_PRODUCT-Product",
    "Company-COMPETES_WITH-Company",
    "Company-SUPPLIES_TO-Company"
  ]
}
```

## 6. 抽取示例

### 6.1 输入片段

```text
国盛证券在《德龙激光(688170.SH)超快激光平台型小巨人》中预测，
公司 2026/2027/2028 年 EPS 为 0.86/1.56/2.46 元，
对应 PE 为 91.9/50.3/31.9x。报告认为 AI PCB 与 M9 封装材料需求
将带动超快激光设备增长，给予“买入”评级。风险包括下游扩产不及预期。
```

### 6.2 期望节点

```json
[
  {"type": "Broker", "name": "国盛证券"},
  {"type": "Company", "name": "德龙激光", "ticker": "688170.SH"},
  {"type": "Report", "title": "德龙激光(688170.SH)超快激光平台型小巨人"},
  {"type": "FinancialMetric", "name": "EPS", "period": "2026E", "value": 0.86, "unit": "元"},
  {"type": "FinancialMetric", "name": "PE", "period": "2026E", "value": 91.9, "unit": "x"},
  {"type": "Product", "name": "AI PCB"},
  {"type": "Product", "name": "M9 封装材料"},
  {"type": "RiskFactor", "name": "下游扩产不及预期"}
]
```

### 6.3 期望关系

```json
[
  {"source": "Report", "relation": "PUBLISHED_BY", "target": "Broker"},
  {"source": "Report", "relation": "COVERS", "target": "Company"},
  {"source": "Report", "relation": "HAS_METRIC", "target": "FinancialMetric"},
  {"source": "FinancialMetric", "relation": "ABOUT_COMPANY", "target": "Company"},
  {"source": "Company", "relation": "HAS_PRODUCT", "target": "Product"},
  {"source": "Report", "relation": "HAS_RISK", "target": "RiskFactor"}
]
```

## 7. Neo4j 约束与索引建议

WeKnora 当前写图使用 APOC merge。生产环境确认 APOC 可用后，建议在 Neo4j Browser 执行：

```cypher
CREATE CONSTRAINT company_id IF NOT EXISTS FOR (n:Company) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT report_doc_id IF NOT EXISTS FOR (n:Report) REQUIRE n.doc_id IS UNIQUE;
CREATE CONSTRAINT broker_name IF NOT EXISTS FOR (n:Broker) REQUIRE n.name IS UNIQUE;
CREATE INDEX company_name IF NOT EXISTS FOR (n:Company) ON (n.name);
CREATE INDEX company_ticker IF NOT EXISTS FOR (n:Company) ON (n.ticker);
CREATE INDEX report_publish_date IF NOT EXISTS FOR (n:Report) ON (n.publish_date);
CREATE INDEX metric_name_period IF NOT EXISTS FOR (n:FinancialMetric) ON (n.name, n.period);
CREATE INDEX event_type_date IF NOT EXISTS FOR (n:Event) ON (n.type, n.event_date);
```

如果当前 WeKnora 写入的节点没有 `id`，先不要创建唯一约束；先运行验收查询观察属性结构，再决定迁移。

## 8. 验收 Cypher

### 8.1 图谱规模

```cypher
MATCH (n) RETURN labels(n) AS labels, count(*) AS count ORDER BY count DESC;
MATCH ()-[r]->() RETURN type(r) AS relation, count(*) AS count ORDER BY count DESC;
```

### 8.2 公司覆盖

```cypher
MATCH (r:Report)-[:COVERS]->(c:Company)
RETURN c.name, c.ticker, count(r) AS report_count
ORDER BY report_count DESC
LIMIT 20;
```

### 8.3 指标抽取

```cypher
MATCH (r:Report)-[:HAS_METRIC]->(m:FinancialMetric)-[:ABOUT_COMPANY]->(c:Company)
RETURN c.name, m.name, m.period, m.value, m.unit, r.title
ORDER BY c.name, m.period
LIMIT 50;
```

### 8.4 目标价/评级观点

```cypher
MATCH (r:Report)-[:HAS_VIEW]->(v:InvestmentView)-[:ABOUT_COMPANY]->(c:Company)
RETURN c.name, r.broker, r.publish_date, v.rating, v.target_price, v.thesis
ORDER BY r.publish_date DESC
LIMIT 50;
```

### 8.5 事件驱动

```cypher
MATCH (e:Event)-[:AFFECTS]->(c:Company)
RETURN c.name, e.type, e.event_date, e.description
ORDER BY e.event_date DESC
LIMIT 50;
```

## 9. GraphRAG 检索策略

### 9.1 查询理解

从用户问题抽取：

```text
Company / ticker
Industry
Metric
Period
Broker
Date range
Report type
Intent: metric_lookup / broker_compare / risk_summary / target_price_change / industry_summary
```

### 9.2 图谱扩展

常见模式：

```cypher
// 公司最近研报
MATCH (r:Report)-[:COVERS]->(c:Company)
WHERE c.name CONTAINS $company OR c.ticker = $ticker
RETURN r.doc_id, r.title, r.broker, r.publish_date
ORDER BY r.publish_date DESC
LIMIT 20;

// 公司指标
MATCH (r:Report)-[:HAS_METRIC]->(m:FinancialMetric)-[:ABOUT_COMPANY]->(c:Company)
WHERE (c.name CONTAINS $company OR c.ticker = $ticker)
  AND m.name = $metric
RETURN r.doc_id, m.name, m.period, m.value, m.unit
ORDER BY m.period DESC
LIMIT 50;

// 风险因素
MATCH (r:Report)-[:HAS_RISK]->(risk:RiskFactor)-[:AFFECTS]->(c:Company)
WHERE c.name CONTAINS $company OR c.ticker = $ticker
RETURN r.doc_id, risk.name, risk.description
LIMIT 30;
```

### 9.3 回填 RAG

图谱返回的 `doc_id` / `ticker` / `publish_date` 用于：

1. metadata filter：缩小向量和关键词搜索范围。
2. boost：提升相关报告 chunk 排名。
3. rerank context：把图谱命中的指标/事件作为 rerank 特征。
4. citation check：最终回答必须引用被命中的 chunk。

## 10. 失败模式与修复

| 失败模式 | 表现 | 修复 |
|---|---|---|
| 公司拆成多个节点 | 宁德时代、CATL、300750.SZ 分散 | alias/ticker 规范化，补 merge job |
| 指标没有 period | EPS 值无法比较 | prompt 强制 period/value/unit/source_doc_id |
| 图谱命中但答案没引用 | 回答只有三元组 | GraphRAG 只做 filter/boost，必须回原文检索 |
| 关系过泛 | 大量 MENTIONS | 收紧 relations，增加 HAS_METRIC/HAS_VIEW/HAS_RISK |
| Neo4j 查询少 | `name CONTAINS` 命不中简称 | aliases 入库，查询阶段加 ticker 和 alias 扩展 |
| 抽取成本高 | 每个 chunk 都调用 LLM | 先只对摘要、投资要点、盈利预测、风险提示段落抽取 |

## 11. 下一步实现建议

1. 在批量入库脚本中生成稳定 `doc_id` 和 metadata。
2. 针对 100 篇 pilot 跑图谱抽取，执行验收 Cypher。
3. 记录 alias 冲突和指标缺 period 的样本，修订 `extract_config.text`。
4. 建立结构化财务表导入通道，把三表和盈利预测表直接转成 `FinancialMetric`。
5. 在检索层补“图谱 doc_id boost/filter”能力，避免图谱和 RAG 两张皮。
