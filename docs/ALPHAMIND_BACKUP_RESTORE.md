# AlphaMind 备份与恢复手册

## 1. 备份范围

交付控制器只备份业务持久数据：

| 服务 | 容器目标 | 内容 |
|---|---|---|
| PostgreSQL | `/var/lib/postgresql/data` | 用户、知识库、文档元数据、系统配置 |
| Qdrant | `/qdrant/storage` | 向量集合与索引 |
| Neo4j | `/data` | Graph RAG 和 AlphaMind 关系图 |
| MinIO | `/data` | 上传的原始文件和对象存储 |
| WeKnora app | `/data/files` | 本地文件存储 |

明确排除：

- Redis `/data`：队列/缓存，可重建；
- Neo4j `/logs`：日志；
- docreader `/tmp/docreader`：临时产物。

## 2. 冷备份

冷备份会短暂停止 Phase3 容器，避免跨数据库时间点不一致；完成或失败后都应恢复服务。

```bash
cd D:/AlphaMind
python scripts/alphamind_delivery.py backup
```

备份默认写入仓库外的 `../AlphaMind-backups/alphamind-<UTC时间戳>/`，包含：

- 每个持久卷的 `.tar.gz`；
- `manifest.json`；
- 每个归档的 SHA-256；
- 原卷名、容器、挂载目标和创建时间。

备份完成后必须校验：

```bash
python scripts/alphamind_delivery.py verify-backup ../AlphaMind-backups/alphamind-<UTC时间戳>
```

只有所有哈希一致时才可把备份复制到异机或离线介质。

## 3. 非破坏恢复演练

交付控制器不会直接覆盖当前持久卷。它先完成 SHA-256、固定归档名和五类数据完整性校验；默认仅打印计划，只有显式传入 `--execute-staging` 才会写入全新 staging 卷。当前业务卷始终保持不变。

必须满足：

1. 目标 AlphaMind/WeKnora 版本与备份清单兼容。
2. `verify-backup` 为 PASS。
3. 明确输入备份目录名作为二次确认。
4. staging 卷不存在；已存在时脚本失败关闭，防止混入旧数据。

先预览计划：

```bash
python scripts/alphamind_delivery.py restore \
  ../AlphaMind-backups/alphamind-<UTC时间戳> \
  --confirm-restore alphamind-<UTC时间戳>
```

再执行非破坏 staging 恢复：

```bash
python scripts/alphamind_delivery.py restore \
  ../AlphaMind-backups/alphamind-<UTC时间戳> \
  --confirm-restore alphamind-<UTC时间戳> \
  --execute-staging
```

恢复只写入 `alphamind-restore-*` 新卷，不删除、不清空、不挂载当前业务卷。随后运行隔离应用级验证：

```bash
python scripts/alphamind_delivery.py validate-restore alphamind-<UTC时间戳>
```

该命令使用 `docker-compose.restore-validation.yml` 启动独立项目、独立容器名和 `28080` 端口；应用必须在恢复后的 PostgreSQL/Qdrant/Neo4j/MinIO 卷上通过 `/health`，然后自动停止隔离容器并删除隔离临时卷。五个 staging 数据卷和当前生产卷都不会被删除。

真正切换生产卷仍必须在维护窗口审核 Compose 覆盖文件后执行。

## 4. 生产切换后验证

以下命令只能在运维人员已把经验证的 staging 卷挂入隔离 Compose 或完成正式切换后执行；不能用当前活动栈的结果代替恢复栈验收。

```bash
python scripts/alphamind_delivery.py status
python scripts/alphamind_healthcheck.py \
  --phase phase3-precision \
  --env-file .env.phase3-precision \
  --require-qdrant --require-neo4j --require-minio
```

还需人工验证：

1. WeKnora 可登录；
2. 知识库和文档数量正确；
3. 随机文档状态为 `completed`；
4. 固定金融问题能够命中引用；
5. Neo4j 节点、关系和约束存在；
6. MinIO 原始文件可访问。

## 5. 灾难恢复原则

- 禁止执行 `docker compose down -v`。
- 禁止在未校验归档哈希时恢复。
- 禁止只恢复 PostgreSQL 而忽略 Qdrant/Neo4j/MinIO 的时间点一致性。
- 恢复失败时保留失败现场和原归档，不自动删除备份。
- 生产建议至少保留“每日 7 份 + 每周 4 份 + 每月 3 份”，并把至少一份放在不同磁盘。

## 6. 备份介质安全

- 备份辅助镜像固定为不可变 digest：`busybox@sha256:73aaf090f3d85aa34ee199857f03fa3a95c8ede2ffd4cc2cdb5b94e566b11662`。
- 默认目录位于仓库外，禁止复制进 Git 工作树。
- `.tar.gz` 只压缩、不加密；离机传输前必须使用 BitLocker、LUKS、加密移动介质或客户批准的加密归档。
- 备份目录仅授予运维账户和备份服务账户访问权限。
- 传输后必须重新运行 `verify-backup`，不能只检查文件大小。
