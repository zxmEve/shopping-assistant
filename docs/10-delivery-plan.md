# 里程碑与交付计划

更新时间：2026-09-04。逐条实时状态以 `08-requirements-traceability.md` 为准。

## 1. 交付规则

每个纵向切片必须同时包含：需求 ID、业务不变量、ADR、实现、失败测试、场景评测、文档状态更新和学习导读。没有验收证据不得标记为 `VERIFIED`。

开发循环：

```text
需求场景 -> 业务不变量 -> 架构决策 -> 失败模式
-> 最小实现 -> 自动化评测 -> 设计复盘 -> 状态更新
```

## 2. 里程碑

| 里程碑 | 交付目标 | 当前状态 | 退出条件 |
|---|---|---|---|
| M0 项目基线 | 需求追踪、ADR、诚实 README、场景映射、CI | 已完成 | 状态和证据可从需求追到测试 |
| M1 完整 P0 | IAM、主动追问、商品归一、HITL、最小生产 Harness | 进行中 | 38 条 P0 全部 VERIFIED，五类 Demo 可重复 |
| M2 真正 Agent 工具链 | 强类型 function tools、多轮 Orchestrator、真实模型评测 | 未开始 | 工具选择/参数/权限/成本有稳定基线 |
| M3 持久化与恢复 | PostgreSQL、状态版本、上下文压缩、必要时 Temporal | 未开始 | 重启和并发故障可恢复 |
| M4 长期记忆与 RAG | Profile/Team/Episodic/Semantic、consent/edit/delete | 未开始 | 记忆准确、安全、可删除且不越租户 |
| M5 完整评测与自进化 | 60/180 场景、holdout、shadow/canary/rollback | 未开始 | 候选版本不能绕过 veto 和人工批准 |
| M6 面试交付 | 六套 Demo、架构材料、失败复盘、讲解稿 | 未开始 | 一键演示，所有数字可由报告复现 |

## 3. M1 执行队列

| 顺序 | 切片 | 状态 |
|---:|---|---|
| 1 | 需求追踪、文档边界和 CI | 已完成 |
| 2 | 报价快照、旧 Token、写前重校验和单进程并发幂等 | 已完成 |
| 3 | Fixture Session、Tool Registry 和角色 allowlist | 已完成 |
| 4 | 信息价值追问、公司标准授权和三轮停止 | 已完成 |
| 5 | Product/Variant/Offer 真正归一、冲突字段和置信度 | 已完成 |
| 6 | 状态迁移表、budgets、timeout/retry/circuit | 已完成最小纵向切片 |
| 7 | checkpoint/resume/cancel 和 cassette replay | 下一项 |
| 8 | 逐事实 evidence、trade-off UI 和五套 P0 Demo | 待开始 |

## 4. 版本与发布门禁

- 金额、权限、租户、Policy、旧 Token、重复草稿和 Prompt Injection 为 veto；
- Fixture 和 live-model 指标分开报告；
- 每个指标名称必须与 grader 真正检查的行为一致；
- P0 完成前不把长期记忆、自进化和 live provider 写成当前能力；
- 当前 Git 根目录尚无初始提交且包含多个项目，创建提交、Tag 或发布前需先确认仓库边界。
