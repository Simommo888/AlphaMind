# AlphaMind 客户验收签字清单

项目：金融垂直领域 Graph RAG 系统（方案三：终极高精版）

## A. 当前环境硬门禁

- [ ] `python scripts/alphamind_release_manifest.py verify` 返回 PASS，并登记 manifest 中 Git SHA、dirty 状态、配置/数据集 hash 和验收 run ID
- [ ] `python scripts/alphamind_delivery.py preflight` 返回 PASS
- [ ] `python scripts/alphamind_delivery.py status` 显示核心容器正常
- [ ] WeKnora Web UI 可登录
- [ ] 复杂年报解析数值保留率 >= 0.98
- [ ] Dense/Sparse/Graph 三路检索均返回结果
- [ ] 固定数值问题回答正确并带有效来源引用
- [ ] 无证据问题能够拒答
- [ ] Neo4j 可查看板块—行业—产业链—企业关系
- [ ] 业务关系可回跳源文件、行号和原文
- [ ] 40 请求/并发 4 错误率 <= 1%、p95 <= 3000ms
- [ ] 冷备份生成并通过 SHA-256 校验
- [ ] 隔离环境恢复演练通过

## B. 条件项确认

请客户选择：

- Ubuntu 24.04：
  - [ ] 已在客户目标机复验通过
  - [ ] 接受当前 Windows + Docker Desktop 基线，目标机复验后补签
- vLLM `qwen3-14b` 本地模型：
  - [ ] 客户目标机已用 vLLM 启动 `qwen3-14b`，`/v1/models` 返回模型 id
  - [ ] 已用该模型复跑固定在线评测、引用答案和质量门禁
  - [ ] 确认报价单原 35B 聊天模型按客户说明替换为 `qwen3-14b`
- Embedding/Reranker 精确型号：
  - [ ] 已按报价单切换 embedding 8B / reranker 0.6 并复验通过
  - [ ] Reranker 0.6B 已按报价模型复验；接受当前 Embedding 4B 基线，Embedding 8B 作为目标硬件到位后的条件项
- 扫描件 OCR：
  - [ ] 已配置 MinerU/OpenDataLab 兼容端点并复验通过
  - [ ] 当前仅验收带文本层 PDF
- 最终私有语料：
  - [ ] 已导入并通过 Recall/MRR/citation 门禁
  - [ ] 当前使用代表性年报样本验收，私有语料到位后补测
- CUDA 调优（方案一继承项）：
  - [ ] 已在客户 Ubuntu 24.04 目标机按实际 GPU/驱动/CUDA 组合完成调优并复跑验收
  - [ ] 接受当前 Windows 10 + Docker Desktop 8GB GPU 基线；目标机 CUDA 调优状态为 `CONDITIONAL`
- 客户数据脱敏：
  - [ ] 客户确认拟交付的企业微信/内部文档已完成核心商业信息与个人信息脱敏，并登记审批人与脱敏后数据集 hash
  - [ ] 本次未提供客户私有语料；接受脱敏验收状态为 `CONDITIONAL`，数据到位后补签（不得勾选“已脱敏”）

## C. 运维移交

- [ ] 客户已收到 `.env.phase3-precision.example`
- [ ] 客户已收到 `delivery/manifest.json`、`delivery/requirements-phase3.txt` 和生成/校验脚本
- [ ] 客户已独立执行清单校验，并核对所有文件 SHA-256、Git SHA/dirty、配置 hash、数据集 hash、验收 run ID
- [ ] 私有 `.env.phase3-precision` 已安全移交且未进入 Git
- [ ] 客户已演示独立启动、停止、状态和日志查看
- [ ] 客户已演示文档上传、等待解析和问答引用检查
- [ ] 客户已收到备份与恢复手册
- [ ] 已登记 1 个月咨询服务起止日期与问题渠道

## D. 签字

客户代表：__________________

开发方代表：________________

验收日期：__________________

备注：____________________________________________________________
