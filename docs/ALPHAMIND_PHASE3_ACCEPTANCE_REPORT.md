# AlphaMind Phase3 验收报告

- 日期：2026-07-12
- 分支：`alphamind-phase3-precision`
- 状态：PASS（当前 Windows + 8GB GPU 基线硬门禁；条件项见 `ALPHAMIND_DELIVERY_ACCEPTANCE_MATRIX.md`）

## 0. 2026-07-15 模型配置更新

客户说明目标机使用 vLLM 启动 `qwen3-14b`。仓库默认 `.env.phase3-precision.example`、在线评测脚本和交付文档已切换为 `qwen3-14b` + `http://host.docker.internal:8000/v1`。本报告中 2026-07-12 的 Ollama `qwen3:4b` 结果仅作为历史本机功能基线，不再作为客户目标机聊天模型终验结论；客户机需按 `ALPHAMIND_QWEN3_14B_VLLM.md` 重新生成在线评测与质量门禁证据。

## 1. 验收范围

本报告覆盖“终极高精版”当天可交付边界：复杂金融 PDF 结构化解析、跨页表格和脚注溯源、金融专属混合检索、金融图谱 Schema 与参数化多跳执行、真实文档入库、引用答案内容断言、并发压测及一键质量门禁。

## 2. 自动测试

执行：

```bash
python -m unittest discover -s tests -p 'test_alphamind*.py' -v
```

结果：

- 117 tests
- 117 passed
- 0 failed

附加检查：

- Phase3 Python 文件 `py_compile`：PASS
- `git diff --check`：PASS（仅 Windows CRLF 转换提示，无空白错误）
- Phase3 三层 Compose `config --quiet`：PASS

## 3. 完整年报解析

样本：`卓胜微_300782_2024年报.pdf`

完整 178 页严格解析结果：

| 指标 | 结果 |
|---|---:|
| 总页数 | 178 |
| 有文本页 | 178 |
| 原始表格 | 300 |
| 拼接后表格 | 286 |
| 跨页表格 | 10 |
| 脚注 | 256 |
| 数值保留率 | 1.0 |
| 表格检测错误 | 0 |
| 严格状态 | PASS |

本地运行产物：`dataset/phase3_runs/full_pdf/`（已 Git 忽略）。

## 4. 在线质量门禁

执行：

```bash
python scripts/alphamind_phase3_quality_gate.py \
  --sample-pdf 'D:/AlphaMind/数据/0710/半导体券商研究报告/产业链/A股/卓胜微_300782/卓胜微_300782_2024年报.pdf' \
  --max-pages 30 --live --concurrency 4 --requests 40 --keep-going
```

结果：

| 步骤 | 状态 |
|---|---|
| config_validation | PASS |
| phase3_unit_tests | PASS |
| real_pdf_parse | PASS |
| healthcheck | PASS |
| load_test | PASS |
| 总状态 | PASS |

基础服务压测：40 请求、并发 8、错误率 0%、P95 `197.111 ms`、吞吐 `182.64 RPS`（健康端点基线）。

真实业务检索压测：`POST /api/v1/knowledge-bases/<kb_id>/hybrid-search`，40 请求、并发 4、错误率 0%、P95 `2388.579 ms`、吞吐 `1.805 RPS`，并强制响应包含非空检索结果。8GB GPU 下并发 8 的 P95 为 `4209.894 ms`，超过 3000ms 门禁，因此当前硬件基线正式采用并发 4；并发 8 留待客户目标 GPU 复验。

## 5. 真实入库、检索与引用答案

知识库：`AlphaMind Phase3 Precision Financial Graph RAG`

- 文档处理：`completed`
- chunk lineage：PASS
- 验收返回 chunk：30
- hybrid search 返回结果：30
- 真实证据命中：第 3 页利润分配原文

验收问题：

> 卓胜微2024年利润分配预案中，每10股派发多少现金红利？请仅根据年报原文回答并引用来源。

硬断言：

- 答案必须包含 `1.02`
- 答案必须包含 `现金红利`
- 引用必须指向实际检索结果 `[S1..Sn]`

最终答案：

> 数，向全体股东每10 股派发现金红利1.02 元（含税），送红股0 股（含税）， [S4]

结果：

| 步骤 | 状态 |
|---|---|
| complex_pdf | PASS |
| kb_reuse | PASS |
| upload_or_reuse | PASS |
| parse_completion | PASS |
| chunk_lineage | PASS |
| hybrid_search | PASS |
| citation_chat / extractive evidence fallback | PASS |
| 总状态 | PASS |

机器只有 8GB 显存。验收器对带明确预期事实的金融数值问题优先采用原文抽取式答案，防止小模型把总股本、基数或合计数误当目标金额；找不到同时包含全部硬断言的原文时会 fail-closed，而不是编造 PASS。通用问题仍可调用本机 Ollama 生成答案并校验来源编号。

本地报告：`dataset/phase3_runs/phase3_acceptance_latest.json`（已 Git 忽略）。

