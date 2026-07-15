# AlphaMind 终极高精版交付验收矩阵

依据：`C:/Users/59521/Desktop/金融系统报价单(终版).pdf`，方案三“终极高精版”。

状态定义：

- `PASS`：当前机器已真实执行并有可复现产物。
- `PARTIAL`：核心链路可用，但仍有交付/覆盖缺口。
- `CONDITIONAL`：受客户硬件、外部服务或目标 OS 限制，当前环境不能诚实标为完成。
- `FAIL`：硬门禁失败，禁止交付。

## 1. 报价单逐项映射

| 报价单要求 | 状态 | 当前实现/证据 | 交付门禁 |
|---|---|---|---|
| Ubuntu 24.04 Desktop | CONDITIONAL | Compose 与 Python 脚本为跨平台；当前实测宿主为 Windows 10 + Docker Desktop，尚未在客户 Ubuntu 主机实机复验 | 客户 Ubuntu 到位后执行 `preflight/start/acceptance`，不得把 Windows 验证冒充 Ubuntu 验证 |
| CUDA 调优（方案一继承项） | CONDITIONAL | 当前仅完成 Windows 10 + Docker Desktop、8GB GPU、Embedding 4B / Reranker 0.6B 共享模型服务基线；未在客户目标 GPU/驱动/CUDA 组合上调优 | 客户 Ubuntu 目标机到位后记录 GPU/驱动/CUDA 版本、显存与并发参数，复跑内容和负载门禁 |
| WeKnora 私有部署 | PASS | 三层 Compose 已运行，`http://localhost:18080/health` 返回 OK，前端 `18088` | app、frontend、postgres、redis、docreader 全部正常 |
| 本地聊天大模型 | CONDITIONAL | 已按客户最新说明把默认配置切换为 vLLM `qwen3-14b`（`LLM_BASE_URL=http://host.docker.internal:8000/v1`）；当前仓库不再把旧 Ollama `qwen3:4b` 当作目标配置 | 客户 Ubuntu/GPU 目标机启动 vLLM `qwen3-14b` 后，执行 `/v1/models`、真实问答、固定在线评测与质量门禁 |
| Qwen3 embedding/reranker | CONDITIONAL（仅 Embedding 型号） | 当前共享服务已验证 `qwen3-embedding-4b-local` / `qwen3-reranker-0.6b-local`；Reranker 已与报价单 0.6B 对齐，Embedding 与报价单 8B 仍有偏差 | 当前 4B/0.6B 基线可交付；若合同要求精确 Embedding 8B，需目标硬件到位后切换并复跑 |
| 图数据库 Neo4j | PASS | `17474/17687`，1077 个 AlphaMind 节点；板块—行业—产业链—企业关系可视化 | 节点/边计数、证据字段、9 条 Cypher 全通过 |
| Markdown/MHTML 接入 | PASS | `alphamind_bulk_ingest.py` Phase1/2/3 规则及真实 KB 上传链路 | dry-run + 真实小样本上传 |
| 复杂金融 PDF | PASS（文本层 PDF） | `alphamind_phase3_pdf.py`；卓胜微 178 页、300 原始表、10 跨页表、256 脚注、数值保留率 1.0 | `--strict --require-table --min-numeric-preservation 0.98` |
| 扫描件/OCR/极端无框表格 | CONDITIONAL | 可接 `DOCREADER_ODL_HYBRID_URL`；当前未配置 MinerU/OpenDataLab 兼容视觉端点 | 未配置时必须明确“不支持 OCR”，不得静默伪成功 |
| 金融专属 Graph RAG Schema | PASS | Company/Organization/Institution/Executive/Report/FinancialMetric/Evidence + Sector/Industry/ChainStage | schema/config 门禁与参数化导入 |
| 复杂多跳逻辑 | PASS（受控意图） | 供应链交叉、机构—高管、指标跨期；另有 Neo4j 板块产业链企业链 | 未知 intent、空条件、无证据均 fail-closed |
| Dense + Sparse + Graph 混合检索 | PASS | 加权 RRF，权重和为 1，去重融合 | 单测 + 真实 hybrid search |
| 金融词典 | PASS | `dataset/alphamind_financial_lexicon_phase3.json` | 唯一 canonical、>=10 条、扩展测试 |
| 动态语义分块 | PASS | auto + 标题/自然段/表格边界 + parent-child 配置 | Phase3 process config 校验 |
| 防幻觉/引用 | PASS（验收问题） | 内容硬断言、`[S1..Sn]` 引用、找不到证据即失败 | 引用越界、数值错误、无证据均 FAIL |
| 召回率反复打磨 | PARTIAL | 固定评测集和 Recall/MRR 门槛已存在；需在客户最终私有语料导入后重跑 | Recall@10 >=0.90、MRR >=0.90、citation recall >=0.95 |
| 准生产压测 | PARTIAL | 健康端点 40 请求/并发8通过；真实检索 POST 压测能力已有，但需客户最终 KB 请求体复跑 | error <=1%、p95 <=3000ms |
| 客户数据脱敏 | CONDITIONAL | 当前交付 inventory 不包含客户企业微信/内部私有语料，故不能宣称客户数据已脱敏 | 客户传输前完成核心商业信息与个人信息脱敏，登记审批记录及脱敏后数据集 SHA-256；数据未到位时保持条件项 |
| 运维移交 | IN PROGRESS | Phase3 runbook 已有；正在补一键控制器、冷备份/恢复、交付清单 | 客户可独立启停、查看状态、备份、校验备份、恢复演练 |
| 1 个月咨询服务 | PROCESS | 属于商务服务承诺，不是代码功能 | 交付单记录起止日期、问题渠道与响应边界 |

