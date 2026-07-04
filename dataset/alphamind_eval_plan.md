# AlphaMind RAG 评测计划

> RAG 方案没有评测数字只能算假设。本计划固定 smoke/pilot/full 三档评测集，并记录每次只改一个变量后的指标变化。

## 1. 当前基线

已跑工具输出：

- Chunking: `dataset/alphamind_chunking_eval_latest.json`
- Pipeline design: `dataset/alphamind_design_latest.json`
- Retrieval baseline: `dataset/alphamind_retrieval_eval_baseline.json`

当前检索基线基于 4 篇 `.txt` 样本和 TF-IDF evaluator：

| 指标 | 当前值 | MVP 目标 |
|---|---:|---:|
| Precision@1 | 0.333 | >= 0.60 |
| Precision@5 | 0.611 | >= 0.75 |
| Recall@5 | 1.000 | 保持 >= 0.85 |
| Recall@10 | 1.000 | >= 0.85 |
| MRR | 0.667 | >= 0.80 |
| NDCG@5 | 0.754 | >= 0.80 |

解释：召回能找到，但首位排序不足；这通常需要 metadata filter、hybrid、rerank 和解析噪声清理共同解决。

## 2. Chunking 观察

`chunking_optimizer.py` 在 4 篇解析后 `.txt` 样本上推荐 `fixed_size_char`，但样例 chunk 大量出现页眉/页脚/联系方式/版面噪声。因此落地策略不是直接固定 fixed-size，而是：

1. 优先改善 PDF 解析质量。
2. WeKnora 知识库继续用 `auto + parent-child`。
3. 对研报正文保守使用 `chunk_size=1000, overlap=150, parent=4096, child=512`。
4. 后续用真实 WeKnora 检索评测决定是否调小 chunk 或关闭 parent-child。

## 3. Smoke 评测集（5-10 篇）

当前 smoke query 已绑定到 Phase2 小样本文档，而不是占位公司：

- Query: `dataset/alphamind_eval_queries.json`
- Ground truth: `dataset/alphamind_ground_truth_template.json`
- 离线 smoke evaluator: `scripts/alphamind_smoke_retrieval_eval.py`
- 在线 WeKnora evaluator: `scripts/alphamind_online_retrieval_eval.py`
- 最新离线结果: `dataset/alphamind_smoke_retrieval_eval_latest.json`
- 最新在线结果: `dataset/alphamind_online_retrieval_eval_latest.json`

运行命令：

```powershell
python scripts\alphamind_smoke_retrieval_eval.py `
  --queries dataset\alphamind_eval_queries.json `
  --ground-truth dataset\alphamind_ground_truth_template.json `
  --corpus-root "D:\AlphaMind\数据\数据库\Documents" `
  --out dataset\alphamind_smoke_retrieval_eval_latest.json
```

当前 smoke 覆盖：

| 类别 | 示例 |
|---|---|
| 财务指标 | “德龙激光 2026E EPS 预测是多少？” |
| 收入/利润预测 | “德龙激光 2026E 收入和归母净利润预测是多少？” |
| 行业市场规模 | “超快激光市场规模从 2018 到 2024 的 CAGR 是多少？” |
| 技术主题 | “M8/M9 PCB 与 TGV 在封装材料报告中的关系是什么？” |
| 表格查找 | “平安证券 EPS 预测表覆盖哪些股票代码？” |
| CSV 结构化表 | “乔尔格林布拉特的神奇公式策略类型、2019 年涨跌幅和 Sharp 比率是多少？” |
| 多文档推理 | “AI PCB / M9 PCB 相关需求在德龙激光和华泰封装材料报告中有哪些线索？” |
| 实体查找 | “2019 华泰超快激光报告提到哪些海外超快激光厂商或品牌？” |