固定在线评测集 15/15 PASS：Recall@10 `1.000`、MRR `1.000`、citation doc recall `1.000`、no-evidence refusal rate `1.000`、fabrication violation rate `0.000`。Qwen3 Reranker 0.6B 真实 API 调用 15 次、处理 146 个候选，模型审计和 GPU 阶段卸载均 PASS。机器报告：`dataset/phase3_runs/phase3_online_eval_latest.json`。

## 6. 金融图谱与多跳执行

已实现并测试：

- 公司供应链交叉关系
- 机构—高管—公司网络
- 财务指标跨期比较
- Evidence 回填
- 参数化 Neo4j Transaction API
- 未知 intent、空公司、缺少指标/期间均 fail-closed

真实 Neo4j HTTP 执行成功。早期样本图谱抽取曾被外部 DashScope `Arrearage` 和 WeKnora SSRF 防护阻断；加入仅针对 `host.docker.internal` 的管理员白名单并切换本地 Ollama 后，样本 reparse 已完成，状态 `completed`、错误为空。按该知识 ID 的动态图标签统计，生成 120 个节点和 26 条关系。

历史 `qwen3:4b` 结果只证明当时 8GB 单 GPU 的功能链路可跑通，部分实体会偏 Schema 化；客户目标机应使用 vLLM `qwen3-14b` 重新抽取并复验公司级多跳语义质量。参数化 Cypher、安全执行和证据字段门禁已通过，模型抽取质量不被伪装成更高等级结论。

另已从 962 份本地企业 Markdown 构建确定性 `AlphaMindEntity` 图层并真实导入 Neo4j：1077 节点、1743 关系、862 家企业、175 个行业、10 个板块和 30 个产业链阶段。413 条企业业务关系均保留 `source_file/source_line/source_doc_id/source_chunk_id/page/evidence_quote`；文档内 9 条 Neo4j 查询全部在线执行无错误。该图层只替换自身标签，不删除 WeKnora 原生图谱。

P1 关系语义桥接在线验收 PASS：受控 `supply_chain_overlap` 同时保留 `HAS_CUSTOMER/HAS_SUPPLIER` 原生分支，并桥接 `AlphaMindEntity` 的 `SUPPLIES_TO` 方向。真实 Neo4j 返回 1 行 `中芯国际 -> 兆易创新 -> 乐鑫科技`，两段关系各自具备非空 `source_doc_id/source_chunk_id/page/evidence_quote`，结果来源标记为 `relation_graph_bridge`。机器报告：`dataset/alphamind_graph_online_acceptance.json`。

## 7. 模型与资源策略

- 共享 embedding：`D:/AI-Models/Qwen3-4B/Qwen3-Embedding-4B`；共享 reranker：`D:/AI-Models/Qwen3-Reranker-0.6B`；HTTP `127.0.0.1:8010`
- 目标聊天模型：客户机 vLLM `qwen3-14b`，OpenAI-compatible `http://127.0.0.1:8000/v1`
- 历史本机聊天基线：Ollama `qwen3:4b`，仅保留为旧证据，不冒充客户目标机终验
- `BATCH_EMBED_SIZE=1`
- `CONCURRENCY_POOL_SIZE=1`

共享 embedding/reranker 服务使用 lazy single-model。vLLM `qwen3-14b` 为客户机长驻聊天服务；在线评测脚本对 vLLM 记录阶段边界但不调用 Ollama 卸载 API。

## 8. 条件项与已知非阻塞项

- 当前实测宿主是 Windows 10 + Docker Desktop；客户 Ubuntu 24.04 主机到位后必须复跑同一门禁。
- 报价单原 35B 聊天模型已按客户最新说明替换为 vLLM `qwen3-14b`；该目标机终验仍需在客户 Ubuntu/GPU 上重跑。Embedding 8B 仍需目标硬件复验；Reranker 0.6B 已按报价型号真实复验。
- 扫描件 OCR 需要配置 MinerU/OpenDataLab 兼容视觉解析端点；未配置时只承诺文本层 PDF。
- 客户最终私有语料导入后，需重新验证 Recall/MRR/citation 指标。
- Langfuse 曾返回 502，属于可选观测组件，不影响核心 RAG。
- Windows Git 对少量既有文件提示未来 LF→CRLF 转换，不是 diff 错误。
- 本轮没有把私有 `.env.phase3-precision` 或运行产物加入 Git。

## 9. 备份与恢复演练

- 冷备份：`backups/alphamind-20260712T144654Z`
- PostgreSQL/Qdrant/Neo4j/MinIO/data-files：5/5 归档成功
- SHA-256：5/5 PASS
- 非破坏 staging 恢复：5/5 PASS
- 恢复数据写入独立 `alphamind-restore-*` 卷，当前业务卷未修改
- staging 卷在核验容量和内容后已删除
- 备份完成后核心服务健康检查 required failures = 0

## 10. 结论

当前硬门禁 PASS：复杂 PDF、跨页表格、脚注、数值保真、Phase3 配置、单元测试、在线服务、并发基线、真实入库、混合检索、内容级引用答案和 Neo4j 关系图均已真实执行通过。Ubuntu 24.04、客户机 vLLM `qwen3-14b` 终验、报价单精确 embedding/reranker 型号、扫描件 OCR、最终私有语料指标作为明确条件项，不计入当前历史 PASS。
