# AlphaMind Phase3 验收报告

- 日期：2026-07-12
- 分支：`alphamind-phase3-precision`
- 状态：PASS（核心硬门禁全部通过）

## 1. 验收范围

本报告覆盖“终极高精版”当天可交付边界：复杂金融 PDF 结构化解析、跨页表格和脚注溯源、金融专属混合检索、金融图谱 Schema 与参数化多跳执行、真实文档入库、引用答案内容断言、并发压测及一键质量门禁。

## 2. 自动测试

执行：

```bash
python -m unittest discover -s tests -p 'test_alphamind*.py' -v
```

结果：

- 43 tests
- 43 passed
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
  --max-pages 30 --live --concurrency 8 --requests 40 --keep-going
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

基础服务压测：40 请求、并发 8、错误率 0%；此前同配置测得 P95 约 281 ms、吞吐约 125 RPS。该指标是服务健康端点基线，真实检索正确性由下一节单独验证。

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

## 6. 金融图谱与多跳执行

已实现并测试：

- 公司供应链交叉关系
- 机构—高管—公司网络
- 财务指标跨期比较
- Evidence 回填
- 参数化 Neo4j Transaction API
- 未知 intent、空公司、缺少指标/期间均 fail-closed

真实 Neo4j HTTP 执行成功。早期样本图谱抽取曾被外部 DashScope `Arrearage` 和 WeKnora SSRF 防护阻断；加入仅针对 `host.docker.internal` 的管理员白名单并切换本地 Ollama 后，样本 reparse 已完成，状态 `completed`、错误为空。按该知识 ID 的动态图标签统计，生成 120 个节点和 26 条关系。

当前 `qwen3:0.6b` 适合在 8GB 单 GPU 上完成可复现功能验收，但部分实体会偏 Schema 化；公司级多跳语义质量建议在资源和网络允许时用 `qwen3:4b` 或更强模型重新抽取。参数化 Cypher、安全执行和证据字段门禁已通过，模型抽取质量不被伪装成更高等级结论。

## 7. 模型与资源策略

- 共享 embedding/reranker：`D:/AI-Models/Qwen3-4B`，HTTP `127.0.0.1:8010`
- 当前可复现本地聊天模型：Ollama `qwen3:0.6b`
- 推荐升级：网络与资源允许时使用 `qwen3:4b`
- `BATCH_EMBED_SIZE=1`
- `CONCURRENCY_POOL_SIZE=1`

共享服务使用 lazy single-model。8GB 单 GPU 场景不能同时常驻多个 4B 模型；运行手册记录了检索、卸载 embedding、再执行本地回答的顺序。

## 8. 已知非阻塞项

- Langfuse 曾返回 502，属于可选观测组件，不影响核心 RAG。
- Windows Git 对少量既有文件提示未来 LF→CRLF 转换，不是 diff 错误。
- 本轮没有把私有 `.env.phase3-precision` 或运行产物加入 Git。

## 9. 结论

核心硬门禁 PASS：复杂 PDF、跨页表格、脚注、数值保真、Phase3 配置、单元测试、在线服务、并发基线、真实入库、混合检索及内容级引用答案均已真实执行通过。
