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

人工从 smoke 文档中标注 10-20 个 query：

| 类别 | 示例 |
|---|---|
| 公司观点 | “这篇报告的核心投资逻辑是什么？请引用原文。” |
| 指标查询 | “公司 2026E EPS/收入/净利润预测是多少？” |
| 目标价/评级 | “该报告给出的目标价和评级是什么，理由是什么？” |
| 风险因素 | “报告列出的主要风险因素有哪些？” |
| 表格计算 | “2024A 到 2026E 收入 CAGR 是多少？” |
| 多文档比较 | “不同券商对同一公司预测差异来自哪些假设？” |

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
