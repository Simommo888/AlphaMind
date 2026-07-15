# AlphaMind 交付偏差与条件验收登记

本登记用于避免把当前硬件基线误写成《金融系统报价单(终版)》方案三的精确规格完成。只有客户签字接受偏差，或在目标环境补测 PASS 后，相关项目才能关闭。

| ID | 报价要求 | 当前实况 | 状态 | 关闭条件 |
|---|---|---|---|---|
| D-001 | Ubuntu 24.04 Desktop | 当前主验收环境为 Windows 10 + Docker Desktop；已提供 Ubuntu 手册、systemd、host-gateway、digest overlay | CONDITIONAL | 客户 Ubuntu 24.04 目标机执行 preflight/start/acceptance 并归档报告 |
| D-002 | 本地 Qwen 35B | 客户最新说明改为 vLLM `qwen3-14b`；仓库默认配置已切换，旧 `qwen3:4b` 仅是历史本机基线 | CUSTOMER-UPDATED/CONDITIONAL | 客户目标机启动 vLLM `qwen3-14b` 后复跑在线评测、质量门禁和交付 manifest |
| D-003 | Qwen3 Embedding 8B | 当前共享服务为 Embedding 4B | DEVIATION | 切换报价型号并复验，或书面接受 4B |
| D-004 | Qwen3 Reranker 0.6B | 已部署 `Qwen3-Reranker-0.6B`，固定评测真实调用 15 次、146 候选 | CLOSED/PASS | 已关闭 |
| D-005 | 扫描件 OCR | 文本层复杂 PDF 已通过；OCR 视觉端点尚未在客户环境配置 | CONDITIONAL | 配置 MinerU/OpenDataLoader 兼容端点并用扫描样本复验 |
| D-006 | 客户最终私有语料 | 当前使用代表性年报和固定评测集 | CONDITIONAL | 导入最终语料并重跑 Recall/MRR/citation/no-evidence/压测 |
| D-007 | 客户数据脱敏 | 客户企业微信/内部文档尚未提供 | CONDITIONAL | 客户确认脱敏规则、抽检样本并签字 |
| D-008 | GPU/CUDA 调优 | 当前只完成 RTX 4060 Laptop 8GB 基线 | CONDITIONAL | 在目标 GPU 记录驱动、CUDA、显存峰值、batch、吞吐和稳定性 |

## 签字

- 客户是否接受 D-002/D-003/D-004 当前模型偏差：是 / 否
- 客户是否接受 D-001/D-005/D-006/D-007/D-008 作为目标环境条件项：是 / 否
- 客户代表：
- 实施方代表：
- 日期：
- 备注：

未签署时，交付状态只能是 `READY WITH DEVIATIONS / NOT FINAL CONTRACT ACCEPTANCE`。
