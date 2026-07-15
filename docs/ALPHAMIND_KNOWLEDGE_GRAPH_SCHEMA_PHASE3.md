# AlphaMind Phase3 金融证据图谱 Schema

Phase3 在原 Phase2 `Company/Report/FinancialMetric` 基础上增加供应链、机构和高管网络，并把 `Evidence` 设为所有多跳结论的强制证据锚点。

## 核心不变量

1. 图谱只做候选发现、过滤与排序；最终答案必须回到 WeKnora 原文 chunk。
2. 每个 FinancialMetric、客户/供应商、高管/机构关系都必须可追溯到 `source_doc_id/source_chunk_id/page`；表格事实还应保留 `table_id/footnote_id`。独立 `AlphaMindEntity` 关系图对尚未进入 WeKnora 的本地 Markdown 使用 `local-md:*` / `local-line:*` 命名空间，并同时保留 `source_file/source_line`，不得把本地标识冒充 WeKnora ID。
3. 公司优先以 ticker 归一；匿名“客户一/供应商A”保持匿名，绝不猜测真实主体。
4. 用户输入只能进入 Cypher 参数，不允许拼接查询文本。
5. 多跳查询无证据时返回空结果，由 no-evidence policy 拒答。

## 关键节点

- Sector：AlphaMind 主题板块，保留 `taxonomy_source`，不得冒充申万/中信官方分类。
- Industry：目录行业/子行业，记录所属板块和产业链阶段。
- ChainStage：上游资源材料、中游制造、下游应用服务。
- Company：ticker、简称、全称、aliases。
- Organization：客户/供应商/合作方统一主体；匿名主体带 `anonymous=true`。
- Institution：投资机构、控股股东、券商、基金等。
- Executive：高管及任期。
- FinancialMetric：指标、期间、值、单位、币种。
- Report：年报、招股书、公告、研报。
- Evidence：文档、chunk、页码、表格、脚注、原文引用、置信度。

## 关键关系

- `Company-[:BELONGS_TO]->Industry-[:PART_OF_SECTOR]->Sector`
- `Industry-[:PART_OF_CHAIN]->ChainStage-[:UPSTREAM_OF]->ChainStage`
- `Company-[:SUPPLIES_TO|PARTNERS_WITH|COMPETES_WITH|CONTROLS]->Company`
- `Company-[:HAS_CUSTOMER {rank,amount,ratio,period,...}]->Organization`
- `Company-[:HAS_SUPPLIER {rank,amount,ratio,period,...}]->Organization`
- `Company-[:HAS_EXECUTIVE {title,term_start,term_end,...}]->Executive`
- `Institution-[:AFFILIATED_WITH|APPOINTED|NOMINATED]->Executive`
- `Institution-[:INVESTS_IN {ratio,period,...}]->Company`
- `Report-[:HAS_METRIC]->FinancialMetric-[:ABOUT_COMPANY]->Company`
- `Evidence-[:SUPPORTED_BY]->Organization|Executive|FinancialMetric`

关系属性至少包括：`source_doc_id/source_chunk_id/page/table_id/footnote_id/confidence/extracted_at`。

## 受控多跳查询

`scripts/alphamind_phase3_graph.py` 只允许：

- `supply_chain_overlap`：兼容原生 `A-[:HAS_CUSTOMER]->Organization<-[:HAS_SUPPLIER]-B`，并桥接独立图层同义方向 `A-[:SUPPLIES_TO]->Company-[:SUPPLIES_TO]->B`；两条分支统一返回双方证据与 `semantic_source`。
- `institution_executive_network`：公司 → 高管 ← 机构网络，并返回证据。
- `metric_period_compare`：公司 → 指标 → 报告跨期比较，并返回原文定位。

示例：

```bash
python scripts/alphamind_phase3_graph.py supply_chain_overlap --company-a A公司 --company-b B公司
python scripts/alphamind_phase3_graph.py metric_period_compare --company-a 300782.SZ --metric ROE --period 2023A --period 2024A
```