最新离线结果：`validation=pass`、`Precision@1=1.000`、`Recall@5=1.000`、`Recall@10=1.000`、`MRR=1.000`。`Precision@5=0.225` 是因为当前离线语料只有 5 个文档且每题相关文档通常只有 1-2 个；后续用于对比 rerank/top-k 变化，不单独作为 smoke 阻断项。

在线 WeKnora 评测命令：

```powershell
python scripts\alphamind_online_retrieval_eval.py `
  --env-file .env.phase2-advanced `
  --queries dataset\alphamind_eval_queries.json `
  --ground-truth dataset\alphamind_ground_truth_template.json `
  --retrieval-config dataset\alphamind_retrieval_config_phase2_advanced.json `
  --kb-id-file dataset\alphamind_phase2_advanced_kb_id.txt `
  --out dataset\alphamind_online_retrieval_eval_latest.json
```

2026-07-04 在线 smoke 运行记录：

- Corpus: Phase2 KB `1d608de5-98d6-4a5d-858b-e1d85730531c`，合并 `phase2_acceptance_*.json` 与 `phase2_runs/alphamind_ingest_*.csv` 建立 `knowledge_id -> doc_id` 映射。
- Changed variable: 补齐在线 KB 中缺失的两篇 smoke PDF；`alphamind_bulk_ingest.py` 增加 `--include-name` 用于精确选择文件，避免按目录顺序误上传大批文档。
- 补传样本：
  - 德龙激光：`56cf7c1c-6b7b-4696-8c83-6ae679af7829`
  - 华泰封装材料：`0a08e10a-13d1-426f-a6a2-76ee6f408071`
- Metrics:
  - Setup: `pass`
  - Quality: `warn`
  - Precision@1: `0.750`
  - Precision@5: `0.225`
  - Recall@5: `1.000`
  - Recall@10: `1.000`
  - MRR: `0.875`
  - NDCG@5: `0.908`
  - Mean latency: `341ms`
- Observations:
  - 两篇补传 PDF 在 knowledge API 中仍显示 `parse_status=finalizing`，但 hybrid search 已可召回并用于指标计算。
  - `q_smoke_002_delong_revenue_profit` 缺少 `1,076` 的 must_contain 命中，`q_smoke_007_ai_pcb_multi_doc` 缺少 `AI PCB` 命中；说明召回到文档但 chunk 内容不一定覆盖精确字段，是下一轮 chunk/context enrichment/rerank 调优输入。
- Decision: keep as online smoke baseline；下一轮优先调 `match_count`、`skip_context_enrichment`、`keyword_threshold` 与 query expansion，而不是继续扩大语料。

## 4. Pilot 评测集（100 篇）

至少 50 个 query，覆盖：

1. 10 个公司近期观点/分歧。
2. 10 个 EPS/收入/毛利率/ROE 指标查询。
3. 8 个目标价变化与原因。
4. 8 个行业供需/景气度总结。
5. 6 个券商假设对比。
6. 4 个风险因素聚合。
7. 4 个表格计算。

每个 query 标注：

```json
{
  "id": "q_pilot_001",
  "text": "...",
  "expected_filters": {"company": "...", "ticker": "...", "period": "..."},
  "relevant": [
    {"doc_id": "...", "chunk_ids": ["..."], "must_contain": ["..."]}
  ]
}
```

## 5. 放行标准

| 阶段 | Precision@5 | Recall@10 | Citation accuracy | 备注 |
|---|---:|---:|---:|---|
| Smoke | >= 0.70 | >= 0.80 | >= 0.85 | 可手工抽查 |
| Pilot | >= 0.75 | >= 0.85 | >= 0.90 | 允许进入 1000 篇 |
| Full | >= 0.80 | >= 0.88 | >= 0.92 | 上线候选 |

## 6. 每轮只改一个变量

建议顺序：

1. 解析：docreader/OCR/表格抽取。
2. chunk：size/overlap/parent-child。
3. embedding 层级或维度。
4. dense only vs hybrid。
5. metadata filter 强制程度。
6. RRF 权重。
7. rerank top_k/threshold。
8. GraphRAG filter/boost。

