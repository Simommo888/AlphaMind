# AlphaMind Phase3 Precision Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** 在一天冲刺内交付可运行、可测试、可复现的 AlphaMind 终极高精版核心能力，并用真实金融年报样本与严格门禁验证。

**Architecture:** Phase3 以现有 WeKnora/Qdrant/Neo4j/MinIO Phase2 为底座，新增预处理层（复杂 PDF → 带页码/表格/脚注 lineage 的 Markdown）、检索增强层（金融词典扩展 + Dense/Sparse/Graph RRF 融合）、图谱多跳查询层（受控 intent → 参数化 Cypher）和独立质量/压测门禁。新增能力优先采用旁路脚本与配置，避免侵入式修改上游 WeKnora 核心；最终 Markdown 仍通过现有批量入库脚本上传。

**Tech Stack:** Python 3、PyMuPDF、WeKnora API、Qdrant、Neo4j、MinIO、unittest、Docker Compose。

---

## 今日硬验收标准

1. 复杂 PDF：真实年报可生成 Markdown + JSON manifest；表格保持完整边界，跨页同表可拼接，重复表头去重；脚注绑定页码；所有块带 `source_doc_id/page/table_id`；数值保留率可计算并纳入门禁。
2. 金融图谱：Phase3 Schema 覆盖公司、机构、高管、客户、供应商、竞争对手、财务指标与证据；至少提供供应链交叉、机构-高管、指标跨期三类参数化多跳查询计划；禁止拼接用户输入到 Cypher。
3. 混合检索：金融词典支持别名/英文/指标规范化；查询扩展有上限；Dense/Sparse/Graph 结果用可配置加权 RRF 融合并去重；Rerank 配置与 no-evidence 引用策略可验证。
4. 验收压测：一键 gate 校验配置、运行 Phase3 单测、解析真实样本并检查质量阈值；live 模式另行检查全栈、在线检索和并发延迟，外部服务不可用时必须 fail-closed，不伪造 PASS。
5. 质量：新增代码严格测试先行；全量 AlphaMind 测试通过；独立审查无 P0/P1 后提交。

## Task 1: 复杂金融 PDF 结构化管线

**Files:**
- Create: `tests/test_alphamind_phase3_pdf.py`
- Create: `scripts/alphamind_phase3_pdf.py`

**TDD:** 先覆盖表格跨页拼接、重复表头去除、脚注提取、Markdown lineage、数值保留率，再实现 CLI 并对真实年报执行。

## Task 2: 金融词典与混合融合

**Files:**
- Create: `tests/test_alphamind_phase3_retrieval.py`
- Create: `scripts/alphamind_phase3_retrieval.py`
- Create: `dataset/alphamind_financial_lexicon_phase3.json`
- Create: `dataset/alphamind_retrieval_config_phase3_precision.json`

**TDD:** 覆盖最长词优先扩展、去重/上限、ticker/指标别名、加权 RRF 排序和文档去重。

## Task 3: 金融专属 Schema 与多跳查询

**Files:**
- Create: `tests/test_alphamind_phase3_graph.py`
- Create: `scripts/alphamind_phase3_graph.py`
- Create: `dataset/alphamind_process_config_phase3_precision.json`
- Create: `docs/ALPHAMIND_KNOWLEDGE_GRAPH_SCHEMA_PHASE3.md`

**TDD:** 覆盖受控 intent、参数化 Cypher、双公司必填、无用户值插入查询文本、证据字段要求。

## Task 4: 一键验收与压测

**Files:**
- Create: `tests/test_alphamind_phase3_quality_gate.py`
- Create: `scripts/alphamind_phase3_quality_gate.py`
- Create: `scripts/alphamind_phase3_load_test.py`
- Create: `dataset/phase3_eval_queries.json`
- Create: `docs/ALPHAMIND_PHASE3_PRECISION_RUNBOOK.md`
- Create: `.env.phase3-precision.example`
- Create: `docker-compose.phase3-precision.yml`

**TDD:** gate 计划、fail-closed、PDF 阈值、离线/在线边界和压测统计均有单测；执行真实样本 gate 和 live gate。

## Task 5: 集成审查与提交

1. 运行 `python -m unittest discover -s tests -p 'test_alphamind*.py' -v`。
2. 运行真实年报 PDF 解析与 Phase3 gate。
3. 容器健康后运行 live health/retrieval/load checks。
4. 独立审查按 P0/P1/P2 输出；修复全部 P0/P1。
5. 检查 secrets 与 diff，提交 Phase3；保留并单独说明进入任务前已有的 Phase2 未提交改动。
