# AlphaMind Neo4j 板块—产业链—企业链 Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** 把本地企业资料确定性转换为可追溯关系图，并直接在 Neo4j Browser 中查看板块、产业链和企业关系。

**Architecture:** Python 离线构建器从目录分类和企业 Markdown 生成版本化 JSON 图谱；企业关系只在命中已知企业与显式关系措辞时生成，并保存文件、行号、原句、抽取方法和置信度。导入器通过 Neo4j Transaction HTTP API 参数化写入独立 `AlphaMindEntity` 图层，只替换 AlphaMind 自有节点，不影响 WeKnora 原图谱。

**Tech Stack:** Python 3 标准库、unittest、Neo4j Transaction HTTP API、Cypher。

---

### Task 1: 确定性图谱构建器

- Create: `scripts/alphamind_relation_graph.py`
- Test: `tests/test_alphamind_relation_graph.py`
- RED：目录分类、产业链阶段、客户/供应商方向、控制方向、第三方误归因、证据和去重测试先失败。
- GREEN：实现 Sector/Industry/ChainStage/Company 与证据边。

### Task 2: 幂等 Neo4j 导入

- Test：未知标签/关系拒绝、参数化语句、证据属性、先建约束再原子替换。
- Implement：`build_neo4j_import_statements()`、`import_graph()`。
- Scope：只删除/替换 `AlphaMindEntity`，不触碰其他 Neo4j 节点。

### Task 3: 真实数据构建和导入

- Create: `dataset/alphamind_relation_graph.json`
- Create: `dataset/alphamind_relation_graph_report.json`
- Source: `数据/0710/企业md`
- Verify：无悬空边、企业关系均有证据、Neo4j 节点边计数与产物一致。

### Task 4: Schema 同步和查询手册

- Modify: `dataset/alphamind_process_config_phase3_precision.json`
- Modify: `docs/ALPHAMIND_KNOWLEDGE_GRAPH_SCHEMA_PHASE3.md`
- Create: `docs/ALPHAMIND_RELATION_GRAPH_NEO4J.md`
- Verify：配置包含 Sector/Industry/ChainStage/Company 和 BELONGS_TO、PART_OF_SECTOR、PART_OF_CHAIN、UPSTREAM_OF、SUPPLIES_TO、PARTNERS_WITH、CONTROLS、COMPETES_WITH。

### Task 5: 真实 Cypher 验收和独立复审

- 启动 Docker/Neo4j。
- 验证板块→行业→企业、上中下游、企业关系和证据查询。
- 运行 AlphaMind Python 全套测试与 `git diff --check`。
- 独立审查 P0/P1/P2；无 P0/P1 时明确 PASS。