## 7. 运行记录模板

```markdown
## YYYY-MM-DD run-N

- Corpus: 10 / 100 / 1000 / full
- Changed variable: rerank_top_k 15 -> 30
- Config files:
  - process_config: dataset/alphamind_process_config_research_report.json
  - retrieval_config: dataset/alphamind_retrieval_config_recommended.json
- Metrics:
  - Precision@5:
  - Recall@10:
  - MRR:
  - Citation accuracy:
- Failure cases:
  - qid:
  - expected:
  - retrieved:
  - suspected cause:
- Decision: keep / revert / try next variable
```

## 8. Phase2 Quality Evaluator

Phase2 系统验收通过后，用独立质量评测脚本量化最终答案质量，而不是只看 hybrid-search 是否返回结果。

- 脚本：`scripts/alphamind_phase2_quality_eval.py`
- Query：`dataset/phase2_eval_queries.json`
- Ground truth：`dataset/phase2_ground_truth.json`
- 默认输出：`dataset/phase2_runs/phase2_quality_eval_latest.json`

职责边界：

- `scripts/alphamind_phase2_acceptance.py`：验证 WeKnora、Qdrant、MinIO、Neo4j、上传、解析、chunks、chat smoke 和 graph smoke 是否可用。
- `scripts/alphamind_phase2_quality_eval.py`：验证 retrieval recall、knowledge-chat references、citation doc coverage、answer must/must-not checks，以及 no-evidence 防幻觉。

只做 schema 验证，不调用在线服务，也不写报告：

```powershell
python scripts\alphamind_phase2_quality_eval.py `
  --queries dataset\phase2_eval_queries.json `
  --ground-truth dataset\phase2_ground_truth.json `
  --validate-only `
  --json
```

只跑 retrieval 质量评测：

```powershell
python scripts\alphamind_phase2_quality_eval.py `
  --mode retrieval `
  --queries dataset\phase2_eval_queries.json `
  --ground-truth dataset\phase2_ground_truth.json `
  --retrieval-config dataset\alphamind_retrieval_config_phase2_advanced.json `
  --out dataset\phase2_runs\phase2_retrieval_eval_latest.json `
  --json
```

只跑 knowledge-chat / references 质量评测：

```powershell
python scripts\alphamind_phase2_quality_eval.py `
  --mode chat `
  --queries dataset\phase2_eval_queries.json `
  --ground-truth dataset\phase2_ground_truth.json `
  --out dataset\phase2_runs\phase2_chat_eval_latest.json `
  --json
```

完整 Phase2 quality 评测：

```powershell
python scripts\alphamind_phase2_quality_eval.py `
  --queries dataset\phase2_eval_queries.json `
  --ground-truth dataset\phase2_ground_truth.json `
  --retrieval-config dataset\alphamind_retrieval_config_phase2_advanced.json `
  --out dataset\phase2_runs\phase2_quality_eval_latest.json `
  --json
```

指标口径：

- Retrieval：文档级 `Recall@10`、MRR、NDCG；tiny corpus 下 `Precision@5` 只作趋势指标，不作 smoke 硬阻断。
- Chat references：`chat_reference_rate`、`mean_distinct_reference_doc_ids`。
- Citation：`citation_doc_recall`、`citation_doc_precision`、`min_distinct_doc_ids_pass_rate`。
- Answer checks：`answer_must_contain_pass_rate`、`answer_must_not_violation_rate`。
- No-evidence：`no_evidence_refusal_rate`、`no_evidence_fabrication_violation_rate`。

`dataset/phase2_ground_truth.json` 支持 `complete` / `partial` / `placeholder` / `no_evidence` 状态。placeholder query 可运行但不进入硬质量分母；`--strict` 下 placeholder/partial 会让 quality status fail。当前信捷电气相关 query 因 Phase2 KB 尚未固定对应样本，保持 placeholder，避免用未验证 doc_id 造数。
