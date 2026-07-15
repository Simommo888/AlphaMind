# AlphaMind 可交付加固 Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** 将现有 Phase3 高精度开发成果整理为客户可自行启停、验收、备份恢复和维护的准生产交付包。

**Architecture:** 保留现有 WeKnora 三层 Compose、Phase3 PDF/Graph/Hybrid 脚本和真实数据；新增一个跨平台 Python 交付控制器，统一 preflight/start/stop/status/logs/backup/verify-backup/restore/acceptance。所有危险恢复操作必须显式确认并失败关闭。交付文档逐项映射《金融系统报价单(终版)》，硬件无法满足的 35B/OCR 项标为条件项，不伪造完成。

**Tech Stack:** Python 3 标准库、unittest、Docker Compose、Neo4j、Qdrant、MinIO、PostgreSQL、WeKnora。

---

### Task 1: 交付验收矩阵

**Files:**
- Create: `docs/ALPHAMIND_DELIVERY_ACCEPTANCE_MATRIX.md`
- Create: `delivery/manifest.json`

**Steps:**
1. 将报价单方案三逐条映射到代码、命令、产物和验收阈值。
2. 明确 PASS/PARTIAL/CONDITIONAL，禁止把 8GB GPU 上的 0.6B 模型冒充 35B。
3. 记录当前 Windows 实测和 Ubuntu 24.04 Compose 兼容边界。

### Task 2: 跨平台交付控制器

**Files:**
- Create: `scripts/alphamind_delivery.py`
- Create: `tests/test_alphamind_delivery.py`

**Steps:**
1. RED：测试 Compose 命令、环境占位密钥检查、持久卷筛选、备份清单校验、恢复确认。
2. GREEN：实现 `preflight/start/stop/status/logs/backup/verify-backup/restore/acceptance`。
3. 所有 Docker 参数数组化；恢复要求备份名二次确认；任一步失败不得删除原数据。
4. 运行定向测试和真实 `preflight/status`。

### Task 3: 冷备份与恢复演练材料

**Files:**
- Modify: `scripts/alphamind_delivery.py`
- Create: `docs/ALPHAMIND_BACKUP_RESTORE.md`

**Steps:**
1. 备份 Postgres/Qdrant/Neo4j/MinIO/data-files 持久卷，排除 Redis、日志和临时卷。
2. 生成带 SHA-256、卷名、容器目标、时间戳的 manifest。
3. 实际执行一次备份并 `verify-backup`；恢复只做安全 dry-run，避免覆盖当前有效数据。

### Task 4: 一键验收与准生产报告

**Files:**
- Modify: `scripts/alphamind_delivery.py`
- Create: `docs/ALPHAMIND_DELIVERY_HANDOFF.md`
- Generate: `dataset/delivery/alphamind_delivery_acceptance_latest.json`

**Steps:**
1. 串联 Compose 配置、健康检查、82+ 单测、复杂 PDF、Neo4j 关系图、9 条 Cypher、负载探针。
2. 报告每个门禁真实命令、退出码、耗时和输出摘要。
3. 真实运行非破坏验收；任何硬门禁失败则总状态 FAIL。

### Task 5: 最终交付清单与复审

**Files:**
- Create: `delivery/README.md`
- Update: `docs/ALPHAMIND_PHASE3_PRECISION_RUNBOOK.md`
- Update: `docs/ALPHAMIND_PHASE3_ACCEPTANCE_REPORT.md`

**Steps:**
1. 写客户首次安装、日常启停、上传查询、Neo4j 查看、故障排查、备份恢复。
2. 更新真实测试数、图谱数和当前端口。
3. 运行全套测试、`py_compile`、`git diff --check`、Compose config、服务健康检查。
4. 独立规格与代码质量复审；无 P0/P1 才标记可交付。
