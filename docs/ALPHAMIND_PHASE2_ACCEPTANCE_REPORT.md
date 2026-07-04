# AlphaMind Phase2 Advanced Acceptance Report

> 封版日期：2026-07-04
> 分支：`alphamind-phase1-acceptance`
> 基线提交：`8a81a770`
> 知识库：`1d608de5-98d6-4a5d-858b-e1d85730531c`

## 1. 结论

AlphaMind Phase2 Advanced 已完成小批量金融 PDF、CSV 表格、Graph RAG 配置、混合检索、knowledge-chat 引用、防幻觉 no-evidence guardrail 与 strict quality evaluator 验收。

最新一键质量门禁输出：

```text
PHASE2 QUALITY GATE: PASS
```

门禁报告路径：

```text
dataset/phase2_runs/phase2_quality_gate_latest.json
```

## 2. 验收范围

Phase2 验收范围包括：

- WeKnora app 健康检查
- Qdrant 向量库连通性
- Neo4j 图数据库连通性
- MinIO 存储连通性
- Phase2 KB 配置文件与 process/retrieval config
- PDF 研报样本解析、chunk、混合检索
- CSV 表格样本解析与字段/指标检索
- knowledge-chat 引用文档检查
- no-evidence 防幻觉拒答检查
- strict ground truth 质量门禁

## 3. 核心文件

| 文件 | 作用 |
| --- | --- |
| `docs/ALPHAMIND_KB_CONFIG.phase2-advanced.json` | Phase2 KB 配置 |
| `dataset/alphamind_process_config_phase2_advanced.json` | Phase2 ingestion/process config |
| `dataset/alphamind_retrieval_config_phase2_advanced.json` | Phase2 retrieval config |
| `dataset/phase2_eval_queries.json` | Phase2 固定评测问题 |
| `dataset/phase2_ground_truth.json` | Phase2 strict ground truth |
| `scripts/alphamind_phase2_quality_eval.py` | Phase2 retrieval/chat evaluator |
| `scripts/alphamind_phase2_quality_gate.py` | 一键 strict quality gate |
| `tests/test_alphamind_phase2_quality_eval.py` | evaluator 单元测试 |
| `tests/test_alphamind_phase2_quality_gate.py` | quality gate 单元测试 |

## 4. 信捷电气样本映射

本轮补齐了信捷电气三篇研报，用于 `p2_q001_company_report_fact` 和 `p2_q004_cross_doc_view` 从 placeholder 升级为 complete。

| 报告 | doc_id | knowledge_id | 状态 |
| --- | --- | --- | --- |
| `2025-07-02-华泰证券-信捷电气(603416.SH)PLC筑牢工控基本盘，人形布局加速.pdf` | `alphamind_2025_07_02_华泰证券_信捷电气_603416_SH_PLC筑牢工控基本盘_人形布局加速_pdf_a51e9c0f4fa1` | `e9837a96-7500-4fee-b71e-39a1491f220e` | success |
| `2025-08-05-国金证券-信捷电气(603416.SH)小型PLC龙头行稳致远，新品类&机器人多级驱动.pdf` | `alphamind_2025_08_05_国金证券_信捷电气_603416_SH_小型PLC龙头行稳致远_新品类_机器人多级驱动_pdf_e536152e95ec` | `9e21dcc5-0ca8-4221-8d0c-204f8b93412c` | success |
| `2026-02-27-银河证券-信捷电气(603416.SH)首次覆盖报告：工控基本盘扎实，加速布局具身智能.pdf` | `alphamind_2026_02_27_银河证券_信捷电气_603416_SH_首次覆盖报告_工控基本盘扎实_加速布局具身智能_pdf_b2c1aedc210b` | `ea9e5199-cc86-407e-b6e5-2e8135b0c972` | success |

来源记录：

```text
dataset/phase2_runs/alphamind_ingest_20260704-184730.csv
```

## 5. Strict quality gate 复现命令

推荐使用一键脚本：

```bash
python scripts/alphamind_phase2_quality_gate.py --chat-timeout 180
```

脚本内部执行：

```bash
python scripts/alphamind_healthcheck.py --phase phase2-advanced --env-file .env.phase2-advanced --require-qdrant --require-neo4j --require-minio
python scripts/alphamind_phase2_quality_eval.py --validate-only --strict --env-file .env.phase2-advanced
python scripts/alphamind_phase2_quality_eval.py --mode retrieval --strict --env-file .env.phase2-advanced --out dataset/phase2_runs/phase2_retrieval_eval_latest.json
python scripts/alphamind_phase2_quality_eval.py --mode chat --strict --env-file .env.phase2-advanced --chat-timeout 180 --out dataset/phase2_runs/phase2_chat_eval_latest.json
python -m unittest discover -s tests -p 'test_alphamind_phase2_quality*.py' -v
```

## 6. 最新验收结果

### 6.1 Healthcheck

```text
required failures=0, warnings=0, total=21
```

### 6.2 Strict validate-only

```text
setup=pass ground_truth=complete quality=pass
```

### 6.3 Strict retrieval evaluator

```text
setup=pass
ground_truth=complete
quality=pass
mean_recall@10=0.889
mean_reciprocal_rank=1.000
```

### 6.4 Strict chat evaluator

```text
setup=pass
ground_truth=complete
quality=pass
chat_reference_rate=1.000
mean_citation_doc_recall=1.000
answer_must_contain_pass_rate=1.000
no_evidence_refusal_rate=1.000
no_evidence_fabrication_violation_rate=0.000
```

### 6.5 单元测试

```text
tests/test_alphamind_phase2_quality_eval.py: 8 passed
tests/test_alphamind_phase2_quality_gate.py: 4 passed
combined unittest discovery: 12 passed
```

## 7. 已知限制与注意事项

1. **在线模型账号状态会影响门禁**
   - retrieval/chat 依赖远端 LLM/Embedding/Rerank 服务。
   - 若 DashScope 或兼容模型服务返回 `Arrearage`、`Access denied`、`insufficient_quota`，quality gate 会将失败归类为 `model_account_error`。

2. **CSV 表格问答存在上下文噪声**
   - 当前 strict gate 已覆盖核心字段和值级 evidence。
   - 后续若扩样到多 CSV/XLSX，建议增加表格专用 query 与字段精确匹配规则。

3. **图谱 smoke 是连通与关系类型层面的验收**
   - WeKnora 当前图谱可能使用 KB-specific ENTITY labels。
   - Phase2 不硬要求固定 `Company` / `Report` / `Broker` label 计数非零。

## 8. 下一阶段建议

Phase2 封版后建议进入 Pilot 扩样：

1. 扩展 20-50 篇 PDF 研报样本。
2. 增加 3-5 个 CSV/XLSX 表格样本。
3. 增加 15-30 条固定评测问题。
4. 将 quality gate 接入 CI 或本地 pre-push 流程。
5. 对 no-evidence、错公司、错年份、错指标查询增加更多负例。