## 2. 当前真实运行基线

- WeKnora UI：`http://localhost:18088`
- Backend：`http://localhost:18080`
- Qdrant REST：`http://localhost:16333`
- Neo4j Browser：`http://localhost:17474`
- Neo4j Bolt：`neo4j://localhost:17687`
- MinIO：`http://localhost:19000`
- MinIO Console：`http://localhost:19001`
- 目标 LLM：vLLM `qwen3-14b`（客户机 `http://127.0.0.1:8000/v1`；容器内 `http://host.docker.internal:8000/v1`）
- 当前 embedding：`qwen3-embedding-4b-local`
- 当前 reranker：`qwen3-reranker-0.6b-local`

## 3. 已完成硬门禁

- 82 项 AlphaMind 自动测试：PASS。
- 真实复杂 PDF 完整解析：PASS。
- WeKnora 真实上传、解析、检索、引用答案：已有 PASS 报告。
- Neo4j 关系图：1077 节点、1743 关系；413 条企业关系的 trace/quote 缺失数均为 0。
- 文档内 9 条 Neo4j Cypher：9/9 执行无错误。
- Docker 核心服务健康：PASS。

## 3.1 可复现发布证据

- 生成：`python scripts/alphamind_release_manifest.py generate`。
- 校验：`python scripts/alphamind_release_manifest.py verify`，任一缺失/遗漏文件、文件 SHA-256、聚合 hash、验收证据或 Git 状态不一致均失败。
- `delivery/manifest.json` 记录全部交付文件的原始字节 SHA-256 与大小，以及 Git commit SHA/dirty 状态。
- `configuration_sha256` 覆盖 env example、三层 Compose、builtin models、process/retrieval/KB 配置。
- `dataset_sha256` 覆盖金融词典与固定评测集；验收报告另有 `acceptance_evidence_sha256`。
- `acceptance.run_id` 优先使用报告自身 run ID；当前旧格式报告由其 SHA-256 确定性派生，避免伪造随机 ID。
- 依赖锚点：`delivery/requirements-phase3.txt`、`go.mod`/`go.sum`、`frontend/package.json`/`package-lock.json`，以及 manifest 中的 Docker/模型端点要求。

## 4. 交付前剩余硬门禁

1. 一键交付控制器完成并在当前环境真实执行 `preflight/status/acceptance`。
2. 每次候选交付冻结后重新生成并校验 `delivery/manifest.json`，客户复核 Git/文件/配置/数据集/验收 hash。
3. 生成一次真实冷备份并通过 SHA-256 校验；恢复仅在隔离环境演练，禁止直接覆盖当前有效卷。
4. 客户最终私有语料完成脱敏审批后重跑在线 Recall/MRR/citation 门禁，并登记脱敏后数据集 hash。
5. 客户 Ubuntu 目标机完成 CUDA 调优；若需关闭 `qwen3-14b` 替代项、恢复报价单原 35B 或增加扫描件 OCR，必须提供相应 GPU/视觉解析端点并重新执行条件项验收。

## 5. 可交付判定

当前判定：`READY WITH CONDITIONS / 当前硬件基线可交付`。

已满足：

- 非破坏性一键验收报告总状态 PASS；
- 117/117 自动测试 PASS；
- 5 类持久数据冷备份与 SHA-256 校验 PASS；
- 5 类数据非破坏 staging 恢复演练 PASS；
- 当前环境无已知 P0/P1 运行故障。

以下条件项不计入当前 PASS，必须由客户书面接受边界，或在目标硬件/数据上补测通过：Ubuntu 24.04、CUDA 调优、客户机 vLLM `qwen3-14b` 终验、报价单精确 embedding/reranker 型号、扫描件 OCR、客户数据脱敏审批、最终私有语料指标。
